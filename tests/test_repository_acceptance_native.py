"""Delivery must traverse the original registered platform, worker, Hook and DSH."""
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import stat

import pytest
from sdk_source_integrity import source_snapshot


ROOT = Path(__file__).resolve().parents[1]


@pytest.mark.parametrize('scenario', ['pending_review', 'fake_test_success', 'merge_required'])
def test_repository_acceptance_through_original_native_entries(scenario):
    configured = os.environ.get('HERMES_TEST_SDK_ROOT')
    if not configured or not os.environ.get('DSH_TEST_SDK_ROOT'):
        if os.environ.get('HERMES_REQUIRE_SDK_SMOKE') == '1' or os.environ.get('DSH_REQUIRE_SDK_SMOKE') == '1':
            pytest.fail('Both original SDKs are required for repository delivery.')
        pytest.skip('Original SDK references are required; a skip does not prove delivery.')
    sdk = Path(configured).resolve(strict=True)
    before = source_snapshot(sdk), source_snapshot(os.environ['DSH_TEST_SDK_ROOT'])
    source_parent = subprocess.run(['getconf', 'DARWIN_USER_TEMP_DIR'], capture_output=True, text=True, check=True).stdout.strip()
    scratch = Path(tempfile.mkdtemp(prefix='hpm-accept-', dir=source_parent)).resolve()
    cleanup = None
    passed = False
    try:
        home = scratch / 'home'
        plugin = home / 'plugins/ghost-hermes-pm'
        plugin.mkdir(parents=True)
        for name in ('__init__.py', 'plugin.yaml', 'ghost_hermes_pm'):
            source = ROOT / name
            if source.is_dir():
                shutil.copytree(source, plugin / name, ignore=shutil.ignore_patterns('__pycache__'))
            else:
                shutil.copy2(source, plugin / name)
        environment = {'PATH': os.environ['PATH'], 'PYTHONDONTWRITEBYTECODE': '1', 'PYTHONPATH': str(sdk),
            'HERMES_HOME': str(home), 'HERMES_KANBAN_HOME': str(home), 'HERMES_BUNDLED_PLUGINS': str(home / 'empty-bundled'),
            'HERMES_TEST_SDK_ROOT': str(sdk), 'DSH_TEST_SDK_ROOT': os.environ['DSH_TEST_SDK_ROOT']}
        process = subprocess.Popen([sys.executable, str(ROOT / 'tests/repository_acceptance_smoke_runner.py'), str(scratch), scenario],
            cwd=scratch, env=environment, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        try:
            stdout, stderr = process.communicate(timeout=100)
        except subprocess.TimeoutExpired:
            process.terminate()
            try:
                process.communicate(timeout=10)
            except subprocess.TimeoutExpired:
                process.kill()
                process.communicate()
            pytest.fail('Native acceptance timed out; cleanup is unknown. Private runtime retained: ' + str(scratch))
        evidence = scratch / 'acceptance-cleanup.json'
        if evidence.exists():
            cleanup = json.loads(evidence.read_text())
        evidence_directory = os.environ.get('HPM_ACCEPTANCE_EVIDENCE_DIR') or os.environ.get('HPM_NATIVE_EVIDENCE_DIR')
        if evidence_directory:
            destination = Path(evidence_directory).resolve(strict=True)
            assert destination.is_dir() and destination.stat().st_uid == os.getuid() and not stat.S_IMODE(destination.stat().st_mode) & 0o077
            for name in ('acceptance-evidence.json', 'acceptance-cleanup.json'):
                saved = scratch / name
                if saved.exists():
                    target = destination / (scenario + '-' + name)
                    descriptor = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
                    with os.fdopen(descriptor, 'wb') as stream:
                        stream.write(saved.read_bytes())
        assert before == (source_snapshot(sdk), source_snapshot(os.environ['DSH_TEST_SDK_ROOT'])), 'Original SDK source changed.'
        assert process.returncode == 0, stdout + stderr + '\nPrivate runtime retained: ' + str(scratch)
        assert cleanup and not cleanup['errors'] and cleanup['logging_flushed_after_stop'], 'Original cleanup remains unknown.'
        assert 'synthetic-unused-secret' not in stdout + stderr
        actual = json.loads((scratch / 'acceptance-evidence.json').read_text())
        assert actual['scenario'] == scenario
        passed = True
    finally:
        if passed and cleanup and not cleanup['errors']:
            shutil.rmtree(scratch)
