"""Plugin-owned independent Feishu driver; pinned private seams never patch the host."""
from dataclasses import replace
from pathlib import Path
import logging
from collections import OrderedDict
import inspect
from importlib.metadata import version
import asyncio

from gateway.config import Platform
from gateway.run import _async_profile_runtime_scope
from gateway.session_identity import identity_of
from hermes_constants import get_hermes_home
from plugins.platforms.feishu.adapter import FeishuAdapter

from .manager import ManagementError, VerifiedIdentity
from .messages import OWNED_PLATFORM
from .sdk_contract import SDK_REVISION, SDK_BASE_FILES, SDK_PRIVACY_FILES, source_files_match

logger = logging.getLogger(__name__)

def _native_protection_verified(manager_home):
    try:
        sdk_root = Path(inspect.getfile(FeishuAdapter)).resolve().parents[3]
        if version('lark-oapi') != '1.6.8' or not source_files_match(sdk_root, {**SDK_BASE_FILES, **SDK_PRIVACY_FILES}):
            return False
        import hermes_native_log_privacy
        if Path(inspect.getfile(hermes_native_log_privacy)).resolve() != sdk_root / 'hermes_native_log_privacy.py':
            return False
        return hermes_native_log_privacy.protected_profile_logging_active(manager_home) is True
    except Exception:
        return False


class OwnedFeishuAdapter(FeishuAdapter):
    def __init__(self, config, intake, ensure_manager, manager_home):
        self.intake, self.ensure_manager, self.manager_home = intake, ensure_manager, manager_home
        if intake.settings.get('enabled') is not True or not intake.settings.get('verification_ref') or manager_home is None:
            raise ManagementError('unavailable', 'Owned Feishu intake is not enabled by trusted management configuration.')
        if not _native_protection_verified(manager_home):
            raise ManagementError('unavailable', 'Owned intake requires the fixed native SDK and active log protection; intake stays disabled.') from None
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
        self._close_requested = False

    def bind_lifecycle(self, ctx):
        async def owner_lifetime():
            try:
                await asyncio.Future()
            finally:
                self.request_close()
                await self.disconnect()
        ctx.on_unload(self.request_close)
        ctx.spawn_task(owner_lifetime(), name='hermes-pm-owned-adapter-lifetime')

    def request_close(self):
        self._close_requested = True
        self._running = False
        if self._ws_client is not None:
            self._ws_client._auto_reconnect = False
        if self._ws_supervisor is not None:
            self._ws_supervisor.cancel()

    async def connect(self, *, is_reconnect=False):
        if self.intake.closed or self._close_requested:
            self._set_fatal_error('intake_closed', 'The plugin owner is unloaded; reconnect was not attempted.', retryable=False)
            return False
        if not _native_protection_verified(self.manager_home):
            self._set_fatal_error('native_log_privacy_unavailable', 'Native log protection could not be verified; connection was not attempted.', retryable=False)
            return False
        runner = self.gateway_runner
        adapters = list(getattr(runner, 'adapters', {}).values())
        for group in getattr(runner, '_profile_adapters', {}).values():
            if isinstance(group, dict):
                adapters.extend(group.values())
        if any(other is not self and isinstance(other, FeishuAdapter) and getattr(other, '_app_id', None) == self._app_id for other in adapters):
            self._set_fatal_error('feishu_app_conflict', 'Another configured Feishu driver owns this app; connection was not attempted.', retryable=False)
            return False
        generation = self.intake.generation
        connected = await super().connect(is_reconnect=is_reconnect)
        if self.intake.closed or self._close_requested or generation != self.intake.generation:
            await self.disconnect()
            return False
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
            if len(matching) != 1:
                return 'owned_bot_not_registered'
            role_entry = self.intake.collaboration_entry
            if role_entry is not None and role_entry.bot_source_in_scope(sender, message, self._app_id):
                return super()._admit(sender, message)
            if manager is None:
                return 'owned_bot_not_registered'
            from .manager import VerifiedIdentity
            try:
                scope = manager.read_snapshot(VerifiedIdentity(matching[0]['identity_ref'], 'registered-owned-bot-policy'))
                if not any(p['id'] == matching[0]['profile_id'] and p['identity_ref'] == matching[0]['identity_ref'] for p in scope['profiles']):
                    return 'owned_bot_scope_rejected'
                project_ids = {p['id'] for p in scope['projects']}
                knowledge_allowed = manager.knowledge_bot_allowed(matching[0], self._app_id, message.chat_id, sender.tenant_key, ids.open_id, native_ids)
                if not knowledge_allowed and not any(b['chat_id'] == message.chat_id and b['project_id'] in project_ids for b in self.bindings):
                    return 'owned_bot_scope_rejected'
            except ManagementError:
                return 'owned_bot_scope_rejected'
        return super()._admit(sender, message)

    async def _dispatch_inbound_event(self, event):
        if self.intake.closed:
            return
        generation = self.intake.generation
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
            self.intake.require_active(generation)
            if not recipient or recipient.get('app_id') != prepared.binding['app_id'] or recipient.get('open_id') != prepared.binding['recipient_open_id']:
                logger.warning('Owned intake not accepted: recipient identity requires verification.')
                return
            identity = identity_of(source)
            if identity is None:
                return
            async with _async_profile_runtime_scope(self.manager_home):
                self.intake.require_active(generation)
                await self.ensure_manager(event, runner)
            self.intake.require_active(generation)
            if not self.intake.in_scope(prepared, identity.runtime_profile):
                logger.warning('Owned intake not accepted: registered runtime responsibility requires verification.')
                return
            owner = VerifiedIdentity(self.intake.owner, 'verified-owned-feishu-owner-entry')
            role_message = bool(getattr(prepared, 'role_kind', None))
            if role_message and self.intake.collaboration_entry.already_processed(prepared):
                return
            if not role_message and self.intake.manager().read_intake_failure(owner, prepared.envelope) is not None:
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
            self.intake.require_active(generation)
            owner = VerifiedIdentity(self.intake.owner, 'verified-owned-feishu-owner-entry')
            if self.intake.manager() is not None:
                self.intake.manager().record_intake_conditions(owner, {
                    'enabled': False, 'runtime_route': 'owned_prebatch', 'compatibility': 'pinned_seams_matched',
                'sdk_revision': SDK_REVISION, 'lark_version': '1.6.8',
                'allowed_users_policy': 'explicit_owner_and_registered_bot_ids',
                'same_app_policy': 'configured_driver_conflict_and_native_lock', 'real_connect': 'unverified',
                'real_group_acceptance': 'unverified'})
            async with _async_profile_runtime_scope(identity.runtime_home):
                self.intake.require_active(generation)
                result = await self.intake.process_prepared(prepared, generation)
                self.intake.require_active(generation)
                if result is None:
                    raise ManagementError('invalid_change', 'Committed input could not be associated.')
        except Exception as exc:
            if self.intake.closed or generation != self.intake.generation:
                return
            identity = VerifiedIdentity(self.intake.owner, 'verified-owned-feishu-owner-entry')
            try:
                existing = any(r['source_anchor'] == prepared.envelope for r in self.intake.manager().read_snapshot(identity)['requests'])
                code = 'processing_unverified' if existing else 'association_unverified' if not prepared.issue_url else 'public_scope_unverified' if isinstance(exc, ManagementError) and exc.code == 'invalid_change' else 'source_unavailable'
                failure = self.intake.manager().record_intake_failure(identity, prepared.binding['project_id'], prepared.binding['profile_id'],
                    prepared.envelope, code)
                if not failure['notification_claimed']:
                    return
                receipt = await prepared.transport.send({'uuid': failure['notification']['uuid'],
                    'text': '受理需核对：' + failure['reason'] + '\n请查看 Dashboard；当前不会启动 Codex。',
                    'chat_id': prepared.envelope['chat_id'], 'reply_to': prepared.envelope['message_id'],
                    'thread_id': prepared.envelope.get('thread_id'), 'mention_open_id': prepared.envelope['sender_open_id']})
                self.intake.require_active(generation)
                self.intake.manager().record_intake_failure_notification(identity, failure['id'], receipt)
            except Exception:
                logger.warning('Owned intake needs reconciliation: failure notification is unverified; native dispatch was suppressed.')
