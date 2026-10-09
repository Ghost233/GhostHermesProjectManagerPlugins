"""Actual owned native Host seam; no model, auth publication or upstream edit."""
import hashlib
import os
from pathlib import Path
import socket
import shutil
import sys
import subprocess
import json
import threading
import time

import pytest

from ghost_hermes_pm.dsh import configured_adapter
from ghost_hermes_pm.dsh_owned import DshOwnedAdapter
from ghost_hermes_pm.dsh_owned_transport import NativeOwnedTransport
from ghost_hermes_pm.manager import ManagementError


@pytest.fixture(scope='module')
def runtime_root():
    root = os.environ.get('DSH_TEST_SDK_ROOT')
    if not root:
        if os.environ.get('DSH_REQUIRE_SDK_SMOKE') == '1':
            pytest.fail('The original DSH runtime is required for native acceptance.')
        pytest.skip('Original runtime absent; native acceptance remains unverified.')
    def inventory():
        return {str(file.relative_to(root)): hashlib.sha256(file.read_bytes()).digest()
                for file in Path(root).rglob('*') if file.is_file()
                and file.suffix in {'.js', '.mjs', '.cjs', '.json', '.ts', '.yaml', '.yml'}}
    before = inventory()
    yield root
    assert inventory() == before, 'Original DSH source bytes changed.'


def owned(tmp_path, runtime_root):
    workspace = tmp_path / 'workspace'
    workspace.mkdir()
    return DshOwnedAdapter(dsh_home=str(tmp_path / 'home'), workspace=str(workspace),
        runtime_package_root=runtime_root, service_ref='local:synthetic-owned', timeout=10)


def test_incomplete_owned_runtime_failure_stays_private_in_the_original_hook_log(tmp_path):
    sdk = os.environ.get('HERMES_TEST_SDK_ROOT')
    if not sdk:
        if os.environ.get('HERMES_REQUIRE_SDK_SMOKE') == '1':
            pytest.fail('The original Hermes SDK is required for hook log acceptance.')
        pytest.skip('Original Hermes SDK absent; hook log acceptance remains unverified.')
    from sdk_source_integrity import source_snapshot
    before = source_snapshot(Path(sdk))
    workspace = tmp_path / 'private-workspace'
    workspace.mkdir()
    runtime = tmp_path / 'private-runtime'
    boot = runtime / '@deepseek-ai/dsh/lib/profile-boot.js'
    boot.parent.mkdir(parents=True)
    boot.write_text('// synthetic incomplete package')
    home = tmp_path / 'private-home'
    code = """
import asyncio, io, logging, os, sys
from hermes_cli.plugins_dispatch import PluginDispatchMixin
from ghost_hermes_pm.dsh_owned_transport import NativeOwnedTransport
from ghost_hermes_pm.manager import ManagementError
output, failures = io.StringIO(), []
handler = logging.StreamHandler(output)
logger = logging.getLogger('hermes_cli.plugins')
logger.addHandler(handler)
def rejected_start(**kwargs):
    try:
        NativeOwnedTransport(dsh_home=sys.argv[1], workspace=sys.argv[2],
            runtime_package_root=sys.argv[3], node_bin=sys.executable)
    except Exception as exc:
        failures.append(exc)
        raise
host = PluginDispatchMixin()
host._hooks = {'pre_gateway_dispatch': [rejected_start]}
host._hook_failures_reported = set()
try:
    assert asyncio.run(host.ainvoke_hook('pre_gateway_dispatch')) == []
finally:
    logger.removeHandler(handler)
assert len(failures) == 1 and isinstance(failures[0], ManagementError)
assert failures[0].__suppress_context__ and failures[0].__cause__ is None
assert 'raised:' in output.getvalue()
assert all(path not in output.getvalue() for path in sys.argv[1:])
assert not os.path.exists(sys.argv[1])
"""
    env = {**os.environ, 'HOME': str(tmp_path), 'HERMES_HOME': str(tmp_path / 'hermes'),
           'PYTHONDONTWRITEBYTECODE': '1', 'HERMES_DISABLE_PROJECT_PLUGINS': '1',
           'PYTHONPATH': os.pathsep.join((str(Path(sdk)), str(Path(__file__).resolve().parents[1])))}
    result = subprocess.run([sys.executable, '-c', code, str(home), str(workspace), str(runtime)],
                            env=env, text=True, capture_output=True, timeout=30)
    assert source_snapshot(Path(sdk)) == before
    assert result.returncode == 0, result.stdout + result.stderr


def test_owned_native_original_boot_reads_empty_catalog_and_shuts_down_without_auth(tmp_path, runtime_root):
    adapter = owned(tmp_path, runtime_root)
    original = Path(runtime_root, '@deepseek-ai/dsh/lib/profile-boot.js')
    before = hashlib.sha256(original.read_bytes()).digest()
    try:
        connection = adapter.connect()
        assert connection['transport'] == 'owned_native'
        assert connection['client_id']
        assert connection['owned_pid'] > 0
        assert connection['process_coverage'] == 'unverified'
        assert adapter.list_sessions() == []
    finally:
        adapter.close()
    assert adapter._transport._process.poll() == 0
    assert adapter._transport.close_outcome == {'kind': 'original_exit', 'exit_code': 0}
    assert hashlib.sha256(original.read_bytes()).digest() == before
    assert Path(adapter.dsh_home).stat().st_mode & 0o777 == 0o700


def test_configured_default_is_owned_native_and_rejects_implicit_old_sources(tmp_path, runtime_root):
    workspace = tmp_path / 'workspace'
    workspace.mkdir()
    config = {'dsh_home': str(tmp_path / 'home'), 'workspace': str(workspace),
              'runtime_package_root': runtime_root, 'service_ref': 'local:synthetic-owned'}
    adapter = configured_adapter(config, tmp_path)
    assert adapter.transport == 'owned_native'
    assert adapter.base_url is None
    adapter.close()
    for other in ({'base_url': 'http://127.0.0.1:8000', 'cookie': 'synthetic=x'},
                  {**config, 'auth_publication': 'synthetic'}, {**config, 'mode': 'owned_sdk'}):
        with pytest.raises(ManagementError):
            configured_adapter(other, tmp_path)


def test_owned_native_existing_permissive_home_is_refused_without_repair(tmp_path, runtime_root):
    adapter = owned(tmp_path, runtime_root)
    home = Path(adapter.dsh_home)
    home.mkdir(mode=0o755)
    home.chmod(0o755)
    with pytest.raises(ManagementError):
        adapter.connect()
    assert home.stat().st_mode & 0o777 == 0o755
    assert adapter._transport._process is None


def test_original_owned_host_still_refuses_unverified_execution_before_any_session_create(tmp_path, runtime_root):
    adapter = owned(tmp_path, runtime_root)
    try:
        adapter.connect()
        with pytest.raises(ManagementError) as caught:
            adapter.verify_start({'logical_id': 'synthetic', 'worktree': adapter.workspace})
        assert caught.value.code == 'capability_unverified'
        assert adapter.list_sessions() == []
    finally:
        adapter.close()


def test_released_owned_work_restarts_original_profile_with_new_generation_and_retained_home(tmp_path, runtime_root):
    adapter = owned(tmp_path, runtime_root)
    try:
        first = adapter.connect()
        marker = Path(adapter.dsh_home, 'synthetic-retained-data')
        marker.write_text('preserved')
        with pytest.raises(ManagementError):
            adapter.prepare_new_work()
        adapter.prepare_new_work(previous_scope_released=True)
        second = adapter.connect()
        assert second['generation'] != first['generation']
        assert second['owned_pid'] != first['owned_pid']
        assert second['client_id'] != first['client_id']
        assert marker.read_text() == 'preserved'
        assert adapter.list_sessions() == []
    finally:
        adapter.close()


def test_original_native_stream_cancellation_does_not_end_the_owned_profile(tmp_path, runtime_root):
    adapter = owned(tmp_path, runtime_root)
    try:
        first = adapter.connect()
        stream = adapter._transport.stream('session/control', {})
        assert next(stream)['type'] == 'baseline'
        stream.close()
        assert adapter.list_sessions() == []
        assert adapter.connect()['generation'] == first['generation']
    finally:
        adapter.close()


def test_closed_owned_native_adapter_never_launches_a_later_process(tmp_path, runtime_root):
    adapter = owned(tmp_path, runtime_root)
    adapter.close()
    with pytest.raises(ManagementError):
        adapter.connect()
    assert adapter._transport._process is None


def test_original_special_event_result_handler_rejects_foreign_client_without_a_network_or_auth_roundtrip(tmp_path, runtime_root):
    adapter = owned(tmp_path, runtime_root)
    try:
        adapter.connect()
        response = adapter._transport.request('$events/result', {'clientId': 'synthetic-foreign-client',
             'eventId': 'synthetic-event', 'outcome': {'kind': 'next'}}, 'synthetic-rpc')
        assert response['type'] == 'server-response'
        assert response['rpcId'] == 'synthetic-rpc'
        assert response['result']['ok'] is False
        assert isinstance(response['result']['error']['code'], str)
        # A current client replying to a finished/absent native event is an
        # original idempotent no-op, rather than an invented question answer.
        args = {'clientId': adapter.connection['client_id'], 'eventId': 'synthetic-finished-event',
                'outcome': {'kind': 'next'}}
        for identity in ('synthetic-next-one', 'synthetic-next-two'):
            current = adapter._transport.request('$events/result', args, identity)
            assert current == {'type': 'server-response', 'rpcId': identity, 'result': {'ok': True}}
        assert adapter.list_sessions() == []
    finally:
        adapter.close()


def test_original_owned_job_roster_uses_its_native_stream_and_stays_readonly(tmp_path, runtime_root):
    adapter = owned(tmp_path, runtime_root)
    try:
        adapter.connect()
        assert adapter.background_terminals('synthetic-session') == []
        assert adapter.list_sessions() == []
    finally:
        adapter.close()


def test_home_alias_inserted_after_configuration_never_creates_a_profile_in_another_home(tmp_path, runtime_root):
    adapter = owned(tmp_path, runtime_root)
    other = tmp_path / 'other-home'
    other.mkdir(mode=0o700)
    Path(adapter.dsh_home).symlink_to(other, target_is_directory=True)
    try:
        with pytest.raises(ManagementError):
            adapter.connect()
        assert not (other / 'profiles').exists()
        assert adapter._transport._process is None
    finally:
        adapter.close()


def test_an_existing_private_home_replaced_by_another_inode_is_refused_before_boot(tmp_path, runtime_root):
    home = tmp_path / 'home'
    home.mkdir(mode=0o700)
    adapter = owned(tmp_path, runtime_root)
    home.rename(tmp_path / 'retained-home')
    home.mkdir(mode=0o700)
    try:
        with pytest.raises(ManagementError):
            adapter.connect()
        assert not (home / 'profiles').exists()
        assert adapter._transport._process is None
    finally:
        adapter.close()


def test_workspace_alias_added_after_configuration_is_refused_before_preparing_home(tmp_path, runtime_root):
    adapter = owned(tmp_path, runtime_root)
    workspace = Path(adapter.workspace)
    workspace.rename(tmp_path / 'retained-workspace')
    other = tmp_path / 'other-workspace'
    other.mkdir()
    workspace.symlink_to(other, target_is_directory=True)
    try:
        with pytest.raises(ManagementError):
            adapter.connect()
        assert not Path(adapter.dsh_home).exists()
        assert adapter._transport._process is None
    finally:
        adapter.close()


def test_backpressure_cannot_block_deadline_or_prevent_close(tmp_path, runtime_root):
    carrier = NativeOwnedTransport(dsh_home=str(tmp_path / 'home'), workspace=str(tmp_path),
                                  runtime_package_root=runtime_root, timeout=0.05)
    parent, peer = socket.socketpair()
    parent.setsockopt(socket.SOL_SOCKET, socket.SO_SNDBUF, 4096)
    carrier._socket, carrier._started = parent, True
    errors = []
    def transmit():
        try:
            carrier.request('session/list', {'_request': {'synthetic': 'x' * 500000}}, 'synthetic-backpressure')
        except Exception as error:
            errors.append(type(error).__name__)
    sender = threading.Thread(target=transmit, daemon=True)
    closer = threading.Thread(target=carrier.close, daemon=True)
    try:
        sender.start()
        time.sleep(0.01)
        closer.start()
        closer.join(timeout=0.3)
        sender.join(timeout=0.3)
        assert not closer.is_alive(), 'Backpressure blocked owned cleanup.'
        assert not sender.is_alive(), 'A bounded write remained blocked.'
        assert carrier._closed is True
        assert errors
    finally:
        peer.close()
        sender.join(timeout=1)
        closer.join(timeout=1)


def test_write_deadline_expires_even_without_a_peer_read_or_close(tmp_path, runtime_root):
    carrier = NativeOwnedTransport(dsh_home=str(tmp_path / 'home'), workspace=str(tmp_path),
                                  runtime_package_root=runtime_root, timeout=0.05)
    parent, peer = socket.socketpair()
    parent.setsockopt(socket.SOL_SOCKET, socket.SO_SNDBUF, 4096)
    carrier._socket, carrier._started = parent, True
    result = []
    def transmit():
        try:
            carrier.request('session/list', {'_request': {'synthetic': 'x' * 500000}}, 'synthetic-deadline')
        except Exception:
            result.append('unconfirmed')
    sender = threading.Thread(target=transmit, daemon=True)
    try:
        sender.start()
        sender.join(timeout=0.3)
        assert not sender.is_alive(), 'The carrier ignored its write deadline.'
        assert result == ['unconfirmed']
    finally:
        peer.close()
        sender.join(timeout=1)
        carrier.close()


def test_parent_eof_during_original_boot_shuts_down_late_host_without_a_ready_publication(tmp_path, runtime_root):
    home = tmp_path / 'home'
    home.mkdir(mode=0o700)
    workspace = tmp_path / 'workspace'
    workspace.mkdir()
    parent, child = socket.socketpair()
    driver = Path(__file__).parents[1] / 'ghost_hermes_pm/owned_native_host.mjs'
    environment = dict(os.environ)
    environment.update(HOME=str(home), DSH_HOME=str(home), DSH_TELEMETRY_DISABLED='1', HERMES_PM_IPC_FD=str(child.fileno()))
    process = subprocess.Popen(['node', str(driver)], cwd=workspace, env=environment,
         pass_fds=(child.fileno(),), stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    child.close()
    try:
        source_paths = [workspace, Path(runtime_root), Path(shutil.which('node')).resolve(), driver,
                        Path(runtime_root, '@deepseek-ai/dsh/lib/profile-boot.js'),
                        Path(runtime_root, '@deepseek-ai/dsh-launch-environment/lib/index.js')]
        bindings = [{'path': str(path), 'resolved': str(path.resolve()), 'device': str(path.stat().st_dev),
                     'inode': str(path.stat().st_ino)} for path in source_paths]
        home_stat = home.stat()
        parent.sendall(json.dumps({'id': 'synthetic-boot-eof', 'op': 'boot',
              'config': {'dsh_home': str(home), 'runtime_package_root': runtime_root,
                         'home_identity': {'device': str(home_stat.st_dev), 'inode': str(home_stat.st_ino)},
                         'source_bindings': bindings}}).encode() + b'\n')
        deadline = time.monotonic() + 5
        while not list(home.glob('.hermes-carrier-*')) and process.poll() is None and time.monotonic() < deadline:
            time.sleep(0.01)
        assert list(home.glob('.hermes-carrier-*')), 'The native boot seam was not reached.'
        parent.close()
        try:
            code = process.wait(timeout=3)
        except subprocess.TimeoutExpired:
            pytest.fail('The late native Host remained alive after parent EOF.')
        assert code == 0
    finally:
        parent.close()
        if process.poll() is None:
            process.terminate()
            process.wait(timeout=5)


def test_forced_owned_child_cleanup_is_not_reported_as_an_original_graceful_shutdown(tmp_path, runtime_root):
    carrier = NativeOwnedTransport(dsh_home=str(tmp_path / 'home'), workspace=str(tmp_path),
                                  runtime_package_root=runtime_root, timeout=0.05)
    process = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(60)'],
                               stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    carrier._process, carrier._started = process, True
    try:
        carrier.close()
        assert process.poll() is not None
        assert carrier.close_outcome['kind'] in {'forced_terminate', 'forced_kill'}
        assert carrier.close_outcome['exit_code'] != 0
    finally:
        if process.poll() is None:
            process.kill()
            process.wait(timeout=2)
