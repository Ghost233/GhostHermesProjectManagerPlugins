"""Explicit migration sources: archive queries never reopen an old execution entry."""
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import sqlite3

from .manager import ManagementError, VerifiedIdentity, _public_text
from .knowledge import _query_scope

KINDS = {'hermes_local', 'feishu_remote', 'codex_history'}


def _now():
    return datetime.now(timezone.utc).isoformat()


class HermesArchiveProvider:
    def __init__(self, path, session_scopes, page_size=100):
        supplied = Path(path)
        if not supplied.is_absolute() or supplied != supplied.resolve() or not supplied.is_file() or not isinstance(session_scopes, dict) or type(page_size) is not int or not 1 <= page_size <= 1000:
            raise ManagementError('invalid_change', 'An explicit existing native archive database, session scopes and bounded page size are required.')
        self.path, self.session_scopes, self.page_size = supplied, dict(session_scopes), page_size
        self.kind = 'hermes_local'

    def query(self, requester, scope_ids, question, complete=False):
        roots = list(dict.fromkeys(s for scope in scope_ids for s in self.session_scopes.get(scope, [])))
        if not roots or any(scope not in self.session_scopes for scope in scope_ids):
            raise ManagementError('forbidden', 'This migration source has no allowed session scope.')
        records, sessions, missing, pages = [], [], [], 0
        with sqlite3.connect(self.path.as_uri() + '?mode=ro', uri=True) as db:
            db.row_factory = sqlite3.Row
            db.execute('PRAGMA query_only=ON')
            db.execute('BEGIN')
            pending = list(roots)
            while pending:
                sid = pending.pop(0)
                if sid in sessions:
                    raise ManagementError('archive_incomplete', 'The compression lineage is cyclic or overlapping; coverage needs reconciliation.')
                row = db.execute('SELECT id,parent_session_id,end_reason FROM sessions WHERE id=?', (sid,)).fetchone()
                if row is None:
                    missing.append(sid)
                    continue
                sessions.append(sid)
                if row['end_reason'] == 'compression':
                    children = db.execute('SELECT id FROM sessions WHERE parent_session_id=? ORDER BY id', (sid,)).fetchall()
                    if len(children) != 1:
                        missing.append(sid + ':compression-continuation')
                    else:
                        pending.append(children[0]['id'])
                cursor = 0
                while True:
                    batch = db.execute('SELECT id,session_id,role,content,timestamp,active,_compressed_summary FROM messages WHERE session_id=? AND id>? ORDER BY id LIMIT ?', (sid,cursor,self.page_size)).fetchall()
                    if not batch:
                        break
                    pages += 1
                    for message in batch:
                        text = message['content']
                        if not isinstance(text, str):
                            missing.append(sid + ':message-' + str(message['id']))
                            continue
                        if text.startswith('\x00json:'):
                            text = json.dumps(json.loads(text[6:]), ensure_ascii=False)
                        records.append({'id': str(message['id']), 'session_id': sid, 'role': message['role'], 'text': text,
                            'timestamp': message['timestamp'], 'active': bool(message['active']), 'compressed_summary': bool(message['_compressed_summary']),
                            'locator': 'hermes-archive:' + sid + '#message-' + str(message['id'])})
                    cursor = batch[-1]['id']
                    if len(records) > 50000:
                        raise ManagementError('archive_incomplete', 'Archive coverage exceeds the bounded complete-read budget.')
            version = hashlib.sha256(json.dumps({'sessions':sessions,'records':records},sort_keys=True).encode()).hexdigest()
        selected = records if complete else [r for r in records if question.casefold() in r['text'].casefold()]
        return {'status':'incomplete' if missing else 'complete' if complete else 'found' if selected else 'not_found',
            'kind':self.kind,'records':selected,'requester':requester,'searched_scope':list(scope_ids),
            'coverage':{'session_ids':sessions,'pages':pages,'end_confirmed':not missing,'compressed_history':'included','missing':missing},
            'source_version':'sha256:'+version,'observed_at':_now(),'old_entry':'not_started'}


def register_source(manager, identity, registration):
    fields = {'id','kind','provider_ref','grant_source_id','new_profile_id','scope_ids','authorization_ref'}
    with manager._lock, manager._db:
        version,data = manager._load()
        if manager._principal(identity,data) is not None:
            raise ManagementError('forbidden','Migration and extra original archive sources require the verified owner.')
        if not isinstance(registration,dict) or set(registration) != fields or registration.get('kind') not in KINDS or any(not isinstance(registration.get(k),str) or not registration[k] for k in fields - {'scope_ids'}):
            raise ManagementError('invalid_change','An explicit archive kind, provider, migration Profile, source grant and owner authorization reference are required.')
        profile = data['profiles'].get(registration['new_profile_id'])
        if not profile:
            raise ManagementError('invalid_change','Register the new Profile before its own migration source.')
        actor = VerifiedIdentity(profile['identity_ref'],'registered-migration-scope')
        _query_scope(manager,actor,registration['grant_source_id'],registration['scope_ids'],data)
        _public_text(json.dumps(registration),manager._sensitive_values())
        sources=data.setdefault('archive_sources',{})
        existing=sources.get(registration['id'])
        if existing:
            if any(existing.get(k)!=v for k,v in registration.items()):
                raise ManagementError('binding_conflict','An archive source ID cannot silently rebind migration or source authority.')
            return existing
        sources[registration['id']]={**registration,'new_identity_ref':profile['identity_ref'],'registered_at':_now(),
            'protection':{'status':'unverified','reason':'Native automatic cleanup protection has not been proved.'}}
        manager._save(version,data)
        return sources[registration['id']]


def query_archive(manager,identity,source_id,query_id,question,scope_ids,complete=False):
    with manager._lock:
        version,data=manager._load()
        principal=manager._principal(identity,data)
        source=data.setdefault('archive_sources',{}).get(source_id)
        if not source or (principal is not None and (principal['id']!=source['new_profile_id'] or identity.subject!=source['new_identity_ref'])):
            raise ManagementError('forbidden','A Profile can read only its explicitly authorized own migration source; extra raw sources need owner grants.')
        _query_scope(manager,identity,source['grant_source_id'],scope_ids,data)
        if not set(scope_ids)<=set(source['scope_ids']) or type(complete)is not bool or not isinstance(query_id,str) or not query_id:
            raise ManagementError('forbidden','The requested archive scope and complete-history intent must be explicit.')
        _public_text(question,manager._sensitive_values())
        queries=data.setdefault('archive_queries',{})
        inputs={'source_id':source_id,'requester':identity.subject,'question':question,'scope_ids':list(scope_ids),'complete':complete}
        existing=queries.get(query_id)
        if existing:
            if any(existing.get(k)!=v for k,v in inputs.items()):
                raise ManagementError('binding_conflict','The archive query ID already identifies another scope.')
            return existing
        provider=manager.archive_providers.get(source['provider_ref'])
        if provider is None or provider.kind!=source['kind']:
            raise ManagementError('archive_unavailable','The exact registered archive adapter is unavailable; no old entry was started.')
        query={'id':query_id,**inputs,'status':'reading','records':[],'created_at':_now()}
        queries[query_id]=query
        with manager._db: manager._save(version,data)
    result=provider.query(identity.subject,scope_ids,question,complete)
    if result.get('requester')!=identity.subject or result.get('kind')!=source['kind'] or result.get('searched_scope')!=scope_ids:
        raise ManagementError('archive_incomplete','The archive reader did not prove the original requester and exact source scope.')
    _public_text(json.dumps(result),manager._sensitive_values())
    with manager._lock,manager._db:
        version,data=manager._load()
        _query_scope(manager,identity,source['grant_source_id'],scope_ids,data)
        stored=data['archive_queries'][query_id]
        stored.update(result)
        manager._save(version,data)
        return stored


def snapshot_archives(manager,identity,data):
    return {'archive_sources':[s for s in data.get('archive_sources',{}).values() if identity.subject in {manager.owner_identity_ref,s['new_identity_ref']}],
            'archive_queries':[q for q in data.get('archive_queries',{}).values() if identity.subject in {manager.owner_identity_ref,q['requester']}]}
