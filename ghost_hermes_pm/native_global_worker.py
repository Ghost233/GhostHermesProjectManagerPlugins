"""Separate authorized materialization action and native fixed test process."""
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys


def main(payload):
    attempt = payload['attempt']
    if payload['action'] == 'prepare':
        from .manager import _git, _repository
        authorization = payload['authorization']
        expected = [{k: c[k] for k in ('request_id', 'commit', 'path')} for c in attempt['children']]
        if authorization['children'] != expected:
            raise ValueError('Child preparation authorization is not the exact frozen scope.')
        for child in attempt['children']:
            path = child['repository']['worktree']
            if _repository({'repo_path': path, 'test_artifact_paths': child['repository']['test_artifact_paths']}) != child['repository']:
                raise ValueError('The approved child root or Git metadata binding changed.')
            if _git(path, 'status', '--porcelain=v1'):
                raise ValueError('User changes prevent native child preparation; nothing is overwritten.')
            _git(path, 'cat-file', '-e', child['commit'] + '^{commit}')
        for child in attempt['children']:
            if _git(child['repository']['worktree'], 'rev-parse', 'HEAD') != child['commit']:
                _git(child['repository']['worktree'], '-c', 'core.hooksPath=/dev/null', 'switch', '--detach', '--no-overwrite-ignore', child['commit'])
        return {'validation_id': attempt['id'], 'authorization_digest': authorization['digest'], 'status': 'ended', 'related_execution': 'ended', 'operations': expected}
    if payload['action'] != 'run':
        raise ValueError('Unknown native validation worker operation.')
    result = {**payload['run'], 'status': 'ended', 'related_execution': 'ended', 'input_changes': [], 'tests': [], 'defects': []}
    for identifier in attempt['test_ids']:
        argv = payload['runner'] + payload['tests'][identifier]
        process = subprocess.Popen(argv, cwd=attempt['repository']['worktree'], env=payload['environment'], stdout=subprocess.PIPE, stderr=subprocess.STDOUT, start_new_session=True)
        output, _ = process.communicate()
        try:
            os.killpg(process.pid, 0)
        except ProcessLookupError:
            pass
        else:
            result.update(status='running', related_execution='unverified')
        result['tests'].append({'id': identifier, 'argv': argv, 'cwd': attempt['repository']['worktree'], 'exit_code': process.returncode, 'output_digest': hashlib.sha256(output).hexdigest(), 'artifact_refs': []})
        if result['related_execution'] != 'ended':
            break
    return result


if __name__ == '__main__':
    try:
        receipt = main(json.load(sys.stdin))
        path = Path(sys.argv[1])
        temporary = path.with_suffix('.pending')
        temporary.write_text(json.dumps(receipt))
        temporary.replace(path)
    except Exception:
        sys.exit(1)
