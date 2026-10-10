"""Actual pytest reports, rather than text resembling a successful summary."""
import json
from pathlib import Path
import subprocess
import sys


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
