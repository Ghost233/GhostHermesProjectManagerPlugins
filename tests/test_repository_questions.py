"""Original DSH question lifecycle and registered Hermes/Feishu reply boundaries."""
import json
import os
from pathlib import Path
import tempfile
import shutil
import subprocess
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from sdk_source_integrity import source_snapshot


class QuestionModel:
    def __init__(self, timeout=-1):
        self.requests = []
        owner = self
        class Handler(BaseHTTPRequestHandler):
            def do_POST(self):
                owner.requests.append(json.loads(self.rfile.read(int(self.headers['Content-Length']))))
                first = len(owner.requests) == 1
                delta = {'tool_calls': [{'index': 0, 'id': 'question-fixture', 'type': 'function', 'function': {
                    'name': 'ask_user_question', 'arguments': json.dumps({'timeout': timeout, 'questions': [
                        {'id': 'colour', 'question': 'Which colour?', 'options': [{'label': 'Blue'}, {'label': 'Green'}]},
                        {'id': 'note', 'question': 'Which delivery note?'}]})}}]} if first else {'content': 'Decisions received.'}
                self.send_response(200)
                self.send_header('Content-Type', 'text/event-stream')
                self.end_headers()
                for part, reason in (({'role': 'assistant'}, None), (delta, None), ({}, 'tool_calls' if first else 'stop')):
                    self.wfile.write(('data: ' + json.dumps({'id': 'question-stream', 'object': 'chat.completion.chunk',
                        'created': 1, 'model': 'fixture-model', 'choices': [{'index': 0, 'delta': part, 'finish_reason': reason}]}) + '\n\n').encode())
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


def configuration(root, sdk, model):
    return {'dsh_home': str(root / 'instance/home'), 'workspace': str(root / 'repo'), 'runtime_package_root': str(sdk),
        'instance_id': 'fixture-questions', 'generation': 'fixture-generation', 'session_id': 'session-fixture-questions',
        'runtime_configuration': {'model': {'provider': 'fixture-provider', 'model': 'fixture-model', 'configuration': {
            'api': 'openai-completions', 'baseURL': model.base_url, 'apiKeyEnv': 'DSH_FIXTURE_MODEL_KEY',
            'models': [{'id': 'fixture-model', 'contextWindow': 16384, 'maxTokens': 256, 'input': ['text']}]}}}}


def await_condition(check, seconds=15):
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        value = check()
        if value:
            return value
        time.sleep(.05)
    raise AssertionError('Original question lifecycle did not reach its expected state.')


@pytest.mark.parametrize('timeout', [-1, 1])
def test_original_question_live_and_continued_round(timeout):
    from ghost_hermes_pm.dsh_owned_transport import PersistentOwnedTransport
    from ghost_hermes_pm.repository_supervision import owned_call, original_history
    configured = os.environ.get('DSH_TEST_SDK_ROOT')
    if not configured:
        pytest.skip('Original DSH SDK is required.')
    sdk = Path(configured).resolve(strict=True)
    before = source_snapshot(sdk)
    model = QuestionModel(timeout)
    try:
        with tempfile.TemporaryDirectory(prefix='hpm-question-', dir='/tmp') as temporary:
            root = Path(temporary).resolve()
            (root / 'repo').mkdir()
            settings = configuration(root, sdk, model)
            carrier = PersistentOwnedTransport(instance_dir=str(root / 'instance'), configuration=settings, timeout=15,
                environment={'DSH_FIXTURE_MODEL_KEY': 'synthetic-key', 'BROWSER': '/usr/bin/true'})
            try:
                sid = settings['session_id']
                owned_call(carrier, 'session/create', {'sessionId': sid, 'cwd': settings['workspace'], 'agentPreset': 'hermes-owned'})
                assert owned_call(carrier, 'session/prompt', {'sessionId': sid, 'requestId': 'input-fixture', 'mode': 'queue',
                    'content': [{'type': 'text', 'text': 'Ask the original two questions.'}]})['accepted']
                def question_packet():
                    packet = carrier.event_frames()
                    return packet if any(frame.get('event') == 'user-questions/request' for frame in packet['frames']) else None
                packet = await_condition(question_packet)
                frame = next(f for f in packet['frames'] if f.get('event') == 'user-questions/request')
                assert frame['agentId'] == sid and frame['request']['wait']['callId'] == 'question-fixture'
                answer = {'answers': [{'id': 'colour', 'selected': ['Blue']}, {'id': 'note', 'selected': [], 'custom': 'Delegate this choice.'}]}
                if timeout == -1:
                    owned_call(carrier, '$events/result', {'clientId': packet['client_id'], 'eventId': frame['eventId'],
                        'outcome': {'kind': 'result', 'value': answer}})
                else:
                    await_condition(lambda: next((q for q in original_history(carrier, sid, settings['workspace'])[1]['userQuestions']['active']
                        if q['callId'] == 'question-fixture' and q['state'] == 'continued'), None))
                    assert owned_call(carrier, 'userQuestions/answer', {'agentId': sid, 'callId': 'question-fixture', 'answer': answer}) is True
                settled = await_condition(lambda: original_history(carrier, sid, settings['workspace'])[1]['userQuestions']['settled'])
                assert settled == [{'callId': 'question-fixture', 'answers': answer['answers']}]
                assert carrier.native_identity['generation'] == settings['generation']
            finally:
                carrier.shutdown_owned()
            assert carrier.close_outcome['kind'] == 'original_exit'
    finally:
        model.close()
    assert source_snapshot(sdk) == before


def test_original_headless_runtime_never_invokes_browser_launcher():
    from ghost_hermes_pm.dsh_owned_transport import PersistentOwnedTransport
    from ghost_hermes_pm.repository_supervision import owned_call
    configured = os.environ.get('DSH_TEST_SDK_ROOT')
    if not configured:
        pytest.skip('Original DSH SDK is required.')
    sdk = Path(configured).resolve(strict=True)
    before = source_snapshot(sdk)
    with tempfile.TemporaryDirectory(prefix='hpm-no-opener-', dir='/tmp') as temporary:
        root = Path(temporary).resolve()
        (root / 'repo').mkdir()
        (root / 'bin').mkdir(mode=0o700)
        marker = root / 'browser-launch-attempts'
        opener = root / 'bin/open'
        opener.write_text('#!/bin/sh\nprintf called >> ' + str(marker) + '\n')
        opener.chmod(0o700)
        assert subprocess.run([str(opener)], check=False).returncode == 0
        assert marker.read_text() == 'called'
        marker.unlink()
        settings = {'dsh_home': str(root / 'instance/home'), 'workspace': str(root / 'repo'), 'runtime_package_root': str(sdk),
            'instance_id': 'fixture-headless', 'generation': 'fixture-generation', 'session_id': 'fixture-headless-session'}
        carrier = PersistentOwnedTransport(instance_dir=str(root / 'instance'), configuration=settings, timeout=15,
            environment={'PATH': str(root / 'bin') + os.pathsep + os.environ['PATH']})
        try:
            created = owned_call(carrier, 'session/create', {'sessionId': settings['session_id'],
                'cwd': settings['workspace'], 'agentPreset': 'hermes-owned'})
            assert created['sessionId'] == settings['session_id']
            overlay = json.loads((root / 'instance/home/.hermes-owned-overlay.json').read_text())
            for name in ('web-runtime', 'webserver'):
                assert next(row for row in overlay if row.get('id') == name)['disabled'] is True
            assert not marker.exists(), 'The original supported headless runtime cannot invoke the external OS browser launcher.'
        finally:
            carrier.shutdown_owned()
        assert carrier.close_outcome['kind'] == 'original_exit'
    assert source_snapshot(sdk) == before


@pytest.mark.parametrize('scenario', ['live', 'continued', 'natural', 'multiselect', 'sensitive'])
def test_registered_feishu_replies_to_original_question_round(scenario):
    configured = os.environ.get('HERMES_TEST_SDK_ROOT')
    if not configured:
        pytest.skip('Original Hermes SDK is required.')
    sdk = Path(configured).resolve(strict=True)
    before = source_snapshot(sdk)
    dsh_before = source_snapshot(os.environ['DSH_TEST_SDK_ROOT'])
    root = Path(__file__).resolve().parents[1]
    scratch = Path(tempfile.mkdtemp(prefix='hpm-questions-sdk-', dir='/tmp')).resolve()
    passed = False
    try:
        home = scratch / 'home'
        plugin = home / 'plugins/ghost-hermes-pm'
        plugin.mkdir(parents=True)
        home.chmod(0o700)
        for name in ('__init__.py', 'plugin.yaml', 'ghost_hermes_pm'):
            source = root / name
            if source.is_dir():
                shutil.copytree(source, plugin / name, ignore=shutil.ignore_patterns('__pycache__'))
            else:
                shutil.copy2(source, plugin / name)
        env = {'PATH': os.environ.get('PATH', '/usr/bin:/bin'), 'HERMES_HOME': str(home),
            'HERMES_BUNDLED_PLUGINS': str(home / 'empty-bundled'), 'HERMES_KANBAN_HOME': str(home),
            'PYTHONDONTWRITEBYTECODE': '1', 'PYTHONPATH': str(sdk), 'HERMES_TEST_SDK_ROOT': str(sdk),
            'DSH_TEST_SDK_ROOT': os.environ['DSH_TEST_SDK_ROOT'], 'BROWSER': '/usr/bin/true'}
        result = subprocess.run([sys.executable, str(root / 'tests/repository_questions_smoke_runner.py'), str(scratch), scenario],
            cwd=scratch, env=env, text=True, capture_output=True, timeout=100)
        assert source_snapshot(sdk) == before and source_snapshot(os.environ['DSH_TEST_SDK_ROOT']) == dsh_before
        assert result.returncode == 0, result.stdout + result.stderr + '\nPrivate fixture retained: ' + str(scratch)
        assert (scratch / 'questions-smoke-result').read_text() == 'passed'
        assert not json.loads((scratch / 'cleanup-evidence.json').read_text())['cleanup_errors']
        passed = True
    finally:
        if passed:
            shutil.rmtree(scratch)
