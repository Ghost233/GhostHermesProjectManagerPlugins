import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from ghost_hermes_pm import Manager, ManagementError
from ghost_hermes_pm.transport import ManagementClient, ManagementServer
from test_directory import OWNER, make_repo
from test_requests import ISSUE
from test_task_execution import accepted, adapter_for


@pytest.mark.parametrize('flags', [['--fixtures'], ['--markers'], ['--setup-plan'], ['--collect-only'], ['test_no_assertions.py']])
def test_public_delivery_requires_executed_assertions(tmp_path, flags):
    repo = make_repo(tmp_path / 'repo')
    (repo / 'test_assertions.py').write_text('def test_assertion():\n    assert True\n')
    (repo / 'test_no_assertions.py').write_text('import pytest\n@pytest.mark.skip(reason="fixture")\ndef test_skipped():\n    assert False\n')
    from readiness_support import ReadyManager
    with ReadyManager(tmp_path / 'state', owner_identity_ref=OWNER.subject, codex_adapter=adapter_for(tmp_path)) as manager:
        request_id = accepted(manager, repo)
        with ManagementServer(manager, {'fixture-entry': OWNER}):
            client = ManagementClient(tmp_path / 'state', 'fixture-entry')
            session = client.start_task(request_id)['session']
            command = [sys.executable, '-m', 'pytest', *flags]
            result = subprocess.run(command, cwd=repo, env={**os.environ, 'PYTHONDONTWRITEBYTECODE': '1'}, text=True, capture_output=True)
            assert result.returncode == 0
            (tmp_path / 'observed.json').write_text(json.dumps({'status': {'type': 'idle'}, 'turns': [{'id': session['turn_id'], 'status': 'completed', 'itemsView': 'full', 'items': [{'type': 'commandExecution', 'id': 'pytest-info', 'command': ' '.join(command), 'cwd': str(repo), 'status': 'completed', 'exitCode': result.returncode, 'aggregatedOutput': result.stdout + result.stderr}]}]}))
            report = {'issue_updated_at': ISSUE['updated_at'], 'criteria': [{'text': ISSUE['body'], 'test_item_ids': ['pytest-info']}], 'source_commit': None, 'pr_url': None, 'sync_branches': []}
            with pytest.raises(ManagementError) as missing:
                client.record_task_delivery(request_id, report)
            assert missing.value.code == 'evidence_missing'
            task = client.read_snapshot()['requests'][0]
            assert task['task_delivery'] == 'unmet'
            assert task['repository_released'] is False


@pytest.mark.parametrize('criterion', ['PR 必须合并到 main', 'The PR must be merged into main', 'Changes must be merged before delivery', '完成 PR 合并后交付'])
def test_public_delivery_honors_frozen_merge_requirement(tmp_path, criterion):
    repo = make_repo(tmp_path / 'repo')
    scope = {**ISSUE, 'body': '- [ ] ' + criterion}
    from readiness_support import ReadyManager
    with ReadyManager(tmp_path / 'state', owner_identity_ref=OWNER.subject, codex_adapter=adapter_for(tmp_path)) as manager:
        request_id = accepted(manager, repo, scope)
        with ManagementServer(manager, {'fixture-entry': OWNER}):
            client = ManagementClient(tmp_path / 'state', 'fixture-entry')
            session = client.start_task(request_id)['session']
            (tmp_path / 'observed.json').write_text(json.dumps({'status': {'type': 'idle'}, 'turns': [{'id': session['turn_id'], 'status': 'completed', 'itemsView': 'full', 'items': [{'type': 'commandExecution', 'id': 'pytest-1', 'command': 'python -m pytest -q', 'cwd': str(repo), 'status': 'completed', 'exitCode': 0, 'aggregatedOutput': '1 passed'}]}]}))
            report = {'issue_updated_at': ISSUE['updated_at'], 'criteria': [{'text': criterion, 'test_item_ids': ['pytest-1']}], 'source_commit': None, 'pr_url': None, 'sync_branches': []}
            with pytest.raises(ManagementError) as missing:
                client.record_task_delivery(request_id, report)
            assert missing.value.code == 'evidence_missing'
            task = client.read_snapshot()['requests'][0]
            assert task['task_delivery'] == 'unmet'
            assert task['repository_released'] is False



def test_migration_bot_identity_cannot_replace_channel_permission(tmp_path):
    from types import SimpleNamespace as NS
    from lark_oapi import Client
    from ghost_hermes_pm.feishu import NativeFeishuTransport
    from ghost_hermes_pm.native_migration import verify_new_bot
    binding = {'profile_id': 'new', 'project_id': 'mono', 'app_id': 'cli_new', 'recipient_open_id': 'ou_new', 'chat_id': 'oc_new', 'recipient_tenant_key': 'tenant-new', 'transport_tenant_key': 'tenant-new', 'owner_open_id': 'ou_owner', 'sender_tenant_key': 'tenant-new'}
    native = Client.builder().app_id('cli_new').app_secret('synthetic-unused').build()
    native.request = lambda request: NS(code=0, raw=NS(content=json.dumps({'code': 0, 'bot': {'open_id': 'ou_new', 'activate_status': 2}}).encode()))
    native.im.v1.chat.get = lambda request: NS(code=99991672, data=None)
    intake = NS(settings={'bindings': [{'profile_id': 'old', 'app_id': 'cli_old', 'recipient_open_id': 'ou_old'}, binding]}, transports=[(object(), NativeFeishuTransport(native))])
    operation = {'digest': 'a' * 64, 'plan': {'source_profile_id': 'old', 'target_profile_id': 'new'}, 'bindings': {'old': {'id': 'old'}, 'new': {'id': 'new', 'project_id': 'mono', 'identity_ref': 'identity:cli_new:ou_new', 'connection_refs': {'bot': 'identity:cli_new:ou_new'}}}}
    with pytest.raises(ManagementError) as blocked:
        verify_new_bot(intake, operation)
    assert blocked.value.code == 'capability_unverified'



def test_configuring_profile_cannot_accept_public_work(tmp_path):
    from test_directory import registration
    from test_requests import MESSAGE
    with Manager(tmp_path / 'state', owner_identity_ref=OWNER.subject) as manager:
        with ManagementServer(manager, {'fixture-entry': OWNER}):
            client = ManagementClient(tmp_path / 'state', 'fixture-entry')
            client.apply_directory_change(0, registration(make_repo(tmp_path / 'repo')))
            with pytest.raises(ManagementError) as blocked:
                manager.accept_request(OWNER, 'mono', 'mono-lead', MESSAGE, ISSUE)
            assert blocked.value.code == 'lifecycle_blocked'
            assert client.read_snapshot()['requests'] == []



def test_native_global_host_has_real_authorized_preparation(tmp_path):
    from ghost_hermes_pm.native_global_validation import configured_global_validation_host
    from test_global_validation import combination, git, LEAD
    from test_repository_queue import queue_adapter
    from readiness_support import ReadyManager
    config = {'host_id': 'local:approved-original-host', 'generation': 'owned-native-generation', 'runner': [sys.executable], 'watcher': [sys.executable, '-c', 'import time; time.sleep(60)'], 'tests': {'unit': ['-c', 'assert True']}, 'environment': {'PATH': '/usr/bin:/bin'}}
    host = configured_global_validation_host(config, tmp_path / 'state')
    with ReadyManager(tmp_path / 'state', owner_identity_ref=OWNER.subject, codex_adapter=queue_adapter(tmp_path), global_validation_host=host) as manager:
        mono, child, parent, kid = combination(manager, tmp_path)
        subprocess.run(['git', '-C', str(child), '-c', 'user.name=Fixture', '-c', 'user.email=fixture@example.invalid', 'commit', '--allow-empty', '-qm', 'different child materialization'], check=True)
        planned = manager.global_validation(LEAD, 'plan', {'request_id': parent, 'mono_commit': git(mono, 'rev-parse', 'HEAD'), 'children': [{'request_id': kid, 'path': 'child'}], 'test_ids': ['unit']})
        with ManagementServer(manager, {'owner': OWNER}):
            client = ManagementClient(tmp_path / 'state', 'owner')
            ready = client.global_validation('prepare', {'validation_id': planned['id'], 'children': [{'request_id': kid, 'commit': planned['children'][0]['commit'], 'path': 'child'}]})
            assert ready['status'] == 'ready'
            assert git(child, 'rev-parse', 'HEAD') == planned['children'][0]['commit']
            assert ready['preparation']['receipt']['related_execution'] == 'ended'
            assert host.find_preparation(ready, ready['preparation']['authorization']) == ready['preparation']['receipt']
            with pytest.raises(ManagementError) as unverified:
                client.global_validation('start', {'validation_id': planned['id']})
            assert unverified.value.code == 'capability_unverified'



def test_native_archive_configuration_queries_original_feishu_source(tmp_path):
    from types import SimpleNamespace as NS
    from lark_oapi.api.im.v1 import ListMessageResponse
    from lark_oapi.api.tenant.v2 import QueryTenantResponse
    from ghost_hermes_pm.archives import configured_providers
    from test_archives import setup_archive, migration_registration
    binding = {'app_id': 'cli_archive', 'tenant_key': 'tenant-archive', 'bot_open_id': 'ou_archive'}
    providers = configured_providers({'local:old-hermes': {'kind': 'feishu_remote', 'binding': binding, 'chat_scopes': {'public': ['oc_archive']}, 'credential_ref': 'native:ARCHIVE_APP_SECRET'}}, state_dir=tmp_path / 'state', credential_resolver=lambda ref: 'synthetic-native-secret')
    provider = providers['local:old-hermes']
    provider.client.request = lambda request: NS(code=0, raw=NS(content=json.dumps({'code': 0, 'bot': {'open_id': 'ou_archive', 'activate_status': 2}}).encode()))
    provider.client.tenant.v2.tenant.query = lambda request: QueryTenantResponse({'code': 0, 'data': {'tenant': {'tenant_key': 'tenant-archive'}}})
    provider.client.im.v1.message.list = lambda request: ListMessageResponse({'code': 0, 'data': {'has_more': False, 'items': [{'message_id': 'om_old', 'chat_id': 'oc_archive', 'deleted': False, 'body': {'content': '{"text":"Original remote requirement"}'}}]}})
    manager, viewer, _ = setup_archive(tmp_path, provider)
    registration = migration_registration(); registration.update(id='old-feishu', kind='feishu_remote')
    with manager, ManagementServer(manager, {'new': viewer}):
        manager.register_archive_source(OWNER, registration)
        result = ManagementClient(tmp_path / 'state', 'new').query_archive('old-feishu', 'configured-query', 'requirement', ['public'], True)
        assert result['status'] == 'complete'
        assert result['records'][0]['locator'] == 'feishu-archive:oc_archive#om_old'
        assert result['coverage']['source_identity'] == binding



def test_owner_enablement_reads_actual_sdk_channel_receipts_then_accepts(tmp_path, monkeypatch):
    from types import SimpleNamespace as NS, ModuleType
    from lark_oapi import Client
    from lark_oapi.api.im.v1 import GetChatResponse, GetMessageResponse
    from ghost_hermes_pm.feishu import NativeFeishuTransport
    from ghost_hermes_pm.readiness import NativeProfileReadinessHost, profile_digest
    from test_directory import registration
    from test_requests import MESSAGE
    home = tmp_path / 'native-profile'; home.mkdir(); (home / 'config.yaml').write_text('model: fixture\n')
    native_profiles = ModuleType('hermes_cli.profiles'); native_profiles.get_profile_dir = lambda name: home
    monkeypatch.setitem(sys.modules, 'hermes_cli.profiles', native_profiles)
    value = registration(make_repo(tmp_path / 'repo'))
    value['profile']['connection_refs'] = {'bot': 'identity:cli_ready:ou_ready', 'credential': 'native:READY_SECRET'}
    digest = profile_digest(value['profile'])
    binding = {'profile_id': 'mono-lead', 'project_id': 'mono', 'app_id': 'cli_ready', 'recipient_open_id': 'ou_ready', 'chat_id': 'oc_ready', 'recipient_tenant_key': 'tenant-bot', 'transport_tenant_key': 'tenant-app', 'owner_open_id': 'ou_owner', 'sender_tenant_key': 'tenant-owner'}
    native = Client.builder().app_id('cli_ready').app_secret('synthetic-ready-secret').build()
    calls = []
    def bot_info(request):
        calls.append(request.uri)
        return NS(code=0, raw=NS(content=json.dumps({'code': 0, 'bot': {'open_id': 'ou_ready', 'activate_status': 2}}).encode()))
    native.request = bot_info
    native.im.v1.chat.get = lambda request: GetChatResponse({'code': 0, 'data': {'tenant_key': 'tenant-bot'}})
    def read_message(request):
        calls.append(request.message_id)
        delivered = request.message_id == 'om_delivery'
        return GetMessageResponse({'code': 0, 'data': {'items': [{'message_id': request.message_id, 'chat_id': 'oc_ready', 'deleted': False, 'parent_id': None if delivered else 'om_delivery', 'sender': {'id': 'ou_ready' if delivered else 'ou_owner', 'id_type': 'open_id', 'sender_type': 'app' if delivered else 'user', 'tenant_key': 'tenant-bot' if delivered else 'tenant-owner'}, 'body': {'content': json.dumps({'text': ('通道验收 ' if delivered else '已受理验收 ') + digest})}}]}})
    native.im.v1.message.get = read_message
    evidence = {'delivery_message_id': 'om_delivery', 'acceptance_message_id': 'om_acceptance'}
    intake = NS(settings={'bindings': [binding], 'channel_acceptance': {digest: {'oc_ready': evidence}}}, transports=[(object(), NativeFeishuTransport(native))])
    readiness = NativeProfileReadinessHost(intake, {'mono-lead': {'native_home': str(home)}}, lambda ref: 'synthetic-ready-secret')
    with Manager(tmp_path / 'state', owner_identity_ref=OWNER.subject, profile_readiness_host=readiness) as manager:
        with ManagementServer(manager, {'owner': OWNER}):
            client = ManagementClient(tmp_path / 'state', 'owner')
            client.apply_directory_change(0, value)
            with pytest.raises(ManagementError):
                manager.accept_request(OWNER, 'mono', 'mono-lead', MESSAGE, ISSUE)
            client.apply_directory_change(1, {'enable_profile': 'mono-lead'})
            profile = client.read_snapshot()['profiles'][0]
            assert profile['lifecycle'] == 'active'
            assert profile['can_execute'] is False
            assert profile['readiness']['channels'][0]['source'] == 'actual_feishu_group_and_message_reads'
            assert calls == ['/open-apis/bot/v3/info', 'om_delivery', 'om_acceptance']
            assert manager.accept_request(OWNER, 'mono', 'mono-lead', MESSAGE, ISSUE)['status'] == 'accepted'


@pytest.mark.parametrize('watch_identity', ['current', 'expired'])
def test_native_runner_executes_fixed_test_and_original_watch_invalidates_pass(tmp_path, watch_identity):
    import hashlib
    import time
    from ghost_hermes_pm.native_global_validation import configured_global_validation_host
    from test_global_validation import combination, git, LEAD
    from test_repository_queue import queue_adapter
    from readiness_support import ReadyManager
    control = tmp_path / 'watch-control'; control.write_text('')
    watcher = ['-c', 'import pathlib,sys,time; p=pathlib.Path(sys.argv[1]); seen=""\nwhile True:\n value=p.read_text()\n if value!=seen: sys.stdout.buffer.write(value.encode()+b"\\0");sys.stdout.buffer.flush();seen=value\n time.sleep(0.01)', str(control)]
    config = {'host_id': 'local:controlled-native-host', 'generation': 'synthetic-original-generation', 'runner': [sys.executable], 'watcher': [sys.executable, *watcher], 'tests': {'unit': ['-c', 'from pathlib import Path; assert Path("child/source.py").read_text() == "baseline\\n"; print("1 assertion passed")']}, 'environment': {'PATH': '/usr/bin:/bin'}}
    state = tmp_path / 'state'
    host = configured_global_validation_host(config, state)
    with ReadyManager(state, owner_identity_ref=OWNER.subject, codex_adapter=queue_adapter(tmp_path), global_validation_host=host) as manager:
        mono, child, parent, kid = combination(manager, tmp_path)
        details = {'request_id': parent, 'mono_commit': git(mono, 'rev-parse', 'HEAD'), 'children': [{'request_id': kid, 'path': 'child'}], 'test_ids': ['unit']}
        first = manager.global_validation(LEAD, 'plan', details)
        scope = [{'request_id': kid, 'commit': git(child, 'rev-parse', 'HEAD'), 'path': 'child'}]
        manager.global_validation(OWNER, 'prepare', {'validation_id': first['id'], 'children': scope})
        with pytest.raises(ManagementError):
            manager.global_validation(LEAD, 'start', {'validation_id': first['id']})
        blocked = manager.read_snapshot(OWNER)['global_validations'][0]
        retry = manager.global_validation(LEAD, 'plan', {**details, 'retry_of': first['id']})
        prepared = manager.global_validation(OWNER, 'prepare', {'validation_id': retry['id'], 'children': scope})
        watch_path = state / 'validation-native' / (hashlib.sha256(retry['id'].encode()).hexdigest() + '-watch-binding.json')
        watch_binding = json.loads(watch_path.read_text()) if watch_path.exists() else {'watcher_pid': 0, 'input_watch': 'unbound-original-watch'}
        if watch_identity == 'expired':
            watch_binding = {'watcher_pid': -1, 'input_watch': 'expired-original-watch'}
        # External trusted-host evidence is a controlled substitute here, not a physical-boundary claim.
        checks = {'source_write_denied', 'child_source_write_denied', 'parent_git_write_denied', 'child_git_write_denied', 'artifact_write_allowed', 'artifact_escape_denied', 'preexisting_hardlink_write_denied', 'tool_paths_confined', 'process_paths_confined', 'input_change_observation_complete'}
        proof = {'host_id': config['host_id'], 'generation': config['generation'], 'runner_configuration_digest': host.configuration_digest, 'validation_id': retry['id'], 'input_digest': prepared['input_digest'], 'source_access': 'read-only', 'git_access': 'read-only', 'artifact_roots': blocked['repository']['test_artifact_paths'], 'runner_binary_sha256': hashlib.sha256(Path(sys.executable).read_bytes()).hexdigest(), 'watcher_binary_sha256': hashlib.sha256(Path(sys.executable).read_bytes()).hexdigest(), 'scope': 'verified-original-host', 'platform_enforcement': 'synthetic-external-host-evidence', 'tool_paths': 'synthetic-external-host-evidence', 'preexisting_hardlink': 'synthetic-external-host-evidence', 'process_paths': 'synthetic-external-host-evidence', **watch_binding, 'watch_event_cursor': 0, 'checks': dict.fromkeys(checks, 'PASS')}
        directory = state / 'validation-evidence'; directory.mkdir()
        path = directory / 'fixture-native.json'; path.write_text(json.dumps(proof))
        (state / 'global-validation-host.json').write_text(json.dumps({retry['id']: {'path': 'validation-evidence/fixture-native.json', 'sha256': hashlib.sha256(path.read_bytes()).hexdigest()}}))
        with ManagementServer(manager, {'lead': LEAD}):
            client = ManagementClient(state, 'lead')
            if watch_identity == 'expired':
                with pytest.raises(ManagementError) as stale:
                    client.global_validation('start', {'validation_id': retry['id']})
                assert stale.value.code == 'capability_unverified'
                return
            run = client.global_validation('start', {'validation_id': retry['id']})
            assert host.find_run(run) == run['run']
            deadline = time.monotonic() + 10
            done = client.global_validation('finish', {'validation_id': retry['id']})
            while done['status'] in {'running', 'unverified'} and time.monotonic() < deadline:
                time.sleep(0.02)
                done = client.global_validation('finish', {'validation_id': retry['id']})
            assert done['status'] == 'passed'
            assert done['tests'][0]['exit_code'] == 0
            control.write_text(str(child / 'source.py'))
            deadline = time.monotonic() + 2
            while not host.read_input_changes(done) and time.monotonic() < deadline:
                time.sleep(0.01)
            assert client.read_snapshot()['global_validations'][-1]['status'] == 'invalidated'


def test_native_codex_archive_configuration_uses_original_proxy_and_public_grant(tmp_path):
    import hashlib
    from pathlib import Path
    from ghost_hermes_pm.archives import configured_providers
    from ghost_hermes_pm.observation import READ_METHODS
    from test_archives import setup_archive, migration_registration
    peer = {'thread': {'id': 'original-thread', 'status': {'type': 'idle'}, 'updatedAt': 1, 'historyMode': 'paginated', 'turns': []}, 'turns': {'': {'data': [{'id': 'turn-1', 'itemsView': 'full'}], 'nextCursor': None}}, 'items': {'turn-1': {'': {'data': [{'id': 'item-1', 'type': 'userMessage', 'text': 'Original executor requirement'}], 'nextCursor': None}}}}
    (tmp_path / 'archive-peer.json').write_text(json.dumps(peer))
    binary = tmp_path / 'original-proxy'
    fixture = Path(__file__).with_name('archive_fixture_server.py')
    binary.write_text('#!' + sys.executable + '\nimport os,runpy,sys\nsys.argv=[' + repr(str(fixture)) + ',os.environ["OWNED_ARCHIVE_ROOT"]]\nrunpy.run_path(sys.argv[0],run_name="__main__")\n')
    binary.chmod(0o700)
    state = tmp_path / 'state'; state.mkdir(mode=0o700)
    config = {'executable': str(binary), 'cwd': str(tmp_path), 'environment': {'PATH': '/usr/bin:/bin', 'CODEX_HOME': str(tmp_path / 'synthetic-home'), 'OWNED_ARCHIVE_ROOT': str(tmp_path)}, 'service_ref': 'local:original-proxy', 'source_kind': 'daemon', 'endpoint': str(tmp_path / 'original.sock'), 'endpoint_ref': 'local:original-socket'}
    providers = configured_providers({'local:old-hermes': {'kind': 'codex_history', 'adapter': config, 'thread_scopes': {'public': ['original-thread']}}}, state_dir=state)
    provider = providers['local:old-hermes']; adapter = provider.adapter
    binding = {'generation': adapter.generation, 'service_ref': config['service_ref'], 'source_kind': config['source_kind'], 'endpoint_ref': config['endpoint_ref'], 'transport': 'original_proxy_stdio'}
    # The original endpoint proof is substituted at the external trusted-host artifact boundary.
    proof = {**binding, 'binary_sha256': hashlib.sha256(binary.read_bytes()).hexdigest(), 'configuration_sha256': hashlib.sha256(json.dumps(config, sort_keys=True).encode()).hexdigest(), 'endpoint_sha256': hashlib.sha256(config['endpoint'].encode()).hexdigest(), 'platform': sys.platform, 'original_endpoint_verified': 'PASS', 'observation_read_only': 'PASS', 'provenance': 'trusted_host_original_executor', 'original_executor_id': 'controlled-original-peer', 'supported_methods': sorted(READ_METHODS), 'source_kinds': ['cli'], 'read_cases': dict.fromkeys(READ_METHODS | {'no_execution_writes', 'original_request_routing', 'unsupported_scope', 'disconnect'}, 'PASS')}
    directory = state / 'observation-evidence'; directory.mkdir()
    path = directory / 'fixture-original.json'; path.write_text(json.dumps(proof))
    (state / 'codex-observation.json').write_text(json.dumps({config['service_ref']: {'path': 'observation-evidence/fixture-original.json', 'sha256': hashlib.sha256(path.read_bytes()).hexdigest()}}))
    manager, viewer, _ = setup_archive(tmp_path, provider)
    registration = migration_registration(); registration.update(id='original-codex', kind='codex_history')
    with manager, ManagementServer(manager, {'new': viewer}):
        manager.register_archive_source(OWNER, registration)
        result = ManagementClient(state, 'new').query_archive('original-codex', 'configured-original', 'requirement', ['public'], True)
        assert result['status'] == 'complete'
        assert result['coverage']['original_executor_id'] == 'controlled-original-peer'
        assert result['records'][0]['locator'] == 'codex-archive:original-thread/turn-1#item-1'
    wire = [json.loads(line)['method'] for line in (tmp_path / 'archive-wire.jsonl').read_text().splitlines()]
    assert set(wire) <= {'initialize', 'initialized', 'thread/read', 'thread/turns/list', 'thread/items/list'}
