"""Load the real SDK; substitute only external Feishu and GitHub services."""
import os
from pathlib import Path
import sys


scratch = Path(sys.argv[1]).resolve()
scenario = sys.argv[2]
model_address = None
protected_home = Path.home()
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
        if any(path.is_relative_to(protected_home / p) for p in ('.hermes', '.config', '.local/state/hermes')):
            raise RuntimeError('Smoke refuses existing personal runtime state.')
    if event == 'socket.connect' and isinstance(args[1], tuple) and args[1][:2] != model_address:
        raise RuntimeError('Smoke refuses external network access.')
    if event == 'import' and args[0] == 'hermes_cli.main':
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
(home / '.env').write_text('HERMES_PM_FEISHU_ALLOWED_USERS=u_owner,on_owner\nOPENAI_API_KEY=synthetic-model-only-key\n')
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
(home / 'config.yaml').write_text(yaml.safe_dump({'kanban': {'auto_decompose': scenario == 'configuration_auto_decompose'}, 'toolsets': ['kanban'],
    'model': {'default': 'fixture-model', 'provider': 'custom', 'base_url': model.base_url, 'api_mode': 'chat_completions', 'context_length': 131072},
    'plugins': {'enabled': ['ghost-hermes-pm'], 'entries': {'ghost-hermes-pm': {'settings': settings}}}}))
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
    global native_runner
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
    expected_body = 'Verify the bounded acceptance.' if scenario == 'create_unknown' else 'Verify [binding:' if scenario in {'private_issue', 'private_all_bindings'} else 'Implement the requested feature and verify it.'
    assert expected_body in record['issue']['body']
    assert record['superior_profile_id'] == ('pm' if scenario == 'private_all_bindings' else 'steward')
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


async def with_cleanup():
    try:
        await main()
    finally:
        if native_runner is not None:
            await native_runner.stop()
        flush_log_queue()


try:
    asyncio.run(with_cleanup())
finally:
    model.close()
