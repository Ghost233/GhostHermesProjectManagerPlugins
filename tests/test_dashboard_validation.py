"""Actual registered dashboard component, with synthetic external SDK/React hooks."""
from pathlib import Path
import shutil
import subprocess
import pytest


@pytest.mark.parametrize('case', ['outcome_unknown', 'repository_busy', 'evidence', 'notifications'])
def test_global_validation_operation_keeps_error_after_refresh_and_displays_complete_public_evidence(case):
    node = shutil.which('node')
    assert node is not None, 'Dashboard behavior verification requires the existing Node runtime.'
    root = Path(__file__).resolve().parents[1]
    result = subprocess.run([node, str(Path(__file__).with_name('dashboard_validation_runner.cjs')),
        str(root / 'dashboard' / 'dist' / 'index.js'), case], capture_output=True, text=True, timeout=10)
    assert result.returncode == 0, result.stdout + result.stderr
    assert 'public dashboard validation controls: OK' in result.stdout
