"""Public enablement using production transport and synthetic external SDK receipts."""
import asyncio
import json
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace as NS

from ghost_hermes_pm import Manager
from ghost_hermes_pm.readiness import profile_digest


class FixtureReadinessHost:
    def verify(self, profile):
        from lark_oapi import AppType
        from lark_oapi.api.im.v1 import GetChatResponse, GetMessageResponse
        from ghost_hermes_pm.feishu import NativeFeishuTransport
        digest = profile_digest(profile)
        binding = {'app_id': 'cli_readiness', 'recipient_open_id': 'ou_readiness', 'chat_id': 'oc_readiness', 'recipient_tenant_key': 'tenant-readiness', 'transport_tenant_key': 'tenant-readiness', 'owner_open_id': 'ou_owner', 'sender_tenant_key': 'tenant-owner'}
        def message(request):
            delivered = request.message_id == 'om_delivery'
            return GetMessageResponse({'code': 0, 'data': {'items': [{'message_id': request.message_id, 'chat_id': 'oc_readiness', 'deleted': False, 'parent_id': None if delivered else 'om_delivery', 'sender': {'id': 'ou_readiness' if delivered else 'ou_owner', 'id_type': 'open_id', 'sender_type': 'app' if delivered else 'user', 'tenant_key': 'tenant-readiness' if delivered else 'tenant-owner'}, 'body': {'content': json.dumps({'text': ('通道验收 ' if delivered else '已受理验收 ') + digest})}}]}})
        native = NS(config=NS(app_id='cli_readiness', app_secret='synthetic-readiness', enable_set_token=False, app_type=AppType.SELF), request=lambda request: NS(code=0, raw=NS(content=json.dumps({'code': 0, 'bot': {'open_id': 'ou_readiness', 'activate_status': 2}}).encode())), im=NS(v1=NS(chat=NS(get=lambda request: GetChatResponse({'code': 0, 'data': {'tenant_key': 'tenant-readiness'}})), message=NS(get=message))))
        transport = NativeFeishuTransport(native)
        with ThreadPoolExecutor(max_workers=1) as pool:
            receipt = pool.submit(lambda: asyncio.run(transport.verify_channel(binding, {'delivery_message_id': 'om_delivery', 'acceptance_message_id': 'om_acceptance'}, digest))).result()
        return {'status': 'verified', 'profile_id': profile['id'], 'native_profile': profile['native_profile'], 'identity_ref': profile['identity_ref'], 'configuration_digest': digest, 'channels': [receipt], 'evidence_ref': 'fixture:controlled-feishu-sdk-response-models'}


class ReadyManager(Manager):
    def __init__(self, *args, **kwargs):
        kwargs.setdefault('profile_readiness_host', FixtureReadinessHost())
        super().__init__(*args, **kwargs)

    def apply_directory_change(self, identity, expected_version, change):
        if identity.subject == self.owner_identity_ref and 'profile' in change and 'enable_profile' not in change:
            current = next((p for p in self.read_snapshot(identity)['profiles'] if p['id'] == change['profile']['id']), {})
            if current.get('lifecycle') not in {'archived', 'archiving', 'restoring'} and not current.get('migration_gate'):
                change = {**change, 'enable_profile': change['profile']['id']}
        return super().apply_directory_change(identity, expected_version, change)
