"""Render the native Hermes confirmation primitive without a second approval protocol."""
import asyncio
import logging
import secrets
from types import SimpleNamespace

from gateway.platforms.base import SendResult
from gateway.run import _async_profile_runtime_scope
from tools import slash_confirm

from .manager import ManagementError

logger = logging.getLogger(__name__)


KIND = 'hermes-native-slash-confirm'
CHOICES = {'once': '一次批准', 'always': '始终批准', 'cancel': '取消'}


def card_for(entry, status=None):
    elements = [{'tag': 'div', 'text': {'tag': 'plain_text', 'content': entry['message']}},
        {'tag': 'div', 'text': {'tag': 'plain_text', 'content':
            '“始终批准”沿用此 Hermes 命令的持久设置，会影响后续确认，不能视为单次批准。'
            + (' 对 /new、/reset、/undo，它会保存 destructive_slash_confirm=false，'
               '让后续这些破坏性会话命令不再询问。' if entry['command'] in {'new', 'reset', 'undo'} else '')}}]
    if status is None:
        elements.append({'tag': 'action', 'actions': [
            {'tag': 'button', 'text': {'tag': 'plain_text', 'content': label},
             'type': 'danger' if choice == 'cancel' else 'primary' if choice == 'once' else 'default',
             'value': {'kind': KIND, 'token': entry['token'], 'confirm_id': entry['confirm_id'], 'choice': choice}}
            for choice, label in CHOICES.items()]})
    else:
        elements.append({'tag': 'div', 'text': {'tag': 'plain_text', 'content': status}})
    return {'config': {'wide_screen_mode': True, 'update_multi': True},
            'header': {'template': 'orange', 'title': {'tag': 'plain_text', 'content': entry['title']}},
            'elements': elements}


class NativeSlashCards:
    def __init__(self, adapter):
        self.adapter = adapter
        self.entries = {}
        self.tasks = set()

    async def send(self, chat_id, title, message, session_key, confirm_id, metadata):
        adapter = self.adapter
        transport, runner = adapter.transport, getattr(adapter, 'gateway_runner', None)
        if transport is None or not callable(getattr(transport, 'update_card', None)):
            return SendResult(False, error='Native confirmation cards are not supported by the current transport.',
                              raw_response={'status': 'unsupported'})
        try:
            current = runner.session_store.lookup_by_session_key(session_key)
            source = adapter._command_source.get() or (current.origin if current else None)
            identity = adapter._canonicalize(source)
            bindings = [binding for binding in adapter.bindings if binding['chat_id'] == chat_id
                        and {source.user_id, source.user_id_alt} & set(binding['owner_native_ids'])]
            pending = slash_confirm.get_pending(session_key)
            if (adapter.intake.closed or adapter._close_requested or not adapter._ordinary_active
                or source.is_bot or identity is None or len(bindings) != 1
                or source.chat_id != chat_id or runner._session_key_for_source(source) != session_key
                or runner._delivery_adapter_for(source) is not adapter
                or runner._is_user_authorized_for_source(source) is not True
                or not pending or pending['confirm_id'] != confirm_id):
                raise ValueError('The original native confirmation is not bound.')
            if any(secret in title + message for secret in adapter.intake.secret_values if secret):
                raise ValueError('Confirmation material cannot be shared.')
            if await transport.verify_identity(bindings[0]) != {
                'app_id': adapter._app_id, 'open_id': bindings[0]['recipient_open_id']}:
                raise ValueError('The original recipient is unavailable.')
            entry = {'token': secrets.token_hex(24), 'confirm_id': confirm_id, 'session_key': session_key,
                     'session_id': current.session_id if current else None, 'source': source, 'identity': identity,
                     'binding': dict(bindings[0]), 'pending': pending, 'title': title, 'message': message,
                     'command': pending['command'].lstrip('/'), 'generation': adapter.intake.generation,
                     'transport': transport, 'message_id': None, 'ready': asyncio.Event(), 'state': 'sending'}
            self.entries[entry['token']] = entry
            segment = {'chat_id': chat_id, 'card': card_for(entry), 'uuid': entry['token'],
                       'reply_to': (metadata or {}).get('message_id') or source.message_id,
                       'thread_id': (metadata or {}).get('thread_id') or source.thread_id}
            if not segment['reply_to']:
                segment['path'] = 'create'
            try:
                receipt = await transport.send(segment)
                status = receipt.get('status', 'unknown')
                if status == 'delivered' and isinstance(receipt.get('message_id'), str) and receipt['message_id']:
                    entry['message_id'] = receipt['message_id']
                    if entry['state'] == 'sending':
                        entry['state'] = 'pending'
                else:
                    if status == 'delivered':
                        status = 'unknown'
                    entry['state'] = 'unsent'
            except asyncio.CancelledError:
                entry['state'] = 'retired'
                self.entries.pop(entry['token'], None)
                raise
            except Exception:
                status = 'unknown'
                entry['state'] = 'unsent'
            finally:
                entry['ready'].set()
            if entry['message_id']:
                if entry['state'] == 'retired' or not self._current(entry) and entry['state'] == 'pending':
                    await self.retire(entry, '原确认或会话已改变；此卡片未执行任何操作。')
                    self.entries.pop(entry['token'], None)
                    return SendResult(False, message_id=entry['message_id'],
                        error='The native confirmation card was delivered but is no longer active.',
                        raw_response={'status': 'delivered', 'confirmation_status': 'retired',
                                      'update_status': entry.get('update_status', 'unknown')})
                task = asyncio.create_task(self.watch(entry), name='hermes-pm-native-confirm-card')
                self.tasks.add(task)
                task.add_done_callback(self.tasks.discard)
                return SendResult(True, message_id=entry['message_id'], raw_response={'status': 'delivered'})
            self.entries.pop(entry['token'], None)
            return SendResult(False, error='Native confirmation card delivery: ' + status,
                              raw_response={'status': status})
        except (AttributeError, KeyError, TypeError, ValueError, ManagementError, OSError, TimeoutError):
            return SendResult(False, error='Native confirmation card binding is unavailable.',
                              raw_response={'status': 'binding_unverified'})

    def _current(self, entry):
        adapter, source = self.adapter, entry['source']
        runner = adapter.gateway_runner
        source = adapter.build_source(source.chat_id, chat_type=source.chat_type, user_id=source.user_id,
            user_id_alt=source.user_id_alt, thread_id=source.thread_id, message_id=source.message_id,
            is_bot=source.is_bot)
        identity = adapter._canonicalize(source)
        current = runner.session_store.lookup_by_session_key(entry['session_key'])
        pending = slash_confirm.get_pending(entry['session_key'])
        return (not adapter.intake.closed and not adapter._close_requested and adapter._ordinary_active
                and entry['generation'] == adapter.intake.generation
                and entry['binding'] in adapter.bindings and identity == entry['identity']
                and adapter.transport is entry['transport']
                and (current.session_id if current else None) == entry['session_id']
                and runner._session_key_for_source(source) == entry['session_key']
                and runner._delivery_adapter_for(source) is adapter
                and runner._is_user_authorized_for_source(source) is True
                and pending is not None and pending['confirm_id'] == entry['confirm_id']
                and pending['created_at'] == entry['pending']['created_at']
                and pending['handler'] is entry['pending']['handler'])

    async def receive(self, payload):
        adapter = self.adapter
        entry = None
        try:
            header, event = payload['header'], payload['event']
            operator, context, action = event['operator'], event['context'], event['action']
            value = action['value']
            if (action['tag'] != 'button' or set(value) != {'kind', 'token', 'confirm_id', 'choice'}
                or value['kind'] != KIND or value['choice'] not in CHOICES):
                return
            entry = self.entries.get(value['token'])
            if entry is None:
                return
            binding = entry['binding']
            if (header['event_type'] != 'card.action.trigger' or header['app_id'] != adapter._app_id
                or header['tenant_key'] != binding['transport_tenant_key']
                or not isinstance(header['event_id'], str) or not header['event_id']
                or operator['open_id'] != binding['owner_open_id']
                or operator['tenant_key'] != binding['sender_tenant_key']
                or not {operator.get(key) for key in ('user_id', 'open_id', 'union_id')}
                    & set(binding['owner_native_ids'])
                or context['open_chat_id'] != binding['chat_id'] or value['confirm_id'] != entry['confirm_id']):
                return
            await entry['ready'].wait()
            if entry['state'] != 'pending' or context['open_message_id'] != entry['message_id']:
                return
            sender = SimpleNamespace(sender_type='user', tenant_key=operator['tenant_key'],
                                     sender_id=SimpleNamespace(**operator))
            message = SimpleNamespace(chat_type=entry['source'].chat_type, chat_id=binding['chat_id'])
            if adapter._base_admit(sender, message, card_action=True) is not None:
                return
            if not self._current(entry):
                await self.retire(entry, '原确认或会话已改变；此卡片未执行任何操作。')
                return
            if await entry['transport'].verify_identity(binding) != {
                'app_id': adapter._app_id, 'open_id': binding['recipient_open_id']}:
                return
            async with _async_profile_runtime_scope(entry['identity'].runtime_home):
                if not self._current(entry):
                    return
                slash_confirm.clear_if_stale(entry['session_key'])
                if slash_confirm.get_pending(entry['session_key']) is None:
                    entry['state'] = 'expired'
                    await self.retire(entry, '确认已过期；此卡片未执行任何操作。')
                    return
                entry['state'] = 'resolving'
                entry['native_admitted'] = True
                result = await slash_confirm.resolve(entry['session_key'], entry['confirm_id'], value['choice'])
            entry['state'] = 'resolved'
            if result and any(secret in result for secret in adapter.intake.secret_values if secret):
                result = '原处理器结果含不能公开的内容；请在原会话中核对。'
            await self.retire(entry, '原 Hermes 确认已处理：' + CHOICES[value['choice']] +
                              ('\n' + result if result else '\n原处理器未返回文本，操作结果请核对原会话。'))
            self.entries.pop(entry['token'], None)
        except asyncio.CancelledError:
            if entry is not None and entry.get('native_admitted'):
                await self.retire(entry, '原 Hermes 处理被中断；操作结果未知，不接受重复点击。')
                self.entries.pop(entry['token'], None)
            raise
        except (AttributeError, KeyError, TypeError, ValueError, ManagementError, OSError, TimeoutError):
            return

    async def retire(self, entry, status):
        entry['state'] = 'retired'
        if entry['message_id']:
            try:
                if self.adapter.transport is not entry['transport']:
                    raise ManagementError('unavailable', 'The original card transport is disconnected.')
                response = await entry['transport'].update_card({'message_id': entry['message_id'], 'card': card_for(entry, status)})
                entry['update_status'] = response.get('status', 'unknown')
            except Exception:
                entry['update_status'] = 'unknown'
            if entry['update_status'] != 'updated':
                logger.warning('Native confirmation card update is unverified; callback buttons remain locally disabled.')

    async def watch(self, entry):
        try:
            while entry['state'] in {'pending', 'resolving'}:
                await asyncio.sleep(1)
                if entry['state'] == 'resolving':
                    continue
                if entry['state'] != 'pending':
                    break
                if not self._current(entry):
                    await self.retire(entry, '原确认已结束、被替代或会话已改变；按钮已停用。')
                elif slash_confirm.clear_if_stale(entry['session_key']):
                    await self.retire(entry, '确认已过期；此卡片未执行任何操作。')
        finally:
            if entry['state'] != 'resolving':
                self.entries.pop(entry['token'], None)

    async def close(self):
        entries = list(self.entries.values())
        self.entries = {entry['token']: entry for entry in entries if entry['state'] == 'resolving'}
        tasks = tuple(self.tasks)
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        for entry in entries:
            resolving = entry['state'] == 'resolving'
            entry['state'] = 'retired'
            entry['ready'].set()
            await self.retire(entry, '此平台已卸载或断开；卡片已停用。' +
                ('原生处理已经受理，执行结果尚未确认，不可重复提交。' if resolving
                 else '原生文本确认仍按 Hermes 状态处理。'))
