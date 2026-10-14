"""The original SDK receives repository work through the registered platform."""
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import json

import pytest
from sdk_source_integrity import source_snapshot


ROOT = Path(__file__).resolve().parents[1]


@pytest.mark.parametrize('case', ['delivery', 'delivery_unknown', 'delivery_wrong_sid', 'delivery_wrong_generation', 'current', 'history', 'pending', 'intent', 'not_requested', 'unknown_quote',
    'wrong_sid', 'wrong_generation', 'wrong_anchor', 'wrong_profile', 'duplicate', 'question', 'approval'])
def test_only_delivered_checkpoint_owner_quotes_enter_ordinary_discussion(tmp_path, case):
    from copy import deepcopy
    from types import SimpleNamespace as NS
    from ghost_hermes_pm.simple_development import RepositoryIntake
    from ghost_hermes_pm.messages import OWNED_PLATFORM
    anchor = {'app_id': 'fixture-app', 'transport_tenant_key': 'fixture-transport', 'tenant_key': 'fixture-owner-tenant',
        'recipient_tenant_key': 'fixture-bot-tenant', 'recipient_open_id': 'fixture-bot', 'chat_id': 'fixture-chat',
        'sender_open_id': 'fixture-owner'}
    binding = {**anchor, 'owner_open_id': 'fixture-owner', 'sender_tenant_key': 'fixture-owner-tenant',
        'profile_id': 'lead', 'target_profiles': ['lead', 'steward'], 'verification_ref': 'fixture-verification'}
    intake = RepositoryIntake(None, {'enabled': True, 'verification_ref': 'fixture-verification', 'bindings': [binding]},
        {'github_account': 'fixture-owner', 'github_config_dir': str(tmp_path)}, tmp_path / 'state')
    adapter, transport = NS(registered_bots=[]), object()
    intake.transports = [(adapter, transport)]
    checkpoint = {'id': 'fixture-checkpoint', 'status': 'reviewed', 'notify_owner': True,
        'session_id': 'fixture-session', 'generation': 'fixture-generation',
        'delivery': {'status': 'delivered', 'message_id': 'fixture-cp-message'}}
    question = {'id': 'fixture-old-question', 'state': 'settled', 'answerable': False,
        'delivery': {**anchor, 'message_id': 'fixture-question-message'}}
    execution = {'mode': 'shared_native', 'state': 'running', 'session_id': 'fixture-session',
        'generation': 'fixture-generation', 'questions': [question], 'supervision_checkpoint': checkpoint}
    record = {'id': 'work-' + 'a' * 32, 'requested_issue_url': 'https://github.com/fixture-user/fixture/issues/1',
        'profile_id': 'lead', 'superior_profile_id': 'steward', 'source_anchor': dict(anchor),
        'target': {'repo_path': str(tmp_path)}, 'dsh_execution': execution}
    parent, text = 'fixture-cp-message', '@_user_1 请纠正总结中的旧预算。'
    if case.startswith('delivery'):
        parent = 'fixture-delivery-message'
        execution['delivery_notification'] = {'status': 'delivered', 'message_id': parent,
            'session_id': execution['session_id'], 'generation': execution['generation']}
        if case == 'delivery_unknown':
            execution['delivery_notification']['status'] = 'unknown'
        elif case == 'delivery_wrong_sid':
            execution['delivery_notification']['session_id'] = 'foreign-session'
        elif case == 'delivery_wrong_generation':
            execution['delivery_notification']['generation'] = 'foreign-generation'
    elif case == 'history':
        execution['supervision_history'] = [checkpoint]
        execution['supervision_checkpoint'] = {**checkpoint, 'delivery': {'status': 'not_requested'}}
    elif case in {'pending', 'intent', 'not_requested'}:
        checkpoint['delivery']['status'] = case
    elif case == 'unknown_quote':
        parent = 'fixture-unknown-message'
    elif case == 'wrong_sid':
        checkpoint['session_id'] = 'foreign-session'
    elif case == 'wrong_generation':
        checkpoint['generation'] = 'foreign-generation'
    elif case == 'wrong_anchor':
        record['source_anchor']['sender_open_id'] = 'foreign-owner'
    elif case == 'wrong_profile':
        binding['profile_id'] = 'foreign-profile'
    elif case == 'question':
        parent = question['delivery']['message_id']
    elif case == 'approval':
        parent, text = 'fixture-unknown-approval', '@_user_1 批准一次'
        execution['approvals'] = [{'approval_id': 'fixture-approval', 'request_message_id': 'fixture-approval-message',
            'delivery': dict(anchor)}]
    intake._save(record)
    if case == 'duplicate':
        other = deepcopy(record)
        other.update(id='work-' + 'b' * 32, requested_issue_url='https://github.com/fixture-user/fixture/issues/2')
        intake._save(other)
    source = NS(platform=OWNED_PLATFORM, user_id='fixture-owner-user', is_bot=False,
        chat_id=anchor['chat_id'], message_id='fixture-feedback')
    sender = NS(sender_type='user', tenant_key=anchor['tenant_key'],
        sender_id=NS(open_id=anchor['sender_open_id'], user_id=source.user_id))
    message = NS(chat_type='group', message_type='text', chat_id=source.chat_id, message_id=source.message_id,
        content=json.dumps({'text': text}), parent_id=parent, root_id=None, thread_id=None,
        mentions=[NS(id=NS(open_id=anchor['recipient_open_id']), tenant_key=anchor['recipient_tenant_key'],
            mentioned_type='bot', key='@_user_1')])
    event = NS(source=source, message_id=source.message_id,
        raw_message=NS(header=NS(event_type='im.message.receive_v1', app_id=anchor['app_id'], tenant_key=anchor['transport_tenant_key']),
            event=NS(sender=sender, message=message)))
    before = intake.snapshot()
    prepared = intake.prepare(event, adapter)
    if case in {'current', 'history', 'delivery'}:
        assert prepared is None, 'Verified checkpoint feedback was diverted into question handling.'
    else:
        assert prepared is not None
        assert prepared.action == ('approval' if case == 'approval' else 'question')
        if case != 'question':
            assert prepared.rejected
    assert intake.snapshot() == before


@pytest.mark.parametrize('verified', [True, False])
def test_shared_acceptance_notification_reports_only_verified_files_tests_and_budget(verified):
    from ghost_hermes_pm.repository_supervision import acceptance_text
    acceptance = {'status': 'accepted', 'pr_status': 'awaiting_review', 'review_status': 'pending', 'leftovers': []}
    if verified:
        acceptance.update(changed_files=['feature.mjs', 'feature.test.mjs'],
            current_budget={'limits': {'max_model_requests': 40}, 'request_base': 20, 'requests_total': 27,
                'requests_used': 7, 'requests_remaining': 33, 'wall_seconds_remaining': None},
            test_evidence={'runner': 'original_shared_dsh_bash', 'runner_receipts': [
                {'executed_tests': 40, 'failed_tests': 0, 'skipped_tests': 0, 'exit_code': 0}]})
    record = {'issue': {'url': 'https://github.com/fixture-user/fixture/issues/1'},
        'dsh_execution': {'mode': 'shared_native', 'state': 'released', 'acceptance': acceptance}}
    text = acceptance_text(record)
    if verified:
        assert 'feature.mjs' in text and 'feature.test.mjs' in text
        assert '40 passed' in text and '0 failed' in text and '0 skipped' in text
        assert '已用 7' in text and '额度 40' in text and '剩余 33' in text and '累计 27' in text
        assert '无插件时限' in text and '无插件 token 配额' in text
    else:
        assert '变更文件：未核验' in text and '测试统计：未核验' in text and '当前窗口额度：未核验' in text


@pytest.mark.parametrize('section', [None, 'overview', 'delivery', 'budget', 'files'])
def test_current_snapshot_is_compact_and_uses_only_bound_current_facts(tmp_path, monkeypatch, section):
    from ghost_hermes_pm.simple_development import RepositoryIntake
    from ghost_hermes_pm import repository_execution, repository_acceptance, shared_repository
    intake = RepositoryIntake(None, {}, {'github_account': 'fixture-owner', 'github_config_dir': str(tmp_path)}, tmp_path / 'state')
    record = {'id': 'work-' + 'a' * 32, 'requested_issue_url': 'https://github.com/fixture-user/fixture/issues/1',
        'profile_id': 'lead', 'target': {'native_profile': 'fixture-native', 'repo_path': str(tmp_path), 'execution_ref': 'bound-reference'},
        'issue': {'url': 'https://github.com/fixture-user/fixture/issues/1', 'body': 'private issue body' * 20000},
        'dsh_execution': {'mode': 'shared_native', 'state': 'released', 'session_id': 'fixture-session',
            'generation': 'fixture-generation', 'reference_sha256': 'bound-reference-hash', 'history': 'private history' * 20000,
            'baseline': {'file_digests': {'feature.mjs': 'old'}},
            'acceptance': {'status': 'accepted', 'delivered': True, 'source_commit': 'a' * 40,
                'source_digest': 'current-digest', 'workspace_status': '', 'pr_status': 'awaiting_review', 'review_status': 'pending',
                'pr': {'url': 'https://github.com/fixture-user/fixture/pull/2'}, 'unmet': [], 'sync': [], 'fine_issues': [],
                'test_evidence': {'original_call_ids': ['fixture-test'], 'runner_receipts': [{
                    'test_files': ['feature.test.mjs'], 'executed_tests': 40, 'failed_tests': 0, 'skipped_tests': 0, 'exit_code': 0}]}}}}
    intake._save(record)
    config = {'mode': 'shared_native', 'reference_sha256': 'bound-reference-hash', 'budget': {'max_model_requests': 40}}
    monkeypatch.setattr(repository_execution, 'execution_configuration', lambda target: (dict(config), {}))
    monkeypatch.setattr(shared_repository, 'read_ledger', lambda cfg, execution: {
        'state': {'requests': 27, 'started_at_ms': 1}, 'renewWindow': {'request_base': 20, 'started_at_ms': 1}})
    monkeypatch.setattr(repository_acceptance, 'repository_source_state', lambda target: {
        'head': 'a' * 40, 'source_digest': 'current-digest', 'workspace_status': '', 'file_digests': {'feature.test.mjs': 'new'}})
    args = {'work_id': record['id'], 'section': section} if section else {}
    before = intake.snapshot()
    view = intake.current_snapshot('fixture-native', args)
    encoded = json.dumps(view, ensure_ascii=False)
    assert len(encoded) < 1500 and 'private history' not in encoded and 'private issue body' not in encoded
    if section in {'overview', 'budget'}:
        assert view['budget']['requests_total'] == 27 and view['budget']['requests_remaining'] == 33
    elif section == 'delivery':
        assert view['tests'][0]['executed_tests'] == 40 and view['pr']['url'].endswith('/pull/2')
    elif section == 'files':
        assert view['changed_files'] == ['feature.mjs', 'feature.test.mjs']
        assert view['test_files'] == ['feature.test.mjs']
    assert intake.current_snapshot('foreign-native', {'work_id': record['id']}) == {'status': 'rejected', 'code': 'unauthorized'}
    assert intake.current_snapshot('fixture-native', {'path': '/private/foreign'}) == {'status': 'rejected', 'code': 'invalid_change'}
    assert intake.snapshot() == before


def test_worker_save_preserves_current_shared_observer(tmp_path):
    from copy import deepcopy
    from ghost_hermes_pm.simple_development import RepositoryIntake
    intake = RepositoryIntake(None, {}, {'github_account': 'fixture-owner',
        'github_config_dir': str(tmp_path)}, tmp_path / 'state')
    record = {'id': 'fixture-work', 'requested_issue_url': 'https://github.com/fixture-user/fixture/issues/1',
        'dsh_execution': {'generation': 'fixture-generation', 'state': 'continuation_pending',
            'shared_event_observer': {'status': 'unavailable'}}}
    intake._save(record)
    stale_worker = deepcopy(record)
    record['dsh_execution']['shared_event_observer'] = {'status': 'ready', 'client_id': 'fixture-client'}
    intake._save_execution(record, fields=['shared_event_observer'])
    stale_worker['dsh_execution']['state'] = 'running'
    intake._save_execution(stale_worker)
    current = intake.snapshot()['work'][0]['dsh_execution']
    assert current['state'] == 'running'
    assert current['shared_event_observer'] == {'status': 'ready', 'client_id': 'fixture-client'}


def test_shared_observer_never_transfers_a_worker_claim():
    from ghost_hermes_pm.repository_supervision import _admit_resumed_worker
    record = {'dsh_execution': {'mode': 'shared_native', 'supervisor_claim': {'run_id': 1}}}
    # No controller admission or Kanban access is available to this observer.
    _admit_resumed_worker(None, record, None)
    assert record['dsh_execution']['supervisor_claim'] == {'run_id': 1}


def test_snapshot_exposes_current_qa_input_before_large_history(tmp_path):
    from ghost_hermes_pm.simple_development import RepositoryIntake
    intake = RepositoryIntake(None, {}, {'github_account': 'fixture-owner',
        'github_config_dir': str(tmp_path)}, tmp_path / 'state')
    record = {'id': 'fixture-work', 'requested_issue_url': 'https://github.com/fixture-user/fixture/issues/1',
        'history': 'H' * 10000, 'dsh_execution': {'state': 'outcome_unknown', 'first_input': 'accepted',
            'continuation_control': {'status': 'admitted'}, 'shared_event_observer': {'status': 'unavailable'},
            'last_failure': {'phase': 'observer', 'code': 'outcome_unknown', 'message': 'Original observer not ready.'}}}
    intake._save(record)
    snapshot = intake.snapshot()
    brief = snapshot['work_status'][0]
    assert brief['dsh_input_accepted'] is False and brief['state'] == 'outcome_unknown'
    assert brief['last_failure']['phase'] == 'observer'
    assert 'Original observer not ready.' in json.dumps(snapshot)[:1500]
    record['dsh_execution'].update(state='running', continuation_control={'status': 'accepted'})
    intake._save(record)
    current = intake.snapshot()['work_status'][0]
    assert current['dsh_input_accepted'] is True and current['last_failure'] is None
    assert current['historical_failure']['phase'] == 'observer'


@pytest.mark.parametrize('case', ['missing', 'present_changed', 'foreign_account'])
def test_shared_github_refresh_keeps_the_original_scope(tmp_path, monkeypatch, case):
    from types import SimpleNamespace
    from ghost_hermes_pm import shared_repository, repository_github
    from ghost_hermes_pm.manager import ManagementError
    directory = Path(tempfile.mkdtemp(prefix='hpm-shared-gh-', dir='/tmp')).resolve()
    reference = {'generation': 'fixture-generation', 'account': 'fixture-owner', 'repository': 'fixture-user/fixture',
        'config_dir': str(directory / 'home/tmp/github'), 'device': 1, 'inode': 2, 'account_verified': True}
    record = {'target': {'repository': reference['repository']}, 'dsh_execution': {
        'generation': reference['generation'], 'github_private_dir': str(directory), 'github_reference': dict(reference)}}
    saves, calls = [], []
    intake = SimpleNamespace(github=SimpleNamespace(account='fixture-owner'),
        _save_execution=lambda value, fields=None: saves.append(fields))
    prepared = {**reference, 'inode': 3}
    def prepare(*args):
        calls.append(args)
        return {'reference': prepared}
    monkeypatch.setattr(repository_github, 'prepare_github_execution', prepare)
    try:
        if case == 'present_changed':
            Path(reference['config_dir']).mkdir(parents=True)
        elif case == 'foreign_account':
            record['dsh_execution']['github_reference']['account'] = 'other-owner'
        if case != 'missing':
            with pytest.raises(ManagementError):
                shared_repository.prepare_shared_github(intake, record)
            assert not saves and 'github_reference_history' not in record['dsh_execution']
            if case == 'foreign_account':
                assert not calls
        else:
            assert shared_repository.prepare_shared_github(intake, record) == prepared
            assert record['dsh_execution']['github_reference_history'] == [reference]
    finally:
        shutil.rmtree(directory)


def test_shared_prompt_reports_original_ids_and_current_budget(tmp_path, monkeypatch):
    from ghost_hermes_pm import shared_repository, repository_execution
    from ghost_hermes_pm.shared_repository import _prompt
    criterion = 'Verify the synthetic public behavior.'
    record = {'issue': {'url': 'https://github.com/fixture-user/fixture/issues/1', 'title': 'Fixture',
        'body': '- [ ] ' + criterion + '\nHistorical 8192 token limit.', 'updated_at': '2026-10-10T00:00:00Z'},
        'target': {'repository': 'fixture-user/fixture', 'repo_path': str(tmp_path)},
        'dsh_execution': {'session_id': 'fixture-session', 'generation': 'fixture-generation', 'first_input': 'accepted',
            'github_reference': {'config_dir': '/private/tmp/fixture-scope', 'account': 'fixture-owner'},
            'acceptance': {'source_digest': 'fixture-digest', 'eligible_test_calls': ['native-test-call'], 'test_files': []}, 'tool_receipts': [
                {'call_id': 'native-test-call', 'name': 'bash', 'exit_code': 0, 'is_error': False,
                    'generation': 'fixture-generation', 'session_id': 'fixture-session',
                    'source_after': {'source_digest': 'fixture-digest'}},
                {'call_id': 'foreign-call', 'name': 'bash', 'exit_code': 0, 'is_error': False,
                    'generation': 'other', 'session_id': 'fixture-session', 'source_after': {'source_digest': 'fixture-digest'}}]}}
    monkeypatch.setattr(repository_execution, 'execution_configuration', lambda target: ({'budget': {}, 'ledger_dir': str(tmp_path)}, {}))
    monkeypatch.setattr(shared_repository, 'read_ledger', lambda cfg, execution: {'state': {'requests': 1, 'started_at_ms': None}})
    prompt = _prompt(None, record)
    assert 'no plugin output-token' in prompt and 'overrides historical budget text' in prompt
    assert 'native-test-call' in prompt and 'foreign-call' not in prompt
    assert 'Copy each frozen acceptance criterion verbatim' in prompt and criterion in prompt


@pytest.mark.parametrize('scenario', [
    'existing',
    'rich_post',
    'rich_post_direct',
    'rich_post_en',
    'create_unknown',
    'private_issue',
    'concurrent',
    'delegation',
    'authority',
    'wrong_card_assignee',
    'wrong_card_path',
    'wrong_card_body',
    'wrong_card_duplicate',
    'changed_card',
    'issue_unavailable',
    'issue_unavailable_unload',
    'private_all_bindings',
    'configuration_auto_decompose',
    'configuration_repository',
    'queued_rejection_unload',
    'responsibility_conflict',
    'worker_required',
    'natural_qa_intake',
    'natural_qa_discoverability',
    pytest.param('worker_dispatch', marks=pytest.mark.retired_independent_runtime),
    pytest.param('worker_query_race', marks=pytest.mark.retired_independent_runtime),
    pytest.param('worker_transport_timeout', marks=pytest.mark.retired_independent_runtime),
    pytest.param('worker_stop_terminal', marks=pytest.mark.retired_independent_runtime),
    pytest.param('worker_stop_reaped', marks=pytest.mark.retired_independent_runtime),
    pytest.param('worker_stop_native_front', marks=pytest.mark.retired_independent_runtime),
    pytest.param('worker_stop_native_background', marks=pytest.mark.retired_independent_runtime),
    pytest.param('worker_stop_native_live', marks=pytest.mark.retired_independent_runtime),
    pytest.param('worker_stop_native_promoted', marks=pytest.mark.retired_independent_runtime),
    pytest.param('worker_stop_running', marks=pytest.mark.retired_independent_runtime),
    pytest.param('worker_stop_uncovered', marks=pytest.mark.retired_independent_runtime),
    pytest.param('worker_retry_terminal', marks=pytest.mark.retired_independent_runtime),
    pytest.param('worker_retry_message_history', marks=pytest.mark.retired_independent_runtime),
    pytest.param('worker_stop_question', marks=pytest.mark.retired_independent_runtime),
    pytest.param('worker_stop_unknown', marks=pytest.mark.retired_independent_runtime),
    pytest.param('worker_retry_failed', marks=pytest.mark.retired_independent_runtime),
    pytest.param('worker_retry_observer', marks=pytest.mark.retired_independent_runtime)
])
def test_simple_repository_intake_uses_original_sdk_public_entries(scenario):
    flags = ('HPM_NATIVE_REAL_MODEL_REFERENCE', 'HPM_NATIVE_EXECUTION_REFERENCE')
    if scenario in {'worker_dispatch', 'worker_query_race', 'worker_transport_timeout', 'worker_stop_terminal', 'worker_stop_reaped', 'worker_stop_native_front', 'worker_stop_native_background', 'worker_stop_native_live', 'worker_stop_native_promoted', 'worker_stop_running', 'worker_stop_uncovered', 'worker_retry_terminal', 'worker_retry_message_history', 'worker_stop_question', 'worker_stop_unknown', 'worker_retry_failed', 'worker_retry_observer'}:
        assert bool(os.environ.get(flags[0])) == bool(os.environ.get(flags[1])), 'Real model acceptance requires both approved references.'
    native_real = scenario in {'worker_dispatch', 'worker_query_race', 'worker_transport_timeout'} and bool(os.environ.get(flags[0]))
    configured = os.environ.get('HERMES_TEST_SDK_ROOT')
    if not configured:
        if os.environ.get('HERMES_REQUIRE_SDK_SMOKE') == '1':
            pytest.fail('Original Hermes SDK is required.')
        pytest.skip('Set HERMES_TEST_SDK_ROOT; a skip does not establish native intake.')
    sdk = Path(configured).resolve(strict=True)
    before = source_snapshot(sdk)
    dsh_before = source_snapshot(os.environ['DSH_TEST_SDK_ROOT']) if scenario in {'worker_dispatch', 'worker_query_race', 'worker_transport_timeout', 'worker_stop_terminal', 'worker_stop_reaped', 'worker_stop_native_front', 'worker_stop_native_background', 'worker_stop_native_live', 'worker_stop_native_promoted', 'worker_stop_running', 'worker_stop_uncovered', 'worker_retry_terminal', 'worker_retry_message_history', 'worker_stop_question', 'worker_stop_unknown', 'worker_retry_failed', 'worker_retry_observer'} else None
    scratch_root = Path('/private/tmp/hermes-shared-dsh-20261012') if scenario in {'natural_qa_intake', 'natural_qa_discoverability'} else Path('/tmp')
    scratch_root.mkdir(mode=0o700, parents=True, exist_ok=True)
    scratch = Path(tempfile.mkdtemp(prefix='natural-qa-intake-' if scenario in {'natural_qa_intake', 'natural_qa_discoverability'} else 'hpm-simple-',
                                   dir=scratch_root)).resolve()
    cleaned = False
    passed = False
    try:
        home = scratch / 'home'
        plugin = home / 'plugins' / 'ghost-hermes-pm'
        plugin.mkdir(parents=True)
        home.chmod(0o700)
        for name in ('__init__.py', 'plugin.yaml', 'ghost_hermes_pm'):
            source = ROOT / name
            if source.is_dir():
                shutil.copytree(source, plugin / name, ignore=shutil.ignore_patterns('__pycache__'))
            else:
                shutil.copy2(source, plugin / name)
        env = {'PATH': os.environ.get('PATH', '/usr/bin:/bin'), 'HERMES_HOME': str(home),
               'HERMES_BUNDLED_PLUGINS': str(home / 'empty-bundled'),
               'HERMES_KANBAN_HOME': str(home), 'PYTHONDONTWRITEBYTECODE': '1',
               'PYTHONPATH': str(sdk), 'HERMES_TEST_SDK_ROOT': str(sdk)}
        if scenario in {'worker_dispatch', 'worker_query_race', 'worker_transport_timeout', 'worker_stop_terminal', 'worker_stop_reaped', 'worker_stop_native_front', 'worker_stop_native_background', 'worker_stop_native_live', 'worker_stop_native_promoted', 'worker_stop_running', 'worker_stop_uncovered', 'worker_retry_terminal', 'worker_retry_message_history', 'worker_stop_question', 'worker_stop_unknown', 'worker_retry_failed', 'worker_retry_observer'}:
            env['DSH_TEST_SDK_ROOT'] = os.environ['DSH_TEST_SDK_ROOT']
            for name in ('HPM_NATIVE_REAL_MODEL_REFERENCE', 'HPM_NATIVE_EXECUTION_REFERENCE'):
                if native_real and name in os.environ:
                    env[name] = os.environ[name]
            if os.environ.get('HPM_SYNTHETIC_WORKER_REFUSE'):
                env['HPM_SYNTHETIC_WORKER_REFUSE'] = os.environ['HPM_SYNTHETIC_WORKER_REFUSE']
        process = subprocess.Popen([sys.executable, str(ROOT / 'tests/simple_intake_smoke_runner.py'), str(scratch), scenario],
            cwd=scratch, env=env, text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        try:
            stdout, stderr = process.communicate(timeout=210 if native_real or scenario == 'worker_retry_message_history' else 220 if scenario == 'worker_stop_reaped' else 100)
        except subprocess.TimeoutExpired:
            process.terminate()
            try:
                process.communicate(timeout=20)
            except subprocess.TimeoutExpired:
                process.kill()
                process.communicate()
            pytest.fail('Fixture timed out; external execution cleanup is unknown. Private runtime retained: ' + str(scratch))
        result = subprocess.CompletedProcess(process.args, process.returncode, stdout, stderr)
        if native_real:
            model = json.loads(Path(os.environ['HPM_NATIVE_REAL_MODEL_REFERENCE']).read_text())
            key = next(line.split('=', 1)[1].strip().strip('\"\'') for line in Path(model['env_file']).read_text().splitlines()
                       if line.startswith(model['api_key_env'] + '='))
            assert key not in stdout + stderr, 'Credential appeared in captured fixture output.'
        outcome = scratch / 'cleanup-evidence.json'
        if outcome.exists():
            cleanup = json.loads(outcome.read_text())
            cleaned = not cleanup['cleanup_errors']
            assert cleanup['logging_flushed_after_stop'] and cleanup['credential_diagnostic_hits'] == 0
        assert source_snapshot(sdk) == before, 'Plugin intake cannot mutate original SDK source.'
        if dsh_before is not None:
            assert source_snapshot(os.environ['DSH_TEST_SDK_ROOT']) == dsh_before, 'Native worker cannot mutate original DSH SDK source.'
        if scenario in {'worker_dispatch', 'worker_query_race', 'worker_transport_timeout', 'worker_stop_terminal', 'worker_stop_reaped', 'worker_stop_native_front', 'worker_stop_native_background', 'worker_stop_native_live', 'worker_stop_native_promoted', 'worker_stop_running', 'worker_stop_uncovered', 'worker_retry_terminal', 'worker_retry_message_history', 'worker_stop_question', 'worker_stop_unknown', 'worker_retry_failed', 'worker_retry_observer'} and os.environ.get('HPM_NATIVE_EVIDENCE_DIR'):
            destination = Path(os.environ['HPM_NATIVE_EVIDENCE_DIR']).resolve(strict=True)
            import stat
            assert destination.is_dir() and destination.stat().st_uid == os.getuid() and stat.S_IMODE(destination.stat().st_mode) & 0o077 == 0
            for name in ('worker-validation-evidence.json', 'worker-failure-evidence.json', 'cleanup-evidence.json', 'control-message-replay-evidence.json'):
                if not (scratch / name).exists():
                    continue
                payload = (scratch / name).read_bytes()
                if native_real:
                    assert key.encode() not in payload, 'Credential appeared in evidence.'
                descriptor = os.open(destination / name, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
                with os.fdopen(descriptor, 'wb') as target:
                    target.write(payload)
        assert result.returncode == 0, result.stdout + result.stderr + '\nPrivate runtime retained: ' + str(scratch)
        assert cleaned, 'Cleanup remains unknown; private runtime retained: ' + str(scratch)
        assert 'synthetic-unused-secret' not in result.stdout + result.stderr
        assert (scratch / 'simple-smoke-result').read_text() == 'passed'
        passed = True
    finally:
        if cleaned and passed:
            shutil.rmtree(scratch)


@pytest.mark.retired_independent_runtime
def test_failed_public_worker_preserves_forensics(monkeypatch):
    if not os.environ.get('HERMES_TEST_SDK_ROOT'):
        pytest.skip('Original SDK is required.')
    monkeypatch.delenv('HPM_NATIVE_REAL_MODEL_REFERENCE', raising=False)
    monkeypatch.delenv('HPM_NATIVE_EXECUTION_REFERENCE', raising=False)
    monkeypatch.setenv('HPM_SYNTHETIC_WORKER_REFUSE', '1')
    before = set(Path('/private/tmp').glob('hpm-simple-*'))
    evidence = Path(tempfile.mkdtemp(prefix='hpm-failure-evidence-', dir='/tmp')).resolve()
    monkeypatch.setenv('HPM_NATIVE_EVIDENCE_DIR', str(evidence))
    retained = None
    try:
        with pytest.raises(AssertionError, match='awaiting_acceptance'):
            test_simple_repository_intake_uses_original_sdk_public_entries('worker_dispatch')
        created = set(Path('/private/tmp').glob('hpm-simple-*')) - before
        assert len(created) == 1, 'Business failure must retain its owner-only original diagnostic runtime.'
        retained = created.pop()
        assert list((retained / 'home/kanban/logs').glob('*.log'))
        assert (retained / 'home/state.db').exists()
        cleanup = json.loads((retained / 'cleanup-evidence.json').read_text())
        assert cleanup['business_failed'] is True and not cleanup['cleanup_errors']
        assert (evidence / 'cleanup-evidence.json').exists()
        diagnostic = json.loads((evidence / 'worker-failure-evidence.json').read_text())
        assert diagnostic['business_failed'] is True
        assert diagnostic['original_workers'][0]['original_log_sha256']
        sessions = diagnostic['original_workers'][0]['sessions']
        assert sessions and sessions[0]['actual_route']['api_call_count'] > 0
        assert sessions[0]['tool_calls'] == [], 'The external refusal never invokes DSH.'
        assert not list(evidence.rglob('.env'))
    finally:
        if retained is not None and not json.loads((retained / 'cleanup-evidence.json').read_text())['cleanup_errors']:
            shutil.rmtree(retained)
        shutil.rmtree(evidence)
