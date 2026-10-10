"""The original SDK receives repository work through the registered platform."""
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile

import pytest
from sdk_source_integrity import source_snapshot


ROOT = Path(__file__).resolve().parents[1]


@pytest.mark.parametrize('scenario', ['existing', 'create_unknown', 'private_issue', 'concurrent', 'delegation', 'authority',
    'wrong_card_assignee', 'wrong_card_path', 'wrong_card_body', 'wrong_card_duplicate', 'changed_card',
    'issue_unavailable', 'issue_unavailable_unload'])
def test_simple_repository_intake_uses_original_sdk_public_entries(scenario):
    configured = os.environ.get('HERMES_TEST_SDK_ROOT')
    if not configured:
        if os.environ.get('HERMES_REQUIRE_SDK_SMOKE') == '1':
            pytest.fail('Original Hermes SDK is required.')
        pytest.skip('Set HERMES_TEST_SDK_ROOT; a skip does not establish native intake.')
    sdk = Path(configured).resolve(strict=True)
    before = source_snapshot(sdk)
    with tempfile.TemporaryDirectory(prefix='hpm-simple-', dir='/tmp') as temporary:
        scratch = Path(temporary).resolve()
        home = scratch / 'home'
        plugin = home / 'plugins' / 'ghost-hermes-pm'
        plugin.mkdir(parents=True)
        home.chmod(0o700)
        for name in ('__init__.py', 'plugin.yaml', 'ghost_hermes_pm'):
            source = ROOT / name
            if source.is_dir():
                shutil.copytree(source, plugin / name, ignore=shutil.ignore_patterns('__pycache__'))
            else:
                shutil.copy2(source, plugin / name)
        env = {'PATH': os.environ.get('PATH', '/usr/bin:/bin'), 'HERMES_HOME': str(home),
               'HERMES_BUNDLED_PLUGINS': str(home / 'empty-bundled'),
               'HERMES_KANBAN_HOME': str(home), 'PYTHONDONTWRITEBYTECODE': '1',
               'PYTHONPATH': str(sdk), 'HERMES_TEST_SDK_ROOT': str(sdk)}
        result = subprocess.run([sys.executable, str(ROOT / 'tests/simple_intake_smoke_runner.py'),
                                 str(scratch), scenario], cwd=scratch, env=env,
                                text=True, capture_output=True, timeout=75)
        assert source_snapshot(sdk) == before, 'Plugin intake cannot mutate original SDK source.'
        assert result.returncode == 0, result.stdout + result.stderr
        assert 'synthetic-unused-secret' not in result.stdout + result.stderr
        assert (scratch / 'simple-smoke-result').read_text() == 'passed'
