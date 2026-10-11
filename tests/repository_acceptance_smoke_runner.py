"""Original Hermes/DSH lifecycle; only model, GitHub and Feishu services are substituted."""
import asyncio
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import importlib
import json
import os
from pathlib import Path
import subprocess
import shlex
import signal
import psutil
import sys
import threading
import time
import types
import re

import yaml


scratch = Path(sys.argv[1]).resolve()
scenario = sys.argv[2]
rearranged = scenario in {'completion_rearranged', 'completion_rearranged_reaped'}
home, state, repo = scratch / 'home', scratch / 'state', scratch / 'repository'
home.chmod(0o700)
repo.mkdir()
for name in ('os-home', 'os-config', 'os-state'):
    (scratch / name).mkdir(mode=0o700)
os.environ.update(HOME=str(scratch / 'os-home'), XDG_CONFIG_HOME=str(scratch / 'os-config'),
    XDG_STATE_HOME=str(scratch / 'os-state'), HERMES_SKIP_PM_BOOTSTRAP='1', HERMES_DISABLE_PROJECT_PLUGINS='1',
    HERMES_FEISHU_TEXT_BATCH_DELAY_SECONDS='0.01', HERMES_FEISHU_TEXT_BATCH_SPLIT_DELAY_SECONDS='0.01')
subprocess.run(['git', 'init', '-q', '-b', 'fixture-delivery', str(repo)], check=True)
(repo / 'feature.py').write_text('def answer():\n    return 42\n')
(repo / 'test_feature.py').write_text('from feature import answer\ndef test_answer():\n    assert answer() == '
                                    + ('0' if scenario == 'fake_test_success' else '42') + '\n')
subprocess.run(['git', '-C', str(repo), 'add', '.'], check=True)
subprocess.run(['git', '-C', str(repo), '-c', 'user.name=Synthetic Fixture',
                '-c', 'user.email=fixture@example.invalid', 'commit', '-qm', 'Bounded fixture source'], check=True)
fixed = subprocess.run(['git', '-C', str(repo), 'rev-parse', 'HEAD'], capture_output=True, text=True, check=True).stdout.strip()
criterion = 'Verify the existing fixture feature and report its fixed source.'
body = '- [ ] ' + criterion
if scenario == 'merge_required':
    body += '\n- [ ] Must merge the PR before delivery.'
report = {'issue_updated_at': '2026-10-10T00:00:00Z',
          'criteria': [{'text': text, 'test_call_ids': ['fixture-dsh-test']} for text in
                       ([criterion, 'Must merge the PR before delivery.'] if scenario == 'merge_required' else [criterion])],
          'source_commit': fixed, 'pr_url': 'https://github.com/fixture-user/fixture/pull/9',
          'sync_branches': ['fixture-delivery'], 'fine_issue_urls': ['https://github.com/fixture-user/fixture/issues/8'],
          'review_call_ids': [], 'leftovers': [], 'test_files': ['test_feature.py']}
test_command = "'" + sys.executable + "' -I -B -m pytest --capture=sys -p no:cacheprovider -q test_feature.py"


class ModelService:
    def __init__(self):
        self.requests = []
        self.steps = {'worker': 0, 'dsh': 0}
        self.rearranged = False
        self.final_ready = threading.Event()
        self.final_release = threading.Event()
        service = self
        class Handler(BaseHTTPRequestHandler):
            def do_POST(self):
                payload = json.loads(self.rfile.read(int(self.headers['Content-Length'])))
                kind = 'worker' if payload['model'] == 'fixture-model' else 'dsh'
                names = [t['function']['name'] for t in payload.get('tools', [])]
                service.requests.append({'kind': kind, 'tools': names})
                service.steps[kind] += 1
                step = service.steps[kind]
                call = None
                if kind == 'worker':
                    if step == 1:
                        call = ('fixture-premature-complete', 'kanban_complete', {'summary': 'Unverified fixture delivery.'})
                    elif step == 2:
                        call = ('fixture-supervise', 'hermes_pm_supervise', {})
                    elif step == 3:
                        responses = [m for m in payload['messages'] if m.get('role') == 'tool' and m.get('tool_call_id') == 'fixture-supervise']
                        outcome = json.loads(responses[-1]['content'])
                        call = ('fixture-terminal', 'kanban_complete', {'summary': 'Verified fixture delivery.'}) if outcome.get('delivered') else (
                            'fixture-terminal', 'kanban_block', {'kind': 'needs_input', 'reason': 'Original acceptance remains unmet.'})
                    if scenario in {'completion_scope', 'completion_reload'} and step == 4:
                        service.final_ready.set()
                        if not service.final_release.wait(timeout=45):
                            raise AssertionError('Original worker final response was not released.')
                    if scenario == 'completion_proof_changed' and step == 3:
                        files=list((state/'owned-work').rglob('.hermes-native-cleanup.json'))
                        assert len(files)==1
                        proof=json.loads(files[0].read_text())
                        proof['generation']='foreign-generation'
                        files[0].write_text(json.dumps(proof))
                    if rearranged and step == 3 and not service.rearranged:
                        call = ('fixture-handoff-rearrange', 'kanban_block', {'kind': 'needs_input', 'reason': 'Original accepted work waits for the next native worker.'})
                    if scenario in {'completion_repeat', 'completion_proof_changed'} and step == 3:
                        call = ('fixture-repeat-supervise', 'hermes_pm_supervise', {})
                    elif scenario in {'completion_repeat', 'completion_proof_changed'} and step == 4:
                        responses = [m for m in payload['messages'] if m.get('role') == 'tool' and m.get('tool_call_id') == 'fixture-repeat-supervise']
                        content = responses[-1]['content']
                        if content == '[hermes note: this result is byte-identical to the hermes_pm_supervise result earlier this turn (tool_call_id fixture-supervise). Refer to that result; it has not changed. Args: {}]':
                            content = next(m['content'] for m in payload['messages'] if m.get('role') == 'tool' and m.get('tool_call_id') == 'fixture-supervise')
                        outcome = json.loads(content)
                        call = ('fixture-terminal', 'kanban_complete', {'summary': 'Verified repeated supervision.'}) if outcome.get('delivered') else (
                            'fixture-terminal', 'kanban_block', {'kind': 'needs_input', 'reason': 'Original repeated supervision is unconfirmed.'})
                    content = 'Original outer work has its verified terminal outcome.'
                else:
                    if step == 1:
                        call = ('fixture-dsh-skill', 'skill', {'name': 'fixture-matt'})
                    elif step == 2:
                        command = "printf '400 passed\\n'" if scenario == 'fake_test_success' else test_command
                        if scenario in {'completion_scope', 'completion_reload'}:
                            child = "const fs=require('fs'),p=process.env.TMPDIR+'/delivery-child'; fs.writeFileSync(p+'.json',JSON.stringify({pid:process.pid})); const timer=setInterval(()=>fs.appendFileSync(p+'.log','x'),30); setTimeout(()=>clearInterval(timer),25000)"
                            parent = "const c=require('child_process').spawn(process.execPath,['-e'," + json.dumps(child) + "],{stdio:'ignore'}); c.unref()"
                            command += ' && ' + shlex.join(['/opt/homebrew/bin/node', '-e', parent])
                        call = ('fixture-dsh-test', 'bash', {'description': 'Run the bounded fixture verification.', 'command': command})
                    content = 'HERMES_REPOSITORY_DELIVERY_JSON\n' + json.dumps(report)
                self.send_response(200)
                self.send_header('Content-Type', 'text/event-stream')
                self.end_headers()
                delta = {'tool_calls': [{'index': 0, 'id': call[0], 'type': 'function',
                    'function': {'name': call[1], 'arguments': json.dumps(call[2])}}]} if call else {'content': content}
                for part, reason in (({'role': 'assistant'}, None), (delta, None), ({}, 'tool_calls' if call else 'stop')):
                    chunk = {'id': 'fixture-completion', 'object': 'chat.completion.chunk', 'created': 1,
                             'model': payload['model'], 'choices': [{'index': 0, 'delta': part, 'finish_reason': reason}]}
                    self.wfile.write(('data: ' + json.dumps(chunk) + '\n\n').encode())
                self.wfile.write(b'data: [DONE]\n\n')
            def log_message(self, *_):
                pass
        self.server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.base_url = 'http://127.0.0.1:' + str(self.server.server_port) + '/v1'
    def close(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(2)
        assert not self.thread.is_alive()


model = ModelService()
bin_dir = scratch / 'bin'
bin_dir.mkdir(mode=0o700)
opener = bin_dir / 'open'
opener.write_text('#!' + sys.executable + '\n' +
    'from pathlib import Path\nimport sys\n' +
    'Path(' + repr(str(scratch / 'unexpected-opener-call')) + ').write_text("Unexpected OS opener invocation")\n'
    'raise SystemExit(1)\n')
opener.chmod(0o700)
os.environ['BROWSER'] = str(opener)
gh = bin_dir / 'gh'
gh.write_text('#!' + sys.executable + '\n' + '''
import json, os, sys, time
from pathlib import Path
a = sys.argv[1:]
p = Path(os.environ['SYNTHETIC_ACCEPTANCE_GH'])
v = json.loads(p.read_text())
with p.with_suffix('.calls').open('a') as f:
    f.write(json.dumps(a) + '\\n')
if a[:2] == ['auth', 'switch']:
    pass
elif a[:2] == ['auth', 'token']:
    print('synthetic-configured-account-token')
elif a[:2] == ['api', 'user']:
    print('fixture-user')
elif a[:2] == ['issue', 'view']:
    print(json.dumps({'url': a[2], 'title': 'Verify existing fixture', 'body': v['body'],
                     'updatedAt': '2026-10-10T00:00:00Z', 'state': 'OPEN'}))
elif a[:2] == ['pr', 'view']:
    print(json.dumps({'url': a[2], 'state': 'OPEN', 'reviewDecision': 'REVIEW_REQUIRED',
        'headRefOid': v['head'], 'headRefName': 'fixture-delivery', 'baseRefName': 'main', 'mergeCommit': None}))
elif '/sub_issues' in a[1]:
    print(json.dumps([[{'html_url': 'https://github.com/fixture-user/fixture/issues/8', 'state': 'closed'}]]))
elif '/git/ref/heads/' in a[1]:
    if v['scenario'] == 'completion_unknown':
        p.with_suffix('.branch-ready').write_text('ready')
        deadline=time.monotonic()+20
        while not p.with_suffix('.branch-release').exists() and time.monotonic()<deadline:time.sleep(.025)
        if not p.with_suffix('.branch-release').exists():sys.exit(3)
    print(v['head'])
else:
    sys.exit(2)
''')
gh.chmod(0o700)
gh_state = scratch / 'github.json'
gh_state.write_text(json.dumps({'body': body, 'head': fixed, 'scenario': scenario}))
gh_config = scratch / 'gh-config'
gh_config.mkdir(mode=0o700)
os.environ.update(PATH=str(bin_dir) + os.pathsep + os.environ['PATH'], SYNTHETIC_ACCEPTANCE_GH=str(gh_state))
from simple_worker_model import execution_reference  # noqa: E402
reference = execution_reference(scratch, model.base_url, Path(os.environ['DSH_TEST_SDK_ROOT']))
(home / '.env').write_text('HERMES_PM_FEISHU_ALLOWED_USERS=u_owner,on_owner\nOPENAI_API_KEY=synthetic-model-only-key\n')
(home / '.env').chmod(0o600)
binding = {'sender_tenant_key': 'tenant-owner', 'recipient_tenant_key': 'tenant-bot',
           'transport_tenant_key': 'tenant-app', 'verification_ref': 'fixture:identity-map',
           'app_id': 'cli_fixture', 'recipient_open_id': 'ou_lead', 'owner_open_id': 'ou_owner',
           'owner_native_ids': ['u_owner', 'on_owner'], 'chat_id': 'oc_fixture',
           'project_id': 'project-fixture', 'profile_id': 'lead', 'native_profile': 'default',
           'repository': 'fixture-user/fixture', 'repo_path': str(repo), 'execution_ref': reference,
           'superior_profile_id': 'steward', 'capability': 'development'}
settings = {'state_dir': str(state), 'owner_identity_ref': 'fixture:owner',
    'simple_development': {'enabled': True, 'github_account': 'fixture-user', 'github_config_dir': str(gh_config)},
    'feishu_intake': {'enabled': True, 'verification_ref': 'fixture:controlled-smoke', 'bindings': [binding]}}
(home / 'config.yaml').write_text(yaml.safe_dump({'kanban': {'auto_decompose': False},
    'toolsets': ['kanban', 'hermes_pm'], 'platform_toolsets': {'cli': ['hermes_pm_supervision']},
    'known_plugin_toolsets': {'cli': ['hermes_pm', 'hermes_pm_supervision']}, 'tools': {'tool_search': {'enabled': 'off'}},
    'model': {'default': 'fixture-model', 'provider': 'custom', 'base_url': model.base_url,
              'api_mode': 'chat_completions', 'key_env': 'OPENAI_API_KEY', 'max_tokens': 2048, 'context_length': 131072},
    'agent': {'max_turns': 8}, 'plugins': {'enabled': ['ghost-hermes-pm'],
        'entries': {'ghost-hermes-pm': {'settings': settings}}}}))
import hermes_bootstrap  # noqa: E402,F401
from hermes_cli.plugins import get_plugin_manager  # noqa: E402
from gateway.config import Platform, PlatformConfig, GatewayConfig  # noqa: E402
from gateway.run import GatewayRunner  # noqa: E402
from tools.registry import registry  # noqa: E402
from feishu_service_support import service_client, connect_service, receive  # noqa: E402
from lark_oapi.api.im.v1 import P2ImMessageReceiveV1, ReplyMessageResponse  # noqa: E402
from simple_worker_evidence import remember_worker, cleanup_worker, carrier_exit_evidence  # noqa: E402
from hermes_cli.kanban_db_connect import connect_closing  # noqa: E402
from hermes_cli.kanban_db_dispatch import dispatch_once  # noqa: E402
from hermes_cli import kanban_db as kb  # noqa: E402
from hermes_logging import setup_logging, flush_log_queue  # noqa: E402

plugins = get_plugin_manager()
plugins.discover_and_load()
setup_logging(hermes_home=home, mode='gateway')
native_runner = None
worker = None
workers = []
paused_host = None


def inbound(mid, text, reply_to=None):
    return P2ImMessageReceiveV1({'schema': '2.0', 'header': {'event_type': 'im.message.receive_v1',
        'app_id': 'cli_fixture', 'tenant_key': 'tenant-app'}, 'event': {'sender': {'sender_type': 'user',
        'tenant_key': 'tenant-owner', 'sender_id': {'open_id': 'ou_owner', 'user_id': 'u_owner', 'union_id': 'on_owner'}},
        'message': {'message_id': mid, 'chat_id': 'oc_fixture', 'chat_type': 'group', 'message_type': 'text',
            'parent_id': reply_to, 'content': json.dumps({'text': text}),
            'mentions': [{'key': '@_user_1', 'mentioned_type': 'bot', 'tenant_key': 'tenant-bot', 'id': {'open_id': 'ou_lead'}}]}}})


async def main():
    global native_runner, worker, paused_host
    native_runner = GatewayRunner(GatewayConfig(multiplex_profiles=False))
    native_runner.config.profile_routes = []
    platform = Platform('hermes_feishu_pm')
    adapter = native_runner._create_adapter(platform, PlatformConfig(enabled=True, extra={
        'app_id': 'cli_fixture', 'app_secret': 'synthetic-unused-secret', 'require_mention': True,
        'default_group_policy': 'open', 'allow_bots': 'none'}))
    native_runner.adapters[platform] = adapter
    native_runner._wire_adapter_handlers(adapter)
    native = service_client('cli_fixture')
    native.request = lambda _: types.SimpleNamespace(code=0, raw=types.SimpleNamespace(
        content=b'{"code":0,"bot":{"open_id":"ou_lead","activate_status":2}}'))
    sent = []
    def reply(request):
        sent.append(request)
        return ReplyMessageResponse({'code': 0, 'data': {'message_id': 'om_result_' + str(len(sent)),
            'chat_id': 'oc_fixture', 'parent_id': request.message_id}})
    native.im.v1.message.reply = reply
    await connect_service(adapter, native)
    await receive(adapter, inbound('om_fixture', '@_user_1 派发 https://github.com/fixture-user/fixture/issues/7'))
    record = json.loads(registry.dispatch('hermes_pm_snapshot', {}))['work'][0]
    assert record['card_id'] and record['dsh_execution']['state'] == 'admitted'
    with connect_closing() as db:
        dispatched = dispatch_once(db, max_spawn=1)
        assert dispatched.spawned
        worker = remember_worker(db, record['card_id'])
        workers.append(worker)
    if scenario == 'completion_unknown':
        deadline=time.monotonic()+35
        while not gh_state.with_suffix('.branch-ready').exists() and time.monotonic()<deadline:
            await asyncio.sleep(.025)
        assert gh_state.with_suffix('.branch-ready').exists()
        current=json.loads(registry.dispatch('hermes_pm_snapshot', {}))['work'][0]
        directory=state/'owned-work'/record['id']
        birth=json.loads((directory/'process-birth.json').read_text())
        process=psutil.Process(current['dsh_execution']['native_identity']['pid'])
        assert process.pid==birth['pid'] and process.create_time()==birth['created_at']
        paused_host={'pid':process.pid,'birth':process.create_time()}
        os.kill(process.pid,signal.SIGSTOP)
        gh_state.with_suffix('.branch-release').write_text('release')
    if scenario in {'completion_scope', 'completion_reload'}:
        directory = state / 'owned-work' / record['id']
        child_path = directory / 'home/tmp/delivery-child.json'
        deadline = time.monotonic() + 20
        while not child_path.exists() and time.monotonic() < deadline:
            await asyncio.sleep(.025)
        assert child_path.exists(), 'The real same-group Node child must start.'
        child_pid = json.loads(child_path.read_text())['pid']
        child = psutil.Process(child_pid)
        child_birth = child.create_time()
        assert child.is_running() and child.name() == 'node'
        deadline = time.monotonic() + 35
        while not model.final_ready.is_set() and time.monotonic() < deadline:
            await asyncio.sleep(.025)
        assert model.final_ready.is_set(), 'Original native card completion must reach its final model response.'
        current = json.loads(registry.dispatch('hermes_pm_snapshot', {}))['work'][0]
        with connect_closing() as db:
            card = kb.get_task(db, record['card_id'])
        alive = psutil.pid_exists(child_pid) and psutil.Process(child_pid).create_time() == child_birth
        facts = {'scenario': scenario, 'work': current, 'card_status': card.status,
            'child_pid': child_pid, 'child_birth': child_birth, 'child_alive_at_card_completion': alive,
            'worker_alive_at_card_completion': psutil.Process(worker['pid']).is_running(),
            'child_activity_bytes': (directory / 'home/tmp/delivery-child.log').stat().st_size,
            'native_exit_exists': (directory / 'native-exit.json').exists(),
            'native_cleanup_exists': (directory / 'home/.hermes-native-cleanup.json').exists()}
        (scratch / 'acceptance-evidence.json').write_text(json.dumps(facts)); (scratch / 'acceptance-evidence.json').chmod(0o600)
        assert current['dsh_execution']['acceptance']['status'] == 'accepted' and card.status == 'done', facts
        assert current['dsh_execution']['state'] != 'released', facts
        assert not alive and facts['native_exit_exists'] and facts['native_cleanup_exists'], facts
        cleanup = json.loads((directory / 'home/.hermes-native-cleanup.json').read_text())
        assert cleanup['status'] == 'completed' and cleanup['generation'] == current['dsh_execution']['generation']
        if scenario == 'completion_reload':
            assert plugins.unload('ghost-hermes-pm')
            plugins.discover_and_load(force=True)
            replacement=native_runner._create_adapter(platform,adapter.config)
            native_runner.adapters[platform]=replacement
            native_runner._wire_adapter_handlers(replacement)
            await connect_service(replacement,native)
            adapter=replacement
            reloaded=json.loads(registry.dispatch('hermes_pm_snapshot', {}))['work'][0]
            assert reloaded['card_id']==record['card_id'] and reloaded['dsh_execution']['state']!='released'
            assert reloaded['dsh_execution']['generation']==current['dsh_execution']['generation']
        model.final_release.set()
    deadline = time.monotonic() + 75
    while time.monotonic() < deadline:
        current = json.loads(registry.dispatch('hermes_pm_snapshot', {}))['work'][0]
        with connect_closing() as db:
            card = kb.get_task(db, record['card_id'])
        if card.status == 'blocked' or card.status == 'done' and current['dsh_execution']['state'] == 'released':
            break
        await asyncio.sleep(.05)
    if rearranged:
        assert card.status == 'blocked' and current['dsh_execution']['acceptance']['status'] == 'accepted', current
        first_generation = current['dsh_execution']['generation']
        first_session = current['dsh_execution']['session_id']
        directory = state / 'owned-work' / record['id']
        old_bytes = {name: (directory / name).read_bytes() for name in ['native-configuration.json', 'home/.hermes-first-input.json', 'home/.hermes-budget.json']}
        await asyncio.to_thread(psutil.Process(worker['pid']).wait, timeout=20)
        workers[0]['exit_log_bytes'] = (home / 'kanban/logs' / (record['card_id'] + '.log')).stat().st_size
        assert '[kanban-worker-exit] rc=0' in (home / 'kanban/logs' / (record['card_id'] + '.log')).read_text()
        if scenario == 'completion_rearranged_reaped':
            from hermes_cli.kanban_db_dispatch import reap_terminal_workers
            deadline = time.monotonic() + 140
            while time.monotonic() < deadline:
                with connect_closing() as db:
                    reap_terminal_workers(db)
                    original_run = kb.get_run(db, workers[0]['run_id'])
                if original_run.worker_pid is None:
                    break
                await asyncio.sleep(.5)
            assert original_run.worker_pid is None and original_run.ended_at is not None
            original_reaper = {'run_id': original_run.id, 'worker_pid': original_run.worker_pid,
                'claim_pid': workers[0]['pid'], 'ended_at': original_run.ended_at}
            (scratch / 'original-reaper-proof.json').write_text(json.dumps(original_reaper))
        model.rearranged = True
        model.steps['worker'] = 0
        assert json.loads(registry.dispatch('kanban_unblock', {'task_id': record['card_id']}))['ok'] is True
        with connect_closing() as db:
            dispatched = dispatch_once(db, max_spawn=1)
            assert len(dispatched.spawned) == 1 and dispatched.spawned[0][0] == record['card_id']
            worker = remember_worker(db, record['card_id'])
            workers.append(worker)
        assert worker['run_id'] != workers[0]['run_id']
        deadline = time.monotonic() + 25
        while time.monotonic() < deadline:
            current = json.loads(registry.dispatch('hermes_pm_snapshot', {}))['work'][0]
            with connect_closing() as db:
                card = kb.get_task(db, record['card_id'])
            if card.status == 'done' and current['dsh_execution']['state'] == 'released':
                break
            await asyncio.sleep(.025)
        (scratch / 'acceptance-evidence.json').write_text(json.dumps({'scenario': scenario, 'work': current, 'workers': workers, 'card_status': card.status}))
        assert current['dsh_execution']['supervisor_claim']['run_id'] == worker['run_id'], current
        assert current['dsh_execution']['generation'] == first_generation and current['dsh_execution']['session_id'] == first_session
        assert len(list((state / 'owned-work').rglob('native-configuration.json'))) == 1
        assert all((directory / name).read_bytes() == value for name, value in old_bytes.items())
    acceptance = current['dsh_execution'].get('acceptance', {})
    assert acceptance, current
    overlay = json.loads((state / 'owned-work' / record['id'] / 'home/.hermes-owned-overlay.json').read_text())
    for name in ('web-runtime', 'webserver'):
        assert next(row for row in overlay if row.get('id') == name)['disabled'] is True
    assert not (scratch / 'unexpected-opener-call').exists(), 'The supported headless setting must prevent any OS opener call.'
    if scenario in {'completion_repeat', 'completion_proof_changed'}:
        (scratch / 'acceptance-evidence.json').write_text(json.dumps({'scenario': scenario, 'work': current, 'card_status': card.status})); (scratch / 'acceptance-evidence.json').chmod(0o600)
    if scenario == 'completion_proof_changed':
        assert acceptance['status']=='accepted' and acceptance['delivered'] is True, current
        assert card.status=='blocked' and current['dsh_execution']['state']!='released', current
        assert len(list((state/'owned-work').rglob('native-configuration.json')))==1
    elif scenario == 'completion_unknown':
        assert acceptance['status']=='accepted' and acceptance['delivered'] is True, current
        assert current['dsh_execution']['state']=='outcome_unknown' and card.status=='blocked', current
        assert not (state/'owned-work'/record['id']/'native-exit.json').exists()
    elif scenario in {'pending_review', 'completion_scope', 'completion_reload', 'completion_repeat', 'completion_rearranged', 'completion_rearranged_reaped'}:
        assert acceptance['status'] == 'accepted' and acceptance['delivered'] is True, acceptance
        assert card.status == 'done', card
        assert current['dsh_execution']['state'] == 'released'
        assert acceptance['pr_status'] == 'awaiting_review' and acceptance['review_status'] == 'pending'
        actual = acceptance['test_evidence']['runner_receipt']
        assert actual['executed_tests'] == 1 and actual['actual_exit_code'] == 0
        assert actual['source_access'] == actual['git_access'] == 'read-only'
        assert actual['before_source_digest'] == actual['after_source_digest'] == acceptance['source_digest']
    else:
        assert acceptance['status'] == 'unmet' and acceptance['delivered'] is False, acceptance
        assert card.status == 'blocked', card
        assert 'test_runner_unconfirmed' in acceptance['unmet'] if scenario == 'fake_test_success' else 'required_merge_missing' in acceptance['unmet']
    await receive(adapter, inbound('om_fixture_status', '@_user_1 核对 ' + record['id']))
    assert sent[-1].message_id == 'om_fixture_status'
    reply_text = sent[-1].request_body.content
    assert record['issue']['url'] in reply_text and fixed in reply_text, reply_text
    assert '待审查' in reply_text, reply_text
    assert ('保留仓库占用' in reply_text if scenario in {'completion_unknown', 'completion_proof_changed'} else '交付' in reply_text if scenario in {'pending_review', 'completion_scope', 'completion_reload', 'completion_repeat', 'completion_rearranged', 'completion_rearranged_reaped'} else '未交付' in reply_text), reply_text
    listing = json.loads(registry.dispatch('kanban_list', {'limit': 200}))
    assert len(listing['tasks']) == 1, 'Fine GitHub Issues never create outer native cards.'
    from hermes_state import SessionDB
    log_path = home / 'kanban/logs' / (record['card_id'] + '.log')
    deadline = time.monotonic() + 20
    while time.monotonic() < deadline and '[kanban-worker-exit] rc=0' not in log_path.read_text():
        await asyncio.sleep(.05)
    transcript_ids = re.findall(r'^Session: *([^\s]+)', log_path.read_text(), re.MULTILINE)
    assert len(transcript_ids) == (2 if rearranged else 1) and '[kanban-worker-exit] rc=0' in log_path.read_text()
    ledger = SessionDB(db_path=home / 'state.db', read_only=True)
    try:
        messages = ledger.get_messages(transcript_ids[-1])
    finally:
        ledger.close()
    terminal_name = 'kanban_complete' if scenario in {'pending_review', 'completion_scope', 'completion_reload', 'completion_repeat', 'completion_rearranged', 'completion_rearranged_reaped'} else 'kanban_block'
    terminal = [json.loads(m.get('content') or '{}') for m in messages
                if m['role'] == 'tool' and m.get('tool_name') == terminal_name]
    assert any(v.get('ok') is True and v.get('task_id') == record['card_id'] for v in terminal), terminal
    evidence = {'scenario': scenario, 'card_status': card.status, 'acceptance': acceptance,
        'execution_state': current['dsh_execution']['state'], 'lifecycle_end': current['dsh_execution'].get('lifecycle_end'),
        'supervisor_claim': current['dsh_execution'].get('supervisor_claim'),
        'original_terminal_reaper': locals().get('original_reaper'),
        'native_instance_count': len(list((state / 'owned-work').rglob('native-configuration.json'))),
        'before_worker_exit': locals().get('facts'),
        'headless': {'webserver_disabled': True, 'web_runtime_disabled': True, 'os_opener_calls': 0},
        'native_terminal_tool': terminal_name, 'original_terminal_results': terminal,
        'original_dsh_events': current['dsh_execution']['observed_events'],
        'original_dsh_tool_receipts': current['dsh_execution']['tool_receipts'], 'native_worker': worker,
        'external_model_requests': model.requests, 'feishu_replies': [m.request_body.content for m in sent]}
    (scratch / 'acceptance-evidence.json').write_text(json.dumps(evidence))
    (scratch / 'acceptance-evidence.json').chmod(0o600)


async def with_cleanup():
    primary = None
    try:
        await main()
    except BaseException as error:
        primary = error
        raise
    finally:
        model.final_release.set()
        errors, exits = [], []
        if paused_host:
            process=psutil.Process(paused_host['pid'])
            assert process.create_time()==paused_host['birth']
            os.kill(process.pid,signal.SIGCONT)

        for known in workers:
            try:
                exits.append(await asyncio.to_thread(cleanup_worker, known, home))
            except Exception as error:
                errors.append(type(error).__name__)
        module = next((m for n, m in sys.modules.items() if n.endswith('.ghost_hermes_pm.repository_supervision')), None)
        if module:
            carrier_type = importlib.import_module(module.__package__ + '.dsh_owned_transport').PersistentOwnedTransport
            for saved in (state / 'owned-work').glob('*/native-configuration.json'):
                try:
                    if not (saved.parent / 'native-exit.json').exists():
                        config = json.loads(saved.read_text())
                        carrier = carrier_type(instance_dir=str(saved.parent), configuration={k: v for k, v in config.items()
                            if k not in {'socket_path', 'identity_path', 'exit_path', 'configuration_sha256', 'source_bindings', 'home_identity'}}, timeout=15)
                        carrier.shutdown_owned()
                        assert carrier.close_outcome['kind'] == 'original_exit'
                    exits.append(carrier_exit_evidence(saved))
                except Exception as error:
                    errors.append(type(error).__name__)
        if native_runner:
            await native_runner.stop()
        plugins.unload('ghost-hermes-pm')
        flush_log_queue()
        (scratch / 'acceptance-cleanup.json').write_text(json.dumps({'errors': errors, 'original_exits': exits,
            'business_failed': primary is not None, 'logging_flushed_after_stop': True}))
        (scratch / 'acceptance-cleanup.json').chmod(0o600)
        assert not errors, errors


try:
    asyncio.run(with_cleanup())
finally:
    model.close()
