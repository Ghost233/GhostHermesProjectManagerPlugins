"""Explicit native configuration stays usable after removing the auth companion."""
import hashlib
import json

import pytest

from ghost_hermes_pm import ManagementError
from ghost_hermes_pm import dsh, observation, recovery, takeover


def native_configuration():
    return {'mode': 'remote', 'base_url': 'http://127.0.0.1:9', 'cookie': 'synthetic-auth=value',
            'service_ref': 'local:synthetic-service', 'source_kind': 'desktop',
            'endpoint_ref': 'local:synthetic-endpoint'}


def test_explicit_native_configuration_freezes_only_the_credential_digest():
    configuration = native_configuration()
    frozen, runtime = observation.resolved_configuration(configuration)
    assert runtime['base_url'] == configuration['base_url']
    assert runtime['cookie'] == configuration['cookie']
    assert 'cookie' not in frozen
    assert frozen['credential_sha256'] == hashlib.sha256(configuration['cookie'].encode()).hexdigest()
    assert configuration['cookie'] not in json.dumps(frozen)
    assert configuration == native_configuration()


@pytest.mark.parametrize('kind', ['executor', 'observation', 'recovery', 'takeover'])
@pytest.mark.parametrize('change', ['publication_only', 'mixed_publication', 'binding'])
def test_withdrawn_auth_configuration_is_rejected_before_any_native_client(tmp_path, monkeypatch, kind, change):
    def unexpected(*args, **kwargs):
        pytest.fail('Withdrawn authentication settings must not construct a native client.')
    factories = {
        'executor': (dsh, 'DshRemoteAdapter', lambda configuration: dsh.configured_adapter(configuration, tmp_path)),
        'observation': (observation, 'ReadOnlyDshAdapter', lambda configuration: observation.configured_observation_adapters([configuration], tmp_path)),
        'recovery': (recovery, 'OriginalRecoveryAdapter', lambda configuration: recovery.configured_recovery_adapters([configuration], tmp_path)),
        'takeover': (takeover, 'OriginalControlAdapter', lambda configuration: takeover.configured_control_adapters([configuration], tmp_path)),
    }
    module, constructor, factory = factories[kind]
    monkeypatch.setattr(module, constructor, unexpected)
    configuration = native_configuration()
    if kind == 'takeover':
        configuration['source_id'] = 'synthetic-manual-source'
    if change == 'binding':
        configuration['auth_binding'] = {'digest': 'synthetic-withdrawn-binding'}
    else:
        configuration.update(auth_publication=str(tmp_path / 'withdrawn.json'), expected_backend={})
        if change == 'publication_only':
            del configuration['base_url']
            del configuration['cookie']
    with pytest.raises(ManagementError) as rejected:
        factory(configuration)
    assert rejected.value.code == 'invalid_change'
