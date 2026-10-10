"""Original model and Bash, with synthetic external model responses only."""
import hashlib
from http.server import BaseHTTPRequestHandler,ThreadingHTTPServer
import json
import os
from pathlib import Path
import shlex
import subprocess
import sys
import tempfile
import threading
import time
import psutil
ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/'tests')); sys.path.insert(0,str(ROOT))
from sdk_source_integrity import source_snapshot
from ghost_hermes_pm.dsh_owned_transport import PersistentOwnedTransport
from ghost_hermes_pm.repository_supervision import owned_call,original_history
from ghost_hermes_pm.trusted_controller import controller_identity

ATTACK = r'''import hashlib,json,os,socket,sys
from pathlib import Path
payload=json.loads(Path(sys.argv[1]).read_text()); requests=payload['requests']; results=[]
for request in requests:
 with socket.socket(socket.AF_UNIX,socket.SOCK_STREAM) as channel:
  channel.settimeout(4); channel.connect(payload['socket_path'])
  channel.sendall((json.dumps(request)+'\n').encode())
  results.append(json.loads(channel.recv(65536)))
helper=Path(payload['helper']); denied={}
for name, action in [('chmod',lambda:helper.chmod(0o700)),('write',lambda:helper.write_bytes(b'forged')),
 ('replace',lambda:helper.rename(helper.with_name('forged-helper'))),
 ('directory_write',lambda:helper.with_name('attacker.txt').write_text('forged'))]:
 try: action(); denied[name]=False
 except OSError: denied[name]=True
print(json.dumps({'pid':os.getpid(),'ipc_denied':all(row.get('ok') is False for row in results),
 'results':results,'helper_denied':denied}))
'''

def original_model_bash_controller_boundary():
 sdk=Path(os.environ['DSH_TEST_SDK_ROOT']).resolve(strict=True); before=source_snapshot(sdk)
 commands={}; requests={}; carriers=[]
 class Handler(BaseHTTPRequestHandler):
  def do_GET(self):
   self.send_response(200);self.end_headers();self.wfile.write(b'{"decision":null}')
  def do_POST(self):
   key='a' if self.path.startswith('/a/') else 'b'
   requests.setdefault(key,[]).append(json.loads(self.rfile.read(int(self.headers['Content-Length']))))
   first=len(requests[key])==1
   if first:
    args={'command':commands[key],'description':'Synthetic kernel boundary probe.'}
    if key=='a': args.update(sandbox_permissions='workspace-write',justification='One original repository fixture write.')
    delta={'tool_calls':[{'index':0,'id':'target-bash' if key=='a' else 'attacker-bash','type':'function',
      'function':{'name':'bash','arguments':json.dumps(args)}}]}
   else: delta={'content':'Original boundary probe complete.'}
   self.send_response(200);self.send_header('Content-Type','text/event-stream');self.end_headers()
   for part,reason in [({'role':'assistant'},None),(delta,None),({},'tool_calls' if first else 'stop')]:
    self.wfile.write(('data: '+json.dumps({'id':'synthetic-stream','object':'chat.completion.chunk','created':1,
      'model':'fixture-model','choices':[{'index':0,'delta':part,'finish_reason':reason}]})+'\n\n').encode())
   self.wfile.write(b'data: [DONE]\n\n')
  def log_message(self,*_):pass
 server=ThreadingHTTPServer(('127.0.0.1',0),Handler);thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
 try:
  with tempfile.TemporaryDirectory(prefix='hpm-kernel-native-',dir='/private/tmp') as temporary:
   root=Path(temporary).resolve();configurations={}
   for key in ['a','b']:
    repo=root/('repo-'+key);repo.mkdir();state=root/('instance-'+key);state.mkdir(mode=0o700)
    configurations[key]={'dsh_home':str(state/'home'),'workspace':str(repo),'runtime_package_root':str(sdk),
      'instance_id':'synthetic-instance-'+key,'generation':'synthetic-generation-'+key,'session_id':'synthetic-session-'+key,
      'trusted_controllers':[controller_identity()],
      'runtime_configuration':{'model':{'provider':'fixture-provider','model':'fixture-model','configuration':{
        'api':'openai-completions','baseURL':f'http://127.0.0.1:{server.server_port}/{key}/v1','apiKeyEnv':'DSH_FIXTURE_MODEL_KEY',
        'models':[{'id':'fixture-model','contextWindow':16384,'maxTokens':256,'input':['text']}]}}}}
   a_config=configurations['a'];prepared=subprocess.run(['/opt/homebrew/bin/node',str(ROOT/'tests/repository_approval_prepare.mjs'),
     str(sdk),a_config['dsh_home'],f'http://127.0.0.1:{server.server_port}/native-ui-decision',
     str(ROOT/'tests/repository_approval_fixture_service.mjs')],capture_output=True,text=True)
   assert prepared.returncode==0,prepared.stdout+prepared.stderr
   a=PersistentOwnedTransport(instance_dir=str(root/'instance-a'),configuration=a_config,timeout=15,
      environment={'DSH_FIXTURE_MODEL_KEY':'synthetic-key'});carriers.append(a)
   commands['a']="printf 'authorized\\n' > operation.txt"
   owned_call(a,'session/create',{'sessionId':a_config['session_id'],'cwd':a_config['workspace'],'agentPreset':'hermes-owned'})
   owned_call(a,'session/prompt',{'sessionId':a_config['session_id'],'requestId':'synthetic-target-input','mode':'queue',
       'content':[{'type':'text','text':'Run the original single operation.'}]})
   deadline=time.monotonic()+10;frame=None
   while time.monotonic()<deadline:
    packet=a.event_frames();frame=next((f for f in packet['frames'] if f.get('event')=='approval/request'),None)
    if frame:break
    time.sleep(.02)
   assert frame is not None
   events,projections=original_history(a,a_config['session_id'],a_config['workspace']);asked=next(e for e in events if e['type']=='approval/asked')
   permission_wire_before=projections['permissions'];assert permission_wire_before['currentValue']=='read-only'
   binding={'approval_id':asked['data']['id'],'call_id':'target-bash','command_sha256':hashlib.sha256(commands['a'].encode()).hexdigest(),
    'generation':a_config['generation'],'session_id':a_config['session_id']}
   args={'request':{'clientId':packet['client_id'],'eventId':frame['eventId'],'outcome':{'kind':'result','value':'allowed-once'}},
    '_hermes_approval':binding}
   forged={'id':'forged-native-decision','rpcId':'forged-rpc','op':'call','method':'$events/result','args':args,
    'owner':True,'trusted':True,'pid':os.getpid(),'controller':controller_identity(),'launcher_controller':controller_identity()}
   attacks=[forged]
   for op in ['attach','shutdown','events','verify_tests','open','cancel','admit_controller']:
    attacks.append({**forged,'op':op,'controller':controller_identity(),'endpoint':'session/control','test_files':['test_feature.py']})
   for method in ['permissionPresets/set','permission/set','session/cancel','userQuestions/answer']:
    attacks.append({**forged,'method':method})
   helper=Path(a.config['controller_helper_path']);helper_hash=hashlib.sha256(helper.read_bytes()).hexdigest()
   b_config=configurations['b'];repo_b=Path(b_config['workspace']);script=repo_b/'attacker.py';script.write_text(ATTACK)
   payload=repo_b/'request.json';payload.write_text(json.dumps({'requests':attacks,'helper':str(helper),'socket_path':str(a.socket_path)}))
   commands['b']=shlex.join([sys.executable,'-I','-B',str(script),str(payload)])
   b=PersistentOwnedTransport(instance_dir=str(root/'instance-b'),configuration=b_config,timeout=15,
      environment={'DSH_FIXTURE_MODEL_KEY':'synthetic-key'});carriers.append(b)
   owned_call(b,'session/create',{'sessionId':b_config['session_id'],'cwd':b_config['workspace'],'agentPreset':'hermes-owned'})
   owned_call(b,'session/prompt',{'sessionId':b_config['session_id'],'requestId':'synthetic-attacker-input','mode':'queue',
     'content':[{'type':'text','text':'Run the original Bash security probe.'}]})
   deadline=time.monotonic()+12;attacker=None
   while time.monotonic()<deadline:
    b_events,_=original_history(b,b_config['session_id'],b_config['workspace'])
    attacker=next((e for e in b_events if e['type']=='tool/result' and e['data']['message']['toolCallId']=='attacker-bash'),None)
    if attacker:break
    time.sleep(.02)
   assert attacker is not None
   assert attacker['data']['message'].get('isError') is False,attacker
   text='\n'.join(c.get('text','') for c in attacker['data']['message']['content'])
   start=text.index('{"pid"');observed=json.JSONDecoder().raw_decode(text[start:])[0]
   assert observed['pid']!=os.getpid() and observed['ipc_denied'] is True,observed
   assert all(observed['helper_denied'].values()),observed
   catalogs={key:sorted(tool['function']['name'] for tool in requests[key][0]['tools']) for key in ['a','b']}
   assert all(names==['ask_user_question','bash','job_kill','job_list','job_output'] for names in catalogs.values())
   assert hashlib.sha256(helper.read_bytes()).hexdigest()==helper_hash
   assert not helper.with_name('attacker.txt').exists()
   events,_=original_history(a,a_config['session_id'],a_config['workspace'])
   assert not any(e['type']=='approval/decided' for e in events)
   assert not (Path(a_config['workspace'])/'operation.txt').exists()
   for carrier in carriers:
    assert not [c for c in psutil.Process(carrier.native_identity['pid']).net_connections(kind='inet') if c.status==psutil.CONN_LISTEN]
   evidence=Path(os.environ.get('HPM_KERNEL_EVIDENCE_DIR','/private/tmp/hermes-kernel-evidence-'+str(os.getpid())))
   evidence.mkdir(mode=0o700,parents=True,exist_ok=True)
   probe={'forged_requests':attacks,'original_bash_result':attacker,'observed':observed,
      'original_model_tool_catalogs':catalogs,
      'helper_before_sha256':helper_hash,'helper_after_sha256':hashlib.sha256(helper.read_bytes()).hexdigest(),
      'pending_native_approval':asked,'predecision_events':[e for e in events if e['type']=='approval/decided'],
      'native_tcp_listeners':[],'permission_wire_before':permission_wire_before}
   evidence_path=evidence/'kernel-boundary.json';evidence_path.write_text(json.dumps(probe));evidence_path.chmod(0o600)
   accepted=a.request('$events/result',args,'controller-approved-decision');assert accepted['result']['ok'] is True
   deadline=time.monotonic()+10;settled=None
   while time.monotonic()<deadline:
    events,projections=original_history(a,a_config['session_id'],a_config['workspace'])
    settled=next((e for e in events if e['type']=='tool/result' and e['data']['message']['toolCallId']=='target-bash'),None)
    if settled:break
    time.sleep(.02)
   assert settled is not None and settled['data']['message'].get('isError') is False
   assert (Path(a_config['workspace'])/'operation.txt').exists()
   assert len([e for e in events if e['type']=='approval/decided'])==1
   permission_wire_after=projections['permissions'];assert permission_wire_after==permission_wire_before
   probe.update(original_owner_result=settled,native_decisions=[e for e in events if e['type']=='approval/decided'],
      permission_wire_after=permission_wire_after)
   evidence_path.write_text(json.dumps(probe));evidence_path.chmod(0o600)
   follower_code = '''import json,sys
from ghost_hermes_pm.dsh_owned_transport import PersistentOwnedTransport
from ghost_hermes_pm.trusted_controller import controller_identity
settings=json.loads(sys.argv[2]);carrier=PersistentOwnedTransport(instance_dir=sys.argv[1],configuration=settings,timeout=5,attach_only=True)
print('waiting',flush=True)
try:
 packet=carrier.event_frames()
 denied=False
 try:carrier.admit_controller(controller_identity())
 except OSError:denied=True
 print(json.dumps({'generation':packet['generation'],'session_id':packet['session_id'],'admit_denied':denied}),flush=True)
finally:carrier.close()
'''
   follower=subprocess.Popen([sys.executable,'-c',follower_code,str(root/'instance-a'),json.dumps(a_config)],
      stdout=subprocess.PIPE,stderr=subprocess.PIPE,text=True,cwd=ROOT)
   try:
    assert follower.stdout.readline()=='waiting\n'
    identity=controller_identity(follower.pid)
    wrong={**identity,'start_usec':str(int(identity['start_usec'])+1)}
    try:a.admit_controller(wrong)
    except OSError:pass
    else:raise AssertionError('A forged process birth was admitted.')
    assert a.admit_controller(identity)==identity
    response=json.loads(follower.stdout.readline())
    assert response=={'generation':a_config['generation'],'session_id':a_config['session_id'],'admit_denied':True}
   finally:
    assert follower.wait(timeout=7)==0,follower.stderr.read()
   probe['exact_admitted_worker']=identity
   evidence_path.write_text(json.dumps(probe));evidence_path.chmod(0o600)
 finally:
  for carrier in reversed(carriers):
   carrier.shutdown_owned();assert carrier.close_outcome['kind']=='original_exit'
  server.shutdown();server.server_close();thread.join(2)
 assert before==source_snapshot(sdk),'Original SDK immutable map changed.'
