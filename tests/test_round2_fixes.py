"""Public round-two review regressions with owned Git and actual test execution."""
import json
import os
from pathlib import Path
import subprocess
import sys

import pytest

from ghost_hermes_pm import ManagementError
from ghost_hermes_pm.transport import ManagementClient, ManagementServer
from readiness_support import ReadyManager
from test_directory import OWNER, make_repo
from test_requests import ISSUE
from test_task_execution import accepted, adapter_for


@pytest.mark.parametrize('criterion, expected_error', [
    ('PR 待审查或待合并状态应单独显示', None),
    ('测试完成即可交付；PR 合并状态另行显示', None),
    ('The report must show whether the PR is open or merged.', None),
    ('PR 合并可选，测试结果满足即可交付', None),
    ('PR 不需要合并，只验证测试结果', None),
    ('The PR may be merged later; passing tests is sufficient for delivery.', None),
    ('Merging is optional after delivery.', None),
    ('The PR must not be merged.', None),
    ('Do not merge the PR; keep it open.', None),
    ('不要合并该 PR；完成测试', None),
    ('PR 不是必须合并', None),
    ('The PR is not required to be merged.', None),
    ('PR 合并处理', 'needs_clarification'),
    ('Merging follows the review decision.', 'needs_clarification'),
    ('Merge the PR if the Owner approves.', 'needs_clarification'),
    ('PR 合并可选，但必须合并后交付', 'needs_clarification'),
])
def test_status_reporting_does_not_require_merge(tmp_path, criterion, expected_error):
    repo = make_repo(tmp_path / 'repo')
    (repo / 'test_acceptance.py').write_text('def test_acceptance():\n    assert 2 + 2 == 4\n')
    subprocess.run(['git', '-C', str(repo), 'add', 'test_acceptance.py'], check=True)
    subprocess.run(['git', '-C', str(repo), '-c', 'user.name=Owned Fixture', '-c', 'user.email=owned@example.invalid', 'commit', '-qm', 'Owned assertions'], check=True)
    scope = {**ISSUE, 'body': '- [ ] ' + criterion}
    with ReadyManager(tmp_path / 'state', owner_identity_ref=OWNER.subject, codex_adapter=adapter_for(tmp_path)) as manager:
        request_id = accepted(manager, repo, scope)
        with ManagementServer(manager, {'owner': OWNER}):
            client = ManagementClient(tmp_path / 'state', 'owner')
            session = client.start_task(request_id)['session']
            argv = [sys.executable, '-m', 'pytest', 'test_acceptance.py', '-q']
            result = subprocess.run(argv, cwd=repo, env={**os.environ, 'PYTHONDONTWRITEBYTECODE': '1'}, text=True, capture_output=True)
            assert result.returncode == 0
            assert '1 passed' in result.stdout
            (tmp_path / 'observed.json').write_text(json.dumps({'status': {'type': 'idle'}, 'turns': [{'id': session['turn_id'], 'status': 'completed', 'itemsView': 'full', 'items': [{'type': 'commandExecution', 'id': 'actual-assertion', 'command': ' '.join(argv), 'cwd': str(repo), 'status': 'completed', 'exitCode': result.returncode, 'aggregatedOutput': result.stdout + result.stderr}]}]}))
            report = {'issue_updated_at': scope['updated_at'], 'criteria': [{'text': criterion, 'test_item_ids': ['actual-assertion']}], 'source_commit': None, 'pr_url': None, 'sync_branches': []}
            if expected_error:
                with pytest.raises(ManagementError) as unclear:
                    client.record_task_delivery(request_id, report)
                assert unclear.value.code == expected_error
                task = client.read_snapshot()['requests'][0]
                assert task['accepted_scope']['body'] == scope['body']
                assert task['task_delivery'] == 'unmet' and task['repository_released'] is False
            else:
                delivered = client.record_task_delivery(request_id, report)
                assert delivered['task_delivery'] == 'delivered'
                assert delivered['pr_status'] == 'none'
                assert delivered['repository_released'] is True


def native_runner(tmp_path, code='assert True', environment=None):
    import hashlib
    from ghost_hermes_pm.native_global_validation import configured_global_validation_host
    repo = make_repo(tmp_path / 'native-repo')
    with ReadyManager(tmp_path / 'directory', owner_identity_ref=OWNER.subject) as directory:
        from test_directory import registration
        directory.apply_directory_change(OWNER, 0, registration(repo))
        repository = directory.read_snapshot(OWNER)['projects'][0]['repo']
    config = {'host_id': 'local:owned-resource-test', 'generation': 'owned-resource-generation', 'runner': [sys.executable], 'watcher': [sys.executable, '-c', 'import time; time.sleep(60)'], 'tests': {'unit': ['-c', code]}, 'environment': environment or {'PATH': '/usr/bin:/bin'}}
    state = tmp_path / 'native-state'
    host = configured_global_validation_host(config, state)
    attempt = {'id': 'owned-resource-round', 'input_digest': 'a' * 64, 'repository': repository, 'children': [], 'test_ids': ['unit']}
    host.prepare(attempt, {'owner': OWNER.subject, 'source': OWNER.source, 'children': [], 'digest': 'b' * 64})
    binding = json.loads((state / 'validation-native' / (hashlib.sha256(attempt['id'].encode()).hexdigest() + '-watch-binding.json')).read_text())
    checks = {'source_write_denied', 'child_source_write_denied', 'parent_git_write_denied', 'child_git_write_denied', 'artifact_write_allowed', 'artifact_escape_denied', 'preexisting_hardlink_write_denied', 'tool_paths_confined', 'process_paths_confined', 'input_change_observation_complete'}
    proof = {**binding, 'host_id': config['host_id'], 'generation': config['generation'], 'runner_configuration_digest': host.configuration_digest, 'validation_id': attempt['id'], 'input_digest': attempt['input_digest'], 'source_access': 'read-only', 'git_access': 'read-only', 'artifact_roots': repository['test_artifact_paths'], 'runner_binary_sha256': hashlib.sha256(Path(sys.executable).read_bytes()).hexdigest(), 'watcher_binary_sha256': hashlib.sha256(Path(sys.executable).read_bytes()).hexdigest(), 'scope': 'verified-original-host', 'platform_enforcement': 'synthetic-external-proof', 'tool_paths': 'synthetic-external-proof', 'preexisting_hardlink': 'synthetic-external-proof', 'process_paths': 'synthetic-external-proof', 'watch_event_cursor': 0, 'checks': dict.fromkeys(checks, 'PASS')}
    evidence = state / 'validation-evidence'; evidence.mkdir()
    receipt = evidence / 'owned-proof.json'; receipt.write_text(json.dumps(proof))
    (state / 'global-validation-host.json').write_text(json.dumps({attempt['id']: {'path': 'validation-evidence/owned-proof.json', 'sha256': hashlib.sha256(receipt.read_bytes()).hexdigest()}}))
    attempt['boundary'] = host.verify_boundary(attempt)
    return host, attempt


def owned_worker_pid(state):
    import psutil
    watchers = {json.loads(path.read_text())['watcher_pid'] for path in (state / 'validation-native').glob('*-watch-binding.json')}
    for process in psutil.Process().children():
        if process.cwd() == str(state / 'validation-native') and process.pid not in watchers:
            return process.pid
    raise AssertionError('Owned native worker process was not found.')


def test_public_host_result_reaps_ended_native_worker(tmp_path):
    import time
    import psutil
    host, attempt = native_runner(tmp_path, 'import time; time.sleep(0.1); assert True')
    try:
        run = host.start(attempt)
        pid = owned_worker_pid(tmp_path / 'native-state')
        assert host.find_run(attempt) == run
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline and psutil.Process(pid).status() != psutil.STATUS_ZOMBIE:
            time.sleep(0.01)
        result = host.read_result(run['run_id'])
        assert result['tests'][0]['exit_code'] == 0
        assert result['worker_exit_code'] == 0
        assert host.find_run(attempt)['worker_exit_code'] == 0
        with pytest.raises(ChildProcessError):
            os.waitpid(pid, os.WNOHANG)
    finally:
        host.close()



def wait_for_owned_exit(pid):
    import time
    import psutil
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        try:
            if psutil.Process(pid).status() == psutil.STATUS_ZOMBIE:
                return
        except psutil.NoSuchProcess:
            return
        time.sleep(0.01)
    raise AssertionError('The short owned worker did not reach an actual terminal process state.')


def test_public_close_reaps_terminal_worker_without_requiring_result_read(tmp_path):
    host, attempt = native_runner(tmp_path, 'import time; time.sleep(0.1); assert True')
    run = host.start(attempt)
    pid = owned_worker_pid(tmp_path / 'native-state')
    wait_for_owned_exit(pid)
    host.close()
    with pytest.raises(ChildProcessError):
        os.waitpid(pid, os.WNOHANG)
    assert host.find_run(attempt)['worker_exit_code'] == 0
    assert host.read_result(run['run_id'])['worker_exit_code'] == 0


def test_public_result_checks_actual_nonzero_worker_exit(tmp_path):
    host, attempt = native_runner(tmp_path, '\0')
    try:
        run = host.start(attempt)
        pid = owned_worker_pid(tmp_path / 'native-state')
        wait_for_owned_exit(pid)
        with pytest.raises(ManagementError) as failed:
            host.read_result(run['run_id'])
        assert failed.value.code == 'capability_unverified'
        assert host.find_run(attempt)['worker_exit_code'] == 1
        with pytest.raises(ChildProcessError):
            os.waitpid(pid, os.WNOHANG)
    finally:
        host.close()


def test_active_owned_worker_is_preserved_across_public_close(tmp_path):
    import psutil
    import time
    release = tmp_path / 'release-owned-worker'
    code = 'import time; from pathlib import Path\np=Path(' + repr(str(release)) + ')\nwhile not p.exists(): time.sleep(0.01)\nassert p.read_text() == "released"'
    host, attempt = native_runner(tmp_path, code)
    try:
        run = host.start(attempt)
        pid = owned_worker_pid(tmp_path / 'native-state')
        host.close()
        assert psutil.Process(pid).is_running()
        assert os.waitpid(pid, os.WNOHANG) == (0, 0)
        assert host.find_run(attempt) == run
        with pytest.raises(ManagementError):
            host.read_result(run['run_id'])
        release.write_text('released')
        wait_for_owned_exit(pid)
        result = host.read_result(run['run_id'])
        assert result['worker_exit_code'] == result['tests'][0]['exit_code'] == 0
        with pytest.raises(ChildProcessError):
            os.waitpid(pid, os.WNOHANG)
    finally:
        release.write_text('released')
        host.close()
