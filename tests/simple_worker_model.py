"""Finite external model responses for the real native dispatcher and DSH."""
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import threading
import fcntl
import time


class WorkerModelService:
    def __init__(self, query_lock=None, refuse=False, hold_dsh=False):
        self.requests = []
        self.worker_steps = 0
        self.dsh_steps = 0
        self.foreign_task_id = None
        self.query_ready = threading.Event()
        self.dsh_ready = threading.Event()
        self.dsh_release = threading.Event()
        service = self

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self):
                payload = json.loads(self.rfile.read(int(self.headers['Content-Length'])))
                names = [tool['function']['name'] for tool in payload.get('tools', [])]
                worker = payload['model'] == 'fixture-model'
                expected_key = 'synthetic-model-only-key' if worker else 'synthetic-only-key'
                authenticated = self.headers.get('Authorization') == 'Bearer ' + expected_key
                service.requests.append({'worker': worker, 'tool_names': names,
                    'authentication_accepted': authenticated,
                    'outer_instructions_present': any('Managed outer card:' in str(m.get('content', '')) for m in payload.get('messages', [])),
                    'skill_catalog': any('fixture-matt' in str(m.get('content', '')) for m in payload.get('messages', []))})
                if not authenticated:
                    self.send_response(401)
                    self.send_header('Content-Type', 'application/json')
                    self.end_headers()
                    self.wfile.write(json.dumps({'error': {'message': 'Synthetic model authentication failed.',
                        'type': 'authentication_error'}}).encode())
                    return
                if worker:
                    service.worker_steps += 1
                    step = service.worker_steps
                    extra = []
                    call = (('kanban_create', {'title': 'Forbidden fine card', 'assignee': 'default'}) if step == 1 else
                        ('hermes_pm_supervise', {}) if step == 2 else
                        ('kanban_block', {'task_id': service.foreign_task_id, 'kind': 'needs_input', 'reason': 'Fixture wrong target block.'}) if step == 3 else
                        ('kanban_block', {'kind': 'needs_input', 'reason': 'Dedicated execution awaits acceptance.'}) if step == 4 else
                        ('kanban_block', {'kind': 'needs_input', 'reason': 'Fixture stale claim block.'}) if step == 5 else None)
                    if step == 1:
                        extra = [('kanban_block', {'kind': 'needs_input', 'reason': 'Fixture premature block.'})]
                    elif step == 5:
                        extra = [('kanban_complete', {'summary': 'Forbidden premature delivery.'})]
                    if refuse:
                        call = None
                        extra = []
                else:
                    extra = []
                    service.dsh_steps += 1
                    call = (('skill', {'name': 'fixture-matt'}) if service.dsh_steps == 1 else
                        ('bash', {'description': 'Write and verify one bounded synthetic work result.',
                                 'command': "printf 'native-worker\\n' > native-delivery.txt && cat native-delivery.txt"}) if service.dsh_steps == 2 else None)
                    if hold_dsh and service.dsh_steps == 1:
                        service.dsh_ready.set()
                        if not service.dsh_release.wait(timeout=45):
                            raise AssertionError('Bounded external DSH response was not released.')
                        call = None
                    if query_lock and service.dsh_steps == 3:
                        service.query_ready.set()
                        until = time.monotonic() + 10
                        with open(query_lock, 'rb') as lock:
                            while time.monotonic() < until:
                                try:
                                    fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
                                    fcntl.flock(lock, fcntl.LOCK_UN)
                                except BlockingIOError:
                                    # The public intake already read its work row before scanning native cards.
                                    time.sleep(.04)
                                    break
                                time.sleep(.002)
                calls = [call, *extra] if call else []
                delta = {'tool_calls': [{'index': index, 'id': 'fixture-call-' + str(len(service.requests)) + '-' + str(index), 'type': 'function',
                        'function': {'name': value[0], 'arguments': json.dumps(value[1])}} for index, value in enumerate(calls)]} if call else {'content': 'Original execution is awaiting acceptance.'}
                self.send_response(200)
                self.send_header('Content-Type', 'text/event-stream')
                self.end_headers()
                for part, reason in (({'role': 'assistant'}, None), (delta, None), ({}, 'tool_calls' if call else 'stop')):
                    chunk = {'id': 'fixture-completion', 'object': 'chat.completion.chunk', 'created': 1,
                             'model': payload['model'], 'choices': [{'index': 0, 'delta': part, 'finish_reason': reason}]}
                    self.wfile.write(('data: ' + json.dumps(chunk) + '\n\n').encode())
                self.wfile.write(('data: ' + json.dumps({'id': 'fixture-completion', 'object': 'chat.completion.chunk', 'created': 1,
                    'model': payload['model'], 'choices': [], 'usage': {'prompt_tokens': 12, 'completion_tokens': 8, 'total_tokens': 20}}) + '\n\n').encode())
                self.wfile.write(b'data: [DONE]\n\n')

            def log_message(self, *_):
                pass

        self.server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.base_url = f'http://127.0.0.1:{self.server.server_port}/v1'

    def close(self):
        self.dsh_release.set()
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(2)
        assert not self.thread.is_alive()


def execution_reference(scratch, base_url, sdk):
    """A private synthetic approval fixture; real receipts remain separate."""
    from pathlib import Path
    import hashlib
    node = Path('/opt/homebrew/bin/node').resolve(strict=True)
    root = Path(__file__).resolve().parents[1]
    skills = scratch / 'skills'
    (skills / 'fixture-matt').mkdir(parents=True)
    (skills / 'fixture-matt/SKILL.md').write_text('---\nname: fixture-matt\ndescription: Bounded synthetic workflow.\n---\nImplement the single requested change and report actual tests.\n')
    receipt = scratch / 'tool-receipt.json'
    native_sources = ['@deepseek-ai/dsh/lib/profile-boot.js', '@deepseek-ai/dsh-sandbox-policy/lib/index.js',
        '@deepseek-ai/dsh-tool-bash/lib/index.js', '@deepseek-ai/dsh-tool-jobs/lib/index.js',
        '@deepseek-ai/dsh-jobs-local/lib/index.js', '@deepseek-ai/dsh-agent-loop/lib/index.js']
    receipt.write_text(json.dumps({'status': 'passed', 'foreground_only': True,
        'tool_catalog': ['bash', 'job_kill', 'job_list', 'job_output'],
        'passed_cases': ['allowed_write', 'outside_write_denied', 'symlink_write_denied', 'hardlink_write_denied', 'danger_denied', 'runtime_directory'],
        'composition_sha256': hashlib.sha256((root / 'ghost_hermes_pm/owned_runtime.mjs').read_bytes()).hexdigest(),
        'node_sha256': hashlib.sha256(node.read_bytes()).hexdigest(),
        'sdk_sources': {p: hashlib.sha256((sdk / p).read_bytes()).hexdigest() for p in native_sources}}))
    receipt.chmod(0o600)
    env = scratch / 'model.env'
    env.write_text('DSH_FIXTURE_MODEL_KEY=synthetic-only-key\n')
    env.chmod(0o600)
    reference = scratch / 'execution.json'
    reference.write_text(json.dumps({'schema': 1, 'approved': True, 'runtime_package_root': str(sdk), 'node_bin': str(node),
        'model': {'api': 'openai-completions', 'base_url': base_url, 'model': 'fixture-dsh', 'context_window': 16384,
                  'api_key_env': 'DSH_FIXTURE_MODEL_KEY', 'env_file': str(env)},
        'skill_directories': [str(skills)], 'tool_receipt_ref': str(receipt),
        'budget': {'max_wall_seconds': 20, 'max_model_requests': 5, 'max_reported_tokens': 1000, 'max_output_tokens_per_request': 256}}))
    reference.chmod(0o600)
    return str(reference)
