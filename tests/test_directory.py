from pathlib import Path
import subprocess
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
