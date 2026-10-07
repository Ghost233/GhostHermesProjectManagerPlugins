import json
import sys
from pathlib import Path

from ghost_hermes_pm import Manager
from ghost_hermes_pm.codex import CodexStdioAdapter, repository_fingerprint
from ghost_hermes_pm.transport import ManagementClient, ManagementServer
from test_directory import OWNER, make_repo, registration
from test_requests import MESSAGE, ISSUE


def adapter_for(root, control_proof=None):
    def verifier(connection, repository):
        proof = {'generation': connection['generation'], 'service_id': connection['service_id'],
                'repository_fingerprint': repository_fingerprint(repository), 'permission_profile': 'fixture-boundary',
                'runtime_roots': [repository['worktree']], 'policy_digest': 'fixture-policy',
                'process_coverage': {'kind': 'no_unregistered_process_paths', 'evidence': 'synthetic-peer-only'},
                'platform_enforcement': 'synthetic-peer-only', 'tool_paths': 'synthetic-peer-only',
                'task_control': {'append': 'synthetic-peer-only', 'stop': 'synthetic-peer-only', 'continue': 'synthetic-peer-only', 'related_execution': 'synthetic-peer-only', 'idle_input': 'synthetic-peer-only'},
                'task_start': 'synthetic-peer-only', 'manual_execution_coverage': 'synthetic-peer-only', 'model': 'fixture-model'}
        proof.update(control_proof or {})
        return proof
    return CodexStdioAdapter([sys.executable, str(Path(__file__).with_name('codex_fixture_server.py')), str(root)],
                            cwd=root, env={'PATH': '/usr/bin:/bin', 'CODEX_HOME': str(root / 'codex-home')},
                            service_ref='local:fixture-stdio', verifier=verifier, timeout=2)



def execution_registration(repo):
    value = registration(repo)
    value['profile']['connection_refs']['codex'] = 'local:fixture-stdio'
    return value

def prepare_fixture(manager, request_id, repo):
    from ghost_hermes_pm.queue import workspace
    repository = next(p['repo'] for p in manager.read_snapshot(OWNER)['projects'] if p['repo']['worktree'] == str(repo))
    current = workspace(repository)
    manager.prepare_task(OWNER, request_id, {'branch': current['branch'], 'commit': current['head'], 'dependencies': [],
        'issue_updated_at': next(r['accepted_scope']['updated_at'] for r in manager.read_snapshot(OWNER)['requests'] if r['id'] == request_id),
        'workspace_digest': current['source_digest']})


def accepted(manager, repo):
    manager.apply_directory_change(OWNER, 0, execution_registration(repo))
    request = manager.accept_request(OWNER, 'mono', 'mono-lead', MESSAGE, ISSUE)['request']
    manager.publish_request_message(OWNER, request['id'], 'confirmation', '已受理')
    segment = manager.claim_delivery(OWNER, request['id'])
    manager.record_delivery(OWNER, request['id'], segment['uuid'],
                            {'status': 'delivered', 'chat_id': 'oc_project', 'message_id': 'om_ack'})
    prepare_fixture(manager, request['id'], repo)
    return request['id']


def test_public_bridge_starts_one_issue_and_registers_thread_durably_before_turn(tmp_path):
    adapter = adapter_for(tmp_path)
    with Manager(tmp_path / 'state', owner_identity_ref=OWNER.subject, codex_adapter=adapter) as manager:
        request_id = accepted(manager, make_repo(tmp_path / 'repo'))
        with ManagementServer(manager, {'fixture-entry': OWNER}):
            client = ManagementClient(tmp_path / 'state', 'fixture-entry')
            started = client.start_task(request_id)
            snapshot = client.read_snapshot()
        task = snapshot['requests'][0]
        assert started['status'] == 'running'
        assert task['execution'] == 'running'
        assert task['task_delivery'] == 'unmet'
        assert task['pr_status'] == 'none'
        assert task['session']['thread_id'] == '00000000-0000-7000-8000-000000000016'
        assert task['session']['turn_id'] == '00000000-0000-7000-8000-000000000017'
        wire = [json.loads(line) for line in (tmp_path / 'wire.jsonl').read_text().splitlines()]
        methods = [r['method'] for r in wire]
        assert methods.count('thread/start') == methods.count('turn/start') == 1
        assert methods.index('thread/start') < methods.index('turn/start')
        prompt = next(r for r in wire if r['method'] == 'turn/start')['params']['input'][0]['text']
        assert ISSUE['body'] in prompt and ISSUE['updated_at'] in prompt
        assert 'Matt' in prompt and str(tmp_path / 'repo') in prompt
        assert task['outbox'][-1]['kind'] == 'progress'


def test_public_refresh_distinguishes_waiting_turn_end_and_delivery(tmp_path):
    adapter = adapter_for(tmp_path)
    with Manager(tmp_path / 'state', owner_identity_ref=OWNER.subject, codex_adapter=adapter) as manager:
        request_id = accepted(manager, make_repo(tmp_path / 'repo'))
        with ManagementServer(manager, {'fixture-entry': OWNER}):
            client = ManagementClient(tmp_path / 'state', 'fixture-entry')
            client.start_task(request_id)
            (tmp_path / 'observed.json').write_text(json.dumps({'status': {'type': 'active', 'activeFlags': ['waitingOnApproval']}}))
            waiting = client.refresh_task(request_id)
            assert waiting['execution'] == 'waiting_approval'
            (tmp_path / 'observed.json').write_text(json.dumps({'status': {'type': 'idle'}, 'turns': [
                {'id': '00000000-0000-7000-8000-000000000017', 'status': 'completed', 'itemsView': 'full', 'items': [
                    {'type': 'agentMessage', 'id': 'final', 'text': 'Done. All tests passed.'}]}]}))
            ended = client.refresh_task(request_id)
            assert ended['execution'] == 'turn_ended'
            assert ended['task_delivery'] == 'unmet'
            assert ended['pr_status'] == 'none'
            assert ended['repository_released'] is False
            assert ended['test_evidence'] == []
        wire = [json.loads(line)['method'] for line in (tmp_path / 'wire.jsonl').read_text().splitlines()]
        assert wire.count('thread/start') == wire.count('turn/start') == 1
        assert 'thread/resume' not in wire


def test_public_delivery_uses_recorded_test_execution_without_forcing_pr(tmp_path):
    import pytest
    from ghost_hermes_pm import ManagementError
    adapter = adapter_for(tmp_path)
    with Manager(tmp_path / 'state', owner_identity_ref=OWNER.subject, codex_adapter=adapter) as manager:
        request_id = accepted(manager, make_repo(tmp_path / 'repo'))
        with ManagementServer(manager, {'fixture-entry': OWNER}):
            client = ManagementClient(tmp_path / 'state', 'fixture-entry')
            client.start_task(request_id)
            observed = {'status': {'type': 'idle'}, 'turns': [{'id': '00000000-0000-7000-8000-000000000017',
                'status': 'completed', 'itemsView': 'full', 'items': [{'type': 'agentMessage', 'id': 'final', 'text': 'All tests passed.'}]}]}
            (tmp_path / 'observed.json').write_text(json.dumps(observed))
            report = {'issue_updated_at': ISSUE['updated_at'], 'criteria': [{'text': ISSUE['body'], 'test_item_ids': ['pytest-1']}],
                      'source_commit': None, 'pr_url': None, 'sync_branches': []}
            with pytest.raises(ManagementError) as missing:
                client.record_task_delivery(request_id, report)
            assert missing.value.code == 'evidence_missing'
            observed['turns'][0]['items'].append({'type': 'commandExecution', 'id': 'pytest-1', 'command': 'python -m pytest tests/test_fixture.py -q',
                'cwd': str(tmp_path / 'repo'), 'status': 'completed', 'exitCode': 0, 'aggregatedOutput': '1 passed'})
            (tmp_path / 'observed.json').write_text(json.dumps(observed))
            delivered = client.record_task_delivery(request_id, report)
            assert delivered['task_delivery'] == 'delivered'
            assert delivered['pr_status'] == 'none'
            assert delivered['repository_released'] is True
            assert delivered['test_evidence'][0]['command'] == 'python -m pytest tests/test_fixture.py -q'
            assert delivered['test_evidence'][0]['source'] == 'codex_command_execution'
            assert delivered['delivery_evidence']['issue_updated_at'] == ISSUE['updated_at']


def test_failed_thread_boundary_is_durable_and_never_starts_or_replays_turn(tmp_path):
    import pytest
    from ghost_hermes_pm import ManagementError
    (tmp_path / 'behavior.json').write_text(json.dumps({'thread/start': {'activePermissionProfile': None}}))
    adapter = adapter_for(tmp_path)
    with Manager(tmp_path / 'state', owner_identity_ref=OWNER.subject, codex_adapter=adapter) as manager:
        request_id = accepted(manager, make_repo(tmp_path / 'repo'))
        with pytest.raises(ManagementError) as denied:
            manager.start_task(OWNER, request_id)
        assert denied.value.code == 'capability_unverified'
        task = manager.read_snapshot(OWNER)['requests'][0]
        assert task['session']['thread_id']
        assert task['execution'] == 'unverified'
        with pytest.raises(ManagementError) as duplicate:
            manager.start_task(OWNER, request_id)
        assert duplicate.value.code == 'binding_conflict'
        wire = [json.loads(line)['method'] for line in (tmp_path / 'wire.jsonl').read_text().splitlines()]
        assert wire.count('thread/start') == 1
        assert 'turn/start' not in wire


def test_same_repository_competition_and_new_generation_cannot_resume_execution(tmp_path):
    import pytest
    from ghost_hermes_pm import ManagementError
    adapter = adapter_for(tmp_path)
    with Manager(tmp_path / 'state', owner_identity_ref=OWNER.subject, codex_adapter=adapter) as manager:
        request_id = accepted(manager, make_repo(tmp_path / 'repo'))
        manager.start_task(OWNER, request_id)
        second = manager.accept_request(OWNER, 'mono', 'mono-lead', {**MESSAGE, 'message_id': 'om_second'}, ISSUE)['request']['id']
        manager.publish_request_message(OWNER, second, 'confirmation', '已受理第二项')
        segment = manager.claim_delivery(OWNER, second)
        manager.record_delivery(OWNER, second, segment['uuid'], {'status': 'delivered', 'chat_id': 'oc_project', 'message_id': 'om_ack_second'})
        with pytest.raises(ManagementError) as busy:
            manager.start_task(OWNER, second)
        assert busy.value.code == 'repository_busy'
    replacement = adapter_for(tmp_path)
    with Manager(tmp_path / 'state', owner_identity_ref=OWNER.subject, codex_adapter=replacement) as manager:
        observed = manager.refresh_task(OWNER, request_id)
        assert observed['execution'] == 'unverified'
        assert observed['repository_released'] is False
        with pytest.raises(ManagementError):
            manager.start_task(OWNER, second)
        assert replacement.connection is None


def test_stdio_oversized_response_preserves_unknown_execution_and_repository_occupancy(tmp_path):
    adapter = adapter_for(tmp_path)
    with Manager(tmp_path / 'state', owner_identity_ref=OWNER.subject, codex_adapter=adapter) as manager:
        request_id = accepted(manager, make_repo(tmp_path / 'repo'))
        manager.start_task(OWNER, request_id)
        (tmp_path / 'behavior.json').write_text(json.dumps({'oversized': 'thread/read'}))
        result = manager.refresh_task(OWNER, request_id)
        assert result['execution'] == 'unverified'
        assert result['repository_released'] is False
        assert result['task_delivery'] == 'unmet'
        assert result['session']['thread_id']


def test_unknown_turn_start_is_not_replayed_after_timeout_or_restart(tmp_path):
    import pytest
    from ghost_hermes_pm import ManagementError
    (tmp_path / 'behavior.json').write_text(json.dumps({'omit_response': 'turn/start'}))
    adapter = adapter_for(tmp_path)
    with Manager(tmp_path / 'state', owner_identity_ref=OWNER.subject, codex_adapter=adapter) as manager:
        request_id = accepted(manager, make_repo(tmp_path / 'repo'))
        with pytest.raises(ManagementError) as timeout:
            manager.start_task(OWNER, request_id)
        assert timeout.value.code == 'outcome_unknown'
        with pytest.raises(ManagementError):
            manager.start_task(OWNER, request_id)
        assert manager.read_snapshot(OWNER)['requests'][0]['session']['start_phase'] == 'turn_start_intent'
    with Manager(tmp_path / 'state', owner_identity_ref=OWNER.subject, codex_adapter=adapter_for(tmp_path)) as manager:
        with pytest.raises(ManagementError):
            manager.start_task(OWNER, request_id)
    methods = [json.loads(line)['method'] for line in (tmp_path / 'wire.jsonl').read_text().splitlines()]
    assert methods.count('thread/start') == methods.count('turn/start') == 1


def test_static_dashboard_capability_claim_cannot_enable_missing_host_proof(tmp_path):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from ghost_hermes_pm.codex import configured_adapter
    from ghost_hermes_pm.dashboard import create_router
    config = {'command': [sys.executable, str(Path(__file__).with_name('codex_fixture_server.py')), str(tmp_path)],
              'cwd': str(tmp_path), 'environment': {'PATH': '/usr/bin:/bin', 'CODEX_HOME': str(tmp_path / 'codex-home')}, 'service_ref': 'local:fixture-stdio'}
    with Manager(tmp_path / 'state', owner_identity_ref=OWNER.subject, codex_adapter=configured_adapter(config, tmp_path / 'state')) as manager:
        request_id = accepted(manager, make_repo(tmp_path / 'repo'))
        with ManagementServer(manager, {'fixture-entry': OWNER}):
            app = FastAPI(); app.include_router(create_router(lambda request: ManagementClient(tmp_path / 'state', 'fixture-entry')))
            browser = TestClient(app)
            forged = browser.post('/task', json={'action': 'verify', 'request_id': request_id, 'verifier': True, 'enabled': True})
            assert forged.status_code == 422
            proof = browser.post('/task', json={'action': 'verify', 'request_id': request_id}).json()
            assert proof['enabled'] is False
            assert proof['connection']['generation']
            repository = manager.read_snapshot(OWNER)['projects'][0]['repo']
            (tmp_path / 'state' / 'codex-validation.json').write_text(json.dumps({
                'generation': proof['connection']['generation'], 'service_id': proof['connection']['service_id'],
                'repository_fingerprint': repository_fingerprint(repository), 'permission_profile': 'fixture-boundary',
                'runtime_roots': [repository['worktree']], 'policy_digest': 'static-claim',
                'platform_enforcement': 'passed', 'tool_paths': 'passed', 'task_start': 'passed', 'model': 'fixture-model'}))
            assert browser.post('/task', json={'action': 'verify', 'request_id': request_id}).json()['enabled'] is False
            assert browser.post('/task', json={'action': 'start', 'request_id': request_id}).status_code == 422
        methods = [json.loads(line)['method'] for line in (tmp_path / 'wire.jsonl').read_text().splitlines()]
        assert 'thread/start' not in methods and 'turn/start' not in methods


def test_public_start_budget_handles_slow_service_without_duplicate_mutation(tmp_path):
    (tmp_path / 'behavior.json').write_text(json.dumps({'delay': {'thread/start': 3.2}}))
    adapter = adapter_for(tmp_path)
    adapter.timeout = 5
    with Manager(tmp_path / 'state', owner_identity_ref=OWNER.subject, codex_adapter=adapter) as manager:
        request_id = accepted(manager, make_repo(tmp_path / 'repo'))
        with ManagementServer(manager, {'fixture-entry': OWNER}):
            client = ManagementClient(tmp_path / 'state', 'fixture-entry')
            assert client.start_task(request_id)['status'] == 'running'
            assert client.read_snapshot()['requests'][0]['execution'] == 'running'
        methods = [json.loads(line)['method'] for line in (tmp_path / 'wire.jsonl').read_text().splitlines()]
        assert methods.count('thread/start') == methods.count('turn/start') == 1


def test_issue_requiring_merge_keeps_delivery_unmet_but_records_actual_pr_state(tmp_path):
    import pytest
    import subprocess
    from ghost_hermes_pm import ManagementError
    repo = make_repo(tmp_path / 'repo')
    subprocess.run(['git', '-C', str(repo), '-c', 'user.name=Fixture', '-c', 'user.email=fixture@example.invalid', 'commit', '--allow-empty', '-qm', 'baseline'], check=True)
    subprocess.run(['git', '-C', str(repo), 'branch', '-M', 'main'], check=True)
    head = subprocess.check_output(['git', '-C', str(repo), 'rev-parse', 'HEAD'], text=True).strip()
    class Source:
        state, review = 'open', 'pending'
        def read_pr(self, url):
            return {'url': url, 'head_commit': head, 'state': self.state, 'review': self.review,
                    'merge_commit': head if self.state == 'merged' else None, 'base_branch': 'main', 'source': 'fixture_github_read'}
        def read_branch(self, repository_url, branch):
            return head
    source = Source()
    with Manager(tmp_path / 'state', owner_identity_ref=OWNER.subject, codex_adapter=adapter_for(tmp_path), delivery_source=source) as manager:
        manager.apply_directory_change(OWNER, 0, execution_registration(repo))
        scope = {**ISSUE, 'body': '- [ ] Run fixture tests\n- [ ] Merge PR into main'}
        request_id = manager.accept_request(OWNER, 'mono', 'mono-lead', MESSAGE, scope)['request']['id']
        manager.publish_request_message(OWNER, request_id, 'confirmation', '已受理')
        segment = manager.claim_delivery(OWNER, request_id)
        manager.record_delivery(OWNER, request_id, segment['uuid'], {'status': 'delivered', 'chat_id': 'oc_project', 'message_id': 'om_ack'})
        prepare_fixture(manager, request_id, repo)
        manager.start_task(OWNER, request_id)
        (tmp_path / 'observed.json').write_text(json.dumps({'status': {'type': 'idle'}, 'turns': [{'id': '00000000-0000-7000-8000-000000000017',
            'status': 'completed', 'itemsView': 'full', 'items': [{'type': 'commandExecution', 'id': 'pytest-1',
                'command': 'python -m pytest tests/test_fixture.py -q', 'cwd': str(repo), 'status': 'completed', 'exitCode': 0, 'aggregatedOutput': '1 passed'}]}]}))
        report = {'issue_updated_at': scope['updated_at'], 'criteria': [{'text': 'Run fixture tests', 'test_item_ids': ['pytest-1']},
            {'text': 'Merge PR into main', 'pr_evidence': True}], 'source_commit': head,
            'pr_url': 'https://github.com/Ghost233/fixture/pull/16', 'sync_branches': ['main']}
        with pytest.raises(ManagementError):
            manager.record_task_delivery(OWNER, request_id, report)
        task = manager.read_snapshot(OWNER)['requests'][0]
        assert task['pr_status'] == 'awaiting_review'
        assert task['task_delivery'] == 'unmet'
        assert task['repository_released'] is False
        source.review = 'approved'
        with pytest.raises(ManagementError):
            manager.record_task_delivery(OWNER, request_id, report)
        assert manager.read_snapshot(OWNER)['requests'][0]['pr_status'] == 'awaiting_merge'
        source.state = 'merged'
        delivered = manager.record_task_delivery(OWNER, request_id, report)
        assert delivered['pr_status'] == 'merged'
        assert delivered['task_delivery'] == 'delivered'
        assert delivered['delivery_evidence']['sync'][0]['local_commit'] == head


async def _feishu_start(tmp_path):
    from ghost_hermes_pm.messages import FeishuEntry
    from test_feishu_entry import CONFIG, Gateway, Transport, event
    adapter, transport = object(), Transport()
    with Manager(tmp_path / 'state', owner_identity_ref=OWNER.subject, codex_adapter=adapter_for(tmp_path)) as manager:
        manager.apply_directory_change(OWNER, 0, execution_registration(make_repo(tmp_path / 'repo')))
        intake = FeishuEntry(lambda: manager, OWNER.subject, CONFIG, lambda _: ISSUE)
        intake.attach_transport(adapter, transport)
        assert await intake.receive(event(), Gateway(adapter)) == {'action': 'skip'}
        prepare_fixture(manager, manager.read_snapshot(OWNER)['requests'][0]['id'], tmp_path / 'repo')
        command = event('执行', 'om_execute')
        command.raw_message.event.message.parent_id = manager.read_snapshot(OWNER)['requests'][0]['task_start_anchor']['message_id']
        assert await intake.receive(command, Gateway(adapter)) == {'action': 'skip'}
        task = manager.read_snapshot(OWNER)['requests'][0]
        assert task['execution'] == 'running'
        assert any('Codex 已核实运行' in s['text'] for s in transport.sent)
        assert transport.sent[-1]['reply_to'] == task['task_start_anchor']['message_id']
        assert transport.sent[-1]['mention_open_id'] is None


def test_verified_feishu_execute_uses_same_single_task_entry_and_original_anchor(tmp_path):
    import asyncio
    asyncio.run(_feishu_start(tmp_path))


def test_native_task_tool_cannot_borrow_owner_credential_alias(tmp_path, monkeypatch):
    from types import ModuleType
    from test_plugin_entry import Context, load_entry, fixture_native_home
    fixture_native_home(monkeypatch, tmp_path / 'native-home')
    secrets = ModuleType('agent.secret_scope')
    secrets.get_secret = lambda ref: 'fixture-owner-token'
    monkeypatch.setitem(sys.modules, 'agent.secret_scope', secrets)
    with Manager(tmp_path / 'state', owner_identity_ref=OWNER.subject) as manager:
        request_id = accepted(manager, make_repo(tmp_path / 'repo'))
        with ManagementServer(manager, {'fixture-owner-token': OWNER}):
            ctx = Context({'state_dir': str(tmp_path / 'state'), 'participant_credential_ref': 'native:OWNER_ALIAS'})
            load_entry().register(ctx)
            result = json.loads(ctx.tools['hermes_pm_task']({'action': 'verify', 'request_id': request_id}))
            assert result['status'] == 'rejected'
            assert result['code'] == 'forbidden'
            assert 'fixture-owner-token' not in str(result)


def test_dirty_source_changes_cannot_be_delivered_as_unchanged_head_commit(tmp_path):
    import subprocess
    import pytest
    from ghost_hermes_pm import ManagementError
    repo = make_repo(tmp_path / 'repo')
    (repo / 'source.py').write_text('original\n')
    subprocess.run(['git', '-C', str(repo), 'add', 'source.py'], check=True)
    subprocess.run(['git', '-C', str(repo), '-c', 'user.name=Fixture', '-c', 'user.email=fixture@example.invalid', 'commit', '-qm', 'baseline'], check=True)
    head = subprocess.check_output(['git', '-C', str(repo), 'rev-parse', 'HEAD'], text=True).strip()
    (repo / 'source.py').write_text('preexisting user change\n')
    with Manager(tmp_path / 'state', owner_identity_ref=OWNER.subject, codex_adapter=adapter_for(tmp_path)) as manager:
        request_id = accepted(manager, repo)
        manager.start_task(OWNER, request_id)
        (repo / 'source.py').write_text('new uncommitted task change\n')
        (tmp_path / 'observed.json').write_text(json.dumps({'status': {'type': 'idle'}, 'turns': [{'id': '00000000-0000-7000-8000-000000000017',
            'status': 'completed', 'itemsView': 'full', 'items': [{'type': 'commandExecution', 'id': 'pytest-1',
                'command': 'python -m pytest tests/test_fixture.py -q', 'cwd': str(repo), 'status': 'completed', 'exitCode': 0, 'aggregatedOutput': '1 passed'}]}]}))
        report = {'issue_updated_at': ISSUE['updated_at'], 'criteria': [{'text': ISSUE['body'], 'test_item_ids': ['pytest-1']}],
                  'source_commit': head, 'pr_url': None, 'sync_branches': []}
        with pytest.raises(ManagementError) as unsafe:
            manager.record_task_delivery(OWNER, request_id, report)
        assert unsafe.value.code == 'evidence_missing'
        assert manager.read_snapshot(OWNER)['requests'][0]['repository_released'] is False
        assert (repo / 'source.py').read_text() == 'new uncommitted task change\n'


def test_directory_correction_cannot_reassign_old_task_or_use_wrong_executor(tmp_path):
    import pytest
    from ghost_hermes_pm import ManagementError
    for change in ({'capability': 'non_development'}, {'identity_ref': 'fixture:new-lead'},
                   {'connection_refs': {'codex': 'local:different-executor'}}):
        case = tmp_path / str(len(list(tmp_path.iterdir())))
        case.mkdir()
        with Manager(case / 'state', owner_identity_ref=OWNER.subject, codex_adapter=adapter_for(case)) as manager:
            repo = make_repo(case / 'repo')
            request_id = accepted(manager, repo)
            correction = execution_registration(repo)['profile']
            correction.update(change)
            manager.apply_directory_change(OWNER, manager.read_snapshot(OWNER)['version'], {'profile': correction})
            with pytest.raises(ManagementError):
                manager.start_task(OWNER, request_id)
            assert manager.codex_adapter.connection is None
            assert manager.read_snapshot(OWNER)['requests'][0].get('session') is None


def test_existing_thread_observation_keeps_original_repository_after_directory_correction(tmp_path):
    adapter = adapter_for(tmp_path)
    with Manager(tmp_path / 'state', owner_identity_ref=OWNER.subject, codex_adapter=adapter) as manager:
        repo = make_repo(tmp_path / 'repo')
        request_id = accepted(manager, repo)
        manager.start_task(OWNER, request_id)
        other = make_repo(tmp_path / 'corrected-repo')
        manager.apply_directory_change(OWNER, manager.read_snapshot(OWNER)['version'], {'project': registration(other)['project']})
        observed = manager.refresh_task(OWNER, request_id)
        assert observed['execution'] == 'running'
        assert observed['session']['logical_repository'] == str(repo / '.git')
        assert observed['repository_released'] is False
        methods = [json.loads(line)['method'] for line in (tmp_path / 'wire.jsonl').read_text().splitlines()]
        assert methods.count('thread/start') == methods.count('turn/start') == 1
        assert methods[-1] == 'thread/read'


def test_plain_echo_cannot_be_relabelled_as_a_passing_test(tmp_path):
    import pytest
    from ghost_hermes_pm import ManagementError
    with Manager(tmp_path / 'state', owner_identity_ref=OWNER.subject, codex_adapter=adapter_for(tmp_path)) as manager:
        repo = make_repo(tmp_path / 'repo')
        request_id = accepted(manager, repo)
        manager.start_task(OWNER, request_id)
        (tmp_path / 'observed.json').write_text(json.dumps({'status': {'type': 'idle'}, 'turns': [{'id': '00000000-0000-7000-8000-000000000017',
            'status': 'completed', 'itemsView': 'full', 'items': [{'type': 'commandExecution', 'id': 'echo-1',
                'command': 'echo all tests passed', 'cwd': str(repo), 'status': 'completed', 'exitCode': 0, 'aggregatedOutput': 'all tests passed'}]}]}))
        report = {'issue_updated_at': ISSUE['updated_at'], 'criteria': [{'text': ISSUE['body'], 'test_item_ids': ['echo-1']}],
                  'source_commit': None, 'pr_url': None, 'sync_branches': []}
        with pytest.raises(ManagementError) as fabricated:
            manager.record_task_delivery(OWNER, request_id, report)
        assert fabricated.value.code == 'evidence_missing'
        assert manager.read_snapshot(OWNER)['requests'][0]['test_evidence'] == []


def test_executor_cannot_write_authoritative_manager_receipts_inside_repository(tmp_path):
    import pytest
    from ghost_hermes_pm import ManagementError
    repo = make_repo(tmp_path / 'repo')
    adapter = adapter_for(tmp_path)
    with Manager(repo / 'manager-state', owner_identity_ref=OWNER.subject, codex_adapter=adapter) as manager:
        request_id = accepted(manager, repo)
        with pytest.raises(ManagementError) as overlap:
            manager.start_task(OWNER, request_id)
        assert overlap.value.code == 'capability_unverified'
        assert adapter.connection is None
