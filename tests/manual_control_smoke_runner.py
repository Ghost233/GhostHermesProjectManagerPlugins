"""Pristine SDK registry controls only an owner-granted synthetic original thread."""
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
sys.path.insert(0, str(scratch / 'takeover-fixtures'))
from test_task_execution import accepted, adapter_for
from test_directory import OWNER, make_repo
from test_manual_control import adapters, original_state, setup, ORIGINAL_THREAD, ORIGINAL_TURN
from test_requests import ISSUE
from hermes_cli.plugins import get_plugin_manager
from tools.registry import registry

participant = VerifiedIdentity('fixture:lead', 'fixture-native-participant')
repo = make_repo(scratch / 'repo')
peer = scratch / 'original'
original_state(peer, repo)
read, control = adapters(peer)
with Manager(state, owner_identity_ref=OWNER.subject, observation_adapters={'local:manual-daemon': read}, control_adapters={'manual-daemon': control}) as authority:
    request_id, observed = setup(authority, repo)
    authority.take_over_session(OWNER, request_id, observed['id'], 'sdk-current-work', ORIGINAL_TURN)
    with ManagementServer(authority, {os.environ['HERMES_FIXTURE_PARTICIPANT_TOKEN']: participant}):
        plugin_manager = get_plugin_manager()
        plugin_manager.discover_and_load()
        def task(body):
            return json.loads(registry.dispatch('hermes_pm_task', {'request_id': request_id, **body}, scope=str(home)))
        assert task({'action': 'append', 'instruction_id': 'sdk-original-append', 'text': 'Finish only this work.', 'expected_turn_id': ORIGINAL_TURN})['status'] == 'accepted'
        assert task({'action': 'return', 'grant_id': 'sdk-current-work'})['status'] == 'returned'
        assert task({'action': 'refresh'})['execution'] == 'running'
        assert task({'action': 'append', 'instruction_id': 'sdk-late-append', 'text': 'Future work.', 'expected_turn_id': ORIGINAL_TURN})['status'] == 'rejected'
        assert task({'action': 'takeover', 'grant_id': 'bot-takeover', 'manual_session_id': observed['id'], 'expected_turn_id': ORIGINAL_TURN})['code'] == 'forbidden'
        assert plugin_manager.unload('ghost-hermes-pm')
methods = [json.loads(line).get('method') for line in (peer / 'original-wire.jsonl').read_text().splitlines()]
assert methods.count('turn/steer') == 1 and not {'thread/start', 'thread/resume', 'thread/fork', 'turn/interrupt'} & set(methods)
print('native load, Dashboard bridge, restart, teardown: OK')
