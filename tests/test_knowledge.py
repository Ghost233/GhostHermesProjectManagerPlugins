"""Public knowledge boundaries with actual local material and protocol peers."""
import hashlib
import json
from pathlib import Path

from ghost_hermes_pm import Manager, VerifiedIdentity
from ghost_hermes_pm.knowledge import LocalKnowledgeProvider
from ghost_hermes_pm.transport import ManagementClient, ManagementServer
from test_directory import OWNER


WIKI = {'id': 'wiki', 'native_profile': 'wiki', 'identity_ref': 'fixture:wiki', 'role': 'independent',
        'capability': 'non_development', 'project_id': None, 'parent_profile_id': None, 'connection_refs': {}}


def source_grant():
    return {'id': 'fixture-wiki', 'name': 'Fixture knowledge', 'provider_ref': 'local:fixture-wiki',
            'wiki_profile_id': 'wiki', 'query_subjects': {OWNER.subject: ['public']},
            'public_channels': [], 'task_profiles': [], 'wiki_bindings': []}


def local_provider(root):
    wiki = root / 'source'; wiki.mkdir()
    (wiki / 'retry.md').write_text('Retry policy\n\nRetry only definite failures. Unknown delivery needs reconciliation.\n')
    (wiki / 'private.md').write_text('PRIVATE material must never enter the public result.\n')
    return LocalKnowledgeProvider(wiki, [
        {'id': 'retry', 'path': 'retry.md', 'scope_id': 'public', 'kind': 'fact', 'terms': ['retry', 'delivery']},
        {'id': 'private', 'path': 'private.md', 'scope_id': 'private', 'kind': 'fact', 'terms': ['retry']}])


def test_token_query_reads_actual_allowed_material_and_provenance_without_issue_or_source_write(tmp_path):
    provider = local_provider(tmp_path)
    before = hashlib.sha256((tmp_path / 'source' / 'retry.md').read_bytes()).hexdigest()
    with Manager(tmp_path / 'state', owner_identity_ref=OWNER.subject,
                 knowledge_providers={'local:fixture-wiki': provider}) as manager:
        manager.apply_directory_change(OWNER, 0, {'profile': WIKI})
        with ManagementServer(manager, {'owner-entry': OWNER}):
            client = ManagementClient(tmp_path / 'state', 'owner-entry')
            client.register_knowledge_source(1, source_grant())
            result = client.query_knowledge('fixture-wiki', 'query-1', 'retry delivery', ['public'])
            snapshot = client.read_snapshot()
        assert result['status'] == 'found'
        assert result['requester'] == OWNER.subject
        assert result['materials'][0]['text'] == 'Retry only definite failures. Unknown delivery needs reconciliation.'
        assert result['materials'][0]['kind'] == 'fact'
        assert result['materials'][0]['locator'].endswith('retry.md#L3')
        assert result['materials'][0]['version'] == 'sha256:' + before
        assert result['materials'][0]['updated_at']
        assert result['searched_scope'] == ['public']
        assert 'PRIVATE' not in json.dumps(result)
        assert snapshot['requests'] == []
        assert snapshot['knowledge_queries'][0]['id'] == 'query-1'
        assert snapshot['knowledge_sources'][0]['id'] == 'fixture-wiki'
    assert hashlib.sha256((tmp_path / 'source' / 'retry.md').read_bytes()).hexdigest() == before


def test_wiki_and_superior_membership_cannot_replace_original_requester_source_grants(tmp_path):
    import pytest
    from ghost_hermes_pm import ManagementError
    provider = local_provider(tmp_path)
    superior = {**WIKI, 'id': 'steward', 'native_profile': 'steward', 'identity_ref': 'fixture:steward', 'role': 'steward'}
    wiki_identity = VerifiedIdentity(WIKI['identity_ref'], 'fixture-wiki-entry')
    steward_identity = VerifiedIdentity(superior['identity_ref'], 'fixture-steward-entry')
    with Manager(tmp_path / 'state', owner_identity_ref=OWNER.subject, knowledge_providers={'local:fixture-wiki': provider}) as manager:
        manager.apply_directory_change(OWNER, 0, {'profile': WIKI})
        manager.apply_directory_change(OWNER, 1, {'profile': superior})
        with ManagementServer(manager, {'owner': OWNER, 'wiki': wiki_identity, 'steward': steward_identity}):
            owner = ManagementClient(tmp_path / 'state', 'owner')
            owner.register_knowledge_source(2, source_grant())
            for token in ('owner', 'wiki', 'steward'):
                with pytest.raises(ManagementError) as denied:
                    ManagementClient(tmp_path / 'state', token).query_knowledge('fixture-wiki', token + '-private', 'retry', ['private'])
                assert denied.value.code == 'forbidden'
            assert owner.read_snapshot()['knowledge_queries'] == []
            assert owner.read_snapshot()['requests'] == []


def test_duplicate_query_keeps_fixed_result_and_material_is_hidden_after_grant_revocation(tmp_path):
    provider = local_provider(tmp_path)
    with Manager(tmp_path / 'state', owner_identity_ref=OWNER.subject, knowledge_providers={'local:fixture-wiki': provider}) as manager:
        manager.apply_directory_change(OWNER, 0, {'profile': WIKI})
        with ManagementServer(manager, {'owner': OWNER}):
            client = ManagementClient(tmp_path / 'state', 'owner')
            client.register_knowledge_source(1, source_grant())
            first = client.query_knowledge('fixture-wiki', 'same-query', 'retry delivery', ['public'])
            (tmp_path / 'source' / 'retry.md').write_text('Retry policy\n\nChanged material must not replace a repeated fixed result.\n')
            repeated = client.query_knowledge('fixture-wiki', 'same-query', 'retry delivery', ['public'])
            assert repeated['materials'] == first['materials']
            changed = source_grant(); changed['query_subjects'] = {WIKI['identity_ref']: ['public']}
            client.register_knowledge_source(client.read_snapshot()['version'], changed)
            snapshot = client.read_snapshot()
            assert all(not q.get('materials') for q in snapshot['knowledge_queries'])
            assert 'Retry only definite failures' not in json.dumps(snapshot)
