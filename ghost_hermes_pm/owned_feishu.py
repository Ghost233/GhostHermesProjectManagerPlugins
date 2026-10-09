"""Plugin-owned Feishu platform. Native Hermes source and host logging stay untouched."""
import asyncio
import hashlib
import json
import logging
import os
import stat
import tempfile
from collections import OrderedDict
from dataclasses import replace
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace
import time
import weakref

from gateway.config import Platform
from gateway.platforms.base import BasePlatformAdapter, SendResult
from gateway.platforms.event import MessageEvent, MessageType
from gateway.session import SessionSource
from gateway.run import _async_profile_runtime_scope
from gateway.session_identity import identity_of, canonical_identity
from gateway.status import acquire_scoped_lock, release_scoped_lock
from hermes_constants import get_hermes_home

from .manager import ManagementError, VerifiedIdentity
from .messages import OWNED_PLATFORM
from .owned_feishu_process import FeishuProcessTransport

logger = logging.getLogger(__name__)


class OwnedFeishuAdapter(BasePlatformAdapter):
    def __init__(self, config, intake, ensure_manager, manager_home, *, connected_authority=None):
        self.intake, self.ensure_manager, self.manager_home = intake, ensure_manager, manager_home
        self.connected_authority = connected_authority
        if intake.settings.get('enabled') is not True or not intake.settings.get('verification_ref') or manager_home is None:
            raise ManagementError('unavailable', 'Owned Feishu intake is not enabled by trusted management configuration.')
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
        recipients = {b.get('recipient_open_id') for b in self.bindings}
        if len(recipients) != 1 or not next(iter(recipients)):
            raise ManagementError('invalid_change', 'One app needs one verified recipient identity.')
        super().__init__(replace(config, extra=extra), Platform(OWNED_PLATFORM))
        self._app_id, self._app_secret = extra['app_id'], extra['app_secret']
        self._bot_open_id = next(iter(recipients))
        self._allow_bots = extra['allow_bots']
        self._credentials = {'app_id': self._app_id, 'app_secret': self._app_secret, 'domain': extra.get('domain', 'feishu')}
        self.intake.secret_values = tuple(dict.fromkeys((*self.intake.secret_values, self._app_secret)))
        self._dedup_cache_size = 2048
        self._seen = OrderedDict()
        self._dedup_lock = asyncio.Lock()
        # App-scoped, one-way digest keys; restricted plugin state contains no raw IDs.
        digest = hashlib.sha256(self._app_id.encode()).hexdigest()
        self._dedup_state_path = Path(get_hermes_home()) / 'hermes-pm' / ('seen-' + digest + '.json')
        self._load_seen()
        self._committed = OrderedDict()
        self._close_requested = False
        self._connect_requested = False
        self.transport = None
        self._ws_supervisor = None
        self._app_lock_identity = None
        self._connection_lock = asyncio.Lock()
        self._transport_close_task = None
        self._ordinary_active = False
        self._owned_timer_tasks = set()
        self._group_rules = extra.get('group_rules', {})
        if not isinstance(self._group_rules, dict) or any(not isinstance(rule, dict) for rule in self._group_rules.values()):
            raise ManagementError('invalid_change', 'Group policy needs explicit valid configuration.')
        from agent.secret_scope import get_secret
        self._default_group_policy = str(extra.get('default_group_policy') or extra.get('group_policy')
            or get_secret('FEISHU_GROUP_POLICY', 'allowlist')).strip().lower()
        self._admins = set(extra.get('admins', []))
        self._require_mention = extra.get('require_mention', get_secret('FEISHU_REQUIRE_MENTION', 'true'))
        self._require_mention = self._require_mention is True or self._require_mention in {'true', 1}

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
        self._connect_requested = False
        self._running = False
        self._ordinary_active = False
        self._discard_ordinary_buffers()
        if self.transport is not None and callable(getattr(self.transport, 'request_close', None)):
            self.transport.request_close()
        if self._ws_supervisor is not None:
            self._ws_supervisor.cancel()

    def create_transport(self):
        """Plugin-owned I/O boundary; tests replace this factory, never native SDK methods."""
        return FeishuProcessTransport(self._credentials, self.receive_payload)

    async def connect(self, *, is_reconnect=False):
        async with self._connection_lock:
            if self.intake.closed or self._close_requested:
                self._set_fatal_error('intake_closed', 'The plugin owner is unloaded.', retryable=False)
                return False
            if self._running and self.transport is not None:
                return True
            if self.transport is not None:
                await self._close_transport()
            self._connect_requested = True
            runner = getattr(self, 'gateway_runner', None)
            adapters = list(getattr(runner, 'adapters', {}).values())
            for group in getattr(runner, '_profile_adapters', {}).values():
                if isinstance(group, dict):
                    adapters.extend(group.values())
            if any(other is not self and getattr(other, '_app_id', None) == self._app_id for other in adapters):
                self._set_fatal_error('feishu_app_conflict', 'Another configured platform owns this app.', retryable=False)
                return False
            try:
                acquired, _ = acquire_scoped_lock('feishu-app-id', self._app_id, metadata={'platform': self.platform.value})
            except Exception:
                self._set_fatal_error('feishu_app_lock_unavailable', 'Platform ownership could not be verified.', retryable=False)
                return False
            if not acquired:
                self._set_fatal_error('feishu_app_lock', 'Another local platform owns this app.', retryable=False)
                return False
            self._app_lock_identity = self._app_id
            generation = self.intake.generation
            try:
                transport = self.create_transport()
                self.transport = transport
                if not await transport.start():
                    raise ManagementError('unavailable', 'Owned platform connection is unverified.')
                if self.intake.closed or self._close_requested or not self._connect_requested or generation != self.intake.generation:
                    raise ManagementError('unavailable', 'The owner changed during connection.')
                self.intake.attach_transport(self, transport)
                if self.connected_authority is not None:
                    await self.connected_authority(transport, generation)
                if self.intake.closed or self._close_requested or not self._connect_requested or generation != self.intake.generation:
                    raise ManagementError('unavailable', 'The owner changed during connection verification.')
                self._ordinary_active = True
                self._mark_connected()
                if callable(getattr(transport, 'activate', None)):
                    transport.activate()
                self._ws_supervisor = asyncio.create_task(self._supervise(transport), name='hermes-pm-feishu-reconnect')
                return True
            except asyncio.CancelledError:
                self._connect_requested = False
                self._running = False
                await self._close_transport()
                raise
            except Exception:
                try:
                    await self._close_transport()
                except Exception:
                    self._set_fatal_error('feishu_cleanup_unverified', 'Owned platform cleanup could not be verified.', retryable=False)
                    return False
                self._set_fatal_error('feishu_connect_error', 'Owned platform connection failed.', retryable=True)
                return False

    async def _supervise(self, transport):
        process = getattr(transport, 'process', None)
        if process is None:
            return
        await process.wait()
        if self.transport is not transport or not self._connect_requested or self.intake.closed or self._close_requested:
            return
        try:
            await self._close_transport()
        except Exception:
            self._set_fatal_error('feishu_cleanup_unverified', 'Owned platform cleanup could not be verified.', retryable=False)
            return
        self._mark_disconnected()
        delay = 5
        while self._connect_requested and not self.intake.closed and not self._close_requested:
            await asyncio.sleep(delay)
            if not self._connect_requested or self.intake.closed or self._close_requested:
                return
            if await self.connect(is_reconnect=True):
                return
            if not self._fatal_error_retryable:
                return
            delay = min(delay * 2, 60)

    async def _close_transport(self):
        if self._transport_close_task is None or self._transport_close_task.done():
            self._transport_close_task = asyncio.create_task(self._finish_transport_close(), name='hermes-pm-feishu-adapter-close')
        await asyncio.shield(self._transport_close_task)

    async def _finish_transport_close(self):
        transport = self.transport
        self.intake.detach_transport(self)
        if transport is not None:
            await transport.close()
        self.transport = None
        if self._app_lock_identity is not None:
            release_scoped_lock('feishu-app-id', self._app_lock_identity)
            self._app_lock_identity = None

    async def disconnect(self):
        self._connect_requested = False
        self._running = False
        self._ordinary_active = False
        self._discard_ordinary_buffers()
        task, self._ws_supervisor = self._ws_supervisor, None
        if task is not None and task is not asyncio.current_task():
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        async with self._connection_lock:
            await self._close_transport()
        await self.cancel_background_tasks()
        timers = tuple(self._owned_timer_tasks)
        if timers:
            await asyncio.wait_for(asyncio.gather(*timers, return_exceptions=True), timeout=5)
        self._owned_timer_tasks.clear()
        self._mark_disconnected()

    def _discard_ordinary_buffers(self):
        # Prevent Base task cancellation from flushing queued text into another turn.
        for state in self._text_debounce.values():
            if state.task is not None:
                self._owned_timer_tasks.add(state.task)
                state.cancel_timer()
        self._text_debounce.clear()
        for task in self._pending_text_batch_tasks.values():
            self._owned_timer_tasks.add(task)
            task.cancel()
        self._pending_text_batch_tasks.clear()
        self._pending_text_batches.clear()
        self._pending_messages.clear()

    def set_message_handler(self, handler):
        generation = self.intake.generation

        async def scoped_handler(event):
            if not self._ordinary_active or self._close_requested or self.intake.closed or generation != self.intake.generation:
                return None
            result = await handler(event)
            if not self._ordinary_active or self._close_requested or self.intake.closed or generation != self.intake.generation:
                return None
            return result

        super().set_message_handler(scoped_handler if handler is not None else None)

    async def handle_message(self, event):
        if not self._ordinary_active or self._close_requested or self.intake.closed:
            return
        await super().handle_message(event)

    async def _process_message_background(self, event, session_key):
        if not self._ordinary_active or self._close_requested or self.intake.closed:
            return
        await super()._process_message_background(event, session_key)

    def _load_seen(self):
        try:
            descriptor = os.open(self._dedup_state_path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
            with os.fdopen(descriptor) as stream:
                info = os.fstat(stream.fileno())
                if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or stat.S_IMODE(info.st_mode) & 0o077:
                    raise ManagementError('unsafe_state', 'Owned dedup state is not private.')
                value = json.load(stream)
            now = time.time()
            if isinstance(value, dict):
                self._seen.update((k, v) for k, v in value.items()
                    if isinstance(k, str) and len(k) == 64 and isinstance(v, (float, int)) and now - v < 86400)
            while len(self._seen) > self._dedup_cache_size:
                self._seen.popitem(last=False)
        except (OSError, ValueError):
            pass

    async def _is_duplicate(self, message_id):
        digest = hashlib.sha256((self._app_id + '\0' + message_id).encode()).hexdigest()
        async with self._dedup_lock:
            now = time.time()
            if digest in self._seen and now - self._seen[digest] < 86400:
                return True
            staged = OrderedDict(self._seen)
            staged[digest] = now
            while len(staged) > self._dedup_cache_size:
                staged.popitem(last=False)
            from .manager import _private_state_directory
            directory = _private_state_directory(self._dedup_state_path.parent)
            temporary = None
            try:
                with tempfile.NamedTemporaryFile(mode='w', dir=directory, delete=False) as stream:
                    temporary = Path(stream.name)
                    json.dump(dict(staged), stream)
                    stream.flush()
                    os.fsync(stream.fileno())
                os.replace(temporary, self._dedup_state_path)
                temporary = None
                self._seen = staged
            finally:
                if temporary is not None:
                    temporary.unlink(missing_ok=True)
            return False

    async def receive_payload(self, payload):
        def namespace(value):
            if isinstance(value, dict):
                return SimpleNamespace(**{k: namespace(v) for k, v in value.items()})
            if isinstance(value, list):
                return [namespace(v) for v in value]
            return value
        await self._handle_message_event_data(namespace(payload))

    async def _handle_message_event_data(self, data):
        if self.intake.closed or self._close_requested:
            return
        try:
            header, message, sender = data.header, data.event.message, data.event.sender
            if header.app_id != self._app_id or header.event_type != 'im.message.receive_v1' or not any(
                b.get('chat_id') == getattr(message, 'chat_id', None) and
                b.get('transport_tenant_key') == getattr(header, 'tenant_key', None) for b in self.bindings):
                return
            if self._admit(sender, message) is not None:
                return
            if message.message_type != 'text' or not isinstance(message.message_id, str) or not message.message_id:
                return
            text = json.loads(message.content)['text']
            if not isinstance(text, str) or any(secret in text for secret in self.intake.secret_values if secret):
                return
            for mention in getattr(message, 'mentions', None) or []:
                if getattr(getattr(mention, 'id', None), 'open_id', None) == self._bot_open_id:
                    text = text.replace(getattr(mention, 'key', ''), '')
            text = text.strip()
            if not text or await self._is_duplicate(message.message_id):
                return
            ids = sender.sender_id
            source = self.build_source(chat_id=message.chat_id, chat_type='group' if message.chat_type == 'group' else 'dm',
                user_id=getattr(ids, 'user_id', None) or getattr(ids, 'open_id', None),
                user_id_alt=getattr(ids, 'union_id', None), is_bot=sender.sender_type in {'bot', 'app'},
                thread_id=getattr(message, 'thread_id', None), message_id=message.message_id)
            event = MessageEvent(text=text, source=source, raw_message=data, message_id=message.message_id,
                message_type=MessageType.COMMAND if text.startswith('/') else MessageType.TEXT,
                reply_to_message_id=getattr(message, 'parent_id', None) or getattr(message, 'upper_message_id', None) or getattr(message, 'root_id', None),
                timestamp=datetime.now())
            await self._dispatch_inbound_event(event)
        except Exception:
            logger.warning('Owned intake not accepted: malformed or unavailable input.')

    def build_source(self, chat_id, **fields):
        owner = getattr(self, '_owner_profile', None)
        source = SessionSource(platform=self.platform, chat_id=chat_id, profile=owner, **fields)
        runner = getattr(self, 'gateway_runner', None)
        try:
            if runner is not None:
                source.profile = runner._profile_name_for_source(source, adapter_profile=owner) or owner
        except Exception:
            source.profile_route_rejected = True
        source._transport_adapter_ref = weakref.ref(self)
        return source

    def _canonicalize(self, source):
        identity = identity_of(source)
        if identity is not None:
            return identity
        runner = getattr(self, 'gateway_runner', None)
        if runner is None:
            return None
        try:
            return canonical_identity(source, runner=runner, adapter=self, transport_profile=self._owner_transport_profile())
        except Exception:
            return None

    def _drop_unresolved(self, event):
        return self._canonicalize(event.source) is None and getattr(event.source, 'profile_route_rejected', False) is True

    async def send(self, chat_id, content, reply_to=None, metadata=None):
        if self.transport is None:
            return SendResult(False, error='Owned platform is disconnected.')
        segment = {'chat_id': chat_id, 'text': content, 'uuid': (metadata or {}).get('uuid') or __import__('uuid').uuid4().hex,
                   'reply_to': reply_to, 'thread_id': (metadata or {}).get('thread_id')}
        if not reply_to:
            segment['path'] = 'create'
        result = await self.transport.send(segment)
        return SendResult(result.get('status') == 'delivered', message_id=result.get('message_id'),
                          error=None if result.get('status') == 'delivered' else 'Owned platform delivery is unverified.')

    async def get_chat_info(self, chat_id):
        return {'type': 'group'}

    def _base_admit(self, sender, message):
        if getattr(getattr(sender, 'sender_id', None), 'open_id', None) == self._bot_open_id:
            return 'self_echo'
        if sender.sender_type in {'bot', 'app'} and self._allow_bots not in {'mentions', 'all'}:
            return 'bots_disabled'
        matching = [b for b in self.bindings if b.get('chat_id') == getattr(message, 'chat_id', None)]
        if len(matching) != 1:
            return 'owned_group_not_registered'
        binding = matching[0]
        if sender.sender_type == 'user' and (getattr(sender, 'tenant_key', None) != binding.get('sender_tenant_key')
            or getattr(sender.sender_id, 'open_id', None) != binding.get('owner_open_id')):
            return 'owned_sender_identity_rejected'
        if message.chat_type == 'group':
            rule = self._group_rules.get(message.chat_id, {})
            policy = str(rule.get('policy', 'open') if rule else self._default_group_policy).strip().lower()
            sender_ids = {getattr(sender.sender_id, key, None) for key in ('user_id', 'open_id')} - {None, ''}
            is_bot = sender.sender_type in {'bot', 'app'}
            if policy == 'disabled':
                return 'owned_group_disabled'
            if not sender_ids & self._admins:
                if policy == 'admin_only':
                    return 'owned_group_admin_required'
                if policy == 'allowlist' and not is_bot and not sender_ids & set(rule.get('allowlist', []) if rule else self.allowed_native_ids):
                    return 'owned_group_not_allowlisted'
                if policy == 'blacklist' and not is_bot and sender_ids & set(rule.get('blacklist', [])):
                    return 'owned_group_blacklisted'
                if policy not in {'open', 'allowlist', 'blacklist'}:
                    return 'owned_group_policy_rejected'
            require_mention = rule.get('require_mention', self._require_mention)
            require_mention = require_mention is True or require_mention in {'true', 1}
            text = getattr(message, 'content', '')
            if (require_mention or is_bot) and not any(getattr(getattr(m, 'id', None), 'open_id', None) == self._bot_open_id
                       and getattr(m, 'tenant_key', None) == binding.get('recipient_tenant_key')
                       and getattr(m, 'mentioned_type', None) == 'bot'
                       and isinstance(getattr(m, 'key', None), str) and m.key and m.key in text
                       for m in getattr(message, 'mentions', None) or []):
                return 'owned_real_mention_required'
        return None

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
                return self._base_admit(sender, message)
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
        return self._base_admit(sender, message)

    async def _dispatch_inbound_event(self, event):
        if self.intake.closed:
            return
        generation = self.intake.generation
        if self._drop_unresolved(event):
            return
        prepared = self.intake.prepare(event, self)
        if prepared is None:
            await self.handle_message(event)
            return
        source, runner = event.source, self.gateway_runner
        try:
            authorized = runner._intake_adapter_for(source) is self and runner._is_user_authorized_for_source(source) is True
        except Exception:
            authorized = False
        if not authorized:
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
                    'enabled': False, 'runtime_route': 'owned_prebatch', 'compatibility': 'native_platform_registration',
                'sdk_revision': 'unmodified', 'lark_version': '1.6.8',
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
