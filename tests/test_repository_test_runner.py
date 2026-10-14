"""Actual pytest reports, rather than text resembling a successful summary."""
import json
from pathlib import Path
import subprocess
import sys
import shutil

import pytest


RUNNER = Path(__file__).resolve().parents[1] / 'ghost_hermes_pm/repository_test_runner.py'


def test_runner_counts_real_test_reports_and_excludes_skipped_tests(tmp_path):
    repository = tmp_path / 'repository'
    artifacts = tmp_path / 'artifacts'
    repository.mkdir()
    artifacts.mkdir(mode=0o700)
    subprocess.run(['git', 'init', '-q', str(repository)], check=True)
    (repository / 'test_feature.py').write_text(
        'import pytest\n'
        'def test_feature():\n'
        '    print("900 passed")\n'
        '    assert 6 * 7 == 42\n'
        '@pytest.mark.skip(reason="Synthetic unavailable dependency")\n'
        'def test_unavailable():\n'
        '    assert False\n')
    result = subprocess.run([sys.executable, str(RUNNER), str(repository), str(artifacts), 'test_feature.py'],
                            capture_output=True, text=True, cwd=repository)
    assert result.returncode == 0, result.stderr
    receipt = json.loads(result.stdout)
    assert receipt['executed_tests'] == 1
    assert receipt['skipped_tests'] == 1
    assert receipt['exit_code'] == 0
    assert receipt['before_source_digest'] == receipt['after_source_digest']


def test_node_runner_counts_native_events_and_ignores_printed_success(tmp_path):
    repository = tmp_path / 'repository'
    artifacts = tmp_path / 'artifacts'
    repository.mkdir()
    artifacts.mkdir(mode=0o700)
    subprocess.run(['git', 'init', '-q', str(repository)], check=True)
    (repository / 'feature.test.mjs').write_text("import test from 'node:test'; import assert from 'node:assert/strict';\n"
        "test('real assertion',()=>{console.log('900 passed'); assert.equal(6*7,42)});\n"
        "test.skip('unavailable',()=>{throw Error('skip')});\n")
    result = subprocess.run([sys.executable, '-I', str(RUNNER), str(repository), str(artifacts),
        '--node', str(Path(shutil.which('node')).resolve()), '--binding', 'fixture-session', 'fixture-generation',
        'fixture-nonce', '--', 'feature.test.mjs'], cwd=repository, capture_output=True, text=True)
    assert result.returncode == 0, result.stderr
    receipt = json.loads(result.stdout)
    assert receipt['executed_tests'] == 1 and receipt['skipped_tests'] == 1 and receipt['failed_tests'] == 0
    assert receipt['runner'] == 'node:test' and receipt['binding']['nonce'] == 'fixture-nonce'
    assert receipt['before_source_digest'] == receipt['after_source_digest']
    assert receipt['source_access'] == receipt['git_access'] == 'workspace-write'


def test_bound_python_runner_counts_unittest_results_and_skips(tmp_path):
    repository = tmp_path / 'repository'
    artifacts = tmp_path / 'artifacts'
    repository.mkdir()
    artifacts.mkdir(mode=0o700)
    subprocess.run(['git', 'init', '-q', str(repository)], check=True)
    (repository / 'test_feature.py').write_text(
        'import unittest\n'
        'class Feature(unittest.TestCase):\n'
        '    def test_feature(self):\n'
        '        print("900 passed")\n'
        '        self.assertEqual(6 * 7, 42)\n'
        '    @unittest.skip("Synthetic unavailable dependency")\n'
        '    def test_unavailable(self):\n'
        '        self.fail("Skipped")\n')
    result = subprocess.run([sys.executable, '-I', str(RUNNER), str(repository), str(artifacts),
        '--node', str(Path(shutil.which('node')).resolve()), '--binding', 'fixture-session', 'fixture-generation',
        'fixture-nonce', '--', 'test_feature.py'], cwd=repository, capture_output=True, text=True)
    assert result.returncode == 0, result.stderr
    receipt = json.loads(result.stdout)
    assert receipt['executed_tests'] == 1 and receipt['skipped_tests'] == 1 and receipt['failed_tests'] == 0
    assert receipt['runner'] == 'unittest' and receipt['binding'] == {
        'session_id': 'fixture-session', 'generation': 'fixture-generation', 'nonce': 'fixture-nonce'}
    assert receipt['test_files'] == ['test_feature.py']
    assert receipt['before_source_digest'] == receipt['after_source_digest']


@pytest.mark.parametrize('code, exit_code, executed, skipped, failed', [
    ('print("900 passed")\n', 0, 0, 0, 0),
    ('import unittest\nclass Feature(unittest.TestCase):\n    def test_failure(self):\n        self.fail("Actual failure")\n', 1, 0, 0, 1),
    ('import unittest\nclass Feature(unittest.TestCase):\n    def test_error(self):\n        raise RuntimeError("Actual error")\n', 1, 0, 0, 1),
    ('import unittest\n@unittest.skip("Unavailable")\nclass Feature(unittest.TestCase):\n    def test_skipped(self):\n        self.fail("Skipped")\n', 0, 0, 1, 0),
])
def test_bound_python_runner_preserves_native_failure_skip_and_zero_test_results(tmp_path, code, exit_code, executed, skipped, failed):
    repository, artifacts = tmp_path / 'repository', tmp_path / 'artifacts'
    repository.mkdir()
    artifacts.mkdir(mode=0o700)
    subprocess.run(['git', 'init', '-q', str(repository)], check=True)
    (repository / 'test_feature.py').write_text(code)
    result = subprocess.run([sys.executable, '-I', str(RUNNER), str(repository), str(artifacts),
        '--node', str(Path(shutil.which('node')).resolve()), '--binding', 'fixture-session', 'fixture-generation',
        'fixture-nonce', '--', 'test_feature.py'], cwd=repository, capture_output=True, text=True)
    assert result.returncode == exit_code, result.stderr
    receipt = json.loads(result.stdout)
    assert (receipt['executed_tests'], receipt['skipped_tests'], receipt['failed_tests']) == (executed, skipped, failed)
    assert receipt['runner'] == 'unittest' and receipt['exit_code'] == exit_code
    assert receipt['before_source_digest'] == receipt['after_source_digest']


def test_bound_runner_rejects_mixed_python_and_node_files(tmp_path):
    repository, artifacts = tmp_path / 'repository', tmp_path / 'artifacts'
    repository.mkdir()
    artifacts.mkdir(mode=0o700)
    subprocess.run(['git', 'init', '-q', str(repository)], check=True)
    (repository / 'test_feature.py').write_text('print("900 passed")\n')
    (repository / 'feature.test.mjs').write_text("console.log('900 passed');\n")
    result = subprocess.run([sys.executable, '-I', str(RUNNER), str(repository), str(artifacts),
        '--node', str(Path(shutil.which('node')).resolve()), '--binding', 'fixture-session', 'fixture-generation',
        'fixture-nonce', '--', 'test_feature.py', 'feature.test.mjs'], cwd=repository, capture_output=True, text=True)
    assert result.returncode != 0 and result.stdout == ''
    assert 'separate bound calls' in result.stderr
