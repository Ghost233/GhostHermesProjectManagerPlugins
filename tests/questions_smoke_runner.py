"""Pristine native SDK registry drives original-session controls against a JSONL peer."""
from pathlib import Path
import json
import os
import sys
import yaml

scratch = Path(sys.argv[1]).resolve()
sys.path.insert(0, str(scratch / 'readiness-fixtures'))
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
from readiness_support import ReadyManager as Manager
from ghost_hermes_pm.transport import ManagementServer
sys.path.insert(0, str(scratch / 'control-fixtures'))
from test_task_execution import accepted
from test_questions import question_adapter, emit, user_question
from test_directory import OWNER, make_repo
from hermes_cli.plugins import get_plugin_manager
from tools.registry import registry
from ghost_hermes_pm.transport import ManagementClient
from ghost_hermes_pm.dashboard import create_router
from fastapi import FastAPI
from fastapi.testclient import TestClient

participant = VerifiedIdentity('fixture:lead', 'fixture-native-participant')
with Manager(state, owner_identity_ref=OWNER.subject, codex_adapter=question_adapter(scratch)) as authority:
    request_id = accepted(authority, make_repo(scratch / 'repo'))
    with ManagementServer(authority, {os.environ['HERMES_FIXTURE_PARTICIPANT_TOKEN']: participant,
                                     os.environ['HERMES_FIXTURE_OWNER_TOKEN']: OWNER}):
        plugin_manager = get_plugin_manager()
        plugin_manager.discover_and_load()
        def task(body):
            return json.loads(registry.dispatch('hermes_pm_task', {'request_id': request_id, **body}, scope=str(home)))
        assert task({'action': 'start'})['status'] == 'running'
        emit(scratch, user_question())
        question = task({'action': 'refresh'})['human_requests'][0]
        body = {'action': 'answer', 'human_request_id': question['id'], 'reply_id': 'sdk-answer',
                'response': {'answers': {'colour': ['Blue']}}}
        rejected = task(body)
        assert rejected['status'] == 'rejected' and rejected['code'] == 'forbidden', rejected
        assert task({**body, 'actor': OWNER.subject})['status'] == 'rejected'
        app = FastAPI()
        app.include_router(create_router(lambda request: ManagementClient(state, os.environ['HERMES_FIXTURE_OWNER_TOKEN'])))
        browser = TestClient(app)
        answered = browser.post('/task', json={'request_id': request_id, **body})
        assert answered.status_code == 200 and answered.json()['reply']['sent'] == 'sent', answered.text
        assert task({'action': 'refresh'})['human_requests'][0]['resolution'] == 'resolved'
        wire = list(map(json.loads, (scratch / 'wire.jsonl').read_text().splitlines()))
        assert [r for r in wire if 'method' not in r] == [{'id': 8, 'result': {'answers': {'colour': {'answers': ['Blue']}}}}]
        assert plugin_manager.unload('ghost-hermes-pm')
print('native load, Dashboard bridge, restart, teardown: OK')
