"""Known facts and curated memory through the authenticated management entry."""
import json
import pytest

from ghost_hermes_pm import Manager, ManagementError, VerifiedIdentity
from ghost_hermes_pm.transport import ManagementClient, ManagementServer
from test_directory import OWNER, make_repo
from test_task_execution import accepted, prepare_fixture
from test_questions import question_adapter, emit, user_question, replies
from test_knowledge import WIKI, local_provider, public_grant

LEAD = VerifiedIdentity('fixture:lead', 'synthetic-participant')
QUESTION = 'What is the retry delivery policy?'


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
    with Manager(tmp_path / 'state', owner_identity_ref=OWNER.subject, codex_adapter=question_adapter(tmp_path),
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
        answer = sent[0]['result']['answers']['policy']['answers'][0]
        assert 'Retry only definite failures.' in answer and 'retry.md#L3' in answer and 'sha256:' in answer
