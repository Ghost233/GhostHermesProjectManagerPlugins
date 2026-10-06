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
    def get_config(self, key, default=None):
        return self.settings.get(key, default)
    def register_tool(self, **kwargs):
        self.tools[kwargs['name']] = kwargs['handler']
    def register_command(self, name, handler, **kwargs):
        self.commands[name] = handler
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
