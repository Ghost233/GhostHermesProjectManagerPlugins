"""The public Hermes Doctor seam loads this plugin without changing the SDK."""
import json
import os
from pathlib import Path
import subprocess
import sys

import pytest

from ghost_hermes_pm.manager import ManagementError
from ghost_hermes_pm.native_maintenance import NativeMaintenanceHost
from sdk_source_integrity import source_snapshot


ROOT = Path(__file__).resolve().parents[1]


def test_unmodified_sdk_doctor_registers_plugin_from_its_temporary_copy(tmp_path):
    configured = os.environ.get('HERMES_TEST_SDK_ROOT')
    if not configured:
        if os.environ.get('HERMES_REQUIRE_SDK_SMOKE') == '1':
            pytest.fail('Plugin Doctor requires an explicit fixed HERMES_TEST_SDK_ROOT.')
        pytest.skip('Set HERMES_TEST_SDK_ROOT to verify the real Plugin Doctor seam.')
    sdk = Path(configured).resolve(strict=True)
    before = source_snapshot(sdk)
    script = '''
import json
import sys
from hermes_cli.plugin_dev import doctor_plugin

report = doctor_plugin(sys.argv[1])
print(json.dumps({
    "findings": [[finding.level, finding.message] for finding in report.findings],
    "tools": list(report.registered_tools),
    "hooks": list(report.registered_hooks),
}))
'''
    env = {
        'HOME': str(tmp_path),
        'HERMES_HOME': str(tmp_path / 'home'),
        'HERMES_SKIP_PM_BOOTSTRAP': '1',
        'PATH': os.environ.get('PATH', '/usr/bin:/bin'),
        'PYTHONDONTWRITEBYTECODE': '1',
        'PYTHONPATH': str(sdk),
    }
    result = subprocess.run(
        [sys.executable, '-c', script, str(ROOT)],
        cwd=tmp_path,
        env=env,
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert source_snapshot(sdk) == before
    assert result.returncode == 0, result.stdout + result.stderr
    report = json.loads(result.stdout)
    assert all(
        'Maintenance paths must be fixed canonical ordinary files/directories' not in message
        for _level, message in report['findings']
    )
    assert {
        'hermes_pm_loaded_version',
        'hermes_pm_migration',
        'hermes_pm_snapshot',
    } <= set(report['tools'])
    assert report['hooks'] == ['pre_api_request', 'pre_gateway_dispatch']


def test_configured_maintenance_paths_still_reject_aliases(tmp_path):
    host = tmp_path / 'host'
    host.mkdir()
    alias = tmp_path / 'host-alias'
    alias.symlink_to(host, target_is_directory=True)

    with pytest.raises(ManagementError, match='fixed canonical ordinary files/directories'):
        NativeMaintenanceHost(alias, host, host, {}, [], lambda _binding: {})
