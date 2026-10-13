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
supervise_takeover = shared_scenario.startswith('supervise_takeover')
takeover_case = shared_scenario.removeprefix('supervise_takeover_') if shared_scenario != 'supervise_takeover' else 'valid'
supervise_slice = shared_scenario == 'supervise_slice' or supervise_takeover
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
owner_context = shared_scenario == 'owner_context'
renew_window_again = shared_scenario == 'renew_window_again' or owner_context
completed_renewed_window = resume_completed_window or renew_window_again
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
    "'values': {'agentPreset': behavior.get('agent_preset', 'fixture-shared'), 'userQuestions': {'active': [], 'settled': []}, "
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
    'budget': {'max_wall_seconds': 60, 'max_model_requests': 9 if max_tokens_last_slot else 80}, 'tool_receipt_ref': str(proof),
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
    try:
        material_cases = []
        material_observations = []
        material_pending = None
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
        await receive(adapter, raw('om_shared'))
        def record():
            return json.loads(registry.dispatch('hermes_pm_snapshot', {}))['work'][0]
        service_namespace['record'] = record
        initial = record()
        assert initial['dsh_execution']['mode'] == 'shared_native', initial
        if triage_continuation:
            assert 'error' not in json.loads(registry.dispatch('kanban_block', {
                'task_id': initial['card_id'], 'kind': 'needs_input', 'reason': 'Original failure still needs explicit continuation.'}))
            assert 'error' not in json.loads(registry.dispatch('kanban_unblock', {'task_id': initial['card_id']}))
        takeover = None
        preflight_runs = []
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
        takeover_rejection = None
        old_ledger_path = old_ledger = old_history = changed_ledger = None
        supervise_first = None
        supervise_terminal = None
        supervise_ledger = None
        while time.monotonic() < deadline:
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
                started = int(time.time() * 1000) - (61000 if renew_window_again else 1000)
                base_requests = 4 if renew_window_again else 3
                total_requests = 11 if renew_window_again else 9
                original_state = {'started_at_ms': started, 'requests': base_requests,
                                  'reported_tokens': 700 if renew_window_again else 30,
                                  'admissions': ['fixture-old-' + str(index) for index in range(base_requests)], 'stop_reason': None}
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
                    rows.append({'type': 'event', 'event': {'type': 'assistant/message', 'seq': len(rows), 'time': 4,
                        'data': {'turn': 1, 'step': index, 'usage': {'inputTokens': 10, 'outputTokens': 5},
                            'message': {'id': 'fixture-settlement-' + str(index), 'role': 'assistant',
                                'content': [{'type': 'text', 'text': 'Synthetic bounded work.'}]}, 'stream': []}}})
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
                receipt_rows = [{'schema': 1, 'generation': budget_row['generation'], 'session_id': sid,
                    'session_created_at': budget_row['session_created_at'], 'service_ref': 'fixture-native',
                    'host_lease': 'fixture-shared-lease', 'call_id': 'fixture-source-' + str(index), 'name': 'bash',
                    'kind': 'foreground', 'execution_admission': 'admitted', 'is_error': False, 'exit_code': 0,
                    'timed_out': False, 'aborted': False, 'native_session_seq': index + 1, 'source_before': source,
                    'source_after': source} for index in range(3)] + [{'schema': 1, 'generation': budget_row['generation'], 'session_id': sid,
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
                    'data': {'turn': 1, 'reason': {'kind': 'completed'}}}})
                next(row for row in peer['sessions'] if row['sessionId'] == sid)['running'] = False
                ended = True
            latest = record()
            if supervise_slice:
                tool_results = [result for batch in getattr(model, 'worker_tool_results', []) for result in batch
                                if isinstance(result, dict)]
                running_results = [result for result in tool_results
                                   if result.get('status') == 'running' and result.get('next_action') == 'hermes_pm_supervise']
                stopped_results = [result for result in tool_results
                                   if result.get('status') == 'budget_stopped']
                rejected_results = [result for result in tool_results if result.get('status') == 'unverified']
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
                    if question and question.get('notification') == 'delivered':
                        delivered = next(message for index, message in enumerate(sent, 1)
                            if 'om_sent_' + str(index) == question['delivery']['message_id'])
                        material_observations.append((question, delivered.request_body.content))
                        material_pending = None
                if material_pending is None and len(material_observations) < len(material_cases):
                    label, reference_text, _ = material_cases[len(material_observations)]
                    material_pending = len(rows)
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
                            ('assistant/message', {'turn': 5, 'step': 1, 'usage': {'inputTokens': 10, 'outputTokens': 5},
                                'message': {'id': 'fixture-owner-settlement', 'role': 'assistant', 'content': [
                                    {'type': 'text', 'text': 'Synthetic owner context has been read.'}]}, 'stream': []}),
                            ('turn/end', {'turn': 5, 'reason': {'kind': 'completed'}})]:
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
                    assert len(peer['prompts']) == 5 and sum(call['method'] == 'session/create' for call in peer['calls']) == creates
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
        assert len(peer['prompts']) == (5 if owner_context else 1 if supervise_slice else 4 if renew_continue else 3 if max_tokens_continuation else 2 if shared_scenario in continuation_scenarios or completed_renewed_window else 1)
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
        if supervise_slice or owner_context:
            import traceback
            (scratch / 'supervise-failure.json').write_text(json.dumps({
                'traceback': traceback.format_exc(), 'public_work': record(),
                'model_requests': model.requests, 'tool_results': model.worker_tool_results,
                'preflight_runs': preflight_runs,
                'prompts': peer['prompts']}))
        raise
    finally:
        model.worker_request_release.set()
        if (supervise_slice or expired_window) and worker is not None:
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
