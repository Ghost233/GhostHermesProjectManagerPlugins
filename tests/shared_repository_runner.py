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
runtime_metadata = shared_scenario == 'runtime_metadata'
if runtime_metadata:
    shared_scenario = 'terminal'
triage_continuation = shared_scenario == 'continue_triage'
concurrent_claim = shared_scenario == 'continue_claim_race'
native_restore = shared_scenario == 'continue_native_restore'
renew_continue = shared_scenario == 'renew_window_continue'
resume_completed_window = shared_scenario == 'resume_completed_window'
renew_window_again = shared_scenario == 'renew_window_again'
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
                        or (renew_window_again and len(state['prompts']) >= 2)):
                    budget_path = ledger / (hashlib.sha256(args['request']['sessionId'].encode()).hexdigest() + '.json')
                    budget_row = json.loads(budget_path.read_bytes())
                    budget_row['renewWindow'].update(started_at_ms=int(time.time() * 1000), hook_marker='same-sid-window-v1')
                    budget_row['state']['requests'] += 1
                    budget_row['state']['reported_tokens'] += 20
                    budget_row['state']['admissions'].append(args['request']['requestId'])
                    budget_path.write_text(json.dumps(budget_row))
                    behavior['renew_budget_after'] = budget_path.read_bytes()""")
service_source = service_source.replace("if behavior.get('admit_prompt'):",
    """if shared_scenario in {'continue_restart', 'continue_unknown'} and len(state['prompts']) == 1:
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
        initial = record()
        assert initial['dsh_execution']['mode'] == 'shared_native', initial
        if triage_continuation:
            assert 'error' not in json.loads(registry.dispatch('kanban_block', {
                'task_id': initial['card_id'], 'kind': 'needs_input', 'reason': 'Original failure still needs explicit continuation.'}))
            assert 'error' not in json.loads(registry.dispatch('kanban_unblock', {'task_id': initial['card_id']}))
        with connect_closing() as db:
            assert dispatch_once(db, max_spawn=1).spawned
            worker = remember_worker(db, initial['card_id'])
        deadline = time.monotonic() + (60 if runtime_metadata or native_restore or renew_window or renew_window_again else 35)
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
                rows.append({'type': 'event', 'event': {'type': 'turn/end', 'seq': len(rows), 'time': 6,
                    'data': {'turn': 2, 'reason': {'kind': 'error', 'error': {'code': 'UNKNOWN', 'message': 'Synthetic renewed terminal.'}}}}})
                next(row for row in peer['sessions'] if row['sessionId'] == sid)['running'] = False
                renewal_ended = True
            if renew_window_again and renewal_ended and card['status'] in {'blocked', 'triage'}:
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
                            'data': {'turn': 3, 'reason': ({'kind': 'max-tokens'} if renew_continue else {'kind': 'completed'} if max_tokens_last_slot else
                                {'kind': 'error', 'error': {'message': 'Synthetic explicit continuation ended.'}})}}})
                        max_tokens_ended = True
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
        assert len(peer['prompts']) == (4 if renew_continue else 3 if max_tokens_continuation else 2 if shared_scenario in continuation_scenarios or completed_renewed_window else 1)
        assert latest['dsh_execution']['native_identity']['kind'] == 'shared_native'
        assert latest['dsh_execution']['native_identity']['pid'] == (host_processes[-1].pid if host_processes else os.getpid())
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
        assert peer['sessions'][0]['sessionId'] == 'session-fixture' and peer['sessions'][0]['running'] is False
        assert not (scratch / 'state/owned-work').exists(), 'No dedicated Home or runtime may be created.'
        result = {'status': 'passed', 'scope': 'original_Hermes_worker_external_DSH_fixture',
            'single_native_session': latest['dsh_execution']['session_id'], 'input_count': len(peer['prompts'])}
        if concurrent_claim:
            result['concurrent_claim'] = claim_result
        (scratch / 'shared-worker-result.json').write_text(json.dumps(result))
    finally:
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
