"""Native Hermes command confirmation through the registered owned platform."""
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile

import pytest

from sdk_source_integrity import source_snapshot


def test_native_command_confirmation_card_resolves_original_request():
    sdk = os.environ.get('HERMES_TEST_SDK_ROOT')
    if not sdk:
        pytest.skip('The unmodified Hermes SDK is required.')
    before = source_snapshot(sdk)
    root = Path(__file__).resolve().parents[1]
    scratch = Path(tempfile.mkdtemp(prefix='hc-slash-', dir='/tmp'))
    try:
        home = scratch / 'home'
        plugin = home / 'plugins/ghost-hermes-pm'
        plugin.mkdir(parents=True)
        home.chmod(0o700)
        for name in ('__init__.py', 'plugin.yaml', 'ghost_hermes_pm'):
            source = root / name
            if source.is_dir():
                shutil.copytree(source, plugin / name, ignore=shutil.ignore_patterns('__pycache__'))
            else:
                shutil.copy2(source, plugin / name)
        env = {**os.environ, 'HERMES_HOME': str(home), 'HERMES_KANBAN_HOME': str(home),
               'HERMES_BUNDLED_PLUGINS': str(home / 'empty-bundled'), 'PYTHONPATH': str(sdk),
               'PYTHONDONTWRITEBYTECODE': '1'}
        result = subprocess.run([sys.executable, str(root / 'tests/native_slash_cards_runner.py'),
                                 str(scratch)], env=env, cwd=scratch, capture_output=True,
                                text=True, timeout=60)
        assert result.returncode == 0, result.stdout + result.stderr + '\nFixture: ' + str(scratch)
        assert source_snapshot(sdk) == before
    finally:
        if (scratch / 'passed').exists():
            shutil.rmtree(scratch)
