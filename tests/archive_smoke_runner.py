"""Pristine SDK native automatic cleanup and public migration/restore demonstration."""
from pathlib import Path
import hashlib
import json
import os
import sys
import yaml

scratch=Path(sys.argv[1]).resolve()
protected=[Path.home()/'.hermes',Path.home()/'.codex']
def audit(event,args):
    if event=='open' and isinstance(args[0],(str,bytes)):
        path=Path(os.fsdecode(args[0])).resolve()
        if any(path.is_relative_to(p) for p in protected) or (path.name=='.env' and not path.is_relative_to(scratch)):
            raise RuntimeError('Archive smoke refused real home, histories or credentials.')
    if event=='socket.connect' and isinstance(args[1],tuple):
        raise RuntimeError('Archive smoke refused external network access.')
sys.addaudithook(audit)
os.environ['HERMES_SKIP_PM_BOOTSTRAP']='1'
os.environ['HERMES_DISABLE_PROJECT_PLUGINS']='1'
home=scratch/'home';state=scratch/'state'
settings={'state_dir':str(state),'participant_credential_ref':'native:HERMES_FIXTURE_PARTICIPANT_TOKEN'}
(home/'config.yaml').write_text(yaml.safe_dump({'plugins':{'enabled':['ghost-hermes-pm'],'entries':{'ghost-hermes-pm':{'settings':settings}}}}))
sys.path.insert(0,str(home/'plugins'/'ghost-hermes-pm'))
sys.path.insert(0,str(scratch/'control-fixtures'))
from hermes_state import SessionDB
from ghost_hermes_pm import Manager,VerifiedIdentity
from ghost_hermes_pm.archives import HermesArchiveProvider
from ghost_hermes_pm.transport import ManagementClient,ManagementServer
from test_directory import OWNER
from test_archives import NEW,migration_registration
from test_knowledge import WIKI,source_grant
from hermes_cli.plugins import get_plugin_manager
from tools.registry import registry

original=scratch/'native-original';original.mkdir()
path=original/'state.db'
native=SessionDB(path)
native.create_session('old-root','cli')
first=native.append_message('old-root','user','Original retry requirement',timestamp=1)
native.append_message('old-root','assistant','Unknown outcomes remain unknown',timestamp=2)
native.publish_compression_child(parent_session_id='old-root',child_session_id='old-tip',source='cli',
    messages=[{'role':'system','content':'Earlier compacted summary','_compressed_summary':True,'timestamp':3}],require_compression_lease=False)
native.append_message('old-tip','assistant','Actual delivery receipts',timestamp=4)
native.end_session('old-tip','user_close')
native._write_sql('UPDATE sessions SET ended_at=1,started_at=1,archived=1')
native._write_sql('UPDATE messages SET active=0,compacted=1 WHERE id=?',(first,))
native.close()
provider=HermesArchiveProvider(path,{'public':['old-tip']},page_size=1)
viewer=VerifiedIdentity(NEW['identity_ref'],'native-explicit-migration')
with Manager(state,owner_identity_ref=OWNER.subject,archive_providers={'local:old-hermes':provider}) as authority:
    authority.apply_directory_change(OWNER,0,{'profile':WIKI})
    authority.apply_directory_change(OWNER,1,{'profile':NEW})
    grant=source_grant();grant['query_subjects'][viewer.subject]=['public']
    authority.register_knowledge_source(OWNER,2,grant)
    authority.register_archive_source(OWNER,migration_registration())
    with ManagementServer(authority,{os.environ['HERMES_FIXTURE_OWNER_TOKEN']:OWNER,os.environ['HERMES_FIXTURE_PARTICIPANT_TOKEN']:viewer}):
        plugin_manager=get_plugin_manager();plugin_manager.discover_and_load()
        provider.query(viewer.subject,['public'],'retry',True)
        before=hashlib.sha256(path.read_bytes()).hexdigest()
        query=json.loads(registry.dispatch('hermes_pm_archive',{'source_id':'old-hermes','query_id':'native-query','question':'retry','scope_ids':['public'],'complete':True},scope=str(home)))
        assert query['status']=='complete',query
        assert query['coverage']['session_ids']==['old-root','old-tip'],query
        assert query['coverage']['pages']==4,query
        assert query['records'][0]['compacted'] is True
        assert hashlib.sha256(path.read_bytes()).hexdigest()==before,'Readonly native tool wrote the source.'
        denied=json.loads(registry.dispatch('hermes_pm_archive',{'source_id':'old-hermes','query_id':'forged','question':'retry','scope_ids':['public'],'owner':True},scope=str(home)))
        assert denied['status']=='rejected' and denied['code']=='invalid_change'
        owner=ManagementClient(state,os.environ['HERMES_FIXTURE_OWNER_TOKEN'])
        protection=owner.protect_archive('old-hermes','native-pin-28')
        assert protection['status']=='verified_native_cleanup_copy',protection
        assert protection['archived_unpinned_control']=='deleted',protection
        # Actual source auto-cleanup, with on-disk transcripts: pin, not archived, is the protection.
        sessions_dir=original/'sessions';sessions_dir.mkdir()
        for sid in ('old-root','old-tip','unprotected-archived'):
            (sessions_dir/(sid+'.jsonl')).write_text('Synthetic original transcript '+sid)
        native=SessionDB(path)
        native.create_session('unprotected-archived','cli')
        native.end_session('unprotected-archived','user_close')
        native._write_sql('UPDATE sessions SET ended_at=1,started_at=1,archived=1 WHERE id=?',('unprotected-archived',))
        frozen=provider.query(viewer.subject,['public'],'all',True)
        cleanup=native.maybe_auto_prune_and_vacuum(retention_days=0,min_interval_hours=0,vacuum=False,sessions_dir=sessions_dir)
        assert cleanup['pruned']==1 and not cleanup.get('error'),cleanup
        assert native.get_session('unprotected-archived') is None
        assert not (sessions_dir/'unprotected-archived.jsonl').exists()
        assert all((sessions_dir/(sid+'.jsonl')).exists() and native.get_session(sid)['pinned'] for sid in ('old-root','old-tip'))
        assert all(native.get_session(sid)['ended_at']==1 for sid in ('old-root','old-tip')),'Protection reopened an old entry.'
        native.close()
        assert provider.query(viewer.subject,['public'],'all',True)['source_version']==frozen['source_version']
        baseline=owner.backup_archive('old-hermes','native-baseline','baseline')
        assert baseline['status']=='complete',baseline
        restored=owner.restore_archive('native-baseline','native-restored')
        assert restored['status']=='verified' and restored['query']['source_version']==baseline['source_version'],restored
        assert restored['query']['records'][0]['text']=='Original retry requirement',restored
        assert owner.read_snapshot()['requests']==[]
        assert plugin_manager.unload('ghost-hermes-pm')
        assert registry.get_entry('hermes_pm_archive',scope=str(home)) is None
print('native archive: actual pinned compression/compaction+transcripts survived automatic prune; archived unpinned control deleted; SQLite checkpoint restored and queried: OK')
print('native load, Dashboard bridge, restart, teardown: OK')
