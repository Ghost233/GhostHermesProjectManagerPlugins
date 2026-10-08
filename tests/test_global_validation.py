"""Global validation through the authenticated public operation and real fixture Git."""
import json
import subprocess
from pathlib import Path

import pytest

from ghost_hermes_pm import Manager, ManagementError, VerifiedIdentity
from readiness_support import ReadyManager as Manager
from ghost_hermes_pm.transport import ManagementClient, ManagementServer
from test_directory import OWNER, registration
from test_repository_queue import commit_repo, acknowledge, queue_adapter
from test_task_execution import prepare_fixture
from test_requests import ISSUE

LEAD = VerifiedIdentity('fixture:lead', 'participant')


def git(repo, *args):
    return subprocess.check_output(['git', '-C', str(repo), *args], text=True).strip()


def combination(manager, root, *, unassigned=False, public_goal=False):
    mono, _ = commit_repo(root / 'mono')
    child, child_head = commit_repo(mono / 'child')
    (mono / '.gitmodules').write_text('[submodule "child"]\n\tpath = child\n\turl = ./child\n')
    subprocess.run(['git', '-C', str(mono), 'add', '.gitmodules'], check=True)
    subprocess.run(['git', '-C', str(mono), 'update-index', '--add', '--cacheinfo', '160000,' + child_head + ',child'], check=True)
    subprocess.run(['git', '-C', str(mono), '-c', 'user.name=Fixture', '-c', 'user.email=fixture@example.invalid', 'commit', '-qm', 'fixed child'], check=True)
    if unassigned:
        leaf, leaf_head = commit_repo(mono / 'unassigned')
        subprocess.run(['git', '-C', str(mono), 'update-index', '--add', '--cacheinfo', '160000,' + leaf_head + ',unassigned'], check=True)
        subprocess.run(['git', '-C', str(mono), '-c', 'user.name=Fixture', '-c', 'user.email=fixture@example.invalid', 'commit', '-qm', 'unassigned module'], check=True)
    value = registration(mono)
    value['profile']['connection_refs']['codex'] = 'local:fixture-stdio'
    manager.apply_directory_change(OWNER, 0, value)
    value = registration(child, 'child-project', 'child-lead')
    value['profile'].update(identity_ref='fixture:child', role='subproject_lead', parent_profile_id='mono-lead', connection_refs={'codex': 'local:fixture-stdio'})
    manager.apply_directory_change(OWNER, manager.read_snapshot(OWNER)['version'], value)
    if public_goal:
        from test_collaboration import channel, source, STEWARD
        manager.apply_directory_change(OWNER, manager.read_snapshot(OWNER)['version'], {'profile': {'id': 'steward', 'native_profile': 'steward', 'identity_ref': STEWARD.subject,
            'role': 'steward', 'capability': 'non_development', 'project_id': None, 'parent_profile_id': None, 'connection_refs': {}}})
        channels = [channel('steward', 'entry'), channel('steward'), channel('mono-lead'), channel('child-lead')]
        for c in channels:
            c['bot_sources'] = [{'profile_id': other, 'open_id': other + '-seen-' + c['profile_id'], 'tenant_key': tenant, 'native_ids': [other + '-native']}
                for other, tenant in [('steward', 'steward-tenant'), ('mono-lead', 'lead-tenant'), ('child-lead', 'child-tenant')] if other != c['profile_id']]
        manager.collaborate(OWNER, 'register_channels', {'channels': channels})
        h = manager.collaborate(OWNER, 'project_goal', {'sender_profile_id': 'steward', 'target_profile_id': 'mono-lead', 'source_anchor': source(channels[0]), 'issue_url': ISSUE['url']})
        ingress = VerifiedIdentity(LEAD.subject, 'native-collaboration-ingress')
        while True:
            part = manager.collaborate(STEWARD, 'claim_delivery', {'handoff_id': h['id']})
            if not part: break
            manager.collaborate(STEWARD, 'record_delivery', {'handoff_id': h['id'], 'uuid': part['uuid'], 'receipt': {'status': 'delivered', 'message_id': 'om_goal_' + str(part['number']), 'chat_id': 'oc_project'}})
            observed = source(channels[2], 'bot', 'om_goal_' + str(part['number']))
            manager.collaborate(ingress, 'ingest', {'channel_id': channels[2]['id'], 'source_anchor': observed, 'text': part['text']})
        accepted_handoff = next(a for a in manager.read_snapshot(OWNER)['collaboration']['handoffs'] if a['id'] == h['id'])
        parent = accepted_handoff['task_request_id']
        manager.collaborate(ingress, 'publish_ack', {'handoff_id': h['id']})
        ack = manager.collaborate(ingress, 'claim_ack', {'handoff_id': h['id']})
        manager.collaborate(ingress, 'record_ack', {'handoff_id': h['id'], 'uuid': ack['uuid'], 'receipt': {'status': 'delivered', 'message_id': 'om_ack_mono', 'chat_id': 'oc_project'}})
    else:
        parent = acknowledge(manager, suffix='mono')
    kid = acknowledge(manager, 'child-project', 'child-lead', 'child')
    prepare_fixture(manager, kid, child)
    session = manager.start_task(OWNER, kid)['session']
    (root / 'queue-observed.json').write_text(json.dumps({session['thread_id']: {'status': {'type': 'idle'}, 'turns': [{'id': session['turn_id'], 'status': 'completed', 'itemsView': 'full', 'items': [{'type': 'commandExecution', 'id': 'child-test', 'command': 'python -m unittest', 'cwd': str(child), 'status': 'completed', 'exitCode': 0, 'aggregatedOutput': 'Ran 1 test in 0.01s\n\nOK'}]}]}}))
    manager.record_task_delivery(OWNER, kid, {'source_commit': child_head, 'issue_updated_at': ISSUE['updated_at'], 'criteria': [{'text': ISSUE['body'], 'test_item_ids': ['child-test']}]})
    prepare_fixture(manager, parent, mono)
    manager.start_task(OWNER, parent)
    return mono, child, parent, kid


def test_fixed_combination_holds_related_repositories_and_only_releases_validation_occupancy(tmp_path):
    with Manager(tmp_path / 'state', owner_identity_ref=OWNER.subject, codex_adapter=queue_adapter(tmp_path)) as manager:
        mono, child, parent, kid = combination(manager, tmp_path)
        with ManagementServer(manager, {'owner': OWNER, 'lead': LEAD}):
            client = ManagementClient(tmp_path / 'state', 'lead')
            planned = client.global_validation('plan', {'request_id': parent, 'mono_commit': git(mono, 'rev-parse', 'HEAD'), 'children': [{'request_id': kid, 'path': 'child'}], 'test_ids': ['unit']})
            assert planned['children'][0]['commit'] == git(child, 'rev-parse', 'HEAD')
            assert planned['children'][0]['tests'][0]['item_id'] == 'child-test'
            assert planned['children'][0]['leftover_changes'] == ''
            assert planned['whole_project_complete'] is False
            with pytest.raises(ManagementError) as unsupported:
                client.global_validation('start', {'validation_id': planned['id']})
            assert unsupported.value.code == 'capability_unverified'
            snapshot = client.read_snapshot()
            attempt = snapshot['global_validations'][0]
            assert attempt['status'] == 'blocked' and attempt['occupancy']['released'] is True
            task = next(r for r in snapshot['requests'] if r['id'] == parent)
            assert task['repository_released'] is False and task['queue']['status'] == 'occupied'
            assert any('全局验证' in s['text'] for p in task['outbox'] for s in p['segments'])


class FixtureHost:
    """Trusted substitute: executes real tests; its enforcement is explicitly synthetic."""
    def __init__(self):
        self.runs = {}
        self.expected = 'baseline\n'

    def verify_boundary(self, context):
        import hashlib
        return {'host_id': 'fixture:global-host', 'generation': 'fixture-generation', 'runner_configuration_digest': hashlib.sha256(self.expected.encode()).hexdigest(), 'validation_id': context['id'], 'input_digest': context['input_digest'], 'source_access': 'read-only',
                'git_access': 'read-only', 'artifact_roots': context['repository']['test_artifact_paths'],
                'scope': 'synthetic-fixture', 'platform_enforcement': 'fixture-only', 'tool_paths': 'fixture-only',
                'preexisting_hardlink': 'fixture-only', 'process_paths': 'fixture-only', 'input_watch': 'fixture:watch:' + context['id'], 'evidence_ref': 'fixture:approved-synthetic-repository'}

    def start(self, context):
        import hashlib
        import sys
        argv = [sys.executable, '-c', 'from pathlib import Path; import sys; assert Path("child/source.py").read_text() == sys.argv[1]; print("1 test passed")', self.expected]
        result = subprocess.run(argv, cwd=context['repository']['worktree'], capture_output=True)
        run_id = 'run-' + context['id']
        self.runs[run_id] = {'host_id': context['boundary']['host_id'], 'generation': context['boundary']['generation'], 'run_id': run_id, 'validation_id': context['id'], 'input_digest': context['input_digest'], 'status': 'ended',
            'related_execution': 'ended', 'input_changes': [], 'tests': [{'id': 'unit', 'argv': argv, 'cwd': context['repository']['worktree'],
                'exit_code': result.returncode, 'output_digest': hashlib.sha256(result.stdout + result.stderr).hexdigest(), 'artifact_refs': []}], 'defects': []}
        return {'host_id': context['boundary']['host_id'], 'generation': context['boundary']['generation'], 'run_id': run_id, 'validation_id': context['id'], 'input_digest': context['input_digest']}

    def read_result(self, run_id):
        return self.runs[run_id]

    def read_input_changes(self, context):
        return self.runs[context['run']['run_id']]['input_changes']


def test_real_synthetic_test_run_blocks_new_child_work_and_releases_only_its_own_holds(tmp_path):
    host = FixtureHost()
    with Manager(tmp_path / 'state', owner_identity_ref=OWNER.subject, codex_adapter=queue_adapter(tmp_path), global_validation_host=host) as manager:
        mono, child, parent, kid = combination(manager, tmp_path)
        with ManagementServer(manager, {'owner': OWNER, 'lead': LEAD}):
            client = ManagementClient(tmp_path / 'state', 'lead')
            plan = client.global_validation('plan', {'request_id': parent, 'mono_commit': git(mono, 'rev-parse', 'HEAD'), 'children': [{'request_id': kid, 'path': 'child'}], 'test_ids': ['unit']})
            running = client.global_validation('start', {'validation_id': plan['id']})
            assert running['status'] == 'running' and running['occupancy']['released'] is False
            next_child = acknowledge(manager, 'child-project', 'child-lead', 'after-validation')
            prepare_fixture(manager, next_child, child)
            with pytest.raises(ManagementError) as occupied:
                manager.start_task(OWNER, next_child)
            assert occupied.value.code == 'repository_busy'
            child_task = next(r for r in client.read_snapshot()['requests'] if r['id'] == next_child)
            assert child_task['queue']['validation_blockers'] == [plan['id']]
            done = client.global_validation('finish', {'validation_id': plan['id']})
            assert done['status'] == 'passed' and done['tests'][0]['exit_code'] == 0
            assert done['whole_project_complete'] is False
            assert done['occupancy']['released'] is True
            original = next(r for r in client.read_snapshot()['requests'] if r['id'] == parent)
            assert original['repository_released'] is False and original['queue']['status'] == 'occupied'
            assert manager.start_task(OWNER, next_child)['status'] == 'running'


@pytest.mark.parametrize('change', ['source', 'metadata', 'commit', 'ignored_source'])
def test_input_change_invalidates_passed_tests_and_releases_validation_hold(tmp_path, change):
    host = FixtureHost()
    with Manager(tmp_path / 'state', owner_identity_ref=OWNER.subject, codex_adapter=queue_adapter(tmp_path), global_validation_host=host) as manager:
        mono, child, parent, kid = combination(manager, tmp_path)
        plan = manager.global_validation(LEAD, 'plan', {'request_id': parent, 'mono_commit': git(mono, 'rev-parse', 'HEAD'), 'children': [{'request_id': kid, 'path': 'child'}], 'test_ids': ['unit']})
        manager.global_validation(LEAD, 'start', {'validation_id': plan['id']})
        if change == 'source':
            (child / 'source.py').write_text('manual change\n')
        elif change == 'metadata':
            git(child, 'config', 'fixture.changed', 'yes')
        elif change == 'commit':
            subprocess.run(['git', '-C', str(child), '-c', 'user.name=Fixture', '-c', 'user.email=fixture@example.invalid', 'commit', '--allow-empty', '-qm', 'manual commit'], check=True)
        else:
            (child / '.git' / 'info' / 'exclude').write_text('hidden.py\n')
            (child / 'hidden.py').write_text('ignored manual source\n')
        ended = manager.global_validation(LEAD, 'finish', {'validation_id': plan['id']})
        assert ended['status'] == 'invalidated' and ended['whole_project_complete'] is False
        assert ended['occupancy']['released'] is True
        assert (child / 'source.py').read_text() == ('manual change\n' if change == 'source' else 'baseline\n')


class PreparationHost(FixtureHost):
    def __init__(self):
        super().__init__()
        self.operations = []

    def prepare(self, context, authorization):
        for child in context['children']:
            subprocess.run(['git', '-C', child['repository']['worktree'], 'checkout', '-q', '--detach', child['commit']], check=True)
            self.operations.append({'request_id': child['request_id'], 'commit': child['commit'], 'path': child['path']})
        return {'validation_id': context['id'], 'authorization_digest': authorization['digest'], 'status': 'ended', 'related_execution': 'ended', 'operations': self.operations.copy()}


def test_only_independent_owner_preparation_materializes_fixed_child_before_testing(tmp_path):
    host = PreparationHost()
    with Manager(tmp_path / 'state', owner_identity_ref=OWNER.subject, codex_adapter=queue_adapter(tmp_path), global_validation_host=host) as manager:
        mono, child, parent, kid = combination(manager, tmp_path)
        fixed = git(child, 'rev-parse', 'HEAD')
        subprocess.run(['git', '-C', str(child), '-c', 'user.name=Fixture', '-c', 'user.email=fixture@example.invalid', 'commit', '--allow-empty', '-qm', 'different materialization'], check=True)
        original_parent = git(mono, 'rev-parse', 'HEAD')
        plan = manager.global_validation(LEAD, 'plan', {'request_id': parent, 'mono_commit': original_parent, 'children': [{'request_id': kid, 'path': 'child'}], 'test_ids': ['unit']})
        permission = {'validation_id': plan['id'], 'children': [{'request_id': kid, 'commit': fixed, 'path': 'child'}]}
        with pytest.raises(ManagementError) as denied:
            manager.global_validation(LEAD, 'prepare', permission)
        assert denied.value.code == 'forbidden' and host.operations == []
        ready = manager.global_validation(OWNER, 'prepare', permission)
        assert ready['preparation']['status'] == 'ended' and ready['status'] == 'ready'
        assert git(child, 'rev-parse', 'HEAD') == fixed and git(mono, 'rev-parse', 'HEAD') == original_parent
        assert manager.global_validation(LEAD, 'start', {'validation_id': plan['id']})['status'] == 'running'
        assert manager.global_validation(LEAD, 'finish', {'validation_id': plan['id']})['status'] == 'passed'


def test_preparation_preserves_user_changes_and_never_calls_materializer_when_dirty(tmp_path):
    host = PreparationHost()
    with Manager(tmp_path / 'state', owner_identity_ref=OWNER.subject, codex_adapter=queue_adapter(tmp_path), global_validation_host=host) as manager:
        mono, child, parent, kid = combination(manager, tmp_path)
        plan = manager.global_validation(LEAD, 'plan', {'request_id': parent, 'mono_commit': git(mono, 'rev-parse', 'HEAD'), 'children': [{'request_id': kid, 'path': 'child'}], 'test_ids': ['unit']})
        (child / 'source.py').write_text('owner uncommitted work\n')
        with pytest.raises(ManagementError) as dirty:
            manager.global_validation(OWNER, 'prepare', {'validation_id': plan['id'], 'children': [{'request_id': kid, 'commit': git(child, 'rev-parse', 'HEAD'), 'path': 'child'}]})
        assert dirty.value.code == 'handoff_blocked' and host.operations == []
        assert (child / 'source.py').read_text() == 'owner uncommitted work\n'
        ended = manager.read_snapshot(OWNER)['global_validations'][0]
        assert ended['status'] == 'blocked' and ended['occupancy']['released'] is True


class IssueReadSource:
    def read_issue(self, url):
        if url == ISSUE['url']:
            return dict(ISSUE)
        return {**ISSUE, 'url': url, 'title': 'Repair integration contract', 'body': '- [ ] Child supports mono integration'}


class FailedHost(FixtureHost):
    def __init__(self):
        super().__init__()
        self.expected = 'repaired integration\n'

    def start(self, context):
        run = super().start(context)
        self.runs[run['run_id']]['defects'] = [{'target': 'child', 'description': 'Child supports mono integration', 'test_ids': ['unit']}]
        return run


def test_failed_child_has_an_explicit_verified_issue_and_a_public_rework_handoff(tmp_path):
    from test_collaboration import channel
    with Manager(tmp_path / 'state', owner_identity_ref=OWNER.subject, codex_adapter=queue_adapter(tmp_path), global_validation_host=FailedHost(), delivery_source=IssueReadSource()) as manager:
        mono, child, parent, kid = combination(manager, tmp_path)
        lead_channel, kid_channel = channel('mono-lead'), channel('child-lead')
        lead_channel['bot_sources'] = []
        kid_channel['bot_sources'] = []
        lead_channel['bot_sources'].append({'profile_id': 'child-lead', 'open_id': 'child-seen-lead', 'tenant_key': 'child-tenant', 'native_ids': ['child-native']})
        kid_channel['bot_sources'].append({'profile_id': 'mono-lead', 'open_id': 'lead-seen-child', 'tenant_key': 'lead-tenant', 'native_ids': ['lead-native']})
        manager.collaborate(OWNER, 'register_channels', {'channels': [lead_channel, kid_channel]})
        plan = manager.global_validation(LEAD, 'plan', {'request_id': parent, 'mono_commit': git(mono, 'rev-parse', 'HEAD'), 'children': [{'request_id': kid, 'path': 'child'}], 'test_ids': ['unit']})
        manager.global_validation(LEAD, 'start', {'validation_id': plan['id']})
        assert manager.global_validation(LEAD, 'finish', {'validation_id': plan['id']})['status'] == 'failed'
        returned = manager.global_validation(LEAD, 'rework', {'validation_id': plan['id'], 'target': 'child', 'issue_url': 'https://github.com/example-user/fixture/issues/28'})
        assert returned['rework'][0]['route'] == 'child' and returned['rework'][0]['profile_id'] == 'child-lead'
        assert returned['rework'][0]['issue']['url'].endswith('/issues/28')
        handoff = returned['rework'][0]['handoff_id']
        packet = manager.collaborate(LEAD, 'claim_delivery', {'handoff_id': handoff})
        assert packet['mention_open_id'] == 'child-seen-lead' and '/issues/28' in packet['text']
        assert returned['occupancy']['released'] is True
        assert manager.global_validation(LEAD, 'rework', {'validation_id': plan['id'], 'target': 'child', 'issue_url': 'https://github.com/example-user/fixture/issues/28'})['rework'] == returned['rework']


def test_final_completion_requires_original_mono_acceptance_and_fresh_stable_validation(tmp_path):
    host = FixtureHost()
    with Manager(tmp_path / 'state', owner_identity_ref=OWNER.subject, codex_adapter=queue_adapter(tmp_path), global_validation_host=host) as manager:
        mono, child, parent, kid = combination(manager, tmp_path)
        plan = manager.global_validation(LEAD, 'plan', {'request_id': parent, 'mono_commit': git(mono, 'rev-parse', 'HEAD'), 'children': [{'request_id': kid, 'path': 'child'}], 'test_ids': ['unit']})
        manager.global_validation(LEAD, 'start', {'validation_id': plan['id']})
        manager.global_validation(LEAD, 'finish', {'validation_id': plan['id']})
        with pytest.raises(ManagementError) as own_unmet:
            manager.global_validation(LEAD, 'complete', {'validation_id': plan['id']})
        assert own_unmet.value.code == 'evidence_missing'
        task = next(r for r in manager.read_snapshot(OWNER)['requests'] if r['id'] == parent)
        session = task['session']
        (tmp_path / 'queue-observed.json').write_text(json.dumps({session['thread_id']: {'status': {'type': 'idle'}, 'turns': [{'id': session['turn_id'], 'status': 'completed', 'itemsView': 'full', 'items': [{'type': 'commandExecution', 'id': 'mono-test', 'command': 'python -m unittest', 'cwd': str(mono), 'status': 'completed', 'exitCode': 0, 'aggregatedOutput': 'Ran 1 test in 0.01s\n\nOK'}]}]}}))
        manager.record_task_delivery(LEAD, parent, {'source_commit': git(mono, 'rev-parse', 'HEAD'), 'issue_updated_at': ISSUE['updated_at'], 'criteria': [{'text': ISSUE['body'], 'test_item_ids': ['mono-test']}]})
        completed = manager.global_validation(LEAD, 'complete', {'validation_id': plan['id']})
        assert completed['whole_project_complete'] is True and completed['status'] == 'complete'
        (child / 'source.py').write_text('late manual input\n')
        invalid = manager.global_validation(LEAD, 'check', {'validation_id': plan['id']})
        assert invalid['whole_project_complete'] is False and invalid['status'] == 'invalidated'


@pytest.mark.asyncio
async def test_dashboard_and_native_group_share_versions_evidence_and_round_holds(tmp_path):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from test_feishu_entry import CONFIG, Gateway, Transport, event
    from ghost_hermes_pm.messages import FeishuEntry
    from ghost_hermes_pm.dashboard import create_router
    with Manager(tmp_path / 'state', owner_identity_ref=OWNER.subject, codex_adapter=queue_adapter(tmp_path), global_validation_host=FixtureHost()) as manager:
        mono, child, parent, kid = combination(manager, tmp_path)
        with ManagementServer(manager, {'owner': OWNER}):
            client = ManagementClient(tmp_path / 'state', 'owner')
            app = FastAPI()
            app.include_router(create_router(lambda request: client))
            with TestClient(app) as browser:
                body = {'action': 'plan', 'details': {'request_id': parent, 'mono_commit': git(mono, 'rev-parse', 'HEAD'), 'children': [{'request_id': kid, 'path': 'child'}], 'test_ids': ['unit']}}
                response = browser.post('/global-validation', json=body)
                assert response.status_code == 200
                plan = response.json()
                assert browser.post('/global-validation', json={**body, 'actor': 'owner'}).status_code == 422
                transport, adapter = Transport(), object()
                intake = FeishuEntry(lambda: manager, OWNER.subject, CONFIG, lambda url: None)
                intake.attach_transport(adapter, transport)
                incoming = event('@_user_1 全局验证 start：' + json.dumps({'validation_id': plan['id']}), 'om_global_start')
                incoming.raw_message.event.message.parent_id = 'om_ack_mono'
                assert await intake.receive(incoming, Gateway(adapter)) == {'action': 'skip'}
                running = browser.get('/snapshot').json()['global_validations'][0]
                assert running['status'] == 'running' and running['occupancy']['released'] is False
                assert browser.post('/global-validation', json={'action': 'finish', 'details': {'validation_id': plan['id']}}).json()['status'] == 'passed'
                current = browser.get('/snapshot').json()['global_validations'][0]
                assert current['tests'][0]['output_digest'] and current['children'][0]['commit'] == git(child, 'rev-parse', 'HEAD')
                assert any('全局验证' in packet['text'] for packet in transport.sent)
                (child / 'source.py').write_text('manual invalidation\n')
                changed = browser.get('/snapshot').json()['global_validations'][0]
                assert changed['status'] == 'invalidated' and changed['whole_project_complete'] is False


def test_unassigned_materialized_module_is_checked_without_creating_an_owner(tmp_path):
    host = FailedHost()
    with Manager(tmp_path / 'state', owner_identity_ref=OWNER.subject, codex_adapter=queue_adapter(tmp_path), global_validation_host=host, delivery_source=IssueReadSource()) as manager:
        mono, child, parent, kid = combination(manager, tmp_path, unassigned=True)
        plan = manager.global_validation(LEAD, 'plan', {'request_id': parent, 'mono_commit': git(mono, 'rev-parse', 'HEAD'), 'children': [{'request_id': kid, 'path': 'child'}], 'test_ids': ['unit']})
        assert plan['unassigned'][0]['path'] == 'unassigned' and plan['unassigned'][0]['commit'] == git(mono / 'unassigned', 'rev-parse', 'HEAD')
        manager.global_validation(LEAD, 'start', {'validation_id': plan['id']})
        run = host.runs['run-' + plan['id']]
        run['defects'] = [{'target': 'unassigned', 'description': 'Unassigned child contract', 'test_ids': ['unit']}]
        failed = manager.global_validation(LEAD, 'finish', {'validation_id': plan['id']})
        assert failed['status'] == 'failed'
        returned = manager.global_validation(LEAD, 'rework', {'validation_id': plan['id'], 'target': 'unassigned', 'issue_url': 'https://github.com/example-user/fixture/issues/29'})
        assert returned['rework'][0]['route'] == 'owner_decision' and returned['rework'][0]['status'] == 'needs_owner'
        assert len(manager.read_snapshot(OWNER)['profiles']) == 2
        assert returned['occupancy']['released'] is True


def test_current_manual_activity_blocks_global_validation_without_any_control(tmp_path):
    from test_manual_observation import observer, manual_state, READ_ONLY
    peer = tmp_path / 'manual-peer'
    with Manager(tmp_path / 'state', owner_identity_ref=OWNER.subject, codex_adapter=queue_adapter(tmp_path), global_validation_host=FixtureHost(), observation_adapters={'local:manual-daemon': observer(peer)}) as manager:
        mono, child, parent, kid = combination(manager, tmp_path)
        manual_state(peer, child, 'idle')
        manager.register_observation_source(OWNER, {'id': 'manual-child', 'kind': 'daemon', 'project_ids': ['child-project'], 'adapter_ref': 'local:manual-daemon'})
        manager.refresh_manual_sessions(OWNER, 'child-project')
        manual_state(peer, child, 'active')
        plan = manager.global_validation(LEAD, 'plan', {'request_id': parent, 'mono_commit': git(mono, 'rev-parse', 'HEAD'), 'children': [{'request_id': kid, 'path': 'child'}], 'test_ids': ['unit']})
        with pytest.raises(ManagementError) as active:
            manager.global_validation(LEAD, 'start', {'validation_id': plan['id']})
        assert active.value.code == 'repository_busy'
        assert all(json.loads(line).get('method') in READ_ONLY for line in (peer / 'manual-wire.jsonl').read_text().splitlines())
        assert manager.read_snapshot(OWNER)['global_validations'][0]['occupancy']['released'] is True


class RunnerIssueSource(IssueReadSource):
    """Controlled original runner receipts; no GitHub requests or real accounts."""
    def __init__(self):
        self.receipts = {}

    def run(self, root, session, item_id):
        import hashlib
        import os
        import sys
        repo = Path(session['repository']['worktree'])
        names = subprocess.check_output(['git', '-C', str(repo), 'ls-files', '-z'], text=True).split('\0')
        digest = hashlib.sha256()
        for name in sorted(n for n in names if n):
            digest.update(name.encode())
            if (repo / name).is_file():
                digest.update((repo / name).read_bytes())
        fixed = digest.hexdigest()
        result = subprocess.run([sys.executable, '-m', 'unittest', 'discover'], cwd=repo, env={**os.environ, 'PYTHONDONTWRITEBYTECODE': '1'}, capture_output=True, text=True)
        assert result.returncode == 0, result.stdout + result.stderr
        output = result.stdout + result.stderr
        import re
        self.receipts[item_id] = {'executed_tests': int(re.search(r'Ran (\d+) tests? in', output).group(1)), 'service_id': session['service_id'], 'generation': session['generation'], 'turn_id': session['turn_id'], 'item_id': item_id,
            'command_sha256': hashlib.sha256(b'python -m unittest discover').hexdigest(), 'output_digest': hashlib.sha256(output.encode()).hexdigest(), 'exit_code': 0,
            'before_source_digest': fixed, 'after_source_digest': fixed, 'source_access': 'read-only', 'git_access': 'read-only', 'artifact_roots': session['repository']['test_artifact_paths']}
        observed = root / 'queue-observed.json'
        patches = json.loads(observed.read_text())
        patches[session['thread_id']] = {'status': {'type': 'idle'}, 'turns': [{'id': session['turn_id'], 'status': 'completed', 'itemsView': 'full',
            'items': [{'type': 'commandExecution', 'id': item_id, 'command': 'python -m unittest discover', 'cwd': str(repo), 'status': 'completed', 'exitCode': 0, 'aggregatedOutput': output}]}]}
        observed.write_text(json.dumps(patches))

    def read_test_version(self, session, item_id):
        return self.receipts[item_id]


@pytest.mark.asyncio
async def test_approved_repository_delivery_failure_issue_repair_revalidation_and_completion(tmp_path):
    from test_collaboration import channel, source, STEWARD
    from lark_oapi import Client
    from lark_oapi.api.im.v1 import CreateMessageResponse, ReplyMessageResponse
    from types import SimpleNamespace as NS
    from ghost_hermes_pm.feishu import NativeFeishuTransport
    issue_source, host = RunnerIssueSource(), FailedHost()
    with Manager(tmp_path / 'state', owner_identity_ref=OWNER.subject, codex_adapter=queue_adapter(tmp_path), global_validation_host=host, delivery_source=issue_source) as manager:
        mono, child, parent, kid = combination(manager, tmp_path, public_goal=True)
        channels = [channel('steward', 'entry'), channel('steward'), channel('mono-lead'), channel('child-lead')]
        manager.apply_directory_change(OWNER, manager.read_snapshot(OWNER)['version'], {'profile': {'id': 'steward', 'native_profile': 'steward', 'identity_ref': STEWARD.subject,
            'role': 'steward', 'capability': 'non_development', 'project_id': None, 'parent_profile_id': None, 'connection_refs': {}}})
        for c in channels:
            c['bot_sources'] = []
            for other, tenant in [('steward', 'steward-tenant'), ('mono-lead', 'lead-tenant'), ('child-lead', 'child-tenant')]:
                if other != c['profile_id']:
                    c['bot_sources'].append({'profile_id': other, 'open_id': other + '-seen-' + c['profile_id'], 'tenant_key': tenant, 'native_ids': [other + '-native']})
        manager.collaborate(OWNER, 'register_channels', {'channels': channels})
        sent = []
        async def deliver(actor, handoff_id, receiving=None):
            while True:
                packet = manager.collaborate(actor, 'claim_delivery', {'handoff_id': handoff_id})
                if not packet:
                    break
                binding = packet['sender_binding']
                native = Client.builder().app_id(binding['app_id']).app_secret('fixture-unused-credential').build()
                native.request = lambda request: NS(code=0, raw=NS(content=json.dumps({'code': 0, 'bot': {'open_id': binding['recipient_open_id'], 'activate_status': 2}}).encode()))
                message_id = 'om_full_' + str(len(sent))
                native.im.v1.message.create = lambda request: CreateMessageResponse({'code': 0, 'data': {'message_id': message_id, 'chat_id': packet['chat_id']}})
                native.im.v1.message.reply = lambda request: ReplyMessageResponse({'code': 0, 'data': {'message_id': message_id, 'chat_id': packet['chat_id']}})
                transport = NativeFeishuTransport(native)
                await transport.verify_identity(binding)
                receipt = await transport.send(packet)
                manager.collaborate(actor, 'record_delivery', {'handoff_id': handoff_id, 'uuid': packet['uuid'], 'receipt': receipt})
                sent.append(packet)
                if receiving:
                    receiver, c = receiving
                    observed = source(c, 'bot', message_id)
                    observed.update(tenant_key=next(b['tenant_key'] for b in c['bot_sources'] if b['profile_id'] == binding['profile_id']), sender_open_id=binding['profile_id'] + '-seen-' + c['profile_id'])
                    manager.collaborate(receiver, 'ingest', {'channel_id': c['id'], 'source_anchor': observed, 'text': packet['text']})
        first = manager.global_validation(LEAD, 'plan', {'request_id': parent, 'mono_commit': git(mono, 'rev-parse', 'HEAD'), 'children': [{'request_id': kid, 'path': 'child'}], 'test_ids': ['unit']})
        manager.global_validation(LEAD, 'start', {'validation_id': first['id']})
        assert manager.global_validation(LEAD, 'finish', {'validation_id': first['id']})['status'] == 'failed'
        returned = manager.global_validation(LEAD, 'rework', {'validation_id': first['id'], 'target': 'child', 'issue_url': 'https://github.com/example-user/fixture/issues/28'})
        handoff_id = returned['rework'][0]['handoff_id']
        child_ingress = VerifiedIdentity('fixture:child', 'native-collaboration-ingress')
        await deliver(LEAD, handoff_id, (child_ingress, channels[3]))
        h = next(h for h in manager.read_snapshot(OWNER)['collaboration']['handoffs'] if h['id'] == handoff_id)
        repair_id = h['task_request_id']
        manager.collaborate(child_ingress, 'publish_ack', {'handoff_id': handoff_id})
        ack = manager.collaborate(child_ingress, 'claim_ack', {'handoff_id': handoff_id})
        manager.collaborate(child_ingress, 'record_ack', {'handoff_id': handoff_id, 'uuid': ack['uuid'], 'receipt': {'status': 'delivered', 'message_id': 'om_repair_ack', 'chat_id': 'oc_project'}})
        prepare_fixture(manager, repair_id, child)
        repair_session = manager.start_task(VerifiedIdentity('fixture:child', 'participant'), repair_id)['session']
        (child / 'source.py').write_text('repaired integration\n')
        (child / 'test_child.py').write_text('import unittest\nfrom pathlib import Path\nclass Child(unittest.TestCase):\n def test_contract(self): self.assertEqual(Path("source.py").read_text(), "repaired integration\\n")\n')
        subprocess.run(['git', '-C', str(child), 'add', 'source.py', 'test_child.py'], check=True)
        subprocess.run(['git', '-C', str(child), '-c', 'user.name=Fixture', '-c', 'user.email=fixture@example.invalid', 'commit', '-qm', 'repair child contract'], check=True)
        issue_source.run(tmp_path, repair_session, 'repair-test')
        manager.record_task_delivery(OWNER, repair_id, {'source_commit': git(child, 'rev-parse', 'HEAD'), 'issue_updated_at': ISSUE['updated_at'], 'criteria': [{'text': 'Child supports mono integration', 'test_item_ids': ['repair-test']}]})
        (mono / 'test_mono.py').write_text('import unittest\nfrom pathlib import Path\nclass Mono(unittest.TestCase):\n def test_contract(self): self.assertEqual(Path("child/source.py").read_text(), "repaired integration\\n")\n')
        subprocess.run(['git', '-C', str(mono), 'add', 'child', 'test_mono.py'], check=True)
        subprocess.run(['git', '-C', str(mono), '-c', 'user.name=Fixture', '-c', 'user.email=fixture@example.invalid', 'commit', '-qm', 'integrate fixed child'], check=True)
        task = next(r for r in manager.read_snapshot(OWNER)['requests'] if r['id'] == parent)
        issue_source.run(tmp_path, task['session'], 'mono-final')
        manager.record_task_delivery(LEAD, parent, {'source_commit': git(mono, 'rev-parse', 'HEAD'), 'issue_updated_at': ISSUE['updated_at'], 'criteria': [{'text': ISSUE['body'], 'test_item_ids': ['mono-final']}]})
        second = manager.global_validation(LEAD, 'plan', {'request_id': parent, 'mono_commit': git(mono, 'rev-parse', 'HEAD'), 'children': [{'request_id': repair_id, 'path': 'child'}], 'test_ids': ['unit']})
        manager.global_validation(LEAD, 'start', {'validation_id': second['id']})
        assert manager.global_validation(LEAD, 'finish', {'validation_id': second['id']})['status'] == 'passed'
        completed = manager.global_validation(LEAD, 'complete', {'validation_id': second['id']})
        assert completed['whole_project_complete'] is True
        rounds = manager.read_snapshot(OWNER)['global_validations']
        previous = next(r for r in rounds if r['id'] == first['id'])
        assert previous['rework'][0]['status'] == 'resolved' and previous['rework'][0]['revalidated_by'] == second['id']
        assert all(r['occupancy']['released'] for r in rounds)
        assert any('issues/28' in p['text'] and p['mention_open_id'] == 'child-lead-seen-mono-lead' for p in sent)

        child_result = manager.collaborate(VerifiedIdentity('fixture:child', 'participant'), 'report_result', {'handoff_id': handoff_id})
        await deliver(VerifiedIdentity('fixture:child', 'participant'), child_result['id'], (VerifiedIdentity(LEAD.subject, 'native-collaboration-ingress'), channels[2]))
        original = next(h for h in manager.read_snapshot(OWNER)['collaboration']['handoffs'] if h.get('task_request_id') == parent)
        summary = manager.collaborate(LEAD, 'report_summary', {'handoff_id': original['id']})
        assert summary['whole_project_complete'] is True and summary['global_validation_id'] == second['id']
        await deliver(LEAD, summary['id'], (VerifiedIdentity(STEWARD.subject, 'native-collaboration-ingress'), channels[1]))
        owner_summary = manager.collaborate(STEWARD, 'publish_owner_summary', {'handoff_id': summary['id']})
        assert owner_summary['whole_project_complete'] is True
        await deliver(STEWARD, owner_summary['id'])
        assert any(p['path'] == 'reply' and '整体完成' in p['text'] for p in sent)


class UnknownStartHost(FixtureHost):
    def start(self, context):
        super().start(context)
        raise ManagementError('outcome_unknown', 'Fixture response lost after actual test start.')

    def find_run(self, context):
        run_id = 'run-' + context['id']
        return {k: self.runs[run_id][k] for k in ('run_id', 'validation_id', 'input_digest', 'host_id', 'generation')}


def test_unknown_test_start_is_not_replayed_and_reconciles_the_original_run_after_restart(tmp_path):
    host = UnknownStartHost()
    with Manager(tmp_path / 'state', owner_identity_ref=OWNER.subject, codex_adapter=queue_adapter(tmp_path), global_validation_host=host) as manager:
        mono, child, parent, kid = combination(manager, tmp_path)
        plan = manager.global_validation(LEAD, 'plan', {'request_id': parent, 'mono_commit': git(mono, 'rev-parse', 'HEAD'), 'children': [{'request_id': kid, 'path': 'child'}], 'test_ids': ['unit']})
        with pytest.raises(ManagementError) as unknown:
            manager.global_validation(LEAD, 'start', {'validation_id': plan['id']})
        assert unknown.value.code == 'outcome_unknown'
        with pytest.raises(ManagementError):
            manager.global_validation(LEAD, 'start', {'validation_id': plan['id']})
        assert len(host.runs) == 1
        assert manager.read_snapshot(OWNER)['global_validations'][0]['occupancy']['released'] is False
    with Manager(tmp_path / 'state', owner_identity_ref=OWNER.subject, global_validation_host=host) as restored:
        reconciled = restored.global_validation(LEAD, 'reconcile', {'validation_id': plan['id']})
        assert reconciled['status'] == 'passed' and reconciled['occupancy']['released'] is True
        assert len(host.runs) == 1
        assert next(r for r in restored.read_snapshot(OWNER)['requests'] if r['id'] == parent)['repository_released'] is False


def test_duplicate_plan_is_one_round_and_explicit_retry_is_a_new_related_round(tmp_path):
    with Manager(tmp_path / 'state', owner_identity_ref=OWNER.subject, codex_adapter=queue_adapter(tmp_path)) as manager:
        mono, child, parent, kid = combination(manager, tmp_path)
        details = {'request_id': parent, 'mono_commit': git(mono, 'rev-parse', 'HEAD'), 'children': [{'request_id': kid, 'path': 'child'}], 'test_ids': ['unit']}
        first = manager.global_validation(LEAD, 'plan', details)
        assert manager.global_validation(LEAD, 'plan', details)['id'] == first['id']
        with pytest.raises(ManagementError):
            manager.global_validation(LEAD, 'start', {'validation_id': first['id']})
        retry = manager.global_validation(LEAD, 'plan', {**details, 'retry_of': first['id']})
        assert retry['id'] != first['id'] and retry['retry_of'] == first['id']
        assert len(manager.read_snapshot(OWNER)['global_validations']) == 2


def test_detected_invalidation_is_durable_and_restoring_bytes_cannot_revive_old_pass(tmp_path):
    host = FixtureHost()
    with Manager(tmp_path / 'state', owner_identity_ref=OWNER.subject, codex_adapter=queue_adapter(tmp_path), global_validation_host=host) as manager:
        mono, child, parent, kid = combination(manager, tmp_path)
        (child / '.git' / 'info' / 'exclude').write_text('hidden.py\n')
        (child / 'hidden.py').write_text('original ignored input\n')
        plan = manager.global_validation(LEAD, 'plan', {'request_id': parent, 'mono_commit': git(mono, 'rev-parse', 'HEAD'), 'children': [{'request_id': kid, 'path': 'child'}], 'test_ids': ['unit']})
        manager.global_validation(LEAD, 'start', {'validation_id': plan['id']})
        manager.global_validation(LEAD, 'finish', {'validation_id': plan['id']})
        (child / 'hidden.py').write_text('manual ignored input\n')
        assert manager.read_snapshot(OWNER)['global_validations'][0]['status'] == 'invalidated'
        (child / 'hidden.py').write_text('original ignored input\n')
        assert manager.read_snapshot(OWNER)['global_validations'][0]['status'] == 'invalidated'
    with Manager(tmp_path / 'state', owner_identity_ref=OWNER.subject, global_validation_host=host) as restored:
        assert restored.read_snapshot(OWNER)['global_validations'][0]['status'] == 'invalidated'


def test_runner_configuration_change_withdraws_a_previously_passed_combination(tmp_path):
    host = FixtureHost()
    with Manager(tmp_path / 'state', owner_identity_ref=OWNER.subject, codex_adapter=queue_adapter(tmp_path), global_validation_host=host) as manager:
        mono, child, parent, kid = combination(manager, tmp_path)
        plan = manager.global_validation(LEAD, 'plan', {'request_id': parent, 'mono_commit': git(mono, 'rev-parse', 'HEAD'), 'children': [{'request_id': kid, 'path': 'child'}], 'test_ids': ['unit']})
        manager.global_validation(LEAD, 'start', {'validation_id': plan['id']})
        manager.global_validation(LEAD, 'finish', {'validation_id': plan['id']})
        host.expected = 'changed test contract\n'
        assert manager.read_snapshot(OWNER)['global_validations'][0]['status'] == 'invalidated'


def test_responsibility_correction_invalidates_existing_global_evidence(tmp_path):
    host = FixtureHost()
    with Manager(tmp_path / 'state', owner_identity_ref=OWNER.subject, codex_adapter=queue_adapter(tmp_path), global_validation_host=host) as manager:
        mono, child, parent, kid = combination(manager, tmp_path)
        plan = manager.global_validation(LEAD, 'plan', {'request_id': parent, 'mono_commit': git(mono, 'rev-parse', 'HEAD'), 'children': [{'request_id': kid, 'path': 'child'}], 'test_ids': ['unit']})
        manager.global_validation(LEAD, 'start', {'validation_id': plan['id']})
        manager.global_validation(LEAD, 'finish', {'validation_id': plan['id']})
        snapshot = manager.read_snapshot(OWNER)
        profile = next(p for p in snapshot['profiles'] if p['id'] == 'child-lead')
        corrected = {k: profile[k] for k in ('id', 'native_profile', 'identity_ref', 'role', 'capability', 'project_id', 'parent_profile_id', 'connection_refs')}
        corrected['capability'] = 'non_development'
        manager.apply_directory_change(OWNER, snapshot['version'], {'profile': corrected})
        assert manager.read_snapshot(OWNER)['global_validations'][0]['status'] == 'invalidated'


class UnknownPreparationHost(PreparationHost):
    def prepare(self, context, authorization):
        self.preparation_receipt = super().prepare(context, authorization)
        raise ManagementError('outcome_unknown', 'Preparation response lost after authorized materialization.')

    def find_preparation(self, context, authorization):
        return self.preparation_receipt


def test_unknown_authorized_preparation_queries_original_action_without_another_checkout(tmp_path):
    host = UnknownPreparationHost()
    with Manager(tmp_path / 'state', owner_identity_ref=OWNER.subject, codex_adapter=queue_adapter(tmp_path), global_validation_host=host) as manager:
        mono, child, parent, kid = combination(manager, tmp_path)
        fixed = git(child, 'rev-parse', 'HEAD')
        subprocess.run(['git', '-C', str(child), '-c', 'user.name=Fixture', '-c', 'user.email=fixture@example.invalid', 'commit', '--allow-empty', '-qm', 'other materialization'], check=True)
        plan = manager.global_validation(LEAD, 'plan', {'request_id': parent, 'mono_commit': git(mono, 'rev-parse', 'HEAD'), 'children': [{'request_id': kid, 'path': 'child'}], 'test_ids': ['unit']})
        with pytest.raises(ManagementError) as unknown:
            manager.global_validation(OWNER, 'prepare', {'validation_id': plan['id'], 'children': [{'request_id': kid, 'commit': fixed, 'path': 'child'}]})
        assert unknown.value.code == 'outcome_unknown'
        pending = manager.read_snapshot(OWNER)['global_validations'][0]
        assert pending['status'] == 'preparation_unverified' and pending['occupancy']['released'] is False
        ready = manager.global_validation(LEAD, 'reconcile', {'validation_id': plan['id']})
        assert ready['status'] == 'ready' and ready['preparation']['status'] == 'ended'
        assert ready['occupancy']['released'] is True and len(host.operations) == 1
        assert git(child, 'rev-parse', 'HEAD') == fixed
        assert manager.global_validation(LEAD, 'start', {'validation_id': plan['id']})['status'] == 'running'
        assert manager.global_validation(LEAD, 'finish', {'validation_id': plan['id']})['status'] == 'passed'


def test_original_input_change_event_invalidates_even_when_ignored_bytes_are_restored(tmp_path):
    host = FixtureHost()
    with Manager(tmp_path / 'state', owner_identity_ref=OWNER.subject, codex_adapter=queue_adapter(tmp_path), global_validation_host=host) as manager:
        mono, child, parent, kid = combination(manager, tmp_path)
        (child / '.git' / 'info' / 'exclude').write_text('hidden.py\n')
        (child / 'hidden.py').write_text('original ignored input\n')
        plan = manager.global_validation(LEAD, 'plan', {'request_id': parent, 'mono_commit': git(mono, 'rev-parse', 'HEAD'), 'children': [{'request_id': kid, 'path': 'child'}], 'test_ids': ['unit']})
        running = manager.global_validation(LEAD, 'start', {'validation_id': plan['id']})
        (child / 'hidden.py').write_text('temporary manual input\n')
        (child / 'hidden.py').write_text('original ignored input\n')
        host.runs[running['run']['run_id']]['input_changes'] = [{'path': str(child / 'hidden.py'), 'kind': 'source_changed'}]
        ended = manager.global_validation(LEAD, 'finish', {'validation_id': plan['id']})
        assert ended['status'] == 'invalidated' and ended['occupancy']['released'] is True
        assert ended['input_change_events'][0]['path'] == str(child / 'hidden.py')


def test_restart_without_original_input_watch_withdraws_current_completion(tmp_path):
    host = FixtureHost()
    with Manager(tmp_path / 'state', owner_identity_ref=OWNER.subject, codex_adapter=queue_adapter(tmp_path), global_validation_host=host) as manager:
        mono, child, parent, kid = combination(manager, tmp_path)
        plan = manager.global_validation(LEAD, 'plan', {'request_id': parent, 'mono_commit': git(mono, 'rev-parse', 'HEAD'), 'children': [{'request_id': kid, 'path': 'child'}], 'test_ids': ['unit']})
        manager.global_validation(LEAD, 'start', {'validation_id': plan['id']})
        manager.global_validation(LEAD, 'finish', {'validation_id': plan['id']})
        session = next(r for r in manager.read_snapshot(OWNER)['requests'] if r['id'] == parent)['session']
        (tmp_path / 'queue-observed.json').write_text(json.dumps({session['thread_id']: {'status': {'type': 'idle'}, 'turns': [{'id': session['turn_id'], 'status': 'completed', 'itemsView': 'full', 'items': [{'type': 'commandExecution', 'id': 'mono-test', 'command': 'python -m unittest', 'cwd': str(mono), 'status': 'completed', 'exitCode': 0, 'aggregatedOutput': 'Ran 1 test in 0.01s\n\nOK'}]}]}}))
        manager.record_task_delivery(LEAD, parent, {'source_commit': git(mono, 'rev-parse', 'HEAD'), 'issue_updated_at': ISSUE['updated_at'], 'criteria': [{'text': ISSUE['body'], 'test_item_ids': ['mono-test']}]})
        assert manager.global_validation(LEAD, 'complete', {'validation_id': plan['id']})['whole_project_complete'] is True
    with Manager(tmp_path / 'state', owner_identity_ref=OWNER.subject) as restored:
        current = restored.global_validation(LEAD, 'check', {'validation_id': plan['id']})
        assert current['status'] == 'unverified' and current['whole_project_complete'] is False
        assert current['occupancy']['released'] is True
        assert next(r for r in restored.read_snapshot(OWNER)['requests'] if r['id'] == parent)['whole_project_complete'] is False
