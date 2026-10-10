"""The original SDK receives repository work through the registered platform."""
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import json

import pytest
from sdk_source_integrity import source_snapshot


ROOT = Path(__file__).resolve().parents[1]


@pytest.mark.parametrize('scenario', ['existing', 'create_unknown', 'private_issue', 'concurrent', 'delegation', 'authority',
    'wrong_card_assignee', 'wrong_card_path', 'wrong_card_body', 'wrong_card_duplicate', 'changed_card',
    'issue_unavailable', 'issue_unavailable_unload', 'private_all_bindings',
    'configuration_auto_decompose', 'configuration_repository', 'queued_rejection_unload', 'responsibility_conflict', 'worker_required', 'worker_dispatch', 'worker_query_race', 'worker_transport_timeout'])
def test_simple_repository_intake_uses_original_sdk_public_entries(scenario):
    flags = ('HPM_NATIVE_REAL_MODEL_REFERENCE', 'HPM_NATIVE_EXECUTION_REFERENCE')
    if scenario in {'worker_dispatch', 'worker_query_race', 'worker_transport_timeout'}:
        assert bool(os.environ.get(flags[0])) == bool(os.environ.get(flags[1])), 'Real model acceptance requires both approved references.'
    configured = os.environ.get('HERMES_TEST_SDK_ROOT')
    if not configured:
        if os.environ.get('HERMES_REQUIRE_SDK_SMOKE') == '1':
            pytest.fail('Original Hermes SDK is required.')
        pytest.skip('Set HERMES_TEST_SDK_ROOT; a skip does not establish native intake.')
    sdk = Path(configured).resolve(strict=True)
    before = source_snapshot(sdk)
    dsh_before = source_snapshot(os.environ['DSH_TEST_SDK_ROOT']) if scenario in {'worker_dispatch', 'worker_query_race', 'worker_transport_timeout'} else None
    scratch = Path(tempfile.mkdtemp(prefix='hpm-simple-', dir='/tmp')).resolve()
    cleaned = False
    passed = False
    try:
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
        if scenario in {'worker_dispatch', 'worker_query_race', 'worker_transport_timeout'}:
            env['DSH_TEST_SDK_ROOT'] = os.environ['DSH_TEST_SDK_ROOT']
            for name in ('HPM_NATIVE_REAL_MODEL_REFERENCE', 'HPM_NATIVE_EXECUTION_REFERENCE'):
                if name in os.environ:
                    env[name] = os.environ[name]
            if os.environ.get('HPM_SYNTHETIC_WORKER_REFUSE'):
                env['HPM_SYNTHETIC_WORKER_REFUSE'] = os.environ['HPM_SYNTHETIC_WORKER_REFUSE']
        process = subprocess.Popen([sys.executable, str(ROOT / 'tests/simple_intake_smoke_runner.py'), str(scratch), scenario],
            cwd=scratch, env=env, text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        try:
            stdout, stderr = process.communicate(timeout=210 if os.environ.get('HPM_NATIVE_REAL_MODEL_REFERENCE') else 100)
        except subprocess.TimeoutExpired:
            process.terminate()
            try:
                process.communicate(timeout=20)
            except subprocess.TimeoutExpired:
                process.kill()
                process.communicate()
            pytest.fail('Fixture timed out; external execution cleanup is unknown. Private runtime retained: ' + str(scratch))
        result = subprocess.CompletedProcess(process.args, process.returncode, stdout, stderr)
        if os.environ.get('HPM_NATIVE_REAL_MODEL_REFERENCE'):
            model = json.loads(Path(os.environ['HPM_NATIVE_REAL_MODEL_REFERENCE']).read_text())
            key = next(line.split('=', 1)[1].strip().strip('\"\'') for line in Path(model['env_file']).read_text().splitlines()
                       if line.startswith(model['api_key_env'] + '='))
            assert key not in stdout + stderr, 'Credential appeared in captured fixture output.'
        outcome = scratch / 'cleanup-evidence.json'
        if outcome.exists():
            cleanup = json.loads(outcome.read_text())
            cleaned = not cleanup['cleanup_errors']
            assert cleanup['logging_flushed_after_stop'] and cleanup['credential_diagnostic_hits'] == 0
        assert source_snapshot(sdk) == before, 'Plugin intake cannot mutate original SDK source.'
        if dsh_before is not None:
            assert source_snapshot(os.environ['DSH_TEST_SDK_ROOT']) == dsh_before, 'Native worker cannot mutate original DSH SDK source.'
        if scenario in {'worker_dispatch', 'worker_query_race', 'worker_transport_timeout'} and os.environ.get('HPM_NATIVE_EVIDENCE_DIR'):
            destination = Path(os.environ['HPM_NATIVE_EVIDENCE_DIR']).resolve(strict=True)
            import stat
            assert destination.is_dir() and destination.stat().st_uid == os.getuid() and stat.S_IMODE(destination.stat().st_mode) & 0o077 == 0
            for name in ('worker-validation-evidence.json', 'worker-failure-evidence.json', 'cleanup-evidence.json'):
                if not (scratch / name).exists():
                    continue
                payload = (scratch / name).read_bytes()
                if os.environ.get('HPM_NATIVE_REAL_MODEL_REFERENCE'):
                    assert key.encode() not in payload, 'Credential appeared in evidence.'
                descriptor = os.open(destination / name, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
                with os.fdopen(descriptor, 'wb') as target:
                    target.write(payload)
        assert result.returncode == 0, result.stdout + result.stderr + '\nPrivate runtime retained: ' + str(scratch)
        assert cleaned, 'Cleanup remains unknown; private runtime retained: ' + str(scratch)
        assert 'synthetic-unused-secret' not in result.stdout + result.stderr
        assert (scratch / 'simple-smoke-result').read_text() == 'passed'
        passed = True
    finally:
        if cleaned and passed:
            shutil.rmtree(scratch)


def test_failed_public_worker_preserves_forensics(monkeypatch):
    if not os.environ.get('HERMES_TEST_SDK_ROOT'):
        pytest.skip('Original SDK is required.')
    monkeypatch.delenv('HPM_NATIVE_REAL_MODEL_REFERENCE', raising=False)
    monkeypatch.delenv('HPM_NATIVE_EXECUTION_REFERENCE', raising=False)
    monkeypatch.setenv('HPM_SYNTHETIC_WORKER_REFUSE', '1')
    before = set(Path('/private/tmp').glob('hpm-simple-*'))
    evidence = Path(tempfile.mkdtemp(prefix='hpm-failure-evidence-', dir='/tmp')).resolve()
    monkeypatch.setenv('HPM_NATIVE_EVIDENCE_DIR', str(evidence))
    retained = None
    try:
        with pytest.raises(AssertionError, match='awaiting_acceptance'):
            test_simple_repository_intake_uses_original_sdk_public_entries('worker_dispatch')
        created = set(Path('/private/tmp').glob('hpm-simple-*')) - before
        assert len(created) == 1, 'Business failure must retain its owner-only original diagnostic runtime.'
        retained = created.pop()
        assert list((retained / 'home/kanban/logs').glob('*.log'))
        assert (retained / 'home/state.db').exists()
        cleanup = json.loads((retained / 'cleanup-evidence.json').read_text())
        assert cleanup['business_failed'] is True and not cleanup['cleanup_errors']
        assert (evidence / 'cleanup-evidence.json').exists()
        diagnostic = json.loads((evidence / 'worker-failure-evidence.json').read_text())
        assert diagnostic['business_failed'] is True
        assert diagnostic['original_workers'][0]['original_log_sha256']
        sessions = diagnostic['original_workers'][0]['sessions']
        assert sessions and sessions[0]['actual_route']['api_call_count'] > 0
        assert sessions[0]['tool_calls'] == [], 'The external refusal never invokes DSH.'
        assert not list(evidence.rglob('.env'))
    finally:
        if retained is not None and not json.loads((retained / 'cleanup-evidence.json').read_text())['cleanup_errors']:
            shutil.rmtree(retained)
        shutil.rmtree(evidence)
