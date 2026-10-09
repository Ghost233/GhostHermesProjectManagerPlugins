"""Original Hermes loader owns two configured DSH adapters without starting work."""
import asyncio
import json
import os
from pathlib import Path
import shutil
import sys
import yaml

scratch = Path(sys.argv[1]).resolve()
protected = Path.home()
os.environ['HOME'] = str(scratch / 'os-home')
(scratch / 'os-home').mkdir()
def audit(event, args):
    if event == 'open' and isinstance(args[0], (str, bytes)):
        path = Path(os.fsdecode(args[0])).resolve()
        if any(path.is_relative_to(protected / name) for name in ('.hermes', '.dsh', '.codex')):
            raise RuntimeError('Mapped executor smoke refused real host configuration.')
    if event == 'socket.connect' and isinstance(args[1], tuple):
        raise RuntimeError('Mapped executor smoke refused external network access.')
    if event == 'subprocess.Popen':
        raise RuntimeError('Mapped executor smoke refused to start an execution process.')
sys.addaudithook(audit)
os.environ['HERMES_SKIP_PM_BOOTSTRAP'] = '1'
os.environ['HERMES_DISABLE_PROJECT_PLUGINS'] = '1'
home = scratch / 'home'
runtime_package_root = Path(os.environ['DSH_TEST_SDK_ROOT']).resolve(strict=True)
node_bin = str(Path(shutil.which('node')).resolve(strict=True))
settings = {'manager_profile': 'default', 'state_dir': str(scratch / 'state'), 'owner_identity_ref': 'fixture:owner',
            'dashboard_credential_ref': 'native:HERMES_FIXTURE_OWNER_TOKEN', 'dsh_executors': {}}
for name in ('first', 'second'):
    workspace = scratch / (name + '-workspace')
    workspace.mkdir()
    reference = 'local:synthetic-' + name
    settings['dsh_executors'][reference] = {'service_ref': reference, 'dsh_home': str(scratch / (name + '-dsh-home')),
        'workspace': str(workspace), 'runtime_package_root': str(runtime_package_root), 'node_bin': node_bin}
(home / 'config.yaml').write_text(yaml.safe_dump({'plugins': {'enabled': ['ghost-hermes-pm'],
    'entries': {'ghost-hermes-pm': {'settings': settings}}}}))
captured = []
def capture_manager(module):
    original = module.Manager
    class CapturingManager(original):
        def __init__(self, *args, **kwargs):
            super().__init__(*args, **kwargs)
            captured.append(self)
    module.Manager = CapturingManager
from native_fixture_boundary import install
install(home / 'plugins' / 'ghost-hermes-pm', {'manager': capture_manager}, synthetic_readiness=False)
from hermes_cli.plugins import get_plugin_manager
plugin_manager = get_plugin_manager()
plugin_manager.discover_and_load()
class Gateway:
    def __init__(self):
        self.shutdown = asyncio.Event()
    async def wait_for_shutdown(self):
        await self.shutdown.wait()
async def main():
    for index in range(2):
        await plugin_manager.ainvoke_hook('pre_gateway_dispatch', event=object(), gateway=Gateway())
        assert len(captured) == index + 1
        authority = captured[-1]
        assert authority.dsh_adapter is None
        assert set(authority.dsh_adapters) == set(settings['dsh_executors'])
        for reference, adapter in authority.dsh_adapters.items():
            assert adapter.service_ref == reference
            assert adapter.transport == 'owned_native'
            assert adapter.workspace == settings['dsh_executors'][reference]['workspace']
            assert adapter.runtime_package_root == str(runtime_package_root)
            assert not adapter._closed and not adapter._transport._closed
            assert not adapter._transport._started and adapter._transport._process is None
            assert adapter._transport._socket is None and adapter._transport._reader is None
        assert plugin_manager.unload('ghost-hermes-pm')
        await asyncio.sleep(0)
        assert all(adapter._closed and adapter._transport._closed
                   and adapter._transport._process is None and adapter._transport._socket is None
                   for adapter in authority.dsh_adapters.values())
        assert not (scratch / 'state' / 'manager.sock').exists()
        if index == 0:
            plugin_manager.discover_and_load(force=True)
    assert all(not Path(config['dsh_home']).exists() for config in settings['dsh_executors'].values())
    (scratch / 'native-smoke-result.json').write_text(json.dumps({'native_smoke': 'passed'}))
asyncio.run(main())
