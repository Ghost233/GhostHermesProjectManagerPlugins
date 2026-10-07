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
            assert owner.read_snapshot()['archive_queries'][0]['source_id'] == 'old-hermes'
            assert owner.read_snapshot()['requests'] == []
    assert hashlib.sha256(path.read_bytes()).hexdigest() == before
    with sqlite3.connect(path) as db:
        assert db.execute('SELECT ended_at FROM sessions ORDER BY id').fetchall() == [(1.0,),(2.0,)]
