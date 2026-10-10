"""Fixed pytest runner invoked by the owned host inside its read-only shell."""
from contextlib import redirect_stderr, redirect_stdout
import io
import json
import os
from pathlib import Path
import stat
import sys

import pytest

if __package__ in {None, ''}:
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from ghost_hermes_pm.delivery import source_state


class TestReports:
    def __init__(self):
        self.executed = 0
        self.skipped = 0

    def pytest_runtest_logreport(self, report):
        if report.when == 'call' and report.passed:
            self.executed += 1
        if report.skipped:
            self.skipped += 1


def main(arguments):
    # The host fixes this script, interpreter and repository. Only test paths vary.
    repository = Path(arguments[0]).resolve(strict=True)
    if arguments[1:] == ['--state']:
        print(json.dumps(source_state({'worktree': str(repository), 'test_artifact_paths': []})))
        return 0
    artifacts = Path(arguments[1])
    if (not artifacts.is_absolute() or artifacts != artifacts.resolve(strict=True)
        or artifacts.is_relative_to(repository) or not artifacts.is_dir()
        or artifacts.stat().st_uid != os.getuid() or stat.S_IMODE(artifacts.stat().st_mode) & 0o077):
        raise ValueError('The host-owned private test artifact root is required.')
    paths = arguments[2:]
    if not paths or any(p.startswith('-') or not (repository / p).resolve(strict=True).is_relative_to(repository)
                        or not (repository / p).is_file() for p in paths):
        raise ValueError('Explicit in-repository test files are required.')
    settings = {'worktree': str(repository), 'test_artifact_paths': []}
    before = source_state(settings)
    reports = TestReports()
    output = io.StringIO()
    sys.dont_write_bytecode = True
    with redirect_stdout(output), redirect_stderr(output):
        code = int(pytest.main(['-q', '--capture=sys', '-p', 'no:cacheprovider',
                               '--basetemp', str(artifacts / 'pytest'), *paths], plugins=[reports]))
    after = source_state(settings)
    receipt = {'runner': 'pytest', 'exit_code': code, 'executed_tests': reports.executed,
               'skipped_tests': reports.skipped, 'source_commit': before['head'],
               'before_source_digest': before['source_digest'], 'after_source_digest': after['source_digest'],
               'before_workspace_status': before['workspace_status'], 'after_workspace_status': after['workspace_status'],
               'artifact_roots': [str(artifacts)], 'output': output.getvalue()}
    print(json.dumps(receipt))
    return code if code else 0 if before == after else 1


if __name__ == '__main__':
    raise SystemExit(main(sys.argv[1:]))
