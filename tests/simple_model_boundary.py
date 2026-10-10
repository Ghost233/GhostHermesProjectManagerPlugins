"""One external OpenAI-compatible HTTP response for native ordinary replies."""
import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import threading


class OrdinaryModelService:
    def __init__(self):
        self.requests = []
        service = self

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self):
                payload = json.loads(self.rfile.read(int(self.headers['Content-Length'])))
                service.requests.append({'model': payload.get('model'), 'stream': payload.get('stream'),
                                         'message_count': len(payload.get('messages', []))})
                if payload.get('stream') is True:
                    self.send_response(200)
                    self.send_header('Content-Type', 'text/event-stream')
                    self.end_headers()
                    for delta, reason in (({'role': 'assistant'}, None),
                                          ({'content': 'Native ordinary reply.'}, None), ({}, 'stop')):
                        chunk = {'id': 'fixture-completion', 'object': 'chat.completion.chunk', 'created': 1,
                                 'model': 'fixture-model', 'choices': [{'index': 0, 'delta': delta, 'finish_reason': reason}]}
                        self.wfile.write(('data: ' + json.dumps(chunk) + '\n\n').encode())
                    self.wfile.write(b'data: [DONE]\n\n')
                    return
                response = {'id': 'fixture-completion', 'object': 'chat.completion', 'created': 1,
                            'model': 'fixture-model', 'choices': [{'index': 0, 'finish_reason': 'stop',
                            'message': {'role': 'assistant', 'content': 'Native ordinary reply.'}}],
                            'usage': {'prompt_tokens': 5, 'completion_tokens': 4, 'total_tokens': 9}}
                data = json.dumps(response).encode()
                self.send_response(200)
                self.send_header('Content-Type', 'application/json')
                self.send_header('Content-Length', str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def log_message(self, *_):
                pass

        self.server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.base_url = 'http://127.0.0.1:' + str(self.server.server_port) + '/v1'

    def close(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=2)
        assert not self.thread.is_alive(), 'The synthetic model service must be released.'
