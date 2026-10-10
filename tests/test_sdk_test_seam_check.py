"""Public SDK boundary check command."""
from pathlib import Path
import subprocess
import sys

COMMAND = Path(__file__).resolve().parents[1] / "tools/check_sdk_test_seams.py"

def test_original_gateway_is_allowed(tmp_path):
    directory = tmp_path / "tests"
    directory.mkdir()
    (directory / "simple_example_smoke_runner.py").write_text("from gateway.run import GatewayRunner as NativeGateway\nrunner = NativeGateway(config)\n")
    result = subprocess.run([sys.executable, str(COMMAND), "--root", str(tmp_path)], capture_output=True, text=True)
    assert result.returncode == 0, result.stdout + result.stderr


def test_gateway_subclass_replacement_is_rejected(tmp_path):
    directory = tmp_path / 'tests'
    directory.mkdir()
    (directory / 'simple_example_smoke_runner.py').write_text(
        'from gateway.run import GatewayRunner as NativeGateway\n'
        'class Fixture(NativeGateway):\n    async def _handle_message(self, event):\n        pass\n')
    result = subprocess.run([sys.executable, str(COMMAND), '--root', str(tmp_path)], capture_output=True, text=True)
    assert result.returncode == 1, result.stdout + result.stderr


def test_gateway_monkey_patch_is_rejected(tmp_path):
    directory = tmp_path / 'tests'
    directory.mkdir()
    (directory / 'simple_example_smoke_runner.py').write_text(
        'import gateway.run as sdk\n'
        'monkeypatch.setattr(sdk.GatewayRunner, "_handle_message", lambda *_: None)\n')
    result = subprocess.run([sys.executable, str(COMMAND), '--root', str(tmp_path)], capture_output=True, text=True)
    assert result.returncode == 1, result.stdout + result.stderr


def test_gateway_instance_method_replacement_is_rejected(tmp_path):
    directory = tmp_path / 'tests'
    directory.mkdir()
    (directory / 'simple_example_smoke_runner.py').write_text(
        'from gateway.run import GatewayRunner\nrunner = GatewayRunner(config)\n'
        'runner._handle_active_session_busy_message = lambda *_: False\n')
    result = subprocess.run([sys.executable, str(COMMAND), '--root', str(tmp_path)], capture_output=True, text=True)
    assert result.returncode == 1, result.stdout + result.stderr


def test_historical_fixture_is_not_reclassified_as_new_scope(tmp_path):
    directory = tmp_path / 'tests'
    directory.mkdir()
    (directory / 'historical_smoke_runner.py').write_text(
        'from gateway.run import GatewayRunner\nclass Fixture(GatewayRunner):\n    pass\n')
    result = subprocess.run([sys.executable, str(COMMAND), '--root', str(tmp_path)], capture_output=True, text=True)
    assert result.returncode == 0, result.stdout + result.stderr
