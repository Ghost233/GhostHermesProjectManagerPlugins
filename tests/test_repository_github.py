"""Only the configured account is copied to a private original-instance reference."""
import hashlib
import json
from pathlib import Path
import stat
import sys

import pytest

from ghost_hermes_pm.manager import ManagementError
from ghost_hermes_pm.simple_github import GitHubWorkSource


@pytest.fixture
def github_source(tmp_path, monkeypatch):
    configuration = tmp_path / 'original-gh'
    configuration.mkdir(mode=0o700)
    hosts = configuration / 'hosts.yml'
    hosts.write_text('original account metadata remains unchanged\n')
    hosts.chmod(0o600)
    binary = tmp_path / 'bin'
    binary.mkdir()
    gh = binary / 'gh'
    gh.write_text('#!' + sys.executable + '\n' + '''
import json, os, pathlib, sys
args=sys.argv[1:]
root=pathlib.Path(os.environ['GH_CONFIG_DIR'])
with pathlib.Path(os.environ['FIXTURE_GH_CALLS']).open('a') as output: output.write(json.dumps(args)+'\\n')
if args[:2]==['auth','token']: print('synthetic-configured-account-token')
elif args[:2]==['auth','switch']:
 (root/'config.yml').write_text('active synthetic configured account\\n')
 (root/'config.yml').chmod(0o600)
elif args[:2]==['api','user']: print(os.environ.get('FIXTURE_GH_LOGIN','fixture-user'))
else: sys.exit(2)
''')
    gh.chmod(0o700)
    monkeypatch.setenv('PATH', str(binary))
    monkeypatch.setenv('FIXTURE_GH_CALLS', str(tmp_path / 'gh-calls.jsonl'))
    return GitHubWorkSource('fixture-user', str(configuration)), hosts


def test_owned_account_copy_uses_cli_identity_and_preserves_original(github_source, tmp_path):
    from ghost_hermes_pm.repository_github import prepare_github_execution, cleanup_github_execution
    source, original = github_source
    digest = hashlib.sha256(original.read_bytes()).hexdigest()
    prepared = prepare_github_execution(source, {'repository': 'fixture-user/fixture'}, tmp_path / 'instance', 'generation-fixture')
    directory = Path(prepared['environment']['GH_CONFIG_DIR'])
    assert directory.is_relative_to(tmp_path / 'instance/home/tmp')
    assert stat.S_IMODE(directory.stat().st_mode) == 0o700
    assert stat.S_IMODE((directory / 'hosts.yml').stat().st_mode) == 0o600
    assert prepared['environment']['HERMES_OWNED_GITHUB_ACCOUNT'] == 'fixture-user'
    assert prepared['environment']['HERMES_OWNED_GITHUB_REPOSITORY'] == 'fixture-user/fixture'
    assert prepared['reference']['account_verified'] is True
    assert 'synthetic-configured-account-token' not in json.dumps(prepared)
    assert hashlib.sha256(original.read_bytes()).hexdigest() == digest
    assert stat.S_IMODE(original.stat().st_mode) == 0o600
    cleanup_github_execution(prepared['reference'], 'generation-fixture')
    assert not directory.exists() and original.exists()


def test_wrong_actual_account_cannot_reach_owned_runtime(github_source, tmp_path, monkeypatch):
    from ghost_hermes_pm.repository_github import prepare_github_execution
    source, original = github_source
    before = original.read_bytes()
    monkeypatch.setenv('FIXTURE_GH_LOGIN', 'different-user')
    with pytest.raises(ManagementError, match='account') as failure:
        prepare_github_execution(source, {'repository': 'fixture-user/fixture'}, tmp_path / 'instance', 'generation-fixture')
    assert failure.value.code == 'unauthorized'
    assert original.read_bytes() == before
    assert not (tmp_path / 'instance/home/tmp/github').exists()


def test_same_generation_reuses_verified_copy_without_reading_original_token_again(github_source, tmp_path):
    from ghost_hermes_pm.repository_github import prepare_github_execution, cleanup_github_execution
    source, original = github_source
    original_config = original.parent / 'config.yml'
    original_config.write_text('original preferences\n')
    original_config.chmod(0o640)
    before = original.read_bytes(), original_config.read_bytes(), stat.S_IMODE(original_config.stat().st_mode)
    first = prepare_github_execution(source, {'repository': 'fixture-user/fixture'}, tmp_path / 'instance', 'generation-fixture')
    second = prepare_github_execution(source, {'repository': 'fixture-user/fixture'}, tmp_path / 'instance', 'generation-fixture')
    assert second == first
    calls = [json.loads(line) for line in (tmp_path / 'gh-calls.jsonl').read_text().splitlines()]
    assert [call[:2] for call in calls] == [
        ['auth', 'token'], ['auth', 'switch'], ['api', 'user'], ['auth', 'switch'], ['api', 'user']]
    assert calls[0][-2:] == ['--user', 'fixture-user']
    assert before == (original.read_bytes(), original_config.read_bytes(), stat.S_IMODE(original_config.stat().st_mode))
    cleanup_github_execution(second['reference'], 'generation-fixture')


@pytest.mark.parametrize('change', ['generation', 'repository', 'identity', 'public_mode', 'unknown_file', 'hosts_changed'])
def test_unknown_or_mismatched_copy_is_preserved(github_source, tmp_path, change):
    from ghost_hermes_pm.repository_github import prepare_github_execution
    source, _ = github_source
    prepared = prepare_github_execution(source, {'repository': 'fixture-user/fixture'}, tmp_path / 'instance', 'generation-fixture')
    directory = Path(prepared['environment']['GH_CONFIG_DIR'])
    target, generation = {'repository': 'fixture-user/fixture'}, 'generation-fixture'
    if change == 'generation':
        generation = 'different-generation'
    elif change == 'repository':
        target['repository'] = 'fixture-user/different-repository'
    elif change == 'identity':
        moved = directory.with_name('preserved-original')
        directory.rename(moved)
        directory.mkdir(mode=0o700)
        for path in moved.iterdir():
            copied = directory / path.name
            copied.write_bytes(path.read_bytes())
            copied.chmod(0o600)
    elif change == 'public_mode':
        directory.chmod(0o755)
    elif change == 'unknown_file':
        (directory / 'owner-note.txt').write_text('Preserve synthetic owner content.\n')
    else:
        (directory / 'hosts.yml').write_text('Changed unknown credential configuration.\n')
    before = {p.name: p.read_bytes() for p in directory.iterdir()}
    with pytest.raises(ManagementError):
        prepare_github_execution(source, target, tmp_path / 'instance', generation)
    assert before == {p.name: p.read_bytes() for p in directory.iterdir()}


def test_cleanup_rejects_wrong_generation_and_preserves_unknown_files(github_source, tmp_path):
    from ghost_hermes_pm.repository_github import prepare_github_execution, cleanup_github_execution
    source, original = github_source
    prepared = prepare_github_execution(source, {'repository': 'fixture-user/fixture'}, tmp_path / 'instance', 'generation-fixture')
    directory = Path(prepared['environment']['GH_CONFIG_DIR'])
    with pytest.raises(ManagementError):
        cleanup_github_execution(prepared['reference'], 'different-generation')
    note = directory / 'owner-note.txt'
    note.write_text('Preserve synthetic owner content.\n')
    with pytest.raises(ManagementError):
        cleanup_github_execution(prepared['reference'], 'generation-fixture')
    assert note.read_text() == 'Preserve synthetic owner content.\n'
    assert original.exists() and (directory / 'hosts.yml').exists()


def test_inherited_tokens_cannot_override_owned_account_copy(github_source, tmp_path, monkeypatch, capsys):
    from ghost_hermes_pm.repository_github import prepare_github_execution, cleanup_github_execution
    source, _ = github_source
    for name in ('GH_TOKEN', 'GITHUB_TOKEN', 'GH_ENTERPRISE_TOKEN', 'GITHUB_ENTERPRISE_TOKEN'):
        monkeypatch.setenv(name, 'synthetic-unrelated-account-token')
    prepared = prepare_github_execution(source, {'repository': 'fixture-user/fixture'}, tmp_path / 'instance', 'generation-fixture')
    assert all(prepared['environment'][name] == '' for name in
               ('GH_TOKEN', 'GITHUB_TOKEN', 'GH_ENTERPRISE_TOKEN', 'GITHUB_ENTERPRISE_TOKEN'))
    assert 'synthetic-configured-account-token' not in capsys.readouterr().out
    assert 'synthetic-configured-account-token' not in json.dumps(prepared)
    cleanup_github_execution(prepared['reference'], 'generation-fixture')
