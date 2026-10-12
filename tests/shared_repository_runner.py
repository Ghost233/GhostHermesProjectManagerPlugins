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
triage_continuation = shared_scenario == 'continue_triage'
concurrent_claim = shared_scenario == 'continue_claim_race'
if triage_continuation or concurrent_claim:
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
service_source = service_source.replace("state['journals'].setdefault(value['sessionId'], list(behavior.get('records', [])))", """state['journals'].setdefault(value['sessionId'], list(behavior.get('records', [])))
                if 'workspaceId' in args['request']:
                    assert 'cwd' not in args['request'] and args['request']['workspaceId'] == state['workspace']['workspaceId']
                    state['workspace']['sessionIds'].append(value['sessionId'])""")
service_source = service_source.replace("'values': {'inbox': behavior.get('inbox', {'next-turn': [], 'next-step': []})}",
    "'values': {'agentPreset': behavior.get('agent_preset', 'fixture-shared'), 'userQuestions': {'active': [], 'settled': []}, "
    "'permissions': {'currentValue': 'workspace-write'}, "
    "'inbox': behavior.get('inbox', {'next-turn': [], 'next-step': []})}")
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
                behavior['inbox'] = {'next-turn': [], 'next-step': []}""")
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
    "value = None if method == 'session/prompt' and shared_scenario == 'continue_unknown' and len(state['prompts']) == 2 else {'accepted': True}")
service_source = service_source.replace("'createdAt': 0,", "'createdAt': behavior.get('created_at', 0),")
service_source = service_source.replace("elif method == 'session/cancel':\n                value", """elif method == 'session/cancel':
                sid = args['request']['sessionId']
                row = next(row for row in state['sessions'] if row['sessionId'] == sid)
                row['running'] = False
                journal = state['journals'][sid]
                journal.append({'type': 'event', 'event': {'type': 'turn/end', 'seq': len(journal),
                    'time': 4, 'data': {'turn': 1, 'reason': {'kind': 'aborted', 'reason': {'kind': 'user'}}}}})
                value""")
service_namespace = {'permission_contract': permission_contract, 'shared_scenario': shared_scenario}
exec(compile(service_source, str(ROOT / 'tests/dsh_fixture_server.py'), 'exec'), service_namespace)
fixture_behavior = {'admit_prompt': True}
peer_context = service_namespace['remote_peer'](behavior=fixture_behavior)
url, peer = peer_context.__enter__()
shared_address = ('127.0.0.1', int(url.rsplit(':', 1)[1]))
receipt = scratch / 'desktop-host.json'
receipt.write_text(json.dumps({'schema': 1, 'lease': 'fixture-shared-lease', 'pid': host_processes[0].pid if host_processes else os.getpid(),
    'url': url + '/?token=fixture-launch', 'runtimeVersion': 'fixture', 'permissionMode': 'danger-full-access'}))
receipt.chmod(0o600)
ledger = scratch / 'ledger'
ledger.mkdir(mode=0o700)
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
    'budget': {'max_wall_seconds': 60, 'max_model_requests': 80, 'max_reported_tokens': 300000,
        'max_output_tokens_per_request': 8192}, 'tool_receipt_ref': str(proof),
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
        if shared_scenario in continuation_scenarios:
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
        deadline = time.monotonic() + 35
        ended = False
        stop_sent = False
        reconciled = False
        original_binding = None
        continued = False
        continuation_ended = False
        continuation_started = False
        continuation_seen_at = None
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
            latest = record()
            if shared_scenario == 'stop' and latest['dsh_execution'].get('first_input') == 'accepted' and not stop_sent:
                stop_sent = True
                await receive(adapter, raw('om_stop', '@_user_1 停止 ' + latest['id']))
                latest = record()
            card = json.loads(registry.dispatch('kanban_show', {'task_id': initial['card_id']}))['task']
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
            if shared_scenario == 'continue_restart' and continued:
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
                            rows.append({'type': 'event', 'event': {'type': 'turn/end', 'seq': len(rows),
                                'time': 6, 'data': {'turn': 2, 'reason': {'kind': 'error',
                                    'error': {'message': 'Synthetic continuation ended.', 'code': 'UNKNOWN'}}}}})
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
                    await receive(adapter, raw('om_continue_after_restart', '@_user_1 继续 ' + latest['id']))
                    await receive(adapter, raw('om_continue_already_accepted', '@_user_1 继续 ' + latest['id']))
                    assert len(peer['prompts']) == 2
                    await receive(adapter, raw('om_stop_continued', '@_user_1 停止 ' + latest['id']))
                    latest = record()
                    assert latest['dsh_execution']['stop_requested'] and latest['dsh_execution']['state'] == 'stopped'
                    await receive(adapter, raw('om_continue_stopped', '@_user_1 继续 ' + latest['id']))
                    assert len(peer['prompts']) == 2
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
        assert len(peer['prompts']) == (2 if shared_scenario in continuation_scenarios else 1)
        assert latest['dsh_execution']['native_identity']['kind'] == 'shared_native'
        assert latest['dsh_execution']['native_identity']['pid'] == (host_processes[-1].pid if host_processes else os.getpid())
        assert latest['dsh_execution']['native_permission'] == 'workspace-write'
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
