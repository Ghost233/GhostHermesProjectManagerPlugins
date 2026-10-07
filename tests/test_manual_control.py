"""Owner grants current-work control of an original manual service through the bridge."""
import json
import sys
from pathlib import Path

import pytest

from ghost_hermes_pm import Manager, ManagementError, VerifiedIdentity
from ghost_hermes_pm.codex import repository_fingerprint
from ghost_hermes_pm.transport import ManagementClient, ManagementServer
from test_directory import OWNER, make_repo
from test_task_execution import accepted, adapter_for
from test_manual_observation import observer, source

ORIGINAL_THREAD = 'original-manual-thread'
ORIGINAL_TURN = 'original-manual-turn'
CONTROLLER = VerifiedIdentity('fixture:lead', 'verified-controller-entry')


def original_state(peer, repo):
    peer.mkdir(exist_ok=True)
    thread = {'id': ORIGINAL_THREAD, 'cwd': str(repo), 'cliVersion': '0.160.1', 'source': 'cli', 'canAcceptDirectInput': True,
              'status': {'type': 'active', 'activeFlags': []}, 'turns': [{'id': ORIGINAL_TURN, 'status': 'inProgress', 'itemsView': 'full', 'items': []}]}
    (peer / 'original-state.json').write_text(json.dumps({'thread': thread}))


def adapters(peer, host_capability=True):
    from ghost_hermes_pm.takeover import OriginalControlAdapter
    read = observer(peer)
    read.command[1] = str(Path(__file__).with_name('takeover_fixture_server.py'))
    def verifier(binding, repository, context):
        if not host_capability:
            return None
        return {**binding, 'grant_binding': context, 'permission_profile': 'original-fixture-policy', 'policy_digest': 'original-fixture-policy-digest',
            'repository_fingerprint': repository_fingerprint(repository), 'runtime_roots': [repository['worktree']],
            'platform_enforcement': 'synthetic-original-only', 'tool_paths': 'synthetic-original-only', 'manual_execution_coverage': 'synthetic-original-only',
            'takeover': 'synthetic-original-only', 'model': 'fixture-model',
            'task_control': {action: 'synthetic-original-only' for action in ('append', 'stop', 'continue', 'idle_input', 'related_execution', 'human_response')},
            'process_coverage': {'kind': 'no_unregistered_process_paths', 'evidence': 'synthetic-original-only'},
            'external_actor_coverage': 'unknown', 'control_access': 'verified-original-input-path'}
    control = OriginalControlAdapter([sys.executable, str(Path(__file__).with_name('takeover_fixture_server.py')), str(peer), 'app-server', 'proxy', '--sock', str(peer / 'registered-synthetic.sock')],
        cwd=peer, env={'PATH': '/usr/bin:/bin', 'CODEX_HOME': str(peer / 'synthetic-home')}, service_ref='local:manual-daemon-control',
        source_kind='daemon', endpoint_ref='local:registered-daemon', verifier=verifier, timeout=1)
    return read, control


def setup(manager, repo):
    request_id = accepted(manager, repo)
    manager.register_observation_source(OWNER, source())
    observed = manager.refresh_manual_sessions(OWNER)['manual_sessions'][0]
    return request_id, observed


def test_owner_takeover_controls_original_turn_and_return_keeps_it_running_without_interrupt(tmp_path):
    repo = make_repo(tmp_path / 'repo')
    peer = tmp_path / 'original'
    original_state(peer, repo)
    read, control = adapters(peer)
    with Manager(tmp_path / 'state', owner_identity_ref=OWNER.subject, codex_adapter=adapter_for(tmp_path),
                 observation_adapters={'local:manual-daemon': read}, control_adapters={'manual-daemon': control}) as manager:
        request_id, observed = setup(manager, repo)
        with ManagementServer(manager, {'manual-owner': OWNER, 'manual-controller': CONTROLLER}):
            owner = ManagementClient(tmp_path / 'state', 'manual-owner')
            controller = ManagementClient(tmp_path / 'state', 'manual-controller')
            granted = owner.take_over_session(request_id, observed['id'], 'grant-current-work', ORIGINAL_TURN)
            assert granted['status'] == 'active'
            assert granted['controller_profile_id'] == 'mono-lead'
            assert granted['original_executor_id'] == observed['original_executor_id']
            assert granted['thread_id'] == ORIGINAL_THREAD
            assert controller.control_task(request_id, 'append', 'manual-append', text='Complete only the accepted work.', expected_turn_id=ORIGINAL_TURN)['status'] == 'accepted'
            returned = owner.return_session_control(request_id, 'grant-current-work')
            assert returned['status'] == 'returned'
            task = owner.refresh_task(request_id)
            assert task['execution'] == 'running'
            assert task['session']['control'] == 'observe_only'
            assert task['repository_released'] is False
            with pytest.raises(ManagementError) as expired:
                controller.control_task(request_id, 'append', 'after-return', text='Do new work.', expected_turn_id=ORIGINAL_TURN)
            assert expired.value.code == 'forbidden'
        methods = [json.loads(line).get('method') for line in (peer / 'original-wire.jsonl').read_text().splitlines()]
        assert methods.count('turn/steer') == 1
        assert not {'thread/start', 'thread/resume', 'thread/fork', 'turn/interrupt'} & set(methods)
