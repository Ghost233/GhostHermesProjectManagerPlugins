"""Original DSH approval lifecycle; the plugin observer never supplies a decision."""
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import subprocess
import tempfile
import threading

import pytest
from sdk_source_integrity import source_snapshot


@pytest.mark.parametrize('outcome', ['allowed-once', 'rejected', 'cancelled', 'unavailable'])
def test_original_owned_approval_keeps_native_decision_and_tool_result(outcome):
    sdk = os.environ.get('DSH_TEST_SDK_ROOT')
    if not sdk:
        pytest.skip('Original DSH SDK is required.')
    before = source_snapshot(sdk)
    requests = []
    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):
            requests.append(json.loads(self.rfile.read(int(self.headers['Content-Length']))))
            first = len(requests) == 1
            delta = {'tool_calls': [{'index': 0, 'id': 'approval-fixture', 'type': 'function', 'function': {
                'name': 'bash', 'arguments': json.dumps({'command': "printf 'approved\\n' > approval-result.txt",
                    'description': 'Write one synthetic repository file.', 'sandbox_permissions': 'workspace-write',
                    'justification': 'Write this exact synthetic file inside the bound repository.'})}}]} if first else {'content': 'Original tool finished.'}
            self.send_response(200)
            self.send_header('Content-Type', 'text/event-stream')
            self.end_headers()
            for part, reason in (({'role': 'assistant'}, None), (delta, None), ({}, 'tool_calls' if first else 'stop')):
                chunk = {'id': 'approval-stream', 'object': 'chat.completion.chunk', 'created': 1,
                    'model': 'fixture-model', 'choices': [{'index': 0, 'delta': part, 'finish_reason': reason}]}
                self.wfile.write(('data: ' + json.dumps(chunk) + '\n\n').encode())
            self.wfile.write(b'data: [DONE]\n\n')
        def log_message(self, *_):
            pass
    server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        with tempfile.TemporaryDirectory(prefix='hpm-approval-', dir='/tmp') as temporary:
            root = Path(temporary).resolve()
            (root / 'repo').mkdir()
            (root / 'home').mkdir(mode=0o700)
            config = {'dsh_home': str(root / 'home'), 'workspace': str(root / 'repo'), 'runtime_package_root': sdk,
                'instance_id': 'fixture-approval', 'generation': 'fixture-generation', 'session_id': 'session-fixture-approval',
                'runtime_configuration': {'model': {'provider': 'fixture-provider', 'model': 'fixture-model', 'configuration': {
                    'api': 'openai-completions', 'baseURL': f'http://127.0.0.1:{server.server_port}/v1',
                    'apiKeyEnv': 'DSH_FIXTURE_MODEL_KEY', 'models': [{'id': 'fixture-model', 'contextWindow': 16384,
                        'maxTokens': 256, 'input': ['text']}]}}}}
            reference = root / 'config.json'
            reference.write_text(json.dumps(config))
            reference.chmod(0o600)
            process = subprocess.run(['/opt/homebrew/Cellar/node/26.9.0/bin/node',
                str(Path(__file__).with_name('repository_approval_probe.mjs')), str(reference), outcome],
                capture_output=True, text=True, timeout=35,
                env={**os.environ, 'DSH_HOME': config['dsh_home'], 'HOME': config['dsh_home'],
                     'DSH_FIXTURE_MODEL_KEY': 'synthetic-key', 'DSH_TELEMETRY_DISABLED': '1'})
            assert process.returncode == 0, process.stdout + process.stderr
            receipt = json.loads(process.stdout.splitlines()[-1])
            assert receipt['outcome'] == outcome and receipt['observer_supplied_decisions'] == 0
            assert receipt['same_session'] and receipt['same_call'] and receipt['original_exit_code'] == 0
            assert (root / 'repo/approval-result.txt').exists() is (outcome == 'allowed-once')
            assert receipt['tool_failed'] is (outcome != 'allowed-once')
    finally:
        server.shutdown()
        server.server_close()
        thread.join(2)
    assert source_snapshot(sdk) == before
