"""Explicit final axis files and their post-save source proof are required."""
import hashlib
import json
from pathlib import Path
import subprocess
import sys
import time

import pytest

from tools.check_support import fingerprint
from test_local_checks import owned_repo

GATE = Path(__file__).resolve().parents[1] / 'tools' / 'phase_gate.py'


def write(path, value):
    path.write_text(json.dumps(value))
    return hashlib.sha256(path.read_bytes()).hexdigest()


def evidence(tmp_path, mode):
    root = owned_repo(tmp_path)
    source = fingerprint(root)
    reports = {}
    for axis in ('Spec', 'Standards'):
        report_source = {**source, 'head': '0' * 40} if mode == 'wrong_source' and axis == 'Spec' else source
        path = tmp_path / (axis + '.json')
        sha = write(path, {'axis': axis, 'status': 'progress' if mode == 'progress' and axis == 'Spec' else 'final', 'source': report_source})
        reports[axis] = {'path': str(path), 'sha256': sha, 'saved_at_ns': path.stat().st_mtime_ns}
    captured = time.time_ns()
    if mode == 'early_proof':
        captured = min(report['saved_at_ns'] for report in reports.values()) - 1
    proof = tmp_path / 'proof.json'
    proof_sha = write(proof, {'source': source, 'captured_at_ns': captured, 'report_digests': {axis: report['sha256'] for axis, report in reports.items()}})
    if mode == 'digest':
        reports['Spec']['sha256'] = '0' * 64
    if mode.startswith('missing_'):
        del reports[mode.removeprefix('missing_')]
    manifest = tmp_path / 'manifest.json'
    write(manifest, {'source': source, 'reports': reports, 'proof': {'path': str(proof), 'sha256': proof_sha}})
    if mode == 'stale':
        (root / 'source.txt').write_text('later writer')
    return root, manifest


def test_both_final_axes_and_post_save_matching_source_pass(tmp_path):
    root, manifest = evidence(tmp_path, 'complete')
    result = subprocess.run([sys.executable, str(GATE), '--root', str(root), '--manifest', str(manifest)], capture_output=True, text=True)
    assert result.returncode == 0, result.stdout + result.stderr
    assert len(result.stdout) < 2000


@pytest.mark.parametrize('mode', ['missing_Spec', 'missing_Standards', 'progress', 'wrong_source', 'early_proof', 'digest', 'stale'])
def test_missing_incomplete_or_stale_phase_evidence_fails(tmp_path, mode):
    root, manifest = evidence(tmp_path, mode)
    result = subprocess.run([sys.executable, str(GATE), '--root', str(root), '--manifest', str(manifest)], capture_output=True, text=True)
    assert result.returncode != 0
    assert 'failed' in result.stdout
