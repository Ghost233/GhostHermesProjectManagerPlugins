"""Business tests for the owned platform against unmodified Hermes interfaces."""
import os
from pathlib import Path
import subprocess
import sys

import pytest

ROOT = Path(__file__).resolve().parents[1]


def test_owned_driver_receives_without_native_feishu_logging(tmp_path):
    sdk = os.environ.get('HERMES_TEST_SDK_ROOT')
    if not sdk:
        pytest.skip('Requires the unmodified Hermes SDK; no native claim is made by a skip.')
    env = dict(os.environ, HERMES_HOME=str(tmp_path / 'home'), HOME=str(tmp_path),
               HERMES_SKIP_PM_BOOTSTRAP='1', HERMES_DISABLE_PROJECT_PLUGINS='1',
               XDG_STATE_HOME=str(tmp_path/'state'), HERMES_PROFILE='default',
               PYTHONDONTWRITEBYTECODE='1', PYTHONPATH=os.pathsep.join([str(ROOT), sdk]))
    result = subprocess.run([sys.executable, '-c', r'''
import asyncio, json, logging, types
from gateway.config import Platform, PlatformConfig
from gateway.platforms.base import BasePlatformAdapter
from ghost_hermes_pm.owned_feishu import OwnedFeishuAdapter
from gateway.platform_registry import platform_registry, PlatformEntry
platform_registry.register(PlatformEntry('hermes_feishu_pm', 'Owned', lambda config: None, lambda: True))
binding = {'app_id':'cli_synthetic','recipient_open_id':'ou_target','owner_native_ids':['u_owner'],
           'verification_ref':'fixture:verified','chat_id':'oc_group','recipient_tenant_key':'tenant_bot',
           'transport_tenant_key':'tenant_app','sender_tenant_key':'tenant_owner','owner_open_id':'ou_owner'}
class Intake:
    closed=False; generation=0; secret_values=(); collaboration_entry=None
    settings={'enabled':True,'verification_ref':'fixture:verified','bindings':[binding]}
    def manager(self): return None
    def prepare(self,event,adapter): return None
    def attach_transport(self,adapter,transport): self.transport=transport
    def detach_transport(self,adapter): self.transport=None
intake=Intake()
adapter=OwnedFeishuAdapter(PlatformConfig(extra={'app_id':'cli_synthetic','app_secret':'synthetic-secret'}), intake, None, '.')
assert isinstance(adapter, BasePlatformAdapter)
received=[]
async def dispatch(event): received.append(event)
adapter._dispatch_inbound_event=dispatch
raw=types.SimpleNamespace(header=types.SimpleNamespace(app_id='cli_synthetic',tenant_key='tenant_app',event_type='im.message.receive_v1'),event=types.SimpleNamespace(
 sender=types.SimpleNamespace(sender_id=types.SimpleNamespace(open_id='ou_owner',user_id='u_owner',union_id=None),sender_type='user',tenant_key='tenant_owner'),
 message=types.SimpleNamespace(message_id='om_first',chat_id='oc_group',chat_type='group',message_type='text',content=json.dumps({'text':'@_user_1 synthetic-private-text'}),
 mentions=[types.SimpleNamespace(key='@_user_1',tenant_key='tenant_bot',mentioned_type='bot',id=types.SimpleNamespace(open_id='ou_target'))],parent_id='om_start',thread_id=None)))
logs=[]
class Capture(logging.Handler):
 def emit(self,record): logs.append(record.getMessage())
logging.getLogger().addHandler(Capture()); logging.getLogger().setLevel(logging.DEBUG)
async def main():
 await adapter._handle_message_event_data(raw)
 await adapter._handle_message_event_data(raw)
 assert len(received)==1
 assert received[0].text=='synthetic-private-text'
 assert received[0].reply_to_message_id=='om_start'
 assert received[0].source.user_id=='u_owner'
 assert received[0].raw_message is raw
 raw.event.message.message_id='om_forged'
 raw.event.message.mentions=[]
 await adapter._handle_message_event_data(raw)
 assert len(received)==1
 raw.event.message.mentions=[types.SimpleNamespace(key='@_user_1',tenant_key='tenant_bot',mentioned_type='bot',id=types.SimpleNamespace(open_id='ou_target'))]
 raw.event.sender.sender_id.user_id='u_stranger';raw.event.sender.sender_id.open_id='ou_stranger'
 await adapter._handle_message_event_data(raw)
 assert len(received)==1
 assert not any(marker in '\n'.join(logs) for marker in ['synthetic-private-text','oc_group','om_first','u_owner','synthetic-secret'])
 # A fresh plugin instance keeps event deduplication without storing raw bindings.
 second=OwnedFeishuAdapter(PlatformConfig(extra={'app_id':'cli_synthetic','app_secret':'synthetic-secret'}), Intake(), None, '.')
 second._dispatch_inbound_event=dispatch
 raw.event.message.message_id='om_first';raw.event.sender.sender_id.user_id='u_owner';raw.event.sender.sender_id.open_id='ou_owner'
 await second._handle_message_event_data(raw)
 assert len(received)==1
 assert not any(marker in adapter._dedup_state_path.read_text() for marker in ['oc_group','om_first','u_owner','cli_synthetic'])
 class Service:
  process=None; closed=False; requested=False
  async def start(self): return True
  async def close(self): self.closed=True
  def request_close(self): self.requested=True
  async def send(self,segment): return {'status':'delivered','message_id':'om_sent'}
 service=Service()
 adapter.create_transport=lambda: service
 assert await adapter.connect()
 assert adapter.transport is service and intake.transport is service and adapter._app_lock_identity
 assert (await adapter.send('oc_group','answer','om_start')).message_id=='om_sent'
 adapter.request_close()
 assert service.requested
 await adapter.disconnect()
 assert service.closed and adapter.transport is None and intake.transport is None
 assert adapter._app_lock_identity is None and adapter._ws_supervisor is None
 assert not await adapter.connect(is_reconnect=True)
 # A new owned instance may reconnect after old resources are actually released.
 other=Service();second.create_transport=lambda: other
 assert await second.connect()
 await second.disconnect()
 assert other.closed and second._app_lock_identity is None

asyncio.run(main())
'''], env=env, capture_output=True, text=True, timeout=45)
    assert result.returncode == 0, result.stdout + result.stderr


@pytest.mark.asyncio
@pytest.mark.parametrize('activate', [True, False])
async def test_owned_process_handles_event_rpc_and_releases_resources(monkeypatch, tmp_path, caplog, activate):
    """The fake is the external service process; IPC and process ownership are real."""
    import asyncio
    import logging
    from ghost_hermes_pm.owned_feishu_process import FeishuProcessTransport

    service = tmp_path / 'service.py'
    service.write_text(r'''
import json, os, socket, sys
stream = socket.socket(fileno=int(sys.argv[1])).makefile('rwb')
initial = json.loads(stream.readline())
assert initial['operation']=='initialize'
assert initial['credentials']['app_secret']=='synthetic-private-secret'
assert 'FEISHU_APP_SECRET' not in os.environ
print('synthetic-private-secret', flush=True)
print('synthetic-private-text', file=sys.stderr, flush=True)
def send(value):
 stream.write((json.dumps(value)+'\n').encode());stream.flush()
send({'kind':'ready'})
send({'kind':'event','payload':{'text':'synthetic-private-text'}})
for raw in stream:
 frame=json.loads(raw)
 if frame['operation']=='verify_identity':
  result={'app_id':'cli_fixture','open_id':'ou_target'}
 else:
  result={'status':'delivered','message_id':'om_delivery','parent_id':frame['value']['reply_to']}
 send({'id':frame['id'],'ok':True,'result':result})
''')
    original = asyncio.create_subprocess_exec
    launched = []

    async def launch(*args, **kwargs):
        # Replace the external process only, keeping production fd/env/console isolation.
        process = await original(sys.executable, str(service), args[-1], **kwargs)
        launched.append(process)
        return process

    monkeypatch.setattr(asyncio, 'create_subprocess_exec', launch)
    monkeypatch.setenv('FEISHU_APP_SECRET', 'synthetic-unrelated-secret')
    before_factory = logging.getLogRecordFactory()
    received = asyncio.Event()
    transport = None

    async def event(payload):
        assert payload == {'text': 'synthetic-private-text'}
        assert await transport.verify_identity({'app_id': 'cli_fixture'}) == {'app_id': 'cli_fixture', 'open_id': 'ou_target'}
        receipt = await transport.send({'chat_id': 'oc_group', 'text': 'answer', 'reply_to': 'om_source', 'uuid': 'segment-1'})
        assert receipt == {'status': 'delivered', 'message_id': 'om_delivery', 'parent_id': 'om_source'}
        received.set()

    transport = FeishuProcessTransport({'app_id': 'cli_fixture', 'app_secret': 'synthetic-private-secret'}, event)
    try:
        assert await transport.start()
        assert not received.is_set()
        if activate:
            transport.activate()
            await asyncio.wait_for(received.wait(), timeout=5)
        else:
            await asyncio.sleep(0.05)
            assert not received.is_set()
            transport.request_close()
    finally:
        await transport.close()
    assert all(p.returncode == 0 for p in launched)
    assert transport.reader_task.done() and transport.event_task.done()
    assert logging.getLogRecordFactory() is before_factory
    assert 'synthetic-private-secret' not in caplog.text
    assert 'synthetic-private-text' not in caplog.text
    assert await transport.send({'text': 'late'}) == {'status': 'unknown'}
    assert not transport.pending
    assert transport.writer.is_closing()
    assert transport.events.empty()
    await transport.close()


async def _external_process(monkeypatch, tmp_path, script):
    import asyncio
    service = tmp_path / 'delayed-service.py'
    service.write_text(script)
    original = asyncio.create_subprocess_exec
    launched = []

    async def launch(*args, **kwargs):
        process = await original(sys.executable, str(service), args[-1], **kwargs)
        launched.append(process)
        return process

    monkeypatch.setattr(asyncio, 'create_subprocess_exec', launch)
    return launched


@pytest.mark.asyncio
async def test_send_timeout_returns_unknown_without_repeating_send(monkeypatch, tmp_path):
    import asyncio
    import ghost_hermes_pm.owned_feishu_process as owned
    launched = await _external_process(monkeypatch, tmp_path, r'''
import json, socket, sys, time
stream=socket.socket(fileno=int(sys.argv[1])).makefile('rwb')
stream.readline()
stream.write(b'{"kind":"ready"}\n');stream.flush()
frame=json.loads(stream.readline())
assert frame['operation']=='send'
time.sleep(.2)
stream.close()
''')
    original_wait = asyncio.wait_for

    async def bounded_wait(awaitable, timeout):
        return await original_wait(awaitable, timeout=.05 if timeout == 30 else timeout)

    monkeypatch.setattr(asyncio, 'wait_for', bounded_wait)
    transport = owned.FeishuProcessTransport({'app_id': 'cli_test', 'app_secret': 'synthetic'}, lambda payload: None)
    try:
        assert await transport.start()
        assert await transport.send({'uuid': 'same-segment', 'text': 'answer'}) == {'status': 'unknown'}
        assert len(launched) == 1
    finally:
        await transport.close()


@pytest.mark.asyncio
async def test_cancelled_close_can_be_awaited_again_until_actual_cleanup(monkeypatch, tmp_path):
    import asyncio
    from ghost_hermes_pm.owned_feishu_process import FeishuProcessTransport
    launched = await _external_process(monkeypatch, tmp_path, r'''
import socket, sys, time
stream=socket.socket(fileno=int(sys.argv[1])).makefile('rwb')
stream.readline();stream.write(b'{"kind":"ready"}\n');stream.flush()
for raw in stream: pass
time.sleep(.2)
''')
    transport = FeishuProcessTransport({'app_id': 'cli_test', 'app_secret': 'synthetic'}, lambda payload: None)
    try:
        assert await transport.start()
        first = asyncio.create_task(transport.close())
        await asyncio.sleep(.01)
        first.cancel()
        with pytest.raises(asyncio.CancelledError):
            await first
        await transport.close()
        assert launched[0].returncode is not None
        assert transport.reader_task.done() and transport.event_task.done()
        assert not transport.pending and transport.events.empty()
    finally:
        # A failing regression must still release the fake service and its tasks.
        transport.writer.close()
        await launched[0].wait()
        for task in (transport.reader_task, transport.event_task):
            if task is not None:
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)


def _sdk_business_case(tmp_path, body, *, directory_load=False):
    sdk = os.environ.get('HERMES_TEST_SDK_ROOT')
    if not sdk:
        pytest.skip('Requires the unmodified Hermes SDK; a skip proves no native acceptance.')
    env = dict(os.environ, HOME=str(tmp_path), HERMES_HOME=str(tmp_path/'home'),
               HERMES_SKIP_PM_BOOTSTRAP='1', HERMES_DISABLE_PROJECT_PLUGINS='1', HERMES_PROFILE='default',
               XDG_STATE_HOME=str(tmp_path/'state'), PYTHONDONTWRITEBYTECODE='1',
               PYTHONPATH=sdk if directory_load else os.pathsep.join([str(ROOT), sdk]))
    setup = r'''
import asyncio, json, types
from gateway.config import PlatformConfig
from gateway.platform_registry import platform_registry, PlatformEntry
platform_registry.register(PlatformEntry('hermes_feishu_pm','Owned',lambda config:None,lambda:True))
from ghost_hermes_pm.owned_feishu import OwnedFeishuAdapter
binding={'app_id':'cli_synthetic','recipient_open_id':'ou_target','owner_native_ids':['u_owner'],
 'verification_ref':'fixture:verified','chat_id':'oc_group','recipient_tenant_key':'tenant_bot',
 'transport_tenant_key':'tenant_app','sender_tenant_key':'tenant_owner','owner_open_id':'ou_owner'}
class Intake:
 closed=False;generation=0;secret_values=();collaboration_entry=None
 settings={'enabled':True,'verification_ref':'fixture:verified','bindings':[binding]}
 def manager(self):return None
 def attach_transport(self,adapter,transport):self.transport=transport
 def detach_transport(self,adapter):self.transport=None
 def prepare(self,event,adapter):return None
intake=Intake()
adapter=OwnedFeishuAdapter(PlatformConfig(extra={'app_id':'cli_synthetic','app_secret':'synthetic'}),intake,None,'.')
'''
    result = subprocess.run([sys.executable, '-c', ('' if directory_load else setup) + body, str(ROOT)],
                            env=env, cwd=tmp_path, capture_output=True, text=True, timeout=45)
    assert result.returncode == 0, result.stdout + result.stderr


def test_cancelled_connection_releases_app_ownership(tmp_path):
    _sdk_business_case(tmp_path, r'''
async def main():
 started=asyncio.Event()
 class Service:
  process=None;closed=False
  async def start(self):started.set();await asyncio.Future()
  async def close(self):self.closed=True
 service=Service();adapter.create_transport=lambda:service
 pending=asyncio.create_task(adapter.connect())
 await started.wait();pending.cancel()
 try:await pending
 except asyncio.CancelledError:pass
 assert service.closed
 assert adapter.transport is None and adapter._app_lock_identity is None
 assert not adapter._running
asyncio.run(main())
''')


def test_failed_dedup_save_allows_original_message_retry(tmp_path):
    _sdk_business_case(tmp_path, r'''
import ghost_hermes_pm.owned_feishu as owned
raw=types.SimpleNamespace(header=types.SimpleNamespace(app_id='cli_synthetic',tenant_key='tenant_app',event_type='im.message.receive_v1'),event=types.SimpleNamespace(
 sender=types.SimpleNamespace(sender_id=types.SimpleNamespace(open_id='ou_owner',user_id='u_owner',union_id=None),sender_type='user',tenant_key='tenant_owner'),
 message=types.SimpleNamespace(message_id='om_retry',chat_id='oc_group',chat_type='group',message_type='text',content=json.dumps({'text':'@_user_1 work'}),
 mentions=[types.SimpleNamespace(key='@_user_1',tenant_key='tenant_bot',mentioned_type='bot',id=types.SimpleNamespace(open_id='ou_target'))])))
received=[]
async def dispatch(event):received.append(event)
adapter._dispatch_inbound_event=dispatch
async def main():
 original=owned.os.replace
 def unavailable(*args):raise OSError('synthetic unavailable storage')
 owned.os.replace=unavailable
 try:await adapter._handle_message_event_data(raw)
 finally:owned.os.replace=original
 assert not received
 await adapter._handle_message_event_data(raw)
 assert len(received)==1
 await adapter._handle_message_event_data(raw)
 assert len(received)==1
asyncio.run(main())
''')


def test_owned_reconnect_continues_after_transient_failure(tmp_path):
    _sdk_business_case(tmp_path, r'''
import ghost_hermes_pm.owned_feishu as owned
async def main():
 attempts=[];third=asyncio.Event();first_exited=asyncio.Event()
 class Process:
  async def wait(self):await first_exited.wait()
 class Service:
  def __init__(self,n):self.n=n;self.process=Process() if n==0 else None;self.closed=False
  async def start(self):
   attempts.append(self.n)
   if self.n==1:return False
   if self.n==2:third.set()
   return True
  async def close(self):self.closed=True
 services=[]
 def factory():
  value=Service(len(services));services.append(value);return value
 adapter.create_transport=factory
 original_sleep=owned.asyncio.sleep
 async def quick_sleep(delay):await original_sleep(0)
 owned.asyncio.sleep=quick_sleep
 try:
  assert await adapter.connect()
  first_exited.set()
  await asyncio.wait_for(third.wait(),timeout=1)
  assert attempts==[0,1,2]
 finally:
  owned.asyncio.sleep=original_sleep
  adapter.request_close();await adapter.disconnect()
 assert all(s.closed for s in services)
asyncio.run(main())
''')


def test_directory_loaded_plugin_binds_its_own_worker_package(tmp_path):
    _sdk_business_case(tmp_path, r'''
import asyncio, importlib, sys, types
from pathlib import Path
from hermes_cli.plugins import PluginManager
root=Path(sys.argv[1])
assert str(root) not in sys.path
manager=PluginManager.__new__(PluginManager)
manager._load_directory_module(types.SimpleNamespace(path=root), module_name='hermes_plugins.synthetic_owned')
module=importlib.import_module('hermes_plugins.synthetic_owned.ghost_hermes_pm.owned_feishu_process')
# External service substitute validates the actual child import environment.
worker=Path.cwd()/'worker.py'
worker.write_text(r"""
import ghost_hermes_pm.owned_feishu_process as module
import json,socket,sys
from pathlib import Path
assert Path(module.__file__).resolve()==Path(sys.argv[2]).resolve()/'ghost_hermes_pm'/'owned_feishu_process.py'
stream=socket.socket(fileno=int(sys.argv[1])).makefile('rwb')
stream.readline();stream.write(b'{"kind":"ready"}\n');stream.flush()
for raw in stream:pass
""")
original=asyncio.create_subprocess_exec
async def service(*args,**kwargs):
 return await original(sys.executable,str(worker),args[-1],str(root),**kwargs)
asyncio.create_subprocess_exec=service
async def main():
 transport=module.FeishuProcessTransport({'app_id':'cli_test','app_secret':'synthetic'},lambda payload:None)
 try:
  assert await transport.start()
  assert str(root) not in sys.path
 finally:await transport.close()
 assert transport.process.returncode==0
asyncio.run(main())
''', directory_load=True)


def test_verified_owner_allowlist_reaches_unmodified_native_authorization(tmp_path):
    _sdk_business_case(tmp_path, r'''
from gateway.authz_mixin import GatewayAuthorizationMixin
from gateway.config import Platform
platform_registry.register(PlatformEntry('hermes_feishu_pm','Owned',lambda config:None,lambda:True,allowed_users_env='SYNTHETIC_ALLOWED_USERS'))
class Authorization(GatewayAuthorizationMixin):
 def _delivery_adapter_for(self,source):return adapter
source=types.SimpleNamespace(platform=Platform('hermes_feishu_pm'))
authorization=Authorization()
assert authorization._adapter_extra_allowlist_authorizes(source,'u_owner',True)
assert not authorization._adapter_extra_allowlist_authorizes(source,'u_unknown',True)
''')


@pytest.mark.parametrize('extra,accepted', [
    ({'group_rules': {'oc_group': {'policy': 'disabled'}}}, False),
    ({'default_group_policy': 'disabled'}, False),
    ({'group_rules': {'oc_group': {'policy': 'allowlist', 'allowlist': ['u_unknown']}}}, False),
    ({'group_rules': {'oc_group': {'policy': 'blacklist', 'blacklist': ['u_owner']}}}, False),
    ({'group_rules': {'oc_group': {'policy': 'admin_only'}}, 'admins': ['u_unknown']}, False),
    ({'group_rules': {'oc_group': {'policy': 'admin_only'}}, 'admins': ['u_owner']}, True),
    ({'group_rules': {'oc_group': {'policy': 'allowlist', 'allowlist': ['u_owner']}}}, True),
])
def test_owned_group_policy_prevents_dispatch_before_side_effects(tmp_path, extra, accepted):
    _sdk_business_case(tmp_path, r'''
from dataclasses import replace
configured=OwnedFeishuAdapter(replace(adapter.config, extra={**adapter.config.extra, **EXTRA}),Intake(),None,'.')
received=[]
async def dispatch(event):received.append(event)
configured._dispatch_inbound_event=dispatch
raw={'header':{'app_id':'cli_synthetic','tenant_key':'tenant_app','event_type':'im.message.receive_v1'},'event':{
 'sender':{'sender_id':{'open_id':'ou_owner','user_id':'u_owner'},'sender_type':'user','tenant_key':'tenant_owner'},
 'message':{'message_id':'om_policy','chat_id':'oc_group','chat_type':'group','message_type':'text','content':json.dumps({'text':'@_user_1 work'}),
 'mentions':[{'key':'@_user_1','tenant_key':'tenant_bot','mentioned_type':'bot','id':{'open_id':'ou_target'}}]}}}
async def main():
 await configured.receive_payload(raw)
 assert bool(received) is ACCEPTED
 assert bool(configured._seen) is ACCEPTED
asyncio.run(main())
'''.replace('EXTRA', repr(extra)).replace('ACCEPTED', repr(accepted)))


def test_owned_unload_blocks_base_debounce_followup_dispatch(tmp_path):
    _sdk_business_case(tmp_path, r'''
from gateway.platforms.event import MessageEvent
async def main():
 started=asyncio.Event();release=asyncio.Event();dispatched=[]
 class Service:
  process=None
  async def start(self):return True
  async def close(self):pass
 adapter.create_transport=lambda:Service()
 assert await adapter.connect()
 async def handler(event):
  dispatched.append(event.message_id);started.set();await release.wait()
 adapter.set_message_handler(handler)
 adapter._busy_text_mode='queue';adapter._busy_text_debounce_seconds=.05;adapter._busy_text_hard_cap_seconds=.05
 source=adapter.build_source(chat_id='oc_group',chat_type='group',user_id='u_owner')
 await adapter.handle_message(MessageEvent(text='ordinary',source=source,message_id='om_running'))
 await started.wait()
 await adapter.handle_message(MessageEvent(text='followup',source=source,message_id='om_followup'))
 assert dispatched==['om_running']
 adapter.request_close()
 await adapter.disconnect()
 release.set();await asyncio.sleep(.1)
 assert dispatched==['om_running']
 assert not adapter._background_tasks and not adapter._pending_messages and not adapter._text_debounce
 assert not adapter._pending_text_batch_tasks and not adapter._pending_text_batches
asyncio.run(main())
''')
