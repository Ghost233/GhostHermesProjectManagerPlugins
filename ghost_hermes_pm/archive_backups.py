"""Consistent local data checkpoints, seven changed-day copies, and query-verified restore."""
from datetime import date
import hashlib
from pathlib import Path
import sqlite3
import shutil
import uuid

from .manager import ManagementError, VerifiedIdentity
from .archives import HermesArchiveProvider, _source, _now, _digest, provider_binding


def _owner(manager,identity,data):
    if manager._principal(identity,data) is not None:
        raise ManagementError('forbidden','Only the verified owner may protect, checkpoint or restore original data.')


def _provider(manager,source):
    provider=manager.archive_providers.get(source['provider_ref'])
    if not isinstance(provider,HermesArchiveProvider):
        raise ManagementError('capability_unverified','This external original data service has no verified local backup/restore or native-retention capability.')
    if provider_binding(provider)!=source.get('provider_binding'):
        raise ManagementError('binding_conflict','The original archive provider binding changed; protection/backup requires owner reconciliation.')
    return provider


def _location(manager,category,identifier):
    supplied=manager.state_dir/category
    if supplied!=supplied.resolve():
        raise ManagementError('invalid_change','Archive artifacts must remain in the owned ordinary Manager directory.')
    base=supplied.resolve()
    base.mkdir(parents=True,exist_ok=True)
    path=base/(_digest(identifier)+'.sqlite')
    if path.is_symlink():
        raise ManagementError('invalid_change','An archive artifact must remain an owned ordinary file.')
    return path


def _copy(provider,target,scopes):
    # Full-file copies are admitted only when the explicit manifest covers every session.
    # A partial session grant cannot become implicit authority for an entire state database.
    records,sessions,coverage,all_ids=provider.read(scopes)
    if not coverage['end_confirmed'] or set(all_ids)!=set(coverage['session_ids']):
        raise ManagementError('forbidden','A consistent full native database backup requires explicit coverage of all original sessions; extend owner-authorized source scopes first.')
    before=provider.query('backup-verification',scopes,'all',True)
    temporary=target.with_name(target.name+'.'+str(uuid.uuid4())+'.tmp')
    try:
        with sqlite3.connect(provider.path.as_uri()+'?mode=ro',uri=True) as src, sqlite3.connect(temporary) as dst:
            src.backup(dst)
            if dst.execute('PRAGMA integrity_check').fetchall()!=[('ok',)] or dst.execute('PRAGMA foreign_key_check').fetchall():
                raise ManagementError('archive_incomplete','The consistent archive checkpoint did not pass SQLite integrity checks.')
        files={}
        directory=target.with_name(target.name+'.files')
        if provider.files:
            if directory.is_symlink():
                raise ManagementError('invalid_change','Sealed file snapshots must remain in the owned backup directory.')
            directory.mkdir(mode=0o700,exist_ok=True)
            for scope in scopes:
                files[scope]=[]
                for row in provider.files.get(scope,[]):
                    destination=directory/(_digest(row['id'])+'.data')
                    if destination.is_symlink():
                        raise ManagementError('invalid_change','A sealed artifact cannot follow a symbolic link.')
                    shutil.copy2(row['path'],destination)
                    destination.chmod(0o600)
                    files[scope].append({'id':row['id'],'path':str(destination)})
        copied=HermesArchiveProvider(temporary,provider.session_scopes,provider.page_size,files=files)
        query=copied.query('backup-verification',scopes,'all',True)
        _,_,copied_coverage,copied_all=copied.read(scopes)
        after=provider.query('backup-verification',scopes,'all',True)
        if query['status']!='complete' or set(copied_all)!=set(copied_coverage['session_ids']) or query['source_version']!=before['source_version'] or query['source_version']!=after['source_version']:
            raise ManagementError('archive_incomplete','Source coverage changed during the consistent backup; no complete checkpoint was published.')
        temporary.chmod(0o600)
        temporary.replace(target)
        return query, files
    finally:
        if temporary.exists():
            temporary.unlink()


def backup(manager,identity,source_id,backup_id,kind='checkpoint',day=None):
    with manager._lock:
        version,data=manager._load()
        _owner(manager,identity,data)
        if not isinstance(backup_id,str) or not backup_id or len(backup_id)>256 or kind not in {'baseline','checkpoint','daily'}:
            raise ManagementError('invalid_change','A stable backup ID and explicit checkpoint kind are required.')
        source=data.get('archive_sources',{}).get(source_id)
        if not source:
            raise ManagementError('invalid_change','Unknown original archive source.')
        _source(manager,identity,source_id,source['scope_ids'],data)
        backups=data.setdefault('archive_backups',{})
        existing=backups.get(backup_id)
        if existing:
            if (existing['source_id'],existing['kind'],existing.get('date'))!=(source_id,kind,day):
                raise ManagementError('binding_conflict','The backup ID already identifies another checkpoint.')
            return existing
        row={'id':backup_id,'source_id':source_id,'kind':kind,'date':day,'status':'copying','created_at':_now(),
            'source_registration':dict(source),'grant_revision':data['knowledge_sources'][source['grant_source_id']]['revision'],
            'old_entry':'not_started','restore_scope':'data_only_no_profile_or_entry_activation'}
        backups[backup_id]=row
        with manager._db: manager._save(version,data)
        try:
            provider=_provider(manager,source)
            path=_location(manager,'archive-backups',backup_id)
            query,files=_copy(provider,path,source['scope_ids'])
            row.update(status='complete',artifact_ref='local:archive-backup:'+_digest(backup_id),
                file_sha256=hashlib.sha256(path.read_bytes()).hexdigest(),source_version=query['source_version'],
                coverage=query['coverage'],consistency='sqlite_backup_with_stable_files' if files else 'sqlite_backup',session_scopes=provider.session_scopes,files=files,
                recorded_at=_now())
        except Exception as exc:
            row.update(status='blocked',reason=str(exc) if isinstance(exc,ManagementError) else 'Native backup integrity could not be verified.')
        version,data=manager._load()
        data['archive_backups'][backup_id]=row
        if row['status']=='complete' and kind=='daily':
            daily=sorted((b for b in data['archive_backups'].values() if b['source_id']==source_id and b['kind']=='daily' and b['status']=='complete'),key=lambda b:b['date'])
            keep=list({b['date']:b['id'] for b in daily}.values())[-7:]
            for old in [b for b in daily if b['id'] not in keep]:
                old_path=_location(manager,'archive-backups',old['id'])
                old_path.unlink(missing_ok=True)
                old_files=old_path.with_name(old_path.name+'.files')
                if old_files.exists() and not old_files.is_symlink():
                    shutil.rmtree(old_files)
                old.update(status='rotated',reason='Only this owned daily copy was rotated; originals, baseline and long-term checkpoints remain.')
        with manager._db: manager._save(version,data)
        return row


def daily(manager,day=None):
    day=day or _now()[:10]
    try:
        if date.fromisoformat(day).isoformat()!=day:
            raise ValueError
    except (ValueError,TypeError):
        raise ManagementError('invalid_change','Daily snapshots use a trusted host ISO calendar date.')
    outcomes=[]
    with manager._lock:
        _,data=manager._load()
        sources=list(data.get('archive_sources',{}).values())
    identity=VerifiedIdentity(manager.owner_identity_ref,'trusted-native-archive-scheduler')
    for source in sources:
        provider=manager.archive_providers.get(source['provider_ref'])
        if not isinstance(provider,HermesArchiveProvider):
            continue
        try:
            current=provider.query(identity.subject,source['scope_ids'],'all',True)
            with manager._lock:
                _,data=manager._load()
                previous=next(iter(sorted((b for b in data.get('archive_backups',{}).values() if b['source_id']==source['id'] and b['status']=='complete'),key=lambda b:b['created_at'],reverse=True)),None)
            if previous and previous.get('source_version')==current['source_version']:
                outcomes.append({'source_id':source['id'],'date':day,'status':'unchanged'})
                continue
            outcomes.append(backup(manager,identity,source['id'],'daily:'+source['id']+':'+day+':'+current['source_version'],'daily',day))
        except ManagementError as exc:
            outcomes.append({'source_id':source['id'],'date':day,'status':'blocked','reason':str(exc)})
    return outcomes


def restore(manager,identity,backup_id,restore_id):
    with manager._lock:
        version,data=manager._load()
        _owner(manager,identity,data)
        if not isinstance(restore_id,str) or not restore_id or len(restore_id)>256:
            raise ManagementError('invalid_change','An explicit stable restore ID is required.')
        saved=data.get('archive_backups',{}).get(backup_id)
        if not saved or saved['status']!='complete':
            raise ManagementError('capability_unverified','No complete retained data checkpoint can be restored.')
        source,_=_source(manager,identity,saved['source_id'],saved['source_registration']['scope_ids'],data)
        if any(source.get(k)!=saved['source_registration'].get(k) for k in ('provider_ref','new_identity_ref','grant_source_id','scope_ids')):
            raise ManagementError('binding_conflict','The source authority changed; restore requires reconciliation.')
        restores=data.setdefault('archive_restores',{})
        if restore_id in restores:
            if restores[restore_id]['backup_id']!=backup_id:
                raise ManagementError('binding_conflict','This restore ID already identifies another checkpoint.')
            return restores[restore_id]
        row={'id':restore_id,'backup_id':backup_id,'source_id':source['id'],'status':'restoring','created_at':_now(),'old_entry':'not_started'}
        restores[restore_id]=row
        with manager._db: manager._save(version,data)
        try:
            original=_location(manager,'archive-backups',backup_id)
            if hashlib.sha256(original.read_bytes()).hexdigest()!=saved['file_sha256']:
                raise ManagementError('archive_incomplete','The retained checkpoint hash changed; restore was refused.')
            target=_location(manager,'archive-restores',restore_id)
            copied=HermesArchiveProvider(original,saved['session_scopes'],files=saved.get('files'))
            _,files=_copy(copied,target,source['scope_ids'])
            # The restored artifact, not the original, is actually opened and queried here.
            query=HermesArchiveProvider(target,saved['session_scopes'],files=files).query(identity.subject,source['scope_ids'],'all',True)
            if query['source_version']!=saved['source_version'] or query['status']!='complete':
                raise ManagementError('archive_incomplete','The restored data did not reproduce complete checkpoint query coverage.')
            row.update(status='verified',query=query,artifact_ref='local:archive-restore:'+_digest(restore_id),
                integrity='sqlite_integrity_and_query',profile_state='not_restored',external_service_restore='unverified')
        except Exception as exc:
            row.update(status='blocked',reason=str(exc) if isinstance(exc,ManagementError) else 'Restored data could not be verified by querying.')
        version,data=manager._load()
        data['archive_restores'][restore_id]=row
        with manager._db: manager._save(version,data)
        return row


def protect(manager,identity,source_id,protection_id):
    """Pin originals explicitly, then exercise real native auto-cleanup on a consistent copy."""
    with manager._lock:
        version,data=manager._load()
        _owner(manager,identity,data)
        source=data.get('archive_sources',{}).get(source_id)
        if not source or not isinstance(protection_id,str) or not protection_id:
            raise ManagementError('invalid_change','A registered source and stable protection operation are required.')
        _source(manager,identity,source_id,source['scope_ids'],data)
        if source['protection'].get('id'):
            if source['protection']['id']!=protection_id:
                raise ManagementError('binding_conflict','Protection already has a durable operation; reconcile its state first.')
            return source['protection']
        evidence={'id':protection_id,'status':'pin_intent','created_at':_now(),'old_entry':'not_started','permanent_protection':'unverified','ordinary_tool_read_write_delete_boundary':'unverified','original_source_cleanup':'not_run'}
        source['protection']=evidence
        with manager._db: manager._save(version,data)
        try:
            import hermes_state
            provider=_provider(manager,source)
            _,_,coverage,_=provider.read(source['scope_ids'])
            if not coverage['end_confirmed']:
                raise ManagementError('archive_incomplete','Missing history cannot be protected or reconstructed by retention.')
            native=hermes_state.SessionDB(provider.path)
            try:
                for sid in coverage['session_ids']:
                    native.set_session_pinned(sid,True)
                if any(not native.get_session(sid).get('pinned') for sid in coverage['session_ids']):
                    raise ManagementError('capability_unverified','The native pin did not cover every protected segment.')
            finally:
                native.close()
            path=_location(manager,'archive-retention-probes',protection_id)
            before,files=_copy(provider,path,source['scope_ids'])
            native=hermes_state.SessionDB(path)
            sentinel='retention-control-'+str(uuid.uuid4())
            try:
                native.create_session(sentinel,'cli')
                native.end_session(sentinel,'user_close')
                native._write_sql('UPDATE sessions SET ended_at=1,started_at=1,archived=1 WHERE id=?',(sentinel,))
                cleanup=native.maybe_auto_prune_and_vacuum(retention_days=0,min_interval_hours=0,vacuum=False)
                control_removed=native.get_session(sentinel) is None
            finally:
                native.close()
            after=HermesArchiveProvider(path,provider.session_scopes,files=files).query(identity.subject,source['scope_ids'],'all',True)
            if cleanup.get('skipped') or cleanup.get('error') or cleanup.get('pruned',0)<1 or not control_removed or before['source_version']!=after['source_version']:
                raise ManagementError('capability_unverified','Actual native automatic cleanup did not prove protected content and the endangered archived control.')
            evidence.update(status='verified_native_cleanup_copy',native_runtime_sha256=hashlib.sha256(Path(hermes_state.__file__).read_bytes()).hexdigest(),
                protected_session_ids=coverage['session_ids'],native_auto_cleanup=cleanup,archived_unpinned_control='deleted',
                protected_content='unchanged',source_version=after['source_version'],proof_scope='consistent_isolated_copy_with_real_native_automatic_cleanup',verified_at=_now())
        except Exception as exc:
            evidence.update(status='unverified',reason=str(exc) if isinstance(exc,ManagementError) else 'Native retention execution is unavailable or unverified; a partial pin may require reconciliation.')
        version,data=manager._load()
        data['archive_sources'][source_id]['protection']=evidence
        with manager._db: manager._save(version,data)
        return evidence
