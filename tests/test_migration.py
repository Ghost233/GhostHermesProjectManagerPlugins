"""Selective migration through the authenticated Owner management boundary."""
import copy
import pytest

from ghost_hermes_pm import Manager, ManagementError, VerifiedIdentity
from ghost_hermes_pm.transport import ManagementClient, ManagementServer
from test_directory import OWNER, make_repo, registration


def setup(manager, root):
    manager.apply_directory_change(OWNER, 0, registration(make_repo(root / 'repo')))
    target = registration(root / 'repo')['profile']
    target.update(id='new-lead', native_profile='new-lead', identity_ref='fixture:new-lead',
                  connection_refs={'bot': 'identity:new-bot', 'credential': 'native:new-key', 'codex': 'local:new-executor'})
    manager.apply_directory_change(OWNER, 1, {'profile': target})


def proposal(client):
    snapshot = client.read_snapshot()
    return {'plan_id': 'selective-30', 'expected_version': snapshot['version'],
        'expected_profile_ids': ['mono-lead', 'new-lead'],
        'source_profile_id': 'mono-lead', 'target_profile_id': 'new-lead',
        'selection': [], 'preferences': [{'statement': 'Use short progress updates.', 'scope': {'kind': 'project', 'id': 'mono'}}],
        'execution': {'model': 'synthetic-model', 'provider': 'synthetic-provider', 'toolsets': ['memory']},
        'archive_source_ids': [], 'external_memory': {'kind': 'builtin'},
        'human_steps': ['Create the independent new bot and issue its credentials.']}


def test_owner_freezes_selection_and_new_identity_before_any_native_effect(tmp_path):
    with Manager(tmp_path / 'state', owner_identity_ref=OWNER.subject) as manager:
        setup(manager, tmp_path)
        with ManagementServer(manager, {'owner': OWNER, 'new': VerifiedIdentity('fixture:new-lead', 'participant')}):
            owner, participant = ManagementClient(tmp_path / 'state', 'owner'), ManagementClient(tmp_path / 'state', 'new')
            details = proposal(owner)
            with pytest.raises(ManagementError) as forbidden:
                participant.migrate_profile('plan', details)
            assert forbidden.value.code == 'forbidden'
            planned = owner.migrate_profile('plan', details)
            assert planned['status'] == 'planned' and planned['native_state'] == 'not_created'
            assert planned['approved_scope'] == {'expected_version': 2, 'expected_profile_ids': ['mono-lead', 'new-lead']}
            assert owner.migrate_profile('plan', details)['digest'] == planned['digest']
            altered = copy.deepcopy(details)
            altered['preferences'][0]['statement'] = 'Always choose blue.'
            with pytest.raises(ManagementError) as rebind:
                owner.migrate_profile('plan', altered)
            assert rebind.value.code == 'binding_conflict'
            prepared = owner.migrate_profile('prepare', {'plan_id': planned['id'], 'digest': planned['digest']})
            assert prepared['status'] == 'blocked' and prepared['checkpoint']['status'] == 'verified_manager_directory'
            assert prepared['native_state'] == 'not_created' and prepared['needs_human']
    with Manager(tmp_path / 'state', owner_identity_ref=OWNER.subject) as restarted:
        current = restarted.read_snapshot(OWNER)['migration_plans'][0]
        assert current['digest'] == planned['digest'] and current['plan'] == details
        assert current['rollback']['old_entry'] == 'not_started'
