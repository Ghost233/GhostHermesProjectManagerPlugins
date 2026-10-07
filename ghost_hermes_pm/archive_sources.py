"""Read-only Feishu pagination and original-executor Codex history adapters."""
import json

from .archives import _result, _digest
from .manager import ManagementError
from .observation import ReadOnlyCodexAdapter


class FeishuArchiveProvider:
    kind = 'feishu_remote'

    def __init__(self, client, binding, chat_scopes, identity_verifier=None):
        if not isinstance(binding,dict) or set(binding)!={'app_id','tenant_key','bot_open_id'} or any(not isinstance(v,str) or not v for v in binding.values()) or not isinstance(chat_scopes,dict):
            raise ManagementError('invalid_change','Remote archives need the exact registered app, tenant, bot and chat scope manifest.')
        self.client,self.binding,self.chat_scopes,self.identity_verifier=client,dict(binding),dict(chat_scopes),identity_verifier

    def query(self,requester,scope_ids,question,complete=False):
        from lark_oapi.api.im.v1 import ListMessageRequest
        proof=self.identity_verifier(self.client) if callable(self.identity_verifier) else None
        if not isinstance(proof,dict) or any(proof.get(k)!=v for k,v in self.binding.items()) or not proof.get('evidence_ref') or getattr(getattr(self.client,'config',None),'app_id',None)!=self.binding['app_id']:
            raise ManagementError('capability_unverified','The current original app/tenant source identity has not been verified by the trusted adapter.')
        chats=list(dict.fromkeys(c for s in scope_ids for c in self.chat_scopes.get(s,[])))
        if not chats or any(s not in self.chat_scopes for s in scope_ids):
            raise ManagementError('forbidden','The remote source has no allowed original chat scope.')
        records,pages,missing,seen=[],0,[],set()
        for chat in chats:
            token,cursors=None,set()
            while True:
                builder=ListMessageRequest.builder().container_id_type('chat').container_id(chat).sort_type('ByCreateTimeAsc').page_size(50)
                if token is not None:
                    builder.page_token(token)
                response=self.client.im.v1.message.list(builder.build())
                pages+=1
                data=getattr(response,'data',None)
                if type(getattr(response,'code',None))is not int or response.code!=0 or data is None or type(getattr(data,'has_more',None))is not bool or not isinstance(getattr(data,'items',None),list):
                    missing.append(chat+':permission-or-page-unavailable')
                    break
                for item in data.items:
                    mid=getattr(item,'message_id',None)
                    if not isinstance(mid,str) or not mid or getattr(item,'chat_id',None)!=chat or mid in seen:
                        raise ManagementError('archive_incomplete','Remote history identity or overlapping pages are inconsistent.')
                    seen.add(mid)
                    body=getattr(getattr(item,'body',None),'content',None)
                    if not isinstance(body,str) or getattr(item,'deleted',False):
                        missing.append(chat+':'+mid+':content-unavailable')
                        continue
                    records.append({'id':mid,'chat_id':chat,'text':body,'timestamp':getattr(item,'create_time',None),
                        'locator':'feishu-archive:'+chat+'#'+mid,'message_type':getattr(item,'msg_type',None)})
                if not data.has_more:
                    break
                token=getattr(data,'page_token',None)
                if not isinstance(token,str) or not token or token in cursors or len(cursors)>=100:
                    missing.append(chat+':pagination-not-complete')
                    break
                cursors.add(token)
        coverage={'chat_ids':chats,'pages':pages,'end_confirmed':not missing,'compressed_history':'remote_messages_only','missing':missing,
            'source_identity':self.binding,'identity_evidence_ref':proof['evidence_ref'],'historical_deleted_messages':'not_reconstructable'}
        return _result(self.kind,requester,scope_ids,question,complete,records,coverage,_digest(records))


class CodexArchiveProvider:
    kind = 'codex_history'

    def __init__(self,adapter,thread_scopes):
        if not isinstance(adapter,ReadOnlyCodexAdapter) or not isinstance(thread_scopes,dict):
            raise ManagementError('invalid_change','Codex archives require an original-executor read-only adapter and explicit thread scopes.')
        self.adapter,self.thread_scopes=adapter,dict(thread_scopes)

    def close(self):
        self.adapter.close()

    def query(self,requester,scope_ids,question,complete=False):
        adapter=self.adapter
        proof=adapter.proof()
        required={'thread/read','thread/turns/list','thread/items/list'}
        if not required<=set(proof['supported_methods']):
            raise ManagementError('capability_unverified','Original executor full turn/item pagination capability is unavailable.')
        threads=list(dict.fromkeys(t for s in scope_ids for t in self.thread_scopes.get(s,[])))
        if not threads or any(s not in self.thread_scopes for s in scope_ids):
            raise ManagementError('forbidden','The original Codex source has no allowed thread scope.')
        records,missing,pages,versions,seen=[],[],0,[],set()
        def read_pages(method,params):
            nonlocal pages
            items,cursors=[],set()
            while True:
                result=adapter._call(method,params)
                pages+=1
                if not isinstance(result.get('data'),list) or 'nextCursor' not in result:
                    raise ManagementError('archive_incomplete','Codex history pagination end could not be confirmed.')
                items.extend(result['data'])
                cursor=result['nextCursor']
                if cursor is None:
                    return items
                if not isinstance(cursor,str) or not cursor or cursor in cursors or len(cursors)>=100:
                    raise ManagementError('archive_incomplete','Codex history cursor repeated or exceeded the bounded read budget.')
                cursors.add(cursor)
                params={**params,'cursor':cursor}
        for thread_id in threads:
            thread=adapter.read_thread(thread_id,include_turns=True)
            versions.append((thread_id,thread.get('updatedAt')))
            # A branch/subagent does not authorize the ancestor's private history.
            turns=read_pages('thread/turns/list',{'threadId':thread_id,'limit':100,'itemsView':'full','sortDirection':'asc'})
            for turn in turns:
                if not isinstance(turn,dict) or not isinstance(turn.get('id'),str) or not isinstance(turn.get('itemsView'),str):
                    raise ManagementError('archive_incomplete','Original Codex turn identity and item coverage are missing.')
                tid=turn['id']
                items=read_pages('thread/items/list',{'threadId':thread_id,'turnId':tid,'limit':100,'sortDirection':'asc'})
                for item in items:
                    if not isinstance(item,dict) or not isinstance(item.get('id'),str) or (thread_id,tid,item['id']) in seen:
                        raise ManagementError('archive_incomplete','Codex original items overlap or lack stable identity.')
                    seen.add((thread_id,tid,item['id']))
                    if item.get('type')=='contextCompaction':
                        missing.append(thread_id+':'+tid+':pre-compaction-context-not-proved-by-api')
                    records.append({'id':item['id'],'thread_id':thread_id,'turn_id':tid,'text':json.dumps(item,ensure_ascii=False),
                        'timestamp':turn.get('startedAt'),'locator':'codex-archive:'+thread_id+'/'+tid+'#'+item['id'],'payload':item})
            after=adapter.read_thread(thread_id,include_turns=False)
            if after.get('updatedAt')!=thread.get('updatedAt'):
                missing.append(thread_id+':changed-during-pagination')
        coverage={'thread_ids':threads,'pages':pages,'end_confirmed':not missing,'compressed_history':'compaction_gaps_explicit',
            'missing':missing,'original_executor_id':proof['original_executor_id'],'generation':adapter.generation,
            'identity_evidence_ref':proof['evidence_ref'],'parent_threads':'excluded_without_separate_source_grant'}
        return _result(self.kind,requester,scope_ids,question,complete,records,coverage,_digest({'versions':versions,'records':records}))
