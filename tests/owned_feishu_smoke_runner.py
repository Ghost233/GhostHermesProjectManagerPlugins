"""Actual fixed Gateway/adapter/Lark pipeline in the staged artificial runtime."""
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

import asyncio
import json
import subprocess
import types
import yaml
from lark_oapi import Client
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

home = scratch / 'home'
state = scratch / 'state'
repo = scratch / 'repo'
repo.mkdir()
subprocess.run(['git', 'init', '-q', str(repo)], check=True)
runtime_home = get_profile_dir('fixture-runtime')
runtime_home.mkdir(parents=True)
(home / '.env').write_text('HERMES_PM_FEISHU_ALLOWED_USERS=u_owner,on_owner,u_collab\nFEISHU_ALLOW_BOTS=all\n')
(runtime_home / '.env').write_text('HERMES_PM_FEISHU_ALLOWED_USERS=u_runtime_only\n')
binding = {'sender_tenant_key': 'tenant-owner', 'recipient_tenant_key': 'tenant-bot', 'transport_tenant_key': 'tenant-app',
           'verification_ref': 'fixture:identity-map', 'app_id': 'cli_fixture', 'recipient_open_id': 'ou_lead',
           'owner_open_id': 'ou_owner', 'owner_native_ids': ['u_owner', 'on_owner'], 'chat_id': 'oc_fixture',
           'project_id': 'mono', 'profile_id': 'lead', 'repository': 'Ghost233/fixture'}
settings = {'manager_profile': 'default', 'state_dir': str(state), 'owner_identity_ref': 'fixture:owner',
            'dashboard_credential_ref': 'native:HERMES_FIXTURE_OWNER_TOKEN',
            'feishu_intake': {'enabled': True, 'verification_ref': 'fixture:controlled-smoke', 'bindings': [binding]}}
settings['feishu_intake']['registered_bots'] = [{'profile_id': 'collab', 'identity_ref': 'fixture:collab',
    'app_id': 'cli_fixture', 'tenant_key': 'tenant-collab', 'open_id': 'ou_collab', 'native_ids': ['u_collab']}]
(home / 'config.yaml').write_text(yaml.safe_dump({'plugins': {'enabled': ['ghost-hermes-pm'],
    'entries': {'ghost-hermes-pm': {'settings': settings}}}}))
sys.path.insert(0, str(home / 'plugins' / 'ghost-hermes-pm'))
import ghost_hermes_pm.feishu as external_issue
def fixture_issue(url):
    if url.endswith('/18'): raise RuntimeError('Artificial unavailable Issue source')
    return {'url': url, 'title': 'Scope ' + url.rsplit('/', 1)[-1],
            'body': 'Frozen material for ' + url.rsplit('/', 1)[-1], 'updated_at': '2026-10-07T00:00:00Z'}
external_issue.read_github_issue = fixture_issue
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
            'content': json.dumps({'text': text or '@_user_1 派发 https://github.com/Ghost233/fixture/issues/' + str(number)}),
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
        'require_mention': False, 'default_group_policy': 'open', 'allow_bots': 'none'})
    if os.environ.get('HERMES_TEST_OWNED_ARTIFACT_MISMATCH') == '1':
        # Only this disposable SDK copy is changed; runtime code has no test flag.
        artifact = scratch / 'sdk' / 'gateway' / 'profile_routing.py'
        artifact.write_text(artifact.read_text() + '\n# artificial unsupported artifact revision\n')
        assert runner._create_adapter(platform, config) is None
        assert not (state / 'manager.sqlite3').exists()
        assert not runner.budget_sources
        plugins.unload('ghost-hermes-pm')
        print('native load, Dashboard bridge, restart, teardown: OK')
        return
    adapter = runner._create_adapter(platform, config)
    assert adapter is not None and adapter.platform is platform
    runner.adapters[platform] = adapter
    runner._wire_adapter_handlers(adapter)
    async def chat_info(chat_id): return {'name': 'Synthetic chat', 'type': 'group'}
    adapter.get_chat_info = chat_info
    native = Client.builder().app_id('cli_fixture').app_secret('synthetic-unused-secret').build()
    sent = []
    native.request = lambda request: types.SimpleNamespace(code=0, raw=types.SimpleNamespace(
        content=b'{"code":0,"bot":{"open_id":"ou_lead","activate_status":2}}'))
    def reply(request):
        sent.append(request)
        return ReplyMessageResponse({'code': 0, 'data': {'message_id': 'om_sent_' + str(len(sent)),
            'chat_id': 'oc_fixture', 'parent_id': request.message_id}})
    native.im.v1.message.reply = reply
    factories = plugins.get_platform_handler_factories('hermes_feishu_pm')
    assert len(factories) == 1
    factories[0][0](native, adapter)
    await plugins.ainvoke_hook('pre_gateway_dispatch', event=object(), gateway=runner)
    from ghost_hermes_pm.transport import ManagementClient
    client = ManagementClient(state, 'synthetic-owner-credential')
    client.apply_directory_change(0, {'project': {'id': 'mono', 'name': 'Fixture', 'repo_path': str(repo)},
        'profile': {'id': 'lead', 'native_profile': 'fixture-runtime', 'identity_ref': 'fixture:lead',
            'role': 'project_lead', 'capability': 'development', 'project_id': 'mono'}})
    if os.environ.get('HERMES_TEST_OWNED_UNLOAD_STAGE') == 'verify':
        transport = next(t for a, t in adapter.intake.transports if a is adapter)
        verify = transport.verify_identity
        started, release = asyncio.Event(), asyncio.Event()
        async def pending_verify(binding):
            started.set()
            await release.wait()
            return await verify(binding)
        transport.verify_identity = pending_verify
        pending = asyncio.create_task(adapter._handle_message_event_data(raw('om_unload_pending', 15)))
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
        print('native load, Dashboard bridge, restart, teardown: OK')
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
        pending = asyncio.create_task(adapter._handle_message_event_data(raw('om_queued_issue', 15)))
        await asyncio.wait_for(queued.wait(), timeout=4)
        assert calls == [] and len(runner.budget_sources) == 1
        assert plugins.unload('ghost-hermes-pm')
        release.set()
        await asyncio.wait_for(pending, timeout=6)
        assert calls == [] and not sent and not (state / 'manager.sock').exists()
        print('native load, Dashboard bridge, restart, teardown: OK')
        return
    if os.environ.get('HERMES_TEST_OWNED_UNLOAD_STAGE') == 'issue':
        started, release = asyncio.Event(), asyncio.Event()
        reader = adapter.intake.issue_reader
        async def pending_issue(url):
            started.set()
            await release.wait()
            return reader(url)
        adapter.intake.issue_reader = pending_issue
        pending = asyncio.create_task(adapter._handle_message_event_data(raw('om_unload_issue', 15)))
        await asyncio.wait_for(started.wait(), timeout=3)
        assert plugins.unload('ghost-hermes-pm')
        release.set()
        await asyncio.wait_for(pending, timeout=6)
        assert not (state / 'manager.sock').exists() and not sent
        from ghost_hermes_pm import Manager, VerifiedIdentity
        with Manager(state, owner_identity_ref='fixture:owner') as stopped:
            assert stopped.read_snapshot(VerifiedIdentity('fixture:owner', 'fixture-audit'))['requests'] == []
        print('native load, Dashboard bridge, restart, teardown: OK')
        return
    if os.environ.get('HERMES_TEST_OWNED_UNLOAD_STAGE') in {'failure_replay', 'failure_optional'}:
        await adapter._handle_message_event_data(raw('om_seed', 15))
        def unknown_notice(request):
            sent.append(request)
            return ReplyMessageResponse({'code': 0, 'data': {}})
        native.im.v1.message.reply = unknown_notice
        await adapter._handle_message_event_data(raw('om_fail', 18))
        before = len(sent)
        assert client.read_snapshot()['intake_failures'][0]['notification']['status'] == 'unknown'
        for number in range(33):
            await adapter._handle_message_event_data(raw('om_thanks_' + str(number), text='谢谢'))
        budget_before_replay = len(runner.budget_sources)
        replay = raw('om_fail', 18)
        if os.environ.get('HERMES_TEST_OWNED_UNLOAD_STAGE') == 'failure_optional':
            replay.event.message.thread_id = ''
        await adapter._handle_message_event_data(replay)
        assert len(sent) == before, 'An evicted original must not resend its unknown durable failure notice.'
        assert len(runner.budget_sources) == budget_before_replay
        failure = client.read_snapshot()['intake_failures']
        assert len(failure) == 1 and failure[0]['source_anchor']['thread_id'] is None
        assert plugins.unload('ghost-hermes-pm')
        print('native load, Dashboard bridge, restart, teardown: OK')
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
        pending = asyncio.create_task(adapter._handle_message_event_data(raw('om_unload_send', 15)))
        await asyncio.wait_for(started.wait(), timeout=3)
        assert plugins.unload('ghost-hermes-pm')
        release.set()
        await asyncio.wait_for(pending, timeout=6)
        assert len(calls) == 1 and not sent and not (state / 'manager.sock').exists()
        from ghost_hermes_pm import Manager, VerifiedIdentity
        with Manager(state, owner_identity_ref='fixture:owner') as stopped:
            record = stopped.read_snapshot(VerifiedIdentity('fixture:owner', 'fixture-audit'))['requests'][0]
            assert record['delivery'] == 'unknown' and record['task_start_anchor'] is None
        print('native load, Dashboard bridge, restart, teardown: OK')
        return
    await adapter._handle_message_event_data(raw('om_one', 15))
    await adapter._handle_message_event_data(raw('om_two', 16))
    snapshot = client.read_snapshot()
    assert [r['source_anchor']['message_id'] for r in snapshot['requests']] == ['om_one', 'om_two']
    assert [r['accepted_scope']['title'] for r in snapshot['requests']] == ['Scope 15', 'Scope 16']
    assert len(sent) == 4 and not adapter._pending_text_batches and not runner.cold
    source = runner.budget_sources[-1]
    identity = identity_of(source)
    assert source.platform is platform and source.profile == 'fixture-runtime'
    assert identity.transport_profile == 'default' and identity.runtime_profile == 'fixture-runtime'
    assert identity.authorization_home == home and identity.runtime_home == runtime_home
    assert source._transport_adapter_ref() is adapter and runner.auth_sources[-1] is source
    assert Path(get_hermes_home()) == home
    await adapter._handle_message_event_data(raw('om_two', 16))
    assert len(sent) == 4 and len(runner.budget_sources) == 2
    busy_key = adapter._event_session_key(types.SimpleNamespace(source=source, metadata={}))
    owner_task = asyncio.create_task(asyncio.Event().wait())
    adapter._active_sessions[busy_key] = asyncio.Event()
    adapter._session_tasks[busy_key] = owner_task
    await adapter._handle_message_event_data(raw('om_busy', 17))
    assert len(client.read_snapshot()['requests']) == 3 and not runner.busy and len(sent) == 6
    await adapter._handle_message_event_data(raw('om_fail', 18))
    failed = client.read_snapshot()
    assert len(failed['requests']) == 3
    assert failed['intake_failures'][0]['acceptance'] == 'unaccepted'
    assert failed['intake_failures'][0]['reason'] == 'Issue source could not be verified; no new work was accepted.'
    budget_count = len(runner.budget_sources)
    await adapter._handle_message_event_data(raw('om_fail', 18))
    assert len(runner.budget_sources) == budget_count and not runner.busy and not runner.cold
    for mode in (False, None, 'raises'):
        runner.auth_mode = mode
        await adapter._handle_message_event_data(raw('om_auth_' + str(mode), 19))
        await asyncio.sleep(0.06)
        assert len(client.read_snapshot()['requests']) == 3
        assert len(runner.budget_sources) == budget_count
    runner.auth_mode = 'native'
    other_namespace = raw('om_wrong_namespace', 20)
    other_namespace.header.app_id = 'cli_foreign'
    await adapter._handle_message_event_data(other_namespace)
    await asyncio.sleep(0.06)
    assert len(client.read_snapshot()['requests']) == 3
    disabled = runner._create_adapter(platform, PlatformConfig(enabled=True, extra={**config.extra,
        'group_rules': {'oc_fixture': {'policy': 'disabled'}}}))
    runner._wire_adapter_handlers(disabled)
    disabled.get_chat_info = chat_info
    factories[0][0](native, disabled)
    await disabled._handle_message_event_data(raw('om_native_group_denied', 21))
    assert len(client.read_snapshot()['requests']) == 3 and len(runner.budget_sources) == budget_count
    bot_echo = raw('om_owner_id_is_bot', 22)
    bot_echo.event.sender.sender_type = 'bot'
    await adapter._handle_message_event_data(bot_echo)
    assert len(client.read_snapshot()['requests']) == 3 and len(runner.budget_sources) == budget_count
    replacement = Client.builder().app_id('cli_fixture').app_secret('synthetic-reconnected-secret').build()
    replacement_sent = []
    replacement.request = native.request
    def replacement_reply(request):
        replacement_sent.append(request)
        return ReplyMessageResponse({'code': 0, 'data': {'message_id': 'om_new_client_' + str(len(replacement_sent)),
            'chat_id': 'oc_fixture', 'parent_id': request.message_id}})
    replacement.im.v1.message.reply = replacement_reply
    factories[0][0](replacement, adapter)
    old_sent_count = len(sent)
    await adapter._handle_message_event_data(raw('om_reconnected', 23))
    assert len(replacement_sent) == 2 and len(sent) == old_sent_count
    assert len(client.read_snapshot()['requests']) == 4
    owner_task.cancel()
    await asyncio.gather(owner_task, return_exceptions=True)
    adapter._active_sessions.clear(); adapter._session_tasks.clear()
    client.apply_directory_change(client.read_snapshot()['version'], {'profile': {'id': 'collab', 'native_profile': 'fixture-collab',
        'identity_ref': 'fixture:collab', 'role': 'subproject_lead', 'capability': 'development', 'project_id': 'mono', 'parent_profile_id': 'lead'}})
    bot_adapter = runner._create_adapter(platform, PlatformConfig(enabled=True, extra={**config.extra, 'allow_bots': 'all'}))
    runner.adapters[platform] = bot_adapter
    runner._wire_adapter_handlers(bot_adapter)
    bot_adapter.get_chat_info = chat_info
    factories[0][0](replacement, bot_adapter)
    def bot_raw(mid, user_id='u_collab'):
        incoming = raw(mid, text='/fixture-native-bot')
        incoming.event.sender.sender_type = 'bot'
        incoming.event.sender.tenant_key = 'tenant-collab'
        incoming.event.sender.sender_id.user_id = user_id
        incoming.event.sender.sender_id.open_id = 'ou_collab' if user_id == 'u_collab' else 'ou_unknown_bot'
        incoming.event.sender.sender_id.union_id = None
        return incoming
    previous_budget = len(runner.budget_sources)
    await adapter._handle_message_event_data(bot_raw('om_native_bots_disabled'))
    assert len(runner.budget_sources) == previous_budget
    for mid in ('om_bot_one', 'om_bot_two', 'om_bot_over_budget'):
        await bot_adapter._handle_message_event_data(bot_raw(mid))
        await asyncio.sleep(0.04)
    assert len(runner.native_bots) == 2, {'bots': len(runner.native_bots), 'cold': [(e.message_id, e.source.user_id, e.source.is_bot) for e in runner.cold], 'budget': [(s.user_id, s.is_bot) for s in runner.budget_sources]}
    assert all(e.source.is_bot is True for e in runner.native_bots)
    assert len(client.read_snapshot()['requests']) == 4
    previous_budget = len(runner.budget_sources)
    await bot_adapter._handle_message_event_data(bot_raw('om_unknown_bot', 'u_unknown_bot'))
    assert len(runner.budget_sources) == previous_budget
    await bot_adapter.disconnect()
    runner.adapters[platform] = adapter
    conditions = client.read_snapshot()['intake_conditions']
    assert conditions['runtime_route'] == 'owned_prebatch'
    assert conditions['compatibility'] == 'pinned_seams_matched'
    assert conditions['allowed_users_policy'] == 'explicit_owner_and_registered_bot_ids'
    assert conditions['real_group_acceptance'] == 'unverified' and conditions['enabled'] is False
    from plugins.platforms.feishu.adapter import FeishuAdapter
    builtin = FeishuAdapter(config)
    runner.adapters[Platform.FEISHU] = builtin
    assert await adapter.connect() is False
    assert adapter.fatal_error_code == 'feishu_app_conflict'
    del runner.adapters[Platform.FEISHU]
    # Drive the actual public connect/reconnect/disconnect lifecycle. Only the
    # external SDK HTTP and websocket service boundary is inert, never the host.
    from unittest.mock import patch
    from lark_oapi.core.http import Transport as SdkHTTP
    from lark_oapi.core.model import RawResponse
    from lark_oapi.ws import Client as SdkWebSocket
    http_requests = []
    def sdk_http(conf, request, option=None):
        http_requests.append(request.uri)
        if 'tenant_access_token' in request.uri:
            payload = {'code': 0, 'tenant_access_token': 'synthetic-sdk-token', 'expire': 7200}
        elif '/bot/v3/info' in request.uri:
            payload = {'code': 0, 'bot': {'open_id': 'ou_lead', 'activate_status': 2, 'app_name': 'Fixture only'}}
        elif request.uri.endswith('/reply'):
            payload = {'code': 0, 'data': {'message_id': 'om_connected_' + str(len(http_requests)), 'chat_id': 'oc_fixture', 'parent_id': request.message_id}}
        else:
            payload = {'code': 0, 'data': {}}
        response = RawResponse()
        response.status_code, response.headers, response.content = 200, {'Content-Type': 'application/json'}, json.dumps(payload).encode()
        return response
    if os.environ.get('HERMES_TEST_OWNED_UNLOAD_STAGE') == 'connected':
        ready = asyncio.Event()
        main_loop = asyncio.get_running_loop()
        sockets = []
        class SocketBoundary:
            closed = False
            async def close(self): self.closed = True
        def active_start(ws):
            ws._conn = SocketBoundary()
            sockets.append((ws, ws._conn))
            main_loop.call_soon_threadsafe(ready.set)
            asyncio.get_event_loop().run_forever()
        with patch.object(SdkHTTP, 'execute', side_effect=sdk_http), patch.object(SdkWebSocket, 'start', active_start):
            assert await adapter.connect() is True
            await asyncio.wait_for(ready.wait(), timeout=3)
            assert adapter._running and adapter._ws_supervisor is not None and adapter._app_lock_identity
            assert plugins.unload('ghost-hermes-pm')
            for _ in range(100):
                if not adapter._running and adapter._ws_supervisor is None and adapter._app_lock_identity is None and sockets[0][1].closed:
                    break
                await asyncio.sleep(0.02)
            assert not adapter._running and adapter._ws_supervisor is None and adapter._app_lock_identity is None
            assert sockets[0][1].closed and sockets[0][0]._auto_reconnect is False
            assert not (state / 'manager.sock').exists()
        print('native load, Dashboard bridge, restart, teardown: OK')
        return
    with patch.object(SdkHTTP, 'execute', side_effect=sdk_http), patch.object(SdkWebSocket, 'start', return_value=None):
        assert await adapter.connect() is True
        await adapter._handle_message_event_data(raw('om_connected', 26))
        assert len(client.read_snapshot()['requests']) == 5
        await adapter.disconnect()
        assert await adapter.connect(is_reconnect=True) is True
        await adapter._handle_message_event_data(raw('om_after_reconnect', 27))
        assert len(client.read_snapshot()['requests']) == 6
        await adapter.disconnect()
    assert '/open-apis/bot/v3/info' in http_requests
    await adapter.disconnect()
    runner.stopped.set()
    await asyncio.sleep(0)
    assert not (state / 'manager.sock').exists()
    assert plugins.unload('ghost-hermes-pm')
    assert not platform_registry.is_registered('hermes_feishu_pm')
    assert await adapter.connect(is_reconnect=True) is False
    assert adapter.fatal_error_code == 'intake_closed'
    print('native load, Dashboard bridge, restart, teardown: OK')

asyncio.run(main())
