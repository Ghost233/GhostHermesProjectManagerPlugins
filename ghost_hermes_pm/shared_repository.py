"""One native Kanban work in the user's existing DSH Desktop, never a new Host."""
import asyncio
from collections import Counter
import fcntl
import hashlib
import json
import os
from pathlib import Path
import stat
import shlex
import re
import subprocess
import time
import tempfile
import uuid
from copy import deepcopy

from .manager import ManagementError, _private_state_directory
from .repository_execution import private_file
from .shared_desktop import SharedDesktopTransport, desktop_identity


TOOLS = ['ask_user_question', 'bash', 'job_kill', 'job_list', 'job_output', 'skill']
CASES = {'allowed_write', 'outside_write_denied', 'symlink_write_denied', 'hardlink_write_denied',
         'danger_denied', 'foreground_job_registered', 'managed_background_stopped',
         'foreground_cancelled', 'foreign_job_kill_denied', 'background_write_boundary'}
CAPS = {'max_wall_seconds': 1800, 'max_model_requests': 80}
SUPERVISE_OBSERVATION_SLICE_SECONDS = 360
_RUNTIME_VERSIONS = {}
_RUNTIME_ENTRIES = set()


def _trusted_binary(value, workspace, kind):
    path = Path(value)
    info = path.lstat()
    if (not path.is_absolute() or str(path.resolve(strict=True)) != str(path)
            or not stat.S_ISREG(info.st_mode) or info.st_uid not in {0, os.getuid()}
            or info.st_mode & 0o022 or not os.access(path, os.R_OK | os.X_OK)
            or workspace is not None and path.is_relative_to(Path(workspace).resolve(strict=True))):
        raise ManagementError('configuration_missing', 'The approved runtime path is not a safe executable outside the repository.')
    key = (str(path), kind, info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns, info.st_ctime_ns)
    if key in _RUNTIME_VERSIONS:
        return dict(_RUNTIME_VERSIONS[key])
    environment = {key: value for key, value in os.environ.items()
        if not key.startswith(('PYTHON', 'NODE_', 'DYLD_', 'LD_', 'GIT_'))}
    result = subprocess.run([str(path), '--version'], env=environment, capture_output=True, text=True, timeout=5)
    version = (result.stdout + result.stderr).strip()
    match = re.fullmatch(r'Python (\d+)\.(\d+)\.(\d+)' if kind == 'python' else r'v(\d+)\.(\d+)\.(\d+)', version)
    if result.returncode or not match or tuple(map(int, match.groups())) < ((3, 11, 0) if kind == 'python' else (22, 0, 0)):
        raise ManagementError('configuration_missing', 'The approved runtime version is incompatible.')
    evidence = {'path': str(path), 'version': version, 'sha256': hashlib.sha256(path.read_bytes()).hexdigest()}
    _RUNTIME_VERSIONS[key] = evidence
    return dict(evidence)


def _runtime_entries(configuration, workspace):
    source, verification = configuration['source_snapshot'], configuration['verification']
    evidence = {'source_python': _trusted_binary(source['python'], workspace, 'python'),
        'verification_python': _trusted_binary(verification['python'], workspace, 'python'),
        'node': _trusted_binary(verification['node'], workspace, 'node'),
        'native_node': _trusted_binary(configuration['node_bin'], workspace, 'node')}
    paths = (source['python'], verification['python'], verification['node'], configuration['node_bin'], source['reader'], verification['runner'])
    key = tuple((path, *(getattr(Path(path).stat(), name) for name in ('st_dev', 'st_ino', 'st_size', 'st_mtime_ns', 'st_ctime_ns'))) for path in paths)
    key += (source.get('reader_sha256'), verification.get('runner_sha256'))
    if key in _RUNTIME_ENTRIES:
        return evidence
    environment = {key: value for key, value in os.environ.items()
        if not key.startswith(('PYTHON', 'NODE_', 'DYLD_', 'LD_', 'GIT_'))}
    with tempfile.TemporaryDirectory(prefix='hc-runtime-', dir='/tmp') as directory:
        private_root = Path(directory).resolve(strict=True)
        probe = private_root / 'repository'
        probe.mkdir(mode=0o700)
        subprocess.run(['/usr/bin/git', 'init', '-q', str(probe)], env=environment, check=True, timeout=5)
        for python, entry in ((source['python'], source['reader']), (verification['python'], verification['runner'])):
            arguments = [python, '-I', '-B', entry, str(probe), *(['--state'] if entry == verification['runner'] else [])]
            result = subprocess.run(arguments, env=environment, capture_output=True, text=True, timeout=10)
            if result.returncode or not isinstance(json.loads(result.stdout), dict):
                raise ManagementError('configuration_missing', 'The original isolated read-only runtime entry failed.')
        (probe / 'runtime.test.mjs').write_text("import test from 'node:test'; import assert from 'node:assert/strict'; test('runtime',()=>assert.equal(1+1,2));\n")
        artifacts = private_root / 'artifacts'
        artifacts.mkdir(mode=0o700)
        result = subprocess.run([verification['python'], '-I', '-B', verification['runner'], str(probe), str(artifacts),
            '--node', verification['node'], '--binding', 'runtime-probe', 'runtime-probe', 'runtime-probe', '--', 'runtime.test.mjs'],
            env=environment, capture_output=True, text=True, timeout=15)
        if result.returncode:
            raise ManagementError('configuration_missing', 'The original Node test runtime entry failed.')
        receipt = json.loads(result.stdout)
        if receipt.get('executed_tests') != 1 or receipt.get('failed_tests') != 0 or receipt.get('exit_code') != 0:
            raise ManagementError('configuration_missing', 'The original Node framework runtime result is unverified.')
    _RUNTIME_ENTRIES.add(key)
    return evidence


def shared_configuration(reference, configuration, *, workspace=None):
    required = {'schema', 'approved', 'mode', 'desktop_receipt', 'host_lease', 'service_ref',
                'runtime_package_root', 'node_bin', 'agent_preset', 'ledger_dir', 'provider', 'model',
                'budget', 'tool_receipt_ref', 'composition_sha256', 'source_snapshot', 'verification'}
    if (set(configuration) != required or configuration['schema'] != 1
        or configuration['approved'] is not True or configuration['mode'] != 'shared_native'):
        raise ManagementError('configuration_missing', 'An approved shared native execution reference is required.')
    for key in ('host_lease', 'service_ref', 'agent_preset', 'provider', 'model'):
        if not isinstance(configuration[key], str) or not configuration[key]:
            raise ManagementError('configuration_missing', 'Shared execution identities are incomplete.')
    budget = configuration['budget']
    if (not isinstance(budget, dict) or set(budget) != set(CAPS)
        or any(type(budget[key]) is not int or not 0 < budget[key] <= maximum for key, maximum in CAPS.items())):
        raise ManagementError('configuration_missing', 'The shared work requires its explicit finite budget.')
    sdk, node, ledger = (Path(configuration[key]) for key in ('runtime_package_root', 'node_bin', 'ledger_dir'))
    if any(not path.is_absolute() or str(path.resolve(strict=True)) != str(path) for path in (sdk, node, ledger)):
        raise ManagementError('unsafe_state', 'Shared native paths must be canonical.')
    _private_state_directory(ledger)
    source = configuration['source_snapshot']
    if not isinstance(source, dict) or set(source) != {'python', 'reader', 'python_sha256', 'reader_sha256'}:
        raise ManagementError('configuration_missing', 'A fixed read-only source fingerprint entry is required.')
    for name in ('python', 'reader'):
        path = Path(source[name])
        if (not path.is_absolute() or str(path.resolve(strict=True)) != str(path)
                or workspace is not None and path.is_relative_to(Path(workspace).resolve(strict=True))
                or name == 'reader' and hashlib.sha256(path.read_bytes()).hexdigest() != source[name + '_sha256']):
            raise ManagementError('configuration_missing', 'The fixed source fingerprint entry changed.')
    if source['reader_sha256'] != hashlib.sha256(Path(__file__).with_name('shared_source_reader.py').read_bytes()).hexdigest():
        raise ManagementError('configuration_missing', 'The shared source reader must be this reviewed main plugin entry.')
    verify_configuration(configuration, workspace=workspace)
    runtime_versions = _runtime_entries(configuration, workspace)
    receipt = json.loads(private_file(configuration['tool_receipt_ref']).read_text())
    if (receipt.get('schema') != 1 or receipt.get('status') != 'passed'
        or sorted(receipt.get('tool_catalog', [])) != TOOLS
        or receipt.get('composition_sha256') != configuration['composition_sha256']
        or receipt.get('source_hook_sha256') != hashlib.sha256(Path(__file__).with_name('shared_native_hook.mjs').read_bytes()).hexdigest()
        or not CASES <= set(receipt.get('passed_cases', []))
        or receipt.get('managed_jobs') != {'foreground': True, 'background': True, 'scope': 'darwin_process_group'}
        or not isinstance(receipt.get('sdk_sources'), dict) or not receipt['sdk_sources']):
        raise ManagementError('configuration_missing', 'The exact shared native tool boundary has not passed.')
    for relative, digest in receipt['sdk_sources'].items():
        path = (sdk / relative).resolve(strict=True)
        if not path.is_relative_to(sdk) or hashlib.sha256(path.read_bytes()).hexdigest() != digest:
            raise ManagementError('configuration_missing', 'An original shared native SDK proof source changed.')
    desktop_identity(configuration['desktop_receipt'], configuration['host_lease'])
    return dict(configuration, reference_sha256=hashlib.sha256(reference.read_bytes()).hexdigest(),
                runtime_configuration={'budget': budget}, runtime_versions=runtime_versions)


def ledger_path(configuration, session_id, suffix='.json'):
    return Path(configuration['ledger_dir']) / (hashlib.sha256(session_id.encode()).hexdigest() + suffix)


def read_ledger(configuration, execution):
    row = json.loads(private_file(ledger_path(configuration, execution['session_id'])).read_text())
    expected = {'schema': 1, 'session_id': execution['session_id'], 'generation': execution['generation'],
                'session_created_at': execution['session_created_at'], 'workspace': configuration['workspace']}
    expected.update({key: configuration[key] for key in ('host_lease', 'service_ref', 'agent_preset', 'provider', 'model')})
    budget = row.get('budget')
    legacy_tokens = {'max_reported_tokens', 'max_output_tokens_per_request'}
    if (any(row.get(key) != value for key, value in expected.items())
            or not isinstance(budget, dict) or set(budget) not in (set(CAPS), set(CAPS) | legacy_tokens)
            or any(type(budget[key]) is not int or budget[key] != configuration['budget'][key] for key in CAPS)
            or any(type(budget[key]) is not int or budget[key] <= 0 for key in legacy_tokens & set(budget))):
        raise ManagementError('binding_conflict', 'The original shared Session ledger changed.')
    return row


def _write_initial_ledger(configuration, execution):
    row = {'schema': 1, 'session_id': execution['session_id'], 'generation': execution['generation'],
           'session_created_at': execution['session_created_at'], 'workspace': configuration['workspace'],
           **{key: configuration[key] for key in ('host_lease', 'service_ref', 'agent_preset', 'provider', 'model', 'budget')},
           'state': {'started_at_ms': None, 'requests': 0, 'reported_tokens': 0, 'admissions': [], 'stop_reason': None}}
    path = ledger_path(configuration, execution['session_id'])
    if path.exists():
        read_ledger(configuration, execution)
        return
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, 'w') as output:
        json.dump(row, output)
        output.flush()
        os.fsync(output.fileno())


def confirm_shared_permission(permission, events, projections, *, before_cursor=None, reconcile=False):
    """Validate the original permission-command result and Session policy."""
    if permission is None and not reconcile or permission is not None and (not isinstance(permission, dict)
            or not isinstance(permission.get('result'), dict) or permission['result'].get('kind') != 'success'
            or not isinstance(permission.get('commandId'), str) or not permission['commandId']):
        raise ManagementError('outcome_unknown', 'The exact new shared Session permission switch was not confirmed.')
    allowed = {'permission/preset', 'sandbox/mode', 'approval/policy', 'command/run', 'command/done', 'agent/inbox/spliced'}
    if any(event['type'] not in allowed for event in events):
        raise ManagementError('binding_conflict', 'The new shared Session has unexpected work before permission admission.')
    runs = [event for event in events if event['type'] == 'command/run']
    done = [event for event in events if event['type'] == 'command/done']
    if (len(runs) != 1 or len(done) != 1 or runs[0]['data'].get('name') != 'permission'
            or runs[0]['data'].get('args', '').strip() != 'workspace-write'
            or runs[0]['data'].get('source') != {'kind': 'user'}
            or not isinstance(runs[0]['data'].get('commandId'), str) or not runs[0]['data']['commandId']
            or done[0]['data'].get('commandId') != runs[0]['data']['commandId']
            or done[0]['data'].get('kind') != 'success' or done[0]['seq'] <= runs[0]['seq']
            or before_cursor is not None and runs[0]['seq'] <= before_cursor
            or permission is not None and permission['commandId'] != runs[0]['data']['commandId']):
        raise ManagementError('outcome_unknown', 'The original shared permission-command pair is unconfirmed; no replay.')
    effects = {}
    for event in events:
        if event['type'] in {'permission/preset', 'sandbox/mode', 'approval/policy'}:
            effects[event['type']] = event
    expected = {'permission/preset': ('preset', 'workspace-write'), 'sandbox/mode': ('mode', 'workspace-write'),
                'approval/policy': ('policy', 'ask')}
    if (projections.get('permissions') != {'currentValue': 'workspace-write'}
            or any(kind not in effects or effects[kind]['data'].get(key) != value
                   or effects[kind]['seq'] >= done[0]['seq']
                   for kind, (key, value) in expected.items())):
        raise ManagementError('binding_conflict', 'The original shared Session permission projection differs.')
    notices = []
    for event in events:
        if event['type'] != 'agent/inbox/spliced':
            continue
        inserted = event['data'].get('inserted')
        if (not runs[0]['seq'] < event['seq'] < done[0]['seq'] or event['data'].get('target') != 'next-step'
                or not isinstance(inserted, list) or any(item.get('role') != 'user'
                    or item.get('source') != {'kind': 'user-approval'} or not isinstance(item.get('id'), str)
                    or item.get('content') != [{'type': 'text', 'text':
                        'The approval policy changed from "never" to "ask" (changed by the user).'}]
                    for item in inserted)):
            raise ManagementError('binding_conflict', 'The new shared Session has unexpected queued input.')
        notices.extend(inserted)
    if projections.get('inbox') != {'next-turn': [], 'next-step': notices}:
        raise ManagementError('binding_conflict', 'The new shared Session inbox differs from its original policy notice.')
    return {'command_id': runs[0]['data']['commandId'], 'run_seq': runs[0]['seq'], 'done_seq': done[0]['seq'],
            'policy_seq': {kind: event['seq'] for kind, event in effects.items()}}


def require_permission_initializer_end(previous, old_run, card_id):
    """Check only the preceding worker of this permission-initialization intent."""
    import psutil
    identity = previous['identity']
    if (old_run is None or old_run.id != previous['run_id'] or old_run.task_id != card_id
            or old_run.status == 'running' or old_run.ended_at is None
            or type(identity.get('pid')) is not int or identity['pid'] <= 0
            or old_run.worker_pid not in {None, identity['pid']}):
        raise ManagementError('outcome_unknown', 'The original permission-initialization worker has not ended.')
    if not psutil.pid_exists(identity['pid']):
        return
    try:
        birth = float(identity['start_sec']) + float(identity['start_usec']) / 1000000
        actual_birth = psutil.Process(identity['pid']).create_time()
    except psutil.NoSuchProcess:
        return
    except (psutil.AccessDenied, KeyError, TypeError, ValueError, OSError):
        raise ManagementError('outcome_unknown', 'The original permission-initialization process identity is unknown.') from None
    if abs(actual_birth - birth) < .000001:
        raise ManagementError('outcome_unknown', 'The original permission-initialization worker is still alive.')


def resolve_shared_workspace(intake, record, carrier):
    from .repository_supervision import owned_call
    execution = record['dsh_execution']
    workspace = owned_call(carrier, 'workspace/create', {'path': record['target']['repo_path']})['workspace']
    if execution.get('workspace_id', workspace['workspaceId']) != workspace['workspaceId']:
        raise ManagementError('binding_conflict', 'The registered original Workspace identity changed.')
    execution['workspace_id'] = workspace['workspaceId']
    intake._save_execution(record, fields=['workspace_id'])
    return workspace


def bind_shared_workspace(intake, record, carrier):
    """Account only this existing Session, without resending work or changing its journal."""
    from .repository_supervision import owned_call
    execution = record['dsh_execution']
    if (execution.get('mode') != 'shared_native' or carrier.config['session_id'] != execution['session_id']
            or carrier.config['generation'] != execution['generation'] or carrier.native_identity != execution['native_identity']):
        raise ManagementError('binding_conflict', 'Only the registered shared Session may gain its Workspace account.')
    def snapshot():
        stream = carrier.stream('session/follow', {'request': {'address': {'kind': 'session',
            'sessionId': execution['session_id']}, 'maxMessages': 500}})
        try:
            value = next(stream)
        finally:
            stream.close()
        header = value.get('header', {})
        if (header.get('id') != execution['session_id'] or header.get('cwd') != record['target']['repo_path']
                or header.get('createdAt') != execution['session_created_at'] or value.get('hasMore') is not False
                or value.get('projections', {}).get('values', {}).get('agentPreset') != carrier.config['agent_preset']):
            raise ManagementError('binding_conflict', 'The existing Session identity or original history differs.')
        return value
    before = snapshot()
    workspace = resolve_shared_workspace(intake, record, carrier)
    if execution['session_id'] not in workspace['sessionIds']:
        rows = [row for row in owned_call(carrier, 'session/list', {})['items']
                if row['sessionId'] == execution['session_id']]
        if (len(rows) != 1 or rows[0].get('cwd') != record['target']['repo_path']
                or rows[0].get('running') is not False or rows[0].get('agentAvailable') is not True):
            raise ManagementError('outcome_unknown', 'Only the original idle shared Session may gain its Workspace account.')
        ledger = ledger_path(carrier.config, execution['session_id'])
        budget_before = private_file(ledger).read_bytes() if ledger.exists() else None
        created = owned_call(carrier, 'session/create', {'sessionId': execution['session_id'],
            'workspaceId': workspace['workspaceId'], 'agentPreset': carrier.config['agent_preset']})
        budget_after = private_file(ledger).read_bytes() if ledger.exists() else None
        if (created != {'sessionId': execution['session_id'], 'agentPreset': carrier.config['agent_preset']}
                or snapshot() != before or budget_after != budget_before):
            raise ManagementError('outcome_unknown', 'Workspace accounting changed the original Session or budget.')
        workspace = resolve_shared_workspace(intake, record, carrier)
    if workspace['sessionIds'].count(execution['session_id']) != 1:
        raise ManagementError('binding_conflict', 'The original Workspace has no unique account for this Session.')
    return workspace


def prepare_shared_continuation(intake, record, message_id):
    """Checkpoint one explicitly owned pre-model failure; never dispatch or replay input."""
    from .repository_control import _card, _worker_end
    from .repository_supervision import owned_call
    from .repository_execution import execution_configuration
    execution = record['dsh_execution']
    if (execution.get('mode') != 'shared_native' or execution.get('state') != 'execution_failed'
            or execution.get('first_input') != 'accepted' or execution.get('stop_requested')):
        raise ManagementError('outcome_unknown', 'Only the original pre-model failure can prepare same-Session continuation.')
    _card(intake, record)
    worker = _worker_end(intake, record)
    configuration, _ = execution_configuration(record['target'])
    configuration['workspace'] = record['target']['repo_path']
    budget_file = ledger_path(configuration, execution['session_id'])
    budget_before = private_file(budget_file).read_bytes()
    state = read_ledger(configuration, execution)['state']
    if state != {'started_at_ms': None, 'requests': 0, 'reported_tokens': 0, 'admissions': [], 'stop_reason': None}:
        raise ManagementError('outcome_unknown', 'Started or uncertain work cannot use pre-model continuation.')
    carrier = shared_attach(intake, record, observe=False)
    try:
        stream = carrier.stream('session/follow', {'request': {'address': {'kind': 'session',
            'sessionId': execution['session_id']}, 'maxMessages': 500}})
        try:
            snapshot = next(stream)
        finally:
            stream.close()
        rows = [row for row in owned_call(carrier, 'session/list', {})['items'] if row['sessionId'] == execution['session_id']]
        events = [row['event'] for row in snapshot['records']]
        starts = [row for row in events if row['type'] == 'turn/start']
        ends = [row for row in events if row['type'] == 'turn/end']
        if (len(rows) != 1 or rows[0].get('running') is not False or rows[0].get('agentAvailable') is not True
                or not starts or not ends or ends[-1]['seq'] <= starts[-1]['seq']
                or ends[-1]['data'].get('reason', {}).get('kind') != 'error'
                or snapshot['projections']['values'].get('inbox') != {'next-turn': [], 'next-step': []}
                or any(row['type'] in {'assistant/message', 'tool/call'} for row in events)
                or carrier.owned_jobs() or private_file(budget_file).read_bytes() != budget_before):
            raise ManagementError('outcome_unknown', 'The original failure has unverified input, model, tool or job activity.')
        approved = json.loads(private_file(record['target']['execution_ref']).read_text())
        approved.pop('host_lease')
        workspace = resolve_shared_workspace(intake, record, carrier)
        checkpoint = {'status': 'prepared', 'message_id': message_id, 'native_identity': execution['native_identity'],
            'scope_sha256': hashlib.sha256(json.dumps(approved, sort_keys=True).encode()).hexdigest(),
            'reference_sha256': execution['reference_sha256'], 'snapshot_sha256': hashlib.sha256(json.dumps(snapshot, sort_keys=True).encode()).hexdigest(),
            'ledger_sha256': hashlib.sha256(budget_before).hexdigest(), 'cursor': snapshot['cursor'], 'worker': worker,
            'workspace': {'workspace_id': workspace['workspaceId'], 'session_ids': workspace['sessionIds']}}
        execution['maintenance_checkpoint'] = checkpoint
        intake._save_execution(record, fields=['maintenance_checkpoint'])
        return record
    finally:
        carrier.close()


def _maintenance_snapshot_matches(snapshot, checkpoint):
    """Prove the original snapshot through the one native restore marker and default."""
    if hashlib.sha256(json.dumps(snapshot, sort_keys=True).encode()).hexdigest() == checkpoint['snapshot_sha256']:
        return True
    cursor = checkpoint['cursor']
    header, projections, records = snapshot.get('header', {}), snapshot.get('projections', {}), snapshot.get('records', [])
    if (header.get('isSeeded') is not False or type(header.get('delegationDepth')) is not int
            or header['delegationDepth'] != 0 or snapshot.get('cursor') != cursor + 1
            or projections.get('asOfSeq') != cursor + 1 or len(records) != cursor + 2):
        return False
    marker = records[-1]
    event = marker.get('event', {})
    if (set(marker) != {'type', 'event'} or marker['type'] != 'event'
            or set(event) != {'type', 'seq', 'time', 'data'} or event['type'] != 'session/end-seed'
            or type(event['seq']) is not int or event['seq'] != cursor + 1 or event['data'] != {}
            or type(event['time']) is not int or not 0 <= event['time'] <= 2 ** 53 - 1):
        return False
    original = dict(snapshot, cursor=cursor, records=records[:-1],
        projections=dict(projections, asOfSeq=cursor))
    if hashlib.sha256(json.dumps(original, sort_keys=True).encode()).hexdigest() == checkpoint['snapshot_sha256']:
        return True
    candidates = [original, dict(original, header={key: value for key, value in header.items() if key != 'delegationDepth'})]
    for candidate in candidates:
        if hashlib.sha256(json.dumps(candidate, sort_keys=True).encode()).hexdigest() == checkpoint['snapshot_sha256']:
            return True
        attributed = deepcopy(candidate)
        changed = False
        for entry in attributed.get('projections', {}).get('values', {}).get('contextHeaders', {}).get('headers', []):
            for tool in entry.get('tools', []):
                if tool.get('name') == 'bash' and tool.get('plugin') == 'tool-bash-persistent':
                    tool['plugin'] = 'tool-bash'
                    changed = True
        if changed and hashlib.sha256(json.dumps(attributed, sort_keys=True).encode()).hexdigest() == checkpoint['snapshot_sha256']:
            return True
    return False


def admit_shared_continuation(intake, record, message_id, generation):
    """Rebind only a checkpointed pre-model failure after the original Host ended."""
    import psutil
    from .repository_control import _card, _worker_end
    from .repository_execution import execution_configuration
    from .repository_supervision import owned_call
    execution = record['dsh_execution']
    checkpoint = execution.get('maintenance_checkpoint', {})
    if (execution.get('mode') != 'shared_native' or execution.get('state') != 'execution_failed'
            or execution.get('first_input') != 'accepted' or execution.get('stop_requested')
            or execution.get('continuation_control') or checkpoint.get('status') != 'prepared'
            or execution.get('native_identity') != checkpoint.get('native_identity')
            or execution.get('reference_sha256') != checkpoint.get('reference_sha256')):
        raise ManagementError('outcome_unknown', 'Only the prepared original pre-model failure may continue once.')
    card = _card(intake, record)
    if card['status'] not in {'blocked', 'scheduled', 'triage'} or _worker_end(intake, record,
            frozenset({'blocked', 'scheduled', 'triage'})) != checkpoint['worker']:
        raise ManagementError('outcome_unknown', 'The checkpointed original worker has not ended.')
    configuration, _ = execution_configuration(record['target'])
    approved = json.loads(private_file(record['target']['execution_ref']).read_text())
    approved.pop('host_lease')
    if hashlib.sha256(json.dumps(approved, sort_keys=True).encode()).hexdigest() != checkpoint['scope_sha256']:
        raise ManagementError('binding_conflict', 'Only the original configuration Host lease may change.')
    old = checkpoint['native_identity']
    if psutil.pid_exists(old['pid']):
        try:
            if psutil.Process(old['pid']).create_time() == old['created_at']:
                raise ManagementError('outcome_unknown', 'The checkpointed original Host is still alive.')
        except psutil.NoSuchProcess:
            pass
    configuration.update(workspace=record['target']['repo_path'], session_id=execution['session_id'],
        generation=execution['generation'], workspace_id=execution['workspace_id'])
    budget_file = ledger_path(configuration, execution['session_id'])
    budget_before = private_file(budget_file).read_bytes()
    if hashlib.sha256(budget_before).hexdigest() != checkpoint['ledger_sha256']:
        raise ManagementError('binding_conflict', 'The checkpointed original budget bytes changed.')
    row = read_ledger(dict(configuration, host_lease=old['lease']), execution)
    if row['state'] != {'started_at_ms': None, 'requests': 0, 'reported_tokens': 0,
                        'admissions': [], 'stop_reason': None}:
        raise ManagementError('outcome_unknown', 'Started or uncertain work cannot continue after maintenance.')
    carrier = SharedDesktopTransport(configuration)
    try:
        new = carrier.native_identity
        if (new['lease'] == old['lease'] or new['pid'] == old['pid'] or new['created_at'] == old['created_at']):
            raise ManagementError('binding_conflict', 'The original Launcher maintenance restart is unverified.')
        stream = carrier.stream('session/follow', {'request': {'address': {'kind': 'session',
            'sessionId': execution['session_id']}, 'maxMessages': 500}})
        try:
            snapshot = next(stream)
        finally:
            stream.close()
        header = snapshot.get('header', {})
        listing = owned_call(carrier, 'session/list', {})
        sessions = [item for item in listing['items'] if item['sessionId'] == execution['session_id']]
        workspace = owned_call(carrier, 'workspace/create', {'path': configuration['workspace']})
        account = workspace['workspace']
        if (header.get('id') != execution['session_id'] or header.get('createdAt') != execution['session_created_at']
                or header.get('cwd') != configuration['workspace'] or snapshot.get('hasMore') is not False
                or snapshot.get('projections', {}).get('values', {}).get('agentPreset') != configuration['agent_preset']
                or not _maintenance_snapshot_matches(snapshot, checkpoint)
                or len(sessions) != 1 or sessions[0].get('cwd') != configuration['workspace']
                or sessions[0].get('running') is not False or sessions[0].get('agentAvailable') is not True
                or workspace.get('created') is not False or account.get('path') != configuration['workspace']
                or {'workspace_id': account['workspaceId'], 'session_ids': account['sessionIds']} != checkpoint['workspace']
                or carrier.owned_jobs() or shared_receipts(dict(configuration, host_lease=old['lease']), execution, '.tools.jsonl')
                or shared_receipts(dict(configuration, host_lease=old['lease']), execution, '.jobs.jsonl')
                or private_file(budget_file).read_bytes() != budget_before):
            raise ManagementError('binding_conflict', 'The restored original Session, Workspace, journal or budget differs.')
        intake.require_active(generation)
        retire_shared(intake, record['id'])
        execution['continuation_request_id'] = 'continue-' + uuid.uuid4().hex
        execution['continuation_control'] = {'message_id': message_id, 'status': 'rebind_intent',
            'dispatch_status': 'intent', 'previous_claim': execution['supervisor_claim'],
            'original_failure': {key: execution[key] for key in ('state', 'terminal_reason', 'last_failure') if key in execution}}
        intake._save_execution(record, fields=['continuation_request_id', 'continuation_control'])
        row['host_lease'] = configuration['host_lease']
        temporary = budget_file.with_name(budget_file.name + '.' + uuid.uuid4().hex)
        fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
        try:
            with os.fdopen(fd, 'w') as output:
                json.dump(row, output)
                output.flush()
                os.fsync(output.fileno())
            os.replace(temporary, budget_file)
            directory = os.open(budget_file.parent, os.O_RDONLY)
            try:
                os.fsync(directory)
            finally:
                os.close(directory)
        finally:
            if temporary.exists():
                temporary.unlink()
        execution.update(native_identity=new, reference_sha256=configuration['reference_sha256'], state='continuation_pending')
        execution['continuation_control']['status'] = 'admitted'
        execution['continuation_control']['ledger_sha256'] = hashlib.sha256(private_file(budget_file).read_bytes()).hexdigest()
        execution['shared_event_observer'] = {'status': 'unavailable', 'session_id': execution['session_id'],
            'generation': execution['generation']}
        intake._save_execution(record, fields=['native_identity', 'reference_sha256', 'state', 'continuation_control', 'shared_event_observer'])
        if card['status'] == 'triage':
            from hermes_cli import kanban_db as kb
            from hermes_cli.kanban_db_connect import connect_closing
            with connect_closing(board=intake.board) as connection:
                if not kb.specify_triage_task(connection, record['card_id'], author='verified-owner'):
                    raise ManagementError('outcome_unknown', 'Original native triage continuation remains unconfirmed.')
        else:
            intake._kanban('kanban_unblock', {'task_id': record['card_id']})
        execution['continuation_control']['dispatch_status'] = 'accepted'
        intake._save_execution(record, fields=['continuation_control'])
        return record
    finally:
        carrier.close()


def _require_max_tokens_budget(configuration, state, window=None):
    if window is not None:
        return _require_window_budget(configuration, {'state': state, 'renewWindow': window})
    now = time.time() * 1000
    if (state.get('stop_reason') is not None or type(state.get('started_at_ms')) is not int
            or not 0 < state['started_at_ms'] <= now
            or now >= state['started_at_ms'] + configuration['budget']['max_wall_seconds'] * 1000
            or type(state.get('requests')) is not int or not 0 < state['requests'] < configuration['budget']['max_model_requests']
            or type(state.get('reported_tokens')) is not int
            or state['reported_tokens'] < 0
            or not isinstance(state.get('admissions'), list) or len(state['admissions']) != state['requests']
            or any(not isinstance(value, str) or not value for value in state['admissions'])
            or len(set(state['admissions'])) != state['requests']):
        raise ManagementError('outcome_unknown', 'The original shared budget has no confirmed remaining allowance.')
    return (state['started_at_ms'] + configuration['budget']['max_wall_seconds'] * 1000 - now) / 1000


def _window_fingerprint(row):
    binding = {key: row[key] for key in ('schema', 'service_ref', 'host_lease', 'agent_preset', 'session_id',
        'session_created_at', 'generation', 'workspace', 'provider', 'model')}
    binding['budget'] = {key: row['budget'][key] for key in (*CAPS, 'max_reported_tokens', 'max_output_tokens_per_request') if key in row['budget']}
    binding['renewWindow'] = {key: value for key, value in row['renewWindow'].items() if key not in {'stop_reason', 'hook_marker', 'started_at_ms'}}
    return hashlib.sha256(json.dumps(binding, separators=(',', ':'), ensure_ascii=False).encode()).hexdigest()


def _require_window_budget(configuration, row):
    window, state = row['renewWindow'], row['state']
    original = window['original_state']
    used = state['requests'] - window['request_base']
    if (window['stop_reason'] is not None or not 0 <= used < configuration['budget']['max_model_requests']
            or state['started_at_ms'] != original['started_at_ms'] or state['stop_reason'] != original['stop_reason']
            or state['reported_tokens'] < original['reported_tokens']
            or state['admissions'][:window['request_base']] != original['admissions']):
        raise ManagementError('outcome_unknown', 'The explicit new window is stopped or has no request remainder.')
    started = window['started_at_ms']
    remaining = configuration['budget']['max_wall_seconds'] if started is None else (started + configuration['budget']['max_wall_seconds'] * 1000 - time.time() * 1000) / 1000
    if remaining <= 0:
        raise ManagementError('outcome_unknown', 'The explicit new window wall deadline has passed.')
    return remaining


def _rejected_bash_window_evidence(events, receipts):
    calls = [event['data'] for event in events if event['type'] == 'tool/call' and event['data'].get('name') == 'bash']
    results = {event['data'].get('message', {}).get('toolCallId'): event['data'].get('message', {})
               for event in events if event['type'] == 'tool/result'}
    call_ids = [call.get('callId') for call in calls]
    if (any(not isinstance(call_id, str) or not call_id for call_id in call_ids)
            or len(set(call_ids)) != len(call_ids)):
        return False
    receipt_by_id = {row.get('call_id'): row for row in receipts if row.get('name') == 'bash'}
    rejected = []
    for call_id in call_ids:
        result, receipt = results.get(call_id), receipt_by_id.get(call_id)
        if isinstance(result, dict) and result.get('isError') is not True:
            continue
        texts = [block.get('text') for block in result.get('content', []) if isinstance(block, dict)
                 and block.get('type') == 'text'] if isinstance(result, dict) else []
        if (not isinstance(result, dict) or result.get('isError') is not True
                or not any(text in {'Shared work tool ownership is unknown', 'Error: Shared work tool ownership is unknown.'}
                           for text in texts)
                or not isinstance(receipt, dict) or receipt.get('is_error') is not True
                or receipt.get('execution_admission') != 'rejected'):
            return False
        rejected.append(call_id)
    return len(rejected) == 6


def _source_core(value):
    if not isinstance(value, dict):
        return None
    core = {key: value.get(key) for key in ('head', 'workspace_status', 'source_digest')}
    if (not isinstance(core['workspace_status'], str) or not isinstance(core['source_digest'], str)
            or core['head'] is not None and not isinstance(core['head'], str)):
        return None
    return core


def _verified_source_chain(execution, baseline, receipts, current, owned_jobs=()):
    previous = _source_core(baseline)
    current = _source_core(current)
    if previous is None or current is None:
        return False
    found = False
    previous_seq = 0
    for receipt in receipts:
        before, after = receipt.get('source_before'), receipt.get('source_after')
        if before is None and after is None:
            continue
        if (receipt.get('generation') != execution['generation'] or receipt.get('session_id') != execution['session_id']
                or receipt.get('name') != 'bash'
                or receipt.get('execution_admission') != 'admitted' or receipt.get('is_error') is not False):
            return False
        if receipt.get('kind') == 'foreground':
            if (type(receipt.get('exit_code')) is not int or receipt.get('timed_out') is not False
                    or receipt.get('aborted') is not False):
                return False
        elif receipt.get('kind') == 'promoted':
            job_id = receipt.get('job_id')
            if (any(key in receipt for key in ('exit_code', 'timed_out', 'aborted'))
                    or not isinstance(job_id, str) or not job_id or not isinstance(owned_jobs, (list, tuple))):
                return False
            matches = [job for job in owned_jobs if isinstance(job, dict) and job.get('id') == job_id]
            if (len(matches) != 1 or matches[0].get('owner') != execution['session_id']
                    or matches[0].get('status') not in {'completed', 'killed', 'failed'}):
                return False
        else:
            return False
        sequence = receipt.get('native_session_seq')
        if type(sequence) is not int or sequence <= previous_seq:
            return False
        before, after = _source_core(before), _source_core(after)
        if before is None or after is None or before != previous:
            return False
        previous, previous_seq, found = after, sequence, True
    return found and previous == current


def prepare_shared_window_resume(intake, record, message_id):
    """Freeze one explicitly rejected, undelivered active window before normal maintenance."""
    from .repository_acceptance import DELIVERY_MARKER, repository_source_state
    from .repository_control import _card, _worker_end
    from .repository_supervision import owned_call
    execution = record['dsh_execution']
    acceptance = execution.get('acceptance', {})
    if (execution.get('mode') != 'shared_native' or execution.get('state') != 'awaiting_acceptance'
            or execution.get('terminal_reason') != {'kind': 'completed'} or execution.get('first_input') != 'accepted'
            or execution.get('stop_requested') or execution.get('lifecycle_end')
            or execution.get('window_resume_checkpoint')
            or acceptance.get('status') != 'unmet' or acceptance.get('delivered') is not False
            or 'delivery_report_missing' not in acceptance.get('unmet', [])):
        raise ManagementError('outcome_unknown', 'Only the original undelivered completed renewed window may prepare maintenance continuation.')
    _card(intake, record)
    worker = _worker_end(intake, record)
    scope = json.loads(private_file(record['target']['execution_ref']).read_text())
    configuration = shared_configuration(Path(record['target']['execution_ref']), scope, workspace=record['target']['repo_path'])
    configuration.update(workspace=record['target']['repo_path'], session_id=execution['session_id'], generation=execution['generation'])
    budget_file = ledger_path(configuration, execution['session_id'])
    raw = private_file(budget_file).read_bytes()
    row = read_ledger(configuration, execution)
    if ('renewWindow' not in row or row['renewWindow'].get('host_lease') != configuration['host_lease']
            or row['renewWindow'].get('hook_marker') != 'same-sid-window-v1'):
        raise ManagementError('outcome_unknown', 'The original active renewed window is unavailable.')
    _require_window_budget(configuration, row)
    source = repository_source_state(record['target'])
    if source != execution.get('baseline'):
        raise ManagementError('binding_conflict', 'The original repository source changed before maintenance continuation.')
    carrier = SharedDesktopTransport(configuration)
    try:
        if carrier.native_identity != execution['native_identity']:
            raise ManagementError('binding_conflict', 'Prepare the active window before its normal Host maintenance.')
        stream = carrier.stream('session/follow', {'request': {'address': {'kind': 'session', 'sessionId': execution['session_id']}, 'maxMessages': 500}})
        try:
            snapshot = next(stream)
        finally:
            stream.close()
        events = [item['event'] for item in snapshot['records']]
        header, values = snapshot.get('header', {}), snapshot.get('projections', {}).get('values', {})
        starts = [event for event in events if event['type'] == 'turn/start']
        ends = [event for event in events if event['type'] == 'turn/end']
        settlements = [event for event in events if event['type'] in {'assistant/message', 'assistant/attempt'}]
        sessions = [item for item in owned_call(carrier, 'session/list', {})['items'] if item['sessionId'] == execution['session_id']]
        workspace = resolve_shared_workspace(intake, record, carrier)
        receipts = shared_receipts(configuration, execution, '.tools.jsonl')
        delivery = any(DELIVERY_MARKER in block.get('text', '') for event in events if event['type'] == 'assistant/message'
                       for block in event['data'].get('message', {}).get('content', []) if isinstance(block, dict)
                       and block.get('type') == 'text')
        if (snapshot.get('hasMore') is not False or header.get('id') != execution['session_id']
                or header.get('createdAt') != execution['session_created_at'] or header.get('cwd') != configuration['workspace']
                or values.get('agentPreset') != configuration['agent_preset']
                or values.get('permissions') != {'currentValue': 'workspace-write'}
                or values.get('inbox') != {'next-turn': [], 'next-step': []}
                or len(sessions) != 1 or sessions[0].get('running') is not False or sessions[0].get('agentAvailable') is not True
                or not starts or not ends or ends[-1]['seq'] <= starts[-1]['seq']
                or ends[-1]['data'].get('reason') != execution['terminal_reason']
                or row['state'].get('requests') != len(settlements) or delivery
                or not _rejected_bash_window_evidence(events, receipts)
                or any(job['status'] in {'running', 'stopping'} for job in carrier.owned_jobs())
                or private_file(budget_file).read_bytes() != raw):
            raise ManagementError('outcome_unknown', 'The completed active window has unverified delivery, tool, job or budget activity.')
        backup = budget_file.with_name(budget_file.name + '.resume-window-' + hashlib.sha256(raw).hexdigest())
        if not backup.exists():
            with os.fdopen(os.open(backup, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600), 'wb') as output:
                output.write(raw)
                output.flush()
                os.fsync(output.fileno())
        if private_file(backup).read_bytes() != raw:
            raise ManagementError('binding_conflict', 'Original active window backup bytes differ.')
        receipt_proofs = {}
        for suffix in ('.tools.jsonl', '.jobs.jsonl'):
            path = ledger_path(configuration, execution['session_id'], suffix)
            content = private_file(path).read_bytes() if path.exists() else b''
            receipt_proofs[suffix] = {'size': len(content), 'sha256': hashlib.sha256(content).hexdigest()}
        execution['window_resume_checkpoint'] = {'status': 'prepared', 'message_id': message_id, 'scope': scope,
            'native_identity': execution['native_identity'], 'worker': worker, 'ledger_backup': str(backup),
            'ledger_sha256': hashlib.sha256(raw).hexdigest(), 'cursor': snapshot['cursor'],
            'snapshot_sha256': hashlib.sha256(json.dumps(snapshot, sort_keys=True).encode()).hexdigest(),
            'workspace': {'workspace_id': workspace['workspaceId'], 'session_ids': workspace['sessionIds']},
            'receipts': receipt_proofs, 'source': source}
        intake._save_execution(record, fields=['window_resume_checkpoint'])
        return record
    finally:
        carrier.close()


def admit_shared_window_resume(intake, record, message_id, generation):
    """Re-adopt one checkpointed active window without resetting its deadline or usage."""
    import psutil
    from .repository_control import _card, _worker_end
    from .repository_execution import execution_configuration
    from .repository_supervision import owned_call
    execution = record['dsh_execution']
    checkpoint = execution.get('window_resume_checkpoint', {})
    if (checkpoint.get('status') != 'prepared' or execution.get('mode') != 'shared_native'
            or execution.get('state') != 'awaiting_acceptance' or execution.get('terminal_reason') != {'kind': 'completed'}
            or execution.get('stop_requested') or _worker_end(intake, record) != checkpoint.get('worker')
            or execution.get('native_identity') != checkpoint.get('native_identity')):
        raise ManagementError('outcome_unknown', 'Only the prepared active window may continue after normal maintenance.')
    configuration, _ = execution_configuration(record['target'])
    scope = json.loads(private_file(record['target']['execution_ref']).read_text())
    expected = json.loads(json.dumps(checkpoint['scope']))
    expected['host_lease'] = scope.get('host_lease')
    if scope != expected:
        raise ManagementError('binding_conflict', 'The prepared active window scope changed beyond its Host lease.')
    old = checkpoint['native_identity']
    if psutil.pid_exists(old['pid']):
        try:
            if psutil.Process(old['pid']).create_time() == old['created_at']:
                raise ManagementError('outcome_unknown', 'The original Host has not completed normal maintenance.')
        except psutil.NoSuchProcess:
            pass
    configuration.update(workspace=record['target']['repo_path'], session_id=execution['session_id'],
        generation=execution['generation'], workspace_id=execution['workspace_id'])
    budget_file = ledger_path(configuration, execution['session_id'])
    raw = private_file(budget_file).read_bytes()
    if (hashlib.sha256(raw).hexdigest() != checkpoint['ledger_sha256']
            or private_file(checkpoint['ledger_backup']).read_bytes() != raw):
        raise ManagementError('binding_conflict', 'The prepared active window bytes changed.')
    row = read_ledger(dict(configuration, host_lease=old['lease']), execution)
    if (row.get('renewWindow', {}).get('host_lease') != old['lease']
            or row['renewWindow'].get('original_host_lease') == configuration['host_lease']):
        raise ManagementError('binding_conflict', 'The active renewed window Host binding changed.')
    _require_window_budget(configuration, row)
    carrier = SharedDesktopTransport(configuration)
    try:
        new = carrier.native_identity
        if any(new[key] == old[key] for key in ('lease', 'pid', 'created_at')):
            raise ManagementError('binding_conflict', 'Normal Launcher maintenance is unverified.')
        workspace = resolve_shared_workspace(intake, record, carrier)
        if {'workspace_id': workspace['workspaceId'], 'session_ids': workspace['sessionIds']} != checkpoint['workspace']:
            raise ManagementError('binding_conflict', 'The original Workspace account changed.')
        if execution.get('window_resume_adoption') == 'intent':
            raise ManagementError('outcome_unknown', 'Original active window adoption is unknown; no replay.')
        if execution.get('window_resume_adoption') != 'accepted':
            execution['window_resume_adoption'] = 'intent'
            intake._save_execution(record, fields=['window_resume_adoption'])
            created = owned_call(carrier, 'session/create', {'sessionId': execution['session_id'],
                'workspaceId': workspace['workspaceId'], 'agentPreset': configuration['agent_preset']})
            if created != {'sessionId': execution['session_id'], 'agentPreset': configuration['agent_preset']}:
                raise ManagementError('outcome_unknown', 'Original active window adoption was not confirmed.')
            execution['window_resume_adoption'] = 'accepted'
            intake._save_execution(record, fields=['window_resume_adoption'])
        stream = carrier.stream('session/follow', {'request': {'address': {'kind': 'session', 'sessionId': execution['session_id']}, 'maxMessages': 500}})
        try:
            snapshot = next(stream)
        finally:
            stream.close()
        sessions = [item for item in owned_call(carrier, 'session/list', {})['items'] if item['sessionId'] == execution['session_id']]
        if (not _maintenance_snapshot_matches(snapshot, checkpoint) or len(sessions) != 1
                or sessions[0].get('running') is not False or sessions[0].get('agentAvailable') is not True
                or any(job['status'] in {'running', 'stopping'} for job in carrier.owned_jobs())
                or private_file(budget_file).read_bytes() != raw):
            raise ManagementError('binding_conflict', 'The restored active window, history or jobs differ.')
        for suffix, proof in checkpoint['receipts'].items():
            path = ledger_path(configuration, execution['session_id'], suffix)
            content = private_file(path).read_bytes() if path.exists() else b''
            if len(content) != proof['size'] or hashlib.sha256(content).hexdigest() != proof['sha256']:
                raise ManagementError('binding_conflict', 'The prepared active receipt bytes changed.')
        from .repository_acceptance import repository_source_state
        if repository_source_state(record['target']) != checkpoint['source']:
            raise ManagementError('binding_conflict', 'The original repository source changed during maintenance.')
        intake.require_active(generation)
        previous = execution.get('continuation_control')
        execution.setdefault('continuation_history', [])
        if previous:
            execution['continuation_history'].append(previous)
        execution['continuation_request_id'] = 'continue-' + uuid.uuid4().hex
        execution['continuation_control'] = {'kind': 'resume_window', 'message_id': message_id, 'status': 'rebind_intent',
            'dispatch_status': 'intent', 'previous_claim': execution['supervisor_claim'], 'worker': checkpoint['worker'],
            'cursor': snapshot['cursor'], 'workspace': checkpoint['workspace'],
            'snapshot_sha256': hashlib.sha256(json.dumps(snapshot, sort_keys=True).encode()).hexdigest(),
            'original_failure': {key: execution[key] for key in ('state', 'terminal_reason', 'last_failure') if key in execution}}
        intake._save_execution(record, fields=['continuation_history', 'continuation_request_id', 'continuation_control'])
        row['host_lease'] = new['lease']
        row['renewWindow']['host_lease'] = new['lease']
        row['state']['binding_sha256'] = _window_fingerprint(row)
        temporary = budget_file.with_name(budget_file.name + '.' + uuid.uuid4().hex)
        with os.fdopen(os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600), 'w') as output:
            json.dump(row, output)
            output.flush()
            os.fsync(output.fileno())
        os.replace(temporary, budget_file)
        retire_shared(intake, record['id'])
        execution.update(native_identity=new, reference_sha256=configuration['reference_sha256'], state='continuation_pending')
        execution['continuation_control'].update(status='admitted', ledger_sha256=hashlib.sha256(private_file(budget_file).read_bytes()).hexdigest())
        execution['shared_event_observer'] = {'status': 'unavailable', 'session_id': execution['session_id'],
            'generation': execution['generation']}
        checkpoint['status'] = 'admitted'
        intake._save_execution(record, fields=['native_identity', 'reference_sha256', 'state', 'continuation_control',
            'shared_event_observer', 'window_resume_checkpoint'])
        card = _card(intake, record)
        if card['status'] == 'triage':
            from hermes_cli import kanban_db as kb
            from hermes_cli.kanban_db_connect import connect_closing
            with connect_closing(board=intake.board) as connection:
                if not kb.specify_triage_task(connection, record['card_id'], author='verified-owner'):
                    raise ManagementError('outcome_unknown', 'Original triage dispatch is unconfirmed.')
        else:
            intake._kanban('kanban_unblock', {'task_id': record['card_id']})
        execution['continuation_control']['dispatch_status'] = 'accepted'
        intake._save_execution(record, fields=['continuation_control'])
        return record
    finally:
        carrier.close()


def _require_expired_window_budget(configuration, row):
    window, state = row.get('renewWindow', {}), row.get('state', {})
    original = window.get('original_state', {})
    started = window.get('started_at_ms')
    requests, request_base = state.get('requests'), window.get('request_base')
    used = requests - request_base if type(requests) is int and type(request_base) is int else -1
    if (window.get('stop_reason') not in {None, 'shared-work-wall-budget'} or type(started) is not int or started <= 0
            or time.time() * 1000 < started + configuration['budget']['max_wall_seconds'] * 1000
            or not 0 <= used < configuration['budget']['max_model_requests']
            or type(state.get('reported_tokens')) is not int or state['reported_tokens'] < 0
            or not isinstance(state.get('admissions'), list) or len(state['admissions']) != requests
            or any(not isinstance(value, str) or not value for value in state['admissions'])
            or state.get('started_at_ms') != original.get('started_at_ms')
            or state.get('stop_reason') != original.get('stop_reason')
            or state.get('reported_tokens', -1) < original.get('reported_tokens', 0)
            or state.get('admissions', [])[:window.get('request_base', -1)] != original.get('admissions')):
        raise ManagementError('outcome_unknown', 'The original renewed window is not the known expired window.')


def _repeat_budget_stopped_window(execution):
    return (execution.get('state') == 'budget_stopped' and execution.get('terminal_reason') == {
        'kind': 'aborted', 'reason': {'kind': 'hook', 'reason': 'shared-work-wall-budget'}})


def _renewal_history_sha256(execution):
    return hashlib.sha256(json.dumps({key: execution.get(key) for key in (
        'renewal_window_checkpoint_history', 'renewal_window_history', 'continuation_history', 'continuation_control')},
        sort_keys=True).encode()).hexdigest()


def prepare_shared_window_renewal(intake, record, message_id):
    """Freeze the one authorized expired renewed window before normal maintenance."""
    from .repository_acceptance import DELIVERY_MARKER, repository_source_state
    from .repository_control import _card, _worker_end
    from .repository_supervision import owned_call
    execution = record['dsh_execution']
    acceptance = execution.get('acceptance', {})
    previous_checkpoint = execution.get('renewal_window_checkpoint', {})
    repeat = _repeat_budget_stopped_window(execution)
    completed = (execution.get('state') == 'awaiting_acceptance'
        and execution.get('terminal_reason') == {'kind': 'completed'} and not previous_checkpoint)
    if (execution.get('mode') != 'shared_native' or not (completed or repeat)
            or execution.get('first_input') != 'accepted' or execution.get('stop_requested') or execution.get('lifecycle_end')
            or repeat and (previous_checkpoint.get('status') != 'admitted'
                or execution.get('renewal_window_adoption') != 'accepted'
                or execution.get('continuation_control', {}).get('status') != 'accepted')
            or acceptance.get('status') != 'unmet'
            or acceptance.get('delivered') is not False or 'delivery_report_missing' not in acceptance.get('unmet', [])):
        raise ManagementError('outcome_unknown', 'Only the original undelivered completed or wall-stopped window may prepare renewal.')
    _card(intake, record)
    worker = _worker_end(intake, record, allow_later_terminal=True)
    scope = json.loads(private_file(record['target']['execution_ref']).read_text())
    configuration = shared_configuration(Path(record['target']['execution_ref']), scope, workspace=record['target']['repo_path'])
    configuration.update(workspace=record['target']['repo_path'], session_id=execution['session_id'], generation=execution['generation'])
    budget_file = ledger_path(configuration, execution['session_id'])
    raw = private_file(budget_file).read_bytes()
    row = read_ledger(configuration, execution)
    if (row.get('renewWindow', {}).get('host_lease') != configuration['host_lease']
            or row['renewWindow'].get('hook_marker') != 'same-sid-window-v1'):
        raise ManagementError('outcome_unknown', 'The current window is not the authorized expired window.')
    _require_expired_window_budget(configuration, row)
    if repeat and row['renewWindow'].get('stop_reason') != 'shared-work-wall-budget':
        raise ManagementError('outcome_unknown', 'The repeated renewal is not the confirmed wall-budget stop.')
    source = repository_source_state(record['target'])
    carrier = SharedDesktopTransport(configuration)
    try:
        if carrier.native_identity != execution['native_identity']:
            raise ManagementError('binding_conflict', 'Prepare the expired window before its normal Host maintenance.')
        stream = carrier.stream('session/follow', {'request': {'address': {'kind': 'session', 'sessionId': execution['session_id']}, 'maxMessages': 500}})
        try:
            snapshot = next(stream)
        finally:
            stream.close()
        events = [item['event'] for item in snapshot['records']]
        header, values = snapshot.get('header', {}), snapshot.get('projections', {}).get('values', {})
        starts = [event for event in events if event['type'] == 'turn/start']
        ends = [event for event in events if event['type'] == 'turn/end']
        settlements = [event for event in events if event['type'] in {'assistant/message', 'assistant/attempt'}]
        sessions = [item for item in owned_call(carrier, 'session/list', {})['items'] if item['sessionId'] == execution['session_id']]
        workspace = resolve_shared_workspace(intake, record, carrier)
        receipts = shared_receipts(configuration, execution, '.tools.jsonl')
        owned_jobs = carrier.owned_jobs()
        if not _verified_source_chain(execution, execution.get('baseline'), receipts, source, owned_jobs):
            raise ManagementError('binding_conflict', 'The original shared source receipt chain changed before renewal.')
        delivery = any(DELIVERY_MARKER in block.get('text', '') for event in events if event['type'] == 'assistant/message'
                       for block in event['data'].get('message', {}).get('content', []) if isinstance(block, dict)
                       and block.get('type') == 'text')
        if (snapshot.get('hasMore') is not False or header.get('id') != execution['session_id']
                or header.get('createdAt') != execution['session_created_at'] or header.get('cwd') != configuration['workspace']
                or values.get('agentPreset') != configuration['agent_preset']
                or values.get('permissions') != {'currentValue': 'workspace-write'}
                or values.get('inbox') != {'next-turn': [], 'next-step': []}
                or len(sessions) != 1 or sessions[0].get('running') is not False or sessions[0].get('agentAvailable') is not True
                or not starts or not ends or ends[-1]['seq'] <= starts[-1]['seq']
                or ends[-1]['data'].get('reason') != execution['terminal_reason']
                or len(settlements) != row['state']['requests'] or delivery
                or not repeat and not _rejected_bash_window_evidence(events, receipts)
                or any(not isinstance(job, dict) or job.get('status') not in {'completed', 'killed', 'failed'} for job in owned_jobs)
                or repeat and (execution.get('tool_receipts') != receipts or execution.get('jobs') != [
                    {'id': job['id'], 'status': job['status']} for job in owned_jobs])
                or private_file(budget_file).read_bytes() != raw):
            raise ManagementError('outcome_unknown', 'The expired window has unverified delivery, tool, job or budget activity.')
        backup = budget_file.with_name(budget_file.name + '.renewal-window-' + hashlib.sha256(raw).hexdigest())
        if not backup.exists():
            with os.fdopen(os.open(backup, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600), 'wb') as output:
                output.write(raw)
                output.flush()
                os.fsync(output.fileno())
        if private_file(backup).read_bytes() != raw:
            raise ManagementError('binding_conflict', 'Original expired window backup bytes differ.')
        proofs = {}
        for suffix in ('.tools.jsonl', '.jobs.jsonl'):
            path = ledger_path(configuration, execution['session_id'], suffix)
            content = private_file(path).read_bytes() if path.exists() else b''
            proofs[suffix] = {'size': len(content), 'sha256': hashlib.sha256(content).hexdigest()}
        if repeat:
            execution.setdefault('renewal_window_checkpoint_history', []).append({
                'checkpoint': deepcopy(previous_checkpoint), 'adoption': execution['renewal_window_adoption']})
            execution['renewal_window_adoption'] = None
        execution['renewal_window_checkpoint'] = {'status': 'prepared', 'message_id': message_id, 'scope': scope,
            'repeat_budget_stopped': repeat, 'terminal_reason': deepcopy(execution['terminal_reason']),
            'jobs': [{'id': job['id'], 'status': job['status']} for job in owned_jobs],
            'history_sha256': _renewal_history_sha256(execution),
            'native_identity': execution['native_identity'], 'worker': worker, 'ledger_backup': str(backup),
            'ledger_sha256': hashlib.sha256(raw).hexdigest(), 'cursor': snapshot['cursor'],
            'snapshot_sha256': hashlib.sha256(json.dumps(snapshot, sort_keys=True).encode()).hexdigest(),
            'workspace': {'workspace_id': workspace['workspaceId'], 'session_ids': workspace['sessionIds']},
            'receipts': proofs, 'source': source, 'previous_state': deepcopy(row['state']),
            'previous_renew_window': deepcopy(row['renewWindow'])}
        fields = ['renewal_window_checkpoint']
        if repeat:
            fields += ['renewal_window_checkpoint_history', 'renewal_window_adoption']
        intake._save_execution(record, fields=fields)
        return record
    finally:
        carrier.close()


def admit_shared_window_renewal(intake, record, message_id, generation):
    """Adopt one authorized new finite window while retaining the expired-window evidence."""
    import psutil
    from .repository_control import _card, _worker_end
    from .repository_execution import execution_configuration
    from .repository_supervision import owned_call
    execution = record['dsh_execution']
    checkpoint = execution.get('renewal_window_checkpoint', {})
    repeat = checkpoint.get('repeat_budget_stopped') is True
    if (checkpoint.get('status') != 'prepared' or execution.get('mode') != 'shared_native'
            or (not _repeat_budget_stopped_window(execution) if repeat else
                execution.get('state') != 'awaiting_acceptance' or execution.get('terminal_reason') != {'kind': 'completed'})
            or execution.get('first_input') != 'accepted' or execution.get('stop_requested') or execution.get('lifecycle_end')
            or message_id == checkpoint.get('message_id')
            or repeat and (checkpoint.get('terminal_reason') != execution.get('terminal_reason')
                or checkpoint.get('history_sha256') != _renewal_history_sha256(execution)
                or execution.get('renewal_window_adoption') not in {None, 'intent', 'accepted'})
            or _worker_end(intake, record, allow_later_terminal=True) != checkpoint.get('worker')
            or execution.get('native_identity') != checkpoint.get('native_identity')):
        raise ManagementError('outcome_unknown', 'Only the prepared expired window may enter its authorized renewal.')
    configuration, _ = execution_configuration(record['target'])
    scope = json.loads(private_file(record['target']['execution_ref']).read_text())
    expected = json.loads(json.dumps(checkpoint['scope']))
    expected['host_lease'] = scope.get('host_lease')
    if scope != expected:
        raise ManagementError('binding_conflict', 'The prepared renewal scope changed beyond its Host lease.')
    old = checkpoint['native_identity']
    if psutil.pid_exists(old['pid']):
        try:
            if psutil.Process(old['pid']).create_time() == old['created_at']:
                raise ManagementError('outcome_unknown', 'The original Host has not completed normal maintenance.')
        except psutil.NoSuchProcess:
            pass
    configuration.update(workspace=record['target']['repo_path'], session_id=execution['session_id'],
        generation=execution['generation'], workspace_id=execution['workspace_id'])
    budget_file = ledger_path(configuration, execution['session_id'])
    raw = private_file(budget_file).read_bytes()
    if (hashlib.sha256(raw).hexdigest() != checkpoint['ledger_sha256']
            or private_file(checkpoint['ledger_backup']).read_bytes() != raw):
        raise ManagementError('binding_conflict', 'The prepared expired window bytes changed.')
    row = read_ledger(dict(configuration, host_lease=old['lease']), execution)
    if (row.get('state') != checkpoint['previous_state']
            or row.get('renewWindow') != checkpoint['previous_renew_window']):
        raise ManagementError('binding_conflict', 'The original expired window state changed.')
    _require_expired_window_budget(configuration, row)
    carrier = SharedDesktopTransport(configuration)
    try:
        new = carrier.native_identity
        if any(new[key] == old[key] for key in ('lease', 'pid', 'created_at')):
            raise ManagementError('binding_conflict', 'Normal Launcher maintenance is unverified.')
        workspace = resolve_shared_workspace(intake, record, carrier)
        if {'workspace_id': workspace['workspaceId'], 'session_ids': workspace['sessionIds']} != checkpoint['workspace']:
            raise ManagementError('binding_conflict', 'The original Workspace account changed.')
        if execution.get('renewal_window_adoption') == 'intent':
            raise ManagementError('outcome_unknown', 'Original renewal adoption is unknown; no replay.')
        if execution.get('renewal_window_adoption') != 'accepted':
            execution['renewal_window_adoption'] = 'intent'
            intake._save_execution(record, fields=['renewal_window_adoption'])
            created = owned_call(carrier, 'session/create', {'sessionId': execution['session_id'],
                'workspaceId': workspace['workspaceId'], 'agentPreset': configuration['agent_preset']})
            if created != {'sessionId': execution['session_id'], 'agentPreset': configuration['agent_preset']}:
                raise ManagementError('outcome_unknown', 'Original renewal adoption was not confirmed.')
            execution['renewal_window_adoption'] = 'accepted'
            intake._save_execution(record, fields=['renewal_window_adoption'])
        stream = carrier.stream('session/follow', {'request': {'address': {'kind': 'session', 'sessionId': execution['session_id']}, 'maxMessages': 500}})
        try:
            snapshot = next(stream)
        finally:
            stream.close()
        sessions = [item for item in owned_call(carrier, 'session/list', {})['items'] if item['sessionId'] == execution['session_id']]
        owned_jobs = carrier.owned_jobs()
        saved_jobs = checkpoint.get('jobs')
        if repeat and (not isinstance(saved_jobs, list)
                or any(not isinstance(job, dict) or not isinstance(job.get('id'), str) or not job['id']
                    or job.get('status') not in {'completed', 'killed', 'failed'} for job in saved_jobs)
                or len({job['id'] for job in saved_jobs}) != len(saved_jobs)
                or not isinstance(owned_jobs, list)):
            raise ManagementError('binding_conflict', 'The restored expired window, history or jobs differ.')
        if (not _maintenance_snapshot_matches(snapshot, checkpoint) or len(sessions) != 1
                or sessions[0].get('running') is not False or sessions[0].get('agentAvailable') is not True
                or any(not isinstance(job, dict) or job.get('status') not in {'completed', 'killed', 'failed'} for job in owned_jobs)
                or repeat and (any('owner' in job and job['owner'] != execution['session_id'] for job in owned_jobs)
                    or owned_jobs and [{'id': job.get('id'), 'status': job['status']} for job in owned_jobs] != saved_jobs)
                or private_file(budget_file).read_bytes() != raw):
            raise ManagementError('binding_conflict', 'The restored expired window, history or jobs differ.')
        for suffix, proof in checkpoint['receipts'].items():
            path = ledger_path(configuration, execution['session_id'], suffix)
            content = private_file(path).read_bytes() if path.exists() else b''
            if len(content) != proof['size'] or hashlib.sha256(content).hexdigest() != proof['sha256']:
                raise ManagementError('binding_conflict', 'The prepared renewal receipt bytes changed.')
        from .repository_acceptance import repository_source_state
        if repository_source_state(record['target']) != checkpoint['source']:
            raise ManagementError('binding_conflict', 'The original repository source changed during renewal maintenance.')
        intake.require_active(generation)
        previous = execution.get('continuation_control')
        execution.setdefault('continuation_history', [])
        if previous:
            execution['continuation_history'].append(previous)
        execution.setdefault('renewal_window_history', []).append({'message_id': checkpoint['message_id'],
            'ledger_backup': checkpoint['ledger_backup'], 'renew_window': checkpoint['previous_renew_window']})
        execution['continuation_request_id'] = 'continue-' + uuid.uuid4().hex
        execution['continuation_control'] = {'kind': 'renew_window', 'message_id': message_id, 'status': 'rebind_intent',
            'dispatch_status': 'intent', 'previous_claim': execution['supervisor_claim'], 'worker': checkpoint['worker'],
            'cursor': snapshot['cursor'], 'workspace': checkpoint['workspace'],
            'snapshot_sha256': hashlib.sha256(json.dumps(snapshot, sort_keys=True).encode()).hexdigest(),
            'original_failure': {key: execution[key] for key in ('state', 'terminal_reason', 'last_failure') if key in execution}}
        intake._save_execution(record, fields=['continuation_history', 'renewal_window_history', 'continuation_request_id', 'continuation_control'])
        row['renewWindow'] = {'id': 'window-' + uuid.uuid4().hex, 'message_id': message_id, 'host_lease': new['lease'],
            'request_base': row['state']['requests'], 'original_state': dict(row['state']), 'original_host_lease': old['lease'],
            'started_at_ms': None, 'stop_reason': None, 'hook_marker': None}
        row['host_lease'] = new['lease']
        row['state']['binding_sha256'] = _window_fingerprint(row)
        temporary = budget_file.with_name(budget_file.name + '.' + uuid.uuid4().hex)
        with os.fdopen(os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600), 'w') as output:
            json.dump(row, output)
            output.flush()
            os.fsync(output.fileno())
        os.replace(temporary, budget_file)
        retire_shared(intake, record['id'])
        execution.update(native_identity=new, reference_sha256=configuration['reference_sha256'], state='continuation_pending')
        execution['continuation_control'].update(status='admitted', ledger_sha256=hashlib.sha256(private_file(budget_file).read_bytes()).hexdigest())
        execution['shared_event_observer'] = {'status': 'unavailable', 'session_id': execution['session_id'],
            'generation': execution['generation']}
        checkpoint['status'] = 'admitted'
        intake._save_execution(record, fields=['native_identity', 'reference_sha256', 'state', 'continuation_control',
            'shared_event_observer', 'renewal_window_checkpoint'])
        card = _card(intake, record)
        if card['status'] == 'triage':
            from hermes_cli import kanban_db as kb
            from hermes_cli.kanban_db_connect import connect_closing
            with connect_closing(board=intake.board) as connection:
                if not kb.specify_triage_task(connection, record['card_id'], author='verified-owner'):
                    raise ManagementError('outcome_unknown', 'Original triage dispatch is unconfirmed.')
        else:
            intake._kanban('kanban_unblock', {'task_id': record['card_id']})
        execution['continuation_control']['dispatch_status'] = 'accepted'
        intake._save_execution(record, fields=['continuation_control'])
        return record
    finally:
        carrier.close()


def prepare_shared_window(intake, record, message_id):
    """Freeze one expired original window; neither adopt nor send input."""
    from .repository_control import _card, _worker_end
    from .repository_supervision import owned_call
    execution = record['dsh_execution']
    if (execution.get('mode') != 'shared_native' or execution.get('state') not in {'execution_failed', 'budget_stopped'}
            or execution.get('terminal_reason', {}).get('kind') != 'max-tokens' or execution.get('first_input') != 'accepted'
            or execution.get('stop_requested') or execution.get('lifecycle_end')
            or execution.get('acceptance', {}).get('status') == 'accepted'
            or execution.get('continuation_control', {}).get('status') not in {None, 'accepted'}):
        raise ManagementError('outcome_unknown', 'Only an explicitly unfinished original max-tokens window may be prepared.')
    _card(intake, record)
    worker = _worker_end(intake, record)
    scope = json.loads(private_file(record['target']['execution_ref']).read_text())
    checked = json.loads(json.dumps(scope))
    if checked['source_snapshot']['python'] != checked['verification']['python']:
        raise ManagementError('binding_conflict', 'The two approved Python entries must name the same canonical binary.')
    configuration = shared_configuration(Path(record['target']['execution_ref']), checked, workspace=record['target']['repo_path'])
    configuration.update(workspace=record['target']['repo_path'], session_id=execution['session_id'], generation=execution['generation'])
    budget_file = ledger_path(configuration, execution['session_id'])
    raw = private_file(budget_file).read_bytes()
    row = read_ledger(configuration, execution)
    state = row['state']
    if ('renewWindow' in row or type(state.get('started_at_ms')) is not int or not 0 < state['started_at_ms'] <= time.time() * 1000
            or time.time() * 1000 < state['started_at_ms'] + row['budget']['max_wall_seconds'] * 1000
            or state.get('stop_reason') not in {None, 'shared-work-wall-budget'} or state.get('requests', 0) <= 0):
        raise ManagementError('outcome_unknown', 'Only one known expired original wall window may gain a prepared renewal.')
    carrier = SharedDesktopTransport(configuration)
    try:
        if carrier.native_identity != execution['native_identity']:
            raise ManagementError('binding_conflict', 'Prepare the original window before its normal Host maintenance.')
        stream = carrier.stream('session/follow', {'request': {'address': {'kind': 'session', 'sessionId': execution['session_id']}, 'maxMessages': 500}})
        try:
            snapshot = next(stream)
        finally:
            stream.close()
        events = [item['event'] for item in snapshot['records']]
        starts = [event for event in events if event['type'] == 'turn/start']
        ends = [event for event in events if event['type'] == 'turn/end']
        settlements = [event for event in events if event['type'] in {'assistant/message', 'assistant/attempt'}]
        headers = [event['data']['header']['config'] for event in events if event['type'] == 'request/header']
        for event in settlements:
            usage = event['data'].get('usage') or next((item['chunk']['usage'] for item in event['data'].get('stream', [])
                if item.get('type') == 'chunk' and item.get('chunk', {}).get('type') == 'usage'), None)
            if not usage or any(type(usage.get(key, 0 if key.startswith('cache') else None)) is not int
                    or usage.get(key, 0) < 0 for key in ('inputTokens', 'outputTokens', 'cacheReadTokens', 'cacheWriteTokens')):
                raise ManagementError('outcome_unknown', 'Original native settlement usage is unknown.')
        sessions = [item for item in owned_call(carrier, 'session/list', {})['items'] if item['sessionId'] == execution['session_id']]
        workspace = resolve_shared_workspace(intake, record, carrier)
        values = snapshot['projections']['values']
        if (snapshot['hasMore'] is not False or snapshot['header']['id'] != execution['session_id']
                or snapshot['header']['createdAt'] != execution['session_created_at'] or snapshot['header']['cwd'] != configuration['workspace']
                or values.get('agentPreset') != scope['agent_preset'] or values.get('permissions') != {'currentValue': 'workspace-write'}
                or values.get('inbox') != {'next-turn': [], 'next-step': []}
                or len(sessions) != 1 or sessions[0].get('running') is not False or sessions[0].get('agentAvailable') is not True
                or not starts or not ends or ends[-1]['seq'] <= starts[-1]['seq'] or ends[-1]['data']['reason'] != execution['terminal_reason']
                or type(state['requests']) is not int or state['requests'] != len(settlements)
                or type(state.get('reported_tokens')) is not int or state['reported_tokens'] < 0
                or not isinstance(state.get('admissions'), list) or len(state['admissions']) != state['requests']
                or not all(isinstance(key, str) and key for key in state['admissions']) or len(set(state['admissions'])) != state['requests']
                or not headers or any(headers[-1].get(key) != scope[key] for key in ('provider', 'model'))
                or any(job['status'] in {'running', 'stopping'} for job in carrier.owned_jobs()) or private_file(budget_file).read_bytes() != raw):
            raise ManagementError('outcome_unknown', 'The original post-model terminal, journal, queue or jobs are unconfirmed.')
        backup = budget_file.with_name(budget_file.name + '.old-window-' + hashlib.sha256(raw).hexdigest())
        if not backup.exists():
            with os.fdopen(os.open(backup, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600), 'wb') as output:
                output.write(raw)
                output.flush()
                os.fsync(output.fileno())
        if private_file(backup).read_bytes() != raw:
            raise ManagementError('binding_conflict', 'Original window backup bytes differ.')
        receipts = {}
        for suffix in ('.tools.jsonl', '.jobs.jsonl'):
            path = ledger_path(configuration, execution['session_id'], suffix)
            content = private_file(path).read_bytes() if path.exists() else b''
            receipts[suffix] = {'size': len(content), 'sha256': hashlib.sha256(content).hexdigest()}
        execution['renew_window_checkpoint'] = {'status': 'prepared', 'message_id': message_id, 'scope': scope,
            'native_identity': execution['native_identity'], 'worker': worker, 'ledger_backup': str(backup),
            'ledger_sha256': hashlib.sha256(raw).hexdigest(), 'cursor': snapshot['cursor'], 'receipts': receipts,
            'snapshot_sha256': hashlib.sha256(json.dumps(snapshot, sort_keys=True).encode()).hexdigest(),
            'workspace': {'workspace_id': workspace['workspaceId'], 'session_ids': workspace['sessionIds']}}
        intake._save_execution(record, fields=['renew_window_checkpoint'])
        return record
    finally:
        carrier.close()


def admit_shared_window(intake, record, message_id, generation):
    """One explicit window after normal maintenance, using the original Session."""
    import psutil
    from .repository_control import _card, _worker_end
    from .repository_execution import execution_configuration
    from .repository_supervision import owned_call
    execution, checkpoint = record['dsh_execution'], record['dsh_execution']['renew_window_checkpoint']
    if (checkpoint['status'] != 'prepared' or execution.get('stop_requested') or execution.get('state') not in {'execution_failed', 'budget_stopped'}
            or _worker_end(intake, record) != checkpoint['worker'] or execution['native_identity'] != checkpoint['native_identity']):
        raise ManagementError('outcome_unknown', 'Only the prepared expired original worker may enter its one renewed window.')
    configuration, _ = execution_configuration(record['target'])
    scope = json.loads(private_file(record['target']['execution_ref']).read_text())
    expected = json.loads(json.dumps(checkpoint['scope']))
    expected['host_lease'] = scope['host_lease']
    for field in ('source_snapshot', 'verification'):
        entry = expected[field]
        if entry['python'] != expected['source_snapshot']['python'] or scope[field]['python'] != entry['python']:
            raise ManagementError('binding_conflict', 'Only the same canonical approved Python binary may refresh its two fingerprints.')
        entry['python_sha256'] = scope[field]['python_sha256']
    expected['verification']['node_sha256'] = scope['verification']['node_sha256']
    if scope != expected:
        raise ManagementError('binding_conflict', 'The prepared source scope changed beyond Host lease or the two approved Python fingerprints.')
    old = checkpoint['native_identity']
    if psutil.pid_exists(old['pid']):
        try:
            if psutil.Process(old['pid']).create_time() == old['created_at']:
                raise ManagementError('outcome_unknown', 'The original Host has not completed normal maintenance.')
        except psutil.NoSuchProcess:
            pass
    configuration.update(workspace=record['target']['repo_path'], session_id=execution['session_id'], generation=execution['generation'], workspace_id=execution['workspace_id'])
    budget_file = ledger_path(configuration, execution['session_id'])
    raw = private_file(budget_file).read_bytes()
    if (hashlib.sha256(raw).hexdigest() != checkpoint['ledger_sha256'] or private_file(checkpoint['ledger_backup']).read_bytes() != raw):
        raise ManagementError('binding_conflict', 'The original expired window bytes changed.')
    row = read_ledger(dict(configuration, host_lease=old['lease']), execution)
    carrier = SharedDesktopTransport(configuration)
    try:
        new = carrier.native_identity
        if any(new[key] == old[key] for key in ('lease', 'pid', 'created_at')):
            raise ManagementError('binding_conflict', 'Normal Launcher maintenance is unverified.')
        workspace = resolve_shared_workspace(intake, record, carrier)
        if {'workspace_id': workspace['workspaceId'], 'session_ids': workspace['sessionIds']} != checkpoint['workspace']:
            raise ManagementError('binding_conflict', 'The original Workspace account changed.')
        if execution.get('renew_window_adoption') == 'intent':
            raise ManagementError('outcome_unknown', 'Original Session adoption is unknown; no replay.')
        if execution.get('renew_window_adoption') != 'accepted':
            execution['renew_window_adoption'] = 'intent'
            intake._save_execution(record, fields=['renew_window_adoption'])
            created = owned_call(carrier, 'session/create', {'sessionId': execution['session_id'], 'workspaceId': workspace['workspaceId'], 'agentPreset': configuration['agent_preset']})
            if created != {'sessionId': execution['session_id'], 'agentPreset': configuration['agent_preset']}:
                raise ManagementError('outcome_unknown', 'Original Session adoption was not confirmed.')
            execution['renew_window_adoption'] = 'accepted'
            intake._save_execution(record, fields=['renew_window_adoption'])
        stream = carrier.stream('session/follow', {'request': {'address': {'kind': 'session', 'sessionId': execution['session_id']}, 'maxMessages': 500}})
        try:
            snapshot = next(stream)
        finally:
            stream.close()
        sessions = [item for item in owned_call(carrier, 'session/list', {})['items'] if item['sessionId'] == execution['session_id']]
        if (not _maintenance_snapshot_matches(snapshot, checkpoint) or len(sessions) != 1
                or sessions[0].get('running') is not False or sessions[0].get('agentAvailable') is not True
                or any(job['status'] in {'running', 'stopping'} for job in carrier.owned_jobs()) or private_file(budget_file).read_bytes() != raw):
            raise ManagementError('binding_conflict', 'The restored original Session, history or jobs differ.')
        for suffix, proof in checkpoint['receipts'].items():
            path = ledger_path(configuration, execution['session_id'], suffix)
            content = private_file(path).read_bytes() if path.exists() else b''
            if len(content) != proof['size'] or hashlib.sha256(content).hexdigest() != proof['sha256']:
                raise ManagementError('binding_conflict', 'The prepared original receipt bytes changed.')
        intake.require_active(generation)
        previous = execution.get('continuation_control')
        execution.setdefault('continuation_history', [])
        if previous:
            execution['continuation_history'].append(previous)
        execution['continuation_request_id'] = 'continue-' + uuid.uuid4().hex
        execution['continuation_control'] = {'kind': 'renew_window', 'message_id': message_id, 'status': 'rebind_intent', 'dispatch_status': 'intent',
            'previous_claim': execution['supervisor_claim'], 'worker': checkpoint['worker'], 'cursor': snapshot['cursor'], 'workspace': checkpoint['workspace'],
            'snapshot_sha256': hashlib.sha256(json.dumps(snapshot, sort_keys=True).encode()).hexdigest(),
            'original_failure': {key: execution[key] for key in ('state', 'terminal_reason', 'last_failure', 'native_identity', 'reference_sha256') if key in execution}}
        intake._save_execution(record, fields=['continuation_history', 'continuation_request_id', 'continuation_control'])
        row['renewWindow'] = {'id': 'window-' + uuid.uuid4().hex, 'message_id': message_id, 'host_lease': new['lease'],
            'request_base': row['state']['requests'], 'original_state': dict(row['state']), 'original_host_lease': old['lease'],
            'started_at_ms': None, 'stop_reason': None, 'hook_marker': None}
        row['host_lease'] = new['lease']
        row['state']['binding_sha256'] = _window_fingerprint(row)
        temporary = budget_file.with_name(budget_file.name + '.' + uuid.uuid4().hex)
        with os.fdopen(os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600), 'w') as output:
            json.dump(row, output)
            output.flush()
            os.fsync(output.fileno())
        os.replace(temporary, budget_file)
        retire_shared(intake, record['id'])
        execution.update(native_identity=new, reference_sha256=configuration['reference_sha256'], state='continuation_pending')
        execution['continuation_control'].update(status='admitted', ledger_sha256=hashlib.sha256(private_file(budget_file).read_bytes()).hexdigest())
        execution['shared_event_observer'] = {'status': 'unavailable', 'session_id': execution['session_id'], 'generation': execution['generation']}
        checkpoint['status'] = 'admitted'
        intake._save_execution(record, fields=['native_identity', 'reference_sha256', 'state', 'continuation_control', 'shared_event_observer', 'renew_window_checkpoint'])
        card = _card(intake, record)
        if card['status'] == 'triage':
            from hermes_cli import kanban_db as kb
            from hermes_cli.kanban_db_connect import connect_closing
            with connect_closing(board=intake.board) as connection:
                if not kb.specify_triage_task(connection, record['card_id'], author='verified-owner'):
                    raise ManagementError('outcome_unknown', 'Original triage dispatch is unconfirmed.')
        else:
            intake._kanban('kanban_unblock', {'task_id': record['card_id']})
        execution['continuation_control']['dispatch_status'] = 'accepted'
        intake._save_execution(record, fields=['continuation_control'])
        return record
    finally:
        carrier.close()


def _owner_context_failed_turn(snapshot, execution, owner_control=None):
    """Only the exact admitted renewal input that failed before its first request."""
    proof = owner_control if owner_control is not None else execution['continuation_control']
    cursor = proof.get('cursor')
    events = [row['event'] for row in snapshot['records']]
    reason = {'kind': 'error', 'error': {'message': 'Shared work native settlement usage is unknown.', 'code': 'UNKNOWN'}}
    if (type(cursor) is not int or cursor < 0 or len(events) != cursor + 7
            or snapshot.get('cursor') != cursor + 6
            or any(event.get('seq') != index for index, event in enumerate(events))
            or owner_control is None and (events[cursor].get('type') != 'session/end-seed' or events[cursor].get('data') != {})
            or owner_control is not None and (events[cursor].get('type') != 'turn/end'
                or events[cursor].get('data') != {'turn': proof.get('failed_turn'), 'reason': reason})):
        raise ManagementError('outcome_unknown', 'The original renewal input boundary is unconfirmed.')
    delta = events[cursor + 1:]
    if [event['type'] for event in delta] != ['agent/inbox/spliced', 'turn/start', 'agent/inbox/spliced',
            'step/start', 'step/end', 'turn/end']:
        raise ManagementError('outcome_unknown', 'The original renewal already has model, tool or unknown activity.')
    inserted = delta[0]['data']
    items = inserted.get('inserted', [])
    turn = delta[1]['data'].get('turn')
    if (set(inserted) != {'target', 'start', 'inserted'} or inserted.get('target') != 'next-turn'
            or inserted.get('start') != 0 or len(items) != 1 or items[0].get('role') != 'user'
            or not isinstance(items[0].get('id'), str) or not items[0]['id']
            or items[0].get('source') != {'kind': 'user', 'rpcId': (
                proof.get('request_id') if owner_control is not None else execution['continuation_request_id'])}
            or not isinstance(items[0].get('content'), list) or not items[0]['content']
            or any(not isinstance(block, dict) or block.get('type') != 'text' or not isinstance(block.get('text'), str) or not block['text']
                for block in items[0]['content'])
            or type(turn) is not int or turn < 1 or delta[1]['data'] != {'turn': turn}
            or delta[2]['data'] != {'target': 'next-turn', 'start': 0, 'removedCount': 1, 'inserted': []}
            or delta[3]['data'] != {'turn': turn, 'step': 1} or delta[4]['data'] != {'turn': turn, 'step': 1}
            or delta[5]['data'] != {'turn': turn, 'reason': reason}):
        raise ManagementError('outcome_unknown', 'The exact pre-request native failure is unconfirmed.')
    if owner_control is not None and (turn != proof.get('turn') or delta[1]['seq'] != proof.get('turn_start_seq')
            or len(items[0]['content']) != 1
            or hashlib.sha256(items[0]['content'][0]['text'].encode()).hexdigest() != proof.get('text_sha256')):
        raise ManagementError('outcome_unknown', 'The original owner context input or turn correlation changed.')
    return turn


def admit_shared_owner_context(intake, record, prepared, generation, *, prepare_maintenance=False):
    """Append one owner message to the admitted, unstarted original renewal window."""
    from .manager import _public_text
    from .simple_development import OWNER_CONTEXT_MAX_CHARS
    from .repository_control import _card, _worker_end
    from .repository_execution import execution_configuration
    from .repository_supervision import owned_call
    from .repository_acceptance import repository_source_state
    execution = record['dsh_execution']
    continuation, checkpoint = execution.get('continuation_control', {}), execution.get('renewal_window_checkpoint', {})
    previous = execution.get('owner_context_control')
    history = execution.get('owner_context_history', [])
    maintenance = execution.get('owner_context_maintenance_checkpoint', {})
    final_input = maintenance.get('status') == 'admitted'
    maintenance_chain = prepare_maintenance or final_input
    message_id = prepared.envelope['message_id']
    if (execution.get('mode') != 'shared_native' or execution.get('state') != 'outcome_unknown'
            or execution.get('first_input') != 'accepted' or execution.get('stop_requested') or execution.get('lifecycle_end')
            or execution.get('last_failure', {}).get('code') != 'outcome_unknown'
            or checkpoint.get('status') != 'admitted' or execution.get('renewal_window_adoption') != 'accepted'
            or continuation.get('kind') != 'renew_window' or continuation.get('status') != 'accepted'
            or continuation.get('dispatch_status') != 'accepted'
            or not isinstance(history, list) or len(history) != (1 if maintenance_chain else 0)
            or prepare_maintenance and maintenance
            or final_input and hashlib.sha256(json.dumps({'current': previous, 'history': history}, sort_keys=True).encode()).hexdigest()
                != maintenance.get('owners_sha256')
            or 'owner_context_control' in execution and (not isinstance(previous, dict)
                or previous.get('status') != 'accepted' or previous.get('dispatch_status') != 'accepted'
                or not isinstance(previous.get('message_id'), str) or not previous['message_id']
                or previous['message_id'] == message_id or not previous.get('snapshot_sha256')
                or type(continuation.get('cursor')) is not int or type(previous.get('cursor')) is not int
                or previous['cursor'] != continuation['cursor'] + 6 * (len(history) + 1)
                or type(previous.get('failed_turn')) is not int or previous.get('turn') != previous['failed_turn'] + 1
                or previous.get('turn_start_seq') != previous['cursor'] + 2
                or previous.get('request_id') != 'owner-context-' + hashlib.sha256(
                    (record['id'] + ':' + previous['message_id']).encode()).hexdigest())
            or not continuation.get('snapshot_sha256') or not continuation.get('ledger_sha256')):
        raise ManagementError('outcome_unknown', 'Only the unstarted original admitted renewal may accept owner context once.')
    if not isinstance(checkpoint.get('receipts'), dict) or set(checkpoint['receipts']) != {'.tools.jsonl', '.jobs.jsonl'}:
        raise ManagementError('binding_conflict', 'The original renewal receipt checkpoints are incomplete.')
    account = continuation.get('workspace')
    if (not isinstance(account, dict) or account != checkpoint.get('workspace')
            or account.get('workspace_id') != execution.get('workspace_id')
            or not isinstance(account.get('session_ids'), list) or execution.get('session_id') not in account['session_ids']):
        raise ManagementError('binding_conflict', 'The original admitted Workspace account changed.')
    text = None if prepare_maintenance else prepared.command.split('\n', 1)[1]
    if not prepare_maintenance:
        _public_text(text, intake.secret_values)
        if len(text) > OWNER_CONTEXT_MAX_CHARS:
            raise ManagementError('invalid_change', 'Owner context exceeds its bounded public input limit.')
    card = _card(intake, record)
    if card['status'] not in {'blocked', 'triage'}:
        raise ManagementError('outcome_unknown', 'The original owner-context card must be idle and awaiting input.')
    worker = _worker_end(intake, record, allow_later_terminal=True)
    configuration, _ = execution_configuration(record['target'])
    if configuration['reference_sha256'] != execution['reference_sha256']:
        raise ManagementError('binding_conflict', 'The original owner-context execution reference changed.')
    scope = json.loads(private_file(record['target']['execution_ref']).read_text())
    if scope != dict(checkpoint['scope'], host_lease=configuration['host_lease']):
        raise ManagementError('binding_conflict', 'The original renewal scope changed beyond its admitted Host lease.')
    configuration.update(workspace=record['target']['repo_path'], session_id=execution['session_id'],
        generation=execution['generation'], workspace_id=execution['workspace_id'])
    ledger_file = ledger_path(configuration, execution['session_id'])
    raw = private_file(ledger_file).read_bytes()
    row = read_ledger(configuration, execution)
    window, state = row.get('renewWindow', {}), row['state']
    previous_state = checkpoint['previous_state']
    backup = private_file(checkpoint['ledger_backup']).read_bytes()
    old = json.loads(backup)
    if (hashlib.sha256(raw).hexdigest() != (maintenance['admitted_ledger_sha256'] if final_input else continuation['ledger_sha256'])
            or window.get('request_base') != state.get('requests') or window.get('host_lease') != configuration['host_lease']
            or any(window.get(key, 'missing') is not None for key in ('started_at_ms', 'hook_marker', 'stop_reason'))
            or window.get('original_state') != previous_state or old.get('state') != previous_state
            or old.get('renewWindow') != checkpoint['previous_renew_window']
            or hashlib.sha256(backup).hexdigest() != checkpoint['ledger_sha256']
            or {key: value for key, value in state.items() if key != 'binding_sha256'}
                != {key: value for key, value in previous_state.items() if key != 'binding_sha256'}
            or state.get('binding_sha256') != _window_fingerprint(row)
            or execution.get('renewal_window_history', [])[-1:] != [{'message_id': checkpoint['message_id'],
                'ledger_backup': checkpoint['ledger_backup'], 'renew_window': checkpoint['previous_renew_window']}]):
        raise ManagementError('binding_conflict', 'The unstarted renewal ledger or cumulative state changed.')
    if repository_source_state(record['target']) != checkpoint['source']:
        raise ManagementError('binding_conflict', 'The original renewal repository source changed.')
    for suffix, proof in checkpoint['receipts'].items():
        path = ledger_path(configuration, execution['session_id'], suffix)
        content = private_file(path).read_bytes() if path.exists() else b''
        if len(content) != proof['size'] or hashlib.sha256(content).hexdigest() != proof['sha256']:
            raise ManagementError('binding_conflict', 'The original renewal native receipts changed.')
        shared_receipts(configuration, execution, suffix)
    carrier = SharedDesktopTransport(configuration)
    try:
        if carrier.native_identity != execution['native_identity']:
            raise ManagementError('binding_conflict', 'The original owner-context native identity changed.')
        def snapshot():
            stream = carrier.stream('session/follow', {'request': {'address': {'kind': 'session',
                'sessionId': execution['session_id']}, 'maxMessages': 500}})
            try:
                value = next(stream)
            finally:
                stream.close()
            header = value.get('header', {})
            if (value.get('hasMore') is not False or header.get('id') != execution['session_id']
                    or header.get('createdAt') != execution['session_created_at'] or header.get('cwd') != configuration['workspace']
                    or value.get('projections', {}).get('values', {}).get('agentPreset') != configuration['agent_preset']):
                raise ManagementError('binding_conflict', 'The original owner-context Session changed.')
            return value
        before = snapshot()
        checked = before
        if final_input:
            if not _maintenance_snapshot_matches(before, maintenance):
                raise ManagementError('binding_conflict', 'The admitted owner maintenance snapshot changed.')
            checked = dict(before, cursor=maintenance['cursor'], records=before['records'][:maintenance['cursor'] + 1])
        values = before['projections']['values']
        sessions = [item for item in owned_call(carrier, 'session/list', {})['items'] if item['sessionId'] == execution['session_id']]
        jobs = carrier.owned_jobs()
        if (len(sessions) != 1 or sessions[0].get('running') is not False or sessions[0].get('agentAvailable') is not True
                or sessions[0].get('cwd') != configuration['workspace']
                or values.get('permissions') != {'currentValue': 'workspace-write'}
                or values.get('inbox') != {'next-turn': [], 'next-step': []}
                or not isinstance(jobs, list)
                or any(not isinstance(job, dict) or job.get('status') not in {'completed', 'killed', 'failed'}
                    or 'owner' in job and job['owner'] != execution['session_id'] for job in jobs)
                or jobs and [{'id': job.get('id'), 'status': job['status']} for job in jobs] != checkpoint.get('jobs')
                or private_file(ledger_file).read_bytes() != raw):
            raise ManagementError('outcome_unknown', 'The original owner-context Session, queue or jobs are not idle.')
        if previous is not None:
            controls = [*history, previous]
            first_cursor = controls[0]['cursor']
            prefix = dict(checked, cursor=first_cursor, records=checked['records'][:first_cursor + 1])
            if _owner_context_failed_turn(prefix, execution) != controls[0]['failed_turn']:
                raise ManagementError('outcome_unknown', 'The original renewal-to-owner failure boundary changed.')
            for index, control in enumerate(controls):
                end = controls[index + 1]['cursor'] if index + 1 < len(controls) else checked['cursor']
                prefix = dict(checked, cursor=end, records=checked['records'][:end + 1])
                turn = _owner_context_failed_turn(prefix, execution, control)
        else:
            turn = _owner_context_failed_turn(checked, execution)
        intake.require_active(generation)
        if prepare_maintenance:
            backup = ledger_file.with_name(ledger_file.name + '.owner-maintenance-' + hashlib.sha256(raw).hexdigest())
            if not backup.exists():
                with os.fdopen(os.open(backup, os.O_CREAT | os.O_EXCL | os.O_WRONLY | os.O_NOFOLLOW, 0o600), 'wb') as output:
                    output.write(raw)
                    output.flush()
                    os.fsync(output.fileno())
            if private_file(backup).read_bytes() != raw:
                raise ManagementError('binding_conflict', 'The owner maintenance original ledger backup differs.')
            owners = {'current': deepcopy(previous), 'history': deepcopy(history)}
            maintenance = execution['owner_context_maintenance_checkpoint'] = {'status': 'intent', 'message_id': message_id,
                'owners': owners, 'owners_sha256': hashlib.sha256(json.dumps(owners, sort_keys=True).encode()).hexdigest(),
                'native_identity': deepcopy(execution['native_identity']), 'reference_sha256': execution['reference_sha256'],
                'scope': scope, 'worker': worker, 'workspace': deepcopy(account), 'source': checkpoint['source'],
                'receipts': deepcopy(checkpoint['receipts']), 'jobs': deepcopy(jobs), 'ledger_backup': str(backup),
                'ledger_sha256': hashlib.sha256(raw).hexdigest(), 'cursor': before['cursor'],
                'snapshot_sha256': hashlib.sha256(json.dumps(before, sort_keys=True).encode()).hexdigest()}
            intake._save_execution(record, fields=['owner_context_maintenance_checkpoint'])
            maintenance['status'] = 'prepared'
            intake._save_execution(record, fields=['owner_context_maintenance_checkpoint'])
            return record
        request_id = 'owner-context-' + hashlib.sha256((record['id'] + ':' + message_id).encode()).hexdigest()
        if previous is not None:
            execution['owner_context_history'] = [*deepcopy(history), deepcopy(previous)]
        control = execution['owner_context_control'] = {'message_id': message_id, 'request_id': request_id,
            'text_sha256': hashlib.sha256(text.encode()).hexdigest(), 'status': 'intent', 'cursor': before['cursor'],
            'snapshot_sha256': hashlib.sha256(json.dumps(before, sort_keys=True).encode()).hexdigest(),
            'previous_claim': execution['supervisor_claim'], 'worker': worker, 'failed_turn': turn}
        intake._save_execution(record, fields=['owner_context_control'] + (['owner_context_history'] if previous is not None else []))
        try:
            answer = owned_call(carrier, 'session/prompt', {'sessionId': execution['session_id'], 'requestId': request_id,
                'mode': 'queue', 'content': [{'type': 'text', 'text': text}]})
            if not isinstance(answer, dict) or answer.get('accepted') is not True:
                raise ManagementError('outcome_unknown', 'Owner context admission is unknown; no replay.')
            until = time.monotonic() + 3
            while True:
                intake.require_active(generation)
                after = snapshot()
                if any(item['event']['type'] == 'turn/start' and item['event']['seq'] > before['cursor'] for item in after['records']):
                    break
                if time.monotonic() >= until:
                    raise ManagementError('outcome_unknown', 'The owner-context turn has not appeared; no replay.')
                time.sleep(.05)
            events = [item['event'] for item in after['records'] if item['event']['seq'] > before['cursor']]
            inserted = [(event, item) for event in events if event['type'] == 'agent/inbox/spliced'
                for item in event['data'].get('inserted', [])]
            starts = [event for event in events if event['type'] == 'turn/start']
            if (after['records'][:len(before['records'])] != before['records'] or len(inserted) != 1 or len(starts) != 1
                    or inserted[0][1].get('source') != {'kind': 'user', 'rpcId': request_id}
                    or inserted[0][1].get('role') != 'user' or inserted[0][1].get('content') != [{'type': 'text', 'text': text}]
                    or inserted[0][0]['seq'] >= starts[0]['seq'] or starts[0]['data'].get('turn') != turn + 1):
                raise ManagementError('outcome_unknown', 'The unique original owner-context turn is unconfirmed; no replay.')
        except (ManagementError, OSError, TimeoutError, ValueError, KeyError):
            control['status'] = 'unknown'
            intake._save_execution(record, fields=['owner_context_control'])
            raise
        control.update(status='accepted', turn=starts[0]['data']['turn'], turn_start_seq=starts[0]['seq'], dispatch_status='intent')
        execution['state'] = 'running'
        intake._save_execution(record, fields=['owner_context_control', 'state'])
        if card['status'] == 'triage':
            from hermes_cli import kanban_db as kb
            from hermes_cli.kanban_db_connect import connect_closing
            with connect_closing(board=intake.board) as connection:
                if not kb.specify_triage_task(connection, record['card_id'], author='verified-owner'):
                    raise ManagementError('outcome_unknown', 'Original owner-context card dispatch is unconfirmed.')
        else:
            intake._kanban('kanban_unblock', {'task_id': record['card_id']})
        landed = _card(intake, record)
        if landed['status'] != 'ready' or landed.get('current_run_id') is not None:
            raise ManagementError('outcome_unknown', 'Original owner-context card is not ready for native dispatch.')
        control['dispatch_status'] = 'accepted'
        intake._save_execution(record, fields=['owner_context_control'])
        return record
    finally:
        carrier.close()


def admit_shared_owner_maintenance(intake, record, message_id, generation):
    """Rebind only the prepared owner failure; never dispatch or send input."""
    import psutil
    from .repository_control import _card, _worker_end
    from .repository_execution import execution_configuration
    from .repository_supervision import owned_call
    from .repository_acceptance import repository_source_state
    execution = record['dsh_execution']
    checkpoint = execution.get('owner_context_maintenance_checkpoint', {})
    owners = {'current': execution.get('owner_context_control'), 'history': execution.get('owner_context_history')}
    if (checkpoint.get('status') != 'prepared' or checkpoint.get('message_id') == message_id
            or execution.get('mode') != 'shared_native' or execution.get('state') != 'outcome_unknown'
            or execution.get('stop_requested') or execution.get('lifecycle_end')
            or execution.get('native_identity') != checkpoint.get('native_identity')
            or execution.get('reference_sha256') != checkpoint.get('reference_sha256')
            or owners != checkpoint.get('owners') or _worker_end(intake, record, allow_later_terminal=True) != checkpoint['worker']
            or _card(intake, record)['status'] not in {'blocked', 'triage'}):
        raise ManagementError('outcome_unknown', 'Only the exact prepared owner failure may enter normal maintenance once.')
    configuration, _ = execution_configuration(record['target'])
    scope = json.loads(private_file(record['target']['execution_ref']).read_text())
    if scope != dict(checkpoint['scope'], host_lease=configuration['host_lease']):
        raise ManagementError('binding_conflict', 'Only the owner maintenance Host lease may change.')
    old = checkpoint['native_identity']
    if psutil.pid_exists(old['pid']):
        try:
            if psutil.Process(old['pid']).create_time() == old['created_at']:
                raise ManagementError('outcome_unknown', 'The original owner maintenance Host is still alive.')
        except psutil.NoSuchProcess:
            pass
    configuration.update(workspace=record['target']['repo_path'], session_id=execution['session_id'],
        generation=execution['generation'], workspace_id=execution['workspace_id'])
    path = ledger_path(configuration, execution['session_id'])
    raw = private_file(path).read_bytes()
    if (hashlib.sha256(raw).hexdigest() != checkpoint['ledger_sha256']
            or private_file(checkpoint['ledger_backup']).read_bytes() != raw
            or repository_source_state(record['target']) != checkpoint['source']):
        raise ManagementError('binding_conflict', 'The prepared owner maintenance ledger or source changed.')
    row = read_ledger(dict(configuration, host_lease=old['lease']), execution)
    carrier = SharedDesktopTransport(configuration)
    try:
        new = carrier.native_identity
        if any(new[key] == old[key] for key in ('lease', 'pid', 'created_at')):
            raise ManagementError('binding_conflict', 'The original owner maintenance restart is unverified.')
        if checkpoint.get('adoption') == 'intent':
            raise ManagementError('outcome_unknown', 'Owner maintenance adoption remains unknown; no replay.')
        if checkpoint.get('adoption') != 'accepted':
            checkpoint['adoption'] = 'intent'
            intake._save_execution(record, fields=['owner_context_maintenance_checkpoint'])
            answer = owned_call(carrier, 'session/create', {'sessionId': execution['session_id'],
                'workspaceId': execution['workspace_id'], 'agentPreset': configuration['agent_preset']})
            if answer != {'sessionId': execution['session_id'], 'agentPreset': configuration['agent_preset']}:
                raise ManagementError('outcome_unknown', 'Same-SID owner maintenance adoption is unconfirmed.')
            checkpoint['adoption'] = 'accepted'
            intake._save_execution(record, fields=['owner_context_maintenance_checkpoint'])
        stream = carrier.stream('session/follow', {'request': {'address': {'kind': 'session',
            'sessionId': execution['session_id']}, 'maxMessages': 500}})
        try:
            snapshot = next(stream)
        finally:
            stream.close()
        header = snapshot.get('header', {})
        values = snapshot.get('projections', {}).get('values', {})
        sessions = [item for item in owned_call(carrier, 'session/list', {})['items'] if item['sessionId'] == execution['session_id']]
        if (not _maintenance_snapshot_matches(snapshot, checkpoint) or header.get('id') != execution['session_id']
                or header.get('cwd') != configuration['workspace'] or header.get('createdAt') != execution['session_created_at']
                or values.get('agentPreset') != configuration['agent_preset'] or snapshot.get('hasMore') is not False
                or values.get('inbox') != {'next-turn': [], 'next-step': []}
                or values.get('permissions') != {'currentValue': 'workspace-write'}
                or len(sessions) != 1 or sessions[0].get('cwd') != configuration['workspace']
                or sessions[0].get('running') is not False or sessions[0].get('agentAvailable') is not True
                or carrier.owned_jobs() != checkpoint['jobs'] or private_file(path).read_bytes() != raw):
            raise ManagementError('binding_conflict', 'The restored owner maintenance Session or history differs.')
        for suffix, proof in checkpoint['receipts'].items():
            receipt = ledger_path(configuration, execution['session_id'], suffix)
            content = private_file(receipt).read_bytes() if receipt.exists() else b''
            if len(content) != proof['size'] or hashlib.sha256(content).hexdigest() != proof['sha256']:
                raise ManagementError('binding_conflict', 'Original owner maintenance receipt bytes changed.')
        intake.require_active(generation)
        row['host_lease'] = new['lease']
        row['renewWindow']['host_lease'] = new['lease']
        row['state']['binding_sha256'] = _window_fingerprint(row)
        temporary = path.with_name(path.name + '.' + uuid.uuid4().hex)
        with os.fdopen(os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600), 'w') as output:
            json.dump(row, output)
            output.flush()
            os.fsync(output.fileno())
        os.replace(temporary, path)
        read_ledger(configuration, execution)
        retire_shared(intake, record['id'])
        execution.update(native_identity=new, reference_sha256=configuration['reference_sha256'])
        checkpoint.update(status='admitted', admit_message_id=message_id,
            admitted_ledger_sha256=hashlib.sha256(private_file(path).read_bytes()).hexdigest())
        intake._save_execution(record, fields=['native_identity', 'reference_sha256', 'owner_context_maintenance_checkpoint'])
        return record
    finally:
        carrier.close()


def admit_shared_max_tokens(intake, record, message_id, generation):
    """One owner-requested continuation of an idle max-tokens turn, within its original budget."""
    from .repository_control import _card, _worker_end
    from .repository_execution import execution_configuration
    from .repository_supervision import owned_call
    execution = record['dsh_execution']
    previous = execution.get('continuation_control')
    if (execution.get('mode') != 'shared_native' or execution.get('state') != 'execution_failed'
            or execution.get('terminal_reason', {}).get('kind') != 'max-tokens'
            or execution.get('first_input') != 'accepted' or execution.get('stop_requested')
            or execution.get('lifecycle_end') or execution.get('acceptance', {}).get('status') == 'accepted'
            or previous and previous.get('status') != 'accepted'):
        raise ManagementError('outcome_unknown', 'Only a known unfinished max-tokens turn may explicitly continue.')
    card = _card(intake, record)
    worker = _worker_end(intake, record)
    configuration, _ = execution_configuration(record['target'])
    if configuration['reference_sha256'] != execution['reference_sha256']:
        raise ManagementError('binding_conflict', 'The original shared scope changed.')
    configuration.update(workspace=record['target']['repo_path'], session_id=execution['session_id'],
        generation=execution['generation'], workspace_id=execution['workspace_id'])
    budget_file = ledger_path(configuration, execution['session_id'])
    budget_before = private_file(budget_file).read_bytes()
    budget_row = read_ledger(configuration, execution)
    budget = budget_row['state']
    _require_max_tokens_budget(configuration, budget, budget_row.get('renewWindow'))
    carrier = SharedDesktopTransport(configuration)
    try:
        if carrier.native_identity != execution['native_identity']:
            raise ManagementError('binding_conflict', 'The original max-tokens Host identity changed.')
        stream = carrier.stream('session/follow', {'request': {'address': {'kind': 'session',
            'sessionId': execution['session_id']}, 'maxMessages': 500}})
        try:
            snapshot = next(stream)
        finally:
            stream.close()
        header, values = snapshot.get('header', {}), snapshot.get('projections', {}).get('values', {})
        events = [row['event'] for row in snapshot['records']]
        starts = [event for event in events if event['type'] == 'turn/start']
        ends = [event for event in events if event['type'] == 'turn/end']
        messages = [event for event in events if event['type'] == 'assistant/message']
        sessions = [row for row in owned_call(carrier, 'session/list', {})['items'] if row['sessionId'] == execution['session_id']]
        workspace = owned_call(carrier, 'workspace/create', {'path': configuration['workspace']})
        account = workspace['workspace']
        if (header.get('id') != execution['session_id'] or header.get('createdAt') != execution['session_created_at']
                or header.get('cwd') != configuration['workspace'] or snapshot.get('hasMore') is not False
                or values.get('agentPreset') != configuration['agent_preset']
                or values.get('permissions') != {'currentValue': 'workspace-write'} or execution.get('native_permission') != 'workspace-write'
                or values.get('inbox') != {'next-turn': [], 'next-step': []}
                or len(sessions) != 1 or sessions[0].get('running') is not False or sessions[0].get('agentAvailable') is not True
                or sessions[0].get('cwd') != configuration['workspace'] or workspace.get('created') is not False
                or account.get('workspaceId') != execution['workspace_id'] or account.get('path') != configuration['workspace']
                or account.get('sessionIds', []).count(execution['session_id']) != 1
                or not starts or not ends or ends[-1]['seq'] <= starts[-1]['seq']
                or ends[-1]['data'].get('turn') != starts[-1]['data'].get('turn')
                or ends[-1]['data'].get('reason') != execution['terminal_reason']
                or not messages or not starts[-1]['seq'] < messages[-1]['seq'] < ends[-1]['seq']
                or not any(row.get('type') == 'chunk' and row.get('chunk', {}).get('type') == 'finish'
                    and row['chunk'].get('reason', {}).get('kind') == 'max-tokens' for row in messages[-1]['data'].get('stream', []))
                or budget['requests'] != sum(event['type'] in {'assistant/message', 'assistant/attempt'} for event in events)
                or any(job['status'] in {'running', 'stopping'} for job in carrier.owned_jobs())
                or private_file(budget_file).read_bytes() != budget_before):
            raise ManagementError('outcome_unknown', 'The original max-tokens turn, scope, input or jobs are unconfirmed.')
        headers = [event['data']['header']['config'] for event in events if event['type'] == 'request/header']
        if not headers or any(headers[-1].get(key) != configuration[key] for key in ('provider', 'model')):
            raise ManagementError('binding_conflict', 'The original max-tokens model scope changed.')
        _require_max_tokens_budget(configuration, budget, budget_row.get('renewWindow'))
        intake.require_active(generation)
        history = execution.setdefault('continuation_history', [])
        if previous:
            history.append(previous)
        execution['continuation_request_id'] = 'continue-' + uuid.uuid4().hex
        execution['continuation_control'] = {'kind': 'max_tokens', 'message_id': message_id,
            'status': 'admitted', 'dispatch_status': 'intent', 'previous_claim': execution['supervisor_claim'],
            'worker': worker, 'cursor': snapshot['cursor'],
            'workspace': {'workspace_id': account['workspaceId'], 'session_ids': account['sessionIds']},
            'snapshot_sha256': hashlib.sha256(json.dumps(snapshot, sort_keys=True).encode()).hexdigest(),
            'ledger_sha256': hashlib.sha256(budget_before).hexdigest(),
            'original_failure': {key: execution[key] for key in ('state', 'terminal_reason', 'last_failure') if key in execution}}
        execution['state'] = 'continuation_pending'
        intake._save_execution(record, fields=['continuation_history', 'continuation_request_id', 'continuation_control', 'state'])
        if card['status'] == 'triage':
            from hermes_cli import kanban_db as kb
            from hermes_cli.kanban_db_connect import connect_closing
            with connect_closing(board=intake.board) as connection:
                if not kb.specify_triage_task(connection, record['card_id'], author='verified-owner'):
                    raise ManagementError('outcome_unknown', 'Original native triage continuation remains unconfirmed.')
        else:
            intake._kanban('kanban_unblock', {'task_id': record['card_id']})
        execution['continuation_control']['dispatch_status'] = 'accepted'
        intake._save_execution(record, fields=['continuation_control'])
        return record
    finally:
        carrier.close()


def shared_attach(intake, record, *, observe=True):
    from .repository_execution import execution_configuration
    execution = record['dsh_execution']
    configuration, _ = execution_configuration(record['target'])
    if configuration['reference_sha256'] != execution['reference_sha256'] or execution.get('mode') != 'shared_native':
        raise ManagementError('binding_conflict', 'The admitted shared execution reference changed.')
    configuration.update(workspace=record['target']['repo_path'], session_id=execution['session_id'],
                         generation=execution['generation'])
    if execution.get('workspace_id'):
        configuration['workspace_id'] = execution['workspace_id']
    read_ledger(configuration, execution)
    if not observe:
        carrier = SharedDesktopTransport(configuration)
        if carrier.native_identity != execution['native_identity']:
            carrier.close()
            raise ManagementError('binding_conflict', 'The registered shared Desktop generation changed.')
        try:
            bind_shared_workspace(intake, record, carrier)
        except Exception:
            carrier.close()
            raise
        return carrier
    cache = getattr(intake, '_shared_carriers', None)
    if cache is None:
        cache = intake._shared_carriers = {}
    carrier = cache.get(record['id'])
    if carrier is not None and carrier.config['generation'] != execution['generation']:
        carrier.dispose()
        cache.pop(record['id'])
        carrier = None
    if carrier is None:
        carrier = SharedDesktopTransport(configuration)
        if carrier.native_identity != execution['native_identity']:
            carrier.close()
            raise ManagementError('binding_conflict', 'The registered shared Desktop generation changed.')
        try:
            bind_shared_workspace(intake, record, carrier)
            carrier.start_events()
        except Exception:
            carrier.close()
            raise
        carrier.keep_open = True
        cache[record['id']] = carrier
    carrier.event_frames()
    execution['shared_event_observer'] = {'status': 'ready', 'client_id': carrier.client_id, 'host_lease': configuration['host_lease'],
        'session_id': execution['session_id'], 'generation': execution['generation']}
    intake._save_execution(record, fields=['shared_event_observer'])
    return carrier


def dispose_shared(intake):
    for carrier in getattr(intake, '_shared_carriers', {}).values():
        carrier.dispose()
    intake._shared_carriers = {}


def retire_shared(intake, work_id):
    carrier = getattr(intake, '_shared_carriers', {}).pop(work_id, None)
    if carrier is not None:
        carrier.dispose()


def shared_receipts(configuration, execution, suffix):
    path = ledger_path(configuration, execution['session_id'], suffix)
    if not path.exists():
        return []
    info = private_file(path).stat()
    if info.st_nlink != 1 or not stat.S_ISREG(info.st_mode):
        raise ManagementError('coverage_unverified', 'The shared native receipt is not a private original file.')
    content = path.read_bytes()
    rows = [json.loads(line) for line in content.splitlines()]
    expected = {'schema': 1, 'generation': execution['generation'], 'session_id': execution['session_id'],
        'session_created_at': execution['session_created_at'], 'service_ref': configuration['service_ref'],
        'host_lease': configuration['host_lease']}
    prefixes = []
    for checkpoint in (execution.get('renew_window_checkpoint', {}), execution.get('window_resume_checkpoint', {}),
                       *(entry['checkpoint'] for entry in execution.get('renewal_window_checkpoint_history', [])),
                       execution.get('renewal_window_checkpoint', {})):
        proof = checkpoint.get('receipts', {}).get(suffix)
        if not proof:
            continue
        prefix = content[:proof['size']]
        if hashlib.sha256(prefix).hexdigest() != proof['sha256']:
            raise ManagementError('coverage_unverified', 'Original window receipt prefix changed.')
        prefixes.append((len(prefix.splitlines()), checkpoint['native_identity']['lease']))
    if any(prefixes[index][0] > prefixes[index + 1][0] for index in range(len(prefixes) - 1)):
        raise ManagementError('coverage_unverified', 'Original window receipt checkpoints are unordered.')
    if any(any(row.get(key) != (next((lease for count, lease in prefixes if index < count), value)
               if key == 'host_lease' else value) for key, value in expected.items()) for index, row in enumerate(rows)):
        raise ManagementError('coverage_unverified', 'A shared native receipt belongs to another Session.')
    return rows


def shared_range_proof(intake, record, session_end):
    """An idle exact Session and settled own jobs, never proof of a Host exit."""
    from .repository_supervision import original_history, owned_call
    execution = record['dsh_execution']
    carrier = shared_attach(intake, record, observe=False)
    try:
        listing = owned_call(carrier, 'session/list', {})
        rows = [row for row in listing['items'] if row['sessionId'] == execution['session_id']]
        events, projections = original_history(carrier, execution['session_id'], record['target']['repo_path'])
        jobs = carrier.owned_jobs()
        starts = [event for event in events if event['type'] == 'turn/start']
        ends = [event for event in events if event['type'] == 'turn/end']
        if (len(rows) != 1 or rows[0].get('running') is not False or rows[0].get('agentAvailable') is not True
            or rows[0].get('cwd') != record['target']['repo_path']
            or starts and (not ends or ends[-1]['seq'] <= starts[-1]['seq'])
            or projections.get('inbox') != {'next-turn': [], 'next-step': []}
            or any(job['status'] in {'running', 'stopping'} for job in jobs)
            or session_end.get('session_id') != execution['session_id']
            or session_end.get('generation') != execution['generation']
            or session_end.get('journal_cursor') != (events[-1]['seq'] if events else -1)):
            raise ManagementError('outcome_unknown', 'The exact shared Session and its jobs are not quiescent.')
        tools = shared_receipts(carrier.config, execution, '.tools.jsonl')
        jobs_log = shared_receipts(carrier.config, execution, '.jobs.jsonl')
        calls = [(event['data']['callId'], event['data']['name']) for event in events if event['type'] == 'tool/call']
        if Counter(calls) != Counter((row['call_id'], row['name']) for row in tools):
            raise ManagementError('coverage_unverified', 'Shared native tool settlement coverage is incomplete.')
        registered = [row for row in jobs_log if row['type'] == 'registered']
        settled = [row for row in jobs_log if row['type'] == 'settled']
        if (any(row.get('owner') != execution['session_id'] for row in jobs_log)
            or Counter(row['job_id'] for row in registered) != Counter(row['job_id'] for row in settled)
            or any(row.get('awaited') is not True or row.get('cause') == 'teardown'
                   or row.get('status') not in {'completed', 'killed', 'failed'} for row in settled)
            or Counter(row['command_sha256'] for row in tools if row['name'] == 'bash' and row.get('execution_admission') != 'rejected')
                != Counter(row['command_sha256'] for row in registered)):
            raise ManagementError('coverage_unverified', 'Shared native managed job settlement is incomplete.')
        return {'scope': 'shared_session', 'host': execution['native_identity'],
            'session_id': execution['session_id'], 'generation': execution['generation'],
            'journal_cursor': session_end['journal_cursor'], 'settled_call_ids': [row['call_id'] for row in tools],
            'settled_job_ids': [row['job_id'] for row in settled], 'managed_scope': 'darwin_process_group',
            'escaped_descendants': 'unverified', 'host_shutdown': False}
    finally:
        carrier.close()


def prepare_shared_github(intake, record):
    """Use native writable tmp for this generation's existing account-copy flow."""
    from .repository_github import prepare_github_execution
    execution = record['dsh_execution']
    if not execution.get('github_private_dir'):
        execution['github_private_dir'] = str(Path(tempfile.mkdtemp(prefix='hpm-shared-gh-', dir='/tmp')).resolve(strict=True))
        intake._save_execution(record, fields=['github_private_dir'])
    directory = Path(execution['github_private_dir'])
    if directory.parent != Path('/tmp').resolve() or not directory.name.startswith('hpm-shared-gh-'):
        raise ManagementError('binding_conflict', 'Shared GitHub preparation must stay in its original native tmp directory.')
    prepared = prepare_github_execution(intake.github, record['target'], directory, execution['generation'])
    if execution.get('github_reference') and execution['github_reference'] != prepared['reference']:
        raise ManagementError('binding_conflict', 'The original shared GitHub generation reference changed.')
    execution['github_reference'] = prepared['reference']
    intake._save_execution(record, fields=['github_reference'])
    return prepared['reference']


def cleanup_shared_github(intake, record):
    from .repository_github import cleanup_github_execution
    execution = record['dsh_execution']
    reference = execution.get('github_reference')
    if not reference:
        return {'status': 'not_created'}
    try:
        result = cleanup_github_execution(reference, execution['generation'])
    except ManagementError as error:
        execution['github_cleanup'] = {'status': 'unconfirmed', 'code': error.code}
        intake._save_execution(record, fields=['github_cleanup'])
        raise
    execution['github_cleanup'] = result
    intake._save_execution(record, fields=['github_cleanup'])
    return result


def verify_configuration(configuration, *, workspace=None):
    verification = configuration.get('verification')
    if not isinstance(verification, dict) or set(verification) != {'python', 'runner', 'node', 'python_sha256', 'runner_sha256', 'node_sha256'}:
        raise ManagementError('configuration_missing', 'The fixed shared test entry is required.')
    for name in ('python', 'runner', 'node'):
        path = Path(verification[name])
        if (not path.is_absolute() or str(path.resolve(strict=True)) != str(path)
            or workspace is not None and path.is_relative_to(Path(workspace).resolve(strict=True))
            or name == 'runner' and hashlib.sha256(path.read_bytes()).hexdigest() != verification[name + '_sha256']):
            raise ManagementError('configuration_missing', 'An original fixed shared test executable changed.')
    if verification['runner_sha256'] != hashlib.sha256(Path(__file__).with_name('repository_test_runner.py').read_bytes()).hexdigest():
        raise ManagementError('configuration_missing', 'The shared test entry must be this reviewed original runner.')
    for name in ('python', 'node'):
        _trusted_binary(verification[name], workspace or configuration.get('workspace'), name)
    return verification


def verification_command(configuration, execution, workspace, test_files):
    verification = verify_configuration(configuration, workspace=workspace)
    if (not isinstance(test_files, list) or not test_files or len(test_files) != len(set(test_files))
        or any(not isinstance(path, str) or path.startswith('-') or Path(path).is_absolute()
            or not (Path(workspace) / path).resolve(strict=True).is_relative_to(Path(workspace))
            or not (Path(workspace) / path).is_file() for path in test_files)):
        raise ManagementError('test_evidence_missing', 'Actual explicit in-repository test files are required.')
    work = execution['verification']
    return shlex.join([verification['python'], '-I', verification['runner'], workspace, work['artifact_root'],
        '--node', verification['node'], '--binding', execution['session_id'], execution['generation'], work['nonce'], '--', *test_files])


def prepare_shared_verification(intake, record, configuration):
    verification = verify_configuration(configuration, workspace=record['target']['repo_path'])
    execution = record['dsh_execution']
    if not execution.get('verification'):
        execution['verification'] = {'artifact_root': str(Path(tempfile.mkdtemp(prefix='hpm-shared-tests-', dir='/tmp')).resolve(strict=True)),
            'nonce': uuid.uuid4().hex}
        intake._save_execution(record, fields=['verification'])
    work = execution['verification']
    _private_state_directory(work['artifact_root'])
    if Path(work['artifact_root']).parent != Path('/tmp').resolve():
        raise ManagementError('binding_conflict', 'The original verification artifacts left native tmp.')
    return shlex.join([verification['python'], '-I', verification['runner'], record['target']['repo_path'], work['artifact_root'],
        '--node', verification['node'], '--binding', execution['session_id'], execution['generation'], work['nonce'], '--'])


def _prompt(intake, record):
    return ('Work only in this bound repository. Use the installed Matt skills to host requirement discussion, '
        'fine Issue breakdown, TDD, development and tests. Ask ordinary questions through ask_user_question; '
        'execution approvals use the native approval request. Never change other repositories or start a new DSH.\n'
        + 'Frozen Issue: ' + record['issue']['url'] + '\n' + record['issue']['title'] + '\n' + record['issue']['body']
        + '\nFor GitHub business and git remote operations explicitly prefix each invocation with `env '
          'GH_CONFIG_DIR=' + record['dsh_execution']['github_reference']['config_dir']
        + ' GH_TOKEN= GITHUB_TOKEN= GH_ENTERPRISE_TOKEN= GITHUB_ENTERPRISE_TOKEN=`. Switch gh to '
        + record['dsh_execution']['github_reference']['account'] + ', read gh api user login, and require this exact account. '
          'Use the same scoped env prefix for every operation; do not change the shared Host environment, print credentials, '
          'or copy them into repository files, reports or questions. '
          'Only access the bound repository ' + record['target']['repository'] + '. '
          'Do not merge the game PR. Develop code yourself, run actual tests through the original bash tool, '
          'then submit the fixed-source PR and synchronize the original local branch. '
          'After writing your actual Node test files, run the fixed trusted test entry through the original bash tool: '
        + record['dsh_execution'].get('verification_prefix', 'The fixed test entry must be prepared before dispatch.')
        + ' followed by the explicit relative test file paths; do not append commands, redirect output, or replace the entry. '
          'It uses native node:test events to prove actual test results; ordinary command success is insufficient. '
          'Report with HERMES_REPOSITORY_DELIVERY_JSON followed by one JSON object with source_commit, '
          'issue_updated_at, criteria [{text,test_call_ids}], test_files, fine_issue_urls, pr_url, '
          'sync_branches, leftovers. Reference exact successful native bash test call IDs. '
          'Outer Kanban acceptance belongs to the supervisor.')


def _take_over_gave_up_claim(intake, record, run_id, configuration, carrier):
    import psutil
    from hermes_cli import kanban_db as kb
    from hermes_cli.kanban_db_connect import connect_closing
    from .repository_acceptance import repository_source_state
    from .trusted_controller import controller_identity
    execution = record['dsh_execution']
    continuation = execution.get('continuation_control', {})
    previous = execution.get('supervisor_claim', {})
    if not previous or previous.get('run_id') == run_id:
        return False
    failure = execution.get('last_failure', {})
    if (execution.get('state') not in {'running', 'outcome_unknown'}
            or execution.get('first_input') != 'accepted'
            or execution.get('state') == 'outcome_unknown' and failure.get('code') != 'unauthorized'
            and (failure.get('code'), failure.get('message')) not in {
                ('binding_conflict', 'The original shared source changed before supervision takeover.'),
                ('outcome_unknown', 'The original worker input remains unconfirmed; no claim transfer.')}
            or continuation and continuation.get('status') != 'accepted'):
        raise ManagementError('outcome_unknown', 'The original worker input remains unconfirmed; no claim transfer.')
    identity = previous.get('identity', {})
    with connect_closing(board=intake.board) as connection:
        old = kb.get_run(connection, previous['run_id'])
    if (old is None or old.id != previous['run_id'] or old.task_id != record['card_id']
            or old.status != 'gave_up' or old.outcome != 'gave_up' or old.ended_at is None
            or old.worker_pid not in {None, identity.get('pid')} or not isinstance(old.error, str)
            or re.fullmatch(r'Iteration budget exhausted \(([1-9]\d*)/\1\) — task could not complete within the allowed iterations', old.error) is None):
        raise ManagementError('outcome_unknown', 'The original iteration-budget worker cannot be safely supervised.')
    try:
        if type(identity.get('pid')) is not int or identity['pid'] <= 0:
            raise ValueError
        seconds, micros = int(identity['start_sec']), int(identity['start_usec'])
        if seconds <= 0 or not 0 <= micros < 1000000 or not identity.get('executable'):
            raise ValueError
        # The existing kernel-birth guard also refuses access-unknown processes
        # and treats exec changes as the same live process generation.
        require_permission_initializer_end(previous, old, record['card_id'])
    except (KeyError, TypeError, ValueError, psutil.AccessDenied, OSError):
        raise ManagementError('outcome_unknown', 'The original iteration-budget worker identity is incomplete.') from None
    offset = previous.get('exit_log_offset')
    path = kb.worker_log_path(record['card_id'], board=intake.board)
    if type(offset) is not int or offset < 0 or offset > path.stat().st_size:
        raise ManagementError('outcome_unknown', 'The original iteration-budget log boundary changed.')
    exit_code = None
    with path.open('rb') as log:
        log.seek(offset)
        for line in log:
            receipt = re.fullmatch(rb'\[kanban-worker-exit\] rc=(-?\d+)\s*', line)
            if receipt:
                exit_code = receipt.group(1)
                break
    if exit_code != b'0':
        raise ManagementError('outcome_unknown', 'The original iteration-budget worker lacks its native exit receipt.')
    read_ledger(configuration, execution)
    source = repository_source_state(record['target'])
    receipts = shared_receipts(configuration, execution, '.tools.jsonl')
    owned_jobs = carrier.owned_jobs()
    if any(not isinstance(job, dict) or job.get('status') not in {'completed', 'killed', 'failed'} for job in owned_jobs):
        raise ManagementError('outcome_unknown', 'Owned shared jobs must be terminal before supervision takeover.')
    if not (_source_core(execution.get('baseline')) == _source_core(source)
            and _source_core(source) is not None and not receipts
            or _verified_source_chain(execution, execution.get('baseline'), receipts, source, owned_jobs)):
        raise ManagementError('binding_conflict', 'The original shared source changed before supervision takeover.')
    execution.update(state='running', supervisor_claim={'run_id': run_id, 'identity': controller_identity(),
        'exit_log_offset': path.stat().st_size})
    intake._save_execution(record, fields=['state', 'supervisor_claim'])
    return True


async def run_shared_work(ctx, intake, record, generation):
    from .repository_execution import execution_configuration
    from .repository_supervision import current_worker, native_call, owned_call, original_history, execution_event_references
    from .trusted_controller import controller_identity
    from .repository_acceptance import repository_source_state, accept_repository_work, finish_delivery_range
    from hermes_cli import kanban_db as kb
    run_id = current_worker(ctx, intake, record)
    configuration, _ = execution_configuration(record['target'])
    execution = record['dsh_execution']
    if configuration['reference_sha256'] != execution['reference_sha256']:
        raise ManagementError('configuration_missing', 'The admitted shared configuration changed.')
    continuation = execution.get('continuation_control', {})
    pending_continuation = continuation.get('status') == 'admitted' and continuation.get('dispatch_status') == 'accepted'
    if execution.get('stop_requested') or execution['state'] in {'budget_stopped', 'stopped'} or execution['state'] == 'execution_failed' and not pending_continuation:
        return {'status': execution['state'], 'next_action': 'kanban_block', 'block_kind': 'needs_input', 'delivered': False}
    descriptor = os.open(intake.lock_path, os.O_RDWR | os.O_NOFOLLOW)
    await asyncio.to_thread(fcntl.flock, descriptor, fcntl.LOCK_EX)
    carrier = None
    try:
        intake.require_active(generation)
        record = next(row for row in intake.snapshot()['work'] if row['id'] == record['id'])
        execution = record['dsh_execution']
        run_id = current_worker(ctx, intake, record)
        configuration, _ = execution_configuration(record['target'])
        if configuration['reference_sha256'] != execution['reference_sha256']:
            raise ManagementError('configuration_missing', 'The admitted shared configuration changed.')
        continuation = execution.get('continuation_control', {})
        pending_continuation = continuation.get('status') == 'admitted' and continuation.get('dispatch_status') == 'accepted'
        if execution.get('stop_requested') or execution['state'] in {'budget_stopped', 'stopped'} or execution['state'] == 'execution_failed' and not pending_continuation:
            return {'status': execution['state'], 'next_action': 'kanban_block', 'block_kind': 'needs_input', 'delivered': False}
        occupied = [row for row in intake.snapshot()['work'] if row['id'] != record['id']
            and row['target'].get('repo_path') == record['target']['repo_path']
            and row.get('dsh_execution', {}).get('state') not in {None, 'admitted', 'stopped', 'released'}]
        if occupied:
            raise ManagementError('repository_busy', 'Earlier work retains this repository.')
        if 'generation' not in execution:
            execution.update(mode='shared_native', state='startup_intent', generation=str(uuid.uuid4()),
                session_id='session-' + uuid.uuid4().hex, request_id='input-' + uuid.uuid4().hex)
            intake._save_execution(record)
        configuration.update(workspace=record['target']['repo_path'], session_id=execution['session_id'],
                             generation=execution['generation'])
        carrier = SharedDesktopTransport(configuration)
        if execution.get('native_identity') and execution['native_identity'] != carrier.native_identity:
            raise ManagementError('binding_conflict', 'Original shared Desktop generation changed.')
        execution['native_identity'] = carrier.native_identity
        if not execution.get('supervisor_claim'):
            execution['supervisor_claim'] = {'run_id': run_id, 'identity': controller_identity(),
                'exit_log_offset': kb.worker_log_path(record['card_id'], board=intake.board).stat().st_size}
        elif pending_continuation and execution['supervisor_claim']['run_id'] != run_id:
            from hermes_cli.kanban_db_connect import connect_closing
            previous = continuation['previous_claim']
            checkpoint = continuation if continuation.get('kind') in {'max_tokens', 'renew_window', 'resume_window'} else execution['maintenance_checkpoint']
            if (execution['supervisor_claim'] != previous or checkpoint['worker']['run_id'] != previous['run_id']
                    or checkpoint['worker']['identity'] != previous['identity'] or checkpoint['worker']['exit_code'] != 0):
                raise ManagementError('binding_conflict', 'The checkpointed continuation worker claim differs.')
            with connect_closing(board=intake.board) as connection:
                old_run = kb.get_run(connection, previous['run_id'])
            require_permission_initializer_end(previous, old_run, record['card_id'])
            execution['supervisor_claim'] = {'run_id': run_id, 'identity': controller_identity(),
                'exit_log_offset': kb.worker_log_path(record['card_id'], board=intake.board).stat().st_size}
            intake._save_execution(record, fields=['supervisor_claim'])
        elif (execution.get('owner_context_control', {}).get('status') == 'accepted'
                and execution['owner_context_control'].get('dispatch_status') == 'accepted'
                and execution['supervisor_claim'] == execution['owner_context_control'].get('previous_claim')
                and execution['supervisor_claim']['run_id'] != run_id):
            control = execution['owner_context_control']
            previous, worker = control['previous_claim'], control['worker']
            if (execution['supervisor_claim'] != previous or worker.get('run_id') != previous['run_id']
                    or worker.get('identity') != previous['identity'] or worker.get('exit_code') != 0):
                raise ManagementError('binding_conflict', 'The original owner-context worker claim differs.')
            from hermes_cli.kanban_db_connect import connect_closing
            with connect_closing(board=intake.board) as connection:
                old_run = kb.get_run(connection, previous['run_id'])
            require_permission_initializer_end(previous, old_run, record['card_id'])
            execution['supervisor_claim'] = {'run_id': run_id, 'identity': controller_identity(),
                'exit_log_offset': kb.worker_log_path(record['card_id'], board=intake.board).stat().st_size}
            intake._save_execution(record, fields=['supervisor_claim'])
        listing = await asyncio.to_thread(owned_call, carrier, 'session/list', {})
        existing = [row for row in listing['items'] if row['sessionId'] == execution['session_id']]
        if existing and (len(existing) != 1 or existing[0].get('cwd') != configuration['workspace']):
            raise ManagementError('binding_conflict', 'Shared Session registration differs.')
        workspace = await asyncio.to_thread(resolve_shared_workspace, intake, record, carrier)
        proof = continuation if continuation.get('kind') in {'max_tokens', 'renew_window', 'resume_window'} else execution.get('maintenance_checkpoint', {})
        if pending_continuation and {'workspace_id': workspace['workspaceId'], 'session_ids': workspace['sessionIds']} != proof['workspace']:
            raise ManagementError('binding_conflict', 'The admitted original Workspace account changed before dispatch.')
        if not existing:
            if execution.get('session_creation'):
                raise ManagementError('outcome_unknown', 'Original shared Session creation was not confirmed; no replacement.')
            execution['session_creation'] = 'intent'
            intake._save_execution(record)
            created = await asyncio.to_thread(owned_call, carrier, 'session/create',
                {'sessionId': execution['session_id'], 'workspaceId': workspace['workspaceId'], 'agentPreset': configuration['agent_preset']})
            if created != {'sessionId': execution['session_id'], 'agentPreset': configuration['agent_preset']}:
                raise ManagementError('binding_conflict', 'The created ordinary shared Session differs.')
        elif len(existing) != 1 or existing[0].get('cwd') != configuration['workspace']:
            raise ManagementError('binding_conflict', 'Shared Session registration differs.')
        snapshot_stream = carrier.stream('session/follow', {'request': {'address': {'kind': 'session',
            'sessionId': execution['session_id']}, 'maxMessages': 500}})
        try:
            snapshot = await asyncio.to_thread(next, snapshot_stream)
        finally:
            snapshot_stream.close()
        header = snapshot.get('header', {})
        if (header.get('id') != execution['session_id'] or header.get('cwd') != configuration['workspace']
            or snapshot.get('hasMore') is not False
            or snapshot.get('projections', {}).get('values', {}).get('agentPreset') != configuration['agent_preset']):
            raise ManagementError('binding_conflict', 'Original shared Session header or preset is unverified.')
        if execution.get('session_created_at', header.get('createdAt')) != header.get('createdAt'):
            raise ManagementError('binding_conflict', 'Original shared Session creation identity changed.')
        _take_over_gave_up_claim(intake, record, run_id, configuration, carrier)
        if pending_continuation:
            budget = read_ledger(configuration, execution)
            snapshot_matches = (hashlib.sha256(json.dumps(snapshot, sort_keys=True).encode()).hexdigest() == continuation['snapshot_sha256']
                if continuation.get('kind') in {'max_tokens', 'renew_window', 'resume_window'} else _maintenance_snapshot_matches(snapshot, execution['maintenance_checkpoint']))
            if continuation.get('kind') == 'max_tokens':
                _require_max_tokens_budget(configuration, budget['state'], budget.get('renewWindow'))
            elif continuation.get('kind') in {'renew_window', 'resume_window'}:
                _require_window_budget(configuration, budget)
            jobs = carrier.owned_jobs()
            if (not snapshot_matches
                    or hashlib.sha256(private_file(ledger_path(configuration, execution['session_id'])).read_bytes()).hexdigest()
                    != continuation['ledger_sha256']
                    or continuation.get('kind') not in {'max_tokens', 'renew_window', 'resume_window'} and budget['state'] != {'started_at_ms': None, 'requests': 0, 'reported_tokens': 0,
                                           'admissions': [], 'stop_reason': None}
                    or (any(job['status'] in {'running', 'stopping'} for job in jobs)
                        if continuation.get('kind') in {'max_tokens', 'renew_window', 'resume_window'} else bool(jobs))):
                raise ManagementError('binding_conflict', 'The admitted pre-model continuation changed before dispatch.')
        if not execution.get('first_input') and any(row.get('event', {}).get('type') in
                {'turn/start', 'user/message', 'assistant/message', 'tool/call'} for row in snapshot.get('records', [])):
            raise ManagementError('binding_conflict', 'The newly owned shared Session has unexpected work.')
        execution.update(session_creation='registered', session_created_at=header['createdAt'])
        await asyncio.to_thread(bind_shared_workspace, intake, record, carrier)
        if not execution.get('native_permission'):
            catalog = await asyncio.to_thread(owned_call, carrier, 'commands/list', {'agentId': execution['session_id']})
            if not isinstance(catalog, list) or not any(row.get('name') == 'permission' for row in catalog):
                raise ManagementError('configuration_missing', 'The original per-Session permission command is unavailable.')
            execution['native_permission'] = 'intent'
            execution['native_permission_cursor'] = snapshot['cursor']
            intake._save_execution(record)
            permission = await asyncio.to_thread(owned_call, carrier, 'commands/execute',
                {'agentId': execution['session_id'], 'line': '/permission workspace-write', 'submittedAttachments': []})
            events, projections = await asyncio.to_thread(original_history, carrier, execution['session_id'], configuration['workspace'])
            execution['native_permission_response'] = permission
            intake._save_execution(record)
            proof = confirm_shared_permission(permission, events, projections,
                before_cursor=execution['native_permission_cursor'])
            execution['native_permission_proof'] = proof
            execution['native_permission'] = 'workspace-write'
        elif execution['native_permission'] == 'intent':
            events, projections = await asyncio.to_thread(original_history, carrier, execution['session_id'], configuration['workspace'])
            proof = confirm_shared_permission(execution.get('native_permission_response'), events, projections,
                before_cursor=execution.get('native_permission_cursor'), reconcile=True)
            previous = execution['supervisor_claim']
            if previous['run_id'] != run_id:
                from hermes_cli.kanban_db_connect import connect_closing
                with connect_closing(board=intake.board) as connection:
                    old_run = kb.get_run(connection, previous['run_id'])
                require_permission_initializer_end(previous, old_run, record['card_id'])
                execution['permission_initialization_claim'] = previous
                execution['supervisor_claim'] = {'run_id': run_id, 'identity': controller_identity(),
                    'exit_log_offset': kb.worker_log_path(record['card_id'], board=intake.board).stat().st_size}
            execution['native_permission_proof'] = proof
            execution['native_permission'] = 'workspace-write'
        elif execution['native_permission'] != 'workspace-write':
            raise ManagementError('outcome_unknown', 'The shared Session permission transition is unconfirmed; no replay.')
        _write_initial_ledger(configuration, execution)
        execution['runtime_versions'] = configuration['runtime_versions']
        intake._save_execution(record)
        if not execution.get('native_title'):
            title = record['issue']['title']
            await asyncio.to_thread(owned_call, carrier, 'session/rename', {'sessionId': execution['session_id'], 'title': title})
            execution['native_title'] = title
            intake._save_execution(record)
    except Exception as error:
        if isinstance(error, ManagementError):
            execution['last_failure'] = {'code': error.code, 'message': str(error)}
            try:
                intake._save_execution(record, fields=['last_failure'])
            except ManagementError:
                pass
        if carrier is not None:
            carrier.close()
        raise
    finally:
        fcntl.flock(descriptor, fcntl.LOCK_UN)
        os.close(descriptor)
    try:
        window_start = None
        if continuation.get('kind') in {'renew_window', 'resume_window'}:
            budget = read_ledger(configuration, execution)
            window_start = budget['renewWindow']['started_at_ms']
            if (not pending_continuation and execution.get('first_input') == 'accepted'
                    and continuation.get('status') == 'accepted' and type(window_start) is int
                    and time.time() * 1000 >= window_start + configuration['budget']['max_wall_seconds'] * 1000):
                _require_expired_window_budget(configuration, budget)
                remaining = (window_start + configuration['budget']['max_wall_seconds'] * 1000 - time.time() * 1000) / 1000
            else:
                remaining = _require_window_budget(configuration, budget)
            deadline = time.monotonic() + remaining + 15
        if continuation.get('kind') == 'max_tokens':
            budget = read_ledger(configuration, execution)
            started_at_ms = (budget.get('renewWindow') or budget['state'])['started_at_ms']
            deadline = time.monotonic() + (started_at_ms + configuration['budget']['max_wall_seconds'] * 1000 - time.time() * 1000) / 1000 + 15
        if not execution.get('first_input') or pending_continuation:
            observer_deadline = time.monotonic() + 15
            while time.monotonic() < observer_deadline:
                current = next(row for row in intake.snapshot()['work'] if row['id'] == record['id'])['dsh_execution']
                observer = current.get('shared_event_observer', {})
                if (observer.get('status') == 'ready' and observer.get('session_id') == execution['session_id'] and observer.get('generation') == execution['generation']
                    and observer.get('host_lease') == configuration['host_lease'] and observer.get('client_id')):
                    break
                intake.require_active(generation)
                current_worker(ctx, intake, record)
                await asyncio.sleep(.1)
            else:
                raise ManagementError('outcome_unknown', 'The original gateway human observer is not ready; no model input sent.')
            if pending_continuation:
                if continuation.get('kind') == 'max_tokens':
                    budget = read_ledger(configuration, execution)
                    _require_max_tokens_budget(configuration, budget['state'], budget.get('renewWindow'))
                elif continuation.get('kind') in {'renew_window', 'resume_window'}:
                    _require_window_budget(configuration, read_ledger(configuration, execution))
                continuation['status'] = 'input_intent'
                intake._save_execution(record, fields=['continuation_control'])
                request_id = execution['continuation_request_id']
                reason = ('its max-tokens turn boundary' if continuation.get('kind') == 'max_tokens'
                    else 'its pre-model failure and verified maintenance restart')
                prompt = ('Continue the original accepted task in this Session after ' + reason + '. Keep the original scope, frozen Issue, rules and remaining '
                    'budget. Resume that task in this original Session.\n\n' + _prompt(intake, record))
                if continuation.get('kind') == 'resume_window':
                    prompt = ('Continue the original accepted task in this same Session after verified normal maintenance. '
                        'The original finite execution window is still active: preserve its existing start time, request base, '
                        'cumulative usage and remaining allowance. Do not create or extend any budget window.\n\n' + _prompt(intake, record))
                elif read_ledger(configuration, execution).get('renewWindow'):
                    prompt = ('The owner explicitly authorized one new finite execution window in this same Session. '
                        'Its wall limit is ' + str(configuration['budget']['max_wall_seconds']) + ' seconds and its request limit is '
                        + str(configuration['budget']['max_model_requests']) + '. It starts at the first actual native request. '
                        'These current instructions override all old budget and token quota text in the frozen Issue: no plugin output-token or cumulative-token quota. '
                        'Native model limits still apply. Preserve the original game scope, history and cumulative usage.\n\n' + _prompt(intake, record))
            else:
                await asyncio.to_thread(prepare_shared_github, intake, record)
                execution['verification_prefix'] = prepare_shared_verification(intake, record, configuration)
                intake._save_execution(record, fields=['verification_prefix'])
                execution['baseline'] = await asyncio.to_thread(repository_source_state, record['target'])
                execution.update(first_input='intent', state='input_intent')
                intake._save_execution(record)
                request_id, prompt = execution['request_id'], _prompt(intake, record)
            answer = await asyncio.to_thread(owned_call, carrier, 'session/prompt',
                {'sessionId': execution['session_id'], 'requestId': request_id, 'mode': 'queue',
                 'content': [{'type': 'text', 'text': prompt}]})
            if not isinstance(answer, dict) or answer.get('accepted') is not True:
                raise ManagementError('outcome_unknown', 'Original shared input admission was not confirmed.')
            if pending_continuation:
                continuation['status'] = 'accepted'
            execution.update(first_input='accepted', state='running')
            intake._save_execution(record)
        elif execution['first_input'] != 'accepted' or (continuation and continuation.get('status') != 'accepted'):
            raise ManagementError('outcome_unknown', 'Original shared input needs reconciliation; no replay.')
        heartbeat_at = 0
        if continuation.get('kind') not in {'max_tokens', 'renew_window', 'resume_window'}:
            budget = read_ledger(configuration, execution)
            started_at_ms = budget['state']['started_at_ms']
            remaining = (configuration['budget']['max_wall_seconds'] if started_at_ms is None else
                (started_at_ms + configuration['budget']['max_wall_seconds'] * 1000 - time.time() * 1000) / 1000)
            deadline = time.monotonic() + remaining + 15
        observation_deadline = min(deadline, time.monotonic() + SUPERVISE_OBSERVATION_SLICE_SECONDS)
        observed = False
        while not observed or time.monotonic() < observation_deadline:
            observed = True
            intake.require_active(generation)
            current_worker(ctx, intake, record)
            current = next(row for row in intake.snapshot()['work'] if row['id'] == record['id'])['dsh_execution']
            if current.get('stop_requested'):
                return {'status': 'stop_requested', 'next_action': 'kanban_block', 'block_kind': 'needs_input', 'delivered': False}
            if time.monotonic() - heartbeat_at >= 30:
                native_call(ctx, 'kanban_heartbeat', {'task_id': record['card_id'], 'board': intake.board})
                heartbeat_at = time.monotonic()
            listing = await asyncio.to_thread(owned_call, carrier, 'session/list', {})
            row = next((row for row in listing['items'] if row['sessionId'] == execution['session_id']), None)
            events, projections = await asyncio.to_thread(original_history, carrier, execution['session_id'], configuration['workspace'])
            budget = read_ledger(configuration, execution)
            active_budget = budget.get('renewWindow') or budget['state']
            if active_budget['started_at_ms'] is not None:
                deadline = time.monotonic() + (active_budget['started_at_ms'] + configuration['budget']['max_wall_seconds'] * 1000 - time.time() * 1000) / 1000 + 15
                observation_deadline = min(observation_deadline, deadline)
            if continuation.get('kind') in {'renew_window', 'resume_window'} and window_start is None and budget['renewWindow']['started_at_ms'] is not None:
                window_start = budget['renewWindow']['started_at_ms']
                deadline = time.monotonic() + (window_start + configuration['budget']['max_wall_seconds'] * 1000 - time.time() * 1000) / 1000 + 15
            execution.update(journal_cursor=events[-1]['seq'] if events else -1, usage=projections.get('tokenUsage'),
                observed_events=execution_event_references(events), shared_budget=budget['state'])
            intake._save_execution(record)
            cursor = (continuation['cursor'] if continuation.get('kind') in {'max_tokens', 'renew_window', 'resume_window'}
                else execution['maintenance_checkpoint']['cursor']) if continuation else -1
            if execution.get('owner_context_control', {}).get('status') == 'accepted':
                cursor = execution['owner_context_control']['cursor']
                submitted = {execution.get('request_id'), execution.get('continuation_request_id'),
                    execution['owner_context_control'].get('request_id')}
                submitted.update(control.get('request_id') for control in execution.get('owner_context_history', []))
                submitted.update(question.get('request_id') for question in execution.get('questions', []))
                submitted.discard(None)
                if any(item.get('source', {}).get('kind') != 'user' or item.get('source', {}).get('rpcId') not in submitted
                        for event in events if event['type'] == 'agent/inbox/spliced' and event['seq'] > continuation['cursor']
                        for item in event['data'].get('inserted', []) if item.get('role') == 'user'):
                    raise ManagementError('outcome_unknown', 'Foreign input in the original owner-context history was preserved.')
            starts = [event for event in events if event['type'] == 'turn/start' and event['seq'] > cursor]
            ends = [event for event in events if event['type'] == 'turn/end' and event['seq'] > cursor]
            if row and row.get('running') is False and starts and ends and ends[-1]['seq'] > starts[-1]['seq']:
                if budget.get('renewWindow') and budget['renewWindow'].get('hook_marker') != 'same-sid-window-v1':
                    raise ManagementError('outcome_unknown', 'The actual new native beforeRequest Hook revision is unconfirmed.')
                if projections.get('inbox') != {'next-turn': [], 'next-step': []}:
                    raise ManagementError('outcome_unknown', 'The shared Session retains pending input.')
                jobs = await asyncio.to_thread(carrier.owned_jobs)
                if any(job['status'] in {'running', 'stopping'} for job in jobs):
                    raise ManagementError('outcome_unknown', 'Owned shared background jobs remain active.')
                reason = ends[-1]['data'].get('reason', {})
                stop_reason = (budget.get('renewWindow') or execution['shared_budget']).get('stop_reason')
                state = 'awaiting_acceptance' if reason.get('kind') == 'completed' else 'budget_stopped' if stop_reason else 'execution_failed'
                execution.update(state=state, terminal_reason=reason, jobs=[{'id': job['id'], 'status': job['status']} for job in jobs],
                    tool_receipts=shared_receipts(configuration, execution, '.tools.jsonl'))
                intake._save_execution(record)
                if state == 'awaiting_acceptance':
                    result = await accept_repository_work(intake, record, carrier, events, projections, generation)
                    if result.get('status') == 'accepted':
                        await finish_delivery_range(intake, record, carrier, generation)
                        await asyncio.to_thread(cleanup_shared_github, intake, record)
                    return result
                return {'status': state, 'next_action': 'kanban_block', 'block_kind': 'needs_input', 'delivered': False}
            await asyncio.sleep(.25)
        if time.monotonic() < deadline:
            return {'status': 'running', 'next_action': 'hermes_pm_supervise', 'delivered': False}
        raise ManagementError('outcome_unknown', 'Original shared execution has no confirmed terminal boundary.')
    except (ManagementError, OSError, TimeoutError, ValueError, KeyError) as error:
        execution.update(state='outcome_unknown', last_failure={'code': getattr(error, 'code', 'outcome_unknown'),
            'message': 'Original shared execution requires reconciliation (' + type(error).__name__ + ').'})
        intake._save_execution(record)
        if isinstance(error, ManagementError):
            raise
        raise ManagementError('outcome_unknown', 'Original shared execution remains unconfirmed.') from None
    finally:
        if carrier is not None:
            carrier.close()


async def stop_shared_work(intake, record, generation):
    from .repository_supervision import owned_call, original_history
    from .repository_control import _card, _worker_end
    from .repository_acceptance import repository_source_state
    execution = record['dsh_execution']
    if execution['state'] == 'stopped':
        return record
    card = _card(intake, record)
    if card['status'] == 'ready':
        intake._kanban('kanban_block', {'task_id': record['card_id'], 'kind': 'needs_input',
            'reason': 'Owner requested exact shared Session termination; repository remains occupied until verified.'})
    control = execution.get('stop_control', {})
    deadline = time.monotonic() + 30
    carrier = None
    try:
        if execution.get('native_identity'):
            carrier = shared_attach(intake, record, observe=False)
            while time.monotonic() < deadline:
                intake.require_active(generation)
                listing = await asyncio.to_thread(owned_call, carrier, 'session/list', {})
                rows = [row for row in listing['items'] if row['sessionId'] == execution['session_id']]
                if (len(rows) != 1 or rows[0].get('cwd') != record['target']['repo_path']
                    or rows[0].get('agentAvailable') is not True):
                    raise ManagementError('outcome_unknown', 'The exact shared Session is unavailable.')
                if rows[0].get('running') is True and not control.get('cancel_status'):
                    control['cancel_status'] = 'intent'
                    execution['stop_control'] = control
                    intake._save_execution(record, fields=['stop_control'])
                    answer = await asyncio.to_thread(owned_call, carrier, 'session/cancel', {'sessionId': execution['session_id']})
                    if answer.get('accepted') is not True:
                        raise ManagementError('outcome_unknown', 'The original exact shared cancel was not confirmed.')
                    control['cancel_status'] = 'accepted'
                    intake._save_execution(record, fields=['stop_control'])
                events, projections = await asyncio.to_thread(original_history, carrier, execution['session_id'], record['target']['repo_path'])
                inbox = projections.get('inbox')
                if not isinstance(inbox, dict) or set(inbox) != {'next-turn', 'next-step'}:
                    raise ManagementError('outcome_unknown', 'Shared pending input coverage is unavailable.')
                submitted = {execution.get('request_id'), execution.get('continuation_request_id'),
                    execution.get('owner_context_control', {}).get('request_id')}
                submitted.update(control.get('request_id') for control in execution.get('owner_context_history', []))
                submitted.update(question.get('request_id') for question in execution.get('questions', []))
                submitted.discard(None)
                for item in [*inbox['next-turn'], *inbox['next-step']]:
                    source = item.get('source', {})
                    if source.get('kind') != 'user' or source.get('rpcId') not in submitted:
                        raise ManagementError('outcome_unknown', 'Foreign input in the shared Session was preserved.')
                    removed = control.setdefault('queue_remove', {})
                    if removed.get(item['id']) == 'intent':
                        raise ManagementError('outcome_unknown', 'Owned queued input removal was not confirmed; no replay.')
                    if item['id'] not in removed:
                        removed[item['id']] = 'intent'
                        intake._save_execution(record, fields=['stop_control'])
                        answer = await asyncio.to_thread(owned_call, carrier, 'session/updateQueue',
                            {'sessionId': execution['session_id'], 'itemId': item['id'], 'action': {'kind': 'remove'}})
                        if answer.get('accepted') is not True:
                            raise ManagementError('outcome_unknown', 'Owned shared pending input removal was not confirmed.')
                        removed[item['id']] = 'accepted'
                        intake._save_execution(record, fields=['stop_control'])
                jobs = await asyncio.to_thread(carrier.owned_jobs)
                registered = {row['job_id'] for row in shared_receipts(carrier.config, execution, '.jobs.jsonl')
                              if row['type'] == 'registered' and row.get('owner') == execution['session_id']}
                for job in jobs:
                    if job['id'] not in registered:
                        raise ManagementError('coverage_unverified', 'An unregistered shared job was preserved.')
                    if job['status'] not in {'running', 'stopping'}:
                        continue
                    cancelled = control.setdefault('job_cancel', {})
                    if cancelled.get(job['id']) == 'intent':
                        raise ManagementError('outcome_unknown', 'The original shared job cancel was not confirmed; no replay.')
                    if job['id'] not in cancelled:
                        cancelled[job['id']] = 'intent'
                        intake._save_execution(record, fields=['stop_control'])
                        answer = await asyncio.to_thread(owned_call, carrier, 'job/kill',
                            {'sessionId': execution['session_id'], 'jobId': job['id']})
                        if answer.get('outcome') not in {'requested', 'already-finished'}:
                            raise ManagementError('outcome_unknown', 'Owned shared job cancel admission is unknown.')
                        cancelled[job['id']] = 'accepted'
                        intake._save_execution(record, fields=['stop_control'])
                starts = [event for event in events if event['type'] == 'turn/start']
                ends = [event for event in events if event['type'] == 'turn/end']
                if (rows[0].get('running') is False and not any(inbox.values())
                    and (not starts or ends and ends[-1]['seq'] > starts[-1]['seq'])
                    and not any(job['status'] in {'running', 'stopping'} for job in jobs)):
                    control['session_end'] = {'session_id': execution['session_id'], 'generation': execution['generation'],
                        'journal_cursor': events[-1]['seq'] if events else -1,
                        'tool_calls': [{'id': event['data']['callId'], 'name': event['data']['name']}
                                       for event in events if event['type'] == 'tool/call'],
                        'jobs': [{'id': job['id'], 'status': job['status']} for job in jobs]}
                    execution['stop_control'] = control
                    intake._save_execution(record, fields=['stop_control'])
                    proof = await asyncio.to_thread(shared_range_proof, intake, record, control['session_end'])
                    break
                await asyncio.sleep(.05)
            else:
                raise ManagementError('outcome_unknown', 'The exact shared Session or owned jobs remain unconfirmed.')
        elif execution.get('generation'):
            raise ManagementError('outcome_unknown', 'Shared Session startup remains unconfirmed.')
        else:
            proof = {'scope': 'not_started', 'host_shutdown': False}
        while time.monotonic() < deadline:
            intake.require_active(generation)
            try:
                worker = await asyncio.to_thread(_worker_end, intake, record)
                break
            except ManagementError as error:
                if error.code != 'outcome_unknown':
                    raise
            await asyncio.sleep(.05)
        else:
            raise ManagementError('outcome_unknown', 'The original outer worker exit is unconfirmed.')
        source = await asyncio.to_thread(repository_source_state, record['target'])
        await asyncio.to_thread(cleanup_shared_github, intake, record)
        control.update(status='stopped', proof={'session': proof, 'worker': worker, 'repository': source})
        execution.update(state='stopped', stop_control=control)
        intake._save_execution(record, fields=['state', 'stop_control'])
        return record
    finally:
        if carrier is not None:
            carrier.close()
