"""External Feishu service substitutes; no native SDK classes are changed."""
from types import SimpleNamespace


def service_client(app_id, secret='synthetic-unused-secret'):
    from lark_oapi import AppType
    def unavailable(_):
        raise AssertionError('Configure the expected external service response.')
    return SimpleNamespace(config=SimpleNamespace(app_id=app_id, app_secret=secret,
        app_type=AppType.SELF, enable_set_token=False), request=unavailable,
        im=SimpleNamespace(v1=SimpleNamespace(message=SimpleNamespace(
            create=unavailable, reply=unavailable, get=unavailable),
            chat=SimpleNamespace(get=unavailable))))


async def connect_service(adapter, client):
    import importlib
    package = type(adapter).__module__.rsplit('.', 1)[0]
    NativeFeishuTransport = importlib.import_module(package + '.feishu').NativeFeishuTransport
    class ServiceTransport(NativeFeishuTransport):
        async def start(self):
            self.closed = False
            return True
        def request_close(self):
            self.closed = True
        async def close(self):
            self.closed = True
    transport = ServiceTransport(client)
    adapter.create_transport = lambda: transport
    assert await adapter.connect(), adapter.fatal_error_code
    return transport


async def receive(adapter, event):
    import json
    from lark_oapi import JSON
    await adapter.receive_payload(json.loads(JSON.marshal(event)))
