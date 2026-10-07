"""Explicit migration sources. Reads never reopen a Profile, bot or executor."""
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import sqlite3

from .manager import ManagementError, VerifiedIdentity, _public_text
from .knowledge import _query_scope, _channel, _publication, NAMESPACE

KINDS = {'hermes_local', 'feishu_remote', 'codex_history'}
RESULT_FIELDS = {'status','kind','records','requester','searched_scope','coverage','source_version','observed_at','old_entry','reason'}


def _now():
    return datetime.now(timezone.utc).isoformat()


def _digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False).encode()).hexdigest()


def provider_binding(provider):
    if isinstance(provider,HermesArchiveProvider):
        stat=provider.path.stat()
        return _digest({'kind':provider.kind,'path':str(provider.path),'file_identity':[stat.st_dev,stat.st_ino],
            'sessions':provider.session_scopes,'files':provider.files})
    from .archive_sources import FeishuArchiveProvider,CodexArchiveProvider
    if isinstance(provider,FeishuArchiveProvider):
        return _digest({'kind':provider.kind,'binding':provider.binding,'chats':provider.chat_scopes})
    if isinstance(provider,CodexArchiveProvider):
        adapter=provider.adapter
        return _digest({'kind':provider.kind,'threads':provider.thread_scopes,'service_ref':adapter.service_ref,
            'endpoint_ref':adapter.endpoint_ref,'source_kind':adapter.source_kind})
    return None


def _fork(row):
    """Match native _is_explicit_fork_child_row(include_reset=True), without SDK startup."""
    if row.get('source') == 'tool':
        return True
    config = row.get('model_config')
    if isinstance(config, str):
        try:
            config = json.loads(config)
        except ValueError:
            config = None
    return isinstance(config, dict) and row.get('parent_session_id') in [config.get(k) for k in ('_branched_from','_delegate_from','_reset_from')] and bool(row.get('parent_session_id'))


def _result(kind, requester, scopes, question, complete, records, coverage, version):
    if complete:
        selected = records
    else:
        hits = {i for i, row in enumerate(records) if question.casefold() in row['text'].casefold()}
        selected = [row for i, row in enumerate(records) if any(abs(i-hit)<=1 for hit in hits)]
    return {'status':'incomplete' if not coverage['end_confirmed'] else 'complete' if complete else 'found' if selected else 'not_found',
        'kind':kind,'records':selected,'requester':requester,'searched_scope':list(scopes),
        'coverage':coverage | {'original_rows':len(records),'selected_rows':len(selected)},
        'source_version':'sha256:'+version,'observed_at':_now(),'old_entry':'not_started'}


class HermesArchiveProvider:
    def __init__(self, path, session_scopes, page_size=100, files=None):
        supplied = Path(path)
        if not supplied.is_absolute() or supplied != supplied.resolve() or not supplied.is_file() or not isinstance(session_scopes, dict) or not session_scopes or any(not isinstance(k,str) or not isinstance(v,list) or not v or any(not isinstance(s,str) or not s for s in v) for k,v in session_scopes.items()) or type(page_size) is not int or not 1 <= page_size <= 1000:
            raise ManagementError('invalid_change', 'An explicit existing native archive database, session scopes and bounded page size are required.')
        self.path, self.session_scopes, self.page_size = supplied, dict(session_scopes), page_size
        self.files = files or {}
        if not isinstance(self.files,dict) or any(not isinstance(rows,list) or any(not isinstance(row,dict) or set(row)!={'id','path'} or any(not isinstance(row.get(k),str) or not row[k] for k in ('id','path')) for row in rows) for rows in self.files.values()):
            raise ManagementError('invalid_change','Sealed archive materials require explicit stable IDs and approved absolute files per scope.')
        self.kind = 'hermes_local'

    def read(self, scope_ids):
        roots = list(dict.fromkeys(s for scope in scope_ids for s in self.session_scopes.get(scope, [])))
        if not roots or any(scope not in self.session_scopes for scope in scope_ids):
            raise ManagementError('forbidden', 'This migration source has no allowed session scope.')
        records, sessions, missing, pages = [], [], [], 0
        with sqlite3.connect(self.path.as_uri() + '?mode=ro', uri=True) as db:
            db.row_factory = sqlite3.Row
            db.execute('PRAGMA query_only=ON')
            db.execute('BEGIN')
            pending = []
            for sid in roots:
                seen = set()
                while True:
                    row = db.execute('SELECT * FROM sessions WHERE id=?', (sid,)).fetchone()
                    if row is None:
                        missing.append(sid)
                        break
                    row = dict(row)
                    if sid in seen:
                        missing.append(sid+':compression-cycle')
                        break
                    seen.add(sid)
                    parent = db.execute('SELECT * FROM sessions WHERE id=?', (row.get('parent_session_id'),)).fetchone()
                    if row.get('parent_session_id') and parent is None and not _fork(row):
                        missing.append(sid+':parent-session-unavailable')
                    if not _fork(row) and parent is not None and parent['end_reason']=='compression':
                        sid = parent['id']
                    else:
                        pending.append(sid)
                        break
            seen = set()
            while pending:
                sid = pending.pop(0)
                if sid in seen:
                    continue
                seen.add(sid)
                row = dict(db.execute('SELECT * FROM sessions WHERE id=?', (sid,)).fetchone())
                sessions.append(row)
                if row['end_reason'] == 'compression':
                    children = [dict(r) for r in db.execute('SELECT * FROM sessions WHERE parent_session_id=? ORDER BY id', (sid,)) if not _fork(dict(r))]
                    if len(children) != 1 or children[0]['id'] in seen:
                        missing.append(sid + ':compression-continuation')
                    else:
                        pending.append(children[0]['id'])
                cursor = 0
                while True:
                    batch = db.execute('SELECT * FROM messages WHERE session_id=? AND id>? ORDER BY id LIMIT ?', (sid,cursor,self.page_size)).fetchall()
                    if not batch:
                        break
                    pages += 1
                    for message in batch:
                        payload = {key: {'encoding':'hex','value':value.hex()} if isinstance(value,bytes) and key=='display_identity' else value for key,value in dict(message).items()}
                        text = payload.get('content')
                        if text is None:
                            text = ''
                        if not isinstance(text, str):
                            missing.append(sid + ':message-' + str(message['id']))
                            continue
                        if text.startswith('\x00json:'):
                            text = json.dumps(json.loads(text[6:]), ensure_ascii=False)
                        # Preserve tool, compaction and model metadata as original evidence, not instructions.
                        records.append({'id':str(message['id']),'session_id':sid,'role':payload['role'],'text':text,
                            'timestamp':payload['timestamp'],'active':bool(payload.get('active',1)),
                            'compressed_summary':bool(payload.get('_compressed_summary',0)), 'compacted':bool(payload.get('compacted',0)),
                            'payload':payload,'locator':'hermes-archive:'+sid+'#message-'+str(message['id'])})
                    cursor = batch[-1]['id']
                    if len(records)>50000:
                        raise ManagementError('archive_incomplete','Archive coverage exceeds the bounded complete-read budget; no complete result was claimed.')
            all_ids = [r[0] for r in db.execute('SELECT id FROM sessions ORDER BY id')]
        file_versions=[]
        seen_files=set()
        for scope in scope_ids:
            for document in self.files.get(scope,[]):
                if document['id'] in seen_files:
                    continue
                seen_files.add(document['id'])
                path=Path(document['path'])
                if not path.is_absolute() or path!=path.resolve() or not path.is_file():
                    raise ManagementError('archive_incomplete','An explicitly registered sealed file is missing or changed its ordinary-file binding.')
                before=path.stat()
                with path.open('rb') as stream:
                    raw=stream.read(10*1024*1024+1)
                after=path.stat()
                if (before.st_dev,before.st_ino,before.st_size,before.st_mtime_ns)!=(after.st_dev,after.st_ino,after.st_size,after.st_mtime_ns):
                    raise ManagementError('archive_incomplete','A sealed file changed during its bounded read; consistent coverage is unverified.')
                if len(raw)>10*1024*1024:
                    raise ManagementError('archive_incomplete','The sealed material exceeds the bounded original-file read budget.')
                text=raw.decode('utf-8')
                version=hashlib.sha256(raw).hexdigest()
                file_versions.append({'id':document['id'],'version':'sha256:'+version,'size':len(raw)})
                records.append({'id':'file:'+document['id'],'text':text,'timestamp':before.st_mtime,'locator':'archive-file:'+document['id'],'file_version':'sha256:'+version})
        return records, sessions, {'session_ids':[s['id'] for s in sessions],'pages':pages,'end_confirmed':not missing,
            'compressed_history':'included','missing':missing,'sealed_files':file_versions}, all_ids

    def query(self, requester, scope_ids, question, complete=False):
        records, sessions, coverage, _ = self.read(scope_ids)
        return _result(self.kind,requester,scope_ids,question,complete,records,coverage,_digest({'sessions':sessions,'records':records}))


def _source(manager, identity, source_id, scope_ids, data):
    principal = manager._principal(identity,data)
    source = data.get('archive_sources',{}).get(source_id)
    if not source or (principal is not None and (principal['id']!=source['new_profile_id'] or identity.subject!=source['new_identity_ref'])):
        raise ManagementError('forbidden','A Profile can read only its explicitly authorized own migration source; extra raw sources need owner grants.')
    grant = _query_scope(manager,identity,source['grant_source_id'],scope_ids,data)
    if not set(scope_ids)<=set(source['scope_ids']):
        raise ManagementError('forbidden','This material scope is outside the explicit migration grant.')
    return source, grant


def register_source(manager, identity, registration):
    fields = {'id','kind','provider_ref','grant_source_id','new_profile_id','scope_ids','authorization_ref'}
    with manager._lock, manager._db:
        version,data = manager._load()
        if manager._principal(identity,data) is not None:
            raise ManagementError('forbidden','Migration and extra original archive sources require the verified owner.')
        if not isinstance(registration,dict) or set(registration)!=fields or registration.get('kind') not in KINDS or any(not isinstance(registration.get(k),str) or not registration[k] for k in fields-{'scope_ids'}):
            raise ManagementError('invalid_change','An explicit archive kind, provider, migration Profile, source grant and owner authorization reference are required.')
        profile = data['profiles'].get(registration['new_profile_id'])
        if not profile:
            raise ManagementError('invalid_change','Register the new Profile before its own migration source.')
        _query_scope(manager,VerifiedIdentity(profile['identity_ref'],'registered-migration-scope'),registration['grant_source_id'],registration['scope_ids'],data)
        _public_text(json.dumps(registration),manager._sensitive_values())
        sources=data.setdefault('archive_sources',{})
        existing=sources.get(registration['id'])
        if existing:
            if any(existing.get(k)!=v for k,v in registration.items()):
                raise ManagementError('binding_conflict','An archive source ID cannot silently rebind migration or source authority.')
            return existing
        sources[registration['id']]={**registration,'new_identity_ref':profile['identity_ref'],'registered_at':_now(),'provider_binding':provider_binding(manager.archive_providers.get(registration['provider_ref'])),
            'protection':{'status':'unverified','reason':'Native automatic cleanup protection has not been proved.'},
            'external_restore':{'status':'unverified','reason':'No original external-service restore capability has been verified.'}}
        manager._save(version,data)
        return sources[registration['id']]


def _validate_result(manager, result, source, query):
    if not isinstance(result,dict) or set(result)-RESULT_FIELDS or result.get('requester')!=query['requester'] or result.get('kind')!=source['kind'] or result.get('searched_scope')!=query['scope_ids'] or result.get('status') not in {'complete','incomplete','found','not_found'} or result.get('old_entry')!='not_started' or not isinstance(result.get('records'),list) or not isinstance(result.get('coverage'),dict) or type(result['coverage'].get('end_confirmed')) is not bool or not result.get('source_version') or not result.get('observed_at'):
        raise ManagementError('archive_incomplete','The archive reader did not prove the original requester, coverage and exact source scope.')
    if result['status']=='complete' and (not query['complete'] or not result['coverage']['end_confirmed']):
        raise ManagementError('archive_incomplete','A complete result requires confirmed coverage and the explicit complete request.')
    for record in result['records']:
        if not isinstance(record,dict) or any(not isinstance(record.get(k),str) or not record[k] for k in ('id','locator')) or not isinstance(record.get('text'),str) or 'timestamp' not in record:
            raise ManagementError('archive_incomplete','Actual archive records require text, locator and original timestamp.')
    _public_text(json.dumps(result),manager._sensitive_values())
    return result


def query_archive(manager,identity,source_id,query_id,question,scope_ids,complete=False,channel_id=None,anchor=None):
    from .manager import _message_anchor
    with manager._lock:
        version,data=manager._load()
        source,grant=_source(manager,identity,source_id,scope_ids,data)
        if type(complete)is not bool or not isinstance(query_id,str) or not query_id or len(query_id)>256:
            raise ManagementError('invalid_change','A stable query ID and explicit complete-history intent are required.')
        _public_text(question,manager._sensitive_values())
        channel=None
        if channel_id is not None or anchor is not None:
            channel=_channel(grant,channel_id,identity.subject,scope_ids)
            _message_anchor(anchor)
            if any(anchor.get(k)!=channel[k] for k in NAMESPACE):
                raise ManagementError('forbidden','The original archive query is outside the approved public namespace.')
        inputs={'source_id':source_id,'requester':identity.subject,'question':question,'scope_ids':list(scope_ids),'complete':complete,'channel_id':channel_id,'source_anchor':anchor}
        queries=data.setdefault('archive_queries',{})
        existing=queries.get(query_id)
        if existing:
            anchor_keys=NAMESPACE+('tenant_key','sender_open_id','message_id')
            previous=existing.get('source_anchor')
            same_anchor=previous is None and anchor is None or isinstance(previous,dict) and isinstance(anchor,dict) and all(previous.get(k)==anchor.get(k) for k in anchor_keys)
            if any(existing.get(k)!=v for k,v in inputs.items() if k!='source_anchor') or not same_anchor:
                raise ManagementError('binding_conflict','The archive query ID already identifies another scope.')
            # Preserve the original optional locator and any unknown delivery, including legacy IDs.
            return existing
        provider=manager.archive_providers.get(source['provider_ref'])
        query={'id':query_id,**inputs,'grant_revision':grant['revision'],'status':'reading','records':[],'outbox':[],'created_at':_now()}
        queries[query_id]=query
        with manager._db: manager._save(version,data)
    try:
        if provider is None or provider.kind!=source['kind']:
            raise ManagementError('archive_unavailable','The exact registered archive adapter is unavailable; no old entry was started.')
        if provider_binding(provider)!=source.get('provider_binding'):
            raise ManagementError('binding_conflict','The registered original archive provider binding changed; owner reconciliation is required.')
        result=_validate_result(manager,provider.query(identity.subject,scope_ids,question,complete),source,query)
        if provider_binding(provider)!=source.get('provider_binding'):
            raise ManagementError('binding_conflict','The original archive binding changed during the read.')
    except Exception as exc:
        result={'status':'blocked','records':[],'reason':str(exc) if isinstance(exc,ManagementError) else 'Archive read failed; complete coverage remains unverified.',
            'kind':source['kind'],'old_entry':'not_started'}
    with manager._lock,manager._db:
        version,data=manager._load()
        stored=data['archive_queries'][query_id]
        try:
            _,current=_source(manager,identity,source_id,scope_ids,data)
            if current['revision']!=query['grant_revision']:
                raise ManagementError('forbidden','The migration source grants changed during the read.')
        except ManagementError:
            result={'status':'grant_changed','records':[],'reason':'Source grants changed; no private archive material was published.'}
            channel=None
        stored.update(result)
        if channel:
            text='档案结果 '+query_id+'\n来源：'+source_id+' · '+source['kind']+'\n状态：'+stored['status']+'\n'+stored.get('reason','')
            text+='\n覆盖：'+json.dumps(stored.get('coverage',{}),ensure_ascii=False)
            for row in stored['records']:
                text+='\n'+row['locator']+' · '+str(row['timestamp'])+' · '+stored['source_version']+'\n'+row['text']
            _publication(stored,'result',text,channel,anchor,anchor['sender_open_id'])
        manager._save(version,data)
        return stored


def snapshot_archives(manager,identity,data):
    sources=data.get('archive_sources',{})
    queries=[]
    visible_ids=set()
    for sid,source in sources.items():
        if identity.subject in {manager.owner_identity_ref,source['new_identity_ref']}:
            visible_ids.add(sid)
    for query in data.get('archive_queries',{}).values():
        source=sources.get(query['source_id'],{})
        grant=data.get('knowledge_sources',{}).get(source.get('grant_source_id'),{})
        channel=next((c for c in grant.get('public_channels',[]) if c['id']==query.get('channel_id')),None)
        public=bool(channel and identity.subject in channel['view_subjects'] and set(query['scope_ids'])<=set(channel['scope_ids']))
        allowed=set(query['scope_ids'])<=set(grant.get('query_subjects',{}).get(identity.subject,[]))
        if query['requester']!=identity.subject and not public:
            continue
        queries.append(query if public or allowed else {k:query[k] for k in ('id','source_id','requester','scope_ids','created_at')}|{'status':'grant_revoked','records':[]})
    visible_sources=[]
    for sid,source in sources.items():
        if sid not in visible_ids:
            continue
        protection=dict(source['protection'])
        protection.update(permanent_protection='unverified',ordinary_tool_read_write_delete_boundary='unverified',original_source_cleanup='not_run',current_native_pins='unverified')
        if protection.get('status')=='verified_native_cleanup_copy':
            try:
                _source(manager,identity,sid,source['scope_ids'],data)
                provider=manager.archive_providers.get(source['provider_ref'])
                if not isinstance(provider,HermesArchiveProvider) or provider_binding(provider)!=source.get('provider_binding'):
                    raise ManagementError('binding_conflict','The current original source binding is unverified.')
                _,sessions,coverage,_=provider.read(source['scope_ids'])
                if coverage['end_confirmed'] and all(row.get('pinned') for row in sessions):
                    protection['current_native_pins']='verified'
                else:
                    protection['reason']='Current native pin coverage changed; the past cleanup-copy proof cannot establish present protection.'
            except Exception:
                protection['reason']='Current native pin/source/grant coverage is unavailable; original protection remains unverified.'
        visible_sources.append(source|{'protection':protection})
    return {'archive_sources':visible_sources,'archive_queries':queries,
        'archive_backups':[b for b in data.get('archive_backups',{}).values() if identity.subject==manager.owner_identity_ref],
        'archive_restores':[r for r in data.get('archive_restores',{}).values() if identity.subject==manager.owner_identity_ref]}


def configured_providers(config, *, state_dir=None, credential_resolver=None):
    if not config:
        return {}
    if not isinstance(config, dict):
        raise ManagementError('invalid_change', 'Archive providers require explicit trusted original source references.')
    from .archive_sources import FeishuArchiveProvider, CodexArchiveProvider, verify_feishu_source
    from .observation import configured_observation_adapters
    result = {}
    for ref, value in config.items():
        if not isinstance(ref, str) or not ref.startswith('local:') or not isinstance(value, dict):
            raise ManagementError('invalid_change', 'Archive configuration needs explicit original source references.')
        kind = value.get('kind', 'hermes_local')
        if kind == 'hermes_local':
            if set(value) - {'kind', 'path', 'session_scopes', 'files'} or not {'path', 'session_scopes'} <= set(value):
                raise ManagementError('invalid_change', 'Local archives require an approved native database and session manifest.')
            result[ref] = HermesArchiveProvider(value['path'], value['session_scopes'], files=value.get('files'))
        elif kind == 'feishu_remote':
            if set(value) != {'kind', 'binding', 'chat_scopes', 'credential_ref'} or not isinstance(value['credential_ref'], str) or not value['credential_ref'].startswith('native:'):
                raise ManagementError('invalid_change', 'Remote archives require exact app/tenant/bot scopes and a native credential reference.')
            secret = credential_resolver(value['credential_ref']) if callable(credential_resolver) else None
            if not secret:
                raise ManagementError('capability_unverified', 'The original remote archive native credential is unavailable.')
            from lark_oapi import Client
            binding = value['binding']
            if not isinstance(binding, dict) or not isinstance(binding.get('app_id'), str):
                raise ManagementError('invalid_change', 'The original remote app identity is required.')
            client = Client.builder().app_id(binding['app_id']).app_secret(secret).build()
            result[ref] = FeishuArchiveProvider(client, binding, value['chat_scopes'], lambda native, binding=dict(binding): verify_feishu_source(native, binding))
        elif kind == 'codex_history':
            if set(value) != {'kind', 'adapter', 'thread_scopes'} or state_dir is None:
                raise ManagementError('invalid_change', 'Codex archives require an original read-only proxy configuration and trusted evidence directory.')
            adapters = configured_observation_adapters([value['adapter']], state_dir)
            result[ref] = CodexArchiveProvider(adapters[value['adapter']['service_ref']], value['thread_scopes'])
        else:
            raise ManagementError('invalid_change', 'Unknown original archive source kind.')
    return result
