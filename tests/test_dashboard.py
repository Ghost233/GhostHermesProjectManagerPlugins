from pathlib import Path
import tempfile
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from ghost_hermes_pm import Manager
from ghost_hermes_pm.transport import ManagementClient, ManagementServer
from ghost_hermes_pm.dashboard import create_router
from test_directory import OWNER, registration, make_repo


def test_dashboard_registers_corrects_and_reads_same_gateway_state_and_rejects_actor(tmp_path):
    repo = make_repo(tmp_path / 'repo')
    with tempfile.TemporaryDirectory(prefix='hpm-', dir='/tmp') as state:
        with Manager(state, owner_identity_ref=OWNER.subject) as manager:
            with ManagementServer(manager, {'fixture-token': OWNER}):
                client = ManagementClient(state, 'fixture-token')
                app = FastAPI()
                # The host adapter, rather than body fields, establishes this fixture identity.
                def authenticated_owner(request):
                    if request.headers.get('x-fixture-owner') != 'verified':
                        from fastapi import HTTPException
                        raise HTTPException(403, 'Unverified owner')
                    return client
                app.include_router(create_router(authenticated_owner))
                browser = TestClient(app)
                assert browser.get('/snapshot').status_code == 403
                headers = {'x-fixture-owner': 'verified'}
                body = {'expected_version': 0, 'change': registration(repo)}
                assert browser.post('/directory', json={**body, 'actor': OWNER.subject}, headers=headers).status_code == 422
                result = browser.post('/directory', json=body, headers=headers)
                assert result.status_code == 200
                correction = registration(repo)
                correction['project']['name'] = 'Dashboard correction'
                assert browser.post('/directory', json={'expected_version': 1, 'change': correction}, headers=headers).status_code == 200
                assert browser.get('/snapshot', headers=headers).json() == manager.read_snapshot(OWNER)
                assert browser.post('/directory', json=body, headers=headers).status_code == 409
            offline = browser.get('/snapshot', headers=headers).json()
            assert offline['status'] == 'unverified'
            assert offline['last_verified_at']
            assert offline['projects'][0]['name'] == 'Dashboard correction'
            assert browser.post('/directory', json=body, headers=headers).status_code == 503


def test_native_dashboard_owner_mapping_rejects_other_authenticated_users_and_machine_tokens(tmp_path, monkeypatch):
    from ghost_hermes_pm.native import NativeDashboardEntry
    from types import ModuleType, SimpleNamespace
    import sys
    from fastapi import HTTPException
    settings = {'state_dir': str(tmp_path), 'dashboard_credential_ref': 'native:FIXTURE_OWNER_TOKEN',
                'dashboard_owner_users': [{'provider': 'fixture-idp', 'user_id': 'owner', 'org_id': 'tenant'}]}
    config = ModuleType('hermes_cli.config')
    config.load_config_readonly = lambda: {'plugins': {'entries': {'ghost-hermes-pm': {'settings': settings}}}}
    host = ModuleType('hermes_cli.web_server')
    def require_token(request):
        if getattr(request.state, 'session', None) is None:
            raise HTTPException(401, 'Unauthorized')
    host._require_token = require_token
    secrets = ModuleType('agent.secret_scope')
    secrets.get_secret = lambda ref: 'fixture-only-secret' if ref == 'FIXTURE_OWNER_TOKEN' else None
    monkeypatch.setitem(sys.modules, 'hermes_cli.config', config)
    monkeypatch.setitem(sys.modules, 'hermes_cli.web_server', host)
    monkeypatch.setitem(sys.modules, 'agent.secret_scope', secrets)
    app = FastAPI()
    app.state.auth_required = True
    @app.middleware('http')
    async def fixture_identity(request, call_next):
        user = request.headers.get('x-fixture-user')
        if user:
            request.state.session = SimpleNamespace(provider='fixture-idp', user_id=user, org_id='tenant')
        request.state.token_authenticated = request.headers.get('x-fixture-machine') == 'yes'
        return await call_next(request)
    app.include_router(create_router(NativeDashboardEntry()))
    browser = TestClient(app)
    assert browser.get('/snapshot', headers={'x-fixture-user': 'stranger'}).status_code == 403
    assert browser.get('/snapshot', headers={'x-fixture-user': 'owner', 'x-fixture-machine': 'yes'}).status_code == 403
    assert browser.get('/snapshot?profile=another', headers={'x-fixture-user': 'owner'}).status_code == 403
    authorized = browser.get('/snapshot', headers={'x-fixture-user': 'owner'})
    assert authorized.status_code == 503
    assert 'fixture-only-secret' not in authorized.text


def test_dashboard_reads_authoritative_acceptance_delivery_and_unexecuted_reason(tmp_path):
    from test_requests import MESSAGE, ISSUE
    with tempfile.TemporaryDirectory(prefix='hpm-requests-', dir='/tmp') as state:
        with Manager(state, owner_identity_ref=OWNER.subject) as manager:
            manager.apply_directory_change(OWNER, 0, registration(make_repo(tmp_path / 'repo')))
            accepted = manager.accept_request(OWNER, 'mono', 'mono-lead', MESSAGE, ISSUE)['request']
            manager.publish_request_message(OWNER, accepted['id'], 'confirmation', '已受理')
            segment = manager.claim_delivery(OWNER, accepted['id'])
            manager.record_delivery(OWNER, accepted['id'], segment['uuid'], {'status': 'failed', 'code': 999})
            with ManagementServer(manager, {'fixture-token': OWNER}):
                client = ManagementClient(state, 'fixture-token')
                app = FastAPI()
                app.include_router(create_router(lambda request: client))
                browser = TestClient(app)
                snapshot = browser.get('/snapshot').json()
                assert snapshot == manager.read_snapshot(OWNER)
                task = snapshot['requests'][0]
                assert task['acceptance'] == 'accepted'
                assert task['delivery'] == 'failed'
                assert task['execution'] == 'waiting'
                assert task['unexecuted_reason'] == 'Codex execution is not enabled.'
            offline = browser.get('/snapshot').json()
            assert offline['status'] == 'unverified'
            assert offline['requests'][0] == task
