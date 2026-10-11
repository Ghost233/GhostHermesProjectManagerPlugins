"""Real gateway confirmation primitive; only external Feishu service is replaced."""
import asyncio
import json
from pathlib import Path
import sys
from types import SimpleNamespace
import copy
import yaml
import threading
import gc
import weakref

ROOT = Path(__file__).resolve().parents[1]
fixture = ROOT / 'tests/simple_intake_smoke_runner.py'
scratch = Path(sys.argv[1])
sys.argv = [str(fixture), str(scratch), 'slash-cards']
namespace = {'__name__': '__slash_fixture__', '__file__': str(fixture)}
exec(compile(fixture.read_text().split('async def main():', 1)[0], str(fixture), 'exec'), namespace)
GatewayRunner, GatewayConfig, Platform, PlatformConfig = (namespace[name] for name in
    ('GatewayRunner', 'GatewayConfig', 'Platform', 'PlatformConfig'))
service_client, connect_service, plugins, model = (namespace[name] for name in
    ('service_client', 'connect_service', 'plugins', 'model'))
from gateway.platforms.event import MessageEvent, MessageType
from lark_oapi.api.im.v1 import CreateMessageResponse
from tools import slash_confirm
from agent.i18n import t


async def main():
    runner = GatewayRunner(GatewayConfig(multiplex_profiles=False))
    platform = Platform('hermes_feishu_pm')
    adapter = runner._create_adapter(platform, PlatformConfig(enabled=True, extra={
        'app_id': 'cli_fixture', 'app_secret': 'synthetic-unused-secret',
        'require_mention': True, 'default_group_policy': 'open'}))
    runner.adapters[platform] = adapter
    runner._wire_adapter_handlers(adapter)
    client = service_client('cli_fixture')
    client.request = lambda _: SimpleNamespace(code=0, raw=SimpleNamespace(
        content=b'{"code":0,"bot":{"open_id":"ou_lead","activate_status":2}}'))
    sent, updates, decisions = [], [], []
    def delivered(request):
        sent.append(request)
        return CreateMessageResponse({'code': 0, 'data': {'message_id': 'om_card_' + str(len(sent)),
            'chat_id': 'oc_fixture', 'parent_id': getattr(request, 'message_id', None)}})
    client.im.v1.message.create = client.im.v1.message.reply = delivered
    def updated(request):
        updates.append(request)
        return SimpleNamespace(code=0)
    client.im.v1.message.patch = updated
    await connect_service(adapter, client)
    source = adapter.build_source('oc_fixture', chat_type='group', user_id='u_owner',
                                  user_id_alt='on_owner', message_id='om_start')
    assert adapter._canonicalize(source) is not None
    entry = runner.session_store.get_or_create_session(source)
    event = MessageEvent(text='/new', source=source, message_id='om_start', message_type=MessageType.COMMAND)
    async def confirmed(choice):
        decisions.append(choice)
        return 'Native command: ' + choice
    reply = await runner._request_slash_confirm(event=event, command='new', title='新会话',
                                              message='确认重置当前会话？', handler=confirmed)
    assert reply is None, 'Native slash confirmation still falls back to text.'
    card = json.loads(sent[0].request_body.content)
    actions = next(element['actions'] for element in card['elements'] if element['tag'] == 'action')
    assert [action['text']['content'] for action in actions] == ['一次批准', '始终批准', '取消']
    value = actions[0]['value']
    payload = {'header': {'event_type': 'card.action.trigger', 'event_id': 'event-click',
        'app_id': 'cli_fixture', 'tenant_key': 'tenant-app'}, 'event': {
        'operator': {'open_id': 'ou_owner', 'user_id': 'u_owner', 'union_id': 'on_owner',
                     'tenant_key': 'tenant-owner'},
        'context': {'open_chat_id': 'oc_fixture', 'open_message_id': 'om_card_1'},
        'action': {'tag': 'button', 'value': value}}}
    # Changing any authenticated envelope coordinate cannot select the pending request.
    for path, replacement in ((('header', 'app_id'), 'cli_foreign'),
        (('header', 'tenant_key'), 'tenant-foreign'),
        (('event', 'operator', 'open_id'), 'ou_stranger'),
        (('event', 'operator', 'user_id'), 'u_stranger'),
        (('event', 'operator', 'union_id'), 'on_stranger'),
        (('event', 'operator', 'tenant_key'), 'tenant-foreign'),
        (('event', 'context', 'open_chat_id'), 'oc_foreign'),
        (('event', 'context', 'open_message_id'), 'om_foreign'),
        (('event', 'action', 'value', 'confirm_id'), 'other'),
        (('event', 'action', 'value', 'token'), 'stale-token'),
        (('event', 'action', 'value', 'choice'), 'allow-permanent')):
        wrong = copy.deepcopy(payload)
        destination = wrong
        for field in path[:-1]:
            destination = destination[field]
        destination[path[-1]] = replacement
        if path[-1] in {'user_id', 'union_id'}:
            wrong['event']['operator'].update(user_id='u_stranger', union_id='on_stranger')
        await adapter.receive_payload(wrong)
        assert decisions == [] and slash_confirm.get_pending(entry.session_key), path
    await adapter.receive_payload(payload)
    assert decisions == ['once']
    assert slash_confirm.get_pending(entry.session_key) is None
    assert updates and not any(element['tag'] == 'action' for element in
                               json.loads(updates[-1].request_body.content)['elements'])
    await adapter.receive_payload(payload)
    assert decisions == ['once']
    # Text replies use the original resolver, and the old card must lose its buttons too.
    await runner._request_slash_confirm(event=event, command='new', title='新会话',
                                       message='确认重置当前会话？', handler=confirmed)
    pending = slash_confirm.get_pending(entry.session_key)
    await slash_confirm.resolve(entry.session_key, pending['confirm_id'], 'cancel')
    await asyncio.sleep(1.1)
    assert len(updates) == 2, 'Text-resolved native confirmation leaves a live card behind.'

    def callback(choice, request=None):
        request = request or sent[-1]
        rendered = json.loads(request.request_body.content)
        button = next(button for element in rendered['elements'] if element['tag'] == 'action'
                      for button in element['actions'] if button['value']['choice'] == choice)
        value = copy.deepcopy(payload)
        value['event']['action']['value'] = button['value']
        value['event']['context']['open_message_id'] = 'om_card_' + str(sent.index(request) + 1)
        return value

    # The actual /new command and original reset handler rotate the durable native session only once.
    original_id = runner.session_store.peek_session_id(entry.session_key)
    handled, reply = await runner._hm_cmd_new(event, source, entry.session_key)
    assert handled and reply is None
    once_payload = callback('once')
    await asyncio.gather(adapter.receive_payload(once_payload), adapter.receive_payload(copy.deepcopy(once_payload)))
    new_id = runner.session_store.peek_session_id(entry.session_key)
    assert new_id != original_id
    assert t('gateway.reset.header_default') in updates[-1].request_body.content
    assert not yaml.safe_load((scratch / 'home/config.yaml').read_text()).get('approvals', {}).get('destructive_slash_confirm') is False
    handled, reply = await runner._hm_cmd_new(event, source, entry.session_key)
    assert handled and reply is None
    await adapter.receive_payload(callback('cancel'))
    assert runner.session_store.peek_session_id(entry.session_key) == new_id
    assert t('gateway.confirm.cancelled', command='new') in updates[-1].request_body.content

    # A later request invalidates the earlier card without clearing the replacement.
    await runner._request_slash_confirm(event=event, command='new', title='新会话', message='旧确认', handler=confirmed)
    obsolete = callback('once')
    await runner._request_slash_confirm(event=event, command='new', title='新会话', message='新确认', handler=confirmed)
    replacement_id = slash_confirm.get_pending(entry.session_key)['confirm_id']
    before_decisions = list(decisions)
    await adapter.receive_payload(obsolete)
    assert decisions == before_decisions
    assert slash_confirm.get_pending(entry.session_key)['confirm_id'] == replacement_id
    assert not any(element['tag'] == 'action' for element in json.loads(updates[-1].request_body.content)['elements'])
    await adapter.receive_payload(callback('cancel'))

    # Native request identifiers alone are insufficient when a gateway counter is reused.
    await runner._request_slash_confirm(event=event, command='new', title='新会话', message='重启前确认', handler=confirmed)
    old_counter = callback('once')
    pending = slash_confirm.get_pending(entry.session_key)
    reused = []
    async def after_restart(choice):
        reused.append(choice)
        return 'New handler only'
    slash_confirm.register(entry.session_key, pending['confirm_id'], 'new', after_restart)
    await adapter.receive_payload(old_counter)
    assert reused == [] and slash_confirm.get_pending(entry.session_key)
    result = await adapter.send_slash_confirm('oc_fixture', '新会话', '重启后确认', entry.session_key, pending['confirm_id'])
    assert result.success
    await adapter.receive_payload(callback('cancel'))
    assert reused == ['cancel']

    # A new durable session and explicit native expiry both disable the old card without executing it.
    await runner._request_slash_confirm(event=event, command='new', title='新会话', message='旧会话确认', handler=confirmed)
    cross_session = callback('once')
    runner.session_store.reset_session(entry.session_key)
    before_decisions = list(decisions)
    await adapter.receive_payload(cross_session)
    assert decisions == before_decisions
    await runner._request_slash_confirm(event=event, command='new', title='新会话', message='待过期确认', handler=confirmed)
    expired = callback('once')
    assert slash_confirm.clear_if_stale(entry.session_key, timeout=-1)
    await adapter.receive_payload(expired)
    assert decisions == before_decisions

    # Failed or uncertain Feishu sends preserve the native text fallback and original pending request.
    for outcome in ('failed', 'unknown'):
        def undelivered(request):
            sent.append(request)
            if outcome == 'unknown':
                raise TimeoutError('External response was lost after sending.')
            return CreateMessageResponse({'code': 999, 'msg': 'Synthetic service failure'})
        client.im.v1.message.create = client.im.v1.message.reply = undelivered
        reply = await runner._request_slash_confirm(event=event, command='new', title='新会话',
            message='Fallback remains answerable', handler=confirmed)
        assert reply == 'Fallback remains answerable'
        pending = slash_confirm.get_pending(entry.session_key)
        assert pending
        failed_callback = callback('once')
        await adapter.receive_payload(failed_callback)
        assert decisions == before_decisions
        receipt = await adapter.send_slash_confirm('oc_fixture', '新会话', 'Same failed boundary',
            entry.session_key, pending['confirm_id'])
        assert not receipt.success and receipt.raw_response['status'] == outcome
        assert await slash_confirm.resolve(entry.session_key, pending['confirm_id'], 'cancel')
        before_decisions = list(decisions)
    client.im.v1.message.create = client.im.v1.message.reply = delivered

    # A callback arriving before Feishu finishes returning its send receipt is not lost or deadlocked.
    loop, fast_callbacks = asyncio.get_running_loop(), []
    def fast_delivery(request):
        response = delivered(request)
        early = callback('once', request)
        loop.call_soon_threadsafe(lambda: fast_callbacks.append(asyncio.create_task(adapter.receive_payload(early))))
        return response
    client.im.v1.message.create = client.im.v1.message.reply = fast_delivery
    reply = await runner._request_slash_confirm(event=event, command='new', title='新会话',
                                              message='Fast native callback', handler=confirmed)
    assert reply is None
    await asyncio.gather(*fast_callbacks)
    assert decisions == [*before_decisions, 'once']
    before_decisions = list(decisions)
    client.im.v1.message.create = client.im.v1.message.reply = delivered

    # Native cancellation remains at-most-once when the external card update is unavailable.
    client.im.v1.message.patch = lambda _: SimpleNamespace(code=999)
    await runner._request_slash_confirm(event=event, command='new', title='新会话',
                                       message='Update failed', handler=confirmed)
    update_failure = callback('cancel')
    await adapter.receive_payload(update_failure)
    await adapter.receive_payload(update_failure)
    assert decisions == [*before_decisions, 'cancel']
    namespace['flush_log_queue']()
    assert 'card update is unverified' in (scratch / 'home/logs/gateway.log').read_text()
    client.im.v1.message.patch = updated

    # Disconnect disables old cards without removing native text confirmations or other requests.
    await runner._request_slash_confirm(event=event, command='new', title='新会话',
                                       message='Old adapter lifetime', handler=confirmed)
    before_disconnect = callback('once')
    native_pending = slash_confirm.get_pending(entry.session_key)
    before_decisions = list(decisions)
    await adapter.disconnect()
    assert slash_confirm.get_pending(entry.session_key)['confirm_id'] == native_pending['confirm_id']
    await connect_service(adapter, client)
    await adapter.receive_payload(before_disconnect)
    assert decisions == before_decisions
    await slash_confirm.resolve(entry.session_key, native_pending['confirm_id'], 'cancel')

    # Real received slash commands keep their own reply anchor rather than the stored old origin.
    before_receive = len(sent)
    await namespace['receive'](adapter, namespace['raw']('om_actual_new', '@_user_1 /new'))
    for _ in range(100):
        if len(sent) > before_receive:
            break
        await asyncio.sleep(.01)
    assert sent[-1].request_body.msg_type == 'interactive'
    assert sent[-1].message_id == 'om_actual_new'
    await adapter.receive_payload(callback('cancel'))
    assert model.requests == [], 'Native command confirmations must not launch a model or development task.'

    # A valid /new from an empty native thread also renders a confirmation before creating its session.
    before_receive = len(sent)
    await namespace['receive'](adapter, namespace['raw']('om_empty_new', '@_user_1 /new', thread_id='thread-fresh'))
    for _ in range(100):
        if len(sent) > before_receive:
            break
        await asyncio.sleep(.01)
    assert sent[-1].request_body.msg_type == 'interactive', 'First slash command in a native session needs a card too.'
    assert sent[-1].message_id == 'om_empty_new'
    await adapter.receive_payload(callback('cancel'))

    # A delivered response arriving after disconnection cannot revive the old lifetime's card.
    sending, release_send = threading.Event(), threading.Event()
    def delayed_delivery(request):
        sending.set()
        assert release_send.wait(5)
        return delivered(request)
    client.im.v1.message.create = client.im.v1.message.reply = delayed_delivery
    slash_confirm.register(entry.session_key, 'in-flight', 'new', confirmed)
    sending_card = asyncio.create_task(adapter.send_slash_confirm('oc_fixture', '新会话',
        'Delayed old card', entry.session_key, 'in-flight'))
    for _ in range(100):
        if sending.is_set():
            break
        await asyncio.sleep(.01)
    assert sending.is_set()
    await adapter.disconnect()
    release_send.set()
    delivered_after_close = await sending_card
    assert not delivered_after_close.success
    assert delivered_after_close.raw_response['status'] == 'delivered'
    assert delivered_after_close.raw_response['confirmation_status'] == 'retired'
    old_lifetime_card = callback('once')
    client.im.v1.message.create = client.im.v1.message.reply = delivered
    await connect_service(adapter, client)
    before_decisions = list(decisions)
    await adapter.receive_payload(old_lifetime_card)
    assert decisions == before_decisions
    await slash_confirm.resolve(entry.session_key, 'in-flight', 'cancel')

    # Cancelling the original send releases its own card binding without consuming text confirmation.
    sending, release_send = threading.Event(), threading.Event()
    client.im.v1.message.create = client.im.v1.message.reply = delayed_delivery
    async def cancelled_send_handler(choice):
        decisions.append(choice)
        return 'Cancelled send native text response'
    handler_reference = weakref.ref(cancelled_send_handler)
    before_cancel_send = len(sent)
    requesting = asyncio.create_task(runner._request_slash_confirm(event=event, command='new',
        title='新会话', message='Cancelled original send', handler=cancelled_send_handler))
    for _ in range(100):
        if sending.is_set():
            break
        await asyncio.sleep(.01)
    assert sending.is_set()
    requesting.cancel()
    try:
        await requesting
    except asyncio.CancelledError:
        pass
    else:
        raise AssertionError('Original native send cancellation must propagate.')
    release_send.set()
    for _ in range(100):
        if len(sent) > before_cancel_send:
            break
        await asyncio.sleep(.01)
    assert len(sent) == before_cancel_send + 1, 'An uncertain cancelled send must not be retried.'
    cancelled_send_callback = callback('once')
    before_decisions = list(decisions)
    await adapter.receive_payload(cancelled_send_callback)
    assert decisions == before_decisions and slash_confirm.get_pending(entry.session_key)
    slash_confirm.clear(entry.session_key)
    cancelled_send_handler = requesting = None
    await asyncio.sleep(.02)
    gc.collect()
    assert handler_reference() is None, 'Cancelled send retains the original handler outside native pending state.'
    client.im.v1.message.create = client.im.v1.message.reply = delivered

    # An accepted native handler stays at-most-once when the adapter closes during its await.
    accepted, release_handler = asyncio.Event(), asyncio.Event()
    handler_choices = []
    async def held_native_handler(choice):
        handler_choices.append(choice)
        accepted.set()
        await release_handler.wait()
        return 'Original held handler finished'
    await runner._request_slash_confirm(event=event, command='new', title='新会话',
                                       message='Held native handler', handler=held_native_handler)
    held_callback = callback('once')
    resolving = asyncio.create_task(adapter.receive_payload(held_callback))
    await accepted.wait()
    await asyncio.sleep(1.1)
    await adapter.disconnect()
    release_handler.set()
    await resolving
    await connect_service(adapter, client)
    await adapter.receive_payload(held_callback)
    assert handler_choices == ['once']

    # A cancelled original handler must disable its card and remain an unknown operation result.
    accepted, release_handler = asyncio.Event(), asyncio.Event()
    await runner._request_slash_confirm(event=event, command='new', title='新会话',
                                       message='Cancelled native handler', handler=held_native_handler)
    cancelled_callback = callback('once')
    original_updates = len(updates)
    resolving = asyncio.create_task(adapter.receive_payload(cancelled_callback))
    await accepted.wait()
    resolving.cancel()
    try:
        await resolving
    except asyncio.CancelledError:
        pass
    await asyncio.sleep(1.1)
    assert len(updates) == original_updates + 1, 'Cancelled native handler leaves active card buttons behind.'
    assert '操作结果未知' in updates[-1].request_body.content
    await adapter.receive_payload(cancelled_callback)
    assert handler_choices == ['once', 'once']

    # The "always" button invokes the ORIGINAL persistence code in this disposable test Profile.
    handled, reply = await runner._hm_cmd_new(event, source, entry.session_key)
    assert handled and reply is None
    always_card = json.loads(sent[-1].request_body.content)
    assert 'destructive_slash_confirm=false' in json.dumps(always_card, ensure_ascii=False)
    await adapter.receive_payload(callback('always'))
    assert yaml.safe_load((scratch / 'home/config.yaml').read_text())['approvals']['destructive_slash_confirm'] is False
    persisted_id = runner.session_store.peek_session_id(entry.session_key)
    messages_before = len(sent)
    handled, reply = await runner._hm_cmd_new(event, source, entry.session_key)
    assert handled and reply and len(sent) == messages_before
    assert runner.session_store.peek_session_id(entry.session_key) != persisted_id

    # Original unload/reload registers a fresh adapter, whose nonce map rejects all old callbacks.
    await runner._request_slash_confirm(event=event, command='new', title='新会话',
                                       message='Before plugin unload', handler=confirmed)
    unloaded_callback = callback('once')
    before_decisions = list(decisions)
    assert plugins.unload('ghost-hermes-pm')
    await adapter.disconnect()
    plugins.discover_and_load(force=True)
    adapter = runner._create_adapter(platform, PlatformConfig(enabled=True, extra={
        'app_id': 'cli_fixture', 'app_secret': 'synthetic-unused-secret',
        'require_mention': True, 'default_group_policy': 'open'}))
    runner.adapters[platform] = adapter
    runner._wire_adapter_handlers(adapter)
    await connect_service(adapter, client)
    await adapter.receive_payload(unloaded_callback)
    assert decisions == before_decisions
    assert plugins.unload('ghost-hermes-pm')
    await adapter.disconnect()
    (scratch / 'passed').write_text('passed')


try:
    asyncio.run(main())
finally:
    model.close()
    namespace['flush_log_queue']()
