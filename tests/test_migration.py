"""Selective migration through the authenticated Owner management boundary."""
import copy
import hashlib
import json
import os
import subprocess
import sys
from pathlib import Path
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


def test_native_fresh_profile_stays_parked_and_imports_only_owner_selected_material(tmp_path):
    from ghost_hermes_pm.native_migration import NativeMigrationHost
    sdk = Path(os.environ['HERMES_TEST_SDK_ROOT'])
    native = tmp_path / 'native'
    native.mkdir()
    source = native / 'profiles' / 'mono-lead'
    (source / 'memories').mkdir(parents=True)
    (source / 'memories' / 'MEMORY.md').write_text('Retry only definite failures.\n\nDo not transfer this unrelated history.')
    (source / 'SOUL.md').write_text('A careful project steward.\n\nPrivate unrelated persona notes.')
    (source / '.env').write_text('OLD_TEST_CREDENTIAL=synthetic-only\n')
    (source / 'config.yaml').write_text('model: old-model\nproviders:\n  old: {base_url: https://invalid.example}\n')
    original = {p.name: p.read_bytes() for p in (source / 'SOUL.md', source / '.env', source / 'config.yaml')}
    host = NativeMigrationHost(native, tmp_path / 'work', sdk_root=sdk)
    with Manager(tmp_path / 'state', owner_identity_ref=OWNER.subject, migration_host=host) as manager:
        setup(manager, tmp_path)
        with ManagementServer(manager, {'owner': OWNER}):
            owner = ManagementClient(tmp_path / 'state', 'owner')
            preview = owner.migrate_profile('preview', {'source_profile_id': 'mono-lead'})
            details = proposal(owner)
            details['selection'] = [{**next(e for e in preview['materials'] if e['kind'] == kind), 'text': text}
                for kind, text in [('knowledge', 'Retry only definite failures.'), ('persona', 'A careful project steward.')]]
            planned = owner.migrate_profile('plan', details)
            prepared = owner.migrate_profile('prepare', {'plan_id': planned['id'], 'digest': planned['digest']})
            assert prepared['status'] == 'prepared', prepared
            assert prepared['native_state'] == 'parked_created'
            target = native / 'profiles' / 'new-lead'
            assert (target / 'gateway.parked').exists()
            assert 'Retry only definite failures.' in (target / 'memories' / 'MEMORY.md').read_text()
            assert 'unrelated' not in (target / 'memories' / 'MEMORY.md').read_text()
            assert (target / 'SOUL.md').read_text() == 'A careful project steward.'
            assert 'short progress updates' in (target / 'memories' / 'USER.md').read_text()
            assert 'OLD_TEST_CREDENTIAL' not in (target / '.env').read_text()
            assert 'old-model' not in (target / 'config.yaml').read_text()
            assert 'providers:' not in (target / 'config.yaml').read_text()
            assert all(e['status'] == 'written' for e in prepared['selection_ledger'])
            assert owner.migrate_profile('prepare', {'plan_id': planned['id'], 'digest': planned['digest']})['selection_ledger'] == prepared['selection_ledger']
            assert all(p.read_bytes() == original[p.name] for p in (source / 'SOUL.md', source / '.env', source / 'config.yaml'))


@pytest.mark.parametrize('decision', ['approve', 'deny'])
def test_pending_native_memory_is_not_claimed_written_or_retried_as_new_approval(tmp_path, decision):
    from ghost_hermes_pm.native_migration import NativeMigrationHost
    native = tmp_path / 'native'
    (native / 'profiles' / 'mono-lead' / 'memories').mkdir(parents=True)
    (native / 'profiles' / 'mono-lead' / 'memories' / 'MEMORY.md').write_text('Only approved knowledge.')
    (native / 'profiles' / 'mono-lead' / 'SOUL.md').write_text('New persona selected by Owner.')
    host = NativeMigrationHost(native, tmp_path / 'work', sdk_root=os.environ['HERMES_TEST_SDK_ROOT'], memory_write_approval=True)
    with Manager(tmp_path / 'state', owner_identity_ref=OWNER.subject, migration_host=host) as manager:
        setup(manager, tmp_path)
        with ManagementServer(manager, {'owner': OWNER}):
            owner = ManagementClient(tmp_path / 'state', 'owner')
            planned = owner.migrate_profile('plan', proposal(owner))
            key = {'plan_id': planned['id'], 'digest': planned['digest']}
            prepared = owner.migrate_profile('prepare', key)
            assert prepared['status'] == 'pending_approval', prepared
            assert prepared['selection_ledger'][0]['status'] == 'pending_approval'
            pending = prepared['selection_ledger'][0]['native_receipt']['pending_id']
            checked = owner.migrate_profile('check', key)
            assert checked['status'] == 'pending_approval' and checked['selection_ledger'][0]['native_receipt']['pending_id'] == pending
            assert not (native / 'profiles' / 'new-lead' / 'memories' / 'USER.md').exists()
            assert checked['switch_state'] == 'not_switched' and checked['native_state'] == 'parked_created'
            target = native / 'profiles' / 'new-lead'
            result = subprocess.run([sys.executable, str(Path(__file__).with_name('native_migration_probe.py')), decision, pending],
                env={'PATH': '/usr/bin:/bin', 'HOME': str(tmp_path / 'synthetic-home'), 'HERMES_HOME': str(target),
                     'PYTHONPATH': os.environ['HERMES_TEST_SDK_ROOT'], 'PYTHONDONTWRITEBYTECODE': '1'},
                cwd=tmp_path, text=True, capture_output=True, timeout=15)
            assert result.returncode == 0, result.stdout + result.stderr
            reviewed = owner.migrate_profile('check', key)
            assert reviewed['selection_ledger'][0]['status'] == ('written' if decision == 'approve' else 'rejected'), reviewed
            assert reviewed['status'] == ('prepared' if decision == 'approve' else 'blocked')
            assert reviewed['switch_state'] == 'not_switched'
