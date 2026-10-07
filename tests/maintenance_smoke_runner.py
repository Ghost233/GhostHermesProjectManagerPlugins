"""Actual pristine SDK registrations/reload in an owned artificial gateway and home."""
from pathlib import Path
import json
import os
import sys
import yaml

scratch = Path(sys.argv[1]).resolve()
def audit(event, args):
    if event == 'open' and isinstance(args[0], (str, bytes)):
        path = Path(os.fsdecode(args[0])).resolve()
        if any(path.is_relative_to(Path.home() / p) for p in ('.hermes', '.codex')) or path.name == '.env' and not path.is_relative_to(scratch):
            raise RuntimeError('Maintenance smoke refused production homes and credentials.')
    if event == 'socket.connect' and isinstance(args[1], tuple):
        raise RuntimeError('Maintenance smoke refused external network.')
sys.addaudithook(audit)
os.environ['HERMES_SKIP_PM_BOOTSTRAP'] = '1'
os.environ['HERMES_DISABLE_PROJECT_PLUGINS'] = '1'
home = scratch / 'home'
plugin = home / 'plugins' / 'ghost-hermes-pm'
(home / 'config.yaml').write_text(yaml.safe_dump({'plugins': {'enabled': ['ghost-hermes-pm']}}))
sys.path.insert(0, str(plugin))
from hermes_cli.plugins import get_plugin_manager
from tools.registry import registry
native = get_plugin_manager()
native.discover_and_load()
loaded = json.loads(registry.dispatch('hermes_pm_loaded_version', {}, scope=str(home)))
assert loaded['plugin_version'] == '0.1.0', loaded
import asyncio
import threading
from gateway.control_socket import GatewayControlServer, reload_gateway_plugins
from gateway.run_plugin_rewire import reload_plugins_verb
loop = asyncio.new_event_loop()
ready = threading.Event()
class Runner:
    _served_profile_homes = {}
    _primary_profile_name = 'default'
    adapters = {}
runner = Runner()
server = None
def run_control():
    global server
    asyncio.set_event_loop(loop)
    async def start():
        global server
        server = GatewayControlServer(home, verb_handlers={'reload-plugins': reload_plugins_verb(runner, loop)})
        assert await server.start()
        ready.set()
    loop.run_until_complete(start())
    loop.run_forever()
thread = threading.Thread(target=run_control, daemon=True)
thread.start()
assert ready.wait(5)
old_entry = registry.get_entry('hermes_pm_loaded_version', scope=str(home))
try:
    target_file = plugin / 'ghost_hermes_pm' / 'native.py'
    text = target_file.read_text().replace('return json.dumps(registered_release)', "return json.dumps({**registered_release, 'native_probe': 'target-v2'})")
    target_file.write_text(text)
    manifest = plugin / 'plugin.yaml'
    manifest.write_text(manifest.read_text().replace('version: "0.1.0"', 'version: "0.2.0"'))
    ack = reload_gateway_plugins(home, profile_home=home)
    assert ack['reloaded'] is True, ack
    new = json.loads(registry.dispatch('hermes_pm_loaded_version', {}, scope=str(home)))
    assert new.get('native_probe') == 'target-v2', new
    assert new['load_id'] != loaded['load_id']
    assert registry.get_entry('hermes_pm_loaded_version', scope=str(home)) is not old_entry
finally:
    asyncio.run_coroutine_threadsafe(server.stop(), loop).result(timeout=5)
    loop.call_soon_threadsafe(loop.stop)
    thread.join(timeout=5)
print('native load, Dashboard bridge, restart, teardown: OK')
