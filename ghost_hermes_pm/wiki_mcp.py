"""Narrow readonly adapter for the existing Conso knowledge context-pack tool."""
from datetime import datetime, timezone
import hashlib
import json
import time
from urllib.parse import quote, urlsplit

import requests

from .manager import ManagementError, _public_text

PROTOCOL = '2025-03-26'
SCHEMA = {'type': 'object', 'properties': {'prompt': {'type': 'string'},
    'budget': {'type': 'integer', 'minimum': 500, 'maximum': 6000}},
    'required': ['prompt'], 'additionalProperties': False}
DESCRIPTION = 'Read compact compiled Conso knowledge and its citations. Raw sources are read separately only when needed.'


class ConsoWikiMCPProvider:
    """An explicitly granted entire corpus; the remote tool has no requester ACL."""
    def __init__(self, config, credential_resolver):
        if not isinstance(config, dict) or set(config) != {'url', 'corpus_scope_id', 'credential_ref'}:
            raise ManagementError('invalid_change', 'An original MCP endpoint, entire corpus scope and native credential reference are required.')
        url, corpus, reference = (config[k] for k in ('url', 'corpus_scope_id', 'credential_ref'))
        if not isinstance(url, str) or not isinstance(corpus, str) or not corpus or len(corpus) > 256:
            raise ManagementError('invalid_change', 'Explicit bounded MCP endpoint and corpus scope are required.')
        parsed = urlsplit(url)
        if parsed.scheme not in {'http', 'https'} or not parsed.hostname or parsed.username or parsed.password or parsed.query or parsed.fragment:
            raise ManagementError('invalid_change', 'MCP endpoints cannot embed credentials, query parameters or fragments.')
        if reference is not None and (not isinstance(reference, str) or not reference.startswith('native:') or not reference.removeprefix('native:') or not callable(credential_resolver)):
            raise ManagementError('invalid_change', 'MCP authentication requires a native secret reference.')
        self._token = credential_resolver(reference) if reference is not None else None
        if reference is not None and (not isinstance(self._token, str) or not self._token or '\r' in self._token or '\n' in self._token):
            raise ManagementError('source_unavailable', 'The native MCP credential is unavailable in the verified Profile scope.')
        self.url, self.corpus_scope_id = url, corpus
        self.binding_digest = hashlib.sha256(json.dumps(config, sort_keys=True).encode()).hexdigest()

    def query(self, *, requester, source_id, question, scope_ids):
        if scope_ids != [self.corpus_scope_id]:
            raise ManagementError('source_denied', 'This original MCP tool cannot prove a filtered subset of its corpus.')
        _public_text(question, (self._token,))
        headers = {'Accept': 'application/json, text/event-stream', 'Content-Type': 'application/json'}
        if self._token:
            headers['Authorization'] = 'Bearer ' + self._token
        session_id = None
        http = requests.Session()
        http.trust_env = False
        deadline = time.monotonic() + 20
        def rpc(method, params=None, request_id=None):
            nonlocal session_id
            if time.monotonic() >= deadline:
                raise ManagementError('source_denied', 'The bounded MCP query timed out.')
            body = {'jsonrpc': '2.0', 'method': method}
            if params is not None: body['params'] = params
            if request_id is not None: body['id'] = request_id
            with http.post(self.url, headers=headers, json=body, timeout=(3, 5), stream=True, allow_redirects=False) as response:
                if request_id is None:
                    if response.status_code not in {200, 202, 204}:
                        raise ManagementError('source_denied', 'The MCP initialization notification was not accepted.')
                    return None
                if response.status_code != 200:
                    raise ManagementError('source_denied', 'The original MCP source refused or could not verify this query.')
                current_session = response.headers.get('Mcp-Session-Id')
                if method == 'initialize' and current_session:
                    if len(current_session) > 256 or any(ord(c) < 33 or ord(c) > 126 for c in current_session):
                        raise ManagementError('source_denied', 'The source returned an invalid MCP session identifier.')
                    session_id = current_session
                    headers['Mcp-Session-Id'] = session_id
                elif current_session and current_session != session_id:
                    raise ManagementError('source_denied', 'The MCP source changed its session binding.')
                content_type = response.headers.get('Content-Type', '').split(';')[0].strip().lower()
                if content_type not in {'application/json', 'text/event-stream'}:
                    raise ManagementError('source_denied', 'The source returned an unsupported MCP response format.')
                buffer, size = bytearray(), 0
                data = None
                for chunk in response.iter_content(chunk_size=1):
                    size += len(chunk)
                    if size > 1048576 or time.monotonic() >= deadline:
                        raise ManagementError('source_denied', 'The MCP response exceeded its bounded query budget.')
                    buffer.extend(chunk)
                    if content_type == 'text/event-stream' and (buffer.endswith(b'\n\n') or buffer.endswith(b'\r\n\r\n')):
                        lines = [line[5:].lstrip() for line in bytes(buffer).splitlines() if line.startswith(b'data:')]
                        buffer.clear()
                        if lines:
                            candidate = json.loads(b'\n'.join(lines))
                            if isinstance(candidate, dict) and type(candidate.get('id')) is int and candidate['id'] == request_id:
                                data = candidate
                                break
                if content_type == 'application/json': data = json.loads(buffer)
                if not isinstance(data, dict) or data.get('jsonrpc') != '2.0' or type(data.get('id')) is not int or data['id'] != request_id or 'error' in data or not isinstance(data.get('result'), dict):
                    raise ManagementError('source_denied', 'The MCP response did not verify this original request.')
                return data['result']
        try:
            initial = rpc('initialize', {'protocolVersion': PROTOCOL, 'capabilities': {},
                'clientInfo': {'name': 'ghost-hermes-pm-readonly', 'version': '0.1.0'}}, 1)
            if initial.get('protocolVersion') != PROTOCOL or initial.get('serverInfo') != {'name': 'conso-knowledge', 'version': '0.1.0'}:
                raise ManagementError('source_denied', 'The registered original Wiki protocol or server changed.')
            headers['MCP-Protocol-Version'] = PROTOCOL
            rpc('notifications/initialized')
            tools = rpc('tools/list', {}, 2).get('tools')
            reader = [t for t in tools if isinstance(t, dict) and t.get('name') == 'get_context_pack'] if isinstance(tools, list) else []
            if len(reader) != 1 or reader[0].get('inputSchema') != SCHEMA or reader[0].get('description') != DESCRIPTION:
                raise ManagementError('source_denied', 'The registered readonly context-pack contract changed.')
            result = rpc('tools/call', {'name': 'get_context_pack', 'arguments': {'prompt': question, 'budget': 500}}, 3)
            content = result.get('content')
            if result.get('isError') or not isinstance(content, list) or len(content) != 1 or content[0].get('type') != 'text' or not isinstance(content[0].get('text'), str):
                raise ManagementError('source_denied', 'The source did not return a verified knowledge context pack.')
            pack = json.loads(content[0]['text'])
            _public_text(json.dumps(pack), (self._token,))
            observed = datetime.now(timezone.utc).isoformat()
            snapshot = hashlib.sha256(json.dumps(pack, sort_keys=True).encode()).hexdigest()
            if not isinstance(pack, dict) or pack.get('prompt') != question or pack.get('budget') != 500 or not isinstance(pack.get('primary'), list) or not isinstance(pack.get('secondary'), list):
                raise ManagementError('source_denied', 'The context pack did not match this bounded query.')
            materials = []
            for entry in pack['primary'] + pack['secondary']:
                if not isinstance(entry, dict) or not isinstance(entry.get('id'), str) or not entry['id'] or not isinstance(entry.get('summary'), str) or not entry['summary'].strip() or not isinstance(entry.get('type'), str) or not isinstance(entry.get('citations'), list) or not entry['citations'] or len(entry['citations']) > 20:
                    raise ManagementError('source_denied', 'The context pack lacks verifiable related material and citations.')
                citations = []
                for citation in entry['citations']:
                    if not isinstance(citation, dict) or not isinstance(citation.get('source'), str) or not citation['source'] or type(citation.get('start')) is not int or type(citation.get('end')) is not int or not 0 <= citation['start'] <= citation['end']:
                        raise ManagementError('source_denied', 'The source returned unverifiable citation locations.')
                    citations.append('source=' + quote(citation['source'], safe='') + '&start=' + str(citation['start']) + '&end=' + str(citation['end']))
                freshness = entry.get('freshness')
                if not isinstance(freshness, dict) or any(type(freshness.get(k)) is not bool for k in ('stale', 'archived', 'contradicted')):
                    raise ManagementError('source_denied', 'The material freshness could not be verified.')
                kind = 'conflict' if freshness['contradicted'] else 'stale' if freshness['stale'] or freshness['archived'] else entry['type'] if entry['type'] in {'fact', 'inference', 'suggestion', 'conflict', 'stale'} else 'inference'
                excerpt = entry.get('excerpt', '')
                if not isinstance(excerpt, str):
                    raise ManagementError('source_denied', 'The source excerpt is not verifiable text.')
                text = '[' + entry['type'] + '] ' + entry['summary']
                if excerpt and excerpt != entry['summary']: text += '\n\n' + excerpt
                reported = entry.get('version')
                if isinstance(reported, dict):
                    reported = {k: v for k, v in reported.items() if k in {'repoRef', 'commit', 'observedAt', 'updatedAt'} and isinstance(v, str)}
                    if reported: text += '\n\nReported artifact version (source revision unverified): ' + json.dumps(reported, sort_keys=True)
                materials.append({'id': entry['id'], 'scope_id': self.corpus_scope_id, 'kind': kind, 'text': text,
                    'locator': 'mcp:' + source_id + '/artifact/' + quote(entry['id'], safe='') + '?' + ';'.join(citations),
                    'version': 'result-snapshot-sha256:' + snapshot, 'updated_at': observed, 'link_accessible': False})
            return {'status': 'conflict' if any(m['kind'] == 'conflict' for m in materials) else 'found' if materials else 'not_found',
                'materials': materials, 'searched_scope': list(scope_ids), 'requester': requester, 'observed_at': observed}
        except Exception:
            raise ManagementError('source_denied', 'The original Wiki denied or could not verify this bounded readonly query.') from None
        finally:
            if session_id:
                try:
                    with http.delete(self.url, headers=headers, timeout=(3, 3), stream=True, allow_redirects=False):
                        pass
                except Exception:
                    pass
            http.close()
