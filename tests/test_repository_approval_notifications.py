"""Approval notification mapping through the original registered platform and worker."""
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile

import pytest
from sdk_source_integrity import source_snapshot


@pytest.mark.parametrize('outcome', ['allowed-once', 'rejected'])
@pytest.mark.parametrize('reply_mode', ['native-ui', 'lark', 'lark-id'])
def test_headless_original_approval_accepts_only_the_bound_owner_decision(outcome, reply_mode):
    sdk = os.environ.get('HERMES_TEST_SDK_ROOT')
    dsh = os.environ.get('DSH_TEST_SDK_ROOT')
    if not sdk or not dsh:
        pytest.skip('Original Hermes and DSH SDKs are required.')
    before = source_snapshot(sdk), source_snapshot(dsh)
    root = Path(__file__).resolve().parents[1]
    scratch = Path(tempfile.mkdtemp(prefix='hpm-approval-public-', dir='/tmp')).resolve()
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
        environment = {**os.environ, 'HERMES_HOME': str(home), 'HERMES_KANBAN_HOME': str(home),
            'HERMES_BUNDLED_PLUGINS': str(home / 'empty-bundled'), 'PYTHONPATH': str(sdk),
            'PYTHONDONTWRITEBYTECODE': '1'}
        result = subprocess.run([sys.executable, str(root / 'tests/repository_approval_notifications_runner.py'),
            str(scratch), outcome, reply_mode], env=environment, cwd=scratch, capture_output=True, text=True, timeout=100)
        assert result.returncode == 0, result.stdout + result.stderr + '\nRetained fixture: ' + str(scratch)
        assert (scratch / 'approval-public-passed').read_text() == 'passed'
        assert source_snapshot(sdk) == before[0] and source_snapshot(dsh) == before[1]
    finally:
        if (scratch / 'approval-public-passed').exists():
            shutil.rmtree(scratch)
