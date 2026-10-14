"""A verified owned-platform connection can bootstrap its native authority."""
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile

import pytest

from sdk_source_integrity import source_snapshot

ROOT = Path(__file__).resolve().parents[1]


@pytest.mark.parametrize('scenario', ['verified', 'missing_credential', 'wrong_profile',
    'wrong_bot', 'wrong_home', 'wrong_secret_scope', 'unload_during_verification',
    'disconnect_during_verification'])
def test_native_owned_connection_bootstraps_only_its_verified_authority(scenario):
    selected = os.environ.get('HERMES_TEST_SDK_ROOT')
    if not selected:
        if os.environ.get('HERMES_REQUIRE_SDK_SMOKE') == '1':
            pytest.fail('Bootstrap integration requires the explicitly selected unmodified SDK.')
        pytest.skip('No native SDK selected; bootstrap acceptance remains unverified.')
    sdk = Path(selected).resolve(strict=True)
    assert not (sdk / 'hermes_native_log_privacy.py').exists()
    before = source_snapshot(sdk)
    with tempfile.TemporaryDirectory(prefix='hpm-bootstrap-', dir='/tmp') as directory:
        scratch = Path(directory).resolve()
        home = scratch / 'home'
        plugin = home / 'plugins' / 'ghost-hermes-pm'
        plugin.mkdir(parents=True)
        home.chmod(0o700)
        for name in ('__init__.py', 'plugin.yaml', 'ghost_hermes_pm', 'dashboard'):
            source = ROOT / name
            if source.is_dir():
                shutil.copytree(source, plugin / name, ignore=shutil.ignore_patterns('__pycache__'))
            else:
                shutil.copy2(source, plugin / name)
        env = {'PATH': os.environ.get('PATH', '/usr/bin:/bin'), 'HERMES_HOME': str(home),
            'HERMES_PROFILE': 'default', 'HERMES_BUNDLED_PLUGINS': str(home / 'empty-bundled'),
            'HERMES_SKIP_PM_BOOTSTRAP': '1', 'HERMES_DISABLE_PROJECT_PLUGINS': '1',
            'PYTHONDONTWRITEBYTECODE': '1', 'PYTHONPATH': str(sdk),
            'HERMES_FIXTURE_OWNER_TOKEN': 'synthetic-bootstrap-owner-token'}
        result = subprocess.run([sys.executable, str(ROOT / 'tests' / 'authority_bootstrap_smoke_runner.py'),
            str(scratch), scenario], cwd=scratch, env=env, capture_output=True, text=True, timeout=45)
        assert source_snapshot(sdk) == before, 'Bootstrap, unload and reload must preserve every SDK source byte.'
        assert result.returncode == 0, result.stdout + result.stderr
        assert result.stdout == ''
        assert 'synthetic-bootstrap-owner-token' not in result.stderr
