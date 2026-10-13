"""Shared Desktop public HTTP seam; no new runtime or authenticated URL export."""
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
import threading
from pathlib import Path
import subprocess
import sys
import shutil
import asyncio
import hashlib
import time
from copy import deepcopy
from types import SimpleNamespace

from sdk_source_integrity import source_snapshot
from test_repository_github import github_source  # noqa: F401

import pytest

from ghost_hermes_pm.manager import ManagementError


def _natural_qa_fixture(tmp_path, monkeypatch, *, expired=False, foreign_host=False):
    from ghost_hermes_pm import shared_repository
    from ghost_hermes_pm import repository_acceptance, repository_control, repository_execution, repository_supervision
    now = int(time.time() * 1000)
    repo = tmp_path / 'repo'
    repo.mkdir()
    ledger_dir = tmp_path / 'ledger'
    ledger_dir.mkdir(mode=0o700)
    sid = 'session-fixture'
    generation = 'generation-fixture'
    source = {'head': 'a' * 40, 'source_digest': 'd' * 64, 'workspace_status': ''}
    baseline = {'head': 'b' * 40, 'source_digest': 'e' * 64, 'workspace_status': ''}
    started = now - (2_000_000 if expired else 1_000)
    total_requests = 2 if expired else 5
    state = {'started_at_ms': started, 'requests': total_requests, 'reported_tokens': 20,
             'admissions': ['request-' + str(index) for index in range(total_requests)],
             'stop_reason': 'shared-work-wall-budget' if expired else None}
    previous_state = {'started_at_ms': started, 'requests': 1, 'reported_tokens': 10,
                      'admissions': ['request-0'],
                      'stop_reason': 'shared-work-wall-budget' if expired else None}
    renew_window = {'id': 'old-window', 'message_id': 'old-message', 'host_lease': 'lease-fixture',
        'request_base': 1, 'original_state': previous_state, 'original_host_lease': 'lease-original',
        'started_at_ms': started, 'stop_reason': 'shared-work-wall-budget' if expired else None,
        'hook_marker': 'same-sid-window-v1'}
    row = {'schema': 1, 'session_id': sid, 'generation': generation, 'session_created_at': 7,
        'workspace': str(repo), 'host_lease': 'lease-fixture', 'service_ref': 'fixture-native',
        'agent_preset': 'fixture-shared', 'provider': 'fixture', 'model': 'fixture',
        'budget': {'max_wall_seconds': 1800, 'max_model_requests': 80}, 'state': state,
        'renewWindow': renew_window}
    ledger_file = ledger_dir / (hashlib.sha256(sid.encode()).hexdigest() + '.json')
    ledger_file.write_text(json.dumps(row))
    ledger_file.chmod(0o600)
    anchor = {'app_id': 'fixture-app', 'transport_tenant_key': 'fixture-app-tenant',
        'tenant_key': 'fixture-owner-tenant', 'recipient_tenant_key': 'fixture-bot-tenant',
        'recipient_open_id': 'fixture-bot', 'chat_id': 'fixture-chat', 'sender_open_id': 'fixture-owner'}
    context = {**anchor, 'profile_id': 'fixture-profile', 'message_id': 'natural-qa-message'}
    events = []
    if expired:
        events.extend([
            {'type': 'event', 'event': {'type': 'turn/start', 'seq': 0, 'time': 1, 'data': {'turn': 1}}},
            {'type': 'event', 'event': {'type': 'assistant/message', 'seq': 1, 'time': 1,
                'data': {'turn': 1, 'step': 1, 'message': {'id': 'interrupted', 'role': 'assistant', 'content': []}}}},
            {'type': 'event', 'event': {'type': 'step/end', 'seq': 2, 'time': 1, 'data': {'turn': 1, 'step': 1}}},
            {'type': 'event', 'event': {'type': 'turn/end', 'seq': 3, 'time': 1, 'data': {'turn': 1,
                'reason': {'kind': 'aborted', 'reason': {'kind': 'hook', 'reason': 'shared-work-wall-budget'}}}}},
        ])
        start_seq, turn = 4, 2
    else:
        start_seq, turn = 0, 1
    report = {'source_commit': source['head'], 'issue_updated_at': '2026-10-10T00:00:00Z',
        'criteria': [{'text': 'Implement synthetic QA and verify it.', 'test_call_ids': ['fixture-test']}],
        'test_files': ['qa.test.mjs'], 'fine_issue_urls': [], 'pr_url': None, 'sync_branches': [], 'leftovers': []}
    events.extend([
        {'type': 'event', 'event': {'type': 'turn/start', 'seq': start_seq, 'time': 2, 'data': {'turn': turn}}},
        {'type': 'event', 'event': {'type': 'assistant/message', 'seq': start_seq + 1, 'time': 2,
            'data': {'turn': turn, 'step': 1, 'message': {'id': 'delivery', 'role': 'assistant',
                'content': [{'type': 'text', 'text': 'HERMES_REPOSITORY_DELIVERY_JSON ' + json.dumps(report)}]}}}},
        {'type': 'event', 'event': {'type': 'turn/end', 'seq': start_seq + 2, 'time': 2,
            'data': {'turn': turn, 'reason': {'kind': 'completed'}}}},
    ])
    before = {'header': {'id': sid, 'createdAt': 7, 'cwd': str(repo)}, 'hasMore': False,
        'cursor': events[-1]['event']['seq'], 'records': events,
        'projections': {'values': {'agentPreset': 'fixture-shared', 'permissions': {'currentValue': 'workspace-write'},
            'inbox': {'next-turn': [], 'next-step': []}}}}
    request = {}
    after = json.loads(json.dumps(before))
    after['cursor'] += 2
    after['records'].extend([
        {'type': 'event', 'event': {'type': 'agent/inbox/spliced', 'seq': before['cursor'] + 1, 'time': 3,
            'data': {'target': 'next-turn', 'start': 0, 'inserted': []}}},
        {'type': 'event', 'event': {'type': 'turn/start', 'seq': before['cursor'] + 2, 'time': 3,
            'data': {'turn': turn + 1}}},
    ])
    card = {'status': 'blocked', 'current_run_id': None, 'worker_pid': None}
    carrier_identity = {'kind': 'shared_native', 'lease': 'lease-fixture', 'pid': 12, 'created_at': 1}
    carrier_current_identity = {'kind': 'shared_native', 'lease': 'foreign-lease', 'pid': 13, 'created_at': 2} if foreign_host else carrier_identity

    class Carrier:
        native_identity = carrier_current_identity
        config = {'generation': generation}
        def __init__(self):
            self.prompts = []
        def stream(self, *_args):
            value = before if not self.prompts else after
            class Stream:
                def __iter__(self):
                    return iter((value,))
                def __next__(self):
                    return value
                def close(self):
                    return None
            return Stream()
        def owned_jobs(self):
            return []
        def close(self):
            return None

    carrier = Carrier()
    execution = {'mode': 'shared_native', 'state': 'awaiting_acceptance', 'terminal_reason': {'kind': 'completed'},
        'first_input': 'accepted', 'session_id': sid, 'generation': generation, 'session_created_at': 7,
        'workspace_id': 'workspace-fixture', 'native_identity': carrier_identity,
        'reference_sha256': 'reference-sha', 'supervisor_claim': {'run_id': 1}, 'control_message_ids': [],
        'baseline': baseline, 'acceptance': {'status': 'unmet', 'delivered': False, 'unmet': ['test_evidence_missing'],
            'source_commit': source['head'], 'source_digest': source['source_digest'],
            'workspace_status': source['workspace_status']}, 'verification_prefix': 'fixture-verification-entry',
        'github_reference': {'config_dir': '/private/tmp/fixture-gh', 'account': 'fixture-account'}}
    record = {'id': 'work-' + 'a' * 32, 'profile_id': 'fixture-profile', 'source_anchor': anchor,
        'target': {'repo_path': str(repo), 'repository': 'fixture-user/fixture', 'execution_ref': str(tmp_path / 'reference')},
        'issue': {'url': 'https://github.com/fixture-user/fixture/issues/7', 'title': 'Synthetic QA',
                  'body': 'Implement synthetic QA and verify it.', 'updated_at': '2026-10-10T00:00:00Z'},
        'card_id': 'card-fixture', 'dsh_execution': execution}
    reference_file = tmp_path / 'reference'
    reference_file.write_text('{}')
    reference_file.chmod(0o600)
    lock_path = tmp_path / 'intake.lock'
    lock_path.touch(mode=0o600)
    intake = SimpleNamespace(secret_values=[], generation=generation, saved=[], card=card, lock_path=str(lock_path))
    intake.snapshot = lambda: {'work': [record]}
    def save_execution(saved_record, fields=None):
        intake.saved.append(tuple(fields or []))
    def kanban(name, _args):
        if name == 'kanban_unblock':
            card.update(status='ready', current_run_id=None, worker_pid=None)
        return {'task': card}
    intake._save_execution = save_execution
    intake._kanban = kanban
    intake.require_active = lambda _generation: None
    monkeypatch.setattr(shared_repository, 'SharedDesktopTransport', lambda _configuration: carrier)
    monkeypatch.setattr(repository_control, '_card', lambda _intake, _record: card)
    monkeypatch.setattr(repository_control, '_worker_end', lambda _intake, _record, **_kwargs: {'run_id': 1, 'exit_code': 0})
    config = {'reference_sha256': 'reference-sha', 'workspace': str(repo), 'session_id': sid,
        'generation': generation, 'workspace_id': 'workspace-fixture', 'agent_preset': 'fixture-shared',
        'host_lease': 'lease-fixture', 'ledger_dir': str(ledger_dir),
        'budget': {'max_wall_seconds': 1800, 'max_model_requests': 80}}
    desktop_receipt = tmp_path / 'desktop-receipt'
    desktop_receipt.write_text(json.dumps({'lease': 'lease-fixture'}))
    desktop_receipt.chmod(0o600)
    config['desktop_receipt'] = str(desktop_receipt)
    intake.config = config
    reference_file.write_text(json.dumps(config))
    record['dsh_execution']['reference_sha256'] = hashlib.sha256(reference_file.read_bytes()).hexdigest()
    config['reference_sha256'] = record['dsh_execution']['reference_sha256']
    monkeypatch.setattr(shared_repository, 'shared_configuration', lambda _reference, _configuration, workspace=None: config)
    monkeypatch.setattr(repository_execution, 'execution_configuration', lambda _target: (
        {**config, 'reference_sha256': hashlib.sha256(reference_file.read_bytes()).hexdigest()}, {}))
    monkeypatch.setattr(shared_repository, 'desktop_identity', lambda *_args: (carrier.native_identity, 'fixture-endpoint', 'fixture-login'))
    monkeypatch.setattr(repository_acceptance, 'repository_source_state', lambda _target: source)
    monkeypatch.setattr(shared_repository, 'shared_receipts', lambda *_args: [{'source_before': baseline,
        'source_after': source, 'generation': generation, 'session_id': sid, 'name': 'bash',
        'execution_admission': 'admitted', 'is_error': False, 'kind': 'foreground', 'exit_code': 0,
        'timed_out': False, 'aborted': False, 'native_session_seq': 1}])
    monkeypatch.setattr(shared_repository, '_verified_source_chain', lambda *_args, **_kwargs: True)
    monkeypatch.setattr(shared_repository, 'read_ledger', lambda _config, _execution: json.loads(ledger_file.read_text()))
    def owned_call(_carrier, method, args):
        if method == 'session/list':
            return {'items': [{'sessionId': sid, 'cwd': str(repo), 'running': False, 'agentAvailable': True}]}
        if method == 'workspace/create':
            return {'created': False, 'workspace': {'workspaceId': 'workspace-fixture', 'path': str(repo), 'sessionIds': [sid]}}
        if method == 'session/create':
            return {'sessionId': sid, 'agentPreset': 'fixture-shared'}
        if method == 'session/prompt':
            request.update(args)
            carrier.prompts.append(args)
            inserted = after['records'][-2]['event']['data']['inserted']
            inserted.append({'id': 'message-' + args['requestId'], 'role': 'user',
                'content': args['content'], 'source': {'kind': 'user', 'rpcId': args['requestId']}})
            return {'accepted': True}
        raise AssertionError(method)
    monkeypatch.setattr(repository_supervision, 'owned_call', owned_call)
    return record, context, intake, carrier, ledger_file, source, request


@pytest.mark.parametrize('source_profile', ['fixture-profile', 'fixture-superior'])
def test_natural_qa_rework_active_public_tool_queues_one_same_session_prompt(tmp_path, monkeypatch, source_profile):
    from ghost_hermes_pm.shared_repository import admit_shared_natural_qa_rework
    record, context, intake, carrier, ledger_file, _source, request = _natural_qa_fixture(tmp_path, monkeypatch)
    record['superior_profile_id'] = 'fixture-superior'
    context['profile_id'] = source_profile
    result = admit_shared_natural_qa_rework(intake, record, context, 'Fix only the missing synthetic QA receipt.', None, intake.generation)
    assert result == {'status': 'accepted', 'work_id': record['id']}
    assert len(carrier.prompts) == 0 and not request
    assert record['dsh_execution']['state'] == 'continuation_pending'
    assert record['dsh_execution']['continuation_control']['kind'] == 'qa_rework'
    assert record['dsh_execution']['continuation_control']['dispatch_status'] == 'accepted'
    assert intake.card['status'] == 'ready'
    assert json.loads(ledger_file.read_text())['state']['requests'] == 5
    with pytest.raises(ManagementError) as duplicate:
        admit_shared_natural_qa_rework(intake, record, context, 'Fix only the missing synthetic QA receipt.', None, intake.generation)
    assert duplicate.value.code in {'duplicate', 'outcome_unknown'}
    assert len(carrier.prompts) == 0


@pytest.mark.parametrize('prior_status', ['accepted', 'maintenance_required'])
@pytest.mark.parametrize('change', ['valid', 'pending', 'input_intent', 'foreign_source',
                                  'original_message', 'historical_duplicate', 'seal_changed', 'expired'])
def test_completed_natural_qa_accepts_new_feedback_in_the_same_active_window(tmp_path, monkeypatch,
                                                                         prior_status, change):
    from ghost_hermes_pm.shared_repository import admit_shared_natural_qa_rework
    record, context, intake, carrier, ledger_file, _source, request = _natural_qa_fixture(
        tmp_path, monkeypatch, expired=change == 'expired')
    execution = record['dsh_execution']
    prior_continuation = {'kind': 'qa_rework', 'status': 'accepted', 'dispatch_status': 'accepted',
        'message_id': 'prior-qa-input', 'qa_feedback': 'Previous synthetic QA requirement.'}
    prior_natural = {'kind': 'browser_qa_rework', 'status': prior_status, 'message_id': 'prior-qa-message',
        'feedback': 'Previous synthetic QA requirement.',
        'window': {'max_wall_seconds': 1800, 'max_model_requests': 80},
        'proposal': {'id': 'prior-maintenance-proposal', 'kind': 'maintenance', 'options': ['维护完成', '取消']},
        'proposal_delivery': {'status': 'delivered', 'message_id': 'prior-maintenance-reply'}}
    execution.update(continuation_control=deepcopy(prior_continuation),
        continuation_request_id='prior-continuation-request',
        natural_qa_rework_control=deepcopy(prior_natural), renewal_window_adoption='accepted',
        renewal_window_checkpoint={'status': 'admitted'})
    # The old interrupted usage seal remains historical evidence in this active window.
    ledger = json.loads(ledger_file.read_text())
    if change == 'expired':
        stream = carrier.stream('session/follow', {})
        snapshot = next(stream)
        stream.close()
        snapshot['records'].insert(1, {'type': 'event', 'event': {'type': 'assistant/attempt',
            'seq': 1, 'time': 1, 'data': {'turn': 1, 'step': 0, 'usage': {'outputTokens': 1}}}})
        for index, row in enumerate(snapshot['records']):
            row['event']['seq'] = index
        snapshot['cursor'] += 1
        ledger['state'].update(requests=3, admissions=['request-0', 'request-1', 'request-2'])
        ledger['renewWindow']['request_base'] = 2
        ledger['renewWindow']['original_state'].update(requests=2, admissions=['request-0', 'request-1'])
    ledger['renewWindow']['sealed_unknown_usage'] = {'ordinal': 1, 'event_sha256': 'f' * 64,
                                                   'prior_window_id': 'earlier-qa-window'}
    if change == 'expired':
        started = int(time.time() * 1000) - 1_801_000
        for state in (ledger['state'], ledger['renewWindow']['original_state'], ledger['renewWindow']):
            state.update(started_at_ms=started, stop_reason='shared-work-wall-budget')
    execution['renewal_window_checkpoint'].update(
        previous_state=deepcopy(ledger['renewWindow']['original_state']),
        sealed_unknown_usage=deepcopy(ledger['renewWindow']['sealed_unknown_usage']))
    if change == 'seal_changed':
        ledger['renewWindow']['sealed_unknown_usage']['event_sha256'] = '0' * 64
    ledger_file.write_text(json.dumps(ledger))
    context = {**context, 'message_id': 'new-completed-qa-feedback'}
    if change == 'pending':
        execution['state'] = 'continuation_pending'
        execution['continuation_control']['status'] = 'admitted'
    elif change == 'input_intent':
        execution['state'] = 'outcome_unknown'
        execution['continuation_control']['status'] = 'input_intent'
    elif change == 'foreign_source':
        context['chat_id'] = 'foreign-synthetic-chat'
    elif change == 'original_message':
        context['message_id'] = prior_natural['message_id']
    elif change == 'historical_duplicate':
        previous = {**prior_natural, 'message_id': 'archived-qa-message'}
        execution['continuation_history'] = [{**prior_continuation,
            'request_id': 'earlier-continuation-request', 'message_id': 'earlier-qa-input',
            'natural_qa_rework_control': previous}]
        execution['control_message_ids'].append(previous['message_id'])
        context['message_id'] = previous['message_id']
    record_before = deepcopy(record)
    ledger_before = ledger_file.read_bytes()
    feedback = 'Correct the next synthetic browser QA problem and report the actual native test call IDs.'
    if change not in {'valid', 'expired'}:
        with pytest.raises(ManagementError):
            admit_shared_natural_qa_rework(intake, record, context, feedback, None, intake.generation)
        assert record == record_before and intake.saved == []
    else:
        result = admit_shared_natural_qa_rework(intake, record, context, feedback, None, intake.generation)
        if change == 'expired':
            assert result['status'] == 'renewal_required' and result['work_id'] == record['id']
            assert result['proposal']['limits'] == {'max_wall_seconds': 1800, 'max_model_requests': 80}
            assert execution['natural_qa_rework_control']['feedback'] == feedback
            assert execution['state'] == 'awaiting_acceptance' and intake.card['status'] == 'blocked'
            assert execution['continuation_control'] == prior_continuation
        else:
            assert result == {'status': 'accepted', 'work_id': record['id']}
            assert execution['state'] == 'continuation_pending' and intake.card['status'] == 'ready'
            assert execution['continuation_control']['kind'] == 'qa_rework'
            assert execution['continuation_control']['message_id'] == context['message_id']
            assert execution['continuation_control']['qa_feedback'] == feedback
        assert execution['continuation_history'][-1] == {**prior_continuation,
            'request_id': 'prior-continuation-request', 'natural_qa_rework_control': prior_natural}
        with pytest.raises(ManagementError):
            admit_shared_natural_qa_rework(intake, record, context, feedback, None, intake.generation)
    if change == 'expired':
        from ghost_hermes_pm.shared_repository import record_shared_natural_qa_proposal
        history = deepcopy(execution['continuation_history'])
        record_shared_natural_qa_proposal(intake, record, result['proposal']['id'], {
            'status': 'delivered', 'message_id': 'new-expired-window-proposal',
            'chat_id': context['chat_id'], 'profile_id': context['profile_id']})
        reply_context = {**context, 'message_id': 'new-expired-window-approval',
            'reply_to_message_id': 'new-expired-window-proposal'}
        approved = admit_shared_natural_qa_rework(intake, record, reply_context,
            '同意', None, intake.generation)
        assert approved['status'] == 'maintenance_required', approved
        assert execution['natural_qa_rework_control']['status'] == 'maintenance_required'
        assert execution['continuation_history'] == history
        assert execution['renewal_window_checkpoint']['status'] == 'prepared'
        assert Path(execution['renewal_window_checkpoint']['ledger_backup']).read_bytes() == ledger_before
        assert ledger_file.read_bytes() == ledger_before
        record_shared_natural_qa_proposal(intake, record, approved['proposal']['id'], {
            'status': 'delivered', 'message_id': 'new-expired-maintenance-proposal',
            'chat_id': context['chat_id'], 'profile_id': context['profile_id']})
        carrier.native_identity = {'kind': 'shared_native', 'lease': 'lease-new', 'pid': 14, 'created_at': 2}
        Path(intake.config['desktop_receipt']).write_text(json.dumps({'lease': 'lease-new'}))
        intake.config['host_lease'] = 'lease-new'
        maintenance_context = {**context, 'message_id': 'new-expired-maintenance-complete',
            'reply_to_message_id': 'new-expired-maintenance-proposal'}
        adopted = admit_shared_natural_qa_rework(intake, record, maintenance_context,
            '维护完成', None, intake.generation)
        assert adopted == {'status': 'accepted', 'work_id': record['id']}
        renewed = json.loads(ledger_file.read_text())
        old = json.loads(ledger_before)
        assert renewed['session_id'] == execution['session_id'] == old['session_id']
        assert renewed['renewWindow']['id'] != old['renewWindow']['id']
        assert renewed['renewWindow']['request_base'] == renewed['state']['requests'] == old['state']['requests']
        assert renewed['renewWindow']['original_state'] == old['state']
        assert {key: value for key, value in renewed['state'].items() if key != 'binding_sha256'} == {
            key: value for key, value in old['state'].items() if key != 'binding_sha256'}
        assert all(renewed['renewWindow'][key] is None for key in ('started_at_ms', 'hook_marker', 'stop_reason'))
        assert execution['continuation_control']['kind'] == 'qa_rework'
        assert execution['continuation_control']['status'] == 'admitted'
        assert execution['continuation_control']['qa_feedback'] == feedback
        assert execution['continuation_history'][:len(history)] == history
    else:
        assert ledger_file.read_bytes() == ledger_before
    assert carrier.prompts == [] and request == {}


@pytest.mark.parametrize('limits', [{}, {'max_wall_seconds': 3600}, {'max_model_requests': 100}])
def test_natural_qa_preserves_original_optional_limits_without_a_renewal(tmp_path, monkeypatch, limits):
    from ghost_hermes_pm.shared_repository import admit_shared_natural_qa_rework
    record, context, intake, carrier, ledger_file, _source, request = _natural_qa_fixture(tmp_path, monkeypatch)
    row = json.loads(ledger_file.read_text())
    row.pop('renewWindow')
    row['budget'] = limits
    ledger_file.write_text(json.dumps(row))
    intake.config['budget'] = limits
    reference = Path(record['target']['execution_ref'])
    reference.write_text(json.dumps(intake.config))
    record['dsh_execution']['reference_sha256'] = hashlib.sha256(reference.read_bytes()).hexdigest()
    before = ledger_file.read_bytes()
    result = admit_shared_natural_qa_rework(intake, record, context, 'Verify the synthetic remaining work.', None, intake.generation)
    assert result['status'] == 'accepted' and intake.card['status'] == 'ready'
    assert record['dsh_execution']['continuation_control']['kind'] == 'qa_rework'
    assert ledger_file.read_bytes() == before and not carrier.prompts and not request


def test_sealed_unknown_usage_counts_attempt_before_interrupted_message():
    from ghost_hermes_pm.shared_repository import _sealed_unknown_usage
    events = [
        {'type': 'assistant/attempt', 'seq': 0, 'data': {'turn': 1, 'step': 0, 'usage': {'outputTokens': 1}}},
        {'type': 'assistant/message', 'seq': 1, 'data': {'turn': 1, 'step': 1, 'message': {'content': []}}},
        {'type': 'step/end', 'seq': 2, 'data': {'turn': 1, 'step': 1}},
        {'type': 'turn/end', 'seq': 3, 'data': {'turn': 1,
            'reason': {'kind': 'aborted', 'reason': {'kind': 'hook', 'reason': 'shared-work-wall-budget'}}}},
    ]
    seal = _sealed_unknown_usage(events, {'id': 'prior', 'request_base': 2,
        'original_state': {'stop_reason': 'shared-work-wall-budget'}})
    assert seal['ordinal'] == 2 and seal['prior_window_id'] == 'prior'


def test_renew_window_hook_metadata_is_strict_and_controlled():
    from ghost_hermes_pm.shared_repository import _validate_renew_window
    from ghost_hermes_pm.manager import ManagementError
    window = {'id': 'window', 'message_id': 'message', 'host_lease': 'lease', 'request_base': 2,
        'original_host_lease': 'old-lease', 'started_at_ms': None, 'stop_reason': None,
        'hook_marker': None, 'original_state': {'started_at_ms': None, 'requests': 2,
            'reported_tokens': 0, 'admissions': ['a', 'b'], 'stop_reason': None,
            'binding_sha256': 'a' * 64, 'observed_at_ms': 1, 'model_context_window': 100,
            'effective_max_output_tokens': 2048,
            'stop_cleanup': {'kind': 'terminal', 'job_ids': ['job']}}}
    _validate_renew_window(window)
    invalid = dict(window, original_state={**window['original_state'], 'unexpected': True})
    with pytest.raises(ManagementError):
        _validate_renew_window(invalid)
    with pytest.raises(ManagementError):
        _validate_renew_window(dict(window, request_base=0))
    for value in ('2048', 0, -1, True):
        with pytest.raises(ManagementError):
            _validate_renew_window(dict(window, original_state={**window['original_state'],
                'effective_max_output_tokens': value}))


@pytest.mark.parametrize('prior_qa_window', [False, True])
def test_natural_qa_rework_expired_window_renews_once_and_keeps_sealed_usage(tmp_path, monkeypatch, prior_qa_window):
    from ghost_hermes_pm.shared_repository import admit_shared_natural_qa_rework
    record, context, intake, carrier, ledger_file, _source, _request = _natural_qa_fixture(tmp_path, monkeypatch, expired=True)
    record['superior_profile_id'] = 'fixture-superior'
    context['profile_id'] = 'fixture-superior'
    if prior_qa_window:
        from ghost_hermes_pm.shared_repository import prepare_shared_window_renewal
        prepare_shared_window_renewal(intake, record, 'previous-window-message')
        record['dsh_execution']['renewal_window_checkpoint']['status'] = 'admitted'
        record['dsh_execution']['renewal_window_adoption'] = 'accepted'
        record['dsh_execution']['continuation_control'] = {'status': 'accepted', 'dispatch_status': 'accepted'}
    required = admit_shared_natural_qa_rework(intake, record, context, 'Re-run the actual synthetic QA test.', None, intake.generation)
    assert required['status'] == 'renewal_required'
    assert required['proposal']['limits'] == {'max_wall_seconds': 1800, 'max_model_requests': 80}
    before = ledger_file.read_bytes()
    from ghost_hermes_pm.shared_repository import record_shared_natural_qa_proposal
    with pytest.raises(ManagementError):
        record_shared_natural_qa_proposal(intake, record, required['proposal']['id'],
            {'status': 'delivered', 'message_id': 'wrong-profile-proposal', 'chat_id': context['chat_id'],
             'profile_id': record['profile_id']})
    assert record['dsh_execution']['natural_qa_rework_control']['proposal_delivery']['status'] == 'intent'
    record_shared_natural_qa_proposal(intake, record, required['proposal']['id'],
        {'status': 'delivered', 'message_id': 'proposal-window', 'chat_id': context['chat_id'], 'profile_id': context['profile_id']})
    context = {**context, 'message_id': 'natural-qa-message-maintenance', 'reply_to_message_id': 'proposal-window'}
    prepared = admit_shared_natural_qa_rework(intake, record, context, '同意', None, intake.generation)
    assert prepared['status'] == 'maintenance_required'
    assert record['dsh_execution'].get('renewal_window_checkpoint'), record['dsh_execution']
    if prior_qa_window:
        history = record['dsh_execution']['renewal_window_checkpoint_history']
        assert history[-1]['checkpoint']['message_id'] == 'previous-window-message'
        assert history[-1]['checkpoint']['status'] == 'admitted' and history[-1]['adoption'] == 'accepted'
    assert ledger_file.read_bytes() == before and not carrier.prompts
    record_shared_natural_qa_proposal(intake, record, prepared['proposal']['id'],
        {'status': 'delivered', 'message_id': 'proposal-maintenance', 'chat_id': context['chat_id'], 'profile_id': context['profile_id']})
    carrier.native_identity = {'kind': 'shared_native', 'lease': 'lease-new', 'pid': 14, 'created_at': 2}
    Path(intake.config['desktop_receipt']).write_text(json.dumps({'lease': 'lease-new'}))
    context = {**context, 'message_id': 'natural-qa-message-adopt', 'reply_to_message_id': 'proposal-maintenance'}
    intake.config['host_lease'] = 'lease-new'
    result = admit_shared_natural_qa_rework(intake, record, context, '维护完成', None, intake.generation)
    assert result['status'] == 'accepted'
    budget = json.loads(ledger_file.read_text())
    assert budget['state']['requests'] == 2 and budget['renewWindow']['request_base'] == 2
    assert budget['renewWindow']['started_at_ms'] is None
    assert budget['renewWindow']['host_lease'] == 'lease-new'
    assert budget['renewWindow']['original_host_lease'] == 'lease-fixture'
    assert budget['renewWindow']['hook_marker'] is None
    triplet = [
        {'type': 'assistant/message', 'seq': 1, 'time': 1,
         'data': {'turn': 1, 'step': 1, 'message': {'id': 'interrupted', 'role': 'assistant', 'content': []}}},
        {'type': 'step/end', 'seq': 2, 'time': 1, 'data': {'turn': 1, 'step': 1}},
        {'type': 'turn/end', 'seq': 3, 'time': 1,
         'data': {'turn': 1, 'reason': {'kind': 'aborted', 'reason': {'kind': 'hook', 'reason': 'shared-work-wall-budget'}}}},
    ]
    expected_seal = hashlib.sha256(json.dumps(triplet, ensure_ascii=False, separators=(',', ':')).encode()).hexdigest()
    assert budget['renewWindow']['sealed_unknown_usage'] == {
        'ordinal': 1, 'event_sha256': expected_seal,
        'prior_window_id': 'old-window'}
    assert len(carrier.prompts) == 0 and record['dsh_execution']['state'] == 'continuation_pending'
    assert record['dsh_execution']['continuation_control']['kind'] == 'qa_rework'


@pytest.mark.parametrize('change', [None, 'wrong_parent', 'wrong_profile', 'scope_changed', 'old_instance_live', 'admission_retry'])
def test_natural_qa_maintenance_reply_refreshes_only_the_original_desktop_lease(tmp_path, monkeypatch, change):
    from ghost_hermes_pm import shared_repository, repository_execution
    record, context, intake, carrier, ledger_file, _source, _request = _natural_qa_fixture(tmp_path, monkeypatch, expired=True)
    reference = Path(record['target']['execution_ref'])
    receipt = tmp_path / 'desktop-receipt.json'
    receipt.write_text(json.dumps({'lease': 'lease-fixture'}))
    receipt.chmod(0o600)
    scope = json.loads(reference.read_text())
    scope['desktop_receipt'] = str(receipt)
    reference.write_text(json.dumps(scope))
    record['dsh_execution']['reference_sha256'] = hashlib.sha256(reference.read_bytes()).hexdigest()

    def configuration(path, value, **_kwargs):
        if value['host_lease'] != json.loads(receipt.read_text())['lease']:
            raise ManagementError('binding_conflict', 'Original Desktop lease differs.')
        return {**value, 'reference_sha256': hashlib.sha256(path.read_bytes()).hexdigest()}

    monkeypatch.setattr(shared_repository, 'shared_configuration', configuration)
    monkeypatch.setattr(repository_execution, 'execution_configuration', lambda _target:
        (configuration(reference, json.loads(reference.read_text())), {}))
    proposal = shared_repository.admit_shared_natural_qa_rework(intake, record, context, 'Repair synthetic QA.', None, intake.generation)
    shared_repository.record_shared_natural_qa_proposal(intake, record, proposal['proposal']['id'],
        {'status': 'delivered', 'message_id': 'proposal-window', 'chat_id': context['chat_id'], 'profile_id': context['profile_id']})
    context = {**context, 'message_id': 'confirm-window', 'reply_to_message_id': 'proposal-window'}
    prepared = shared_repository.admit_shared_natural_qa_rework(intake, record, context, '同意', None, intake.generation)
    shared_repository.record_shared_natural_qa_proposal(intake, record, prepared['proposal']['id'],
        {'status': 'delivered', 'message_id': 'proposal-maintenance', 'chat_id': context['chat_id'], 'profile_id': context['profile_id']})
    before = reference.read_bytes()
    receipt.write_text(json.dumps({'lease': 'lease-new'}))
    carrier.native_identity = {'kind': 'shared_native', 'lease': 'lease-new', 'pid': 14, 'created_at': 2}
    monkeypatch.setattr(shared_repository, 'desktop_identity', lambda *_args: (carrier.native_identity, 'fixture-endpoint', 'fixture-login'))
    import psutil
    monkeypatch.setattr(psutil, 'pid_exists', lambda _pid: False)
    context = {**context, 'message_id': 'confirm-maintenance', 'reply_to_message_id': 'proposal-maintenance'}
    if change == 'wrong_parent':
        context['reply_to_message_id'] = 'unrelated-proposal'
    elif change == 'wrong_profile':
        context['profile_id'] = 'unrelated-profile'
    elif change == 'scope_changed':
        reference.write_text(json.dumps({**json.loads(before), 'model': 'other-model'}))
    elif change == 'old_instance_live':
        monkeypatch.setattr(psutil, 'pid_exists', lambda _pid: True)
        monkeypatch.setattr(psutil, 'Process', lambda _pid: SimpleNamespace(create_time=lambda: 1))
    if change and change != 'admission_retry':
        reference_before, ledger_before = reference.read_bytes(), ledger_file.read_bytes()
        with pytest.raises(ManagementError):
            shared_repository.admit_shared_natural_qa_rework(intake, record, context, '维护完成', None, intake.generation)
        assert reference.read_bytes() == reference_before and ledger_file.read_bytes() == ledger_before
        assert not carrier.prompts
        return
    if change == 'admission_retry':
        from ghost_hermes_pm import repository_acceptance
        original_source = repository_acceptance.repository_source_state
        monkeypatch.setattr(repository_acceptance, 'repository_source_state', lambda _target:
            (_ for _ in ()).throw(ManagementError('outcome_unknown', 'Synthetic temporary admission failure.')))
        with pytest.raises(ManagementError):
            shared_repository.admit_shared_natural_qa_rework(intake, record, context, '维护完成', None, intake.generation)
        assert json.loads(reference.read_text())['host_lease'] == 'lease-new'
        assert json.loads(ledger_file.read_text())['state']['requests'] == 2 and not carrier.prompts
        monkeypatch.setattr(repository_acceptance, 'repository_source_state', original_source)
        context = {**context, 'message_id': 'retry-confirm-maintenance'}
    result = shared_repository.admit_shared_natural_qa_rework(intake, record, context, '维护完成', None, intake.generation)
    assert result['status'] == 'accepted'
    assert json.loads(reference.read_text()) == {**json.loads(before), 'host_lease': 'lease-new'}
    assert record['dsh_execution']['reference_sha256'] == hashlib.sha256(reference.read_bytes()).hexdigest()
    backup = reference.with_name(reference.name + '.before-maintenance-' + hashlib.sha256(before).hexdigest())
    assert backup.read_bytes() == before
    assert json.loads(ledger_file.read_text())['state']['requests'] == 2 and not carrier.prompts


@pytest.mark.parametrize('change', [None, 'started', 'ledger_changed', 'snapshot_changed', 'workspace_changed', 'worker_failed', 'repeated', 'previous_accepted', 'previous_intent', 'historical_duplicate'])
def test_natural_qa_requeues_only_the_same_unstarted_continuation(tmp_path, monkeypatch, change):
    from ghost_hermes_pm import shared_repository, repository_control
    record, context, intake, carrier, ledger_file, source, _request = _natural_qa_fixture(tmp_path, monkeypatch)
    execution = record['dsh_execution']
    configuration = dict(intake.config, workspace_id=execution['workspace_id'])
    stream = carrier.stream('session/follow', {})
    snapshot = next(stream)
    stream.close()
    ledger = json.loads(ledger_file.read_text())
    ledger['renewWindow'].update(request_base=ledger['state']['requests'], original_state=dict(ledger['state']),
        started_at_ms=None, stop_reason=None, hook_marker=None)
    ledger_file.write_text(json.dumps(ledger))
    execution.update(state='outcome_unknown', renewal_window_adoption='accepted',
        supervisor_claim={'run_id': 2, 'identity': {'pid': 20}},
        renewal_window_checkpoint={'status': 'admitted', 'source': source,
            'previous_state': dict(ledger['state'])},
        continuation_request_id='original-qa-request',
        continuation_control={'kind': 'qa_rework', 'status': 'admitted', 'dispatch_status': 'accepted',
            'previous_claim': {'run_id': 1}, 'worker': {'run_id': 1, 'exit_code': 0},
            'qa_feedback': 'Original synthetic QA requirement.',
            'snapshot_sha256': hashlib.sha256(json.dumps(snapshot, sort_keys=True).encode()).hexdigest(),
            'ledger_sha256': hashlib.sha256(ledger_file.read_bytes()).hexdigest(),
            'workspace': {'workspace_id': execution['workspace_id'], 'session_ids': [execution['session_id']]}})
    worker = {'run_id': 2, 'identity': {'pid': 20}, 'exit_code': 0}
    monkeypatch.setattr(repository_control, '_worker_end', lambda *_args, **_kwargs: worker)
    if change == 'started':
        ledger['renewWindow']['started_at_ms'] = int(time.time()*1000)
        ledger_file.write_text(json.dumps(ledger))
    elif change == 'ledger_changed':
        ledger['state']['reported_tokens'] += 1
        ledger_file.write_text(json.dumps(ledger))
    elif change == 'snapshot_changed':
        execution['continuation_control']['snapshot_sha256'] = '0' * 64
    elif change == 'worker_failed':
        worker['exit_code'] = 1
    elif change == 'workspace_changed':
        execution['continuation_control']['workspace']['workspace_id'] = 'foreign-workspace'
    elif change == 'repeated':
        execution['qa_preinput_retry'] = {'status': 'accepted', 'message_id': context['message_id']}
    elif change in {'previous_accepted', 'previous_intent'}:
        execution['qa_preinput_retry'] = {'status': 'accepted' if change == 'previous_accepted' else 'intent',
            'message_id': 'previous-owner-request', 'previous_failure': {'code': 'outcome_unknown'}}
    previous_retry = deepcopy(execution.get('qa_preinput_retry'))
    if change == 'historical_duplicate':
        execution['qa_preinput_retry'] = {'status': 'accepted', 'message_id': 'latest-owner-request'}
        execution['qa_preinput_retry_history'] = [{'status': 'accepted', 'message_id': context['message_id']}]
    original_save = intake._save_execution
    def save_fields(saved_record, fields=None):
        for key in fields or []:
            saved_record['dsh_execution'][key]
        original_save(saved_record, fields)
    intake._save_execution = save_fields
    before = ledger_file.read_bytes()
    if change not in {None, 'previous_accepted'}:
        with pytest.raises(ManagementError):
            shared_repository.admit_shared_natural_qa_rework(intake, record, context, '继续原修复。', None, intake.generation)
        assert intake.card['status'] == 'blocked' and ledger_file.read_bytes() == before and not carrier.prompts
        return
    result = shared_repository.admit_shared_natural_qa_rework(intake, record, context, '继续原修复。', None, intake.generation)
    assert result == {'status': 'accepted', 'work_id': record['id']}
    assert execution['continuation_request_id'] == 'original-qa-request'
    assert execution['continuation_control']['qa_feedback'] == 'Original synthetic QA requirement.'
    assert execution['continuation_control']['previous_claim'] == execution['supervisor_claim']
    assert execution['continuation_control']['worker'] == worker
    assert execution['qa_preinput_retry']['previous_control']['previous_claim'] == {'run_id': 1}
    assert intake.card['status'] == 'ready' and ledger_file.read_bytes() == before and not carrier.prompts
    if previous_retry:
        assert execution['qa_preinput_retry_history'] == [previous_retry]
        execution['state'] = 'outcome_unknown'
        intake.card['status'] = 'blocked'
        with pytest.raises(ManagementError):
            shared_repository.admit_shared_natural_qa_rework(intake, record,
                {**context, 'message_id': previous_retry['message_id']}, '继续原修复。', None, intake.generation)
        with pytest.raises(ManagementError):
            shared_repository.admit_shared_natural_qa_rework(intake, record, context, '继续原修复。', None, intake.generation)
    assert ledger_file.read_bytes() == before and not carrier.prompts


def test_natural_qa_rework_expired_window_requires_maintenance_without_side_effects(tmp_path, monkeypatch):
    from ghost_hermes_pm.shared_repository import admit_shared_natural_qa_rework
    record, context, intake, carrier, ledger_file, _source, _request = _natural_qa_fixture(
        tmp_path, monkeypatch, expired=True, foreign_host=False)
    before = ledger_file.read_bytes()
    required = admit_shared_natural_qa_rework(intake, record, context, 'Wait for the approved native maintenance.',
        {'max_wall_seconds': 1800, 'max_model_requests': 80}, intake.generation)
    from ghost_hermes_pm.shared_repository import record_shared_natural_qa_proposal
    record_shared_natural_qa_proposal(intake, record, required['proposal']['id'],
        {'status': 'delivered', 'message_id': 'proposal-window', 'chat_id': context['chat_id'], 'profile_id': context['profile_id']})
    result = admit_shared_natural_qa_rework(intake, record,
        {**context, 'message_id': 'natural-qa-maintenance', 'reply_to_message_id': 'proposal-window'},
        '同意', None, intake.generation)
    assert result['status'] == 'maintenance_required'
    assert ledger_file.read_bytes() == before and not carrier.prompts
    assert record['dsh_execution']['natural_qa_rework_control']['status'] == 'maintenance_required'
    carrier.native_identity = record['dsh_execution']['native_identity']
    with pytest.raises(ManagementError):
        admit_shared_natural_qa_rework(intake, record, {**context, 'message_id': 'natural-qa-repeat'},
            'Wait for the approved native maintenance.', {'max_wall_seconds': 1800, 'max_model_requests': 80}, intake.generation)
    assert ledger_file.read_bytes() == before and not carrier.prompts


@pytest.mark.parametrize('case', ['source', 'jobs', 'identity', 'foreign_profile'])
def test_natural_qa_rework_rejects_changed_active_evidence_without_side_effects(tmp_path, monkeypatch, case):
    from ghost_hermes_pm import repository_acceptance
    from ghost_hermes_pm.shared_repository import admit_shared_natural_qa_rework
    record, context, intake, carrier, ledger_file, source, _request = _natural_qa_fixture(tmp_path, monkeypatch)
    before = ledger_file.read_bytes()
    if case == 'source':
        monkeypatch.setattr(repository_acceptance, 'repository_source_state',
            lambda _target: {**source, 'source_digest': 'f' * 64})
    elif case == 'jobs':
        carrier.owned_jobs = lambda: [{'id': 'foreign-job', 'owner': 'foreign-session', 'status': 'running'}]
    elif case == 'identity':
        carrier.native_identity = {'kind': 'shared_native', 'lease': 'foreign-lease', 'pid': 19, 'created_at': 3}
    else:
        record['superior_profile_id'] = 'fixture-superior'
        context['profile_id'] = 'foreign-profile'
    with pytest.raises(ManagementError):
        admit_shared_natural_qa_rework(intake, record, context, 'Fix synthetic evidence.', None, intake.generation)
    assert ledger_file.read_bytes() == before and not carrier.prompts and intake.card['status'] == 'blocked'


@pytest.mark.parametrize('case', ['wrong_sender', 'bot_sender', 'wrong_profile', 'wrong_chat', 'no_newline',
    'empty', 'too_long', 'sensitive', 'wrong_state', 'stop_requested', 'lifecycle_end', 'checkpoint_unknown',
    'continuation_pending', 'duplicate_intent', 'duplicate_unknown', 'duplicate_accepted',
    'new_message_after_intent', 'new_message_after_unknown', 'new_message_after_accepted',
    'retry_wrong_status', 'retry_wrong_dispatch', 'retry_wrong_request', 'retry_wrong_cursor', 'retry_history_present'])
def test_owner_context_public_rejections_never_replay_or_reach_native(tmp_path, monkeypatch, case):
    from types import SimpleNamespace as NS
    from ghost_hermes_pm.simple_development import RepositoryIntake
    from ghost_hermes_pm import shared_repository
    from ghost_hermes_pm.messages import OWNED_PLATFORM
    work_id = 'work-' + 'a' * 32
    anchor = {'app_id': 'fixture-app', 'transport_tenant_key': 'fixture-app-tenant',
        'tenant_key': 'fixture-owner-tenant', 'recipient_tenant_key': 'fixture-bot-tenant',
        'recipient_open_id': 'fixture-bot', 'chat_id': 'fixture-chat', 'sender_open_id': 'fixture-owner'}
    binding = dict(anchor, sender_tenant_key=anchor['tenant_key'], owner_open_id=anchor['sender_open_id'],
        profile_id='fixture-profile', native_profile='fixture-native', verification_ref='fixture-proof')
    intake = RepositoryIntake(None, {'enabled': True, 'verification_ref': 'fixture-proof', 'bindings': [binding]},
        {'github_account': 'fixture-account', 'github_config_dir': str(tmp_path)}, tmp_path / 'state')
    sent = []
    class Transport:
        async def send(self, message):
            sent.append(message)
    adapter = NS(registered_bots=[{'open_id': 'fixture-bot-sender', 'tenant_key': 'fixture-owner-tenant',
        'native_ids': ['fixture-bot-sender'], 'dispatch_profiles': ['fixture-profile']}])
    intake.attach_transport(adapter, Transport())
    execution = {'mode': 'shared_native', 'state': 'outcome_unknown', 'first_input': 'accepted',
        'last_failure': {'code': 'outcome_unknown'}, 'renewal_window_checkpoint': {'status': 'admitted'},
        'renewal_window_adoption': 'accepted', 'continuation_control': {'kind': 'renew_window', 'status': 'accepted',
            'dispatch_status': 'accepted', 'snapshot_sha256': 'fixture-snapshot', 'ledger_sha256': 'fixture-ledger'}}
    record = {'id': work_id, 'profile_id': 'fixture-profile', 'source_anchor': dict(anchor),
        'target': {'profile_id': 'fixture-profile'}, 'dsh_execution': execution}
    command = '补充 ' + work_id + '\nVerify synthetic scope.'
    sender = 'fixture-owner'
    is_bot = False
    if case == 'wrong_sender':
        sender = 'fixture-stranger'
    elif case == 'bot_sender':
        sender, is_bot = 'fixture-bot-sender', True
    elif case == 'wrong_profile':
        record['profile_id'] = 'fixture-other-profile'
    elif case == 'wrong_chat':
        record['source_anchor']['chat_id'] = 'fixture-other-chat'
    elif case == 'no_newline':
        command = '补充 ' + work_id
    elif case == 'empty':
        command = '补充 ' + work_id + '\n   '
    elif case == 'too_long':
        command = '补充 ' + work_id + '\n' + 'x' * 4001
    elif case == 'sensitive':
        command = '补充 ' + work_id + '\napi_key=sk-abcdefghijklmnopqrstuvwxyz'
    elif case == 'wrong_state':
        execution['state'] = 'running'
    elif case in {'stop_requested', 'lifecycle_end'}:
        execution[case] = True
    elif case == 'checkpoint_unknown':
        execution['renewal_window_checkpoint']['status'] = 'unknown'
    elif case == 'continuation_pending':
        execution['continuation_control']['status'] = 'admitted'
    elif case.startswith(('duplicate_', 'new_message_')):
        status = case.rsplit('_', 1)[1]
        execution['owner_context_control'] = {'message_id': 'fixture-message', 'request_id': 'fixture-request', 'status': status}
        if case.startswith('duplicate_'):
            execution['control_message_ids'] = ['fixture-message']
    elif case.startswith('retry_'):
        execution['continuation_control']['cursor'] = 0
        first = {'message_id': 'fixture-first-message', 'request_id': 'owner-context-' + hashlib.sha256(
            (work_id + ':fixture-first-message').encode()).hexdigest(), 'status': 'accepted', 'dispatch_status': 'accepted',
            'snapshot_sha256': 'fixture-owner-snapshot', 'cursor': 6, 'failed_turn': 1, 'turn': 2, 'turn_start_seq': 8}
        if case == 'retry_wrong_status':
            first['status'] = 'unknown'
        elif case == 'retry_wrong_dispatch':
            first['dispatch_status'] = 'intent'
        elif case == 'retry_wrong_request':
            first['request_id'] = 'foreign-request'
        elif case == 'retry_wrong_cursor':
            first['cursor'] = 7
        elif case == 'retry_history_present':
            execution['owner_context_history'] = [deepcopy(first)]
        execution['owner_context_control'] = first
    intake._save(record)
    mid = 'fixture-new-message' if case.startswith('new_message_') else 'fixture-message'
    message = NS(chat_type='group', message_type='text', chat_id=anchor['chat_id'], message_id=mid,
        content=json.dumps({'text': '@fixture ' + command}), mentions=[NS(key='@fixture', mentioned_type='bot',
            tenant_key=anchor['recipient_tenant_key'], id=NS(open_id=anchor['recipient_open_id']))])
    event = NS(message_id=mid, source=NS(platform=OWNED_PLATFORM, user_id=sender, chat_id=anchor['chat_id'],
        is_bot=is_bot, message_id=mid), raw_message=NS(header=NS(event_type='im.message.receive_v1',
        app_id=anchor['app_id'], tenant_key=anchor['transport_tenant_key']), event=NS(message=message,
        sender=NS(sender_type='bot' if is_bot else 'user', tenant_key=anchor['tenant_key'], sender_id=NS(open_id=sender)))))
    def forbidden_native(*args, **kwargs):
        pytest.fail('A rejected or replayed public message must not contact Native.')
    monkeypatch.setattr(shared_repository, 'SharedDesktopTransport', forbidden_native)
    prepared = intake.prepare(event, adapter)
    if case == 'wrong_sender':
        assert prepared is None
        return
    assert prepared is not None and prepared.action == 'control'
    result = asyncio.run(intake.process_prepared(prepared))
    assert result['status'] in {'rejected', 'unverified', 'duplicate'}
    if case.startswith('retry_'):
        assert result['status'] == 'unverified' and result['code'] == 'outcome_unknown'
    assert sent and intake.snapshot()['work'][0]['dsh_execution'].get('owner_context_control') == execution.get('owner_context_control')


@pytest.mark.parametrize('change', ['noncanonical', 'in_workspace', 'unsafe_permissions', 'not_executable', 'wrong_python', 'wrong_node'])
def test_shared_runtime_path_version_and_permissions_refuse_unsafe_entries(tmp_path, change):
    from ghost_hermes_pm.shared_repository import _trusted_binary
    workspace = tmp_path / 'repository'
    workspace.mkdir()
    binary = tmp_path / 'approved-runtime'
    binary.write_text('#!/bin/sh\nprintf "Python 3.10.0\\n"\n')
    binary.chmod(0o700)
    kind = 'python'
    if change == 'noncanonical':
        alias = tmp_path / 'alias-runtime'
        alias.symlink_to(Path(sys.executable).resolve())
        binary = alias
    elif change == 'in_workspace':
        binary = workspace / 'runtime'
        binary.write_text('#!/bin/sh\nprintf "Python 3.12.0\\n"\n')
        binary.chmod(0o700)
    elif change == 'unsafe_permissions':
        binary.chmod(0o777)
    elif change == 'not_executable':
        binary.chmod(0o600)
    elif change == 'wrong_node':
        kind = 'node'
        binary.write_text('#!/bin/sh\nprintf "v18.0.0\\n"\n')
    with pytest.raises(ManagementError):
        _trusted_binary(str(binary), str(workspace), kind)


def test_shared_runtime_requires_actual_isolated_entry_success(tmp_path):
    from ghost_hermes_pm.shared_repository import _runtime_entries
    workspace = tmp_path / 'repository'
    workspace.mkdir()
    binary = tmp_path / 'approved-runtime'
    binary.write_text('#!/bin/sh\nif [ "$1" = "--version" ]; then printf "Python 3.12.0\\n"; else exit 9; fi\n')
    binary.chmod(0o700)
    root = Path(__file__).resolve().parents[1]
    configuration = {'source_snapshot': {'python': str(binary), 'reader': str(root / 'ghost_hermes_pm/shared_source_reader.py')},
        'verification': {'python': str(Path(sys.executable).resolve()), 'node': str(Path(shutil.which('node')).resolve()),
            'runner': str(root / 'ghost_hermes_pm/repository_test_runner.py')}, 'node_bin': str(Path(shutil.which('node')).resolve())}
    with pytest.raises(ManagementError, match='read-only runtime entry failed'):
        _runtime_entries(configuration, str(workspace))


def test_original_workspace_gateway_reuses_path_and_preserves_existing_session_across_reload(tmp_path):
    sdk = Path(os.environ['DSH_TEST_SDK_ROOT']).resolve(strict=True)
    before = source_snapshot(sdk)
    result = subprocess.run([shutil.which('node'), str(Path(__file__).with_name('shared_workspace_native_runner.mjs')),
        str(tmp_path)], capture_output=True, text=True, timeout=20)
    assert result.returncode == 0, result.stdout + result.stderr
    evidence = json.loads(result.stdout)
    (tmp_path / 'workspace-contract.json').write_text(json.dumps(evidence))
    assert evidence['created']['created'] is True
    assert evidence['repeated']['created'] is evidence['restored']['created'] is False
    assert evidence['restored']['workspace']['sessionIds'] == [evidence['original']['sessionId']]
    assert evidence['cwd_xor_refused'] and evidence['model_requests'] == 0
    assert source_snapshot(sdk) == before


@pytest.mark.parametrize('initial', ['danger-full-access/never', 'workspace-write/ask', 'read-only/ask'])
def test_shared_permission_confirmation_uses_original_command_gateway(tmp_path, initial):
    sdk = Path(os.environ['DSH_TEST_SDK_ROOT']).resolve(strict=True)
    before = source_snapshot(sdk)
    result = subprocess.run([shutil.which('node'), str(Path(__file__).with_name('shared_permission_native_runner.mjs')),
                             str(tmp_path), initial], capture_output=True, text=True, timeout=20)
    assert result.returncode == 0, result.stdout + result.stderr
    evidence = json.loads(result.stdout)
    artifact = tmp_path / 'native-permission-contract.json'
    artifact.write_text(json.dumps(evidence))
    artifact.chmod(0o600)
    from ghost_hermes_pm.shared_repository import confirm_shared_permission
    proof = confirm_shared_permission(evidence['response'], evidence['events'], evidence['projections'])
    assert proof['command_id'] == evidence['response']['commandId']
    assert confirm_shared_permission(None, evidence['events'], evidence['projections'], reconcile=True) == proof
    for mutation in ('wrong_command', 'missing_done', 'changed_policy', 'unexpected_work', 'foreign_notice', 'queued_input', 'late_cursor'):
        candidate = deepcopy(evidence)
        kwargs = {}
        if mutation == 'wrong_command':
            candidate['response']['commandId'] = 'fixture-foreign-command'
        elif mutation == 'missing_done':
            candidate['events'] = [event for event in candidate['events'] if event['type'] != 'command/done']
        elif mutation == 'changed_policy':
            candidate['events'].append({'type': 'approval/policy', 'seq': len(candidate['events']), 'data': {'policy': 'never'}})
        elif mutation == 'unexpected_work':
            candidate['events'].append({'type': 'turn/start', 'seq': len(candidate['events']), 'data': {'turn': 1}})
        elif mutation == 'foreign_notice':
            notices = [event for event in candidate['events'] if event['type'] == 'agent/inbox/spliced']
            if not notices:
                continue
            notices[0]['data']['inserted'][0]['source'] = {'kind': 'user'}
        elif mutation == 'queued_input':
            candidate['projections']['inbox']['next-turn'].append({'role': 'user', 'content': []})
        else:
            kwargs['before_cursor'] = proof['run_seq']
        with pytest.raises(ManagementError):
            confirm_shared_permission(candidate['response'], candidate['events'], candidate['projections'], **kwargs)
    assert source_snapshot(sdk) == before


def test_permission_initializer_exec_change_does_not_end_the_old_process():
    from types import SimpleNamespace
    from ghost_hermes_pm.trusted_controller import controller_identity
    from ghost_hermes_pm.shared_repository import require_permission_initializer_end
    identity = controller_identity()
    original = {**identity, 'executable': '/synthetic/previous-executable'}
    run = SimpleNamespace(id=1, task_id='fixture-card', status='blocked', ended_at=time.time(), worker_pid=identity['pid'])
    with pytest.raises(ManagementError):
        require_permission_initializer_end({'run_id': 1, 'identity': original}, run, 'fixture-card')


@pytest.mark.parametrize('change', ['unfinished', 'unknown', 'changed_birth', 'gone'])
def test_permission_initializer_requires_original_terminal_birth(change, monkeypatch):
    import psutil
    from types import SimpleNamespace
    from ghost_hermes_pm.trusted_controller import controller_identity
    from ghost_hermes_pm.shared_repository import require_permission_initializer_end
    identity = controller_identity()
    if change == 'gone':
        child = subprocess.Popen([sys.executable, '-c', 'import sys; sys.stdin.read()'], stdin=subprocess.PIPE)
        identity = controller_identity(child.pid)
        child.communicate(b'')
        assert child.returncode == 0
    run = SimpleNamespace(id=1, task_id='fixture-card', status='blocked', ended_at=time.time(), worker_pid=identity['pid'])
    if change == 'unfinished':
        run.ended_at = None
    elif change == 'unknown':
        def unavailable(pid):
            raise psutil.AccessDenied(pid)
        monkeypatch.setattr(psutil, 'Process', unavailable)
    elif change == 'changed_birth':
        identity = {**identity, 'start_sec': str(int(identity['start_sec']) - 1)}
    previous = {'run_id': 1, 'identity': identity}
    if change in {'unfinished', 'unknown'}:
        with pytest.raises(ManagementError):
            require_permission_initializer_end(previous, run, 'fixture-card')
    else:
        require_permission_initializer_end(previous, run, 'fixture-card')


@pytest.mark.parametrize('job_status', ['completed', 'killed', 'failed'])
@pytest.mark.parametrize('historical', [False, True])
def test_source_chain_attributes_terminal_promoted_job_write(job_status, historical):
    from ghost_hermes_pm.shared_repository import _verified_source_chain
    execution = {'generation': 'fixture-generation', 'session_id': 'fixture-session'}
    baseline = {'head': None, 'workspace_status': '', 'source_digest': 'fixture-baseline'}
    promoted_after = {'head': None, 'workspace_status': '?? fixture-output/', 'source_digest': 'fixture-promoted'}
    current = {'head': None, 'workspace_status': '?? fixture-output/\n?? fixture-result', 'source_digest': 'fixture-current'}
    promoted = {**execution, 'name': 'bash', 'kind': 'promoted', 'execution_admission': 'admitted',
        'is_error': False,
        'job_id': 'fixture-promoted-job', 'native_session_seq': 3,
        'source_before': baseline, 'source_after': promoted_after}
    foreground = {**execution, 'name': 'bash', 'kind': 'foreground', 'execution_admission': 'admitted',
        'is_error': False, 'exit_code': 0, 'timed_out': False, 'aborted': False, 'native_session_seq': 7,
        'source_before': promoted_after, 'source_after': current}
    owned_jobs = [{'id': 'fixture-promoted-job', 'owner': 'fixture-session', 'status': job_status}]
    job_receipts = []
    if historical:
        promoted.update(schema=1, session_created_at=1, service_ref='fixture-service', host_lease='fixture-lease',
                        command_sha256='c' * 64)
        registered = {key: promoted[key] for key in ('schema', 'generation', 'session_id', 'session_created_at',
                      'service_ref', 'host_lease', 'command_sha256', 'job_id')}
        registered.update(type='registered', status='running', kind='bash', owner=execution['session_id'], started_at_ms=10)
        job_receipts = [registered, {**registered, 'type': 'settled', 'status': job_status,
            'finished_at_ms': 20, 'cause': 'kill' if job_status == 'killed' else 'producer', 'awaited': False}]
        owned_jobs = []
    assert _verified_source_chain(execution, baseline, [promoted, foreground], current, owned_jobs,
                                  job_receipts=job_receipts)


@pytest.mark.parametrize('change', [
    'running', 'stopping', 'unknown', 'missing', 'duplicate', 'wrong_owner', 'missing_owner',
    'missing_job_id', 'wrong_job_id', 'exit_code', 'timed_out', 'aborted',
    'null_exit_code', 'null_timed_out', 'null_aborted', 'generation', 'session_id',
    'admission', 'is_error', 'name', 'kind', 'sequence', 'before', 'after', 'current',
    'native_missing', 'native_duplicate', 'native_wrong_owner', 'native_wrong_command',
    'native_wrong_time', 'native_teardown', 'native_running', 'native_partial_roster',
])
def test_source_chain_rejects_unverified_promoted_job_write(change):
    from ghost_hermes_pm.shared_repository import _verified_source_chain
    execution = {'generation': 'fixture-generation', 'session_id': 'fixture-session'}
    baseline = {'head': None, 'workspace_status': '', 'source_digest': 'fixture-baseline'}
    current = {'head': None, 'workspace_status': '?? fixture-output', 'source_digest': 'fixture-current'}
    receipt = {**execution, 'name': 'bash', 'kind': 'promoted', 'execution_admission': 'admitted',
        'is_error': False,
        'job_id': 'fixture-promoted-job', 'native_session_seq': 3,
        'source_before': baseline, 'source_after': current}
    job = {'id': 'fixture-promoted-job', 'owner': 'fixture-session', 'status': 'completed'}
    jobs = [job]
    job_receipts = []
    if change.startswith('native_'):
        receipt.update(schema=1, session_created_at=1, service_ref='fixture-service', host_lease='fixture-lease',
                       command_sha256='c' * 64)
        registered = {key: receipt[key] for key in ('schema', 'generation', 'session_id', 'session_created_at',
                      'service_ref', 'host_lease', 'command_sha256', 'job_id')}
        registered.update(type='registered', status='running', kind='bash', owner=execution['session_id'], started_at_ms=10)
        settled = {**registered, 'type': 'settled', 'status': 'completed', 'finished_at_ms': 20,
                   'cause': 'producer', 'awaited': False}
        job_receipts, jobs = [registered, settled], []
        if change == 'native_missing':
            job_receipts.pop()
        elif change == 'native_duplicate':
            job_receipts.append(dict(settled))
        elif change == 'native_wrong_owner':
            settled['owner'] = 'fixture-foreign-session'
        elif change == 'native_wrong_command':
            settled['command_sha256'] = 'd' * 64
        elif change == 'native_wrong_time':
            settled['finished_at_ms'] = 9
        elif change == 'native_teardown':
            settled['cause'] = 'teardown'
        elif change == 'native_running':
            settled['status'] = 'running'
        else:
            jobs = [{'id': 'fixture-other-job', 'owner': 'fixture-session', 'status': 'completed'}]
    elif change in {'running', 'stopping', 'unknown'}:
        job['status'] = change
    elif change == 'missing':
        jobs = []
    elif change == 'duplicate':
        jobs.append(dict(job))
    elif change == 'wrong_owner':
        job['owner'] = 'fixture-foreign-session'
    elif change == 'missing_owner':
        job.pop('owner')
    elif change.startswith('missing_'):
        receipt.pop(change.removeprefix('missing_'))
    elif change.startswith('null_'):
        receipt[change.removeprefix('null_')] = None
    elif change == 'wrong_job_id':
        receipt['job_id'] = 'fixture-other-job'
    elif change == 'exit_code':
        receipt['exit_code'] = 0
    elif change in {'timed_out', 'aborted'}:
        receipt[change] = False
    elif change in {'generation', 'session_id', 'name', 'kind'}:
        receipt[change] = 'fixture-other'
    elif change == 'admission':
        receipt['execution_admission'] = 'rejected'
    elif change == 'is_error':
        receipt['is_error'] = True
    elif change == 'sequence':
        receipt['native_session_seq'] = 0
    elif change == 'before':
        receipt['source_before'] = {**baseline, 'source_digest': 'fixture-other-source'}
    elif change == 'after':
        receipt['source_after'] = None
    elif change == 'current':
        current = {**current, 'source_digest': 'fixture-other-current'}
    assert not _verified_source_chain(execution, baseline, [receipt], current, jobs, job_receipts=job_receipts)


@pytest.mark.parametrize('exit_code', [0, 1])
def test_source_chain_foreground_attribution_keeps_existing_exit_semantics(exit_code):
    from ghost_hermes_pm.shared_repository import _verified_source_chain
    execution = {'generation': 'fixture-generation', 'session_id': 'fixture-session'}
    baseline = {'head': None, 'workspace_status': '', 'source_digest': 'fixture-baseline'}
    current = {'head': None, 'workspace_status': '?? fixture-output', 'source_digest': 'fixture-current'}
    receipt = {**execution, 'name': 'bash', 'kind': 'foreground', 'execution_admission': 'admitted',
        'is_error': False, 'exit_code': exit_code, 'timed_out': False, 'aborted': False,
        'native_session_seq': 3, 'source_before': baseline, 'source_after': current}
    assert _verified_source_chain(execution, baseline, [receipt], current)


@pytest.mark.parametrize('job', [
    {'id': 'fixture-unrelated-job', 'owner': 'fixture-session', 'status': 'running'},
    {'id': 'fixture-unrelated-job', 'owner': 'fixture-session', 'status': 'stopping'},
    {'id': 'fixture-unrelated-job', 'owner': 'fixture-session', 'status': 'unknown'},
    {'id': 'fixture-unrelated-job', 'owner': 'fixture-session'},
    None,
], ids=['running', 'stopping', 'unknown', 'missing_status', 'malformed'])
def test_supervision_takeover_read_boundary_rejects_unrelated_nonterminal_job(job, monkeypatch, tmp_path):
    """Adverse read-fact replay; public_entry[supervise_takeover] is its canonical companion."""
    import psutil
    from types import SimpleNamespace
    monkeypatch.syspath_prepend(os.environ['HERMES_TEST_SDK_ROOT'])
    from hermes_cli import kanban_db as kb, kanban_db_connect
    from ghost_hermes_pm import shared_repository, repository_acceptance, trusted_controller
    child = subprocess.Popen([sys.executable, '-c', 'import sys; sys.stdin.read()'], stdin=subprocess.PIPE)
    birth = psutil.Process(child.pid).create_time()
    previous = {'run_id': 17, 'identity': {'pid': child.pid, 'start_sec': str(int(birth)),
        'start_usec': str(round((birth - int(birth)) * 1000000)),
        'executable': str(Path(sys.executable).resolve())}, 'exit_log_offset': 0}
    child.communicate(b'')
    assert child.returncode == 0 and not psutil.pid_exists(child.pid)
    source = {'head': None, 'workspace_status': '', 'source_digest': 'fixture-source'}
    execution = {'state': 'running', 'first_input': 'accepted', 'session_id': 'fixture-session',
        'generation': 'fixture-generation', 'baseline': source, 'supervisor_claim': previous}
    record = {'card_id': 'fixture-card', 'target': {'repo_path': 'fixture-repository'}, 'dsh_execution': execution}
    old = SimpleNamespace(id=17, task_id='fixture-card', status='gave_up', outcome='gave_up',
        ended_at=100, worker_pid=child.pid,
        error='Iteration budget exhausted (8/8) — task could not complete within the allowed iterations')
    log = tmp_path / 'fixture-worker.log'
    log.write_text('[kanban-worker-exit] rc=0\n')
    connection = object()
    reads, saves, carrier_calls = [], [], []

    @contextmanager
    def connect_closing(*, board):
        assert board == 'fixture-board'
        yield connection

    def get_run(actual_connection, run_id):
        assert actual_connection is connection and run_id == 17
        reads.append(run_id)
        return old

    class Carrier:
        def owned_jobs(self):
            carrier_calls.append('owned_jobs')
            return [job]

        def __getattr__(self, name):
            pytest.fail('Takeover rejection must not prompt or use another carrier operation: ' + name)

    monkeypatch.setattr(kanban_db_connect, 'connect_closing', connect_closing)
    monkeypatch.setattr(kb, 'get_run', get_run)
    monkeypatch.setattr(kb, 'worker_log_path', lambda *args, **kwargs: log)
    monkeypatch.setattr(shared_repository, 'read_ledger', lambda *args: {})
    monkeypatch.setattr(repository_acceptance, 'repository_source_state', lambda *args: source)
    monkeypatch.setattr(shared_repository, 'shared_receipts', lambda *args: [])
    monkeypatch.setattr(trusted_controller, 'controller_identity', lambda: {'pid': os.getpid()})
    intake = SimpleNamespace(board='fixture-board', _save_execution=lambda *args, **kwargs: saves.append((args, kwargs)))
    before = json.dumps(execution, sort_keys=True, separators=(',', ':')).encode()
    with pytest.raises(ManagementError) as caught:
        shared_repository._take_over_gave_up_claim(intake, record, 18, {}, Carrier())
    assert caught.value.code == 'outcome_unknown'
    assert str(caught.value) == 'Owned shared jobs must be terminal before supervision takeover.'
    assert reads == [17] and saves == [] and carrier_calls == ['owned_jobs']
    assert json.dumps(execution, sort_keys=True, separators=(',', ':')).encode() == before


@pytest.mark.parametrize('failure, allowed', [
    ({'code': 'binding_conflict', 'message': 'The original shared source changed before supervision takeover.'}, True),
    ({'code': 'outcome_unknown', 'message': 'The original worker input remains unconfirmed; no claim transfer.'}, True),
    ({'code': 'unauthorized', 'message': 'Existing unauthorized recovery.'}, True),
    ({'code': 'binding_conflict', 'message': 'The original shared source changed before supervision takeover!'}, False),
    ({'code': 'outcome_unknown', 'message': 'The original worker input remains unconfirmed; no claim transfer!'}, False),
    ({'code': 'outcome_unknown', 'message': 'The original shared source changed before supervision takeover.'}, False),
    ({'code': 'binding_conflict', 'message': 'The original worker input remains unconfirmed; no claim transfer.'}, False),
    ({'code': 'binding_conflict', 'message': '!The original shared source changed before supervision takeover.'}, False),
    ({'code': 'outcome_unknown', 'message': 'The original worker input remains unconfirmed; no claim transfer. Extra suffix'}, False),
    ({'code': 'binding_conflict', 'message': 'Other synthetic binding conflict.'}, False),
    ({'code': 'outcome_unknown', 'message': 'Other synthetic unknown outcome.'}, False),
    ({}, False),
    (None, False),
], ids=['source_repaired', 'input_guard_echo', 'existing_unauthorized', 'source_character', 'echo_character',
    'wrong_source_code', 'wrong_echo_code', 'prefix', 'suffix', 'other_binding', 'other_unknown', 'empty', 'missing'])
def test_supervision_takeover_read_boundary_retries_only_exact_recoverable_causes(failure, allowed, monkeypatch, tmp_path):
    """Read-fact replay; exact cause opens eligibility checks, not an authorization shortcut."""
    import psutil
    from types import SimpleNamespace
    monkeypatch.syspath_prepend(os.environ['HERMES_TEST_SDK_ROOT'])
    from hermes_cli import kanban_db as kb, kanban_db_connect
    from ghost_hermes_pm import shared_repository, repository_acceptance, trusted_controller
    child = subprocess.Popen([sys.executable, '-c', 'import sys; sys.stdin.read()'], stdin=subprocess.PIPE)
    birth = psutil.Process(child.pid).create_time()
    previous = {'run_id': 17, 'identity': {'pid': child.pid, 'start_sec': str(int(birth)),
        'start_usec': str(int((birth - int(birth)) * 1000000)),
        'executable': str(Path(sys.executable).resolve())}, 'exit_log_offset': 0}
    child.communicate(b'')
    assert child.returncode == 0 and not psutil.pid_exists(child.pid)
    source = {'head': None, 'workspace_status': '', 'source_digest': 'fixture-source'}
    execution = {'state': 'outcome_unknown', 'first_input': 'accepted', 'session_id': 'fixture-session',
        'generation': 'fixture-generation', 'baseline': source, 'supervisor_claim': previous}
    if failure is not None:
        execution['last_failure'] = failure
    record = {'card_id': 'fixture-card', 'target': {'repo_path': 'fixture-repository'}, 'dsh_execution': execution}
    old = SimpleNamespace(id=17, task_id='fixture-card', status='gave_up', outcome='gave_up',
        ended_at=int(time.time()), worker_pid=child.pid,
        error='Iteration budget exhausted (8/8) — task could not complete within the allowed iterations')
    log = tmp_path / 'fixture-worker.log'
    log.write_text('[kanban-worker-exit] rc=0\n')
    connection = object()
    reads, saves, carrier_calls = [], [], []

    @contextmanager
    def connect_closing(*, board):
        assert board == 'fixture-board'
        yield connection

    def get_run(actual_connection, run_id):
        assert actual_connection is connection and run_id == 17
        reads.append('run')
        return old

    def ledger(*args):
        reads.append('ledger')
        return {'session_id': execution['session_id'], 'generation': execution['generation']}

    def source_state(*args):
        reads.append('source')
        return source

    def receipts(*args):
        reads.append('receipts')
        return []

    class Carrier:
        def owned_jobs(self):
            carrier_calls.append('owned_jobs')
            return []

        def __getattr__(self, name):
            pytest.fail('Recovery validation must not prompt or use another carrier operation: ' + name)

    current_identity = {'pid': os.getpid(), 'start_sec': '200', 'start_usec': '0',
        'executable': str(Path(sys.executable).resolve())}
    monkeypatch.setattr(kanban_db_connect, 'connect_closing', connect_closing)
    monkeypatch.setattr(kb, 'get_run', get_run)
    monkeypatch.setattr(kb, 'worker_log_path', lambda *args, **kwargs: log)
    monkeypatch.setattr(shared_repository, 'read_ledger', ledger)
    monkeypatch.setattr(repository_acceptance, 'repository_source_state', source_state)
    monkeypatch.setattr(shared_repository, 'shared_receipts', receipts)
    monkeypatch.setattr(trusted_controller, 'controller_identity', lambda: current_identity)
    intake = SimpleNamespace(board='fixture-board', _save_execution=lambda *args, **kwargs: saves.append((args, kwargs)))
    before = json.dumps(execution, sort_keys=True, separators=(',', ':')).encode()
    if allowed:
        assert shared_repository._take_over_gave_up_claim(intake, record, 18, {}, Carrier()) is True
        assert reads == ['run', 'ledger', 'source', 'receipts'] and carrier_calls == ['owned_jobs']
        assert len(saves) == 1 and saves[0][1] == {'fields': ['state', 'supervisor_claim']}
        assert execution['state'] == 'running' and execution['supervisor_claim']['run_id'] == 18
        assert execution['supervisor_claim']['identity'] == current_identity
        assert execution['supervisor_claim']['exit_log_offset'] == log.stat().st_size
        assert source == execution['baseline']
    else:
        with pytest.raises(ManagementError) as caught:
            shared_repository._take_over_gave_up_claim(intake, record, 18, {}, Carrier())
        assert caught.value.code == 'outcome_unknown'
        assert str(caught.value) == 'The original worker input remains unconfirmed; no claim transfer.'
        assert reads == [] and saves == [] and carrier_calls == []
        assert json.dumps(execution, sort_keys=True, separators=(',', ':')).encode() == before


@pytest.fixture
def repeat_window_read_facts(monkeypatch, tmp_path):
    """Synthetic maintenance read facts; no real runtime or budget grant."""
    from types import SimpleNamespace
    from ghost_hermes_pm import shared_repository as shared, repository_control, repository_acceptance, repository_execution, repository_supervision
    child = subprocess.Popen([sys.executable, '-c', 'import sys; sys.stdin.read()'], stdin=subprocess.PIPE)
    import psutil
    birth = psutil.Process(child.pid).create_time()
    child.communicate(b'')
    assert child.returncode == 0 and not psutil.pid_exists(child.pid)
    native = {'kind': 'shared_native', 'lease': 'fixture-old-lease', 'pid': child.pid, 'created_at': birth}
    source = {'head': None, 'workspace_status': '', 'source_digest': 'fixture-source'}
    scope = {'host_lease': native['lease']}
    reference = tmp_path / 'fixture-reference.json'
    reference.write_text(json.dumps(scope))
    reference.chmod(0o600)
    config = {'workspace': str(tmp_path), 'host_lease': native['lease'], 'service_ref': 'fixture-native',
        'agent_preset': 'fixture-preset', 'provider': 'fixture', 'model': 'fixture', 'ledger_dir': str(tmp_path),
        'budget': {'max_wall_seconds': 2, 'max_model_requests': 8}, 'reference_sha256': 'fixture-reference'}
    admissions = ['fixture-request-' + str(index) for index in range(3)]
    original_state = {'started_at_ms': int(time.time() * 1000) - 20000, 'requests': len(admissions) - 1,
        'reported_tokens': 5, 'admissions': admissions[:-1], 'stop_reason': None}
    row = {'schema': 1, 'session_id': 'fixture-session', 'generation': 'fixture-generation', 'session_created_at': 100,
        **{key: config[key] for key in ('workspace', 'host_lease', 'service_ref', 'agent_preset', 'provider', 'model', 'budget')},
        'state': {**original_state, 'requests': len(admissions), 'reported_tokens': 10, 'admissions': admissions},
        'renewWindow': {'id': 'fixture-prior-window', 'message_id': 'fixture-prior-grant',
            'original_host_lease': 'fixture-original-lease',
            'host_lease': native['lease'], 'hook_marker': 'same-sid-window-v1',
            'started_at_ms': int(time.time() * 1000) - 10000, 'stop_reason': 'shared-work-wall-budget',
            'request_base': original_state['requests'], 'original_state': original_state}}
    receipt = {'schema': 1, 'generation': row['generation'], 'session_id': row['session_id'], 'session_created_at': 100,
        'service_ref': config['service_ref'], 'host_lease': native['lease'], 'name': 'bash', 'kind': 'foreground',
        'execution_admission': 'admitted', 'is_error': False, 'exit_code': 0, 'timed_out': False, 'aborted': False,
        'native_session_seq': 1, 'source_before': source, 'source_after': source}
    execution = {'mode': 'shared_native', 'state': 'budget_stopped', 'terminal_reason': {
        'kind': 'aborted', 'reason': {'kind': 'hook', 'reason': 'shared-work-wall-budget'}},
        'first_input': 'accepted', 'session_id': row['session_id'], 'generation': row['generation'],
        'session_created_at': 100, 'workspace_id': 'fixture-workspace', 'native_identity': native, 'baseline': source,
        'acceptance': {'status': 'unmet', 'delivered': False, 'unmet': ['delivery_report_missing']},
        'renewal_window_checkpoint': {'status': 'admitted'}, 'renewal_window_adoption': 'accepted',
        'continuation_control': {'status': 'accepted'}, 'tool_receipts': [receipt], 'jobs': []}
    record = {'id': 'fixture-work', 'card_id': 'fixture-card',
        'target': {'execution_ref': str(reference), 'repo_path': str(tmp_path)}, 'dsh_execution': execution}
    ledger = shared.ledger_path(config, execution['session_id'])
    ledger.write_text(json.dumps(row))
    ledger.chmod(0o600)
    receipts_path = shared.ledger_path(config, execution['session_id'], '.tools.jsonl')
    receipts_path.write_text(json.dumps(receipt) + '\n')
    receipts_path.chmod(0o600)
    events = [{'type': 'turn/start', 'seq': 0, 'data': {'turn': 1}}]
    for _ in admissions:
        events.append({'type': 'assistant/message', 'seq': len(events), 'data': {'message': {'content': []}}})
    events.append({'type': 'turn/end', 'seq': len(events), 'data': {'reason': execution['terminal_reason']}})
    snapshot = {'header': {'id': execution['session_id'], 'createdAt': 100, 'cwd': config['workspace']},
        'hasMore': False, 'cursor': len(events) - 1, 'records': [{'event': event} for event in events],
        'projections': {'values': {'agentPreset': config['agent_preset'], 'permissions': {'currentValue': 'workspace-write'},
            'inbox': {'next-turn': [], 'next-step': []}}}}
    workspace = {'workspaceId': execution['workspace_id'], 'sessionIds': [execution['session_id']]}
    worker = {'run_id': 17, 'identity': {'pid': child.pid}, 'exit_code': 0}
    facts = SimpleNamespace(record=record, config=config, row=row, ledger=ledger, reference=reference,
        source=source, native=native, worker=worker, snapshot=snapshot, jobs=[], saves=[], carrier_calls=[])

    class Carrier:
        def __init__(self, configuration):
            self.native_identity = facts.native
        def stream(self, method, payload):
            return (item for item in [facts.snapshot])
        def owned_jobs(self):
            return facts.jobs
        def close(self):
            pass

    monkeypatch.setattr(repository_control, '_card', lambda *args: {'status': 'blocked'})
    monkeypatch.setattr(repository_control, '_worker_end', lambda *args, **kwargs: worker)
    monkeypatch.setattr(shared, 'shared_configuration', lambda *args, **kwargs: dict(config))
    monkeypatch.setattr(repository_execution, 'execution_configuration', lambda *args: (dict(config), {}))
    monkeypatch.setattr(shared, 'SharedDesktopTransport', Carrier)
    monkeypatch.setattr(shared, 'resolve_shared_workspace', lambda *args: workspace)
    monkeypatch.setattr(repository_acceptance, 'repository_source_state', lambda *args: source)
    def owned_call(carrier, method, payload):
        facts.carrier_calls.append(method)
        assert method == 'session/list', 'A rejected grant must not adopt or prompt.'
        return {'items': [{'sessionId': execution['session_id'], 'running': False, 'agentAvailable': True}]}
    monkeypatch.setattr(repository_supervision, 'owned_call', owned_call)
    facts.intake = SimpleNamespace(board='fixture-board', _save_execution=lambda *args, **kwargs: facts.saves.append((args, kwargs)))
    return facts


@pytest.mark.parametrize('change', ['awaiting', 'wrong_terminal', 'pending_checkpoint', 'unknown_checkpoint',
    'pending_adoption', 'unknown_adoption', 'active_job', 'unknown_job', 'saved_receipts', 'saved_jobs'])
def test_repeat_window_prepare_rejects_unconfirmed_delta(change, repeat_window_read_facts):
    from ghost_hermes_pm.shared_repository import prepare_shared_window_renewal
    f = repeat_window_read_facts
    e = f.record['dsh_execution']
    if change == 'awaiting':
        e['state'] = 'awaiting_acceptance'
    elif change == 'wrong_terminal':
        e['terminal_reason']['reason']['kind'] = 'user'
    elif change in {'pending_checkpoint', 'unknown_checkpoint'}:
        e['renewal_window_checkpoint']['status'] = 'prepared' if change == 'pending_checkpoint' else 'unknown'
    elif change in {'pending_adoption', 'unknown_adoption'}:
        e['renewal_window_adoption'] = 'intent' if change == 'pending_adoption' else 'unknown'
    elif change in {'active_job', 'unknown_job'}:
        f.jobs = [{'id': 'fixture-other-job', 'owner': e['session_id'], 'status': 'running' if change == 'active_job' else 'unknown'}]
    elif change == 'saved_receipts':
        e['tool_receipts'] = []
    elif change == 'saved_jobs':
        e['jobs'] = [{'id': 'fixture-other-job', 'status': 'completed'}]
    before = f.ledger.read_bytes()
    with pytest.raises(ManagementError):
        prepare_shared_window_renewal(f.intake, f.record, 'fixture-first-grant')
    assert f.ledger.read_bytes() == before and f.saves == []
    assert e['renewal_window_checkpoint']['status'] != 'prepared' or change == 'pending_checkpoint'
    assert 'session/prompt' not in f.carrier_calls


@pytest.mark.parametrize('case', ['state', 'history', 'card_worker', 'event_chain', 'active_job',
    'old_identity', 'scope', 'ledger_business'])
def test_owner_maintenance_rejects_unverified_read_facts(case, repeat_window_read_facts, monkeypatch):
    from types import SimpleNamespace
    from ghost_hermes_pm import shared_repository as shared, repository_control, repository_supervision
    f = repeat_window_read_facts
    e = f.record['dsh_execution']
    old_row = deepcopy(f.row)
    backup = f.ledger.with_suffix('.owner-backup')
    backup.write_text(json.dumps(old_row)); backup.chmod(0o600)
    state = deepcopy(old_row['state'])
    f.row['renewWindow'] = {'id': 'fixture-owner-window', 'message_id': 'fixture-renew',
        'host_lease': f.native['lease'], 'request_base': state['requests'], 'original_state': state,
        'original_host_lease': 'fixture-origin-lease', 'started_at_ms': None, 'hook_marker': None, 'stop_reason': None}
    f.row['state']['binding_sha256'] = shared._window_fingerprint(f.row)
    f.ledger.write_text(json.dumps(f.row))
    receipts = {}
    for suffix in ('.tools.jsonl', '.jobs.jsonl'):
        p = shared.ledger_path(f.config, e['session_id'], suffix)
        raw = p.read_bytes() if p.exists() else b''
        receipts[suffix] = {'size': len(raw), 'sha256': hashlib.sha256(raw).hexdigest()}
    account = {'workspace_id': e['workspace_id'], 'session_ids': [e['session_id']]}
    e.update(state='outcome_unknown', reference_sha256=f.config['reference_sha256'], last_failure={'code': 'outcome_unknown'},
        supervisor_claim=f.worker, continuation_request_id='fixture-generic-request', continuation_control={
            'kind': 'renew_window', 'status': 'accepted', 'dispatch_status': 'accepted', 'cursor': 0,
            'snapshot_sha256': 'fixture-original-snapshot', 'ledger_sha256': hashlib.sha256(f.ledger.read_bytes()).hexdigest(),
            'workspace': account}, renewal_window_checkpoint={'status': 'admitted', 'message_id': 'fixture-renew',
            'scope': {'host_lease': f.native['lease']}, 'previous_state': state, 'previous_renew_window': old_row['renewWindow'],
            'ledger_backup': str(backup), 'ledger_sha256': hashlib.sha256(backup.read_bytes()).hexdigest(),
            'receipts': receipts, 'source': f.source, 'workspace': account, 'native_identity': f.native},
        renewal_window_history=[{'message_id': 'fixture-renew', 'ledger_backup': str(backup), 'renew_window': old_row['renewWindow']}])
    events = [{'type': 'session/end-seed', 'seq': 0, 'data': {}}]
    controls = []
    reason = {'kind': 'error', 'error': {'message': 'Shared work native settlement usage is unknown.', 'code': 'UNKNOWN'}}
    for turn in (5, 6, 7):
        mid = 'fixture-owner-' + str(turn)
        request = 'fixture-generic-request' if turn == 5 else 'owner-context-' + hashlib.sha256((f.record['id'] + ':' + mid).encode()).hexdigest()
        cursor = len(events) - 1
        for kind, data in [('agent/inbox/spliced', {'target': 'next-turn', 'start': 0, 'inserted': [
                {'id': mid, 'role': 'user', 'source': {'kind': 'user', 'rpcId': request}, 'content': [{'type': 'text', 'text': 'Verify fixture scope.'}]}]}),
                ('turn/start', {'turn': turn}), ('agent/inbox/spliced', {'target': 'next-turn', 'start': 0, 'removedCount': 1, 'inserted': []}),
                ('step/start', {'turn': turn, 'step': 1}), ('step/end', {'turn': turn, 'step': 1}), ('turn/end', {'turn': turn, 'reason': reason})]:
            events.append({'type': kind, 'seq': len(events), 'data': data})
        if turn > 5:
            controls.append({'message_id': mid, 'request_id': request, 'status': 'accepted', 'dispatch_status': 'accepted',
                'cursor': cursor, 'snapshot_sha256': 'fixture-owner-snapshot', 'failed_turn': turn - 1, 'turn': turn,
                'turn_start_seq': cursor + 2, 'text_sha256': hashlib.sha256(b'Verify fixture scope.').hexdigest()})
    e.update(owner_context_history=controls[:1], owner_context_control=controls[1])
    f.snapshot.update(cursor=len(events) - 1, records=[{'event': event} for event in events])
    f.intake.secret_values = ()
    f.intake.require_active = lambda generation: None
    def owner_read_call(carrier, method, payload):
        f.carrier_calls.append(method)
        assert method == 'session/list', 'A rejected maintenance must not perform adoption or input.'
        return {'items': [{'sessionId': e['session_id'], 'cwd': f.config['workspace'], 'running': False, 'agentAvailable': True}]}
    monkeypatch.setattr(repository_supervision, 'owned_call', owner_read_call)
    if case in {'old_identity', 'scope', 'ledger_business'}:
        e['owner_context_maintenance_checkpoint'] = {'status': 'prepared', 'message_id': 'fixture-first-continue',
            'native_identity': f.native, 'reference_sha256': e['reference_sha256'], 'worker': f.worker,
            'owners': {'current': deepcopy(controls[1]), 'history': deepcopy(controls[:1])},
            'scope': {'host_lease': f.native['lease']}, 'source': f.source, 'ledger_backup': str(f.ledger),
            'ledger_sha256': hashlib.sha256(f.ledger.read_bytes()).hexdigest()}
        if case == 'scope':
            f.reference.write_text(json.dumps({'host_lease': f.native['lease'], 'unexpected': 'fixture'}))
        elif case == 'ledger_business':
            altered = deepcopy(f.row); altered['state']['requests'] += 1
            f.ledger.write_text(json.dumps(altered))
    elif case == 'state': e['state'] = 'running'
    elif case == 'history': e['owner_context_history'] = []
    elif case == 'card_worker': monkeypatch.setattr(repository_control, '_card', lambda *args: {'status': 'running'})
    elif case == 'event_chain': f.snapshot['records'][-1]['event']['data']['reason'] = {'kind': 'completed'}
    elif case == 'active_job': f.jobs = [{'id': 'fixture-active-job', 'status': 'running'}]
    before = f.ledger.read_bytes()
    with pytest.raises(ManagementError):
        if case in {'old_identity', 'scope', 'ledger_business'}:
            shared.admit_shared_owner_maintenance(f.intake, f.record, 'fixture-second-continue', 0)
        else:
            shared.admit_shared_owner_context(f.intake, f.record, SimpleNamespace(command='继续 fixture-work',
                envelope={'message_id': 'fixture-first-continue'}), 0, prepare_maintenance=True)
    assert f.ledger.read_bytes() == before and f.saves == [] and 'session/create' not in f.carrier_calls


def test_owner_context_refuses_incomplete_receipt_checkpoint(repeat_window_read_facts):
    from types import SimpleNamespace
    from ghost_hermes_pm.shared_repository import admit_shared_owner_context
    f = repeat_window_read_facts
    execution = f.record['dsh_execution']
    execution.update(state='outcome_unknown', last_failure={'code': 'outcome_unknown'},
        continuation_control={'kind': 'renew_window', 'status': 'accepted', 'dispatch_status': 'accepted',
            'snapshot_sha256': 'fixture-snapshot', 'ledger_sha256': 'fixture-ledger'})
    execution['renewal_window_checkpoint']['receipts'] = {'.tools.jsonl': {'size': 0, 'sha256': 'fixture-missing-jobs'}}
    with pytest.raises(ManagementError, match='receipt checkpoints are incomplete'):
        admit_shared_owner_context(f.intake, f.record, SimpleNamespace(command='补充 fixture-work\nVerify fixture scope.',
            envelope={'message_id': 'fixture-owner-message'}), 0)
    assert f.saves == [] and f.carrier_calls == []


@pytest.mark.parametrize('change', ['same_identity', 'same_lease', 'same_pid', 'same_birth', 'history', 'same_grant', 'unknown_adoption'])
def test_repeat_window_admit_rejects_unconfirmed_delta(change, repeat_window_read_facts):
    from ghost_hermes_pm.shared_repository import admit_shared_window_renewal, _renewal_history_sha256
    f = repeat_window_read_facts
    e = f.record['dsh_execution']
    e['renewal_window_adoption'] = None
    checkpoint = {'status': 'prepared', 'repeat_budget_stopped': True, 'message_id': 'fixture-first-grant',
        'terminal_reason': deepcopy(e['terminal_reason']), 'worker': f.worker, 'native_identity': deepcopy(e['native_identity']),
        'scope': {'host_lease': e['native_identity']['lease']}, 'previous_state': deepcopy(f.row['state']),
        'previous_renew_window': deepcopy(f.row['renewWindow'])}
    e['renewal_window_checkpoint'] = checkpoint
    checkpoint['history_sha256'] = _renewal_history_sha256(e)
    backup = f.ledger.with_name('fixture-window-backup.json')
    backup.write_bytes(f.ledger.read_bytes())
    backup.chmod(0o600)
    checkpoint.update(ledger_backup=str(backup), ledger_sha256=hashlib.sha256(backup.read_bytes()).hexdigest(),
        workspace={'workspace_id': e['workspace_id'], 'session_ids': [e['session_id']]})
    new = {**f.native, 'lease': 'fixture-new-lease', 'pid': f.native['pid'] + 100000, 'created_at': f.native['created_at'] + 1}
    if change == 'same_identity':
        new = dict(f.native)
    elif change in {'same_lease', 'same_pid', 'same_birth'}:
        key = {'same_lease': 'lease', 'same_pid': 'pid', 'same_birth': 'created_at'}[change]
        new[key] = f.native[key]
    elif change == 'history':
        e['continuation_history'] = [{'changed': True}]
    elif change == 'unknown_adoption':
        e['renewal_window_adoption'] = 'unknown'
    f.reference.write_text(json.dumps({'host_lease': new['lease']}))
    f.config['host_lease'] = new['lease']
    f.native = new
    before = f.ledger.read_bytes()
    message_id = 'fixture-first-grant' if change == 'same_grant' else 'fixture-second-grant'
    with pytest.raises(ManagementError):
        admit_shared_window_renewal(f.intake, f.record, message_id, 'fixture-generation')
    assert f.ledger.read_bytes() == before and f.saves == []
    assert checkpoint['status'] == 'prepared' and 'session/prompt' not in f.carrier_calls


@pytest.mark.parametrize('change', ['partial', 'superset', 'different_id', 'different_status', 'duplicate',
    'wrong_owner', 'active', 'unknown', 'missing_status', 'malformed', 'unreadable',
    'checkpoint_active', 'checkpoint_unknown', 'checkpoint_duplicate', 'checkpoint_malformed'])
def test_repeat_window_restored_roster_rejects_unsealed_or_partial_jobs(change, repeat_window_read_facts):
    from ghost_hermes_pm.shared_repository import admit_shared_window_renewal, _renewal_history_sha256
    f = repeat_window_read_facts
    e = f.record['dsh_execution']
    e['renewal_window_adoption'] = 'accepted'
    saved = [{'id': 'fixture-old-job-one', 'status': 'completed'}, {'id': 'fixture-old-job-two', 'status': 'killed'}]
    checkpoint = {'status': 'prepared', 'repeat_budget_stopped': True, 'message_id': 'fixture-first-grant',
        'terminal_reason': deepcopy(e['terminal_reason']), 'worker': f.worker, 'native_identity': deepcopy(e['native_identity']),
        'scope': {'host_lease': e['native_identity']['lease']}, 'previous_state': deepcopy(f.row['state']),
        'previous_renew_window': deepcopy(f.row['renewWindow']), 'jobs': saved,
        'snapshot_sha256': hashlib.sha256(json.dumps(f.snapshot, sort_keys=True).encode()).hexdigest()}
    e['renewal_window_checkpoint'] = checkpoint
    checkpoint['history_sha256'] = _renewal_history_sha256(e)
    backup = f.ledger.with_name('fixture-window-backup.json')
    backup.write_bytes(f.ledger.read_bytes())
    backup.chmod(0o600)
    checkpoint.update(ledger_backup=str(backup), ledger_sha256=hashlib.sha256(backup.read_bytes()).hexdigest(),
        workspace={'workspace_id': e['workspace_id'], 'session_ids': [e['session_id']]})
    current = [{**job, 'owner': e['session_id']} for job in saved]
    if change == 'partial':
        current = current[:1]
    elif change == 'superset':
        current.append({'id': 'fixture-extra-job', 'status': 'failed', 'owner': e['session_id']})
    elif change == 'different_id':
        current[0]['id'] = 'fixture-different-job'
    elif change == 'different_status':
        current[0]['status'] = 'failed'
    elif change == 'duplicate':
        current = [current[0], dict(current[0])]
    elif change == 'wrong_owner':
        current[0]['owner'] = 'fixture-other-session'
    elif change in {'active', 'unknown'}:
        current[0]['status'] = 'running' if change == 'active' else 'unknown'
    elif change == 'missing_status':
        current[0].pop('status')
    elif change == 'malformed':
        current = [None]
    elif change == 'unreadable':
        current = None
    elif change in {'checkpoint_active', 'checkpoint_unknown'}:
        saved[0]['status'] = 'running' if change == 'checkpoint_active' else 'unknown'
        current = []
    elif change == 'checkpoint_duplicate':
        checkpoint['jobs'] = [saved[0], dict(saved[0])]
        current = []
    elif change == 'checkpoint_malformed':
        checkpoint['jobs'] = [None]
        current = []
    f.jobs = current
    new = {**f.native, 'lease': 'fixture-new-lease', 'pid': f.native['pid'] + 100000,
        'created_at': f.native['created_at'] + 1}
    f.reference.write_text(json.dumps({'host_lease': new['lease']}))
    f.config['host_lease'] = new['lease']
    f.native = new
    ledger_before = f.ledger.read_bytes()
    checkpoint_before = json.dumps(checkpoint, sort_keys=True).encode()
    with pytest.raises(ManagementError) as caught:
        admit_shared_window_renewal(f.intake, f.record, 'fixture-second-grant', 'fixture-generation')
    assert caught.value.code == 'binding_conflict'
    assert str(caught.value) == 'The restored expired window, history or jobs differ.'
    assert f.ledger.read_bytes() == ledger_before and f.saves == []
    assert json.dumps(checkpoint, sort_keys=True).encode() == checkpoint_before
    assert f.carrier_calls == ['session/list']


@pytest.mark.parametrize('error', [
    'Iteration budget exhausted (7/8) — task could not complete within the allowed iterations',
    'Iteration budget exhausted (8/8) — task could not complete within the allowed iterations; extra suffix',
], ids=['unequal_budget', 'suffix'])
def test_supervision_takeover_read_boundary_rejects_noncanonical_budget_error(error, monkeypatch):
    """Guard read-boundary replay, not SDK-generated noncanonical gave_up.

    The public_entry[supervise_takeover] scenario supplies the real canonical
    native lifecycle companion; this isolates its adverse Kanban read facts.
    """
    from types import SimpleNamespace
    monkeypatch.syspath_prepend(os.environ['HERMES_TEST_SDK_ROOT'])
    from hermes_cli import kanban_db as kb, kanban_db_connect
    from ghost_hermes_pm.shared_repository import _take_over_gave_up_claim
    previous = {'run_id': 17, 'identity': {'pid': os.getpid(), 'start_sec': '100',
        'start_usec': '0', 'executable': str(Path(sys.executable).resolve())}, 'exit_log_offset': 0}
    execution = {'state': 'running', 'first_input': 'accepted',
        'continuation_control': {'status': 'accepted'}, 'supervisor_claim': previous}
    record = {'card_id': 'fixture-card', 'dsh_execution': execution}
    old = SimpleNamespace(id=17, task_id='fixture-card', status='gave_up', outcome='gave_up',
        ended_at=100, worker_pid=previous['identity']['pid'], error=error)
    connection = object()
    reads, saves = [], []

    @contextmanager
    def connect_closing(*, board):
        assert board == 'fixture-board'
        yield connection

    def get_run(actual_connection, run_id):
        assert actual_connection is connection and run_id == 17
        reads.append(run_id)
        return old

    class UnusedCarrier:
        def __getattr__(self, name):
            pytest.fail('Read-boundary rejection must not use the carrier or send a prompt: ' + name)

    monkeypatch.setattr(kanban_db_connect, 'connect_closing', connect_closing)
    monkeypatch.setattr(kb, 'get_run', get_run)
    intake = SimpleNamespace(board='fixture-board',
        _save_execution=lambda *args, **kwargs: saves.append((args, kwargs)))
    before = json.dumps(previous, sort_keys=True, separators=(',', ':')).encode()
    with pytest.raises(ManagementError) as caught:
        _take_over_gave_up_claim(intake, record, 18, {}, UnusedCarrier())
    assert caught.value.code == 'outcome_unknown'
    assert str(caught.value) == 'The original iteration-budget worker cannot be safely supervised.'
    assert reads == [17] and saves == []
    assert json.dumps(execution['supervisor_claim'], sort_keys=True, separators=(',', ':')).encode() == before


@contextmanager
def desktop_service(tmp_path):
    calls = []

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def do_GET(self):
            calls.append(('GET', self.path))
            assert self.path == '/?token=synthetic-only'
            self.send_response(303)
            self.send_header('Set-Cookie', 'fixture-session=value; HttpOnly; Path=/')
            self.send_header('Location', './')
            self.end_headers()

        def do_POST(self):
            assert self.headers['Cookie'] == 'fixture-session=value'
            packet = json.loads(self.rfile.read(int(self.headers['Content-Length'])))
            calls.append(packet)
            value = {'items': []} if packet['method'] == 'session/list' else {'accepted': True}
            body = json.dumps({'type': 'server-response', 'rpcId': packet['rpcId'],
                               'result': {'ok': True, 'value': value}}).encode()
            self.send_response(200)
            self.send_header('Content-Length', str(len(body)))
            self.end_headers()
            self.wfile.write(body)

    server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    path = tmp_path / 'desktop-host.json'
    path.write_text(json.dumps({'schema': 1, 'lease': 'synthetic-lease', 'pid': os.getpid(),
        'url': 'http://127.0.0.1:' + str(server.server_port) + '/?token=synthetic-only',
        'runtimeVersion': 'fixture', 'permissionMode': 'workspace-write'}))
    path.chmod(0o600)
    try:
        yield path, calls
    finally:
        server.shutdown()
        server.server_close()
        thread.join()


def test_shared_desktop_consumes_native_receipt_and_never_exports_auth(tmp_path):
    from ghost_hermes_pm.shared_desktop import SharedDesktopTransport
    with desktop_service(tmp_path) as (path, calls):
        carrier = SharedDesktopTransport({'desktop_receipt': str(path), 'host_lease': 'synthetic-lease',
            'service_ref': 'fixture-service', 'session_id': 'fixture-owned', 'generation': 'fixture-generation',
            'workspace': str(tmp_path)}, timeout=1)
        try:
            result = carrier.request('session/list', {'_request': {}}, 'fixture-list')
            assert result['result']['value'] == {'items': []}
            assert carrier.native_identity['kind'] == 'shared_native'
            assert carrier.native_identity['pid'] == os.getpid()
            assert set(tmp_path.iterdir()) == {path}
            with pytest.raises(ManagementError, match='owned'):
                carrier.request('session/cancel', {'request': {'sessionId': 'manual-foreign'}}, 'foreign')
            assert len(calls) == 2
        finally:
            carrier.close()


@pytest.mark.parametrize('change', ['lease', 'mode', 'hardlink'])
def test_shared_desktop_revalidates_receipt_before_mutation(tmp_path, change):
    from ghost_hermes_pm.shared_desktop import SharedDesktopTransport
    with desktop_service(tmp_path) as (path, calls):
        carrier = SharedDesktopTransport({'desktop_receipt': str(path), 'host_lease': 'synthetic-lease',
            'service_ref': 'fixture-service', 'session_id': 'fixture-owned', 'generation': 'fixture-generation',
            'workspace': str(tmp_path)}, timeout=1)
        try:
            carrier.request('session/list', {'_request': {}}, 'first')
            if change == 'lease':
                row = json.loads(path.read_text())
                row['lease'] = 'new-lease'
                path.write_text(json.dumps(row))
            elif change == 'mode':
                path.chmod(0o644)
            else:
                os.link(path, tmp_path / 'receipt-alias')
            with pytest.raises(ManagementError):
                carrier.request('session/cancel', {'request': {'sessionId': 'fixture-owned'}}, 'stop')
            assert len(calls) == 2
        finally:
            carrier.close()


@contextmanager
def shared_remote_peer(tmp_path, behavior):
    # This is only the external protocol boundary fixture, never an SDK patch.
    path = Path(__file__).with_name('dsh_fixture_server.py')
    source = path.read_text().replace("assert self.path == '/api/remote.mux'", """if self.path == '/?token=fixture-launch':
                self.send_response(303)
                self.send_header('Set-Cookie', 'synthetic-auth=value; HttpOnly; Path=/')
                self.send_header('Location', './')
                self.send_header('Content-Length', '0')
                self.end_headers()
                return
            assert self.path == '/api/remote.mux'""")
    source = source.replace("elif method == 'session/create':", """elif method == 'workspace/create':
                assert set(args['request']) == {'path'}
                created = 'workspace' not in state
                if created:
                    state['workspace'] = {'workspaceId': 'fixture-workspace', 'path': args['request']['path'],
                        'title': 'Fixture', 'sessionIds': [], 'createdAt': 'fixture-created', 'updatedAt': 'fixture-created'}
                value = {'workspace': dict(state['workspace']), 'created': created}
            elif method == 'session/create':""")
    begin = source.index("            elif method == 'session/create':")
    end = source.index("            elif method == 'session/prompt':", begin)
    source = source[:begin] + """            elif method == 'session/create':
                creation = args['request']
                assert ('workspaceId' in creation) != ('cwd' in creation)
                cwd = state['workspace']['path'] if 'workspaceId' in creation else creation['cwd']
                value = {'sessionId': creation['sessionId'], 'agentPreset': creation['agentPreset']}
                rows = [row for row in state['sessions'] if row['sessionId'] == creation['sessionId']]
                if not rows:
                    state['sessions'].append({'sessionId': creation['sessionId'], 'cwd': cwd,
                        'running': False, 'agentAvailable': True, 'blank': True, 'updatedAt': 3})
                    state['journals'][creation['sessionId']] = []
                else:
                    assert len(rows) == 1 and rows[0]['cwd'] == cwd
                if 'workspaceId' in creation and creation['sessionId'] not in state['workspace']['sessionIds']:
                    assert creation['workspaceId'] == state['workspace']['workspaceId']
                    state['workspace']['sessionIds'].append(creation['sessionId'])
""" + source[end:]
    source = source.replace("'values': {'inbox': behavior.get('inbox', {'next-turn': [], 'next-step': []})}",
        "'values': {'agentPreset': 'fixture-shared', 'inbox': behavior.get('inbox', {'next-turn': [], 'next-step': []})}")
    namespace = {}
    exec(compile(source, str(path), 'exec'), namespace)
    with namespace['remote_peer'](behavior=behavior) as (url, peer):
        receipt = tmp_path / 'desktop-host.json'
        receipt.write_text(json.dumps({'schema': 1, 'lease': 'fixture-lease', 'pid': os.getpid(),
            'url': url + '/?token=fixture-launch', 'runtimeVersion': 'fixture', 'permissionMode': 'workspace-write'}))
        receipt.chmod(0o600)
        from ghost_hermes_pm.shared_desktop import SharedDesktopTransport
        carrier = SharedDesktopTransport({'desktop_receipt': str(receipt), 'host_lease': 'fixture-lease',
            'service_ref': 'fixture-service', 'session_id': 'session-fixture', 'generation': 'fixture-generation',
            'workspace': str(tmp_path), 'agent_preset': 'fixture-shared', 'ledger_dir': str(tmp_path)}, timeout=1)
        try:
            yield carrier, peer
        finally:
            carrier.close()


def test_shared_workspace_registration_fences_path_and_adopts_only_original_session(tmp_path):
    from ghost_hermes_pm.repository_supervision import owned_call
    with shared_remote_peer(tmp_path, {}) as (carrier, peer):
        peer['sessions'][0]['cwd'] = str(tmp_path)
        original = deepcopy(peer['journals']['session-fixture'])
        workspace = owned_call(carrier, 'workspace/create', {'path': str(tmp_path)})
        assert workspace['created'] is True and workspace['workspace']['sessionIds'] == []
        request = {'sessionId': 'session-fixture', 'workspaceId': workspace['workspace']['workspaceId'],
                   'agentPreset': 'fixture-shared'}
        assert owned_call(carrier, 'session/create', request) == {'sessionId': 'session-fixture', 'agentPreset': 'fixture-shared'}
        repeated = owned_call(carrier, 'workspace/create', {'path': str(tmp_path)})
        assert repeated['created'] is False and repeated['workspace']['sessionIds'] == ['session-fixture']
        assert peer['journals']['session-fixture'] == original and peer['prompts'] == []
        for method, arguments in [
            ('workspace/create', {'path': str(tmp_path.parent)}),
            ('workspace/delete', {'workspaceId': 'fixture-workspace'}),
            ('session/create', {**request, 'cwd': str(tmp_path)}),
            ('session/create', {**request, 'sessionId': 'foreign-session'}),
            ('session/create', {**request, 'workspaceId': 'foreign-workspace'}),
        ]:
            count = len(peer['calls'])
            with pytest.raises(ManagementError):
                owned_call(carrier, method, arguments)
            assert len(peer['calls']) == count


@pytest.mark.parametrize('mismatch', [None, 'cwd', 'sid', 'created_at', 'workspace_id'])
def test_shared_workspace_backfill_preserves_accepted_input_and_budget(tmp_path, mismatch):
    from types import SimpleNamespace
    from ghost_hermes_pm.shared_repository import bind_shared_workspace, ledger_path
    from ghost_hermes_pm.shared_desktop import SharedDesktopTransport
    with shared_remote_peer(tmp_path, {}) as (carrier, peer):
        peer['sessions'][0]['cwd'] = str(tmp_path)
        record = {'id': 'fixture-work', 'target': {'repo_path': str(tmp_path)}, 'dsh_execution': {
            'mode': 'shared_native', 'state': 'execution_failed', 'session_id': 'session-fixture',
            'session_created_at': 0, 'generation': 'fixture-generation', 'first_input': 'accepted',
            'request_id': 'old-input', 'native_identity': carrier.native_identity}}
        ledger = ledger_path(carrier.config, 'session-fixture')
        budget = json.dumps({'state': {'started_at_ms': None, 'requests': 0, 'reported_tokens': 0}}).encode()
        ledger.write_bytes(budget)
        ledger.chmod(0o600)
        saved = []
        intake = SimpleNamespace(_save_execution=lambda value, fields=None: saved.append(json.loads(json.dumps(value))))
        if mismatch == 'cwd':
            peer['sessions'][0]['cwd'] = str(tmp_path.parent)
        elif mismatch == 'sid':
            record['dsh_execution']['session_id'] = 'foreign-session'
        elif mismatch == 'created_at':
            record['dsh_execution']['session_created_at'] = 1
        elif mismatch == 'workspace_id':
            carrier.config['workspace_id'] = record['dsh_execution']['workspace_id'] = 'original-workspace'
            peer['workspace'] = {'workspaceId': 'changed-workspace', 'path': str(tmp_path), 'sessionIds': [],
                'title': 'Fixture', 'createdAt': 'fixture-created', 'updatedAt': 'fixture-created'}
        original = deepcopy(record)
        journal = deepcopy(peer['journals']['session-fixture'])
        if mismatch:
            with pytest.raises(ManagementError):
                bind_shared_workspace(intake, record, carrier)
            assert not saved and record == original
            assert not any(call['method'] == 'session/create' for call in peer['calls'])
            if mismatch != 'workspace_id':
                assert not any(call['method'] == 'workspace/create' for call in peer['calls'])
        else:
            workspace = bind_shared_workspace(intake, record, carrier)
            assert workspace['sessionIds'] == ['session-fixture']
            configuration = dict(carrier.config)
            carrier.close()
            reconnected = SharedDesktopTransport(configuration, timeout=1)
            try:
                assert bind_shared_workspace(intake, record, reconnected)['workspaceId'] == workspace['workspaceId']
            finally:
                reconnected.close()
            assert sum(call['method'] == 'session/create' for call in peer['calls']) == 1
            assert {key: value for key, value in record['dsh_execution'].items() if key != 'workspace_id'} == original['dsh_execution']
        assert peer['journals']['session-fixture'] == journal and ledger.read_bytes() == budget
        assert peer['prompts'] == []


def test_shared_original_waterfalls_ignore_manual_tasks_and_accept_only_one_owned_choice(tmp_path):
    frames = [{'type': 'waterfall', 'event': 'approval/request', 'eventId': 'manual-request',
        'agentId': 'manual-session', 'request': {'toolName': 'bash', 'callId': 'manual-call'}},
        {'type': 'waterfall', 'event': 'approval/request', 'eventId': 'owned-request',
        'agentId': 'session-fixture', 'request': {'toolName': 'bash', 'callId': 'owned-call'}}]
    with shared_remote_peer(tmp_path, {'remote_events': frames}) as (carrier, peer):
        carrier.start_events()
        deadline = time.monotonic() + 2
        while time.monotonic() < deadline:
            packet = carrier.event_frames()
            if packet['frames']:
                break
            time.sleep(.01)
        assert [frame['eventId'] for frame in packet['frames']] == ['owned-request']
        rejected = {'clientId': packet['client_id'], 'eventId': 'owned-request',
            'outcome': {'kind': 'result', 'value': 'allowed-always'}}
        with pytest.raises(ManagementError):
            carrier.request('$events/result', rejected, 'unapproved-persistence')
        approved = dict(rejected, outcome={'kind': 'result', 'value': 'allowed-once'})
        assert carrier.request('$events/result', approved, 'single-choice')['result']['ok'] is True
        with pytest.raises(ManagementError):
            carrier.request('$events/result', approved, 'repeat')
        results = [call['payload']['args'] for call in peer['calls'] if call['method'] == '$events/result']
        assert results == [{'clientId': packet['client_id'], 'eventId': 'manual-request', 'outcome': {'kind': 'next'}}, approved]


def test_shared_stop_never_adopts_visible_unowned_jobs(tmp_path):
    jobs = [{'id': 'unowned', 'owner': None, 'status': 'running'},
            {'id': 'owned', 'owner': 'session-fixture', 'status': 'running'}]
    with shared_remote_peer(tmp_path, {'jobs': jobs}) as (carrier, peer):
        assert carrier.owned_jobs() == [jobs[1]]
        with pytest.raises(ManagementError):
            carrier.request('job/kill', {'request': {'sessionId': 'session-fixture', 'jobId': 'unowned'}}, 'unsafe')
        assert peer['calls'] == []


@pytest.mark.parametrize('entry', ['reader', 'runner'])
def test_privileged_source_reads_do_not_execute_repository_fsmonitor(tmp_path, entry):
    root = Path(__file__).resolve().parents[1]
    repository = tmp_path / 'repository'
    repository.mkdir()
    subprocess.run(['git', 'init', '-q', str(repository)], check=True)
    marker = tmp_path / 'outside-marker'
    extension = tmp_path / 'fsmonitor'
    extension.write_text('#!/bin/sh\nprintf touched > ' + str(marker) + '\nprintf "fixture-token\\0"\n')
    extension.chmod(0o700)
    subprocess.run(['git', '-C', str(repository), 'config', 'core.fsmonitor', str(extension)], check=True)
    arguments = ([str(root / 'ghost_hermes_pm/shared_source_reader.py'), str(repository)] if entry == 'reader'
        else [str(root / 'ghost_hermes_pm/repository_test_runner.py'), str(repository), '--state'])
    result = subprocess.run([sys.executable, '-I', *arguments], capture_output=True, text=True)
    assert result.returncode == 0, result.stderr
    assert not marker.exists(), 'A privileged fingerprint must not execute a repository Git extension.'


def test_privileged_source_reads_disable_clean_filter_without_changing_user_git(tmp_path):
    repository = tmp_path / 'repository'
    repository.mkdir()
    subprocess.run(['git', 'init', '-q', str(repository)], check=True)
    marker = tmp_path / 'outside-filter-marker'
    extension = tmp_path / 'clean-filter'
    extension.write_text('#!/bin/sh\nprintf touched > ' + str(marker) + '\ncat\n')
    extension.chmod(0o700)
    (repository / '.gitattributes').write_text('*.txt filter=fixture\n')
    (repository / 'feature.txt').write_text('original\n')
    subprocess.run(['git', '-C', str(repository), 'config', 'filter.fixture.clean', str(extension)], check=True)
    subprocess.run(['git', '-C', str(repository), 'add', '.'], check=True)
    (repository / 'feature.txt').write_text('changed\n')
    subprocess.run(['git', '-C', str(repository), 'status', '--porcelain=v1'], capture_output=True, check=True)
    assert marker.exists(), 'The external-marker control must demonstrate the executable extension.'
    marker.unlink()
    config_before = (repository / '.git/config').read_bytes()
    reader = Path(__file__).resolve().parents[1] / 'ghost_hermes_pm/shared_source_reader.py'
    result = subprocess.run([sys.executable, '-I', str(reader), str(repository)], capture_output=True, text=True)
    assert result.returncode == 0, result.stderr
    assert not marker.exists() and (repository / '.git/config').read_bytes() == config_before


@pytest.mark.parametrize('unknown_file', [False, True])
def test_shared_github_reference_is_private_native_tmp_and_keeps_host_environment(github_source, unknown_file):
    from types import SimpleNamespace
    from ghost_hermes_pm.shared_repository import prepare_shared_github, cleanup_shared_github, _prompt
    source, original = github_source
    before, environment = original.read_bytes(), dict(os.environ)
    saved = []
    intake = SimpleNamespace(github=source, _save_execution=lambda record, fields=None: saved.append(fields))
    record = {'id': 'fixture-work', 'target': {'repository': 'fixture-user/fixture'},
        'issue': {'url': 'https://github.com/fixture-user/fixture/issues/7', 'title': 'Fixture', 'body': 'Fixture scope'},
        'dsh_execution': {'generation': 'fixture-generation'}}
    reference = prepare_shared_github(intake, record)
    directory = Path(reference['config_dir'])
    try:
        assert directory.resolve().is_relative_to(Path('/tmp').resolve())
        assert directory.stat().st_mode & 0o077 == 0 and (directory / 'hosts.yml').stat().st_mode & 0o077 == 0
        prompt = _prompt(intake, record)
        assert str(directory) in prompt and str(original.parent) not in prompt
        assert 'synthetic-configured-account-token' not in prompt
        assert original.read_bytes() == before and dict(os.environ) == environment
        assert 'DSH_HOME' not in prompt
        record['dsh_execution']['generation'] = 'different-generation'
        with pytest.raises(ManagementError):
            cleanup_shared_github(intake, record)
        assert directory.exists() and original.read_bytes() == before
        record['dsh_execution']['generation'] = 'fixture-generation'
        if unknown_file:
            note = directory / 'owner-note.txt'
            note.write_text('Synthetic owner content must remain.\n')
            note.chmod(0o600)
            with pytest.raises(ManagementError):
                cleanup_shared_github(intake, record)
            assert note.read_text() == 'Synthetic owner content must remain.\n'
            assert record['dsh_execution']['github_cleanup']['status'] == 'unconfirmed'
            assert directory.exists() and (directory / 'hosts.yml').exists()
            note.unlink()  # Remove only this test's own deliberately injected fixture.
        assert cleanup_shared_github(intake, record) == {'status': 'removed'}
        assert not directory.exists() and original.read_bytes() == before
        assert dict(os.environ) == environment
    finally:
        from ghost_hermes_pm.repository_github import cleanup_github_execution
        if directory.exists():
            cleanup_github_execution(reference, 'fixture-generation')


@pytest.mark.parametrize('scenario', ['terminal', 'observer_restart', 'stop', 'reconcile_intent', 'prepare_continue', 'continue_restart', 'continue_unknown', 'continue_triage', 'continue_claim_race', 'continue_native_restore', 'question_material', 'continue_tokens_uncapped', 'continue_max_tokens', 'continue_max_tokens_unknown', 'continue_max_tokens_last_slot', 'renew_window', 'renew_window_unknown', 'renew_window_continue', 'resume_completed_window', 'renew_window_again', 'owner_context', 'owner_maintenance', 'supervise_slice', 'supervise_takeover', 'supervise_takeover_nonzero', 'supervise_takeover_reclaimed', 'supervise_takeover_source', 'supervise_takeover_sid', 'supervise_takeover_native', 'supervise_takeover_ledger', 'supervise_takeover_card', 'supervise_expired_window', 'runtime_metadata', 'qa_preinput_retry', 'supervision_checkpoints', 'qa_request_only_resume'])
def test_original_kanban_worker_attaches_shared_desktop_through_public_entry(tmp_path, scenario):
    root = Path(__file__).resolve().parents[1]
    sdk = Path(os.environ['HERMES_TEST_SDK_ROOT'])
    sdk_before = source_snapshot(sdk)
    home = tmp_path / 'home'
    plugin = home / 'plugins/ghost-hermes-pm'
    plugin.mkdir(parents=True)
    home.chmod(0o700)
    for name in ('__init__.py', 'plugin.yaml', 'ghost_hermes_pm'):
        if (root / name).is_dir():
            shutil.copytree(root / name, plugin / name, ignore=shutil.ignore_patterns('__pycache__'))
        else:
            shutil.copy2(root / name, plugin / name)
    if scenario == 'supervise_slice' or scenario.startswith('supervise_takeover'):
        production = root / 'ghost_hermes_pm/shared_repository.py'
        copied = plugin / 'ghost_hermes_pm/shared_repository.py'
        declaration = 'SUPERVISE_OBSERVATION_SLICE_SECONDS = 300'
        original = copied.read_text()
        assert original.count(declaration) == 1 and production.read_text().count(declaration) == 1
        copied.write_text(original.replace(declaration, 'SUPERVISE_OBSERVATION_SLICE_SECONDS = 0.5', 1))
        (tmp_path / 'slice-clock-transform.json').write_text(json.dumps({
            'production_sha256': hashlib.sha256(production.read_bytes()).hexdigest(),
            'original_sha256': hashlib.sha256(original.encode()).hexdigest(),
            'fixture_sha256': hashlib.sha256(copied.read_bytes()).hexdigest(),
            'path': str(copied), 'delta': [declaration, 'SUPERVISE_OBSERVATION_SLICE_SECONDS = 0.5']}))
    env = {**os.environ, 'HERMES_HOME': str(home), 'HERMES_KANBAN_HOME': str(home),
        'HERMES_BUNDLED_PLUGINS': str(home / 'empty-bundled'), 'PYTHONPATH': str(sdk),
        'PYTHONDONTWRITEBYTECODE': '1'}
    result = subprocess.run([sys.executable, str(Path(__file__).with_name('shared_repository_runner.py')),
        str(tmp_path), scenario], cwd=tmp_path, env=env, capture_output=True, text=True,
        timeout=65 if scenario == 'supervise_slice' or scenario.startswith('supervise_takeover') else 100)
    (tmp_path / 'public-runner-output.log').write_text(result.stdout + result.stderr)
    assert result.returncode == 0, result.stdout + result.stderr
    assert source_snapshot(sdk) == sdk_before
    assert json.loads((tmp_path / 'shared-worker-result.json').read_text())['status'] == 'passed'


@pytest.mark.parametrize('change', ['none', 'source_changed', 'failed_test', 'wrong_command', 'non_test', 'true', 'missing_test', 'failing', 'zero_tests'])
def test_shared_acceptance_reads_fixed_source_and_original_test_settlement(tmp_path, monkeypatch, change):
    from ghost_hermes_pm.simple_development import RepositoryIntake
    from ghost_hermes_pm.repository_acceptance import accept_repository_work, repository_source_state
    repo = tmp_path / 'repository'
    repo.mkdir()
    subprocess.run(['git', 'init', '-q', '-b', 'fixture-delivery', str(repo)], check=True)
    test_file = 'feature.test.mjs'
    (repo / test_file).write_text("console.log('900 passed');\n" if change == 'zero_tests' else
        "import test from 'node:test'; import assert from 'node:assert/strict';\n"
        "test('real assertion',()=>{assert.equal(40+2," + ('41' if change in {'non_test', 'failing'} else '42') + ")});\n")
    subprocess.run(['git', '-C', str(repo), 'add', '.'], check=True)
    subprocess.run(['git', '-C', str(repo), '-c', 'user.name=Fixture', '-c', 'user.email=fixture@example.invalid',
        'commit', '-qm', 'Synthetic fixed test source'], check=True)
    source = repository_source_state({'repo_path': str(repo)})
    from ghost_hermes_pm.shared_repository import verification_command
    from types import SimpleNamespace
    root = Path(__file__).resolve().parents[1]
    artifacts = tmp_path / 'artifacts'
    artifacts.mkdir(mode=0o700)
    verification = {'python': str(Path(sys.executable).resolve()), 'runner': str(root / 'ghost_hermes_pm/repository_test_runner.py'),
        'node': str(Path(shutil.which('node')).resolve())}
    verification.update({name + '_sha256': hashlib.sha256(Path(path).read_bytes()).hexdigest() for name, path in list(verification.items())})
    carrier = SimpleNamespace(config={'verification': verification})
    execution = {'session_id': 'fixture-session', 'generation': 'fixture-generation',
        'verification': {'artifact_root': str(artifacts), 'nonce': 'fixture-nonce'}}
    test_files = [test_file]
    command = verification_command(carrier.config, execution, str(repo), test_files)
    if change == 'non_test':
        command = '/usr/bin/printf "900 passed\\n"'
    elif change == 'true':
        command = '/usr/bin/true'
    elif change == 'missing_test':
        command = command.replace(test_file, 'nonexistent.test.mjs')
        test_files = ['nonexistent.test.mjs']
    actual_test = subprocess.run(['/bin/bash', '-c', command], cwd=repo,
        env={**os.environ, 'PYTHONDONTWRITEBYTECODE': '1'}, capture_output=True, text=True)
    assert actual_test.returncode == (1 if change in {'failing', 'missing_test'} else 0), actual_test.stderr
    issue = {'url': 'https://github.com/fixture-user/fixture/issues/7', 'title': 'Verify synthetic feature',
        'body': '- [ ] Verify the fixture feature.', 'updated_at': '2026-10-10T00:00:00Z'}
    state = tmp_path / 'github.json'
    state.write_text(json.dumps({'url': issue['url'], 'title': issue['title'], 'body': issue['body'],
        'updatedAt': issue['updated_at'], 'state': 'OPEN'}))
    gh = tmp_path / 'gh'
    gh.write_text('#!' + sys.executable + '\nimport json,os,sys\nfrom pathlib import Path\n'
        'a=sys.argv[1:]\n'
        'if a[:2]==["auth","switch"]: pass\n'
        'elif a[:2]==["api","user"]: print("fixture-user")\n'
        'elif a[:2]==["issue","view"]: print(Path(os.environ["SHARED_FIXTURE_GH"]).read_text())\n'
        'elif "/sub_issues" in a[1]: print("[]")\n'
        'else: sys.exit(2)\n')
    gh.chmod(0o700)
    monkeypatch.setenv('PATH', str(tmp_path) + os.pathsep + os.environ['PATH'])
    monkeypatch.setenv('SHARED_FIXTURE_GH', str(state))
    gh_config = tmp_path / 'gh-config'
    gh_config.mkdir(mode=0o700)
    intake = RepositoryIntake('fixture:owner', {}, {'github_account': 'fixture-user',
        'github_config_dir': str(gh_config)}, str(tmp_path / 'private'))
    snapshot = {key: source[key] for key in ('head', 'source_digest', 'workspace_status')}
    receipt = {'name': 'bash', 'kind': 'foreground', 'execution_admission': 'admitted',
        'generation': 'fixture-generation', 'session_id': 'fixture-session', 'call_id': 'fixture-test',
        'is_error': actual_test.returncode != 0, 'exit_code': actual_test.returncode, 'timed_out': False, 'aborted': False,
        'command_sha256': hashlib.sha256(command.encode()).hexdigest(), 'cwd': str(repo),
        'sandbox': {'mode': 'workspace-write'}, 'source_before': snapshot, 'source_after': snapshot,
        'stdout': {'sha256': hashlib.sha256(actual_test.stdout.encode()).hexdigest(), 'byte_length': len(actual_test.stdout.encode()), 'truncated': False},
        'stderr': {'sha256': hashlib.sha256(actual_test.stderr.encode()).hexdigest(), 'byte_length': len(actual_test.stderr.encode()), 'truncated': False}}
    if change == 'source_changed':
        receipt['source_before'] = dict(snapshot, source_digest='unknown-source')
    elif change == 'failed_test':
        receipt['exit_code'] = 1
    elif change == 'wrong_command':
        receipt['command_sha256'] = hashlib.sha256(b'echo ignored').hexdigest()
    report = {'issue_updated_at': issue['updated_at'], 'source_commit': source['head'],
        'criteria': [{'text': 'Verify the fixture feature.', 'test_call_ids': ['fixture-test']}],
        'fine_issue_urls': [], 'sync_branches': [], 'leftovers': [], 'test_files': test_files}
    events = [{'seq': 0, 'type': 'turn/start', 'data': {'turn': 1}},
        {'seq': 1, 'type': 'tool/call', 'data': {'callId': 'fixture-test', 'name': 'bash', 'arguments': {'command': command}}},
        {'seq': 2, 'type': 'tool/result', 'data': {'message': {'toolCallId': 'fixture-test', 'isError': actual_test.returncode != 0,
            'content': [{'type': 'text', 'text': actual_test.stdout}]}}},
        {'seq': 3, 'type': 'assistant/message', 'data': {'turn': 1, 'message': {'content': [{'type': 'text',
            'text': 'HERMES_REPOSITORY_DELIVERY_JSON ' + json.dumps(report)}]}}},
        {'seq': 4, 'type': 'turn/end', 'data': {'turn': 1, 'reason': {'kind': 'completed'}}}]
    record = {'id': 'fixture-work', 'card_id': 'fixture-card', 'issue': issue,
        'target': {'repo_path': str(repo), 'repository': 'fixture-user/fixture'},
        'dsh_execution': {'mode': 'shared_native', 'state': 'awaiting_acceptance', 'generation': 'fixture-generation',
            'session_id': 'fixture-session', 'baseline': source, 'tool_receipts': [receipt], 'jobs': [],
            'verification': execution['verification']}}
    intake._save(record)
    result = asyncio.run(accept_repository_work(intake, record, carrier, events,
        {'inbox': {'next-turn': [], 'next-step': []}}, intake.generation))
    assert (result['status'] == 'accepted') == (change == 'none'), json.dumps(result)
    if change == 'source_changed':
        assert 'test_source_unconfirmed' in result['unmet']
    elif change in {'non_test', 'true', 'zero_tests'}:
        assert 'test_runner_unconfirmed' in result['unmet']
    elif change != 'none':
        assert 'test_evidence_missing' in result['unmet']
