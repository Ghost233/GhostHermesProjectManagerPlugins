"""Global validation through the authenticated public operation and real fixture Git."""
import json
import subprocess
from pathlib import Path

import pytest

from ghost_hermes_pm import Manager, ManagementError, VerifiedIdentity
from ghost_hermes_pm.transport import ManagementClient, ManagementServer
from test_directory import OWNER, registration
from test_repository_queue import commit_repo, acknowledge, queue_adapter
from test_task_execution import prepare_fixture
from test_requests import ISSUE

LEAD = VerifiedIdentity('fixture:lead', 'participant')


def git(repo, *args):
    return subprocess.check_output(['git', '-C', str(repo), *args], text=True).strip()


def combination(manager, root):
    mono, _ = commit_repo(root / 'mono')
    child, child_head = commit_repo(mono / 'child')
    (mono / '.gitmodules').write_text('[submodule "child"]\n\tpath = child\n\turl = ./child\n')
    subprocess.run(['git', '-C', str(mono), 'add', '.gitmodules'], check=True)
    subprocess.run(['git', '-C', str(mono), 'update-index', '--add', '--cacheinfo', '160000,' + child_head + ',child'], check=True)
    subprocess.run(['git', '-C', str(mono), '-c', 'user.name=Fixture', '-c', 'user.email=fixture@example.invalid', 'commit', '-qm', 'fixed child'], check=True)
    value = registration(mono)
    value['profile']['connection_refs']['codex'] = 'local:fixture-stdio'
    manager.apply_directory_change(OWNER, 0, value)
    value = registration(child, 'child-project', 'child-lead')
    value['profile'].update(identity_ref='fixture:child', role='subproject_lead', parent_profile_id='mono-lead', connection_refs={'codex': 'local:fixture-stdio'})
    manager.apply_directory_change(OWNER, manager.read_snapshot(OWNER)['version'], value)
    parent = acknowledge(manager, suffix='mono')
    kid = acknowledge(manager, 'child-project', 'child-lead', 'child')
    prepare_fixture(manager, kid, child)
    session = manager.start_task(OWNER, kid)['session']
    (root / 'queue-observed.json').write_text(json.dumps({session['thread_id']: {'status': {'type': 'idle'}, 'turns': [{'id': session['turn_id'], 'status': 'completed', 'itemsView': 'full', 'items': [{'type': 'commandExecution', 'id': 'child-test', 'command': 'python -m unittest', 'cwd': str(child), 'status': 'completed', 'exitCode': 0, 'aggregatedOutput': 'OK'}]}]}}))
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
        return {'validation_id': context['id'], 'input_digest': context['input_digest'], 'source_access': 'read-only',
                'git_access': 'read-only', 'artifact_roots': context['repository']['test_artifact_paths'],
                'scope': 'synthetic-fixture', 'platform_enforcement': 'fixture-only', 'tool_paths': 'fixture-only',
                'preexisting_hardlink': 'fixture-only', 'process_paths': 'fixture-only', 'evidence_ref': 'fixture:approved-synthetic-repository'}

    def start(self, context):
        import hashlib
        import sys
        result = subprocess.run([sys.executable, '-c', 'from pathlib import Path; import sys; assert Path("child/source.py").read_text() == sys.argv[1]; print("1 test passed")', self.expected], cwd=context['repository']['worktree'], capture_output=True)
        run_id = 'run-' + context['id']
        self.runs[run_id] = {'run_id': run_id, 'validation_id': context['id'], 'input_digest': context['input_digest'], 'status': 'ended',
            'related_execution': 'ended', 'tests': [{'id': 'unit', 'argv': ['python', '-m', 'unittest'], 'cwd': context['repository']['worktree'],
                'exit_code': result.returncode, 'output_digest': hashlib.sha256(result.stdout + result.stderr).hexdigest(), 'artifact_refs': []}], 'defects': []}
        return {'run_id': run_id, 'validation_id': context['id'], 'input_digest': context['input_digest']}

    def read_result(self, run_id):
        return self.runs[run_id]


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
        returned = manager.global_validation(LEAD, 'rework', {'validation_id': plan['id'], 'target': 'child', 'issue_url': 'https://github.com/Ghost233/fixture/issues/28'})
        assert returned['rework'][0]['route'] == 'child' and returned['rework'][0]['profile_id'] == 'child-lead'
        assert returned['rework'][0]['issue']['url'].endswith('/issues/28')
        handoff = returned['rework'][0]['handoff_id']
        packet = manager.collaborate(LEAD, 'claim_delivery', {'handoff_id': handoff})
        assert packet['mention_open_id'] == 'child-seen-lead' and '/issues/28' in packet['text']
        assert returned['occupancy']['released'] is True
        assert manager.global_validation(LEAD, 'rework', {'validation_id': plan['id'], 'target': 'child', 'issue_url': 'https://github.com/Ghost233/fixture/issues/28'})['rework'] == returned['rework']


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
        (tmp_path / 'queue-observed.json').write_text(json.dumps({session['thread_id']: {'status': {'type': 'idle'}, 'turns': [{'id': session['turn_id'], 'status': 'completed', 'itemsView': 'full', 'items': [{'type': 'commandExecution', 'id': 'mono-test', 'command': 'python -m unittest', 'cwd': str(mono), 'status': 'completed', 'exitCode': 0, 'aggregatedOutput': 'OK'}]}]}}))
        manager.record_task_delivery(LEAD, parent, {'source_commit': git(mono, 'rev-parse', 'HEAD'), 'issue_updated_at': ISSUE['updated_at'], 'criteria': [{'text': ISSUE['body'], 'test_item_ids': ['mono-test']}]})
        completed = manager.global_validation(LEAD, 'complete', {'validation_id': plan['id']})
        assert completed['whole_project_complete'] is True and completed['status'] == 'complete'
        (child / 'source.py').write_text('late manual input\n')
        invalid = manager.global_validation(LEAD, 'check', {'validation_id': plan['id']})
        assert invalid['whole_project_complete'] is False and invalid['status'] == 'invalidated'
