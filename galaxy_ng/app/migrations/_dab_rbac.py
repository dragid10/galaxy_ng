import logging

from django.apps import apps as global_apps

from rest_framework.exceptions import ValidationError

from ansible_base.rbac.migrations._utils import give_permissions
from ansible_base.rbac.validators import LocalValidators, combine_values
from ansible_base.rbac.management._old import create_dab_permissions as old_create_dab_permissions
from ansible_base.rbac import permission_registry


logger = logging.getLogger(__name__)


PULP_TO_ROLEDEF = {
    'galaxy.auditor': 'Platform Auditor',
}


ROLEDEF_TO_PULP = {
    'Platform Auditor': 'galaxy.auditor',
}


def pulp_role_to_single_content_type_or_none(pulprole):
    content_types = {perm.content_type for perm in pulprole.permissions.all()}
    if len(content_types) == 1:
        return next(iter(content_types))
    return None


def create_permissions_as_operation(apps, schema_editor):
    # NOTE: this is a snapshot version of the DAB RBAC permission creation logic
    # normally this runs post_migrate, but this exists to keep old logic
    for app_label in {'ansible', 'container', 'core', 'galaxy'}:
        app_config = global_apps.get_app_config(app_label)
        old_create_dab_permissions(app_config, apps=apps)

    print('FINISHED CREATING PERMISSIONS')


def split_pulp_roles(apps, schema_editor):
    '''
    For every user&group role that is tied to a specific content object,
    split the role out into a new single content type role with permissions
    that are only relevant to that content object. Afterwards, swap the
    [User|Group]Role's .role with the new role.
    '''
    Role = apps.get_model('core', 'Role')
    UserRole = apps.get_model('core', 'UserRole')
    GroupRole = apps.get_model('core', 'GroupRole')

    for corerole in Role.objects.all():
        split_roles = {}
        for assignment_cls in (UserRole, GroupRole):
            for pulp_assignment in assignment_cls.objects.filter(role=corerole, content_type__isnull=False):
                if pulp_assignment.content_type_id not in split_roles:

                    # Get all permissions relevant to this content model.
                    # If any model (like synclist) hasn't been registered in the permission
                    # system, it should not be split/recreated ...
                    cls = apps.get_model(pulp_assignment.content_type.app_label, pulp_assignment.content_type.model)
                    try:
                        ct_codenames = combine_values(LocalValidators.permissions_allowed_for_role(cls))
                    except ValidationError:
                        continue

                    # Make a new role for this special content model
                    new_data = {
                        'description': corerole.description,
                        'name': f'{corerole.name}_{pulp_assignment.content_type.model}'
                    }
                    new_role = Role(**new_data)
                    new_role.save()

                    # Add the necesarry permissions to the new role ...
                    for perm in pulp_assignment.role.permissions.all():
                        # The pulp role may have had permissions related to some other
                        # content model we're not interested in, so we will skip adding those.
                        if ct_codenames and perm.codename not in ct_codenames:
                            continue
                        new_role.permissions.add(perm)

                    split_roles[pulp_assignment.content_type_id] = new_role

                pulp_assignment.role = split_roles[pulp_assignment.content_type_id]
                pulp_assignment.save(update_fields=['role'])


def model_class(apps, ct):
    "Utility method because we can not count on method being available in migrations"
    return apps.get_model(ct.app_label, ct.model)


def copy_roles_to_role_definitions(apps, schema_editor):
    Role = apps.get_model('core', 'Role')
    DABPermission = apps.get_model('dab_rbac', 'DABPermission')
    RoleDefinition = apps.get_model('dab_rbac', 'RoleDefinition')
    try:
        DABContentType = apps.get_model('dab_rbac', 'DABContentType')
        logger.info('Running copy_roles_to_role_definitions with DAB RBAC post-remote-permission models')
    except LookupError:
        DABContentType = apps.get_model('contenttypes', 'ContentType')
        logger.info('Running copy_roles_to_role_definitions with DAB RBAC original model state')

    for corerole in Role.objects.all():
        dab_perms = []
        for perm in corerole.permissions.prefetch_related('content_type').all():
            ct = perm.content_type
            model_cls = model_class(apps, ct)
            if not permission_registry.is_registered(model_cls):
                continue
            dabct = DABContentType.objects.filter(model=ct.model, app_label=ct.app_label).first()
            if dabct is None:
                raise dabct.DoesNotExist(
                    f'Content type ({ct.app_label}, {ct.model}) for registered model {model_cls} not found as DABContentType'
                    f'\nexisting: {list(DABContentType.objects.values_list("model", flat=True))}'
                )
            dabperm = DABPermission.objects.filter(
                codename=perm.codename,
                content_type=dabct
            ).first()
            if dabperm:
                dab_perms.append(dabperm)

        if dab_perms:
            roledef_name = PULP_TO_ROLEDEF.get(corerole.name, corerole.name)
            content_type = pulp_role_to_single_content_type_or_none(corerole)
            dabct = None
            if content_type:
                dabct = DABContentType.objects.filter(model=content_type.model, app_label=content_type.app_label).first()
                if dabct is None:
                    raise dabct.DoesNotExist(
                        f'Content type ({content_type.app_label}, {content_type.model}) not found as DABContentType'
                        f'\nexisting: {list(DABContentType.objects.values_list("model", flat=True))}'
                    )
            roledef, created = RoleDefinition.objects.get_or_create(
                name=roledef_name,
                defaults={
                    'description': corerole.description or corerole.name,
                    'managed': corerole.locked,
                    'content_type': dabct,
                }
            )
            if created:
                print(f'CREATED RoleDefinition from {corerole} {corerole.name}')
                roledef.permissions.set(dab_perms)


def migrate_role_assignments(apps, schema_editor):
    UserRole = apps.get_model('core', 'UserRole')
    GroupRole = apps.get_model('core', 'GroupRole')
    Group = apps.get_model('auth', 'Group')
    Team = apps.get_model('galaxy', 'Team')
    RoleDefinition = apps.get_model('dab_rbac', 'RoleDefinition')
    RoleUserAssignment = apps.get_model('dab_rbac', 'RoleUserAssignment')
    RoleTeamAssignment = apps.get_model('dab_rbac', 'RoleTeamAssignment')

    # Migrate user role assignments
    for user_role in UserRole.objects.all():
        rd = RoleDefinition.objects.filter(name=user_role.role.name).first()
        if not rd:
            continue
        if not user_role.object_id:
            # system role
            RoleUserAssignment.objects.create(role_definition=rd, user=user_role.user)
        else:
            give_permissions(apps, rd, users=[user_role.user], object_id=user_role.object_id, content_type_id=user_role.content_type_id)

    # Migrate team/group role assignments
    for group_role in GroupRole.objects.all():
        rd = RoleDefinition.objects.filter(name=group_role.role.name).first()
        if not rd:
            continue

        actor = Team.objects.filter(group=group_role.group).first()
        if actor is None:
            continue

        if not group_role.object_id:
            RoleTeamAssignment.objects.create(role_definition=rd, team=actor)
        else:
            give_permissions(apps, rd, teams=[actor], object_id=group_role.object_id, content_type_id=group_role.content_type_id)

    # Create the local member role if it does not yet exist
    from ansible_base.rbac.permission_registry import permission_registry

    # galaxy_ng/app/settings.py - ANSIBLE_BASE_MANAGED_ROLE_REGISTRY
    rd_template = permission_registry.get_managed_role_constructor('team_member')
    member_rd, created = rd_template.get_or_create(apps)
    if created:
        logger.info(f'Created role definition {member_rd.name}')

    # In DAB RBAC, team users are saved as a role assignment
    # Migrate the pulp group users (relationship) to role assignment
    for group in Group.objects.prefetch_related('user_set').all():
        user_list = list(group.user_set.all())
        team = Team.objects.filter(group=group).first()
        if not team:
            logger.warning(f'Data migration could not find team by name {group.name}')
            continue

        if user_list:
            give_permissions(apps, member_rd, users=user_list, object_id=team.id, content_type_id=member_rd.content_type_id)
