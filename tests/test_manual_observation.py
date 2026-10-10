"""Observe registered original-service peers through the public credential bridge."""
import json
import sys
from pathlib import Path

import pytest

from ghost_hermes_pm import Manager, ManagementError
from readiness_support import ReadyManager as Manager
from ghost_hermes_pm.transport import ManagementClient, ManagementServer
from test_directory import OWNER, make_repo
from test_task_execution import accepted, adapter_for

READ_ONLY = {'fixture/connect', 'fixture/ready', 'fixture/list', 'fixture/loaded', 'fixture/read', 'fixture/background'}


def observer(root, kind='desktop', executor='synthetic-original', coverage='complete'):
    from normalized_executor_fixture import SyntheticReadOnlyAdapter
    root.mkdir(exist_ok=True)
    def verifier(binding):
        return {**binding, 'original_executor_id': executor, 'provenance': 'synthetic-original-service-peer',
                'supported_methods': list(READ_ONLY), 'source_kinds': [kind],
                'runtime_coverage': coverage, 'runtime_evidence': {'background_and_children': 'verified'},
                'evidence_ref': 'synthetic-host-binding'}
    return SyntheticReadOnlyAdapter([sys.executable, str(Path(__file__).with_name('manual_fixture_server.py')), str(root)],
        cwd=root, env={'PATH': '/usr/bin:/bin', 'FIXTURE_HOME': str(root / 'synthetic-home')},
        service_ref='local:manual-' + kind, source_kind=kind, endpoint_ref='local:registered-' + kind, verifier=verifier, timeout=1)


def source(kind='desktop'):
    return {'id': 'manual-' + kind, 'kind': kind, 'project_ids': ['mono'], 'adapter_ref': 'local:manual-' + kind}


def manual_state(root, repo, status='active', thread_id='manual-thread', complete=True):
    root.mkdir(exist_ok=True)
    turn_status = 'inProgress' if status == 'active' else 'completed'
    (root / 'manual-state.json').write_text(json.dumps({'threads': {thread_id: {'id': thread_id, 'cwd': str(repo), 'source': 'desktop',
        'status': {'type': status, 'activeFlags': []}, 'turns': [{'id': 'manual-turn', 'status': turn_status,
            'itemsView': 'full' if complete else 'partial', 'items': []}]}}, 'loaded': [thread_id], 'listed': [thread_id]}))


def test_public_original_service_discovery_is_read_only_and_active_blocks_repository(tmp_path):
    repo = make_repo(tmp_path / 'repo')
    peer = tmp_path / 'manual-peer'
    manual_state(peer, repo)
    with Manager(tmp_path / 'state', owner_identity_ref=OWNER.subject, dsh_adapter=adapter_for(tmp_path), observation_adapters={'local:manual-desktop': observer(peer)}) as manager:
        request_id = accepted(manager, repo)
        with ManagementServer(manager, {'observer-owner': OWNER}):
            client = ManagementClient(tmp_path / 'state', 'observer-owner')
            client.register_observation_source(source())
            result = client.refresh_manual_sessions('mono')
            assert result['manual_sources'][0]['scope']['executor'] == 'synthetic-original'
            session = result['manual_sessions'][0]
            assert session['state'] == 'active'
            assert session['control'] == 'observe_only'
            assert session['logical_repository'] == str(repo / '.git')
            assert session['last_verified_at']
            with pytest.raises(ManagementError) as busy:
                client.start_task(request_id)
            assert busy.value.code == 'repository_busy'
            queued = client.read_snapshot()['requests'][0]
            assert queued['queue']['manual_blockers'] == [session['id']]
            assert any('手动' in s['text'] for p in queued['outbox'] for s in p['segments'])
        wire = [json.loads(line) for line in (peer / 'manual-wire.jsonl').read_text().splitlines()]
        assert all(message.get('method') in READ_ONLY for message in wire)
        lists = [r['params'] for r in wire if r['method'] == 'fixture/list']
        assert lists and all(p['sourceKinds'] == ['desktop'] for p in lists)


def test_start_rechecks_original_manual_service_after_previous_idle_observation(tmp_path):
    repo = make_repo(tmp_path / 'repo')
    peer = tmp_path / 'manual-peer'
    manual_state(peer, repo, 'idle')
    with Manager(tmp_path / 'state', owner_identity_ref=OWNER.subject, dsh_adapter=adapter_for(tmp_path), observation_adapters={'local:manual-desktop': observer(peer)}) as manager:
        request_id = accepted(manager, repo)
        with ManagementServer(manager, {'observer-owner': OWNER}):
            client = ManagementClient(tmp_path / 'state', 'observer-owner')
            client.register_observation_source(source())
            assert client.refresh_manual_sessions()['manual_sessions'][0]['state'] == 'inactive_verified'
            manual_state(peer, repo, 'active')
            with pytest.raises(ManagementError) as changed:
                client.start_task(request_id)
            assert changed.value.code == 'repository_busy'
            assert client.read_snapshot()['manual_sessions'][0]['state'] == 'active'
        if (tmp_path / 'wire.jsonl').exists():
            assert not any(json.loads(line)['method'] == 'fixture/create' for line in (tmp_path / 'wire.jsonl').read_text().splitlines())


def test_new_connection_with_same_history_cannot_release_original_manual_occupancy(tmp_path):
    repo = make_repo(tmp_path / 'repo')
    peer = tmp_path / 'manual-peer'
    manual_state(peer, repo)
    with Manager(tmp_path / 'state', owner_identity_ref=OWNER.subject, dsh_adapter=adapter_for(tmp_path), observation_adapters={'local:manual-desktop': observer(peer)}) as manager:
        request_id = accepted(manager, repo)
        manager.register_observation_source(OWNER, source())
        original = manager.refresh_manual_sessions(OWNER)['manual_sessions'][0]
    manual_state(peer, repo, 'idle')
    with Manager(tmp_path / 'state', owner_identity_ref=OWNER.subject, dsh_adapter=adapter_for(tmp_path), observation_adapters={'local:manual-desktop': observer(peer)}) as restarted:
        with ManagementServer(restarted, {'observer-owner': OWNER}):
            client = ManagementClient(tmp_path / 'state', 'observer-owner')
            saved = client.read_snapshot()['manual_sessions'][0]
            assert saved['state'] == 'unknown'
            assert saved['last_known_state'] == 'active'
            result = client.refresh_manual_sessions()
            assert result['manual_sources'][0]['status'] == 'conflict'
            assert result['manual_sessions'][0]['id'] == original['id']
            assert result['manual_sessions'][0]['blocks_repository'] is True
            with pytest.raises(ManagementError) as blocked:
                client.start_task(request_id)
            assert blocked.value.code == 'repository_busy'


@pytest.mark.parametrize('failure', ['unsupported', 'pagination', 'history_only', 'partial_turn', 'background'])
def test_incomplete_observation_retains_last_known_activity_and_no_mutating_frames(tmp_path, failure):
    repo = make_repo(tmp_path / 'repo')
    peer = tmp_path / 'manual-peer'
    manual_state(peer, repo)
    with Manager(tmp_path / 'state', owner_identity_ref=OWNER.subject, dsh_adapter=adapter_for(tmp_path), observation_adapters={'local:manual-desktop': observer(peer)}) as manager:
        request_id = accepted(manager, repo)
        manager.register_observation_source(OWNER, source())
        manager.refresh_manual_sessions(OWNER)
        manual_state(peer, repo, 'idle', complete=failure != 'partial_turn')
        state = json.loads((peer / 'manual-state.json').read_text())
        if failure == 'unsupported': state['unsupported'] = ['fixture/list']
        if failure == 'pagination': state['loaded_pages'] = {'': {'data': []}}
        if failure == 'history_only': state['loaded'] = []
        if failure == 'background': state['background_pages'] = {'': {'data': [], 'nextCursor': 'next'}, 'next': {'data': [{'itemId': 'bg', 'processId': 'still-active'}], 'nextCursor': None}}
        state['approval_request'] = True
        (peer / 'manual-state.json').write_text(json.dumps(state))
        result = manager.refresh_manual_sessions(OWNER)
        record = result['manual_sessions'][0]
        assert record['state'] in {'unknown', 'last_known'}
        assert record['last_known_state'] == 'active'
        assert record['blocks_repository'] is True
        with pytest.raises(ManagementError): manager.start_task(OWNER, request_id)
    assert all(json.loads(line).get('method') in READ_ONLY for line in (peer / 'manual-wire.jsonl').read_text().splitlines())


@pytest.mark.parametrize('change', ['legacy_engine', 'legacy_methods', 'wrong_client', 'expired', 'broad_source_scope', 'missing_method_case'])
def test_configured_dsh_receipt_cannot_reuse_foreign_or_incomplete_observation_authority(tmp_path, change):
    from datetime import datetime, timedelta, timezone
    from dsh_fixture_server import remote_peer
    from ghost_hermes_pm.observation import configured_observation_adapters
    from test_dsh_engine_transition import dsh_config, observation_receipt, save_observation_receipt
    repo = make_repo(tmp_path / 'repo')
    with remote_peer() as (url, peer):
        config = dsh_config(url)
        adapters = configured_observation_adapters([config], tmp_path / 'state')
        adapter = adapters[config['service_ref']]
        with Manager(tmp_path / 'state', owner_identity_ref=OWNER.subject, observation_adapters=adapters) as manager:
            from test_task_execution import execution_registration
            manager.apply_directory_change(OWNER, 0, execution_registration(repo))
            manager.register_observation_source(OWNER, source())
            assert manager.refresh_manual_sessions(OWNER)['manual_sources'][0]['status'] == 'unknown'
            receipt = observation_receipt(adapter, config)
            if change == 'legacy_engine':
                receipt['engine'] = 'codex'
            elif change == 'legacy_methods':
                receipt['supported_methods'] = ['thread/read', 'thread/list', 'thread/loaded/list']
            elif change == 'wrong_client':
                receipt['client_id'] = 'another-generation-client'
            elif change == 'expired':
                now = datetime.now(timezone.utc)
                receipt.update(verified_at=(now - timedelta(minutes=4)).isoformat(), expires_at=(now - timedelta(minutes=1)).isoformat())
            elif change == 'broad_source_scope':
                receipt['source_kinds'] = ['desktop', 'web']
            else:
                del receipt['read_cases']['session/follow']
            save_observation_receipt(tmp_path / 'state', config, receipt)
            result = manager.refresh_manual_sessions(OWNER)
            assert result['manual_sources'][0]['status'] == 'unknown'
            assert not result['manual_sessions']
        assert peer['calls'] == []


def test_manual_alias_blocks_same_git_repository_but_other_registered_repository_can_execute(tmp_path):
    from test_repository_queue import acknowledge, register_project
    from test_task_execution import prepare_fixture
    repo = make_repo(tmp_path / 'repo')
    alias = tmp_path / 'alias'
    alias.symlink_to(repo, target_is_directory=True)
    other = make_repo(tmp_path / 'other')
    peer = tmp_path / 'manual-peer'
    manual_state(peer, alias)
    with Manager(tmp_path / 'state', owner_identity_ref=OWNER.subject, dsh_adapter=adapter_for(tmp_path), observation_adapters={'local:manual-desktop': observer(peer)}) as manager:
        first = accepted(manager, repo)
        register_project(manager, other, 'other')
        second = acknowledge(manager, 'other', 'other-lead', 'other-task')
        prepare_fixture(manager, second, other)
        manager.register_observation_source(OWNER, source())
        manager.refresh_manual_sessions(OWNER)
        with pytest.raises(ManagementError) as blocked: manager.start_task(OWNER, first)
        assert blocked.value.code == 'repository_busy'
        assert manager.start_task(OWNER, second)['status'] == 'running'
        assert manager.read_snapshot(OWNER)['manual_sessions'][0]['logical_repository'] == str(repo / '.git')


def test_missing_hashed_host_receipt_never_reads_or_controls_the_dsh_backend(tmp_path):
    from dsh_fixture_server import remote_peer
    from ghost_hermes_pm.observation import configured_observation_adapters
    from test_dsh_engine_transition import dsh_config
    repo = make_repo(tmp_path / 'repo')
    with remote_peer() as (url, peer):
        config = dsh_config(url)
        adapters = configured_observation_adapters([config], tmp_path / 'state')
        with Manager(tmp_path / 'state', owner_identity_ref=OWNER.subject, observation_adapters=adapters) as manager:
            from test_task_execution import execution_registration
            manager.apply_directory_change(OWNER, 0, execution_registration(repo))
            manager.register_observation_source(OWNER, source())
            result = manager.refresh_manual_sessions(OWNER)
            assert result['manual_sources'][0]['status'] == 'unknown'
            assert not result['manual_sessions']
            (tmp_path / 'state' / 'dsh-observation.json').write_text(json.dumps({'enabled': True, 'supports_all': True}))
            assert manager.refresh_manual_sessions(OWNER)['manual_sources'][0]['status'] == 'unknown'
        assert peer['calls'] == []
        assert all(frame.get('endpoint') == '$events' for frame in peer['streams'] if frame['type'] == 'open')


def test_idle_parent_does_not_hide_related_child_execution_outside_default_lists(tmp_path):
    repo = make_repo(tmp_path / 'repo')
    peer = tmp_path / 'manual-peer'
    manual_state(peer, repo, 'idle')
    state = json.loads((peer / 'manual-state.json').read_text())
    state['threads']['manual-thread']['turns'][0]['items'] = [{'id': 'collab', 'type': 'collabAgentToolCall', 'status': 'completed', 'receiverThreadIds': ['child-thread']}]
    state['threads']['child-thread'] = {'id': 'child-thread', 'cwd': str(repo), 'status': {'type': 'active', 'activeFlags': []},
        'turns': [{'id': 'child-turn', 'status': 'inProgress', 'itemsView': 'full', 'items': []}]}
    (peer / 'manual-state.json').write_text(json.dumps(state))
    with Manager(tmp_path / 'state', owner_identity_ref=OWNER.subject, dsh_adapter=adapter_for(tmp_path), observation_adapters={'local:manual-desktop': observer(peer)}) as manager:
        request_id = accepted(manager, repo)
        manager.register_observation_source(OWNER, source())
        record = manager.refresh_manual_sessions(OWNER)['manual_sessions'][0]
        assert record['state'] == 'unknown'
        assert record['blocks_repository'] is True
        with pytest.raises(ManagementError): manager.start_task(OWNER, request_id)
    assert all(json.loads(line).get('method') in READ_ONLY for line in (peer / 'manual-wire.jsonl').read_text().splitlines())


@pytest.mark.parametrize('status,owner,expected', [('running', 'manual-thread', 'unknown'),
    ('stopping', None, 'unknown'), ('completed', 'manual-thread', 'inactive_verified'),
    ('failed', None, 'inactive_verified'), ('killed', 'manual-thread', 'inactive_verified')])
def test_idle_observation_distinguishes_registered_job_state_without_inventing_process_ids(tmp_path, status, owner, expected):
    repo = make_repo(tmp_path / 'repo')
    peer = tmp_path / 'manual-peer'
    manual_state(peer, repo, 'idle')
    state = json.loads((peer / 'manual-state.json').read_text())
    job = {'id': 'synthetic-job-1', 'kind': 'synthetic-shell', 'status': status, 'owner': owner}
    state['background_pages'] = {'': {'data': [job], 'nextCursor': None}}
    (peer / 'manual-state.json').write_text(json.dumps(state))
    with Manager(tmp_path / 'state', owner_identity_ref=OWNER.subject,
                 observation_adapters={'local:manual-desktop': observer(peer)}) as manager:
        from test_task_execution import execution_registration
        manager.apply_directory_change(OWNER, 0, execution_registration(repo))
        manager.register_observation_source(OWNER, source())
        observed = manager.refresh_manual_sessions(OWNER)['manual_sessions'][0]
        assert observed['state'] == expected
        assert observed['blocks_repository'] is (expected != 'inactive_verified')


def test_configured_dsh_reads_original_peer_only_after_current_hashed_host_receipt(tmp_path):
    from dsh_fixture_server import remote_peer
    from ghost_hermes_pm.observation import configured_observation_adapters
    from test_dsh_engine_transition import dsh_config, observation_receipt, save_observation_receipt
    repo = make_repo(tmp_path / 'repo')
    with remote_peer() as (url, peer):
        peer['sessions'][0].update(cwd=str(repo), running=True)
        config = dsh_config(url)
        adapters = configured_observation_adapters([config], tmp_path / 'state')
        adapter = adapters[config['service_ref']]
        with Manager(tmp_path / 'state', owner_identity_ref=OWNER.subject, observation_adapters=adapters) as manager:
            from test_task_execution import execution_registration
            manager.apply_directory_change(OWNER, 0, execution_registration(repo))
            manager.register_observation_source(OWNER, source())
            assert manager.refresh_manual_sessions(OWNER)['manual_sources'][0]['status'] == 'unknown'
            assert peer['calls'] == []
            receipt = observation_receipt(adapter, config)
            path = save_observation_receipt(tmp_path / 'state', config, receipt)
            with ManagementServer(manager, {'observer-owner': OWNER}):
                client = ManagementClient(tmp_path / 'state', 'observer-owner')
                result = client.refresh_manual_sessions()
                assert result['manual_sources'][0]['status'] == 'verified'
                assert result['manual_sessions'][0]['original_executor_id'] == adapter.connection['service_id']
                assert result['manual_sessions'][0]['state'] == 'active'
                assert {c['kind']: c['status'] for c in result['manual_capabilities']} == {'desktop': 'verified', 'web': 'unknown'}
                path.write_text(json.dumps({**receipt, 'original_executor_id': 'untrusted-replacement'}))
                result = client.refresh_manual_sessions()
                assert result['manual_sources'][0]['status'] == 'unknown'
                assert result['manual_sessions'][0]['state'] == 'unknown'
                assert result['manual_sessions'][0]['blocks_repository'] is True
        assert all(call['method'] in {'session/list', 'session/page', 'session/projections'} for call in peer['calls'])
        assert all(frame.get('endpoint') in {'$events', 'session/follow'} for frame in peer['streams'] if frame['type'] == 'open')


@pytest.mark.parametrize('declared', [False, True])
def test_registered_dsh_read_scope_requires_declared_pagination_before_native_request(tmp_path, declared):
    from dsh_fixture_server import remote_peer
    from ghost_hermes_pm.observation import configured_observation_adapters
    from test_dsh_engine_transition import dsh_config, observation_receipt, save_observation_receipt
    repo = make_repo(tmp_path / 'repo')
    prefix = [{'type': 'event', 'event': {'type': 'turn/start', 'seq': 0, 'time': 1, 'data': {'turn': 1}}},
        {'type': 'event', 'event': {'type': 'user/message', 'seq': 1, 'time': 1, 'data': {
            'turn': 1, 'content': [{'type': 'text', 'text': 'synthetic prior input'}], 'source': {'kind': 'user', 'rpcId': 'synthetic-prior-input'}}}},
        {'type': 'event', 'event': {'type': 'turn/end', 'seq': 2, 'time': 2, 'data': {'turn': 1, 'reason': {'kind': 'completed'}}}}]
    tail = [{'type': 'event', 'event': {'type': 'turn/start', 'seq': 3, 'time': 3, 'data': {'turn': 2}}}]
    with remote_peer(behavior={'records': tail, 'has_more': True, 'page': {'records': prefix, 'hasMore': False}}) as (url, peer):
        peer['sessions'][0].update(cwd=str(repo), running=True)
        config = dsh_config(url)
        adapters = configured_observation_adapters([config], tmp_path / 'state')
        adapter = adapters[config['service_ref']]
        with Manager(tmp_path / 'state', owner_identity_ref=OWNER.subject, observation_adapters=adapters) as manager:
            from test_task_execution import execution_registration
            manager.apply_directory_change(OWNER, 0, execution_registration(repo))
            manager.register_observation_source(OWNER, source())
            assert manager.refresh_manual_sessions(OWNER)['manual_sources'][0]['status'] == 'unknown'
            receipt = observation_receipt(adapter, config)
            if not declared:
                receipt['supported_methods'].remove('session/page')
                del receipt['read_cases']['session/page']
            save_observation_receipt(tmp_path / 'state', config, receipt)
            result = manager.refresh_manual_sessions(OWNER)
            assert result['manual_sources'][0]['status'] == ('verified' if declared else 'unknown')
            if declared:
                assert result['manual_sessions'][0]['state'] == 'active'
                assert result['manual_sessions'][0]['current_turn_id'] == 'dsh-turn:2'
            else:
                assert not result['manual_sessions']
        assert [call['method'] for call in peer['calls']].count('session/page') == (1 if declared else 0)


def test_registered_dsh_read_scope_guards_native_job_stream_before_opening_it(tmp_path):
    from dsh_fixture_server import remote_peer
    from ghost_hermes_pm.observation import configured_observation_adapters
    from test_dsh_engine_transition import dsh_config, observation_receipt, save_observation_receipt
    job = {'id': 'synthetic-job-1', 'kind': 'synthetic-shell', 'owner': 'session-fixture', 'status': 'completed'}
    with remote_peer(behavior={'jobs': [job]}) as (url, peer):
        config = dsh_config(url)
        adapters = configured_observation_adapters([config], tmp_path / 'state')
        adapter = adapters[config['service_ref']]
        with Manager(tmp_path / 'state', owner_identity_ref=OWNER.subject, observation_adapters=adapters):
            with pytest.raises(ManagementError):
                adapter.proof()
            receipt = observation_receipt(adapter, config)
            save_observation_receipt(tmp_path / 'state', config, receipt)
            with pytest.raises(ManagementError) as missing:
                adapter.background_terminals('session-fixture')
            assert missing.value.code == 'capability_unverified'
            assert not any(frame.get('endpoint') == 'job/list' for frame in peer['streams'])
            receipt['supported_methods'].append('job/list')
            receipt['read_cases']['job/list'] = 'PASS'
            save_observation_receipt(tmp_path / 'state', config, receipt)
            assert adapter.background_terminals('session-fixture') == [job]
        assert sum(frame.get('endpoint') == 'job/list' and frame['type'] == 'open' for frame in peer['streams']) == 1


@pytest.mark.asyncio
async def test_group_dashboard_and_bridge_show_same_manual_state_and_original_public_anchor(tmp_path):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from ghost_hermes_pm.dashboard import create_router
    from ghost_hermes_pm.messages import FeishuEntry
    from test_feishu_entry import CONFIG, Gateway, Transport, event
    from test_requests import ISSUE
    from test_task_execution import execution_registration
    repo = make_repo(tmp_path / 'repo')
    peer = tmp_path / 'manual-peer'
    manual_state(peer, repo)
    state = json.loads((peer / 'manual-state.json').read_text())
    state['approval_request'] = True
    (peer / 'manual-state.json').write_text(json.dumps(state))
    surface, transport = object(), Transport()
    with Manager(tmp_path / 'state', owner_identity_ref=OWNER.subject, observation_adapters={'local:manual-desktop': observer(peer)}) as manager:
        manager.apply_directory_change(OWNER, 0, execution_registration(repo))
        intake = FeishuEntry(lambda: manager, OWNER.subject, CONFIG, lambda _: ISSUE)
        intake.attach_transport(surface, transport)
        await intake.receive(event(), Gateway(surface))
        task = manager.read_snapshot(OWNER)['requests'][0]
        with ManagementServer(manager, {'observer-owner': OWNER}):
            client = ManagementClient(tmp_path / 'state', 'observer-owner')
            app = FastAPI()
            app.include_router(create_router(lambda request: client))
            browser = TestClient(app)
            assert browser.post('/observations', json={'action': 'register', 'registration': source()}).status_code == 200
            assert browser.post('/observations', json={'action': 'refresh', 'actor': OWNER.subject}).status_code == 422
            command = event('核对手动会话', 'om_manual_observe')
            command.raw_message.event.message.parent_id = task['task_start_anchor']['message_id']
            await intake.receive(command, Gateway(surface))
            snapshot = browser.get('/snapshot').json()
            assert snapshot == client.read_snapshot()
            assert snapshot['manual_sessions'][0]['state'] == 'active'
            assert snapshot['manual_sessions'][0]['control'] == 'observe_only'
            assert 'private operation' not in json.dumps(snapshot)
            feedback = [s for s in transport.sent if '手动 DSH 只观察' in s['text']]
            assert feedback and all(s['reply_to'] == task['task_start_anchor']['message_id'] and s['mention_open_id'] is None for s in feedback)
    assert all(json.loads(line).get('method') in READ_ONLY for line in (peer / 'manual-wire.jsonl').read_text().splitlines())


def test_identity_change_during_observation_handshake_cannot_be_saved_as_original_executor(tmp_path):
    repo = make_repo(tmp_path / 'repo')
    peer = tmp_path / 'manual-peer'
    manual_state(peer, repo)
    adapter = observer(peer)
    provider = adapter.observation_verifier
    calls = 0
    def changed_binding(binding):
        nonlocal calls
        calls += 1
        return {**provider(binding), 'original_executor_id': 'original-A' if calls == 1 else 'replacement-B'}
    adapter.observation_verifier = changed_binding
    with Manager(tmp_path / 'state', owner_identity_ref=OWNER.subject, observation_adapters={'local:manual-desktop': adapter}) as manager:
        from test_task_execution import execution_registration
        manager.apply_directory_change(OWNER, 0, execution_registration(repo))
        manager.register_observation_source(OWNER, source())
        with ManagementServer(manager, {'observer-owner': OWNER}):
            result = ManagementClient(tmp_path / 'state', 'observer-owner').refresh_manual_sessions()
            assert result['manual_sources'][0]['status'] == 'conflict'
            assert not result['manual_sessions']
