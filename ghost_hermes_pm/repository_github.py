"""One private GitHub account reference owned by one original execution."""
import hashlib
import json
import os
from pathlib import Path
import re
import stat

from .manager import ManagementError, _private_state_directory
from .simple_github import GitHubWorkSource


_MARKER = '.hermes-owned-github.json'
_FILES = {'hosts.yml', 'config.yml', _MARKER}
_TOKEN_ENV = ('GH_TOKEN', 'GITHUB_TOKEN', 'GH_ENTERPRISE_TOKEN', 'GITHUB_ENTERPRISE_TOKEN')


def _configuration_state(directory):
    values = {}
    for name in ('hosts.yml', 'config.yml'):
        path = directory / name
        if not path.exists() and not path.is_symlink():
            values[name] = None
            continue
        info = path.lstat()
        if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or info.st_nlink != 1:
            raise ManagementError('unsafe_state', 'The original GitHub configuration must use regular owner files.')
        values[name] = (hashlib.sha256(path.read_bytes()).hexdigest(), stat.S_IMODE(info.st_mode), info.st_dev, info.st_ino)
    return values


def _identity(directory):
    info = directory.lstat()
    if (directory != directory.resolve(strict=True) or not stat.S_ISDIR(info.st_mode)
        or info.st_uid != os.getuid() or stat.S_IMODE(info.st_mode) != 0o700):
        raise ManagementError('unsafe_state', 'The owned GitHub directory must remain canonical and private.')
    return {'device': info.st_dev, 'inode': info.st_ino}


def _private_files(directory):
    if {p.name for p in directory.iterdir()} - _FILES:
        raise ManagementError('unsafe_state', 'Unknown files in the owned GitHub directory were preserved.')
    for path in directory.iterdir():
        info = path.lstat()
        if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or info.st_nlink != 1
            or stat.S_IMODE(info.st_mode) != 0o600):
            raise ManagementError('unsafe_state', 'Owned GitHub files must remain regular and private.')


def _write_json(path, value, *, create=False):
    flags = os.O_WRONLY | os.O_NOFOLLOW | (os.O_CREAT | os.O_EXCL if create else os.O_TRUNC)
    descriptor = os.open(path, flags, 0o600)
    with os.fdopen(descriptor, 'w') as output:
        json.dump(value, output)


def _existing(directory, generation):
    identity = _identity(directory)
    _private_files(directory)
    try:
        reference = json.loads((directory / _MARKER).read_text())
        if (reference.get('generation') != generation or reference.get('config_dir') != str(directory)
            or any(reference.get(k) != value for k, value in identity.items())
            or reference.get('hosts_sha256') != hashlib.sha256((directory / 'hosts.yml').read_bytes()).hexdigest()):
            raise ValueError('Original reference changed.')
        return reference
    except (OSError, ValueError, AttributeError):
        raise ManagementError('binding_conflict', 'The original GitHub execution reference needs reconciliation.') from None


def _environment(directory, account, repository):
    return {'GH_CONFIG_DIR': str(directory), 'HERMES_OWNED_GITHUB_ACCOUNT': account,
            'HERMES_OWNED_GITHUB_REPOSITORY': repository, **{key: '' for key in _TOKEN_ENV}}


def prepare_github_execution(source, target, instance_dir, generation):
    repository = target.get('repository')
    if (not isinstance(generation, str) or not generation or not isinstance(repository, str)
        or not re.fullmatch('[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+', repository)):
        raise ManagementError('configuration_missing', 'A fixed GitHub account, repository and generation are required.')
    instance = _private_state_directory(instance_dir)
    parent = _private_state_directory(instance / 'home/tmp')
    directory = parent / 'github'
    created = False
    if directory.exists() or directory.is_symlink():
        reference = _existing(directory, generation)
        if (reference.get('account') != source.account or reference.get('repository') != repository
            or reference.get('account_verified') is not True):
            raise ManagementError('binding_conflict', 'The owned GitHub account or repository reference differs.')
    else:
        original = Path(source.environment['GH_CONFIG_DIR'])
        before = _configuration_state(original)
        try:
            token = source._run('auth', 'token', '--hostname', 'github.com', '--user', source.account).strip()
        finally:
            if _configuration_state(original) != before:
                raise ManagementError('unsafe_state', 'The original GitHub configuration changed during its local credential read.')
        if not token or '\n' in token:
            raise ManagementError('source_unavailable', 'The configured account credential could not be read locally.')
        directory.mkdir(mode=0o700)
        created = True
        reference = {'generation': generation, 'account': source.account, 'repository': repository,
                     'config_dir': str(directory), **_identity(directory), 'account_verified': False}
        try:
            _write_json(directory / _MARKER, reference, create=True)
            _write_json(directory / 'hosts.yml', {'github.com': {'user': source.account, 'oauth_token': token,
                'git_protocol': 'https', 'users': {source.account: {'oauth_token': token}}}}, create=True)
        except OSError:
            cleanup_github_execution(reference, generation, _creating=True)
            raise ManagementError('unsafe_state', 'The private GitHub account copy could not be created.') from None
        finally:
            token = None
    environment = _environment(directory, source.account, repository)
    try:
        account = GitHubWorkSource(source.account, str(directory))
        account.environment.update(environment)
        account._run('auth', 'switch', '--hostname', 'github.com', '--user', source.account)
        if account._run('api', 'user', '--hostname', 'github.com', '--jq', '.login').strip() != source.account:
            raise ManagementError('unauthorized', 'The actual GitHub account differs from the configured account.')
        _private_files(directory)
        reference.update(account_verified=True, hosts_sha256=hashlib.sha256((directory / 'hosts.yml').read_bytes()).hexdigest())
        _write_json(directory / _MARKER, reference)
        return {'environment': environment, 'reference': reference}
    except (ManagementError, OSError):
        if created:
            cleanup_github_execution(reference, generation, _creating=True)
        raise


def cleanup_github_execution(reference, generation, *, _creating=False):
    if not isinstance(reference, dict) or reference.get('generation') != generation:
        raise ManagementError('binding_conflict', 'Only the original generation may remove its GitHub account copy.')
    directory = Path(reference['config_dir'])
    if not directory.exists() and not directory.is_symlink():
        return {'status': 'absent'}
    if any(reference.get(k) != v for k, v in _identity(directory).items()):
        raise ManagementError('binding_conflict', 'The original GitHub account directory identity changed.')
    _private_files(directory)
    if not _creating and _existing(directory, generation) != reference:
        raise ManagementError('binding_conflict', 'The owned GitHub reference changed; files were preserved.')
    for path in directory.iterdir():
        path.unlink()
    directory.rmdir()
    return {'status': 'removed'}
