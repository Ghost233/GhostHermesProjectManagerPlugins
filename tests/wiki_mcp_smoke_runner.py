"""Actual native configuration/secret scope and bridge against one readonly MCP peer."""
from pathlib import Path
import asyncio
import json
import os
import sys
import yaml

scratch = Path(sys.argv[1]).resolve()
protected = [Path.home() / '.hermes', Path.home() / '.dsh', Path.home() / '.codex']
allowed_ports = set()
def audit(event, args):
    if event == 'open' and isinstance(args[0], (str, bytes)):
        path = Path(os.fsdecode(args[0])).resolve()
        if any(path.is_relative_to(p) for p in protected) or (path.name == '.env' and not path.is_relative_to(scratch)):
            raise RuntimeError('Native MCP smoke refused real home or credential access.')
    if event == 'socket.connect' and isinstance(args[1], tuple):
        if args[1][0] != '127.0.0.1' or args[1][1] not in allowed_ports:
            raise RuntimeError('Native MCP smoke refused a non-fixture network connection.')
sys.addaudithook(audit)
os.environ['HERMES_SKIP_PM_BOOTSTRAP'] = '1'
os.environ['HERMES_DISABLE_PROJECT_PLUGINS'] = '1'
home, state = scratch / 'home', scratch / 'state'
sys.path.insert(0, str(home / 'plugins' / 'ghost-hermes-pm'))
sys.path.insert(0, str(scratch / 'control-fixtures'))
from test_wiki_mcp import mcp_peer, provider_config, mcp_grant
from test_knowledge import WIKI
from test_directory import OWNER
from ghost_hermes_pm.transport import ManagementClient
from agent.secret_scope import set_secret_scope, reset_secret_scope

with mcp_peer() as peer:
    from urllib.parse import urlsplit
    allowed_ports.add(urlsplit(peer['url']).port)
    settings = {'manager_profile': 'default', 'state_dir': str(state), 'owner_identity_ref': OWNER.subject,
        'dashboard_credential_ref': 'native:HERMES_FIXTURE_OWNER_TOKEN',
        'knowledge_providers': provider_config(peer)}
    (home / 'config.yaml').write_text(yaml.safe_dump({'plugins': {'enabled': ['ghost-hermes-pm'],
        'entries': {'ghost-hermes-pm': {'settings': settings}}}}))
    from hermes_cli.plugins import get_plugin_manager
    from tools.registry import registry
    manager = get_plugin_manager()
    manager.discover_and_load()
    info = next(p for p in manager.list_plugins() if p['name'] == 'ghost-hermes-pm')
    assert info['enabled'] and info['error'] is None, info
    assert peer['calls'] == [] and not (state / 'manager.sock').exists()
    class GatewayFixture:
        def __init__(self): self.stopped = asyncio.Event()
        async def wait_for_shutdown(self): await self.stopped.wait()
    async def exercise():
        gateway = GatewayFixture()
        secret_scope = set_secret_scope({'HERMES_FIXTURE_OWNER_TOKEN': os.environ['HERMES_FIXTURE_OWNER_TOKEN'],
            'FIXTURE_WIKI_TOKEN': 'synthetic-wiki-token'}, profile_home=str(home))
        try:
            await manager.ainvoke_hook('pre_gateway_dispatch', event=object(), gateway=gateway)
        finally:
            reset_secret_scope(secret_scope)
        assert (state / 'manager.sock').exists(), 'Actual native startup must build the configured original Wiki provider.'
        client = ManagementClient(state, os.environ['HERMES_FIXTURE_OWNER_TOKEN'])
        client.apply_directory_change(0, {'profile': WIKI})
        client.register_knowledge_source(1, mcp_grant())
        # The bridge thread has no native secret scope: startup must retain the bound credential only in RAM.
        result = client.query_knowledge('fixture-wiki', 'sdk-original-wiki', 'retry delivery', ['corpus'])
        assert result['status'] == 'found' and result['requester'] == OWNER.subject, result
        assert 'Unknown delivery needs reconciliation.' in result['materials'][0]['text']
        assert 'synthetic-wiki-token' not in json.dumps(client.read_snapshot())
        assert [c['body']['method'] for c in peer['calls']] == ['initialize', 'notifications/initialized', 'tools/list', 'tools/call']
        gateway.stopped.set()
        for _ in range(100):
            if not (state / 'manager.sock').exists(): break
            await asyncio.sleep(.01)
        assert not (state / 'manager.sock').exists()
        assert manager.unload('ghost-hermes-pm')
        assert registry.get_entry('hermes_pm_knowledge', scope=str(home)) is None
    asyncio.run(exercise())
print('native load, Dashboard bridge, restart, teardown: OK')
