"""Source-integrity and private-input checks for direct, unmodified SDK loading."""
import os
from pathlib import Path
import subprocess
import sys

from sdk_source_integrity import source_snapshot

ROOT = Path(__file__).resolve().parents[1]


def test_direct_sdk_loading_removes_the_mutating_preparation_pipeline():
    assert not (ROOT / 'tools' / 'prepare_sdk_privacy.py').exists()
    assert not (ROOT / 'tools' / 'sdk-patches' / 'native-log-privacy.patch').exists()


def test_complete_inventory_observes_changes_outside_compatibility_pointers(tmp_path):
    source = tmp_path / 'official-fixture'
    source.mkdir()
    (source / 'hermes_bootstrap.py').write_text('# public source\n')
    extra = source / 'another-public-source.txt'
    extra.write_text('original\n')
    (source / '.env.example').write_text('SYNTHETIC_EXAMPLE=placeholder\n')
    before = source_snapshot(source)
    assert set(before) == {'hermes_bootstrap.py', 'another-public-source.txt', '.env.example'}
    assert source_snapshot(source) == before
    extra.write_text('changed\n')
    assert source_snapshot(source) != before
    extra.write_text('original\n')
    extra.chmod(0o755)
    assert source_snapshot(source) != before
    extra.chmod(0o644)
    (source / 'new-public-source.py').write_text('# added\n')
    assert source_snapshot(source) != before


def test_private_environment_input_is_rejected_without_reading_its_bytes(tmp_path):
    source = tmp_path / 'sdk'
    source.mkdir()
    private = source / '.env'
    private.write_text('SYNTHETIC_PRIVATE_INPUT=never-read\n')
    observed = tmp_path / 'read-observed'
    child = '''import os, sys
from pathlib import Path
from sdk_source_integrity import source_snapshot
source, private, observed = map(Path, sys.argv[1:])
def audit(event, args):
    if event == 'open' and isinstance(args[0], (str, bytes)) and Path(os.fsdecode(args[0])) == private:
        observed.write_text('private bytes read')
        raise RuntimeError('private bytes read')
sys.addaudithook(audit)
try:
    source_snapshot(source)
except ValueError:
    pass
else:
    raise AssertionError('Private environment input was admitted.')
'''
    result = subprocess.run([sys.executable, '-c', child, str(source), str(private), str(observed)],
        env={'PATH': os.environ.get('PATH', ''), 'PYTHONPATH': str(ROOT / 'tests'),
             'PYTHONDONTWRITEBYTECODE': '1'}, capture_output=True, text=True)
    assert result.returncode == 0, result.stdout + result.stderr
    assert not observed.exists()


def test_snapshot_includes_symlink_aliases_without_following_them(tmp_path):
    source, outside = tmp_path / 'sdk', tmp_path / 'outside'
    source.mkdir(); outside.mkdir()
    (outside / '.env').write_text('SYNTHETIC_PRIVATE_INPUT=never-follow\n')
    (source / 'public-alias').symlink_to(outside, target_is_directory=True)
    snapshot = source_snapshot(source)
    assert set(snapshot) == {'public-alias'}
    assert snapshot['public-alias'][0] == 'symlink'
