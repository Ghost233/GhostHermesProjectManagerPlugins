"""Real SDK AIAgent turn against an owned localhost OpenAI-compatible peer."""
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import sys
import threading

home = Path(os.environ['HERMES_HOME']).resolve()
observed = []


def audit(event, args):
    if event == 'socket.connect' and isinstance(args[1], tuple) and args[1][0] not in ('127.0.0.1', '::1'):
        raise RuntimeError('Synthetic migration session refuses business network.')
    if event == 'open' and isinstance(args[0], (str, bytes)):
        path = Path(os.fsdecode(args[0])).resolve()
        if path.name == '.env' and not path.is_relative_to(home):
            raise RuntimeError('Synthetic migration session refuses unrelated credentials.')


sys.addaudithook(audit)


class ModelPeer(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers['Content-Length'])))
        observed.append(body)
        answer = {'id': 'synthetic-migration-completion', 'object': 'chat.completion', 'created': 1,
            'model': 'synthetic-model', 'choices': [{'index': 0, 'message': {'role': 'assistant', 'content': 'Selected native memory is loaded.'}, 'finish_reason': 'stop'}],
            'usage': {'prompt_tokens': 10, 'completion_tokens': 5, 'total_tokens': 15}}
        if body.get('stream'):
            chunks = [dict(answer, object='chat.completion.chunk', choices=[{'index': 0, 'delta': {'role': 'assistant', 'content': 'Selected native memory is loaded.'}, 'finish_reason': None}]),
                dict(answer, object='chat.completion.chunk', choices=[{'index': 0, 'delta': {}, 'finish_reason': 'stop'}])]
            raw = (''.join('data: ' + json.dumps(chunk) + '\n\n' for chunk in chunks) + 'data: [DONE]\n\n').encode()
        else:
            raw = json.dumps(answer).encode()
        self.send_response(200)
        self.send_header('Content-Type', 'text/event-stream' if body.get('stream') else 'application/json')
        self.send_header('Content-Length', str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)


server = ThreadingHTTPServer(('127.0.0.1', 0), ModelPeer)
thread = threading.Thread(target=server.serve_forever, daemon=True)
thread.start()
from hermes_state import SessionDB
from run_agent import AIAgent
db = SessionDB(db_path=home / 'state.db')
agent = AIAgent(model='synthetic-model', provider='custom', api_mode='chat_completions',
    base_url=f'http://127.0.0.1:{server.server_address[1]}/v1', api_key='synthetic-test-key',
    enabled_toolsets=['memory'], session_id=sys.argv[1], session_db=db, platform='desktop',
    skip_context_files=True, load_soul_identity=True, skip_background_review=True,
    quiet_mode=True, max_iterations=1, cwd=str(home))
try:
    result = agent.run_conversation('Report the selected native memory in this new identity.')
    assert result['final_response'] == 'Selected native memory is loaded.', result
    row = db.get_session(sys.argv[1])
    messages = db.get_messages(sys.argv[1])
    assert row['profile_name'] == home.name and row['system_prompt']
    assert any(m['role'] == 'user' for m in messages) and any(m['role'] == 'assistant' for m in messages)
    assert any(row['system_prompt'] == request.get('instructions') or any(message.get('role') == 'system' and message.get('content') == row['system_prompt']
        for message in request.get('messages', [])) for request in observed), [list(request) for request in observed]
    assert any([tool.get('function', tool).get('name') for tool in request.get('tools', [])] == ['memory'] for request in observed)
    print(json.dumps({'session_id': row['id'], 'native_profile': row['profile_name'], 'model': row['model'],
        'route': db.get_recent_session_model_route(row['id']), 'tool_names': row['tool_names'], 'native_first_request_output': True}))
finally:
    agent.close()
    db.close()
    server.shutdown()
    server.server_close()
    thread.join()
