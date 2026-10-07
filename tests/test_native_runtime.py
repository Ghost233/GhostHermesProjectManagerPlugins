"""Explicit native SDK smoke, using pristine staged SDK sources and an artificial home."""
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import pytest

ROOT = Path(__file__).resolve().parents[1]


@pytest.mark.parametrize('runner,artifact_mismatch,unload_stage', [('collaboration_smoke_runner.py', False, ''), ('archive_smoke_runner.py', False, ''), ('knowledge_smoke_runner.py', False, ''), ('manual_observation_smoke_runner.py', False, ''), ('repository_queue_smoke_runner.py', False, ''), ('questions_smoke_runner.py', False, ''), ('task_control_smoke_runner.py', False, ''), ('native_smoke_runner.py', False, ''), ('owned_feishu_smoke_runner.py', False, ''), ('owned_feishu_smoke_runner.py', True, ''), ('owned_feishu_smoke_runner.py', False, 'verify'), ('owned_feishu_smoke_runner.py', False, 'issue'), ('owned_feishu_smoke_runner.py', False, 'issue_queue'), ('owned_feishu_smoke_runner.py', False, 'send'), ('owned_feishu_smoke_runner.py', False, 'connected'), ('owned_feishu_smoke_runner.py', False, 'failure_replay'), ('owned_feishu_smoke_runner.py', False, 'failure_optional')])
def test_native_sdk_loads_user_plugin_and_dashboard_backend_and_releases_resources(runner, artifact_mismatch, unload_stage):
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
        if runner in {'archive_smoke_runner.py', 'task_control_smoke_runner.py', 'repository_queue_smoke_runner.py', 'manual_observation_smoke_runner.py', 'questions_smoke_runner.py', 'knowledge_smoke_runner.py'}:
            fixtures = scratch / ('manual-fixtures' if runner == 'manual_observation_smoke_runner.py' else 'queue-fixtures' if runner == 'repository_queue_smoke_runner.py' else 'control-fixtures')
            fixtures.mkdir()
            for name in ('test_task_control.py', 'test_task_execution.py', 'test_directory.py', 'test_requests.py', 'codex_fixture_server.py', 'test_repository_queue.py', 'queue_fixture_server.py', 'test_manual_observation.py', 'manual_fixture_server.py', 'test_questions.py', 'questions_fixture_server.py', 'test_knowledge.py', 'test_feishu_entry.py', 'test_archives.py'):
                shutil.copy2(ROOT / 'tests' / name, fixtures / name)
        if runner == 'collaboration_smoke_runner.py':
            fixtures = scratch / 'collaboration-fixtures'
            fixtures.mkdir()
            for name in ('test_collaboration.py', 'test_directory.py', 'test_requests.py', 'test_task_execution.py', 'test_task_control.py', 'codex_fixture_server.py'):
                shutil.copy2(ROOT / 'tests' / name, fixtures / name)
        env = {'PATH': os.environ.get('PATH', '/usr/bin:/bin'), 'HERMES_HOME': str(home),
               'HERMES_BUNDLED_PLUGINS': str(home / 'empty-bundled'), 'PYTHONDONTWRITEBYTECODE': '1',
               'PYTHONPATH': str(staged), 'HERMES_FIXTURE_OWNER_TOKEN': 'synthetic-owner-credential',
               'HERMES_FIXTURE_PARTICIPANT_TOKEN': 'synthetic-participant-credential'}
        if artifact_mismatch:
            env['HERMES_TEST_OWNED_ARTIFACT_MISMATCH'] = '1'
        if unload_stage:
            env['HERMES_TEST_OWNED_UNLOAD_STAGE'] = unload_stage
        result = subprocess.run([sys.executable, str(ROOT / 'tests' / runner), str(scratch)],
                                cwd=scratch, env=env, text=True, capture_output=True, timeout=60)
        assert result.returncode == 0, result.stdout + result.stderr
        assert 'native load, Dashboard bridge, restart, teardown: OK' in result.stdout
        if runner == 'archive_smoke_runner.py':
            print(result.stdout)
