"""Unmodified Gateway SDK with plugin-owned Feishu intake and external service fixtures."""
import os
import sys
from pathlib import Path

scratch = Path(sys.argv[1]).resolve()
protected_home = Path.home()
for name in ('os-home', 'os-state', 'os-config'):
    (scratch / name).mkdir()
os.environ['HOME'] = str(scratch / 'os-home')
os.environ['XDG_STATE_HOME'] = str(scratch / 'os-state')
os.environ['XDG_CONFIG_HOME'] = str(scratch / 'os-config')
os.environ['HERMES_SKIP_PM_BOOTSTRAP'] = '1'
os.environ['HERMES_DISABLE_PROJECT_PLUGINS'] = '1'
os.environ['HERMES_FEISHU_TEXT_BATCH_DELAY_SECONDS'] = '0.01'
os.environ['HERMES_FEISHU_TEXT_BATCH_SPLIT_DELAY_SECONDS'] = '0.01'
if os.environ.get('HERMES_TEST_OWNED_UNLOAD_STAGE') in {'failure_replay', 'failure_optional'}:
    os.environ['HERMES_FEISHU_DEDUP_CACHE_SIZE'] = '32'
def audit(event, args):
    if event == 'open' and isinstance(args[0], (str, bytes)):
        path = Path(os.fsdecode(args[0])).resolve()
        if any(path.is_relative_to(protected_home / p) for p in ('.hermes', '.config', '.local/state/hermes')) or (path.name == '.env' and not path.is_relative_to(scratch)):
            raise RuntimeError('Owned smoke refused real configuration/credential files.')
    if event == 'socket.connect' and isinstance(args[1], tuple):
        raise RuntimeError('Owned smoke refused external HTTP.')
    if event == 'import' and args[0] in {'hermes_cli.main', 'run_agent'}:
        raise RuntimeError('Owned smoke refused launch/model imports.')
sys.addaudithook(audit)

# Load the original SDK without any preparation or host logging changes.
import hermes_bootstrap  # noqa: F401
import asyncio
import json
import subprocess
import types
import yaml
from feishu_service_support import service_client, connect_service, receive
from lark_oapi.api.im.v1 import P2ImMessageReceiveV1, ReplyMessageResponse
from gateway.config import Platform, PlatformConfig, GatewayConfig
from gateway.run import GatewayRunner
from gateway.profile_routing import ProfileRoute
from gateway.session_identity import identity_of
from gateway.bot_loop_guard import BotLoopGuard, BotLoopGuardSettings
from hermes_cli.profiles import get_profile_dir
from hermes_constants import get_hermes_home
from hermes_cli.plugins import get_plugin_manager
from gateway.platform_registry import platform_registry


def passed(protection=None):
    # Report only a synthetic result through the private test artifact.
    result = {'native_smoke': 'passed'}
    if protection:
        result['protection'] = protection
    (scratch / 'native-smoke-result.json').write_text(json.dumps(result))

home = scratch / 'home'
state = scratch / 'state'
repo = scratch / 'repo'
repo.mkdir()
subprocess.run(['git', 'init', '-q', str(repo)], check=True)
runtime_home = get_profile_dir('fixture-runtime')
runtime_home.mkdir(parents=True)
(home / '.env').write_text('HERMES_PM_FEISHU_ALLOWED_USERS=u_owner,on_owner,u_collab\nFEISHU_ALLOW_BOTS=all\nHERMES_FIXTURE_GITHUB_ACCOUNT=example-user\n')
(runtime_home / '.env').write_text('HERMES_PM_FEISHU_ALLOWED_USERS=u_runtime_only\n')
binding = {'sender_tenant_key': 'tenant-owner', 'recipient_tenant_key': 'tenant-bot', 'transport_tenant_key': 'tenant-app',
           'verification_ref': 'fixture:identity-map', 'app_id': 'cli_fixture', 'recipient_open_id': 'ou_lead',
           'owner_open_id': 'ou_owner', 'owner_native_ids': ['u_owner', 'on_owner'], 'chat_id': 'oc_fixture',
           'project_id': 'mono', 'profile_id': 'lead', 'repository': 'example-user/fixture'}
os.environ['HERMES_FIXTURE_GITHUB_ACCOUNT'] = 'example-user'
settings = {'manager_profile': 'default', 'state_dir': str(state), 'owner_identity_ref': 'fixture:owner',
            'dashboard_credential_ref': 'native:HERMES_FIXTURE_OWNER_TOKEN', 'github_account_ref': 'native:HERMES_FIXTURE_GITHUB_ACCOUNT',
            'feishu_intake': {'enabled': True, 'verification_ref': 'fixture:controlled-smoke', 'bindings': [binding]}}
settings['feishu_intake']['registered_bots'] = [{'profile_id': 'collab', 'identity_ref': 'fixture:collab',
    'app_id': 'cli_fixture', 'tenant_key': 'tenant-collab', 'open_id': 'ou_collab', 'native_ids': ['u_collab']}]
(home / 'config.yaml').write_text(yaml.safe_dump({'plugins': {'enabled': ['ghost-hermes-pm'],
    'entries': {'ghost-hermes-pm': {'settings': settings}}}}))
sys.path.insert(0, str(home / 'plugins' / 'ghost-hermes-pm'))
import ghost_hermes_pm.feishu as external_issue
def fixture_issue(url, *, expected_account=None):
    assert expected_account == 'example-user'
    if url.endswith('/18'): raise RuntimeError('Artificial unavailable Issue source')
    return {'url': url, 'title': 'Scope ' + url.rsplit('/', 1)[-1],
            'body': 'Frozen material for ' + url.rsplit('/', 1)[-1], 'updated_at': '2026-10-07T00:00:00Z'}
external_issue.read_github_issue = fixture_issue
from native_fixture_boundary import install
install(home / 'plugins' / 'ghost-hermes-pm', {'feishu': lambda module: setattr(module, 'read_github_issue', fixture_issue)})
plugins = get_plugin_manager()
plugins.discover_and_load()
assert platform_registry.get('hermes_feishu_pm') is not None
assert platform_registry.get('feishu') is None, 'Standalone plugin must not replace/register builtin Feishu.'

class FixtureRunner(GatewayRunner):
    async def wait_for_shutdown(self): await self.stopped.wait()
    def _is_user_authorized_for_source(self, source, **kwargs):
        self.auth_sources.append(source)
        if self.auth_mode == 'raises': raise RuntimeError('Artificial authorization failure')
        if self.auth_mode != 'native': return self.auth_mode
        return super()._is_user_authorized_for_source(source, **kwargs)
    def _admit_bot_message_for_source(self, source):
        self.budget_sources.append(source)
        return super()._admit_bot_message_for_source(source)
    async def _handle_message(self, event):
        self.cold.append(event)
        if event.source.is_bot and self._is_user_authorized_for_source(event.source) is True and self._admit_bot_message_for_source(event.source) is True:
            self.native_bots.append(event)
    async def _handle_active_session_busy_message(self, event, key):
        self.busy.append(event)
        return True
    async def _handle_adapter_fatal_error(self, adapter): self.fatal.append(adapter)

def raw(mid, number=15, text=None):
    return P2ImMessageReceiveV1({'schema': '2.0', 'header': {'event_type': 'im.message.receive_v1',
        'app_id': 'cli_fixture', 'tenant_key': 'tenant-app'}, 'event': {'sender': {'sender_type': 'user',
        'tenant_key': 'tenant-owner', 'sender_id': {'open_id': 'ou_owner', 'user_id': 'u_owner', 'union_id': 'on_owner'}},
        'message': {'message_id': mid, 'chat_id': 'oc_fixture', 'chat_type': 'group', 'message_type': 'text',
            'content': json.dumps({'text': text or '@_user_1 派发 https://github.com/example-user/fixture/issues/' + str(number)}),
            'mentions': [{'key': '@_user_1', 'mentioned_type': 'bot', 'tenant_key': 'tenant-bot', 'id': {'open_id': 'ou_lead'}}]}}})

async def main():
    runner = object.__new__(FixtureRunner)
    runner.config = GatewayConfig(multiplex_profiles=True)
    runner.config.profile_routes = [ProfileRoute(name='fixture-route', platform='hermes_feishu_pm', profile='fixture-runtime', chat_id='oc_fixture')]
    runner._primary_profile_name = 'default'
    runner.adapters, runner._profile_adapters = {}, {}
    runner.session_store, runner.pairing_store, runner.pairing_stores = None, None, {}
    runner._busy_text_mode, runner._human_delay = 'steer', None
    runner._bot_loop_guard = BotLoopGuard(settings=lambda: BotLoopGuardSettings(max_events=2))
    runner.auth_sources, runner.budget_sources, runner.cold, runner.busy = [], [], [], []
    runner.auth_mode, runner.fatal, runner.native_bots = 'native', [], []
    runner.stopped = asyncio.Event()
    platform = Platform('hermes_feishu_pm')
    config = PlatformConfig(enabled=True, extra={'app_id': 'cli_fixture', 'app_secret': 'synthetic-unused-secret',
        'require_mention': True, 'default_group_policy': 'open', 'allow_bots': 'none'})
    adapter = runner._create_adapter(platform, config)
    assert adapter is not None and adapter.platform is platform
    runner.adapters[platform] = adapter
    runner._wire_adapter_handlers(adapter)
    async def chat_info(chat_id): return {'name': 'Synthetic chat', 'type': 'group'}
    adapter.get_chat_info = chat_info
    native = service_client('cli_fixture')
    sent = []
    native.request = lambda request: types.SimpleNamespace(code=0, raw=types.SimpleNamespace(
        content=b'{"code":0,"bot":{"open_id":"ou_lead","activate_status":2}}'))
    def reply(request):
        sent.append(request)
        return ReplyMessageResponse({'code': 0, 'data': {'message_id': 'om_sent_' + str(len(sent)),
            'chat_id': 'oc_fixture', 'parent_id': request.message_id}})
    native.im.v1.message.reply = reply
    await connect_service(adapter, native)
    await plugins.ainvoke_hook('pre_gateway_dispatch', event=object(), gateway=runner)
    from ghost_hermes_pm.transport import ManagementClient
    client = ManagementClient(state, 'synthetic-owner-credential')
    client.apply_directory_change(0, {'enable_profile': 'lead', 'project': {'id': 'mono', 'name': 'Fixture', 'repo_path': str(repo)},
        'profile': {'id': 'lead', 'native_profile': 'fixture-runtime', 'identity_ref': 'fixture:lead',
            'role': 'project_lead', 'capability': 'development', 'project_id': 'mono'}})
    if os.environ.get('HERMES_TEST_OWNED_UNLOAD_STAGE') == 'ordinary':
        started, cancelled, release = asyncio.Event(), asyncio.Event(), asyncio.Event()
        dispatched = []
        async def ordinary_handler(event):
            dispatched.append(event.message_id)
            started.set()
            try:
                await release.wait()
            finally:
                cancelled.set()
        runner._wire_adapter_handlers(adapter, message_handler=ordinary_handler,
            busy_text_mode='queue', busy_text_timing=(0.2, 0.2))
        adapter.set_busy_session_handler(None)
        await receive(adapter, raw('om_ordinary_running', text='@_user_1 普通消息'))
        await asyncio.wait_for(started.wait(), timeout=3)
        await receive(adapter, raw('om_ordinary_debounced', text='@_user_1 普通后续'))
        assert dispatched == ['om_ordinary_running']
        assert plugins.unload('ghost-hermes-pm')
        plugins.discover_and_load(force=True)
        await asyncio.wait_for(cancelled.wait(), timeout=3)
        release.set()
        await asyncio.sleep(0.3)
        assert dispatched == ['om_ordinary_running'], ('Old message dispatched after reload.', dispatched)
        assert not sent and not adapter.is_connected
        assert plugins.unload('ghost-hermes-pm')
        passed()
        return
    if os.environ.get('HERMES_TEST_OWNED_UNLOAD_STAGE') == 'verify':
        transport = next(t for a, t in adapter.intake.transports if a is adapter)
        verify = transport.verify_identity
        started, release = asyncio.Event(), asyncio.Event()
        async def pending_verify(binding):
            started.set()
            await release.wait()
            return await verify(binding)
        transport.verify_identity = pending_verify
        pending = asyncio.create_task(receive(adapter, raw('om_unload_pending', 15)))
        await asyncio.wait_for(started.wait(), timeout=3)
        assert plugins.unload('ghost-hermes-pm')
        assert not (state / 'manager.sock').exists() and adapter.intake.closed
        release.set()
        await asyncio.wait_for(pending, timeout=6)
        assert not (state / 'manager.sock').exists(), 'Resumed verification must not resurrect authority.'
        assert not sent and not runner.budget_sources
        from ghost_hermes_pm import Manager, VerifiedIdentity
        with Manager(state, owner_identity_ref='fixture:owner') as stopped:
            assert stopped.read_snapshot(VerifiedIdentity('fixture:owner', 'fixture-audit'))['requests'] == []
        passed()
        return
    if os.environ.get('HERMES_TEST_OWNED_UNLOAD_STAGE') == 'issue_queue':
        from concurrent.futures import ThreadPoolExecutor
        import threading
        loop = asyncio.get_running_loop()
        occupied, release = threading.Event(), threading.Event()
        queued = asyncio.Event()
        reader, calls = adapter.intake.issue_reader, []
        def observed_reader(url):
            calls.append(url)
            return reader(url)
        adapter.intake.issue_reader = observed_reader
        class SourceQueue(ThreadPoolExecutor):
            def submit(self, function, *args, **kwargs):
                inner = getattr(function, 'args', ())
                source = inner[0] if inner else function
                if getattr(source, '__name__', '') in {'observed_reader', 'read_if_active'}:
                    def occupy():
                        occupied.set()
                        release.wait(8)
                    super().submit(occupy)
                    assert occupied.wait(2)
                    future = super().submit(function, *args, **kwargs)
                    loop.call_soon_threadsafe(queued.set)
                    return future
                return super().submit(function, *args, **kwargs)
        pool = SourceQueue(max_workers=1)
        loop.set_default_executor(pool)
        pending = asyncio.create_task(receive(adapter, raw('om_queued_issue', 15)))
        await asyncio.wait_for(queued.wait(), timeout=4)
        assert calls == [] and len(runner.budget_sources) == 1
        assert plugins.unload('ghost-hermes-pm')
        release.set()
        await asyncio.wait_for(pending, timeout=6)
        assert calls == [] and not sent and not (state / 'manager.sock').exists()
        passed()
        return
    if os.environ.get('HERMES_TEST_OWNED_UNLOAD_STAGE') == 'issue':
        started, release = asyncio.Event(), asyncio.Event()
        reader = adapter.intake.issue_reader
        async def pending_issue(url):
            started.set()
            await release.wait()
            return reader(url)
        adapter.intake.issue_reader = pending_issue
        pending = asyncio.create_task(receive(adapter, raw('om_unload_issue', 15)))
        await asyncio.wait_for(started.wait(), timeout=3)
        assert plugins.unload('ghost-hermes-pm')
        release.set()
        await asyncio.wait_for(pending, timeout=6)
        assert not (state / 'manager.sock').exists() and not sent
        from ghost_hermes_pm import Manager, VerifiedIdentity
        with Manager(state, owner_identity_ref='fixture:owner') as stopped:
            assert stopped.read_snapshot(VerifiedIdentity('fixture:owner', 'fixture-audit'))['requests'] == []
        passed()
        return
    if os.environ.get('HERMES_TEST_OWNED_UNLOAD_STAGE') in {'failure_replay', 'failure_optional'}:
        await receive(adapter, raw('om_seed', 15))
        def unknown_notice(request):
            sent.append(request)
            return ReplyMessageResponse({'code': 0, 'data': {}})
        native.im.v1.message.reply = unknown_notice
        await receive(adapter, raw('om_fail', 18))
        before = len(sent)
        assert client.read_snapshot()['intake_failures'][0]['notification']['status'] == 'unknown'
        for number in range(33):
            await receive(adapter, raw('om_thanks_' + str(number), text='谢谢'))
        budget_before_replay = len(runner.budget_sources)
        replay = raw('om_fail', 18)
        if os.environ.get('HERMES_TEST_OWNED_UNLOAD_STAGE') == 'failure_optional':
            replay.event.message.thread_id = ''
        await receive(adapter, replay)
        assert len(sent) == before, 'An evicted original must not resend its unknown durable failure notice.'
        assert len(runner.budget_sources) == budget_before_replay
        failure = client.read_snapshot()['intake_failures']
        assert len(failure) == 1 and failure[0]['source_anchor']['thread_id'] is None
        assert plugins.unload('ghost-hermes-pm')
        passed()
        return
    if os.environ.get('HERMES_TEST_OWNED_UNLOAD_STAGE') == 'send':
        transport = next(t for a, t in adapter.intake.transports if a is adapter)
        send = transport.send
        started, release = asyncio.Event(), asyncio.Event()
        calls = []
        async def pending_send(segment):
            calls.append(segment)
            started.set()
            await release.wait()
            return await send(segment)
        transport.send = pending_send
        pending = asyncio.create_task(receive(adapter, raw('om_unload_send', 15)))
        await asyncio.wait_for(started.wait(), timeout=3)
        assert plugins.unload('ghost-hermes-pm')
        release.set()
        await asyncio.wait_for(pending, timeout=6)
        assert len(calls) == 1 and not sent and not (state / 'manager.sock').exists()
        from ghost_hermes_pm import Manager, VerifiedIdentity
        with Manager(state, owner_identity_ref='fixture:owner') as stopped:
            record = stopped.read_snapshot(VerifiedIdentity('fixture:owner', 'fixture-audit'))['requests'][0]
            assert record['delivery'] == 'unknown' and record['task_start_anchor'] is None
        passed()
        return
    await receive(adapter, raw('om_one', 15))
    await receive(adapter, raw('om_two', 16))
    snapshot = client.read_snapshot()
    assert [r['source_anchor']['message_id'] for r in snapshot['requests']] == ['om_one', 'om_two']
    assert [r['accepted_scope']['title'] for r in snapshot['requests']] == ['Scope 15', 'Scope 16']
    assert len(sent) == 4 and not runner.cold
    source = runner.budget_sources[-1]
    identity = identity_of(source)
    assert source.platform is platform and source.profile == 'fixture-runtime'
    assert identity.transport_profile == 'default' and identity.runtime_profile == 'fixture-runtime'
    assert identity.authorization_home == home and identity.runtime_home == runtime_home
    assert source._transport_adapter_ref() is adapter and runner.auth_sources[-1] is source
    assert Path(get_hermes_home()) == home
    await receive(adapter, raw('om_two', 16))
    assert len(sent) == 4 and len(runner.budget_sources) == 2
    native_started, release_native = asyncio.Event(), asyncio.Event()
    native_events, native_task = [], None
    async def pending_native(event):
        native_events.append(event)
        native_started.set()
        await release_native.wait()
    adapter.set_message_handler(pending_native)
    try:
        from gateway.platforms.event import MessageEvent
        native_source = adapter.build_source(chat_id='oc_fixture', chat_type='group',
            user_id='u_owner', user_id_alt='on_owner', message_id='om_native_running', is_bot=False)
        native_event = MessageEvent(text='Ordinary pending native conversation', source=native_source, message_id='om_native_running')
        await adapter.handle_message(native_event)
        await asyncio.wait_for(native_started.wait(), timeout=3)
        assert len(native_events) == 1 and native_events[0].message_id == 'om_native_running'
        assert len(adapter._active_sessions) == 1
        native_key = next(iter(adapter._active_sessions))
        native_task = adapter._session_tasks[native_key]
        assert not native_task.done() and not adapter._active_sessions[native_key].is_set()
        budget_before_issue = len(runner.budget_sources)
        busy_raw = raw('om_busy', 17)
        await receive(adapter, busy_raw)
        accepted = client.read_snapshot()['requests']
        assert len(accepted) == 3 and not runner.busy and not runner.cold and len(sent) == 6
        assert accepted[-1]['source_anchor']['message_id'] == 'om_busy'
        assert accepted[-1]['source_anchor']['chat_id'] == 'oc_fixture'
        assert accepted[-1]['source_anchor']['tenant_key'] == 'tenant-owner'
        assert accepted[-1]['source_anchor']['transport_tenant_key'] == 'tenant-app'
        assert accepted[-1]['source_anchor']['recipient_tenant_key'] == 'tenant-bot'
        assert accepted[-1]['accepted_scope']['title'] == 'Scope 17'
        assert accepted[-1]['accepted_scope']['body'] == 'Frozen material for 17'
        assert len(runner.budget_sources) == budget_before_issue + 1
        busy_source = runner.budget_sources[-1]
        assert busy_source.message_id == 'om_busy' and busy_source._transport_adapter_ref() is adapter
        assert runner.auth_sources[-1] is busy_source
        assert identity_of(busy_source).runtime_profile == 'fixture-runtime'
        assert identity_of(busy_source).authorization_home == home
        await receive(adapter, busy_raw)
        assert len(runner.budget_sources) == budget_before_issue + 1
        assert len(client.read_snapshot()['requests']) == 3 and len(sent) == 6
        assert len(native_events) == 1
        from gateway.session import build_session_key
        assert build_session_key(busy_source, profile='fixture-runtime') == native_key
        assert adapter._session_tasks[native_key] is native_task and not native_task.done()
        assert native_key not in adapter._pending_messages and native_key not in adapter._text_debounce
    finally:
        release_native.set()
        if native_task is not None:
            await asyncio.wait_for(asyncio.shield(native_task), timeout=3)
        runner._wire_adapter_handlers(adapter)
    await receive(adapter, raw('om_fail', 18))
    failed = client.read_snapshot()
    assert len(failed['requests']) == 3
    assert failed['intake_failures'][0]['acceptance'] == 'unaccepted'
    assert failed['intake_failures'][0]['reason'] == 'Issue source could not be verified; no new work was accepted.'
    budget_count = len(runner.budget_sources)
    await receive(adapter, raw('om_fail', 18))
    assert len(runner.budget_sources) == budget_count and not runner.busy and not runner.cold
    for mode in (False, None, 'raises'):
        runner.auth_mode = mode
        await receive(adapter, raw('om_auth_' + str(mode), 19))
        await asyncio.sleep(0.06)
        assert len(client.read_snapshot()['requests']) == 3
        assert len(runner.budget_sources) == budget_count
    runner.auth_mode = 'native'
    other_namespace = raw('om_wrong_namespace', 20)
    other_namespace.header.app_id = 'cli_foreign'
    await receive(adapter, other_namespace)
    await asyncio.sleep(0.06)
    assert len(client.read_snapshot()['requests']) == 3
    wrong_tenant = raw('om_wrong_tenant', 20)
    wrong_tenant.event.sender.tenant_key = 'tenant-untrusted'
    wrong_sender = raw('om_wrong_sender', 20)
    wrong_sender.event.sender.sender_id.open_id = 'ou_untrusted'
    wrong_sender.event.sender.sender_id.user_id = 'u_untrusted'
    wrong_sender.event.sender.sender_id.union_id = 'on_untrusted'
    missing_mention = raw('om_missing_mention', 20)
    missing_mention.event.message.mentions = []
    for rejected in (wrong_tenant, wrong_sender, missing_mention):
        await receive(adapter, rejected)
        assert len(client.read_snapshot()['requests']) == 3 and len(runner.budget_sources) == budget_count, (
            rejected.event.message.message_id, len(client.read_snapshot()['requests']), len(runner.budget_sources), budget_count)
    native_identity = native.request
    native.request = lambda request: types.SimpleNamespace(code=0, raw=types.SimpleNamespace(
        content=b'{"code":0,"bot":{"open_id":"ou_untrusted","activate_status":2}}'))
    await receive(adapter, raw('om_wrong_recipient', 20))
    assert len(client.read_snapshot()['requests']) == 3 and len(runner.budget_sources) == budget_count
    native.request = native_identity
    ordinary = runner._create_adapter(platform, PlatformConfig(enabled=True,
        extra={**config.extra, 'require_mention': False}))
    runner._wire_adapter_handlers(ordinary)
    await adapter.disconnect()
    runner.adapters[platform] = ordinary
    await connect_service(ordinary, native)
    unmentioned = raw('om_owner_ordinary_without_mention', text='普通消息无需提及')
    unmentioned.event.message.mentions = []
    before_fallback = len(runner.budget_sources)
    await receive(ordinary, unmentioned)
    await asyncio.sleep(0.06)
    assert any(source.message_id == 'om_owner_ordinary_without_mention' for source in runner.budget_sources[before_fallback:])
    assert len(client.read_snapshot()['requests']) == 3, 'An allowed ordinary fallback cannot create a managed Issue request.'
    await ordinary.disconnect()
    runner.adapters[platform] = adapter
    budget_count = len(runner.budget_sources)
    disabled = runner._create_adapter(platform, PlatformConfig(enabled=True, extra={**config.extra,
        'group_rules': {'oc_fixture': {'policy': 'disabled'}}}))
    runner._wire_adapter_handlers(disabled)
    disabled.get_chat_info = chat_info
    await adapter.disconnect()
    runner.adapters[platform] = disabled
    await connect_service(disabled, native)
    await receive(disabled, raw('om_native_group_denied', 21))
    assert len(client.read_snapshot()['requests']) == 3 and len(runner.budget_sources) == budget_count, (
        len(client.read_snapshot()['requests']), len(runner.budget_sources), budget_count)
    bot_echo = raw('om_owner_id_is_bot', 22)
    bot_echo.event.sender.sender_type = 'bot'
    await receive(adapter, bot_echo)
    assert len(client.read_snapshot()['requests']) == 3 and len(runner.budget_sources) == budget_count
    await disabled.disconnect()
    runner.adapters[platform] = adapter
    replacement = service_client('cli_fixture', 'synthetic-reconnected-secret')
    replacement_sent = []
    replacement.request = native.request
    def replacement_reply(request):
        replacement_sent.append(request)
        return ReplyMessageResponse({'code': 0, 'data': {'message_id': 'om_new_client_' + str(len(replacement_sent)),
            'chat_id': 'oc_fixture', 'parent_id': request.message_id}})
    replacement.im.v1.message.reply = replacement_reply
    await connect_service(adapter, replacement)
    old_sent_count = len(sent)
    await receive(adapter, raw('om_reconnected', 23))
    assert len(replacement_sent) == 2 and len(sent) == old_sent_count
    assert len(client.read_snapshot()['requests']) == 4
    client.apply_directory_change(client.read_snapshot()['version'], {'enable_profile': 'collab', 'profile': {'id': 'collab', 'native_profile': 'fixture-collab',
        'identity_ref': 'fixture:collab', 'role': 'subproject_lead', 'capability': 'development', 'project_id': 'mono', 'parent_profile_id': 'lead'}})
    bot_adapter = runner._create_adapter(platform, PlatformConfig(enabled=True, extra={**config.extra, 'allow_bots': 'all'}))
    runner.adapters[platform] = bot_adapter
    runner._wire_adapter_handlers(bot_adapter)
    bot_adapter.get_chat_info = chat_info
    await adapter.disconnect()
    await connect_service(bot_adapter, replacement)
    def bot_raw(mid, user_id='u_collab'):
        incoming = raw(mid, text='@_user_1 /fixture-native-bot')
        incoming.event.sender.sender_type = 'bot'
        incoming.event.sender.tenant_key = 'tenant-collab'
        incoming.event.sender.sender_id.user_id = user_id
        incoming.event.sender.sender_id.open_id = 'ou_collab' if user_id == 'u_collab' else 'ou_unknown_bot'
        incoming.event.sender.sender_id.union_id = None
        return incoming
    previous_budget = len(runner.budget_sources)
    await receive(adapter, bot_raw('om_native_bots_disabled'))
    assert len(runner.budget_sources) == previous_budget
    for mid in ('om_bot_one', 'om_bot_two', 'om_bot_over_budget'):
        await receive(bot_adapter, bot_raw(mid))
        await asyncio.sleep(0.04)
    assert len(runner.native_bots) == 2, {'bots': len(runner.native_bots), 'cold': [(e.message_id, e.source.user_id, e.source.is_bot) for e in runner.cold], 'budget': [(s.user_id, s.is_bot) for s in runner.budget_sources]}
    assert all(e.source.is_bot is True for e in runner.native_bots)
    assert len(client.read_snapshot()['requests']) == 4
    previous_budget = len(runner.budget_sources)
    await receive(bot_adapter, bot_raw('om_unknown_bot', 'u_unknown_bot'))
    assert len(runner.budget_sources) == previous_budget
    await bot_adapter.disconnect()
    runner.adapters[platform] = adapter
    conditions = client.read_snapshot()['intake_conditions']
    assert conditions['runtime_route'] == 'owned_prebatch'
    assert conditions['compatibility'] == 'native_platform_registration'
    assert conditions['allowed_users_policy'] == 'explicit_owner_and_registered_bot_ids'
    assert conditions['real_group_acceptance'] == 'unverified' and conditions['enabled'] is False
    from plugins.platforms.feishu.adapter import FeishuAdapter
    builtin = FeishuAdapter(config)
    runner.adapters[Platform.FEISHU] = builtin
    assert await adapter.connect() is False
    assert adapter.fatal_error_code == 'feishu_app_conflict'
    del runner.adapters[Platform.FEISHU]
    if os.environ.get('HERMES_TEST_OWNED_UNLOAD_STAGE') == 'connected':
        transport = await connect_service(adapter, replacement)
        assert adapter.is_connected
        assert plugins.unload('ghost-hermes-pm')
        await asyncio.sleep(0)
        assert not adapter.is_connected and transport.closed and not (state / 'manager.sock').exists()
        passed()
        return
    await connect_service(adapter, replacement)
    await receive(adapter, raw('om_connected', 26))
    assert len(client.read_snapshot()['requests']) == 5
    await adapter.disconnect()
    await connect_service(adapter, replacement)
    await receive(adapter, raw('om_after_reconnect', 27))
    assert len(client.read_snapshot()['requests']) == 6
    await adapter.disconnect()
    runner.stopped.set()
    await asyncio.sleep(0)
    assert not (state / 'manager.sock').exists()
    assert plugins.unload('ghost-hermes-pm')
    assert not platform_registry.is_registered('hermes_feishu_pm')
    assert await adapter.connect(is_reconnect=True) is False
    assert adapter.fatal_error_code == 'intake_closed'
    passed()

asyncio.run(main())
