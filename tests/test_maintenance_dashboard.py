"""Public maintenance dashboard forms with controlled external SDK responses."""
from pathlib import Path
import shutil
import subprocess
import pytest


@pytest.mark.parametrize('case', ['reader', 'permission_absent', 'permission_unknown', 'stale_permission', 'evidence', 'enter', 'unknown', 'stale_version', 'stale_scope', 'stale_release', 'stale_offline', 'action_check', 'action_checkpoint', 'action_switch', 'action_rollback', 'action_deactivate', 'action_reenable'])
def test_dashboard_maintenance_shows_actual_evidence_unknown_release_and_manual_handoff(case):
    node = shutil.which('node')
    assert node is not None
    root = Path(__file__).resolve().parents[1]
    result = subprocess.run([node, str(Path(__file__).with_name('dashboard_maintenance_runner.cjs')),
        str(root / 'dashboard' / 'dist' / 'index.js'), case], capture_output=True, text=True, timeout=10)
    assert result.returncode == 0, result.stdout + result.stderr
    assert 'public dashboard maintenance controls: OK' in result.stdout
