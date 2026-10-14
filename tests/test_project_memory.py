"""Known facts and curated memory through the authenticated management entry."""
import json
import pytest

from ghost_hermes_pm import Manager, ManagementError, VerifiedIdentity
from readiness_support import ReadyManager as Manager
from ghost_hermes_pm.transport import ManagementClient, ManagementServer
from test_directory import OWNER, make_repo
from test_task_execution import accepted, prepare_fixture
from test_questions import question_adapter, emit, user_question, replies
from test_knowledge import WIKI, local_provider, public_grant

LEAD = VerifiedIdentity('fixture:lead', 'synthetic-participant')
QUESTION = 'What is the retry delivery policy?'


def memory_adapter(root):
    from pathlib import Path
    adapter = question_adapter(root)
    adapter.command[1] = str(Path(__file__).with_name('memory_fixture_server.py'))
    return adapter


def prepared_facts(manager, owner, requester, request_id, query_id='known-policy', question=QUESTION):
    source = public_grant()
    source['query_subjects'][LEAD.subject] = ['public']
    source['public_channels'][0]['view_subjects'].append(LEAD.subject)
    owner.register_knowledge_source(owner.read_snapshot()['version'], source)
    requester.query_knowledge('fixture-wiki', query_id, question, ['public'], request_id=request_id,
                              channel_id='project-chat', auto_supplement=False)
    segment = manager.claim_knowledge_delivery(LEAD, query_id)
    manager.record_knowledge_delivery(LEAD, query_id, segment['uuid'], {'status': 'delivered', 'message_id': 'om_query', 'chat_id': 'oc_project'})
    binding = source['wiki_bindings'][0]
    anchor = {k: binding[k] for k in ('app_id', 'transport_tenant_key', 'recipient_tenant_key', 'recipient_open_id', 'chat_id')}
    anchor.update(tenant_key=binding['sender_tenant_key'], sender_open_id=binding['sender_open_id'], message_id='om_query', parent_id='om_ack', root_id=None, thread_id=None)
    wiki = VerifiedIdentity(WIKI['identity_ref'], 'synthetic-wiki')
    manager.receive_wiki_query(wiki, query_id, binding['id'], anchor)
    result = manager.resolve_knowledge(wiki, query_id)
    segment = manager.claim_knowledge_delivery(wiki, query_id)
    manager.record_knowledge_delivery(wiki, query_id, segment['uuid'], {'status': 'delivered', 'message_id': 'om_wiki_result', 'chat_id': 'oc_project'})
    binding = source['wiki_bindings'][1]
    returned = {k: binding[k] for k in ('app_id', 'transport_tenant_key', 'recipient_tenant_key', 'recipient_open_id', 'chat_id')}
    returned.update(tenant_key=binding['sender_tenant_key'], sender_open_id=binding['sender_open_id'], message_id='om_wiki_result', parent_id='om_query', root_id=None, thread_id=None)
    manager.receive_wiki_result(wiki, query_id, binding['id'], returned, result['result_version'])
    return query_id


def test_responsible_lead_answers_actual_fact_request_with_sources_once(tmp_path):
    provider = local_provider(tmp_path)
    with Manager(tmp_path / 'state', owner_identity_ref=OWNER.subject, dsh_adapter=memory_adapter(tmp_path),
                 knowledge_providers={'local:fixture-wiki': provider}) as manager:
        request_id = accepted(manager, make_repo(tmp_path / 'repo'))
        manager.start_task(OWNER, request_id)
        manager.apply_directory_change(OWNER, manager.read_snapshot(OWNER)['version'], {'profile': WIKI})
        with ManagementServer(manager, {'owner': OWNER, 'lead': LEAD}):
            owner, lead = ManagementClient(tmp_path / 'state', 'owner'), ManagementClient(tmp_path / 'state', 'lead')
            query_id = prepared_facts(manager, owner, lead, request_id)
            emit(tmp_path, user_question(questions=[{'id': 'policy', 'header': 'Known policy', 'question': QUESTION,
                'isOther': True, 'isSecret': False, 'options': None}]))
            question = owner.refresh_task(request_id)['human_requests'][0]
            result = lead.answer_from_knowledge(request_id, question['id'], query_id, ['retry:3'])
            assert result['reply']['actor'] == LEAD.subject
            assert result['reply']['factual_evidence']['query_id'] == query_id
            assert lead.answer_from_knowledge(request_id, question['id'], query_id, ['retry:3'])['duplicate'] is True
        sent = replies(tmp_path)
        assert len(sent) == 1
        answer = next(value for value in sent[0]['result']['answers'] if value['id'] == 'policy')['custom']
        assert 'Retry only definite failures.' in answer and 'retry.md#L3' in answer and 'sha256:' in answer


def completed_delivery(root, client, request_id):
    from test_requests import ISSUE
    from test_questions import TURN
    observed = {'status': {'type': 'idle'}, 'turns': [{'id': TURN, 'status': 'completed', 'itemsView': 'full', 'items': [
        {'type': 'agentMessage', 'id': 'final', 'text': 'Done, all tests passed. Raw conversation must not be copied.'},
        {'type': 'commandExecution', 'id': 'pytest-1', 'command': 'python -m pytest tests/test_fixture.py -q',
         'cwd': str(root / 'repo'), 'status': 'completed', 'exitCode': 0, 'aggregatedOutput': '1 passed in 0.01s\nraw output must not be copied'}]}]}
    (root / 'observed.json').write_text(json.dumps(observed))
    return client.record_task_delivery(request_id, {'issue_updated_at': ISSUE['updated_at'],
        'criteria': [{'text': ISSUE['body'], 'test_item_ids': ['pytest-1']}], 'source_commit': None, 'pr_url': None, 'sync_branches': []})


def test_accepted_facts_are_curated_with_fixed_result_indexes_and_loaded_only_in_selected_new_session(tmp_path):
    import hashlib
    from test_repository_queue import acknowledge
    provider = local_provider(tmp_path)
    source_before = (tmp_path / 'source' / 'retry.md').read_bytes()
    with Manager(tmp_path / 'state', owner_identity_ref=OWNER.subject, dsh_adapter=memory_adapter(tmp_path),
                 knowledge_providers={'local:fixture-wiki': provider}) as manager:
        request_id = accepted(manager, make_repo(tmp_path / 'repo'))
        manager.start_task(OWNER, request_id)
        manager.apply_directory_change(OWNER, manager.read_snapshot(OWNER)['version'], {'profile': WIKI})
        with ManagementServer(manager, {'owner': OWNER, 'lead': LEAD}):
            owner, lead = ManagementClient(tmp_path / 'state', 'owner'), ManagementClient(tmp_path / 'state', 'lead')
            query_id = prepared_facts(manager, owner, lead, request_id)
            selected = {'facts': [{'query_id': query_id, 'material_ids': ['retry:3']}], 'decisions': [], 'include_delivery': True}
            with pytest.raises(ManagementError) as denied:
                lead.curate_project_memory('mono-lead', 'policy-v1', request_id, selected)
            assert denied.value.code == 'evidence_missing'
            completed_delivery(tmp_path, owner, request_id)
            entry = lead.curate_project_memory('mono-lead', 'policy-v1', request_id, selected)
            assert entry['project_id'] == 'mono' and entry['role'] == 'project_lead'
            assert entry['facts'][0]['text'].startswith('Retry only definite failures.')
            assert entry['delivery']['issue_updated_at'] and entry['delivery']['verified_at']
            assert entry['delivery']['tests'][0]['item_id'] == 'pytest-1'
            assert 'raw output' not in json.dumps(entry) and 'Raw conversation' not in json.dumps(entry)
            assert 'PRIVATE' not in json.dumps(entry)
            assert lead.curate_project_memory('mono-lead', 'policy-v1', request_id, selected)['duplicate'] is True
            next_id = acknowledge(manager, suffix='memory-follow-up')
            prepare_fixture(manager, next_id, tmp_path / 'repo')
            staged = lead.load_project_memory(next_id, ['policy-v1'])
            assert staged['status'] == 'prepared' and not staged.get('turn_id')
            assert lead.read_snapshot()['requests'][0].get('memory_context') is None
            owner.start_task(next_id)
            loaded = next(r for r in owner.read_snapshot()['requests'] if r['id'] == next_id)['memory_context']
            assert loaded['status'] == 'loaded' and loaded['profile_id'] == 'mono-lead'
            old_session = next(r for r in owner.read_snapshot()['requests'] if r['id'] == request_id)['session']
            assert loaded['turn_id'] != old_session['turn_id'] and loaded['thread_id'] != old_session['thread_id']
            assert loaded['input_digest']
        inputs = [r for r in map(json.loads, (tmp_path / 'wire.jsonl').read_text().splitlines()) if r.get('method') == 'fixture/start']
        assert len(inputs) == 2
        first, followup = [r['params']['input'][0]['text'] for r in inputs]
        assert 'Retry only definite failures.' not in first
        assert 'Retry only definite failures.' in followup and 'retry.md#L3' in followup
        assert hashlib.sha256(source_before).hexdigest() == hashlib.sha256((tmp_path / 'source' / 'retry.md').read_bytes()).hexdigest()


def test_only_explicit_owner_scope_updates_preferences_and_corrections_keep_old_material(tmp_path):
    from test_repository_queue import acknowledge
    with Manager(tmp_path / 'state', owner_identity_ref=OWNER.subject, dsh_adapter=memory_adapter(tmp_path)) as manager:
        request_id = accepted(manager, make_repo(tmp_path / 'repo'))
        with ManagementServer(manager, {'owner': OWNER, 'lead': LEAD}):
            owner, lead = ManagementClient(tmp_path / 'state', 'owner'), ManagementClient(tmp_path / 'state', 'lead')
            with pytest.raises(ManagementError) as denied:
                lead.record_memory_preference('mono-lead', 'fake-rule', 'Always choose blue.', {'kind': 'project', 'id': 'mono'})
            assert denied.value.code == 'forbidden'
            one_off = owner.record_memory_preference('mono-lead', 'one-choice', 'Choose blue for this task.', {'kind': 'task', 'id': request_id})
            assert one_off['owner_origin']['subject'] == OWNER.subject and one_off['scope']['kind'] == 'task'
            old = owner.record_memory_preference('mono-lead', 'project-rule-v1', 'For this project, use short progress updates.', {'kind': 'project', 'id': 'mono'})
            corrected = owner.record_memory_preference('mono-lead', 'project-rule-v2', 'For this project, include evidence in progress updates.', {'kind': 'project', 'id': 'mono'}, supersedes='project-rule-v1')
            assert corrected['supersedes'] == old['id']
            history = lead.read_project_memory('mono-lead', include_superseded=True)['entries']
            previous = next(e for e in history if e['id'] == old['id'])
            assert previous['statement'] == old['statement'] and previous['status'] == 'superseded' and previous['replaced_by'] == corrected['id']
            assert old['id'] not in [e['id'] for e in lead.read_project_memory('mono-lead')['entries']]
            next_id = acknowledge(manager, suffix='one-off-cannot-leak')
            with pytest.raises(ManagementError):
                lead.load_project_memory(next_id, [one_off['id']])
            loaded = lead.load_project_memory(next_id, [corrected['id']])
            assert 'include evidence' in loaded['text'] and 'choose blue' not in loaded['text'].lower()
    with Manager(tmp_path / 'state', owner_identity_ref=OWNER.subject) as restarted:
        assert len(restarted.read_project_memory(LEAD, 'mono-lead', True)['entries']) == 3


@pytest.mark.parametrize('text', [
    'Which retry delivery policy should we choose?', 'What is your preferred retry delivery policy?',
    'What is the retry delivery policy? Add a new transport.', 'Owner, what is the retry delivery policy?',
    'Can we authorize running retry delivery commands?', 'What is the missing delivery date?',
])
def test_decisions_new_work_owner_only_and_unverifiable_questions_stay_with_owner(tmp_path, text):
    with Manager(tmp_path / 'state', owner_identity_ref=OWNER.subject, dsh_adapter=memory_adapter(tmp_path),
                 knowledge_providers={'local:fixture-wiki': local_provider(tmp_path)}) as manager:
        request_id = accepted(manager, make_repo(tmp_path / 'repo'))
        manager.start_task(OWNER, request_id)
        manager.apply_directory_change(OWNER, manager.read_snapshot(OWNER)['version'], {'profile': WIKI})
        with ManagementServer(manager, {'owner': OWNER, 'lead': LEAD}):
            owner, lead = ManagementClient(tmp_path / 'state', 'owner'), ManagementClient(tmp_path / 'state', 'lead')
            query = prepared_facts(manager, owner, lead, request_id)
            emit(tmp_path, user_question(questions=[{'id': 'ask', 'header': 'Original request', 'question': text,
                'isOther': True, 'isSecret': False, 'options': None}]))
            question = owner.refresh_task(request_id)['human_requests'][0]
            result = lead.answer_from_knowledge(request_id, question['id'], query, ['retry:3'])
            assert result['status'] == 'owner_required'
            assert owner.read_snapshot()['requests'][0]['human_requests'][0]['reply'] is None
        assert replies(tmp_path) == []


def test_expired_idle_fact_cannot_start_execution_and_original_owner_decision_can_be_curated(tmp_path):
    from test_questions import TURN
    with Manager(tmp_path / 'state', owner_identity_ref=OWNER.subject, dsh_adapter=memory_adapter(tmp_path),
                 knowledge_providers={'local:fixture-wiki': local_provider(tmp_path)}) as manager:
        request_id = accepted(manager, make_repo(tmp_path / 'repo'))
        manager.start_task(OWNER, request_id)
        manager.apply_directory_change(OWNER, manager.read_snapshot(OWNER)['version'], {'profile': WIKI})
        with ManagementServer(manager, {'owner': OWNER, 'lead': LEAD}):
            owner, lead = ManagementClient(tmp_path / 'state', 'owner'), ManagementClient(tmp_path / 'state', 'lead')
            query = prepared_facts(manager, owner, lead, request_id)
            emit(tmp_path, user_question())
            choice = owner.refresh_task(request_id)['human_requests'][0]
            owner.answer_human_request(request_id, choice['id'], 'explicit-choice', {'answers': {'colour': ['Blue for this task.']}})
            assert owner.refresh_task(request_id)['human_requests'][0]['resolution'] == 'resolved'
            completed_delivery(tmp_path, owner, request_id)
            entry = lead.curate_project_memory('mono-lead', 'confirmed-choice', request_id,
                {'facts': [], 'decisions': [choice['id']], 'include_delivery': True})
            assert entry['decisions'][0]['owner'] == OWNER.subject
            assert entry['decisions'][0]['answers'] == {'colour': ['Blue for this task.']}
            assert entry['kind'] == 'accepted_result' and 'scope' not in entry
            with pytest.raises(ManagementError):
                lead.answer_from_knowledge(request_id, choice['id'], query, ['retry:3'])
        assert len([r for r in map(json.loads, (tmp_path / 'wire.jsonl').read_text().splitlines()) if r.get('method') == 'fixture/start']) == 1


def test_role_memories_remain_separate_and_superiors_keep_only_necessary_result_indexes(tmp_path):
    from test_directory import registration
    child = VerifiedIdentity('fixture:child', 'synthetic-child')
    steward = VerifiedIdentity('fixture:steward', 'synthetic-steward')
    with Manager(tmp_path / 'state', owner_identity_ref=OWNER.subject, dsh_adapter=memory_adapter(tmp_path)) as manager:
        request_id = accepted(manager, make_repo(tmp_path / 'repo'))
        manager.start_task(OWNER, request_id)
        change = registration(make_repo(tmp_path / 'child'), 'child', 'child-lead')
        change['profile'].update(identity_ref=child.subject, role='subproject_lead', parent_profile_id='mono-lead')
        manager.apply_directory_change(OWNER, manager.read_snapshot(OWNER)['version'], change)
        manager.apply_directory_change(OWNER, manager.read_snapshot(OWNER)['version'], {'profile': {
            'id': 'steward', 'native_profile': 'steward', 'identity_ref': steward.subject, 'role': 'steward',
            'capability': 'non_development', 'project_id': None, 'parent_profile_id': None, 'connection_refs': {}}})
        with ManagementServer(manager, {'owner': OWNER, 'lead': LEAD, 'child': child, 'steward': steward}):
            owner, lead, child_client, global_client = [ManagementClient(tmp_path / 'state', token) for token in ('owner', 'lead', 'child', 'steward')]
            owner.record_memory_preference('child-lead', 'child-rule', 'For the child project, show source indexes.', {'kind': 'project', 'id': 'child'})
            assert child_client.read_project_memory('child-lead')['entries'][0]['project_id'] == 'child'
            with pytest.raises(ManagementError):
                lead.read_project_memory('child-lead')
            with pytest.raises(ManagementError):
                global_client.read_project_memory('child-lead')
            completed_delivery(tmp_path, owner, request_id)
            selection = {'facts': [], 'decisions': [], 'include_delivery': True}
            own = lead.curate_project_memory('mono-lead', 'mono-result', request_id, selection)
            summary = global_client.curate_project_memory('steward', 'necessary-result-index', request_id, selection)
            assert summary['project_id'] is None and summary['role'] == 'steward'
            assert summary['facts'] == summary['decisions'] == [] and summary['delivery']['issue_url'] == own['delivery']['issue_url']
            with pytest.raises(ManagementError):
                child_client.curate_project_memory('child-lead', 'foreign-result', request_id, selection)
            with pytest.raises(ManagementError):
                global_client.curate_project_memory('mono-lead', 'overwrite-mono', request_id, selection)
            assert len(lead.read_project_memory('mono-lead')['entries']) == 1
            assert len(child_client.read_project_memory('child-lead')['entries']) == 1


def test_unrelated_returned_fact_and_source_grant_revocation_cannot_answer_or_load(tmp_path):
    from test_repository_queue import acknowledge
    with Manager(tmp_path / 'state', owner_identity_ref=OWNER.subject, dsh_adapter=memory_adapter(tmp_path),
                 knowledge_providers={'local:fixture-wiki': local_provider(tmp_path)}) as manager:
        request_id = accepted(manager, make_repo(tmp_path / 'repo'))
        manager.start_task(OWNER, request_id)
        manager.apply_directory_change(OWNER, manager.read_snapshot(OWNER)['version'], {'profile': WIKI})
        with ManagementServer(manager, {'owner': OWNER, 'lead': LEAD}):
            owner, lead = ManagementClient(tmp_path / 'state', 'owner'), ManagementClient(tmp_path / 'state', 'lead')
            unknown = 'What is the missing delivery date?'
            query = prepared_facts(manager, owner, lead, request_id, query_id='unrelated-source-result', question=unknown)
            emit(tmp_path, user_question(questions=[{'id': 'unknown', 'header': 'Known fact?', 'question': unknown,
                'isOther': True, 'isSecret': False, 'options': None}]))
            q = owner.refresh_task(request_id)['human_requests'][0]
            assert lead.answer_from_knowledge(request_id, q['id'], query, ['retry:3'])['status'] == 'owner_required'
            assert replies(tmp_path) == []
            proper = prepared_facts(manager, owner, lead, request_id, query_id='proper-source-result')
            completed_delivery(tmp_path, owner, request_id)
            lead.curate_project_memory('mono-lead', 'accepted-facts', request_id,
                {'facts': [{'query_id': proper, 'material_ids': ['retry:3']}], 'decisions': [], 'include_delivery': True})
            next_id = acknowledge(manager, suffix='revoke-before-start')
            lead.load_project_memory(next_id, ['accepted-facts'])
            changed = public_grant()
            changed['query_subjects'] = {OWNER.subject: ['public']}
            owner.register_knowledge_source(owner.read_snapshot()['version'], changed)
            with pytest.raises(ManagementError):
                lead.read_project_memory('mono-lead')
            with pytest.raises(ManagementError):
                lead.load_project_memory(next_id, ['accepted-facts'])


def test_memory_update_requires_explicit_effective_original_control_to_affect_running_work(tmp_path):
    from test_task_execution import adapter_for
    from test_manual_control import original_state, adapters, setup, ORIGINAL_TURN
    repo = make_repo(tmp_path / 'repo')
    peer = tmp_path / 'original'
    original_state(peer, repo)
    read, control = adapters(peer)
    with Manager(tmp_path / 'state', owner_identity_ref=OWNER.subject, dsh_adapter=adapter_for(tmp_path),
                 observation_adapters={'local:manual-desktop': read}, control_adapters={'manual-desktop': control}) as manager:
        request_id, observed = setup(manager, repo)
        with ManagementServer(manager, {'owner': OWNER, 'lead': LEAD}):
            owner, lead = ManagementClient(tmp_path / 'state', 'owner'), ManagementClient(tmp_path / 'state', 'lead')
            owner.take_over_session(request_id, observed['id'], 'memory-current-grant', ORIGINAL_TURN)
            owner.record_memory_preference('mono-lead', 'current-rule', 'For this project, cite evidence.', {'kind': 'project', 'id': 'mono'})
            before = json.loads((peer / 'original-state.json').read_text())
            assert before.get('inputs', []) == []
            with pytest.raises(ManagementError):
                lead.load_project_memory(request_id, ['current-rule'])
            supplied = lead.supplement_project_memory(request_id, ['current-rule'], ORIGINAL_TURN)
            assert supplied['status'] == 'accepted' and supplied['control_grant_id'] == 'memory-current-grant'
            assert lead.supplement_project_memory(request_id, ['current-rule'], ORIGINAL_TURN)['duplicate'] is True
            owner.return_session_control(request_id, 'memory-current-grant')
            owner.record_memory_preference('mono-lead', 'late-rule', 'For this project, include locations.', {'kind': 'project', 'id': 'mono'})
            with pytest.raises(ManagementError):
                lead.supplement_project_memory(request_id, ['late-rule'], ORIGINAL_TURN)
        after = json.loads((peer / 'original-state.json').read_text())
        assert len(after['inputs']) == 1 and 'cite evidence' in after['inputs'][0]['input'][0]['text']
        assert not (tmp_path / 'wire.jsonl').exists()
        methods = [r.get('method') for r in map(json.loads, (peer / 'original-wire.jsonl').read_text().splitlines())]
        assert methods.count('fixture/append') == 1 and 'fixture/start' not in methods and 'fixture/create' not in methods


def test_dashboard_and_participant_memory_actions_share_identity_scope_and_loading_rules(tmp_path):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from ghost_hermes_pm.dashboard import create_router
    with Manager(tmp_path / 'state', owner_identity_ref=OWNER.subject, dsh_adapter=memory_adapter(tmp_path)) as manager:
        request_id = accepted(manager, make_repo(tmp_path / 'repo'))
        with ManagementServer(manager, {'owner': OWNER, 'lead': LEAD}):
            app = FastAPI()
            app.include_router(create_router(lambda request: ManagementClient(tmp_path / 'state', request.headers.get('x-fixture-entry', 'invalid'))))
            browser = TestClient(app)
            body = {'action': 'preference', 'details': {'profile_id': 'mono-lead', 'entry_id': 'dashboard-rule',
                'statement': 'For this project, include tested versions.', 'scope': {'kind': 'project', 'id': 'mono'}}}
            response = browser.post('/memory', json=body, headers={'x-fixture-entry': 'owner'})
            assert response.status_code == 200, response.text
            assert browser.post('/memory', json=body, headers={'x-fixture-entry': 'lead'}).status_code == 403
            forged = {**body, 'details': {**body['details'], 'actor': OWNER.subject}}
            assert browser.post('/memory', json=forged, headers={'x-fixture-entry': 'lead'}).status_code == 422
            read = browser.post('/memory', json={'action': 'read', 'details': {'profile_id': 'mono-lead'}}, headers={'x-fixture-entry': 'lead'})
            assert read.status_code == 200 and read.json()['external_memory'] == 'unverified'
            assert browser.post('/memory', json={'action': 'load', 'details': {'request_id': request_id, 'entry_ids': ['dashboard-rule'], 'role': 'steward'}}, headers={'x-fixture-entry': 'lead'}).status_code == 422
            loaded = browser.post('/memory', json={'action': 'load', 'details': {'request_id': request_id, 'entry_ids': ['dashboard-rule']}}, headers={'x-fixture-entry': 'lead'})
            assert loaded.status_code == 200 and loaded.json()['status'] == 'prepared'


@pytest.mark.parametrize('kind', ['inference', 'suggestion', 'stale', 'conflict'])
def test_unaccepted_classifications_and_sensitive_or_temporary_content_never_become_long_term_facts(tmp_path, kind):
    provider = local_provider(tmp_path)
    provider.documents[0]['kind'] = kind
    with Manager(tmp_path / 'state', owner_identity_ref=OWNER.subject, dsh_adapter=memory_adapter(tmp_path),
                 knowledge_providers={'local:fixture-wiki': provider}) as manager:
        request_id = accepted(manager, make_repo(tmp_path / 'repo'))
        manager.start_task(OWNER, request_id)
        manager.apply_directory_change(OWNER, manager.read_snapshot(OWNER)['version'], {'profile': WIKI})
        with ManagementServer(manager, {'owner': OWNER, 'lead': LEAD}):
            owner, lead = ManagementClient(tmp_path / 'state', 'owner'), ManagementClient(tmp_path / 'state', 'lead')
            query = prepared_facts(manager, owner, lead, request_id)
            completed_delivery(tmp_path, owner, request_id)
            with pytest.raises(ManagementError) as rejected:
                lead.curate_project_memory('mono-lead', 'unverified-material', request_id,
                    {'facts': [{'query_id': query, 'material_ids': ['retry:3']}], 'decisions': [], 'include_delivery': True})
            assert rejected.value.code == 'evidence_missing'
            with pytest.raises(ManagementError):
                owner.record_memory_preference('mono-lead', 'unsafe-preference', 'API_KEY=synthetic-sensitive-value', {'kind': 'project', 'id': 'mono'})
            with pytest.raises(ManagementError):
                lead.curate_project_memory('mono-lead', 'temporary-status', request_id,
                    {'facts': [], 'decisions': [], 'include_delivery': True, 'temporary_status': 'DSH claims complete'})
            assert lead.read_project_memory('mono-lead')['entries'] == []


def test_factual_answer_uses_manual_original_service_and_returned_grant_cannot_answer_again(tmp_path):
    from test_task_execution import adapter_for
    from test_manual_control import original_state, adapters, setup, ORIGINAL_THREAD, ORIGINAL_TURN
    repo = make_repo(tmp_path / 'repo')
    peer = tmp_path / 'original'
    original_state(peer, repo)
    read, control = adapters(peer)
    with Manager(tmp_path / 'state', owner_identity_ref=OWNER.subject, dsh_adapter=adapter_for(tmp_path),
                 observation_adapters={'local:manual-desktop': read}, control_adapters={'manual-desktop': control},
                 knowledge_providers={'local:fixture-wiki': local_provider(tmp_path)}) as manager:
        request_id, observed = setup(manager, repo)
        manager.apply_directory_change(OWNER, manager.read_snapshot(OWNER)['version'], {'profile': WIKI})
        with ManagementServer(manager, {'owner': OWNER, 'lead': LEAD}):
            owner, lead = ManagementClient(tmp_path / 'state', 'owner'), ManagementClient(tmp_path / 'state', 'lead')
            owner.take_over_session(request_id, observed['id'], 'factual-original-grant', ORIGINAL_TURN)
            query = prepared_facts(manager, owner, lead, request_id)
            state = json.loads((peer / 'original-state.json').read_text())
            state['server_requests'] = [user_question(61, threadId=ORIGINAL_THREAD, turnId=ORIGINAL_TURN,
                questions=[{'id': 'policy', 'header': 'Known fact', 'question': QUESTION, 'isSecret': False, 'isOther': True, 'options': None}])]
            (peer / 'original-state.json').write_text(json.dumps(state))
            q = owner.refresh_task(request_id)['human_requests'][0]
            answered = lead.answer_from_knowledge(request_id, q['id'], query, ['retry:3'])
            assert answered['reply']['sent'] == 'sent' and answered['thread_id'] == ORIGINAL_THREAD
            owner.return_session_control(request_id, 'factual-original-grant')
            with pytest.raises(ManagementError):
                lead.answer_from_knowledge(request_id, q['id'], query, ['retry:3'])
            owner.refresh_task(request_id)
        wire = list(map(json.loads, (peer / 'original-wire.jsonl').read_text().splitlines()))
        sent = [r for r in wire if 'method' not in r]
        assert len(sent) == 1 and sent[0]['id'] == 61 and 'retry.md#L3' in json.dumps(sent[0])
        assert not any(r.get('method') in {'fixture/start', 'fixture/create', 'thread/resume', 'thread/fork'} for r in wire)
        assert not (tmp_path / 'wire.jsonl').exists()
