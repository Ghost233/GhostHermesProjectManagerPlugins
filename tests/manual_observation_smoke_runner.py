"""Pristine native SDK registry observes a synthetic original service with no control frames."""
from pathlib import Path
import json
import os
import sys
import yaml

scratch = Path(sys.argv[1]).resolve()
protected = [Path.home() / '.hermes', Path.home() / '.codex']

def audit(event, args):
    if event == 'open' and isinstance(args[0], (str, bytes)):
        path = Path(os.fsdecode(args[0])).resolve()
        if any(path.is_relative_to(p) for p in protected) or (path.name == '.env' and not path.is_relative_to(scratch)):
            raise RuntimeError('Native control smoke refused real home or credential access.')
    if event == 'socket.connect' and isinstance(args[1], tuple):
        raise RuntimeError('Native control smoke refused external network access.')

sys.addaudithook(audit)
os.environ['HERMES_SKIP_PM_BOOTSTRAP'] = '1'
os.environ['HERMES_DISABLE_PROJECT_PLUGINS'] = '1'
home = scratch / 'home'
state = scratch / 'state'
settings = {'state_dir': str(state), 'participant_credential_ref': 'native:HERMES_FIXTURE_PARTICIPANT_TOKEN'}
(home / 'config.yaml').write_text(yaml.safe_dump({'plugins': {'enabled': ['ghost-hermes-pm'],
    'entries': {'ghost-hermes-pm': {'settings': settings}}}}))
sys.path.insert(0, str(home / 'plugins' / 'ghost-hermes-pm'))
from ghost_hermes_pm import Manager, VerifiedIdentity
from ghost_hermes_pm.transport import ManagementServer
sys.path.insert(0, str(scratch / 'manual-fixtures'))
from test_task_execution import accepted, adapter_for
from test_directory import OWNER, make_repo
from test_manual_observation import observer, source, manual_state, READ_ONLY
from test_requests import ISSUE
from hermes_cli.plugins import get_plugin_manager
from tools.registry import registry

participant = VerifiedIdentity('fixture:lead', 'fixture-native-participant')
repo = make_repo(scratch / 'repo')
peer = scratch / 'manual-peer'
manual_state(peer, repo)
with Manager(state, owner_identity_ref=OWNER.subject, observation_adapters={'local:manual-daemon': observer(peer)}) as authority:
    request_id = accepted(authority, repo)
    authority.register_observation_source(OWNER, source())
    with ManagementServer(authority, {os.environ['HERMES_FIXTURE_PARTICIPANT_TOKEN']: participant}):
        plugin_manager = get_plugin_manager()
        plugin_manager.discover_and_load()
        result = json.loads(registry.dispatch('hermes_pm_observe', {'scope': 'mono'}, scope=str(home)))
        assert result['manual_sessions'][0]['state'] == 'active', result
        assert result['manual_sessions'][0]['control'] == 'observe_only'
        forged = json.loads(registry.dispatch('hermes_pm_observe', {'scope': 'mono', 'actor': OWNER.subject}, scope=str(home)))
        assert forged['status'] == 'rejected', forged
        assert plugin_manager.unload('ghost-hermes-pm')
assert all(json.loads(line)['method'] in READ_ONLY for line in (peer / 'manual-wire.jsonl').read_text().splitlines())
print('native load, Dashboard bridge, restart, teardown: OK')
