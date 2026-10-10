"""Real PluginManager/factory/connect/bridge/teardown, synthetic external Feishu service."""
import asyncio
from contextlib import nullcontext
import importlib
import json
import os
from pathlib import Path
import sys
import threading
from types import SimpleNamespace

import yaml

scratch, scenario = Path(sys.argv[1]).resolve(), sys.argv[2]
protected_home = Path.home()
os.environ['HOME'] = str(scratch / 'os-home')
os.environ['XDG_STATE_HOME'] = str(scratch / 'os-state')
os.environ['XDG_CONFIG_HOME'] = str(scratch / 'os-config')
for name in ('os-home', 'os-state', 'os-config'):
    (scratch / name).mkdir()


def audit(event, args):
    if event == 'open' and isinstance(args[0], (str, bytes)):
        path = Path(os.fsdecode(args[0])).resolve()
        if any(path.is_relative_to(protected_home / prefix) for prefix in ('.hermes', '.config', '.local/state/hermes')) or (path.name == '.env' and not path.is_relative_to(scratch)):
            raise RuntimeError('Bootstrap smoke refused real configuration and credentials.')
    if event == 'socket.connect' and isinstance(args[1], tuple):
        raise RuntimeError('Bootstrap smoke refused external network access.')
    if event == 'import' and args[0] in {'run_agent', 'hermes_cli.main'}:
        raise RuntimeError('Connection bootstrap must not dispatch a model turn.')


sys.addaudithook(audit)
from gateway.config import GatewayConfig, Platform, PlatformConfig
from gateway.platform_registry import platform_registry
from gateway.run import GatewayRunner, _profile_runtime_scope
from hermes_cli.plugins import get_plugin_manager

home, state = scratch / 'home', scratch / 'authority'
binding = {'app_id': 'cli_bootstrap', 'recipient_open_id': 'ou_manager',
    'owner_open_id': 'ou_owner', 'owner_native_ids': ['u_owner'],
    'sender_tenant_key': 'tenant-owner', 'recipient_tenant_key': 'tenant-bot',
    'transport_tenant_key': 'tenant-app', 'chat_id': 'oc_bootstrap',
    'profile_id': 'manager', 'project_id': 'project', 'repository': 'example-user/fixture',
    'verification_ref': 'fixture:verified-identities'}
settings = {'manager_profile': 'default', 'state_dir': str(state),
    'owner_identity_ref': 'fixture:owner', 'dashboard_credential_ref': 'native:HERMES_FIXTURE_OWNER_TOKEN',
    'feishu_intake': {'enabled': True, 'verification_ref': 'fixture:verified-policy', 'bindings': [binding]}}
if scenario == 'missing_credential':
    settings['dashboard_credential_ref'] = 'native:HERMES_FIXTURE_ABSENT_TOKEN'
(home / 'config.yaml').write_text(yaml.safe_dump({'plugins': {'enabled': ['ghost-hermes-pm'],
    'entries': {'ghost-hermes-pm': {'settings': settings}}}}))
plugins = get_plugin_manager()
plugins.discover_and_load()
assert platform_registry.get('hermes_feishu_pm') is not None
assert platform_registry.get('feishu') is None


class FixtureRunner(GatewayRunner):
    async def wait_for_shutdown(self):
        await self.stopped.wait()


def runner_and_adapter():
    runner = object.__new__(FixtureRunner)
    runner.adapters, runner._profile_adapters = {}, {}
    runner._primary_profile_name = 'default'
    runner.config = GatewayConfig()
    runner.stopped = asyncio.Event()
    adapter = runner._create_adapter(Platform('hermes_feishu_pm'), PlatformConfig(enabled=True,
        extra={'app_id': 'cli_bootstrap', 'app_secret': 'synthetic-bootstrap-app-secret'}))
    assert adapter is not None
    runner.adapters[Platform('hermes_feishu_pm')] = adapter
    if scenario == 'wrong_profile':
        adapter.set_owner_profile('other-profile')
    return runner, adapter


def external_transport(adapter):
    # Replace only the external Feishu API/WebSocket boundary, using production identity parsing.
    package = type(adapter).__module__.rsplit('.', 1)[0]
    NativeFeishuTransport = importlib.import_module(package + '.feishu').NativeFeishuTransport
    from lark_oapi import AppType
    started, release = threading.Event(), threading.Event()

    def request(_):
        if scenario in {'unload_during_verification', 'disconnect_during_verification'}:
            started.set()
            if not release.wait(5):
                raise RuntimeError('Synthetic identity request did not release.')
        bot = 'ou_other_bot' if scenario == 'wrong_bot' else 'ou_manager'
        return SimpleNamespace(code=0, raw=SimpleNamespace(content=json.dumps(
            {'code': 0, 'bot': {'open_id': bot, 'activate_status': 2}}).encode()))

    native = SimpleNamespace(config=SimpleNamespace(app_id='cli_bootstrap', app_secret='synthetic-bootstrap-app-secret',
        app_type=AppType.SELF, enable_set_token=False), request=request)

    class ServiceTransport(NativeFeishuTransport):
        closed = False
        async def start(self):
            return True
        def request_close(self):
            self.closed = True
        async def close(self):
            self.closed = True

    transport = ServiceTransport(native)
    adapter.create_transport = lambda: transport
    return transport, started, release


def unavailable(client):
    try:
        client.read_snapshot()
    except Exception as exc:
        assert getattr(exc, 'code', None) == 'unavailable', type(exc).__name__
    else:
        raise AssertionError('An unverified connection must not start management authority.')


async def unloaded(adapter, transport):
    for _ in range(100):
        if transport.closed and not adapter.is_connected:
            break
        await asyncio.sleep(.01)
    assert transport.closed and not adapter.is_connected
    assert platform_registry.get('hermes_feishu_pm') is None
    assert not any(thread.name == 'hermes-pm-directory' for thread in threading.enumerate())


async def main():
    runner, adapter = runner_and_adapter()
    package = type(adapter).__module__.rsplit('.', 1)[0]
    ManagementClient = importlib.import_module(package + '.transport').ManagementClient
    client = ManagementClient(state, 'synthetic-bootstrap-owner-token')
    unavailable(client)
    transport, started, release = external_transport(adapter)
    context = nullcontext()
    secret_token = None
    if scenario == 'wrong_home':
        wrong_home = scratch / 'other-home'
        wrong_home.mkdir()
        context = _profile_runtime_scope(wrong_home, {'HERMES_FIXTURE_OWNER_TOKEN': 'synthetic-bootstrap-owner-token'})
    elif scenario == 'wrong_secret_scope':
        from agent.secret_scope import set_secret_scope
        secret_token = set_secret_scope({'HERMES_FIXTURE_OWNER_TOKEN': 'synthetic-bootstrap-owner-token'}, profile_home=str(scratch / 'other-home'))
    try:
        with context:
            if scenario in {'unload_during_verification', 'disconnect_during_verification'}:
                pending = asyncio.create_task(adapter.connect())
                assert await asyncio.to_thread(started.wait, 3), 'Connection did not verify the actual bot identity.'
                disconnect = None
                if scenario == 'unload_during_verification':
                    assert plugins.unload('ghost-hermes-pm')
                else:
                    disconnect = asyncio.create_task(adapter.disconnect())
                    await asyncio.sleep(0)
                release.set()
                connected = await pending
                if disconnect is not None:
                    await disconnect
                unavailable(client)
                assert connected is False
                if disconnect is not None:
                    assert plugins.unload('ghost-hermes-pm')
                await unloaded(adapter, transport)
                return
            connected = await adapter.connect()
    finally:
        if secret_token is not None:
            from agent.secret_scope import reset_secret_scope
            reset_secret_scope(secret_token)
    if scenario == 'verified':
        assert connected and adapter.is_connected
        # No message or gateway dispatch hook has run; the authenticated bridge is already useful.
        snapshot = client.read_snapshot()
        assert snapshot['status'] == 'completed' and snapshot['profiles'] == [] and snapshot['requests'] == []
        inode = client.path.stat().st_ino
        assert await adapter.connect()
        assert client.read_snapshot()['version'] == snapshot['version']
        assert client.path.stat().st_ino == inode
        assert sum(thread.name == 'hermes-pm-directory' for thread in threading.enumerate()) == 1
        assert plugins.unload('ghost-hermes-pm')
        unavailable(client)
        await unloaded(adapter, transport)
        plugins.discover_and_load(force=True)
        runner, adapter = runner_and_adapter()
        transport, _, _ = external_transport(adapter)
        assert await adapter.connect()
        assert client.read_snapshot()['status'] == 'completed'
        runner.stopped.set()
        for _ in range(100):
            if not client.path.exists():
                break
            await asyncio.sleep(.01)
        unavailable(client)
    else:
        assert connected is (scenario != 'wrong_bot')
        unavailable(client)
    assert plugins.unload('ghost-hermes-pm')
    await unloaded(adapter, transport)
    unavailable(client)


asyncio.run(main())
