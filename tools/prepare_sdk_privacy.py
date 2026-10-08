"""Prepare the exact reviewed native SDK in a new offline directory."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import shutil
import stat
import subprocess
import sys
import tempfile

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from ghost_hermes_pm.sdk_contract import (SDK_REVISION, SDK_BASE_GIT_TREE, SDK_BASE_TREE_DIGEST, SDK_BASE_FILES,
    SDK_PRIVACY_FILES, SDK_PRIVACY_PATCH_SHA256, source_files_match)

PATCH = Path(__file__).resolve().parent / 'sdk-patches' / 'native-log-privacy.patch'


def prepare_sdk(source, output):
    """Accept the official fixed tree, create only a new isolated patched copy."""
    source, output = Path(source).absolute(), Path(output).absolute()
    created = False
    try:
        if source.is_symlink() or not source.is_dir() or output.exists() or output.is_symlink():
            raise ValueError
        source = source.resolve(strict=True)
        parent = output.parent.resolve(strict=True)
        output = parent / output.name
        if output.is_relative_to(source) or source.is_relative_to(output):
            raise ValueError
        if (source / '.git').is_symlink() or not (source / '.git').is_dir():
            raise ValueError
        git_environment = {'PATH': os.environ.get('PATH', '/usr/bin:/bin'),
            'GIT_CONFIG_NOSYSTEM': '1', 'GIT_CONFIG_GLOBAL': os.devnull, 'GIT_TERMINAL_PROMPT': '0',
            'GIT_OBJECT_DIRECTORY': str(source / '.git' / 'objects'),
            'GIT_NO_REPLACE_OBJECTS': '1', 'GIT_NO_LAZY_FETCH': '1'}
        # Read fixed Git objects without parsing the source repository's config.
        with tempfile.TemporaryDirectory(prefix='hpm-sdk-object-view-') as temporary:
            view = Path(temporary)
            (view / 'objects').mkdir()
            (view / 'refs').mkdir()
            (view / 'HEAD').write_text('ref: refs/heads/unused\n')
            git = ['git', '--no-optional-locks', '--git-dir', str(view)]
            tree = subprocess.run([*git, 'rev-parse', SDK_REVISION + '^{tree}'],
                env=git_environment, capture_output=True)
            if tree.returncode or tree.stdout.decode().strip() != SDK_BASE_GIT_TREE:
                raise ValueError
            listing = subprocess.run([*git, 'ls-tree', '-rz', SDK_REVISION], env=git_environment, capture_output=True)
        if listing.returncode:
            raise ValueError
        known_names = {}
        for row in listing.stdout.split(b'\0'):
            if not row:
                continue
            metadata, name = row.split(b'\t', 1)
            mode, kind, _object = metadata.split()
            if kind != b'blob' or mode not in {b'100644', b'100755'}:
                raise ValueError
            known_names[name.decode()] = mode == b'100755'
        observed = {}
        for directory, dirs, names in os.walk(source):
            dirs[:] = [name for name in dirs if name not in {'.git', '__pycache__'}]
            for name in dirs:
                if (Path(directory) / name).is_symlink():
                    raise ValueError
            for name in names:
                path = Path(directory) / name
                item = path.lstat()
                if not stat.S_ISREG(item.st_mode) or item.st_nlink != 1:
                    raise ValueError
                observed[path.relative_to(source).as_posix()] = bool(item.st_mode & 0o111)
        if observed != known_names:
            raise ValueError
        files = {name: hashlib.sha256((source / name).read_bytes()).hexdigest() for name in sorted(observed)}
        digest = hashlib.sha256(json.dumps(files, sort_keys=True).encode()).hexdigest()
        if digest != SDK_BASE_TREE_DIGEST or not source_files_match(source, SDK_BASE_FILES):
            raise ValueError
        if not SDK_PRIVACY_PATCH_SHA256 or hashlib.sha256(PATCH.read_bytes()).hexdigest() != SDK_PRIVACY_PATCH_SHA256:
            raise ValueError
        output.mkdir(mode=0o700)
        created = True
        for name in files:
            target = output / name
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source / name, target)
            if hashlib.sha256(target.read_bytes()).hexdigest() != files[name]:
                raise ValueError
        result = subprocess.run(['patch', '-p1', '--batch', '--forward', '-i', str(PATCH)],
            cwd=output, capture_output=True)
        if result.returncode or not source_files_match(output, {**SDK_BASE_FILES, **SDK_PRIVACY_FILES}):
            raise ValueError
    except Exception:
        if created:
            shutil.rmtree(output)
        raise ValueError('Fixed native privacy SDK preparation refused.') from None


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--source', required=True)
    parser.add_argument('--output', required=True)
    arguments = parser.parse_args()
    try:
        prepare_sdk(arguments.source, arguments.output)
    except ValueError:
        print('Fixed native privacy SDK preparation refused.', file=sys.stderr)
        return 1
    print('Fixed native privacy SDK prepared.')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
