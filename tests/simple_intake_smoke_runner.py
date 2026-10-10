"""Load the real SDK; substitute only external Feishu and GitHub services."""
import os
from pathlib import Path
import sys
import json


scratch = Path(sys.argv[1]).resolve()
scenario = sys.argv[2]
model_address = None
protected_home = Path.home()
real_model = json.loads(Path(os.environ['HPM_NATIVE_REAL_MODEL_REFERENCE']).read_text()) if os.environ.get('HPM_NATIVE_REAL_MODEL_REFERENCE') else None
approved_files = {Path(os.environ['HPM_NATIVE_REAL_MODEL_REFERENCE']).resolve(), Path(real_model['env_file']).resolve()} if real_model else set()
for name in ('os-home', 'os-state', 'os-config'):
    (scratch / name).mkdir(mode=0o700)
os.environ['HOME'] = str(scratch / 'os-home')
os.environ['XDG_STATE_HOME'] = str(scratch / 'os-state')
os.environ['XDG_CONFIG_HOME'] = str(scratch / 'os-config')
os.environ['HERMES_SKIP_PM_BOOTSTRAP'] = '1'
os.environ['HERMES_DISABLE_PROJECT_PLUGINS'] = '1'
os.environ['HERMES_FEISHU_TEXT_BATCH_DELAY_SECONDS'] = '0.01'
os.environ['HERMES_FEISHU_TEXT_BATCH_SPLIT_DELAY_SECONDS'] = '0.01'


def audit(event, args):
    if event == 'open' and isinstance(args[0], (str, bytes)):
        path = Path(os.fsdecode(args[0])).resolve()
        if path not in approved_files and any(path.is_relative_to(protected_home / p) for p in ('.hermes', '.config', '.local/state/hermes')):
            raise RuntimeError('Smoke refuses existing personal runtime state.')
    if event == 'socket.connect' and isinstance(args[1], tuple) and args[1][:2] != model_address:
        raise RuntimeError('Smoke refuses external network access.')
    if event == 'import' and args[0] == 'hermes_cli.main' and scenario not in {'worker_dispatch', 'worker_transport_timeout'}:
        raise RuntimeError('Intake cannot launch native workers.')


sys.addaudithook(audit)
import hermes_bootstrap  # noqa: E402,F401
import asyncio  # noqa: E402
import json  # noqa: E402
import hashlib  # noqa: E402
import shlex  # noqa: E402
import subprocess  # noqa: E402
import types  # noqa: E402
import yaml  # noqa: E402
from feishu_service_support import service_client, connect_service, receive  # noqa: E402
from gateway.config import Platform, PlatformConfig, GatewayConfig  # noqa: E402
from gateway.run import GatewayRunner  # noqa: E402
from simple_model_boundary import OrdinaryModelService  # noqa: E402
from hermes_cli.plugins import get_plugin_manager  # noqa: E402
from gateway.platform_registry import platform_registry  # noqa: E402
from lark_oapi.api.im.v1 import P2ImMessageReceiveV1, ReplyMessageResponse  # noqa: E402
from tools.registry import registry  # noqa: E402

home = scratch / 'home'
if scenario in {'worker_dispatch', 'worker_query_race', 'worker_transport_timeout'}:
    from simple_worker_model import WorkerModelService, execution_reference
    model = WorkerModelService(scratch / 'state/repository-intake.lock' if scenario == 'worker_query_race' else None,
                               refuse=os.environ.get('HPM_SYNTHETIC_WORKER_REFUSE') == '1', hold_dsh=scenario == 'worker_transport_timeout')
else:
    model = OrdinaryModelService()
model_address = ('127.0.0.1', model.server.server_port)
state = scratch / 'state'
repo = scratch / 'repo'
repo.mkdir()
subprocess.run(['git', 'init', '-q', str(repo)], check=True)
bin_dir = scratch / 'bin'
bin_dir.mkdir(mode=0o700)
gh = bin_dir / 'gh'
gh.write_text('#!' + sys.executable + '\n' + '''
import json, os, sys
from pathlib import Path
base = Path(os.environ['SYNTHETIC_GH_STATE'])
args = sys.argv[1:]
with (base / 'gh-calls').open('a') as log:
    log.write(json.dumps(args) + '\\n')
if args[:2] == ['auth', 'switch']:
    pass
elif args[:2] == ['auth', 'token']:
    print('synthetic-configured-account-token')
elif args[:2] == ['api', 'user']:
    print('fixture-user')
elif args[:2] == ['issue', 'view']:
    if (os.environ.get('SYNTHETIC_GH_SCENARIO', '').startswith('issue_unavailable') or os.environ.get('SYNTHETIC_GH_SCENARIO') == 'queued_rejection_unload') and not (base / 'issue-source-recovered').exists():
        if os.environ['SYNTHETIC_GH_SCENARIO'] in {'issue_unavailable_unload', 'queued_rejection_unload'}:
            import time
            (base / 'issue-view-entered').write_text('entered')
            until = time.monotonic() + 5
            while not (base / 'issue-view-release').exists() and time.monotonic() < until:
                time.sleep(0.01)
        print('A synthetic failing source contained synthetic-unused-secret.', file=sys.stderr)
        sys.exit(1)
    print(json.dumps({'url': args[2], 'title': 'Add a bounded fixture feature',
                     'body': os.environ.get('SYNTHETIC_GH_BODY', 'Implement the requested feature and verify it.'),
                     'updatedAt': '2026-10-10T00:00:00Z'}))
elif args[0] == 'api' and '--method' in args:
    body = json.loads(sys.stdin.read())
    issue = {'html_url': 'https://github.com/fixture-user/fixture/issues/8',
             'title': body['title'], 'body': body['body'], 'updated_at': '2026-10-10T00:00:00Z'}
    (base / 'created-issue').write_text(json.dumps(issue))
    if os.environ.get('SYNTHETIC_GH_SCENARIO') == 'create_unknown':
        sys.exit(1)
    print(json.dumps(issue))
elif args[0] == 'api' and '--slurp' in args:
    issue_file = base / 'created-issue'
    print(json.dumps([[json.loads(issue_file.read_text())]] if issue_file.exists() else [[]]))
else:
    sys.exit(2)
''')
gh.chmod(0o700)
os.environ['PATH'] = str(bin_dir) + os.pathsep + os.environ['PATH']
os.environ['SYNTHETIC_GH_STATE'] = str(scratch)
os.environ['SYNTHETIC_GH_SCENARIO'] = scenario
gh_config = scratch / 'gh-config'
gh_config.mkdir(mode=0o700)
model_key = 'synthetic-model-only-key'
if real_model:
    lines = Path(real_model['env_file']).read_text().splitlines()
    model_key = next(line.split('=', 1)[1].strip().strip('\"\'') for line in lines if line.startswith(real_model['api_key_env'] + '='))
(home / '.env').write_text('HERMES_PM_FEISHU_ALLOWED_USERS=u_owner,on_owner\nOPENAI_API_KEY=' + model_key + '\n')
(home / '.env').chmod(0o600)
binding = {'sender_tenant_key': 'tenant-owner', 'recipient_tenant_key': 'tenant-bot',
           'transport_tenant_key': 'tenant-app', 'verification_ref': 'fixture:identity-map',
           'app_id': 'cli_fixture', 'recipient_open_id': 'ou_lead', 'owner_open_id': 'ou_owner',
           'owner_native_ids': ['u_owner', 'on_owner'], 'chat_id': 'oc_fixture',
           'project_id': 'project-fixture', 'profile_id': 'lead', 'native_profile': 'default',
           'repository': 'fixture-user/fixture', 'repo_path': str(repo),
           'superior_profile_id': 'steward', 'capability': 'development', 'issue_creation_allowed': True}
settings = {'state_dir': str(state), 'owner_identity_ref': 'fixture:owner',
            'simple_development': {'enabled': True, 'github_account': 'fixture-user',
                                   'github_config_dir': str(gh_config)},
            'feishu_intake': {'enabled': True, 'verification_ref': 'fixture:controlled-smoke',
                              'bindings': [binding]}}
if scenario in {'worker_dispatch', 'worker_query_race', 'worker_transport_timeout'}:
    binding['execution_ref'] = os.environ.get('HPM_NATIVE_EXECUTION_REFERENCE') or execution_reference(scratch, model.base_url, Path(os.environ['DSH_TEST_SDK_ROOT']))
    os.environ['SYNTHETIC_GH_BODY'] = ('Create native-delivery.txt containing exactly native-worker followed by one newline. This bounded synthetic task has only three steps. '
        + ('1. Load the tdd skill once with the original skill tool. ' if real_model else '1. Load the fixture-matt skill once with the original skill tool. ')
        + 'Use the returned skill content; do not inspect skill directories or SKILL.md with Bash. '
          "2. In one original Bash call, write native-delivery.txt, compare the target file's exact bytes, and print the target file. "
          "Execute exactly this command: `printf 'native-worker\\n' > native-delivery.txt && printf 'native-worker\\n' | cmp - native-delivery.txt && cat native-delivery.txt`. "
          'The only target file is native-delivery.txt and the exact target content is native-worker followed by one newline. '
          '3. After the command exits successfully, give the final reply reporting the actual result, then await outer acceptance. '
          'Do not run further commands or validations, ask additional questions, or change other files.')
if scenario == 'delegation':
    settings['simple_development']['work_profiles'] = [dict(binding, native_profile='fixture-worker')]
    binding = dict(binding, profile_id='steward', project_id=None, capability='coordination',
                   target_profiles=['lead'], native_profile='default')
    binding.pop('repository')
    binding.pop('repo_path')
    settings['feishu_intake']['bindings'] = [binding]
if scenario == 'authority':
    settings['feishu_intake']['registered_bots'] = [{'app_id': 'cli_fixture', 'profile_id': 'steward',
        'identity_ref': 'fixture:steward', 'tenant_key': 'tenant-steward', 'open_id': 'ou_steward',
        'native_ids': ['u_steward'], 'dispatch_profiles': ['lead']}]
if scenario == 'private_all_bindings':
    binding.update(profile_id='rd', project_id='p1', superior_profile_id='pm', name='甲')
    settings['feishu_intake']['registered_bots'] = [{'app_id': 'cli_fixture', 'profile_id': 'q2',
        'identity_ref': 'fixture:archive-safe', 'tenant_key': 'tenant-distinct', 'open_id': 'ou_distinct',
        'native_ids': ['u_distinct'], 'dispatch_profiles': ['rd']}]
if scenario == 'responsibility_conflict':
    binding['target_profiles'] = ['lead', 'other-lead']
    settings['simple_development']['work_profiles'] = [dict(binding, profile_id='other-lead', native_profile='other-worker')]
if scenario == 'configuration_repository':
    binding['repo_path'] = str(scratch / 'missing-repository')
(home / 'config.yaml').write_text(yaml.safe_dump({'kanban': {'auto_decompose': scenario == 'configuration_auto_decompose'}, 'toolsets': ['kanban', 'hermes_pm'] if scenario in {'worker_dispatch', 'worker_query_race', 'worker_transport_timeout'} else ['kanban'],
    'platform_toolsets': {'cli': ['hermes_pm_supervision']},
    'known_plugin_toolsets': {'cli': ['hermes_pm', 'hermes_pm_supervision']},
    'tools': {'tool_search': {'enabled': 'off'}},
    'model': {'default': real_model['model'] if real_model else 'fixture-model', 'provider': 'custom',
              'base_url': real_model['base_url'] if real_model else model.base_url, 'api_mode': 'chat_completions',
              'key_env': 'OPENAI_API_KEY',
              'max_tokens': real_model['max_tokens_per_request'] if real_model else 256,
              'context_length': real_model['context_window'] if real_model else 131072},
    'agent': {'max_turns': 8},
    'plugins': {'enabled': ['ghost-hermes-pm'], 'entries': {'ghost-hermes-pm': {'settings': settings}}}}))
if scenario in {'worker_dispatch', 'worker_query_race', 'worker_transport_timeout'} and not real_model:
    from hermes_cli.auth import PROVIDER_REGISTRY
    from hermes_cli.providers import HERMES_OVERLAYS
    from agent.models_dev import PROVIDER_TO_MODELS_DEV
    prepared_config = yaml.safe_load((home / 'config.yaml').read_text())
    prepared_config['model_catalog'] = {'excluded_providers': sorted({
        *PROVIDER_REGISTRY, *HERMES_OVERLAYS, *PROVIDER_TO_MODELS_DEV, *PROVIDER_TO_MODELS_DEV.values(), 'custom'})}
    prepared_config['models_dev'] = {'url': model.base_url.rsplit('/v1', 1)[0] + '/models-dev'}
    (home / 'config.yaml').write_text(yaml.safe_dump(prepared_config))
    os.environ.update(HTTP_PROXY=model.base_url, HTTPS_PROXY=model.base_url, ALL_PROXY=model.base_url,
                      NO_PROXY='127.0.0.1,localhost')
plugins = get_plugin_manager()
plugins.discover_and_load()
from hermes_logging import setup_logging, flush_log_queue  # noqa: E402
setup_logging(hermes_home=home, mode='gateway')
assert platform_registry.get('hermes_feishu_pm') is not None
assert platform_registry.get('feishu') is None


def raw(mid, text='@_user_1 派发 https://github.com/fixture-user/fixture/issues/7', sender=None, **changes):
    value = {'schema': '2.0', 'header': {'event_type': 'im.message.receive_v1',
        'app_id': 'cli_fixture', 'tenant_key': 'tenant-app'}, 'event': {'sender': {'sender_type': 'user',
        'tenant_key': 'tenant-owner', 'sender_id': {'open_id': 'ou_owner', 'user_id': 'u_owner', 'union_id': 'on_owner'}},
        'message': {'message_id': mid, 'chat_id': 'oc_fixture', 'chat_type': 'group', 'message_type': 'text',
            'content': json.dumps({'text': text}), 'mentions': [{'key': '@_user_1', 'mentioned_type': 'bot',
                'tenant_key': 'tenant-bot', 'id': {'open_id': 'ou_lead'}}]}}}
    value['event']['message'].update(changes)
    if sender is not None:
        value['event']['sender'].update(sender)
    return P2ImMessageReceiveV1(value)


async def main():
    global native_runner, paused_original
    runner = GatewayRunner(GatewayConfig(multiplex_profiles=False))
    native_runner = runner
    runner.config.profile_routes = []
    platform = Platform('hermes_feishu_pm')
    adapter = runner._create_adapter(platform, PlatformConfig(enabled=True, extra={
        'app_id': 'cli_fixture', 'app_secret': 'synthetic-unused-secret', 'require_mention': True,
        'default_group_policy': 'open', 'allow_bots': 'mentions' if scenario == 'authority' else 'none'}))
    assert adapter is not None
    runner.adapters[platform] = adapter
    runner._wire_adapter_handlers(adapter)
    native = service_client('cli_fixture')
    verifications = []
    def verify(request):
        verifications.append(request)
        return types.SimpleNamespace(code=0, raw=types.SimpleNamespace(
            content=b'{"code":0,"bot":{"open_id":"ou_lead","activate_status":2}}'))
    native.request = verify
    sent = []
    def reply(request):
        sent.append(request)
        return ReplyMessageResponse({'code': 0, 'data': {'message_id': 'om_sent_' + str(len(sent)),
            'chat_id': 'oc_fixture', 'parent_id': request.message_id}})
    native.im.v1.message.reply = reply
    await connect_service(adapter, native)
    if scenario.startswith('configuration_'):
        await receive(adapter, raw('om_configuration'))
        assert len(sent) == 1 and sent[0].message_id == 'om_configuration'
        assert '配置' in sent[0].request_body.content and '未受理' in sent[0].request_body.content
        assert json.loads(registry.dispatch('hermes_pm_snapshot', {}))['work'] == []
        assert not (scratch / 'gh-calls').exists()
        from tools import kanban_tools  # noqa: F401
        assert json.loads(registry.dispatch('kanban_list', {}))['count'] == 0
        assert plugins.unload('ghost-hermes-pm')
        await asyncio.sleep(0.05)
        assert not adapter.is_connected
        (scratch / 'simple-smoke-result').write_text('passed')
        return
    if scenario.startswith('issue_unavailable') or scenario == 'queued_rejection_unload':
        if scenario in {'issue_unavailable_unload', 'queued_rejection_unload'}:
            receiving = asyncio.create_task(receive(adapter, raw('om_work')))
            for _ in range(250):
                if (scratch / 'issue-view-entered').exists():
                    break
                await asyncio.sleep(0.01)
            assert (scratch / 'issue-view-entered').exists()
            queued = None
            if scenario == 'queued_rejection_unload':
                queued = asyncio.create_task(receive(adapter, raw('om_waiting', '@_user_1 派发 https://github.com/fixture-user/other/issues/7')))
                for _ in range(250):
                    if len(verifications) >= 2:
                        break
                    await asyncio.sleep(0.01)
                assert len(verifications) == 2
                await asyncio.sleep(0.01)
            assert plugins.unload('ghost-hermes-pm')
            (scratch / 'issue-view-release').write_text('release')
            await asyncio.wait_for(receiving, timeout=6)
            if queued is not None:
                await asyncio.wait_for(queued, timeout=6)
            assert sent == [], 'An ended plugin generation cannot emit failure replies.'
        else:
            await receive(adapter, raw('om_work'))
            record = json.loads(registry.dispatch('hermes_pm_snapshot', {}))['work'][0]
            assert record['card_id'] is None
            assert len(sent) == 1 and sent[0].message_id == 'om_work'
            assert record['id'] in sent[0].request_body.content
            assert '待核对' in sent[0].request_body.content and 'synthetic-unused-secret' not in sent[0].request_body.content
            assert len([call for call in map(json.loads, (scratch / 'gh-calls').read_text().splitlines())
                        if call[:2] == ['issue', 'view']]) == 1, 'Failure does not trigger an automatic read retry.'
            (scratch / 'issue-source-recovered').write_text('recovered')
            await receive(adapter, raw('om_reconcile', '@_user_1 核对 ' + record['id']))
            recovered = json.loads(registry.dispatch('hermes_pm_snapshot', {}))['work']
            assert len(recovered) == 1 and recovered[0]['id'] == record['id']
            assert recovered[0]['card_id'] and recovered[0]['issue']['url'].endswith('/7')
            assert len(sent) == 2 and sent[-1].message_id == 'om_reconcile'
            assert plugins.unload('ghost-hermes-pm')
        assert not (scratch / 'created-issue').exists(), 'A failed existing Issue read must never create a replacement Issue.'
        await asyncio.sleep(0.05)
        assert not adapter.is_connected
        (scratch / 'simple-smoke-result').write_text('passed')
        return
    if scenario.startswith('wrong_card_'):
        from tools import kanban_tools  # noqa: F401
        issue_url = 'https://github.com/fixture-user/fixture/issues/7'
        marker = '<!-- hermes-outer:' + hashlib.sha256(issue_url.encode()).hexdigest() + ' -->'
        body = '总 Issue：' + issue_url + '\n\n已受理范围（冻结）：\nAdd a bounded fixture feature\nImplement the requested feature and verify it.\n\n' + marker
        assignee, workspace_path = 'default', str(repo)
        if scenario == 'wrong_card_assignee':
            assignee = 'outside'
        if scenario == 'wrong_card_path':
            other_repo = scratch / 'other-repo'
            other_repo.mkdir()
            subprocess.run(['git', 'init', '-q', str(other_repo)], check=True)
            workspace_path = str(other_repo)
        if scenario == 'wrong_card_body':
            body = 'Other requested work\n' + marker
        arguments = {'title': 'Add a bounded fixture feature', 'body': body, 'assignee': assignee,
            'workspace_kind': 'dir', 'workspace_path': workspace_path, 'project': '',
            'initial_status': 'blocked', 'idempotency_key': 'hermes-outer:' + hashlib.sha256(issue_url.encode()).hexdigest()}
        preexisting = json.loads(registry.dispatch('kanban_create', arguments))
        assert preexisting['ok']
        if scenario == 'wrong_card_duplicate':
            duplicate = json.loads(registry.dispatch('kanban_create', dict(arguments, idempotency_key='other-key')))
            assert duplicate['ok'] and duplicate['task_id'] != preexisting['task_id']
        await receive(adapter, raw('om_work'))
        record = json.loads(registry.dispatch('hermes_pm_snapshot', {}))['work'][0]
        assert record['state'] == 'card_unknown' and record['card_id'] is None, record
        assert '已受理' not in sent[0].request_body.content
        card = json.loads(registry.dispatch('kanban_show', {'task_id': preexisting['task_id']}))['task']
        assert card['assignee'] == assignee and card['body'] == body and card['workspace_path'] == workspace_path
        assert plugins.unload('ghost-hermes-pm')
        await asyncio.sleep(0.05)
        assert not adapter.is_connected
        (scratch / 'simple-smoke-result').write_text('passed')
        return
    if scenario == 'changed_card':
        await receive(adapter, raw('om_work'))
        record = json.loads(registry.dispatch('hermes_pm_snapshot', {}))['work'][0]
        card_id = record['card_id']
        assert card_id
        from hermes_cli.kanban import run_slash
        edited = run_slash('edit ' + card_id + ' --body ' + shlex.quote('Changed scope after acceptance.'))
        assert 'error' not in edited.lower(), edited
        await receive(adapter, raw('om_changed'))
        changed = json.loads(registry.dispatch('hermes_pm_snapshot', {}))['work'][0]
        assert changed['state'] == 'card_unknown' and changed['card_id'] is None
        assert changed['issue'] == record['issue']
        assert len(sent) == 2 and '已受理' not in sent[-1].request_body.content
        assert plugins.unload('ghost-hermes-pm')
        await asyncio.sleep(0.05)
        assert not adapter.is_connected
        (scratch / 'simple-smoke-result').write_text('passed')
        return
    if scenario == 'create_unknown':
        await receive(adapter, raw('om_work', '@_user_1 工作 Add the requested feature\nVerify the bounded acceptance.'))
        uncertain = json.loads(registry.dispatch('hermes_pm_snapshot', {}))['work'][0]
        assert uncertain['state'] == 'issue_unknown' and uncertain['card_id'] is None
        await receive(adapter, raw('om_reconcile', '@_user_1 核对 ' + uncertain['id']))
        calls = [json.loads(line) for line in (scratch / 'gh-calls').read_text().splitlines()]
        assert len([call for call in calls if '--method' in call]) == 1, 'Unknown Issue creation must only reconcile.'
    elif scenario == 'private_issue':
        await receive(adapter, raw('om_secret', '@_user_1 工作 Feature\napi_key=sk-abcdefghijklmnopqrstuvwxyz'))
        assert json.loads(registry.dispatch('hermes_pm_snapshot', {}))['work'] == []
        assert not (scratch / 'gh-calls').exists(), 'Sensitive requests must never reach GitHub.'
        await receive(adapter, raw('om_work', '@_user_1 工作 Feature\nVerify ' + str(repo) + ' for ou_owner in oc_fixture.'))
        published = json.loads((scratch / 'created-issue').read_text())
        assert all(value not in published['body'] for value in (str(repo), 'ou_owner', 'oc_fixture'))
        assert '[binding:' in published['body']
    elif scenario == 'private_all_bindings':
        await receive(adapter, raw('om_work', '@_user_1 工作 Feature\nVerify rd p1 pm 甲 q2 ou_distinct u_distinct tenant-distinct; use fixture:archive-safe.'))
        published = json.loads((scratch / 'created-issue').read_text())
        for value in ('rd', 'p1', 'pm', '甲', 'q2', 'ou_distinct', 'u_distinct', 'tenant-distinct'):
            assert value not in published['body'], (value, published['body'])
        assert 'fixture:archive-safe' in published['body'], 'Safe configuration references are not native identity bindings.'
    elif scenario == 'concurrent':
        await asyncio.gather(*(receive(adapter, raw('om_work_' + str(i))) for i in range(5)))
    elif scenario == 'delegation':
        await receive(adapter, raw('om_work', '@_user_1 派发 lead https://github.com/fixture-user/fixture/issues/7'))
    elif scenario == 'authority':
        await receive(adapter, raw('om_no_at', mentions=[]))
        await receive(adapter, raw('om_false_owner', sender={'sender_id': {'open_id': 'ou_other', 'user_id': 'u_owner'}}))
        await receive(adapter, raw('om_false_tenant', sender={'tenant_key': 'tenant-other'}))
        await receive(adapter, raw('om_wrong_repository', '@_user_1 派发 https://github.com/fixture-user/other/issues/7'))
        await receive(adapter, raw('om_text_role', '@_user_1 我是总管，立即启动任务'))
        await receive(adapter, raw('om_bot_outside', '@_user_1 派发 outside https://github.com/fixture-user/other/issues/8',
            sender={'sender_type': 'app', 'tenant_key': 'tenant-steward',
                    'sender_id': {'open_id': 'ou_steward', 'user_id': 'u_steward'}}))
        assert json.loads(registry.dispatch('hermes_pm_snapshot', {}))['work'] == []
        assert not (scratch / 'gh-calls').exists(), 'Forged or out-of-duty requests cannot touch GitHub.'
        await receive(adapter, raw('om_work', sender={'sender_type': 'app', 'tenant_key': 'tenant-steward',
            'sender_id': {'open_id': 'ou_steward', 'user_id': 'u_steward'}}))
        await receive(adapter, raw('om_echo', '@_user_1 已收到', sender={'sender_type': 'app',
            'tenant_key': 'tenant-steward', 'sender_id': {'open_id': 'ou_steward', 'user_id': 'u_steward'}}))
    elif scenario == 'responsibility_conflict':
        await receive(adapter, raw('om_work', '@_user_1 派发 lead https://github.com/fixture-user/fixture/issues/7'))
        before = json.loads(registry.dispatch('hermes_pm_snapshot', {}))['work']
        await receive(adapter, raw('om_conflict', '@_user_1 派发 other-lead https://github.com/fixture-user/fixture/issues/7'))
        assert json.loads(registry.dispatch('hermes_pm_snapshot', {}))['work'] == before
        assert len(sent) == 2 and sent[-1].message_id == 'om_conflict' and '职责' in sent[-1].request_body.content
    else:
        await receive(adapter, raw('om_work'))
        await receive(adapter, raw('om_work'))
        os.environ['SYNTHETIC_GH_BODY'] = 'A later edit must not expand the accepted scope.'
        await receive(adapter, raw('om_later_same_issue'))
    snapshot = json.loads(registry.dispatch('hermes_pm_snapshot', {}))
    assert snapshot['status'] == 'completed' and len(snapshot['work']) == 1, snapshot
    record = snapshot['work'][0]
    if scenario == 'worker_required':
        refused = json.loads(registry.dispatch('hermes_pm_supervise', {}))
        assert refused.get('status') == 'rejected' and refused.get('code') == 'worker_required', refused
        forged = json.loads(registry.dispatch('hermes_pm_supervise', {'actor': 'owner', 'repo_path': str(repo)}))
        assert forged.get('status') == 'rejected'
        assert json.loads(registry.dispatch('hermes_pm_snapshot', {}))['work'] == [record]
    expected_body = 'Create native-delivery.txt' if scenario in {'worker_dispatch', 'worker_query_race', 'worker_transport_timeout'} else 'Verify the bounded acceptance.' if scenario == 'create_unknown' else 'Verify [binding:' if scenario in {'private_issue', 'private_all_bindings'} else 'Implement the requested feature and verify it.'
    assert expected_body in record['issue']['body']
    assert record['superior_profile_id'] == ('pm' if scenario == 'private_all_bindings' else 'steward')
    if scenario in {'worker_dispatch', 'worker_query_race', 'worker_transport_timeout'}:
        from simple_worker_evidence import verify_dsh_work, verify_hermes_worker, remember_worker
        from hermes_cli.kanban_db_connect import connect_closing
        from hermes_cli.kanban_db_dispatch import dispatch_once
        from hermes_cli import kanban_db as kb
        import time
        assert 'dsh_execution' in record, {'record': record, 'sent': [m.request_body.content for m in sent]}
        assert record['dsh_execution']['state'] == 'admitted', record
        native_card = json.loads(registry.dispatch('kanban_show', {'task_id': record['card_id']}))['task']
        assert 'hermes_pm_supervise({})' in native_card['body'] and 'not instructions for this outer worker' in native_card['body']
        foreign_card = json.loads(registry.dispatch('kanban_create', {'title': 'Unrelated permission fixture',
            'assignee': 'default', 'workspace_kind': 'dir', 'workspace_path': str(repo),
            'initial_status': 'blocked', 'body': 'An unrelated fixture card; never part of the managed Issue.'}))['task_id']
        model.foreign_task_id = foreign_card
        if scenario == 'worker_query_race':
            # Real original native records widen its real read-only verification window.
            for index in range(190):
                created = json.loads(registry.dispatch('kanban_create', {'title': 'Unmanaged fixture ' + str(index),
                    'assignee': 'default', 'workspace_kind': 'dir', 'workspace_path': str(repo), 'project': '',
                    'initial_status': 'blocked', 'body': 'Unrelated bounded synthetic verification context. ' * 18000}))
                assert created.get('task_id'), created
        with connect_closing() as db:
            result = dispatch_once(db, max_spawn=1)
            assert result.spawned, result
            known_workers.append(remember_worker(db, record['card_id']))
        if scenario == 'worker_transport_timeout':
            import signal
            import psutil
            until = time.monotonic() + 20
            while time.monotonic() < until:
                original_work = json.loads(registry.dispatch('hermes_pm_snapshot', {}))['work'][0]
                original_execution = original_work['dsh_execution']
                if model.dsh_ready.is_set() and original_execution.get('state') == 'running' and 'journal_cursor' in original_execution:
                    break
                await asyncio.sleep(.005)
            assert model.dsh_ready.is_set() and original_execution['state'] == 'running' and 'journal_cursor' in original_execution
            directory = state / 'owned-work' / record['id']
            identity = json.loads((directory / 'native-identity.json').read_text())
            birth = json.loads((directory / 'process-birth.json').read_text())
            process = psutil.Process(identity['pid'])
            assert identity['generation'] == original_execution['generation'] and process.create_time() == birth['created_at']
            paused_original = {'pid': identity['pid'], 'birth': birth['created_at']}
            input_receipt = (directory / 'home/.hermes-first-input.json').read_bytes()
            input_model_count = len([r for r in model.requests if not r['worker']])
            os.kill(process.pid, signal.SIGSTOP)
            try:
                until = time.monotonic() + 25
                while time.monotonic() < until:
                    unknown_work = json.loads(registry.dispatch('hermes_pm_snapshot', {}))['work'][0]
                    unknown = unknown_work['dsh_execution']
                    if unknown['state'] == 'outcome_unknown':
                        break
                    await asyncio.sleep(.025)
                assert unknown['state'] == 'outcome_unknown', unknown
                assert unknown['transport_failure']['type'] == 'Empty', unknown
                assert all(unknown[k] == original_execution[k] for k in ('generation', 'instance_id', 'session_id', 'request_id', 'first_input', 'native_identity'))
                assert unknown['first_input'] == 'accepted' and unknown_work['target']['repo_path'] == str(repo)
                assert (directory / 'home/.hermes-first-input.json').read_bytes() == input_receipt
                assert len([r for r in model.requests if not r['worker']]) == input_model_count == 1
                assert not (repo / 'native-delivery.txt').exists()
            finally:
                assert psutil.Process(paused_original['pid']).create_time() == paused_original['birth']
                os.kill(paused_original['pid'], signal.SIGCONT)
                paused_original = None
                model.dsh_release.set()
            await asyncio.to_thread(psutil.Process(known_workers[0]['pid']).wait, timeout=40)
            with connect_closing() as db:
                terminal = kb.get_task(db, record['card_id'])
                terminal_run = kb.latest_run(db, record['card_id'])
            assert terminal.status == 'blocked' and terminal.claim_lock is None and terminal_run.outcome == 'blocked'
            assert '[kanban-worker-exit] rc=0' in (home / 'kanban/logs' / (record['card_id'] + '.log')).read_text()
            assert known_workers[0]['cli_toolsets'] == ['hermes_pm_supervision']
            from hermes_state import SessionDB
            database = SessionDB(db_path=home / 'state.db', read_only=True)
            try:
                import re
                worker_log = (home / 'kanban/logs' / (record['card_id'] + '.log')).read_text()
                session_id = re.findall(r'^Session: *([^\s]+)', worker_log, re.MULTILINE)[-1]
                messages = database.get_messages(session_id)
                results = [json.loads(m['content']) for m in messages if m['role'] == 'tool' and m.get('tool_name') == 'hermes_pm_supervise']
                handoff = next(r for r in results if r.get('code') == 'outcome_unknown')
                assert handoff['next_action'] == 'kanban_block' and handoff['block_kind'] == 'needs_input'
                assert handoff['repository_retained'] is True and handoff['delivered'] is False
                assert any(m['role'] == 'tool' and m.get('tool_name') == 'kanban_block' and json.loads(m['content']).get('ok') is True for m in messages)
            finally:
                database.close()
            assert all(r['authentication_accepted'] for r in model.requests)
            assert all('hermes_pm_supervise' in r['tool_names'] and all(n.startswith('kanban_') or n == 'hermes_pm_supervise' for n in r['tool_names']) for r in model.requests if r['worker'])
            await receive(adapter, raw('om_unknown_status', text='@_user_1 核对 ' + record['id']))
            assert '原执行结果未知，保留仓库占用' in sent[-1].request_body.content
            assert json.loads(registry.dispatch('hermes_pm_snapshot', {}))['work'][0]['dsh_execution']['state'] == 'outcome_unknown'
            assert len(list((state / 'owned-work').glob('*/native-configuration.json'))) == 1
            (scratch / 'worker-unknown-evidence.json').write_text(json.dumps({'state': 'outcome_unknown',
                'generation': unknown['generation'], 'session_id': unknown['session_id'], 'request_id': unknown['request_id'],
                'first_input': unknown['first_input'], 'transport_failure': unknown['transport_failure'],
                'native_handoff': handoff, 'native_card_status': terminal.status, 'worker_exit_code': 0, 'dsh_request_count': input_model_count}))
            (scratch / 'worker-unknown-evidence.json').chmod(0o600)
            assert plugins.unload('ghost-hermes-pm')
            (scratch / 'simple-smoke-result').write_text('passed')
            return
        if scenario == 'worker_query_race':
            until = time.monotonic() + 30
            while not model.query_ready.is_set() and time.monotonic() < until:
                await asyncio.sleep(.01)
            assert model.query_ready.is_set()
            before_query = json.loads(registry.dispatch('hermes_pm_snapshot', {}))['work'][0]['dsh_execution']
            await receive(adapter, raw('om_live_status', text='@_user_1 核对 ' + record['id']))
            import importlib
            supervision = next(m for n, m in sys.modules.items() if n.endswith('.ghost_hermes_pm.repository_supervision'))
            carrier_type = importlib.import_module(supervision.__package__ + '.dsh_owned_transport').PersistentOwnedTransport
            saved_path = state / 'owned-work' / record['id'] / 'native-configuration.json'
            cfg = json.loads(saved_path.read_text())
            observed = carrier_type(instance_dir=str(saved_path.parent), configuration={k: v for k, v in cfg.items()
                if k not in {'socket_path', 'identity_path', 'exit_path', 'configuration_sha256', 'source_bindings', 'home_identity'}}, timeout=15)
            try:
                until = time.monotonic() + 10
                while time.monotonic() < until:
                    events, _ = await asyncio.to_thread(supervision.original_history, observed, before_query['session_id'], str(repo))
                    ends = [e for e in events if e['type'] == 'turn/end']
                    with connect_closing() as db:
                        terminal = kb.get_task(db, record['card_id'])
                    if ends and terminal.status == 'blocked':
                        break
                    await asyncio.sleep(.05)
                assert ends[-1]['data']['reason']['kind'] == 'completed' and terminal.status == 'blocked'
                current = json.loads(registry.dispatch('hermes_pm_snapshot', {}))['work'][0]
                assert current['notification_claimed'] is True
                assert all(current['dsh_execution'][k] == before_query[k] for k in ('generation', 'session_id', 'request_id', 'first_input'))
                assert current['dsh_execution']['state'] == 'awaiting_acceptance', {'card_status': terminal.status,
                    'original_turn_end': ends[-1], 'work_state': current['dsh_execution']['state']}
            finally:
                observed.close()
        until = time.monotonic() + (180 if real_model else 60)
        while time.monotonic() < until:
            current = json.loads(registry.dispatch('hermes_pm_snapshot', {}))['work'][0]
            if current['dsh_execution']['state'] in {'awaiting_acceptance', 'budget_stopped', 'outcome_unknown'}:
                break
            if current['dsh_execution']['state'] == 'admitted':
                import psutil
                try:
                    worker_process = psutil.Process(known_workers[-1]['pid'])
                    if worker_process.status() == psutil.STATUS_ZOMBIE:
                        break
                except psutil.NoSuchProcess:
                    break
            await asyncio.sleep(.1)
        assert current['dsh_execution']['state'] == 'awaiting_acceptance', current
        assert (repo / 'native-delivery.txt').read_text() == 'native-worker\n'
        await receive(adapter, raw('om_status', text='@_user_1 核对 ' + record['id']))
        assert sent[-1].message_id == 'om_status' and '开发与测试结果待验收，尚未交付' in sent[-1].request_body.content
        assert record['issue']['url'] in sent[-1].request_body.content
        tasks = json.loads(registry.dispatch('kanban_list', {'limit': 200}))['tasks']
        assert sum(record['issue']['url'] in row.get('body', '') or row['id'] == record['card_id'] for row in tasks) == 1, 'Worker cannot create a second outer queue.'
        with connect_closing() as db:
            run = kb.latest_run(db, record['card_id'])
        import psutil
        try:
            await asyncio.to_thread(psutil.Process(run.worker_pid).wait, timeout=40)
        except psutil.NoSuchProcess:
            pass
        with connect_closing() as db:
            original = kb.get_task(db, record['card_id'])
            run = kb.latest_run(db, record['card_id'])
            assert original.status == 'blocked' and original.claim_lock is None and run.outcome == 'blocked'
            assert kb.get_task(db, foreign_card).status == 'blocked' and kb.latest_run(db, foreign_card) is None
        assert '[kanban-worker-exit] rc=0' in (home / 'kanban/logs' / (record['card_id'] + '.log')).read_text()
        assert known_workers[0]['cli_toolsets'] == ['hermes_pm_supervision'], known_workers[0]
        if not real_model:
            assert all(row['authentication_accepted'] for row in model.requests)
            worker_catalogs = [row['tool_names'] for row in model.requests if row['worker']]
            assert all(
                'hermes_pm_supervise' in names and all(name.startswith('kanban_') or name == 'hermes_pm_supervise' for name in names)
                for names in worker_catalogs), {'actual_cli_toolsets': known_workers[0]['cli_toolsets'], 'actual_worker_catalogs': worker_catalogs}
            assert any(row['worker'] and 'hermes_pm_supervise' in row['tool_names'] for row in model.requests)
            assert any(not row['worker'] and 'bash' in row['tool_names'] for row in model.requests)
            assert all(not row['outer_instructions_present'] for row in model.requests if not row['worker'])
            assert any(not row['worker'] and row['skill_catalog'] and 'skill' in row['tool_names'] for row in model.requests), 'Original DSH must load the approved skill catalog.'
        execution = current['dsh_execution']
        import importlib
        supervision = next(m for n, m in sys.modules.items() if n.endswith('.ghost_hermes_pm.repository_supervision'))
        PersistentOwnedTransport = importlib.import_module(supervision.__package__ + '.dsh_owned_transport').PersistentOwnedTransport
        native_config = json.loads((state / 'owned-work' / record['id'] / 'native-configuration.json').read_text())
        dsh_evidence = verify_dsh_work(execution, native_config, skill_name='tdd' if real_model else 'fixture-matt')
        hermes_evidence = verify_hermes_worker(home, record['card_id'], model_name=real_model['model'] if real_model else 'fixture-model',
                                              base_url=real_model['base_url'] if real_model else model.base_url)
        (scratch / 'worker-validation-evidence.json').write_text(json.dumps({'dsh': dsh_evidence, 'hermes': hermes_evidence,
            'worker_cli_toolsets': known_workers[0]['cli_toolsets'],
            'worker_advertised_catalogs': worker_catalogs if not real_model else None,
            'real_model': bool(real_model), 'fake_model_requests': len(model.requests) if real_model else None}))
        (scratch / 'worker-validation-evidence.json').chmod(0o600)
        if real_model:
            assert model.requests == [], 'Both real flags must leave the synthetic model service unused.'
        carrier = PersistentOwnedTransport(instance_dir=str(state / 'owned-work' / record['id']),
            configuration={k: v for k, v in native_config.items() if k not in {'socket_path', 'identity_path', 'exit_path', 'configuration_sha256', 'source_bindings', 'home_identity'}},
            timeout=15)
        carrier.shutdown_owned()
        assert carrier.close_outcome['kind'] == 'original_exit', carrier.close_outcome
        assert plugins.unload('ghost-hermes-pm')
        (scratch / 'simple-smoke-result').write_text('passed')
        return
    assert record['execution'] == 'not_enabled'
    card = json.loads(registry.dispatch('kanban_show', {'task_id': record['card_id']}))['task']
    assert card['status'] == 'blocked' and card['workspace_kind'] == 'dir'
    assert card['workspace_path'] == str(repo) and card['assignee'] == ('fixture-worker' if scenario == 'delegation' else 'default')
    assert record['issue']['url'] in card['body']
    assert expected_body in card['body']
    assert not (state / 'manager.sock').exists(), 'New work cannot open the old dispatch manager.'
    if scenario == 'authority':
        for _ in range(1000):
            if any('Native ordinary reply.' in message.request_body.content for message in sent):
                break
            await asyncio.sleep(0.02)
        assert any('Native ordinary reply.' in message.request_body.content for message in sent), {
            'sent': [message.request_body.content for message in sent], 'model_requests': model.requests,
            'native_diagnostics': (home / 'logs/gateway.log').read_text()[-6000:]}
        assert model.requests, 'Original Gateway ordinary processing must reach the external model boundary.'
    else:
        assert len(sent) == (2 if scenario in {'create_unknown', 'private_issue', 'responsibility_conflict'} else 1), len(sent)
        assert model.requests == [], 'The registered intake consumes work before native ordinary model processing.'
    listing = json.loads(registry.dispatch('kanban_list', {}))
    assert listing.get('count') == 1 and len(listing['tasks']) == 1, listing
    calls = [json.loads(line) for line in (scratch / 'gh-calls').read_text().splitlines()]
    for offset, call in enumerate(calls):
        if call[:2] == ['issue', 'view'] or call[0] == 'api' and call[1] != 'user':
            assert calls[offset - 2][:2] == ['auth', 'switch'] and calls[offset - 1][:2] == ['api', 'user']
    assert plugins.unload('ghost-hermes-pm')
    await asyncio.sleep(0.05)
    assert not adapter.is_connected and adapter.intake.closed
    plugins.discover_and_load(force=True)
    assert platform_registry.get('hermes_feishu_pm') is not None
    reloaded = json.loads(registry.dispatch('hermes_pm_snapshot', {}))
    assert len(reloaded['work']) == 1 and reloaded['work'][0]['card_id'] == record['card_id']
    assert plugins.unload('ghost-hermes-pm')
    (scratch / 'simple-smoke-result').write_text('passed')


native_runner = None
known_workers = []
paused_original = None


async def with_cleanup():
    primary = None
    try:
        await main()
    except BaseException as error:
        primary = error
        raise
    finally:
        if paused_original is not None:
            import signal
            import psutil
            assert psutil.Process(paused_original['pid']).create_time() == paused_original['birth']
            os.kill(paused_original['pid'], signal.SIGCONT)
        if scenario == 'worker_transport_timeout':
            model.dsh_release.set()
        cleanup_errors = []
        worker_exits, carrier_exits = [], []
        if scenario in {'worker_dispatch', 'worker_query_race', 'worker_transport_timeout'}:
            import importlib
            from simple_worker_evidence import cleanup_worker, carrier_exit_evidence
            for worker in known_workers:
                try:
                    worker_exits.append(await asyncio.to_thread(cleanup_worker, worker, home))
                    if primary is None:
                        assert worker_exits[-1]['exit_code'] == 0
                except Exception as error:
                    cleanup_errors.append(type(error).__name__)
            supervision = next(m for n, m in sys.modules.items() if n.endswith('.ghost_hermes_pm.repository_supervision'))
            carrier_type = importlib.import_module(supervision.__package__ + '.dsh_owned_transport').PersistentOwnedTransport
            for saved in (state / 'owned-work').glob('*/native-configuration.json'):
                try:
                    if not (saved.parent / 'native-exit.json').exists():
                        config = json.loads(saved.read_text())
                        carrier = carrier_type(instance_dir=str(saved.parent), configuration={k: v for k, v in config.items()
                            if k not in {'socket_path', 'identity_path', 'exit_path', 'configuration_sha256', 'source_bindings', 'home_identity'}}, timeout=15)
                        carrier.shutdown_owned()
                        assert carrier.close_outcome['kind'] == 'original_exit'
                    carrier_exits.append(carrier_exit_evidence(saved))
                except Exception as error:
                    cleanup_errors.append(type(error).__name__)
        if native_runner is not None:
            try:
                await native_runner.stop()
            except Exception as error:
                cleanup_errors.append(type(error).__name__)
        flush_log_queue()
        if real_model:
            for directory in (home / 'logs', home / 'kanban/logs', state):
                for file in directory.rglob('*'):
                    if file.is_file() and not file.is_symlink() and model_key.encode() in file.read_bytes():
                        cleanup_errors.append('CredentialDiagnosticHit')
        evidence = {'worker_exits': worker_exits, 'carrier_exits': carrier_exits,
                    'logging_flushed_after_stop': True, 'credential_diagnostic_hits': cleanup_errors.count('CredentialDiagnosticHit'),
                    'cleanup_errors': cleanup_errors, 'business_failed': primary is not None}
        (scratch / 'cleanup-evidence.json').write_text(json.dumps(evidence))
        (scratch / 'cleanup-evidence.json').chmod(0o600)
        if primary is not None and known_workers:
            try:
                from simple_worker_evidence import worker_failure_diagnostics
                diagnostics = {'business_failed': True, 'original_workers': worker_failure_diagnostics(home, known_workers)}
                (scratch / 'worker-failure-evidence.json').write_text(json.dumps(diagnostics))
                (scratch / 'worker-failure-evidence.json').chmod(0o600)
                if real_model:
                    assert model_key not in json.dumps(diagnostics), 'Credential appeared in diagnostic evidence.'
            except Exception as error:
                primary.add_note('Original worker diagnostic capture error: ' + type(error).__name__)
        if cleanup_errors:
            if primary is not None:
                primary.add_note('Cleanup/diagnostic verification errors: ' + ','.join(cleanup_errors))
            else:
                raise AssertionError('Cleanup/diagnostic verification errors: ' + ','.join(cleanup_errors))


try:
    asyncio.run(with_cleanup())
finally:
    model.close()
