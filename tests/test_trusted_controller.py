"""The actual Mac socket peer must match a pinned process, including its birth."""
import hashlib
import json
import os
from pathlib import Path
import socket
import subprocess
import sys
import time
import pytest

from ghost_hermes_pm.trusted_controller import controller_helper, controller_identity, validate_controller_helper


def test_kernel_identity_does_not_collect_an_original_dispatcher_child():
    controller_identity()  # The one-time compiler runs before any dispatched child.
    pid = subprocess.Popen([sys.executable, '-c', 'import time;time.sleep(.2)']).pid
    deadline = time.monotonic() + 3
    while time.monotonic() < deadline:
        controller_identity()
        waited, status = os.waitpid(pid, os.WNOHANG)
        if waited:
            assert waited == pid and os.waitstatus_to_exitcode(status) == 0
            return
        time.sleep(.01)
    raise AssertionError('Original child exit could not be collected.')


def test_original_model_bash_controller_boundary():
    if not os.environ.get('DSH_TEST_SDK_ROOT'):
        if os.environ.get('DSH_REQUIRE_SDK_SMOKE') == '1':
            pytest.fail('The original DSH SDK is required for the native security boundary.')
        pytest.skip('The original DSH SDK was not configured.')
    from trusted_controller_native_runner import original_model_bash_controller_boundary
    original_model_bash_controller_boundary()


@pytest.mark.parametrize('gateway', [True, False])
def test_same_uid_descendant_cannot_forge_a_controller(tmp_path, gateway):
    executable = Path(controller_helper())
    parent = controller_identity()
    configuration = {'controller_helper_path': str(executable), 'launcher_controller': parent,
        'trusted_controllers': [parent] if gateway else [], 'workspace': str(tmp_path), 'dsh_home': str(tmp_path / 'home'),
        'socket_path': '/private/tmp/hpm-peer-' + str(os.getpid()) + '.sock', 'writable_roots': [str(tmp_path), '/private/tmp'],
        'source_bindings': [{'path': str(executable), 'resolved': str(executable),
            'device': str(executable.stat().st_dev), 'inode': str(executable.stat().st_ino),
            'sha256': hashlib.sha256(executable.read_bytes()).hexdigest()}]}
    validate_controller_helper(str(executable), str(tmp_path), str(tmp_path / 'home'))
    settings = tmp_path / 'peer.json'; settings.write_text(json.dumps(configuration))
    host = subprocess.Popen(['/opt/homebrew/bin/node', str(Path(__file__).with_name('trusted_controller_probe.mjs')),
        str(settings)], stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    try:
        assert host.stdout.readline() == 'ready\n', host.stderr.read()
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as channel:
            channel.connect(configuration['socket_path'])
            channel.sendall(b'{"op":"events"}\n')
            confirmed = json.loads(channel.recv(8192))
            assert confirmed['ok'] is True and confirmed['peer'] == parent
        attacker = '''import json,socket,sys
config=json.loads(sys.argv[1]); results=[]
for op in ['attach','shutdown','events','verify_tests','call','open','cancel','admit_controller']:
 with socket.socket(socket.AF_UNIX,socket.SOCK_STREAM) as s:
  s.connect(config['socket_path'])
  s.sendall((json.dumps({'op':op,'controller':config['launcher_controller'],'pid':config['launcher_controller']['pid'],
    'owner':True,'trusted':True,'args':{'_hermes_approval':{'approval_id':'synthetic-pending','call_id':'native-call',
    'command_sha256':'a'*64,'generation':'same-generation','session_id':'same-session'}}})+'\\n').encode())
  results.append(json.loads(s.recv(8192)))
print(json.dumps(results))
'''
        result = subprocess.run([sys.executable, '-c', attacker, json.dumps(configuration)], capture_output=True, text=True)
        assert result.returncode == 0, result.stderr
        assert json.loads(result.stdout) == [{'ok': False}] * 8
        forged_birth = {**parent, 'start_usec': str(int(parent['start_usec']) + 1)}
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as channel:
            channel.connect(configuration['socket_path'])
            channel.sendall((json.dumps({'op': 'admit_controller', 'controller': forged_birth}) + '\n').encode())
            assert json.loads(channel.recv(8192)) == {'ok': False}
        child = subprocess.Popen([sys.executable, '-c', '''import json,socket,sys
for line in sys.stdin:
 with socket.socket(socket.AF_UNIX,socket.SOCK_STREAM) as s:
  s.connect(sys.argv[1]);s.sendall(line.encode());print(s.recv(8192).decode().strip(),flush=True)
''', configuration['socket_path']], stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True)
        try:
            identity = controller_identity(child.pid)
            child.stdin.write('{"op":"events"}\n'); child.stdin.flush()
            assert json.loads(child.stdout.readline()) == {'ok': False}
            with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as channel:
                channel.connect(configuration['socket_path'])
                channel.sendall((json.dumps({'op': 'admit_controller', 'controller': identity}) + '\n').encode())
                response = json.loads(channel.recv(8192))
                assert response['ok'] is gateway
            child.stdin.write('{"op":"events"}\n'); child.stdin.flush()
            response = json.loads(child.stdout.readline())
            assert response['ok'] is gateway
            if gateway:
                assert response['peer'] == identity
                # An admitted worker remains unable to grant any identity.
                child.stdin.write(json.dumps({'op': 'admit_controller', 'controller': identity}) + '\n'); child.stdin.flush()
                assert json.loads(child.stdout.readline()) == {'ok': False}
        finally:
            child.stdin.close()
            assert child.wait(timeout=5) == 0
    finally:
        running = host.poll() is None
        if running:
            host.stdin.write('close\n'); host.stdin.flush()
        code = host.wait(timeout=5)
        Path(configuration['socket_path']).unlink(missing_ok=True)
        if running:
            assert code == 0, host.stderr.read()
