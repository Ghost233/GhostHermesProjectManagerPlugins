"""Real Hermes platform/worker; the external Desktop HTTP service is synthetic."""
import asyncio
import hashlib
import json
import os
from pathlib import Path
import sys
import time
import types
import shutil
import subprocess

ROOT = Path(__file__).resolve().parents[1]
scratch = Path(sys.argv[1]).resolve()
shared_scenario = sys.argv[2]
observer_restart = shared_scenario == 'observer_restart'
if observer_restart:
    shared_scenario = 'terminal'
supervise_takeover = shared_scenario.startswith('supervise_takeover')
takeover_case = shared_scenario.removeprefix('supervise_takeover_') if shared_scenario != 'supervise_takeover' else 'valid'
repository_queue = shared_scenario == 'repository_queue'
blocked_question_continue = shared_scenario == 'blocked_question_continue'
blocked_question_takeover = shared_scenario in {'blocked_question_takeover', 'blocked_question_continue'}
missing_report_rework = shared_scenario == 'missing_report_rework'
if missing_report_rework:
    shared_scenario = 'terminal'
preinput_stop = shared_scenario == 'repository_preinput_stop'
unstarted_ready_stop = shared_scenario == 'repository_unstarted_ready_stop'
unstarted_stop = shared_scenario == 'repository_unstarted_stop' or unstarted_ready_stop
preinput_unverified = shared_scenario == 'repository_preinput_unverified'
supervise_slice = shared_scenario == 'supervise_slice' or supervise_takeover or repository_queue or blocked_question_takeover
background_later_worker = shared_scenario == 'background_cleanup_later_worker'
background_failed_worker = shared_scenario == 'background_cleanup_failed_worker'
background_ended_worker = shared_scenario == 'background_cleanup_ended_worker' or background_later_worker or background_failed_worker
background_secret = shared_scenario == 'background_cleanup_secret_stop'
background_unknown = shared_scenario == 'background_cleanup_unknown_stop' or background_secret
background_foreign_notice = shared_scenario == 'background_cleanup_foreign_notice'
background_whole_stop = shared_scenario == 'background_cleanup_whole_stop'
background_cleanup = shared_scenario == 'background_cleanup' or background_ended_worker or background_unknown or background_foreign_notice or background_whole_stop
supervision_checkpoints = shared_scenario == 'supervision_checkpoints' or background_cleanup
expired_window = shared_scenario == 'supervise_expired_window'
if expired_window:
    shared_scenario = 'renew_window'
runtime_metadata = shared_scenario == 'runtime_metadata'
if runtime_metadata:
    shared_scenario = 'terminal'
triage_continuation = shared_scenario == 'continue_triage'
concurrent_claim = shared_scenario == 'continue_claim_race'
native_restore = shared_scenario == 'continue_native_restore'
renew_continue = shared_scenario == 'renew_window_continue'
resume_completed_window = shared_scenario == 'resume_completed_window'
qa_request_only_resume = shared_scenario == 'qa_request_only_resume'
qa_preinput_retry = shared_scenario == 'qa_preinput_retry' or qa_request_only_resume
owner_maintenance = shared_scenario == 'owner_maintenance'
owner_context = shared_scenario == 'owner_context' or owner_maintenance
renew_window_again = shared_scenario == 'renew_window_again' or owner_context
completed_renewed_window = resume_completed_window or renew_window_again or qa_preinput_retry
renew_window = shared_scenario in {'renew_window', 'renew_window_unknown', 'renew_window_continue'}
max_tokens_continuation = shared_scenario in {'continue_max_tokens', 'continue_max_tokens_unknown', 'continue_max_tokens_last_slot', 'continue_tokens_uncapped', 'renew_window', 'renew_window_unknown', 'renew_window_continue'}
max_tokens_unknown = shared_scenario in {'continue_max_tokens_unknown', 'renew_window_unknown'}
max_tokens_last_slot = shared_scenario == 'continue_max_tokens_last_slot'
tokens_uncapped = shared_scenario == 'continue_tokens_uncapped'
if triage_continuation or concurrent_claim or native_restore or max_tokens_continuation:
    shared_scenario = 'continue_restart'
continuation_scenarios = {'continue_restart', 'continue_unknown'}
host_processes = []
contract_run = subprocess.run([shutil.which('node'), str(ROOT / 'tests/shared_permission_native_runner.mjs'),
    str(scratch)], capture_output=True, text=True, check=True, timeout=20)
permission_contract = json.loads(contract_run.stdout)
# Extend only the external service fixture to expose its native launch exchange
# and native preset/question projections. No original SDK method is replaced.
service_source = (ROOT / 'tests/dsh_fixture_server.py').read_text()
service_source = service_source.replace("assert self.path == '/api/remote.mux'", """if self.path == '/?token=fixture-launch':
                self.send_response(303)
                self.send_header('Set-Cookie', 'synthetic-auth=value; HttpOnly; Path=/')
                self.send_header('Location', './')
                self.send_header('Content-Length', '0')
                self.end_headers()
                return
            assert self.path == '/api/remote.mux'""")
service_source = service_source.replace("elif method == 'session/create':", """elif method == 'workspace/create':
                assert set(args['request']) == {'path'}
                created = 'workspace' not in state
                if created:
                    state['workspace'] = {'workspaceId': 'fixture-shared-workspace', 'path': args['request']['path'],
                        'title': 'Fixture', 'sessionIds': [], 'createdAt': 'fixture-created', 'updatedAt': 'fixture-created'}
                value = {'workspace': dict(state['workspace']), 'created': created}
            elif method == 'session/create':""")
service_source = service_source.replace("value = {'sessionId': args['request']['sessionId']}",
    "value = {'sessionId': args['request']['sessionId'], 'agentPreset': args['request']['agentPreset']}")
service_source = service_source.replace("'cwd': args['request']['cwd']", "'cwd': (state['workspace']['path'] if 'workspaceId' in args['request'] else args['request']['cwd'])")
service_source = service_source.replace("state['sessions'].append(",
    "if not any(row['sessionId'] == value['sessionId'] for row in state['sessions']): state['sessions'].append(")
service_source = service_source.replace("state['journals'].setdefault(value['sessionId'], list(behavior.get('records', [])))", """state['journals'].setdefault(value['sessionId'], list(behavior.get('records', [])))
                if 'workspaceId' in args['request']:
                    assert 'cwd' not in args['request'] and args['request']['workspaceId'] == state['workspace']['workspaceId']
                    if value['sessionId'] not in state['workspace']['sessionIds']:
                        state['workspace']['sessionIds'].append(value['sessionId'])
                next(row for row in state['sessions'] if row['sessionId'] == value['sessionId'])['agentAvailable'] = True""")
service_source = service_source.replace("'values': {'inbox': behavior.get('inbox', {'next-turn': [], 'next-step': []})}",
    "'values': {'agentPreset': behavior.get('agent_preset', 'fixture-shared'), 'userQuestions': behavior.get('user_questions', {'active': [], 'settled': []}), "
    "'permissions': {'currentValue': 'workspace-write'}, "
    "'inbox': behavior.get('inbox', {'next-turn': [], 'next-step': []}), "
    "**({'contextHeaders': behavior['context_headers']} if 'context_headers' in behavior else {})}")
service_source = service_source.replace("elif method == 'session/cancel':", """elif method == 'commands/list':
                assert set(args) == {'agentId'}
                value = [{'name': 'permission', 'definitionId': '@deepseek-ai/dsh-permission-presets'}]
            elif method == 'commands/execute':
                assert args['line'] == '/permission workspace-write' and args['submittedAttachments'] == []
                sid = args['agentId']
                state['journals'][sid] = [{'type': 'event', 'event': event} for event in permission_contract['events']]
                behavior['inbox'] = permission_contract['projections']['inbox']
                value = None if shared_scenario == 'reconcile_intent' else permission_contract['response']
            elif method == 'session/cancel':""")
if background_cleanup:
    service_source = service_source.replace("elif method == 'commands/list':", """elif method == 'job/kill':
                job_request = args['request']
                assert job_request == {'sessionId': behavior['jobs'][0]['owner'], 'jobId': 'fixture-preview'}
                if behavior.get('unknown_background_cleanup'):
                    value = {'outcome': 'unknown'}
                else:
                    behavior['jobs'][0]['status'] = 'killed'
                    settled = dict(behavior['registered_preview'], type='settled', status='killed', cause='kill', awaited=False)
                    with behavior['job_receipts'].open('a') as output:
                        output.write(json.dumps(settled) + '\\n')
                    budget_row = json.loads(behavior['budget_path'].read_text())
                    budget_row['state']['observed_at_ms'] = int(time.time() * 1000)
                    behavior['budget_path'].write_text(json.dumps(budget_row))
                    notice = {'id': 'fixture-native-job-notice', 'role': 'user', 'content': [],
                        'source': {'kind': 'tool-jobs', 'form': 'notice', 'summary': 'Synthetic preview job finished.'}}
                    inserted = [notice]
                    if behavior.get('foreign_job_notice'):
                        inserted.append({'id': 'fixture-foreign-input', 'role': 'user', 'content': [],
                            'source': {'kind': 'user', 'rpcId': 'foreign-input'}})
                    behavior['inbox'] = {'next-turn': [], 'next-step': inserted}
                    journal = state['journals'][job_request['sessionId']]
                    journal.append({'type': 'event', 'event': {'type': 'agent/inbox/spliced', 'seq': len(journal),
                        'time': 6, 'data': {'target': 'next-step', 'start': 0, 'inserted': json.loads(json.dumps(inserted))}}})
                    value = {'outcome': 'requested'}
            elif method == 'session/updateQueue':
                removal = args['request']
                assert removal['sessionId'] == behavior['jobs'][0]['owner'] and removal['action'] == {'kind': 'remove'}
                queued = behavior['inbox']['next-step']
                assert removal['itemId'] == 'fixture-native-job-notice'
                index = next(index for index, item in enumerate(queued) if item['id'] == removal['itemId'])
                del queued[index]
                journal = state['journals'][removal['sessionId']]
                journal.append({'type': 'event', 'event': {'type': 'agent/inbox/spliced', 'seq': len(journal),
                    'time': 7, 'data': {'target': 'next-step', 'start': index, 'removedCount': 1, 'inserted': []}}})
                value = {'accepted': True}
            elif method == 'commands/list':""", 1)
service_source = service_source.replace("state['prompts'].append(args['request'])", """state['prompts'].append(args['request'])
                behavior['inbox'] = {'next-turn': [], 'next-step': []}
                if max_tokens_last_slot and len(state['prompts']) == 3:
                    budget_path = ledger / (hashlib.sha256(args['request']['sessionId'].encode()).hexdigest() + '.json')
                    budget_row = json.loads(budget_path.read_bytes())
                    assert budget_row['state']['requests'] == budget_row['budget']['max_model_requests'] - 1
                    budget_row['state']['requests'] += 1
                    budget_row['state']['admissions'].append(args['request']['requestId'])
                    budget_path.write_text(json.dumps(budget_row))
                    behavior['last_slot_budget'] = budget_path.read_bytes()
                if ((renew_window and not max_tokens_unknown and len(state['prompts']) >= 3)
                        or (renew_window_again and len(state['prompts']) >= 2 and not (owner_context and len(state['prompts']) >= 3))):
                    budget_path = ledger / (hashlib.sha256(args['request']['sessionId'].encode()).hexdigest() + '.json')
                    budget_row = json.loads(budget_path.read_bytes())
                    budget_row['renewWindow'].update(started_at_ms=int(time.time() * 1000), hook_marker='same-sid-window-v1')
                    budget_row['state']['requests'] += 1
                    budget_row['state']['reported_tokens'] += 20
                    budget_row['state']['admissions'].append(args['request']['requestId'])
                    budget_path.write_text(json.dumps(budget_row))
                    behavior['renew_budget_after'] = budget_path.read_bytes()""")
service_source = service_source.replace("if behavior.get('admit_prompt'):",
    """if owner_context and len(state['prompts']) >= 3:
                    submission = args['request']
                    if owner_context and len(state['prompts']) >= 4:
                        durable = record()['dsh_execution']['owner_context_control']
                        assert durable['status'] == 'intent' and durable['request_id'] == submission['requestId']
                        assert set(record()['dsh_execution']['renewal_window_checkpoint']['receipts']) == {'.tools.jsonl', '.jobs.jsonl'}
                        behavior['owner_context_intent'] = dict(durable)
                    journal = state['journals'][submission['sessionId']]
                    turn = len(state['prompts'])
                    data = [('agent/inbox/spliced', {'target': 'next-turn', 'start': 0, 'inserted': [
                        {'id': 'message-' + submission['requestId'], 'role': 'user', 'content': submission['content'],
                         'source': {'kind': 'user', 'rpcId': submission['requestId']}}]}),
                        ('turn/start', {'turn': turn})]
                    if len(state['prompts']) == 3:
                        data += [('agent/inbox/spliced', {'target': 'next-turn', 'start': 0, 'removedCount': 1, 'inserted': []}),
                            ('step/start', {'turn': turn, 'step': 1}), ('step/end', {'turn': turn, 'step': 1}),
                            ('turn/end', {'turn': turn, 'reason': {'kind': 'error', 'error': {
                                'message': 'Shared work native settlement usage is unknown.', 'code': 'UNKNOWN'}}})]
                    for kind, payload in data:
                        journal.append({'type': 'event', 'event': {'type': kind, 'seq': len(journal), 'time': 10, 'data': payload}})
                    next(row for row in state['sessions'] if row['sessionId'] == submission['sessionId'])['running'] = False
                elif shared_scenario in {'continue_restart', 'continue_unknown'} and len(state['prompts']) == 1:
                    submission = args['request']
                    journal = state['journals'][submission['sessionId']]
                    item = {'id': 'message-' + submission['requestId'], 'role': 'user',
                        'content': submission['content'], 'source': {'kind': 'user', 'rpcId': submission['requestId']}}
                    journal.append({'type': 'event', 'event': {'type': 'agent/inbox/spliced', 'seq': len(journal),
                        'time': 3, 'data': {'target': 'next-turn', 'start': 0, 'inserted': [item]}}})
                    journal.append({'type': 'event', 'event': {'type': 'turn/start', 'seq': len(journal),
                        'time': 3, 'data': {'turn': 1}}})
                    journal.append({'type': 'event', 'event': {'type': 'agent/inbox/spliced', 'seq': len(journal),
                        'time': 3, 'data': {'target': 'next-turn', 'start': 0, 'removedCount': 1, 'inserted': []}}})
                    notices = permission_contract['projections']['inbox']['next-step']
                    if notices:
                        journal.append({'type': 'event', 'event': {'type': 'agent/inbox/spliced', 'seq': len(journal),
                            'time': 3, 'data': {'target': 'next-step', 'start': 0, 'removedCount': len(notices), 'inserted': []}}})
                    row = next(row for row in state['sessions'] if row['sessionId'] == submission['sessionId'])
                    row.update(blank=False, running=True)
                elif behavior.get('admit_prompt') and shared_scenario != 'continue_restart':""")
service_source = service_source.replace("value = {'accepted': True}",
    "value = None if method == 'session/prompt' and ((shared_scenario == 'continue_unknown' and len(state['prompts']) == 2) or (max_tokens_unknown and len(state['prompts']) == 3)) else {'accepted': True}")
service_source = service_source.replace("'header': {'id': session_id,", "'header': {'id': behavior.get('header_id', session_id),")
service_source = service_source.replace("'createdAt': 0,", "'createdAt': behavior.get('created_at', 0),")
service_source = service_source.replace("'isSeeded': False}", "'isSeeded': False, **behavior.get('header_defaults', {})}")
service_source = service_source.replace("elif method == 'session/cancel':\n                value", """elif method == 'session/cancel':
                sid = args['request']['sessionId']
                row = next(row for row in state['sessions'] if row['sessionId'] == sid)
                row['running'] = False
                journal = state['journals'][sid]
                journal.append({'type': 'event', 'event': {'type': 'turn/end', 'seq': len(journal),
                    'time': 4, 'data': {'turn': 1, 'reason': {'kind': 'aborted', 'reason': {'kind': 'user'}}}}})
                value""")
service_namespace = {'permission_contract': permission_contract, 'shared_scenario': shared_scenario,
    'owner_context': owner_context,
    'max_tokens_unknown': max_tokens_unknown, 'max_tokens_last_slot': max_tokens_last_slot, 'renew_window': renew_window,
    'renew_window_again': renew_window_again, 'time': time}
exec(compile(service_source, str(ROOT / 'tests/dsh_fixture_server.py'), 'exec'), service_namespace)
fixture_behavior = {'admit_prompt': True}
if native_restore:
    fixture_behavior['context_headers'] = {'headers': [{'tools': [{'name': 'bash', 'plugin': 'tool-bash', 'tokens': 10}]}]}
peer_context = service_namespace['remote_peer'](behavior=fixture_behavior)
url, peer = peer_context.__enter__()
shared_address = ('127.0.0.1', int(url.rsplit(':', 1)[1]))
receipt = scratch / 'desktop-host.json'
receipt.write_text(json.dumps({'schema': 1, 'lease': 'fixture-shared-lease', 'pid': host_processes[0].pid if host_processes else os.getpid(),
    'url': url + '/?token=fixture-launch', 'runtimeVersion': 'fixture', 'permissionMode': 'danger-full-access'}))
receipt.chmod(0o600)
ledger = scratch / 'ledger'
ledger.mkdir(mode=0o700)
service_namespace['ledger'] = ledger
sdk = Path(os.environ['DSH_TEST_SDK_ROOT'])
anchor = '@deepseek-ai/dsh/lib/profile-boot.js'
proof = scratch / 'external-boundary-fixture.json'
proof.write_text(json.dumps({'schema': 1, 'status': 'passed', 'tool_catalog':
    ['ask_user_question', 'bash', 'job_kill', 'job_list', 'job_output', 'skill'],
    'source_hook_sha256': hashlib.sha256((ROOT / 'ghost_hermes_pm/shared_native_hook.mjs').read_bytes()).hexdigest(),
    'composition_sha256': 'synthetic-composition',
    'sdk_sources': {anchor: hashlib.sha256((sdk / anchor).read_bytes()).hexdigest()},
    'managed_jobs': {'foreground': True, 'background': True, 'scope': 'darwin_process_group'},
    'passed_cases': ['allowed_write', 'outside_write_denied', 'symlink_write_denied', 'hardlink_write_denied',
        'danger_denied', 'foreground_job_registered', 'managed_background_stopped', 'foreground_cancelled',
        'foreign_job_kill_denied', 'background_write_boundary'], 'scope': 'offline-external-service-fixture'}))
proof.chmod(0o600)
reference = scratch / 'shared-reference.json'
reference.write_text(json.dumps({'schema': 1, 'approved': True, 'mode': 'shared_native',
    'desktop_receipt': str(receipt), 'host_lease': 'fixture-shared-lease', 'service_ref': 'fixture-native',
    'runtime_package_root': str(sdk), 'node_bin': str(Path(shutil.which('node')).resolve()),
    'agent_preset': 'fixture-shared', 'ledger_dir': str(ledger), 'provider': 'fixture', 'model': 'fixture',
    'budget': {'max_wall_seconds': 1800 if qa_request_only_resume else 60,
        'max_model_requests': 9 if max_tokens_last_slot else 80}, 'tool_receipt_ref': str(proof),
    'composition_sha256': 'synthetic-composition', 'source_snapshot': {
        'python': str(Path(sys.executable).resolve()),
        'reader': str(ROOT / 'ghost_hermes_pm/shared_source_reader.py'),
        'python_sha256': hashlib.sha256(Path(sys.executable).resolve().read_bytes()).hexdigest(),
        'reader_sha256': hashlib.sha256((ROOT / 'ghost_hermes_pm/shared_source_reader.py').read_bytes()).hexdigest()},
    'verification': {'python': str(Path(sys.executable).resolve()),
        'runner': str(ROOT / 'ghost_hermes_pm/repository_test_runner.py'), 'node': str(Path(shutil.which('node')).resolve()),
        'python_sha256': hashlib.sha256(Path(sys.executable).resolve().read_bytes()).hexdigest(),
        'runner_sha256': hashlib.sha256((ROOT / 'ghost_hermes_pm/repository_test_runner.py').read_bytes()).hexdigest(),
        'node_sha256': hashlib.sha256(Path(shutil.which('node')).resolve().read_bytes()).hexdigest()}}))
reference.chmod(0o600)
if qa_request_only_resume:
    policy_scope = json.loads(reference.read_text())
    policy_scope['supervision'] = {'interval_seconds': 3600, 'request_interval': 10}
    reference.write_text(json.dumps(policy_scope))
if supervision_checkpoints:
    checkpoint_scope = json.loads(reference.read_text())
    checkpoint_scope.update(budget={'max_model_requests': 53} if background_cleanup else {},
        supervision={'interval_seconds': 1, 'request_interval': 10})
    reference.write_text(json.dumps(checkpoint_scope))
if runtime_metadata:
    metadata = json.loads(reference.read_text())
    metadata['source_snapshot']['python_sha256'] = '0' * 64
    metadata['verification'].update(python_sha256='0' * 64, node_sha256='0' * 64)
    reference.write_text(json.dumps(metadata))
os.environ['HPM_NATIVE_EXECUTION_REFERENCE'] = str(reference)
fixture = ROOT / 'tests/simple_intake_smoke_runner.py'
prefix = fixture.read_text().split('async def main():', 1)[0]
prefix = prefix.replace("args[1][:2] != model_address", "args[1][:2] not in (model_address, shared_address)")
sys.argv = [str(fixture), str(scratch), 'worker_dispatch']
namespace = {'__name__': '__shared_fixture__', '__file__': str(fixture), 'shared_address': shared_address}
exec(compile(prefix, str(fixture), 'exec'), namespace)
GatewayRunner, GatewayConfig, Platform, PlatformConfig = (namespace[name] for name in
    ('GatewayRunner', 'GatewayConfig', 'Platform', 'PlatformConfig'))
registry, plugins, model, repo = (namespace[name] for name in ('registry', 'plugins', 'model', 'repo'))
if supervise_slice:
    model.repeat_supervise = True
if supervision_checkpoints:
    model.review_supervision_checkpoints = True
    model.notify_first_checkpoint = background_cleanup
service_client, connect_service, receive, raw, ReplyMessageResponse = (namespace[name] for name in
    ('service_client', 'connect_service', 'receive', 'raw', 'ReplyMessageResponse'))


async def main():
    from hermes_cli import kanban_db as kb
    from hermes_cli.kanban_db_connect import connect_closing
    from hermes_cli.kanban_db_dispatch import dispatch_once
    from simple_worker_evidence import remember_worker
    runner = GatewayRunner(GatewayConfig(multiplex_profiles=False))
    runner.config.profile_routes = []
    platform = Platform('hermes_feishu_pm')
    adapter = runner._create_adapter(platform, PlatformConfig(enabled=True, extra={
        'app_id': 'cli_fixture', 'app_secret': 'synthetic-unused-secret', 'require_mention': True,
        'default_group_policy': 'open', 'allow_bots': 'none'}))
    runner.adapters[platform] = adapter
    runner._wire_adapter_handlers(adapter)
    service = service_client('cli_fixture')
    service.request = lambda _: types.SimpleNamespace(code=0, raw=types.SimpleNamespace(
        content=b'{"code":0,"bot":{"open_id":"ou_lead","activate_status":2}}'))
    sent = []
    def reply(request):
        sent.append(request)
        return ReplyMessageResponse({'code': 0, 'data': {'message_id': 'om_sent_' + str(len(sent)),
            'chat_id': 'oc_fixture', 'parent_id': request.message_id}})
    service.im.v1.message.reply = reply
    worker = None
    preflight_runs = []
    try:
        material_cases = []
        material_observations = []
        material_pending = None
        material_legacy_delivery = None
        if shared_scenario == 'question_material':
            docs = repo / 'docs'
            docs.mkdir()
            plan = 'A bounded synthetic plan with two implementation steps.\n'
            (docs / 'spec.md').write_text(plan)
            (repo / 'README.md').write_text(plan)
            outside = scratch / 'outside-plan.md'
            outside.write_text('Outside fixture material must stay private.\n')
            (docs / 'linked.md').symlink_to(outside)
            (docs / 'hidden.md').write_text('password: fixture-sensitive-value\n')
            (docs / 'large.md').write_text('L' * 16001)
            (docs / 'long-message.md').write_text('M' * 7500)
            material_cases = [('relative', '`docs/spec.md`', True),
                ('token_metadata', '现有token权限检查与token预算；`docs/spec.md`', True),
                ('token_metadata_legacy', '现有token权限检查；`docs/spec.md`', True),
                ('token_value', '请提供token值；`docs/spec.md`', False),
                ('token_access', '请批准扩展token权限；`docs/spec.md`', False),
                ('link', '[方案](docs/spec.md)', True), ('bare', '`README.md`', True),
                ('absolute', '`' + str(docs / 'spec.md') + '`', True),
                ('inside_parent', '`docs/../README.md`', True),
                ('missing', '`docs/missing.md`', False), ('parent_escape', '`../outside-plan.md`', False),
                ('absolute_escape', '`' + str(outside) + '`', False), ('symlink_escape', '`docs/linked.md`', False),
                ('sensitive', '`docs/hidden.md`', False), ('file_limit', '`docs/large.md`', False),
                ('message_limit', '`docs/long-message.md`', False)]
        if shared_scenario in continuation_scenarios or completed_renewed_window:
            host_processes.append(subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(120)'],
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL))
            original_receipt = json.loads(receipt.read_text())
            original_receipt['pid'] = host_processes[0].pid
            receipt.write_text(json.dumps(original_receipt))
        await connect_service(adapter, service)
        if qa_request_only_resume:
            (scratch / 'issue-body-override').write_text('Repair synthetic browser QA.\n'
                'Historical budget: stop after 30 minutes, 80 model requests and 300000 reported tokens.\n')
        await receive(adapter, raw('om_shared'))
        primary_id = adapter.intake.snapshot()['work'][0]['id']
        def record():
            return next(row for row in adapter.intake.snapshot()['work'] if row['id'] == primary_id)
        service_namespace['record'] = record
        def checkpoint_progress(requests):
            sid = record()['dsh_execution']['session_id']
            path = ledger / (hashlib.sha256(sid.encode()).hexdigest() + '.json')
            row = json.loads(path.read_text())
            rows = peer['journals'][sid]
            for index in range(row['state']['requests'], requests):
                rows.append({'type': 'event', 'event': {'type': 'assistant/message', 'seq': len(rows), 'time': 4,
                    'data': {'turn': 1, 'step': index, 'usage': {'inputTokens': 12, 'outputTokens': 8},
                        'message': {'id': 'fixture-checkpoint-settlement-' + str(index), 'role': 'assistant',
                            'content': [{'type': 'text', 'text': 'Synthetic active DSH progress.'}]}}}})
            row['state'].update(started_at_ms=row['state']['started_at_ms'] or int(time.time() * 1000) - 1100,
                requests=requests, reported_tokens=requests * 20,
                admissions=['fixture-checkpoint-request-' + str(index) for index in range(requests)])
            if background_cleanup:
                row['state'].setdefault('observed_at_ms', row['state']['started_at_ms'])
            binding = {key: row[key] for key in ('schema', 'service_ref', 'host_lease', 'agent_preset',
                'session_id', 'session_created_at', 'generation', 'workspace', 'provider', 'model', 'budget')}
            row['state']['binding_sha256'] = hashlib.sha256(json.dumps(binding,
                separators=(',', ':'), ensure_ascii=False).encode()).hexdigest()
            temporary = path.with_name(path.name + '.external-progress')
            temporary.write_text(json.dumps(row))
            temporary.chmod(0o600)
            os.replace(temporary, path)
            return path
        initial = record()
        queued = None
        if repository_queue or background_whole_stop or preinput_stop or unstarted_stop:
            await receive(adapter, raw('om_successor', '@_user_1 派发 https://github.com/fixture-user/fixture/issues/8'))
            queued = adapter.intake.snapshot()['work'][1]
            assert queued['card_id'] != initial['card_id'] and not queued['dsh_execution'].get('session_id')
            # Reproduce a persisted pre-input failure from the former ungated
            # successor worker. Native queued-card facts must prove it cannot run.
            if repository_queue:
                with connect_closing() as db:
                    assert kb.unlink_tasks(db, initial['card_id'], queued['card_id'])
                queued.pop('repository_parents')
                adapter.intake._save(queued)
                initial['dsh_execution']['state'] = 'outcome_unknown'
                adapter.intake._save_execution(initial, fields=['state'])
                assert 'error' not in json.loads(registry.dispatch('kanban_block', {
                    'task_id': initial['card_id'], 'kind': 'needs_input', 'reason': 'Original work requires supervision.'}))
                model.repeat_supervise = False
                with connect_closing() as db:
                    assert dispatch_once(db, max_spawn=1).spawned
                    worker = remember_worker(db, queued['card_id'])
                until = time.monotonic() + 15
                while time.monotonic() < until:
                    waited, status = os.waitpid(worker['pid'], os.WNOHANG)
                    if waited:
                        assert os.waitstatus_to_exitcode(status) == 0
                        break
                    await asyncio.sleep(.05)
                else:
                    raise AssertionError('Original historical successor worker did not normally end.')
                worker = None
                queued = adapter.intake.snapshot()['work'][1]
                assert queued['dsh_execution']['state'] == 'outcome_unknown' and not queued['dsh_execution'].get('generation')
                assert not peer['prompts']
                assert 'error' not in json.loads(registry.dispatch('kanban_link', {
                    'parent_id': initial['card_id'], 'child_id': queued['card_id']}))
                assert 'error' not in json.loads(registry.dispatch('kanban_unblock', {'task_id': queued['card_id']}))
                assert 'error' not in json.loads(registry.dispatch('kanban_unblock', {'task_id': initial['card_id']}))
                # This restored unknown card legitimately permits a handoff;
                # start the model at supervision, not the unrelated premature block probe.
                model.worker_steps = 1
                model.repeat_supervise = True
                model.worker_terminal_attempt_at = 5
                model.worker_finish_at = 6
            if preinput_stop:
                # Simulate the archive/private-save crash window through the
                # original native lifecycle, without declaring private release.
                initial['dsh_execution']['state'] = 'outcome_unknown'
                adapter.intake._save_execution(initial, fields=['state'])
                with connect_closing() as db:
                    assert kb.archive_task(db, initial['card_id'])
                primary_id = queued['id']
                initial = queued
        if unstarted_stop:
            target = initial if unstarted_ready_stop else queued
            with connect_closing() as db:
                assert kb.get_task(db, target['card_id']).status == ('ready' if unstarted_ready_stop else 'todo')
                assert kb.list_runs(db, target['card_id']) == []
            await receive(adapter, raw('om_stop_unstarted', '@_user_1 停止 ' + target['id']))
            stopped = adapter.intake.snapshot()['work'][0 if unstarted_ready_stop else 1]['dsh_execution']
            assert stopped['state'] == 'stopped', stopped.get('last_failure')
            proof = stopped['stop_control']['proof']['worker']
            assert proof['exit_code'] is None and proof['not_started'] is True
            assert (proof['run_id'] is not None) is unstarted_ready_stop
            assert not peer['prompts'] and not stopped.get('session_id')
            with connect_closing() as db:
                assert kb.get_task(db, target['card_id']).status == 'archived'
                assert len(kb.list_runs(db, target['card_id'])) == (1 if unstarted_ready_stop else 0)
                assert kb.get_task(db, queued['card_id'] if unstarted_ready_stop else initial['card_id']).status == 'ready'
            if not unstarted_ready_stop:
                assert record()['dsh_execution'] == initial['dsh_execution']
            (scratch / 'shared-worker-result.json').write_text(json.dumps({
                'status': 'passed', 'unstarted_queued_stop_verified': True, 'dsh_input_count': 0}))
            return
        if preinput_unverified:
            with connect_closing() as db:
                claimed = kb.claim_task(db, initial['card_id'])
                assert claimed
                assert kb.block_task(db, initial['card_id'], kind='needs_input', reason='Native spawn identity unavailable.')
            await receive(adapter, raw('om_stop_unverified_preinput', '@_user_1 停止 ' + initial['id']))
            assert record()['dsh_execution']['state'] != 'stopped'
            assert record()['dsh_execution']['stop_control']['status'] == 'unverified'
            assert record()['dsh_execution']['last_failure']['code'] == 'outcome_unknown'
            assert not peer['prompts'] and not record()['dsh_execution'].get('session_id')
            with connect_closing() as db:
                assert kb.get_task(db, initial['card_id']).status == 'blocked'
            (scratch / 'shared-worker-result.json').write_text(json.dumps({
                'status': 'passed', 'missing_native_birth_preserved_unknown': True}))
            return
        compact = json.loads(registry.dispatch('hermes_pm_snapshot', {'work_id': initial['id'], 'section': 'overview'}))
        assert compact['work_id'] == initial['id'] and len(json.dumps(compact)) < 1500
        assert 'dsh_execution' not in compact and 'history' not in compact
        assert initial['dsh_execution']['mode'] == 'shared_native', initial
        if observer_restart:
            initial['gateway_controller'] = dict(initial['gateway_controller'], pid=999999, start_sec=1, start_usec=0)
            adapter.intake._save(initial)
        if triage_continuation:
            assert 'error' not in json.loads(registry.dispatch('kanban_block', {
                'task_id': initial['card_id'], 'kind': 'needs_input', 'reason': 'Original failure still needs explicit continuation.'}))
            assert 'error' not in json.loads(registry.dispatch('kanban_unblock', {'task_id': initial['card_id']}))
        takeover = None
        preflight_runs = []
        blocked_question_stage = None
        if supervise_takeover:
            # One real 8/8 exit after a rejected terminal attempt consumes the native
            # failure allowance. The second worker's real 8/8 exhaustion closes
            # its run as gave_up, without editing database state or retry limits.
            model.worker_finish_at = 8
            model.worker_terminal_attempt_at = 7
            model.worker_preflight = True
            for _ in range(1):
                model.worker_steps = 0
                with connect_closing() as db:
                    assert dispatch_once(db, max_spawn=1).spawned
                    worker = remember_worker(db, initial['card_id'])
                until = time.monotonic() + 12
                while time.monotonic() < until:
                    waited, status = os.waitpid(worker['pid'], os.WNOHANG)
                    if waited:
                        preflight_worker = worker
                        worker = None
                        exit_code = os.waitstatus_to_exitcode(status)
                        with connect_closing() as db:
                            run = kb.get_run(db, preflight_worker['run_id'])
                        preflight_runs.append({'run_id': run.id, 'status': run.status,
                            'outcome': run.outcome, 'error': run.error, 'exit_code': exit_code})
                        assert exit_code == 0
                        assert run.status == run.outcome == 'timed_out'
                        assert run.error == 'Iteration budget exhausted (8/8) — task could not complete within the allowed iterations'
                        assert model.worker_terminal_results
                        terminal_attempt = model.worker_terminal_results[-1]
                        assert terminal_attempt['tool_name'] == 'kanban_block'
                        assert 'Original worker claim and supervision outcome must be verified before kanban_block; repository occupancy is retained.' in terminal_attempt['content']
                        preflight_runs[-1]['terminal_attempt'] = terminal_attempt
                        break
                    await asyncio.sleep(.05)
                assert worker is None, 'The native protocol preflight worker did not exit.'
                assert not peer['prompts'] and 'supervisor_claim' not in record()['dsh_execution']
            model.worker_steps = 0
            model.worker_preflight = False
            if takeover_case == 'nonzero':
                model.worker_terminal_attempt_at = None
                model.worker_first_heartbeat = True
        with connect_closing() as db:
            assert dispatch_once(db, max_spawn=1).spawned
            worker = remember_worker(db, initial['card_id'])
        observer_ready = None
        supervise_first = None
        supervise_terminal = None
        supervise_ledger = None
        supervise_ledger_before = None
        supervise_history_before = None
        if supervise_slice:
            observer_deadline = time.monotonic() + 45
            while time.monotonic() < observer_deadline:
                current = record()['dsh_execution']
                if (repository_queue and current['state'] == 'outcome_unknown'
                        and current.get('last_failure', {}).get('code') == 'repository_busy'):
                    raise AssertionError('Queued successor blocked its predecessor: ' + json.dumps(current['last_failure']))
                if repository_queue:
                    active = json.loads(registry.dispatch('kanban_show', {'task_id': initial['card_id']}))['task']
                    assert active['status'] == 'running', 'The original fixture worker ended before supervision: ' + json.dumps(active)
                observer = current.get('shared_event_observer', {})
                if (observer.get('status') == 'ready' and observer.get('session_id') == current.get('session_id')
                        and observer.get('generation') == current.get('generation')
                        and observer.get('host_lease') == 'fixture-shared-lease' and observer.get('client_id')):
                    observer_ready = dict(observer)
                    break
                await asyncio.sleep(.05)
            else:
                raise AssertionError('Public hermes_pm_snapshot observer did not become ready before first public tool: ' + json.dumps(current))
        deadline = time.monotonic() + (30 if supervise_takeover else 10 if supervise_slice else 60 if runtime_metadata or native_restore or renew_window or renew_window_again else 35)
        ended = False
        stop_sent = False
        reconciled = False
        original_binding = None
        continued = False
        continuation_ended = False
        continuation_started = False
        continuation_seen_at = None
        max_tokens_started = False
        max_tokens_ended = False
        max_tokens_seen_at = None
        window_continued = False
        window_continue_ended = False
        resume_admitted = False
        resume_ended = False
        renewal_admitted = False
        renewal_ended = False
        repeat_window_evidence = None
        expired_window_evidence = None
        qa_retry_evidence = None
        checkpoint_ledger = None
        checkpoint_first_budget = None
        checkpoint_quiet = None
        checkpoint_evidence = None
        takeover_rejection = None
        old_ledger_path = old_ledger = old_history = changed_ledger = None
        supervise_first = None
        supervise_terminal = None
        supervise_ledger = None
        while time.monotonic() < deadline:
            if blocked_question_takeover and peer['prompts']:
                sid = record()['dsh_execution']['session_id']
                if blocked_question_stage is None:
                    pending_questions = [{'id': 'q1', 'question': 'Which fixture contract?'},
                        {'id': 'q2', 'question': 'Which fixture test?'}]
                    journal = peer['journals'][sid]
                    journal.append({'type': 'event', 'event': {'type': 'tool/call', 'seq': len(journal), 'time': 4,
                        'data': {'turn': 1, 'step': 1, 'callId': 'fixture-blocked-question', 'name': 'ask_user_question',
                            'arguments': json.dumps({'questions': pending_questions})}}})
                    fixture_behavior['user_questions'] = {'active': [{'callId': 'fixture-blocked-question',
                        'state': 'open', 'questions': pending_questions}], 'settled': []}
                    peer['send_remote']({'type': 'waterfall', 'event': 'user-questions/request',
                        'agentId': sid, 'eventId': 'fixture-blocked-question-event',
                        'request': {'wait': {'callId': 'fixture-blocked-question'}, 'questions': pending_questions}})
                    blocked_question_stage = 'published'
                pending = next((q for q in record()['dsh_execution'].get('questions', [])
                    if q['id'] == 'fixture-blocked-question'), None)
                if blocked_question_stage == 'published' and pending and pending.get('notification') == 'delivered':
                    await receive(adapter, raw('om_partial_question', '@_user_1 q1：Bounded contract',
                        parent_id=pending['delivery']['message_id']))
                    assert list(record()['dsh_execution']['questions'][0]['answers']) == ['q1']
                    with connect_closing() as db:
                        assert kb.block_task(db, initial['card_id'], kind='needs_input', reason='Original question awaits owner.',
                            expected_run_id=worker['run_id'])
                    model.repeat_supervise = False
                    model.worker_steps = 3
                    until = time.monotonic() + 10
                    while time.monotonic() < until:
                        waited, status = os.waitpid(worker['pid'], os.WNOHANG)
                        if waited:
                            assert os.waitstatus_to_exitcode(status) == 0
                            break
                        await asyncio.sleep(.05)
                    else:
                        raise AssertionError('Original pending-question supervisor did not end normally.')
                    worker = None
                    prior = record()
                    previous_claim = dict(prior['dsh_execution']['supervisor_claim'])
                    prior['dsh_execution'].update(state='outcome_unknown', last_failure={
                        'phase': 'worker_claim', 'code': 'repository_busy', 'message': 'Earlier work retains this repository.'})
                    adapter.intake._save_execution(prior, fields=['state', 'last_failure'])
                    assert 'error' not in json.loads(registry.dispatch('kanban_unblock', {'task_id': initial['card_id']}))
                    if blocked_question_continue:
                        with connect_closing() as db:
                            before_card = kb.get_task(db, initial['card_id'])
                            assert kb.block_task(db, initial['card_id'], kind='needs_input', reason='Original question still awaits owner.')
                            assert kb.get_task(db, initial['card_id']).status == 'triage'
                        pending_budget_path = ledger / (hashlib.sha256(sid.encode()).hexdigest() + '.json')
                        prior_budget = pending_budget_path.read_bytes()
                        wrong_scope = record()
                        wrong_scope['dsh_execution']['questions'][0]['generation'] = 'foreign-question-generation'
                        adapter.intake._save_execution(wrong_scope, fields=['questions'])
                        await receive(adapter, raw('om_continue_foreign_question', '@_user_1 继续 ' + initial['id']))
                        with connect_closing() as db:
                            assert kb.get_task(db, initial['card_id']).status == 'triage'
                        assert record()['dsh_execution']['supervisor_claim'] == previous_claim
                        assert pending_budget_path.read_bytes() == prior_budget and len(peer['prompts']) == 1
                        restored = record()
                        restored['dsh_execution']['questions'][0]['generation'] = restored['dsh_execution']['generation']
                        adapter.intake._save_execution(restored, fields=['questions'])
                        await receive(adapter, raw('om_continue_pending_question', '@_user_1 继续 ' + initial['id']))
                        with connect_closing() as db:
                            after_card = kb.get_task(db, initial['card_id'])
                            assert after_card.status in {'todo', 'ready'}, {'card_status': after_card.status, 'failure': record()['dsh_execution'].get('last_failure'), 'messages': sent[-2:]}
                            assert (after_card.title, after_card.body) == (before_card.title, before_card.body)
                        assert pending_budget_path.read_bytes() == prior_budget
                        assert record()['dsh_execution']['supervisor_claim'] == previous_claim
                        assert record()['dsh_execution']['session_id'] == sid and len(peer['prompts']) == 1
                    model.worker_steps = 1
                    model.repeat_supervise = True
                    model.worker_terminal_attempt_at = 5
                    model.worker_finish_at = 6
                    with connect_closing() as db:
                        assert dispatch_once(db, max_spawn=1).spawned
                        worker = remember_worker(db, initial['card_id'])
                    blocked_question_stage = 'reclaimed'
                    continue
                if blocked_question_stage == 'reclaimed':
                    current = record()['dsh_execution']
                    assert current.get('last_failure', {}).get('message') != 'The original worker input remains unconfirmed; no claim transfer.', current.get('last_failure')
                    if current['supervisor_claim']['run_id'] == worker['run_id']:
                        assert current['session_id'] == sid and len(peer['prompts']) == 1
                        assert current['questions'][0]['state'] == 'open' and list(current['questions'][0]['answers']) == ['q1']
                        blocked_question_stage = 'verified'
            if preinput_stop and record()['dsh_execution']['state'] == 'outcome_unknown':
                assert record()['dsh_execution']['last_failure']['code'] == 'repository_busy'
                assert not peer['prompts'] and not record()['dsh_execution'].get('session_id')
                model.repeat_supervise = False
                until = time.monotonic() + 10
                while time.monotonic() < until:
                    waited, status = os.waitpid(worker['pid'], os.WNOHANG)
                    if waited:
                        assert os.waitstatus_to_exitcode(status) == 0
                        break
                    await asyncio.sleep(.05)
                else:
                    raise AssertionError('Original pre-input worker did not normally end.')
                await receive(adapter, raw('om_stop_preinput', '@_user_1 停止 ' + initial['id']))
                stopped = record()['dsh_execution']
                assert stopped['state'] == 'stopped', stopped.get('last_failure')
                assert stopped['stop_control']['proof']['worker']['not_started'] is True
                assert stopped['stop_control']['proof']['worker']['exit_code'] == 0
                assert adapter.intake.snapshot()['work'][0]['dsh_execution']['state'] == 'outcome_unknown'
                with connect_closing() as db:
                    assert kb.get_task(db, initial['card_id']).status == 'archived'
                worker = None
                (scratch / 'shared-worker-result.json').write_text(json.dumps({
                    'status': 'passed', 'preinput_original_worker_verified': True, 'archive_crash_window_retained_unknown': True}))
                return
            if repository_queue:
                assert record()['dsh_execution']['state'] != 'outcome_unknown', record()['dsh_execution'].get('last_failure')
            if supervision_checkpoints and peer['prompts'] and checkpoint_ledger is None:
                checkpoint_ledger = checkpoint_progress(1)
                checkpoint_first_budget = checkpoint_ledger.read_bytes()
                if background_cleanup:
                    sid = peer['prompts'][0]['sessionId']
                    fixture_behavior['jobs'] = [{'id': 'fixture-preview', 'owner': sid, 'status': 'running'}]
                    rows = peer['journals'][sid]
                    call_id = 'fixture-preview-call'
                    rows.append({'type': 'event', 'event': {'type': 'tool/call', 'seq': len(rows), 'time': 5,
                        'data': {'turn': 1, 'step': 1, 'callId': call_id, 'name': 'bash',
                            'arguments': {'command': 'fixture-preview'}}}})
                    rows.append({'type': 'event', 'event': {'type': 'tool/result', 'seq': len(rows), 'time': 5,
                        'data': {'turn': 1, 'step': 1, 'message': {'toolCallId': call_id, 'isError': False,
                            'content': [{'type': 'text', 'text': 'Synthetic background preview registered.'}]}}}})
                    rows.append({'type': 'event', 'event': {'type': 'turn/end', 'seq': len(rows), 'time': 5,
                        'data': {'turn': 1, 'reason': {'kind': 'completed'}}}})
                    registered = {key: json.loads(checkpoint_first_budget)[key] for key in
                        ('schema', 'generation', 'session_id', 'session_created_at', 'service_ref', 'host_lease')}
                    registered.update(type='registered', owner=sid, job_id='fixture-preview',
                        kind='background', command_sha256=hashlib.sha256(b'fixture-preview').hexdigest())
                    job_receipts = ledger / (hashlib.sha256(sid.encode()).hexdigest() + '.jobs.jsonl')
                    job_receipts.write_text(json.dumps(registered) + '\n')
                    job_receipts.chmod(0o600)
                    fixture_behavior.update(registered_preview=registered, job_receipts=job_receipts)
                    fixture_behavior.update(budget_path=checkpoint_ledger, foreign_job_notice=background_foreign_notice)
                    fixture_behavior['unknown_background_cleanup'] = background_unknown
                    source = record()['dsh_execution']['baseline']
                    import importlib
                    acceptance_criteria = importlib.import_module(type(adapter.intake).__module__.rsplit('.', 1)[0] + '.delivery').acceptance_criteria
                    if background_secret:
                        shared_module = importlib.import_module(type(adapter.intake).__module__.rsplit('.', 1)[0] + '.shared_repository')
                        original_ender = shared_module.end_shared_background
                        Error = importlib.import_module(type(adapter.intake).__module__.rsplit('.', 1)[0] + '.manager').ManagementError
                        async def sensitive_failure(*args):
                            try:
                                return await original_ender(*args)
                            except Error as error:
                                raise Error(error.code, str(error) + ' synthetic-unused-secret') from None
                        shared_module.end_shared_background = sensitive_failure
                    report = {'source_commit': source['head'], 'issue_updated_at': initial['issue']['updated_at'],
                        'criteria': [{'text': text, 'test_call_ids': [call_id], 'note': 'Unsupported synthetic note'}
                            for text in acceptance_criteria(initial['issue']['body'])], 'test_files': [],
                        'fine_issue_urls': [], 'pr_url': None, 'sync_branches': {'fixture': 'unsupported'}, 'leftovers': []}
                    settlement = next(row['event']['data'] for row in reversed(rows) if row['event']['type'] == 'assistant/message')
                    settlement['message']['content'] = [{'type': 'text', 'text': 'HERMES_REPOSITORY_DELIVERY_JSON ' + json.dumps(report)}]
                    tool_receipt = {**registered, 'name': 'bash', 'call_id': call_id, 'job_id': 'fixture-preview',
                        'kind': 'background', 'execution_admission': 'admitted', 'is_error': False, 'exit_code': 0,
                        'source_before': source, 'source_after': source, 'native_session_seq': 1}
                    tool_receipt.pop('type')
                    tool_path = ledger / (hashlib.sha256(sid.encode()).hexdigest() + '.tools.jsonl')
                    tool_path.write_text(json.dumps(tool_receipt) + '\n')
                    tool_path.chmod(0o600)
                    next(row for row in peer['sessions'] if row['sessionId'] == sid)['running'] = False
            if peer['prompts'] and not ended and shared_scenario in {'terminal', 'reconcile_intent', 'prepare_continue', *continuation_scenarios}:
                sid = peer['prompts'][0]['sessionId']
                rows = peer['journals'][sid]
                rows.append({'type': 'event', 'event': {'type': 'turn/end', 'seq': len(rows),
                    'time': 4, 'data': {'turn': 1, 'reason': ({'kind': 'error', 'error': {
                        'message': 'Shared work native tool catalog changed.', 'code': 'UNKNOWN'}}
                        if shared_scenario in {'prepare_continue', *continuation_scenarios} else {'kind': 'completed'})}}})
                next(row for row in peer['sessions'] if row['sessionId'] == sid)['running'] = False
                ended = True
            if peer['prompts'] and not ended and completed_renewed_window:
                sid = peer['prompts'][0]['sessionId']
                rows = peer['journals'][sid]
                budget_file = ledger / (hashlib.sha256(sid.encode()).hexdigest() + '.json')
                budget_row = json.loads(budget_file.read_text())
                if qa_request_only_resume:
                    budget_row['budget'].update(max_reported_tokens=300000, max_output_tokens_per_request=8192)
                started = int(time.time() * 1000) - (1801000 if qa_request_only_resume else
                    61000 if renew_window_again or qa_preinput_retry else 1000)
                base_requests = 4 if renew_window_again or qa_request_only_resume else 3
                total_requests = base_requests + 1 if qa_request_only_resume else 55 if qa_preinput_retry else 11 if renew_window_again else 9
                original_state = {'started_at_ms': started, 'requests': base_requests,
                                  'reported_tokens': 700 if renew_window_again else 30,
                                  'admissions': ['fixture-old-' + str(index) for index in range(base_requests)],
                                  'stop_reason': 'shared-work-wall-budget' if qa_preinput_retry else None}
                budget_row['state'] = {**original_state, 'requests': total_requests,
                    'reported_tokens': 911 if renew_window_again else 90,
                    'admissions': [*original_state['admissions'],
                        *['fixture-current-' + str(index) for index in range(total_requests - base_requests)]]}
                budget_row['renewWindow'] = {'id': 'window-fixture', 'message_id': 'om-prior-window',
                    'host_lease': 'fixture-shared-lease', 'request_base': base_requests, 'original_state': original_state,
                    'original_host_lease': 'fixture-original-lease', 'started_at_ms': started, 'stop_reason': None,
                    'hook_marker': 'same-sid-window-v1'}
                binding = {key: budget_row[key] for key in ('schema', 'service_ref', 'host_lease', 'agent_preset',
                    'session_id', 'session_created_at', 'generation', 'workspace', 'provider', 'model')}
                binding['budget'] = {key: budget_row['budget'][key] for key in
                    ('max_wall_seconds', 'max_model_requests', 'max_reported_tokens', 'max_output_tokens_per_request')
                    if key in budget_row['budget']}
                binding['renewWindow'] = {key: value for key, value in budget_row['renewWindow'].items()
                    if key not in {'stop_reason', 'hook_marker', 'started_at_ms'}}
                budget_row['state']['binding_sha256'] = hashlib.sha256(json.dumps(binding, separators=(',', ':'),
                    ensure_ascii=False).encode()).hexdigest()
                budget_file.write_text(json.dumps(budget_row))
                for index in range(total_requests):
                    if qa_preinput_retry and index == base_requests - (2 if qa_request_only_resume else 1):
                        rows.append({'type': 'event', 'event': {'type': 'assistant/message', 'seq': len(rows), 'time': 4,
                            'data': {'turn': 1, 'step': index, 'message': {'id': 'fixture-interrupted-usage',
                                'role': 'assistant', 'content': []}}}})
                        rows.append({'type': 'event', 'event': {'type': 'step/end', 'seq': len(rows), 'time': 4,
                            'data': {'turn': 1, 'step': index}}})
                        rows.append({'type': 'event', 'event': {'type': 'turn/end', 'seq': len(rows), 'time': 4,
                            'data': {'turn': 1, 'reason': {'kind': 'aborted', 'reason': {
                                'kind': 'hook', 'reason': 'shared-work-wall-budget'}}}}})
                        rows.append({'type': 'event', 'event': {'type': 'turn/start', 'seq': len(rows), 'time': 4,
                            'data': {'turn': 2}}})
                        continue
                    rows.append({'type': 'event', 'event': {'type': 'assistant/message', 'seq': len(rows), 'time': 4,
                        'data': {'turn': 3 if qa_request_only_resume and index >= base_requests else
                            2 if qa_preinput_retry and index >= base_requests - (1 if qa_request_only_resume else 0) else 1,
                            'step': index, 'usage': {'inputTokens': 10, 'outputTokens': 5},
                            'message': {'id': 'fixture-settlement-' + str(index), 'role': 'assistant',
                                'content': [{'type': 'text', 'text': 'Synthetic bounded work.'}]}, 'stream': []}}})
                    if qa_request_only_resume and index == base_requests - 1:
                        rows.append({'type': 'event', 'event': {'type': 'turn/end', 'seq': len(rows), 'time': 4,
                            'data': {'turn': 2, 'reason': {'kind': 'completed'}}}})
                        rows.append({'type': 'event', 'event': {'type': 'turn/start', 'seq': len(rows), 'time': 4,
                            'data': {'turn': 3}}})
                for index in range(6):
                    call_id = 'fixture-rejected-bash-' + str(index)
                    rejection_text = ('Error: Shared work tool ownership is unknown.' if renew_window_again
                                      else 'Shared work tool ownership is unknown')
                    rows.append({'type': 'event', 'event': {'type': 'tool/call', 'seq': len(rows), 'time': 4,
                        'data': {'turn': 1, 'step': index, 'callId': call_id, 'name': 'bash', 'arguments': {'command': 'printf fixture'}}}})
                    rows.append({'type': 'event', 'event': {'type': 'tool/result', 'seq': len(rows), 'time': 4,
                        'data': {'turn': 1, 'step': index, 'message': {'toolCallId': call_id, 'isError': True,
                            'content': [{'type': 'text', 'text': rejection_text}]}}}})
                source = {'head': None, 'workspace_status': '', 'source_digest': hashlib.sha256(b'').hexdigest()}
                promoted = source
                if qa_preinput_retry:
                    # Delivery source and receipts belong to the synthetic external DSH boundary.
                    import importlib
                    package = adapter.intake.__class__.__module__.rsplit('.', 1)[0]
                    acceptance_module = importlib.import_module(package + '.repository_acceptance')
                    (repo / 'qa.test.mjs').write_text("import test from 'node:test'; test('fixture browser receipt', () => {});\n")
                    subprocess.run(['git', '-C', str(repo), 'add', 'qa.test.mjs'], check=True)
                    subprocess.run(['git', '-C', str(repo), '-c', 'user.name=Fixture',
                        '-c', 'user.email=fixture@example.invalid', 'commit', '-qm', 'Synthetic QA delivery'], check=True)
                    delivered = acceptance_module.repository_source_state(record()['target'])
                    promoted = {key: delivered[key] for key in ('head', 'workspace_status', 'source_digest')}
                    report = {'source_commit': promoted['head'], 'issue_updated_at': initial['issue']['updated_at'],
                        'criteria': [{'text': 'Implement the requested feature and verify it.',
                            'test_call_ids': ['fixture-missing-browser-test']}], 'test_files': ['qa.test.mjs'],
                        'fine_issue_urls': [], 'pr_url': None, 'sync_branches': [], 'leftovers': []}
                    settlement = next(row['event']['data'] for row in reversed(rows)
                        if row['event']['type'] == 'assistant/message')
                    settlement['message']['content'] = [{'type': 'text',
                        'text': 'HERMES_REPOSITORY_DELIVERY_JSON ' + json.dumps(report)}]
                receipt_rows = [{'schema': 1, 'generation': budget_row['generation'], 'session_id': sid,
                    'session_created_at': budget_row['session_created_at'], 'service_ref': 'fixture-native',
                    'host_lease': 'fixture-shared-lease', 'call_id': 'fixture-source-' + str(index), 'name': 'bash',
                    'kind': 'foreground', 'execution_admission': 'admitted', 'is_error': False, 'exit_code': 0,
                    'timed_out': False, 'aborted': False, 'native_session_seq': index + 1,
                    'source_before': source if index == 0 else promoted,
                    'source_after': promoted} for index in range(3)] + [{'schema': 1, 'generation': budget_row['generation'], 'session_id': sid,
                    'session_created_at': budget_row['session_created_at'], 'service_ref': 'fixture-native',
                    'host_lease': 'fixture-shared-lease', 'call_id': 'fixture-rejected-bash-' + str(index), 'name': 'bash',
                    'is_error': True, 'execution_admission': 'rejected'} for index in range(6)]
                if renew_window_again:
                    receipt_rows[1]['exit_code'] = 1
                    assert receipt_rows[1]['exit_code'] == 1
                receipt_path = ledger / (hashlib.sha256(sid.encode()).hexdigest() + '.tools.jsonl')
                receipt_path.write_text(''.join(json.dumps(row) + '\n' for row in receipt_rows))
                receipt_path.chmod(0o600)
                rows.append({'type': 'event', 'event': {'type': 'turn/end', 'seq': len(rows), 'time': 4,
                    'data': {'turn': 3 if qa_request_only_resume else 2 if qa_preinput_retry else 1,
                        'reason': {'kind': 'completed'}}}})
                if qa_request_only_resume:
                    shared_module = importlib.import_module(package + '.shared_repository')
                    prior_qa_window = json.loads(json.dumps(budget_row['renewWindow']))
                    prior_qa_window.update(id='window-prior-sealed-fixture', request_base=base_requests - 1,
                        original_state={**original_state, 'requests': base_requests - 1,
                            'admissions': original_state['admissions'][:-1]})
                    prior_qa_seal = shared_module._sealed_unknown_usage([row['event'] for row in rows], prior_qa_window)
                    budget_row['renewWindow']['sealed_unknown_usage'] = prior_qa_seal
                    binding['renewWindow']['sealed_unknown_usage'] = prior_qa_seal
                    budget_row['state']['binding_sha256'] = hashlib.sha256(json.dumps(binding, separators=(',', ':'),
                        ensure_ascii=False).encode()).hexdigest()
                    budget_file.write_text(json.dumps(budget_row))
                next(row for row in peer['sessions'] if row['sessionId'] == sid)['running'] = False
                ended = True
            latest = record()
            if (missing_report_rework and ended and latest['dsh_execution']['state'] == 'awaiting_acceptance'
                    and 'delivery_report_missing' in latest['dsh_execution'].get('acceptance', {}).get('unmet', [])):
                until = time.monotonic() + 12
                while time.monotonic() < until:
                    waited, status = os.waitpid(worker['pid'], os.WNOHANG)
                    if waited:
                        assert os.waitstatus_to_exitcode(status) == 0
                        break
                    await asyncio.sleep(.05)
                else:
                    raise AssertionError('Original missing-report supervisor did not normally end.')
                worker = None
                budget_file = checkpoint_progress(1)
                before_budget = budget_file.read_bytes()
                before_acceptance = json.loads(json.dumps(record()['dsh_execution']['acceptance']))
                notification = record()['dsh_execution']['delivery_notification']
                assert notification['status'] == 'delivered' and notification.get('message_id')
                result = {}
                async def report_handler(event):
                    result['value'] = json.loads(await asyncio.to_thread(registry.dispatch,
                        'hermes_pm_repository_rework', {'work_id': initial['id']}))
                runner._wire_adapter_handlers(adapter, message_handler=report_handler)
                await receive(adapter, raw('om_report_request', '@_user_1 请在原会话补齐真实报告和实际测试，保留原预算。',
                    parent_id=notification['message_id']))
                until = time.monotonic() + 8
                while not result and time.monotonic() < until:
                    await asyncio.sleep(.02)
                assert result.get('value', {}).get('status') == 'accepted' and result['value'].get('work_id') == initial['id'], result
                assert result['value'].get('execution_started') is False
                assert record()['dsh_execution']['acceptance'] == before_acceptance
                assert record()['dsh_execution']['session_id'] == latest['dsh_execution']['session_id']
                assert len(peer['prompts']) == 1 and budget_file.read_bytes() == before_budget
                model.worker_steps = 1
                with connect_closing() as db:
                    assert dispatch_once(db, max_spawn=1).spawned
                    worker = remember_worker(db, initial['card_id'])
                until = time.monotonic() + 12
                while len(peer['prompts']) < 2 and time.monotonic() < until:
                    assert record()['dsh_execution']['state'] != 'outcome_unknown', record()['dsh_execution'].get('last_failure')
                    await asyncio.sleep(.05)
                assert len(peer['prompts']) == 2
                assert peer['prompts'][1]['sessionId'] == latest['dsh_execution']['session_id']
                assert 'HERMES_REPOSITORY_DELIVERY_JSON' in peer['prompts'][1]['content'][0]['text']
                assert budget_file.read_bytes() == before_budget and record()['dsh_execution']['acceptance'] == before_acceptance
                journal = peer['journals'][latest['dsh_execution']['session_id']]
                journal.append({'type': 'event', 'event': {'type': 'turn/start', 'seq': len(journal), 'time': 8, 'data': {'turn': 2}}})
                journal.append({'type': 'event', 'event': {'type': 'turn/end', 'seq': len(journal), 'time': 8,
                    'data': {'turn': 2, 'reason': {'kind': 'completed'}}}})
                next(row for row in peer['sessions'] if row['sessionId'] == latest['dsh_execution']['session_id'])['running'] = False
                until = time.monotonic() + 12
                while time.monotonic() < until:
                    waited, status = os.waitpid(worker['pid'], os.WNOHANG)
                    if waited:
                        assert os.waitstatus_to_exitcode(status) == 0
                        worker = None
                        break
                    await asyncio.sleep(.05)
                assert worker is None
                (scratch / 'shared-worker-result.json').write_text(json.dumps({
                    'status': 'passed', 'missing_report_public_intake': True, 'acceptance_not_fabricated': True}))
                return
            if background_cleanup and not background_foreign_notice:
                assert latest['dsh_execution']['state'] != 'outcome_unknown', latest['dsh_execution'].get('last_failure')
            if supervision_checkpoints:
                checkpoint = latest['dsh_execution'].get('supervision_checkpoint', {})
                if (background_cleanup and checkpoint.get('delivery', {}).get('status') == 'delivered'
                        and checkpoint.get('observed_state') == 'awaiting_acceptance'
                        and latest['dsh_execution'].get('acceptance', {}).get('status') == 'unmet'):
                    actual_prompt = peer['prompts'][0]['content'][0]['text']
                    assert initial['issue']['updated_at'] in actual_prompt
                    assert 'only text and test_call_ids' in actual_prompt and 'array of branch names' in actual_prompt
                    assert 'native sub-issue relationships' in actual_prompt and 'body links alone do not count' in actual_prompt
                    assert 'Never infer receipt meaning from list order' in actual_prompt
                    assert latest['dsh_execution']['state'] == 'awaiting_acceptance'
                    assert latest['dsh_execution']['jobs'] == [{'id': 'fixture-preview', 'status': 'running'}]
                    assert latest['dsh_execution']['acceptance']['status'] == 'unmet'
                    notices = [message for message in sent if checkpoint['summary'] in message.request_body.content]
                    assert len(notices) == 1 and notices[0].message_id == 'om_shared'
                    if background_whole_stop:
                        await receive(adapter, raw('om_whole_stop', '@_user_1 停止 ' + latest['id']))
                        stopped = record()['dsh_execution']
                        assert stopped['state'] == 'stopped', stopped.get('last_failure')
                        assert stopped['stop_control']['queue_remove'] == {'fixture-native-job-notice': 'accepted'}
                        assert stopped['stop_control']['proof']['worker']['exit_code'] == 0
                        assert stopped['session_id'] == latest['dsh_execution']['session_id']
                        with connect_closing() as db:
                            assert kb.get_task(db, initial['card_id']).status == 'archived'
                            assert kb.get_task(db, queued['card_id']).status == 'ready'
                            assert kb.unsatisfied_parents(db, queued['card_id']) == []
                        worker = None
                        (scratch / 'shared-worker-result.json').write_text(json.dumps({
                            'status': 'passed', 'whole_stop_owned_notice': True, 'native_dependency_released': True}))
                        return
                    if background_ended_worker:
                        model.review_supervision_checkpoints = False
                        if background_failed_worker:
                            model.fail_worker_after = model.worker_steps + 1
                        else:
                            model.worker_steps = 3
                            model.worker_finish_at = 5
                        until = time.monotonic() + 10
                        while time.monotonic() < until:
                            waited, status = os.waitpid(worker['pid'], os.WNOHANG)
                            if waited:
                                assert (os.waitstatus_to_exitcode(status) != 0) is background_failed_worker
                                old_claim = record()['dsh_execution']['supervisor_claim']
                                worker = None
                                break
                            await asyncio.sleep(.05)
                        assert worker is None, 'The original preview supervisor did not normally block and exit.'
                        if background_failed_worker:
                            model.fail_worker_after = None
                            os.environ['HERMES_KANBAN_CRASH_GRACE_SECONDS'] = '0'
                            with connect_closing() as db:
                                dispatch_once(db, max_spawn=0)
                                failed_run = kb.get_run(db, old_claim['run_id'])
                                assert failed_run.status in {'failed', 'crashed', 'timed_out', 'gave_up'}
                            assert 'error' not in json.loads(registry.dispatch('kanban_block', {
                                'task_id': initial['card_id'], 'kind': 'needs_input', 'reason': 'Owner ends the failed original work.'}))
                        if background_later_worker:
                            model.repeat_supervise = False
                            model.worker_steps = 1
                            model.worker_finish_at = 5
                            model.worker_pause_on_block = True
                            assert 'error' not in json.loads(registry.dispatch('kanban_unblock', {
                                'task_id': initial['card_id']}))
                            with connect_closing() as db:
                                assert dispatch_once(db, max_spawn=1).spawned
                                worker = remember_worker(db, initial['card_id'])
                            until = time.monotonic() + 15
                            while not model.worker_request_ready.is_set() and time.monotonic() < until:
                                await asyncio.sleep(.05)
                            assert model.worker_request_ready.is_set(), 'Later worker did not reach its terminal pause.'
                            with connect_closing() as db:
                                assert kb.latest_run(db, initial['card_id']).ended_at is not None
                            assert os.waitpid(worker['pid'], os.WNOHANG) == (0, 0)
                            try:
                                await receive(adapter, raw('om_end_later_live', '@_user_1 结束后台 ' + latest['id']))
                                rejected = record()['dsh_execution']
                                assert rejected.get('stop_control', {}).get('status') != 'background_ended'
                                assert rejected['last_failure']['code'] == 'outcome_unknown'
                                assert not any(call['method'] == 'job/kill' for call in peer['calls'])
                                log_path = kb.worker_log_path(initial['card_id'])
                                with log_path.open('ab') as log:
                                    log.write(b'fixture-long-native-diagnostic ' + b'x' * 6500 + b'\n')
                            finally:
                                model.worker_request_release.set()
                            until = time.monotonic() + 15
                            while time.monotonic() < until:
                                waited, status = os.waitpid(worker['pid'], os.WNOHANG)
                                if waited:
                                    assert os.waitstatus_to_exitcode(status) == 0
                                    worker = None
                                    break
                                await asyncio.sleep(.05)
                            assert worker is None, 'The later native worker did not block and end.'
                            assert record()['dsh_execution']['supervisor_claim'] == old_claim
                            with connect_closing() as db:
                                later_run = kb.latest_run(db, initial['card_id'])
                                assert later_run.id != old_claim['run_id'] and later_run.ended_at is not None
                            # Corrupt one external log observation, then restore its exact byte.
                            # The actual later exit must never substitute for the original receipt.
                            log_bytes = log_path.read_bytes()
                            prefix = b'[kanban-worker-exit] rc='
                            original_exit = log_bytes.index(prefix + b'0', old_claim['exit_log_offset']) + len(prefix)
                            assert len(log_bytes) - original_exit > 4000
                            with log_path.open('r+b') as log:
                                log.seek(original_exit)
                                log.write(b'1')
                            try:
                                await receive(adapter, raw('om_end_bad_original_exit', '@_user_1 结束后台 ' + latest['id']))
                                rejected = record()['dsh_execution']
                                assert rejected.get('stop_control', {}).get('status') != 'background_ended'
                                assert rejected['last_failure']['code'] == 'outcome_unknown'
                                assert not any(call['method'] == 'job/kill' for call in peer['calls'])
                            finally:
                                with log_path.open('r+b') as log:
                                    log.seek(original_exit)
                                    log.write(b'0')
                            assert log_path.read_bytes() == log_bytes
                    await receive(adapter, raw('om_foreign_end', '@_user_1 结束后台 ' + latest['id'], sender={
                        'sender_id': {'open_id': 'ou_foreign', 'user_id': 'u_foreign', 'union_id': 'on_foreign'}}))
                    assert not any(call['method'] == 'job/kill' for call in peer['calls'])
                    await receive(adapter, raw('om_end_preview', '@_user_1 结束后台 ' + latest['id']))
                    cleanup = record()['dsh_execution']
                    if background_foreign_notice:
                        assert cleanup['stop_control']['status'] == 'unverified'
                        assert cleanup['stop_control']['job_cancel'] == {'fixture-preview': 'accepted'}
                        assert len([call for call in peer['calls'] if call['method'] == 'job/kill']) == 1
                        assert not any(call['method'] == 'session/updateQueue' for call in peer['calls'])
                        assert [item['id'] for item in fixture_behavior['inbox']['next-step']] == [
                            'fixture-native-job-notice', 'fixture-foreign-input']
                        assert len(peer['prompts']) == 1
                        checkpoint_evidence = {'mixed_notice_preserved': True, 'foreign_input_rejected': True,
                            'input_count': 1, 'job_kill_count': 1}
                        break
                    if background_unknown:
                        assert cleanup['stop_control']['status'] == 'unverified'
                        assert cleanup['stop_control']['job_cancel'] == {'fixture-preview': 'intent'}
                        assert cleanup['last_failure']['code'] == 'outcome_unknown'
                        if background_secret:
                            assert 'synthetic-unused-secret' not in json.dumps(record()), 'Registered credential escaped control diagnostics.'
                            assert '[REDACTED_CREDENTIAL]' in cleanup['stop_control']['last_failure']['message']
                        await receive(adapter, raw('om_unknown_whole_stop', '@_user_1 停止 ' + latest['id']))
                        stopped = record()['dsh_execution']
                        assert len([call for call in peer['calls'] if call['method'] == 'job/kill']) == 1, 'Whole stop replayed an unknown background cancellation.'
                        assert stopped['stop_control']['job_cancel'] == {'fixture-preview': 'intent'}
                        assert stopped['stop_requested'] and stopped['state'] != 'stopped'
                        assert stopped['acceptance'] == cleanup['acceptance'] and stopped['acceptance']['delivered'] is False
                        assert stopped['jobs'] == [{'id': 'fixture-preview', 'status': 'running'}]
                        assert len(peer['prompts']) == 1 and checkpoint_ledger.read_bytes() == checkpoint_first_budget
                        assert 'synthetic-unused-secret' not in json.dumps(record())
                        until = time.monotonic() + 10
                        while time.monotonic() < until:
                            waited, status = os.waitpid(worker['pid'], os.WNOHANG)
                            if waited:
                                assert os.waitstatus_to_exitcode(status) == 0
                                worker = None
                                break
                            await asyncio.sleep(.05)
                        assert worker is None
                        checkpoint_evidence = {'unknown_cleanup_preserved': True, 'job_kill_count': 1,
                            'worker_exit': 0, 'budget_unchanged': True, 'input_count': 1, 'credential_filtered': background_secret}
                        break
                    assert cleanup.get('stop_control', {}).get('status') == 'background_ended', cleanup.get('last_failure')
                    await receive(adapter, raw('om_end_preview', '@_user_1 结束后台 ' + latest['id']))
                    assert len([call for call in peer['calls'] if call['method'] == 'job/kill']) == 1
                    assert len(peer['prompts']) == 1
                    after_jobs = json.loads(checkpoint_ledger.read_text())
                    observed_at = after_jobs['state'].pop('observed_at_ms')
                    before_jobs = json.loads(checkpoint_first_budget)
                    assert before_jobs['state'].pop('observed_at_ms') <= observed_at <= int(time.time() * 1000)
                    assert after_jobs == before_jobs
                    assert len([call for call in peer['calls'] if call['method'] == 'session/updateQueue']) == 1
                    assert fixture_behavior['inbox'] == {'next-turn': [], 'next-step': []}
                    assert any(item.get('source', {}).get('kind') == 'tool-jobs' for row in peer['journals'][sid]
                        if row['event']['type'] == 'agent/inbox/spliced' for item in row['event']['data'].get('inserted', []))
                    assert not record()['dsh_execution'].get('stop_requested')
                    if background_failed_worker:
                        proof = cleanup['stop_control']['proof']['worker']
                        assert proof['exit_code'] is None and any(code != 0 for code in proof['exit_receipts'])
                        assert old_claim['run_id'] in proof['native_failed_run_ids']
                        assert cleanup['acceptance']['status'] != 'accepted'
                        await receive(adapter, raw('om_stop_failed_original', '@_user_1 停止 ' + latest['id']))
                        stopped = record()['dsh_execution']
                        assert stopped['state'] == 'stopped', stopped.get('last_failure')
                        assert stopped['stop_control']['proof']['worker']['exit_code'] is None
                        assert stopped['acceptance']['status'] != 'accepted' and len(peer['prompts']) == 1
                        with connect_closing() as db:
                            assert kb.get_task(db, initial['card_id']).status == 'archived'
                        (scratch / 'shared-worker-result.json').write_text(json.dumps({
                            'status': 'passed', 'actual_failed_worker_exit_preserved': True, 'no_false_delivery': True}))
                        return
                    if background_later_worker:
                        assert cleanup['stop_control']['proof']['worker']['run_id'] == old_claim['run_id']
                        await receive(adapter, raw('om_stop_after_later_worker', '@_user_1 停止 ' + latest['id']))
                        stopped = record()['dsh_execution']
                        assert stopped['state'] == 'stopped', stopped.get('last_failure')
                        assert stopped['stop_control']['proof']['worker']['run_id'] == old_claim['run_id']
                        assert len(peer['prompts']) == 1
                        with connect_closing() as db:
                            assert kb.get_task(db, initial['card_id']).status == 'archived'
                        (scratch / 'shared-worker-result.json').write_text(json.dumps({
                            'status': 'passed', 'later_terminal_worker_preserved': True,
                            'original_claim_cleanup_and_stop': True, 'dsh_input_count': 1}))
                        return
                    if background_ended_worker:
                        assert cleanup['stop_control']['proof']['worker']['run_id'] == old_claim['run_id']
                        assert cleanup['stop_control']['proof']['worker']['exit_code'] == 0
                        previous_owner = record()
                        previous_cursor = next(row['event']['seq'] for row in peer['journals'][sid]
                            if row['event']['type'] == 'turn/start') - 1
                        previous_owner['dsh_execution'].update(continuation_control={
                            'kind': 'qa_rework', 'status': 'accepted', 'dispatch_status': 'accepted', 'cursor': previous_cursor},
                            owner_context_control={'status': 'accepted', 'dispatch_status': 'accepted', 'cursor': previous_cursor,
                                'request_id': previous_owner['dsh_execution']['request_id']})
                        adapter.intake._save_execution(previous_owner, fields=['continuation_control', 'owner_context_control'])
                        observed_budget = json.loads(checkpoint_ledger.read_text())
                        observed_budget['state']['observed_at_ms'] = int(time.time() * 1000)
                        checkpoint_ledger.write_text(json.dumps(observed_budget))
                        model.worker_steps = 0
                        model.worker_finish_at = None
                        model.review_supervision_checkpoints = True
                        with connect_closing() as db:
                            assert dispatch_once(db, max_spawn=1).spawned
                            worker = remember_worker(db, initial['card_id'])
                        assert worker['run_id'] != old_claim['run_id']
                    until = time.monotonic() + 10
                    while time.monotonic() < until:
                        if background_ended_worker:
                            supervised = record()['dsh_execution']
                            assert supervised['state'] != 'outcome_unknown', supervised.get('last_failure')
                        waited, status = os.waitpid(worker['pid'], os.WNOHANG)
                        if waited:
                            assert os.waitstatus_to_exitcode(status) == 0
                            worker = None
                            break
                        await asyncio.sleep(.05)
                    assert worker is None, 'The original completed preview worker did not exit.'
                    latest = record()
                    if background_ended_worker:
                        assert latest['dsh_execution']['supervisor_claim']['run_id'] != old_claim['run_id']
                    assert latest['dsh_execution']['acceptance']['status'] == 'unmet'
                    assert latest['dsh_execution']['acceptance']['delivered'] is False
                    assert 'delivery_report_missing' not in latest['dsh_execution']['acceptance']['unmet']
                    assert 'test_evidence_missing' in latest['dsh_execution']['acceptance']['unmet']
                    assert any('验收未满足' in message.request_body.content for message in sent)
                    if background_ended_worker:
                        # The prior candidate's unsent supervision failure retains its original range proof.
                        failed = record()
                        failed['dsh_execution'].update(state='outcome_unknown', last_failure={
                            'phase': 'budget', 'code': 'outcome_unknown', 'type': 'ManagementError',
                            'message': 'Foreign input in the original owner-context history was preserved.'})
                        adapter.intake._save_execution(failed, fields=['state', 'last_failure'])
                        completed_claim = failed['dsh_execution']['supervisor_claim']
                        observed_budget = json.loads(checkpoint_ledger.read_text())
                        previous_observed = observed_budget['state']['observed_at_ms']
                        observed_budget['state']['observed_at_ms'] = int(time.time() * 1000)
                        assert observed_budget['state']['observed_at_ms'] > previous_observed
                        checkpoint_ledger.write_text(json.dumps(observed_budget))
                        restore_budget = checkpoint_ledger.read_bytes()
                        await receive(adapter, raw('om_resume_background_supervision', '@_user_1 结束后台 ' + latest['id']))
                        assert record()['dsh_execution']['state'] == 'awaiting_acceptance'
                        assert record()['dsh_execution']['stop_control']['proof']['worker']['run_id'] == completed_claim['run_id']
                        model.worker_steps = 0
                        with connect_closing() as db:
                            assert dispatch_once(db, max_spawn=1).spawned
                            worker = remember_worker(db, initial['card_id'])
                        assert worker['run_id'] != completed_claim['run_id']
                        until = time.monotonic() + 10
                        while time.monotonic() < until:
                            assert record()['dsh_execution']['state'] != 'outcome_unknown', record()['dsh_execution'].get('last_failure')
                            waited, status = os.waitpid(worker['pid'], os.WNOHANG)
                            if waited:
                                assert os.waitstatus_to_exitcode(status) == 0
                                worker = None
                                break
                            await asyncio.sleep(.05)
                        assert worker is None, 'The normally restored original supervisor did not exit.'
                        assert len([call for call in peer['calls'] if call['method'] == 'job/kill']) == 1
                        assert len([call for call in peer['calls'] if call['method'] == 'session/updateQueue']) == 1
                        assert len(peer['prompts']) == 1 and checkpoint_ledger.read_bytes() == restore_budget
                        latest = record()
                        assert latest['dsh_execution']['acceptance']['status'] == 'unmet'
                        assert latest['dsh_execution']['last_failure'] == failed['dsh_execution']['last_failure']
                    background_proof = latest['dsh_execution']['stop_control']['proof']
                    assert background_proof['session']['settled_job_ids'] == ['fixture-preview']
                    await receive(adapter, raw('om_checkpoint_stop', '@_user_1 停止 ' + latest['id']))
                    latest = record()
                    assert latest['dsh_execution']['state'] == 'stopped'
                    assert latest['dsh_execution']['stop_control']['proof']['worker']['exit_code'] == 0
                    checkpoint_evidence = {'notified': checkpoint, 'background_proof': background_proof,
                        'worker_exit': 0, 'input_count': 1, 'budget_unchanged': True, 'foreign_rejected': True, 'duplicate': True}
                    if background_ended_worker:
                        checkpoint_evidence['normal_worker_restore'] = True
                    break
                if checkpoint.get('status') == 'reviewed' and checkpoint.get('notify_owner') is False and checkpoint_quiet is None:
                    checkpoint_quiet = json.loads(json.dumps(checkpoint))
                    assert checkpoint_quiet['trigger'] == ['time'], checkpoint_quiet
                    assert checkpoint_quiet['requests'] == 1 and checkpoint_quiet['request_delta'] == 1
                    assert checkpoint_quiet['budget'] == {'limits': {}, 'request_base': 0, 'requests_used': 1,
                        'requests_total': 1, 'requests_remaining': None, 'wall_seconds_remaining': None}
                    assert checkpoint_quiet['acceptance_status'] == 'unverified'
                    assert checkpoint_quiet['delivery']['status'] == 'not_requested'
                    assert not any(checkpoint_quiet['summary'] in message.request_body.content for message in sent)
                    assert checkpoint_ledger.read_bytes() == checkpoint_first_budget
                    checkpoint_progress(11)
                    checkpoint_budget = checkpoint_ledger.read_bytes()
                results = [item for batch in model.worker_tool_results for item in batch if isinstance(item, dict)]
                if checkpoint_quiet is not None and checkpoint.get('status') == 'reviewed' and checkpoint.get('notify_owner') is True \
                        and checkpoint.get('delivery', {}).get('status') == 'delivered' \
                        and any(item.get('status') == 'duplicate' for item in results):
                    assert checkpoint['id'] != checkpoint_quiet['id']
                    assert 'requests' in checkpoint['trigger'] and checkpoint['request_delta'] == 10, checkpoint
                    assert checkpoint['requests'] == 11 and checkpoint['reported_tokens'] == 220
                    assert checkpoint['budget']['requests_used'] == 11 and checkpoint['budget']['requests_remaining'] is None
                    assert checkpoint['latest_progress']['from_current_turn'] is True
                    instructions = [item['instruction'] for item in results if item.get('status') == 'review_required']
                    assert instructions and all('authoritative current facts' in text and 'from_current_turn=false' in text
                        and 'no plugin token quota' in text for text in instructions)
                    assert latest['dsh_execution']['supervision_history'][-1] == checkpoint_quiet
                    notices = [message for message in sent if checkpoint['summary'] in message.request_body.content]
                    assert len(notices) == 1 and notices[0].message_id == 'om_shared'
                    assert '未设置请求上限' in notices[0].request_body.content and '无插件时限' in notices[0].request_body.content
                    assert '待答问题：0' in notices[0].request_body.content and '当前验收状态：unverified' in notices[0].request_body.content
                    assert checkpoint['delivery']['status'] == 'delivered'
                    assert checkpoint_ledger.read_bytes() == checkpoint_budget
                    assert latest['dsh_execution']['state'] == 'running' and len(peer['prompts']) == 1
                    assert len(model.checkpoint_review_calls) == 2 and model.checkpoint_duplicate_sent
                    unauthorized = await asyncio.to_thread(registry.dispatch, 'hermes_pm_supervise',
                        model.checkpoint_review_calls[-1])
                    assert json.loads(unauthorized) == {'status': 'rejected', 'code': 'worker_required'}
                    assert checkpoint_ledger.read_bytes() == checkpoint_budget and len(notices) == 1
                    await receive(adapter, raw('om_checkpoint_stop', '@_user_1 停止 ' + latest['id']))
                    latest = record()
                    assert latest['dsh_execution']['state'] == 'stopped', latest['dsh_execution']
                    proof = latest['dsh_execution']['stop_control']['proof']
                    assert proof['session']['scope'] == 'shared_session' and proof['session']['host_shutdown'] is False
                    assert proof['worker']['exit_code'] == 0
                    assert latest['dsh_execution']['session_id'] == checkpoint['session_id']
                    assert json.loads(checkpoint_ledger.read_text())['state']['requests'] == 11
                    worker = None
                    checkpoint_evidence = {'quiet': checkpoint_quiet, 'notified': checkpoint,
                        'notification_count': len(notices), 'duplicate': True, 'unauthorized': json.loads(unauthorized),
                        'worker_exit': 0, 'ledger_before_stop': json.loads(checkpoint_budget), 'stop_proof': proof}
                    break
            if supervise_slice and (not blocked_question_takeover or blocked_question_stage == 'verified'):
                tool_results = [result for batch in getattr(model, 'worker_tool_results', []) for result in batch
                                if isinstance(result, dict)]
                running_results = [result for result in tool_results
                                   if result.get('status') == 'running' and result.get('next_action') == 'hermes_pm_supervise']
                stopped_results = [result for result in tool_results
                                   if result.get('status') == 'budget_stopped']
                rejected_results = [result for result in tool_results if result.get('status') == 'unverified']
                if repository_queue and running_results:
                    waiting = json.loads(registry.dispatch('kanban_show', {'task_id': queued['card_id']}))
                    assert initial['card_id'] in waiting['parents']
                    with connect_closing() as db:
                        assert kb.claim_task(db, queued['card_id']) is None
                    assert len(peer['prompts']) == 1 and peer['prompts'][0]['sessionId'] == record()['dsh_execution']['session_id']
                    assert not adapter.intake.snapshot()['work'][1]['dsh_execution'].get('session_id')
                if takeover is not None and takeover_case != 'valid' and rejected_results:
                    takeover_rejection = rejected_results[-1]
                    expected_code = ('unauthorized' if takeover_case == 'card' else 'outcome_unknown'
                        if takeover_case in {'nonzero', 'reclaimed'} else 'binding_conflict')
                    assert takeover_rejection['code'] == expected_code
                    latest = record()
                    assert latest['dsh_execution']['supervisor_claim'] == takeover['old_claim']
                    assert len(peer['prompts']) == 1
                    assert old_ledger_path is not None and old_ledger is not None and old_history is not None
                    assert takeover_case != 'ledger' or changed_ledger is not None
                    assert (json.loads(old_ledger_path.read_bytes()) == changed_ledger if takeover_case == 'ledger'
                        else old_ledger_path.read_bytes() == old_ledger)
                    assert peer['journals'][takeover['old_bindings']['session_id']] == old_history
                    model.repeat_supervise = False
                    while worker is not None:
                        waited, status = os.waitpid(worker['pid'], os.WNOHANG)
                        if waited:
                            assert os.waitstatus_to_exitcode(status) == 0
                            worker = None
                            break
                        await asyncio.sleep(.05)
                    latest = record()
                    break
                if supervise_first is None and running_results:
                    supervise_first = running_results[-1]
                    assert supervise_first == {'status': 'running', 'next_action': 'hermes_pm_supervise', 'delivered': False}
                    assert observer_ready and len(peer['prompts']) == 1
                    if supervise_takeover and takeover is None:
                        # The public dispatcher cannot create a second live claim.
                        with connect_closing() as db:
                            assert kb.claim_task(db, initial['card_id']) is None
                            if takeover_case == 'reclaimed':
                                assert kb.reclaim_task(db, initial['card_id'], reason='Synthetic native reclaim rejection case')
                        # Otherwise keep supervising until the native budget ends.
                        until = time.monotonic() + 12
                        while time.monotonic() < until:
                            waited, status = os.waitpid(worker['pid'], os.WNOHANG)
                            if waited:
                                old_exit = os.waitstatus_to_exitcode(status)
                                if takeover_case != 'reclaimed':
                                    assert old_exit == (1 if takeover_case == 'nonzero' else 0)
                                break
                            await asyncio.sleep(.05)
                        else:
                            raise AssertionError('The native 8/8 worker did not exit.')
                        with connect_closing() as db:
                            old_run = kb.get_run(db, worker['run_id'])
                        assert old_run.status == old_run.outcome == ('reclaimed' if takeover_case == 'reclaimed' else 'gave_up'), old_run
                        assert old_run.ended_at is not None
                        if takeover_case == 'reclaimed':
                            assert 'Synthetic native reclaim rejection case' in old_run.error
                        else:
                            assert old_run.error == 'Iteration budget exhausted (8/8) — task could not complete within the allowed iterations', old_run.error
                        old_execution = record()['dsh_execution']
                        assert old_execution['state'] == 'running' or takeover_case == 'reclaimed' and old_execution['state'] == 'outcome_unknown'
                        old_claim = dict(old_execution['supervisor_claim'])
                        old_ledger_path = ledger / (hashlib.sha256(old_execution['session_id'].encode()).hexdigest() + '.json')
                        old_ledger = old_ledger_path.read_bytes()
                        old_history = list(peer['journals'][old_execution['session_id']])
                        old_bindings = {key: old_execution[key] for key in ('session_id', 'generation', 'native_identity', 'baseline', 'first_input')}
                        worker = None
                        if takeover_case == 'source':
                            (repo / 'unexpected-source.txt').write_text('Synthetic out-of-band source change.\n')
                        elif takeover_case == 'sid':
                            fixture_behavior['header_id'] = 'session-foreign-fixture'
                        elif takeover_case == 'native':
                            host_processes.append(subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(120)'],
                                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL))
                            changed_receipt = json.loads(receipt.read_text())
                            changed_receipt['pid'] = host_processes[-1].pid
                            receipt.write_text(json.dumps(changed_receipt))
                        elif takeover_case == 'ledger':
                            changed_ledger = json.loads(old_ledger)
                            changed_ledger['generation'] = 'fixture-foreign-generation'
                            old_ledger_path.write_text(json.dumps(changed_ledger))
                        elif takeover_case == 'card':
                            with connect_closing() as db:
                                assert kb.edit_task(db, initial['card_id'], title='Changed synthetic card title')
                        if takeover_case != 'reclaimed':
                            assert 'error' not in json.loads(registry.dispatch('kanban_unblock', {'task_id': initial['card_id']}))
                        model.worker_first_heartbeat = takeover_case == 'reclaimed'
                        model.worker_terminal_attempt_at = 7
                        model.worker_steps = 0
                        model.worker_tool_results.clear()
                        with connect_closing() as db:
                            assert dispatch_once(db, max_spawn=1).spawned
                            worker = remember_worker(db, initial['card_id'])
                        takeover = {'previous_run_id': old_run.id, 'error': old_run.error,
                            'status': old_run.status, 'outcome': old_run.outcome, 'exit_code': old_exit,
                            'active_claim_refused': True,
                            'old_claim': old_claim, 'old_bindings': old_bindings,
                            'ledger_sha256': hashlib.sha256(old_ledger).hexdigest(), 'history': old_history}
                        supervise_first = None
                        continue
                    if supervise_takeover:
                        assert latest['dsh_execution']['supervisor_claim']['run_id'] == worker['run_id']
                        assert latest['dsh_execution']['supervisor_claim']['run_id'] != takeover['previous_run_id']
                        assert {key: latest['dsh_execution'][key] for key in takeover['old_bindings']} == takeover['old_bindings']
                        assert old_ledger_path.read_bytes() == old_ledger and peer['journals'][latest['dsh_execution']['session_id']] == old_history
                    sid = latest['dsh_execution']['session_id']
                    rows = peer['journals'][sid]
                    supervise_history_before = list(rows)
                    rows.append({'type': 'event', 'event': {'type': 'turn/start', 'seq': len(rows), 'time': 5,
                        'data': {'turn': 2}}})
                    rows.append({'type': 'event', 'event': {'type': 'turn/end', 'seq': len(rows), 'time': 5,
                        'data': {'turn': 2, 'reason': {'kind': 'error', 'error': {
                            'code': 'HOOK_ABORTED', 'message': 'Synthetic beforeRequest hook aborted.'}}}}})
                    next(row for row in peer['sessions'] if row['sessionId'] == sid)['running'] = False
                    budget_file = ledger / (hashlib.sha256(sid.encode()).hexdigest() + '.json')
                    budget_row = json.loads(budget_file.read_text())
                    supervise_ledger_before = json.loads(json.dumps(budget_row))
                    budget_row['state'].update(started_at_ms=int(time.time() * 1000) - 80000, requests=1,
                        reported_tokens=0, admissions=[peer['prompts'][0]['requestId']],
                        stop_reason='shared-work-wall-budget')
                    budget_file.write_text(json.dumps(budget_row))
                    supervise_ledger = dict(budget_row['state'])
                    supervise_terminal = {'session_id': sid, 'turn': 2, 'reason': rows[-1]['event']['data']['reason']}
                elif supervise_first is not None and stopped_results:
                    supervise_result = stopped_results[-1]
                    assert supervise_result == {'status': 'budget_stopped', 'next_action': 'kanban_block',
                        'block_kind': 'needs_input', 'delivered': False}
                    assert len(peer['prompts']) == 1
                    model.repeat_supervise = False
                    if worker is not None:
                        until = time.monotonic() + 8
                        while time.monotonic() < until:
                            waited, status = os.waitpid(worker['pid'], os.WNOHANG)
                            if waited:
                                assert os.waitstatus_to_exitcode(status) == 0
                                worker = None
                                break
                            await asyncio.sleep(.05)
                        assert worker is None, 'The bounded supervise worker did not exit after budget_stopped.'
                    latest = record()
                    assert latest['dsh_execution']['state'] == 'budget_stopped'
                    final_ledger = json.loads(budget_file.read_text())
                    assert {key: value for key, value in final_ledger.items() if key != 'state'} == {
                        key: value for key, value in supervise_ledger_before.items() if key != 'state'}
                    assert final_ledger['state'] == supervise_ledger
                    assert final_ledger['state']['admissions'] == [peer['prompts'][0]['requestId']]
                    assert rows[:len(supervise_history_before)] == supervise_history_before
                    break
            if shared_scenario == 'question_material' and peer['prompts']:
                sid = peer['prompts'][0]['sessionId']
                rows = peer['journals'][sid]
                if material_pending is not None:
                    question = next((question for question in latest['dsh_execution'].get('questions', [])
                        if question['id'] == 'message-' + str(material_pending)), None)
                    legacy_observed = (material_cases[len(material_observations)][0] != 'token_metadata_legacy'
                        or question and question.get('state') == 'open')
                    if question and question.get('notification') == 'delivered' and legacy_observed:
                        if material_cases[len(material_observations)][0] == 'missing':
                            send_count = len(sent)
                            read_count = sum(call.get('endpoint') == 'session/follow' for call in peer['streams'])
                            until = time.monotonic() + 5
                            while sum(call.get('endpoint') == 'session/follow' for call in peer['streams']) < read_count + 6:
                                assert time.monotonic() < until, 'The original observer did not revisit the same round.'
                                await asyncio.sleep(.05)
                            assert len(sent) == send_count, 'Unreadable material was repeatedly re-published.'
                            assert not record()['dsh_execution']['questions'][-1].get('notification_history')
                        delivered = next(message for index, message in enumerate(sent, 1)
                            if 'om_sent_' + str(index) == question['delivery']['message_id'])
                        if material_cases[len(material_observations)][0] == 'token_metadata_legacy':
                            assert question['answerable'] is True, 'The original misclassified open round must become readable.'
                            assert question['delivery']['message_id'] != material_legacy_delivery
                            assert question['notification_history'][0]['delivery']['message_id'] == material_legacy_delivery
                            before_reply = len(peer['prompts'])
                            await receive(adapter, raw('om_old_private_answer', '@_user_1 answer：Stale notice answer',
                                parent_id=material_legacy_delivery))
                            assert len(peer['prompts']) == before_reply
                            assert not record()['dsh_execution']['questions'][-1]['answers']
                        material_observations.append((question, delivered.request_body.content))
                        material_pending = None
                if material_pending is None and len(material_observations) < len(material_cases):
                    label, reference_text, _ = material_cases[len(material_observations)]
                    material_pending = len(rows)
                    if label == 'token_metadata_legacy':
                        previous = record()
                        notice = await adapter.send(previous['source_anchor']['chat_id'], 'Historical private planning notice.',
                            reply_to=previous['source_anchor']['message_id'])
                        assert notice.success
                        material_legacy_delivery = notice.message_id
                        original_questions = [{'id': 'answer', 'question': '请确认方案 ' + reference_text + '？'}]
                        previous['dsh_execution']['questions'].append({'id': 'message-' + str(material_pending),
                            'kind': 'natural', 'state': 'expired', 'answerable': False, 'source_seq': material_pending,
                            'event_id': None, 'generation': previous['dsh_execution']['generation'], 'session_id': sid,
                            'source_digest': hashlib.sha256(json.dumps(original_questions, sort_keys=True, ensure_ascii=False).encode()).hexdigest(),
                            'answers': {}, 'reply_messages': [], 'notification': 'delivered',
                            'delivery': {**{key: previous['source_anchor'][key] for key in ('app_id','transport_tenant_key',
                                'recipient_tenant_key','recipient_open_id','chat_id')}, 'message_id': material_legacy_delivery}})
                        adapter.intake._save_execution(previous, fields=['questions'])
                    rows.append({'type': 'event', 'event': {'type': 'assistant/message', 'seq': len(rows),
                        'time': 5 + len(material_observations), 'data': {'turn': 1, 'step': len(material_observations),
                            'message': {'id': 'fixture-plan-' + label, 'role': 'assistant',
                                'content': [{'type': 'text', 'text': '请确认方案 ' + reference_text + '？'}]}}}})
                elif material_pending is None and not ended:
                    rows.append({'type': 'event', 'event': {'type': 'turn/end', 'seq': len(rows), 'time': 20,
                        'data': {'turn': 1, 'reason': {'kind': 'error', 'error': {'code': 'UNKNOWN', 'message': 'Synthetic material round ended.'}}}}})
                    next(row for row in peer['sessions'] if row['sessionId'] == sid)['running'] = False
                    ended = True
            if shared_scenario == 'stop' and latest['dsh_execution'].get('first_input') == 'accepted' and not stop_sent:
                stop_sent = True
                await receive(adapter, raw('om_stop', '@_user_1 停止 ' + latest['id']))
                latest = record()
            card = json.loads(registry.dispatch('kanban_show', {'task_id': initial['card_id']}))['task']
            if qa_preinput_retry and ended and card['status'] in {'blocked', 'triage'}:
                execution = latest['dsh_execution']
                assert execution['state'] == 'awaiting_acceptance', execution
                assert execution['acceptance']['status'] == 'unmet'
                assert 'delivery_report_missing' not in execution['acceptance']['unmet']
                until = time.monotonic() + 10
                while time.monotonic() < until:
                    waited, status = os.waitpid(worker['pid'], os.WNOHANG)
                    if waited:
                        assert os.waitstatus_to_exitcode(status) == 0
                        worker = None
                        break
                    await asyncio.sleep(.05)
                assert worker is None, 'The original QA delivery worker did not exit.'
                legacy = record()
                for field in ('eligible_test_calls', 'test_files'):
                    legacy['dsh_execution']['acceptance'].pop(field, None)
                adapter.intake._save_execution(legacy, fields=['acceptance'])
                if qa_request_only_resume:
                    prior_qa = record()
                    prior_qa['dsh_execution'].update(renewal_window_adoption='accepted',
                        renewal_window_checkpoint={'status': 'admitted', 'qa_rework': True,
                            'sealed_unknown_usage': prior_qa_seal,
                            'previous_state': budget_row['renewWindow']['original_state'],
                            'previous_renew_window': prior_qa_window, 'cursor': rows[-1]['event']['seq']},
                        continuation_request_id='qa-fixture-previous',
                        continuation_control={'kind': 'qa_rework', 'status': 'accepted', 'dispatch_status': 'accepted',
                            'message_id': 'om_prior_qa_completion'})
                    adapter.intake._save_execution(prior_qa, fields=['renewal_window_adoption',
                        'renewal_window_checkpoint', 'continuation_request_id', 'continuation_control'])
                qa_results = {}
                qa_events = []
                import faulthandler
                qa_trace = (scratch / 'qa-tool-stacks.log').open('w')

                async def qa_tool_handler(event):
                    from inspect import isawaitable
                    assert adapter._command_source.get() is event.source
                    assert adapter.intake.prepare(event, adapter) is None
                    qa_events.append(event.message_id)
                    print('natural QA tool entered: ' + event.message_id, file=qa_trace, flush=True)
                    faulthandler.dump_traceback_later(12, file=qa_trace)
                    answer = await asyncio.to_thread(registry.dispatch,
                        'hermes_pm_repository_rework', {'work_id': initial['id']})
                    if isawaitable(answer):
                        answer = await answer
                    faulthandler.cancel_dump_traceback_later()
                    qa_results[event.message_id] = [json.loads(answer)]
                    print('natural QA tool returned: ' + event.message_id + ': ' + answer,
                        file=qa_trace, flush=True)
                    if event.message_id == 'om_qa_retry':
                        duplicate = await asyncio.to_thread(registry.dispatch,
                            'hermes_pm_repository_rework', {'work_id': initial['id']})
                        if isawaitable(duplicate):
                            duplicate = await duplicate
                        qa_results[event.message_id].append(json.loads(duplicate))

                runner._wire_adapter_handlers(adapter, message_handler=qa_tool_handler)

                async def qa_message(message_id, text, parent_id=None):
                    await receive(adapter, raw(message_id, '@_user_1 ' + text,
                        **({'parent_id': parent_id} if parent_id else {})))
                    until = time.monotonic() + 8
                    while time.monotonic() < until:
                        if message_id in qa_results:
                            return qa_results[message_id][0]
                        await asyncio.sleep(.01)
                    raise AssertionError('The real natural QA tool did not settle: ' + message_id)

                feedback = ('取消30分钟截止，保留80次请求\n' if qa_request_only_resume else '') \
                    + 'Repair only the missing synthetic browser QA receipt and run the actual test.'
                proposal = await qa_message('om_qa_feedback', feedback)
                assert proposal['status'] == 'renewal_required', proposal
                if qa_request_only_resume:
                    assert proposal['proposal']['limits'] == {'max_model_requests': 79}, proposal
                    assert '取消' in proposal['proposal']['text'] and '79' in proposal['proposal']['text']
                control = record()['dsh_execution']['natural_qa_rework_control']
                assert control['proposal_delivery']['status'] == 'delivered'
                approval = await qa_message('om_qa_approve', '同意', control['proposal_delivery']['message_id'])
                assert approval['status'] == 'maintenance_required', approval
                control = record()['dsh_execution']['natural_qa_rework_control']
                assert control['proposal_delivery']['status'] == 'delivered'
                sid = execution['session_id']
                history_before = json.loads(json.dumps(peer['journals'][sid]))
                old_ledger_bytes = budget_file.read_bytes()
                host_processes[0].terminate()
                assert host_processes[0].wait(timeout=5) == -15
                host_processes.append(subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(120)'],
                    stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL))
                updated = json.loads(receipt.read_text())
                updated.update(lease='fixture-qa-maintained-lease', pid=host_processes[-1].pid)
                receipt.write_text(json.dumps(updated))
                admission = await qa_message('om_qa_maintenance', '维护完成', control['proposal_delivery']['message_id'])
                assert admission['status'] == 'accepted' and admission['execution_started'] is False, admission
                admitted = record()['dsh_execution']
                admitted_control = json.loads(json.dumps(admitted['continuation_control']))
                request_id = admitted['continuation_request_id']
                assert admitted_control['kind'] == 'qa_rework' and admitted_control['status'] == 'admitted'
                assert admitted_control['dispatch_status'] == 'accepted'
                assert admitted_control['qa_feedback'] == feedback
                admitted_ledger_bytes = budget_file.read_bytes()
                admitted_ledger = json.loads(admitted_ledger_bytes)
                assert admitted_ledger['renewWindow']['request_base'] == admitted_ledger['state']['requests'] == (
                    5 if qa_request_only_resume else 55)
                if qa_request_only_resume:
                    assert admitted_ledger['budget'] == {'max_model_requests': 79}
                    assert json.loads(reference.read_text())['budget'] == {'max_model_requests': 79}
                    assert json.loads(old_ledger_bytes)['budget'] == {'max_wall_seconds': 1800,
                        'max_model_requests': 80, 'max_reported_tokens': 300000, 'max_output_tokens_per_request': 8192}
                assert admitted_ledger['renewWindow']['started_at_ms'] is None
                assert admitted_ledger['renewWindow']['hook_marker'] is None
                assert len(peer['prompts']) == 1 and peer['journals'][sid] == history_before
                checkpoint = admitted['renewal_window_checkpoint']
                if qa_request_only_resume:
                    assert checkpoint['sealed_unknown_usage'] == admitted_ledger['renewWindow']['sealed_unknown_usage'] == prior_qa_seal
                    assert admitted['renewal_window_checkpoint_history'][-1]['checkpoint']['sealed_unknown_usage'] == prior_qa_seal
                assert Path(checkpoint['ledger_backup']).read_bytes() == old_ledger_bytes
                # Model a departed original gateway with synthetic persisted identity.
                departed = record()
                departed['gateway_controller'] = dict(departed['gateway_controller'], pid=999999,
                    start_sec=1, start_usec=0)
                adapter.intake._save(departed)
                # A failed external response ends this worker before any continuation input.
                fixture_behavior['reject'] = 'workspace/create'
                model.worker_steps = 0
                with connect_closing() as db:
                    assert dispatch_once(db, max_spawn=1).spawned
                    worker = remember_worker(db, initial['card_id'])
                failed_run_id = worker['run_id']
                until = time.monotonic() + 12
                while time.monotonic() < until:
                    waited, status = os.waitpid(worker['pid'], os.WNOHANG)
                    if waited:
                        assert os.waitstatus_to_exitcode(status) == 0
                        worker = None
                        break
                    await asyncio.sleep(.05)
                assert worker is None, 'The pre-input QA failure worker did not exit.'
                failed = record()['dsh_execution']
                assert failed['state'] == 'outcome_unknown', failed
                failure = failed['last_failure']
                assert failure == {'phase': 'session_registration', 'code': 'outcome_unknown',
                    'type': 'ManagementError', 'message': 'Original owned execution response is unconfirmed.'}, failure
                assert failed['continuation_control']['status'] == 'admitted'
                assert len(peer['prompts']) == 1 and peer['journals'][sid] == history_before
                assert budget_file.read_bytes() == admitted_ledger_bytes
                fixture_behavior.pop('reject')
                retry = await qa_message('om_qa_retry', '继续原来的 QA 修复，不增加窗口。')
                assert retry['status'] == 'accepted' and retry['execution_started'] is False, retry
                assert qa_results['om_qa_retry'] == [retry, retry], qa_results
                retried = record()['dsh_execution']
                assert retried['continuation_request_id'] == request_id
                assert retried['qa_preinput_retry']['previous_failure'] == failure
                assert retried['continuation_control']['previous_claim']['run_id'] == failed_run_id
                assert retried['continuation_control']['qa_feedback'] == feedback
                assert budget_file.read_bytes() == admitted_ledger_bytes and len(peer['prompts']) == 1
                blocked_duplicate = await qa_message('om_qa_duplicate_new_message', '继续原来的 QA 修复，不增加窗口。')
                assert blocked_duplicate == {'status': 'unassociated', 'code': 'no_matching_work'}, blocked_duplicate
                assert budget_file.read_bytes() == admitted_ledger_bytes and len(peer['prompts']) == 1
                model.worker_steps = 0
                with connect_closing() as db:
                    assert dispatch_once(db, max_spawn=1).spawned
                    worker = remember_worker(db, initial['card_id'])
                until = time.monotonic() + 20
                while time.monotonic() < until:
                    current = record()['dsh_execution']
                    if len(peer['prompts']) == 2 and current['continuation_control']['status'] == 'accepted':
                        break
                    await asyncio.sleep(.05)
                else:
                    raise AssertionError('The real QA retry worker did not admit the original prompt: ' + json.dumps(current))
                assert current['shared_event_observer']['status'] == 'ready', current
                assert current['supervisor_claim']['run_id'] == worker['run_id'] != failed_run_id
                assert peer['prompts'][1]['sessionId'] == peer['prompts'][0]['sessionId'] == sid
                assert peer['prompts'][1]['requestId'] == request_id
                assert feedback in peer['prompts'][1]['content'][0]['text']
                shared_module = importlib.import_module(package + '.shared_repository')
                execution_module = importlib.import_module(package + '.repository_execution')
                actual_configuration, _ = execution_module.execution_configuration(initial['target'])
                canonical = shared_module.verification_command(actual_configuration, current, str(repo), ['qa.test.mjs'])
                assert 'Complete canonical trusted test command for the declared test files: ' + canonical in peer['prompts'][1]['content'][0]['text']
                assert '"eligible_test_calls": []' in peer['prompts'][1]['content'][0]['text']
                if qa_request_only_resume:
                    actual_prompt = peer['prompts'][1]['content'][0]['text']
                    assert 'no plugin wall deadline' in actual_prompt and '79 model requests remain' in actual_prompt
                    assert 'historical' in actual_prompt and '300000 reported tokens' in actual_prompt
                assert '不增加窗口' not in peer['prompts'][1]['content'][0]['text']
                assert budget_file.read_bytes() == admitted_ledger_bytes
                assert peer['journals'][sid][:len(history_before)] == history_before
                after_input_duplicate = await qa_message('om_qa_duplicate_after_input', '继续原来的 QA 修复。')
                assert after_input_duplicate == {'status': 'unassociated', 'code': 'no_matching_work'}, after_input_duplicate
                assert len(peer['prompts']) == 2 and budget_file.read_bytes() == admitted_ledger_bytes
                rows = peer['journals'][sid]
                rows.append({'type': 'event', 'event': {'type': 'turn/end', 'seq': len(rows), 'time': 8,
                    'data': {'turn': 3, 'reason': {'kind': 'completed'}}}})
                next(row for row in peer['sessions'] if row['sessionId'] == sid)['running'] = False
                until = time.monotonic() + 12
                while time.monotonic() < until:
                    waited, status = os.waitpid(worker['pid'], os.WNOHANG)
                    if waited:
                        assert os.waitstatus_to_exitcode(status) == 0
                        worker = None
                        break
                    await asyncio.sleep(.05)
                assert worker is None, 'The requeued original QA worker did not exit.'
                latest = record()
                qa_retry_evidence = {'failure': failure, 'failed_run_id': failed_run_id,
                    'new_run_id': latest['dsh_execution']['supervisor_claim']['run_id'],
                    'request_id': request_id, 'observer': current['shared_event_observer'],
                    'tool_results': qa_results, 'events': qa_events, 'worker_exit': 0,
                    'ledger_sha256': hashlib.sha256(admitted_ledger_bytes).hexdigest(),
                    'window': admitted_ledger['renewWindow']}
                break
            if shared_scenario == 'question_material' and ended and card['status'] in {'blocked', 'triage'}:
                for (label, _, answerable), (question, notification) in zip(material_cases, material_observations, strict=True):
                    assert question['answerable'] is answerable, {'case': label, 'answerable': question['answerable'], 'notification': notification}
                    if answerable:
                        assert plan.strip() in notification, {'case': label, 'notification': notification}
                    else:
                        assert '原生 DSH 私有界面' in notification
                    assert 'fixture-sensitive-value' not in notification and 'Outside fixture material' not in notification
                assert len(peer['prompts']) == 1, 'Question material notifications do not submit new DSH input.'
                break
            if renew_window_again and ended and not renewal_admitted and card['status'] in {'blocked', 'triage'}:
                assert latest['dsh_execution']['state'] == 'awaiting_acceptance', latest['dsh_execution']
                assert latest['dsh_execution']['acceptance']['status'] == 'unmet'
                assert 'delivery_report_missing' in latest['dsh_execution']['acceptance']['unmet']
                while True:
                    waited, status = os.waitpid(worker['pid'], os.WNOHANG)
                    if waited:
                        assert os.waitstatus_to_exitcode(status) == 0
                        break
                    await asyncio.sleep(.05)
                worker = None
                claim_run_id = latest['dsh_execution']['supervisor_claim']['run_id']
                assert 'error' not in json.loads(registry.dispatch('kanban_unblock', {'task_id': initial['card_id']}))
                with connect_closing() as db:
                    assert dispatch_once(db, max_spawn=1).spawned
                    later_worker = remember_worker(db, initial['card_id'])
                await receive(adapter, raw('om_window_again_later_active', '@_user_1 续预算 ' + latest['id']))
                assert 'renewal_window_checkpoint' not in record()['dsh_execution'] and len(peer['prompts']) == 1
                assert 'error' not in json.loads(registry.dispatch('kanban_block', {
                    'task_id': initial['card_id'], 'kind': 'needs_input', 'reason': 'Synthetic second worker terminal outcome.'}))
                while True:
                    waited, status = os.waitpid(later_worker['pid'], os.WNOHANG)
                    if waited:
                        assert os.waitstatus_to_exitcode(status) == 0
                        break
                    await asyncio.sleep(.05)
                with connect_closing() as db:
                    later_run = kb.latest_run(db, initial['card_id'])
                    later_card = kb.get_task(db, initial['card_id'])
                assert later_run.id != claim_run_id and later_run.status in {'done', 'blocked', 'crashed', 'timed_out', 'failed', 'released'} and later_run.ended_at is not None
                assert later_card.status in {'blocked', 'triage'} and later_card.current_run_id is None and later_card.worker_pid is None
                latest = record()
                budget_file = ledger / (hashlib.sha256(latest['dsh_execution']['session_id'].encode()).hexdigest() + '.json')
                budget_before = budget_file.read_bytes()
                await receive(adapter, raw('om_window_again_continue_expired', '@_user_1 继续 ' + latest['id']))
                assert budget_file.read_bytes() == budget_before and len(peer['prompts']) == 1
                unexpired = json.loads(budget_before)
                unexpired['renewWindow']['started_at_ms'] = int(time.time() * 1000)
                budget_file.write_text(json.dumps(unexpired))
                await receive(adapter, raw('om_window_again_unexpired', '@_user_1 续预算 ' + latest['id']))
                assert json.loads(budget_file.read_text()) == unexpired and len(peer['prompts']) == 1
                budget_file.write_bytes(budget_before)
                receipt_path = ledger / (hashlib.sha256(latest['dsh_execution']['session_id'].encode()).hexdigest() + '.tools.jsonl')
                receipts = [json.loads(row) for row in receipt_path.read_text().splitlines()]
                receipts_before = receipt_path.read_bytes()
                receipts[1]['exit_code'] = '1'
                receipt_path.write_text(''.join(json.dumps(row) + '\n' for row in receipts))
                receipt_path.chmod(0o600)
                await receive(adapter, raw('om_window_again_nonint_exit', '@_user_1 续预算 ' + latest['id']))
                assert budget_file.read_bytes() == budget_before and len(peer['prompts']) == 1
                receipt_path.write_bytes(receipts_before)
                receipts = [json.loads(row) for row in receipt_path.read_text().splitlines()]
                receipts[1]['native_session_seq'] = receipts[0]['native_session_seq']
                receipt_path.write_text(''.join(json.dumps(row) + '\n' for row in receipts))
                receipt_path.chmod(0o600)
                await receive(adapter, raw('om_window_again_duplicate_seq', '@_user_1 续预算 ' + latest['id']))
                assert budget_file.read_bytes() == budget_before and len(peer['prompts']) == 1
                receipt_path.write_bytes(receipts_before)
                receipts = [json.loads(row) for row in receipt_path.read_text().splitlines()]
                receipts[2]['native_session_seq'] = receipts[0]['native_session_seq'] - 1
                receipt_path.write_text(''.join(json.dumps(row) + '\n' for row in receipts))
                receipt_path.chmod(0o600)
                await receive(adapter, raw('om_window_again_descending_seq', '@_user_1 续预算 ' + latest['id']))
                assert budget_file.read_bytes() == budget_before and len(peer['prompts']) == 1
                receipt_path.write_bytes(receipts_before)
                receipts = [json.loads(row) for row in receipt_path.read_text().splitlines()]
                receipts[1]['source_before'] = {**receipts[1]['source_before'], 'source_digest': 'f' * 64}
                receipt_path.write_text(''.join(json.dumps(row) + '\n' for row in receipts))
                receipt_path.chmod(0o600)
                await receive(adapter, raw('om_window_again_broken_chain', '@_user_1 续预算 ' + latest['id']))
                assert budget_file.read_bytes() == budget_before and len(peer['prompts']) == 1
                receipt_path.write_bytes(receipts_before)
                drift = repo / 'synthetic-source-drift'
                drift.write_text('Synthetic source drift.\n')
                await receive(adapter, raw('om_window_again_current_drift', '@_user_1 续预算 ' + latest['id']))
                assert budget_file.read_bytes() == budget_before and len(peer['prompts']) == 1
                drift.unlink()
                receipts = [json.loads(row) for row in receipt_path.read_text().splitlines()]
                journal = peer['journals'][latest['dsh_execution']['session_id']]
                rejected_result = next(row['event']['data']['message'] for row in journal
                    if row['event']['type'] == 'tool/result' and row['event']['data']['message'].get('toolCallId') == 'fixture-rejected-bash-0')
                original_text = rejected_result['content'][0]['text']
                for label, text in [('missing_period', 'Error: Shared work tool ownership is unknown'),
                                    ('other_error', 'Error: Shared work ownership failed.')]:
                    rejected_result['content'][0]['text'] = text
                    await receive(adapter, raw('om_window_again_' + label, '@_user_1 续预算 ' + latest['id']))
                    assert budget_file.read_bytes() == budget_before and len(peer['prompts']) == 1
                rejected_result['content'][0]['text'] = original_text
                rejected_call = next(row['event']['data'] for row in journal
                    if row['event']['type'] == 'tool/call' and row['event']['data'].get('callId') == 'fixture-rejected-bash-0')
                rejected_call['name'] = 'skill'
                await receive(adapter, raw('om_window_again_wrong_tool', '@_user_1 续预算 ' + latest['id']))
                assert budget_file.read_bytes() == budget_before and len(peer['prompts']) == 1
                rejected_call['name'] = 'bash'
                receipts[3]['execution_admission'] = 'admitted'
                receipt_path.write_text(''.join(json.dumps(row) + '\n' for row in receipts))
                receipt_path.chmod(0o600)
                await receive(adapter, raw('om_window_again_admitted_bash', '@_user_1 续预算 ' + latest['id']))
                assert budget_file.read_bytes() == budget_before and len(peer['prompts']) == 1
                receipt_path.write_bytes(receipts_before)
                fixture_behavior['jobs'] = [{'id': 'fixture-running-job', 'owner': latest['dsh_execution']['session_id'], 'status': 'running'}]
                await receive(adapter, raw('om_window_again_active_job', '@_user_1 续预算 ' + latest['id']))
                assert budget_file.read_bytes() == budget_before and len(peer['prompts']) == 1
                fixture_behavior.pop('jobs')
                await receive(adapter, raw('om_window_again_prepare', '@_user_1 续预算 ' + latest['id']))
                latest = record()
                checkpoint = latest['dsh_execution'].get('renewal_window_checkpoint')
                assert checkpoint and checkpoint['status'] == 'prepared', latest['dsh_execution']
                assert budget_file.read_bytes() == budget_before and len(peer['prompts']) == 1
                assert Path(checkpoint['ledger_backup']).read_bytes() == budget_before
                assert checkpoint['previous_renew_window'] == json.loads(budget_before)['renewWindow']
                await receive(adapter, raw('om_window_again_prepare', '@_user_1 续预算 ' + latest['id']))
                assert budget_file.read_bytes() == budget_before and len(peer['prompts']) == 1
                host_processes[0].terminate()
                assert host_processes[0].wait(timeout=5) == -15
                host_processes.append(subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(120)'],
                    stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL))
                updated = json.loads(receipt.read_text())
                updated.update(lease='fixture-renewed-again-lease', pid=host_processes[-1].pid)
                receipt.write_text(json.dumps(updated))
                renewed_scope = json.loads(reference.read_text())
                renewed_scope['host_lease'] = updated['lease']
                reference.write_text(json.dumps(renewed_scope))
                rows = peer['journals'][latest['dsh_execution']['session_id']]
                rows.append({'type': 'event', 'event': {'type': 'session/end-seed', 'seq': len(rows), 'time': 5, 'data': {}}})
                fixture_behavior['header_defaults'] = {'delegationDepth': 0}
                next(row for row in peer['sessions'] if row['sessionId'] == latest['dsh_execution']['session_id'])['agentAvailable'] = False
                await receive(adapter, raw('om_window_again_admit', '@_user_1 续预算 ' + latest['id']))
                latest = record()
                control = latest['dsh_execution']['continuation_control']
                assert control['kind'] == 'renew_window' and control['status'] == 'admitted', latest['dsh_execution']
                granted = json.loads(budget_file.read_text())
                old = json.loads(budget_before)
                assert granted['renewWindow']['request_base'] == old['state']['requests']
                assert granted['renewWindow']['original_state'] == old['state']
                assert granted['renewWindow']['started_at_ms'] is None
                assert granted['renewWindow']['host_lease'] == granted['host_lease'] == updated['lease']
                assert granted['renewWindow']['original_host_lease'] == latest['dsh_execution']['renewal_window_history'][-1]['renew_window']['host_lease']
                assert {key: value for key, value in granted['state'].items() if key != 'binding_sha256'} == {
                    key: value for key, value in old['state'].items() if key != 'binding_sha256'}
                await receive(adapter, raw('om_window_again_admit', '@_user_1 续预算 ' + latest['id']))
                assert len(peer['prompts']) == 1
                model.worker_steps = 0
                with connect_closing() as db:
                    assert dispatch_once(db, max_spawn=1).spawned
                    worker = remember_worker(db, initial['card_id'])
                renewal_admitted = True
                continue
            if renew_window_again and renewal_admitted and len(peer['prompts']) == 2 and not renewal_ended:
                latest = record()
                budget = json.loads(budget_file.read_text())
                assert budget['renewWindow']['started_at_ms'] is not None
                assert budget['renewWindow']['request_base'] == old['state']['requests']
                assert budget['state']['requests'] == old['state']['requests'] + 1
                assert budget['renewWindow']['hook_marker'] == 'same-sid-window-v1'
                sid = latest['dsh_execution']['session_id']
                rows = peer['journals'][sid]
                rows.append({'type': 'event', 'event': {'type': 'assistant/message', 'seq': len(rows), 'time': 6,
                    'data': {'turn': 2, 'step': 0, 'usage': {'inputTokens': 10, 'outputTokens': 5},
                        'message': {'id': 'fixture-repeat-settlement', 'role': 'assistant',
                            'content': [{'type': 'text', 'text': 'Synthetic renewed-window progress.'}]}, 'stream': []}}})
                call_id = 'fixture-repeat-bash-rejection'
                rows.append({'type': 'event', 'event': {'type': 'tool/call', 'seq': len(rows), 'time': 6,
                    'data': {'turn': 2, 'step': 0, 'callId': call_id, 'name': 'bash', 'arguments': {'command': 'fixture bounded work'}}}})
                rows.append({'type': 'event', 'event': {'type': 'tool/result', 'seq': len(rows), 'time': 6,
                    'data': {'turn': 2, 'step': 0, 'message': {'toolCallId': call_id, 'isError': True,
                        'content': [{'type': 'text', 'text': 'Synthetic ordinary bash rejection.'}]}}}})
                receipt_path = ledger / (hashlib.sha256(sid.encode()).hexdigest() + '.tools.jsonl')
                with receipt_path.open('a') as output:
                    output.write(json.dumps({'schema': 1, 'generation': budget['generation'], 'session_id': sid,
                        'session_created_at': budget['session_created_at'], 'service_ref': 'fixture-native',
                        'host_lease': budget['host_lease'], 'call_id': call_id, 'name': 'bash', 'is_error': True,
                        'execution_admission': 'rejected'}) + '\n')
                fixture_behavior['jobs'] = [{'id': 'fixture-repeat-terminal-job', 'owner': sid, 'status': 'killed'}]
                budget['renewWindow'].update(started_at_ms=int(time.time() * 1000) - 80000,
                    stop_reason='shared-work-wall-budget')
                budget_file.write_text(json.dumps(budget))
                rows.append({'type': 'event', 'event': {'type': 'turn/end', 'seq': len(rows), 'time': 6,
                    'data': {'turn': 2, 'reason': {'kind': 'aborted',
                        'reason': {'kind': 'hook', 'reason': 'shared-work-wall-budget'}}}}})
                next(row for row in peer['sessions'] if row['sessionId'] == sid)['running'] = False
                renewal_ended = True
            if renew_window_again and renewal_ended and card['status'] in {'blocked', 'triage'}:
                assert latest['dsh_execution']['state'] == 'budget_stopped', latest['dsh_execution']
                while True:
                    waited, status = os.waitpid(worker['pid'], os.WNOHANG)
                    if waited:
                        assert os.waitstatus_to_exitcode(status) == 0
                        break
                    await asyncio.sleep(.05)
                worker = None
                assert len(peer['prompts']) == 2
                prior_execution = json.loads(json.dumps(record()['dsh_execution']))
                repeat_budget_before = budget_file.read_bytes()
                repeat_history_before = json.loads(json.dumps(peer['journals'][sid]))
                await receive(adapter, raw('om_repeat_window_prepare', '@_user_1 续预算 ' + latest['id']))
                latest = record()
                repeat_checkpoint = latest['dsh_execution'].get('renewal_window_checkpoint', {})
                assert repeat_checkpoint.get('status') == 'prepared' and repeat_checkpoint.get('message_id') == 'om_repeat_window_prepare', {
                    'checkpoint': repeat_checkpoint, 'messages': [m.request_body.content for m in sent if m.message_id == 'om_repeat_window_prepare']}
                assert budget_file.read_bytes() == repeat_budget_before and len(peer['prompts']) == 2
                assert peer['journals'][sid] == repeat_history_before
                repeat_window_evidence = {'phase': 'prepared', 'prompt_count': len(peer['prompts']),
                    'ledger_sha256': hashlib.sha256(repeat_budget_before).hexdigest(),
                    'requests': json.loads(repeat_budget_before)['state']['requests'],
                    'checkpoint': repeat_checkpoint, 'prior_checkpoint': prior_execution['renewal_window_checkpoint'],
                    'prior_adoption': prior_execution.get('renewal_window_adoption')}
                archived = latest['dsh_execution']['renewal_window_checkpoint_history'][-1]
                assert archived == {'checkpoint': prior_execution['renewal_window_checkpoint'],
                    'adoption': prior_execution['renewal_window_adoption']}
                assert latest['dsh_execution']['renewal_window_history'] == prior_execution['renewal_window_history']
                assert latest['dsh_execution']['continuation_history'] == prior_execution['continuation_history']
                assert latest['dsh_execution']['renewal_window_adoption'] is None
                assert Path(repeat_checkpoint['ledger_backup']).read_bytes() == repeat_budget_before
                await receive(adapter, raw('om_repeat_window_still_alive', '@_user_1 续预算 ' + latest['id']))
                assert budget_file.read_bytes() == repeat_budget_before and len(peer['prompts']) == 2
                assert record()['dsh_execution']['renewal_window_checkpoint']['status'] == 'prepared'
                host_processes[-1].terminate()
                assert host_processes[-1].wait(timeout=5) == -15
                host_processes.append(subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(120)'],
                    stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL))
                third_receipt = json.loads(receipt.read_text())
                third_receipt.update(lease='fixture-third-window-lease', pid=host_processes[-1].pid)
                receipt.write_text(json.dumps(third_receipt))
                third_scope = json.loads(reference.read_text())
                third_scope['host_lease'] = third_receipt['lease']
                reference.write_text(json.dumps(third_scope))
                rows.append({'type': 'event', 'event': {'type': 'session/end-seed', 'seq': len(rows), 'time': 7, 'data': {}}})
                fixture_behavior['header_defaults'] = {'delegationDepth': 0}
                next(row for row in peer['sessions'] if row['sessionId'] == sid)['agentAvailable'] = False
                fixture_behavior['jobs'] = []
                await receive(adapter, raw('om_repeat_window_admit', '@_user_1 续预算 ' + latest['id']))
                latest = record()
                third_budget = json.loads(budget_file.read_bytes())
                previous_budget = json.loads(repeat_budget_before)
                assert latest['dsh_execution']['renewal_window_checkpoint']['status'] == 'admitted', latest['dsh_execution']
                assert latest['dsh_execution']['renewal_window_adoption'] == 'accepted'
                assert latest['dsh_execution']['continuation_control']['status'] == 'admitted'
                assert third_budget['renewWindow']['request_base'] == previous_budget['state']['requests']
                assert third_budget['renewWindow']['original_state'] == previous_budget['state']
                assert third_budget['renewWindow']['started_at_ms'] is None
                assert third_budget['renewWindow']['hook_marker'] is None and third_budget['renewWindow']['stop_reason'] is None
                assert {key: value for key, value in third_budget['state'].items() if key != 'binding_sha256'} == {
                    key: value for key, value in previous_budget['state'].items() if key != 'binding_sha256'}
                assert latest['dsh_execution']['renewal_window_checkpoint_history'][-1] == archived
                assert latest['dsh_execution']['continuation_history'][:len(prior_execution['continuation_history'])] == prior_execution['continuation_history']
                assert latest['dsh_execution']['renewal_window_history'][:len(prior_execution['renewal_window_history'])] == prior_execution['renewal_window_history']
                assert len(peer['prompts']) == 2 and peer['prompts'][0]['sessionId'] == peer['prompts'][1]['sessionId'] == sid
                assert peer['journals'][sid][:len(repeat_history_before)] == repeat_history_before
                assert Path(repeat_checkpoint['ledger_backup']).read_bytes() == repeat_budget_before
                repeat_window_evidence.update(phase='admitted', request_base=third_budget['renewWindow']['request_base'],
                    cumulative_state_before=previous_budget['state'], window_after=third_budget['renewWindow'],
                    checkpoint_history=latest['dsh_execution']['renewal_window_checkpoint_history'],
                    adoption=latest['dsh_execution']['renewal_window_adoption'], prompt_count=len(peer['prompts']))
                if owner_context:
                    model.worker_steps = 0
                    with connect_closing() as db:
                        assert dispatch_once(db, max_spawn=1).spawned
                        worker = remember_worker(db, latest['card_id'])
                    until = time.monotonic() + 20
                    while time.monotonic() < until:
                        waited, status = os.waitpid(worker['pid'], os.WNOHANG)
                        if waited:
                            assert os.waitstatus_to_exitcode(status) == 0
                            worker = None
                            break
                        await asyncio.sleep(.05)
                    assert worker is None
                    latest = record()
                    assert latest['dsh_execution']['state'] == 'outcome_unknown', latest['dsh_execution']
                    assert len(peer['prompts']) == 3 and budget_file.read_bytes() == json.dumps(third_budget).encode()
                    preserved = {key: latest['dsh_execution'][key] for key in (
                        'last_failure', 'continuation_control', 'renewal_window_history', 'continuation_history')}
                    creates = sum(call['method'] == 'session/create' for call in peer['calls'])
                    for case in ('started_at_ms', 'hook_marker', 'stop_reason', 'request_base'):
                        altered = json.loads(budget_file.read_bytes())
                        altered['renewWindow'][case] = 1 if case != 'hook_marker' else 'same-sid-window-v1'
                        budget_file.write_text(json.dumps(altered))
                        await receive(adapter, raw('om_owner_ledger_' + case, '@_user_1 补充 ' + latest['id'] + '\nVerify synthetic scope.'))
                        assert len(peer['prompts']) == 3 and not record()['dsh_execution'].get('owner_context_control')
                        budget_file.write_text(json.dumps(third_budget))
                    failed_events = json.loads(json.dumps(peer['journals'][sid]))
                    for case in ('wrong_error', 'wrong_request', 'assistant/message', 'request/header', 'tool/call'):
                        changed = json.loads(json.dumps(failed_events))
                        if case == 'wrong_error':
                            changed[-1]['event']['data']['reason']['error']['message'] += '!'
                        elif case == 'wrong_request':
                            changed[-6]['event']['data']['inserted'][0]['source']['rpcId'] = 'foreign-request'
                        else:
                            changed.append({'type': 'event', 'event': {'type': case, 'seq': len(changed), 'time': 11,
                                'data': {'turn': 3, 'step': 1, 'usage': {'inputTokens': 1, 'outputTokens': 1}}}})
                        peer['journals'][sid] = changed
                        await receive(adapter, raw('om_owner_events_' + case, '@_user_1 补充 ' + latest['id'] + '\nVerify synthetic scope.'))
                        assert len(peer['prompts']) == 3 and not record()['dsh_execution'].get('owner_context_control')
                        peer['journals'][sid] = json.loads(json.dumps(failed_events))
                    for case in ('running', 'unknown'):
                        fixture_behavior['jobs'] = [{'id': 'fixture-owner-job', 'owner': sid, 'status': case}]
                        await receive(adapter, raw('om_owner_job_' + case, '@_user_1 补充 ' + latest['id'] + '\nVerify synthetic scope.'))
                        assert len(peer['prompts']) == 3 and not record()['dsh_execution'].get('owner_context_control')
                    fixture_behavior['jobs'] = []
                    fixture_behavior['inbox'] = {'next-turn': [{'id': 'fixture-other-input', 'role': 'user'}], 'next-step': []}
                    await receive(adapter, raw('om_owner_pending', '@_user_1 补充 ' + latest['id'] + '\nVerify synthetic scope.'))
                    assert len(peer['prompts']) == 3
                    fixture_behavior['inbox'] = {'next-turn': [], 'next-step': []}
                    for index, command in enumerate(('补充 ' + latest['id'], '补充 ' + latest['id'] + '\n',
                            '补充 ' + latest['id'] + '\napi_key=sk-abcdefghijklmnopqrstuvwxyz',
                            '补充 ' + latest['id'] + '\n' + 'x' * 4001)):
                        await receive(adapter, raw('om_owner_invalid_' + str(index), '@_user_1 ' + command))
                        assert len(peer['prompts']) == 3
                    body = 'Before implementation, verify the two synthetic acceptance requirements.\nPreserve the authorized finite window.'
                    await receive(adapter, raw('om_owner_context', '@_user_1 补充 ' + latest['id'] + '\n' + body))
                    latest = record()
                    saved = latest['dsh_execution'].get('owner_context_control', {})
                    assert saved.get('status') == 'accepted', {'saved': saved, 'messages': [m.request_body.content for m in sent if m.message_id == 'om_owner_context']}
                    assert saved['dispatch_status'] == 'accepted' and saved['turn'] == 4
                    assert peer['prompts'][-1] == {'sessionId': sid, 'requestId': saved['request_id'], 'mode': 'queue',
                        'content': [{'type': 'text', 'text': body}]}
                    assert saved['text_sha256'] == hashlib.sha256(body.encode()).hexdigest()
                    assert fixture_behavior['owner_context_intent']['status'] == 'intent'
                    assert fixture_behavior['owner_context_intent']['text_sha256'] == saved['text_sha256']
                    assert len(peer['prompts']) == 4 and latest['dsh_execution']['state'] == 'running'
                    assert all(latest['dsh_execution'][key] == value for key, value in preserved.items())
                    assert budget_file.read_bytes() == json.dumps(third_budget).encode()
                    assert sum(call['method'] == 'session/create' for call in peer['calls']) == creates
                    card = json.loads(registry.dispatch('kanban_show', {'task_id': latest['card_id']}))['task']
                    assert card['status'] == 'ready' and card.get('current_run_id') is None
                    await receive(adapter, raw('om_owner_context', '@_user_1 补充 ' + latest['id'] + '\n' + body))
                    await receive(adapter, raw('om_owner_context_second', '@_user_1 补充 ' + latest['id'] + '\n' + body))
                    assert len(peer['prompts']) == 4
                    assert any(m.message_id == 'om_owner_context' and '补充要求已由原 SID 受理' in m.request_body.content for m in sent)
                    repeat_window_evidence['owner_context'] = saved
                    rows = peer['journals'][sid]
                    for kind, payload in [('agent/inbox/spliced', {'target': 'next-turn', 'start': 0, 'removedCount': 1, 'inserted': []}),
                            ('step/start', {'turn': 4, 'step': 1}), ('step/end', {'turn': 4, 'step': 1}),
                            ('turn/end', {'turn': 4, 'reason': {'kind': 'error', 'error': {
                                'message': 'Shared work native settlement usage is unknown.', 'code': 'UNKNOWN'}}})]:
                        rows.append({'type': 'event', 'event': {'type': kind, 'seq': len(rows), 'time': 11, 'data': payload}})
                    model.worker_steps = 0
                    with connect_closing() as db:
                        assert dispatch_once(db, max_spawn=1).spawned
                        worker = remember_worker(db, latest['card_id'])
                    until = time.monotonic() + 20
                    while time.monotonic() < until:
                        waited, status = os.waitpid(worker['pid'], os.WNOHANG)
                        if waited:
                            assert os.waitstatus_to_exitcode(status) == 0
                            worker = None
                            break
                        await asyncio.sleep(.05)
                    assert worker is None
                    latest = record()
                    assert latest['dsh_execution']['state'] == 'outcome_unknown'
                    assert latest['dsh_execution']['owner_context_control'] == saved
                    assert budget_file.read_bytes() == json.dumps(third_budget).encode()
                    first_control = json.loads(json.dumps(saved))
                    retry_body = 'Recheck the original synthetic acceptance after verified native Hook repair.'
                    first_failure = json.loads(json.dumps(peer['journals'][sid]))
                    for case in ('wrong_request', 'wrong_cursor_event', 'wrong_step', 'wrong_error'):
                        changed = json.loads(json.dumps(first_failure))
                        if case == 'wrong_request':
                            changed[-6]['event']['data']['inserted'][0]['source']['rpcId'] = 'foreign-owner-request'
                        elif case == 'wrong_cursor_event':
                            changed[first_control['cursor']]['event']['type'] = 'assistant/message'
                        elif case == 'wrong_step':
                            changed[-2]['event']['data']['step'] = 2
                        else:
                            changed[-1]['event']['data']['reason']['error']['message'] += '!'
                        peer['journals'][sid] = changed
                        await receive(adapter, raw('om_owner_retry_' + case, '@_user_1 补充 ' + latest['id'] + '\n' + retry_body))
                        assert len(peer['prompts']) == 4 and record()['dsh_execution']['owner_context_control'] == first_control
                        assert not record()['dsh_execution'].get('owner_context_history')
                        peer['journals'][sid] = json.loads(json.dumps(first_failure))
                    altered = json.loads(budget_file.read_bytes())
                    altered['renewWindow']['started_at_ms'] = int(time.time() * 1000)
                    budget_file.write_text(json.dumps(altered))
                    await receive(adapter, raw('om_owner_retry_started', '@_user_1 补充 ' + latest['id'] + '\n' + retry_body))
                    assert len(peer['prompts']) == 4
                    budget_file.write_text(json.dumps(third_budget))
                    fixture_behavior['jobs'] = [{'id': 'fixture-owner-retry-job', 'owner': sid, 'status': 'running'}]
                    await receive(adapter, raw('om_owner_retry_active_job', '@_user_1 补充 ' + latest['id'] + '\n' + retry_body))
                    assert len(peer['prompts']) == 4
                    fixture_behavior['jobs'] = []
                    fixture_behavior['inbox'] = {'next-turn': [{'id': 'fixture-owner-foreign-input', 'role': 'user',
                        'source': {'kind': 'user', 'rpcId': 'foreign-request'}}], 'next-step': []}
                    await receive(adapter, raw('om_owner_retry_foreign_input', '@_user_1 补充 ' + latest['id'] + '\n' + retry_body))
                    assert len(peer['prompts']) == 4
                    fixture_behavior['inbox'] = {'next-turn': [], 'next-step': []}
                    await receive(adapter, raw('om_owner_context_retry', '@_user_1 补充 ' + latest['id'] + '\n' + retry_body))
                    latest = record()
                    saved = latest['dsh_execution'].get('owner_context_control', {})
                    assert saved.get('message_id') == 'om_owner_context_retry' and saved.get('status') == 'accepted', saved
                    assert latest['dsh_execution']['owner_context_history'] == [first_control]
                    assert saved['request_id'] != first_control['request_id'] and saved['turn'] == 5
                    assert peer['prompts'][-1] == {'sessionId': sid, 'requestId': saved['request_id'], 'mode': 'queue',
                        'content': [{'type': 'text', 'text': retry_body}]}
                    assert saved['dispatch_status'] == 'accepted' and len(peer['prompts']) == 5
                    assert budget_file.read_bytes() == json.dumps(third_budget).encode()
                    assert sum(call['method'] == 'session/create' for call in peer['calls']) == creates
                    for mid in ('om_owner_context', 'om_owner_context_retry', 'om_owner_context_third'):
                        await receive(adapter, raw(mid, '@_user_1 补充 ' + latest['id'] + '\n' + retry_body))
                        assert len(peer['prompts']) == 5
                    repeat_window_evidence['owner_context_retry'] = saved
                    if owner_maintenance:
                        rows = peer['journals'][sid]
                        for kind, payload in [('agent/inbox/spliced', {'target': 'next-turn', 'start': 0, 'removedCount': 1, 'inserted': []}),
                                ('step/start', {'turn': 5, 'step': 1}), ('step/end', {'turn': 5, 'step': 1}),
                                ('turn/end', {'turn': 5, 'reason': {'kind': 'error', 'error': {
                                    'message': 'Shared work native settlement usage is unknown.', 'code': 'UNKNOWN'}}})]:
                            rows.append({'type': 'event', 'event': {'type': kind, 'seq': len(rows), 'time': 13, 'data': payload}})
                        model.worker_steps = 0
                        with connect_closing() as db:
                            assert dispatch_once(db, max_spawn=1).spawned
                            worker = remember_worker(db, latest['card_id'])
                        until = time.monotonic() + 20
                        while time.monotonic() < until:
                            waited, status = os.waitpid(worker['pid'], os.WNOHANG)
                            if waited:
                                assert os.waitstatus_to_exitcode(status) == 0
                                worker = None
                                break
                            await asyncio.sleep(.05)
                        assert worker is None
                        latest = record()
                        original_owners = {'owner_context_control': latest['dsh_execution']['owner_context_control'],
                            'owner_context_history': latest['dsh_execution']['owner_context_history']}
                        before_maintenance = budget_file.read_bytes()
                        await receive(adapter, raw('om_owner_maintenance_prepare', '@_user_1 继续 ' + latest['id']))
                        latest = record()
                        checkpoint = latest['dsh_execution'].get('owner_context_maintenance_checkpoint', {})
                        assert checkpoint.get('status') == 'prepared', {'checkpoint': checkpoint, 'execution': latest['dsh_execution']}
                        assert len(peer['prompts']) == 5 and budget_file.read_bytes() == before_maintenance
                        assert json.loads(registry.dispatch('kanban_show', {'task_id': latest['card_id']}))['task']['status'] == 'triage'
                        host_processes[-1].terminate()
                        assert host_processes[-1].wait(timeout=5) == -15
                        host_processes.append(subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(120)'],
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL))
                        updated = json.loads(receipt.read_text())
                        updated.update(lease='fixture-owner-maintenance-lease', pid=host_processes[-1].pid)
                        receipt.write_text(json.dumps(updated))
                        scope = json.loads(reference.read_text())
                        scope['host_lease'] = updated['lease']
                        reference.write_text(json.dumps(scope))
                        rows.append({'type': 'event', 'event': {'type': 'session/end-seed', 'seq': len(rows), 'time': 14, 'data': {}}})
                        next(row for row in peer['sessions'] if row['sessionId'] == sid)['agentAvailable'] = False
                        await receive(adapter, raw('om_owner_maintenance_admit', '@_user_1 继续 ' + latest['id']))
                        latest = record()
                        checkpoint = latest['dsh_execution']['owner_context_maintenance_checkpoint']
                        assert checkpoint['status'] == 'admitted' and latest['dsh_execution']['state'] == 'outcome_unknown'
                        assert all(latest['dsh_execution'][key] == value for key, value in original_owners.items())
                        assert len(peer['prompts']) == 5
                        assert json.loads(registry.dispatch('kanban_show', {'task_id': latest['card_id']}))['task']['status'] == 'triage'
                        await receive(adapter, raw('om_owner_maintenance_final', '@_user_1 补充 ' + latest['id'] + '\n' + retry_body))
                        latest = record()
                        saved = latest['dsh_execution']['owner_context_control']
                        assert saved['status'] == 'accepted' and saved['message_id'] == 'om_owner_maintenance_final'
                        assert latest['dsh_execution']['owner_context_history'] == [first_control, original_owners['owner_context_control']]
                        assert len(peer['prompts']) == 6 and saved['turn'] == 6
                        await receive(adapter, raw('om_owner_maintenance_fourth', '@_user_1 补充 ' + latest['id'] + '\n' + retry_body))
                        assert len(peer['prompts']) == 6
                        repeat_window_evidence['owner_maintenance'] = checkpoint
                    # The original public dispatcher now supervises this accepted
                    # input; the previous failed turn must not become its result.
                    next_budget = json.loads(budget_file.read_bytes())
                    next_budget['renewWindow'].update(started_at_ms=int(time.time() * 1000), hook_marker='same-sid-window-v1')
                    next_budget['state']['requests'] += 1
                    next_budget['state']['reported_tokens'] += 15
                    next_budget['state']['admissions'].append(saved['request_id'])
                    budget_file.write_text(json.dumps(next_budget))
                    rows = peer['journals'][sid]
                    for kind, payload in [('agent/inbox/spliced', {'target': 'next-turn', 'start': 0, 'removedCount': 1, 'inserted': []}),
                            ('assistant/message', {'turn': saved['turn'], 'step': 1, 'usage': {'inputTokens': 10, 'outputTokens': 5},
                                'message': {'id': 'fixture-owner-settlement', 'role': 'assistant', 'content': [
                                    {'type': 'text', 'text': 'Synthetic owner context has been read.'}]}, 'stream': []}),
                            ('turn/end', {'turn': saved['turn'], 'reason': {'kind': 'completed'}})]:
                        rows.append({'type': 'event', 'event': {'type': kind, 'seq': len(rows), 'time': 12, 'data': payload}})
                    model.worker_steps = 0
                    with connect_closing() as db:
                        assert dispatch_once(db, max_spawn=1).spawned
                        worker = remember_worker(db, latest['card_id'])
                    claimed_run = worker['run_id']
                    until = time.monotonic() + 20
                    while time.monotonic() < until:
                        waited, status = os.waitpid(worker['pid'], os.WNOHANG)
                        if waited:
                            assert os.waitstatus_to_exitcode(status) == 0
                            worker = None
                            break
                        await asyncio.sleep(.05)
                    assert worker is None
                    latest = record()
                    assert latest['dsh_execution']['supervisor_claim']['run_id'] == claimed_run
                    assert latest['dsh_execution']['state'] == 'awaiting_acceptance', latest['dsh_execution']
                    assert latest['dsh_execution']['terminal_reason'] == {'kind': 'completed'}
                    assert latest['dsh_execution']['journal_cursor'] == rows[-1]['event']['seq']
                    assert latest['dsh_execution']['owner_context_control'] == saved
                    assert latest['dsh_execution']['last_failure'] == preserved['last_failure']
                    assert len(peer['prompts']) == (6 if owner_maintenance else 5)
                    repeat_window_evidence['owner_context_claim'] = {'run_id': claimed_run, 'cursor': saved['cursor'],
                        'request_id': saved['request_id'], 'terminal_reason': latest['dsh_execution']['terminal_reason'],
                        'prompt_count': len(peer['prompts']), 'journal_cursor': latest['dsh_execution']['journal_cursor']}
                break
            if resume_completed_window and ended and not resume_admitted and card['status'] in {'blocked', 'triage'}:
                assert latest['dsh_execution']['state'] == 'awaiting_acceptance', latest['dsh_execution']
                assert latest['dsh_execution']['acceptance']['status'] == 'unmet'
                assert 'delivery_report_missing' in latest['dsh_execution']['acceptance']['unmet']
                while True:
                    waited, status = os.waitpid(worker['pid'], os.WNOHANG)
                    if waited:
                        assert os.waitstatus_to_exitcode(status) == 0
                        break
                    await asyncio.sleep(.05)
                worker = None
                budget_file = ledger / (hashlib.sha256(latest['dsh_execution']['session_id'].encode()).hexdigest() + '.json')
                budget_before = budget_file.read_bytes()
                original_execution = dict(latest['dsh_execution'])
                await receive(adapter, raw('om_resume_prepare', '@_user_1 继续 ' + latest['id']))
                latest = record()
                checkpoint = latest['dsh_execution'].get('window_resume_checkpoint')
                assert checkpoint and checkpoint['status'] == 'prepared', latest['dsh_execution']
                assert budget_file.read_bytes() == budget_before and len(peer['prompts']) == 1
                assert all(latest['dsh_execution'][key] == original_execution[key]
                           for key in ('session_id', 'generation', 'request_id', 'first_input', 'state'))
                await receive(adapter, raw('om_resume_prepare', '@_user_1 继续 ' + latest['id']))
                assert len(peer['prompts']) == 1 and budget_file.read_bytes() == budget_before
                before_resume = json.loads(budget_before)
                host_processes[0].terminate()
                assert host_processes[0].wait(timeout=5) == -15
                host_processes.append(subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(120)'],
                    stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL))
                updated = json.loads(receipt.read_text())
                updated.update(lease='fixture-resumed-lease', pid=host_processes[-1].pid)
                receipt.write_text(json.dumps(updated))
                resumed_scope = json.loads(reference.read_text())
                resumed_scope['host_lease'] = updated['lease']
                reference.write_text(json.dumps(resumed_scope))
                rows = peer['journals'][latest['dsh_execution']['session_id']]
                rows.append({'type': 'event', 'event': {'type': 'session/end-seed', 'seq': len(rows), 'time': 5, 'data': {}}})
                fixture_behavior['header_defaults'] = {'delegationDepth': 0}
                next(row for row in peer['sessions'] if row['sessionId'] == latest['dsh_execution']['session_id'])['agentAvailable'] = False
                await receive(adapter, raw('om_resume_admit', '@_user_1 继续 ' + latest['id']))
                latest = record()
                control = latest['dsh_execution']['continuation_control']
                assert control['kind'] == 'resume_window' and control['status'] == 'admitted', latest['dsh_execution']
                resumed = json.loads(budget_file.read_text())
                assert resumed['host_lease'] == resumed['renewWindow']['host_lease'] == updated['lease']
                assert resumed['renewWindow']['original_host_lease'] == before_resume['renewWindow']['original_host_lease']
                assert resumed['renewWindow']['request_base'] == before_resume['renewWindow']['request_base']
                assert resumed['renewWindow']['started_at_ms'] == before_resume['renewWindow']['started_at_ms']
                assert {key: value for key, value in resumed['state'].items() if key != 'binding_sha256'} == {
                    key: value for key, value in before_resume['state'].items() if key != 'binding_sha256'}
                assert resumed['state']['binding_sha256'] != before_resume['state']['binding_sha256']
                await receive(adapter, raw('om_resume_admit', '@_user_1 继续 ' + latest['id']))
                assert len(peer['prompts']) == 1
                model.worker_steps = 0
                with connect_closing() as db:
                    assert dispatch_once(db, max_spawn=1).spawned
                    worker = remember_worker(db, initial['card_id'])
                resume_admitted = True
                continue
            if resume_completed_window and resume_admitted and len(peer['prompts']) == 2 and not resume_ended:
                sid = latest['dsh_execution']['session_id']
                rows = peer['journals'][sid]
                rows.append({'type': 'event', 'event': {'type': 'turn/end', 'seq': len(rows), 'time': 6,
                    'data': {'turn': 2, 'reason': {'kind': 'error', 'error': {'code': 'UNKNOWN', 'message': 'Synthetic resumed terminal.'}}}}})
                next(row for row in peer['sessions'] if row['sessionId'] == sid)['running'] = False
                resume_ended = True
            if resume_completed_window and resume_ended and card['status'] in {'blocked', 'triage'}:
                assert latest['dsh_execution']['state'] == 'execution_failed', latest['dsh_execution']
                while True:
                    waited, status = os.waitpid(worker['pid'], os.WNOHANG)
                    if waited:
                        assert os.waitstatus_to_exitcode(status) == 0
                        break
                    await asyncio.sleep(.05)
                worker = None
                assert len(peer['prompts']) == 2
                break
            if shared_scenario in {'prepare_continue', *continuation_scenarios} and not continued and card['status'] in {'blocked', 'triage'}:
                assert latest['dsh_execution']['state'] == 'execution_failed'
                while True:
                    waited, status = os.waitpid(worker['pid'], os.WNOHANG)
                    if waited:
                        assert os.waitstatus_to_exitcode(status) == 0
                        break
                    await asyncio.sleep(.05)
                worker = None
                if triage_continuation:
                    assert card['status'] == 'triage' and card['current_run_id'] is None
                original_execution = dict(latest['dsh_execution'])
                if shared_scenario in continuation_scenarios:
                    original_history = json.loads(json.dumps(peer['journals'][original_execution['session_id']]))
                    assert not any(row['event']['type'] == 'user/message' for row in original_history)
                    first_request = peer['prompts'][0]
                    inserted = [item for row in original_history if row['event']['type'] == 'agent/inbox/spliced'
                                for item in row['event']['data']['inserted']
                                if item.get('source', {}).get('rpcId') == first_request['requestId']]
                    assert len(inserted) == 1 and inserted[0]['content'] == first_request['content']
                    assert any(row['event']['type'] == 'agent/inbox/spliced'
                               and row['event']['data']['target'] == 'next-turn'
                               and row['event']['data'].get('removedCount') == 1
                               and row['event']['data']['inserted'] == [] for row in original_history)
                budget_file = ledger / (hashlib.sha256(latest['dsh_execution']['session_id'].encode()).hexdigest() + '.json')
                budget_before = budget_file.read_bytes()
                await receive(adapter, raw('om_continue_prepare', '@_user_1 继续 ' + latest['id']))
                latest = record()
                checkpoint = latest['dsh_execution'].get('maintenance_checkpoint')
                assert checkpoint and checkpoint['status'] == 'prepared', {
                    'checkpoint': checkpoint, 'execution_state': latest['dsh_execution']['state'],
                    'public_reply': [message.request_body.content for message in sent if message.message_id == 'om_continue_prepare']}
                assert len(peer['prompts']) == 1 and budget_file.read_bytes() == budget_before
                assert all(latest['dsh_execution'][key] == original_execution[key]
                           for key in ('session_id', 'generation', 'request_id', 'first_input', 'state'))
                await receive(adapter, raw('om_continue_prepare', '@_user_1 继续 ' + latest['id']))
                assert len(peer['prompts']) == 1
                assert any('续接准备' in message.request_body.content for message in sent)
                if shared_scenario == 'prepare_continue':
                    break
                host_processes.append(subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(120)'],
                    stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL))
                updated = json.loads(receipt.read_text())
                updated.update(lease='fixture-restarted-lease', pid=host_processes[-1].pid)
                receipt.write_text(json.dumps(updated))
                approved = json.loads(reference.read_text())
                approved['host_lease'] = updated['lease']
                reference.write_text(json.dumps(approved))
                if shared_scenario == 'continue_restart':
                    async def rejected(message_id):
                        before = budget_file.read_bytes()
                        await receive(adapter, raw(message_id, '@_user_1 继续 ' + latest['id']))
                        current = record()['dsh_execution']
                        assert 'continuation_control' not in current
                        assert current['maintenance_checkpoint'] == checkpoint
                        assert all(current[key] == original_execution[key]
                                   for key in ('session_id', 'generation', 'request_id', 'first_input', 'state', 'native_identity', 'reference_sha256'))
                        assert budget_file.read_bytes() == before and len(peer['prompts']) == 1
                        assert any(message.message_id == message_id and '继续未受理' in message.request_body.content for message in sent)
                    await rejected('om_continue_old_host_alive')
                host_processes[0].terminate()
                assert host_processes[0].wait(timeout=5) == -15
                if shared_scenario == 'continue_restart':
                    wrong = {**approved, 'budget': {**approved['budget'], 'max_model_requests': 79}}
                    reference.write_text(json.dumps(wrong))
                    await rejected('om_continue_changed_caps')
                    reference.write_text(json.dumps(approved))
                    for key, value in [('max_reported_tokens', 300000), ('max_output_tokens_per_request', 8192)]:
                        reference.write_text(json.dumps({**approved, 'budget': {**approved['budget'], key: value}}))
                        await rejected('om_continue_removed_' + key)
                        reference.write_text(json.dumps(approved))
                    for name, state in (
                        ('started', {'started_at_ms': 1, 'requests': 1, 'reported_tokens': 0, 'admissions': ['fixture-start'], 'stop_reason': None}),
                        ('uncertain', {'started_at_ms': None, 'requests': 0, 'reported_tokens': 0, 'admissions': [], 'stop_reason': 'usage-unknown'}),
                    ):
                        budget_file.write_text(json.dumps({**json.loads(budget_before), 'state': state}))
                        await rejected('om_continue_budget_' + name)
                        budget_file.write_bytes(budget_before)
                    for key, value in (('created_at', 1), ('agent_preset', 'fixture-foreign')):
                        fixture_behavior[key] = value
                        await rejected('om_continue_changed_' + key)
                        fixture_behavior.pop(key)
                    sid = latest['dsh_execution']['session_id']
                    original_session = next(row for row in peer['sessions'] if row['sessionId'] == sid)
                    original_session['cwd'] = str(repo / 'foreign')
                    await rejected('om_continue_wrong_directory')
                    original_session['cwd'] = str(repo)
                    rows = peer['journals'][sid]
                    rows.append({'type': 'event', 'event': {'type': 'tool/call', 'seq': len(rows), 'time': 5,
                        'data': {'callId': 'fixture-unexpected', 'name': 'bash', 'arguments': '{}'}}})
                    await rejected('om_continue_tool_activity')
                    rows.pop()
                    peer['workspace']['workspaceId'] = 'fixture-foreign-workspace'
                    await rejected('om_continue_wrong_workspace')
                    peer['workspace']['workspaceId'] = 'fixture-shared-workspace'
                    peer['workspace']['sessionIds'].append('fixture-foreign-session')
                    await rejected('om_continue_changed_session_accounts')
                    peer['workspace']['sessionIds'].pop()
                    fixture_behavior['jobs'] = [{'id': 'fixture-active-job', 'owner': sid, 'status': 'running'}]
                    await rejected('om_continue_active_job')
                    fixture_behavior.pop('jobs')
                    if native_restore:
                        rows.append({'type': 'event', 'event': {'type': 'session/end-seed', 'seq': len(rows),
                            'time': 6, 'data': {}}})
                        fixture_behavior['header_defaults'] = {'delegationDepth': 0}
                        fixture_behavior['context_headers']['headers'][0]['tools'][0]['plugin'] = 'tool-bash-persistent'
                        attribution = fixture_behavior['context_headers']['headers'][0]['tools'][0]
                        for label, key, value in [('other_tool', 'name', 'skill'), ('changed_tokens', 'tokens', 11),
                                ('unknown_alias', 'plugin', 'foreign-tool-bash')]:
                            original_value = attribution[key]
                            attribution[key] = value
                            await rejected('om_continue_attribution_' + label)
                            attribution[key] = original_value
                        marker = rows[-1]['event']
                        marker['type'] = 'fixture/unexpected-marker'
                        await rejected('om_continue_unknown_restore_marker')
                        marker['type'] = 'session/end-seed'
                        marker['data'] = {'inherited': True}
                        await rejected('om_continue_inherited_restore_marker')
                        marker['data'] = {}
                        fixture_behavior['header_defaults']['delegationDepth'] = 1
                        await rejected('om_continue_changed_delegation_depth')
                        fixture_behavior['header_defaults']['delegationDepth'] = 0
                        original_reason = rows[-2]['event']['data']['reason']
                        rows[-2]['event']['data']['reason'] = {'kind': 'error', 'error': {'message': 'Changed original failure.', 'code': 'UNKNOWN'}}
                        await rejected('om_continue_changed_original_history')
                        rows[-2]['event']['data']['reason'] = original_reason
                        rows.append({'type': 'event', 'event': {'type': 'session/end-seed', 'seq': len(rows), 'time': 7, 'data': {}}})
                        await rejected('om_continue_extra_restore_marker')
                        rows.pop()
                if concurrent_claim:
                    import threading
                    claim_result = {}
                    model.worker_steps = 0
                    def dispatch_continuation():
                        try:
                            until = time.monotonic() + 10
                            with connect_closing() as db:
                                while time.monotonic() < until:
                                    before = record()['dsh_execution'].get('continuation_control', {}).get('dispatch_status')
                                    if dispatch_once(db, max_spawn=1).spawned:
                                        claim_result['worker'] = remember_worker(db, initial['card_id'])
                                        claim_result['dispatch_status_before_claim'] = before
                                        claim_result['dispatch_status_after_claim'] = record()['dsh_execution']['continuation_control']['dispatch_status']
                                        return
                                    time.sleep(.002)
                            claim_result['error'] = 'Original dispatcher did not claim the continuation.'
                        except Exception as error:
                            claim_result['error'] = repr(error)
                    dispatcher = threading.Thread(target=dispatch_continuation, daemon=True)
                    dispatcher.start()
                await receive(adapter, raw('om_continue_after_restart', '@_user_1 继续 ' + latest['id']))
                latest = record()
                control = latest['dsh_execution'].get('continuation_control')
                assert control and control['status'] == 'admitted' and control['dispatch_status'] == 'accepted', latest['dsh_execution']
                assert latest['dsh_execution']['maintenance_checkpoint'] == checkpoint
                assert all(latest['dsh_execution'][key] == original_execution[key]
                           for key in ('session_id', 'generation', 'request_id', 'first_input'))
                assert latest['dsh_execution']['state'] == 'continuation_pending'
                assert control['original_failure']['state'] == original_execution['state']
                rebound = json.loads(budget_file.read_bytes())
                assert rebound == {**json.loads(budget_before), 'host_lease': updated['lease']}
                await receive(adapter, raw('om_continue_after_restart', '@_user_1 继续 ' + latest['id']))
                assert len(peer['prompts']) == 1
                if concurrent_claim:
                    until = time.monotonic() + 10
                    while dispatcher.is_alive() and time.monotonic() < until:
                        await asyncio.sleep(.01)
                    assert not dispatcher.is_alive() and 'error' not in claim_result, claim_result
                    worker = claim_result['worker']
                else:
                    model.worker_steps = 0
                    with connect_closing() as db:
                        assert dispatch_once(db, max_spawn=1).spawned
                        worker = remember_worker(db, initial['card_id'])
                continued = True
            if shared_scenario == 'continue_unknown' and continued and len(peer['prompts']) == 2:
                await asyncio.sleep(.5)
                latest = record()
                assert latest['dsh_execution']['state'] == 'outcome_unknown', latest['dsh_execution']
                assert latest['dsh_execution']['first_input'] == 'accepted'
                assert latest['dsh_execution']['continuation_control']['status'] == 'input_intent'
                await receive(adapter, raw('om_continue_after_restart', '@_user_1 继续 ' + latest['id']))
                await receive(adapter, raw('om_continue_unknown_new_message', '@_user_1 继续 ' + latest['id']))
                assert len(peer['prompts']) == 2, 'Unknown continuation admission must never be blindly replayed.'
                break
            if shared_scenario == 'continue_restart' and continued and not max_tokens_started:
                if len(peer['prompts']) == 2 and latest['dsh_execution'].get('continuation_control', {}).get('status') == 'accepted' and not continuation_ended:
                    sid = latest['dsh_execution']['session_id']
                    assert peer['journals'][sid][:len(original_history)] == original_history
                    continuation_seen_at = continuation_seen_at or time.monotonic()
                    assert card['status'] == 'running', 'The old turn/end cannot end this continuation worker.'
                    assert latest['dsh_execution']['state'] == 'running'
                    if time.monotonic() - continuation_seen_at > .5:
                        sid = peer['prompts'][1]['sessionId']
                        rows = peer['journals'][sid]
                        if not continuation_started:
                            rows.append({'type': 'event', 'event': {'type': 'turn/start', 'seq': len(rows),
                                'time': 5, 'data': {'turn': 2}}})
                            submission = peer['prompts'][1]
                            rows.append({'type': 'event', 'event': {'type': 'user/message', 'seq': len(rows),
                                'time': 5, 'data': {'id': 'message-' + submission['requestId'], 'role': 'user',
                                    'content': submission['content'], 'source': {'kind': 'user', 'rpcId': submission['requestId']}}}})
                            next(row for row in peer['sessions'] if row['sessionId'] == sid)['running'] = True
                            continuation_started = True
                            continuation_seen_at = time.monotonic()
                        else:
                            reason = {'kind': 'error', 'error': {'message': 'Synthetic continuation ended.', 'code': 'UNKNOWN'}}
                            if max_tokens_continuation:
                                for step in range(8):
                                    rows.append({'type': 'event', 'event': {'type': 'request/header', 'seq': len(rows), 'time': 6,
                                        'data': {'header': {'config': {'provider': 'fixture', 'model': 'fixture'}}}}})
                                    rows.append({'type': 'event', 'event': {'type': 'assistant/message', 'seq': len(rows), 'time': 6,
                                        'data': {'turn': 2, 'step': step,
                                            'usage': {'inputTokens': 41000 if tokens_uncapped else 16000, 'outputTokens': 2150},
                                            'message': {'id': 'fixture-partial-' + str(step), 'role': 'assistant',
                                                'content': [{'type': 'text', 'text': 'Partial bounded fixture work.'}]},
                                            'stream': [{'type': 'chunk', 'chunk': {'type': 'finish', 'reason': {
                                                'kind': 'max-tokens' if step == 7 else 'completed'}}}]}}})
                                budget_row = json.loads(budget_file.read_text())
                                if tokens_uncapped:
                                    budget_row['budget'].update(max_reported_tokens=300000, max_output_tokens_per_request=8192)
                                budget_row['state'] = {'started_at_ms': int(time.time() * 1000), 'requests': 8,
                                    'reported_tokens': 345200 if tokens_uncapped else 145200,
                                    'admissions': ['fixture-request-' + str(step) for step in range(8)],
                                    'stop_reason': None}
                                budget_file.write_text(json.dumps(budget_row))
                                reason = {'kind': 'max-tokens'}
                            rows.append({'type': 'event', 'event': {'type': 'turn/end', 'seq': len(rows),
                                'time': 6, 'data': {'turn': 2, 'reason': reason}}})
                            next(row for row in peer['sessions'] if row['sessionId'] == sid)['running'] = False
                            continuation_ended = True
                if continuation_ended and card['status'] in {'blocked', 'triage'}:
                    continuation_text = peer['prompts'][1]['content'][0]['text']
                    required_scope = [latest['issue']['url'], latest['issue']['title'], latest['issue']['body'],
                        latest['target']['repository'], latest['dsh_execution']['github_reference']['account'],
                        latest['dsh_execution']['github_reference']['config_dir'], latest['dsh_execution']['verification_prefix'],
                        'GH_TOKEN= GITHUB_TOKEN= GH_ENTERPRISE_TOKEN= GITHUB_ENTERPRISE_TOKEN=']
                    assert all(value in continuation_text for value in required_scope), 'The explicit new continuation must include its complete frozen task scope.'
                    assert latest['dsh_execution']['continuation_control']['status'] == 'accepted'
                    assert latest['dsh_execution']['supervisor_claim']['identity']['pid'] == worker['pid']
                    assert latest['dsh_execution']['supervisor_claim']['run_id'] != original_execution['supervisor_claim']['run_id']
                    assert latest['dsh_execution']['continuation_control']['previous_claim'] == original_execution['supervisor_claim']
                    assert all(latest['dsh_execution'][key] == original_execution[key]
                               for key in ('session_id', 'generation', 'request_id', 'first_input'))
                    assert peer['prompts'][1]['sessionId'] == peer['prompts'][0]['sessionId']
                    assert peer['prompts'][1]['requestId'] == latest['dsh_execution']['continuation_request_id']
                    assert peer['prompts'][1]['requestId'] != peer['prompts'][0]['requestId']
                    if max_tokens_continuation:
                        while True:
                            waited, status = os.waitpid(worker['pid'], os.WNOHANG)
                            if waited:
                                assert os.waitstatus_to_exitcode(status) == 0
                                break
                            await asyncio.sleep(.05)
                        worker = None
                        before_max = record()['dsh_execution']
                        max_budget_before = budget_file.read_bytes()
                        max_control_before = before_max['continuation_control']
                        assert before_max['terminal_reason'] == {'kind': 'max-tokens'}
                        if renew_window:
                            old_budget = json.loads(max_budget_before)
                            old_budget['state'].update(started_at_ms=int(time.time() * 1000) - 61000,
                                stop_reason='shared-work-wall-budget')
                            budget_file.write_text(json.dumps(old_budget))
                            max_budget_before = budget_file.read_bytes()
                            await receive(adapter, raw('om_window_plain_continue', '@_user_1 继续 ' + latest['id']))
                            assert budget_file.read_bytes() == max_budget_before and len(peer['prompts']) == 2
                            window_scope = json.loads(reference.read_text())
                            window_scope['verification']['python_sha256'] = '0' * 64
                            reference.write_text(json.dumps(window_scope))
                            await receive(adapter, raw('om_window_prepare', '@_user_1 续预算 ' + latest['id']))
                            assert record()['dsh_execution'].get('renew_window_checkpoint', {}).get('status') == 'prepared', {
                                'replies': [message.request_body.content for message in sent if message.message_id == 'om_window_prepare'],
                                'state': record()['dsh_execution']['state']}
                            renew_checkpoint = record()['dsh_execution']['renew_window_checkpoint']
                            assert Path(renew_checkpoint['ledger_backup']).read_bytes() == max_budget_before
                            await receive(adapter, raw('om_window_prepare', '@_user_1 续预算 ' + latest['id']))
                            await receive(adapter, raw('om_window_alive', '@_user_1 续预算 ' + latest['id']))
                            assert budget_file.read_bytes() == max_budget_before and len(peer['prompts']) == 2
                            host_processes[-1].terminate()
                            assert host_processes[-1].wait(timeout=5) == -15
                            host_processes.append(subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(120)'],
                                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL))
                            renewed_receipt = json.loads(receipt.read_text())
                            renewed_receipt.update(lease='fixture-renewed-lease', pid=host_processes[-1].pid)
                            receipt.write_text(json.dumps(renewed_receipt))
                            renewed_scope = json.loads(reference.read_text())
                            renewed_scope['host_lease'] = renewed_receipt['lease']
                            renewed_scope['verification']['python_sha256'] = hashlib.sha256(Path(renewed_scope['verification']['python']).read_bytes()).hexdigest()
                            reference.write_text(json.dumps(renewed_scope))
                            rows.append({'type': 'event', 'event': {'type': 'session/end-seed', 'seq': len(rows), 'time': 7, 'data': {}}})
                            fixture_behavior['header_defaults'] = {'delegationDepth': 0}
                            next(row for row in peer['sessions'] if row['sessionId'] == sid)['agentAvailable'] = False
                            await receive(adapter, raw('om_window_admit', '@_user_1 续预算 ' + latest['id']))
                            latest = record()
                            assert latest['dsh_execution']['continuation_control']['kind'] == 'renew_window', latest['dsh_execution']
                            granted = json.loads(budget_file.read_bytes())
                            assert granted['renewWindow']['started_at_ms'] is None
                            assert granted['renewWindow']['original_state'] == old_budget['state']
                            assert all(granted['state'][key] == old_budget['state'][key] for key in ('started_at_ms', 'requests', 'reported_tokens', 'admissions', 'stop_reason'))
                            assert Path(renew_checkpoint['ledger_backup']).read_bytes() == max_budget_before
                            assert latest['dsh_execution']['maintenance_checkpoint'] == checkpoint
                            max_budget_before = budget_file.read_bytes()
                            await receive(adapter, raw('om_window_admit', '@_user_1 续预算 ' + latest['id']))
                            await receive(adapter, raw('om_window_duplicate_new', '@_user_1 续预算 ' + latest['id']))
                            assert budget_file.read_bytes() == max_budget_before and len(peer['prompts']) == 2
                            model.worker_steps = 0
                            if expired_window:
                                model.repeat_supervise = True
                                model.worker_pause_on_unmet = True
                            with connect_closing() as db:
                                assert dispatch_once(db, max_spawn=1).spawned
                                worker = remember_worker(db, initial['card_id'])
                            max_tokens_started = True
                            continue
                        async def max_rejected(message_id):
                            before = budget_file.read_bytes()
                            await receive(adapter, raw(message_id, '@_user_1 继续 ' + latest['id']))
                            current = record()['dsh_execution']
                            assert current['continuation_control'] == max_control_before
                            assert current['maintenance_checkpoint'] == checkpoint
                            assert budget_file.read_bytes() == before and len(peer['prompts']) == 2
                        rows[-1]['event']['data']['reason'] = {'kind': 'error', 'error': {'message': 'Ordinary fixture error.'}}
                        await max_rejected('om_continue_ordinary_error')
                        rows[-1]['event']['data']['reason'] = {'kind': 'max-tokens'}
                        for key, value in [('started_at_ms', int(time.time() * 1000) - 61000),
                                           ('requests', 80), ('stop_reason', 'shared-work-wall-budget')]:
                            changed = json.loads(max_budget_before)
                            changed['state'][key] = value
                            budget_file.write_text(json.dumps(changed))
                            await max_rejected('om_continue_max_' + key)
                            budget_file.write_bytes(max_budget_before)
                        fixture_behavior['inbox'] = {'next-turn': [{'id': 'fixture-pending-input', 'role': 'user'}], 'next-step': []}
                        await max_rejected('om_continue_max_pending_input')
                        fixture_behavior['inbox'] = {'next-turn': [], 'next-step': []}
                        fixture_behavior['jobs'] = [{'id': 'fixture-running-job', 'owner': sid, 'status': 'running'}]
                        await max_rejected('om_continue_max_running_job')
                        fixture_behavior.pop('jobs')
                        await receive(adapter, raw('om_continue_max_tokens', '@_user_1 继续 ' + latest['id']))
                        latest = record()
                        max_control = latest['dsh_execution']['continuation_control']
                        assert max_control.get('kind') == 'max_tokens' and max_control['status'] == 'admitted', {
                            'control': max_control, 'state': latest['dsh_execution']['state']}
                        assert max_control['original_failure']['terminal_reason'] == {'kind': 'max-tokens'}
                        assert max_control_before in latest['dsh_execution']['continuation_history']
                        assert latest['dsh_execution']['maintenance_checkpoint'] == checkpoint
                        assert all(latest['dsh_execution'][key] == before_max[key] for key in
                                   ('session_id', 'generation', 'request_id', 'first_input', 'native_identity', 'reference_sha256'))
                        assert budget_file.read_bytes() == max_budget_before
                        await receive(adapter, raw('om_continue_max_tokens', '@_user_1 继续 ' + latest['id']))
                        model.worker_steps = 0
                        with connect_closing() as db:
                            assert dispatch_once(db, max_spawn=1).spawned
                            worker = remember_worker(db, initial['card_id'])
                        max_tokens_started = True
                        continue
                    await receive(adapter, raw('om_continue_after_restart', '@_user_1 继续 ' + latest['id']))
                    await receive(adapter, raw('om_continue_already_accepted', '@_user_1 继续 ' + latest['id']))
                    assert len(peer['prompts']) == 2
                    await receive(adapter, raw('om_stop_continued', '@_user_1 停止 ' + latest['id']))
                    latest = record()
                    assert latest['dsh_execution']['stop_requested'] and latest['dsh_execution']['state'] == 'stopped'
                    await receive(adapter, raw('om_continue_stopped', '@_user_1 继续 ' + latest['id']))
                    assert len(peer['prompts']) == 2
                    break
            if max_tokens_started and len(peer['prompts']) == 3:
                latest = record()
                if max_tokens_unknown:
                    await asyncio.sleep(.5)
                    latest = record()
                    assert latest['dsh_execution']['state'] == 'outcome_unknown'
                    assert latest['dsh_execution']['continuation_control']['status'] == 'input_intent'
                    await receive(adapter, raw('om_continue_max_tokens', '@_user_1 继续 ' + latest['id']))
                    await receive(adapter, raw('om_continue_max_unknown_new', '@_user_1 继续 ' + latest['id']))
                    assert len(peer['prompts']) == 3 and budget_file.read_bytes() == max_budget_before
                    break
                if latest['dsh_execution']['continuation_control']['status'] == 'accepted' and not max_tokens_ended:
                    max_tokens_seen_at = max_tokens_seen_at or time.monotonic()
                    assert card['status'] == 'running' and latest['dsh_execution']['state'] == 'running', latest['dsh_execution'].get('last_failure')
                    if time.monotonic() - max_tokens_seen_at > .5:
                        submission = peer['prompts'][2]
                        rows.append({'type': 'event', 'event': {'type': 'turn/start', 'seq': len(rows), 'time': 7, 'data': {'turn': 3}}})
                        rows.append({'type': 'event', 'event': {'type': 'user/message', 'seq': len(rows), 'time': 7,
                            'data': {'id': 'message-' + submission['requestId'], 'role': 'user', 'content': submission['content'],
                                     'source': {'kind': 'user', 'rpcId': submission['requestId']}}}})
                        if renew_continue:
                            rows.append({'type': 'event', 'event': {'type': 'request/header', 'seq': len(rows), 'time': 7,
                                'data': {'header': {'config': {'provider': 'fixture', 'model': 'fixture'}}}}})
                            rows.append({'type': 'event', 'event': {'type': 'assistant/message', 'seq': len(rows), 'time': 7,
                                'data': {'turn': 3, 'step': 0, 'usage': {'inputTokens': 12, 'outputTokens': 8},
                                    'message': {'id': 'fixture-window-max', 'role': 'assistant', 'content': [{'type': 'text', 'text': 'More bounded work remains.'}]},
                                    'stream': [{'type': 'chunk', 'chunk': {'type': 'finish', 'reason': {'kind': 'max-tokens'}}}]}}})
                        rows.append({'type': 'event', 'event': {'type': 'turn/end', 'seq': len(rows), 'time': 8,
                            'data': {'turn': 3, 'reason': ({'kind': 'max-tokens'} if renew_continue else {'kind': 'completed'} if max_tokens_last_slot or expired_window else
                                {'kind': 'error', 'error': {'message': 'Synthetic explicit continuation ended.'}})}}})
                        max_tokens_ended = True
                if expired_window and model.worker_request_ready.is_set():
                    if expired_window_evidence is None:
                        current = record()['dsh_execution']
                        assert current['first_input'] == 'accepted' and current['continuation_control']['status'] == 'accepted'
                        assert current['state'] == 'awaiting_acceptance'
                        before_expiry = json.loads(budget_file.read_bytes())
                        expired_budget = json.loads(budget_file.read_bytes())
                        expired_budget['renewWindow'].update(started_at_ms=int(time.time() * 1000) - 80000,
                            stop_reason='shared-work-wall-budget')
                        budget_file.write_text(json.dumps(expired_budget))
                        history_before = list(rows)
                        rows.append({'type': 'event', 'event': {'type': 'turn/start', 'seq': len(rows), 'time': 9, 'data': {'turn': 4}}})
                        rows.append({'type': 'event', 'event': {'type': 'turn/end', 'seq': len(rows), 'time': 10,
                            'data': {'turn': 4, 'reason': {'kind': 'error', 'error': {'code': 'HOOK_ABORTED',
                                'message': 'Shared work stopped: shared-work-wall-budget'}}}}})
                        next(row for row in peer['sessions'] if row['sessionId'] == sid)['running'] = False
                        expired_window_evidence = {'before': before_expiry, 'expired': expired_budget,
                            'history_prefix': history_before, 'prompt_count_before': len(peer['prompts'])}
                        model.worker_request_release.set()
                    results = [result for batch in model.worker_tool_results for result in batch if isinstance(result, dict)]
                    stopped = [result for result in results if result.get('status') == 'budget_stopped']
                    if stopped:
                        assert stopped[-1] == {'status': 'budget_stopped', 'next_action': 'kanban_block',
                            'block_kind': 'needs_input', 'delivered': False}
                        assert json.loads(budget_file.read_bytes()) == expired_window_evidence['expired']
                        assert len(peer['prompts']) == expired_window_evidence['prompt_count_before'] == 3
                        assert rows[:len(expired_window_evidence['history_prefix'])] == expired_window_evidence['history_prefix']
                        model.repeat_supervise = False
                        while worker is not None:
                            waited, status = os.waitpid(worker['pid'], os.WNOHANG)
                            if waited:
                                assert os.waitstatus_to_exitcode(status) == 0
                                worker = None
                                break
                            await asyncio.sleep(.05)
                        latest = record()
                        assert latest['dsh_execution']['state'] == 'budget_stopped'
                        expired_window_evidence.update(result=stopped[-1], worker_exit=0, prompt_count_after=len(peer['prompts']))
                        break
                if max_tokens_ended and card['status'] in {'blocked', 'triage'}:
                    if renew_continue:
                        while True:
                            waited, status = os.waitpid(worker['pid'], os.WNOHANG)
                            if waited:
                                assert os.waitstatus_to_exitcode(status) == 0
                                break
                            await asyncio.sleep(.05)
                        worker = None
                        await receive(adapter, raw('om_window_continue', '@_user_1 继续 ' + latest['id']))
                        latest = record()
                        assert latest['dsh_execution']['continuation_control']['message_id'] == 'om_window_continue', latest['dsh_execution'].get('last_failure')
                        assert latest['dsh_execution']['continuation_control']['kind'] == 'max_tokens'
                        model.worker_steps = 0
                        with connect_closing() as db:
                            assert dispatch_once(db, max_spawn=1).spawned
                            worker = remember_worker(db, initial['card_id'])
                        window_continued = True
                        continue
                    assert peer['prompts'][2]['sessionId'] == peer['prompts'][0]['sessionId']
                    assert len({row['requestId'] for row in peer['prompts']}) == 3
                    assert latest['dsh_execution']['maintenance_checkpoint'] == checkpoint
                    if renew_window:
                        final_budget = json.loads(budget_file.read_bytes())
                        assert budget_file.read_bytes() == fixture_behavior['renew_budget_after']
                        assert final_budget['renewWindow']['hook_marker'] == 'same-sid-window-v1'
                        assert final_budget['state']['requests'] == 9 and final_budget['state']['reported_tokens'] == 145220
                        assert final_budget['state']['started_at_ms'] == old_budget['state']['started_at_ms']
                        assert final_budget['state']['stop_reason'] == old_budget['state']['stop_reason']
                        assert Path(renew_checkpoint['ledger_backup']).read_bytes() == json.dumps(old_budget).encode()
                        assert latest['dsh_execution']['terminal_reason']['kind'] == 'error'
                        assert 'override all old budget and token quota text' in peer['prompts'][2]['content'][0]['text']
                    elif max_tokens_last_slot:
                        final_budget = json.loads(budget_file.read_bytes())
                        assert budget_file.read_bytes() == fixture_behavior['last_slot_budget']
                        assert final_budget['state']['requests'] == final_budget['budget']['max_model_requests'] == 9
                        assert final_budget['state']['started_at_ms'] == json.loads(max_budget_before)['state']['started_at_ms']
                        assert latest['dsh_execution']['state'] == 'awaiting_acceptance'
                        assert latest['dsh_execution']['terminal_reason'] == {'kind': 'completed'}
                    else:
                        assert budget_file.read_bytes() == max_budget_before
                    if tokens_uncapped:
                        assert json.loads(budget_file.read_bytes())['state']['reported_tokens'] == 345200
                    await receive(adapter, raw('om_continue_max_tokens', '@_user_1 继续 ' + latest['id']))
                    await receive(adapter, raw('om_stop_max_tokens', '@_user_1 停止 ' + latest['id']))
                    latest = record()
                    assert latest['dsh_execution']['stop_requested'] and latest['dsh_execution']['state'] == 'stopped'
                    await receive(adapter, raw('om_continue_max_stopped', '@_user_1 继续 ' + latest['id']))
                    assert len(peer['prompts']) == 3
                    break
            if window_continued and len(peer['prompts']) == 4:
                latest = record()
                if latest['dsh_execution']['continuation_control']['status'] == 'accepted' and not window_continue_ended:
                    submission = peer['prompts'][3]
                    rows.append({'type': 'event', 'event': {'type': 'turn/start', 'seq': len(rows), 'time': 9, 'data': {'turn': 4}}})
                    rows.append({'type': 'event', 'event': {'type': 'user/message', 'seq': len(rows), 'time': 9,
                        'data': {'id': 'message-' + submission['requestId'], 'role': 'user', 'content': submission['content'], 'source': {'kind': 'user', 'rpcId': submission['requestId']}}}})
                    rows.append({'type': 'event', 'event': {'type': 'request/header', 'seq': len(rows), 'time': 9,
                        'data': {'header': {'config': {'provider': 'fixture', 'model': 'fixture'}}}}})
                    rows.append({'type': 'event', 'event': {'type': 'assistant/message', 'seq': len(rows), 'time': 9,
                        'data': {'turn': 4, 'step': 0, 'usage': {'inputTokens': 12, 'outputTokens': 8},
                            'message': {'id': 'fixture-window-max-again', 'role': 'assistant', 'content': [{'type': 'text', 'text': 'Bounded fixture remains unfinished.'}]},
                            'stream': [{'type': 'chunk', 'chunk': {'type': 'finish', 'reason': {'kind': 'max-tokens'}}}]}}})
                    rows.append({'type': 'event', 'event': {'type': 'turn/end', 'seq': len(rows), 'time': 10,
                        'data': {'turn': 4, 'reason': {'kind': 'max-tokens'}}}})
                    window_continue_ended = True
                if window_continue_ended and card['status'] in {'blocked', 'triage'}:
                    assert latest['dsh_execution']['state'] == 'execution_failed'
                    assert len({row['requestId'] for row in peer['prompts']}) == 4
                    after = budget_file.read_bytes()
                    current_budget = json.loads(after)
                    assert current_budget['state']['requests'] == 10 and current_budget['state']['reported_tokens'] == 145240
                    assert current_budget['state']['started_at_ms'] == old_budget['state']['started_at_ms']
                    assert current_budget['state']['stop_reason'] == old_budget['state']['stop_reason']
                    await receive(adapter, raw('om_window_continue', '@_user_1 继续 ' + latest['id']))
                    assert len(peer['prompts']) == 4 and budget_file.read_bytes() == after
                    while True:
                        waited, status = os.waitpid(worker['pid'], os.WNOHANG)
                        if waited:
                            assert os.waitstatus_to_exitcode(status) == 0
                            break
                        await asyncio.sleep(.05)
                    worker = None
                    for label, changed in [('requests', {**current_budget, 'state': {**current_budget['state'], 'requests': 88}}),
                            ('wall', {**current_budget, 'renewWindow': {**current_budget['renewWindow'], 'started_at_ms': int(time.time() * 1000) - 61000}}),
                            ('stop', {**current_budget, 'renewWindow': {**current_budget['renewWindow'], 'stop_reason': 'shared-work-wall-budget'}})]:
                        budget_file.write_text(json.dumps(changed))
                        before = budget_file.read_bytes()
                        await receive(adapter, raw('om_window_exhausted_' + label, '@_user_1 继续 ' + latest['id']))
                        assert record()['dsh_execution']['continuation_control']['message_id'] == 'om_window_continue'
                        assert len(peer['prompts']) == 4 and budget_file.read_bytes() == before
                        budget_file.write_bytes(after)
                    assert 'override all old budget and token quota text' in peer['prompts'][3]['content'][0]['text']
                    break
            if shared_scenario == 'reconcile_intent' and not reconciled and card['status'] == 'blocked':
                assert latest['dsh_execution']['native_permission'] == 'intent'
                assert not peer['prompts']
                original_binding = {key: latest['dsh_execution'][key] for key in ('session_id', 'generation', 'request_id')}
                while True:
                    waited, status = os.waitpid(worker['pid'], os.WNOHANG)
                    if waited:
                        assert os.waitstatus_to_exitcode(status) == 0
                        break
                    await asyncio.sleep(.05)
                assert 'error' not in json.loads(registry.dispatch('kanban_unblock', {'task_id': initial['card_id']}))
                model.worker_steps = 1
                with connect_closing() as db:
                    assert dispatch_once(db, max_spawn=1).spawned
                    worker = remember_worker(db, initial['card_id'])
                reconciled = True
            elif shared_scenario == 'reconcile_intent' and reconciled and card['status'] == 'blocked' and not peer['prompts']:
                raise AssertionError('Same-card permission initialization could not resume: ' + json.dumps(latest['dsh_execution']))
            if shared_scenario == 'stop' and latest['dsh_execution'].get('state') == 'stopped':
                break
            if shared_scenario == 'reconcile_intent' and latest['dsh_execution'].get('acceptance'):
                break
            if (shared_scenario in {'terminal', 'reconcile_intent'} and latest['dsh_execution'].get('acceptance') and card['status'] == 'blocked'
                    and any(message.message_id == 'om_shared' and '验收未满足' in message.request_body.content for message in sent)):
                break
            await asyncio.sleep(.05)
        else:
            raise AssertionError('Original public shared worker did not settle: ' + json.dumps(record()))
        assert len(peer['prompts']) == (6 if owner_maintenance else 5 if owner_context else 1 if supervise_slice else 4 if renew_continue else 3 if max_tokens_continuation else 2 if shared_scenario in continuation_scenarios or completed_renewed_window else 1)
        assert latest['dsh_execution']['native_identity']['kind'] == 'shared_native'
        assert latest['dsh_execution']['native_identity']['pid'] == (os.getpid() if supervise_takeover else host_processes[-1].pid if host_processes else os.getpid())
        assert latest['dsh_execution']['native_permission'] == 'workspace-write'
        if runtime_metadata:
            assert latest['dsh_execution']['runtime_versions']['source_python']['sha256'] == hashlib.sha256(Path(sys.executable).resolve().read_bytes()).hexdigest()
            assert latest['dsh_execution']['runtime_versions']['node']['sha256'] != '0' * 64
        assert latest['dsh_execution']['workspace_id'] == peer['workspace']['workspaceId']
        assert peer['workspace']['path'] == str(repo) and peer['workspace']['sessionIds'] == [latest['dsh_execution']['session_id']]
        assert sum(call['method'] == 'workspace/create' and call['payload']['args']['request']['path'] == str(repo)
                   for call in peer['calls']) >= 1
        if shared_scenario == 'reconcile_intent':
            assert original_binding == {key: latest['dsh_execution'][key] for key in original_binding}
            assert len([call for call in peer['calls'] if call['method'] == 'commands/execute']) == 1
            assert len([call for call in peer['calls'] if call['method'] == 'session/create']) == 1
            assert latest['dsh_execution']['supervisor_claim']['run_id'] != latest['dsh_execution']['permission_initialization_claim']['run_id']
        if shared_scenario in {'terminal', 'reconcile_intent'}:
            assert latest['dsh_execution']['acceptance']['status'] == 'unmet'
            if shared_scenario == 'terminal':
                assert any(message.message_id == 'om_shared' and '验收未满足' in message.request_body.content for message in sent)
        elif shared_scenario == 'stop':
            assert latest['dsh_execution']['stop_control']['proof']['session']['scope'] == 'shared_session'
            assert latest['dsh_execution']['stop_control']['proof']['session']['host_shutdown'] is False
            assert any(message.message_id == 'om_stop' and '停止已核实' in message.request_body.content for message in sent)
            assert len([call for call in peer['calls'] if call['method'] == 'session/cancel']) == 1
        if supervise_slice and takeover_rejection is None:
            assert latest['dsh_execution']['state'] == 'budget_stopped'
            assert observer_ready and supervise_first and supervise_terminal and supervise_ledger
        assert peer['sessions'][0]['sessionId'] == 'session-fixture' and peer['sessions'][0]['running'] is False
        assert not (scratch / 'state/owned-work').exists(), 'No dedicated Home or runtime may be created.'
        result = {'status': 'passed', 'scope': 'original_Hermes_worker_external_DSH_fixture',
            'single_native_session': latest['dsh_execution']['session_id'], 'input_count': len(peer['prompts'])}
        if supervise_slice and takeover_rejection is None:
            result.update(observer_ready=True, observer={key: observer_ready[key] for key in
                ('status', 'session_id', 'generation', 'host_lease', 'client_id')},
                first_result=supervise_first, second_result=supervise_result, prompt_count=len(peer['prompts']),
                ledger_state=supervise_ledger, ledger_before=supervise_ledger_before, ledger_after=final_ledger,
                history_prefix=supervise_history_before, terminal=supervise_terminal, worker_exit=0)
            if supervise_takeover:
                result['takeover'] = takeover
                result['preflight_runs'] = preflight_runs
                result['new_claim'] = latest['dsh_execution']['supervisor_claim']
        if renew_window_again:
            result['repeat_window'] = repeat_window_evidence
        if expired_window:
            result['expired_window'] = expired_window_evidence
        if qa_preinput_retry:
            result['qa_preinput_retry'] = qa_retry_evidence
        if supervision_checkpoints:
            result['supervision_checkpoints'] = checkpoint_evidence
        if takeover_rejection is not None:
            result.update(case=takeover_case, rejection=takeover_rejection, takeover=takeover,
                retained_claim=latest['dsh_execution']['supervisor_claim'],
                last_failure=latest['dsh_execution'].get('last_failure'),
                prompt_count=len(peer['prompts']),
                worker_exit=0, preflight_runs=preflight_runs)
        if concurrent_claim:
            result['concurrent_claim'] = claim_result
        (scratch / 'shared-worker-result.json').write_text(json.dumps(result))
    except BaseException:
        if supervise_slice or owner_context or qa_preinput_retry or supervision_checkpoints or shared_scenario == 'question_material':
            import traceback
            (scratch / 'supervise-failure.json').write_text(json.dumps({
                'traceback': traceback.format_exc(), 'public_work': record(),
                'model_requests': model.requests, 'tool_results': model.worker_tool_results,
                'journals': peer['journals'],
                'preflight_runs': preflight_runs,
                'prompts': peer['prompts']}))
        raise
    finally:
        model.worker_request_release.set()
        if (supervise_slice or expired_window or qa_preinput_retry or supervision_checkpoints or shared_scenario == 'question_material') and worker is not None:
            # Keep both services alive through the original scoped native cleanup.
            from simple_worker_evidence import cleanup_worker
            cleanup = await asyncio.to_thread(cleanup_worker, worker, scratch / 'home')
            (scratch / 'supervise-cleanup.json').write_text(json.dumps(cleanup))
            worker = None
        for process in host_processes:
            if process.poll() is None:
                process.terminate()
            process.wait(timeout=5)
        plugins.unload('ghost-hermes-pm')
        await runner.stop()
        if worker is not None:
            until = time.monotonic() + 15
            while time.monotonic() < until:
                try:
                    waited, status = os.waitpid(worker['pid'], os.WNOHANG)
                except ChildProcessError:
                    # The public stop verifier has already collected and checked
                    # this same child's native zero exit receipt.
                    assert shared_scenario in {'stop', 'continue_restart'} and latest['dsh_execution']['stop_control']['proof']['worker']['exit_code'] == 0
                    break
                if waited:
                    assert os.waitstatus_to_exitcode(status) == 0
                    break
                await asyncio.sleep(.05)
            else:
                raise AssertionError('Original worker exit remained unconfirmed.')
        model.close()
        peer_context.__exit__(None, None, None)
        namespace['flush_log_queue']()


asyncio.run(main())
