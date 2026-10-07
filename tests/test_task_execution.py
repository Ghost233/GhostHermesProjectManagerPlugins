import json
import sys
from pathlib import Path

from ghost_hermes_pm import Manager
from ghost_hermes_pm.codex import CodexStdioAdapter, repository_fingerprint
from ghost_hermes_pm.transport import ManagementClient, ManagementServer
from test_directory import OWNER, make_repo, registration
from test_requests import MESSAGE, ISSUE


def adapter_for(root):
    def verifier(connection, repository):
        return {'generation': connection['generation'], 'service_id': connection['service_id'],
                'repository_fingerprint': repository_fingerprint(repository), 'permission_profile': 'fixture-boundary',
                'runtime_roots': [repository['worktree']], 'policy_digest': 'fixture-policy',
                'platform_enforcement': 'synthetic-peer-only', 'tool_paths': 'synthetic-peer-only',
                'task_start': 'synthetic-peer-only', 'model': 'fixture-model'}
    return CodexStdioAdapter([sys.executable, str(Path(__file__).with_name('codex_fixture_server.py')), str(root)],
                            cwd=root, env={'PATH': '/usr/bin:/bin', 'CODEX_HOME': str(root / 'codex-home')},
                            service_ref='local:fixture-stdio', verifier=verifier, timeout=2)


def accepted(manager, repo):
    manager.apply_directory_change(OWNER, 0, registration(repo))
    request = manager.accept_request(OWNER, 'mono', 'mono-lead', MESSAGE, ISSUE)['request']
    manager.publish_request_message(OWNER, request['id'], 'confirmation', '已受理')
    segment = manager.claim_delivery(OWNER, request['id'])
    manager.record_delivery(OWNER, request['id'], segment['uuid'],
                            {'status': 'delivered', 'chat_id': 'oc_project', 'message_id': 'om_ack'})
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
