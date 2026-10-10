"""Read-only Feishu pagination and original-executor DSH history adapters."""
import json

from .archives import _result, _digest
from .manager import ManagementError
from .observation import ReadOnlyDshAdapter


def verify_feishu_source(client, binding):
    from lark_oapi import AppType, BaseRequest, HttpMethod, AccessTokenType
    from lark_oapi.api.tenant.v2 import QueryTenantRequest
    config = getattr(client, 'config', None)
    if config is None or config.app_id != binding['app_id'] or config.enable_set_token is not False or config.app_type != AppType.SELF:
        raise ManagementError('capability_unverified', 'Original archive credential ownership is unverified.')
    response = client.request(BaseRequest.builder().http_method(HttpMethod.GET).uri('/open-apis/bot/v3/info').token_types({AccessTokenType.TENANT}).build())
    tenant = client.tenant.v2.tenant.query(QueryTenantRequest.builder().build())
    try:
        payload = json.loads(response.raw.content)
        bot = payload['bot']
        if type(response.code) is not int or response.code != 0 or type(payload.get('code')) is not int or payload['code'] != 0 or bot['open_id'] != binding['bot_open_id'] or bot.get('activate_status') != 2 or type(tenant.code) is not int or tenant.code != 0 or tenant.data.tenant.tenant_key != binding['tenant_key']:
            raise ValueError('Original source identity mismatch.')
    except (AttributeError, ValueError, TypeError, KeyError) as exc:
        raise ManagementError('capability_unverified', 'Current original Feishu app, tenant and bot identity could not be verified.') from exc
    return {**binding, 'evidence_ref': 'actual_feishu_bot_and_tenant_reads'}


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


class DshArchiveProvider:
    kind = 'dsh_history'

    def __init__(self,adapter,thread_scopes):
        if not isinstance(adapter,ReadOnlyDshAdapter) or not isinstance(thread_scopes,dict):
            raise ManagementError('invalid_change','DSH archives require an original-executor read-only adapter and explicit thread scopes.')
        self.adapter,self.thread_scopes=adapter,dict(thread_scopes)

    def close(self):
        self.adapter.close()

    def query(self,requester,scope_ids,question,complete=False):
        adapter=self.adapter
        proof=adapter.proof()
        required={'session/list','session/follow','session/page'}
        if not required<=set(proof['supported_methods']):
            raise ManagementError('capability_unverified','Original DSH journal pagination capability is unavailable.')
        threads=list(dict.fromkeys(t for s in scope_ids for t in self.thread_scopes.get(s,[])))
        if not threads or any(s not in self.thread_scopes for s in scope_ids):
            raise ManagementError('forbidden','The original DSH source has no allowed session scope.')
        records,missing,versions=[],[],[]
        for thread_id in threads:
            thread=adapter.read_thread(thread_id,include_turns=True)
            events=thread.get('nativeEvents')
            cursor=thread.get('journalCursor')
            if thread.get('historyMode')!='full' or type(cursor)is not int or cursor < -1 or not isinstance(events,list) or any(not isinstance(e,dict) or type(e.get('seq'))is not int for e in events) or [e['seq'] for e in events]!=list(range(cursor+1)):
                raise ManagementError('archive_incomplete','A complete contiguous original DSH journal prefix was not established.')
            versions.append((thread_id,cursor))
            for event in events:
                if not isinstance(event.get('type'),str) or not isinstance(event.get('data'),dict):
                    raise ManagementError('archive_incomplete','Original DSH journal event identity or payload is incomplete.')
                # Retain source-native records and distinguish them from model-context coverage.
                records.append({'id':str(event['seq']),'thread_id':thread_id,
                    'text':json.dumps(event,ensure_ascii=False),'timestamp':event.get('time'),
                    'locator':'dsh-archive:'+thread_id+'#'+str(event['seq']),'payload':event})
            after=adapter.read_thread(thread_id,include_turns=True)
            if after.get('journalCursor')!=cursor:
                missing.append(thread_id+':changed-during-pagination')
        coverage={'thread_ids':threads,'pages':'adapter_verified_contiguous_journal','end_confirmed':not missing,
            'compressed_history':'durable_journal_only_model_context_not_reconstructed',
            'missing':missing,'original_executor_id':proof['original_executor_id'],'generation':adapter.generation,
            'identity_evidence_ref':proof['evidence_ref'],'parent_threads':'excluded_without_separate_source_grant'}
        return _result(self.kind,requester,scope_ids,question,complete,records,coverage,_digest({'versions':versions,'records':records}))
