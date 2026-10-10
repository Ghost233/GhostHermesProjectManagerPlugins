"""Frozen repository acceptance rejects unsupported completion claims."""
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time

import pytest

from ghost_hermes_pm.simple_development import RepositoryIntake


def test_completed_turn_without_original_test_receipts_remains_undelivered(tmp_path):
    from ghost_hermes_pm.repository_acceptance import accept_repository_work, repository_source_state
    repo = tmp_path / 'repository'
    repo.mkdir()
    subprocess.run(['git', 'init', '-q', str(repo)], check=True)
    gh = tmp_path / 'gh'
    gh.mkdir(mode=0o700)
    intake = RepositoryIntake('fixture:owner', {},
        {'github_account': 'fixture-user', 'github_config_dir': str(gh)}, str(tmp_path / 'private'))
    target = {'repo_path': str(repo), 'repository': 'fixture-user/fixture'}
    record = {'id': 'work-fixture', 'card_id': 'card-fixture', 'target': target,
        'issue': {'url': 'https://github.com/fixture-user/fixture/issues/7', 'title': 'Verify fixture',
                  'body': '- [ ] Verify the fixture feature.', 'updated_at': '2026-10-10T00:00:00Z'},
        'dsh_execution': {'state': 'awaiting_acceptance', 'generation': 'generation-fixture',
                          'session_id': 'session-fixture', 'baseline': repository_source_state(target),
                          'tool_receipts': [], 'jobs': []}}
    intake._save(record)
    report = {'issue_updated_at': record['issue']['updated_at'],
              'criteria': [{'text': 'Verify the fixture feature.', 'test_call_ids': ['fake-test']}],
              'source_commit': 'a' * 40, 'fine_issue_urls': [], 'review_call_ids': [], 'leftovers': []}
    events = [{'seq': 0, 'type': 'turn/start', 'data': {'turn': 'turn-fixture'}},
              {'seq': 1, 'type': 'assistant/message', 'data': {'turn': 'turn-fixture', 'message': {
                  'content': [{'type': 'text', 'text': 'HERMES_REPOSITORY_DELIVERY_JSON\n' + json.dumps(report)}]}}},
              {'seq': 2, 'type': 'turn/end', 'data': {'turn': 'turn-fixture', 'reason': {'kind': 'completed'}}}]
    import asyncio
    result = asyncio.run(accept_repository_work(intake, record, None, events,
        {'inbox': {'next-turn': [], 'next-step': []}}, intake.generation))
    assert result['status'] == 'unmet' and result['delivered'] is False
    assert result['next_action'] == 'kanban_block'
    assert 'test_evidence_missing' in result['unmet']
    saved = intake.snapshot()['work'][0]
    assert saved['dsh_execution']['state'] == 'awaiting_acceptance'
    assert saved['dsh_execution']['acceptance']['delivered'] is False


@pytest.fixture
def completed_work(tmp_path, monkeypatch):
    if not os.environ.get('DSH_TEST_SDK_ROOT'):
        if os.environ.get('DSH_REQUIRE_SDK_SMOKE') == '1':
            pytest.fail('Original DSH is required for read-only repository verification.')
        pytest.skip('Original DSH is required; a skip does not verify delivery.')
    source_parent = subprocess.run(['getconf', 'DARWIN_USER_TEMP_DIR'], capture_output=True, text=True, check=True).stdout.strip()
    tmp_path = Path(tempfile.mkdtemp(prefix='hpm-accept-unit-', dir=source_parent)).resolve()
    from ghost_hermes_pm.repository_acceptance import repository_source_state
    repo = tmp_path / 'repository'
    repo.mkdir()
    subprocess.run(['git', 'init', '-q', '-b', 'fixture-delivery', str(repo)], check=True)
    (repo / 'feature.py').write_text('def answer():\n    return 42\n')
    (repo / 'test_feature.py').write_text('from feature import answer\ndef test_answer():\n    assert answer() == 42\n')
    subprocess.run(['git', '-C', str(repo), 'add', '.'], check=True)
    subprocess.run(['git', '-C', str(repo), '-c', 'user.name=Synthetic Fixture',
                    '-c', 'user.email=fixture@example.invalid', 'commit', '-qm', 'Fixture source'], check=True)
    source = repository_source_state({'repo_path': str(repo)})
    gh_config = tmp_path / 'gh-config'
    gh_config.mkdir(mode=0o700)
    issue = {'url': 'https://github.com/fixture-user/fixture/issues/7', 'title': 'Verify fixture',
             'body': '- [ ] Verify the fixture feature.', 'updated_at': '2026-10-10T00:00:00Z'}
    pr_url = 'https://github.com/fixture-user/fixture/pull/9'
    gh_state = {'issue': {'url': issue['url'], 'title': issue['title'], 'body': issue['body'],
                         'updatedAt': issue['updated_at'], 'state': 'OPEN'},
                'subissues': [{'html_url': 'https://github.com/fixture-user/fixture/issues/8', 'state': 'closed'}],
                'pr': {'url': pr_url, 'state': 'OPEN', 'reviewDecision': 'REVIEW_REQUIRED',
                       'headRefOid': source['head'], 'headRefName': 'fixture-delivery',
                       'baseRefName': 'main', 'mergeCommit': None}, 'head': source['head'], 'login': 'fixture-user'}
    data = tmp_path / 'github.json'
    data.write_text(json.dumps(gh_state))
    gh = tmp_path / 'gh'
    gh.write_text('#!' + sys.executable + '\n' + '''
import json, os, sys
from pathlib import Path
p = Path(os.environ['SYNTHETIC_ACCEPTANCE_GH'])
v = json.loads(p.read_text())
a = sys.argv[1:]
with p.with_suffix('.calls').open('a') as f:
    f.write(json.dumps(a) + '\\n')
if a[:2] == ['auth', 'switch']:
    pass
elif a[:2] == ['api', 'user']:
    print(v['login'])
elif a[:2] == ['issue', 'view']:
    print(json.dumps(v['issue']))
elif a[:2] == ['pr', 'view']:
    print(json.dumps(v['pr']))
elif '/sub_issues' in a[1]:
    print(json.dumps([v['subissues']]))
elif '/git/ref/heads/' in a[1]:
    print(v['head'])
else:
    sys.exit(2)
''')
    gh.chmod(0o700)
    monkeypatch.setenv('PATH', str(tmp_path) + os.pathsep + os.environ['PATH'])
    monkeypatch.setenv('SYNTHETIC_ACCEPTANCE_GH', str(data))
    intake = RepositoryIntake('fixture:owner', {},
        {'github_account': 'fixture-user', 'github_config_dir': str(gh_config)}, str(tmp_path / 'private'))
    original_command = sys.executable + ' -m pytest test_feature.py'
    receipt = {'name': 'bash', 'generation': 'fixture-generation', 'session_id': 'fixture-session',
               'call_id': 'fixture-test', 'is_error': False, 'exit_code': 0, 'timed_out': False, 'aborted': False,
               'command': original_command, 'cwd': str(repo)}
    record = {'id': 'work-fixture', 'card_id': 'card-fixture', 'target': {'repo_path': str(repo), 'repository': 'fixture-user/fixture'},
              'issue': issue, 'dsh_execution': {'state': 'awaiting_acceptance', 'generation': 'fixture-generation',
                  'session_id': 'fixture-session', 'baseline': source, 'tool_receipts': [receipt], 'jobs': []}}
    report = {'issue_updated_at': issue['updated_at'],
              'criteria': [{'text': 'Verify the fixture feature.', 'test_call_ids': ['fixture-test']}],
              'source_commit': source['head'], 'pr_url': pr_url, 'sync_branches': ['fixture-delivery'],
              'fine_issue_urls': ['https://github.com/fixture-user/fixture/issues/8'], 'review_call_ids': [], 'leftovers': [],
              'test_files': ['test_feature.py']}
    events = [{'seq': 0, 'type': 'turn/start', 'data': {'turn': 'fixture-turn'}},
              {'seq': 1, 'type': 'tool/call', 'data': {'turn': 'fixture-turn', 'callId': 'fixture-test',
                  'name': 'bash', 'arguments': json.dumps({'command': original_command})}},
              {'seq': 2, 'type': 'tool/result', 'data': {'turn': 'fixture-turn',
                  'message': {'toolCallId': 'fixture-test', 'isError': False, 'content': []}}}]
    from simple_worker_model import execution_reference
    from simple_model_boundary import OrdinaryModelService
    from ghost_hermes_pm.repository_execution import execution_configuration
    from ghost_hermes_pm.dsh_owned_transport import PersistentOwnedTransport
    from ghost_hermes_pm.repository_supervision import owned_call
    model = OrdinaryModelService()
    reference = execution_reference(tmp_path, model.base_url, Path(os.environ['DSH_TEST_SDK_ROOT']))
    admitted, environment = execution_configuration({'execution_ref': reference})
    node = admitted.pop('node_bin')
    configuration = dict(admitted, workspace=str(repo), dsh_home=str(tmp_path / 'owned/home'),
        generation='fixture-generation', instance_id='fixture-instance', session_id='fixture-session',
        test_python=sys.executable, test_runner_path=str(Path(__file__).resolve().parents[1] / 'ghost_hermes_pm/repository_test_runner.py'))
    carrier = PersistentOwnedTransport(instance_dir=str(tmp_path / 'owned'), configuration=configuration,
                                      node_bin=node, environment=environment, timeout=30)
    owned_call(carrier, 'session/create', {'sessionId': 'fixture-session', 'cwd': str(repo), 'agentPreset': 'hermes-owned'})
    owned_call(carrier, 'session/prompt', {'sessionId': 'fixture-session', 'requestId': 'fixture-input', 'mode': 'queue',
        'content': [{'type': 'text', 'text': 'Read-only synthetic validation: reply briefly, without tools.'}]})
    deadline = time.monotonic() + 15
    while time.monotonic() < deadline:
        original = owned_call(carrier, 'session/list', {})['items'][0]
        if original.get('agentAvailable') is True and original.get('running') is False:
            break
        time.sleep(.02)
    assert original.get('agentAvailable') is True and original.get('running') is False
    def finish():
        intake._save(record)
        history = events + [
            {'seq': len(events), 'type': 'assistant/message', 'data': {'turn': 'fixture-turn', 'message': {
                'content': [{'type': 'text', 'text': 'HERMES_REPOSITORY_DELIVERY_JSON\n' + json.dumps(report)}]}}},
            {'seq': len(events) + 1, 'type': 'turn/end', 'data': {'turn': 'fixture-turn', 'reason': {'kind': 'completed'}}}]
        import asyncio
        from ghost_hermes_pm.repository_acceptance import accept_repository_work
        return asyncio.run(accept_repository_work(intake, record, carrier, history,
            {'inbox': {'next-turn': [], 'next-step': []}}, intake.generation))
    try:
        yield {'intake': intake, 'record': record, 'report': report, 'receipt': receipt, 'source': source,
               'gh': gh_state, 'gh_path': data, 'events': events, 'finish': finish, 'repo': repo}
    finally:
        carrier.shutdown_owned()
        assert carrier.close_outcome['kind'] == 'original_exit'
        model.close()


def test_verified_delivery_can_complete_while_pr_awaits_review(completed_work):
    result = completed_work['finish']()
    assert result['unmet'] == [], result
    assert result['status'] == 'accepted', result
    assert result['delivered'] is True and result['next_action'] == 'kanban_complete'
    assert result['pr_status'] == 'awaiting_review'
    assert result['source_commit'] == completed_work['source']['head']
    assert result['sync'][0]['local_commit'] == result['sync'][0]['remote_commit']
    calls = [json.loads(line) for line in completed_work['gh_path'].with_suffix('.calls').read_text().splitlines()]
    for offset, call in enumerate(calls):
        if call[:2] != ['auth', 'switch'] and call[:2] != ['api', 'user']:
            assert calls[offset - 2][:2] == ['auth', 'switch'] and calls[offset - 1][:2] == ['api', 'user']


def test_verification_allows_private_tmp_and_denies_repository_and_git_writes(completed_work):
    work = completed_work
    (work['repo'] / 'test_feature.py').write_text(
        'from pathlib import Path\nimport pytest\nfrom feature import answer\n'
        'def test_scoped_verification(tmp_path):\n'
        '    result = tmp_path / "result.txt"\n'
        '    result.write_text("artifact")\n'
        '    assert result.read_text() == "artifact"\n'
        '    assert answer() == 42\n'
        '    repository = Path(__file__).parent\n'
        '    for relative in ("feature.py", ".git/config"):\n'
        '        original = (repository / relative).read_bytes()\n'
        '        with pytest.raises(PermissionError):\n'
        '            (repository / relative).write_text("forbidden")\n'
        '        assert (repository / relative).read_bytes() == original\n')
    subprocess.run(['git', '-C', str(work['repo']), 'add', 'test_feature.py'], check=True)
    subprocess.run(['git', '-C', str(work['repo']), '-c', 'user.name=Synthetic Fixture',
                    '-c', 'user.email=fixture@example.invalid', 'commit', '-qm', 'Verify test write boundary'], check=True)
    from ghost_hermes_pm.repository_acceptance import repository_source_state
    source = repository_source_state(work['record']['target'])
    work['report']['source_commit'] = source['head']
    work['gh']['head'] = source['head']
    work['gh']['pr']['headRefOid'] = source['head']
    work['gh_path'].write_text(json.dumps(work['gh']))
    result = work['finish']()
    assert result['unmet'] == [], result
    assert result['status'] == 'accepted'
    receipt = result['test_evidence']['runner_receipt']
    assert receipt['executed_tests'] == 1 and receipt['actual_exit_code'] == 0
    assert receipt['source_access'] == receipt['git_access'] == 'read-only'
    assert receipt['before_source_digest'] == receipt['after_source_digest'] == source['source_digest']
    artifacts = Path(receipt['artifact_roots'][0])
    assert list((artifacts / 'pytest').glob('test_scoped_verification*/result.txt'))


def test_missing_starting_source_baseline_remains_undelivered(completed_work):
    completed_work['record']['dsh_execution']['baseline'] = None
    result = completed_work['finish']()
    assert result['status'] == 'unmet' and result['delivered'] is False
    assert 'workspace_handoff_unconfirmed' in result['unmet']


@pytest.mark.parametrize(('case', 'reason'), [
    ('failed', 'test_evidence_missing'), ('timeout', 'test_evidence_missing'),
    ('cancelled', 'test_evidence_missing'), ('wrong_execution', 'test_evidence_missing'),
    ('wrong_version', 'source_version_mismatch'), ('scope_changed', 'accepted_scope_changed'),
    ('fine_issue_open', 'fine_issue_work_unconfirmed'), ('branch_behind', 'branch_sync_unconfirmed'),
    ('account_mismatch', 'unauthorized'), ('preserved_content_changed', 'preserved_user_content_changed'),
])
def test_incomplete_or_mismatched_work_remains_undelivered(completed_work, case, reason):
    work = completed_work
    if case == 'failed':
        work['receipt']['exit_code'] = 1
    elif case == 'timeout':
        work['receipt']['timed_out'] = True
    elif case == 'cancelled':
        work['receipt']['aborted'] = True
    elif case == 'wrong_execution':
        work['receipt']['generation'] = 'different-generation'
    elif case == 'wrong_version':
        work['report']['source_commit'] = 'a' * 40
    elif case == 'scope_changed':
        work['gh']['issue']['body'] = '- [ ] Different acceptance scope.'
    elif case == 'fine_issue_open':
        work['gh']['subissues'][0]['state'] = 'open'
    elif case == 'branch_behind':
        work['gh']['head'] = 'b' * 40
    elif case == 'account_mismatch':
        work['gh']['login'] = 'different-account'
    else:
        original = work['repo'] / 'personal-note.txt'
        original.write_text('Original synthetic owner note.\n')
        from ghost_hermes_pm.repository_acceptance import repository_source_state
        work['record']['dsh_execution']['baseline'] = repository_source_state(work['record']['target'])
        original.write_text('Changed synthetic owner note.\n')
    work['gh_path'].write_text(json.dumps(work['gh']))
    result = work['finish']()
    assert result['status'] == 'unmet' and result['delivered'] is False, result
    assert reason in result['unmet'], result
    assert result['next_action'] == 'kanban_block'
    assert work['record']['dsh_execution']['state'] == 'awaiting_acceptance'


def test_issue_activity_without_scope_changes_preserves_frozen_acceptance(completed_work):
    work = completed_work
    work['gh']['issue']['updatedAt'] = '2026-10-10T01:00:00Z'
    work['gh_path'].write_text(json.dumps(work['gh']))
    result = work['finish']()
    assert result['status'] == 'accepted' and result['delivered'] is True, result
