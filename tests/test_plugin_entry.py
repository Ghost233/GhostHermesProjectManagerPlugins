import importlib.util
from pathlib import Path
import tempfile
import json

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


def load_entry():
    spec = importlib.util.spec_from_file_location('fixture_plugin', ROOT / '__init__.py', submodule_search_locations=[str(ROOT)])
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_formal_plugin_registers_synchronously_without_configuration_side_effects():
    ctx = Context({})
    result = load_entry().register(ctx)
    assert result is None
    status = json.loads(ctx.tools['hermes_pm_snapshot']({}))
    assert status['runtime'] == 'configuring'
    assert status['execution'] == 'not_enabled'
    assert 'hermes-pm' in ctx.commands
    assert not ctx.cleanups
    assert 'hermes_feishu_pm' in ctx.platforms
    assert 'feishu' not in ctx.platforms


def test_native_reader_with_participant_secret_but_missing_state_path_stays_configuring(monkeypatch):
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


def test_participant_snapshot_rejects_owner_token_alias_without_revealing_secret(tmp_path, monkeypatch):
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
                settings.pop('dashboard_credential_ref')
                unknown_owner = json.loads(ctx.tools['hermes_pm_snapshot']({}))
                assert unknown_owner['status'] == 'unverified'
                assert unknown_owner['code'] == 'forbidden'
                assert 'projects' not in unknown_owner
                settings['participant_credential_ref'] = 'native:FIXTURE_PARTICIPANT_TOKEN'
                scoped = json.loads(ctx.tools['hermes_pm_snapshot']({}))
                assert [p['id'] for p in scoped['projects']] == ['mono']


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
