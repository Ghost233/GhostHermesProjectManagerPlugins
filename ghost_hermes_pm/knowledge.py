"""Read-only registered knowledge, with original-requester and sharing grants."""
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import re

from .manager import ManagementError, _public_text

KINDS = {'fact', 'inference', 'suggestion', 'conflict', 'stale'}


def _now():
    return datetime.now(timezone.utc).isoformat()


def _digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True).encode()).hexdigest()


class LocalKnowledgeProvider:
    """Query explicitly configured local documents; never enumerate or rewrite a Wiki."""
    def __init__(self, root, documents):
        self.root = Path(root).resolve(strict=True)
        if not self.root.is_dir() or not isinstance(documents, list):
            raise ManagementError('invalid_change', 'An existing registered local source directory and document list are required.')
        self.documents = []
        seen = set()
        for document in documents:
            if not isinstance(document, dict) or set(document) != {'id', 'path', 'scope_id', 'kind', 'terms'} or document.get('id') in seen or document.get('kind') not in KINDS or not isinstance(document.get('terms'), list):
                raise ManagementError('invalid_change', 'Local material requires a unique ID, relative path, scope, classification and query terms.')
            path = self.root / document['path']
            if Path(document['path']).is_absolute() or '..' in Path(document['path']).parts or path != path.resolve() or not path.is_relative_to(self.root) or not path.is_file():
                raise ManagementError('invalid_change', 'Local material must be an existing registered regular file without aliases.')
            seen.add(document['id'])
            self.documents.append(dict(document))

    def query(self, *, requester, source_id, question, scope_ids):
        # The backend receives the original query subject, never the Wiki identity.
        tokens = [t.casefold() for t in re.findall(r'[\w-]+', question)]
        materials = []
        for document in self.documents:
            if document['scope_id'] not in scope_ids or not any(term.casefold() in question.casefold() for term in document['terms']):
                continue
            path = self.root / document['path']
            if path != path.resolve() or not path.is_file() or path.stat().st_size > 100000:
                raise ManagementError('source_denied', 'The registered source location changed or exceeds its bounded scope.')
            raw = path.read_bytes()
            text = raw.decode('utf-8')
            paragraphs = []
            current, start = [], 1
            for number, line in enumerate(text.splitlines() + [''], 1):
                if line.strip():
                    if not current:
                        start = number
                    current.append(line)
                elif current:
                    block = '\n'.join(current).strip()
                    paragraphs.append((sum(t in block.casefold() for t in tokens), start, block))
                    current = []
            if not paragraphs:
                continue
            _, line, material = max(paragraphs, key=lambda p: (p[0], len(p[2])))
            materials.append({'id': document['id'] + ':' + str(line), 'scope_id': document['scope_id'],
                'text': material, 'kind': document['kind'], 'locator': 'local:' + source_id + '/' + document['path'] + '#L' + str(line),
                'version': 'sha256:' + hashlib.sha256(raw).hexdigest(),
                'updated_at': datetime.fromtimestamp(path.stat().st_mtime, timezone.utc).isoformat(),
                'link_accessible': False})
        return {'status': 'found' if materials else 'not_found', 'materials': materials, 'searched_scope': list(scope_ids),
                'observed_at': _now(), 'requester': requester}


def register_source(manager, identity, expected_version, source):
    allowed = {'id', 'name', 'provider_ref', 'wiki_profile_id', 'query_subjects', 'public_channels', 'task_profiles', 'wiki_bindings'}
    with manager._lock, manager._db:
        version, data = manager._load()
        if manager._principal(identity, data) is not None:
            raise ManagementError('forbidden', 'Source and sharing grants require the verified owner.')
        if type(expected_version) is not int or expected_version != version:
            raise ManagementError('version_conflict', 'Read the current source directory before changing grants.')
        if not isinstance(source, dict) or set(source) != allowed or any(not isinstance(source.get(k), str) or not source[k] for k in ('id', 'name', 'provider_ref', 'wiki_profile_id')):
            raise ManagementError('invalid_change', 'Explicit source, provider, Wiki and requester/share scopes are required.')
        wiki = data['profiles'].get(source['wiki_profile_id'])
        if not wiki or wiki['role'] != 'independent' or wiki['capability'] != 'non_development':
            raise ManagementError('invalid_change', 'A separately registered independent non-development Wiki is required.')
        subjects = source['query_subjects']
        known = {manager.owner_identity_ref} | {p['identity_ref'] for p in data['profiles'].values()}
        if not isinstance(subjects, dict) or not subjects or any(subject not in known or not isinstance(scopes, list) or not scopes or any(not isinstance(s, str) or not s for s in scopes) for subject, scopes in subjects.items()) or any(not isinstance(source[k], list) for k in ('public_channels', 'task_profiles', 'wiki_bindings')):
            raise ManagementError('invalid_change', 'Named registered query subjects and explicit material/share scopes are required.')
        _public_text(json.dumps(source), manager._sensitive_values())
        registered = {**json.loads(json.dumps(source)), 'wiki_identity_ref': wiki['identity_ref'], 'registered_by': identity.subject, 'registered_at': _now()}
        registered['revision'] = _digest(source | {'wiki_identity_ref': wiki['identity_ref']})
        data.setdefault('knowledge_sources', {})[source['id']] = registered
        manager._save(version, data)
        return {'status': 'registered', 'source': registered, 'version': version + 1}


def _query_scope(manager, identity, source_id, scope_ids, data):
    manager._principal(identity, data)
    source = data.get('knowledge_sources', {}).get(source_id)
    if not source or not isinstance(scope_ids, list) or not scope_ids or any(not isinstance(s, str) for s in scope_ids) or len(set(scope_ids)) != len(scope_ids):
        raise ManagementError('forbidden', 'The requested source or explicit material scope is not registered.')
    allowed = source['query_subjects'].get(identity.subject, [])
    if not set(scope_ids) <= set(allowed):
        raise ManagementError('forbidden', 'The original requester lacks this source scope; Wiki or superior privileges cannot substitute it.')
    wiki = data['profiles'].get(source['wiki_profile_id'])
    if not wiki or wiki['identity_ref'] != source['wiki_identity_ref'] or wiki['role'] != 'independent':
        raise ManagementError('source_denied', 'The registered source identity changed; reconcile its grants.')
    return source


def _validate_result(manager, result, query):
    fields = {'id', 'scope_id', 'text', 'kind', 'locator', 'version', 'updated_at', 'link_accessible'}
    if not isinstance(result, dict) or result.get('status') not in {'found', 'not_found', 'denied', 'conflict'} or result.get('requester') != query['requester'] or result.get('searched_scope') != query['scope_ids'] or not isinstance(result.get('materials'), list) or len(result['materials']) > 20:
        raise ManagementError('source_denied', 'The source did not confirm the original requester and actual query scope.')
    seen = set()
    for material in result['materials']:
        if not isinstance(material, dict) or set(material) != fields or material.get('id') in seen or material.get('scope_id') not in query['scope_ids'] or material.get('kind') not in KINDS or type(material.get('link_accessible')) is not bool or any(not isinstance(material.get(k), str) or not material[k] for k in ('id', 'text', 'locator', 'version', 'updated_at')):
            raise ManagementError('source_denied', 'Related source material needs an allowed scope, classification, obtainable location, version and time.')
        seen.add(material['id'])
        _public_text(json.dumps(material), manager._sensitive_values())
    if result['status'] == 'not_found' and result['materials'] or result['status'] == 'found' and not result['materials']:
        raise ManagementError('source_denied', 'The source result contradicts its actual material coverage.')
    return result


def query_knowledge(manager, identity, source_id, query_id, question, scope_ids, request_id=None, channel_id=None, auto_supplement=False):
    with manager._lock:
        version, data = manager._load()
        source = _query_scope(manager, identity, source_id, scope_ids, data)
        if not isinstance(query_id, str) or not query_id or len(query_id) > 256 or type(auto_supplement) is not bool:
            raise ManagementError('invalid_change', 'A stable query ID and explicit query intent are required.')
        _public_text(question, manager._sensitive_values())
        query_input = {'source_id': source_id, 'question': question, 'scope_ids': list(scope_ids),
                       'requester': identity.subject, 'request_id': request_id, 'channel_id': channel_id, 'auto_supplement': auto_supplement}
        queries = data.setdefault('knowledge_queries', {})
        existing = queries.get(query_id)
        if existing:
            if any(existing.get(k) != v for k, v in query_input.items()):
                raise ManagementError('binding_conflict', 'This query ID already identifies another request or scope.')
            return existing
        if request_id is not None or channel_id is not None or auto_supplement:
            raise ManagementError('unsupported', 'Public/task knowledge handoff requires a registered message association.')
        provider = manager.knowledge_providers.get(source['provider_ref'])
        if provider is None:
            raise ManagementError('source_unavailable', 'The registered source adapter is unavailable; no material was invented.')
        query = {'id': query_id, **query_input, 'source_revision': source['revision'], 'status': 'source_inflight',
                 'created_at': _now(), 'materials': [], 'outbox': [], 'source_anchor': None}
        queries[query_id] = query
        with manager._db:
            manager._save(version, data)
    try:
        result = provider.query(requester=identity.subject, source_id=source_id, question=question, scope_ids=list(scope_ids))
        result = _validate_result(manager, result, query)
    except Exception:
        result = {'status': 'source_denied', 'materials': [], 'searched_scope': list(scope_ids),
                  'requester': identity.subject, 'observed_at': _now(), 'reason': 'The registered source denied or could not verify this bounded query.'}
    with manager._lock, manager._db:
        version, data = manager._load()
        current = _query_scope(manager, identity, source_id, scope_ids, data)
        stored = data['knowledge_queries'][query_id]
        if current['revision'] != query['source_revision']:
            stored.update(status='grant_changed', materials=[], reason='Query/share grants changed; material was not published.')
        else:
            stored.update(result)
        manager._save(version, data)
        return stored


def snapshot_knowledge(identity, data):
    sources = data.get('knowledge_sources', {})
    visible = []
    for query in data.get('knowledge_queries', {}).values():
        if query['requester'] != identity.subject:
            continue
        source = sources.get(query['source_id'], {})
        allowed = source.get('query_subjects', {}).get(identity.subject, [])
        if set(query['scope_ids']) <= set(allowed):
            visible.append(query)
        else:
            visible.append({k: query[k] for k in ('id', 'source_id', 'requester', 'scope_ids', 'created_at')} |
                           {'status': 'grant_revoked', 'materials': []})
    return {'knowledge_sources': [s for s in sources.values() if identity.subject in s['query_subjects'] or identity.subject in {s['wiki_identity_ref'], s.get('registered_by')}],
            'knowledge_queries': visible}
