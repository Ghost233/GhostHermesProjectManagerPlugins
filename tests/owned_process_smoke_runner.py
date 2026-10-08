"""Actual SDK loader and production owned-process transport, without host path repair."""
import asyncio
import json
import os
from pathlib import Path
import logging
import signal
import sys
import yaml

scratch = Path(sys.argv[1]).resolve()
protected_home = Path.home()
os.environ['HOME'] = str(scratch / 'os-home')
(scratch / 'os-home').mkdir()
def audit(event, args):
    if event == 'open' and isinstance(args[0], (str, bytes)):
        path = Path(os.fsdecode(args[0])).resolve()
        if any(path.is_relative_to(protected_home / p) for p in ('.hermes', '.config', '.local/state/hermes')) or (path.name == '.env' and not path.is_relative_to(scratch)):
            raise RuntimeError('Process smoke refused real configuration/credential files.')
    if event == 'socket.connect' and isinstance(args[1], tuple):
        raise RuntimeError('Process smoke refused external network access.')
sys.addaudithook(audit)
home = scratch / 'home'
plugin_root = home / 'plugins' / 'ghost-hermes-pm'
assert str(plugin_root) not in sys.path
os.environ['HERMES_SKIP_PM_BOOTSTRAP'] = '1'
os.environ['HERMES_DISABLE_PROJECT_PLUGINS'] = '1'
binding = {'app_id': 'cli_fixture', 'recipient_open_id': 'ou_lead', 'owner_open_id': 'ou_owner',
    'owner_native_ids': ['u_owner'], 'sender_tenant_key': 'tenant-owner', 'recipient_tenant_key': 'tenant-bot',
    'transport_tenant_key': 'tenant-app', 'chat_id': 'oc_fixture', 'project_id': 'mono',
    'profile_id': 'lead', 'repository': 'example-user/fixture', 'verification_ref': 'fixture:identity'}
settings = {'manager_profile': 'default', 'state_dir': str(scratch / 'state'), 'owner_identity_ref': 'fixture:owner',
    'dashboard_credential_ref': 'native:HERMES_FIXTURE_OWNER_TOKEN',
    'feishu_intake': {'enabled': True, 'verification_ref': 'fixture:owned-process', 'bindings': [binding]}}
(home / 'config.yaml').write_text(yaml.safe_dump({'plugins': {'enabled': ['ghost-hermes-pm'],
    'entries': {'ghost-hermes-pm': {'settings': settings}}}}))
service = scratch / 'process-service'
service.mkdir()
source = Path(__file__).with_name('feishu_network_boundary.py')
(service / 'feishu_network_boundary.py').write_bytes(source.read_bytes())
(service / 'sitecustomize.py').write_text("import sys\nif any('ghost_hermes_pm.owned_feishu_process' in arg for arg in sys.orig_argv):\n from pathlib import Path\n from feishu_network_boundary import install\n install(Path(__file__).parent)\n")
sys.path.insert(0, str(service))
from hermes_cli.plugins import get_plugin_manager
from gateway.run import GatewayRunner
from gateway.config import Platform, PlatformConfig, GatewayConfig
manager = get_plugin_manager()
host_logging = (logging.getLogRecordFactory(), logging.root.manager.disable, tuple(logging.root.handlers))
host_signal = signal.getsignal(signal.SIGUSR2)
manager.discover_and_load()
assert str(plugin_root) not in sys.path, 'SDK loading must not be repaired by the test.'


async def main():
    for index in range(2):
        runner = object.__new__(GatewayRunner)
        runner.adapters, runner._profile_adapters = {}, {}
        runner.config = GatewayConfig()
        runner._primary_profile_name = 'default'
        adapter = runner._create_adapter(Platform('hermes_feishu_pm'), PlatformConfig(enabled=True,
            extra={'app_id': 'cli_fixture', 'app_secret': 'synthetic-owned-process-secret'}))
        assert adapter is not None
        runner.adapters[Platform('hermes_feishu_pm')] = adapter
        received, inbound = [], asyncio.Event()
        async def on_message(event):
            received.append(event)
            inbound.set()
        adapter.set_message_handler(on_message)
        connected = await adapter.connect()
        assert connected, (adapter.fatal_error_code, sorted(p.name for p in service.iterdir()),
            (service / 'boundary-failure').read_text() if (service / 'boundary-failure').exists() else '')
        try:
            await asyncio.wait_for(inbound.wait(), timeout=3)
        except TimeoutError:
            assert False, (transport.events.qsize() if (transport := adapter.transport) else None,
                (service / 'websocket-ack').read_text() if (service / 'websocket-ack').exists() else 'no-ws-ack')
        assert [(e.message_id, e.text) for e in received] == [('om_process_receive_' + str(index + 1), '/fixture-owned-process')]
        transport = adapter.transport
        assert transport.process is not None and transport.process.returncode is None
        assert await transport.verify_identity(binding) == {'app_id': 'cli_fixture', 'open_id': 'ou_lead'}
        sent = await transport.send({'chat_id': 'oc_fixture', 'reply_to': 'om_original',
            'text': 'Synthetic process delivery', 'mention_open_id': 'ou_owner', 'uuid': 'fixture-' + str(index)})
        assert sent['status'] == 'delivered' and sent['message_id'] == 'om_process_sent'
        process = transport.process
        await adapter.disconnect()
        assert process.returncode == 0 and transport.reader_task.done() and transport.event_task.done()
        assert manager.unload('ghost-hermes-pm')
        assert (logging.getLogRecordFactory(), logging.root.manager.disable, tuple(logging.root.handlers)) == host_logging
        assert signal.getsignal(signal.SIGUSR2) == host_signal
        if index == 0:
            manager.discover_and_load(force=True)
            assert str(plugin_root) not in sys.path
    records = [json.loads(row) for row in (service / 'service-requests.jsonl').read_text().splitlines()]
    assert len(records) == 2
    for row in records:
        post = json.loads(row['body']['content'])
        assert post['zh_cn']['content'][0][0] == {'tag': 'at', 'user_id': 'ou_owner'}
    (scratch / 'native-smoke-result.json').write_text(json.dumps({'native_smoke': 'passed'}))


asyncio.run(main())
