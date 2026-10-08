"""The repository command gate preserves raw exits and rejects changed inputs."""
import json
from pathlib import Path
import subprocess
import sys

import pytest

CHECKS = Path(__file__).resolve().parents[1] / 'tools' / 'local_checks.py'


def owned_repo(tmp_path):
    root = tmp_path / 'repo'
    root.mkdir()
    (root / 'source.txt').write_text('before')
    subprocess.run(['git', 'init', '-q', str(root)], check=True)
    subprocess.run(['git', '-C', str(root), 'add', 'source.txt'], check=True)
    subprocess.run(['git', '-C', str(root), '-c', 'user.name=Owned Checks', '-c', 'user.email=checks@example.invalid', 'commit', '-qm', 'Owned input'], check=True)
    return root


def run_check(tmp_path, root, code, python=None):
    artifacts = tmp_path / 'artifacts'
    args = [sys.executable, str(CHECKS), '--root', str(root), '--artifacts', str(artifacts)]
    if python:
        args += ['--python', str(python)]
    result = subprocess.run(args + ['--', sys.executable, '-c', code], capture_output=True, text=True)
    return result, json.loads((artifacts / 'result.json').read_text())


def test_command_zero_unchanged_is_a_validated_pass(tmp_path):
    result, record = run_check(tmp_path, owned_repo(tmp_path), 'print("owned success")')
    assert result.returncode == 0
    assert record['command_exit_code'] == record['validation_exit_code'] == 0
    assert record['source_unchanged'] is True
    assert len(result.stdout) < 2000 and 'source.txt' not in result.stdout
    assert Path(record['commands'][0]['log']).read_text() == 'owned success\n'


def test_command_zero_with_source_drift_fails_validation(tmp_path):
    result, record = run_check(tmp_path, owned_repo(tmp_path), 'from pathlib import Path; Path("source.txt").write_text("after")')
    assert result.returncode != 0
    assert record['command_exit_code'] == 0 and record['validation_exit_code'] != 0
    assert record['source_unchanged'] is False


def test_original_command_failure_is_retained(tmp_path):
    result, record = run_check(tmp_path, owned_repo(tmp_path), 'import sys; print("original failure"); sys.exit(7)')
    assert result.returncode == 7
    assert record['command_exit_code'] == record['validation_exit_code'] == 7
    assert 'original failure' in Path(record['commands'][0]['log']).read_text()


def test_wrong_runtime_fails_before_command(tmp_path):
    root = owned_repo(tmp_path)
    runtime = tmp_path / 'python39'
    runtime.write_text('#!/bin/sh\nprintf "[3, 9, 0]\\n"\n')
    runtime.chmod(0o700)
    marker = tmp_path / 'executed'
    result, record = run_check(tmp_path, root, 'from pathlib import Path; Path(' + repr(str(marker)) + ').write_text("executed")', runtime)
    assert result.returncode != 0 and record['validation_exit_code'] != 0
    assert record['command_exit_code'] is None and record['commands'] == []
    assert not marker.exists()
    assert '3.11' in record['reason']


def test_index_status_change_is_source_drift_even_when_bytes_are_unchanged(tmp_path):
    root = owned_repo(tmp_path)
    (root / 'source.txt').write_text('existing dirty input')
    result, record = run_check(tmp_path, root, 'import subprocess; subprocess.run(["git", "add", "source.txt"], check=True)')
    assert result.returncode != 0 and record['command_exit_code'] == 0
    assert record['source_unchanged'] is False


def test_terminated_command_retains_actual_negative_exit(tmp_path):
    result, record = run_check(tmp_path, owned_repo(tmp_path), 'import os,signal; os.kill(os.getpid(), signal.SIGTERM)')
    assert result.returncode != 0 and record['validation_exit_code'] != 0
    assert record['command_exit_code'] == -15


def test_wrapper_interruption_has_no_pass_without_terminal(tmp_path):
    import os
    import signal
    import time
    root = owned_repo(tmp_path)
    artifacts = tmp_path / 'artifacts'
    marker = tmp_path / 'started'
    code = 'from pathlib import Path; import time; Path(' + repr(str(marker)) + ').write_text("started"); time.sleep(10)'
    process = subprocess.Popen([sys.executable, str(CHECKS), '--root', str(root), '--artifacts', str(artifacts), '--', sys.executable, '-c', code], stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    deadline = time.monotonic() + 3
    while not marker.exists() and time.monotonic() < deadline:
        time.sleep(.01)
    assert marker.exists()
    os.kill(process.pid, signal.SIGINT)
    process.communicate(timeout=4)
    record = json.loads((artifacts / 'result.json').read_text())
    assert process.returncode != 0 and record['validation_exit_code'] != 0
    assert record['command_exit_code'] is None
    assert record['commands'][0]['terminal'] is False
