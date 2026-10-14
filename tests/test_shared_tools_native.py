"""Original native composition and tools enforce the managed Session boundary."""
import json
import hashlib
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile

import pytest

from sdk_source_integrity import source_snapshot


@pytest.mark.parametrize('scenario', ['tools', 'jobs', 'inherited', 'deadline'])
def test_shared_original_tools_and_owned_jobs(tmp_path, scenario):
    configured = os.environ.get('DSH_TEST_SDK_ROOT')
    if not configured:
        pytest.skip('Original DSH SDK is absent.')
    sdk = Path(configured).resolve(strict=True)
    before = source_snapshot(sdk)
    hook = Path(__file__).resolve().parents[1] / 'ghost_hermes_pm/shared_native_hook.mjs'
    hook_before = hashlib.sha256(hook.read_bytes()).hexdigest()
    # macOS's native sandbox grants /tmp, so a /tmp sibling is no boundary assertion.
    with tempfile.TemporaryDirectory(prefix='.native-boundary-', dir=Path(__file__).resolve().parents[1]) as protected:
        result = subprocess.run(
            [shutil.which('node'), str(Path(__file__).with_name('shared_tools_native_runner.mjs')),
             str(tmp_path), protected, scenario],
            env={**os.environ, 'DSH_TEST_SDK_ROOT': str(sdk),
                 'HERMES_TEST_SESSION_PYTHON': os.environ.get('HERMES_TEST_SESSION_PYTHON', sys.executable)},
            capture_output=True, text=True, timeout=55,
        )
    sdk_same = source_snapshot(sdk) == before
    hook_same = hashlib.sha256(hook.read_bytes()).hexdigest() == hook_before
    (tmp_path / 'native-status.json').write_text(json.dumps({'exit_code': result.returncode,
        'stdout': result.stdout, 'stderr': result.stderr, 'sdk_unchanged': sdk_same,
        'hook_sha256': hook_before, 'hook_unchanged': hook_same}))
    assert result.returncode == 0, result.stdout + result.stderr
    payload = json.loads(result.stdout)
    (tmp_path / 'native-result.json').write_text(json.dumps(payload))
    assert payload['native_shared_tools'] == 'passed'
    assert sdk_same
    assert hook_same, 'The tested Hook changed while the native probe was running.'
