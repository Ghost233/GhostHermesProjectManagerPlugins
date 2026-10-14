from pathlib import Path

import yaml

from test_plugin_entry import current_sdk_entry


ROOT = Path(__file__).resolve().parents[1]


def test_manifest_declares_only_current_registered_capabilities(current_sdk_entry):
    manifest = yaml.safe_load((ROOT / "plugin.yaml").read_text())
    assert set(manifest['provides_tools']) == set(current_sdk_entry['tools']) == {
        'hermes_pm_snapshot', 'hermes_pm_repository_rework', 'hermes_pm_supervise'}
    assert set(manifest['provides_hooks']) == set(current_sdk_entry['hooks']) == {
        'pre_gateway_dispatch', 'pre_tool_call', 'post_tool_call'}
    assert current_sdk_entry['plugin']['error'] is None
    assert current_sdk_entry['plugin']['tools'] == 3 and current_sdk_entry['plugin']['hooks'] == 3
    assert current_sdk_entry['platform'] and not current_sdk_entry['legacy_command']
    assert current_sdk_entry['snapshot']['work'] == []


def test_sdk_loader_reports_missing_configuration_without_legacy_registration(current_sdk_entry):
    missing = current_sdk_entry['missing']
    assert 'configuration_missing' in missing['error']
    assert missing['tools'] == missing['hooks'] == missing['commands'] == 0
