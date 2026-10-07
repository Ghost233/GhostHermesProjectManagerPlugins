"""Maintenance through authenticated public HTTP and original Owner group entry."""
import json

import pytest

from ghost_hermes_pm import Manager
from ghost_hermes_pm.messages import FeishuEntry
from maintenance_fixture_host import MaintenanceHost
from test_directory import OWNER, make_repo, registration
from test_feishu_entry import CONFIG, Gateway, Transport, event


def register(manager, repo):
    manager.apply_directory_change(OWNER, 0, registration(repo))
    manager.apply_directory_change(OWNER, 1, {'profile': {
        'id': 'steward', 'native_profile': 'steward', 'identity_ref': 'fixture:steward',
        'role': 'steward', 'capability': 'non_development', 'project_id': None,
        'parent_profile_id': None, 'connection_refs': {}}})


def group_entry(manager):
    adapter, transport = object(), Transport()
    settings = {**CONFIG, 'bindings': [{**CONFIG['bindings'][0], 'profile_id': 'steward', 'project_id': None}]}
    entry = FeishuEntry(lambda: manager, OWNER.subject, settings, lambda url: None)
    entry.attach_transport(adapter, transport)
    return entry, Gateway(adapter), transport


def approval(manager, operation_id, **extra):
    snapshot = manager.read_snapshot(OWNER)
    return {'operation_id': operation_id, 'expected_version': snapshot['version'],
            'expected_profile_ids': sorted(p['id'] for p in snapshot['profiles']), **extra}


@pytest.mark.asyncio
async def test_verified_owner_group_enters_exact_reviewed_maintenance_plan_and_replies_at_original_anchor(tmp_path):
    host = MaintenanceHost(tmp_path / 'host')
    with Manager(tmp_path / 'state', owner_identity_ref=OWNER.subject, maintenance_host=host) as manager:
        register(manager, make_repo(tmp_path / 'repo'))
        entry, gateway, transport = group_entry(manager)
        details = approval(manager, 'group-maintenance', expected_release=host.release)
        incoming = event('@_user_1 进入维护 ' + json.dumps(details), 'om_maintenance')
        assert await entry.receive(incoming, gateway) == {'action': 'skip'}
        snapshot = manager.read_snapshot(OWNER)
        plan = snapshot['maintenance']['plans'][0]
        assert (snapshot['maintenance']['mode'], plan['id'], plan['approved_scope'], plan['owner_origin']) == (
            'maintenance', 'group-maintenance', {'expected_version': 2, 'expected_profile_ids': ['mono-lead', 'steward']},
            {'subject': OWNER.subject, 'source': 'verified-feishu-owner-entry'})
        assert transport.sent[0]['reply_to'] == 'om_maintenance'
        assert 'group-maintenance' in transport.sent[0]['text']
        assert 'maintenance' in transport.sent[0]['text']
        assert gateway.authorized_sources == [incoming.source]


@pytest.mark.asyncio
async def test_owner_group_checks_checkpoint_switch_failure_rollback_and_reenable_original_plan(tmp_path):
    host = MaintenanceHost(tmp_path / 'host')
    host.fail_switch = True
    with Manager(tmp_path / 'state', owner_identity_ref=OWNER.subject, maintenance_host=host) as manager:
        register(manager, make_repo(tmp_path / 'repo'))
        entry, gateway, transport = group_entry(manager)
        operation_id = 'group-upgrade'
        details = approval(manager, operation_id, expected_release=dict(host.release),
                           target_release={'id': 'release-v2', 'plugin_version': '0.2.0', 'source_digest': 'c' * 64})
        async def send(command, details, message_id):
            assert await entry.receive(event('@_user_1 ' + command + ' ' + json.dumps(details), message_id), gateway) == {'action': 'skip'}
            return manager.read_snapshot(OWNER)['maintenance']['plans'][0]
        await send('进入维护', details, 'om_enter')
        checked = await send('核对维护', {'operation_id': operation_id}, 'om_check')
        assert checked['status'] == 'handoff_verified'
        checkpoint = await send('建立维护检查点', {'operation_id': operation_id}, 'om_checkpoint')
        assert checkpoint['status'] == 'checkpoint_verified'
        switched = await send('切换维护版本', approval(manager, operation_id), 'om_switch')
        assert (switched['status'], switched['switch_request']['status']) == ('switch_failed', 'accepted')
        assert 'switch_failed' in transport.sent[-1]['text']
        restored = await send('回退维护', approval(manager, operation_id), 'om_restore')
        assert restored['status'] == 'rollback_verified'
        assert restored['restore']['old_tasks_started'] is False
        await send('重新启用', approval(manager, operation_id), 'om_reenable')
        snapshot = manager.read_snapshot(OWNER)
        assert (snapshot['maintenance']['mode'], snapshot['maintenance']['plans'][0]['status'], host.effects) == (
            'active', 'reenabled', ['switch', 'restore'])


@pytest.mark.asyncio
async def test_owner_group_deactivation_waits_for_explicit_manual_handling_and_keeps_observation_read_only(tmp_path):
    from test_manual_observation import READ_ONLY, manual_state, observer, source
    repo, peer = make_repo(tmp_path / 'repo'), tmp_path / 'manual'
    manual_state(peer, repo)
    host = MaintenanceHost(tmp_path / 'host')
    with Manager(tmp_path / 'state', owner_identity_ref=OWNER.subject, maintenance_host=host,
                 observation_adapters={'local:manual-daemon': observer(peer)}) as manager:
        register(manager, repo)
        manager.register_observation_source(OWNER, source())
        entry, gateway, transport = group_entry(manager)
        details = approval(manager, 'group-disable', expected_release=host.release)
        assert await entry.receive(event('@_user_1 主动停用 ' + json.dumps(details), 'om_disable'), gateway) == {'action': 'skip'}
        plan = manager.read_snapshot(OWNER)['maintenance']['plans'][0]
        assert plan['status'] == 'blocked'
        session_id = plan['checks']['manual'][0]['session_id']
        assert 'Owner' in transport.sent[-1]['text']
        manual_state(peer, repo, 'idle')
        assert await entry.receive(event('@_user_1 核对维护 ' + json.dumps({'operation_id': 'group-disable'}), 'om_unhandled'), gateway) == {'action': 'skip'}
        assert manager.read_snapshot(OWNER)['maintenance']['plans'][0]['status'] == 'blocked'
        handled = {'operation_id': 'group-disable', 'handled_manual_session_ids': [session_id]}
        assert await entry.receive(event('@_user_1 核对维护 ' + json.dumps(handled), 'om_handled'), gateway) == {'action': 'skip'}
        snapshot = manager.read_snapshot(OWNER)
        assert (snapshot['maintenance']['mode'], snapshot['maintenance']['plans'][0]['status']) == ('disabled', 'deactivated')
        assert all(json.loads(line)['method'] in READ_ONLY for line in (peer / 'manual-wire.jsonl').read_text().splitlines())
