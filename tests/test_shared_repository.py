"""Shared Desktop public HTTP seam; no new runtime or authenticated URL export."""
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
import threading
from pathlib import Path
import subprocess
import sys
import shutil
import asyncio
import hashlib
import time
from copy import deepcopy

from sdk_source_integrity import source_snapshot
from test_repository_github import github_source  # noqa: F401

import pytest

from ghost_hermes_pm.manager import ManagementError


@pytest.mark.parametrize('change', ['noncanonical', 'in_workspace', 'unsafe_permissions', 'not_executable', 'wrong_python', 'wrong_node'])
def test_shared_runtime_path_version_and_permissions_refuse_unsafe_entries(tmp_path, change):
    from ghost_hermes_pm.shared_repository import _trusted_binary
    workspace = tmp_path / 'repository'
    workspace.mkdir()
    binary = tmp_path / 'approved-runtime'
    binary.write_text('#!/bin/sh\nprintf "Python 3.10.0\\n"\n')
    binary.chmod(0o700)
    kind = 'python'
    if change == 'noncanonical':
        alias = tmp_path / 'alias-runtime'
        alias.symlink_to(Path(sys.executable).resolve())
        binary = alias
    elif change == 'in_workspace':
        binary = workspace / 'runtime'
        binary.write_text('#!/bin/sh\nprintf "Python 3.12.0\\n"\n')
        binary.chmod(0o700)
    elif change == 'unsafe_permissions':
        binary.chmod(0o777)
    elif change == 'not_executable':
        binary.chmod(0o600)
    elif change == 'wrong_node':
        kind = 'node'
        binary.write_text('#!/bin/sh\nprintf "v18.0.0\\n"\n')
    with pytest.raises(ManagementError):
        _trusted_binary(str(binary), str(workspace), kind)


def test_shared_runtime_requires_actual_isolated_entry_success(tmp_path):
    from ghost_hermes_pm.shared_repository import _runtime_entries
    workspace = tmp_path / 'repository'
    workspace.mkdir()
    binary = tmp_path / 'approved-runtime'
    binary.write_text('#!/bin/sh\nif [ "$1" = "--version" ]; then printf "Python 3.12.0\\n"; else exit 9; fi\n')
    binary.chmod(0o700)
    root = Path(__file__).resolve().parents[1]
    configuration = {'source_snapshot': {'python': str(binary), 'reader': str(root / 'ghost_hermes_pm/shared_source_reader.py')},
        'verification': {'python': str(Path(sys.executable).resolve()), 'node': str(Path(shutil.which('node')).resolve()),
            'runner': str(root / 'ghost_hermes_pm/repository_test_runner.py')}, 'node_bin': str(Path(shutil.which('node')).resolve())}
    with pytest.raises(ManagementError, match='read-only runtime entry failed'):
        _runtime_entries(configuration, str(workspace))


def test_original_workspace_gateway_reuses_path_and_preserves_existing_session_across_reload(tmp_path):
    sdk = Path(os.environ['DSH_TEST_SDK_ROOT']).resolve(strict=True)
    before = source_snapshot(sdk)
    result = subprocess.run([shutil.which('node'), str(Path(__file__).with_name('shared_workspace_native_runner.mjs')),
        str(tmp_path)], capture_output=True, text=True, timeout=20)
    assert result.returncode == 0, result.stdout + result.stderr
    evidence = json.loads(result.stdout)
    (tmp_path / 'workspace-contract.json').write_text(json.dumps(evidence))
    assert evidence['created']['created'] is True
    assert evidence['repeated']['created'] is evidence['restored']['created'] is False
    assert evidence['restored']['workspace']['sessionIds'] == [evidence['original']['sessionId']]
    assert evidence['cwd_xor_refused'] and evidence['model_requests'] == 0
    assert source_snapshot(sdk) == before


@pytest.mark.parametrize('initial', ['danger-full-access/never', 'workspace-write/ask', 'read-only/ask'])
def test_shared_permission_confirmation_uses_original_command_gateway(tmp_path, initial):
    sdk = Path(os.environ['DSH_TEST_SDK_ROOT']).resolve(strict=True)
    before = source_snapshot(sdk)
    result = subprocess.run([shutil.which('node'), str(Path(__file__).with_name('shared_permission_native_runner.mjs')),
                             str(tmp_path), initial], capture_output=True, text=True, timeout=20)
    assert result.returncode == 0, result.stdout + result.stderr
    evidence = json.loads(result.stdout)
    artifact = tmp_path / 'native-permission-contract.json'
    artifact.write_text(json.dumps(evidence))
    artifact.chmod(0o600)
    from ghost_hermes_pm.shared_repository import confirm_shared_permission
    proof = confirm_shared_permission(evidence['response'], evidence['events'], evidence['projections'])
    assert proof['command_id'] == evidence['response']['commandId']
    assert confirm_shared_permission(None, evidence['events'], evidence['projections'], reconcile=True) == proof
    for mutation in ('wrong_command', 'missing_done', 'changed_policy', 'unexpected_work', 'foreign_notice', 'queued_input', 'late_cursor'):
        candidate = deepcopy(evidence)
        kwargs = {}
        if mutation == 'wrong_command':
            candidate['response']['commandId'] = 'fixture-foreign-command'
        elif mutation == 'missing_done':
            candidate['events'] = [event for event in candidate['events'] if event['type'] != 'command/done']
        elif mutation == 'changed_policy':
            candidate['events'].append({'type': 'approval/policy', 'seq': len(candidate['events']), 'data': {'policy': 'never'}})
        elif mutation == 'unexpected_work':
            candidate['events'].append({'type': 'turn/start', 'seq': len(candidate['events']), 'data': {'turn': 1}})
        elif mutation == 'foreign_notice':
            notices = [event for event in candidate['events'] if event['type'] == 'agent/inbox/spliced']
            if not notices:
                continue
            notices[0]['data']['inserted'][0]['source'] = {'kind': 'user'}
        elif mutation == 'queued_input':
            candidate['projections']['inbox']['next-turn'].append({'role': 'user', 'content': []})
        else:
            kwargs['before_cursor'] = proof['run_seq']
        with pytest.raises(ManagementError):
            confirm_shared_permission(candidate['response'], candidate['events'], candidate['projections'], **kwargs)
    assert source_snapshot(sdk) == before


def test_permission_initializer_exec_change_does_not_end_the_old_process():
    from types import SimpleNamespace
    from ghost_hermes_pm.trusted_controller import controller_identity
    from ghost_hermes_pm.shared_repository import require_permission_initializer_end
    identity = controller_identity()
    original = {**identity, 'executable': '/synthetic/previous-executable'}
    run = SimpleNamespace(id=1, task_id='fixture-card', status='blocked', ended_at=time.time(), worker_pid=identity['pid'])
    with pytest.raises(ManagementError):
        require_permission_initializer_end({'run_id': 1, 'identity': original}, run, 'fixture-card')


@pytest.mark.parametrize('change', ['unfinished', 'unknown', 'changed_birth', 'gone'])
def test_permission_initializer_requires_original_terminal_birth(change, monkeypatch):
    import psutil
    from types import SimpleNamespace
    from ghost_hermes_pm.trusted_controller import controller_identity
    from ghost_hermes_pm.shared_repository import require_permission_initializer_end
    identity = controller_identity()
    if change == 'gone':
        child = subprocess.Popen([sys.executable, '-c', 'import sys; sys.stdin.read()'], stdin=subprocess.PIPE)
        identity = controller_identity(child.pid)
        child.communicate(b'')
        assert child.returncode == 0
    run = SimpleNamespace(id=1, task_id='fixture-card', status='blocked', ended_at=time.time(), worker_pid=identity['pid'])
    if change == 'unfinished':
        run.ended_at = None
    elif change == 'unknown':
        def unavailable(pid):
            raise psutil.AccessDenied(pid)
        monkeypatch.setattr(psutil, 'Process', unavailable)
    elif change == 'changed_birth':
        identity = {**identity, 'start_sec': str(int(identity['start_sec']) - 1)}
    previous = {'run_id': 1, 'identity': identity}
    if change in {'unfinished', 'unknown'}:
        with pytest.raises(ManagementError):
            require_permission_initializer_end(previous, run, 'fixture-card')
    else:
        require_permission_initializer_end(previous, run, 'fixture-card')


@pytest.mark.parametrize('job_status', ['completed', 'killed', 'failed'])
def test_source_chain_attributes_terminal_promoted_job_write(job_status):
    from ghost_hermes_pm.shared_repository import _verified_source_chain
    execution = {'generation': 'fixture-generation', 'session_id': 'fixture-session'}
    baseline = {'head': None, 'workspace_status': '', 'source_digest': 'fixture-baseline'}
    promoted_after = {'head': None, 'workspace_status': '?? fixture-output/', 'source_digest': 'fixture-promoted'}
    current = {'head': None, 'workspace_status': '?? fixture-output/\n?? fixture-result', 'source_digest': 'fixture-current'}
    promoted = {**execution, 'name': 'bash', 'kind': 'promoted', 'execution_admission': 'admitted',
        'is_error': False, 'exit_code': None, 'timed_out': None, 'aborted': None,
        'job_id': 'fixture-promoted-job', 'native_session_seq': 3,
        'source_before': baseline, 'source_after': promoted_after}
    foreground = {**execution, 'name': 'bash', 'kind': 'foreground', 'execution_admission': 'admitted',
        'is_error': False, 'exit_code': 0, 'timed_out': False, 'aborted': False, 'native_session_seq': 7,
        'source_before': promoted_after, 'source_after': current}
    owned_jobs = [{'id': 'fixture-promoted-job', 'owner': 'fixture-session', 'status': job_status}]
    assert _verified_source_chain(execution, baseline, [promoted, foreground], current, owned_jobs)


@pytest.mark.parametrize('change', [
    'running', 'stopping', 'unknown', 'missing', 'duplicate', 'wrong_owner', 'missing_owner',
    'missing_job_id', 'wrong_job_id', 'exit_code', 'timed_out', 'aborted',
    'missing_exit_code', 'missing_timed_out', 'missing_aborted', 'generation', 'session_id',
    'admission', 'is_error', 'name', 'kind', 'sequence', 'before', 'after', 'current',
])
def test_source_chain_rejects_unverified_promoted_job_write(change):
    from ghost_hermes_pm.shared_repository import _verified_source_chain
    execution = {'generation': 'fixture-generation', 'session_id': 'fixture-session'}
    baseline = {'head': None, 'workspace_status': '', 'source_digest': 'fixture-baseline'}
    current = {'head': None, 'workspace_status': '?? fixture-output', 'source_digest': 'fixture-current'}
    receipt = {**execution, 'name': 'bash', 'kind': 'promoted', 'execution_admission': 'admitted',
        'is_error': False, 'exit_code': None, 'timed_out': None, 'aborted': None,
        'job_id': 'fixture-promoted-job', 'native_session_seq': 3,
        'source_before': baseline, 'source_after': current}
    job = {'id': 'fixture-promoted-job', 'owner': 'fixture-session', 'status': 'completed'}
    jobs = [job]
    if change in {'running', 'stopping', 'unknown'}:
        job['status'] = change
    elif change == 'missing':
        jobs = []
    elif change == 'duplicate':
        jobs.append(dict(job))
    elif change == 'wrong_owner':
        job['owner'] = 'fixture-foreign-session'
    elif change == 'missing_owner':
        job.pop('owner')
    elif change.startswith('missing_'):
        receipt.pop(change.removeprefix('missing_'))
    elif change == 'wrong_job_id':
        receipt['job_id'] = 'fixture-other-job'
    elif change == 'exit_code':
        receipt['exit_code'] = 0
    elif change in {'timed_out', 'aborted'}:
        receipt[change] = False
    elif change in {'generation', 'session_id', 'name', 'kind'}:
        receipt[change] = 'fixture-other'
    elif change == 'admission':
        receipt['execution_admission'] = 'rejected'
    elif change == 'is_error':
        receipt['is_error'] = True
    elif change == 'sequence':
        receipt['native_session_seq'] = 0
    elif change == 'before':
        receipt['source_before'] = {**baseline, 'source_digest': 'fixture-other-source'}
    elif change == 'after':
        receipt['source_after'] = None
    elif change == 'current':
        current = {**current, 'source_digest': 'fixture-other-current'}
    assert not _verified_source_chain(execution, baseline, [receipt], current, jobs)


@pytest.mark.parametrize('exit_code', [0, 1])
def test_source_chain_foreground_attribution_keeps_existing_exit_semantics(exit_code):
    from ghost_hermes_pm.shared_repository import _verified_source_chain
    execution = {'generation': 'fixture-generation', 'session_id': 'fixture-session'}
    baseline = {'head': None, 'workspace_status': '', 'source_digest': 'fixture-baseline'}
    current = {'head': None, 'workspace_status': '?? fixture-output', 'source_digest': 'fixture-current'}
    receipt = {**execution, 'name': 'bash', 'kind': 'foreground', 'execution_admission': 'admitted',
        'is_error': False, 'exit_code': exit_code, 'timed_out': False, 'aborted': False,
        'native_session_seq': 3, 'source_before': baseline, 'source_after': current}
    assert _verified_source_chain(execution, baseline, [receipt], current)


@pytest.mark.parametrize('job', [
    {'id': 'fixture-unrelated-job', 'owner': 'fixture-session', 'status': 'running'},
    {'id': 'fixture-unrelated-job', 'owner': 'fixture-session', 'status': 'stopping'},
    {'id': 'fixture-unrelated-job', 'owner': 'fixture-session', 'status': 'unknown'},
    {'id': 'fixture-unrelated-job', 'owner': 'fixture-session'},
    None,
], ids=['running', 'stopping', 'unknown', 'missing_status', 'malformed'])
def test_supervision_takeover_read_boundary_rejects_unrelated_nonterminal_job(job, monkeypatch, tmp_path):
    """Adverse read-fact replay; public_entry[supervise_takeover] is its canonical companion."""
    import psutil
    from types import SimpleNamespace
    monkeypatch.syspath_prepend(os.environ['HERMES_TEST_SDK_ROOT'])
    from hermes_cli import kanban_db as kb, kanban_db_connect
    from ghost_hermes_pm import shared_repository, repository_acceptance, trusted_controller
    child = subprocess.Popen([sys.executable, '-c', 'import sys; sys.stdin.read()'], stdin=subprocess.PIPE)
    birth = psutil.Process(child.pid).create_time()
    previous = {'run_id': 17, 'identity': {'pid': child.pid, 'start_sec': str(int(birth)),
        'start_usec': str(round((birth - int(birth)) * 1000000)),
        'executable': str(Path(sys.executable).resolve())}, 'exit_log_offset': 0}
    child.communicate(b'')
    assert child.returncode == 0 and not psutil.pid_exists(child.pid)
    source = {'head': None, 'workspace_status': '', 'source_digest': 'fixture-source'}
    execution = {'state': 'running', 'first_input': 'accepted', 'session_id': 'fixture-session',
        'generation': 'fixture-generation', 'baseline': source, 'supervisor_claim': previous}
    record = {'card_id': 'fixture-card', 'target': {'repo_path': 'fixture-repository'}, 'dsh_execution': execution}
    old = SimpleNamespace(id=17, task_id='fixture-card', status='gave_up', outcome='gave_up',
        ended_at=100, worker_pid=child.pid,
        error='Iteration budget exhausted (8/8) — task could not complete within the allowed iterations')
    log = tmp_path / 'fixture-worker.log'
    log.write_text('[kanban-worker-exit] rc=0\n')
    connection = object()
    reads, saves, carrier_calls = [], [], []

    @contextmanager
    def connect_closing(*, board):
        assert board == 'fixture-board'
        yield connection

    def get_run(actual_connection, run_id):
        assert actual_connection is connection and run_id == 17
        reads.append(run_id)
        return old

    class Carrier:
        def owned_jobs(self):
            carrier_calls.append('owned_jobs')
            return [job]

        def __getattr__(self, name):
            pytest.fail('Takeover rejection must not prompt or use another carrier operation: ' + name)

    monkeypatch.setattr(kanban_db_connect, 'connect_closing', connect_closing)
    monkeypatch.setattr(kb, 'get_run', get_run)
    monkeypatch.setattr(kb, 'worker_log_path', lambda *args, **kwargs: log)
    monkeypatch.setattr(shared_repository, 'read_ledger', lambda *args: {})
    monkeypatch.setattr(repository_acceptance, 'repository_source_state', lambda *args: source)
    monkeypatch.setattr(shared_repository, 'shared_receipts', lambda *args: [])
    monkeypatch.setattr(trusted_controller, 'controller_identity', lambda: {'pid': os.getpid()})
    intake = SimpleNamespace(board='fixture-board', _save_execution=lambda *args, **kwargs: saves.append((args, kwargs)))
    before = json.dumps(execution, sort_keys=True, separators=(',', ':')).encode()
    with pytest.raises(ManagementError) as caught:
        shared_repository._take_over_gave_up_claim(intake, record, 18, {}, Carrier())
    assert caught.value.code == 'outcome_unknown'
    assert str(caught.value) == 'Owned shared jobs must be terminal before supervision takeover.'
    assert reads == [17] and saves == [] and carrier_calls == ['owned_jobs']
    assert json.dumps(execution, sort_keys=True, separators=(',', ':')).encode() == before


@pytest.mark.parametrize('error', [
    'Iteration budget exhausted (7/8) — task could not complete within the allowed iterations',
    'Iteration budget exhausted (8/8) — task could not complete within the allowed iterations; extra suffix',
], ids=['unequal_budget', 'suffix'])
def test_supervision_takeover_read_boundary_rejects_noncanonical_budget_error(error, monkeypatch):
    """Guard read-boundary replay, not SDK-generated noncanonical gave_up.

    The public_entry[supervise_takeover] scenario supplies the real canonical
    native lifecycle companion; this isolates its adverse Kanban read facts.
    """
    from types import SimpleNamespace
    monkeypatch.syspath_prepend(os.environ['HERMES_TEST_SDK_ROOT'])
    from hermes_cli import kanban_db as kb, kanban_db_connect
    from ghost_hermes_pm.shared_repository import _take_over_gave_up_claim
    previous = {'run_id': 17, 'identity': {'pid': os.getpid(), 'start_sec': '100',
        'start_usec': '0', 'executable': str(Path(sys.executable).resolve())}, 'exit_log_offset': 0}
    execution = {'state': 'running', 'first_input': 'accepted',
        'continuation_control': {'status': 'accepted'}, 'supervisor_claim': previous}
    record = {'card_id': 'fixture-card', 'dsh_execution': execution}
    old = SimpleNamespace(id=17, task_id='fixture-card', status='gave_up', outcome='gave_up',
        ended_at=100, worker_pid=previous['identity']['pid'], error=error)
    connection = object()
    reads, saves = [], []

    @contextmanager
    def connect_closing(*, board):
        assert board == 'fixture-board'
        yield connection

    def get_run(actual_connection, run_id):
        assert actual_connection is connection and run_id == 17
        reads.append(run_id)
        return old

    class UnusedCarrier:
        def __getattr__(self, name):
            pytest.fail('Read-boundary rejection must not use the carrier or send a prompt: ' + name)

    monkeypatch.setattr(kanban_db_connect, 'connect_closing', connect_closing)
    monkeypatch.setattr(kb, 'get_run', get_run)
    intake = SimpleNamespace(board='fixture-board',
        _save_execution=lambda *args, **kwargs: saves.append((args, kwargs)))
    before = json.dumps(previous, sort_keys=True, separators=(',', ':')).encode()
    with pytest.raises(ManagementError) as caught:
        _take_over_gave_up_claim(intake, record, 18, {}, UnusedCarrier())
    assert caught.value.code == 'outcome_unknown'
    assert str(caught.value) == 'The original iteration-budget worker cannot be safely supervised.'
    assert reads == [17] and saves == []
    assert json.dumps(execution['supervisor_claim'], sort_keys=True, separators=(',', ':')).encode() == before


@contextmanager
def desktop_service(tmp_path):
    calls = []

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def do_GET(self):
            calls.append(('GET', self.path))
            assert self.path == '/?token=synthetic-only'
            self.send_response(303)
            self.send_header('Set-Cookie', 'fixture-session=value; HttpOnly; Path=/')
            self.send_header('Location', './')
            self.end_headers()

        def do_POST(self):
            assert self.headers['Cookie'] == 'fixture-session=value'
            packet = json.loads(self.rfile.read(int(self.headers['Content-Length'])))
            calls.append(packet)
            value = {'items': []} if packet['method'] == 'session/list' else {'accepted': True}
            body = json.dumps({'type': 'server-response', 'rpcId': packet['rpcId'],
                               'result': {'ok': True, 'value': value}}).encode()
            self.send_response(200)
            self.send_header('Content-Length', str(len(body)))
            self.end_headers()
            self.wfile.write(body)

    server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    path = tmp_path / 'desktop-host.json'
    path.write_text(json.dumps({'schema': 1, 'lease': 'synthetic-lease', 'pid': os.getpid(),
        'url': 'http://127.0.0.1:' + str(server.server_port) + '/?token=synthetic-only',
        'runtimeVersion': 'fixture', 'permissionMode': 'workspace-write'}))
    path.chmod(0o600)
    try:
        yield path, calls
    finally:
        server.shutdown()
        server.server_close()
        thread.join()


def test_shared_desktop_consumes_native_receipt_and_never_exports_auth(tmp_path):
    from ghost_hermes_pm.shared_desktop import SharedDesktopTransport
    with desktop_service(tmp_path) as (path, calls):
        carrier = SharedDesktopTransport({'desktop_receipt': str(path), 'host_lease': 'synthetic-lease',
            'service_ref': 'fixture-service', 'session_id': 'fixture-owned', 'generation': 'fixture-generation',
            'workspace': str(tmp_path)}, timeout=1)
        try:
            result = carrier.request('session/list', {'_request': {}}, 'fixture-list')
            assert result['result']['value'] == {'items': []}
            assert carrier.native_identity['kind'] == 'shared_native'
            assert carrier.native_identity['pid'] == os.getpid()
            assert set(tmp_path.iterdir()) == {path}
            with pytest.raises(ManagementError, match='owned'):
                carrier.request('session/cancel', {'request': {'sessionId': 'manual-foreign'}}, 'foreign')
            assert len(calls) == 2
        finally:
            carrier.close()


@pytest.mark.parametrize('change', ['lease', 'mode', 'hardlink'])
def test_shared_desktop_revalidates_receipt_before_mutation(tmp_path, change):
    from ghost_hermes_pm.shared_desktop import SharedDesktopTransport
    with desktop_service(tmp_path) as (path, calls):
        carrier = SharedDesktopTransport({'desktop_receipt': str(path), 'host_lease': 'synthetic-lease',
            'service_ref': 'fixture-service', 'session_id': 'fixture-owned', 'generation': 'fixture-generation',
            'workspace': str(tmp_path)}, timeout=1)
        try:
            carrier.request('session/list', {'_request': {}}, 'first')
            if change == 'lease':
                row = json.loads(path.read_text())
                row['lease'] = 'new-lease'
                path.write_text(json.dumps(row))
            elif change == 'mode':
                path.chmod(0o644)
            else:
                os.link(path, tmp_path / 'receipt-alias')
            with pytest.raises(ManagementError):
                carrier.request('session/cancel', {'request': {'sessionId': 'fixture-owned'}}, 'stop')
            assert len(calls) == 2
        finally:
            carrier.close()


@contextmanager
def shared_remote_peer(tmp_path, behavior):
    # This is only the external protocol boundary fixture, never an SDK patch.
    path = Path(__file__).with_name('dsh_fixture_server.py')
    source = path.read_text().replace("assert self.path == '/api/remote.mux'", """if self.path == '/?token=fixture-launch':
                self.send_response(303)
                self.send_header('Set-Cookie', 'synthetic-auth=value; HttpOnly; Path=/')
                self.send_header('Location', './')
                self.send_header('Content-Length', '0')
                self.end_headers()
                return
            assert self.path == '/api/remote.mux'""")
    source = source.replace("elif method == 'session/create':", """elif method == 'workspace/create':
                assert set(args['request']) == {'path'}
                created = 'workspace' not in state
                if created:
                    state['workspace'] = {'workspaceId': 'fixture-workspace', 'path': args['request']['path'],
                        'title': 'Fixture', 'sessionIds': [], 'createdAt': 'fixture-created', 'updatedAt': 'fixture-created'}
                value = {'workspace': dict(state['workspace']), 'created': created}
            elif method == 'session/create':""")
    begin = source.index("            elif method == 'session/create':")
    end = source.index("            elif method == 'session/prompt':", begin)
    source = source[:begin] + """            elif method == 'session/create':
                creation = args['request']
                assert ('workspaceId' in creation) != ('cwd' in creation)
                cwd = state['workspace']['path'] if 'workspaceId' in creation else creation['cwd']
                value = {'sessionId': creation['sessionId'], 'agentPreset': creation['agentPreset']}
                rows = [row for row in state['sessions'] if row['sessionId'] == creation['sessionId']]
                if not rows:
                    state['sessions'].append({'sessionId': creation['sessionId'], 'cwd': cwd,
                        'running': False, 'agentAvailable': True, 'blank': True, 'updatedAt': 3})
                    state['journals'][creation['sessionId']] = []
                else:
                    assert len(rows) == 1 and rows[0]['cwd'] == cwd
                if 'workspaceId' in creation and creation['sessionId'] not in state['workspace']['sessionIds']:
                    assert creation['workspaceId'] == state['workspace']['workspaceId']
                    state['workspace']['sessionIds'].append(creation['sessionId'])
""" + source[end:]
    source = source.replace("'values': {'inbox': behavior.get('inbox', {'next-turn': [], 'next-step': []})}",
        "'values': {'agentPreset': 'fixture-shared', 'inbox': behavior.get('inbox', {'next-turn': [], 'next-step': []})}")
    namespace = {}
    exec(compile(source, str(path), 'exec'), namespace)
    with namespace['remote_peer'](behavior=behavior) as (url, peer):
        receipt = tmp_path / 'desktop-host.json'
        receipt.write_text(json.dumps({'schema': 1, 'lease': 'fixture-lease', 'pid': os.getpid(),
            'url': url + '/?token=fixture-launch', 'runtimeVersion': 'fixture', 'permissionMode': 'workspace-write'}))
        receipt.chmod(0o600)
        from ghost_hermes_pm.shared_desktop import SharedDesktopTransport
        carrier = SharedDesktopTransport({'desktop_receipt': str(receipt), 'host_lease': 'fixture-lease',
            'service_ref': 'fixture-service', 'session_id': 'session-fixture', 'generation': 'fixture-generation',
            'workspace': str(tmp_path), 'agent_preset': 'fixture-shared', 'ledger_dir': str(tmp_path)}, timeout=1)
        try:
            yield carrier, peer
        finally:
            carrier.close()


def test_shared_workspace_registration_fences_path_and_adopts_only_original_session(tmp_path):
    from ghost_hermes_pm.repository_supervision import owned_call
    with shared_remote_peer(tmp_path, {}) as (carrier, peer):
        peer['sessions'][0]['cwd'] = str(tmp_path)
        original = deepcopy(peer['journals']['session-fixture'])
        workspace = owned_call(carrier, 'workspace/create', {'path': str(tmp_path)})
        assert workspace['created'] is True and workspace['workspace']['sessionIds'] == []
        request = {'sessionId': 'session-fixture', 'workspaceId': workspace['workspace']['workspaceId'],
                   'agentPreset': 'fixture-shared'}
        assert owned_call(carrier, 'session/create', request) == {'sessionId': 'session-fixture', 'agentPreset': 'fixture-shared'}
        repeated = owned_call(carrier, 'workspace/create', {'path': str(tmp_path)})
        assert repeated['created'] is False and repeated['workspace']['sessionIds'] == ['session-fixture']
        assert peer['journals']['session-fixture'] == original and peer['prompts'] == []
        for method, arguments in [
            ('workspace/create', {'path': str(tmp_path.parent)}),
            ('workspace/delete', {'workspaceId': 'fixture-workspace'}),
            ('session/create', {**request, 'cwd': str(tmp_path)}),
            ('session/create', {**request, 'sessionId': 'foreign-session'}),
            ('session/create', {**request, 'workspaceId': 'foreign-workspace'}),
        ]:
            count = len(peer['calls'])
            with pytest.raises(ManagementError):
                owned_call(carrier, method, arguments)
            assert len(peer['calls']) == count


@pytest.mark.parametrize('mismatch', [None, 'cwd', 'sid', 'created_at', 'workspace_id'])
def test_shared_workspace_backfill_preserves_accepted_input_and_budget(tmp_path, mismatch):
    from types import SimpleNamespace
    from ghost_hermes_pm.shared_repository import bind_shared_workspace, ledger_path
    from ghost_hermes_pm.shared_desktop import SharedDesktopTransport
    with shared_remote_peer(tmp_path, {}) as (carrier, peer):
        peer['sessions'][0]['cwd'] = str(tmp_path)
        record = {'id': 'fixture-work', 'target': {'repo_path': str(tmp_path)}, 'dsh_execution': {
            'mode': 'shared_native', 'state': 'execution_failed', 'session_id': 'session-fixture',
            'session_created_at': 0, 'generation': 'fixture-generation', 'first_input': 'accepted',
            'request_id': 'old-input', 'native_identity': carrier.native_identity}}
        ledger = ledger_path(carrier.config, 'session-fixture')
        budget = json.dumps({'state': {'started_at_ms': None, 'requests': 0, 'reported_tokens': 0}}).encode()
        ledger.write_bytes(budget)
        ledger.chmod(0o600)
        saved = []
        intake = SimpleNamespace(_save_execution=lambda value, fields=None: saved.append(json.loads(json.dumps(value))))
        if mismatch == 'cwd':
            peer['sessions'][0]['cwd'] = str(tmp_path.parent)
        elif mismatch == 'sid':
            record['dsh_execution']['session_id'] = 'foreign-session'
        elif mismatch == 'created_at':
            record['dsh_execution']['session_created_at'] = 1
        elif mismatch == 'workspace_id':
            carrier.config['workspace_id'] = record['dsh_execution']['workspace_id'] = 'original-workspace'
            peer['workspace'] = {'workspaceId': 'changed-workspace', 'path': str(tmp_path), 'sessionIds': [],
                'title': 'Fixture', 'createdAt': 'fixture-created', 'updatedAt': 'fixture-created'}
        original = deepcopy(record)
        journal = deepcopy(peer['journals']['session-fixture'])
        if mismatch:
            with pytest.raises(ManagementError):
                bind_shared_workspace(intake, record, carrier)
            assert not saved and record == original
            assert not any(call['method'] == 'session/create' for call in peer['calls'])
            if mismatch != 'workspace_id':
                assert not any(call['method'] == 'workspace/create' for call in peer['calls'])
        else:
            workspace = bind_shared_workspace(intake, record, carrier)
            assert workspace['sessionIds'] == ['session-fixture']
            configuration = dict(carrier.config)
            carrier.close()
            reconnected = SharedDesktopTransport(configuration, timeout=1)
            try:
                assert bind_shared_workspace(intake, record, reconnected)['workspaceId'] == workspace['workspaceId']
            finally:
                reconnected.close()
            assert sum(call['method'] == 'session/create' for call in peer['calls']) == 1
            assert {key: value for key, value in record['dsh_execution'].items() if key != 'workspace_id'} == original['dsh_execution']
        assert peer['journals']['session-fixture'] == journal and ledger.read_bytes() == budget
        assert peer['prompts'] == []


def test_shared_original_waterfalls_ignore_manual_tasks_and_accept_only_one_owned_choice(tmp_path):
    frames = [{'type': 'waterfall', 'event': 'approval/request', 'eventId': 'manual-request',
        'agentId': 'manual-session', 'request': {'toolName': 'bash', 'callId': 'manual-call'}},
        {'type': 'waterfall', 'event': 'approval/request', 'eventId': 'owned-request',
        'agentId': 'session-fixture', 'request': {'toolName': 'bash', 'callId': 'owned-call'}}]
    with shared_remote_peer(tmp_path, {'remote_events': frames}) as (carrier, peer):
        carrier.start_events()
        deadline = time.monotonic() + 2
        while time.monotonic() < deadline:
            packet = carrier.event_frames()
            if packet['frames']:
                break
            time.sleep(.01)
        assert [frame['eventId'] for frame in packet['frames']] == ['owned-request']
        rejected = {'clientId': packet['client_id'], 'eventId': 'owned-request',
            'outcome': {'kind': 'result', 'value': 'allowed-always'}}
        with pytest.raises(ManagementError):
            carrier.request('$events/result', rejected, 'unapproved-persistence')
        approved = dict(rejected, outcome={'kind': 'result', 'value': 'allowed-once'})
        assert carrier.request('$events/result', approved, 'single-choice')['result']['ok'] is True
        with pytest.raises(ManagementError):
            carrier.request('$events/result', approved, 'repeat')
        results = [call['payload']['args'] for call in peer['calls'] if call['method'] == '$events/result']
        assert results == [{'clientId': packet['client_id'], 'eventId': 'manual-request', 'outcome': {'kind': 'next'}}, approved]


def test_shared_stop_never_adopts_visible_unowned_jobs(tmp_path):
    jobs = [{'id': 'unowned', 'owner': None, 'status': 'running'},
            {'id': 'owned', 'owner': 'session-fixture', 'status': 'running'}]
    with shared_remote_peer(tmp_path, {'jobs': jobs}) as (carrier, peer):
        assert carrier.owned_jobs() == [jobs[1]]
        with pytest.raises(ManagementError):
            carrier.request('job/kill', {'request': {'sessionId': 'session-fixture', 'jobId': 'unowned'}}, 'unsafe')
        assert peer['calls'] == []


@pytest.mark.parametrize('entry', ['reader', 'runner'])
def test_privileged_source_reads_do_not_execute_repository_fsmonitor(tmp_path, entry):
    root = Path(__file__).resolve().parents[1]
    repository = tmp_path / 'repository'
    repository.mkdir()
    subprocess.run(['git', 'init', '-q', str(repository)], check=True)
    marker = tmp_path / 'outside-marker'
    extension = tmp_path / 'fsmonitor'
    extension.write_text('#!/bin/sh\nprintf touched > ' + str(marker) + '\nprintf "fixture-token\\0"\n')
    extension.chmod(0o700)
    subprocess.run(['git', '-C', str(repository), 'config', 'core.fsmonitor', str(extension)], check=True)
    arguments = ([str(root / 'ghost_hermes_pm/shared_source_reader.py'), str(repository)] if entry == 'reader'
        else [str(root / 'ghost_hermes_pm/repository_test_runner.py'), str(repository), '--state'])
    result = subprocess.run([sys.executable, '-I', *arguments], capture_output=True, text=True)
    assert result.returncode == 0, result.stderr
    assert not marker.exists(), 'A privileged fingerprint must not execute a repository Git extension.'


def test_privileged_source_reads_disable_clean_filter_without_changing_user_git(tmp_path):
    repository = tmp_path / 'repository'
    repository.mkdir()
    subprocess.run(['git', 'init', '-q', str(repository)], check=True)
    marker = tmp_path / 'outside-filter-marker'
    extension = tmp_path / 'clean-filter'
    extension.write_text('#!/bin/sh\nprintf touched > ' + str(marker) + '\ncat\n')
    extension.chmod(0o700)
    (repository / '.gitattributes').write_text('*.txt filter=fixture\n')
    (repository / 'feature.txt').write_text('original\n')
    subprocess.run(['git', '-C', str(repository), 'config', 'filter.fixture.clean', str(extension)], check=True)
    subprocess.run(['git', '-C', str(repository), 'add', '.'], check=True)
    (repository / 'feature.txt').write_text('changed\n')
    subprocess.run(['git', '-C', str(repository), 'status', '--porcelain=v1'], capture_output=True, check=True)
    assert marker.exists(), 'The external-marker control must demonstrate the executable extension.'
    marker.unlink()
    config_before = (repository / '.git/config').read_bytes()
    reader = Path(__file__).resolve().parents[1] / 'ghost_hermes_pm/shared_source_reader.py'
    result = subprocess.run([sys.executable, '-I', str(reader), str(repository)], capture_output=True, text=True)
    assert result.returncode == 0, result.stderr
    assert not marker.exists() and (repository / '.git/config').read_bytes() == config_before


@pytest.mark.parametrize('unknown_file', [False, True])
def test_shared_github_reference_is_private_native_tmp_and_keeps_host_environment(github_source, unknown_file):
    from types import SimpleNamespace
    from ghost_hermes_pm.shared_repository import prepare_shared_github, cleanup_shared_github, _prompt
    source, original = github_source
    before, environment = original.read_bytes(), dict(os.environ)
    saved = []
    intake = SimpleNamespace(github=source, _save_execution=lambda record, fields=None: saved.append(fields))
    record = {'id': 'fixture-work', 'target': {'repository': 'fixture-user/fixture'},
        'issue': {'url': 'https://github.com/fixture-user/fixture/issues/7', 'title': 'Fixture', 'body': 'Fixture scope'},
        'dsh_execution': {'generation': 'fixture-generation'}}
    reference = prepare_shared_github(intake, record)
    directory = Path(reference['config_dir'])
    try:
        assert directory.resolve().is_relative_to(Path('/tmp').resolve())
        assert directory.stat().st_mode & 0o077 == 0 and (directory / 'hosts.yml').stat().st_mode & 0o077 == 0
        prompt = _prompt(intake, record)
        assert str(directory) in prompt and str(original.parent) not in prompt
        assert 'synthetic-configured-account-token' not in prompt
        assert original.read_bytes() == before and dict(os.environ) == environment
        assert 'DSH_HOME' not in prompt
        record['dsh_execution']['generation'] = 'different-generation'
        with pytest.raises(ManagementError):
            cleanup_shared_github(intake, record)
        assert directory.exists() and original.read_bytes() == before
        record['dsh_execution']['generation'] = 'fixture-generation'
        if unknown_file:
            note = directory / 'owner-note.txt'
            note.write_text('Synthetic owner content must remain.\n')
            note.chmod(0o600)
            with pytest.raises(ManagementError):
                cleanup_shared_github(intake, record)
            assert note.read_text() == 'Synthetic owner content must remain.\n'
            assert record['dsh_execution']['github_cleanup']['status'] == 'unconfirmed'
            assert directory.exists() and (directory / 'hosts.yml').exists()
            note.unlink()  # Remove only this test's own deliberately injected fixture.
        assert cleanup_shared_github(intake, record) == {'status': 'removed'}
        assert not directory.exists() and original.read_bytes() == before
        assert dict(os.environ) == environment
    finally:
        from ghost_hermes_pm.repository_github import cleanup_github_execution
        if directory.exists():
            cleanup_github_execution(reference, 'fixture-generation')


@pytest.mark.parametrize('scenario', ['terminal', 'stop', 'reconcile_intent', 'prepare_continue', 'continue_restart', 'continue_unknown', 'continue_triage', 'continue_claim_race', 'continue_native_restore', 'question_material', 'continue_tokens_uncapped', 'continue_max_tokens', 'continue_max_tokens_unknown', 'continue_max_tokens_last_slot', 'renew_window', 'renew_window_unknown', 'renew_window_continue', 'resume_completed_window', 'renew_window_again', 'supervise_slice', 'supervise_takeover', 'supervise_takeover_nonzero', 'supervise_takeover_reclaimed', 'supervise_takeover_source', 'supervise_takeover_sid', 'supervise_takeover_native', 'supervise_takeover_ledger', 'supervise_takeover_card', 'supervise_expired_window', 'runtime_metadata'])
def test_original_kanban_worker_attaches_shared_desktop_through_public_entry(tmp_path, scenario):
    root = Path(__file__).resolve().parents[1]
    sdk = Path(os.environ['HERMES_TEST_SDK_ROOT'])
    sdk_before = source_snapshot(sdk)
    home = tmp_path / 'home'
    plugin = home / 'plugins/ghost-hermes-pm'
    plugin.mkdir(parents=True)
    home.chmod(0o700)
    for name in ('__init__.py', 'plugin.yaml', 'ghost_hermes_pm'):
        if (root / name).is_dir():
            shutil.copytree(root / name, plugin / name, ignore=shutil.ignore_patterns('__pycache__'))
        else:
            shutil.copy2(root / name, plugin / name)
    if scenario == 'supervise_slice' or scenario.startswith('supervise_takeover'):
        production = root / 'ghost_hermes_pm/shared_repository.py'
        copied = plugin / 'ghost_hermes_pm/shared_repository.py'
        declaration = 'SUPERVISE_OBSERVATION_SLICE_SECONDS = 360'
        original = copied.read_text()
        assert original.count(declaration) == 1 and production.read_text().count(declaration) == 1
        copied.write_text(original.replace(declaration, 'SUPERVISE_OBSERVATION_SLICE_SECONDS = 0.5', 1))
        (tmp_path / 'slice-clock-transform.json').write_text(json.dumps({
            'production_sha256': hashlib.sha256(production.read_bytes()).hexdigest(),
            'original_sha256': hashlib.sha256(original.encode()).hexdigest(),
            'fixture_sha256': hashlib.sha256(copied.read_bytes()).hexdigest(),
            'path': str(copied), 'delta': [declaration, 'SUPERVISE_OBSERVATION_SLICE_SECONDS = 0.5']}))
    env = {**os.environ, 'HERMES_HOME': str(home), 'HERMES_KANBAN_HOME': str(home),
        'HERMES_BUNDLED_PLUGINS': str(home / 'empty-bundled'), 'PYTHONPATH': str(sdk),
        'PYTHONDONTWRITEBYTECODE': '1'}
    result = subprocess.run([sys.executable, str(Path(__file__).with_name('shared_repository_runner.py')),
        str(tmp_path), scenario], cwd=tmp_path, env=env, capture_output=True, text=True,
        timeout=65 if scenario == 'supervise_slice' or scenario.startswith('supervise_takeover') else 100)
    (tmp_path / 'public-runner-output.log').write_text(result.stdout + result.stderr)
    assert result.returncode == 0, result.stdout + result.stderr
    assert source_snapshot(sdk) == sdk_before
    assert json.loads((tmp_path / 'shared-worker-result.json').read_text())['status'] == 'passed'


@pytest.mark.parametrize('change', ['none', 'source_changed', 'failed_test', 'wrong_command', 'non_test', 'true', 'missing_test', 'failing', 'zero_tests'])
def test_shared_acceptance_reads_fixed_source_and_original_test_settlement(tmp_path, monkeypatch, change):
    from ghost_hermes_pm.simple_development import RepositoryIntake
    from ghost_hermes_pm.repository_acceptance import accept_repository_work, repository_source_state
    repo = tmp_path / 'repository'
    repo.mkdir()
    subprocess.run(['git', 'init', '-q', '-b', 'fixture-delivery', str(repo)], check=True)
    test_file = 'feature.test.mjs'
    (repo / test_file).write_text("console.log('900 passed');\n" if change == 'zero_tests' else
        "import test from 'node:test'; import assert from 'node:assert/strict';\n"
        "test('real assertion',()=>{assert.equal(40+2," + ('41' if change in {'non_test', 'failing'} else '42') + ")});\n")
    subprocess.run(['git', '-C', str(repo), 'add', '.'], check=True)
    subprocess.run(['git', '-C', str(repo), '-c', 'user.name=Fixture', '-c', 'user.email=fixture@example.invalid',
        'commit', '-qm', 'Synthetic fixed test source'], check=True)
    source = repository_source_state({'repo_path': str(repo)})
    from ghost_hermes_pm.shared_repository import verification_command
    from types import SimpleNamespace
    root = Path(__file__).resolve().parents[1]
    artifacts = tmp_path / 'artifacts'
    artifacts.mkdir(mode=0o700)
    verification = {'python': str(Path(sys.executable).resolve()), 'runner': str(root / 'ghost_hermes_pm/repository_test_runner.py'),
        'node': str(Path(shutil.which('node')).resolve())}
    verification.update({name + '_sha256': hashlib.sha256(Path(path).read_bytes()).hexdigest() for name, path in list(verification.items())})
    carrier = SimpleNamespace(config={'verification': verification})
    execution = {'session_id': 'fixture-session', 'generation': 'fixture-generation',
        'verification': {'artifact_root': str(artifacts), 'nonce': 'fixture-nonce'}}
    test_files = [test_file]
    command = verification_command(carrier.config, execution, str(repo), test_files)
    if change == 'non_test':
        command = '/usr/bin/printf "900 passed\\n"'
    elif change == 'true':
        command = '/usr/bin/true'
    elif change == 'missing_test':
        command = command.replace(test_file, 'nonexistent.test.mjs')
        test_files = ['nonexistent.test.mjs']
    actual_test = subprocess.run(['/bin/bash', '-c', command], cwd=repo,
        env={**os.environ, 'PYTHONDONTWRITEBYTECODE': '1'}, capture_output=True, text=True)
    assert actual_test.returncode == (1 if change in {'failing', 'missing_test'} else 0), actual_test.stderr
    issue = {'url': 'https://github.com/fixture-user/fixture/issues/7', 'title': 'Verify synthetic feature',
        'body': '- [ ] Verify the fixture feature.', 'updated_at': '2026-10-10T00:00:00Z'}
    state = tmp_path / 'github.json'
    state.write_text(json.dumps({'url': issue['url'], 'title': issue['title'], 'body': issue['body'],
        'updatedAt': issue['updated_at'], 'state': 'OPEN'}))
    gh = tmp_path / 'gh'
    gh.write_text('#!' + sys.executable + '\nimport json,os,sys\nfrom pathlib import Path\n'
        'a=sys.argv[1:]\n'
        'if a[:2]==["auth","switch"]: pass\n'
        'elif a[:2]==["api","user"]: print("fixture-user")\n'
        'elif a[:2]==["issue","view"]: print(Path(os.environ["SHARED_FIXTURE_GH"]).read_text())\n'
        'elif "/sub_issues" in a[1]: print("[]")\n'
        'else: sys.exit(2)\n')
    gh.chmod(0o700)
    monkeypatch.setenv('PATH', str(tmp_path) + os.pathsep + os.environ['PATH'])
    monkeypatch.setenv('SHARED_FIXTURE_GH', str(state))
    gh_config = tmp_path / 'gh-config'
    gh_config.mkdir(mode=0o700)
    intake = RepositoryIntake('fixture:owner', {}, {'github_account': 'fixture-user',
        'github_config_dir': str(gh_config)}, str(tmp_path / 'private'))
    snapshot = {key: source[key] for key in ('head', 'source_digest', 'workspace_status')}
    receipt = {'name': 'bash', 'kind': 'foreground', 'execution_admission': 'admitted',
        'generation': 'fixture-generation', 'session_id': 'fixture-session', 'call_id': 'fixture-test',
        'is_error': actual_test.returncode != 0, 'exit_code': actual_test.returncode, 'timed_out': False, 'aborted': False,
        'command_sha256': hashlib.sha256(command.encode()).hexdigest(), 'cwd': str(repo),
        'sandbox': {'mode': 'workspace-write'}, 'source_before': snapshot, 'source_after': snapshot,
        'stdout': {'sha256': hashlib.sha256(actual_test.stdout.encode()).hexdigest(), 'byte_length': len(actual_test.stdout.encode()), 'truncated': False},
        'stderr': {'sha256': hashlib.sha256(actual_test.stderr.encode()).hexdigest(), 'byte_length': len(actual_test.stderr.encode()), 'truncated': False}}
    if change == 'source_changed':
        receipt['source_before'] = dict(snapshot, source_digest='unknown-source')
    elif change == 'failed_test':
        receipt['exit_code'] = 1
    elif change == 'wrong_command':
        receipt['command_sha256'] = hashlib.sha256(b'echo ignored').hexdigest()
    report = {'issue_updated_at': issue['updated_at'], 'source_commit': source['head'],
        'criteria': [{'text': 'Verify the fixture feature.', 'test_call_ids': ['fixture-test']}],
        'fine_issue_urls': [], 'sync_branches': [], 'leftovers': [], 'test_files': test_files}
    events = [{'seq': 0, 'type': 'turn/start', 'data': {'turn': 1}},
        {'seq': 1, 'type': 'tool/call', 'data': {'callId': 'fixture-test', 'name': 'bash', 'arguments': {'command': command}}},
        {'seq': 2, 'type': 'tool/result', 'data': {'message': {'toolCallId': 'fixture-test', 'isError': actual_test.returncode != 0,
            'content': [{'type': 'text', 'text': actual_test.stdout}]}}},
        {'seq': 3, 'type': 'assistant/message', 'data': {'turn': 1, 'message': {'content': [{'type': 'text',
            'text': 'HERMES_REPOSITORY_DELIVERY_JSON ' + json.dumps(report)}]}}},
        {'seq': 4, 'type': 'turn/end', 'data': {'turn': 1, 'reason': {'kind': 'completed'}}}]
    record = {'id': 'fixture-work', 'card_id': 'fixture-card', 'issue': issue,
        'target': {'repo_path': str(repo), 'repository': 'fixture-user/fixture'},
        'dsh_execution': {'mode': 'shared_native', 'state': 'awaiting_acceptance', 'generation': 'fixture-generation',
            'session_id': 'fixture-session', 'baseline': source, 'tool_receipts': [receipt], 'jobs': [],
            'verification': execution['verification']}}
    intake._save(record)
    result = asyncio.run(accept_repository_work(intake, record, carrier, events,
        {'inbox': {'next-turn': [], 'next-step': []}}, intake.generation))
    assert (result['status'] == 'accepted') == (change == 'none'), json.dumps(result)
    if change == 'source_changed':
        assert 'test_source_unconfirmed' in result['unmet']
    elif change in {'non_test', 'true', 'zero_tests'}:
        assert 'test_runner_unconfirmed' in result['unmet']
    elif change != 'none':
        assert 'test_evidence_missing' in result['unmet']
