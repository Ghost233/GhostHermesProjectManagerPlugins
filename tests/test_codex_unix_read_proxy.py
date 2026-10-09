"""Exercise the plugin-owned bridge against a real synthetic Unix-WebSocket peer."""
import hashlib
import json
import selectors
import subprocess
import sys
import tempfile
import time
from pathlib import Path

import pytest

from ghost_hermes_pm import ManagementError
from ghost_hermes_pm.observation import READ_METHODS, configured_observation_adapters

PROXY = Path(__file__).resolve().parents[1] / 'ghost_hermes_pm' / 'codex_unix_read_proxy.py'


def line(process, timeout=5):
    with selectors.DefaultSelector() as selector:
        selector.register(process.stdout, selectors.EVENT_READ)
        assert selector.select(timeout), 'Owned client produced no protocol response.'
    return process.stdout.readline()


def wait_file(path):
    deadline = time.monotonic() + 5
    while not path.exists() and time.monotonic() < deadline:
        time.sleep(.02)
    assert path.exists()


@pytest.fixture
def unix_peer(tmp_path):
    (tmp_path / 'state.json').write_text('{}')
    with tempfile.TemporaryDirectory(prefix='synthetic-unix-', dir='/private/tmp') as sockets:
        endpoint = Path(sockets) / 'peer.sock'
        process = subprocess.Popen([sys.executable, str(Path(__file__).with_name('codex_unix_fixture_server.py')), str(tmp_path), str(endpoint)],
                                   stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        try:
            assert json.loads(line(process)) == {'ready': True}
            yield tmp_path, endpoint, process
        finally:
            process.terminate()
            process.wait(timeout=5)
            process.stdout.close()
            process.stderr.close()


def bridge(peer):
    root, endpoint, _ = peer
    return subprocess.Popen([sys.executable, str(PROXY), '--sock', str(endpoint)], cwd=root,
                            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE)


def send(process, envelope):
    process.stdin.write((json.dumps(envelope) + '\n').encode())
    process.stdin.flush()


def close_bridge(process):
    if not process.stdin.closed:
        process.stdin.close()
    try:
        process.wait(timeout=5)
    except subprocess.TimeoutExpired:
        process.kill()
        process.wait(timeout=5)
        raise
    stderr = process.stderr.read()
    process.stdout.close()
    process.stderr.close()
    assert stderr == b''


def config(peer):
    root, endpoint, _ = peer
    return {'executable': sys.executable, 'cwd': str(root), 'environment': {'PATH': '/usr/bin:/bin', 'CODEX_HOME': str(root / 'synthetic-home')},
            'service_ref': 'local:synthetic-original', 'source_kind': 'daemon', 'endpoint': str(endpoint),
            'endpoint_ref': 'local:synthetic-endpoint', 'transport': 'original_unix_websocket'}


def synthetic_receipt(state, configuration, adapter):
    evidence = state / 'observation-evidence'
    evidence.mkdir(parents=True)
    receipt = {**{key: getattr(adapter, key) for key in ('generation', 'service_ref', 'source_kind', 'endpoint_ref', 'transport')},
               'original_executor_id': 'synthetic-existing-executor', 'provenance': 'trusted_host_original_executor',
               'binary_sha256': hashlib.sha256(Path(configuration['executable']).read_bytes()).hexdigest(),
               'configuration_sha256': hashlib.sha256(json.dumps(configuration, sort_keys=True).encode()).hexdigest(),
               'endpoint_sha256': hashlib.sha256(configuration['endpoint'].encode()).hexdigest(), 'platform': sys.platform,
               'original_endpoint_verified': 'PASS', 'observation_read_only': 'PASS', 'supported_methods': sorted(READ_METHODS),
               'source_kinds': ['appServer'], 'evidence_origin': 'synthetic-fixture-only',
               'read_cases': {method: 'PASS' for method in READ_METHODS | {'no_execution_writes', 'original_request_routing', 'unsupported_scope', 'disconnect'}}}
    path = evidence / 'synthetic.json'
    path.write_text(json.dumps(receipt))
    (state / 'codex-observation.json').write_text(json.dumps({configuration['service_ref']: {
        'path': 'observation-evidence/synthetic.json', 'sha256': hashlib.sha256(path.read_bytes()).hexdigest()}}))


def test_configured_adapter_performs_websocket_upgrade_and_original_read_roundtrip(unix_peer):
    root, _, server = unix_peer
    configuration = config(unix_peer)
    adapter = configured_observation_adapters([configuration], root / 'authority')[configuration['service_ref']]
    synthetic_receipt(root / 'authority', configuration, adapter)
    try:
        connection = adapter.connect()
        assert connection['transport'] == 'original_unix_websocket'
        assert connection['control'] == 'observe_only'
        assert connection['service_id'] == 'synthetic-existing-executor'
        assert adapter.loaded_threads() == ['synthetic-thread']
        assert adapter.read_thread('synthetic-thread')['status']['type'] == 'active'
        assert adapter._pages('thread/list', {'useStateDbOnly': True, 'archived': False, 'sourceKinds': ['appServer']}) == [{'id': 'synthetic-thread'}]
    finally:
        adapter.close()
    wait_file(root / 'closed.jsonl')
    assert server.poll() is None
    handshake = json.loads((root / 'handshakes.jsonl').read_text().splitlines()[0])
    assert handshake == {'path': '/', 'upgrade': 'websocket', 'connection': 'Upgrade'}
    requests = [json.loads(item) for item in (root / 'wire.jsonl').read_text().splitlines()]
    assert [request['method'] for request in requests] == ['initialize', 'initialized', 'thread/loaded/list', 'thread/read', 'thread/list']
    assert all('jsonrpc' not in request for request in requests)


@pytest.mark.parametrize('envelope', [
    {'id': 91, 'method': 'thread/start', 'params': {}},
    {'id': 92, 'method': 'turn/start', 'params': {}},
    {'id': 93, 'method': 'turn/interrupt', 'params': {}},
    {'id': 'server-approval', 'result': {'decision': 'approved'}},
    {'id': 'server-approval', 'error': {'code': -1, 'message': 'synthetic'}},
    {'id': 94, 'method': 'thread/read', 'result': {}, 'params': {}},
    {'id': 95, 'method': 'thread/read', 'jsonrpc': '2.0', 'params': {}},
    {'id': 96, 'method': 'thread/list', 'params': {}},
    {'id': 97, 'method': 'thread/list', 'params': {'useStateDbOnly': False}},
    {'id': 98, 'method': ['thread/read'], 'params': {}},
])
def test_bridge_independently_rejects_forged_writes_and_server_answers(unix_peer, envelope):
    root, _, server = unix_peer
    process = bridge(unix_peer)
    try:
        send(process, envelope)
        rejected = json.loads(line(process))
        assert rejected['id'] == envelope['id']
        assert rejected['error']['code'] == -32601
        send(process, {'id': 100, 'method': 'thread/loaded/list', 'params': {}})
        assert json.loads(line(process)) == {'id': 100, 'result': {'data': ['synthetic-thread'], 'nextCursor': None}}
    finally:
        close_bridge(process)
    assert server.poll() is None
    requests = [json.loads(item) for item in (root / 'wire.jsonl').read_text().splitlines()]
    assert requests == [{'id': 100, 'method': 'thread/loaded/list', 'params': {}}]


def test_original_server_request_is_observed_without_answer(unix_peer):
    root, _, _ = unix_peer
    (root / 'state.json').write_text(json.dumps({'approval_request': True}))
    configuration = config(unix_peer)
    adapter = configured_observation_adapters([configuration], root / 'authority')[configuration['service_ref']]
    synthetic_receipt(root / 'authority', configuration, adapter)
    try:
        adapter.connect()
        assert adapter.take_events('synthetic-thread')[0]['method'] == 'item/commandExecution/requestApproval'
        with pytest.raises(ManagementError) as refused:
            adapter._write({'id': 'server-approval', 'result': {'decision': 'approved'}})
        assert refused.value.code == 'forbidden'
    finally:
        adapter.close()
    assert all('result' not in json.loads(item) for item in (root / 'wire.jsonl').read_text().splitlines())


def test_remote_disconnect_closes_owned_bridge_without_touching_server(unix_peer):
    root, _, server = unix_peer
    (root / 'state.json').write_text(json.dumps({'disconnect_on': 'thread/loaded/list'}))
    process = bridge(unix_peer)
    try:
        send(process, {'id': 1, 'method': 'thread/loaded/list', 'params': {}})
        assert line(process) == b''
        assert process.wait(timeout=5) == 0
    finally:
        close_bridge(process)
    assert server.poll() is None
    wait_file(root / 'closed.jsonl')


def test_parent_stdin_eof_closes_websocket_without_touching_server(unix_peer):
    root, _, server = unix_peer
    process = bridge(unix_peer)
    try:
        send(process, {'id': 1, 'method': 'thread/loaded/list', 'params': {}})
        assert json.loads(line(process))['id'] == 1
    finally:
        close_bridge(process)
    assert process.returncode == 0
    assert server.poll() is None
    wait_file(root / 'closed.jsonl')


def test_terminated_owned_client_closes_socket_and_leaves_original_peer_alive(unix_peer):
    root, _, server = unix_peer
    process = bridge(unix_peer)
    try:
        send(process, {'id': 1, 'method': 'thread/loaded/list', 'params': {}})
        assert json.loads(line(process))['id'] == 1
        process.terminate()
        assert process.wait(timeout=5) != 0
    finally:
        close_bridge(process)
    assert server.poll() is None
    wait_file(root / 'closed.jsonl')


@pytest.mark.parametrize('extra_bytes', [0, 1])
def test_input_jsonl_frame_boundary_matches_existing_adapter(unix_peer, extra_bytes):
    root, _, _ = unix_peer
    process = bridge(unix_peer)
    envelope = json.dumps({'id': 1, 'method': 'thread/loaded/list', 'params': {}}).encode()
    frame = envelope + b' ' * (16 * 1024 * 1024 - len(envelope) - 1 + extra_bytes) + b'\n'
    try:
        process.stdin.write(frame)
        process.stdin.flush()
        response = line(process)
        if extra_bytes:
            assert response == b''
            assert process.wait(timeout=5) != 0
            assert not (root / 'wire.jsonl').exists()
        else:
            assert json.loads(response)['result']['data'] == ['synthetic-thread']
    finally:
        close_bridge(process)


def test_oversized_websocket_frame_is_not_written_to_jsonl(unix_peer):
    root, _, _ = unix_peer
    (root / 'state.json').write_text(json.dumps({'oversize_on': 'thread/loaded/list'}))
    process = bridge(unix_peer)
    try:
        send(process, {'id': 1, 'method': 'thread/loaded/list', 'params': {}})
        assert line(process) == b''
        assert process.wait(timeout=5) != 0
    finally:
        close_bridge(process)


def test_transport_configuration_keeps_original_proof_and_stdio_default(unix_peer):
    root, _, _ = unix_peer
    configuration = config(unix_peer)
    for configured in (configuration, {key: value for key, value in configuration.items() if key != 'transport'}):
        adapter = configured_observation_adapters([configured], root / 'missing-authority')[configuration['service_ref']]
        try:
            with pytest.raises(ManagementError) as blocked:
                adapter.connect()
            assert blocked.value.code == 'capability_unverified'
            assert adapter._process is None
            if 'transport' not in configured:
                assert adapter.command == [sys.executable, 'app-server', 'proxy', '--sock', configuration['endpoint']]
                assert adapter.transport == 'original_proxy_stdio'
        finally:
            adapter.close()
    assert not (root / 'handshakes.jsonl').exists()
    with pytest.raises(ManagementError) as unknown:
        configured_observation_adapters([{**configuration, 'transport': 'unregistered'}], root)
    assert unknown.value.code == 'invalid_change'
