"""Project memory checks the actual public global-validation round, never old prose."""
import json
import time
import socket
import struct
import threading
import pytest

from ghost_hermes_pm import Manager, ManagementError
from readiness_support import ReadyManager as Manager
from ghost_hermes_pm.transport import ManagementClient, ManagementServer
from test_directory import OWNER
from test_requests import ISSUE
from test_repository_queue import queue_adapter, acknowledge
from test_global_validation import LEAD, FixtureHost, combination, git


def accept_mono(root, manager, client, mono, request_id):
    task = next(r for r in client.read_snapshot()['requests'] if r['id'] == request_id)
    session = task['session']
    (root / 'queue-observed.json').write_text(json.dumps({session['thread_id']: {'status': {'type': 'idle'},
        'turns': [{'id': session['turn_id'], 'status': 'completed', 'itemsView': 'full', 'items': [
            {'type': 'commandExecution', 'id': 'mono-test', 'command': 'python -m unittest', 'cwd': str(mono),
             'status': 'completed', 'exitCode': 0, 'aggregatedOutput': 'Ran 1 test in 0.01s\n\nOK\nraw output is not memory'}]}]}}))
    client.record_task_delivery(request_id, {'source_commit': git(mono, 'rev-parse', 'HEAD'),
        'issue_updated_at': ISSUE['updated_at'], 'criteria': [{'text': ISSUE['body'], 'test_item_ids': ['mono-test']}]})


def test_curated_global_index_requires_real_public_complete_and_withdraws_after_input_change(tmp_path):
    host = FixtureHost()
    with Manager(tmp_path / 'state', owner_identity_ref=OWNER.subject, codex_adapter=queue_adapter(tmp_path), global_validation_host=host) as manager:
        mono, child, request_id, child_id = combination(manager, tmp_path)
        with ManagementServer(manager, {'owner': OWNER, 'lead': LEAD}):
            client = ManagementClient(tmp_path / 'state', 'lead')
            planned = client.global_validation('plan', {'request_id': request_id, 'mono_commit': git(mono, 'rev-parse', 'HEAD'),
                'children': [{'request_id': child_id, 'path': 'child'}], 'test_ids': ['unit']})
            client.global_validation('start', {'validation_id': planned['id']})
            assert client.global_validation('finish', {'validation_id': planned['id']})['status'] == 'passed'
            accept_mono(tmp_path, manager, client, mono, request_id)
            selection = {'facts': [], 'decisions': [], 'include_delivery': True, 'global_validation_id': planned['id']}
            with pytest.raises(ManagementError) as incomplete:
                client.curate_project_memory('mono-lead', 'global-result', request_id, selection)
            assert incomplete.value.code == 'evidence_missing'
            complete = client.global_validation('complete', {'validation_id': planned['id']})
            assert complete['whole_project_complete'] and complete['completed_at']
            entry = client.curate_project_memory('mono-lead', 'global-result', request_id, selection)
            index = entry['global_validation']
            assert index['validation_id'] == planned['id'] and index['mono_commit'] == complete['mono_commit']
            assert index['children'][0]['request_id'] == child_id and index['children'][0]['commit'] == git(child, 'rev-parse', 'HEAD')
            assert index['completed_at'] == complete['completed_at'] and index['boundary_scope'] == 'synthetic-fixture'
            assert index['production_acceptance'] is False and index['tests'][0]['output_digest']
            assert not any(key in json.dumps(index) for key in ('git_dir', 'common_dir', 'raw output', 'aggregatedOutput'))
            assert client.curate_project_memory('mono-lead', 'global-result', request_id, selection)['duplicate'] is True
            next_id = acknowledge(manager, suffix='global-memory-followup')
            assert client.load_project_memory(next_id, ['global-result'])['status'] == 'prepared'
            original = (child / 'source.py').read_bytes()
            (child / 'source.py').write_text('changed manual input\n')
            with pytest.raises(ManagementError) as stale:
                client.read_project_memory('mono-lead')
            assert stale.value.code == 'evidence_missing'
            assert client.global_validation('check', {'validation_id': planned['id']})['status'] == 'invalidated'
            (child / 'source.py').write_bytes(original)
            with pytest.raises(ManagementError):
                client.load_project_memory(next_id, ['global-result'])
            assert client.global_validation('check', {'validation_id': planned['id']})['whole_project_complete'] is False


def test_restart_without_original_watch_host_withholds_old_global_memory_and_cannot_load_it(tmp_path):
    host = FixtureHost()
    with Manager(tmp_path / 'state', owner_identity_ref=OWNER.subject, codex_adapter=queue_adapter(tmp_path), global_validation_host=host) as manager:
        mono, child, request_id, child_id = combination(manager, tmp_path)
        with ManagementServer(manager, {'owner': OWNER, 'lead': LEAD}):
            client = ManagementClient(tmp_path / 'state', 'lead')
            planned = client.global_validation('plan', {'request_id': request_id, 'mono_commit': git(mono, 'rev-parse', 'HEAD'),
                'children': [{'request_id': child_id, 'path': 'child'}], 'test_ids': ['unit']})
            client.global_validation('start', {'validation_id': planned['id']})
            client.global_validation('finish', {'validation_id': planned['id']})
            accept_mono(tmp_path, manager, client, mono, request_id)
            client.global_validation('complete', {'validation_id': planned['id']})
            client.curate_project_memory('mono-lead', 'before-restart-global', request_id,
                {'facts': [], 'decisions': [], 'include_delivery': False, 'global_validation_id': planned['id']})
    with Manager(tmp_path / 'state', owner_identity_ref=OWNER.subject) as restarted:
        with ManagementServer(restarted, {'owner': OWNER, 'lead': LEAD}):
            client = ManagementClient(tmp_path / 'state', 'lead')
            with pytest.raises(ManagementError) as missing_host:
                client.read_project_memory('mono-lead')
            assert missing_host.value.code == 'evidence_missing'
            round_state = client.global_validation('check', {'validation_id': planned['id']})
            assert round_state['status'] == 'unverified' and round_state['whole_project_complete'] is False
            next_id = acknowledge(restarted, suffix='no-watch-memory-load')
            with pytest.raises(ManagementError):
                client.load_project_memory(next_id, ['before-restart-global'])
            assert next(r for r in client.read_snapshot()['requests'] if r['id'] == next_id).get('memory_context') is None


def test_curating_global_memory_waits_for_the_delayed_original_host_check(tmp_path):
    class DelayedHost(FixtureHost):
        delay_next_check = False
        def read_input_changes(self, context):
            if self.delay_next_check:
                self.delay_next_check = False
                time.sleep(3.5)
            return super().read_input_changes(context)
    host = DelayedHost()
    with Manager(tmp_path / 'state', owner_identity_ref=OWNER.subject, codex_adapter=queue_adapter(tmp_path), global_validation_host=host) as manager:
        mono, child, request_id, child_id = combination(manager, tmp_path)
        with ManagementServer(manager, {'lead': LEAD}):
            client = ManagementClient(tmp_path / 'state', 'lead')
            planned = client.global_validation('plan', {'request_id': request_id, 'mono_commit': git(mono, 'rev-parse', 'HEAD'),
                'children': [{'request_id': child_id, 'path': 'child'}], 'test_ids': ['unit']})
            client.global_validation('start', {'validation_id': planned['id']})
            assert client.global_validation('finish', {'validation_id': planned['id']})['status'] == 'passed'
            accept_mono(tmp_path, manager, client, mono, request_id)
            client.global_validation('complete', {'validation_id': planned['id']})
            host.delay_next_check = True
            try:
                entry = client.curate_project_memory('mono-lead', 'delayed-global-result', request_id,
                    {'facts': [], 'decisions': [], 'include_delivery': False, 'global_validation_id': planned['id']})
            finally:
                # Wait through the public manager lock before closing a still-running source check.
                manager.read_snapshot(LEAD)
            assert entry['global_validation']['validation_id'] == planned['id']
            assert entry['global_validation']['provenance'] == 'public_global_validation_current_check'
            assert client.read_project_memory('mono-lead')['entries'][0]['id'] == 'delayed-global-result'


def test_lost_curation_reply_preserves_the_same_durable_entry_without_replay(tmp_path):
    host = FixtureHost()
    state = tmp_path / 'state'
    with Manager(state, owner_identity_ref=OWNER.subject, codex_adapter=queue_adapter(tmp_path), global_validation_host=host) as manager:
        mono, child, request_id, child_id = combination(manager, tmp_path)
        with ManagementServer(manager, {'lead': LEAD}):
            client = ManagementClient(state, 'lead')
            planned = client.global_validation('plan', {'request_id': request_id, 'mono_commit': git(mono, 'rev-parse', 'HEAD'),
                'children': [{'request_id': child_id, 'path': 'child'}], 'test_ids': ['unit']})
            client.global_validation('start', {'validation_id': planned['id']})
            client.global_validation('finish', {'validation_id': planned['id']})
            accept_mono(tmp_path, manager, client, mono, request_id)
            client.global_validation('complete', {'validation_id': planned['id']})
            relay_home = tmp_path / 'reply-loss'; relay_home.mkdir(mode=0o700)
            stop, calls, failures = threading.Event(), [], []
            def packet(connection):
                with connection.makefile('rb') as reader:
                    header = reader.read(8)
                    body = reader.read(struct.unpack('!Q', header)[0])
                return header + body, json.loads(body)
            with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as listener:
                listener.bind(str(relay_home / 'manager.sock'))
                listener.listen(); listener.settimeout(0.1)
                def relay():
                    try:
                        while not stop.is_set():
                            try:
                                incoming, _ = listener.accept()
                            except socket.timeout:
                                continue
                            with incoming, socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as original:
                                incoming.settimeout(30); original.settimeout(30)
                                request, payload = packet(incoming)
                                calls.append(payload)
                                original.connect(str(state / 'manager.sock'))
                                original.sendall(request)
                                response, committed = packet(original)
                                assert committed['result']['id'] == 'lost-global-result'
                                # Drop the first committed reply; a replay would receive a reply and fail this test.
                                if len(calls) > 1:
                                    incoming.sendall(response)
                    except Exception as exc:
                        failures.append(exc)
                thread = threading.Thread(target=relay)
                thread.start()
                try:
                    with pytest.raises(ManagementError) as lost:
                        ManagementClient(relay_home, 'lead').curate_project_memory('mono-lead', 'lost-global-result', request_id,
                            {'facts': [], 'decisions': [], 'include_delivery': False, 'global_validation_id': planned['id']})
                    assert lost.value.code == 'outcome_unknown'
                    entries = client.read_project_memory('mono-lead')['entries']
                    assert len(entries) == 1 and entries[0]['id'] == 'lost-global-result'
                    assert entries[0]['request_id'] == request_id
                    assert entries[0]['global_validation']['validation_id'] == planned['id']
                    assert len(calls) == 1 and calls[0]['entry_id'] == 'lost-global-result' and calls[0]['request_id'] == request_id
                finally:
                    stop.set(); thread.join(timeout=5)
                assert not thread.is_alive() and failures == []
