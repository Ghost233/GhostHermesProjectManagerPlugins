"""Real registered Hermes platform, native worker, and original DSH question rounds."""
import json
from pathlib import Path
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

scratch = Path(sys.argv[1]).resolve()
question_scenario = sys.argv[2]
ROOT = Path(__file__).resolve().parents[1]


class QuestionWorkerModelService:
    """Only the external model response boundary is substituted."""
    def __init__(self, *_args, **_kwargs):
        self.requests = []
        self.worker_steps = self.dsh_steps = 0
        owner = self
        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):
                self.send_response(200)
                self.send_header('Content-Type', 'application/json')
                self.end_headers()
                self.wfile.write(b'{}' if self.path == '/models-dev' else b'{"data":[{"id":"fixture-model"}]}')

            def do_CONNECT(self):
                self.send_error(502, 'Only the fixture local external services are available.')

            def do_POST(self):
                payload = json.loads(self.rfile.read(int(self.headers['Content-Length'])))
                worker = payload['model'] == 'fixture-model'
                expected = 'synthetic-model-only-key' if worker else 'synthetic-only-key'
                if self.headers.get('Authorization') != 'Bearer ' + expected:
                    self.send_response(401)
                    self.end_headers()
                    self.wfile.write(b'{"error":{"message":"Synthetic model identity differs."}}')
                    return
                owner.requests.append(payload)
                if worker:
                    owner.worker_steps += 1
                    answered = [message for message in payload['messages'] if message.get('role') == 'tool']
                    call = ('hermes_pm_supervise', {}) if not answered else (
                        ('kanban_block', {'kind': 'needs_input', 'reason': 'Original bounded work awaits acceptance.'}) if len(answered) == 1 else None)
                    text = 'Original supervision returned.'
                    call_id = 'outer-' + str(owner.worker_steps)
                else:
                    owner.dsh_steps += 1
                    step = owner.dsh_steps
                    call_id = 'round-' + str(step)
                    text = 'No questions remain.'
                    if question_scenario == 'natural' and step == 1:
                        call, text = None, 'Before planning: which colour?\nPlease answer in your own words.'
                    elif step < 3:
                        questions = ([{'id': 'colour', 'question': 'Which colour?', 'options': [{'label': 'Blue'}, {'label': 'Green'}]},
                                      {'id': 'note', 'question': 'Which delivery note?'}] if step == 1 else
                                     [{'id': 'confirmation', 'question': 'Which final planning choice?', 'options': [{'label': 'Proceed'}, {'label': 'Revise'}]}])
                        if question_scenario == 'sensitive' and step == 1:
                            questions = [{'id': 'private', 'question': 'Please enter your password in the private interface.'}]
                        if question_scenario == 'multiselect' and step == 1:
                            questions[0]['multi_select'] = True
                        call = ('ask_user_question', {'timeout': 1 if question_scenario in {'continued', 'sensitive'} else -1,
                                                      'questions': questions})
                    else:
                        call = None
                delta = {'tool_calls': [{'index': 0, 'id': call_id, 'type': 'function',
                    'function': {'name': call[0], 'arguments': json.dumps(call[1])}}]} if call else {'content': text}
                self.send_response(200)
                self.send_header('Content-Type', 'text/event-stream')
                self.end_headers()
                for part, reason in (({'role': 'assistant'}, None), (delta, None), ({}, 'tool_calls' if call else 'stop')):
                    self.wfile.write(('data: ' + json.dumps({'id': 'round-stream', 'object': 'chat.completion.chunk', 'created': 1,
                        'model': payload['model'], 'choices': [{'index': 0, 'delta': part, 'finish_reason': reason}]}) + '\n\n').encode())
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


# Reuse the same public SDK registration and external Feishu/GitHub fixtures.
fixture = ROOT / 'tests/simple_intake_smoke_runner.py'
prefix = fixture.read_text().split('async def main():', 1)[0]
prefix = prefix.replace('from simple_worker_model import WorkerModelService, execution_reference',
                        'from simple_worker_model import execution_reference')
sys.argv = [str(fixture), str(scratch), 'worker_dispatch']
namespace = {'__name__': '__questions_fixture__', '__file__': str(fixture), 'WorkerModelService': QuestionWorkerModelService}
exec(compile(prefix, str(fixture), 'exec'), namespace)
asyncio, os, types, subprocess = (namespace[key] for key in ('asyncio', 'os', 'types', 'subprocess'))
GatewayRunner, GatewayConfig, Platform, PlatformConfig = (namespace[key] for key in
    ('GatewayRunner', 'GatewayConfig', 'Platform', 'PlatformConfig'))
service_client, connect_service, receive, raw = (namespace[key] for key in
    ('service_client', 'connect_service', 'receive', 'raw'))
ReplyMessageResponse, registry, plugins, flush_log_queue = (namespace[key] for key in
    ('ReplyMessageResponse', 'registry', 'plugins', 'flush_log_queue'))
model, repo, home = (namespace[key] for key in ('model', 'repo', 'home'))
os.environ['BROWSER'] = '/usr/bin/true'
# The fixture has only its explicit local model; unrelated picker catalogs must
# not leave external prefetch work alive after the one-shot native worker.
from hermes_cli.auth import PROVIDER_REGISTRY
from hermes_cli.providers import HERMES_OVERLAYS
from agent.models_dev import PROVIDER_TO_MODELS_DEV
configuration_path = home / 'config.yaml'
fixture_configuration = namespace['yaml'].safe_load(configuration_path.read_text())
fixture_configuration['model_catalog'] = {'excluded_providers': sorted({
    *PROVIDER_REGISTRY, *HERMES_OVERLAYS, *PROVIDER_TO_MODELS_DEV, *PROVIDER_TO_MODELS_DEV.values(), 'custom'})}
fixture_configuration['models_dev'] = {'url': model.base_url.rsplit('/v1', 1)[0] + '/models-dev'}
configuration_path.write_text(namespace['yaml'].safe_dump(fixture_configuration))
os.environ.update(HTTP_PROXY=model.base_url, HTTPS_PROXY=model.base_url, ALL_PROXY=model.base_url,
                  NO_PROXY='127.0.0.1,localhost')
os.environ['SYNTHETIC_GH_BODY'] = 'Ask the original planning question rounds, preserve the owner answers, and wait for acceptance.'


async def main():
    import importlib
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
    native = service_client('cli_fixture')
    native.request = lambda request: types.SimpleNamespace(code=0, raw=types.SimpleNamespace(
        content=b'{"code":0,"bot":{"open_id":"ou_lead","activate_status":2}}'))
    sent = []
    def reply(request):
        sent.append(request)
        return ReplyMessageResponse({'code': 0, 'data': {'message_id': 'om_sent_' + str(len(sent)),
            'chat_id': 'oc_fixture', 'parent_id': request.message_id}})
    native.im.v1.message.reply = reply
    workers, carriers = [], []
    failed = True
    try:
        await connect_service(adapter, native)
        await receive(adapter, raw('om_work'))
        def record():
            return json.loads(registry.dispatch('hermes_pm_snapshot', {}))['work'][0]
        original = record()
        assert original['dsh_execution']['state'] == 'admitted', original
        with connect_closing() as db:
            assert dispatch_once(db, max_spawn=1).spawned
            workers.append(remember_worker(db, original['card_id']))
        async def wait_for(check, seconds=25):
            for _ in range(int(seconds / .025)):
                observer = adapter.intake.human_observer
                if observer.done() and not observer.cancelled() and observer.exception():
                    raise observer.exception()
                value = check()
                if value:
                    return value
                await asyncio.sleep(.025)
            raise AssertionError('Original public question round timed out: ' + json.dumps(record()['dsh_execution']))
        first = await wait_for(lambda: next((q for q in record()['dsh_execution'].get('questions', []) if q.get('delivery')), None))
        if question_scenario == 'sensitive':
            assert first['answerable'] is False and 'questions' not in first
            value = 'synthetic-owner-private-reply'
            await receive(adapter, raw('om_private_reply', '@_user_1 api_key: ' + value,
                                       parent_id=first['delivery']['message_id']))
            assert value not in json.dumps(record())
            assert value.encode() not in adapter.intake.database.read_bytes()
            assert all(value not in message.request_body.content for message in sent)
            assert record()['dsh_execution']['questions'][0]['answers'] == {}
            await wait_for(lambda: record()['dsh_execution'].get('state') == 'awaiting_acceptance')
            failed = False
            (scratch / 'questions-smoke-result').write_text('passed')
            return
        assert len([message for message in sent if '问题轮次：' in message.request_body.content]) == 1
        sid, generation = first['session_id'], first['generation']
        parent = first['delivery']['message_id']
        if question_scenario == 'continued':
            await wait_for(lambda: any(q['id'] == first['id'] and q['state'] == 'continued' for q in record()['dsh_execution']['questions']))
            await wait_for(lambda: json.loads(registry.dispatch('kanban_show', {'task_id': original['card_id']}))['task']['status'] == 'blocked')
            input_path = adapter.intake.state_dir / 'owned-work' / original['id'] / 'home/.hermes-first-input.json'
            first_input = input_path.read_bytes()
        await receive(adapter, raw('om_no_mention', 'colour：Green', parent_id=parent, mentions=[]))
        await receive(adapter, raw('om_wrong_round', '@_user_1 colour：Green', parent_id='om_unrelated_round'))
        assert record()['dsh_execution']['questions'][0]['answers'] == {}
        if question_scenario == 'natural':
            await receive(adapter, raw('om_natural_answer', '@_user_1 Blue, keep the original scope.', parent_id=parent))
        else:
            selected = ['Blue', 'Green'] if question_scenario == 'multiselect' else ['Blue']
            partial_text = '@_user_1 colour：' + ','.join(selected)
            await receive(adapter, raw('om_partial', partial_text, parent_id=parent))
            partial = record()['dsh_execution']['questions'][0]
            assert partial['answers'] == {'colour': {'id': 'colour', 'selected': selected}} and not partial.get('reply_status')
            assert model.dsh_steps == (3 if question_scenario == 'continued' else 1)
            await receive(adapter, raw('om_partial', partial_text, parent_id=parent))
            note = '跳过' if question_scenario == 'multiselect' else '委托决定'
            await receive(adapter, raw('om_complete', '@_user_1 note：' + note, parent_id=parent))
        second = await wait_for(lambda: next((q for q in record()['dsh_execution'].get('questions', [])
            if q['id'] != first['id'] and q.get('delivery')), None))
        assert second['session_id'] == sid and second['generation'] == generation
        first_done = next(q for q in record()['dsh_execution']['questions'] if q['id'] == first['id'])
        await wait_for(lambda: next(q for q in record()['dsh_execution']['questions'] if q['id'] == first['id'])['reply_status'] == 'accepted')
        await receive(adapter, raw('om_late_first', '@_user_1 note：different', parent_id=parent))
        await receive(adapter, raw('om_second_answer', '@_user_1 confirmation：Proceed', parent_id=second['delivery']['message_id']))
        await wait_for(lambda: next(q for q in record()['dsh_execution']['questions'] if q['id'] == second['id']).get('reply_status') == 'accepted')
        supervision = next(module for name, module in sys.modules.items() if name.endswith('.ghost_hermes_pm.repository_supervision'))
        carrier = supervision.attach_existing_owned(adapter.intake, record())
        carriers.append(carrier)
        events, _ = await asyncio.to_thread(supervision.original_history, carrier, sid, str(repo))
        structured = [event for event in events if event['type'] == 'tool/result'
            and event['data']['message'].get('toolCallId') == first['id']]
        continued = [event for event in events if event['type'] == 'user/message'
            and event['data'].get('source', {}).get('callId') == first['id']]
        if question_scenario == 'continued':
            assert len(continued) == 1 and continued[0]['data']['source']['outcome'] == 'answered'
        elif question_scenario in {'live', 'multiselect'}:
            assert len(structured) == 1
            content = json.loads(structured[0]['data']['message']['content'][0]['text'])
            expected_note = {'id': 'note', 'selected': []}
            if question_scenario != 'multiselect':
                expected_note['custom'] = '委托决定'
            assert content['answers'][1] == expected_note
            assert content['answers'][0]['selected'] == selected
        else:
            inputs = [event for event in events if event['type'] == 'user/message' and event['data'].get('source', {}).get('rpcId') == 'answer-om_natural_answer']
            assert len(inputs) == 1 and len(carrier.event_frames()['frames']) > 0
        assert len([message for message in sent if '问题轮次：' in message.request_body.content]) == 2
        if question_scenario == 'continued':
            await wait_for(lambda: json.loads(registry.dispatch('kanban_show', {'task_id': original['card_id']}))['task']['status'] == 'ready')
            original_exit = await wait_for(lambda: (exit_result if (exit_result := os.waitpid(workers[0]['pid'], os.WNOHANG))[0] else None))
            workers[0]['collected_exit_code'] = os.waitstatus_to_exitcode(original_exit[1])
            assert workers[0]['collected_exit_code'] == 0
            with connect_closing() as db:
                assert dispatch_once(db, max_spawn=1).spawned
                resumed = remember_worker(db, original['card_id'])
                workers.append(resumed)
            assert resumed['run_id'] != workers[0]['run_id']
            assert input_path.read_bytes() == first_input
            await wait_for(lambda: record()['dsh_execution']['state'] == 'awaiting_acceptance')
            resumed_question = next(q for q in record()['dsh_execution']['questions'] if q['id'] == first['id'])
            assert resumed_question['resume_unblock']['previous_run_id'] == workers[0]['run_id']
            assert resumed_question['resume_unblock']['status'] == 'accepted'
            assert record()['dsh_execution']['supervisor_claim']['run_id'] == resumed['run_id']
            assert record()['dsh_execution']['session_id'] == sid
            assert record()['dsh_execution']['generation'] == generation
            assert model.dsh_steps == 5
        else:
            assert model.dsh_steps == 3
        failed = False
        (scratch / 'questions-smoke-result').write_text('passed')
    finally:
        cleanup = []
        plugins.unload('ghost-hermes-pm')
        await runner.stop()
        worker_exits = []
        for worker in workers:
            try:
                if 'collected_exit_code' in worker:
                    worker_exits.append({'pid': worker['pid'], 'birth': worker['birth'], 'exit_code': worker['collected_exit_code'],
                        'wait': 'original_child_waitpid_before_resume'})
                    continue
                import psutil
                from time import monotonic
                deadline = monotonic() + 15
                while True:
                    try:
                        process = psutil.Process(worker['pid'])
                        assert process.create_time() == worker['birth']
                    except psutil.NoSuchProcess:
                        raise AssertionError('Original worker exit code was not collected.') from None
                    waited, status = os.waitpid(worker['pid'], os.WNOHANG)
                    if waited == worker['pid']:
                        code = os.waitstatus_to_exitcode(status)
                        assert code == 0, code
                        worker_exits.append({'pid': waited, 'birth': worker['birth'], 'exit_code': code, 'wait': 'original_child_waitpid'})
                        break
                    if monotonic() >= deadline:
                        (scratch / 'worker-cleanup-diagnostic.json').write_text(json.dumps({
                            'worker': worker, 'status': process.status(), 'same_birth': True,
                            'command': process.cmdline(), 'worker_steps': model.worker_steps,
                            'model_request_count': len(model.requests)}))
                        sample = scratch / 'original-worker-sample.txt'
                        sampled = subprocess.run(['/usr/bin/sample', str(worker['pid']), '1', '-file', str(sample)],
                            text=True, capture_output=True, timeout=10)
                        if sample.exists():
                            sample.chmod(0o600)
                        (scratch / 'sample-result.json').write_text(json.dumps({'exit_code': sampled.returncode,
                            'stdout': sampled.stdout, 'stderr': sampled.stderr}))
                        raise TimeoutError('Original worker did not exit within the bounded cleanup wait.')
                    await asyncio.sleep(.05)
            except Exception as error:
                cleanup.append('worker:' + type(error).__name__)
        for carrier in carriers:
            carrier.close()
        supervision = next((module for name, module in sys.modules.items() if name.endswith('.ghost_hermes_pm.repository_supervision')), None)
        if supervision:
            for work in adapter.intake.snapshot()['work']:
                if work.get('dsh_execution', {}).get('native_identity'):
                    try:
                        carrier = supervision.attach_existing_owned(adapter.intake, work)
                        carrier.shutdown_owned()
                        assert carrier.close_outcome['kind'] == 'original_exit'
                    except Exception as error:
                        cleanup.append('carrier:' + type(error).__name__)
        flush_log_queue()
        (scratch / 'cleanup-evidence.json').write_text(json.dumps({'business_failed': failed, 'cleanup_errors': cleanup,
            'worker_exits': worker_exits}))
        assert not cleanup, cleanup


try:
    asyncio.run(main())
finally:
    model.close()
