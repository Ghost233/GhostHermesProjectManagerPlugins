"""Explicit native SDK smoke, using pristine staged SDK sources and an artificial home."""
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import pytest

ROOT = Path(__file__).resolve().parents[1]


@pytest.mark.parametrize('runner,artifact_mismatch', [('native_smoke_runner.py', False), ('owned_feishu_smoke_runner.py', False), ('owned_feishu_smoke_runner.py', True)])
def test_native_sdk_loads_user_plugin_and_dashboard_backend_and_releases_resources(runner, artifact_mismatch):
    configured = os.environ.get('HERMES_TEST_SDK_ROOT')
    sdk = Path(configured) if configured else ROOT / 'tests' / 'fixtures' / 'hermes-sdk'
    if not (sdk / 'hermes_cli' / 'plugins.py').exists():
        if configured or os.environ.get('HERMES_REQUIRE_SDK_SMOKE') == '1':
            pytest.fail('Native smoke requires an explicit pristine HERMES_TEST_SDK_ROOT.')
        pytest.skip('Native SDK fixture absent; set HERMES_TEST_SDK_ROOT. This skip does not verify native loading.')
    with tempfile.TemporaryDirectory(prefix='hpm-sdk-', dir='/tmp') as temporary:
        scratch = Path(temporary).resolve()
        staged = scratch / 'sdk'
        shutil.copytree(sdk, staged, ignore=shutil.ignore_patterns('.git', '.env', '.env.*', '.hermes', '.venv', '__pycache__', 'node_modules'))
        home = scratch / 'home'
        plugin = home / 'plugins' / 'ghost-hermes-pm'
        plugin.mkdir(parents=True)
        for name in ('__init__.py', 'plugin.yaml', 'ghost_hermes_pm', 'dashboard'):
            source = ROOT / name
            if source.is_dir():
                shutil.copytree(source, plugin / name, ignore=shutil.ignore_patterns('__pycache__'))
            else:
                shutil.copy2(source, plugin / name)
        env = {'PATH': os.environ.get('PATH', '/usr/bin:/bin'), 'HERMES_HOME': str(home),
               'HERMES_BUNDLED_PLUGINS': str(home / 'empty-bundled'), 'PYTHONDONTWRITEBYTECODE': '1',
               'PYTHONPATH': str(staged), 'HERMES_FIXTURE_OWNER_TOKEN': 'synthetic-owner-credential',
               'HERMES_FIXTURE_PARTICIPANT_TOKEN': 'synthetic-participant-credential'}
        if artifact_mismatch:
            env['HERMES_TEST_OWNED_ARTIFACT_MISMATCH'] = '1'
        result = subprocess.run([sys.executable, str(ROOT / 'tests' / runner), str(scratch)],
                                cwd=scratch, env=env, text=True, capture_output=True, timeout=60)
        assert result.returncode == 0, result.stdout + result.stderr
        assert 'native load, Dashboard bridge, restart, teardown: OK' in result.stdout
