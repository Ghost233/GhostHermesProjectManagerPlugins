"""Read the existing repository fingerprint; this entry never executes tests."""
import json
from contextlib import contextmanager
import os
from pathlib import Path
import subprocess
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from ghost_hermes_pm.delivery import source_state  # noqa: E402
from ghost_hermes_pm.manager import ManagementError  # noqa: E402


@contextmanager
def safe_git_reads(repository):
    """Only this child read scope excludes repository executable Git extensions."""
    environment = {key: value for key, value in os.environ.items() if not key.startswith('GIT_')}
    environment.update(PATH='/usr/bin:/bin', GIT_CONFIG_NOSYSTEM='1', GIT_CONFIG_GLOBAL='/dev/null',
        GIT_OPTIONAL_LOCKS='0', GIT_PAGER='', GIT_ATTR_NOSYSTEM='1', GIT_NO_REPLACE_OBJECTS='1')
    settings = [('core.fsmonitor', 'false'), ('core.hooksPath', '/dev/null'),
                    ('core.attributesFile', '/dev/null'), ('diff.external', '')]
    def command():
        return ['/usr/bin/git', '--no-pager', *[arg for key, value in settings for arg in ('-c', key + '=' + value)]]
    filters = subprocess.run([*command(), '-C', str(repository), 'config', '--null', '--name-only',
        '--get-regexp', r'^filter\..*\.(clean|smudge|process|required)$'],
        env=environment, capture_output=True, text=True, check=False)
    if filters.returncode not in {0, 1}:
        raise ValueError('Repository Git read configuration is unavailable.')
    settings.extend((key, 'false' if key.endswith('.required') else '')
                    for key in filters.stdout.split('\0') if key)
    def read(path, *arguments):
        if Path(path).resolve(strict=True) != repository:
            raise ValueError('A source read crossed the bound repository.')
        result = subprocess.run([*command(), '-C', str(repository), *arguments],
            capture_output=True, text=True, env=environment, timeout=15)
        if result.returncode:
            raise ManagementError('invalid_repository', 'The fixed source read failed.')
        return result.stdout if '-z' in arguments else result.stdout.strip()
    yield read


def main():
    if len(sys.argv) not in {2, 3} or len(sys.argv) == 3 and sys.argv[2] != '--repository-state':
        raise ValueError('Exactly one bound repository is required.')
    workspace = Path(sys.argv[1])
    if not workspace.is_absolute() or str(workspace.resolve(strict=True)) != str(workspace):
        raise ValueError('A canonical bound repository is required.')
    with safe_git_reads(workspace) as git_read:
        if len(sys.argv) == 3:
            from ghost_hermes_pm.repository_acceptance import _repository_source_state
            row = _repository_source_state({'repo_path': str(workspace)}, git_read=git_read)
        else:
            row = source_state({'worktree': str(workspace), 'test_artifact_paths': []}, git_read=git_read)
            row = {key: row[key] for key in ('head', 'source_digest', 'workspace_status')}
    print(json.dumps(row))


if __name__ == '__main__':
    main()
