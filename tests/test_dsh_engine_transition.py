"""Executor transitions preserve prior authority and require explicit DSH bindings."""
from datetime import datetime, timedelta, timezone
import hashlib
import json
import os
import sqlite3

import pytest

from ghost_hermes_pm import Manager, ManagementError
from ghost_hermes_pm.dsh import configured_adapter
from ghost_hermes_pm.observation import configured_observation_adapters, original_configuration
from ghost_hermes_pm.recovery import configured_recovery_adapters
from ghost_hermes_pm.takeover import configured_control_adapters
from test_directory import OWNER


def dsh_config(url, *, source_id=None):
    config = {'base_url': url, 'cookie': 'synthetic-auth=value',
        'service_ref': 'local:manual-desktop', 'source_kind': 'desktop',
        'endpoint_ref': 'local:registered-desktop', 'expected_home': '/synthetic/home'}
    if source_id is not None:
        config['source_id'] = source_id
    return config


def observation_receipt(adapter, config):
    """Synthetic fixture evidence only; no live backend acceptance is claimed."""
    connection = adapter.connection
    now = datetime.now(timezone.utc)
    methods = ['session/list', 'session/projections', 'session/follow', 'session/page']
    return {'engine': 'dsh', 'generation': adapter.generation, 'service_ref': adapter.service_ref,
        'source_kind': adapter.source_kind, 'endpoint_ref': adapter.endpoint_ref,
        'transport': 'desktop_http_mux', 'service_id': connection['service_id'],
        'client_id': connection['client_id'], 'original_executor_id': connection['service_id'],
        'backend_instance_ref': 'fixture:synthetic-http-peer', 'provenance': 'trusted_host_original_dsh',
        'configuration_sha256': hashlib.sha256(json.dumps(original_configuration(config), sort_keys=True).encode()).hexdigest(),
        'endpoint_sha256': hashlib.sha256(config['base_url'].encode()).hexdigest(), 'platform': connection['platform'],
        'original_endpoint_verified': 'PASS', 'observation_read_only': 'PASS',
        'supported_methods': methods, 'source_kinds': ['desktop'], 'runtime_coverage': 'unknown',
        'evidence_origin': 'synthetic-fixture-only', 'verified_at': now.isoformat(),
        'expires_at': (now + timedelta(minutes=5)).isoformat(),
        'read_cases': {method: 'PASS' for method in methods + ['no_execution_writes', 'original_request_routing', 'unsupported_scope', 'disconnect']}}


def save_observation_receipt(state_dir, config, receipt):
    evidence = state_dir / 'observation-evidence'
    evidence.mkdir(exist_ok=True)
    path = evidence / 'synthetic-native.json'
    path.write_text(json.dumps(receipt))
    (state_dir / 'dsh-observation.json').write_text(json.dumps({config['service_ref']:
        {'path': 'observation-evidence/synthetic-native.json', 'sha256': hashlib.sha256(path.read_bytes()).hexdigest()}}))
    return path


def test_new_dsh_directory_survives_reload_with_its_explicit_engine(tmp_path):
    state = tmp_path / 'state'
    with Manager(state, owner_identity_ref=OWNER.subject) as manager:
        assert manager.read_snapshot(OWNER)['projects'] == []
    with Manager(state, owner_identity_ref=OWNER.subject) as manager:
        assert manager.read_snapshot(OWNER)['profiles'] == []
    with sqlite3.connect(state / 'manager.sqlite3') as database:
        schema, payload = database.execute('SELECT schema_version,payload FROM directory').fetchone()
    assert schema == 2
    assert json.loads(payload)['executor_engine'] == 'dsh'


@pytest.mark.parametrize('schema,engine', [(1, None), (2, None), (2, 'codex')])
def test_legacy_executor_state_is_refused_without_changing_database_bytes(tmp_path, schema, engine):
    state = tmp_path / 'state'
    state.mkdir(mode=0o700)
    path = state / 'manager.sqlite3'
    payload = {'projects': {}, 'profiles': {}}
    if engine is not None:
        payload['executor_engine'] = engine
    with sqlite3.connect(path) as database:
        database.execute('CREATE TABLE directory(id INTEGER PRIMARY KEY,schema_version INTEGER,version INTEGER,payload TEXT)')
        database.execute('INSERT INTO directory VALUES(1,?,0,?)', (schema, json.dumps(payload)))
    os.chmod(path, 0o600)
    before = path.read_bytes()
    with pytest.raises(ManagementError) as rejected:
        Manager(state, owner_identity_ref=OWNER.subject)
    assert rejected.value.code == 'unknown_version'
    assert path.read_bytes() == before
    assert {entry.name for entry in state.iterdir()} == {'manager.sqlite3'}


@pytest.mark.parametrize('factory', [configured_adapter, configured_observation_adapters,
    configured_control_adapters, configured_recovery_adapters])
def test_legacy_process_configuration_is_not_reinterpreted_as_dsh(tmp_path, factory):
    legacy = {'command': ['synthetic-old-executor', 'app-server'], 'executable': '/synthetic/executor',
        'cwd': '/synthetic/project', 'environment': {'CODEX_HOME': '/synthetic/old-home'},
        'endpoint': '/synthetic/old.sock', 'service_ref': 'local:old', 'source_kind': 'desktop',
        'endpoint_ref': 'local:old-endpoint'}
    value = legacy if factory is configured_adapter else [legacy]
    with pytest.raises(ManagementError) as rejected:
        factory(value, tmp_path / 'state')
    assert rejected.value.code == 'invalid_change'
    assert not (tmp_path / 'state').exists()


def test_legacy_profile_executor_binding_cannot_be_registered_in_new_dsh_directory(tmp_path):
    from test_directory import make_repo
    from test_task_execution import execution_registration
    repo = make_repo(tmp_path / 'repo')
    change = execution_registration(repo)
    profile = change['profile']
    profile['connection_refs'] = {'codex': 'local:legacy-executor'}
    with Manager(tmp_path / 'state', owner_identity_ref=OWNER.subject) as manager:
        before = manager.read_snapshot(OWNER)
        with pytest.raises(ManagementError) as rejected:
            manager.apply_directory_change(OWNER, before['version'], change)
        assert rejected.value.code == 'invalid_change'
        after = manager.read_snapshot(OWNER)
        assert after['projects'] == [] and after['profiles'] == []
        assert after['version'] == before['version']
