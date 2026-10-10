"""Run repository checks once, retaining raw command exits and frozen input evidence."""
import argparse
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import tempfile
import time

from check_support import digest, fingerprint, write_json


def execute(command, root, environment, artifacts, number):
    log = artifacts / ('command-' + str(number) + '.log')
    record = {'command': command, 'log': str(log), 'actual_exit_code': None, 'terminal': False}
    try:
        with log.open('wb') as output:
            result = subprocess.run(command, cwd=root, env=environment, stdout=output, stderr=subprocess.STDOUT)
        record.update(actual_exit_code=result.returncode, terminal=True)
    except KeyboardInterrupt:
        record['reason'] = 'Command interrupted without complete terminal evidence.'
    finally:
        if log.exists():
            record['log_sha256'] = digest(log)
    return record


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--root', type=Path, default=Path(__file__).resolve().parents[1])
    parser.add_argument('--artifacts', type=Path)
    parser.add_argument('--python', default=sys.executable)
    parser.add_argument('--node', default='node')
    parser.add_argument('--diff-base')
    parser.add_argument('command', nargs=argparse.REMAINDER)
    args = parser.parse_args()
    root = args.root.resolve()
    artifacts = (args.artifacts or Path(tempfile.mkdtemp(prefix='hermes-checks-'))).resolve()
    if artifacts.is_relative_to(root):
        parser.error('Check artifacts must be outside the frozen repository.')
    artifacts.mkdir(parents=True, exist_ok=True)
    environment = {**os.environ, 'PYTHONDONTWRITEBYTECODE': '1', 'HERMES_REQUIRE_SDK_SMOKE': '1'}
    before = fingerprint(root)
    record = {'source_before': before, 'commands': [], 'preparation': [], 'command_exit_code': None,
              'validation_exit_code': 3, 'reason': None, 'mode': 'command' if args.command else 'full', 'started_at_ns': time.time_ns(),
              'environment': {key: environment.get(key) for key in ('HERMES_TEST_SDK_ROOT', 'HERMES_REQUIRE_SDK_SMOKE', 'HERMES_TEST_SESSION_PYTHON', 'DSH_TEST_SDK_ROOT', 'DSH_REQUIRE_SDK_SMOKE')}}
    def interrupted(*_):
        raise KeyboardInterrupt
    signal.signal(signal.SIGTERM, interrupted)
    try:
        runtime = execute([args.python, '-c', 'import json,sys; print(json.dumps(list(sys.version_info[:3])))'], root, environment, artifacts, 'runtime')
        record['preparation'].append(runtime)
        version = json.loads(Path(runtime['log']).read_text()) if runtime['actual_exit_code'] == 0 else []
        if version[:2] < [3, 11]:
            raise ValueError('Python >=3.11 is required before any validation command.')
        commands = args.command[1:] if args.command[:1] == ['--'] else args.command
        if commands:
            commands = [commands]
        else:
            sdk = environment.get('HERMES_TEST_SDK_ROOT')
            if not sdk or not Path(sdk).is_dir():
                raise ValueError('Prepare the fixed Hermes SDK and set HERMES_TEST_SDK_ROOT before full validation.')
            dsh_sdk = environment.get('DSH_TEST_SDK_ROOT')
            if not dsh_sdk or not Path(dsh_sdk).is_dir():
                raise ValueError('Prepare the original DSH SDK before full validation.')
            environment['DSH_REQUIRE_SDK_SMOKE'] = '1'
            session_python = environment.get('HERMES_TEST_SESSION_PYTHON')
            if session_python:
                session = execute([session_python, '-c', 'import json,sys; print(json.dumps(list(sys.version_info[:3])))'], root, environment, artifacts, 'sdk-runtime')
                record['preparation'].append(session)
                if session['actual_exit_code'] != 0 or json.loads(Path(session['log']).read_text())[:2] < [3, 11]:
                    raise ValueError('The SDK session Python >=3.11 must be prepared before full validation.')
            lint = execute([args.python, '-m', 'ruff', '--version'], root, environment, artifacts, 'ruff')
            node = execute([args.node, '--version'], root, environment, artifacts, 'node')
            record['preparation'] += [lint, node]
            if lint['actual_exit_code'] != 0 or node['actual_exit_code'] != 0:
                raise ValueError('Install the pinned test dependencies and Node before full validation.')
            import tomllib
            project = tomllib.loads((root / 'pyproject.toml').read_text())
            pinned = next(value.removeprefix('ruff==') for value in project['project']['optional-dependencies']['test'] if value.startswith('ruff=='))
            if Path(lint['log']).read_text().strip() != 'ruff ' + pinned:
                raise ValueError('Installed Ruff differs from the pinned test dependency.')
            base = tempfile.mkdtemp(prefix='hc-', dir='/tmp')
            diff = ['git', 'diff', '--check'] + ([args.diff_base, 'HEAD'] if args.diff_base else [])
            commands = [[args.python, 'tools/check_sdk_test_seams.py'],
                        [args.python, '-m', 'ruff', 'check', '--no-cache', '--select', 'F821', 'ghost_hermes_pm', 'tests', 'tools', '__init__.py'],
                        [args.python, '-m', 'pytest', '-q', '--basetemp=' + base],
                        [args.node, '--check', 'dashboard/dist/index.js'],
                        [args.node, '--check', 'ghost_hermes_pm/owned_native_host.mjs'], diff]
        record['validation_exit_code'] = 0
        for number, command in enumerate(commands):
            result = execute(command, root, environment, artifacts, number)
            record['commands'].append(result)
            record['command_exit_code'] = result['actual_exit_code']
            if not result['terminal']:
                record.update(validation_exit_code=130, reason=result['reason'])
                break
            if result['actual_exit_code'] != 0:
                record['validation_exit_code'] = result['actual_exit_code'] if result['actual_exit_code'] > 0 else 1
                record['reason'] = 'Original command failed or was terminated.'
                break
    except (OSError, ValueError) as exc:
        record.update(validation_exit_code=3, reason=str(exc))
    except KeyboardInterrupt:
        record.update(validation_exit_code=130, reason='Validation interrupted; no complete command terminal evidence.')
    finally:
        after = fingerprint(root)
        record.update(source_after=after, source_unchanged=before == after, finished_at_ns=time.time_ns())
        if before != after:
            record['validation_exit_code'] = record['validation_exit_code'] or 2
            record['reason'] = record['reason'] or 'Frozen source changed during validation.'
        write_json(artifacts / 'result.json', record)
        print(json.dumps({'status': 'passed' if record['validation_exit_code'] == 0 else 'failed',
              'command_exit_code': record['command_exit_code'], 'validation_exit_code': record['validation_exit_code'],
              'files': len(before['files']), 'artifacts': str(artifacts)}))
    return record['validation_exit_code']


if __name__ == '__main__':
    sys.exit(main())
