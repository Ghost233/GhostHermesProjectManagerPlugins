"""Shared frozen source evidence for local checks and review phase validation."""
import hashlib
import json
from pathlib import Path
import subprocess


def digest(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def fingerprint(root):
    root = Path(root).resolve()
    def git(*args):
        return subprocess.check_output(['git', *args], cwd=root).decode().strip()
    names = subprocess.check_output(['git', 'ls-files', '--cached', '--others', '--exclude-standard', '-z'], cwd=root).decode().split('\0')
    files = {}
    for name in sorted(set(names) - {''}):
        if name.startswith(('.codex/', 'docs/research/')):
            continue
        path = root / name
        raw = path.readlink().as_posix().encode() if path.is_symlink() else path.read_bytes() if path.is_file() else None
        files[name] = hashlib.sha256(raw).hexdigest() if raw is not None else None
    return {'head': git('rev-parse', 'HEAD'), 'tree': git('rev-parse', 'HEAD^{tree}'),
            'status': subprocess.check_output(['git', 'status', '--porcelain=v1'], cwd=root, text=True), 'files': files,
            'all_file_digest': hashlib.sha256(json.dumps(files, sort_keys=True).encode()).hexdigest()}


def write_json(path, value):
    Path(path).write_text(json.dumps(value, indent=2) + '\n')
