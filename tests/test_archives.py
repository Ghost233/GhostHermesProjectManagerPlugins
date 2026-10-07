"""Public migration archive query and read-only source coverage."""
import hashlib
import json
import sqlite3

from ghost_hermes_pm import Manager, VerifiedIdentity
from ghost_hermes_pm.archives import HermesArchiveProvider
from ghost_hermes_pm.transport import ManagementClient, ManagementServer
from test_directory import OWNER
from test_knowledge import WIKI, source_grant


NEW = {**WIKI, 'id': 'new-profile', 'native_profile': 'new-profile', 'identity_ref': 'fixture:new-profile'}


def synthetic_history(root):
    path = root / 'old-state.db'
    with sqlite3.connect(path) as db:
        db.executescript('CREATE TABLE sessions (id TEXT PRIMARY KEY,parent_session_id TEXT,end_reason TEXT,pinned INTEGER,ended_at REAL); '
                         'CREATE TABLE messages (id INTEGER PRIMARY KEY,session_id TEXT,role TEXT,content TEXT,timestamp REAL,active INTEGER,_compressed_summary INTEGER);')
        db.executemany('INSERT INTO sessions VALUES(?,?,?,?,?)', [('old-root',None,'compression',0,1),('old-tip','old-root','user_close',0,2)])
        db.executemany('INSERT INTO messages VALUES(?,?,?,?,?,?,?)', [
            (1,'old-root','user','Original retry requirement',1,0,0),
            (2,'old-root','assistant','Keep unknown outcomes for reconciliation',2,0,0),
            (3,'old-tip','system','Earlier segment summary',3,1,1),
            (4,'old-tip','assistant','Delivery needs actual receipts',4,1,0)])
    return path


def migration_registration():
    return {'id':'old-hermes','kind':'hermes_local','provider_ref':'local:old-hermes',
            'grant_source_id':'fixture-wiki','new_profile_id':'new-profile','scope_ids':['public'],
            'authorization_ref':'owner:explicit-migration-28'}


def test_public_archive_query_reads_complete_compression_pages_and_never_reopens_old_entry(tmp_path):
    path = synthetic_history(tmp_path)
    before = hashlib.sha256(path.read_bytes()).hexdigest()
    provider = HermesArchiveProvider(path, {'public':['old-root']}, page_size=1)
    viewer = VerifiedIdentity(NEW['identity_ref'], 'verified-new-profile-entry')
    with Manager(tmp_path / 'state', owner_identity_ref=OWNER.subject, archive_providers={'local:old-hermes':provider}) as manager:
        manager.apply_directory_change(OWNER, 0, {'profile':WIKI})
        manager.apply_directory_change(OWNER, 1, {'profile':NEW})
        grant = source_grant(); grant['query_subjects'][viewer.subject] = ['public']
        manager.register_knowledge_source(OWNER, 2, grant)
        with ManagementServer(manager, {'owner':OWNER,'new':viewer}):
            owner = ManagementClient(tmp_path / 'state','owner')
            owner.register_archive_source(migration_registration())
            result = ManagementClient(tmp_path / 'state','new').query_archive('old-hermes','archive-query','retry', ['public'], complete=True)
            assert result['status'] == 'complete'
            assert result['kind'] == 'hermes_local'
            assert [r['text'] for r in result['records']] == ['Original retry requirement','Keep unknown outcomes for reconciliation','Earlier segment summary','Delivery needs actual receipts']
            assert result['coverage']['session_ids'] == ['old-root','old-tip']
            assert result['coverage']['pages'] == 4
            assert result['coverage']['compressed_history'] == 'included'
            assert result['source_version']
            assert result['records'][0]['locator'].endswith('old-root#message-1')
            assert ManagementClient(tmp_path / 'state','new').read_snapshot()['archive_queries'][0]['source_id'] == 'old-hermes'
            assert owner.read_snapshot()['requests'] == []
    assert hashlib.sha256(path.read_bytes()).hexdigest() == before
    with sqlite3.connect(path) as db:
        assert db.execute('SELECT ended_at FROM sessions ORDER BY id').fetchall() == [(1.0,),(2.0,)]


def setup_archive(tmp_path, provider=None):
    path = synthetic_history(tmp_path)
    manager = Manager(tmp_path / 'state', owner_identity_ref=OWNER.subject,
        archive_providers={'local:old-hermes':provider or HermesArchiveProvider(path, {'public':['old-root']}, page_size=1)})
    manager.apply_directory_change(OWNER, 0, {'profile':WIKI})
    manager.apply_directory_change(OWNER, 1, {'profile':NEW})
    viewer = VerifiedIdentity(NEW['identity_ref'], 'verified-migration')
    grant = source_grant(); grant['query_subjects'][viewer.subject] = ['public']
    manager.register_knowledge_source(OWNER, 2, grant)
    manager.register_archive_source(OWNER, migration_registration())
    return manager, viewer, path


def test_archive_grants_duplicates_and_revoked_results_are_not_implicit_owner_or_wiki_access(tmp_path):
    import pytest
    from ghost_hermes_pm.manager import ManagementError
    manager, viewer, path = setup_archive(tmp_path)
    with manager, ManagementServer(manager, {'owner':OWNER,'new':viewer,'wiki':VerifiedIdentity(WIKI['identity_ref'],'wiki')}):
        client = ManagementClient(tmp_path / 'state','new')
        with pytest.raises(ManagementError, match='own migration'):
            ManagementClient(tmp_path / 'state','wiki').query_archive('old-hermes','wrong','retry',['public'])
        with pytest.raises(ManagementError, match='verified owner'):
            client.register_archive_source(migration_registration())
        with pytest.raises(ManagementError):
            client.query_archive('old-hermes','private','retry',['private'])
        first = client.query_archive('old-hermes','stable','retry',['public'])
        assert client.query_archive('old-hermes','stable','retry',['public']) == first
        with pytest.raises(ManagementError, match='another scope'):
            client.query_archive('old-hermes','stable','changed',['public'])
        grant = source_grant()
        manager.register_knowledge_source(OWNER, manager.read_snapshot(OWNER)['version'], grant)
        assert client.read_snapshot()['archive_queries'][0]['status'] == 'grant_revoked'
        assert client.read_snapshot()['archive_queries'][0]['records'] == []


def test_complete_archive_missing_compression_segment_is_explicit_and_search_has_context(tmp_path):
    manager, viewer, path = setup_archive(tmp_path)
    with sqlite3.connect(path) as db:
        db.execute('DELETE FROM sessions WHERE id=?', ('old-tip',))
    with manager:
        result = manager.query_archive(viewer,'old-hermes','missing','retry',['public'],True)
        assert result['status'] == 'incomplete'
        assert result['coverage']['end_confirmed'] is False
        assert result['coverage']['missing'] == ['old-root:compression-continuation']
        assert result['coverage']['original_rows'] == 2


def test_source_cannot_inject_authority_or_secret_result(tmp_path):
    class Bad:
        kind = 'hermes_local'
        def query(self, requester, scope_ids, question, complete):
            return {'kind':self.kind,'requester':requester,'searched_scope':scope_ids,'status':'complete',
                'records':[],'new_identity_ref':'evil','complete':True}
    manager, viewer, _ = setup_archive(tmp_path, Bad())
    with manager:
        result = manager.query_archive(viewer,'old-hermes','bad','retry',['public'],True)
        assert result['status'] == 'blocked'
        assert 'new_identity_ref' not in result
        assert result['records'] == []


def test_checkpoint_daily_rotation_and_restore_query_preserve_source_and_longterm(tmp_path):
    manager, viewer, path = setup_archive(tmp_path)
    original = path.read_bytes()
    with manager, ManagementServer(manager, {'owner':OWNER,'new':viewer}):
        owner = ManagementClient(tmp_path / 'state','owner')
        baseline = owner.backup_archive('old-hermes','baseline-28','baseline')
        assert baseline['status'] == 'complete'
        assert baseline['consistency'] == 'sqlite_backup'
        for day in range(1,10):
            with sqlite3.connect(path) as db:
                db.execute('UPDATE messages SET content=? WHERE id=4', ('changed day '+str(day),))
            manager.run_archive_daily('2026-10-'+str(day).zfill(2))
        backups = owner.read_snapshot()['archive_backups']
        daily = [b for b in backups if b['kind']=='daily' and b['status']=='complete']
        assert len(daily) == 7
        assert {b['date'] for b in daily} == {'2026-10-'+str(d).zfill(2) for d in range(3,10)}
        assert any(b['id']=='baseline-28' for b in backups)
        assert path.exists() and path.read_bytes() != original
        manager.run_archive_daily('2026-10-10')
        assert len([b for b in owner.read_snapshot()['archive_backups'] if b['kind']=='daily' and b['status']=='complete']) == 7
        restored = owner.restore_archive('baseline-28','restore-28')
        assert restored['status'] == 'verified'
        assert restored['query']['status'] == 'complete'
        assert restored['query']['records'][0]['text'] == 'Original retry requirement'
        assert restored['old_entry'] == 'not_started'
        assert path.read_bytes() != original  # the original changed source was never overwritten
        assert owner.read_snapshot()['requests'] == []
        assert owner.restore_archive('baseline-28','restore-28') == restored


def test_codex_archive_full_turn_item_pagination_is_original_read_only_and_compaction_gaps_explicit(tmp_path):
    import sys
    from pathlib import Path
    from ghost_hermes_pm.archive_sources import CodexArchiveProvider
    from ghost_hermes_pm.observation import ReadOnlyCodexAdapter, READ_METHODS
    state={'thread':{'id':'old-thread','status':{'type':'idle'},'updatedAt':7,'historyMode':'paginated','turns':[]},
        'turns':{'':{'data':[{'id':'turn-1','itemsView':'summary','startedAt':3}],'nextCursor':'turn-page-2'},
            'turn-page-2':{'data':[{'id':'turn-2','itemsView':'full','startedAt':4}],'nextCursor':None}},
        'items':{'turn-1':{'':{'data':[{'id':'i1','type':'userMessage','text':'Original requirement'}],'nextCursor':'item-page-2'},
            'item-page-2':{'data':[{'id':'i2','type':'agentMessage','text':'Actual response'}],'nextCursor':None}},
            'turn-2':{'':{'data':[{'id':'i3','type':'contextCompaction'}],'nextCursor':None}}}}
    (tmp_path/'archive-peer.json').write_text(json.dumps(state))
    adapter=ReadOnlyCodexAdapter([sys.executable,str(Path(__file__).with_name('archive_fixture_server.py')),str(tmp_path)],
        cwd=tmp_path,env={'PATH':'/usr/bin:/bin','CODEX_HOME':str(tmp_path/'synthetic-home')},service_ref='local:original',source_kind='daemon',endpoint_ref='local:original',
        verifier=lambda binding:{**binding,'original_executor_id':'fixture-original','supported_methods':list(READ_METHODS),'source_kinds':['cli'],
            'evidence_ref':'fixture-current-peer-binding','provenance':'synthetic-original-proxy'})
    try:
        provider=CodexArchiveProvider(adapter,{'public':['old-thread']})
        manager,viewer,_=setup_archive(tmp_path,provider)
        registration=migration_registration();registration['id']='old-codex';registration['kind']='codex_history'
        with manager,ManagementServer(manager,{'new':viewer}):
            manager.register_archive_source(OWNER,registration)
            result=ManagementClient(tmp_path/'state','new').query_archive('old-codex','codex-query','requirement',['public'],True)
            assert result['status']=='incomplete'
            assert result['coverage']['pages']==5
            assert len(result['records'])==3
            assert 'pre-compaction' in result['coverage']['missing'][0]
        assert adapter._closed, 'Manager lifecycle must close the owned read-only archive proxy.'
        wire=[json.loads(line)['method'] for line in (tmp_path/'archive-wire.jsonl').read_text().splitlines()]
        assert set(wire)<={'initialize','initialized','thread/read','thread/turns/list','thread/items/list'}
    finally:
        adapter.close()


def test_feishu_remote_source_real_sdk_builders_follow_pages_and_permission_loss_is_partial(tmp_path):
    from types import SimpleNamespace as N
    from ghost_hermes_pm.archive_sources import FeishuArchiveProvider
    calls=[]
    def read(request):
        calls.append(request)
        params=dict(request.queries)
        if params.get('page_token')=='next':
            return N(code=230002,data=None)
        return N(code=0,data=N(has_more=True,page_token='next',items=[N(message_id='om-1',chat_id='oc-archive',
            create_time='123',msg_type='text',deleted=False,body=N(content='{"text":"Actual old reply"}'))]))
    client=N(config=N(app_id='archive-app'),im=N(v1=N(message=N(list=read))))
    binding={'app_id':'archive-app','tenant_key':'archive-tenant','bot_open_id':'ou-archive'}
    provider=FeishuArchiveProvider(client,binding,{'public':['oc-archive']},lambda native:binding|{'evidence_ref':'synthetic-current-app-tenant'})
    manager,viewer,_=setup_archive(tmp_path,provider)
    registration=migration_registration();registration['id']='old-feishu';registration['kind']='feishu_remote'
    with manager:
        manager.register_archive_source(OWNER,registration)
        result=manager.query_archive(viewer,'old-feishu','remote-query','reply',['public'],True)
        assert result['status']=='incomplete'
        assert result['records'][0]['locator']=='feishu-archive:oc-archive#om-1'
        assert result['coverage']['pages']==2
        assert result['coverage']['source_identity']==binding
        assert calls[0].http_method.name=='GET'
        assert dict(calls[1].queries)['page_token']=='next'
        backup=manager.backup_archive(OWNER,'old-feishu','remote-backup')
        assert backup['status']=='blocked'
        assert 'external original data service' in backup['reason']


def test_owner_public_archive_query_uses_real_reply_builder_and_duplicate_has_no_new_delivery_or_issue(tmp_path):
    import asyncio
    from ghost_hermes_pm.messages import FeishuEntry
    from test_knowledge import native_transport
    from test_feishu_entry import CONFIG, Gateway, event
    manager,viewer,path=setup_archive(tmp_path)
    channel={'id':'archive-public','profile_id':'new-profile','app_id':'cli_fixture','transport_tenant_key':'tenant-transport',
        'recipient_tenant_key':'tenant-bot','recipient_open_id':'ou_lead','chat_id':'oc_project','scope_ids':['public'],
        'view_subjects':[OWNER.subject,viewer.subject],'wiki_mention_open_id':'ou_unused'}
    grant=source_grant();grant['query_subjects'][viewer.subject]=['public'];grant['public_channels']=[channel]
    sent=[]
    config={**CONFIG,'bindings':[{**CONFIG['bindings'][0],'profile_id':'new-profile','project_id':None}]}
    with manager:
        manager.register_knowledge_source(OWNER,manager.read_snapshot(OWNER)['version'],grant)
        adapter=object()
        intake=FeishuEntry(lambda:manager,OWNER.subject,config,lambda url:None)
        intake.attach_transport(adapter,native_transport('cli_fixture','ou_lead','om_archive_',sent))
        incoming=event('@_user_1 完整档案 old-hermes public：retry','om_archive_question')
        assert asyncio.run(intake.receive(incoming,Gateway(adapter)))=={'action':'skip'}
        snapshot=manager.read_snapshot(OWNER)
        assert snapshot['archive_queries'][0]['status']=='complete'
        assert snapshot['requests']==[]
        assert sent[0].message_id=='om_archive_question'
        content=json.loads(sent[0].request_body.content)['zh_cn']['content'][0]
        assert content[0]=={'tag':'at','user_id':'ou_owner'}
        assert 'hermes-archive:old-root#message-1' in content[1]['text']
        assert asyncio.run(intake.receive(incoming,Gateway(adapter)))=={'action':'skip'}
        assert len(sent)==len(snapshot['archive_queries'][0]['outbox'][0]['segments'])


def test_dashboard_archive_query_backup_restore_uses_same_authenticated_state(tmp_path):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from ghost_hermes_pm.dashboard import create_router
    manager,viewer,_=setup_archive(tmp_path)
    with manager,ManagementServer(manager,{'owner':OWNER}):
        client=ManagementClient(tmp_path/'state','owner')
        app=FastAPI();app.include_router(create_router(lambda request:client))
        with TestClient(app) as browser:
            result=browser.post('/archives',json={'action':'query','source_id':'old-hermes','query_id':'dashboard-query',
                'question':'retry','scope_ids':['public'],'complete':True})
            assert result.status_code==200,result.text
            assert result.json()['records'][0]['locator'].endswith('#message-1')
            assert browser.get('/snapshot').json()['archive_queries'][0]['id']=='dashboard-query'
            blocked=browser.post('/archives',json={'action':'query','source_id':'old-hermes','query_id':'forged','question':'retry',
                'scope_ids':['public'],'owner':True})
            assert blocked.status_code==422


def test_sealed_local_materials_are_consistent_data_backups_and_restore_actually_reads_them(tmp_path):
    manager,viewer,path=setup_archive(tmp_path)
    sealed=tmp_path/'migration-notes.md';sealed.write_text('Original migration decision: preserve every uncertain result.\n')
    provider=HermesArchiveProvider(path,{'public':['old-root']},files={'public':[{'id':'notes','path':str(sealed)}]})
    manager.archive_providers['local:sealed']=provider
    with manager:
        registration=migration_registration();registration.update(id='old-sealed',provider_ref='local:sealed')
        manager.register_archive_source(OWNER,registration)
        first=manager.query_archive(viewer,'old-sealed','sealed','migration',['public'],True)
        assert first['records'][-1]['locator']=='archive-file:notes'
        baseline=manager.backup_archive(OWNER,'old-sealed','sealed-baseline','baseline')
        assert baseline['status']=='complete'
        sealed.write_text('New notes must not replace the fixed checkpoint.\n')
        restored=manager.restore_archive(OWNER,'sealed-baseline','sealed-restore')
        assert restored['status']=='verified',restored
        assert restored['query']['records'][-1]['text'].startswith('Original migration decision')
        assert sealed.read_text().startswith('New notes')


def test_same_day_catchup_keeps_latest_real_version_and_rotation_does_not_claim_missed_dates(tmp_path):
    manager,viewer,path=setup_archive(tmp_path)
    with manager:
        first=manager.run_archive_daily('2026-10-01')[0]
        with sqlite3.connect(path) as db:
            db.execute('UPDATE messages SET content=? WHERE id=4',('Changed after same-day restart',))
        second=manager.run_archive_daily('2026-10-01')[0]
        assert second['source_version']!=first['source_version']
        rows=manager.read_snapshot(OWNER)['archive_backups']
        assert len([b for b in rows if b['kind']=='daily' and b['status']=='complete'])==1
        with sqlite3.connect(path) as db:
            db.execute('UPDATE messages SET content=? WHERE id=4',('Changed during downtime',))
        manager.run_archive_daily('2026-10-05')
        assert {b['date'] for b in manager.read_snapshot(OWNER)['archive_backups'] if b['status']=='complete'}=={'2026-10-01','2026-10-05'}


def test_narrow_archive_grant_cannot_copy_ungranted_sibling_database_or_restore_tampered_checkpoint(tmp_path):
    manager,viewer,path=setup_archive(tmp_path)
    with manager:
        with sqlite3.connect(path) as db:
            db.execute('INSERT INTO sessions VALUES(?,?,?,?,?)',('private-sibling',None,'user_close',0,1))
        refused=manager.backup_archive(OWNER,'old-hermes','narrow-baseline','baseline')
        assert refused['status']=='blocked' and 'all original sessions' in refused['reason']
        with sqlite3.connect(path) as db:
            db.execute('DELETE FROM sessions WHERE id=?',('private-sibling',))
        saved=manager.backup_archive(OWNER,'old-hermes','intact-baseline','baseline')
        from ghost_hermes_pm.archives import _digest
        artifact=manager.state_dir/'archive-backups'/(_digest('intact-baseline')+'.sqlite')
        artifact.write_bytes(b'tampered')
        restored=manager.restore_archive(OWNER,'intact-baseline','bad-restore')
        assert restored['status']=='blocked' and 'hash changed' in restored['reason']


def test_missing_compression_ancestor_and_changed_provider_binding_are_not_complete_or_new_authority(tmp_path):
    manager,viewer,path=setup_archive(tmp_path)
    with manager:
        moved=tmp_path/'other-history.db'
        with sqlite3.connect(path) as src,sqlite3.connect(moved) as dst:
            src.backup(dst)
        manager.archive_providers['local:old-hermes']=HermesArchiveProvider(moved,{'public':['old-root']})
        changed=manager.query_archive(viewer,'old-hermes','changed-source','retry',['public'],True)
        assert changed['status']=='blocked' and 'binding' in changed['reason']
        manager.archive_providers['local:old-hermes']=HermesArchiveProvider(path,{'public':['old-root']})
        with sqlite3.connect(path) as db:
            db.execute('UPDATE sessions SET parent_session_id=? WHERE id=?',('missing-ancestor','old-root'))
        missing=manager.query_archive(viewer,'old-hermes','missing-ancestor','retry',['public'],True)
        assert missing['status']=='incomplete'
        assert 'parent-session-unavailable' in missing['coverage']['missing'][0]


def test_owned_backup_directory_cannot_redirect_copy_outside_manager_state(tmp_path):
    manager,viewer,path=setup_archive(tmp_path)
    other=tmp_path/'unrelated';other.mkdir()
    (manager.state_dir/'archive-backups').symlink_to(other,target_is_directory=True)
    with manager:
        result=manager.backup_archive(OWNER,'old-hermes','redirected','baseline')
        assert result['status']=='blocked' and 'owned' in result['reason']
        assert list(other.iterdir())==[]


def test_unknown_group_archive_delivery_keeps_original_uuid_and_never_replays(tmp_path):
    import asyncio
    from ghost_hermes_pm.messages import FeishuEntry
    from test_feishu_entry import CONFIG,Gateway,event
    from ghost_hermes_pm.archive_delivery import deliver
    manager,viewer,path=setup_archive(tmp_path)
    channel={'id':'archive-public','profile_id':'new-profile','app_id':'cli_fixture','transport_tenant_key':'tenant-transport',
        'recipient_tenant_key':'tenant-bot','recipient_open_id':'ou_lead','chat_id':'oc_project','scope_ids':['public'],
        'view_subjects':[OWNER.subject,viewer.subject],'wiki_mention_open_id':'ou_unused'}
    grant=source_grant();grant['query_subjects'][viewer.subject]=['public'];grant['public_channels']=[channel]
    class Unknown:
        sent=[]
        async def verify_identity(self,binding):
            return {'app_id':'cli_fixture','open_id':'ou_lead'}
        async def send(self,segment):
            self.sent.append(segment)
            return {'status':'unknown'}
    adapter=object();transport=Unknown()
    with manager:
        manager.register_knowledge_source(OWNER,manager.read_snapshot(OWNER)['version'],grant)
        intake=FeishuEntry(lambda:manager,OWNER.subject,{**CONFIG,'bindings':[{**CONFIG['bindings'][0],'profile_id':'new-profile','project_id':None}]},lambda url:None)
        intake.attach_transport(adapter,transport)
        message=event('@_user_1 查档案 old-hermes public：retry','om_unknown_archive')
        assert asyncio.run(intake.receive(message,Gateway(adapter)))=={'action':'skip'}
        result=manager.read_snapshot(OWNER)['archive_queries'][0]
        segment=result['outbox'][0]['segments'][0]
        assert segment['status']=='unknown'
        asyncio.run(deliver(intake,OWNER,result['id'],transport))
        assert asyncio.run(intake.receive(message,Gateway(adapter)))=={'action':'skip'}
        assert len(transport.sent)==1
        assert transport.sent[0]['uuid']==segment['uuid']


def test_live_wal_checkpoint_includes_committed_changes_without_copying_stale_main_file(tmp_path):
    manager,viewer,path=setup_archive(tmp_path)
    with manager,sqlite3.connect(path) as writer:
        writer.execute('PRAGMA journal_mode=WAL')
        writer.execute('UPDATE messages SET content=? WHERE id=4',('Committed data still in the live WAL',))
        writer.commit()
        assert path.with_name(path.name+'-wal').exists()
        baseline=manager.backup_archive(OWNER,'old-hermes','wal-baseline','baseline')
        assert baseline['status']=='complete'
        restored=manager.restore_archive(OWNER,'wal-baseline','wal-restore')
        assert restored['status']=='verified'
        assert restored['query']['records'][3]['text']=='Committed data still in the live WAL'


def test_public_archive_read_can_finish_after_old_three_second_bridge_budget(tmp_path):
    import time
    class Slow(HermesArchiveProvider):
        def query(self,*args,**kwargs):
            time.sleep(3.1)
            return super().query(*args,**kwargs)
    manager,viewer,path=setup_archive(tmp_path)
    manager.archive_providers['local:old-hermes']=Slow(path,{'public':['old-root']})
    with manager,ManagementServer(manager,{'new':viewer}):
        result=ManagementClient(tmp_path/'state','new').query_archive('old-hermes','slow-history','retry',['public'],True)
        assert result['status']=='complete'
        assert ManagementClient(tmp_path/'state','new').read_snapshot()['archive_queries'][0]['id']=='slow-history'


def test_narrow_protection_and_legacy_schema_refuse_before_native_database_write(tmp_path,monkeypatch):
    import sys
    from types import SimpleNamespace
    manager,viewer,path=setup_archive(tmp_path)
    native_calls=[]
    def native_write(path):
        native_calls.append(path)
        raise AssertionError('This native write must never be entered.')
    monkeypatch.setitem(sys.modules,'hermes_state',SimpleNamespace(SessionDB=native_write))
    monkeypatch.setitem(sys.modules,'hermes_state_common',SimpleNamespace(SCHEMA_VERSION=31,SCHEMA_SQL='CREATE TABLE schema_version(version INTEGER);'))
    with manager,ManagementServer(manager,{'owner':OWNER}):
        client=ManagementClient(tmp_path/'state','owner')
        with sqlite3.connect(path) as db:
            db.execute('INSERT INTO sessions VALUES(?,?,?,?,?)',('private-sibling',None,'user_close',0,1))
        before=path.read_bytes()
        narrow=client.protect_archive('old-hermes','narrow-protect')
        assert narrow['status']=='unverified' and 'all original sessions' in narrow['reason']
        assert path.read_bytes()==before and native_calls==[]
        with sqlite3.connect(path) as db:
            db.execute('DELETE FROM sessions WHERE id=?',('private-sibling',))
        registration=migration_registration();registration['id']='legacy-hermes'
        client.register_archive_source(registration)
        before=path.read_bytes()
        legacy=client.protect_archive('legacy-hermes','legacy-protect')
        assert legacy['status']=='unverified' and 'schema' in legacy['reason']
        assert path.read_bytes()==before and native_calls==[]
        with sqlite3.connect(path) as db:
            db.executescript('CREATE TABLE schema_version(version INTEGER);INSERT INTO schema_version VALUES(31);')
        monkeypatch.setitem(sys.modules,'hermes_state_common',SimpleNamespace(SCHEMA_VERSION=31,
            SCHEMA_SQL='CREATE TABLE schema_version(version INTEGER);CREATE TABLE required_native_table(value TEXT NOT NULL);'))
        registration=migration_registration();registration['id']='shape-drift'
        client.register_archive_source(registration)
        before=path.read_bytes()
        drift=client.protect_archive('shape-drift','shape-protect')
        assert drift['status']=='unverified' and 'schema shape' in drift['reason']
        assert path.read_bytes()==before and native_calls==[]
