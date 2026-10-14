"""DSH adapter public seam tested against native HTTP/Remote-mux shapes."""
from ghost_hermes_pm.dsh import DshRemoteAdapter, repository_fingerprint
from ghost_hermes_pm.manager import ManagementError
import pytest
import time
import threading
from concurrent.futures import ThreadPoolExecutor
import json
import os
from pathlib import Path
import subprocess
from dsh_fixture_server import remote_peer


def test_original_dsh_connect_and_list_do_not_create_an_executor():
    with remote_peer() as (url, peer):
        adapter = DshRemoteAdapter(url, cookie='synthetic-auth=value', service_ref='local:synthetic-service', timeout=1)
        try:
            connection = adapter.connect()
            assert connection['transport'] == 'desktop_http_mux'
            assert adapter.loaded_threads() == ['session-fixture']
            assert [call['method'] for call in peer['calls']] == ['session/list']
        finally:
            adapter.close()


def admitted_adapter(url):
    repository = {'worktree': '/synthetic/project', 'logical_id': 'fixture-logical', 'test_artifact_paths': []}
    def verifier(connection, actual):
        return {'generation': connection['generation'], 'service_id': connection['service_id'],
                'repository_fingerprint': repository_fingerprint(actual), 'runtime_roots': [actual['worktree']],
                'permission_profile': 'fixture-host-policy', 'policy_digest': 'fixture-policy',
                'platform_enforcement': 'fixture-kernel-enforcement', 'tool_paths': 'fixture-tools',
                'task_start': 'fixture-admission', 'manual_execution_coverage': 'fixture-original-input-fence',
                'startup_exclusive_input': {'kind': 'exclusive_original_input', 'scope': 'create_and_first_input',
                                           'evidence': 'fixture-startup-fence', 'generation': connection['generation'],
                                           'service_id': connection['service_id'],
                                           'repository_fingerprint': repository_fingerprint(actual),
                                           'thread_id': None, 'turn_id': None},
                'exclusive_input': {'kind': 'exclusive_original_input', 'evidence': 'fixture-fence',
                                    'generation': connection['generation'], 'service_id': connection['service_id'],
                                    'repository_fingerprint': repository_fingerprint(actual),
                                    'thread_id': verifier.target[0], 'turn_id': verifier.target[1]},
                'task_control': {'continue': 'fixture-continue', 'append': 'fixture-append',
                                 'stop': 'fixture-stop', 'human_response': 'fixture-answer'}}
    verifier.target = (None, None)
    return DshRemoteAdapter(url, cookie='synthetic-auth=value', service_ref='local:synthetic-service',
                            verifier=verifier, timeout=0.15), repository


def test_dsh_inbox_acceptance_does_not_claim_a_started_turn_or_replay_input():
    with remote_peer() as (url, peer):
        adapter, repository = admitted_adapter(url)
        try:
            proof = adapter.verify_start(repository)
            response = adapter.start_thread(repository, proof)
            adapter.verify_thread(response, repository, proof)
            adapter.verifier.target = (response['thread']['id'], None)
            with pytest.raises(ManagementError) as error:
                adapter.start_turn(response['thread']['id'], 'synthetic request')
            assert error.value.code == 'outcome_unknown'
            with pytest.raises(ManagementError):
                adapter.start_turn(response['thread']['id'], 'synthetic request')
            assert len(peer['prompts']) == 1
        finally:
            adapter.close()


def test_dsh_prompt_is_correlated_to_actual_durable_input_and_turn():
    with remote_peer(behavior={'admit_prompt': True}) as (url, peer):
        adapter, repository = admitted_adapter(url)
        try:
            proof = adapter.verify_start(repository)
            response = adapter.start_thread(repository, proof)
            adapter.verify_thread(response, repository, proof)
            adapter.verifier.target = (response['thread']['id'], None)
            result = adapter.start_turn(response['thread']['id'], 'synthetic request')
            assert result['turn']['id'] == 'dsh-turn:1'
            assert result['turn']['status'] == 'inProgress'
            assert result['requestId'] == peer['prompts'][0]['requestId']
            assert [call['method'] for call in peer['calls'] if call['method'] not in {'session/list'}][-1] == 'session/prompt'
        finally:
            adapter.close()


def test_raw_dsh_control_cannot_bypass_task_admission_with_authority_metadata():
    with remote_peer() as (url, peer):
        adapter = DshRemoteAdapter(url, cookie='synthetic-auth=value', service_ref='local:synthetic-service', timeout=1)
        try:
            adapter.connect()
            with pytest.raises(ManagementError) as error:
                adapter._call('session/cancel', {'request': {'sessionId': 'session-fixture'},
                                                '_authority': {'thread_id': 'session-fixture', 'turn_id': 'dsh-turn:1'}})
            assert error.value.code == 'capability_unverified'
            assert peer['calls'] == []
        finally:
            adapter.close()


@pytest.mark.parametrize('behavior,code', [
    ({'wrong_id': 'session/list'}, 'capability_unverified'),
    ({'omit_value': 'session/list'}, 'capability_unverified'),
    ({'reject': 'session/list'}, 'service_rejected'),
    ({'redirect': 'session/list'}, 'unavailable'),
])
def test_dsh_read_response_requires_its_actual_correlated_native_result(behavior, code):
    with remote_peer(behavior=behavior) as (url, peer):
        adapter = DshRemoteAdapter(url, cookie='synthetic-auth=value', service_ref='local:synthetic-service', timeout=1)
        try:
            with pytest.raises(ManagementError) as error:
                adapter.loaded_threads()
            assert error.value.code == code
            assert len(peer['calls']) == 1
        finally:
            adapter.close()


@pytest.mark.parametrize('url', ['https://127.0.0.1:8000', 'http://localhost:8000',
                                'http://127.0.0.1:8000/api', 'http://user:pass@127.0.0.1:8000',
                                'http://127.0.0.1:8000/?redirect=elsewhere'])
def test_dsh_endpoint_cannot_forward_credentials_to_an_unregistered_target(url):
    with pytest.raises(ManagementError) as error:
        DshRemoteAdapter(url, cookie='synthetic-auth=value', service_ref='local:synthetic-service')
    assert error.value.code == 'invalid_change'


def test_dsh_inactive_generation_does_not_reopen_or_control_an_original_service():
    with remote_peer() as (url, peer):
        adapter = DshRemoteAdapter(url, cookie='synthetic-auth=value', service_ref='local:synthetic-service', timeout=1)
        adapter.connect()
        adapter.close()
        with pytest.raises(ManagementError) as error:
            adapter.connect()
        assert error.value.code == 'unavailable'
        assert peer['calls'] == []


def test_dsh_unknown_mutation_response_is_preserved_without_replaying():
    with remote_peer(behavior={'drop': 'session/prompt'}) as (url, peer):
        adapter, repository = admitted_adapter(url)
        try:
            proof = adapter.verify_start(repository)
            response = adapter.start_thread(repository, proof)
            adapter.verify_thread(response, repository, proof)
            adapter.verifier.target = (response['thread']['id'], None)
            with pytest.raises(ManagementError) as error:
                adapter.start_turn(response['thread']['id'], 'synthetic request')
            assert error.value.code == 'outcome_unknown'
            with pytest.raises(ManagementError):
                adapter.start_turn(response['thread']['id'], 'synthetic request')
            assert len(peer['prompts']) == 1
        finally:
            adapter.close()


def test_cancel_does_not_claim_exact_turn_or_related_process_exit_from_acceptance():
    with remote_peer() as (url, peer):
        adapter, repository = admitted_adapter(url)
        try:
            proof = adapter.verify_start(repository)
            adapter.bind_repository('session-fixture', repository, proof)
            adapter.verifier.target = ('session-fixture', 'dsh-turn:1')
            assert adapter.interrupt_turn('session-fixture', 'dsh-turn:1') == {'accepted': True}
            with pytest.raises(ManagementError) as error:
                adapter.verify_process_coverage({'repository': repository}, {'turn_id': 'dsh-turn:1'})
            assert error.value.code == 'capability_unverified'
            assert [call['method'] for call in peer['calls']][-1] == 'session/cancel'
        finally:
            adapter.close()


def test_dsh_cancel_without_exclusive_original_input_does_not_send_native_control():
    with remote_peer() as (url, peer):
        adapter, repository = admitted_adapter(url)
        try:
            proof = adapter.verify_start(repository)
            adapter.bind_repository('session-fixture', repository, proof)
            # A receipt for another target cannot give the adapter native compare-and-swap.
            adapter.verifier.target = ('other-session', 'dsh-turn:1')
            with pytest.raises(ManagementError) as error:
                adapter.interrupt_turn('session-fixture', 'dsh-turn:1')
            assert error.value.code == 'capability_unverified'
            assert all(call['method'] != 'session/cancel' for call in peer['calls'])
        finally:
            adapter.close()


def test_dsh_partial_journal_cannot_claim_complete_turn_history():
    gap = [{'type': 'event', 'event': {'type': 'turn/start', 'seq': 3, 'time': 1, 'data': {'turn': 1}}}]
    with remote_peer(behavior={'records': gap}) as (url, peer):
        adapter = DshRemoteAdapter(url, cookie='synthetic-auth=value', service_ref='local:synthetic-service', timeout=1)
        try:
            with pytest.raises(ManagementError) as error:
                adapter.read_thread('session-fixture')
            assert error.value.code == 'capability_unverified'
        finally:
            adapter.close()


def test_dsh_owned_client_does_not_log_cookie_or_change_host_logger(caplog):
    import logging
    host_logger = logging.getLogger()
    before = (host_logger.level, tuple(host_logger.handlers))
    with caplog.at_level(logging.DEBUG):
        with remote_peer() as (url, peer):
            adapter = DshRemoteAdapter(url, cookie='synthetic-auth=value', service_ref='local:synthetic-service', timeout=1)
            try:
                adapter.loaded_threads()
            finally:
                adapter.close()
        assert 'synthetic-auth=value' not in caplog.text
        assert '/synthetic/home' not in caplog.text
        assert url not in caplog.text
        assert url.removeprefix('http://') not in caplog.text
    assert (host_logger.level, tuple(host_logger.handlers)) == before


def test_original_dsh_question_answer_names_exact_live_waterfall_and_actual_tool_turn():
    question = {'type': 'waterfall', 'event': 'user-questions/request', 'eventId': 'question-fixture',
                'agentId': 'session-fixture', 'request': {'questions': [{'id': 'choice', 'question': 'Synthetic choice?'}],
                                                       'wait': {'callId': 'call-fixture'}}}
    records = [
        {'type': 'event', 'event': {'type': 'turn/start', 'seq': 0, 'time': 1, 'data': {'turn': 1}}},
        {'type': 'event', 'event': {'type': 'tool/call', 'seq': 1, 'time': 1,
                                  'data': {'turn': 1, 'step': 1, 'callId': 'call-fixture', 'name': 'ask_user_question', 'arguments': '{}'}}},
    ]
    with remote_peer(behavior={'remote_events': [question], 'records': records, 'defer_remote_events': True}) as (url, peer):
        adapter, repository = admitted_adapter(url)
        try:
            proof = adapter.verifier(adapter.connect(), repository)
            adapter.bind_repository('session-fixture', repository, proof)
            adapter.verifier.target = ('session-fixture', 'dsh-turn:1')
            adapter.waterfall_authority = lambda session_id, turn_id: session_id == 'session-fixture' and turn_id == 'dsh-turn:1'
            adapter.waterfall_question_supported = lambda envelope: True
            peer['emit_remote'].set()
            pending, deadline = [], time.monotonic() + 1
            while not pending and time.monotonic() < deadline:
                pending = adapter.server_requests('session-fixture')
                if not pending:
                    time.sleep(0.01)
            assert pending[0]['envelope']['params']['turnId'] == 'dsh-turn:1'
            assert pending[0]['envelope']['params']['native_response_available'] is True
            envelope = pending[0]['envelope']
            adapter.respond_server_request(envelope['id'], envelope, {'answers': [{'id': 'choice', 'selected': [], 'custom': 'synthetic answer'}]})
            reply = [call for call in peer['calls'] if call['method'] == '$events/result'][0]
            assert reply['payload']['args'] == {'clientId': 'fixture-client', 'eventId': 'question-fixture',
                                               'outcome': {'kind': 'result', 'value': {'answers': [
                                                   {'id': 'choice', 'selected': [], 'custom': 'synthetic answer'}]}}}
            with pytest.raises(ManagementError):
                adapter.respond_server_request(envelope['id'], envelope, {'answers': []})
        finally:
            adapter.close()


def test_original_dsh_history_turn_is_read_from_the_durable_journal():
    with remote_peer() as (url, peer):
        adapter = DshRemoteAdapter(url, cookie='synthetic-auth=value', service_ref='local:synthetic-service', timeout=1)
        try:
            thread = adapter.read_thread('session-fixture')
            assert thread['cwd'] == '/synthetic/project'
            assert thread['turns'][0]['id'] == 'dsh-turn:1'
            assert thread['turns'][0]['status'] == 'completed'
            assert thread['turns'][0]['items'][0]['text'] == 'synthetic task'
            assert adapter.background_terminals('session-fixture') == []
            assert all(call['method'] != 'session/create' for call in peer['calls'])
        finally:
            adapter.close()


def test_dsh_summary_and_shell_settlement_are_read_without_inventing_exit_success():
    records = [
        {'type': 'event', 'event': {'type': 'turn/start', 'seq': 0, 'time': 1, 'data': {'turn': 1}}},
        {'type': 'event', 'event': {'type': 'assistant/message', 'seq': 1, 'time': 1, 'data': {
            'turn': 1, 'step': 1, 'message': {'id': 'message-fixture', 'role': 'assistant',
            'content': [{'type': 'text', 'text': 'Synthetic progress.'}], 'source': {'kind': 'model', 'provider': 'synthetic', 'model': 'synthetic'}}, 'stream': []}}},
        {'type': 'event', 'event': {'type': 'tool/call', 'seq': 2, 'time': 1, 'data': {
            'turn': 1, 'step': 1, 'callId': 'bash-fixture', 'name': 'bash', 'arguments': '{"command":"pytest synthetic_test.py"}'}}},
        {'type': 'event', 'event': {'type': 'tool/result', 'seq': 3, 'time': 2, 'data': {
            'turn': 1, 'step': 1, 'message': {'id': 'result-fixture', 'role': 'tool', 'toolCallId': 'bash-fixture',
            'source': {'kind': 'tool'}, 'content': [{'type': 'text', 'text': '1 passed'}], 'isError': False}}}},
        {'type': 'event', 'event': {'type': 'turn/end', 'seq': 4, 'time': 3, 'data': {'turn': 1, 'reason': {'kind': 'completed'}}}},
    ]
    with remote_peer(behavior={'records': records}) as (url, peer):
        adapter = DshRemoteAdapter(url, cookie='synthetic-auth=value', service_ref='local:synthetic-service', timeout=1)
        try:
            turn = adapter.read_thread('session-fixture')['turns'][0]
            assert turn['items'][0] == {'type': 'agentMessage', 'id': 'message-fixture', 'text': 'Synthetic progress.',
                                       'status': 'completed', 'nativeSeq': 1, 'nativeType': 'assistant/message'}
            command = turn['items'][1]
            assert command['type'] == 'commandExecution'
            assert command['status'] == 'completed'
            assert command['command'] == 'pytest synthetic_test.py'
            assert command['cwd'] == '/synthetic/project'
            assert command['aggregatedOutput'] == '1 passed'
            assert command['exitCode'] is None
        finally:
            adapter.close()


def test_actual_dsh_job_roster_distinguishes_active_background_from_process_coverage():
    jobs = [{'id': 'bash-fixture', 'kind': 'bash', 'label': 'synthetic command', 'owner': 'session-fixture',
             'status': 'running', 'startedAt': 1, 'output': {'total': 0, 'earliest': 0}}]
    with remote_peer(behavior={'jobs': jobs}) as (url, peer):
        adapter = DshRemoteAdapter(url, cookie='synthetic-auth=value', service_ref='local:synthetic-service', timeout=1)
        try:
            assert adapter.background_terminals('session-fixture') == jobs
            with pytest.raises(ManagementError) as error:
                adapter.verify_process_coverage({}, {})
            assert error.value.code == 'capability_unverified'
        finally:
            adapter.close()


def test_native_dsh_oversized_response_does_not_bypass_the_frame_bound():
    with remote_peer(behavior={'oversized': 'session/list'}) as (url, peer):
        adapter = DshRemoteAdapter(url, cookie='synthetic-auth=value', service_ref='local:synthetic-service', timeout=1)
        try:
            with pytest.raises(ManagementError) as error:
                adapter.loaded_threads()
            assert error.value.code == 'unavailable'
        finally:
            adapter.close()


def test_original_dsh_carrier_loss_requires_a_new_admitted_generation():
    with remote_peer(behavior={'disconnect_events': True}) as (url, peer):
        adapter = DshRemoteAdapter(url, cookie='synthetic-auth=value', service_ref='local:synthetic-service', timeout=1)
        try:
            adapter.connect()
            deadline = time.monotonic() + 1
            while time.monotonic() < deadline:
                try:
                    adapter.connect()
                except ManagementError:
                    break
                time.sleep(0.01)
            with pytest.raises(ManagementError) as error:
                adapter.loaded_threads()
            assert error.value.code == 'unavailable'
            assert sum(frame.get('endpoint') == '$events' for frame in peer['streams']) == 1
        finally:
            adapter.close()


def test_fresh_dsh_task_starts_from_its_dedicated_creation_and_first_input_lease():
    with remote_peer(behavior={'admit_prompt': True}) as (url, peer):
        adapter, repository = admitted_adapter(url)
        original_verifier = adapter.verifier
        adapter.verifier = lambda connection, actual: {
            key: value for key, value in original_verifier(connection, actual).items()
            if key not in {'task_control', 'exclusive_input'}}
        try:
            proof = adapter.verify_start(repository)
            response = adapter.start_thread(repository, proof)
            adapter.verify_thread(response, repository, proof)
            result = adapter.start_turn(response['thread']['id'], 'synthetic initial request')
            assert result['turn']['id'] == 'dsh-turn:1'
            assert len(peer['prompts']) == 1
            with pytest.raises(ManagementError):
                adapter.interrupt_turn(response['thread']['id'], result['turn']['id'])
            assert all(call['method'] != 'session/cancel' for call in peer['calls'])
        finally:
            adapter.close()


@pytest.mark.parametrize('behavior', [
    {'create_foreign_id': True},
    {'inbox': {'next-turn': [{'id': 'foreign-input'}], 'next-step': []}},
])
def test_first_dsh_input_refuses_foreign_creation_or_a_nonempty_native_inbox(behavior):
    with remote_peer(behavior=behavior) as (url, peer):
        adapter, repository = admitted_adapter(url)
        try:
            proof = adapter.verify_start(repository)
            response = adapter.start_thread(repository, proof)
            with pytest.raises(ManagementError) as error:
                adapter.verify_thread(response, repository, proof)
            assert error.value.code == 'binding_conflict'
            with pytest.raises(ManagementError):
                adapter.start_turn(response['thread']['id'], 'synthetic initial input')
            assert peer['prompts'] == []
        finally:
            adapter.close()


def test_unknown_dsh_creation_is_not_replayed_in_the_same_generation():
    with remote_peer(behavior={'drop': 'session/create'}) as (url, peer):
        adapter, repository = admitted_adapter(url)
        try:
            proof = adapter.verify_start(repository)
            with pytest.raises(ManagementError) as error:
                adapter.start_thread(repository, proof)
            assert error.value.code == 'outcome_unknown'
            with pytest.raises(ManagementError):
                adapter.start_thread(repository, proof)
            assert sum(call['method'] == 'session/create' for call in peer['calls']) == 1
        finally:
            adapter.close()


def test_unknown_ignorable_dsh_json_payload_is_retained_without_terminal_claims():
    records = [
        {'type': 'event', 'event': {'type': 'turn/start', 'seq': 0, 'time': 1, 'data': {'turn': 1}}},
        {'type': 'event', 'event': {'type': 'extension/opaque', 'seq': 1, 'time': 1,
                                  'ignorable': True, 'data': [None, 'synthetic-value', 3]}},
        {'type': 'event', 'event': {'type': 'turn/end', 'seq': 2, 'time': 2, 'data': {'turn': 1, 'reason': {'kind': 'completed'}}}},
    ]
    with remote_peer(behavior={'records': records}) as (url, peer):
        adapter = DshRemoteAdapter(url, cookie='synthetic-auth=value', service_ref='local:synthetic-service', timeout=1)
        try:
            thread = adapter.read_thread('session-fixture')
            assert thread['source'] == 'registered_backend'
            item = thread['turns'][0]['items'][0]
            assert item['type'] == 'dshEvent'
            assert item['nativeData'] == [None, 'synthetic-value', 3]
            assert item['status'] == 'unknown'
        finally:
            adapter.close()


def test_known_dsh_execution_payload_cannot_use_opaque_scalar_shape():
    records = [{'type': 'event', 'event': {'type': 'turn/start', 'seq': 0, 'time': 1,
                                         'data': ['not-a-producer-turn-payload']}}]
    with remote_peer(behavior={'records': records}) as (url, peer):
        adapter = DshRemoteAdapter(url, cookie='synthetic-auth=value', service_ref='local:synthetic-service', timeout=1)
        try:
            with pytest.raises(ManagementError) as error:
                adapter.read_thread('session-fixture')
            assert error.value.code == 'capability_unverified'
        finally:
            adapter.close()


def test_native_dsh_steering_preserves_each_admitted_input_in_its_session_journal():
    with remote_peer(behavior={'admit_prompt': True}) as (url, peer):
        adapter, repository = admitted_adapter(url)
        try:
            proof = adapter.verify_start(repository)
            created = adapter.start_thread(repository, proof)
            adapter.verify_thread(created, repository, proof)
            session_id = created['thread']['id']
            started = adapter.start_turn(session_id, 'synthetic first task')
            adapter.verifier.target = (session_id, started['turn']['id'])
            adapter.steer_turn(session_id, started['turn']['id'], 'synthetic extra requirement', 'fixture-append')
            messages = [item for item in adapter.read_thread(session_id)['turns'][0]['items'] if item['type'] == 'userMessage']
            assert [(item['text'], item['requestId']) for item in messages] == [
                ('synthetic first task', started['requestId']), ('synthetic extra requirement', 'fixture-append')]
            assert len(peer['prompts']) == 2
        finally:
            adapter.close()


def test_close_during_original_ready_cannot_publish_an_ended_connection():
    from ghost_hermes_pm.dsh_remote import NativeRemoteTransport
    arrived, release = threading.Event(), threading.Event()
    with remote_peer() as (url, peer):
        native = NativeRemoteTransport(url, 'synthetic-auth=value', 1)

        class ReadyBarrier:
            def __init__(self, stream):
                self.stream = stream

            def __iter__(self):
                return self

            def __next__(self):
                frame = next(self.stream)
                if isinstance(frame, dict) and frame.get('type') == 'ready':
                    arrived.set()
                    release.wait(timeout=3)
                return frame

            def close(self):
                self.stream.close()

        class OriginalTransport:
            def stream(self, endpoint, args):
                return ReadyBarrier(native.stream(endpoint, args))

            def request(self, method, args, rpc_id):
                return native.request(method, args, rpc_id)

            def close(self):
                native.close()

        adapter = DshRemoteAdapter(url, cookie='synthetic-auth=value', service_ref='local:synthetic-service',
                                   timeout=1, transport=OriginalTransport())
        with ThreadPoolExecutor(max_workers=2) as pool:
            connecting = pool.submit(adapter.connect)
            try:
                assert arrived.wait(timeout=2)
                closing = pool.submit(adapter.close)
                closing.result(timeout=2)
                release.set()
                with pytest.raises(ManagementError) as error:
                    connecting.result(timeout=2)
                assert error.value.code == 'unavailable'
                with pytest.raises(ManagementError):
                    adapter.connect()
                assert peer['calls'] == []
            finally:
                release.set()
                adapter.close()


def test_close_cancels_owned_original_http_read_without_waiting_for_host_response():
    received, release = threading.Event(), threading.Event()
    behavior = {'http_received': received, 'release_http_response': release, 'blocked_http_method': 'session/list'}
    with remote_peer(behavior=behavior) as (url, peer):
        adapter = DshRemoteAdapter(url, cookie='synthetic-auth=value', service_ref='local:synthetic-service', timeout=2)
        with ThreadPoolExecutor(max_workers=2) as pool:
            reading = pool.submit(adapter.loaded_threads)
            try:
                assert received.wait(timeout=2)
                pool.submit(adapter.close).result(timeout=1)
                with pytest.raises(ManagementError) as error:
                    reading.result(timeout=1)
                assert error.value.code == 'unavailable'
                release.set()
                with pytest.raises(ManagementError):
                    adapter.loaded_threads()
                assert len(peer['calls']) == 1
            finally:
                release.set()
                adapter.close()


@pytest.mark.parametrize('inbox,admitted', [
    ({'next-turn': [], 'next-step': []}, True),
    ({'next-turn': [{'id': 'foreign-input'}], 'next-step': []}, False),
    ({'nextTurn': [], 'nextStep': []}, False),
])
def test_first_input_uses_the_native_durable_inbox_projection_keys(inbox, admitted):
    with remote_peer(behavior={'inbox': inbox, 'admit_prompt': True}) as (url, peer):
        adapter, repository = admitted_adapter(url)
        try:
            proof = adapter.verify_start(repository)
            created = adapter.start_thread(repository, proof)
            if admitted:
                adapter.verify_thread(created, repository, proof)
                assert adapter.start_turn(created['thread']['id'], 'synthetic request')['turn']['id'] == 'dsh-turn:1'
            else:
                with pytest.raises(ManagementError) as error:
                    adapter.verify_thread(created, repository, proof)
                assert error.value.code == 'binding_conflict'
                assert peer['prompts'] == []
        finally:
            adapter.close()


@pytest.mark.parametrize('kind,data', [
    ('turn/end', {'turn': 1, 'reason': []}),
    ('tool/call', {'turn': 1, 'step': 1, 'callId': [], 'name': 'bash', 'arguments': '{}'}),
])
def test_malformed_nested_known_journal_payload_is_a_sanitized_unknown_observation(kind, data):
    records = [
        {'type': 'event', 'event': {'type': 'turn/start', 'seq': 0, 'time': 1, 'data': {'turn': 1}}},
        {'type': 'event', 'event': {'type': kind, 'seq': 1, 'time': 1, 'data': data}},
    ]
    with remote_peer(behavior={'records': records}) as (url, peer):
        adapter = DshRemoteAdapter(url, cookie='synthetic-auth=value', service_ref='local:synthetic-service', timeout=1)
        try:
            with pytest.raises(ManagementError) as error:
                adapter.read_thread('session-fixture')
            assert error.value.code == 'capability_unverified'
            assert '/synthetic/project' not in str(error.value)
        finally:
            adapter.close()


@pytest.mark.parametrize('method', ['session/prompt', 'session/cancel'])
def test_native_numeric_acceptance_cannot_claim_boolean_admission(method):
    with remote_peer(behavior={'admit_prompt': True, 'numeric_acceptance': method}) as (url, peer):
        adapter, repository = admitted_adapter(url)
        try:
            proof = adapter.verify_start(repository)
            created = adapter.start_thread(repository, proof)
            adapter.verify_thread(created, repository, proof)
            session_id = created['thread']['id']
            if method == 'session/prompt':
                operation = lambda: adapter.start_turn(session_id, 'synthetic initial request')
            else:
                started = adapter.start_turn(session_id, 'synthetic initial request')
                adapter.verifier.target = (session_id, started['turn']['id'])
                operation = lambda: adapter.interrupt_turn(session_id, started['turn']['id'])
            with pytest.raises(ManagementError) as error:
                operation()
            assert error.value.code == 'outcome_unknown'
        finally:
            adapter.close()


@pytest.mark.parametrize('change', ['settled', 'missing'])
def test_question_lifetime_is_rederived_from_the_current_original_tool_journal(change):
    question = {'type': 'waterfall', 'event': 'user-questions/request', 'eventId': 'question-fixture',
                'agentId': 'session-fixture', 'request': {'questions': [{'id': 'choice', 'question': 'Synthetic choice?'}],
                                                       'wait': {'callId': 'call-fixture'}}}
    records = [
        {'type': 'event', 'event': {'type': 'turn/start', 'seq': 0, 'time': 1, 'data': {'turn': 1}}},
        {'type': 'event', 'event': {'type': 'tool/call', 'seq': 1, 'time': 1,
                                  'data': {'turn': 1, 'step': 1, 'callId': 'call-fixture', 'name': 'ask_user_question', 'arguments': '{}'}}},
    ]
    with remote_peer(behavior={'remote_events': [question], 'records': records, 'defer_remote_events': True}) as (url, peer):
        adapter, repository = admitted_adapter(url)
        try:
            proof = adapter.verifier(adapter.connect(), repository)
            adapter.bind_repository('session-fixture', repository, proof)
            adapter.verifier.target = ('session-fixture', 'dsh-turn:1')
            adapter.waterfall_authority = lambda session_id, turn_id: session_id == 'session-fixture' and turn_id == 'dsh-turn:1'
            adapter.waterfall_question_supported = lambda envelope: True
            peer['emit_remote'].set()
            pending, deadline = [], time.monotonic() + 1
            while not pending and time.monotonic() < deadline:
                pending = adapter.server_requests('session-fixture')
                if not pending:
                    time.sleep(0.01)
            envelope = pending[0]['envelope']
            assert envelope['params']['native_response_available'] is True
            if change == 'settled':
                peer['journals']['session-fixture'].append({'type': 'event', 'event': {'type': 'tool/result', 'seq': 2,
                    'time': 2, 'data': {'turn': 1, 'step': 1, 'message': {'id': 'result-fixture', 'role': 'tool',
                    'toolCallId': 'call-fixture', 'source': {'kind': 'tool'}, 'content': [{'type': 'text', 'text': 'settled'}],
                    'isError': False}}}})
            else:
                peer['journals']['session-fixture'][1] = {'type': 'event', 'event': {
                    'type': 'extension/opaque', 'seq': 1, 'time': 1, 'ignorable': True, 'data': []}}
            refreshed = adapter.server_requests('session-fixture')[0]
            assert refreshed['envelope']['params']['native_response_available'] is False
            if change == 'missing':
                assert refreshed['envelope']['params']['turnId'] is None
            else:
                assert refreshed['state'] == 'resolved'
            with pytest.raises(ManagementError):
                adapter.respond_server_request(envelope['id'], envelope, {'answers': [{'id': 'choice', 'selected': [], 'custom': 'late'}]})
            assert all(call['payload']['args']['outcome']['kind'] == 'next'
                       for call in peer['calls'] if call['method'] == '$events/result')
        finally:
            adapter.close()


def test_quiet_original_event_stream_outlives_its_tcp_connect_timeout():
    with remote_peer() as (url, peer):
        adapter = DshRemoteAdapter(url, cookie='synthetic-auth=value', service_ref='local:synthetic-service', timeout=0.15)
        try:
            adapter.connect()
            time.sleep(0.6)
            assert adapter.loaded_threads() == ['session-fixture']
            assert sum(frame.get('endpoint') == '$events' for frame in peer['streams']) == 1
        finally:
            adapter.close()


def original_sdk_contract(payload):
    if not os.environ.get('DSH_TEST_SDK_ROOT'):
        if os.environ.get('DSH_REQUIRE_SDK_SMOKE') == '1':
            pytest.fail('The original DSH SDK is required for protocol verification.')
        pytest.skip('Original SDK is absent; native acceptance is unverified.')
    result = subprocess.run([os.environ.get('DSH_TEST_NODE', 'node'), str(Path(__file__).with_name('dsh_sdk_contract.mjs'))],
                            input=json.dumps(payload), text=True, capture_output=True, timeout=20)
    assert result.returncode == 0, 'The isolated original SDK contract failed.'
    value = json.loads(result.stdout)
    assert value['sdkBytesUnchanged'] is True
    return value


@pytest.mark.parametrize('event,business_request', [('unknown/request', {}), ('approval/request', {'toolName': 'bash'}),
                                         ('user-questions/request', {'questions': [], 'wait': {'callId': 'call-fixture', 'timed': True}})])
def test_unowned_native_waterfall_is_transferred_once_without_answer_or_approval(event, business_request):
    frame = {'type': 'waterfall', 'event': event, 'eventId': 'event-fixture', 'agentId': 'unowned-session', 'request': business_request}
    with remote_peer(behavior={'remote_events': [frame, frame]}) as (url, peer):
        adapter = DshRemoteAdapter(url, cookie='synthetic-auth=value', service_ref='local:synthetic-service', timeout=1)
        try:
            adapter.connect()
            deadline = time.monotonic() + 1
            while not peer['calls'] and time.monotonic() < deadline:
                time.sleep(0.01)
            transfers = [call['payload']['args'] for call in peer['calls'] if call['method'] == '$events/result']
            assert transfers == [{'clientId': 'fixture-client', 'eventId': 'event-fixture', 'outcome': {'kind': 'next'}}]
            native = original_sdk_contract({'kind': 'waterfall', 'eventId': 'event-fixture', 'replies': transfers})
            assert native['afterDesktop'] is None
            assert native['settled'] == {'kind': 'next'}
            assert native['remainingDeliveries'] == 0
        finally:
            adapter.close()


def native_rewritten_tool_events():
    events = [
        {'type': 'turn/start', 'seq': 0, 'time': 1, 'data': {'turn': 1}},
        {'type': 'tool/call', 'seq': 1, 'time': 1, 'data': {'turn': 1, 'step': 1, 'callId': 'synthetic-call', 'name': 'bash', 'arguments': '{"command":"true"}'}},
        {'type': 'tool/result', 'seq': 2, 'time': 2, 'surfaceOp': 'append', 'data': {'turn': 1, 'step': 1,
            'message': {'id': 'synthetic-result', 'role': 'tool', 'toolCallId': 'synthetic-call',
            'source': {'kind': 'tool', 'callId': 'synthetic-call'}, 'content': [{'type': 'text', 'text': 'original output'}]}}},
        {'type': 'turn/end', 'seq': 3, 'time': 3, 'data': {'turn': 1, 'reason': {'kind': 'completed'}}},
    ]
    rewrite = json.loads(json.dumps(events[2]))
    rewrite.update(seq=4, time=4, surfaceOp={'op': 'replace', 'startSeq': 2, 'endSeq': 2}, sourceEventSeqs=[2])
    rewrite['data']['message']['content'][0]['text'] = 'projected output'
    events.append(rewrite)
    return events


def test_native_surface_rewrite_preserves_execution_output_and_one_settlement():
    events = native_rewritten_tool_events()
    native = original_sdk_contract({'kind': 'surface', 'events': events})
    with remote_peer(behavior={'records': [{'type': 'event', 'event': event} for event in native['events']]}) as (url, peer):
        adapter = DshRemoteAdapter(url, cookie='synthetic-auth=value', service_ref='local:synthetic-service', timeout=1)
        try:
            command = adapter.read_thread('session-fixture')['turns'][0]['items'][0]
            assert command['nativeResultSeq'] == 2 and command['status'] == 'completed'
            assert command['aggregatedOutput'] == 'original output'
            assert command['surfaceRewrites'][0]['text'] == 'projected output'
            assert command['exitCode'] is None
        finally:
            adapter.close()


@pytest.mark.parametrize('case', ['foreign_call', 'changed_outcome', 'missing_sources', 'wrong_range', 'duplicate_append'])
def test_native_result_rewrite_cannot_forge_a_new_execution_or_replace_foreign_provenance(case):
    events = native_rewritten_tool_events()
    if case == 'foreign_call':
        events[-1]['data']['message']['toolCallId'] = 'foreign-call'
    elif case == 'changed_outcome':
        events[-1]['data']['message']['isError'] = True
    elif case == 'missing_sources':
        events[-1]['sourceEventSeqs'] = []
    elif case == 'wrong_range':
        events[-1]['surfaceOp']['startSeq'] = 1
        events[-1]['sourceEventSeqs'] = [1, 2]
    else:
        events[-1]['surfaceOp'] = 'append'
        events[-1].pop('sourceEventSeqs')
    if case != 'duplicate_append':
        assert original_sdk_contract({'kind': 'surface', 'events': events, 'expectRejected': True})['rejected'] is True
    with remote_peer(behavior={'records': [{'type': 'event', 'event': event} for event in events]}) as (url, peer):
        adapter = DshRemoteAdapter(url, cookie='synthetic-auth=value', service_ref='local:synthetic-service', timeout=1)
        try:
            with pytest.raises(ManagementError) as error:
                adapter.read_thread('session-fixture')
            assert error.value.code == 'capability_unverified'
        finally:
            adapter.close()


def test_readonly_client_transfers_foreign_waterfalls_but_cannot_answer_them():
    from ghost_hermes_pm.observation import ReadOnlyDshAdapter
    def proof(binding):
        return {**binding, 'engine': 'dsh', 'original_executor_id': binding['service_id'],
                'backend_instance_ref': 'local:synthetic-instance', 'provenance': 'synthetic-host-proof',
                'evidence_ref': 'local:synthetic-evidence', 'supported_methods': ['session/list', 'session/projections', 'session/follow'],
                'source_kinds': ['desktop']}
    frame = {'type': 'waterfall', 'event': 'approval/request', 'eventId': 'readonly-event',
             'agentId': 'foreign-session', 'request': {'toolName': 'bash'}}
    with remote_peer(behavior={'remote_events': [frame]}) as (url, peer):
        adapter = ReadOnlyDshAdapter(url, cookie='synthetic-auth=value', service_ref='local:synthetic-service',
                                    source_kind='desktop', endpoint_ref='local:synthetic-endpoint', verifier=proof, timeout=1)
        try:
            adapter.connect()
            deadline = time.monotonic() + 1
            while not peer['calls'] and time.monotonic() < deadline:
                time.sleep(0.01)
            args = [call['payload']['args'] for call in peer['calls']]
            assert args == [{'clientId': 'fixture-client', 'eventId': 'readonly-event', 'outcome': {'kind': 'next'}}]
            native = original_sdk_contract({'kind': 'waterfall', 'eventId': 'readonly-event', 'replies': args})
            assert native['settled'] == {'kind': 'next'}
            with pytest.raises(ManagementError) as error:
                adapter._call('$events/result', {'clientId': 'fixture-client', 'eventId': 'readonly-event',
                                                'outcome': {'kind': 'result', 'value': 'allowed-once'}})
            assert error.value.code == 'forbidden'
            adapter._next_waterfall('unobserved-forged-event')
            assert len(peer['calls']) == 1
        finally:
            adapter.close()


@pytest.mark.parametrize('case', ['intent', 'secret', 'operation', 'timed'])
def test_even_owned_unsupported_question_returns_next_to_original_answerers(case):
    from ghost_hermes_pm.questions import _describe
    question = {'id': 'choice', 'question': 'Synthetic choice?'}
    wait = {'callId': 'call-fixture'}
    if case == 'intent': question['intent'] = {'kind': 'plan-review', 'approve': 'Accept'}
    elif case == 'secret': question['question'] = 'Enter the password?'
    elif case == 'operation': question['question'] = 'May I run a shell command?'
    else: wait['timed'] = True
    frame = {'type': 'waterfall', 'event': 'user-questions/request', 'eventId': 'unsupported-event',
             'agentId': 'session-fixture', 'request': {'questions': [question], 'wait': wait}}
    records = [
        {'type': 'event', 'event': {'type': 'turn/start', 'seq': 0, 'time': 1, 'data': {'turn': 1}}},
        {'type': 'event', 'event': {'type': 'tool/call', 'seq': 1, 'time': 1,
                                  'data': {'turn': 1, 'step': 1, 'callId': 'call-fixture', 'name': 'ask_user_question', 'arguments': '{}'}}},
    ]
    class PrivateQuestionScope:
        def _sensitive_values(self): return ()
    with remote_peer(behavior={'remote_events': [frame], 'records': records, 'defer_remote_events': True}) as (url, peer):
        adapter, repository = admitted_adapter(url)
        try:
            proof = adapter.verifier(adapter.connect(), repository)
            adapter.bind_repository('session-fixture', repository, proof)
            adapter.verifier.target = ('session-fixture', 'dsh-turn:1')
            adapter.waterfall_authority = lambda *_: True
            adapter.waterfall_question_supported = lambda envelope: _describe(PrivateQuestionScope(), envelope).get('answerable') is True
            peer['emit_remote'].set()
            deadline = time.monotonic() + 1
            while not any(call['method'] == '$events/result' for call in peer['calls']) and time.monotonic() < deadline:
                time.sleep(.01)
            transfers = [call['payload']['args'] for call in peer['calls'] if call['method'] == '$events/result']
            assert transfers == [{'clientId': 'fixture-client', 'eventId': 'unsupported-event', 'outcome': {'kind': 'next'}}]
            native = original_sdk_contract({'kind': 'waterfall', 'eventId': 'unsupported-event', 'replies': transfers})
            assert native['settled'] == {'kind': 'next'}
            notice = adapter.server_requests('session-fixture')[0]
            assert notice['state'] == 'notice'
            assert notice['envelope']['params']['notification_only'] is True
            assert notice['envelope']['params']['notice_current'] is True
            assert notice['envelope']['params']['native_response_available'] is False
            adapter.waterfall_question_supported = lambda _: True
            with pytest.raises(ManagementError):
                adapter.respond_server_request(notice['envelope']['id'], notice['envelope'], {'answers': []})
            assert len([call for call in peer['calls'] if call['method'] == '$events/result']) == 1
        finally:
            adapter.close()


@pytest.mark.parametrize('end', ['cancel', 'settled', 'scope', 'close'])
def test_transferred_notice_expires_independently_and_cannot_revive_or_send_a_reply(end):
    frame = {'type': 'waterfall', 'event': 'approval/request', 'eventId': 'notice-fixture',
             'agentId': 'session-fixture', 'request': {'callId': 'notice-call', 'toolName': 'bash'}}
    records = [
        {'type': 'event', 'event': {'type': 'turn/start', 'seq': 0, 'time': 1, 'data': {'turn': 1}}},
        {'type': 'event', 'event': {'type': 'tool/call', 'seq': 1, 'time': 1,
            'data': {'turn': 1, 'step': 1, 'callId': 'notice-call', 'name': 'bash', 'arguments': '{}'}}},
        {'type': 'event', 'event': {'type': 'approval/asked', 'seq': 2, 'time': 1,
            'data': {'id': 'notice-audit', 'toolName': 'bash', 'callId': 'notice-call'}}},
    ]
    with remote_peer(behavior={'remote_events': [frame, frame], 'records': records, 'defer_remote_events': True}) as (url, peer):
        adapter, repository = admitted_adapter(url)
        try:
            proof = adapter.verifier(adapter.connect(), repository)
            adapter.bind_repository('session-fixture', repository, proof)
            adapter.waterfall_authority = lambda *_: True
            peer['emit_remote'].set()
            deadline = time.monotonic() + 1
            while not peer['calls'] and time.monotonic() < deadline: time.sleep(.01)
            notice = adapter.server_requests('session-fixture')[0]
            assert notice['state'] == 'notice' and notice['envelope']['params']['notice_current'] is True
            if end == 'scope': adapter.waterfall_authority = lambda *_: False
            elif end == 'close': adapter.close()
            elif end == 'cancel':
                peer['send_remote']({'type': 'cancel', 'eventId': 'notice-fixture'})
                deadline = time.monotonic() + 1
                while adapter.server_requests('session-fixture')[0]['state'] == 'notice' and time.monotonic() < deadline:
                    time.sleep(.01)
            else:
                peer['journals']['session-fixture'].append({'type': 'event', 'event': {'type': 'tool/result',
                    'seq': 3, 'time': 2, 'surfaceOp': 'append', 'data': {'turn': 1, 'step': 1,
                    'message': {'id': 'result-notice', 'role': 'tool', 'toolCallId': 'notice-call',
                    'source': {'kind': 'tool', 'callId': 'notice-call'}, 'content': [{'type': 'text', 'text': 'settled'}]}}}})
            expired = adapter.server_requests('session-fixture')[0]
            assert expired['state'] in {'expired', 'resolved'}
            assert expired['envelope']['params']['notice_current'] is False
            adapter.waterfall_authority = lambda *_: True
            assert adapter.server_requests('session-fixture')[0]['envelope']['params']['notice_current'] is False
            with pytest.raises(ManagementError):
                adapter.respond_server_request(notice['envelope']['id'], notice['envelope'], {'answers': []})
            assert [call['payload']['args']['outcome'] for call in peer['calls']
                    if call['method'] == '$events/result'] == [{'kind': 'next'}]
        finally:
            adapter.close()


@pytest.mark.parametrize('outcome', ['allowed-once', 'rejected', 'cancelled', 'unavailable'])
def test_original_approval_decision_ends_notice_while_its_bash_call_stays_running(outcome):
    frame = {'type': 'waterfall', 'event': 'approval/request', 'eventId': 'audit-event', 'agentId': 'session-fixture',
             'request': {'callId': 'audit-call', 'toolName': 'bash', 'reason': 'Synthetic operation'}}
    records = [
        {'type': 'event', 'event': {'type': 'turn/start', 'seq': 0, 'time': 1, 'data': {'turn': 1}}},
        {'type': 'event', 'event': {'type': 'tool/call', 'seq': 1, 'time': 1,
            'data': {'turn': 1, 'step': 1, 'callId': 'audit-call', 'name': 'bash', 'arguments': '{}'}}},
        {'type': 'event', 'event': {'type': 'approval/asked', 'seq': 2, 'time': 1,
            'data': {'id': 'audit-identity', 'toolName': 'bash', 'callId': 'audit-call', 'reason': 'Synthetic operation'}}},
    ]
    with remote_peer(behavior={'remote_events': [frame], 'records': records, 'defer_remote_events': True}) as (url, peer):
        adapter, repository = admitted_adapter(url)
        try:
            proof = adapter.verifier(adapter.connect(), repository)
            adapter.bind_repository('session-fixture', repository, proof)
            adapter.waterfall_authority = lambda *_: True
            peer['emit_remote'].set()
            deadline = time.monotonic() + 1
            while not peer['calls'] and time.monotonic() < deadline: time.sleep(.01)
            assert adapter.server_requests('session-fixture')[0]['state'] == 'notice'
            peer['journals']['session-fixture'].append({'type': 'event', 'event': {
                'type': 'approval/decided', 'seq': 3, 'time': 2, 'data': {'id': 'audit-identity', 'outcome': outcome}}})
            current = adapter.server_requests('session-fixture')[0]
            assert current['state'] == 'resolved'
            assert current['envelope']['params']['notice_current'] is False
            assert adapter.read_thread('session-fixture')['turns'][0]['items'][0]['status'] == 'inProgress'
            assert [call['payload']['args']['outcome'] for call in peer['calls']
                    if call['method'] == '$events/result'] == [{'kind': 'next'}]
        finally:
            adapter.close()


@pytest.mark.parametrize('case', ['unique_no_call', 'resolved_no_call', 'missing', 'ambiguous', 'unknown', 'foreign_decision', 'old_turn'])
def test_approval_notice_uses_a_unique_current_original_audit_without_guessing_the_last_index(case):
    frame = {'type': 'waterfall', 'event': 'approval/request', 'eventId': 'indexed-event', 'agentId': 'session-fixture',
             'request': {'toolName': 'bash', 'reason': 'Synthetic operation'}}
    prefix = [
        {'type': 'turn/start', 'seq': 0, 'time': 1, 'data': {'turn': 1}},
        {'type': 'tool/call', 'seq': 1, 'time': 1, 'data': {'turn': 1, 'step': 1,
            'callId': 'index-call', 'name': 'bash', 'arguments': '{}'}},
    ]
    # Actual installed service generates the id and both audit records; it is
    # intentionally absent from the original request and waterfall frame.
    native = original_sdk_contract({'kind': 'approval-audit', 'events': prefix,
                                   'request': frame['request'], 'outcome': 'allowed-once'})
    events = native['events'][:-1]
    if case == 'resolved_no_call': events = native['events']
    elif case == 'missing': events = prefix
    elif case == 'ambiguous':
        extra = json.loads(json.dumps(events[-1])); extra['seq'] = len(events); extra['data']['id'] = 'other-audit'
        events.append(extra)
    elif case in {'unknown', 'foreign_decision'}:
        extra = json.loads(json.dumps(native['events'][-1]))
        if case == 'unknown': extra['data']['outcome'] = 'future-outcome'
        else: extra['data']['id'] = 'foreign-audit'
        events.append(extra)
    elif case == 'old_turn':
        events += [{'type': 'turn/end', 'seq': len(events), 'time': 3, 'data': {'turn': 1, 'reason': {'kind': 'completed'}}},
                   {'type': 'turn/start', 'seq': len(events) + 1, 'time': 4, 'data': {'turn': 2}}]
    with remote_peer(behavior={'records': [{'type': 'event', 'event': event} for event in events],
                              'remote_events': [frame], 'defer_remote_events': True}) as (url, peer):
        adapter, repository = admitted_adapter(url)
        try:
            proof = adapter.verifier(adapter.connect(), repository)
            adapter.bind_repository('session-fixture', repository, proof)
            adapter.waterfall_authority = lambda *_: True
            peer['emit_remote'].set()
            deadline = time.monotonic() + 1
            while not peer['calls'] and time.monotonic() < deadline: time.sleep(.01)
            notice = adapter.server_requests('session-fixture')[0]
            expected = 'notice' if case in {'unique_no_call', 'foreign_decision'} else 'resolved' if case == 'resolved_no_call' else 'notice_unverified'
            assert notice['state'] == expected
            assert notice['envelope']['params']['notice_current'] is (expected == 'notice')
            assert notice['envelope']['params']['native_response_available'] is False
            with pytest.raises(ManagementError):
                adapter.respond_server_request(frame['eventId'], notice['envelope'], {'answers': []})
            assert [call['payload']['args']['outcome'] for call in peer['calls']
                    if call['method'] == '$events/result'] == [{'kind': 'next'}]
        finally:
            adapter.close()


@pytest.mark.parametrize('case', ['authority', 'proof', 'support', 'unknown_answer'])
def test_held_question_retires_on_lost_scope_without_resending_an_unknown_answer(case):
    frame = {'type': 'waterfall', 'event': 'user-questions/request', 'eventId': 'held-event', 'agentId': 'session-fixture',
             'request': {'questions': [{'id': 'choice', 'question': 'Synthetic choice?'}], 'wait': {'callId': 'call-fixture'}}}
    records = [
        {'type': 'event', 'event': {'type': 'turn/start', 'seq': 0, 'time': 1, 'data': {'turn': 1}}},
        {'type': 'event', 'event': {'type': 'tool/call', 'seq': 1, 'time': 1,
            'data': {'turn': 1, 'step': 1, 'callId': 'call-fixture', 'name': 'ask_user_question', 'arguments': '{}'}}},
    ]
    behavior = {'remote_events': [frame], 'records': records, 'defer_remote_events': True}
    if case == 'unknown_answer': behavior['drop'] = '$events/result'
    with remote_peer(behavior=behavior) as (url, peer):
        adapter, repository = admitted_adapter(url)
        try:
            proof = adapter.verifier(adapter.connect(), repository)
            adapter.bind_repository('session-fixture', repository, proof)
            adapter.verifier.target = ('session-fixture', 'dsh-turn:1')
            adapter.waterfall_authority = lambda *_: True
            adapter.waterfall_question_supported = lambda _: True
            peer['emit_remote'].set()
            pending, deadline = [], time.monotonic() + 1
            while not pending and time.monotonic() < deadline:
                pending = adapter.server_requests('session-fixture')
                if not pending: time.sleep(.01)
            envelope = pending[0]['envelope']
            if case == 'unknown_answer':
                with pytest.raises(ManagementError):
                    adapter.respond_server_request(envelope['id'], envelope, {'answers': [{'id': 'choice', 'selected': ['A']}]})
                adapter.waterfall_authority = lambda *_: False
            elif case == 'authority': adapter.waterfall_authority = lambda *_: False
            elif case == 'support': adapter.waterfall_question_supported = lambda _: False
            else: adapter.verifier.target = ('foreign-session', 'foreign-turn')
            refreshed = adapter.server_requests('session-fixture')[0]
            assert refreshed['envelope']['params']['native_response_available'] is False
            with pytest.raises(ManagementError):
                adapter.respond_server_request(envelope['id'], envelope, {'answers': [{'id': 'choice', 'selected': ['A']}]})
            replies = [call['payload']['args'] for call in peer['calls'] if call['method'] == '$events/result']
            assert len(replies) == 1
            assert replies[0]['outcome']['kind'] == ('result' if case == 'unknown_answer' else 'next')
        finally:
            adapter.close()
