"""Install only external HTTP/WebSocket service substitutes in an owned child."""
import asyncio
import json
import os
from pathlib import Path
from types import SimpleNamespace
from urllib.parse import urlparse


def install(directory):
    import requests
    import websockets
    trace = Path(directory) / 'service-requests.jsonl'
    starts = Path(directory) / 'boundary-starts'
    generation = int(starts.read_text()) + 1 if starts.exists() else 1
    starts.write_text(str(generation))
    (Path(directory) / 'boundary-ready').write_text('external-service-installed')
    import sys
    protected_home = Path.home()
    def audit(event, args):
        if event == 'open' and isinstance(args[0], (str, bytes)):
            path = Path(os.fsdecode(args[0])).resolve()
            if path.name == '.env' or any(path.is_relative_to(protected_home / p) for p in ('.hermes', '.dsh', '.codex', '.config', '.local/state/hermes')):
                raise RuntimeError('Owned test child refused user data.')
        if event == 'socket.connect' and isinstance(args[1], tuple):
            raise RuntimeError('Owned test child refused external network.')
    sys.addaudithook(audit)
    def failed(exception_type, _, traceback):
        frames = []
        while traceback is not None:
            frames.append(Path(traceback.tb_frame.f_code.co_filename).name + ':' + str(traceback.tb_lineno))
            traceback = traceback.tb_next
        (Path(directory) / 'boundary-failure').write_text(exception_type.__name__ + ' ' + ' '.join(frames))
    sys.excepthook = failed
    def response(value):
        return SimpleNamespace(status_code=200, headers={'Content-Type': 'application/json'},
                               content=json.dumps(value).encode())
    def request(method, url, **kwargs):
        path = urlparse(url).path
        if '/auth/' in path:
            return response({'code': 0, 'tenant_access_token': 'synthetic-token', 'expire': 3600})
        if '/bot/' in path:
            return response({'code': 0, 'bot': {'open_id': 'ou_lead', 'activate_status': 2}})
        body = json.loads(kwargs.get('data', b'{}'))
        with trace.open('a') as output:
            output.write(json.dumps({'method': method, 'path': path, 'body': body}) + '\n')
        return response({'code': 0, 'data': {'message_id': 'om_process_sent', 'chat_id': 'oc_fixture'}})
    def post(url, **kwargs):
        if '/callback/ws/' in url:
            return response({'code': 0, 'data': {'URL': 'ws://fixture.invalid?device_id=fixture&service_id=1'}})
        return request('POST', url, **kwargs)
    class Connection:
        def __init__(self):
            self.emitted = False
        async def recv(self):
            if not self.emitted:
                self.emitted = True
                from lark_oapi.ws.pb.pbbp2_pb2 import Frame
                from lark_oapi.ws.enum import FrameType, MessageType
                frame = Frame(method=FrameType.DATA.value, service=1, SeqID=0, LogID=0)
                for key, value in {'type': MessageType.EVENT.value, 'message_id': 'fixture-event',
                    'trace_id': 'fixture-trace', 'sum': '1', 'seq': '0'}.items():
                    header = frame.headers.add()
                    header.key, header.value = key, value
                frame.payload = json.dumps({'schema': '2.0', 'header': {'event_type': 'im.message.receive_v1',
                    'app_id': 'cli_fixture', 'tenant_key': 'tenant-app'}, 'event': {
                    'sender': {'sender_type': 'user', 'tenant_key': 'tenant-owner',
                        'sender_id': {'open_id': 'ou_owner', 'user_id': 'u_owner'}},
                    'message': {'message_id': 'om_process_receive_' + str(generation), 'chat_id': 'oc_fixture',
                        'chat_type': 'group', 'message_type': 'text',
                        'content': json.dumps({'text': '@_user_1 /fixture-owned-process'}),
                        'mentions': [{'key': '@_user_1', 'mentioned_type': 'bot', 'tenant_key': 'tenant-bot',
                            'id': {'open_id': 'ou_lead'}}]}}}).encode()
                return frame.SerializeToString()
            await asyncio.Future()
        async def send(self, _):
            from lark_oapi.ws.pb.pbbp2_pb2 import Frame
            frame = Frame()
            frame.ParseFromString(_)
            if frame.payload:
                (Path(directory) / 'websocket-ack').write_text(frame.payload.decode())
            return None
        async def close(self):
            return None
    async def connect(*_, **__):
        return Connection()
    requests.request, requests.post = request, post
    websockets.connect = connect
