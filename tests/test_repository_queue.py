"""Repository queue behavior through the authenticated public bridge and JSONL peer."""
import json
import subprocess
from pathlib import Path

import pytest

from ghost_hermes_pm import Manager, ManagementError
from readiness_support import ReadyManager as Manager
from ghost_hermes_pm.transport import ManagementClient, ManagementServer
from test_directory import OWNER, make_repo, registration
from test_requests import MESSAGE, ISSUE
from test_task_execution import adapter_for, accepted


def acknowledge(manager, project_id='mono', profile_id='mono-lead', suffix='second', issue=None):
    request = manager.accept_request(OWNER, project_id, profile_id, {**MESSAGE, 'message_id': 'om_' + suffix}, issue or ISSUE)['request']
    manager.publish_request_message(OWNER, request['id'], 'confirmation', '已受理')
    segment = manager.claim_delivery(OWNER, request['id'])
    manager.record_delivery(OWNER, request['id'], segment['uuid'], {'status': 'delivered', 'chat_id': 'oc_project', 'message_id': 'om_ack_' + suffix})
    return request['id']


def test_busy_repository_persists_fifo_reason_and_unknown_occupancy_across_restart(tmp_path):
    with Manager(tmp_path / 'state', owner_identity_ref=OWNER.subject, codex_adapter=adapter_for(tmp_path)) as manager:
        first = accepted(manager, make_repo(tmp_path / 'repo'))
        second = acknowledge(manager)
        with ManagementServer(manager, {'queue-owner': OWNER}):
            client = ManagementClient(tmp_path / 'state', 'queue-owner')
            client.start_task(first)
            with pytest.raises(ManagementError) as busy:
                client.start_task(second)
            assert busy.value.code == 'repository_busy'
            records = {r['id']: r for r in client.read_snapshot()['requests']}
            assert records[first]['queue']['status'] == 'occupied'
            assert records[second]['queue']['status'] == 'queued'
            assert records[second]['queue']['blocked_by'] == [first]
            assert records[second]['queue']['sequence'] > records[first]['queue']['sequence']
            assert any('排队' in s['text'] for p in records[second]['outbox'] for s in p['segments'])
    with Manager(tmp_path / 'state', owner_identity_ref=OWNER.subject, codex_adapter=adapter_for(tmp_path)) as restarted:
        records = {r['id']: r for r in restarted.read_snapshot(OWNER)['requests']}
        assert records[first]['execution'] == 'unverified'
        assert records[first]['queue']['status'] == 'occupied'
        assert records[second]['queue']['blocked_by'] == [first]
        with pytest.raises(ManagementError) as busy:
            restarted.start_task(OWNER, second)
        assert busy.value.code == 'repository_busy'


def commit_repo(path):
    repo = make_repo(path)
    (repo / 'source.py').write_text('baseline\n')
    subprocess.run(['git', '-C', str(repo), 'add', 'source.py'], check=True)
    subprocess.run(['git', '-C', str(repo), '-c', 'user.name=Fixture', '-c', 'user.email=fixture@example.invalid', 'commit', '-qm', 'baseline'], check=True)
    subprocess.run(['git', '-C', str(repo), 'branch', '-M', 'main'], check=True)
    return repo, subprocess.check_output(['git', '-C', str(repo), 'rev-parse', 'HEAD'], text=True).strip()


def test_next_task_requires_own_explicit_baseline_and_preserves_dirty_workspace(tmp_path):
    repo, head = commit_repo(tmp_path / 'repo')
    with Manager(tmp_path / 'state', owner_identity_ref=OWNER.subject, codex_adapter=adapter_for(tmp_path)) as manager:
        request_id = accepted(manager, repo)
        with ManagementServer(manager, {'queue-owner': OWNER}):
            client = ManagementClient(tmp_path / 'state', 'queue-owner')
            with pytest.raises(ManagementError) as wrong:
                client.prepare_task(request_id, {'branch': 'previous-unmerged', 'commit': head, 'dependencies': [], 'issue_updated_at': ISSUE['updated_at']})
            assert wrong.value.code == 'handoff_blocked'
            ready = client.prepare_task(request_id, {'branch': 'main', 'commit': head, 'dependencies': [], 'issue_updated_at': ISSUE['updated_at']})
            assert ready['preparation']['status'] == 'ready'
            (repo / 'source.py').write_text('user changes remain\n')
            (repo / 'notes.txt').write_text('untracked user notes\n')
            with pytest.raises(ManagementError) as dirty:
                client.start_task(request_id)
            assert dirty.value.code == 'handoff_blocked'
            task = client.read_snapshot()['requests'][0]
            assert task['queue']['status'] == 'handoff_blocked'
            assert task['preparation']['workspace']['head'] == head
            assert 'notes.txt' in task['preparation']['workspace']['workspace_status']
            assert (repo / 'source.py').read_text() == 'user changes remain\n'
            assert (repo / 'notes.txt').read_text() == 'untracked user notes\n'
        methods = [json.loads(line)['method'] for line in (tmp_path / 'wire.jsonl').read_text().splitlines()] if (tmp_path / 'wire.jsonl').exists() else []
        assert 'thread/start' not in methods


def test_ordinary_delivery_cannot_release_repository_with_unknown_process_coverage(tmp_path):
    repo, head = commit_repo(tmp_path / 'repo')
    with Manager(tmp_path / 'state', owner_identity_ref=OWNER.subject, codex_adapter=adapter_for(tmp_path, {'process_coverage': None})) as manager:
        request_id = accepted(manager, repo)
        with ManagementServer(manager, {'queue-owner': OWNER}):
            client = ManagementClient(tmp_path / 'state', 'queue-owner')
            client.start_task(request_id)
            (tmp_path / 'observed.json').write_text(json.dumps({'status': {'type': 'idle'}, 'turns': [{'id': '00000000-0000-7000-8000-000000000017',
                'status': 'completed', 'itemsView': 'full', 'items': [{'type': 'commandExecution', 'id': 'test-1', 'command': 'python -m pytest -q',
                    'cwd': str(repo), 'status': 'completed', 'exitCode': 0, 'aggregatedOutput': '1 passed'}]}]}))
            report = {'issue_updated_at': ISSUE['updated_at'], 'criteria': [{'text': ISSUE['body'], 'test_item_ids': ['test-1']}], 'source_commit': head}
            with pytest.raises(ManagementError) as missing:
                client.record_task_delivery(request_id, report)
            assert missing.value.code in {'capability_unverified', 'evidence_missing'}
            task = client.read_snapshot()['requests'][0]
            assert task['repository_released'] is False
            assert task['queue']['status'] == 'occupied'
            assert task['task_delivery'] == 'unmet'


def queue_adapter(root):
    adapter = adapter_for(root)
    adapter.command[1] = str(Path(__file__).with_name('queue_fixture_server.py'))
    return adapter


def register_project(manager, repo, name):
    value = registration(repo, name, name + '-lead')
    value['profile']['identity_ref'] = 'fixture:' + name
    value['profile']['connection_refs']['codex'] = 'local:fixture-stdio'
    manager.apply_directory_change(OWNER, manager.read_snapshot(OWNER)['version'], value)


def test_confirmed_delivery_advances_only_prepared_head_and_other_repository_runs_concurrently(tmp_path):
    from test_task_execution import prepare_fixture
    repo, head = commit_repo(tmp_path / 'repo')
    other, _ = commit_repo(tmp_path / 'other')
    with Manager(tmp_path / 'state', owner_identity_ref=OWNER.subject, codex_adapter=queue_adapter(tmp_path)) as manager:
        first = accepted(manager, repo)
        second = acknowledge(manager)
        prepare_fixture(manager, second, repo)
        register_project(manager, other, 'other')
        independent = acknowledge(manager, 'other', 'other-lead', 'independent')
        prepare_fixture(manager, independent, other)
        with ManagementServer(manager, {'queue-owner': OWNER}):
            client = ManagementClient(tmp_path / 'state', 'queue-owner')
            one = client.start_task(first)
            with pytest.raises(ManagementError) as waiting:
                client.start_task(second)
            assert waiting.value.code == 'repository_busy'
            two = client.start_task(independent)
            assert two['status'] == 'running'
            active = json.loads((tmp_path / 'active-threads.json').read_text())
            assert len(active) == 2
            assert {item['cwd'] for item in active} == {str(repo), str(other)}
            session = one['session']
            (tmp_path / 'queue-observed.json').write_text(json.dumps({session['thread_id']: {'status': {'type': 'idle'}, 'turns': [{'id': session['turn_id'],
                'status': 'completed', 'itemsView': 'full', 'items': [{'type': 'commandExecution', 'id': 'test-1', 'command': 'python -m pytest -q',
                    'cwd': str(repo), 'status': 'completed', 'exitCode': 0, 'aggregatedOutput': '1 passed'}]}]}}))
            report = {'issue_updated_at': ISSUE['updated_at'], 'criteria': [{'text': ISSUE['body'], 'test_item_ids': ['test-1']}], 'source_commit': head}
            delivered = client.record_task_delivery(first, report)
            assert delivered['repository_released'] is True
            records = {r['id']: r for r in client.read_snapshot()['requests']}
            assert records[second]['execution'] == 'running'
            assert records[second]['session']['thread_id'] != session['thread_id']
            assert records[second]['session']['baseline']['head'] == head
            assert records[independent]['execution'] == 'running'


def test_explicit_continue_joins_current_queue_after_new_work_instead_of_old_acceptance_time(tmp_path):
    from test_task_execution import prepare_fixture
    repo, head = commit_repo(tmp_path / 'repo')
    with Manager(tmp_path / 'state', owner_identity_ref=OWNER.subject, codex_adapter=queue_adapter(tmp_path)) as manager:
        first = accepted(manager, repo)
        with ManagementServer(manager, {'queue-owner': OWNER}):
            client = ManagementClient(tmp_path / 'state', 'queue-owner')
            session = client.start_task(first)['session']
            client.control_task(first, 'stop', 'queue-stop', expected_turn_id=session['turn_id'])
            (tmp_path / 'queue-observed.json').write_text(json.dumps({session['thread_id']: {'status': {'type': 'idle'}, 'turns': [{'id': session['turn_id'],
                'status': 'interrupted', 'itemsView': 'full', 'items': []}]}}))
            assert client.refresh_task(first)['execution'] == 'stopped'
            second = acknowledge(manager)
            prepare_fixture(manager, second, repo)
            with pytest.raises(ManagementError) as queued:
                client.control_task(first, 'continue', 'continue-later', text='Continue accepted work.', expected_turn_id=session['turn_id'])
            assert queued.value.code == 'repository_busy'
            records = {r['id']: r for r in client.read_snapshot()['requests']}
            assert records[first]['queue']['sequence'] > records[second]['queue']['sequence']
            assert records[first]['queue']['status'] == 'queued'
            assert records[first]['stop_records'][0]['status'] == 'confirmed'
            assert client.control_task(first, 'continue', 'continue-later', text='Continue accepted work.', expected_turn_id=session['turn_id'])['status'] == 'queued'
            next_session = client.start_task(second)['session']
            (tmp_path / 'queue-observed.json').write_text(json.dumps({session['thread_id']: {'status': {'type': 'idle'}, 'turns': [{'id': session['turn_id'],
                'status': 'interrupted', 'itemsView': 'full', 'items': []}]}, next_session['thread_id']: {'status': {'type': 'idle'}, 'turns': [{'id': next_session['turn_id'],
                'status': 'completed', 'itemsView': 'full', 'items': [{'type': 'commandExecution', 'id': 'test-2', 'command': 'python -m pytest -q',
                    'cwd': str(repo), 'status': 'completed', 'exitCode': 0, 'aggregatedOutput': '1 passed'}]}]}}))
            client.record_task_delivery(second, {'issue_updated_at': ISSUE['updated_at'], 'criteria': [{'text': ISSUE['body'], 'test_item_ids': ['test-2']}], 'source_commit': head})
            records = {r['id']: r for r in client.read_snapshot()['requests']}
            assert records[first]['execution'] == 'running'
            assert records[first]['session']['thread_id'] == session['thread_id']
            assert records[first]['stop_records'][0]['status'] == 'confirmed'
            assert records[first]['execution_arrangements'][-1]['created_at'] > records[first]['accepted_at']


def test_issue_source_difference_is_visible_without_replacing_frozen_accepted_scope(tmp_path):
    class Source:
        def read_issue(self, url):
            return {**ISSUE, 'url': url, 'body': ISSUE['body'] + '\n- [ ] Newly added scope', 'updated_at': '2026-10-07T04:00:00Z'}
    with Manager(tmp_path / 'state', owner_identity_ref=OWNER.subject, codex_adapter=adapter_for(tmp_path), delivery_source=Source()) as manager:
        request_id = accepted(manager, make_repo(tmp_path / 'repo'))
        with ManagementServer(manager, {'queue-owner': OWNER}):
            client = ManagementClient(tmp_path / 'state', 'queue-owner')
            updated = client.refresh_task_source(request_id)
            assert updated['accepted_scope'] == ISSUE
            assert updated['issue_source']['status'] == 'changed'
            assert '+- [ ] Newly added scope' in updated['issue_source']['diff']
            assert updated['issue_source']['current']['updated_at'] == '2026-10-07T04:00:00Z'
            assert any('来源变化' in s['text'] for p in updated['outbox'] for s in p['segments'])
            client.start_task(request_id)
        prompt = next(json.loads(line) for line in (tmp_path / 'wire.jsonl').read_text().splitlines() if json.loads(line)['method'] == 'turn/start')['params']['input'][0]['text']
        assert 'Newly added scope' not in prompt
        assert ISSUE['body'] in prompt


def test_linked_worktree_alias_cannot_bypass_manually_loaded_repository_execution(tmp_path):
    from test_task_execution import prepare_fixture
    repo, head = commit_repo(tmp_path / 'repo')
    linked = tmp_path / 'linked'
    subprocess.run(['git', '-C', str(repo), 'worktree', 'add', '-q', '-b', 'manual-branch', str(linked)], check=True)
    (tmp_path / 'queue-external.json').write_text(json.dumps({'manual-thread': {'id': 'manual-thread', 'cwd': str(linked), 'cliVersion': '0.160.1',
        'canAcceptDirectInput': True, 'status': {'type': 'active', 'activeFlags': []}, 'turns': [{'id': 'manual-turn', 'status': 'inProgress', 'itemsView': 'full', 'items': []}]}}))
    with Manager(tmp_path / 'state', owner_identity_ref=OWNER.subject, codex_adapter=queue_adapter(tmp_path)) as manager:
        first = accepted(manager, repo)
        register_project(manager, linked, 'alias')
        second = acknowledge(manager, 'alias', 'alias-lead', 'alias-request')
        prepare_fixture(manager, second, linked)
        with ManagementServer(manager, {'queue-owner': OWNER}):
            client = ManagementClient(tmp_path / 'state', 'queue-owner')
            with pytest.raises(ManagementError) as busy:
                client.start_task(first)
            assert busy.value.code == 'repository_busy'
            with pytest.raises(ManagementError) as waiting:
                client.start_task(second)
            assert waiting.value.code == 'repository_busy'
            records = {r['id']: r for r in client.read_snapshot()['requests']}
            assert records[first]['queue']['logical_repository'] == records[second]['queue']['logical_repository'] == str(repo / '.git')
            assert records[first]['queue']['status'] == 'external_unknown'
            assert not any(json.loads(line)['method'] in {'thread/start', 'turn/start'} for line in (tmp_path / 'wire.jsonl').read_text().splitlines())


def test_existing_issue17_durable_session_retains_occupancy_when_queue_schema_is_added(tmp_path):
    import sqlite3
    with Manager(tmp_path / 'state', owner_identity_ref=OWNER.subject, codex_adapter=adapter_for(tmp_path)) as manager:
        request_id = accepted(manager, make_repo(tmp_path / 'repo'))
        manager.start_task(OWNER, request_id)
    # A legacy on-disk fixture, not an assertion through private manager internals.
    with sqlite3.connect(tmp_path / 'state' / 'manager.sqlite3') as db:
        payload = json.loads(db.execute('SELECT payload FROM directory').fetchone()[0])
        payload.pop('queue_sequence', None)
        record = payload['requests'][request_id]
        for field in ('queue', 'preparation', 'accepted_repository'):
            record.pop(field, None)
        db.execute('UPDATE directory SET payload=?', (json.dumps(payload),))
    with Manager(tmp_path / 'state', owner_identity_ref=OWNER.subject, codex_adapter=adapter_for(tmp_path)) as restarted:
        with ManagementServer(restarted, {'queue-owner': OWNER}):
            client = ManagementClient(tmp_path / 'state', 'queue-owner')
            task = client.read_snapshot()['requests'][0]
            assert task['queue']['status'] == 'occupied'
            assert task['session']['thread_id']
            assert task['repository_released'] is False
            assert task['execution'] == 'unverified'
        second = acknowledge(restarted)
        with pytest.raises(ManagementError) as busy:
            restarted.start_task(OWNER, second)
        assert busy.value.code == 'repository_busy'


def test_unmerged_delivery_is_not_implicitly_reused_as_next_issue_baseline(tmp_path):
    import hashlib
    repo, baseline = commit_repo(tmp_path / 'repo')
    with Manager(tmp_path / 'state', owner_identity_ref=OWNER.subject, codex_adapter=queue_adapter(tmp_path)) as manager:
        first = accepted(manager, repo)
        with ManagementServer(manager, {'queue-owner': OWNER}):
            client = ManagementClient(tmp_path / 'state', 'queue-owner')
            session = client.start_task(first)['session']
            subprocess.run(['git', '-C', str(repo), 'checkout', '-qb', 'unmerged-first'], check=True)
            (repo / 'source.py').write_text('first fixed delivery\n')
            subprocess.run(['git', '-C', str(repo), 'add', 'source.py'], check=True)
            subprocess.run(['git', '-C', str(repo), '-c', 'user.name=Fixture', '-c', 'user.email=fixture@example.invalid', 'commit', '-qm', 'first delivery'], check=True)
            fixed = subprocess.check_output(['git', '-C', str(repo), 'rev-parse', 'HEAD'], text=True).strip()
            (tmp_path / 'queue-observed.json').write_text(json.dumps({session['thread_id']: {'status': {'type': 'idle'}, 'turns': [{'id': session['turn_id'],
                'status': 'completed', 'itemsView': 'full', 'items': []}]}}))
            delivered = client.record_task_delivery(first, {'source_commit': fixed, 'issue_updated_at': ISSUE['updated_at'], 'criteria': [{'text': ISSUE['body'],
                'artifact_refs': [{'path': str(repo / 'source.py'), 'sha256': hashlib.sha256((repo / 'source.py').read_bytes()).hexdigest()}]}]})
            assert delivered['task_delivery'] == 'delivered'
            assert delivered['delivery_evidence']['source_commit'] == fixed
            second = acknowledge(manager)
            plan = {'branch': 'unmerged-first', 'commit': fixed, 'dependencies': [], 'issue_updated_at': ISSUE['updated_at']}
            with pytest.raises(ManagementError) as inherited:
                client.prepare_task(second, plan)
            assert inherited.value.code == 'handoff_blocked'
            assert 'unmerged delivery' in str(inherited.value)
            assert (repo / 'source.py').read_text() == 'first fixed delivery\n'
            ready = client.prepare_task(second, {**plan, 'dependencies': [first]})
            assert ready['preparation']['status'] == 'ready'
            assert ready['preparation']['plan']['dependencies'] == [first]
            assert ready.get('session') is None


@pytest.mark.asyncio
async def test_group_and_dashboard_share_preparation_queue_reason_and_frozen_source_difference(tmp_path):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from ghost_hermes_pm.dashboard import create_router
    from ghost_hermes_pm.messages import FeishuEntry
    from test_feishu_entry import CONFIG, Gateway, Transport, event
    from test_task_execution import execution_registration
    class Source:
        def read_issue(self, url):
            return {**ISSUE, 'url': url, 'body': ISSUE['body'] + '\nNew source context.', 'updated_at': '2026-10-07T04:00:00Z'}
    repo, head = commit_repo(tmp_path / 'repo')
    surface, transport = object(), Transport()
    with Manager(tmp_path / 'state', owner_identity_ref=OWNER.subject, codex_adapter=queue_adapter(tmp_path), delivery_source=Source()) as manager:
        manager.apply_directory_change(OWNER, 0, execution_registration(repo))
        intake = FeishuEntry(lambda: manager, OWNER.subject, CONFIG, lambda _: ISSUE)
        intake.attach_transport(surface, transport)
        await intake.receive(event(), Gateway(surface))
        first = manager.read_snapshot(OWNER)['requests'][0]
        async def command(record, text, message_id):
            incoming = event(text, message_id)
            incoming.raw_message.event.message.parent_id = record['task_start_anchor']['message_id']
            return await intake.receive(incoming, Gateway(surface))
        await command(first, '确认基线：main ' + head, 'om_prepare_first')
        with ManagementServer(manager, {'queue-owner': OWNER}):
            client = ManagementClient(tmp_path / 'state', 'queue-owner')
            app = FastAPI()
            app.include_router(create_router(lambda request: client))
            browser = TestClient(app)
            assert browser.post('/task', json={'action': 'start', 'request_id': first['id']}).json()['status'] == 'running'
            await intake.receive(event(message_id='om_second_group'), Gateway(surface))
            second = next(r for r in client.read_snapshot()['requests'] if r['id'] != first['id'])
            await command(second, '确认基线：main ' + head, 'om_prepare_second')
            await command(second, '执行', 'om_execute_second')
            await command(second, '核对Issue来源', 'om_source_second')
            snapshot = browser.get('/snapshot').json()
            assert snapshot == client.read_snapshot()
            record = next(r for r in snapshot['requests'] if r['id'] == second['id'])
            assert record['queue']['status'] == 'queued' and record['queue']['blocked_by'] == [first['id']]
            assert record['preparation']['status'] == 'ready'
            assert record['issue_source']['status'] == 'changed'
            assert record['accepted_scope'] == ISSUE
            relevant = [s for s in transport.sent if s['reply_to'] == second['task_start_anchor']['message_id']]
            assert any('仓库排队' in s['text'] for s in relevant)
            assert any('来源变化' in s['text'] for s in relevant)
            assert all(s['mention_open_id'] is None for s in relevant)


@pytest.mark.parametrize('filename', ['source.py', ' leading.py'])
def test_preservation_acknowledgment_identifies_first_modified_file_without_trimming_git_status(tmp_path, filename):
    import hashlib
    repo, head = commit_repo(tmp_path / 'repo')
    if filename != 'source.py':
        subprocess.run(['git', '-C', str(repo), 'mv', 'source.py', filename], check=True)
        subprocess.run(['git', '-C', str(repo), '-c', 'user.name=Fixture', '-c', 'user.email=fixture@example.invalid', 'commit', '-qm', 'leading filename baseline'], check=True)
        head = subprocess.check_output(['git', '-C', str(repo), 'rev-parse', 'HEAD'], text=True).strip()
    (repo / filename).write_text('pre-existing user content\n')
    with Manager(tmp_path / 'state', owner_identity_ref=OWNER.subject, codex_adapter=queue_adapter(tmp_path)) as manager:
        manager.apply_directory_change(OWNER, 0, {**registration(repo), 'profile': {**registration(repo)['profile'], 'connection_refs': {'codex': 'local:fixture-stdio'}}})
        request_id = acknowledge(manager, suffix='dirty-intake')
        with ManagementServer(manager, {'queue-owner': OWNER}):
            client = ManagementClient(tmp_path / 'state', 'queue-owner')
            plan = {'branch': 'main', 'commit': head, 'dependencies': [], 'issue_updated_at': ISSUE['updated_at']}
            with pytest.raises(ManagementError) as blocked:
                client.prepare_task(request_id, plan)
            assert blocked.value.code == 'handoff_blocked'
            task = client.read_snapshot()['requests'][0]
            assert task['preparation']['workspace']['dirty_paths'] == [filename]
            ready = client.prepare_task(request_id, {**plan, 'workspace_digest': task['preparation']['workspace']['source_digest']})
            assert ready['preparation']['preserved_files'] == {filename: hashlib.sha256((repo / filename).read_bytes()).hexdigest()}
            client.start_task(request_id)
            assert (repo / filename).read_text() == 'pre-existing user content\n'
