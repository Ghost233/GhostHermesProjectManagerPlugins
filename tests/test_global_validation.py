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
