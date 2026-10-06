"""Plugin-owned independent Feishu driver; pinned private seams never patch the host."""
from dataclasses import replace
from pathlib import Path
import logging
from collections import OrderedDict
import hashlib
import inspect
from importlib.metadata import version

from gateway.config import Platform
from gateway.run import _async_profile_runtime_scope
from gateway.session_identity import identity_of
from hermes_constants import get_hermes_home
from plugins.platforms.feishu.adapter import FeishuAdapter

from .manager import ManagementError, VerifiedIdentity
from .messages import OWNED_PLATFORM

logger = logging.getLogger(__name__)
SDK_REVISION = 'bd0affe5e5f723579df8902852f5d0c47795f355'
_PINNED_FILES = {
    'plugins/platforms/feishu/adapter.py': '2dcbaa98e9801c2ac0e10b4cf7436e778f6d38fe98d5a585e995700f80147afc',
    'gateway/platforms/base.py': '8657cab37e6e481a7f7893d975c623a24500a216ccf82a1bdf35e473b04a357b',
    'gateway/authz_mixin.py': '2f5b9507cd7fc3d5d559c005ec32317ec6f4aec5479cf89108c7c9d4ca5cd884',
    'gateway/session_identity.py': '9ad91ff2ae1bbce835b6a44c0926d0bbf56c15c91a81c5d595c16f561a0d4d7c',
    'gateway/run.py': 'd0ea287688b45a6e0b67e0bbb0487cd44dff2f434aab628b7e087c951bbb9a54',
    'gateway/profile_routing.py': 'e399603aa627b2794a5723fcafbf1eac5531617dacd0ab8aa0a2e5fdf70c75f4',
    'gateway/run_adapters.py': 'dfb882f4e90e82b3543b1c994671245ba27ab6fbff585bb8b965fef696ac2514',
    'gateway/platform_registry.py': '40c0e47d43462bf174e058189cbb305d88b42e1cd2ca4c3f16fd42656ac76c9e',
    'gateway/config.py': '93fecbfbfc6f09b710aadbd266668f99281ffa567680bf7f9dedbc124e9fb45e',
    'gateway/session.py': 'd274960c58869d456d0063358a8143562ccdbe9471d996e99841275789c4e998',
    'agent/secret_scope.py': '5f2584dc93be49ef7e4de23f9f92a4abd05aa42aab8fc2fac40f54f9f8b9c664',
    'hermes_cli/plugins.py': '31c99f61f61732557bb84429d014e943d2d51a753b2541ab5de3b196a893bf9a',
}


class OwnedFeishuAdapter(FeishuAdapter):
    def __init__(self, config, intake, ensure_manager, manager_home):
        self.intake, self.ensure_manager, self.manager_home = intake, ensure_manager, manager_home
        if intake.settings.get('enabled') is not True or not intake.settings.get('verification_ref') or manager_home is None:
            raise ManagementError('unavailable', 'Owned Feishu intake is not enabled by trusted management configuration.')
        sdk_root = Path(inspect.getfile(FeishuAdapter)).resolve().parents[3]
        if version('lark-oapi') != '1.6.8' or any(hashlib.sha256((sdk_root / path).read_bytes()).hexdigest() != digest for path, digest in _PINNED_FILES.items()):
            raise ManagementError('unavailable', 'Owned Feishu private compatibility differs from the pinned SDK; intake stays disabled.')
        extra = dict(config.extra or {})
        if not extra.get('app_id') or not extra.get('app_secret'):
            raise ManagementError('invalid_change', 'Configure this new platform with explicit native app credentials.')
        self.bindings = [b for b in intake.settings.get('bindings', []) if b.get('app_id') == extra['app_id']]
        if not self.bindings or any(not b.get('verification_ref') or not isinstance(b.get('owner_native_ids'), list)
            or not b['owner_native_ids'] or any(not isinstance(i, str) or not i or i == '*' for i in b['owner_native_ids']) for b in self.bindings):
            raise ManagementError('invalid_change', 'Explicit verified owner native IDs are required for this app.')
        self.allowed_native_ids = {i for b in self.bindings for i in b['owner_native_ids']}
        self.registered_bots = [b for b in intake.settings.get('registered_bots', []) if b.get('app_id') == extra['app_id']]
        for bot in self.registered_bots:
            if not bot.get('profile_id') or not bot.get('identity_ref') or not bot.get('tenant_key') or not bot.get('open_id') or not bot.get('native_ids') or any(not isinstance(i, str) or not i or i == '*' for i in bot['native_ids']):
                raise ManagementError('invalid_change', 'Registered bot policy requires verified scoped native IDs.')
            self.allowed_native_ids.update(bot['native_ids'])
        extra['allowed_users'] = sorted(self.allowed_native_ids)
        # Do not inherit an old FEISHU_ALLOW_BOTS=all bypass for the new driver.
        extra.setdefault('allow_bots', 'none')
        super().__init__(replace(config, extra=extra))
        self.intake.secret_values = tuple(dict.fromkeys((*self.intake.secret_values,
            *(v for v in (self._app_secret, self._encrypt_key, self._verification_token) if isinstance(v, str) and v))))
        self.platform = Platform(OWNED_PLATFORM)
        self._committed = OrderedDict()

    async def connect(self, *, is_reconnect=False):
        if self.intake.closed:
            self._set_fatal_error('intake_closed', 'The plugin owner is unloaded; reconnect was not attempted.', retryable=False)
            return False
        runner = self.gateway_runner
        adapters = list(getattr(runner, 'adapters', {}).values())
        for group in getattr(runner, '_profile_adapters', {}).values():
            if isinstance(group, dict):
                adapters.extend(group.values())
        if any(other is not self and isinstance(other, FeishuAdapter) and getattr(other, '_app_id', None) == self._app_id for other in adapters):
            self._set_fatal_error('feishu_app_conflict', 'Another configured Feishu driver owns this app; connection was not attempted.', retryable=False)
            return False
        connected = await super().connect(is_reconnect=is_reconnect)
        if connected:
            from .feishu import NativeFeishuTransport
            # This client belongs to this owned instance; secondary runtime scopes
            # need not expose the management Profile's read-only handler factory.
            self.intake.attach_transport(self, NativeFeishuTransport(self._client))
        return connected

    async def disconnect(self):
        self.intake.detach_transport(self)
        await super().disconnect()

    def _load_settings(self, extra):
        settings = FeishuAdapter._load_settings(extra)
        ids = {b['recipient_open_id'] for b in self.bindings}
        if len(ids) != 1:
            raise ManagementError('invalid_change', 'One app must have one verified recipient bot identity.')
        return replace(settings, allowed_group_users=frozenset(self.allowed_native_ids), allow_all_dm=False,
                       bot_open_id=next(iter(ids)), bot_user_id='', bot_name='')

    def _load_seen_message_ids(self):
        self._dedup_state_path = Path(get_hermes_home()) / (OWNED_PLATFORM + '_seen_message_ids.json')
        super()._load_seen_message_ids()

    def _admit(self, sender, message):
        ids = getattr(sender, 'sender_id', None)
        native_ids = {getattr(ids, k, None) for k in ('user_id', 'open_id', 'union_id')} - {None, ''}
        if not native_ids & self.allowed_native_ids:
            return 'owned_sender_not_registered'
        if getattr(sender, 'sender_type', None) in {'bot', 'app'}:
            matching = [b for b in self.registered_bots if b['tenant_key'] == getattr(sender, 'tenant_key', None)
                        and b['open_id'] == getattr(ids, 'open_id', None) and native_ids & set(b['native_ids'])]
            manager = self.intake.manager()
            if len(matching) != 1 or manager is None:
                return 'owned_bot_not_registered'
            from .manager import VerifiedIdentity
            try:
                scope = manager.read_snapshot(VerifiedIdentity(matching[0]['identity_ref'], 'registered-owned-bot-policy'))
                if not any(p['id'] == matching[0]['profile_id'] and p['identity_ref'] == matching[0]['identity_ref'] for p in scope['profiles']):
                    return 'owned_bot_scope_rejected'
                project_ids = {p['id'] for p in scope['projects']}
                if not any(b['chat_id'] == message.chat_id and b['project_id'] in project_ids for b in self.bindings):
                    return 'owned_bot_scope_rejected'
            except ManagementError:
                return 'owned_bot_scope_rejected'
        return super()._admit(sender, message)

    async def _dispatch_inbound_event(self, event):
        if self.intake.closed:
            return
        if self._drop_unresolved(event):
            return
        prepared = self.intake.prepare(event, self)
        if prepared is None:
            await super()._dispatch_inbound_event(event)
            return
        source, runner = event.source, self.gateway_runner
        try:
            authorized = runner._intake_adapter_for(source) is self and runner._is_user_authorized_for_source(source) is True
        except Exception:
            authorized = False
        if not authorized:
            await super()._dispatch_inbound_event(event)
            return
        try:
            recipient = await prepared.transport.verify_identity(prepared.binding)
            if not recipient or recipient.get('app_id') != prepared.binding['app_id'] or recipient.get('open_id') != prepared.binding['recipient_open_id']:
                logger.warning('Owned intake not accepted: recipient identity requires verification.')
                return
            identity = identity_of(source)
            if identity is None:
                return
            async with _async_profile_runtime_scope(self.manager_home):
                await self.ensure_manager(event, runner)
            if not self.intake.in_scope(prepared, identity.runtime_profile):
                logger.warning('Owned intake not accepted: registered runtime responsibility requires verification.')
                return
            key = tuple(prepared.envelope.get(k) for k in ('app_id', 'transport_tenant_key', 'tenant_key', 'recipient_tenant_key', 'recipient_open_id', 'chat_id', 'message_id'))
            if key in self._committed:
                return
            if runner._admit_bot_message_for_source(source) is not True:
                return
        except Exception:
            logger.warning('Owned intake not accepted: native authorization or identity could not be verified.')
            return
        # This process-local object binds the commitment to the exact original/source.
        # It never travels in raw metadata or a batch and never returns to native flow.
        self._committed[key] = (event, source)
        while len(self._committed) > self._dedup_cache_size:
            self._committed.popitem(last=False)
        try:
            owner = VerifiedIdentity(self.intake.owner, 'verified-owned-feishu-owner-entry')
            self.intake.manager().record_intake_conditions(owner, {
                'enabled': False, 'runtime_route': 'owned_prebatch', 'compatibility': 'pinned_seams_matched',
                'sdk_revision': SDK_REVISION, 'lark_version': '1.6.8',
                'allowed_users_policy': 'explicit_owner_and_registered_bot_ids',
                'same_app_policy': 'configured_driver_conflict_and_native_lock', 'real_connect': 'unverified',
                'real_group_acceptance': 'unverified'})
            async with _async_profile_runtime_scope(identity.runtime_home):
                result = await self.intake.process_prepared(prepared)
                if result is None:
                    raise ManagementError('invalid_change', 'Committed input could not be associated.')
        except Exception as exc:
            identity = VerifiedIdentity(self.intake.owner, 'verified-owned-feishu-owner-entry')
            try:
                existing = any(r['source_anchor'] == prepared.envelope for r in self.intake.manager().read_snapshot(identity)['requests'])
                code = 'processing_unverified' if existing else 'association_unverified' if not prepared.issue_url else 'public_scope_unverified' if isinstance(exc, ManagementError) and exc.code == 'invalid_change' else 'source_unavailable'
                failure = self.intake.manager().record_intake_failure(identity, prepared.binding['project_id'], prepared.binding['profile_id'],
                    prepared.envelope, code)
                receipt = await prepared.transport.send({'uuid': failure['notification']['uuid'],
                    'text': '受理需核对：' + failure['reason'] + '\n请查看 Dashboard；当前不会启动 Codex。',
                    'chat_id': prepared.envelope['chat_id'], 'reply_to': prepared.envelope['message_id'],
                    'thread_id': prepared.envelope.get('thread_id'), 'mention_open_id': prepared.envelope['sender_open_id']})
                self.intake.manager().record_intake_failure_notification(identity, failure['id'], receipt)
            except Exception:
                logger.warning('Owned intake needs reconciliation: failure notification is unverified; native dispatch was suppressed.')
