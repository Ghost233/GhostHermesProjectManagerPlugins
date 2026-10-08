"""Validate explicit final review artifacts against their frozen and post-save source."""
import argparse
import json
from pathlib import Path
import sys

from check_support import digest, fingerprint, write_json


def validate(root, manifest):
    source = manifest['source']
    if fingerprint(root) != source:
        raise ValueError('Current source differs from the frozen manifest.')
    reports = manifest['reports']
    if set(reports) != {'Spec', 'Standards'}:
        raise ValueError('Both Spec and Standards final reports are required.')
    proof_ref = manifest['proof']
    proof_path = Path(proof_ref['path'])
    if digest(proof_path) != proof_ref['sha256']:
        raise ValueError('Post-save source proof digest differs.')
    proof = json.loads(proof_path.read_text())
    if proof['source'] != source:
        raise ValueError('Post-save source proof differs from the frozen manifest.')
    for axis, reference in reports.items():
        path = Path(reference['path'])
        actual = digest(path)
        report = json.loads(path.read_text())
        if actual != reference['sha256'] or proof['report_digests'].get(axis) != actual:
            raise ValueError(axis + ' report digest differs from the saved evidence.')
        if report.get('axis') != axis or report.get('status') != 'final':
            raise ValueError(axis + ' artifact is not an explicit final report.')
        if report.get('source') != source:
            raise ValueError(axis + ' report belongs to another source.')
        if reference['saved_at_ns'] != path.stat().st_mtime_ns or proof['captured_at_ns'] < reference['saved_at_ns']:
            raise ValueError('Source proof must follow the saved ' + axis + ' report.')
    if fingerprint(root) != source:
        raise ValueError('Source changed while checking the final artifacts.')
    return {'status': 'passed', 'axes': ['Spec', 'Standards'], 'head': source['head'],
            'tree': source['tree'], 'files': len(source['files']),
            'boundary': 'Validates supplied final evidence at this instant; does not prevent external writers or certify review quality.'}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--root', type=Path, default=Path(__file__).resolve().parents[1])
    parser.add_argument('--manifest', type=Path, required=True)
    parser.add_argument('--artifacts', type=Path)
    args = parser.parse_args()
    root = args.root.resolve()
    if args.artifacts:
        args.artifacts = args.artifacts.resolve()
        if args.artifacts.is_relative_to(root):
            print(json.dumps({'status': 'failed', 'reason': 'Phase artifacts must be outside the frozen repository.'}))
            return 1
    try:
        result = validate(root, json.loads(args.manifest.read_text()))
        code = 0
    except (OSError, ValueError, KeyError, TypeError) as exc:
        result, code = {'status': 'failed', 'reason': str(exc)}, 1
    if args.artifacts:
        args.artifacts.mkdir(parents=True, exist_ok=True)
        write_json(args.artifacts / 'phase-gate.json', result)
    print(json.dumps(result))
    return code


if __name__ == '__main__':
    sys.exit(main())
