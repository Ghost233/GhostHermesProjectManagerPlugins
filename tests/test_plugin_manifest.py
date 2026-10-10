from pathlib import Path
import asyncio
import json

import yaml

from test_plugin_entry import Context, fixture_native_home, load_entry


ROOT = Path(__file__).resolve().parents[1]


def test_manifest_declares_every_registered_tool(tmp_path, monkeypatch):
    fixture_native_home(monkeypatch, tmp_path / "native-home")
    context = Context({})

    load_entry().register(context)
    manifest = yaml.safe_load((ROOT / "plugin.yaml").read_text())

    assert set(manifest["provides_tools"]) == set(context.tools)


def test_unconfigured_supervision_registration_does_not_enable_execution(tmp_path, monkeypatch):
    home = tmp_path / "native-home"
    fixture_native_home(monkeypatch, home)
    context = Context({})
    load_entry().register(context)

    result = json.loads(asyncio.run(context.tools["hermes_pm_supervise"]({})))

    assert result == {"status": "rejected", "code": "configuration_missing", "delivered": False}
    assert not context.cleanups
    assert not home.exists()
