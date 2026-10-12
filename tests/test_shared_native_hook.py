"""An ordinary native Loader/preset limits only its registered Session."""
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys

import pytest

from sdk_source_integrity import source_snapshot


@pytest.mark.parametrize('scenario', ['basic', 'integrity', 'limits', 'unknown', 'observer', 'recovery',
                                      'lifecycle', 'tool_guard', 'inherited', 'guard_deadline', 'team', 'host_restart', 'late_host', 'renew_window',
                                      'runtime_stale_hash', 'runtime_noncanonical', 'runtime_workspace', 'runtime_writable',
                                      'runtime_notexec', 'runtime_old_version', 'runtime_entry_failed', 'runtime_reader_changed'])
def test_native_shared_preset_budget_and_lifecycle(tmp_path, scenario):
    configured = os.environ.get('DSH_TEST_SDK_ROOT')
    if not configured:
        pytest.skip('Original DSH SDK is absent.')
    sdk = Path(configured).resolve(strict=True)
    before = source_snapshot(sdk)
    result = subprocess.run(
        [shutil.which('node'), '--expose-gc', str(Path(__file__).with_name('shared_native_hook_runner.mjs')),
         str(tmp_path), scenario],
        env={**os.environ, 'DSH_TEST_SDK_ROOT': str(sdk), 'HERMES_TEST_SESSION_PYTHON': sys.executable},
        capture_output=True, text=True, timeout=45,
    )
    sdk_same = source_snapshot(sdk) == before
    (tmp_path / 'native-status.json').write_text(json.dumps({'exit_code': result.returncode,
        'stdout': result.stdout, 'stderr': result.stderr, 'sdk_unchanged': sdk_same}))
    assert result.returncode == 0, result.stdout + result.stderr
    payload = json.loads(result.stdout)
    (tmp_path / 'native-result.json').write_text(json.dumps(payload))
    assert payload['native_shared_hook'] == 'passed'
    assert sdk_same
