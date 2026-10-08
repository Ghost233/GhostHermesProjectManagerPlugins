from pathlib import Path
import subprocess
import stat
import traceback
import pytest

from ghost_hermes_pm import Manager, VerifiedIdentity


def make_repo(path: Path) -> Path:
    path.mkdir()
    subprocess.run(['git', 'init', '-q', str(path)], check=True)
    return path


def registration(repo: Path, project_id='mono', profile_id='mono-lead'):
    return {
        'project': {'id': project_id, 'name': 'My Mono', 'repo_path': str(repo),
                    'test_artifact_paths': [str(repo / 'build')]},
        'profile': {'id': profile_id, 'native_profile': profile_id, 'identity_ref': 'fixture:lead',
                    'role': 'project_lead', 'capability': 'development', 'project_id': project_id,
                    'parent_profile_id': None, 'connection_refs': {}},
    }


OWNER = VerifiedIdentity('fixture:owner', 'test-owner-entry')


def test_manager_keeps_private_bindings_in_restricted_local_state(tmp_path):
    state = tmp_path / 'private-state'
    with Manager(state, owner_identity_ref=OWNER.subject) as manager:
        manager.apply_directory_change(OWNER, 0, registration(make_repo(tmp_path / 'repo')))
        assert stat.S_IMODE(state.stat().st_mode) == 0o700
        assert stat.S_IMODE((state / 'manager.sqlite3').stat().st_mode) == 0o600
        assert manager.read_snapshot(OWNER)['version'] == 1
    with Manager(state, owner_identity_ref=OWNER.subject) as restarted:
        assert restarted.read_snapshot(OWNER)['version'] == 1


@pytest.mark.parametrize('kind', ['validation', 'migration'])
def test_native_host_creation_keeps_state_and_work_directories_private_for_manager(tmp_path, kind):
    from ghost_hermes_pm.native_global_validation import NativeGlobalValidationHost
    from ghost_hermes_pm.native_migration import NativeMigrationHost
    state = tmp_path / 'new-parent' / 'state'
    if kind == 'validation':
        host = NativeGlobalValidationHost({'host_id': 'local:fixture-host'}, state)
        work = host.work
    else:
        native = tmp_path / 'synthetic-native'; native.mkdir()
        host = NativeMigrationHost(native, state / 'migration-native', sdk_root=tmp_path)
        work = host.work_dir
    with Manager(state, owner_identity_ref=OWNER.subject) as manager:
        assert manager.read_snapshot(OWNER)['version'] == 0
        assert stat.S_IMODE(state.stat().st_mode) == 0o700
        assert stat.S_IMODE(work.stat().st_mode) == 0o700
        assert stat.S_IMODE(state.parent.stat().st_mode) == 0o700
        assert stat.S_IMODE((state / 'manager.sqlite3').stat().st_mode) == 0o600


@pytest.mark.parametrize('kind', ['validation', 'migration'])
def test_native_host_configuration_preserves_preexisting_unsafe_state(tmp_path, kind):
    from ghost_hermes_pm import ManagementError
    from ghost_hermes_pm.native_global_validation import NativeGlobalValidationHost
    from ghost_hermes_pm.native_migration import configured_migration_host
    state = tmp_path / 'state'; state.mkdir(); state.chmod(0o755)
    preserved = state / 'user-file'; preserved.write_text('preserve this original material')
    native = tmp_path / 'synthetic-native'; native.mkdir()
    with pytest.raises(ManagementError) as rejected:
        if kind == 'validation':
            NativeGlobalValidationHost({'host_id': 'local:fixture-host'}, state)
        else:
            configured_migration_host({'host_home': str(native)}, state)
    assert rejected.value.code == 'unsafe_state'
    assert stat.S_IMODE(state.stat().st_mode) == 0o755
    assert preserved.read_text() == 'preserve this original material'
    assert list(state.iterdir()) == [preserved]


@pytest.mark.parametrize('unsafe', ['directory', 'database', 'alias', 'hardlink'])
def test_manager_preserves_preexisting_unsafe_state_without_restricting_user_files(tmp_path, unsafe):
    from ghost_hermes_pm import ManagementError
    state = tmp_path / 'private-state'
    with Manager(state, owner_identity_ref=OWNER.subject):
        pass
    database = state / 'manager.sqlite3'
    if unsafe == 'directory':
        state.chmod(0o755)
    elif unsafe == 'database':
        database.chmod(0o644)
    elif unsafe == 'alias':
        requested = tmp_path / 'state-alias'
        requested.symlink_to(state, target_is_directory=True)
    else:
        import os
        os.link(database, tmp_path / 'user-preserved-copy')
    before = database.read_bytes()
    modes = (stat.S_IMODE(state.stat().st_mode), stat.S_IMODE(database.stat().st_mode))
    with pytest.raises(ManagementError) as rejected:
        Manager(requested if unsafe == 'alias' else state, owner_identity_ref=OWNER.subject)
    assert rejected.value.code == 'unsafe_state'
    assert database.read_bytes() == before
    assert modes == (stat.S_IMODE(state.stat().st_mode), stat.S_IMODE(database.stat().st_mode))


def test_manager_rejects_runtime_state_inside_git_before_creating_private_files(tmp_path):
    from ghost_hermes_pm import ManagementError
    repo = make_repo(tmp_path / 'repo')
    state = repo / 'runtime-state'
    with pytest.raises(ManagementError) as rejected:
        Manager(state, owner_identity_ref=OWNER.subject)
    assert rejected.value.code == 'unsafe_state'
    assert not state.exists()


def test_project_registration_cannot_include_existing_private_runtime_state(tmp_path):
    from ghost_hermes_pm import ManagementError
    repo = tmp_path / 'later-repo'
    with Manager(repo / 'runtime-state', owner_identity_ref=OWNER.subject) as manager:
        subprocess.run(['git', 'init', '-q', str(repo)], check=True)
        with pytest.raises(ManagementError) as rejected:
            manager.apply_directory_change(OWNER, 0, registration(repo))
        assert rejected.value.code == 'unsafe_state'
        assert manager.read_snapshot(OWNER)['version'] == 0
        assert manager.read_snapshot(OWNER)['projects'] == []


def test_repository_read_errors_do_not_print_private_paths(tmp_path):
    from ghost_hermes_pm import ManagementError
    missing = tmp_path / 'synthetic-private-path-marker'
    change = registration(missing)
    with Manager(tmp_path / 'state', owner_identity_ref=OWNER.subject) as manager:
        with pytest.raises(ManagementError) as rejected:
            manager.apply_directory_change(OWNER, 0, change)
        assert rejected.value.code == 'invalid_repository'
        assert missing.name not in ''.join(traceback.format_exception(rejected.value))
        assert manager.read_snapshot(OWNER)['version'] == 0


def test_owner_registers_existing_project_and_profile_and_reads_same_directory(tmp_path):
    repo = make_repo(tmp_path / 'repo')
    with Manager(tmp_path / 'state', owner_identity_ref=OWNER.subject) as manager:
        result = manager.apply_directory_change(OWNER, 0, registration(repo))
        snapshot = manager.read_snapshot(OWNER)
    assert result['status'] == 'completed'
    assert snapshot['version'] == 1
    assert snapshot['projects'][0]['repo']['worktree'] == str(repo.resolve())
    assert snapshot['projects'][0]['repo']['common_dir'] == str(repo / '.git')
    assert snapshot['profiles'][0]['role'] == 'project_lead'
    assert snapshot['profiles'][0]['capability'] == 'development'
    assert snapshot['profiles'][0]['lifecycle'] == 'configuring'
    assert snapshot['profiles'][0]['can_execute'] is False
    assert snapshot['last_verified_at']


def test_profile_identity_and_project_binding_survive_corrections_and_restart(tmp_path):
    from ghost_hermes_pm import ManagementError
    import pytest
    repo = make_repo(tmp_path / 'repo')
    other = make_repo(tmp_path / 'other')
    with Manager(tmp_path / 'state', owner_identity_ref=OWNER.subject) as manager:
        manager.apply_directory_change(OWNER, 0, registration(repo))
        corrected = registration(repo)
        corrected['project']['name'] = 'Corrected Mono'
        manager.apply_directory_change(OWNER, 1, corrected)
        manager.apply_directory_change(OWNER, 2, {'project': registration(other, 'other')['project']})
        rebound = registration(other, 'other')['profile']
        with pytest.raises(ManagementError, match='long-term'):
            manager.apply_directory_change(OWNER, 3, {'profile': rebound})
    with Manager(tmp_path / 'state', owner_identity_ref=OWNER.subject) as restarted:
        snapshot = restarted.read_snapshot(OWNER)
    assert snapshot['version'] == 3
    assert len(snapshot['profiles']) == 1
    assert snapshot['profiles'][0]['project_id'] == 'mono'
    assert snapshot['projects'][0]['name'] == 'Corrected Mono'


def test_forged_subject_role_and_stale_change_are_rejected_without_partial_write(tmp_path):
    from ghost_hermes_pm import ManagementError
    import pytest
    repo = make_repo(tmp_path / 'repo')
    with Manager(tmp_path / 'state', owner_identity_ref=OWNER.subject) as manager:
        with pytest.raises(ManagementError) as forged:
            manager.apply_directory_change(VerifiedIdentity('stranger', 'owner'), 0, registration(repo))
        assert forged.value.code == 'unauthorized'
        manager.apply_directory_change(OWNER, 0, registration(repo))
        with pytest.raises(ManagementError) as stale:
            manager.apply_directory_change(OWNER, 0, registration(repo))
        assert stale.value.code == 'version_conflict'
        secret = registration(repo)
        secret['profile']['connection_refs']['token'] = 'literal-secret'
        with pytest.raises(ManagementError) as invalid:
            manager.apply_directory_change(OWNER, 1, secret)
        assert invalid.value.code == 'invalid_change'
        snapshot = manager.read_snapshot(OWNER)
    assert snapshot['version'] == 1
    assert 'literal-secret' not in str(snapshot)


def test_linked_worktree_and_nested_repositories_record_logical_git_and_write_boundaries(tmp_path):
    import pytest
    from ghost_hermes_pm import ManagementError
    repo = make_repo(tmp_path / 'repo')
    subprocess.run(['git', '-C', str(repo), '-c', 'user.name=Fixture', '-c', 'user.email=fixture@example.invalid',
                    'commit', '--allow-empty', '-qm', 'fixture'], check=True)
    linked = tmp_path / 'linked'
    subprocess.run(['git', '-C', str(repo), 'worktree', 'add', '-q', str(linked)], check=True)
    nested = make_repo(linked / 'nested')
    with Manager(tmp_path / 'state', owner_identity_ref=OWNER.subject) as manager:
        manager.apply_directory_change(OWNER, 0, registration(linked))
        binding = manager.read_snapshot(OWNER)['projects'][0]['repo']
        assert binding['common_dir'] == str(repo / '.git')
        assert binding['git_dir'] == str(repo / '.git' / 'worktrees' / 'linked')
        assert binding['nested_repositories'][0]['worktree'] == str(nested)
        assert binding['nested_repositories'][0]['access'] == 'read_only'
        invalid = registration(linked)
        invalid['project']['test_artifact_paths'] = [str(nested / 'build')]
        with pytest.raises(ManagementError) as rejected:
            manager.apply_directory_change(OWNER, 1, invalid)
        assert rejected.value.code == 'invalid_repository'
        assert manager.read_snapshot(OWNER)['version'] == 1


def test_registered_project_lead_can_correct_own_directory_but_cannot_escalate_or_read_another_project(tmp_path):
    import pytest
    from ghost_hermes_pm import ManagementError
    repo = make_repo(tmp_path / 'repo')
    other = make_repo(tmp_path / 'other')
    lead = VerifiedIdentity('fixture:lead', 'verified-bot-entry')
    with Manager(tmp_path / 'state', owner_identity_ref=OWNER.subject) as manager:
        manager.apply_directory_change(OWNER, 0, registration(repo))
        isolated = registration(other, 'other', 'other-lead')
        isolated['profile']['identity_ref'] = 'fixture:other'
        manager.apply_directory_change(OWNER, 1, isolated)
        correction = registration(repo)['project']
        correction['name'] = 'Renamed by lead'
        manager.apply_directory_change(lead, 2, {'project': correction})
        assert [p['id'] for p in manager.read_snapshot(lead)['projects']] == ['mono']
        with pytest.raises(ManagementError) as denied:
            manager.apply_directory_change(lead, 3, {'project': isolated['project']})
        assert denied.value.code == 'forbidden'
        escalation = registration(repo)['profile']
        escalation.update(role='steward', project_id=None)
        with pytest.raises(ManagementError):
            manager.apply_directory_change(lead, 3, {'profile': escalation})
        assert manager.read_snapshot(OWNER)['version'] == 3


@pytest.mark.parametrize('change', [None, {'profile': 'not-a-profile'}, {'profile': {'id': 'broken'}}])
def test_malformed_public_directory_input_is_rejected_atomically(tmp_path, change):
    from ghost_hermes_pm import ManagementError
    with Manager(tmp_path / 'state', owner_identity_ref=OWNER.subject) as manager:
        with pytest.raises(ManagementError) as denied:
            manager.apply_directory_change(OWNER, 0, change)
        assert denied.value.code == 'invalid_change'
        assert manager.read_snapshot(OWNER)['version'] == 0


def test_steward_cannot_cycle_project_leads_but_can_transfer_long_term_child_binding(tmp_path):
    from ghost_hermes_pm import ManagementError
    first = registration(make_repo(tmp_path / 'first'), 'first', 'first-lead')
    first['profile']['identity_ref'] = 'fixture:first'
    second = registration(make_repo(tmp_path / 'second'), 'second', 'second-lead')
    second['profile']['identity_ref'] = 'fixture:second'
    steward_profile = {'id': 'steward', 'native_profile': 'steward', 'identity_ref': 'fixture:steward',
                       'role': 'steward', 'capability': 'non_development', 'project_id': None,
                       'parent_profile_id': None, 'connection_refs': {}}
    steward = VerifiedIdentity('fixture:steward', 'trusted-steward-entry')
    first_lead = VerifiedIdentity('fixture:first', 'trusted-lead-entry')
    second_lead = VerifiedIdentity('fixture:second', 'trusted-lead-entry')
    with Manager(tmp_path / 'state', owner_identity_ref=OWNER.subject) as manager:
        manager.apply_directory_change(OWNER, 0, first)
        manager.apply_directory_change(OWNER, 1, second)
        manager.apply_directory_change(OWNER, 2, {'profile': steward_profile})
        for target, parent in ((second, 'first-lead'), (first, 'second-lead'), (first, 'first-lead')):
            invalid = {**target['profile'], 'parent_profile_id': parent}
            with pytest.raises(ManagementError) as rejected:
                manager.apply_directory_change(steward, 3, {'profile': invalid})
            assert rejected.value.code == 'invalid_change'
        assert manager.read_snapshot(OWNER)['version'] == 3
        assert [p['id'] for p in manager.read_snapshot(first_lead)['projects']] == ['first']
        child = registration(make_repo(tmp_path / 'child'), 'child', 'child-lead')
        child['profile'].update(identity_ref='fixture:child', role='subproject_lead', parent_profile_id='first-lead')
        manager.apply_directory_change(OWNER, 3, child)
        moved = {**child['profile'], 'parent_profile_id': 'second-lead'}
        manager.apply_directory_change(steward, 4, {'profile': moved})
        assert manager.read_snapshot(OWNER)['profiles'][-1]['project_id'] == 'child'
        assert [p['id'] for p in manager.read_snapshot(first_lead)['projects']] == ['first']
        assert [p['id'] for p in manager.read_snapshot(second_lead)['projects']] == ['second', 'child']
        invalid_parent = {**second['profile'], 'role': 'subproject_lead', 'parent_profile_id': 'first-lead'}
        with pytest.raises(ManagementError):
            manager.apply_directory_change(OWNER, 5, {'profile': invalid_parent})
        assert manager.read_snapshot(OWNER)['version'] == 5
