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
    "'values': {'agentPreset': 'fixture-shared', 'userQuestions': {'active': [], 'settled': []}, "
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
peer_context = service_namespace['remote_peer'](behavior={'admit_prompt': True})
url, peer = peer_context.__enter__()
shared_address = ('127.0.0.1', int(url.rsplit(':', 1)[1]))
receipt = scratch / 'desktop-host.json'
receipt.write_text(json.dumps({'schema': 1, 'lease': 'fixture-shared-lease', 'pid': os.getpid(),
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
        await connect_service(adapter, service)
        await receive(adapter, raw('om_shared'))
        def record():
            return json.loads(registry.dispatch('hermes_pm_snapshot', {}))['work'][0]
        initial = record()
        assert initial['dsh_execution']['mode'] == 'shared_native', initial
        with connect_closing() as db:
            assert dispatch_once(db, max_spawn=1).spawned
            worker = remember_worker(db, initial['card_id'])
        deadline = time.monotonic() + 35
        ended = False
        stop_sent = False
        reconciled = False
        original_binding = None
        while time.monotonic() < deadline:
            if peer['prompts'] and not ended and shared_scenario in {'terminal', 'reconcile_intent'}:
                sid = peer['prompts'][0]['sessionId']
                rows = peer['journals'][sid]
                rows.append({'type': 'event', 'event': {'type': 'turn/end', 'seq': len(rows),
                    'time': 4, 'data': {'turn': 1, 'reason': {'kind': 'completed'}}}})
                next(row for row in peer['sessions'] if row['sessionId'] == sid)['running'] = False
                ended = True
            latest = record()
            if shared_scenario == 'stop' and latest['dsh_execution'].get('first_input') == 'accepted' and not stop_sent:
                stop_sent = True
                await receive(adapter, raw('om_stop', '@_user_1 停止 ' + latest['id']))
                latest = record()
            card = json.loads(registry.dispatch('kanban_show', {'task_id': initial['card_id']}))['task']
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
        assert len(peer['prompts']) == 1
        assert latest['dsh_execution']['native_identity']['kind'] == 'shared_native'
        assert latest['dsh_execution']['native_identity']['pid'] == os.getpid()
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
        else:
            assert latest['dsh_execution']['stop_control']['proof']['session']['scope'] == 'shared_session'
            assert latest['dsh_execution']['stop_control']['proof']['session']['host_shutdown'] is False
            assert any(message.message_id == 'om_stop' and '停止已核实' in message.request_body.content for message in sent)
            assert len([call for call in peer['calls'] if call['method'] == 'session/cancel']) == 1
        assert peer['sessions'][0]['sessionId'] == 'session-fixture' and peer['sessions'][0]['running'] is False
        assert not (scratch / 'state/owned-work').exists(), 'No dedicated Home or runtime may be created.'
        result = {'status': 'passed', 'scope': 'original_Hermes_worker_external_DSH_fixture',
            'single_native_session': latest['dsh_execution']['session_id'], 'input_count': len(peer['prompts'])}
        (scratch / 'shared-worker-result.json').write_text(json.dumps(result))
    finally:
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
                    assert shared_scenario == 'stop' and latest['dsh_execution']['stop_control']['proof']['worker']['exit_code'] == 0
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
