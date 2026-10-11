"""An owned DSH generation survives the supervisor connection that created it."""
import os
from pathlib import Path
import tempfile
import json
import time
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest
from sdk_source_integrity import source_snapshot


class WorkModel:
    """A finite external model stream; original DSH handles every tool call."""
    def __init__(self, command="printf 'bounded\\n' > delivery.txt", dangerous=False, cached=False, background=False):
        self.requests = []
        owner = self

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self):
                body = json.loads(self.rfile.read(int(self.headers['Content-Length'])))
                owner.requests.append(body)
                self.send_response(200)
                self.send_header('Content-Type', 'text/event-stream')
                self.end_headers()
                tool = len(owner.requests) == 1
                delta = {'tool_calls': [{'index': 0, 'id': 'fixture-write', 'type': 'function',
                    'function': {'name': 'bash', 'arguments': json.dumps({'command': command,
                        'description': 'Run one bounded synthetic tool.', **({'run_in_background': True} if background else {}), **({'sandbox_permissions': 'danger-full-access',
                            'justification': 'Synthetic rejected capability.'} if dangerous else {})})}}]} if tool else {'content': 'Awaiting acceptance.'}
                for part, reason in (({'role': 'assistant'}, None), (delta, None), ({}, 'tool_calls' if tool else 'stop')):
                    data = {'id': 'fixture-response', 'object': 'chat.completion.chunk', 'created': 1, 'model': 'fixture-model',
                            'choices': [{'index': 0, 'delta': part, 'finish_reason': reason}]}
                    self.wfile.write(('data: ' + json.dumps(data) + '\n\n').encode())
                self.wfile.write(('data: ' + json.dumps({'id': 'fixture-response', 'object': 'chat.completion.chunk', 'created': 1,
                    'model': 'fixture-model', 'choices': [], 'usage': {'prompt_tokens': 12, 'completion_tokens': 8, 'total_tokens': 20,
                        **({'prompt_tokens_details': {'cached_tokens': 5, 'cache_write_tokens': 3}} if cached else {})}}) + '\n\n').encode())
                self.wfile.write(b'data: [DONE]\n\n')

            def log_message(self, *_):
                pass

        self.server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.base_url = f'http://127.0.0.1:{self.server.server_port}/v1'

    def close(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(2)
        assert not self.thread.is_alive()


@pytest.mark.parametrize('max_requests,token_limit,cached,expected_requests,npm_probe', [
    (1, 1000, False, 1, False), (3, 1000, False, 2, False),
    (3, 20, False, 1, False), (3, 21, False, 2, False),
    (3, 20, True, 1, False), (3, 21, True, 2, False),
    (3, 1000, False, 2, True),
])
def test_original_work_input_once_and_observed_budget(max_requests, token_limit, cached, expected_requests, npm_probe, tmp_path):
    configured = os.environ.get('DSH_TEST_SDK_ROOT')
    if not configured:
        pytest.skip('Original DSH SDK is absent.')
    sdk = Path(configured).resolve(strict=True)
    before = source_snapshot(sdk)
    from ghost_hermes_pm.dsh_owned_transport import PersistentOwnedTransport
    model = WorkModel("npm config get cache > cache-location.txt && printf 'bounded\\n' > delivery.txt", cached=cached) if npm_probe else WorkModel(cached=cached)
    try:
        with tempfile.TemporaryDirectory(prefix='hpm-work-', dir='/tmp') as temporary:
            root = Path(temporary).resolve()
            repo = root / 'repo'
            repo.mkdir()
            state = root / 'instance'
            state.mkdir(mode=0o700)
            settings = {'dsh_home': str(state / 'home'), 'workspace': str(repo), 'runtime_package_root': str(sdk),
                'instance_id': 'fixture-work', 'generation': 'fixture-generation', 'session_id': 'session-fixture-work',
                'runtime_configuration': {'model': {'provider': 'fixture-provider', 'model': 'fixture-model', 'configuration': {
                    'api': 'openai-completions', 'baseURL': model.base_url, 'apiKeyEnv': 'DSH_FIXTURE_MODEL_KEY',
                    'models': [{'id': 'fixture-model', 'contextWindow': 16384, 'maxTokens': 256, 'input': ['text']}]}},
                    'budget': {'max_wall_seconds': 15, 'max_model_requests': max_requests, 'max_reported_tokens': token_limit,
                               'max_output_tokens_per_request': 256}}}
            carrier = PersistentOwnedTransport(instance_dir=str(state), configuration=settings, timeout=15,
                                                environment={'DSH_FIXTURE_MODEL_KEY': 'synthetic-key'})
            diagnostics = None
            diagnostic_path = tmp_path / 'native-work-failure.json'
            try:
                sid = settings['session_id']
                assert carrier.request('session/create', {'request': {'sessionId': sid, 'cwd': str(repo),
                    'agentPreset': 'hermes-owned'}}, 'create')['result']['ok']
                args = {'request': {'sessionId': sid, 'requestId': 'input-fixture-once', 'mode': 'queue',
                        'content': [{'type': 'text', 'text': 'Perform the single bounded work.'}]}}
                accepted = carrier.request('session/prompt', args, 'prompt')['result']
                assert accepted['ok'] and accepted['value']['accepted']
                repeated = carrier.request('session/prompt', args, 'prompt-again')['result']
                assert repeated == accepted, 'Repeated supervision must return the saved first-input receipt.'
                deadline = time.monotonic() + 15
                failed_journal = None
                while time.monotonic() < deadline:
                    listing = carrier.request('session/list', {'_request': {}}, 'poll')['result']['value']['items']
                    item = next(row for row in listing if row['sessionId'] == sid)
                    if (repo / 'delivery.txt').exists() and not item['running']:
                        break
                    if model.requests and not item['running'] and not (repo / 'delivery.txt').exists():
                        stream = carrier.stream('session/follow', {'request': {'address': {'kind': 'session', 'sessionId': sid}, 'maxMessages': 500}})
                        try:
                            journal = next(stream)
                        finally:
                            stream.close()
                        if any(row['event']['type'] == 'tool/result'
                               and row['event']['data'].get('error', {}).get('code') for row in journal['records']):
                            failed_journal = journal
                            break
                    time.sleep(.05)
                if not (repo / 'delivery.txt').exists():
                    diagnostics = {'model_requests': model.requests, 'native_session': item, 'receipts': {}}
                    if failed_journal is not None:
                        diagnostics['native_journal'] = failed_journal
                    else:
                        stream = carrier.stream('session/follow', {'request': {'address': {'kind': 'session', 'sessionId': sid}, 'maxMessages': 500}})
                        try:
                            diagnostics['native_journal'] = next(stream)
                        finally:
                            stream.close()
                    for name in ('.hermes-budget.json', '.hermes-tool-receipts.jsonl', '.hermes-native-jobs.jsonl'):
                        receipt = state / 'home' / name
                        diagnostics['receipts'][name] = receipt.read_text() if receipt.exists() else None
                    diagnostic_path.write_text(json.dumps(diagnostics))
                    diagnostic_path.chmod(0o600)
                    print('Original work failure evidence:', diagnostic_path)
                assert (repo / 'delivery.txt').read_text() == 'bounded\n'
                if npm_probe:
                    assert (repo / 'cache-location.txt').read_text().strip() == str(repo / '.cache/npm'), 'Original npm must use the bound repository cache.'
                assert item['running'] is False
                budget = json.loads((state / 'home/.hermes-budget.json').read_text())
                assert budget['usage']['totals'] == {
                    'uncachedInputTokens': (4 if cached else 12) * expected_requests,
                    'outputTokens': 8 * expected_requests,
                    'cacheReadTokens': (5 if cached else 0) * expected_requests,
                    'cacheWriteTokens': (3 if cached else 0) * expected_requests,
                }, budget
                assert len(model.requests) == expected_requests
                assert all(sorted(t['function']['name'] for t in row['tools']) ==
                           ['ask_user_question', 'bash', 'job_kill', 'job_list', 'job_output'] for row in model.requests)
                assert all(row.get('max_tokens', row.get('max_completion_tokens')) == 256 for row in model.requests)
                stream = carrier.stream('session/follow', {'request': {'address': {'kind': 'session', 'sessionId': sid}, 'maxMessages': 500}})
                try:
                    snapshot = next(stream)
                finally:
                    stream.close()
                assert snapshot['header']['id'] == sid and snapshot['header']['cwd'] == str(repo)
                receipt = json.loads((state / 'home/.hermes-first-input.json').read_text())
                assert receipt['status'] == 'accepted' and receipt['request_id'] == 'input-fixture-once'
                if expected_requests == 2:
                    receipt['status'] = 'intent'
                    (state / 'home/.hermes-first-input.json').write_text(json.dumps(receipt))
                    count = len(model.requests)
                    with pytest.raises(OSError):
                        carrier.request('session/prompt', args, 'unknown-must-not-send')
                    time.sleep(.1)
                    assert len(model.requests) == count, 'Unknown first input cannot be resent or replaced.'
                if expected_requests == 1:
                    # Original turnBoundary has no wire definition; require its durable cause instead.
                    ends = [r['event'] for r in snapshot['records'] if r['event']['type'] == 'turn/end']
                    assert len(ends) == 1 and ends[0]['data']['reason']['kind'] == 'aborted', ends
                    assert ends[0]['data']['reason']['reason']['reason'] == 'owned-budget-observed-limit'
                    budget = json.loads((state / 'home/.hermes-budget.json').read_text())
                    assert budget['stop_reason'] == 'owned-budget-observed-limit' and budget['requests'] == 1
            finally:
                carrier.shutdown_owned()
                if diagnostics is not None:
                    diagnostics['native_close_outcome'] = carrier.close_outcome
                    diagnostics['sdk_bytes_unchanged'] = source_snapshot(sdk) == before
                    diagnostic_path.write_text(json.dumps(diagnostics))
                    diagnostic_path.chmod(0o600)
            assert carrier.close_outcome['kind'] == 'original_exit'
    finally:
        model.close()
    assert source_snapshot(sdk) == before


@pytest.mark.parametrize('background', [False, True])
@pytest.mark.parametrize('boundary', ['outside', 'symlink', 'hardlink', 'danger', 'temporary', 'runtime'])
def test_original_bash_write_boundary_and_aliases(boundary, background):
    configured = os.environ.get('DSH_TEST_SDK_ROOT')
    if not configured:
        pytest.skip('Original DSH SDK is absent.')
    sdk = Path(configured).resolve(strict=True)
    before = source_snapshot(sdk)
    from ghost_hermes_pm.dsh_owned_transport import PersistentOwnedTransport
    outside_root = str(Path(os.environ.get('DSH_TEST_OUTSIDE_TMP_ROOT', tempfile.gettempdir())).resolve())
    if Path(outside_root).is_relative_to(Path('/tmp').resolve()):
        pytest.skip('A self-owned temporary root outside the actual DSH /tmp grant is required.')
    with tempfile.TemporaryDirectory(prefix='hpm-outside-', dir=outside_root) as external, tempfile.TemporaryDirectory(prefix='hpm-boundary-', dir='/tmp') as temporary:
        root = Path(temporary).resolve()
        repo = root / 'repo'
        repo.mkdir()
        state = Path(external) / 'instance'
        state.mkdir(mode=0o700)
        sentinel = Path(external) / 'sentinel'
        sentinel.write_text('unchanged\n')
        quoted = __import__('shlex').quote(str(sentinel))
        command = f"printf 'changed\\n' > {quoted}"
        if boundary in {'symlink', 'hardlink'}:
            command = ('ln -s ' if boundary == 'symlink' else 'ln ') + quoted + " alias.txt && printf 'changed\\n' > alias.txt"
        elif boundary == 'temporary':
            command = "printf 'temporary\\n' > \"$TMPDIR/temporary.txt\""
        if boundary == 'runtime':
            sentinel = state / 'protected-runtime.txt'
            sentinel.write_text('unchanged\n')
            command = "printf 'changed\\n' > " + __import__('shlex').quote(str(sentinel))
        model = WorkModel(command, dangerous=boundary == 'danger', background=background)
        settings = {'dsh_home': str(state / 'home'), 'workspace': str(repo), 'runtime_package_root': str(sdk),
            'instance_id': 'fixture-boundary', 'generation': 'fixture-generation', 'session_id': 'session-fixture-boundary',
            'runtime_configuration': {'model': {'provider': 'fixture-provider', 'model': 'fixture-model', 'configuration': {
                'api': 'openai-completions', 'baseURL': model.base_url, 'apiKeyEnv': 'DSH_FIXTURE_MODEL_KEY',
                'models': [{'id': 'fixture-model', 'contextWindow': 16384, 'maxTokens': 256, 'input': ['text']}]}}}}
        carrier = PersistentOwnedTransport(instance_dir=str(state), configuration=settings, timeout=15,
                                            environment={'DSH_FIXTURE_MODEL_KEY': 'synthetic-key'})
        try:
            sid = settings['session_id']
            assert carrier.request('session/create', {'request': {'sessionId': sid, 'cwd': str(repo), 'agentPreset': 'hermes-owned'}}, 'create')['result']['ok']
            assert carrier.request('session/prompt', {'request': {'sessionId': sid, 'requestId': 'input-boundary', 'mode': 'queue',
                'content': [{'type': 'text', 'text': 'Run the single finite boundary probe.'}]}}, 'prompt')['result']['value']['accepted']
            receipt_path = state / 'home/.hermes-tool-receipts.jsonl'
            deadline = time.monotonic() + 15
            while not receipt_path.exists() and time.monotonic() < deadline:
                time.sleep(.05)
            assert receipt_path.exists(), 'Original tool settlement was not observed.'
            receipt = json.loads(receipt_path.read_text().splitlines()[0])
            job = None
            if background and boundary != 'danger':
                assert receipt['kind'] == 'background'
                deadline = time.monotonic() + 15
                while time.monotonic() < deadline:
                    stream = carrier.stream('job/list', {'request': {'sessionId': sid}})
                    try:
                        roster = next(stream)
                    finally:
                        stream.close()
                    job = next(row for row in roster['jobs'] if row['id'] == receipt['job_id'])
                    if job['status'] in {'completed', 'killed', 'failed'}:
                        break
                    time.sleep(.025)
                assert job['owner'] == sid and job['status'] == 'completed', job
            assert receipt['generation'] == settings['generation'] and receipt['session_id'] == sid
            assert sentinel.read_text() == 'unchanged\n', {'boundary': boundary, 'receipt': receipt}
            if boundary == 'danger':
                assert receipt['is_error'] is True and receipt['execution_admission'] == 'rejected' and 'exit_code' not in receipt
            elif boundary == 'temporary':
                assert (job and job['detail'] == 'exit code: 0') if background else receipt['exit_code'] == 0
                assert (state / 'home/tmp/temporary.txt').read_text() == 'temporary\n'
            else:
                if background:
                    assert '[sandbox:' in job['detail'] and 'exit code: 0;' not in job['detail'], job
                else:
                    assert receipt['exit_code'] != 0 and receipt['sandbox']['denied'] is True, receipt
        finally:
            carrier.shutdown_owned()
            model.close()
        assert carrier.close_outcome['kind'] == 'original_exit'
    assert source_snapshot(sdk) == before


def test_reconnect_uses_same_original_owned_instance_and_session():
    configured = os.environ.get('DSH_TEST_SDK_ROOT')
    if not configured:
        if os.environ.get('DSH_REQUIRE_SDK_SMOKE') == '1':
            pytest.fail('Original DSH SDK is required.')
        pytest.skip('Original DSH SDK is absent; no persistent instance claim.')
    sdk = Path(configured).resolve(strict=True)
    before = source_snapshot(sdk)
    from ghost_hermes_pm.dsh_owned_transport import PersistentOwnedTransport
    with tempfile.TemporaryDirectory(prefix='hpm-persist-', dir='/tmp') as temporary:
        root = Path(temporary).resolve()
        workspace = root / 'repo'
        workspace.mkdir()
        state = root / 'instance'
        state.mkdir(mode=0o700)
        settings = {'dsh_home': str(state / 'home'), 'workspace': str(workspace),
                    'runtime_package_root': str(sdk), 'instance_id': 'fixture-instance',
                    'generation': 'fixture-generation', 'session_id': 'session-fixture-persistent'}
        first = PersistentOwnedTransport(instance_dir=str(state), configuration=settings, timeout=15)
        try:
            created = first.request('session/create', {'request': {'sessionId': settings['session_id'],
                'cwd': str(workspace), 'agentPreset': 'hermes-owned'}}, 'create-first')
            assert created['result']['ok'] is True
            identity = dict(first.native_identity)
        finally:
            first.close()
        second = PersistentOwnedTransport(instance_dir=str(state), configuration=settings, timeout=15)
        try:
            listed = second.request('session/list', {'_request': {}}, 'read-reconnected')
            assert second.native_identity == identity, 'Connection loss cannot replace the original execution generation.'
            assert listed['result']['ok'] is True
            assert any(item['sessionId'] == settings['session_id'] for item in listed['result']['value']['items'])
        finally:
            second.shutdown_owned()
        assert second.close_outcome['kind'] == 'original_exit' and second.close_outcome['exit_code'] == 0
    assert source_snapshot(sdk) == before


def test_original_managed_job_rejects_foreign_session_cancel_and_keeps_six_tools():
    configured = os.environ.get('DSH_TEST_SDK_ROOT')
    if not configured:
        pytest.skip('Original DSH SDK is absent.')
    sdk = Path(configured).resolve(strict=True)
    before = source_snapshot(sdk)
    from ghost_hermes_pm.dsh_owned_transport import PersistentOwnedTransport
    command = "node -e \"require('fs').writeFileSync('managed.pid',String(process.pid)); setTimeout(()=>{},25000)\""
    model = WorkModel(command, background=True)
    with tempfile.TemporaryDirectory(prefix='hpm-job-owner-', dir='/tmp') as temporary:
        root = Path(temporary).resolve()
        repo, state, skills = root / 'repo', root / 'instance', root / 'skills'
        repo.mkdir()
        state.mkdir(mode=0o700)
        (skills / 'fixture-workflow').mkdir(parents=True)
        (skills / 'fixture-workflow/SKILL.md').write_text('---\nname: fixture-workflow\ndescription: Synthetic workflow.\n---\nReport actual results.\n')
        sid = 'fixture-managed-job-session'
        configuration = {'dsh_home': str(state / 'home'), 'workspace': str(repo), 'runtime_package_root': str(sdk),
            'instance_id': 'fixture-managed-job', 'generation': 'fixture-managed-generation', 'session_id': sid,
            'runtime_configuration': {'skill_directories': [str(skills)], 'model': {
                'provider': 'fixture-provider', 'model': 'fixture-model', 'configuration': {
                    'api': 'openai-completions', 'baseURL': model.base_url, 'apiKeyEnv': 'DSH_FIXTURE_MODEL_KEY',
                    'models': [{'id': 'fixture-model', 'contextWindow': 16384, 'maxTokens': 256, 'input': ['text']}]}}}}
        carrier = PersistentOwnedTransport(instance_dir=str(state), configuration=configuration, timeout=15,
                                            environment={'DSH_FIXTURE_MODEL_KEY': 'synthetic-key'})
        try:
            assert carrier.request('session/create', {'request': {'sessionId': sid, 'cwd': str(repo),
                'agentPreset': 'hermes-owned'}}, 'create')['result']['ok']
            assert carrier.request('session/prompt', {'request': {'sessionId': sid, 'requestId': 'managed-input', 'mode': 'queue',
                'content': [{'type': 'text', 'text': 'Start the single bounded original managed job.'}]}}, 'prompt')['result']['value']['accepted']
            deadline = time.monotonic() + 15
            receipt_path = state / 'home/.hermes-tool-receipts.jsonl'
            while (not (repo / 'managed.pid').exists() or not receipt_path.exists()) and time.monotonic() < deadline:
                time.sleep(.025)
            assert (repo / 'managed.pid').exists() and receipt_path.exists()
            receipt = json.loads(receipt_path.read_text().splitlines()[0])
            assert receipt['kind'] == 'background' and receipt['session_id'] == sid
            import psutil
            child = psutil.Process(int((repo / 'managed.pid').read_text()))
            birth = child.create_time()
            for index, request in enumerate(({'sessionId': 'foreign-session', 'jobId': receipt['job_id']},
                    {'sessionId': sid, 'jobId': 'absent-job'}, {'jobId': receipt['job_id']})):
                with pytest.raises(OSError):
                    carrier.request('job/kill', {'request': request}, 'rejected-' + str(index))
                assert child.is_running() and child.create_time() == birth
            result = carrier.request('job/kill', {'request': {'sessionId': sid, 'jobId': receipt['job_id']}}, 'owned-cancel')
            assert result['result']['ok'] and result['result']['value']['outcome'] == 'requested'
            deadline = time.monotonic() + 10
            while time.monotonic() < deadline:
                stream = carrier.stream('job/list', {'request': {'sessionId': sid}})
                try:
                    row = next(stream)['jobs'][0]
                finally:
                    stream.close()
                if row['status'] == 'killed':
                    break
                time.sleep(.025)
            assert row['owner'] == sid and row['status'] == 'killed'
            if psutil.pid_exists(child.pid):
                assert psutil.Process(child.pid).create_time() == birth
                child.wait(timeout=5)
            assert all(sorted(tool['function']['name'] for tool in request['tools']) ==
                ['ask_user_question', 'bash', 'job_kill', 'job_list', 'job_output', 'skill'] for request in model.requests)
        finally:
            carrier.shutdown_owned()
            model.close()
        assert carrier.close_outcome['kind'] == 'original_exit'
        cleanup = json.loads((state / 'home/.hermes-native-cleanup.json').read_text())
        assert cleanup['status'] == 'completed' and cleanup['session_id'] == sid
    assert source_snapshot(sdk) == before


def test_silent_owned_response_is_typed_unknown_without_replacing_session():
    """Stop only this original carrier; its unanswered public RPC must stay unknown."""
    import signal
    from ghost_hermes_pm.dsh_owned_transport import PersistentOwnedTransport
    from ghost_hermes_pm.repository_supervision import owned_call
    from ghost_hermes_pm.manager import ManagementError
    configured = os.environ.get('DSH_TEST_SDK_ROOT')
    if not configured:
        if os.environ.get('DSH_REQUIRE_SDK_SMOKE') == '1':
            pytest.fail('Original DSH SDK is required.')
        pytest.skip('Original DSH SDK is absent.')
    sdk = Path(configured).resolve(strict=True)
    before = source_snapshot(sdk)
    with tempfile.TemporaryDirectory(prefix='hpm-silent-', dir='/tmp') as temporary:
        root = Path(temporary).resolve()
        repo = root / 'repo'
        repo.mkdir()
        state = root / 'instance'
        state.mkdir(mode=0o700)
        configuration = {'dsh_home': str(state / 'home'), 'workspace': str(repo),
            'runtime_package_root': str(sdk), 'instance_id': 'fixture-silent',
            'generation': 'fixture-silent-generation', 'session_id': 'fixture-silent-session'}
        carrier = PersistentOwnedTransport(instance_dir=str(state), configuration=configuration, timeout=15)
        paused = False
        try:
            created = owned_call(carrier, 'session/create', {'sessionId': configuration['session_id'],
                'cwd': str(repo), 'agentPreset': 'hermes-owned'})
            assert created['sessionId'] == configuration['session_id']
            identity = dict(carrier.native_identity)
            os.kill(identity['pid'], signal.SIGSTOP)
            paused = True
            with pytest.raises(ManagementError) as failure:
                owned_call(carrier, 'session/list', {})
            assert failure.value.code == 'outcome_unknown'
            import queue
            assert isinstance(failure.value.__cause__, queue.Empty)
            assert carrier.native_identity == identity
            assert json.loads(carrier.identity_path.read_text()) == identity
        finally:
            if paused:
                os.kill(carrier.native_identity['pid'], signal.SIGCONT)
            carrier.shutdown_owned()
        assert carrier.close_outcome['kind'] == 'original_exit'
    assert source_snapshot(sdk) == before


@pytest.mark.parametrize('error', [OSError('Original owned socket failed.'), TimeoutError('Original owned reply timed out.'),
    __import__('psutil').NoSuchProcess(pid=987654321), __import__('psutil').AccessDenied(pid=987654321),
    ValueError('Unexpected malformed request.'), KeyError('Unexpected missing field.')])
def test_owned_response_preserves_known_failure_cause_and_does_not_retry(error):
    """A failure-only peer tests normalization; no SDK class or successful reply is replaced."""
    from types import SimpleNamespace
    from ghost_hermes_pm.repository_supervision import owned_call
    from ghost_hermes_pm.manager import ManagementError
    calls = []
    def request(*args):
        calls.append(args)
        raise error
    known = not isinstance(error, (ValueError, KeyError))
    with pytest.raises(ManagementError if known else type(error)) as failure:
        owned_call(SimpleNamespace(request=request), 'session/list', {})
    if known:
        assert failure.value.code == 'outcome_unknown'
        assert failure.value.__cause__ is error
        assert type(error).__name__ in str(failure.value)
    else:
        assert failure.value is error, 'Programming errors must retain their original type.'
    assert len(calls) == 1, 'An unknown original response is never automatically resent.'


def test_disappeared_original_owned_pid_retains_configuration_and_session():
    import signal
    import psutil
    from ghost_hermes_pm.dsh_owned_transport import PersistentOwnedTransport
    from ghost_hermes_pm.repository_supervision import owned_call
    from ghost_hermes_pm.manager import ManagementError
    configured = os.environ.get('DSH_TEST_SDK_ROOT')
    if not configured:
        if os.environ.get('DSH_REQUIRE_SDK_SMOKE') == '1':
            pytest.fail('Original DSH SDK is required.')
        pytest.skip('Original DSH SDK is absent.')
    sdk = Path(configured).resolve(strict=True)
    before = source_snapshot(sdk)
    with tempfile.TemporaryDirectory(prefix='hpm-lost-', dir='/tmp') as temporary:
        root = Path(temporary).resolve()
        repo = root / 'repo'
        repo.mkdir()
        state = root / 'instance'
        state.mkdir(mode=0o700)
        settings = {'dsh_home': str(state / 'home'), 'workspace': str(repo), 'runtime_package_root': str(sdk),
            'instance_id': 'fixture-lost', 'generation': 'fixture-lost-generation', 'session_id': 'fixture-lost-session'}
        carrier = PersistentOwnedTransport(instance_dir=str(state), configuration=settings, timeout=15)
        resumed = None
        try:
            created = owned_call(carrier, 'session/create', {'sessionId': settings['session_id'],
                'cwd': str(repo), 'agentPreset': 'hermes-owned'})
            assert created['sessionId'] == settings['session_id']
            saved = {path: path.read_bytes() for path in (carrier.configuration_path, carrier.identity_path, carrier.birth_path)}
            identity = dict(carrier.native_identity)
            birth = json.loads(carrier.birth_path.read_text())
            original = psutil.Process(identity['pid'])
            assert original.create_time() == birth['created_at']
            carrier.close()
            os.kill(identity['pid'], signal.SIGKILL)
            assert original.wait(timeout=5) == -signal.SIGKILL
            resumed = PersistentOwnedTransport(instance_dir=str(state), configuration=settings, timeout=15)
            with pytest.raises(ManagementError) as failure:
                owned_call(resumed, 'session/list', {})
            assert failure.value.code == 'outcome_unknown'
            assert isinstance(failure.value.__cause__, psutil.NoSuchProcess)
            assert all(path.read_bytes() == value for path, value in saved.items())
            assert not (state / 'home/.hermes-first-input.json').exists(), 'Lost original identity cannot send a new input.'
        finally:
            if resumed:
                resumed.close()
            carrier.close()
            if carrier._process is not None and carrier._process.poll() is None:
                carrier.shutdown_owned()
            if carrier.socket_path.exists():
                assert not carrier.socket_path.is_symlink()
                carrier.socket_path.unlink()
                carrier.socket_path.parent.rmdir()
    assert source_snapshot(sdk) == before


@pytest.mark.parametrize('binding_change', ['match', 'digest', 'controller', 'work', 'runtime', 'home', 'source'])
def test_attach_only_retains_original_profile_configuration(tmp_path, binding_change):
    """Two installed plugin copies must attach to the same pinned native scope."""
    import importlib
    import shutil
    import sys
    import types
    from ghost_hermes_pm.trusted_controller import controller_identity
    sdk = Path(os.environ['DSH_TEST_SDK_ROOT']).resolve(strict=True)
    root = Path(__file__).resolve().parents[1]
    modules = []
    for profile in ['child', 'manager']:
        directory = tmp_path / profile / 'ghost_hermes_pm'
        shutil.copytree(root / 'ghost_hermes_pm', directory, ignore=shutil.ignore_patterns('__pycache__'))
        name = 'native_attach_' + profile
        for key in list(sys.modules):
            if key == name or key.startswith(name + '.'):
                del sys.modules[key]
        package = types.ModuleType(name)
        package.__path__ = [str(directory)]
        sys.modules[name] = package
        modules.append(importlib.import_module(name + '.dsh_owned_transport'))
    repo = tmp_path / 'repo'
    repo.mkdir()
    instance = tmp_path / 'instance'
    instance.mkdir(mode=0o700)
    home = instance / 'home'
    home.mkdir(mode=0o700)
    expected = {'dsh_home': str(home), 'workspace': str(repo), 'runtime_package_root': str(sdk),
        'instance_id': 'fixture-cross-profile', 'generation': 'fixture-cross-generation', 'session_id': 'fixture-cross-session',
        'trusted_controllers': [controller_identity()],
        'runtime_configuration': {'budget': {'max_wall_seconds': 15}}}
    original = modules[0].PersistentOwnedTransport(instance_dir=str(instance), configuration=expected,
        node_bin='/opt/homebrew/bin/node')
    # The original writer appends this actual Home identity after its digest is fixed.
    saved = dict(original.config, home_identity={'device': str(home.stat().st_dev), 'inode': str(home.stat().st_ino)})
    path = instance / 'native-configuration.json'
    path.write_text(json.dumps(saved))
    path.chmod(0o600)
    before = path.read_bytes()
    altered_source = None
    original_source = None
    old_home = None
    if binding_change == 'digest':
        corrupt = dict(saved, configuration_sha256='0'*64)
        path.write_text(json.dumps(corrupt))
        before = path.read_bytes()
    elif binding_change == 'controller':
        expected['trusted_controllers'] = [{**expected['trusted_controllers'][0], 'start_usec': '0'}]
    elif binding_change == 'work':
        expected['generation'] = 'different-original-generation'
    elif binding_change == 'runtime':
        expected['runtime_configuration'] = {'budget': {'max_wall_seconds': 16}}
    elif binding_change == 'home':
        old_home = instance / 'original-home'
        home.rename(old_home)
        home.mkdir(mode=0o700)
    elif binding_change == 'source':
        altered_source = Path(saved['source_bindings'][7]['path'])
        original_source = altered_source.read_bytes()
        altered_source.write_bytes(original_source + b'\n// synthetic source change\n')
    attached = None
    try:
        if binding_change != 'match':
            ManagementError = importlib.import_module(modules[1].__package__ + '.manager').ManagementError
            with pytest.raises(ManagementError) as rejected:
                modules[1].PersistentOwnedTransport(instance_dir=str(instance), configuration=expected,
                    node_bin='/opt/homebrew/bin/node', attach_only=True)
            assert rejected.value.code == 'binding_conflict'
        else:
            attached = modules[1].PersistentOwnedTransport(instance_dir=str(instance), configuration=expected,
                node_bin='/opt/homebrew/bin/node', attach_only=True)
            assert attached.config == saved, 'Attach-only must retain the validated original full configuration.'
            assert attached.driver == Path(saved['source_bindings'][6]['path'])
        assert path.read_bytes() == before, 'Attach cannot rewrite the persisted digest.'
    finally:
        if attached is not None:
            attached.close()
        if altered_source is not None:
            altered_source.write_bytes(original_source)
        if old_home is not None:
            home.rmdir()
            old_home.rename(home)
        original.close()


def test_original_sdk_attach_across_profile_sources(tmp_path):
    import importlib
    import shutil
    import sys
    import types
    import psutil
    from ghost_hermes_pm.trusted_controller import controller_identity
    from simple_worker_model import execution_reference
    from ghost_hermes_pm.repository_execution import execution_configuration
    sdk = Path(os.environ['DSH_TEST_SDK_ROOT']).resolve(strict=True)
    sdk_before = source_snapshot(sdk)
    root = Path(__file__).resolve().parents[1]
    modules = []
    for profile in ['child', 'manager']:
        directory = tmp_path / profile / 'ghost_hermes_pm'
        shutil.copytree(root / 'ghost_hermes_pm', directory, ignore=shutil.ignore_patterns('__pycache__'))
        name = 'native_live_attach_' + profile
        package = types.ModuleType(name)
        package.__path__ = [str(directory)]
        sys.modules[name] = package
        modules.append(importlib.import_module(name + '.dsh_owned_transport'))
    repo = tmp_path / 'repo'
    repo.mkdir()
    state = tmp_path / 'state'
    state.mkdir(mode=0o700)
    work_id = 'work-' + 'a'*32
    instance = state / 'owned-work' / work_id
    model = WorkModel()
    original = attached = None
    try:
        reference = execution_reference(tmp_path, model.base_url, sdk)
        target = {'repo_path': str(repo), 'execution_ref': reference}
        configuration, environment = execution_configuration(target)
        node = configuration.pop('node_bin')
        controller = controller_identity()
        alias = tmp_path / 'child-python'
        alias.symlink_to(sys.executable)
        configuration.update(dsh_home=str(instance / 'home'), workspace=str(repo), instance_id=work_id,
            generation='fixture-profile-generation', session_id='fixture-profile-session', trusted_controllers=[controller],
            test_python=str(alias), test_runner_path=str(tmp_path / 'child/ghost_hermes_pm/repository_test_runner.py'))
        original = modules[0].PersistentOwnedTransport(instance_dir=str(instance), configuration=configuration,
            node_bin=node, timeout=15, environment=environment)
        sid = configuration['session_id']
        assert original.request('session/create', {'request': {'sessionId': sid, 'cwd': str(repo),
            'agentPreset': 'hermes-owned'}}, 'fixture-profile-create')['result']['ok']
        assert original.request('session/prompt', {'request': {'sessionId': sid, 'requestId': 'fixture-original-input',
            'mode': 'queue', 'content': [{'type': 'text', 'text': 'Write the bounded synthetic delivery.'}]}},
            'fixture-profile-prompt')['result']['value']['accepted'] is True
        deadline = time.monotonic() + 15
        while time.monotonic() < deadline:
            rows = original.request('session/list', {'_request': {}}, 'fixture-before-attach')['result']['value']['items']
            if (repo / 'delivery.txt').exists() and any(row['sessionId'] == sid and row['running'] is False for row in rows):
                break
            time.sleep(.05)
        assert (repo / 'delivery.txt').read_text() == 'bounded\n'
        path = instance / 'native-configuration.json'
        saved_bytes = path.read_bytes()
        saved = json.loads(saved_bytes)
        budget_bytes = (instance / 'home/.hermes-budget.json').read_bytes()
        model_calls = len(model.requests)
        record = {'id': work_id, 'target': target, 'gateway_controller': controller,
            'dsh_execution': {'native_identity': dict(original.native_identity), 'reference_sha256': configuration['reference_sha256'],
                'instance_id': work_id, 'generation': configuration['generation'], 'session_id': sid}}
        supervision = importlib.import_module('native_live_attach_manager.repository_supervision')
        ManagementError = importlib.import_module('native_live_attach_manager.manager').ManagementError
        attached = supervision.attach_existing_owned(types.SimpleNamespace(state_dir=state), record)
        observed = attached.request('session/list', {'_request': {}}, 'fixture-manager-attach')['result']['value']['items']
        assert any(row['sessionId'] == sid and row['running'] is False for row in observed)
        assert attached.native_identity == original.native_identity
        waiting = supervision.attach_existing_owned(types.SimpleNamespace(state_dir=state), record)
        corrupt = json.loads(saved_bytes)
        corrupt['runtime_configuration']['budget']['max_wall_seconds'] += 1
        path.write_text(json.dumps(corrupt))
        try:
            with pytest.raises(ManagementError) as rejected:
                waiting.request('session/list', {'_request': {}}, 'fixture-changed-configuration')
            assert rejected.value.code == 'binding_conflict'
        finally:
            path.write_bytes(saved_bytes)
            waiting.close()
        waiting = supervision.attach_existing_owned(types.SimpleNamespace(state_dir=state), record)
        source = Path(saved['source_bindings'][7]['path'])
        source_bytes = source.read_bytes()
        source.write_bytes(source_bytes + b'\n// synthetic late source change\n')
        try:
            with pytest.raises(ManagementError) as rejected:
                waiting.request('session/list', {'_request': {}}, 'fixture-changed-source')
            assert rejected.value.code == 'invalid_change'
        finally:
            source.write_bytes(source_bytes)
            waiting.close()
        assert attached.config == saved and attached.driver == Path(saved['source_bindings'][6]['path'])
        assert attached.config['test_python'] == str(alias)
        assert attached.config['test_runner_path'] != str(tmp_path / 'manager/ghost_hermes_pm/repository_test_runner.py')
        assert path.read_bytes() == saved_bytes and (instance / 'home/.hermes-budget.json').read_bytes() == budget_bytes
        assert len(model.requests) == model_calls, 'Cross-profile observation cannot send replacement input.'
        assert sorted(t['function']['name'] for t in model.requests[0]['tools']) == ['ask_user_question','bash','job_kill','job_list','job_output','skill']
        attached.shutdown_owned()
        assert attached.close_outcome['kind'] == 'original_exit' and attached.close_outcome['exit_code'] == 0
        assert psutil.Process(original.native_identity['pid']).wait(timeout=5) == 0
        evidence = Path(os.environ.get('HPM_ATTACH_EVIDENCE_DIR', '/private/tmp/hermes-cross-profile-attach-20261011/native-evidence'))
        evidence.mkdir(mode=0o700, parents=True, exist_ok=True)
        (evidence/'original-cross-profile.json').write_text(json.dumps({'native_identity':original.native_identity,
            'manager_close':attached.close_outcome,'original_configuration_sha256':saved['configuration_sha256'],
            'source_bindings':saved['source_bindings'],'same_session':sid,'model_requests_before_after':[model_calls,len(model.requests)],
            'budget_unchanged':True,'configuration_unchanged':True,'native_exit':json.loads((instance/'native-exit.json').read_text())}))
    finally:
        if attached is not None:
            attached.close()
        if original is not None:
            if not (instance/'native-exit.json').exists():
                original.shutdown_owned()
                assert original.close_outcome['kind'] == 'original_exit'
            original.close()
        model.close()
    assert source_snapshot(sdk) == sdk_before
