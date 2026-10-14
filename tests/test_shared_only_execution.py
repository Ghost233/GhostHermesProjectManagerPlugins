"""Retired dedicated execution is refused at public admission boundaries."""
import os
import json
from pathlib import Path
import sys
import subprocess

import pytest

from ghost_hermes_pm.dsh import configured_adapter
from ghost_hermes_pm.manager import ManagementError
from ghost_hermes_pm.repository_execution import execution_configuration


@pytest.mark.parametrize('mode', [None, 'owned_native'])
def test_configured_executor_refuses_dedicated_home_before_start(tmp_path, mode):
    workspace = tmp_path / 'repository'
    workspace.mkdir()
    home = tmp_path / 'retired-home'
    configuration = {'dsh_home': str(home), 'workspace': str(workspace),
        'runtime_package_root': str(Path(os.environ['DSH_TEST_SDK_ROOT']).resolve()),
        'service_ref': 'local:fixture-retired'}
    if mode is not None:
        configuration['mode'] = mode
    environment_before = os.environ.get('DSH_HOME')
    launches = []
    active = True

    def observe(event, args):
        if active and event == 'subprocess.Popen':
            launches.append(args[0])

    sys.addaudithook(observe)
    adapter = None
    try:
        with pytest.raises(ManagementError) as refused:
            adapter = configured_adapter(configuration, tmp_path / 'state')
        assert refused.value.code == 'invalid_change'
    finally:
        active = False
        if adapter is not None:
            adapter.close()
    assert launches == []
    assert not home.exists()
    assert not (tmp_path / 'state').exists()
    assert os.environ.get('DSH_HOME') == environment_before


@pytest.mark.parametrize('mode', ['default', 'owned_native'])
def test_registered_supervision_refuses_retired_history_without_resurrection(tmp_path, mode):
    from sdk_source_integrity import source_snapshot
    sdk = Path(os.environ['HERMES_TEST_SDK_ROOT']).resolve()
    before = source_snapshot(sdk)
    result = subprocess.run([sys.executable, str(Path(__file__).with_name('shared_only_native_runner.py')),
        str(tmp_path), mode], capture_output=True, text=True, timeout=20,
        env={**os.environ, 'PYTHONPATH': os.pathsep.join((str(sdk), str(Path(__file__).resolve().parents[1]))),
             'PYTHONDONTWRITEBYTECODE': '1'})
    assert result.returncode == 0, result.stdout + result.stderr
    evidence = json.loads(result.stdout)
    (tmp_path / 'native-rejection.json').write_text(json.dumps(evidence))
    assert evidence['response'] == {'status': 'unverified', 'code': 'invalid_change',
        'repository_retained': True, 'delivered': False}
    assert evidence['launches'] == evidence['legacy_imports'] == evidence['old_home_accesses'] == []
    assert evidence['record_unchanged'] and evidence['database_bytes_unchanged']
    assert evidence['history_bytes_unchanged'] and evidence['dsh_home_unchanged']
    assert evidence['card'] == 'blocked'
    assert source_snapshot(sdk) == before


@pytest.mark.parametrize('mode', ['default', 'owned_native', 'remote'])
def test_repository_admission_refuses_legacy_before_reading_model_credentials(tmp_path, mode):
    from simple_worker_model import execution_reference
    reference = Path(execution_reference(tmp_path, 'http://127.0.0.1:9/v1',
        Path(os.environ['DSH_TEST_SDK_ROOT']).resolve()))
    configuration = json.loads(reference.read_text())
    if mode != 'default':
        reference.write_text(json.dumps({**configuration, 'mode': mode}))
    credential = Path(configuration['model']['env_file'])
    before = {p: p.read_bytes() for p in tmp_path.rglob('*') if p.is_file()}
    credential_reads, launches = [], []
    environment_before = os.environ.get('DSH_HOME')
    active = True

    def observe(event, args):
        if active and event == 'open' and isinstance(args[0], (str, bytes)):
            if os.fsdecode(args[0]) == str(credential):
                credential_reads.append(str(credential))
        if active and event == 'subprocess.Popen':
            launches.append(args[0])

    sys.addaudithook(observe)
    refusal = None
    try:
        execution_configuration({'execution_ref': str(reference)})
    except ManagementError as error:
        refusal = error.code
    finally:
        active = False
    assert credential_reads == [], 'Retired execution must not read its old model credentials.'
    assert refusal == 'configuration_missing'
    assert launches == []
    assert {p: p.read_bytes() for p in tmp_path.rglob('*') if p.is_file()} == before
    assert os.environ.get('DSH_HOME') == environment_before


def test_explicit_remote_adapter_remains_a_connection_without_a_dedicated_home(tmp_path):
    configuration = {'mode': 'remote', 'base_url': 'http://127.0.0.1:9',
        'cookie': 'synthetic-only=value', 'service_ref': 'local:fixture-existing',
        'source_kind': 'desktop', 'endpoint_ref': 'local:fixture-desktop'}
    environment_before = os.environ.get('DSH_HOME')
    adapter = configured_adapter(configuration, tmp_path / 'state')
    try:
        assert adapter.base_url == 'http://127.0.0.1:9'
        assert adapter.service_ref == 'local:fixture-existing'
    finally:
        adapter.close()
    assert configured_adapter({}, tmp_path / 'state') is None
    assert not (tmp_path / 'state').exists()
    assert list(tmp_path.iterdir()) == []
    assert os.environ.get('DSH_HOME') == environment_before
