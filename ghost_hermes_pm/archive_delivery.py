"""Original-requester archive outbox: unknown native sends are never replayed."""
from .archives import _source
from .manager import ManagementError


def _query(manager,identity,query_id,data):
    query=data.get('archive_queries',{}).get(query_id)
    if not query or query['requester']!=identity.subject:
        raise ManagementError('forbidden','Archive replies belong to the original authorized requester.')
    _,grant=_source(manager,identity,query['source_id'],query['scope_ids'],data)
    if grant['revision']!=query['grant_revision']:
        raise ManagementError('forbidden','The original archive sharing grant changed; delivery needs reconciliation.')
    return query


def binding(manager,identity,query_id):
    with manager._lock:
        _,data=manager._load()
        query=_query(manager,identity,query_id,data)
        for publication in query['outbox']:
            for segment in publication['segments']:
                if segment['status']!='delivered':
                    return publication['binding'] if segment['status']=='pending' else None
        return None


def claim(manager,identity,query_id):
    with manager._lock,manager._db:
        version,data=manager._load()
        query=_query(manager,identity,query_id,data)
        for publication in query['outbox']:
            for segment in publication['segments']:
                if segment['status']=='delivered':
                    continue
                if segment['status']!='pending':
                    return None
                segment['status']='sending'
                manager._inflight.add(segment['uuid'])
                segment['attempts'].append({'status':'sending','intended_reply_to':publication['anchor']['message_id']})
                manager._save(version,data)
                return {**segment,**publication['binding'],'reply_to':publication['anchor']['message_id'],
                    'thread_id':publication['anchor'].get('thread_id'),'mention_open_id':publication['mention_open_id']}
        return None


def record(manager,identity,query_id,segment_id,receipt):
    with manager._lock,manager._db:
        version,data=manager._load()
        query=_query(manager,identity,query_id,data)
        if not isinstance(receipt,dict) or set(receipt)-{'status','code','message_id','chat_id','root_id','parent_id','thread_id'} or receipt.get('status') not in {'delivered','failed','unknown'}:
            raise ManagementError('invalid_change','A bounded original native archive message receipt is required.')
        pair=next(((p,s) for p in query['outbox'] for s in p['segments'] if s['uuid']==segment_id),None)
        if not pair or pair[1]['status']!='sending':
            raise ManagementError('binding_conflict','No inflight archive message matches this receipt.')
        publication,segment=pair
        status=receipt['status']
        if status=='delivered' and (not isinstance(receipt.get('message_id'),str) or not receipt['message_id'] or receipt.get('chat_id')!=publication['anchor']['chat_id']):
            status='unknown'
        segment['status']=status
        segment['attempts'][-1].update(receipt|{'status':status})
        manager._inflight.discard(segment_id)
        manager._save(version,data)
        return {'status':status,'query_id':query_id}


async def deliver(entry,identity,query_id,transport,generation=None):
    generation=entry.generation if generation is None else generation
    manager=entry.manager()
    while True:
        entry.require_active(generation)
        expected=binding(manager,identity,query_id)
        if expected is None:
            return
        recipient=await transport.verify_identity(expected)
        entry.require_active(generation)
        if not recipient or recipient.get('app_id')!=expected['app_id'] or recipient.get('open_id')!=expected['recipient_open_id']:
            return
        segment=claim(manager,identity,query_id)
        if segment is None:
            return
        try:
            receipt=await transport.send(segment)
            entry.require_active(generation)
        except Exception:
            entry.require_active(generation)
            receipt={'status':'unknown'}
        record(manager,identity,query_id,segment['uuid'],receipt)
        if receipt['status']!='delivered':
            return
