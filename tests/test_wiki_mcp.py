"""Original-requester knowledge queries against a bounded Streamable HTTP peer."""
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import threading
import time
import pytest

from ghost_hermes_pm import Manager
from readiness_support import ReadyManager as Manager
from ghost_hermes_pm.knowledge import configured_providers
from ghost_hermes_pm.transport import ManagementClient, ManagementServer
from test_directory import OWNER
from test_knowledge import WIKI, source_grant


EXPECTED_SERVER_INFO = {'name': 'synthetic-knowledge', 'version': '0.1.0'}
EXPECTED_TOOL_DESCRIPTION = 'Synthetic compiled knowledge with cited source windows.'


SCHEMA = {'type': 'object', 'properties': {'prompt': {'type': 'string'},
    'budget': {'type': 'integer', 'minimum': 500, 'maximum': 6000}},
    'required': ['prompt'], 'additionalProperties': False}


def context_pack():
    return {'prompt': 'retry delivery', 'budget': 500, 'revision': 'a' * 64,
        'primary': [{'id': 'artifact-retry', 'type': 'fact', 'title': 'Retry policy',
            'summary': 'Retry only definite failures.', 'excerpt': 'Unknown delivery needs reconciliation.',
            'owner': {}, 'version': {'repoRef': 'fixture/repo', 'commit': 'b' * 40,
                'observedAt': '2026-10-07', 'updatedAt': '2026-10-06'},
            'freshness': {'status': 'fresh', 'stale': False, 'archived': False, 'contradicted': False, 'warnings': []},
            'freshnessStatus': 'fresh', 'citations': [{'source': 'retry.md', 'start': 10, 'end': 80}],
            'sourceRefCount': 1, 'sourceWindows': [], 'truncated': False}],
        'secondary': [], 'warnings': [], 'diagnostics': [], 'estimatedTokens': 42,
        'rawSourcesInContext': 0, 'totalDocuments': 1, 'snapshotStatus': 'ready'}


@contextmanager
def mcp_peer(pack=None, *, mode='json', delay=0, fault=None, owned_session=True):
    calls, deletions = [], []
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass
        def do_POST(self):
            body = json.loads(self.rfile.read(int(self.headers['Content-Length'])))
            calls.append({'body': body, 'session': self.headers.get('Mcp-Session-Id'),
                          'protocol': self.headers.get('MCP-Protocol-Version'),
                          'authorization': self.headers.get('Authorization')})
            if self.headers.get('Authorization') != 'Bearer synthetic-wiki-token':
                self.send_response(401); self.end_headers(); return
            if body['method'] == 'initialize':
                result = {'protocolVersion': '2025-03-26', 'capabilities': {'tools': {}},
                          'serverInfo': dict(EXPECTED_SERVER_INFO)}
                if fault == 'protocol': result['protocolVersion'] = 'unsupported'
                if fault == 'server-name': result['serverInfo']['name'] = 'unexpected-source'
                if fault == 'server-version': result['serverInfo']['version'] = 'unexpected-version'
            elif body['method'] == 'notifications/initialized':
                self.send_response(202); self.end_headers(); return
            elif body['method'] == 'tools/list':
                result = {'tools': [{'name': 'get_context_pack', 'inputSchema': SCHEMA,
                    'description': EXPECTED_TOOL_DESCRIPTION}]}
                if fault == 'schema': result['tools'][0]['inputSchema'] = {'type': 'object'}
                if fault == 'description': result['tools'][0]['description'] = 'Unexpected compiled reader.'
            elif body['method'] == 'tools/call':
                time.sleep(delay)
                if fault == 'http-error':
                    self.send_response(500); self.end_headers(); self.wfile.write(b'opaque-synthetic-source-error'); return
                result = {'content': [{'type': 'text', 'text': json.dumps(pack if pack is not None else context_pack())}]}
            else:
                self.send_response(400); self.end_headers(); return
            if fault == 'rpc-error' and body['method'] == 'tools/call':
                payload = json.dumps({'jsonrpc': '2.0', 'id': body['id'], 'error': {'code': -32603, 'message': 'opaque-synthetic-source-error'}}).encode()
            else:
                payload = json.dumps({'jsonrpc': '2.0', 'id': 999 if fault == 'mismatched-id' else body['id'], 'result': result}).encode()
            self.send_response(200)
            self.send_header('Content-Type', 'text/event-stream' if mode == 'sse' else 'application/json')
            if body['method'] == 'initialize' and owned_session: self.send_header('Mcp-Session-Id', 'owned-fixture-session')
            if mode == 'sse': payload = b'event: message\ndata: ' + payload + b'\n\n'
            self.send_header('Content-Length', str(len(payload))); self.end_headers(); self.wfile.write(payload)
        def do_DELETE(self):
            deletions.append(self.headers.get('Mcp-Session-Id'))
            self.send_response(200); self.end_headers()
    server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True); thread.start()
    try:
        yield {'url': 'http://127.0.0.1:' + str(server.server_port) + '/mcp',
               'calls': calls, 'deletions': deletions}
    finally:
        server.shutdown(); server.server_close(); thread.join()


def provider_config(peer, corpus='corpus'):
    return {'mcp:fixture-wiki': {'url': peer['url'], 'corpus_scope_id': corpus,
        'credential_ref': 'native:FIXTURE_WIKI_TOKEN',
        'expected_server_info': dict(EXPECTED_SERVER_INFO),
        'expected_tool_description': EXPECTED_TOOL_DESCRIPTION}}


def mcp_grant(scope='corpus'):
    return {**source_grant(), 'provider_ref': 'mcp:fixture-wiki',
            'query_subjects': {OWNER.subject: [scope]}}


@pytest.mark.parametrize('field,value', [('expected_server_info', None), ('expected_tool_description', None),
    ('expected_server_info', {}), ('expected_tool_description', '')])
def test_unreviewed_descriptors_never_resolve_credentials_or_contact_the_source(field, value):
    from ghost_hermes_pm import ManagementError
    with mcp_peer() as peer:
        config = provider_config(peer)
        if value is None:
            config['mcp:fixture-wiki'].pop(field)
        else:
            config['mcp:fixture-wiki'][field] = value
        credentials = []
        with pytest.raises(ManagementError) as missing:
            configured_providers(config, credential_resolver=lambda ref: credentials.append(ref))
        assert missing.value.code == 'invalid_change'
        assert credentials == [] and peer['calls'] == peer['deletions'] == []


@pytest.mark.parametrize('field', ['expected_server_info', 'expected_tool_description'])
def test_original_corpus_grant_cannot_follow_changed_reviewed_descriptors(tmp_path, field):
    from ghost_hermes_pm import ManagementError
    with mcp_peer() as peer:
        config = provider_config(peer)
        resolver = lambda ref: 'synthetic-wiki-token'
        with Manager(tmp_path / 'state', owner_identity_ref=OWNER.subject,
                knowledge_providers=configured_providers(config, credential_resolver=resolver)) as manager:
            manager.apply_directory_change(OWNER, 0, {'profile': WIKI})
            with ManagementServer(manager, {'owner': OWNER}):
                client = ManagementClient(tmp_path / 'state', 'owner')
                client.register_knowledge_source(1, mcp_grant())
                if field == 'expected_server_info':
                    config['mcp:fixture-wiki'][field]['name'] = 'replacement-synthetic-source'
                else:
                    config['mcp:fixture-wiki'][field] = 'Replacement synthetic compiled reader.'
                manager.knowledge_providers.update(configured_providers(config, credential_resolver=resolver))
                with pytest.raises(ManagementError) as changed:
                    client.query_knowledge('fixture-wiki', 'changed-descriptor', 'retry delivery', ['corpus'])
                assert changed.value.code == 'source_denied'
                assert peer['calls'] == peer['deletions'] == []


def test_registered_query_uses_original_mcp_corpus_and_returns_actual_citations(tmp_path):
    with mcp_peer() as peer:
        providers = configured_providers(provider_config(peer), credential_resolver=lambda ref: 'synthetic-wiki-token')
        with Manager(tmp_path / 'state', owner_identity_ref=OWNER.subject, knowledge_providers=providers) as manager:
            manager.apply_directory_change(OWNER, 0, {'profile': WIKI})
            with ManagementServer(manager, {'owner-entry': OWNER}):
                client = ManagementClient(tmp_path / 'state', 'owner-entry')
                client.register_knowledge_source(1, mcp_grant())
                result = client.query_knowledge('fixture-wiki', 'mcp-query', 'retry delivery', ['corpus'])
                assert result['status'] == 'found'
                assert result['requester'] == OWNER.subject and result['searched_scope'] == ['corpus']
                material = result['materials'][0]
                assert 'Retry only definite failures.' in material['text']
                assert 'Unknown delivery needs reconciliation.' in material['text']
                assert 'Reported artifact version' in material['text'] and 'b' * 40 in material['text']
                assert 'retry.md' in material['locator'] and 'start=10' in material['locator']
                assert material['version'].startswith('result-snapshot-sha256:')
                assert material['updated_at'] == result['observed_at']
                assert material['kind'] == 'fact' and material['link_accessible'] is False
                assert client.read_snapshot()['requests'] == []
                assert 'synthetic-wiki-token' not in json.dumps(client.read_snapshot())
        assert [c['body']['method'] for c in peer['calls']] == ['initialize', 'notifications/initialized', 'tools/list', 'tools/call']
        assert peer['calls'][-1]['body']['params'] == {'name': 'get_context_pack', 'arguments': {'prompt': 'retry delivery', 'budget': 500}}
        assert peer['calls'][-1]['session'] == 'owned-fixture-session'
        assert peer['calls'][-1]['protocol'] == '2025-03-26'
        assert peer['deletions'] == ['owned-fixture-session']


def test_original_corpus_grant_does_not_follow_a_reconfigured_endpoint(tmp_path):
    import pytest
    from ghost_hermes_pm import ManagementError
    with mcp_peer() as original, mcp_peer() as replacement:
        resolver = lambda ref: 'synthetic-wiki-token'
        with Manager(tmp_path / 'state', owner_identity_ref=OWNER.subject,
                knowledge_providers=configured_providers(provider_config(original), credential_resolver=resolver)) as manager:
            manager.apply_directory_change(OWNER, 0, {'profile': WIKI})
            with ManagementServer(manager, {'owner': OWNER}):
                client = ManagementClient(tmp_path / 'state', 'owner')
                client.register_knowledge_source(1, mcp_grant())
                manager.knowledge_providers.update(configured_providers(provider_config(replacement), credential_resolver=resolver))
                with pytest.raises(ManagementError) as changed:
                    client.query_knowledge('fixture-wiki', 'changed-endpoint', 'retry delivery', ['corpus'])
                assert changed.value.code == 'source_denied'
                assert original['calls'] == replacement['calls'] == []
                client.register_knowledge_source(client.read_snapshot()['version'], mcp_grant())
                assert client.query_knowledge('fixture-wiki', 'fresh-owner-grant', 'retry delivery', ['corpus'])['status'] == 'found'


def test_bounded_remote_query_waits_for_the_original_result_without_requery(tmp_path):
    with mcp_peer(delay=3.3) as peer:
        with Manager(tmp_path / 'state', owner_identity_ref=OWNER.subject,
                knowledge_providers=configured_providers(provider_config(peer), credential_resolver=lambda ref: 'synthetic-wiki-token')) as manager:
            manager.apply_directory_change(OWNER, 0, {'profile': WIKI})
            with ManagementServer(manager, {'owner': OWNER}):
                client = ManagementClient(tmp_path / 'state', 'owner')
                client.register_knowledge_source(1, mcp_grant())
                assert client.query_knowledge('fixture-wiki', 'slow-original-query', 'retry delivery', ['corpus'])['status'] == 'found'
                assert len([c for c in peer['calls'] if c['body']['method'] == 'tools/call']) == 1


def test_subset_corpus_and_wiki_identity_cannot_use_the_original_requesters_authority(tmp_path):
    from ghost_hermes_pm import VerifiedIdentity, ManagementError
    with mcp_peer() as peer:
        with Manager(tmp_path / 'state', owner_identity_ref=OWNER.subject,
                knowledge_providers=configured_providers(provider_config(peer), credential_resolver=lambda ref: 'synthetic-wiki-token')) as manager:
            manager.apply_directory_change(OWNER, 0, {'profile': WIKI})
            wiki = VerifiedIdentity(WIKI['identity_ref'], 'registered-wiki')
            with ManagementServer(manager, {'owner': OWNER, 'wiki': wiki}):
                client = ManagementClient(tmp_path / 'state', 'owner')
                client.register_knowledge_source(1, mcp_grant('subset'))
                result = client.query_knowledge('fixture-wiki', 'subset', 'retry delivery', ['subset'])
                assert result['status'] == 'source_denied' and result['materials'] == []
                with pytest.raises(ManagementError) as denied:
                    ManagementClient(tmp_path / 'state', 'wiki').query_knowledge('fixture-wiki', 'wiki-authority', 'retry delivery', ['corpus'])
                assert denied.value.code == 'forbidden'
                assert peer['calls'] == peer['deletions'] == []


@pytest.mark.parametrize('artifact_type,flag,kind,status', [('module_brief', None, 'inference', 'found'),
    ('fact', 'stale', 'stale', 'found'), ('fact', 'contradicted', 'conflict', 'conflict')])
def test_sse_preserves_compiled_material_classification_and_owned_session_only(tmp_path, artifact_type, flag, kind, status):
    pack = context_pack(); pack['primary'][0]['type'] = artifact_type
    if flag: pack['primary'][0]['freshness'][flag] = True
    with mcp_peer(pack, mode='sse', owned_session=False) as peer:
        with Manager(tmp_path / 'state', owner_identity_ref=OWNER.subject,
                knowledge_providers=configured_providers(provider_config(peer), credential_resolver=lambda ref: 'synthetic-wiki-token')) as manager:
            manager.apply_directory_change(OWNER, 0, {'profile': WIKI})
            with ManagementServer(manager, {'owner': OWNER}):
                client = ManagementClient(tmp_path / 'state', 'owner')
                client.register_knowledge_source(1, mcp_grant())
                first = client.query_knowledge('fixture-wiki', 'sse-query', 'retry delivery', ['corpus'])
                assert first['status'] == status and first['materials'][0]['kind'] == kind
                assert client.query_knowledge('fixture-wiki', 'sse-query', 'retry delivery', ['corpus']) == first
                assert len([c for c in peer['calls'] if c['body']['method'] == 'tools/call']) == 1
        assert peer['deletions'] == []


@pytest.mark.parametrize('fault,methods', [('protocol', ['initialize']),
    ('server-name', ['initialize']), ('server-version', ['initialize']),
    ('mismatched-id', ['initialize']), ('schema', ['initialize', 'notifications/initialized', 'tools/list']),
    ('description', ['initialize', 'notifications/initialized', 'tools/list']),
    ('http-error', ['initialize', 'notifications/initialized', 'tools/list', 'tools/call']),
    ('rpc-error', ['initialize', 'notifications/initialized', 'tools/list', 'tools/call'])])
def test_mcp_failure_is_sanitized_without_blind_retry(tmp_path, fault, methods):
    with mcp_peer(fault=fault) as peer:
        with Manager(tmp_path / 'state', owner_identity_ref=OWNER.subject,
                knowledge_providers=configured_providers(provider_config(peer), credential_resolver=lambda ref: 'synthetic-wiki-token')) as manager:
            manager.apply_directory_change(OWNER, 0, {'profile': WIKI})
            with ManagementServer(manager, {'owner': OWNER}):
                client = ManagementClient(tmp_path / 'state', 'owner')
                client.register_knowledge_source(1, mcp_grant())
                result = client.query_knowledge('fixture-wiki', 'unknown-source', 'retry delivery', ['corpus'])
                assert result['status'] == 'source_denied' and result['materials'] == []
                assert client.query_knowledge('fixture-wiki', 'unknown-source', 'retry delivery', ['corpus']) == result
                assert 'opaque-synthetic-source-error' not in json.dumps(client.read_snapshot())
        assert [c['body']['method'] for c in peer['calls']] == methods
        assert peer['deletions'] == ['owned-fixture-session']


@pytest.mark.parametrize('case,status', [('no-citation', 'source_denied'), ('credential-echo', 'source_denied'), ('not-found', 'not_found')])
def test_unknown_provenance_and_secret_echo_do_not_become_public_material(tmp_path, case, status):
    pack = context_pack()
    if case == 'no-citation': pack['primary'][0]['citations'] = []
    if case == 'credential-echo': pack['primary'][0]['summary'] = 'synthetic-wiki-token'
    if case == 'not-found': pack['primary'] = []
    with mcp_peer(pack) as peer:
        with Manager(tmp_path / 'state', owner_identity_ref=OWNER.subject,
                knowledge_providers=configured_providers(provider_config(peer), credential_resolver=lambda ref: 'synthetic-wiki-token')) as manager:
            manager.apply_directory_change(OWNER, 0, {'profile': WIKI})
            with ManagementServer(manager, {'owner': OWNER}):
                client = ManagementClient(tmp_path / 'state', 'owner')
                client.register_knowledge_source(1, mcp_grant())
                result = client.query_knowledge('fixture-wiki', case, 'retry delivery', ['corpus'])
                assert result['status'] == status and result['materials'] == []
                assert 'synthetic-wiki-token' not in json.dumps(client.read_snapshot())
        assert b'synthetic-wiki-token' not in (tmp_path / 'state' / 'manager.sqlite3').read_bytes()


@pytest.mark.parametrize('case', ['active-fact', 'late-fact', 'compiled-inference', 'conflict'])
def test_configured_original_mcp_completes_registered_mentions_and_supplements_only_current_facts(tmp_path, case):
    import asyncio
    from ghost_hermes_pm import VerifiedIdentity
    from ghost_hermes_pm.messages import FeishuEntry
    from test_feishu_entry import CONFIG, Gateway
    from test_directory import make_repo
    from test_task_execution import accepted, adapter_for
    from test_task_control import TURN, wire
    from test_knowledge import public_grant, native_transport, bot_message
    pack = context_pack()
    if case == 'compiled-inference': pack['primary'][0]['type'] = 'module_brief'
    if case == 'conflict': pack['primary'][0]['freshness']['contradicted'] = True
    source = public_grant(); source['provider_ref'] = 'mcp:fixture-wiki'
    sender = VerifiedIdentity('fixture:lead', 'verified-native-participant')
    settings = {**CONFIG, 'registered_bots': [{'profile_id': b['sender_profile_id'], 'identity_ref': b['sender_identity_ref'],
        'app_id': b['app_id'], 'tenant_key': b['sender_tenant_key'], 'open_id': b['sender_open_id'],
        'native_ids': b['sender_native_ids']} for b in source['wiki_bindings']]}
    with mcp_peer(pack) as peer:
        providers = configured_providers(provider_config(peer, 'public'), credential_resolver=lambda ref: 'synthetic-wiki-token')
        with Manager(tmp_path / 'state', owner_identity_ref=OWNER.subject, codex_adapter=adapter_for(tmp_path),
                knowledge_providers=providers) as manager:
            request_id = accepted(manager, make_repo(tmp_path / 'repo'))
            manager.start_task(OWNER, request_id)
            manager.apply_directory_change(OWNER, manager.read_snapshot(OWNER)['version'], {'profile': WIKI})
            with ManagementServer(manager, {'owner': OWNER, 'lead': sender}):
                owner, lead = ManagementClient(tmp_path / 'state', 'owner'), ManagementClient(tmp_path / 'state', 'lead')
                owner.register_knowledge_source(owner.read_snapshot()['version'], source)
                lead.query_knowledge('fixture-wiki', 'mcp-public-chain', 'retry delivery', ['public'],
                    request_id=request_id, channel_id='project-chat', auto_supplement=True)
                assert peer['calls'] == [], 'A public query waits for verified original Wiki reception.'
                if case == 'late-fact': owner.control_task(request_id, 'stop', 'stop-before-wiki', expected_turn_id=TURN)
                left, right, left_sent, right_sent = object(), object(), [], []
                left_transport = native_transport('cli_fixture', 'ou_lead', 'om_lead_', left_sent)
                right_transport = native_transport('cli_wiki', 'ou_wiki_self', 'om_wiki_', right_sent)
                intake = FeishuEntry(lambda: manager, OWNER.subject, settings, lambda _: None)
                intake.attach_transport(left, left_transport); intake.attach_transport(right, right_transport)
                async def exchange():
                    await intake.deliver_knowledge(sender, 'mcp-public-chain', left_transport)
                    query_text = json.loads(left_sent[0].request_body.content)['zh_cn']['content'][0][1]['text'].strip()
                    assert await intake.receive(bot_message(source['wiki_bindings'][0], query_text, 'om_lead_1', 'om_ack'), Gateway(right)) == {'action': 'skip'}
                    assert json.loads(right_sent[0].request_body.content)['zh_cn']['content'][0][0]['user_id'] == 'ou_lead_in_wiki'
                    result_text = json.loads(right_sent[0].request_body.content)['zh_cn']['content'][0][1]['text'].strip()
                    result_event = bot_message(source['wiki_bindings'][1], result_text, 'om_wiki_1', 'om_lead_1')
                    assert await intake.receive(result_event, Gateway(left)) == {'action': 'skip'}
                    assert await intake.receive(result_event, Gateway(left)) == {'action': 'skip'}
                asyncio.run(exchange())
                query = lead.read_snapshot()['knowledge_queries'][0]
                assert query['materials'] and query['requester'] == sender.subject
                assert query['result_anchor']['message_id'] == 'om_wiki_1'
                steering = [r for r in wire(tmp_path) if r['method'] == 'turn/steer']
                assert len(steering) == (1 if case == 'active-fact' else 0)
                assert len([r for r in wire(tmp_path) if r['method'] == 'turn/start']) == 1
                if steering:
                    assert steering[0]['params']['expectedTurnId'] == TURN
                    assert 'untrusted source data' in steering[0]['params']['input'][0]['text']
                    assert 'retry.md' in steering[0]['params']['input'][0]['text']
                assert len([c for c in peer['calls'] if c['body']['method'] == 'tools/call']) == 1
