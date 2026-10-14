"""Pristine SDK entry: actual selected memory in a distinct synthetic new session."""
from pathlib import Path
import json
import os
import sys
import yaml

scratch = Path(sys.argv[1]).resolve()
protected = [Path.home() / '.hermes', Path.home() / '.dsh', Path.home() / '.codex']


def audit(event, args):
    if event == 'open' and isinstance(args[0], (str, bytes)):
        path = Path(os.fsdecode(args[0])).resolve()
        if any(path.is_relative_to(p) for p in protected) or path.name == '.env' and not path.is_relative_to(scratch):
            raise RuntimeError('Memory smoke refused real home or credentials.')
    if event == 'socket.connect' and isinstance(args[1], tuple):
        raise RuntimeError('Memory smoke refused external network access.')


sys.addaudithook(audit)
os.environ['HERMES_SKIP_PM_BOOTSTRAP'] = '1'
os.environ['HERMES_DISABLE_PROJECT_PLUGINS'] = '1'
home, state = scratch / 'home', scratch / 'state'
(home / 'config.yaml').write_text(yaml.safe_dump({'plugins': {'enabled': ['ghost-hermes-pm'], 'entries': {
    'ghost-hermes-pm': {'settings': {'state_dir': str(state), 'participant_credential_ref': 'native:HERMES_FIXTURE_PARTICIPANT_TOKEN'}}}}}))
sys.path.insert(0, str(home / 'plugins' / 'ghost-hermes-pm'))
sys.path.insert(0, str(scratch / 'control-fixtures'))
from ghost_hermes_pm import Manager
from readiness_support import ReadyManager as Manager
from ghost_hermes_pm.transport import ManagementServer, ManagementClient
from test_directory import OWNER, make_repo
from test_task_execution import accepted, prepare_fixture
from test_repository_queue import acknowledge
from test_questions import emit, user_question
from test_knowledge import WIKI, local_provider
from test_project_memory import LEAD, QUESTION, memory_adapter, prepared_facts, completed_delivery
from hermes_cli.plugins import get_plugin_manager
from tools.registry import registry


def call(action, **details):
    return json.loads(registry.dispatch('hermes_pm_memory', {'action': action, 'details': details}, scope=str(home)))


with Manager(state, owner_identity_ref=OWNER.subject, dsh_adapter=memory_adapter(scratch),
             knowledge_providers={'local:fixture-wiki': local_provider(scratch)}) as authority:
    request_id = accepted(authority, make_repo(scratch / 'repo'))
    authority.start_task(OWNER, request_id)
    authority.apply_directory_change(OWNER, authority.read_snapshot(OWNER)['version'], {'profile': WIKI})
    with ManagementServer(authority, {'owner-entry': OWNER, os.environ['HERMES_FIXTURE_PARTICIPANT_TOKEN']: LEAD}):
        owner, lead = ManagementClient(state, 'owner-entry'), ManagementClient(state, os.environ['HERMES_FIXTURE_PARTICIPANT_TOKEN'])
        native = get_plugin_manager()
        native.discover_and_load()
        assert registry.get_entry('hermes_pm_memory', scope=str(home)) is not None
        query_id = prepared_facts(authority, owner, lead, request_id)
        emit(scratch, user_question(questions=[{'id': 'policy', 'header': 'Known policy', 'question': QUESTION,
            'isOther': True, 'isSecret': False, 'options': None}]))
        question = owner.refresh_task(request_id)['human_requests'][0]
        answered = call('answer', request_id=request_id, human_request_id=question['id'], query_id=query_id, material_ids=['retry:3'])
        assert answered['reply']['actor'] == LEAD.subject and answered['reply']['sent'] == 'sent', answered
        assert call('preference', profile_id='mono-lead', entry_id='fake-owner-choice', statement='Choose blue permanently.',
            scope={'kind': 'project', 'id': 'mono'})['code'] == 'forbidden'
        assert call('read', profile_id='mono-lead', actor=OWNER.subject)['code'] == 'invalid_change'
        completed_delivery(scratch, owner, request_id)
        curated = call('curate', profile_id='mono-lead', entry_id='accepted-policy', request_id=request_id,
            selection={'facts': [{'query_id': query_id, 'material_ids': ['retry:3']}], 'decisions': [], 'include_delivery': True})
        assert curated['facts'][0]['locator'].endswith('retry.md#L3'), curated
        selected = call('read', profile_id='mono-lead')
        assert selected['external_memory'] == 'unverified' and not selected['running_sessions_loaded']
        next_id = acknowledge(authority, suffix='native-memory-follow-up')
        prepare_fixture(authority, next_id, scratch / 'repo')
        assert call('load', request_id=next_id, entry_ids=['accepted-policy'])['status'] == 'prepared'
        started = json.loads(registry.dispatch('hermes_pm_task', {'action': 'start', 'request_id': next_id}, scope=str(home)))
        assert started['status'] == 'running', started
        tasks = {r['id']: r for r in owner.read_snapshot()['requests']}
        loaded = tasks[next_id]['memory_context']
        assert loaded['status'] == 'loaded' and loaded['thread_id'] != tasks[request_id]['session']['thread_id']
        wire = [r for r in map(json.loads, (scratch / 'wire.jsonl').read_text().splitlines()) if r.get('method') == 'fixture/start']
        assert len(wire) == 2 and 'Retry only definite failures.' in wire[-1]['params']['input'][0]['text']
        assert native.unload('ghost-hermes-pm') and registry.get_entry('hermes_pm_memory', scope=str(home)) is None
print('native load, Dashboard bridge, restart, teardown: OK')
