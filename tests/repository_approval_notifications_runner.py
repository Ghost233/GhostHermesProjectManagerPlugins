"""Original platform and worker; only model, Feishu, GitHub and UI protocol boundaries vary."""
import asyncio
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
from pathlib import Path
import sys
import threading

scratch = Path(sys.argv[1]).resolve()
outcome = sys.argv[2]
reply_mode = sys.argv[3]
ROOT = Path(__file__).resolve().parents[1]


class ApprovalModelService:
    def __init__(self, *_args, **_kwargs):
        self.worker_steps = self.dsh_steps = 0
        self.ui_release = False
        owner = self
        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):
                self.send_response(200)
                self.send_header('Content-Type', 'application/json')
                self.end_headers()
                self.wfile.write(json.dumps({'decision': outcome if owner.ui_release else None} if self.path.endswith('/native-ui-decision')
                                           else {} if self.path == '/models-dev' else {'data': [{'id': 'fixture-model'}]}).encode())
            def do_CONNECT(self):
                self.send_error(502, 'Only fixture local external services are available.')
            def do_POST(self):
                payload = json.loads(self.rfile.read(int(self.headers['Content-Length'])))
                worker = payload['model'] == 'fixture-model'
                if worker:
                    owner.worker_steps += 1
                    call = ('hermes_pm_supervise', {}) if owner.worker_steps == 1 else (
                        ('kanban_block', {'kind': 'needs_input', 'reason': 'The original approval work awaits acceptance.'}) if owner.worker_steps == 2 else None)
                    call_id = 'outer-' + str(owner.worker_steps)
                else:
                    owner.dsh_steps += 1
                    call = ('bash', {'command': "printf 'approved\\n' > approval-result.txt",
                        'description': 'Write one synthetic repository file.', 'sandbox_permissions': 'workspace-write',
                        'justification': 'Write this exact synthetic file inside the bound repository.'}) if owner.dsh_steps == 1 else None
                    call_id = 'original-approval-operation'
                delta = {'tool_calls': [{'index': 0, 'id': call_id, 'type': 'function',
                    'function': {'name': call[0], 'arguments': json.dumps(call[1])}}]} if call else {'content': 'Original operation settled.'}
                self.send_response(200)
                self.send_header('Content-Type', 'text/event-stream')
                self.end_headers()
                for part, reason in (({'role': 'assistant'}, None), (delta, None), ({}, 'tool_calls' if call else 'stop')):
                    self.wfile.write(('data: ' + json.dumps({'id': 'approval-stream', 'object': 'chat.completion.chunk',
                        'created': 1, 'model': payload['model'], 'choices': [{'index': 0, 'delta': part,
                        'finish_reason': reason}]}) + '\n\n').encode())
                self.wfile.write(b'data: [DONE]\n\n')
            def log_message(self, *_):
                pass
        self.server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.base_url = f'http://127.0.0.1:{self.server.server_port}/v1'
    def close(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(2)


fixture = ROOT / 'tests/simple_intake_smoke_runner.py'
prefix = fixture.read_text().split('async def main():', 1)[0]
prefix = prefix.replace('from simple_worker_model import WorkerModelService, execution_reference',
                        'from simple_worker_model import execution_reference')
sys.argv = [str(fixture), str(scratch), 'worker_dispatch']
namespace = {'__name__': '__approval_fixture__', '__file__': str(fixture), 'WorkerModelService': ApprovalModelService}
exec(compile(prefix, str(fixture), 'exec'), namespace)
os, subprocess, types = (namespace[name] for name in ('os', 'subprocess', 'types'))
GatewayRunner, GatewayConfig, Platform, PlatformConfig = (namespace[name] for name in ('GatewayRunner', 'GatewayConfig', 'Platform', 'PlatformConfig'))
service_client, connect_service, receive, raw = (namespace[name] for name in ('service_client', 'connect_service', 'receive', 'raw'))
registry, plugins, ReplyMessageResponse, model, repo, state = (namespace[name] for name in ('registry', 'plugins', 'ReplyMessageResponse', 'model', 'repo', 'state'))
flush_log_queue = namespace['flush_log_queue']
from hermes_cli.auth import PROVIDER_REGISTRY
from hermes_cli.providers import HERMES_OVERLAYS
from agent.models_dev import PROVIDER_TO_MODELS_DEV
configuration_path = scratch / 'home/config.yaml'
fixture_configuration = namespace['yaml'].safe_load(configuration_path.read_text())
fixture_configuration['model_catalog'] = {'excluded_providers': sorted({
    *PROVIDER_REGISTRY, *HERMES_OVERLAYS, *PROVIDER_TO_MODELS_DEV, *PROVIDER_TO_MODELS_DEV.values(), 'custom'})}
fixture_configuration['models_dev'] = {'url': model.base_url.rsplit('/v1', 1)[0] + '/models-dev'}
configuration_path.write_text(namespace['yaml'].safe_dump(fixture_configuration))
os.environ.update(HTTP_PROXY=model.base_url, HTTPS_PROXY=model.base_url, ALL_PROXY=model.base_url,
                  NO_PROXY='127.0.0.1,localhost')
os.environ['SYNTHETIC_GH_BODY'] = ('Write approval-result.txt containing approved followed by one newline. '
    'Use the original specific-operation approval if the standing repository policy is read-only. '
    'After the original tool settles, report its actual outcome and wait for outer acceptance.')


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
    binding = adapter.bindings[0]
    runner.adapters[platform] = adapter
    runner._wire_adapter_handlers(adapter)
    native = service_client('cli_fixture')
    native.request = lambda request: types.SimpleNamespace(code=0, raw=types.SimpleNamespace(
        content=b'{"code":0,"bot":{"open_id":"ou_lead","activate_status":2}}'))
    sent = []
    updated = []
    def reply(request):
        sent.append(request)
        return ReplyMessageResponse({'code': 0, 'data': {'message_id': 'om_sent_' + str(len(sent)),
            'chat_id': 'oc_fixture', 'parent_id': request.message_id}})
    native.im.v1.message.reply = reply
    def patch(request):
        updated.append(request)
        from lark_oapi.api.im.v1 import PatchMessageResponse
        return PatchMessageResponse({'code': 0})
    native.im.v1.message.patch = patch
    worker = carrier = None
    passed = False
    try:
        await connect_service(adapter, native)
        await receive(adapter, raw('om_work'))
        def record():
            return json.loads(registry.dispatch('hermes_pm_snapshot', {}))['work'][0]
        original = record()
        instance = state / 'owned-work' / original['id']
        prepared = subprocess.run(['/opt/homebrew/Cellar/node/26.9.0/bin/node',
            str(ROOT / 'tests/repository_approval_prepare.mjs'), os.environ['DSH_TEST_SDK_ROOT'],
            str(instance / 'home'), model.base_url + '/native-ui-decision',
            str(ROOT / 'tests/repository_approval_fixture_service.mjs')], capture_output=True, text=True)
        assert prepared.returncode == 0, prepared.stdout + prepared.stderr
        with connect_closing() as db:
            assert dispatch_once(db, max_spawn=1).spawned
            worker = remember_worker(db, original['card_id'])
        async def wait_for(check):
            for _ in range(1400):
                value = check()
                if value:
                    return value
                await asyncio.sleep(.025)
            raise AssertionError('Original approval public boundary timed out: ' + json.dumps(record()['dsh_execution']))
        notice = await wait_for(lambda: next((n for n in record()['dsh_execution'].get('approvals', [])
                                              if n.get('request_message_id')), None))
        execution = record()['dsh_execution']
        assert notice['call_id'] == 'original-approval-operation' and notice['state'] == 'pending'
        assert notice['work_id'] == original['id'] and notice['card_id'] == original['card_id']
        assert notice['generation'] == execution['generation'] and notice['session_id'] == execution['session_id']
        request_message = next(message for message in sent if '审批 ID：' + notice['approval_id'] in message.request_body.content)
        assert request_message.request_body.msg_type == 'interactive'
        request_card = json.loads(request_message.request_body.content)
        request_text = '\n'.join(element['text']['content'] for element in request_card['elements'] if 'text' in element)
        assert "printf 'approved\\n' > approval-result.txt" in request_text
        assert 'workspace-write' in request_text
        assert execution['native_identity']['generation'] == execution['generation']
        import importlib
        supervision = importlib.import_module(type(adapter.intake).__module__.rsplit('.', 1)[0] + '.repository_supervision')
        carrier = supervision.attach_existing_owned(adapter.intake, record())
        events, before_projections = await asyncio.to_thread(supervision.original_history, carrier, execution['session_id'], str(repo))
        standing_before = before_projections['permissions']
        baseline_seq = events[-1]['seq']
        assert standing_before == {'currentValue': 'read-only'}
        assert not any(e['type'] == 'approval/decided' for e in events)
        await receive(adapter, raw('om_group_agree', '@_user_1 同意', parent_id=notice['request_message_id']))
        assert '群内答复没有改变权限' in sent[-1].request_body.content
        events, _ = await asyncio.to_thread(supervision.original_history, carrier, execution['session_id'], str(repo))
        assert not any(e['type'] == 'approval/decided' for e in events)
        for mid, text, parent, sender in (
            ('om_old_task_quote', '批准一次', 'om_work', None),
            ('om_old_approval_id', '批准一次 审批 old-fixture', None, None),
            ('om_other_person', '批准一次', notice['request_message_id'],
             {'sender_id': {'open_id': 'ou_other', 'user_id': 'u_other', 'union_id': 'on_other'}}),
            ('om_bot_reply', '批准一次', notice['request_message_id'], {'sender_type': 'app'}),
            ('om_permanent', '批准永久', notice['request_message_id'], None),
        ):
            await receive(adapter, raw(mid, '@_user_1 ' + text, parent_id=parent, sender=sender))
        events, _ = await asyncio.to_thread(supervision.original_history, carrier, execution['session_id'], str(repo))
        assert not any(e['type'] == 'approval/decided' for e in events)
        def card_payload(event_id, value):
            return {'schema': '2.0', 'header': {'event_type': 'card.action.trigger', 'event_id': event_id,
                'app_id': binding['app_id'], 'tenant_key': binding['transport_tenant_key']},
                'event': {'operator': {'open_id': binding['owner_open_id'], 'user_id': 'u_owner',
                    'union_id': 'on_owner', 'tenant_key': binding['sender_tenant_key']},
                    'context': {'open_message_id': notice['request_message_id'], 'open_chat_id': binding['chat_id']},
                    'action': {'tag': 'button', 'value': value}}}
        if reply_mode == 'card':
            action = next(button for element in request_card['elements'] for button in element.get('actions', [])
                          if button['value']['outcome'] == outcome)
            bad_owner = card_payload('event-card-other', action['value'])
            bad_owner['event']['operator']['open_id'] = 'ou_other'
            await adapter.receive_payload(bad_owner)
            bad_hash = card_payload('event-card-hash', {**action['value'], 'binding_sha256': 'f' * 64})
            await adapter.receive_payload(bad_hash)
            events, _ = await asyncio.to_thread(supervision.original_history, carrier, execution['session_id'], str(repo))
            assert not any(e['type'] == 'approval/decided' for e in events)
            await adapter.receive_payload(card_payload('event-card-owner', action['value']))
        elif reply_mode.startswith('lark'):
            phrase = '批准一次' if outcome == 'allowed-once' else '拒绝'
            await receive(adapter, raw('om_exact_decision', '@_user_1 ' + phrase
                + (' 审批 ' + notice['approval_id'] if reply_mode == 'lark-id' else ''),
                parent_id=None if reply_mode == 'lark-id' else notice['request_message_id']))
        else:
            model.ui_release = True
        resolved = await wait_for(lambda: next((n for n in record()['dsh_execution'].get('approvals', [])
            if n.get('outcome') == outcome and n.get('operation_state') in {'settled', 'failed'}), None))
        assert resolved['approval_id'] == notice['approval_id'] and resolved['call_id'] == notice['call_id']
        events, projections = await asyncio.to_thread(supervision.original_history, carrier, execution['session_id'], str(repo))
        assert projections['permissions'] == standing_before, 'One approval must not change the standing preset.'
        assert not any(event['seq'] > baseline_seq and event['type'] in {'sandbox/mode', 'approval/policy', 'permission/preset'}
                       for event in events), 'Only the original operation decision may change after the baseline.'
        assert (repo / 'approval-result.txt').exists() is (outcome == 'allowed-once')
        decisions = [event for event in events if event['type'] == 'approval/decided']
        assert len(decisions) == 1 and decisions[0]['data']['id'] == notice['approval_id']
        await receive(adapter, raw('om_duplicate_decision', '@_user_1 批准一次', parent_id=notice['request_message_id']))
        after, _ = await asyncio.to_thread(supervision.original_history, carrier, execution['session_id'], str(repo))
        assert [event for event in after if event['type'] == 'approval/decided'] == decisions
        if reply_mode == 'card':
            await adapter.receive_payload(card_payload('event-card-duplicate', action['value']))
            after, _ = await asyncio.to_thread(supervision.original_history, carrier, execution['session_id'], str(repo))
            assert [event for event in after if event['type'] == 'approval/decided'] == decisions
        await wait_for(lambda: next((notice for notice in record()['dsh_execution']['approvals']
            if notice['approval_id'] == resolved['approval_id'] and notice.get('result_notice') == 'delivered'
            and notice.get('result_message_id')), None))
        assert any(('允许本次操作' if outcome == 'allowed-once' else '拒绝操作') in message.request_body.content for message in sent)
        await wait_for(lambda: any('原工具：' + ('已结束' if outcome == 'allowed-once' else '失败') in request.request_body.content for request in updated))
        assert all(request.message_id == notice['request_message_id'] for request in updated)
        assert all(not any(element.get('actions') for element in json.loads(request.request_body.content)['elements']) for request in updated)
        await wait_for(lambda: record()['dsh_execution']['state'] == 'awaiting_acceptance')
        overlay = json.loads((instance / 'home/.hermes-owned-overlay.json').read_text())
        assert next(p for p in overlay if p.get('id') == 'web-runtime')['disabled'] is True
        assert next(p for p in overlay if p.get('id') == 'webserver')['disabled'] is True
        passed = True
    finally:
        plugins.unload('ghost-hermes-pm')
        await runner.stop()
        if carrier is None and adapter.intake.snapshot()['work'][0].get('dsh_execution', {}).get('native_identity'):
            import importlib
            supervision = importlib.import_module(type(adapter.intake).__module__.rsplit('.', 1)[0] + '.repository_supervision')
            carrier = supervision.attach_existing_owned(adapter.intake, adapter.intake.snapshot()['work'][0])
        if carrier is not None:
            await asyncio.to_thread(carrier.shutdown_owned)
            assert carrier.close_outcome['kind'] == 'original_exit'
        if worker is not None:
            import time
            deadline = time.monotonic() + 20
            while time.monotonic() < deadline:
                try:
                    pid, status = os.waitpid(worker['pid'], os.WNOHANG)
                except ChildProcessError:
                    break
                if pid:
                    assert os.waitstatus_to_exitcode(status) == 0
                    break
                await asyncio.sleep(.05)
            else:
                raise AssertionError('Original approval worker has no verified exit.')
        flush_log_queue()
        if passed:
            (scratch / 'approval-public-passed').write_text('passed')


try:
    asyncio.run(main())
finally:
    model.close()
