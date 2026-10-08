"""Fixed privacy preparation through the public offline command."""
import os
from pathlib import Path
import subprocess
import sys

import pytest

ROOT = Path(__file__).resolve().parents[1]


def sdk_source():
    source = os.environ.get('HERMES_TEST_SDK_ROOT')
    if not source:
        if os.environ.get('HERMES_REQUIRE_SDK_SMOKE') == '1':
            pytest.fail('An explicit fixed SDK source is required.')
        pytest.skip('Fixed SDK fixture absent; preparation has not been verified.')
    return Path(source)


def prepare(source, target):
    return subprocess.run([sys.executable, str(ROOT / 'tools' / 'prepare_sdk_privacy.py'),
        '--source', str(source), '--output', str(target)], capture_output=True, text=True)


@pytest.fixture
def isolated_official_sdk(tmp_path):
    """Extract only the fixed official tree; borrow objects without source config."""
    source = tmp_path / 'official-sdk-input'
    source.mkdir(mode=0o700)
    view = source / '.git'
    (view / 'objects' / 'info').mkdir(parents=True)
    (view / 'refs').mkdir()
    (view / 'HEAD').write_text('ref: refs/heads/unused\n')
    (view / 'objects' / 'info' / 'alternates').write_text(str((sdk_source() / '.git' / 'objects').resolve()) + '\n')
    environment = {'PATH': os.environ.get('PATH', '/usr/bin:/bin'),
        'GIT_CONFIG_NOSYSTEM': '1', 'GIT_CONFIG_GLOBAL': os.devnull,
        'GIT_TERMINAL_PROMPT': '0', 'GIT_NO_REPLACE_OBJECTS': '1', 'GIT_NO_LAZY_FETCH': '1'}
    git = ['git', '--no-optional-locks', '--git-dir', str(view)]
    listing = subprocess.run([*git, 'ls-tree', '-rz', 'bd0affe5e5f723579df8902852f5d0c47795f355'],
        env=environment, capture_output=True)
    assert listing.returncode == 0, 'The fixed official SDK objects must be available.'
    entries = []
    for row in listing.stdout.split(b'\0'):
        if row:
            metadata, name = row.split(b'\t', 1)
            mode, kind, object_id = metadata.split()
            assert kind == b'blob' and mode in {b'100644', b'100755'}
            entries.append((mode, object_id, name.decode()))
    # Raw blobs preserve all bytes; git archive honors export attributes and
    # therefore is not a complete fixed-worktree input for this boundary.
    batch = subprocess.run([*git, 'cat-file', '--batch'],
        input=b''.join(object_id + b'\n' for _, object_id, _ in entries),
        env=environment, capture_output=True)
    assert batch.returncode == 0, 'The fixed official SDK blobs must be available.'
    position = 0
    for mode, object_id, name in entries:
        header_end = batch.stdout.index(b'\n', position)
        actual_id, kind, size = batch.stdout[position:header_end].split()
        assert actual_id == object_id and kind == b'blob'
        start, end = header_end + 1, header_end + 1 + int(size)
        target = source / name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(batch.stdout[start:end])
        target.chmod(0o755 if mode == b'100755' else 0o644)
        assert batch.stdout[end:end + 1] == b'\n'
        position = end + 1
    assert position == len(batch.stdout)
    return source


def test_preparation_creates_fixed_private_sdk_without_changing_input(tmp_path, isolated_official_sdk):
    source = isolated_official_sdk
    before = (source / 'hermes_bootstrap.py').read_bytes()
    target = tmp_path / 'prepared-sdk'
    result = prepare(source, target)
    assert result.returncode == 0, result.stdout + result.stderr
    assert result.stdout.strip() == 'Fixed native privacy SDK prepared.'
    assert (target / 'hermes_native_log_privacy.py').is_file()
    assert (target / 'hermes_bootstrap.py').read_bytes() != before
    assert (source / 'hermes_bootstrap.py').read_bytes() == before
    assert target.stat().st_mode & 0o777 == 0o700
    assert not (target / '.git').exists()
    protected = (target / 'hermes_native_log_privacy.py').read_bytes()
    repeated = prepare(source, target)
    assert repeated.returncode == 1
    assert repeated.stderr.strip() == 'Fixed native privacy SDK preparation refused.'
    assert (target / 'hermes_native_log_privacy.py').read_bytes() == protected
    assert prepare(source, source).returncode == 1
    assert (source / 'hermes_bootstrap.py').read_bytes() == before


def test_preparation_refuses_unknown_input_before_reading_its_contents(tmp_path, isolated_official_sdk):
    source = isolated_official_sdk
    # Prove this exact input passes every preparation prerequisite before adding
    # the only difference under test: an unknown private file.
    admitted = prepare(source, tmp_path / 'verified-output')
    assert admitted.returncode == 0, admitted.stdout + admitted.stderr
    private = source / '.env'
    private.write_text('SYNTHETIC_PRIVATE_INPUT=never-read\n')
    private.chmod(0o600)
    observed = tmp_path / 'read-observed'
    child = '''import os, runpy, sys
from pathlib import Path
private, observed, tool, source, target = map(Path, sys.argv[1:])
def audit(event, args):
    if event == 'open' and isinstance(args[0], (str, bytes)) and Path(os.fsdecode(args[0])) == private:
        observed.write_text('private input was read')
        raise RuntimeError('private input access refused')
sys.addaudithook(audit)
sys.argv = [str(tool), '--source', str(source), '--output', str(target)]
runpy.run_path(str(tool), run_name='__main__')
'''
    # A counterfactual offline probe may choose an isolated tool copy. Runtime
    # code has no test flag; the ordinary test always invokes the real CLI.
    tool = Path(os.environ.get('HERMES_TEST_PREPARATION_TOOL', ROOT / 'tools' / 'prepare_sdk_privacy.py'))
    result = subprocess.run([sys.executable, '-c', child, str(private), str(observed),
        str(tool), str(source), str(tmp_path / 'output')],
        capture_output=True, text=True)
    assert result.returncode == 1
    assert result.stderr.strip() == 'Fixed native privacy SDK preparation refused.'
    assert not observed.exists(), 'Unknown private input must be rejected without reading its bytes.'
    assert not (tmp_path / 'output').exists()
