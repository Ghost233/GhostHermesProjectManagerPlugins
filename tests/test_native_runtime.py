"""Explicit native SDK smoke, using pristine staged SDK sources and an artificial home."""
import os
import hashlib
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import pytest

ROOT = Path(__file__).resolve().parents[1]


@pytest.mark.parametrize('runner,artifact_mismatch,unload_stage', [('maintenance_smoke_runner.py', False, ''), ('wiki_mcp_smoke_runner.py', False, ''), ('migration_smoke_runner.py', False, ''), ('lifecycle_smoke_runner.py', False, ''), ('notifications_smoke_runner.py', False, ''), ('recovery_smoke_runner.py', False, ''), ('memory_smoke_runner.py', False, ''), ('collaboration_smoke_runner.py', False, ''), ('manual_control_smoke_runner.py', False, ''), ('archive_smoke_runner.py', False, ''), ('knowledge_smoke_runner.py', False, ''), ('manual_observation_smoke_runner.py', False, ''), ('repository_queue_smoke_runner.py', False, ''), ('questions_smoke_runner.py', False, ''), ('task_control_smoke_runner.py', False, ''), ('native_smoke_runner.py', False, ''), ('owned_feishu_smoke_runner.py', False, ''), ('owned_feishu_smoke_runner.py', True, ''), ('owned_feishu_smoke_runner.py', False, 'verify'), ('owned_feishu_smoke_runner.py', False, 'issue'), ('owned_feishu_smoke_runner.py', False, 'issue_queue'), ('owned_feishu_smoke_runner.py', False, 'send'), ('owned_feishu_smoke_runner.py', False, 'connected'), ('owned_feishu_smoke_runner.py', False, 'failure_replay'), ('owned_feishu_smoke_runner.py', False, 'failure_optional')])
def test_native_sdk_loads_user_plugin_and_dashboard_backend_and_releases_resources(runner, artifact_mismatch, unload_stage, migration_entity=''):
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
        readiness_fixtures = scratch / 'readiness-fixtures'
        readiness_fixtures.mkdir()
        shutil.copy2(ROOT / 'tests' / 'readiness_support.py', readiness_fixtures / 'readiness_support.py')
        if runner in {'wiki_mcp_smoke_runner.py', 'recovery_smoke_runner.py', 'archive_smoke_runner.py', 'task_control_smoke_runner.py', 'repository_queue_smoke_runner.py', 'manual_observation_smoke_runner.py', 'questions_smoke_runner.py', 'manual_control_smoke_runner.py', 'knowledge_smoke_runner.py', 'memory_smoke_runner.py'}:
            fixtures = scratch / ('takeover-fixtures' if runner == 'manual_control_smoke_runner.py' else 'manual-fixtures' if runner == 'manual_observation_smoke_runner.py' else 'queue-fixtures' if runner == 'repository_queue_smoke_runner.py' else 'control-fixtures')
            fixtures.mkdir()
            for name in ('test_task_control.py', 'test_task_execution.py', 'test_directory.py', 'test_requests.py', 'codex_fixture_server.py', 'test_repository_queue.py', 'queue_fixture_server.py', 'test_manual_observation.py', 'manual_fixture_server.py', 'test_questions.py', 'questions_fixture_server.py', 'test_manual_control.py', 'takeover_fixture_server.py', 'test_knowledge.py', 'test_wiki_mcp.py', 'test_feishu_entry.py', 'test_archives.py', 'recovery_service_support.py', 'recovery_service_fixture.py'):
                shutil.copy2(ROOT / 'tests' / name, fixtures / name)
        if runner == 'memory_smoke_runner.py':
            for name in ('test_project_memory.py', 'memory_fixture_server.py'):
                shutil.copy2(ROOT / 'tests' / name, fixtures / name)
        if runner == 'migration_smoke_runner.py':
            fixtures = scratch / 'migration-fixtures'
            fixtures.mkdir()
            shutil.copy2(ROOT / 'tests' / 'test_directory.py', fixtures / 'test_directory.py')
            if migration_entity == 'developer':
                for name in ('recovery_service_support.py', 'recovery_service_fixture.py', 'test_requests.py', 'test_task_execution.py'):
                    shutil.copy2(ROOT / 'tests' / name, fixtures / name)
        if runner == 'lifecycle_smoke_runner.py':
            fixtures = scratch / 'lifecycle-fixtures'
            fixtures.mkdir()
            for name in ('test_lifecycle.py', 'test_directory.py', 'test_requests.py', 'test_task_execution.py', 'test_task_control.py'):
                shutil.copy2(ROOT / 'tests' / name, fixtures / name)
        if runner in {'collaboration_smoke_runner.py', 'notifications_smoke_runner.py'}:
            fixtures = scratch / 'collaboration-fixtures'
            fixtures.mkdir()
            for name in ('test_collaboration.py', 'test_directory.py', 'test_requests.py', 'test_task_execution.py', 'test_task_control.py', 'codex_fixture_server.py', 'test_questions.py', 'questions_fixture_server.py'):
                shutil.copy2(ROOT / 'tests' / name, fixtures / name)
        for fixture_dir in scratch.glob('*-fixtures'):
            shutil.copy2(ROOT / 'tests' / 'readiness_support.py', fixture_dir / 'readiness_support.py')
        env = {'PATH': os.environ.get('PATH', '/usr/bin:/bin'), 'HERMES_HOME': str(home),
               'HERMES_BUNDLED_PLUGINS': str(home / 'empty-bundled'), 'PYTHONDONTWRITEBYTECODE': '1',
               'PYTHONPATH': str(staged), 'HERMES_FIXTURE_OWNER_TOKEN': 'synthetic-owner-credential',
               'HERMES_FIXTURE_PARTICIPANT_TOKEN': 'synthetic-participant-credential',
               'HERMES_FIXTURE_APP_SECRET': 'synthetic-unused-secret'}
        if runner == 'maintenance_smoke_runner.py':
            fixtures = scratch / 'maintenance-fixtures'
            fixtures.mkdir()
            shutil.copy2(ROOT / 'tests' / 'native_fixture_boundary.py', fixtures / 'native_fixture_boundary.py')
            # These Python bytes were independently matched to the prescribed
            # official tree. An archive fixture need not contain Git metadata.
            for name, expected in {
                'hermes_cli/plugins.py': '31c99f61f61732557bb84429d014e943d2d51a753b2541ab5de3b196a893bf9a',
                'gateway/control_socket.py': '5acbcf998b3ed5adf6e6e9c5c608d9cc91f5e9da58bc4c564ea613fb6b038f94',
                'gateway/run_plugin_rewire.py': 'b56ecc5118f7ff2666b56d928d6951b0efa58d1469b8b83ad91009892fddd16b'}.items():
                assert hashlib.sha256((sdk / name).read_bytes()).hexdigest() == expected
            env['HERMES_TEST_SDK_COMMIT'] = 'bd0affe5e5f723579df8902852f5d0c47795f355'
        if artifact_mismatch:
            env['HERMES_TEST_OWNED_ARTIFACT_MISMATCH'] = '1'
        if unload_stage:
            env['HERMES_TEST_OWNED_UNLOAD_STAGE'] = unload_stage
        runtime_python = os.environ.get('HERMES_TEST_SESSION_PYTHON', sys.executable) if runner in {'migration_smoke_runner.py', 'maintenance_smoke_runner.py'} else sys.executable
        result = subprocess.run([runtime_python, str(ROOT / 'tests' / runner), str(scratch), *([migration_entity] if migration_entity else [])],
                                cwd=scratch, env=env, text=True, capture_output=True, timeout=120 if runner == 'maintenance_smoke_runner.py' else 60)
        assert result.returncode == 0, result.stdout + result.stderr
        assert 'native load, Dashboard bridge, restart, teardown: OK' in result.stdout
        if runner in {'archive_smoke_runner.py', 'lifecycle_smoke_runner.py', 'maintenance_smoke_runner.py'}:
            print(result.stdout)


@pytest.mark.parametrize('entity', ['wiki', 'ghost', 'steward', 'developer'])
def test_native_named_global_migration_preserves_single_entry_scope_and_observed_execution(entity):
    test_native_sdk_loads_user_plugin_and_dashboard_backend_and_releases_resources('migration_smoke_runner.py', False, '', entity)
