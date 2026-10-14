import importlib.util
from pathlib import Path
import tempfile
import json
import os
import shutil
import subprocess
import sys
import pytest

ROOT = Path(__file__).resolve().parents[1]


class Context:
    profile_name = 'fixture-manager'
    def __init__(self, settings):
        self.settings = settings
        self.tools = {}
        self.commands = {}
        self.cleanups = []
        self.hooks = {}
        self.platform_handlers = {}
        self.platforms = {}
    def get_config(self, key, default=None):
        return self.settings.get(key, default)
    def register_tool(self, **kwargs):
        self.tools[kwargs['name']] = kwargs['handler']
    def register_command(self, name, handler, **kwargs):
        self.commands[name] = handler
    def register_hook(self, name, callback):
        self.hooks[name] = callback
    def register_platform_handler(self, name, factory):
        self.platform_handlers[name] = factory
    def register_platform(self, **entry):
        self.platforms[entry['name']] = entry
    def on_unload(self, callback):
        self.cleanups.append(callback)


def fixture_native_home(monkeypatch, home):
    from types import ModuleType
    import sys
    constants = ModuleType('hermes_constants')
    constants.get_hermes_home = lambda: home
    monkeypatch.setitem(sys.modules, 'hermes_constants', constants)


def load_entry():
    spec = importlib.util.spec_from_file_location('fixture_plugin', ROOT / '__init__.py', submodule_search_locations=[str(ROOT)])
    module = importlib.util.module_from_spec(spec)
    import sys
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope='session')
def current_sdk_entry():
    """Observe the current entry through the original SDK loader and public registries."""
    from sdk_source_integrity import source_snapshot
    sdk = Path(os.environ['HERMES_TEST_SDK_ROOT']).resolve(strict=True)
    before = source_snapshot(sdk)
    with tempfile.TemporaryDirectory(prefix='hpe-', dir='/tmp') as runtime:
        scratch = Path(runtime).resolve()
        home = scratch / 'home'
        plugin = home / 'plugins' / 'ghost-hermes-pm'
        plugin.mkdir(parents=True)
        for name in ('__init__.py', 'plugin.yaml', 'ghost_hermes_pm'):
            source = ROOT / name
            if source.is_dir():
                shutil.copytree(source, plugin / name, ignore=shutil.ignore_patterns('__pycache__'))
            else:
                shutil.copy2(source, plugin / name)
        github = scratch / 'github-config'
        github.mkdir(mode=0o700)
        import yaml
        settings = {'state_dir': str(scratch / 'state'), 'simple_development': {
            'enabled': True, 'github_account': 'fixture-user', 'github_config_dir': str(github)}}
        (home / 'config.yaml').write_text(yaml.safe_dump({'plugins': {'enabled': ['ghost-hermes-pm'],
            'entries': {'ghost-hermes-pm': {'settings': settings}}}}))
        script = '''import json, sys
from pathlib import Path
def audit(event, args):
    if event == 'socket.connect':
        raise RuntimeError('Entry validation cannot contact external services.')
sys.addaudithook(audit)
import hermes_bootstrap
import yaml
from hermes_cli.plugins import get_plugin_manager, get_plugin_commands, invoke_hook
from gateway.platform_registry import platform_registry
from tools.registry import registry
manager = get_plugin_manager()
manager.discover_and_load()
def facts():
    row = next(row for row in manager.list_plugins() if row['name'] == 'ghost-hermes-pm')
    names = [name for name in registry.get_all_tool_names() if name.startswith('hermes_pm_')]
    return {'plugin': row, 'tools': names,
        'platform': platform_registry.get('hermes_feishu_pm') is not None,
        'legacy_command': 'hermes-pm' in get_plugin_commands(),
        'snapshot': json.loads(registry.dispatch('hermes_pm_snapshot', {})),
        'hooks': {name: invoke_hook(name, event=None, gateway=None, tool_name='fixture', args={})
            for name in ('pre_gateway_dispatch', 'pre_tool_call', 'post_tool_call')}}
first = facts()
manager.unload()
assert not any(name.startswith('hermes_pm_') for name in registry.get_all_tool_names())
assert platform_registry.get('hermes_feishu_pm') is None
manager.discover_and_load(force=True)
second = facts()
assert second == first
manager.unload()
config = Path(sys.argv[2])
settings = yaml.safe_load(config.read_text())
settings['plugins']['entries']['ghost-hermes-pm']['settings']['simple_development']['enabled'] = False
missing_state = config.parent.parent / 'missing-state'
settings['plugins']['entries']['ghost-hermes-pm']['settings']['state_dir'] = str(missing_state)
config.write_text(yaml.safe_dump(settings))
manager.discover_and_load(force=True)
first['missing'] = next(row for row in manager.list_plugins() if row['name'] == 'ghost-hermes-pm')
assert not any(name.startswith('hermes_pm_') for name in registry.get_all_tool_names())
assert platform_registry.get('hermes_feishu_pm') is None
assert 'hermes-pm' not in get_plugin_commands()
assert not missing_state.exists()
manager.unload()
Path(sys.argv[1]).write_text(json.dumps(first))
'''
        environment = {'PATH': os.environ.get('PATH', '/usr/bin:/bin'),
            'HOME': str(scratch), 'HERMES_HOME': str(home), 'XDG_STATE_HOME': str(scratch / 'os-state'),
            'XDG_CONFIG_HOME': str(scratch / 'os-config'), 'HERMES_BUNDLED_PLUGINS': str(home / 'empty-bundled'),
            'HERMES_SKIP_PM_BOOTSTRAP': '1', 'HERMES_DISABLE_PROJECT_PLUGINS': '1',
            'PYTHONDONTWRITEBYTECODE': '1', 'PYTHONPATH': str(sdk)}
        result_path = scratch / 'result.json'
        result = subprocess.run([sys.executable, '-c', script, str(result_path), str(home / 'config.yaml')], cwd=scratch,
            env=environment, text=True, capture_output=True, timeout=60)
        assert result.returncode == 0, result.stdout + result.stderr
        assert source_snapshot(sdk) == before, 'Plugin registration must not change SDK bytes.'
        return json.loads(result_path.read_text())


@pytest.mark.parametrize('configuration', [None, {}, {'enabled': False}, {'enabled': 'true'}, []])
def test_formal_plugin_rejects_missing_current_configuration_without_side_effects(tmp_path, monkeypatch, configuration):
    fixture_native_home(monkeypatch, tmp_path / 'native-home')
    state = tmp_path / 'state'
    ctx = Context({'simple_development': configuration, 'state_dir': str(state),
        'manager_profile': Context.profile_name, 'owner_identity_ref': 'fixture-owner',
        'dashboard_credential_ref': 'native:FIXTURE_OLD_OWNER'})
    with pytest.raises(Exception) as error:
        load_entry().register(ctx)
    assert type(error.value).__name__ == 'ManagementError'
    assert error.value.code == 'configuration_missing'
    assert not ctx.tools and not ctx.hooks and not ctx.commands and not ctx.platforms
    assert not ctx.cleanups
    assert not state.exists() and not (tmp_path / 'native-home').exists()


@pytest.mark.retired_legacy_manager
def test_native_reader_with_participant_secret_but_missing_state_path_stays_configuring(tmp_path, monkeypatch):
    fixture_native_home(monkeypatch, tmp_path / 'native-home')
    from types import ModuleType
    import sys
    secrets = ModuleType('agent.secret_scope')
    secrets.get_secret = lambda ref: 'synthetic-participant-token' if ref == 'FIXTURE_PARTICIPANT_TOKEN' else None
    monkeypatch.setitem(sys.modules, 'agent.secret_scope', secrets)
    ctx = Context({'participant_credential_ref': 'native:FIXTURE_PARTICIPANT_TOKEN'})
    load_entry().register(ctx)
    result = json.loads(ctx.tools['hermes_pm_snapshot']({}))
    command = json.loads(ctx.commands['hermes-pm'](''))
    assert result['runtime'] == 'configuring'
    assert result['execution'] == 'not_enabled'
    assert command == result
    assert not ctx.cleanups


@pytest.mark.retired_legacy_manager
def test_participant_snapshot_rejects_owner_token_alias_without_revealing_secret(tmp_path, monkeypatch):
    fixture_native_home(monkeypatch, tmp_path / 'native-home')
    from types import ModuleType
    import sys
    from ghost_hermes_pm import Manager, VerifiedIdentity
    from ghost_hermes_pm.transport import ManagementServer
    from test_directory import OWNER, make_repo, registration
    secrets = ModuleType('agent.secret_scope')
    values = {'FIXTURE_OWNER_TOKEN': 'synthetic-owner-token', 'FIXTURE_OWNER_ALIAS': 'synthetic-owner-token',
              'FIXTURE_PARTICIPANT_TOKEN': 'synthetic-participant-token'}
    secrets.get_secret = values.get
    monkeypatch.setitem(sys.modules, 'agent.secret_scope', secrets)
    with tempfile.TemporaryDirectory(prefix='hpm-alias-', dir='/tmp') as state:
        with Manager(state, owner_identity_ref=OWNER.subject) as manager:
            manager.apply_directory_change(OWNER, 0, registration(make_repo(tmp_path / 'mono')))
            second = registration(make_repo(tmp_path / 'other'), 'other', 'other-lead')
            second['profile']['identity_ref'] = 'fixture:other'
            manager.apply_directory_change(OWNER, 1, second)
            participant = VerifiedIdentity('fixture:lead', 'fixture-participant-entry')
            with ManagementServer(manager, {values['FIXTURE_OWNER_TOKEN']: OWNER, values['FIXTURE_PARTICIPANT_TOKEN']: participant}):
                settings = {'state_dir': state, 'dashboard_credential_ref': 'native:FIXTURE_OWNER_TOKEN',
                            'participant_credential_ref': 'native:FIXTURE_OWNER_ALIAS'}
                ctx = Context(settings)
                load_entry().register(ctx)
                result = json.loads(ctx.tools['hermes_pm_snapshot']({}))
                assert result['status'] == 'unverified'
                assert 'projects' not in result
                assert 'synthetic-owner-token' not in str(result)
                role_alias = json.loads(ctx.tools['hermes_pm_collaborate']({'action': 'read_routes', 'details': {}}))
                assert role_alias['status'] == 'rejected' and role_alias['code'] == 'forbidden'
                assert 'synthetic-owner-token' not in str(role_alias)
                settings.pop('dashboard_credential_ref')
                unknown_owner = json.loads(ctx.tools['hermes_pm_snapshot']({}))
                assert unknown_owner['status'] == 'unverified'
                assert unknown_owner['code'] == 'forbidden'
                assert 'projects' not in unknown_owner
                settings['participant_credential_ref'] = 'native:FIXTURE_PARTICIPANT_TOKEN'
                scoped = json.loads(ctx.tools['hermes_pm_snapshot']({}))
                assert [p['id'] for p in scoped['projects']] == ['mono']


@pytest.mark.retired_legacy_manager
def test_management_profile_cli_registration_does_not_start_or_lease_authority(monkeypatch):
    from types import ModuleType
    import sys
    secrets = ModuleType('agent.secret_scope')
    secrets.get_secret = lambda ref: {'FIXTURE_OWNER_TOKEN': 'fixture-owner-token',
                                    'FIXTURE_PARTICIPANT_TOKEN': 'fixture-participant-token'}.get(ref)
    monkeypatch.setitem(sys.modules, 'agent.secret_scope', secrets)
    with tempfile.TemporaryDirectory(prefix='hpm-cli-', dir='/tmp') as runtime:
        state = Path(runtime) / 'state'
        constants = ModuleType('hermes_constants')
        constants.get_hermes_home = lambda: Path(runtime) / 'native-home'
        monkeypatch.setitem(sys.modules, 'hermes_constants', constants)
        ctx = Context({'manager_profile': Context.profile_name, 'state_dir': str(state), 'owner_identity_ref': 'fixture-owner',
                       'dashboard_credential_ref': 'native:FIXTURE_OWNER_TOKEN',
                       'participant_credential_ref': 'native:FIXTURE_PARTICIPANT_TOKEN'})
        try:
            assert load_entry().register(ctx) is None
            assert not state.exists()
            status = json.loads(ctx.tools['hermes_pm_snapshot']({}))
            assert status['runtime'] == 'manager_unavailable'
            assert status['execution'] == 'not_enabled'
            assert 'pre_gateway_dispatch' in ctx.hooks
        finally:
            for close in reversed(ctx.cleanups):
                close()
