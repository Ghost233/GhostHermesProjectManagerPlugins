"""Pristine native SDK registry drives queue preparation and fixed delivery through the bridge."""
from pathlib import Path
import json
import os
import sys
import yaml

scratch = Path(sys.argv[1]).resolve()
sys.path.insert(0, str(scratch / 'readiness-fixtures'))
protected = [Path.home() / '.hermes', Path.home() / '.dsh', Path.home() / '.codex']

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
from readiness_support import ReadyManager as Manager
from ghost_hermes_pm.transport import ManagementServer
sys.path.insert(0, str(scratch / 'queue-fixtures'))
from test_task_execution import accepted, adapter_for
from test_directory import OWNER, make_repo
from test_repository_queue import acknowledge, commit_repo, queue_adapter
from test_requests import ISSUE
from hermes_cli.plugins import get_plugin_manager
from tools.registry import registry

participant = VerifiedIdentity('fixture:lead', 'fixture-native-participant')
repo, head = commit_repo(scratch / 'repo')
class Source:
    def read_issue(self, url):
        return {**ISSUE, 'url': url, 'body': ISSUE['body'] + '\nNew source context.', 'updated_at': '2026-10-07T04:00:00Z'}
with Manager(state, owner_identity_ref=OWNER.subject, dsh_adapter=queue_adapter(scratch), delivery_source=Source()) as authority:
    request_id = accepted(authority, repo)
    second = acknowledge(authority)
    with ManagementServer(authority, {os.environ['HERMES_FIXTURE_PARTICIPANT_TOKEN']: participant}):
        plugin_manager = get_plugin_manager()
        plugin_manager.discover_and_load()
        def task(body, target=request_id):
            return json.loads(registry.dispatch('hermes_pm_task', {'request_id': target, **body}, scope=str(home)))
        plan = {'branch': 'main', 'commit': head, 'dependencies': [], 'issue_updated_at': ISSUE['updated_at']}
        assert task({'action': 'prepare', 'plan': plan})['preparation']['status'] == 'ready'
        assert task({'action': 'prepare', 'plan': plan}, second)['preparation']['status'] == 'ready'
        first = task({'action': 'start'})
        assert first['status'] == 'running', first
        waiting = task({'action': 'start'}, second)
        assert waiting['status'] == 'rejected' and waiting['code'] == 'repository_busy', waiting
        session = first['session']
        (scratch / 'queue-observed.json').write_text(json.dumps({session['thread_id']: {'status': {'type': 'idle'}, 'turns': [{'id': session['turn_id'],
            'status': 'completed', 'itemsView': 'full', 'items': [{'type': 'commandExecution', 'id': 'sdk-test', 'command': 'python -m pytest -q',
                'cwd': str(repo), 'status': 'completed', 'exitCode': 0, 'aggregatedOutput': '1 passed'}]}]}}))
        report = {'source_commit': head, 'issue_updated_at': ISSUE['updated_at'], 'criteria': [{'text': ISSUE['body'], 'test_item_ids': ['sdk-test']}]}
        delivered = task({'action': 'delivery', 'report': report})
        assert delivered['task_delivery'] == 'delivered' and delivered['repository_released'], delivered
        assert task({'action': 'refresh'}, second)['execution'] == 'running'
        source = task({'action': 'source'}, second)
        assert source['issue_source']['status'] == 'changed' and source['accepted_scope'] == ISSUE, source
        assert task({'action': 'prepare', 'actor': OWNER.subject, 'plan': plan})['status'] == 'rejected'
        assert plugin_manager.unload('ghost-hermes-pm')
print('native load, Dashboard bridge, restart, teardown: OK')
