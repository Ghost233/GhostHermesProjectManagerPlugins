"""Selective migration through the authenticated Owner management boundary."""
import copy
import hashlib
import json
import os
import subprocess
import sys
import shutil
import time
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
        'execution': {'model': 'synthetic-model', 'provider': 'custom', 'toolsets': ['memory']},
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


def test_actual_new_native_session_persists_selected_prompt_and_first_request_output(tmp_path):
    from ghost_hermes_pm.native_migration import NativeMigrationHost
    native = tmp_path / 'native'
    (native / 'profiles' / 'mono-lead' / 'memories').mkdir(parents=True)
    (native / 'profiles' / 'mono-lead' / 'memories' / 'MEMORY.md').write_text('Retry only definite failures.\n\nUnselected old history.')
    (native / 'profiles' / 'mono-lead' / 'SOUL.md').write_text('Careful new role.\n\nUnselected old persona.')
    host = NativeMigrationHost(native, tmp_path / 'work', sdk_root=os.environ['HERMES_TEST_SDK_ROOT'])
    with Manager(tmp_path / 'state', owner_identity_ref=OWNER.subject, migration_host=host) as manager:
        setup(manager, tmp_path)
        with ManagementServer(manager, {'owner': OWNER}):
            owner = ManagementClient(tmp_path / 'state', 'owner')
            details = proposal(owner)
            preview = owner.migrate_profile('preview', {'source_profile_id': 'mono-lead'})
            details['selection'] = [{**next(e for e in preview['materials'] if e['kind'] == kind), 'text': text}
                for kind, text in [('knowledge', 'Retry only definite failures.'), ('persona', 'Careful new role.')]]
            planned = owner.migrate_profile('plan', details)
            key = {'plan_id': planned['id'], 'digest': planned['digest']}
            owner.migrate_profile('prepare', key)
            session = 'new-native-session-30'
            target = native / 'profiles' / 'new-lead'
            plugin = target / 'plugins' / 'ghost-hermes-pm'
            plugin.mkdir(parents=True)
            root = Path(__file__).resolve().parents[1]
            for name in ('__init__.py', 'plugin.yaml', 'ghost_hermes_pm', 'dashboard'):
                if (root / name).is_dir():
                    shutil.copytree(root / name, plugin / name, ignore=shutil.ignore_patterns('__pycache__'))
                else:
                    shutil.copy2(root / name, plugin / name)
            result = subprocess.run([os.environ.get('HERMES_TEST_SESSION_PYTHON', sys.executable), str(Path(__file__).with_name('native_migration_session.py')), session],
                env={'PATH': '/usr/bin:/bin', 'HOME': str(tmp_path / 'synthetic-home'), 'HERMES_HOME': str(target), 'HERMES_SKIP_PM_BOOTSTRAP': '1',
                    'HERMES_DISABLE_PROJECT_PLUGINS': '1', 'PYTHONPATH': os.environ['HERMES_TEST_SDK_ROOT'], 'PYTHONDONTWRITEBYTECODE': '1'},
                cwd=tmp_path, text=True, capture_output=True, timeout=30)
            assert result.returncode == 0, result.stdout + result.stderr
            observed = json.loads(result.stdout.strip().splitlines()[-1])
            assert observed['native_first_request_output']
            checked = owner.migrate_profile('check', {**key, 'session_id': session})
            assert 'session_receipt' in checked, {'needs_human': checked['needs_human'], 'native_observed': observed}
            assert checked['session_receipt']['status'] == 'verified_persisted_prompt', checked
            assert checked['session_receipt']['native_profile'] == 'new-lead'
            assert checked['session_receipt']['request_output'] == 'observed'
            assert checked['session_receipt']['provider'] == 'custom'
            assert checked['session_receipt']['tool_names'] == ['memory']
            assert checked['session_receipt']['request_capture']['origin'] == 'actual_sdk_pre_api_request'
            assert checked['switch_state'] == 'not_switched' and (target / 'gateway.parked').exists()


def test_blocked_cutover_restores_reviewable_checkpoint_without_old_entry_or_repository_effects(tmp_path):
    from ghost_hermes_pm.native_migration import NativeMigrationHost
    native = tmp_path / 'native'
    (native / 'profiles' / 'mono-lead' / 'memories').mkdir(parents=True)
    (native / 'profiles' / 'mono-lead' / 'memories' / 'MEMORY.md').write_text('Preserve original history.')
    (native / 'profiles' / 'mono-lead' / 'SOUL.md').write_text('Original persona.')
    host = NativeMigrationHost(native, tmp_path / 'work', sdk_root=os.environ['HERMES_TEST_SDK_ROOT'])
    with Manager(tmp_path / 'state', owner_identity_ref=OWNER.subject, migration_host=host) as manager:
        setup(manager, tmp_path)
        (tmp_path / 'repo' / 'untracked.txt').write_text('Owner changes must survive.')
        with ManagementServer(manager, {'owner': OWNER}):
            owner = ManagementClient(tmp_path / 'state', 'owner')
            planned = owner.migrate_profile('plan', proposal(owner))
            key = {'plan_id': planned['id'], 'digest': planned['digest']}
            owner.migrate_profile('prepare', key)
            snapshot = owner.read_snapshot()
            activation = {**key, 'expected_version': snapshot['version'], 'expected_profile_ids': ['mono-lead', 'new-lead'],
                          'archive_operation_id': 'old-stop-not-proven', 'session_id': 'missing-native-session'}
            blocked = owner.migrate_profile('activate', activation)
            assert blocked['status'] == 'blocked' and blocked['switch_state'] == 'not_switched'
            restored = owner.migrate_profile('rollback', key)
            assert restored['status'] == 'rolled_back', restored
            assert restored['rollback']['checkpoint_data'] == 'verified_restored_artifact'
            assert restored['rollback']['target_control'] == 'parked'
            assert restored['rollback']['old_entry'] == 'not_started' and restored['rollback']['old_tasks'] == 'not_resumed'
            assert restored['rollback']['external_services'] == 'unverified'
            assert (tmp_path / 'repo' / 'untracked.txt').read_text() == 'Owner changes must survive.'
            assert (native / 'profiles' / 'mono-lead' / 'memories' / 'MEMORY.md').read_text() == 'Preserve original history.'
            assert (native / 'profiles' / 'new-lead' / 'gateway.parked').exists()


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


@pytest.mark.asyncio
async def test_dashboard_and_verified_owner_group_show_the_same_migration_state(tmp_path):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from ghost_hermes_pm.dashboard import create_router
    from ghost_hermes_pm.messages import FeishuEntry
    from test_feishu_entry import CONFIG, Gateway, Transport, event
    with Manager(tmp_path / 'state', owner_identity_ref=OWNER.subject) as manager:
        setup(manager, tmp_path)
        with ManagementServer(manager, {'owner': OWNER, 'new': VerifiedIdentity('fixture:new-lead', 'participant')}):
            client = ManagementClient(tmp_path / 'state', 'owner')
            app = FastAPI()
            app.include_router(create_router(lambda _: client))
            http = TestClient(app)
            planned = http.post('/migration', json={'action': 'plan', 'details': proposal(client)})
            assert planned.status_code == 200, planned.text
            plan = planned.json()
            adapter, transport = object(), Transport()
            intake = FeishuEntry(lambda: manager, OWNER.subject, CONFIG, lambda _: {})
            intake.attach_transport(adapter, transport)
            original = event('@_user_1 准备迁移 ' + plan['id'] + ' ' + plan['digest'], 'om_migration_prepare')
            assert await intake.receive(original, Gateway(adapter)) == {'action': 'skip'}
            current = http.get('/snapshot').json()['migration_plans'][0]
            assert current['status'] == 'blocked' and current['digest'] == plan['digest']
            assert len(transport.sent) == 1 and 'blocked' in transport.sent[0]['text'] and plan['digest'] in transport.sent[0]['text']
            assert transport.sent[0]['mention_open_id'] == 'ou_owner'
            assert transport.sent[0]['reply_to'] == 'om_migration_prepare'


def test_owner_bridge_waits_for_actual_native_preparation_receipt(tmp_path):
    from ghost_hermes_pm.native_migration import NativeMigrationHost
    class SlowNativeHost(NativeMigrationHost):
        def prepare(self, operation):
            time.sleep(3.2)
            return super().prepare(operation)
    native = tmp_path / 'native'
    (native / 'profiles' / 'mono-lead' / 'memories').mkdir(parents=True)
    (native / 'profiles' / 'mono-lead' / 'memories' / 'MEMORY.md').write_text('Bounded synthetic knowledge.')
    (native / 'profiles' / 'mono-lead' / 'SOUL.md').write_text('Bounded synthetic persona.')
    host = SlowNativeHost(native, tmp_path / 'work', sdk_root=os.environ['HERMES_TEST_SDK_ROOT'])
    with Manager(tmp_path / 'state', owner_identity_ref=OWNER.subject, migration_host=host) as manager:
        setup(manager, tmp_path)
        with ManagementServer(manager, {'owner': OWNER}):
            owner = ManagementClient(tmp_path / 'state', 'owner')
            planned = owner.migrate_profile('plan', proposal(owner))
            assert owner.migrate_profile('prepare', {'plan_id': planned['id'], 'digest': planned['digest']})['status'] == 'prepared'


def test_persona_selection_does_not_require_an_unselected_old_memory_file(tmp_path):
    from ghost_hermes_pm.native_migration import NativeMigrationHost
    native = tmp_path / 'native'
    source = native / 'profiles' / 'mono-lead'
    source.mkdir(parents=True)
    (source / 'SOUL.md').write_text('Selected new project persona.')
    host = NativeMigrationHost(native, tmp_path / 'work', sdk_root=os.environ['HERMES_TEST_SDK_ROOT'])
    with Manager(tmp_path / 'state', owner_identity_ref=OWNER.subject, migration_host=host) as manager:
        setup(manager, tmp_path)
        with ManagementServer(manager, {'owner': OWNER}):
            owner = ManagementClient(tmp_path / 'state', 'owner')
            preview = owner.migrate_profile('preview', {'source_profile_id': 'mono-lead'})
            assert [entry['kind'] for entry in preview['materials']] == ['persona']
            assert preview['missing_materials'] == ['memories/MEMORY.md']
            details = proposal(owner)
            details['selection'] = preview['materials']
            planned = owner.migrate_profile('plan', details)
            prepared = owner.migrate_profile('prepare', {'plan_id': planned['id'], 'digest': planned['digest']})
            assert prepared['status'] == 'prepared', prepared
            assert (native / 'profiles' / 'new-lead' / 'SOUL.md').read_text() == 'Selected new project persona.'
            assert not (source / 'memories' / 'MEMORY.md').exists()
