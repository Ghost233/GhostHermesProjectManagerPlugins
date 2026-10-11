"""Fixed pytest runner invoked by the owned host inside its read-only shell."""
from contextlib import redirect_stderr, redirect_stdout
import io
import json
import os
from pathlib import Path
import stat
import sys
import subprocess

if __package__ in {None, ''}:
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from ghost_hermes_pm.delivery import source_state
from ghost_hermes_pm.shared_source_reader import safe_git_reads


class TestReports:
    def __init__(self):
        self.executed = 0
        self.skipped = 0
        self.failed = 0

    def pytest_runtest_logreport(self, report):
        if report.when == 'call' and report.passed:
            self.executed += 1
        if report.skipped:
            self.skipped += 1
        if report.failed:
            self.failed += 1


NODE_REPORTER = '''export default async function* (source) {
  for await (const event of source) {
    if (['test:pass', 'test:fail'].includes(event.type) && event.data.details?.type !== 'suite'
        && (event.type === 'test:fail' || typeof event.data.entryFile === 'string')) {
      yield JSON.stringify({type:event.type,skip:Boolean(event.data.skip),todo:Boolean(event.data.todo)})+'\\n';
    }
  }
}
'''


def run_node(repository, artifacts, node, paths):
    reporter = artifacts / 'native-node-reporter.mjs'
    if reporter.exists():
        info = reporter.lstat()
        if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or info.st_nlink != 1
                or info.st_mode & 0o077 or reporter.read_text() != NODE_REPORTER):
            raise ValueError('The original fixed Node reporter changed.')
    else:
        fd = os.open(reporter, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
        with os.fdopen(fd, 'w') as output:
            output.write(NODE_REPORTER)
    environment = {key: value for key, value in os.environ.items() if key not in {'NODE_OPTIONS', 'NODE_PATH'}}
    result = subprocess.run([node, '--test', '--test-reporter=' + str(reporter), *paths], cwd=repository,
        env=environment, capture_output=True, text=True, timeout=120)
    rows = [json.loads(line) for line in result.stdout.splitlines()]
    if any(set(row) != {'type', 'skip', 'todo'} or row['type'] not in {'test:pass', 'test:fail'}
           or type(row['skip']) is not bool or type(row['todo']) is not bool for row in rows):
        raise ValueError('The original Node framework result is malformed.')
    return result.returncode, {
        'executed_tests': sum(row['type'] == 'test:pass' and not row['skip'] and not row['todo'] for row in rows),
        'skipped_tests': sum(row['skip'] or row['todo'] for row in rows),
        'failed_tests': sum(row['type'] == 'test:fail' and not row['todo'] for row in rows),
        'output': result.stderr[-4000:]}


def main(arguments):
    # The host fixes this script, interpreter and repository. Only test paths vary.
    repository = Path(arguments[0]).resolve(strict=True)
    if arguments[1:] == ['--state']:
        with safe_git_reads(repository) as git_read:
            print(json.dumps(source_state({'worktree': str(repository), 'test_artifact_paths': []}, git_read=git_read)))
        return 0
    artifacts = Path(arguments[1])
    if (not artifacts.is_absolute() or artifacts != artifacts.resolve(strict=True)
        or artifacts.is_relative_to(repository) or not artifacts.is_dir()
        or artifacts.stat().st_uid != os.getuid() or stat.S_IMODE(artifacts.stat().st_mode) & 0o077):
        raise ValueError('The host-owned private test artifact root is required.')
    paths = arguments[2:]
    binding = None
    node = None
    if paths[:1] == ['--node']:
        if len(paths) < 8 or paths[2] != '--binding' or paths[6] != '--':
            raise ValueError('The fixed Node verification binding is required.')
        node, binding, paths = paths[1], dict(zip(('session_id', 'generation', 'nonce'), paths[3:6])), paths[7:]
        if any(not isinstance(value, str) or not value for value in binding.values()):
            raise ValueError('The original work binding is empty.')
        if not Path(node).is_absolute() or str(Path(node).resolve(strict=True)) != node:
            raise ValueError('The fixed original Node executable is required.')
    if not paths or any(p.startswith('-') or not (repository / p).resolve(strict=True).is_relative_to(repository)
                        or not (repository / p).is_file() for p in paths):
        raise ValueError('Explicit in-repository test files are required.')
    settings = {'worktree': str(repository), 'test_artifact_paths': []}
    with safe_git_reads(repository) as git_read:
        before = source_state(settings, git_read=git_read)
    if node:
        code, results = run_node(repository, artifacts, node, paths)
    else:
        import pytest
        reports = TestReports()
        output = io.StringIO()
        sys.dont_write_bytecode = True
        with redirect_stdout(output), redirect_stderr(output):
            code = int(pytest.main(['-q', '--capture=sys', '-p', 'no:cacheprovider',
                                   '--basetemp', str(artifacts / 'pytest'), *paths], plugins=[reports]))
        results = {'executed_tests': reports.executed, 'skipped_tests': reports.skipped,
                   'failed_tests': reports.failed, 'output': output.getvalue()}
    with safe_git_reads(repository) as git_read:
        after = source_state(settings, git_read=git_read)
    receipt = {'runner': 'node:test' if node else 'pytest', 'exit_code': code, **results,
               'source_commit': before['head'],
               'before_source_digest': before['source_digest'], 'after_source_digest': after['source_digest'],
               'before_workspace_status': before['workspace_status'], 'after_workspace_status': after['workspace_status'],
               'artifact_roots': [str(artifacts)],
               **({'schema': 1, 'binding': binding, 'test_files': paths,
                   'source_access': 'workspace-write', 'git_access': 'workspace-write'} if binding else {})}
    print(json.dumps(receipt))
    return code if code else 0 if before == after else 1


if __name__ == '__main__':
    raise SystemExit(main(sys.argv[1:]))
