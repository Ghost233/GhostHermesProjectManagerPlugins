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
CAPS = {'max_wall_seconds', 'max_model_requests'}
SUPERVISE_OBSERVATION_SLICE_SECONDS = 300
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
    if (set(configuration) not in (required, required | {'supervision'}) or configuration['schema'] != 1
        or configuration['approved'] is not True or configuration['mode'] != 'shared_native'):
        raise ManagementError('configuration_missing', 'An approved shared native execution reference is required.')
    for key in ('host_lease', 'service_ref', 'agent_preset', 'provider', 'model'):
        if not isinstance(configuration[key], str) or not configuration[key]:
            raise ManagementError('configuration_missing', 'Shared execution identities are incomplete.')
    budget = configuration['budget']
    if (not isinstance(budget, dict) or not set(budget) <= CAPS
        or any(type(value) is not int or not 0 < value <= 2**53 - 1 for value in budget.values())):
        raise ManagementError('configuration_missing', 'Shared hard limits must be explicit positive integers.')
    supervision = configuration.get('supervision', {'interval_seconds': 300, 'request_interval': 10})
    if (not isinstance(supervision, dict) or set(supervision) != {'interval_seconds', 'request_interval'}
        or any(type(value) is not int or value <= 0 for value in supervision.values())):
        raise ManagementError('configuration_missing', 'Shared supervision checkpoints require positive intervals.')
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
            or not isinstance(budget, dict) or not set(budget) <= CAPS | legacy_tokens
            or {key: value for key, value in budget.items() if key in CAPS} != configuration['budget']
            or any(type(value) is not int or value <= 0 for value in budget.values())
            or any(type(budget[key]) is not int or budget[key] <= 0 for key in legacy_tokens & set(budget))):
        raise ManagementError('binding_conflict', 'The original shared Session ledger changed.')
    if 'renewWindow' in row:
        _validate_renew_window(row['renewWindow'])
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
            or _wall_remaining(configuration, state['started_at_ms']) <= 0
            or type(state.get('requests')) is not int or not 0 < state['requests']
            or state['requests'] >= configuration['budget'].get('max_model_requests', float('inf'))
            or type(state.get('reported_tokens')) is not int
            or state['reported_tokens'] < 0
            or not isinstance(state.get('admissions'), list) or len(state['admissions']) != state['requests']
            or any(not isinstance(value, str) or not value for value in state['admissions'])
            or len(set(state['admissions'])) != state['requests']):
        raise ManagementError('outcome_unknown', 'The original shared budget has no confirmed remaining allowance.')
    return _wall_remaining(configuration, state['started_at_ms'])


def _wall_remaining(configuration, started):
    limit = configuration['budget'].get('max_wall_seconds')
    if limit is None:
        return float('inf')
    return limit if started is None else limit - (time.time() * 1000 - started) / 1000


def _active_budget_snapshot(configuration, budget):
    active = budget.get('renewWindow') or budget['state']
    limits = {key: configuration['budget'][key] for key in ('max_wall_seconds', 'max_model_requests')
        if key in configuration['budget']}
    request_base = (budget.get('renewWindow') or {}).get('request_base', 0)
    used = budget['state']['requests'] - request_base
    return {'limits': limits, 'request_base': request_base, 'requests_total': budget['state']['requests'],
        'requests_used': used,
        'requests_remaining': limits['max_model_requests'] - used if 'max_model_requests' in limits else None,
        'wall_seconds_remaining': _wall_remaining(configuration, active.get('started_at_ms')) if 'max_wall_seconds' in limits else None}


def supervision_checkpoint(intake, record, configuration, budget, events=()):
    from .manager import _public_text
    from .repository_acceptance import _human_pending
    execution = record['dsh_execution']
    latest = next(row for row in intake.snapshot()['work'] if row['id'] == record['id'])['dsh_execution']
    prior = latest.get('supervision_checkpoint', {})
    if prior.get('status') == 'pending':
        return prior
    if prior.get('delivery', {}).get('status') in {'pending', 'intent'}:
        return None
    active = budget.get('renewWindow') or budget['state']
    started = active.get('started_at_ms')
    if started is None:
        return None
    now = time.time()
    policy = configuration.get('supervision', {'interval_seconds': 300, 'request_interval': 10})
    baseline_time = prior.get('observed_at', started / 1000)
    baseline_requests = prior.get('requests', budget.get('renewWindow', {}).get('request_base', 0))
    reasons = []
    if now - baseline_time >= policy['interval_seconds']:
        reasons.append('time')
    if budget['state']['requests'] - baseline_requests >= policy['request_interval']:
        reasons.append('requests')
    if not reasons:
        return None
    starts = [event for event in events if event['type'] == 'turn/start']
    current_turn = {'turn': starts[-1]['data'].get('turn'), 'start_seq': starts[-1]['seq']} if starts else None
    checkpoint = {'id': uuid.uuid4().hex, 'status': 'pending', 'observed_at': now,
        'session_id': execution['session_id'], 'generation': execution['generation'],
        'requests': budget['state']['requests'], 'request_delta': budget['state']['requests'] - baseline_requests,
        'reported_tokens': budget['state']['reported_tokens'], 'trigger': reasons,
        'observed_state': execution['state'], 'journal_cursor': execution.get('journal_cursor'),
        'questions_pending': sum(_human_pending({'questions': [q]}) for q in latest.get('questions', [])),
        'approvals_pending': sum(_human_pending({'approvals': [a]}) for a in latest.get('approvals', [])),
        'budget': _active_budget_snapshot(configuration, budget),
        'current_turn': current_turn, 'acceptance_status': latest.get('acceptance', {}).get('status', 'unverified')
            if execution['state'] == 'awaiting_acceptance' else 'unverified'}
    messages = [event for event in events if event['type'] == 'assistant/message']
    if messages:
        text = '\n'.join(part.get('text', '') for part in messages[-1]['data'].get('message', {}).get('content', [])
            if part.get('type') == 'text')[:2000]
        try:
            _public_text(text, intake.secret_values)
            checkpoint['latest_progress'] = {'seq': messages[-1]['seq'], 'text': text}
        except ManagementError:
            checkpoint['latest_progress'] = {'seq': messages[-1]['seq'], 'text': 'Progress details remain in the original private Session.'}
        checkpoint['latest_progress']['from_current_turn'] = bool(starts and messages[-1]['seq'] > starts[-1]['seq'])
    checkpoint['recent_tool_results'] = [{'seq': event['seq'], 'is_error': event['data'].get('message', {}).get('isError')}
        for event in events if event['type'] == 'tool/result'][-10:]
    if prior:
        execution['supervision_history'] = deepcopy(latest.get('supervision_history', [])) + [deepcopy(prior)]
    execution['supervision_checkpoint'] = checkpoint
    intake._save_execution(record, fields=['supervision_checkpoint', *(['supervision_history'] if prior else [])])
    return checkpoint


def _window_fingerprint(row):
    binding = {key: row[key] for key in ('schema', 'service_ref', 'host_lease', 'agent_preset', 'session_id',
        'session_created_at', 'generation', 'workspace', 'provider', 'model')}
    binding['budget'] = {key: row['budget'][key] for key in ('max_wall_seconds', 'max_model_requests', 'max_reported_tokens', 'max_output_tokens_per_request') if key in row['budget']}
    binding['renewWindow'] = {key: value for key, value in row['renewWindow'].items() if key not in {'stop_reason', 'hook_marker', 'started_at_ms'}}
    if 'sealed_unknown_usage' in row['renewWindow']:
        binding['renewWindow']['sealed_unknown_usage'] = row['renewWindow']['sealed_unknown_usage']
    return hashlib.sha256(json.dumps(binding, separators=(',', ':'), ensure_ascii=False).encode()).hexdigest()


def _require_window_budget(configuration, row):
    if 'renewWindow' not in row:
        return _require_max_tokens_budget(configuration, row['state'])
    window, state = row['renewWindow'], row['state']
    original = window['original_state']
    used = state['requests'] - window['request_base']
    if (window['stop_reason'] is not None or not 0 <= used < configuration['budget'].get('max_model_requests', float('inf'))
            or state['started_at_ms'] != original['started_at_ms'] or state['stop_reason'] != original['stop_reason']
            or state['reported_tokens'] < original['reported_tokens']
            or state['admissions'][:window['request_base']] != original['admissions']):
        raise ManagementError('outcome_unknown', 'The explicit new window is stopped or has no request remainder.')
    started = window['started_at_ms']
    remaining = _wall_remaining(configuration, started)
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


def _pending_qa_rework_retry(execution):
    control = execution.get('continuation_control', {})
    retry = execution.get('qa_preinput_retry')
    return (execution.get('state') == 'outcome_unknown' and control.get('kind') == 'qa_rework'
            and control.get('status') == 'admitted' and control.get('dispatch_status') == 'accepted'
            and (not retry or retry.get('status') == 'accepted'))


def _qa_rework_acceptance(execution):
    acceptance = execution.get('acceptance', {})
    unmet = acceptance.get('unmet', [])
    return ((execution.get('state') == 'awaiting_acceptance' or _pending_qa_rework_retry(execution))
            and execution.get('terminal_reason') == {'kind': 'completed'}
            and acceptance.get('status') == 'unmet' and acceptance.get('delivered') is False
            and isinstance(unmet, list) and ('delivery_report_missing' in unmet
            or isinstance(acceptance.get('source_commit'), str) and acceptance.get('source_commit')
            and isinstance(acceptance.get('source_digest'), str) and acceptance.get('source_digest')
            and isinstance(acceptance.get('workspace_status'), str)))


def _sealed_unknown_usage(events, previous_window):
    if (not isinstance(previous_window, dict)
            or previous_window.get('original_state', {}).get('stop_reason') != 'shared-work-wall-budget'
            or type(previous_window.get('request_base')) is not int or previous_window['request_base'] <= 0
            or not isinstance(previous_window.get('id'), str) or not previous_window['id']):
        raise ManagementError('outcome_unknown', 'The prior wall-stopped renewal window has no sealed usage boundary.')
    settlements = [event for event in events if event.get('type') in {'assistant/message', 'assistant/attempt'}]
    matches = []
    for index in range(len(events) - 2):
        assistant, step_end, turn_end = events[index:index + 3]
        assistant_data = assistant.get('data', {})
        stream = assistant_data.get('stream', [])
        has_usage = 'usage' in assistant_data or any(
            isinstance(item, dict) and isinstance(item.get('chunk'), dict) and 'usage' in item['chunk']
            for item in stream)
        reason = turn_end.get('data', {}).get('reason')
        turn, step = assistant_data.get('turn'), assistant_data.get('step')
        if (assistant.get('type') == 'assistant/message' and not has_usage
                and step_end.get('type') == 'step/end'
                and step_end.get('data') == {'turn': turn, 'step': step}
                and turn_end.get('data') == {'turn': turn,
                    'reason': {'kind': 'aborted', 'reason': {'kind': 'hook', 'reason': 'shared-work-wall-budget'}}}):
            matches.append((index, [assistant, step_end, turn_end]))
    if len(matches) != 1:
        raise ManagementError('outcome_unknown', 'The exact interrupted no-usage wall-stop triplet is unconfirmed.')
    ordinal = settlements.index(matches[0][1][0]) + 1
    if ordinal != previous_window['request_base']:
        raise ManagementError('outcome_unknown', 'The interrupted usage boundary does not match the prior request base.')
    triplet = matches[0][1]
    return {'ordinal': ordinal,
        'event_sha256': hashlib.sha256(json.dumps(triplet, ensure_ascii=False, separators=(',', ':')).encode()).hexdigest(),
        'prior_window_id': previous_window.get('id')}


def _validate_renew_window(window):
    if not isinstance(window, dict):
        raise ManagementError('binding_conflict', 'The renewed window shape is malformed.')
    required = {'id', 'message_id', 'host_lease', 'request_base', 'original_state', 'original_host_lease',
        'started_at_ms', 'stop_reason', 'hook_marker'}
    if set(window) not in (required, required | {'sealed_unknown_usage'}):
        raise ManagementError('binding_conflict', 'The renewed window shape is malformed.')
    if (any(not isinstance(window.get(key), str) or not window[key]
            for key in ('id', 'message_id', 'host_lease', 'original_host_lease'))
            or type(window.get('request_base')) is not int or window['request_base'] <= 0
            or window.get('started_at_ms') is not None and type(window['started_at_ms']) is not int
            or window.get('stop_reason') is not None and not isinstance(window['stop_reason'], str)
            or window.get('hook_marker') is not None and not isinstance(window['hook_marker'], str)):
        raise ManagementError('binding_conflict', 'The renewed window shape is malformed.')
    original = window.get('original_state')
    state_keys = {'started_at_ms', 'requests', 'reported_tokens', 'admissions', 'stop_reason'}
    metadata_keys = {'binding_sha256', 'observed_at_ms', 'model_context_window', 'effective_max_output_tokens', 'stop_cleanup'}
    if (not isinstance(original, dict) or not state_keys <= set(original)
            or set(original) - state_keys - metadata_keys
            or original.get('started_at_ms') is not None and type(original['started_at_ms']) is not int
            or type(original.get('requests')) is not int or original['requests'] < 0
            or type(original.get('reported_tokens')) is not int or original['reported_tokens'] < 0
            or not isinstance(original.get('admissions'), list)
            or any(not isinstance(value, str) or not value for value in original['admissions'])
            or len(original['admissions']) != original['requests']
            or original.get('stop_reason') is not None and not isinstance(original['stop_reason'], str)):
        raise ManagementError('binding_conflict', 'The renewed window original state is malformed.')
    if ('binding_sha256' in original and (not isinstance(original['binding_sha256'], str)
            or not re.fullmatch(r'[a-f0-9]{64}', original['binding_sha256']))
            or 'observed_at_ms' in original and (type(original['observed_at_ms']) is not int or original['observed_at_ms'] <= 0)
            or 'model_context_window' in original and (type(original['model_context_window']) is not int
                or original['model_context_window'] <= 0)
            or 'effective_max_output_tokens' in original and (type(original['effective_max_output_tokens']) is not int
                or original['effective_max_output_tokens'] <= 0)):
        raise ManagementError('binding_conflict', 'The renewed window metadata is malformed.')
    if 'stop_cleanup' in original:
        cleanup = original['stop_cleanup']
        if cleanup == {'kind': 'unknown'}:
            pass
        elif (not isinstance(cleanup, dict) or cleanup.get('kind') != 'terminal'
              or set(cleanup) != {'kind', 'job_ids'} or not isinstance(cleanup.get('job_ids'), list)
              or any(not isinstance(job_id, str) or not job_id for job_id in cleanup['job_ids'])):
            raise ManagementError('binding_conflict', 'The renewed window stop cleanup metadata is malformed.')
    sealed = window.get('sealed_unknown_usage')
    if sealed is None:
        return
    if (not isinstance(sealed, dict) or set(sealed) != {'ordinal', 'event_sha256', 'prior_window_id'}
            or type(sealed.get('ordinal')) is not int or sealed['ordinal'] <= 0
            or not isinstance(sealed.get('event_sha256'), str) or not re.fullmatch(r'[a-f0-9]{64}', sealed['event_sha256'])
            or not isinstance(sealed.get('prior_window_id'), str) or not sealed['prior_window_id']
            or sealed['prior_window_id'] == window.get('id')
            or sealed['ordinal'] >= window.get('request_base', 0)):
        raise ManagementError('binding_conflict', 'The sealed historical unknown-usage boundary is malformed.')


def _qa_rework_prompt(intake, record, feedback, *, test_files=None):
    acceptance = record['dsh_execution'].get('acceptance', {})
    unmet = '、'.join(acceptance.get('unmet', [])) or '当前 QA 证据'
    return ('This is browser acceptance rework for the same original work in this existing Session. '
            'Read the latest comments on the frozen original Issue URL before acting: ' + record['issue']['url'] + '. '
            'Fix only the current unmet acceptance/QA evidence: ' + unmet + '. Public owner feedback: ' + feedback + '\n'
            'Retain the frozen original Issue URL and body; do not expand the scope, create a new Session, '
            'request a new budget, or merge any PR. Use this same repository and finite renewal window. '
            'Run the actual tests through the original native bash tool. Emit one corrected delivery JSON with '
            'actual native bash call IDs, including source_commit, issue_updated_at, criteria [{text,test_call_ids}], '
            'test_files, fine_issue_urls, pr_url, sync_branches, and leftovers.\n\n' + _prompt(intake, record, test_files=test_files))


def _verified_source_chain(execution, baseline, receipts, current, owned_jobs=(), *, job_receipts=()):
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
        elif receipt.get('kind') in {'promoted', 'background'}:
            job_id = receipt.get('job_id')
            if (any(key in receipt for key in ('exit_code', 'timed_out', 'aborted'))
                    or not isinstance(job_id, str) or not job_id or not isinstance(owned_jobs, (list, tuple))):
                return False
            matches = [job for job in owned_jobs if isinstance(job, dict) and job.get('id') == job_id]
            old_lease, current_lease = receipt.get('host_lease'), execution.get('native_identity', {}).get('lease')
            historical_lease = (isinstance(old_lease, str) and bool(old_lease)
                and isinstance(current_lease, str) and bool(current_lease) and old_lease != current_lease)
            if owned_jobs == [] and job_receipts or historical_lease:
                keys = ('schema', 'generation', 'session_id', 'session_created_at', 'service_ref', 'host_lease',
                        'command_sha256', 'job_id')
                rows = [row for row in job_receipts if isinstance(row, dict)
                        and row.get('job_id') == job_id and row.get('host_lease') == receipt.get('host_lease')]
                if (len(rows) != 2 or any(any(key not in receipt or row.get(key) != receipt[key] for key in keys)
                        or row.get('owner') != execution['session_id'] or row.get('kind') != 'bash' for row in rows)
                        or rows[0].get('type') != 'registered' or rows[0].get('status') != 'running'
                        or rows[1].get('type') != 'settled' or rows[1].get('cause') not in {'producer', 'kill'}
                        or type(rows[0].get('started_at_ms')) is not int or rows[0]['started_at_ms'] <= 0
                        or rows[1].get('started_at_ms') != rows[0]['started_at_ms']
                        or type(rows[1].get('finished_at_ms')) is not int
                        or rows[1]['finished_at_ms'] < rows[0]['started_at_ms']):
                    return False
                matches = [{'id': job_id, 'owner': rows[1]['owner'], 'status': rows[1].get('status')}]
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
    from .repository_acceptance import DELIVERY_MARKER, _report, repository_source_state
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


SHARED_INTAKE_LOCK_TIMEOUT_SECONDS = 5


def _try_shared_intake_lock(intake, descriptor, generation, deadline):
    intake.require_active(generation)
    if time.monotonic() >= deadline:
        raise ManagementError('repository_busy', 'Original repository intake is busy; no operation was admitted.')
    try:
        fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        return False
    intake.require_active(generation)
    return True


def _acquire_shared_intake_lock(intake, descriptor, generation):
    deadline = time.monotonic() + SHARED_INTAKE_LOCK_TIMEOUT_SECONDS
    while not _try_shared_intake_lock(intake, descriptor, generation, deadline):
        time.sleep(.02)


def admit_shared_natural_qa_rework(intake, record, source_context, feedback, window, generation):
    """Serialize one public QA intake and execute it against a fresh record snapshot."""
    descriptor = os.open(intake.lock_path, os.O_RDWR | os.O_NOFOLLOW)
    try:
        _acquire_shared_intake_lock(intake, descriptor, generation)
        current = next((item for item in intake.snapshot().get('work', []) if item.get('id') == record.get('id')), None)
        if current is None:
            raise ManagementError('outcome_unknown', 'The natural QA work record disappeared before admission.')
        return _admit_shared_natural_qa_rework_locked(intake, current, source_context, feedback, window, generation)
    finally:
        fcntl.flock(descriptor, fcntl.LOCK_UN)
        os.close(descriptor)


def _qa_request_only_budget(feedback, row):
    if '取消30分钟截止，保留80次请求' not in feedback:
        return None
    if {key: value for key, value in row.get('budget', {}).items() if key in CAPS} != {'max_wall_seconds': 1800, 'max_model_requests': 80}:
        raise ManagementError('invalid_change', 'Only the original 1800-second/80-request QA policy may drop its wall limit.')
    window, state = row.get('renewWindow', {}), row['state']
    used = state['requests'] - window.get('request_base', 0)
    if not 0 <= used < 80:
        raise ManagementError('outcome_unknown', 'The original QA request allowance has no confirmed remainder.')
    return {'max_model_requests': 80 - used}


def _refresh_qa_desktop_lease(intake, record, source_context, feedback):
    """Refresh only the Launcher lease after the verified maintenance reply."""
    import psutil
    execution = record['dsh_execution']
    control = execution.get('natural_qa_rework_control', {})
    checkpoint = execution.get('renewal_window_checkpoint', {})
    delivery = control.get('proposal_delivery', {})
    if (feedback != '维护完成' or checkpoint.get('status') != 'prepared'
            or checkpoint != control.get('checkpoint') or delivery.get('status') != 'delivered'
            or source_context.get('reply_to_message_id') != delivery.get('message_id')
            or any(source_context.get(key) != delivery.get(key) for key in ('chat_id', 'profile_id'))):
        raise ManagementError('binding_conflict', 'The maintenance reply does not match the original delivered proposal.')
    reference = private_file(record['target']['execution_ref'])
    raw = reference.read_bytes()
    scope = json.loads(raw)
    original_scope = checkpoint.get('scope', {})
    policy = _qa_request_only_budget(control.get('feedback', ''), {
        'budget': original_scope.get('budget'), 'state': checkpoint.get('previous_state', {}),
        'renewWindow': checkpoint.get('previous_renew_window', {})})
    revised_scope = dict(original_scope, budget=policy) if policy and checkpoint.get('window') == policy else original_scope
    if (hashlib.sha256(raw).hexdigest() != execution.get('reference_sha256')
            or scope not in (dict(original_scope, host_lease=scope.get('host_lease')),
                             dict(revised_scope, host_lease=scope.get('host_lease')))):
        raise ManagementError('binding_conflict', 'The original maintenance configuration changed.')
    receipt = json.loads(private_file(scope['desktop_receipt']).read_text())
    lease = receipt.get('lease')
    if lease == original_scope.get('host_lease') and scope == original_scope:
        return
    identity, _, _ = desktop_identity(scope['desktop_receipt'], lease)
    old = checkpoint['native_identity']
    if (any(identity.get(key) == old.get(key) for key in ('lease', 'pid', 'created_at'))
            or psutil.pid_exists(old['pid']) and psutil.Process(old['pid']).create_time() == old['created_at']):
        raise ManagementError('binding_conflict', 'Normal maintenance of the original Desktop is unverified.')
    if lease == scope['host_lease']:
        saved = control.get('desktop_reference_backup', {})
        preserved = private_file(saved['path']).read_bytes() if saved.get('path') else b''
        if (hashlib.sha256(preserved).hexdigest() != saved.get('sha256')
                or json.loads(preserved) != original_scope):
            raise ManagementError('binding_conflict', 'The original maintenance configuration backup is unverified.')
        return
    updated = {**scope, 'host_lease': lease}
    shared_configuration(reference, updated, workspace=record['target']['repo_path'])
    backup = reference.with_name(reference.name + '.before-maintenance-' + hashlib.sha256(raw).hexdigest())
    if not backup.exists():
        with os.fdopen(os.open(backup, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600), 'wb') as output:
            output.write(raw)
            output.flush()
            os.fsync(output.fileno())
    if private_file(backup).read_bytes() != raw or reference.read_bytes() != raw:
        raise ManagementError('binding_conflict', 'The original maintenance configuration backup differs.')
    content = (json.dumps(updated, indent=2) + '\n').encode()
    temporary = reference.with_name(reference.name + '.' + uuid.uuid4().hex)
    with os.fdopen(os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600), 'wb') as output:
        output.write(content)
        output.flush()
        os.fsync(output.fileno())
    os.replace(temporary, reference)
    execution['reference_sha256'] = hashlib.sha256(content).hexdigest()
    control['desktop_reference_backup'] = {'path': str(backup), 'sha256': hashlib.sha256(raw).hexdigest()}
    intake._save_execution(record, fields=['reference_sha256', 'natural_qa_rework_control'])


def _requeue_unstarted_qa(intake, record, source_context, generation, card, worker):
    from .repository_execution import execution_configuration
    from .repository_acceptance import repository_source_state
    from .repository_supervision import owned_call
    execution = record['dsh_execution']
    previous_retry = execution.get('qa_preinput_retry')
    retries = [*execution.get('qa_preinput_retry_history', []), *([previous_retry] if previous_retry else [])]
    if any(retry.get('message_id') == source_context['message_id'] for retry in retries):
        raise ManagementError('duplicate', 'The original QA retry message was already handled.')
    control = execution['continuation_control']
    checkpoint = execution.get('renewal_window_checkpoint', {})
    configuration, _ = execution_configuration(record['target'])
    configuration.update(workspace=record['target']['repo_path'], session_id=execution['session_id'],
                         generation=execution['generation'], workspace_id=execution['workspace_id'])
    raw = private_file(ledger_path(configuration, execution['session_id'])).read_bytes()
    budget = read_ledger(configuration, execution)
    window = budget.get('renewWindow', {})
    if (execution.get('renewal_window_adoption') != 'accepted' or checkpoint.get('status') != 'admitted'
            or configuration['reference_sha256'] != execution['reference_sha256']
            or hashlib.sha256(raw).hexdigest() != control.get('ledger_sha256')
            or window.get('request_base') != budget['state']['requests']
            or any(window.get(key, 'missing') is not None for key in ('started_at_ms', 'hook_marker', 'stop_reason'))
            or window.get('original_state') != checkpoint.get('previous_state')
            or repository_source_state(record['target']) != checkpoint.get('source')
            or worker.get('exit_code') != 0 or worker.get('run_id') != execution['supervisor_claim']['run_id']):
        raise ManagementError('outcome_unknown', 'Only the unchanged unstarted QA continuation may be requeued.')
    carrier = SharedDesktopTransport(configuration)
    try:
        stream = carrier.stream('session/follow', {'request': {'address': {'kind': 'session',
            'sessionId': execution['session_id']}, 'maxMessages': 500}})
        try:
            snapshot = next(stream)
        finally:
            stream.close()
        sessions = [row for row in owned_call(carrier, 'session/list', {})['items']
                    if row.get('sessionId') == execution['session_id']]
        workspace = owned_call(carrier, 'workspace/create', {'path': configuration['workspace']})
        account = workspace.get('workspace', {})
        if (carrier.native_identity != execution['native_identity']
                or hashlib.sha256(json.dumps(snapshot, sort_keys=True).encode()).hexdigest() != control['snapshot_sha256']
                or len(sessions) != 1 or sessions[0].get('running') is not False
                or sessions[0].get('agentAvailable') is not True or carrier.owned_jobs()
                or workspace.get('created') is not False or account.get('path') != configuration['workspace']
                or {'workspace_id': account.get('workspaceId'), 'session_ids': account.get('sessionIds')} != control.get('workspace')
                or account.get('workspaceId') != execution['workspace_id']
                or private_file(ledger_path(configuration, execution['session_id'])).read_bytes() != raw):
            raise ManagementError('outcome_unknown', 'The original QA input or owned execution is not idle.')
        intake.require_active(generation)
        history = execution.setdefault('qa_preinput_retry_history', [])
        if previous_retry:
            history.append(deepcopy(previous_retry))
        execution['qa_preinput_retry'] = {'status': 'intent', 'message_id': source_context['message_id'],
            'previous_control': deepcopy(control), 'previous_failure': deepcopy(execution.get('last_failure'))}
        control.update(previous_claim=deepcopy(execution['supervisor_claim']), worker=worker)
        execution['state'] = 'continuation_pending'
        intake._save_execution(record, fields=['qa_preinput_retry', 'qa_preinput_retry_history', 'continuation_control', 'state'])
        if card['status'] == 'triage':
            from hermes_cli import kanban_db as kb
            from hermes_cli.kanban_db_connect import connect_closing
            with connect_closing(board=intake.board) as connection:
                if not kb.specify_triage_task(connection, record['card_id'], author='verified-owner'):
                    raise ManagementError('outcome_unknown', 'Original QA redispatch is unconfirmed.')
        else:
            intake._kanban('kanban_unblock', {'task_id': record['card_id']})
        execution['qa_preinput_retry']['status'] = 'accepted'
        intake._save_execution(record, fields=['qa_preinput_retry'])
        return {'status': 'accepted', 'work_id': record['id']}
    finally:
        carrier.close()


def _admit_shared_natural_qa_rework_locked(intake, record, source_context, feedback, window, generation):
    """Queue bounded browser acceptance feedback from the public NATURAL-QA-INTAKE tool."""
    import psutil
    from .manager import _public_text
    from .repository_acceptance import _human_pending, _report, repository_source_state
    from .repository_control import _card, _worker_end
    from .repository_execution import execution_configuration
    from .repository_supervision import owned_call
    from .simple_development import _qa_profile_matches
    execution = record['dsh_execution']
    allowed_context = {'app_id', 'transport_tenant_key', 'tenant_key', 'chat_id', 'profile_id', 'message_id',
                       'reply_to_message_id',
                       'recipient_tenant_key', 'recipient_open_id', 'sender_open_id'}
    message_id = source_context.get('message_id') if isinstance(source_context, dict) else None
    if (not isinstance(source_context, dict) or not set(source_context) <= allowed_context
            or not {'app_id', 'transport_tenant_key', 'tenant_key', 'chat_id', 'profile_id', 'message_id'} <= set(source_context)
            or not all(isinstance(source_context.get(key), str) and source_context[key] for key in source_context)
            or not isinstance(feedback, str) or not feedback.strip() or len(feedback) > 4000
            or not isinstance(window, (dict, type(None)))):
        raise ManagementError('invalid_change', 'Natural QA intake requires bounded public identity, feedback and window data.')
    _public_text(feedback, intake.secret_values)
    anchor = record.get('source_anchor', {})
    for key in ('app_id', 'transport_tenant_key', 'tenant_key', 'chat_id'):
        if source_context.get(key) != anchor.get(key):
            raise ManagementError('binding_conflict', 'The natural QA intake identity differs from the frozen source.')
    if not _qa_profile_matches(record, source_context['profile_id']):
        raise ManagementError('unauthorized', 'The natural QA intake profile differs from the original owner.')
    if any(source_context.get(key) != anchor.get(key) for key in
           ('recipient_tenant_key', 'recipient_open_id', 'sender_open_id') if key in source_context):
        raise ManagementError('binding_conflict', 'The natural QA intake sender or recipient differs from the frozen source.')
    if message_id in record.get('dsh_execution', {}).get('control_message_ids', []):
        raise ManagementError('duplicate', 'The natural QA intake message was already handled.')
    if record.get('dsh_execution', {}).get('natural_qa_rework_control', {}).get('message_id') == message_id:
        raise ManagementError('duplicate', 'The natural QA intake message was already handled.')
    prior_natural = execution.get('natural_qa_rework_control', {})
    previous_qa_completed = (execution.get('state') == 'awaiting_acceptance'
        and execution.get('terminal_reason') == {'kind': 'completed'}
        and execution.get('continuation_control', {}).get('kind') == 'qa_rework'
        and execution['continuation_control'].get('status') == 'accepted'
        and execution['continuation_control'].get('dispatch_status') == 'accepted')
    completed_qa = previous_qa_completed and not any(
        control.get('request_id') == execution.get('continuation_request_id')
        for control in execution.get('continuation_history', [])
        if 'natural_qa_rework_control' in control)
    if completed_qa:
        previous_ids = {control.get('message_id') for control in execution.get('continuation_history', [])}
        previous_ids.add(execution['continuation_control'].get('message_id'))
        if message_id in previous_ids:
            raise ManagementError('duplicate', 'The completed QA message was already handled.')
        prior_natural = {}
    if (execution.get('mode') != 'shared_native' or not _qa_rework_acceptance(execution)
            or execution.get('first_input') != 'accepted' or execution.get('stop_requested')
            or execution.get('lifecycle_end')
            or prior_natural.get('status') in {'intent', 'accepted', 'unknown', 'renewal_intent', 'renewed'}
            or _human_pending(execution)):
        raise ManagementError('outcome_unknown', 'Only one idle unmet browser acceptance may queue QA rework.')
    acceptance = execution['acceptance']
    missing_report = 'delivery_report_missing' in acceptance.get('unmet', [])
    card = _card(intake, record)
    if card['status'] not in {'blocked', 'triage'} or card.get('current_run_id') is not None or card.get('worker_pid') is not None:
        raise ManagementError('outcome_unknown', 'The browser acceptance rework card is not idle.')
    worker = _worker_end(intake, record)
    if _pending_qa_rework_retry(execution):
        return _requeue_unstarted_qa(intake, record, source_context, generation, card, worker)
    if prior_natural.get('status') == 'maintenance_required' and feedback == '维护完成':
        _refresh_qa_desktop_lease(intake, record, source_context, feedback)
    configuration, _ = execution_configuration(record['target'])
    if configuration['reference_sha256'] != execution.get('reference_sha256'):
        raise ManagementError('binding_conflict', 'The original shared execution reference changed.')
    configuration.update(workspace=record['target']['repo_path'], session_id=execution['session_id'],
                         generation=execution['generation'], workspace_id=execution.get('workspace_id'))
    budget_file = ledger_path(configuration, execution['session_id'])
    budget_before = private_file(budget_file).read_bytes()
    adopting = prior_natural.get('status') == 'maintenance_required'
    maintenance = execution.get('natural_qa_rework_control', {}).get('checkpoint', {})
    ledger_configuration = (dict(configuration, host_lease=maintenance.get('native_identity', {}).get('lease'),
        budget=maintenance['scope']['budget'])
        if adopting else configuration)
    budget = read_ledger(ledger_configuration, execution)
    source = repository_source_state(record['target'])
    if not missing_report and (source.get('head') != acceptance.get('source_commit')
            or source.get('source_digest') != acceptance.get('source_digest')
            or source.get('workspace_status') != acceptance.get('workspace_status')):
        raise ManagementError('binding_conflict', 'The accepted delivery source no longer matches the repository.')
    baseline = _source_core(execution.get('baseline'))
    current = _source_core(source)
    if baseline is None or current is None or not missing_report and baseline == current:
        raise ManagementError('binding_conflict', 'The delivered source was not promoted from the original baseline.')
    current_window = budget.get('renewWindow')
    if current_window is None:
        current_window = budget['state']
    expected_lease = maintenance.get('native_identity', {}).get('lease') if adopting else configuration['host_lease']
    if (not isinstance(current_window, dict) or 'renewWindow' in budget
            and (current_window.get('host_lease') != expected_lease or current_window.get('hook_marker') != 'same-sid-window-v1')):
        raise ManagementError('outcome_unknown', 'The original renewed window is unavailable.')
    requested_window = None
    approved_renewal = False
    approved_maintenance = False
    revised_budget = _qa_request_only_budget(prior_natural.get('feedback', feedback), budget)
    if prior_natural.get('status') in {'renewal_required', 'maintenance_required'}:
        delivery = prior_natural.get('proposal_delivery', {})
        proposal = prior_natural.get('proposal', {})
        if (delivery.get('status') != 'delivered' or source_context.get('reply_to_message_id') != delivery.get('message_id')
                or feedback not in proposal.get('options', [])):
            raise ManagementError('binding_conflict', 'The natural QA reply does not reference the delivered proposal.')
        if feedback == '拒绝' or feedback == '取消':
            execution['natural_qa_rework_control']['status'] = 'declined'
            intake._save_execution(record, fields=['natural_qa_rework_control'])
            return {'status': 'declined', 'work_id': record['id']}
        requested_window = prior_natural.get('window')
        approved_renewal = prior_natural.get('status') == 'renewal_required'
        approved_maintenance = prior_natural.get('status') == 'maintenance_required'
        if adopting:
            maintenance = execution.get('renewal_window_checkpoint', maintenance)
            if requested_window is None:
                requested_window = maintenance.get('window')
        if adopting and requested_window is None:
            raise ManagementError('outcome_unknown', 'The natural QA maintenance checkpoint is incomplete.')
    if prior_natural.get('status') == 'maintenance_required' and requested_window is None:
        raise ManagementError('outcome_unknown', 'Maintenance-required QA renewal must be retried with its explicit configured window.')
    if requested_window is None and 'sealed_unknown_usage' in current_window:
        checkpoint = execution.get('renewal_window_checkpoint', {})
        if (not previous_qa_completed or execution.get('renewal_window_adoption') != 'accepted'
                or checkpoint.get('status') != 'admitted'
                or checkpoint.get('sealed_unknown_usage') != current_window['sealed_unknown_usage']
                or checkpoint.get('previous_state') != current_window.get('original_state')):
            raise ManagementError('binding_conflict', 'A current window with sealed unknown usage cannot be reused for QA intake.')
    renewal_required = False
    if requested_window is None:
        if type(current_window.get('started_at_ms')) is not int or current_window['started_at_ms'] <= 0:
            raise ManagementError('outcome_unknown', 'The original active renewed window is unavailable.')
        try:
            if revised_budget:
                _require_expired_window_budget(ledger_configuration, budget)
                renewal_required = True
            else:
                _require_window_budget(configuration, budget)
        except ManagementError:
            _require_expired_window_budget(ledger_configuration, budget)
            renewal_required = True
    else:
        expected_window = revised_budget or {'max_wall_seconds': configuration['budget']['max_wall_seconds'],
                                            'max_model_requests': configuration['budget']['max_model_requests']}
        if requested_window != expected_window:
            raise ManagementError('invalid_change', 'Natural QA renewal must use the configured finite window exactly.')
        _require_expired_window_budget(ledger_configuration, budget)
        renewal_required = prior_natural.get('status') not in {'renewal_required', 'maintenance_required'}
    request_id = 'qa-rework-' + hashlib.sha256((record['id'] + ':' + message_id).encode()).hexdigest()
    carrier = SharedDesktopTransport(configuration)
    control = None
    renewal_control = None
    try:
        new_identity = carrier.native_identity
        if requested_window is None and not adopting and not renewal_required and new_identity != execution.get('native_identity'):
            raise ManagementError('binding_conflict', 'The active natural QA Host identity changed.')
        stream = carrier.stream('session/follow', {'request': {'address': {'kind': 'session',
            'sessionId': execution['session_id']}, 'maxMessages': 500}})
        try:
            before = next(stream)
        finally:
            stream.close()
        header = before.get('header', {})
        values = before.get('projections', {}).get('values', {})
        events = [row['event'] for row in before.get('records', [])]
        starts = [event for event in events if event.get('type') == 'turn/start']
        ends = [event for event in events if event.get('type') == 'turn/end']
        if (before.get('hasMore') is not False or header.get('id') != execution['session_id']
                or header.get('createdAt') != execution.get('session_created_at')
                or header.get('cwd') != configuration['workspace']
                or values.get('agentPreset') != configuration['agent_preset']
                or values.get('permissions') != {'currentValue': 'workspace-write'}
                or values.get('inbox') != {'next-turn': [], 'next-step': []}
                or not starts or not ends or ends[-1]['seq'] <= starts[-1]['seq']
                or ends[-1].get('data', {}).get('reason') != execution.get('terminal_reason')):
            raise ManagementError('outcome_unknown', 'The original completed browser acceptance evidence is unconfirmed.')
        report = _report(events)
        if not missing_report and (report is None or report.get('source_commit') != acceptance.get('source_commit')
                or 'source_digest' in report and report.get('source_digest') != acceptance.get('source_digest')
                or 'workspace_status' in report and report.get('workspace_status') != acceptance.get('workspace_status')):
            raise ManagementError('outcome_unknown', 'The original completed browser acceptance evidence is unconfirmed.')
        sessions = [row for row in owned_call(carrier, 'session/list', {})['items']
                    if row.get('sessionId') == execution['session_id']]
        workspace = owned_call(carrier, 'workspace/create', {'path': configuration['workspace']})
        account = workspace.get('workspace', {})
        jobs = carrier.owned_jobs()
        receipt_configuration = (dict(configuration, host_lease=maintenance.get('native_identity', {}).get('lease'))
            if adopting else configuration)
        receipts = shared_receipts(receipt_configuration, execution, '.tools.jsonl')
        if (workspace.get('created') is not False or account.get('workspaceId') != execution.get('workspace_id')
                or account.get('path') != configuration['workspace']
                or account.get('sessionIds', []).count(execution['session_id']) != 1
                or len(sessions) != 1 or sessions[0].get('cwd') != configuration['workspace']
                or sessions[0].get('running') is not False or sessions[0].get('agentAvailable') is not True
                or any(not isinstance(job, dict) or job.get('status') not in {'completed', 'killed', 'failed'}
                    or job.get('owner', execution['session_id']) != execution['session_id'] for job in jobs)):
            raise ManagementError('outcome_unknown', 'The original Session, Workspace, receipts or jobs are not idle.')
        if not (missing_report and baseline == current and not receipts
                or _verified_source_chain(execution, execution.get('baseline'), receipts, source, jobs,
                    job_receipts=shared_receipts(receipt_configuration, execution, '.jobs.jsonl'))):
            raise ManagementError('outcome_unknown', 'Original source attribution receipts are unverified.')
        if private_file(budget_file).read_bytes() != budget_before:
            raise ManagementError('outcome_unknown', 'The original Session, Workspace, receipts or jobs are not idle.')
        if completed_qa:
            previous = deepcopy(execution['continuation_control'])
            previous.update(request_id=execution.get('continuation_request_id'),
                natural_qa_rework_control=deepcopy(execution.get('natural_qa_rework_control')))
            execution.setdefault('continuation_history', []).append(previous)
            intake._save_execution(record, fields=['continuation_history'])
        if not renewal_required and requested_window is None and not adopting:
            request_id = 'qa-rework-' + hashlib.sha256((record['id'] + ':' + message_id).encode()).hexdigest()
            execution['continuation_request_id'] = request_id
            execution['continuation_control'] = {'kind': 'qa_rework', 'message_id': message_id,
                'status': 'admitted', 'dispatch_status': 'intent', 'previous_claim': execution['supervisor_claim'],
                'worker': worker, 'cursor': before['cursor'],
                'snapshot_sha256': hashlib.sha256(json.dumps(before, sort_keys=True).encode()).hexdigest(),
                'ledger_sha256': hashlib.sha256(budget_before).hexdigest(), 'qa_feedback': feedback,
                **({'repository_source': current, 'missing_report': True} if missing_report else {}),
                'qa_proposal': prior_natural.get('proposal'),
                'workspace': {'workspace_id': account['workspaceId'], 'session_ids': account['sessionIds']}}
            execution['state'] = 'continuation_pending'
            execution['natural_qa_rework_control'] = {'kind': 'browser_qa_rework', 'status': 'accepted',
                'message_id': message_id, 'feedback': feedback, 'proposal': prior_natural.get('proposal')}
            control_ids = execution.setdefault('control_message_ids', [])
            if message_id not in control_ids:
                control_ids.append(message_id)
            intake._save_execution(record, fields=['continuation_request_id', 'continuation_control',
                'natural_qa_rework_control', 'control_message_ids', 'state', *(['continuation_history'] if completed_qa else [])])
            if card['status'] == 'triage':
                from hermes_cli import kanban_db as kb
                from hermes_cli.kanban_db_connect import connect_closing
                with connect_closing(board=intake.board) as connection:
                    if not kb.specify_triage_task(connection, record['card_id'], author='verified-owner'):
                        raise ManagementError('outcome_unknown', 'QA rework triage dispatch is unconfirmed.')
            else:
                intake._kanban('kanban_unblock', {'task_id': record['card_id']})
            execution['continuation_control']['dispatch_status'] = 'accepted'
            intake._save_execution(record, fields=['continuation_control'])
            return {'status': 'accepted', 'work_id': record['id']}
        if approved_renewal and requested_window is not None and not adopting:
            prepared = prepare_shared_window_renewal(intake, record, message_id)
            current = next(item for item in intake.snapshot()['work'] if item['id'] == record['id'])
            if revised_budget:
                current['dsh_execution']['renewal_window_checkpoint']['window'] = revised_budget
                intake._save_execution(current, fields=['renewal_window_checkpoint'])
            proposal = {'id': 'qa-maintenance-' + hashlib.sha256((record['id'] + ':' + message_id).encode()).hexdigest(),
                'kind': 'maintenance', 'options': ['维护完成', '取消'],
                'text': '请在原生 Launcher 完成 Host 维护后回复“维护完成”或“取消”。'}
            current['dsh_execution']['natural_qa_rework_control'] = {
                'kind': 'browser_qa_rework', 'status': 'maintenance_required', 'message_id': message_id,
                'feedback': current['dsh_execution'].get('natural_qa_rework_control', {}).get('feedback', feedback),
                'window': current['dsh_execution'].get('natural_qa_rework_control', {}).get('window') or {
                    'max_wall_seconds': configuration['budget']['max_wall_seconds'],
                    'max_model_requests': configuration['budget']['max_model_requests']},
                'proposal': proposal, 'proposal_delivery': {'status': 'intent', 'proposal_id': proposal['id'],
                    'chat_id': source_context['chat_id'], 'profile_id': source_context['profile_id']},
                'checkpoint': current['dsh_execution'].get('renewal_window_checkpoint')}
            intake._save_execution(current, fields=['natural_qa_rework_control'])
            return {'status': 'maintenance_required', 'work_id': current['id'],
                'proposal': proposal}
        if approved_maintenance and adopting:
            admitted = admit_shared_window_renewal(intake, record, message_id, generation)
            current = next(item for item in intake.snapshot()['work'] if item['id'] == record['id'])
            control = current['dsh_execution'].get('continuation_control', {})
            if control.get('kind') != 'qa_rework' or not isinstance(control.get('qa_feedback'), str) or not control['qa_feedback']:
                raise ManagementError('outcome_unknown', 'The admitted QA continuation feedback is missing.')
            return {'status': 'accepted', 'work_id': current['id']}
        if renewal_required:
            caps = revised_budget or {'max_wall_seconds': configuration['budget']['max_wall_seconds'],
                'max_model_requests': configuration['budget']['max_model_requests']}
            proposal = {'id': 'qa-window-' + hashlib.sha256((record['id'] + ':' + message_id).encode()).hexdigest(),
                'kind': 'window', 'limits': caps, 'options': ['同意', '拒绝'],
                'text': ('请确认取消30分钟截止；原80次请求已用' + str(80 - caps['max_model_requests'])
                    + '，剩' + str(caps['max_model_requests']) + '次。原 SID、累计用量及旧窗口保留。'
                    if revised_budget else '请确认同一原生 Session 的一次有限浏览器 QA 窗口：'
                    + str(caps['max_wall_seconds']) + ' 秒、' + str(caps['max_model_requests']) + ' 次模型请求。')}
            execution['natural_qa_rework_control'] = {'kind': 'browser_qa_rework',
                'status': 'renewal_required', 'message_id': message_id,
                'feedback': feedback, 'window': caps, 'proposal': proposal,
                'proposal_delivery': {'status': 'intent', 'proposal_id': proposal['id'],
                    'chat_id': source_context['chat_id'], 'profile_id': source_context['profile_id']}}
            intake._save_execution(record, fields=['natural_qa_rework_control'])
            return {'status': 'renewal_required', 'work_id': record['id'], 'proposal': proposal}
        raise ManagementError('outcome_unknown', 'The natural QA continuation could not be admitted.')
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
    if not CAPS <= set(configuration['budget']):
        raise ManagementError('outcome_unknown', 'No explicit finite wall window is available to renew.')
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
    from .repository_acceptance import DELIVERY_MARKER, _report, repository_source_state
    from .repository_control import _card, _worker_end
    from .repository_supervision import owned_call
    execution = record['dsh_execution']
    acceptance = execution.get('acceptance', {})
    previous_checkpoint = execution.get('renewal_window_checkpoint', {})
    repeat = _repeat_budget_stopped_window(execution)
    qa_case = (execution.get('state') == 'awaiting_acceptance'
        and execution.get('terminal_reason') == {'kind': 'completed'}
        and acceptance.get('status') == 'unmet' and acceptance.get('delivered') is False
        and 'delivery_report_missing' not in acceptance.get('unmet', []))
    prior_qa_window = (qa_case and previous_checkpoint.get('status') == 'admitted'
        and execution.get('renewal_window_adoption') == 'accepted'
        and execution.get('continuation_control', {}).get('status') == 'accepted'
        and execution.get('continuation_control', {}).get('dispatch_status') == 'accepted')
    completed = (execution.get('state') == 'awaiting_acceptance'
        and execution.get('terminal_reason') == {'kind': 'completed'} and (not previous_checkpoint or prior_qa_window)
        and ('delivery_report_missing' in acceptance.get('unmet', []) or qa_case))
    if (execution.get('mode') != 'shared_native' or not (completed or repeat)
            or execution.get('first_input') != 'accepted' or execution.get('stop_requested') or execution.get('lifecycle_end')
            or repeat and (previous_checkpoint.get('status') != 'admitted'
                or execution.get('renewal_window_adoption') != 'accepted'
                or execution.get('continuation_control', {}).get('status') != 'accepted')
            or acceptance.get('status') != 'unmet'
            or acceptance.get('delivered') is not False
            or ('delivery_report_missing' not in acceptance.get('unmet', []) and not qa_case)):
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
        if not _verified_source_chain(execution, execution.get('baseline'), receipts, source, owned_jobs,
                job_receipts=shared_receipts(configuration, execution, '.jobs.jsonl')):
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
                or len(settlements) != row['state']['requests']
                or (not qa_case and delivery)
                or not repeat and not qa_case and not _rejected_bash_window_evidence(events, receipts)
                or any(not isinstance(job, dict) or job.get('status') not in {'completed', 'killed', 'failed'} for job in owned_jobs)
                or repeat and (execution.get('tool_receipts') != receipts or execution.get('jobs') != [
                    {'id': job['id'], 'status': job['status']} for job in owned_jobs])
                or private_file(budget_file).read_bytes() != raw):
            raise ManagementError('outcome_unknown', 'The expired window has unverified delivery, tool, job or budget activity.')
        if qa_case:
            report = _report(events)
            if (report is None or report.get('source_commit') != acceptance.get('source_commit')
                    or 'source_digest' in report and report.get('source_digest') != acceptance.get('source_digest')
                    or 'workspace_status' in report and report.get('workspace_status') != acceptance.get('workspace_status')):
                raise ManagementError('outcome_unknown', 'The QA delivery report no longer matches acceptance source evidence.')
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
        if repeat or prior_qa_window:
            execution.setdefault('renewal_window_checkpoint_history', []).append({
                'checkpoint': deepcopy(previous_checkpoint), 'adoption': execution['renewal_window_adoption']})
            execution['renewal_window_adoption'] = None
        execution['renewal_window_checkpoint'] = {'status': 'prepared', 'message_id': message_id, 'scope': scope,
            'repeat_budget_stopped': repeat, 'terminal_reason': deepcopy(execution['terminal_reason']),
            'qa_rework': qa_case,
            'jobs': [{'id': job['id'], 'status': job['status']} for job in owned_jobs],
            'history_sha256': _renewal_history_sha256(execution),
            'native_identity': execution['native_identity'], 'worker': worker, 'ledger_backup': str(backup),
            'ledger_sha256': hashlib.sha256(raw).hexdigest(), 'cursor': snapshot['cursor'],
            'snapshot_sha256': hashlib.sha256(json.dumps(snapshot, sort_keys=True).encode()).hexdigest(),
            'workspace': {'workspace_id': workspace['workspaceId'], 'session_ids': workspace['sessionIds']},
            'receipts': proofs, 'source': source, 'previous_state': deepcopy(row['state']),
            'previous_renew_window': deepcopy(row['renewWindow'])}
        if qa_case:
            sealed = row['renewWindow'].get('sealed_unknown_usage')
            if prior_qa_window and sealed is not None and sealed == previous_checkpoint.get('sealed_unknown_usage'):
                if _sealed_unknown_usage(events, previous_checkpoint.get('previous_renew_window')) != sealed:
                    raise ManagementError('outcome_unknown', 'The previously sealed QA usage boundary is unconfirmed.')
                execution['renewal_window_checkpoint']['sealed_unknown_usage'] = deepcopy(sealed)
            else:
                execution['renewal_window_checkpoint']['sealed_unknown_usage'] = _sealed_unknown_usage(events, row['renewWindow'])
        fields = ['renewal_window_checkpoint']
        if repeat or prior_qa_window:
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
    revised_budget = _qa_request_only_budget(execution.get('natural_qa_rework_control', {}).get('feedback', ''), {
        'budget': checkpoint['scope'].get('budget'), 'state': checkpoint['previous_state'],
        'renewWindow': checkpoint['previous_renew_window']})
    if revised_budget and (checkpoint.get('window') != revised_budget
            or execution.get('natural_qa_rework_control', {}).get('window') != revised_budget):
        raise ManagementError('binding_conflict', 'The approved remaining QA request allowance changed.')
    expected = json.loads(json.dumps(checkpoint['scope']))
    expected['host_lease'] = scope.get('host_lease')
    if scope != expected and (not revised_budget or scope != dict(expected, budget=revised_budget)):
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
    old_configuration = dict(configuration, host_lease=old['lease'],
        budget=checkpoint['scope'].get('budget', configuration['budget']))
    row = read_ledger(old_configuration, execution)
    if (row.get('state') != checkpoint['previous_state']
            or row.get('renewWindow') != checkpoint['previous_renew_window']):
        raise ManagementError('binding_conflict', 'The original expired window state changed.')
    _require_expired_window_budget(old_configuration, row)
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
        qa_rework = checkpoint.get('qa_rework') is True
        natural_control = execution.get('natural_qa_rework_control', {})
        execution['continuation_control'] = {'kind': 'qa_rework' if qa_rework else 'renew_window', 'message_id': message_id, 'status': 'rebind_intent',
            'dispatch_status': 'intent', 'previous_claim': execution['supervisor_claim'], 'worker': checkpoint['worker'],
            'cursor': snapshot['cursor'], 'workspace': checkpoint['workspace'],
            'snapshot_sha256': hashlib.sha256(json.dumps(snapshot, sort_keys=True).encode()).hexdigest(),
            'original_failure': {key: execution[key] for key in ('state', 'terminal_reason', 'last_failure') if key in execution},
            **({'qa_feedback': natural_control.get('feedback'), 'qa_proposal': natural_control.get('proposal')}
               if qa_rework else {})}
        intake._save_execution(record, fields=['continuation_history', 'renewal_window_history', 'continuation_request_id', 'continuation_control'])
        if revised_budget:
            # Each file is replaced atomically; the saved intent and old backups retain partial-update evidence.
            reference = private_file(record['target']['execution_ref'])
            raw_scope = reference.read_bytes()
            if json.loads(raw_scope) != scope or hashlib.sha256(raw_scope).hexdigest() != execution['reference_sha256']:
                raise ManagementError('binding_conflict', 'The approved QA configuration changed before adoption.')
            updated_scope = dict(scope, budget=revised_budget)
            shared_configuration(reference, updated_scope, workspace=record['target']['repo_path'])
            content = (json.dumps(updated_scope, indent=2) + '\n').encode()
            temporary_scope = reference.with_name(reference.name + '.' + uuid.uuid4().hex)
            with os.fdopen(os.open(temporary_scope, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600), 'wb') as output:
                output.write(content)
                output.flush()
                os.fsync(output.fileno())
            os.replace(temporary_scope, reference)
            configuration['reference_sha256'] = hashlib.sha256(content).hexdigest()
            row['budget'] = revised_budget
        row['renewWindow'] = {'id': 'window-' + uuid.uuid4().hex, 'message_id': message_id, 'host_lease': new['lease'],
            'request_base': row['state']['requests'], 'original_state': dict(row['state']), 'original_host_lease': old['lease'],
            'started_at_ms': None, 'stop_reason': None, 'hook_marker': None}
        if qa_rework:
            row['renewWindow']['sealed_unknown_usage'] = checkpoint['sealed_unknown_usage']
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
            or any(type(row.get('awaited')) is not bool or row.get('cause') not in {'producer', 'kill'}
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
    previous = execution.get('github_reference')
    if previous and any(previous.get(k) != value for k, value in {
            'generation': execution['generation'], 'account': intake.github.account,
            'repository': record['target']['repository'], 'config_dir': str(directory / 'home/tmp/github')}.items()):
        raise ManagementError('binding_conflict', 'The original shared GitHub scope differs.')
    missing = bool(previous and not Path(previous['config_dir']).exists()
        and not Path(previous['config_dir']).is_symlink())
    prepared = prepare_github_execution(intake.github, record['target'], directory, execution['generation'])
    if previous and previous != prepared['reference']:
        if not missing or any(previous.get(k) != prepared['reference'].get(k)
                for k in ('generation', 'account', 'repository', 'config_dir')):
            raise ManagementError('binding_conflict', 'The original shared GitHub generation reference changed.')
        execution.setdefault('github_reference_history', []).append(deepcopy(previous))
        intake._save_execution(record, fields=['github_reference_history'])
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


def _prompt(intake, record, *, test_files=None):
    from .delivery import acceptance_criteria
    from .repository_execution import execution_configuration
    execution = record['dsh_execution']
    configuration, _ = execution_configuration(record['target'])
    configuration['workspace'] = record['target']['repo_path']
    path = ledger_path(configuration, execution['session_id'])
    if not path.exists() and not execution.get('first_input') and not execution.get('continuation_control'):
        budget_declaration = ('Current configured limits; original ledger usage is uninitialized: '
            + json.dumps({'limits': configuration['budget']}) + '. ')
    else:
        active_budget = _active_budget_snapshot(configuration, read_ledger(configuration, execution))
        budget_declaration = 'Current authoritative budget from the original ledger: ' + json.dumps(active_budget) + '. '
    evidence = {key: execution.get('acceptance', {}).get(key, []) for key in ('eligible_test_calls', 'test_files')}
    evidence['test_files'] = execution.get('acceptance', {}).get('test_files', test_files if test_files is not None else [])
    canonical = None
    if evidence['test_files']:
        try:
            canonical = verification_command(configuration, execution, record['target']['repo_path'], evidence['test_files'])
        except (ManagementError, KeyError, ValueError, TypeError, OSError):
            pass
    return (budget_declaration
        + 'Limits absent from this declaration are not configured. There is no plugin output-token or cumulative-token quota. '
        'This declaration overrides historical budget text in the frozen Issue; do not reuse old limits or request counts.\n'
        'Work only in this bound repository. Use the installed Matt skills to host requirement discussion, '
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
        + 'issue_updated_at must be the frozen value ' + record['issue']['updated_at']
        + '; comment timestamps do not replace it. Each criteria item has only text and test_call_ids, no note or other keys. '
          'sync_branches is an array of branch names, not an object. fine_issue_urls requires GitHub native sub-issue relationships; body links alone do not count. '
        + 'Copy each frozen acceptance criterion verbatim and in order: ' + json.dumps(acceptance_criteria(record['issue']['body']), ensure_ascii=False)
        + '. Verified eligible original native test evidence for the exact current source: '
        + json.dumps(evidence) + '. These call IDs match the fixed trusted test command and these explicit files. '
          'An empty list provides no eligible test evidence; run the trusted command and use its actual receipt. '
        + ('Complete canonical trusted test command for the declared test files: ' + canonical
            + '. Send this exact string unchanged as one separate native bash call. Do not prepend cd/env, '
              'reorder arguments, append commands or wrap the command. '
            if canonical else 'No valid explicit test file list is available for a complete canonical command yet. ')
        + 'Never replace IDs with narrative descriptions. '
          "Never infer receipt meaning from list order. Use only the actual trusted test command's own native call ID; "
          'Git/other IDs do not belong in test_call_ids; test_files are exactly that command\'s explicit Node test file list, '
          'formal pytest evidence separately in prose. '
          'Outer Kanban acceptance belongs to the supervisor.')


def record_shared_natural_qa_proposal(intake, record, proposal_id, delivery_context):
    """Record exactly one verified transport delivery for a natural QA proposal."""
    descriptor = os.open(intake.lock_path, os.O_RDWR | os.O_NOFOLLOW)
    try:
        _acquire_shared_intake_lock(intake, descriptor, intake.generation)
        current = next((item for item in intake.snapshot().get('work', []) if item.get('id') == record.get('id')), None)
        if current is None:
            raise ManagementError('outcome_unknown', 'The natural QA work record disappeared before proposal delivery.')
        execution = current['dsh_execution']
        prior = execution.get('natural_qa_rework_control', {}).get('proposal_delivery', {})
        from .simple_development import _qa_profile_matches
        proposal = execution.get('natural_qa_rework_control', {}).get('proposal', {})
        if proposal.get('id') != proposal_id or execution.get('natural_qa_rework_control', {}).get('status') not in {
                'renewal_required', 'maintenance_required'}:
            raise ManagementError('binding_conflict', 'The natural QA proposal is not current.')
        if (not isinstance(delivery_context, dict)
                or set(delivery_context) not in ({'status', 'message_id', 'chat_id', 'profile_id'},
                    {'status', 'chat_id', 'profile_id'})
                or delivery_context.get('status') not in {'delivered', 'unknown', 'failed'}
                or delivery_context.get('status') == 'delivered' and not isinstance(delivery_context.get('message_id'), str)
                or not all(isinstance(value, str) and value for key, value in delivery_context.items()
                           if key not in {'status', 'message_id'})
                or delivery_context['chat_id'] != current.get('source_anchor', {}).get('chat_id')
                or not _qa_profile_matches(current, delivery_context['profile_id'])
                or any(delivery_context.get(key) != prior.get(key) for key in ('chat_id', 'profile_id'))):
            raise ManagementError('binding_conflict', 'The natural QA proposal delivery identity differs.')
        if prior is None or prior.get('status') != 'intent' or prior.get('proposal_id') != proposal_id:
            raise ManagementError('duplicate', 'The natural QA proposal is no longer awaiting delivery.')
        if prior.get('status') != 'intent':
            if all(prior.get(key) == value for key, value in delivery_context.items()):
                return {'status': 'duplicate', 'work_id': current['id']}
            raise ManagementError('duplicate', 'A different delivery was already recorded for this proposal.')
        execution['natural_qa_rework_control']['proposal_delivery'] = {'proposal_id': proposal_id, **delivery_context}
        intake._save_execution(current, fields=['natural_qa_rework_control'])
        return {'status': 'recorded', 'work_id': current['id']}
    finally:
        fcntl.flock(descriptor, fcntl.LOCK_UN)
        os.close(descriptor)


def continue_shared_pending_question(intake, record, message_id):
    """Requeue the owner's original triage card; never replay its Session input."""
    from hermes_cli import kanban_db as kb
    from hermes_cli.kanban_db_connect import connect_closing
    from .repository_control import _worker_end
    from .repository_execution import execution_configuration
    execution = record['dsh_execution']
    if execution.get('mode') != 'shared_native' or not execution.get('workspace_id'):
        raise ManagementError('binding_conflict', 'The original pending-question Workspace is unverified.')
    _worker_end(intake, record, statuses={'triage'}, allow_later_terminal=True)
    configuration, _ = execution_configuration(record['target'])
    configuration['workspace'] = record['target']['repo_path']
    budget_file = ledger_path(configuration, execution['session_id'])
    budget_before = private_file(budget_file).read_bytes()
    carrier = shared_attach(intake, record, observe=False)
    try:
        stream = carrier.stream('session/follow', {'request': {'address': {'kind': 'session',
            'sessionId': execution['session_id']}, 'maxMessages': 500}})
        try:
            snapshot = next(stream)
        finally:
            stream.close()
        header = snapshot.get('header', {})
        if (header.get('id') != execution['session_id'] or header.get('cwd') != configuration['workspace']
                or header.get('createdAt') != execution.get('session_created_at')
                or snapshot.get('projections', {}).get('values', {}).get('agentPreset') != configuration['agent_preset']):
            raise ManagementError('binding_conflict', 'Original pending-question Session identity changed.')
        if not _take_over_gave_up_claim(intake, record, None, configuration, carrier,
                snapshot=snapshot, verify_only=True):
            raise ManagementError('outcome_unknown', 'The original pending-question claim is unverified.')
        if private_file(budget_file).read_bytes() != budget_before:
            raise ManagementError('outcome_unknown', 'The original pending-question budget changed.')
        with connect_closing(board=intake.board) as connection:
            card = kb.get_task(connection, record['card_id'])
            if (not _original_shared_card(intake, record, card) or card.status != 'triage'
                    or card.current_run_id is not None or card.worker_pid is not None):
                raise ManagementError('binding_conflict', 'Original pending-question triage card changed.')
            if not kb.specify_triage_task(connection, record['card_id'], author='verified-owner'):
                raise ManagementError('outcome_unknown', 'Original pending-question dispatch is unconfirmed.')
        execution.setdefault('control_message_ids', []).append(message_id)
        intake._save_execution(record, fields=['control_message_ids'])
        return record
    finally:
        carrier.close()


def _pending_original_question(execution, snapshot, carrier):
    from .dsh import DshRemoteAdapter
    if not isinstance(snapshot, dict) or snapshot.get('hasMore') is not False:
        raise ManagementError('outcome_unknown', 'Original pending-question history is incomplete.')
    events = [DshRemoteAdapter._journal_event(row) for row in snapshot.get('records', [])]
    if (len(events) != snapshot.get('cursor', -2) + 1
            or any(event['seq'] != index for index, event in enumerate(events))
            or not any(event['type'] == 'user/message'
                and event['data'].get('source') == {'kind': 'user', 'rpcId': execution.get('request_id')}
                for event in events)):
        raise ManagementError('outcome_unknown', 'Original pending-question input has no complete native audit.')
    projections = snapshot.get('projections', {}).get('values', {})
    if projections.get('inbox') != {'next-turn': [], 'next-step': []}:
        raise ManagementError('outcome_unknown', 'Pending-question Session has unrelated queued input.')
    # Waterfall clients belong to the gateway. A worker can verify its fresh
    # native projection against the question already registered by that gateway.
    observer = execution.get('shared_event_observer', {})
    if (observer.get('status') != 'ready' or not observer.get('client_id')
            or observer.get('session_id') != execution['session_id']
            or observer.get('generation') != execution['generation']
            or observer.get('host_lease') != execution.get('native_identity', {}).get('lease')):
        raise ManagementError('binding_conflict', 'Original pending-question observer belongs to another execution.')
    active = projections.get('userQuestions', {}).get('active', [])
    matched = []
    for question in execution.get('questions', []):
        if question.get('kind') != 'structured' or question.get('state') != 'open':
            continue
        native = [row for row in active if row.get('callId') == question['id'] and row.get('state') == 'open'
            and row.get('questions') == question['questions']]
        calls = [event for event in events if event['type'] == 'tool/call'
            and event['data'].get('name') == 'ask_user_question' and event['data'].get('callId') == question['id']
            and event['seq'] == question['source_seq']]
        digest = hashlib.sha256(json.dumps(question['questions'], sort_keys=True, ensure_ascii=False).encode()).hexdigest()
        if (question.get('session_id') == execution['session_id'] and question.get('generation') == execution['generation']
                and question.get('source_digest') == digest and len(native) == len(calls) == 1
                and isinstance(question.get('event_id'), str) and bool(question['event_id'])
                and not any(event['type'] == 'tool/result'
                    and event['data'].get('message', {}).get('toolCallId') == question['id'] for event in events)):
            matched.append(question['id'])
    if not matched or len(matched) != len(active):
        raise ManagementError('outcome_unknown', 'Original open question ownership is unverified.')


def _take_over_gave_up_claim(intake, record, run_id, configuration, carrier, *, snapshot=None, verify_only=False):
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
    identity = previous.get('identity', {})
    with connect_closing(board=intake.board) as connection:
        old = kb.get_run(connection, previous['run_id'])
    blocked_question = old is not None and old.status == old.outcome == 'blocked'
    if verify_only and not blocked_question:
        raise ManagementError('outcome_unknown', 'Only an original blocked-question worker may be requeued.')
    if blocked_question:
        with connect_closing(board=intake.board) as connection:
            card = kb.get_task(connection, record['card_id'])
        if (not _original_shared_card(intake, record, card) or old.profile != record['target']['native_profile']
                or execution.get('stop_requested') or execution.get('stop_control')
                or execution.get('first_input') != 'accepted' or continuation
                or execution.get('state') not in {'running', 'outcome_unknown'}):
            raise ManagementError('outcome_unknown', 'Original blocked-question supervision scope is unverified.')
        _pending_original_question(execution, snapshot, carrier)
    failure = execution.get('last_failure', {})
    if (execution.get('state') not in {'running', 'outcome_unknown'}
            or execution.get('first_input') != 'accepted'
            or not blocked_question and execution.get('state') == 'outcome_unknown' and failure.get('code') != 'unauthorized'
            and (failure.get('code'), failure.get('message')) not in {
                ('binding_conflict', 'The original shared source changed before supervision takeover.'),
                ('outcome_unknown', 'The original worker input remains unconfirmed; no claim transfer.')}
            or continuation and continuation.get('status') != 'accepted'):
        raise ManagementError('outcome_unknown', 'The original worker input remains unconfirmed; no claim transfer.')
    if (old is None or old.id != previous['run_id'] or old.task_id != record['card_id']
            or old.ended_at is None or old.worker_pid not in {None, identity.get('pid')}
            or not blocked_question and (old.status != 'gave_up' or old.outcome != 'gave_up' or not isinstance(old.error, str)
                or re.fullmatch(r'Iteration budget exhausted \(([1-9]\d*)/\1\) — task could not complete within the allowed iterations', old.error) is None)):
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
    if verify_only:
        return True
    execution.update(state='running', supervisor_claim={'run_id': run_id, 'identity': controller_identity(),
        'exit_log_offset': path.stat().st_size})
    intake._save_execution(record, fields=['state', 'supervisor_claim'])
    return True


def _record_shared_failure(intake, record, error, phase):
    from .manager import _public_text
    message = str(error)
    for secret in intake.secret_values:
        if secret:
            message = message.replace(secret, '[REDACTED_CREDENTIAL]')
    try:
        _public_text(message, intake.secret_values)
    except ManagementError:
        message = 'Original shared failure details remain private (' + type(error).__name__ + ').'
    record['dsh_execution']['last_failure'] = {'phase': phase, 'code': getattr(error, 'code', 'outcome_unknown'),
        'type': type(error).__name__, 'message': message}
    intake._save_execution(record, fields=['last_failure'])


def _same_budget_state_observation(before, after):
    old, new = dict(before), dict(after)
    old_observed, new_observed = old.pop('observed_at_ms', 0), new.pop('observed_at_ms', 0)
    return (old == new and type(old_observed) is int and type(new_observed) is int
        and 0 <= old_observed <= new_observed <= int(time.time() * 1000))


def _verified_background_notice(execution, event, item):
    control = execution.get('stop_control', {})
    end, proof = control.get('session_end', {}), control.get('proof', {})
    session, worker = proof.get('session', {}), proof.get('worker', {})
    source = item.get('source', {})
    return (control.get('status') == 'background_ended'
        and isinstance(item.get('id'), str) and bool(item['id'])
        and control.get('queue_remove', {}).get(item['id']) == 'accepted'
        and set(source) == {'kind', 'form', 'summary'}
        and source.get('kind') == 'tool-jobs' and source.get('form') == 'notice'
        and isinstance(source.get('summary'), str) and bool(source['summary'])
        and session.get('scope') == 'shared_session' and session.get('host') == execution.get('native_identity')
        and all(end.get(key) == session.get(key) == execution.get(key) for key in ('session_id', 'generation'))
        and type(end.get('journal_cursor')) is int and type(event.get('seq')) is int
        and event['seq'] <= end['journal_cursor'] == session.get('journal_cursor')
        and bool(proof.get('repository')) and type(worker.get('exit_code')) is int and worker['exit_code'] == 0
        and bool(worker.get('run_id')) and bool(worker.get('identity')))


def _waiting_repository_successor(intake, record, candidate):
    from hermes_cli import kanban_db as kb
    from hermes_cli.kanban_db_connect import connect_closing
    execution = candidate.get('dsh_execution', {})
    if (execution.get('mode') != 'shared_native'
            or any(execution.get(key) for key in ('generation', 'session_id', 'request_id', 'first_input',
                'native_identity', 'supervisor_claim', 'baseline', 'tool_receipts', 'continuation_control', 'stop_requested'))):
        return False
    with connect_closing(board=intake.board) as connection:
        card = kb.get_task(connection, candidate['card_id'])
        parents = kb.parent_ids(connection, candidate['card_id'])
        runs = kb.list_runs(connection, candidate['card_id'])
    if (not _original_shared_card(intake, candidate, card) or card.status not in {'todo', 'blocked'}
            or card.current_run_id is not None or card.worker_pid is not None
            or record['card_id'] not in parents
            or not set(candidate.get('repository_parents', [])).issubset(parents)):
        return False
    if runs:
        try:
            _unstarted_shared_worker_end(intake, candidate, statuses=frozenset({'todo', 'blocked'}))
        except ManagementError:
            return False
    return True


def _original_shared_card(intake, record, card):
    return (card is not None and card.id == record['card_id']
        and card.assignee == record['target']['native_profile'] and card.workspace_kind == 'dir'
        and Path(card.workspace_path).resolve(strict=True) == Path(record['target']['repo_path']).resolve(strict=True)
        and card.title == record['issue']['title'] and card.body == intake._card_body(record))


async def run_shared_work(ctx, intake, record, generation):
    from .repository_execution import execution_configuration
    from .repository_supervision import current_worker, native_call, owned_call, original_history, execution_event_references
    from .trusted_controller import controller_identity
    from .repository_acceptance import repository_source_state, accept_repository_work, finish_delivery_range, _report
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
    carrier = None
    phase = 'worker_claim'
    descriptor = os.open(intake.lock_path, os.O_RDWR | os.O_NOFOLLOW)
    try:
        lock_deadline = time.monotonic() + SHARED_INTAKE_LOCK_TIMEOUT_SECONDS
        while not _try_shared_intake_lock(intake, descriptor, generation, lock_deadline):
            await asyncio.sleep(.02)
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
        from .queue import logical_repository
        repository_key = logical_repository(record['target']['repo_path'])
        occupied = [row for row in intake.snapshot()['work'] if row['id'] != record['id']
            and row.get('dsh_execution', {}).get('state') not in {None, 'admitted', 'stopped', 'released'}
            and logical_repository(row['target']['repo_path']) == repository_key
            and not _waiting_repository_successor(intake, record, row)]
        if occupied:
            raise ManagementError('repository_busy', 'Earlier work retains this repository.')
        if 'generation' not in execution:
            execution.update(mode='shared_native', state='startup_intent', generation=str(uuid.uuid4()),
                session_id='session-' + uuid.uuid4().hex, request_id='input-' + uuid.uuid4().hex)
            intake._save_execution(record)
        configuration.update(workspace=record['target']['repo_path'], session_id=execution['session_id'],
                             generation=execution['generation'])
        phase = 'session_attach'
        carrier = SharedDesktopTransport(configuration)
        if execution.get('native_identity') and execution['native_identity'] != carrier.native_identity:
            raise ManagementError('binding_conflict', 'Original shared Desktop generation changed.')
        execution['native_identity'] = carrier.native_identity
        phase = 'worker_handoff'
        if not execution.get('supervisor_claim'):
            execution['supervisor_claim'] = {'run_id': run_id, 'identity': controller_identity(),
                'exit_log_offset': kb.worker_log_path(record['card_id'], board=intake.board).stat().st_size}
        elif pending_continuation and execution['supervisor_claim']['run_id'] != run_id:
            from hermes_cli.kanban_db_connect import connect_closing
            previous = continuation['previous_claim']
            checkpoint = continuation if continuation.get('kind') in {'max_tokens', 'renew_window', 'resume_window', 'qa_rework'} else execution['maintenance_checkpoint']
            if (execution['supervisor_claim'] != previous or checkpoint['worker']['run_id'] != previous['run_id']
                    or checkpoint['worker']['identity'] != previous['identity'] or checkpoint['worker']['exit_code'] != 0):
                raise ManagementError('binding_conflict', 'The checkpointed continuation worker claim differs.')
            with connect_closing(board=intake.board) as connection:
                old_run = kb.get_run(connection, previous['run_id'])
            require_permission_initializer_end(previous, old_run, record['card_id'])
            execution['supervisor_claim'] = {'run_id': run_id, 'identity': controller_identity(),
                'exit_log_offset': kb.worker_log_path(record['card_id'], board=intake.board).stat().st_size}
            intake._save_execution(record, fields=['supervisor_claim'])
        elif (execution.get('stop_control', {}).get('status') == 'background_ended'
                and execution.get('stop_control', {}).get('proof', {}).get('worker')
                and execution['supervisor_claim']['run_id'] != run_id):
            control = execution['stop_control']
            previous, worker = execution['supervisor_claim'], control['proof']['worker']
            if worker != {'run_id': previous['run_id'], 'identity': previous['identity'], 'exit_code': 0}:
                raise ManagementError('binding_conflict', 'The original background supervisor exit differs.')
            from hermes_cli.kanban_db_connect import connect_closing
            with connect_closing(board=intake.board) as connection:
                old_run = kb.get_run(connection, previous['run_id'])
            require_permission_initializer_end(previous, old_run, record['card_id'])
            if (await asyncio.to_thread(shared_range_proof, intake, record, control['session_end']) != control['proof']['session']
                    or await asyncio.to_thread(repository_source_state, record['target']) != control['proof']['repository']
                    or not _same_budget_state_observation(execution['shared_budget'], read_ledger(configuration, execution)['state'])):
                raise ManagementError('binding_conflict', 'The completed background scope, source or budget changed before supervision.')
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
        phase = 'session_registration'
        listing = await asyncio.to_thread(owned_call, carrier, 'session/list', {})
        existing = [row for row in listing['items'] if row['sessionId'] == execution['session_id']]
        if existing and (len(existing) != 1 or existing[0].get('cwd') != configuration['workspace']):
            raise ManagementError('binding_conflict', 'Shared Session registration differs.')
        workspace = await asyncio.to_thread(resolve_shared_workspace, intake, record, carrier)
        proof = continuation if continuation.get('kind') in {'max_tokens', 'renew_window', 'resume_window', 'qa_rework'} else execution.get('maintenance_checkpoint', {})
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
        phase = 'history'
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
        _take_over_gave_up_claim(intake, record, run_id, configuration, carrier, snapshot=snapshot)
        phase = 'continuation_checkpoint'
        if pending_continuation:
            budget = read_ledger(configuration, execution)
            if (continuation.get('missing_report') and _source_core(await asyncio.to_thread(
                    repository_source_state, record['target'])) != continuation.get('repository_source')):
                raise ManagementError('binding_conflict', 'The checkpointed missing-report repository source changed.')
            snapshot_matches = (hashlib.sha256(json.dumps(snapshot, sort_keys=True).encode()).hexdigest() == continuation['snapshot_sha256']
                if continuation.get('kind') in {'max_tokens', 'renew_window', 'resume_window', 'qa_rework'} else _maintenance_snapshot_matches(snapshot, execution['maintenance_checkpoint']))
            if continuation.get('kind') == 'max_tokens':
                _require_max_tokens_budget(configuration, budget['state'], budget.get('renewWindow'))
            elif continuation.get('kind') in {'renew_window', 'resume_window', 'qa_rework'}:
                _require_window_budget(configuration, budget)
            jobs = carrier.owned_jobs()
            if (not snapshot_matches
                    or hashlib.sha256(private_file(ledger_path(configuration, execution['session_id'])).read_bytes()).hexdigest()
                    != continuation['ledger_sha256']
                    or continuation.get('kind') not in {'max_tokens', 'renew_window', 'resume_window', 'qa_rework'} and budget['state'] != {'started_at_ms': None, 'requests': 0, 'reported_tokens': 0,
                                           'admissions': [], 'stop_reason': None}
                    or (any(job['status'] in {'running', 'stopping'} for job in jobs)
                        if continuation.get('kind') in {'max_tokens', 'renew_window', 'resume_window', 'qa_rework'} else bool(jobs))):
                raise ManagementError('binding_conflict', 'The admitted pre-model continuation changed before dispatch.')
        if not execution.get('first_input') and any(row.get('event', {}).get('type') in
                {'turn/start', 'user/message', 'assistant/message', 'tool/call'} for row in snapshot.get('records', [])):
            raise ManagementError('binding_conflict', 'The newly owned shared Session has unexpected work.')
        execution.update(session_creation='registered', session_created_at=header['createdAt'])
        await asyncio.to_thread(bind_shared_workspace, intake, record, carrier)
        phase = 'permission'
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
        try:
            _record_shared_failure(intake, record, error, phase)
        except ManagementError:
            pass
        if carrier is not None:
            carrier.close()
        raise
    finally:
        fcntl.flock(descriptor, fcntl.LOCK_UN)
        os.close(descriptor)
    try:
        phase = 'budget'
        window_start = None
        if continuation.get('kind') in {'renew_window', 'resume_window', 'qa_rework'}:
            budget = read_ledger(configuration, execution)
            window_start = (budget.get('renewWindow') or budget['state'])['started_at_ms']
            if (not pending_continuation and execution.get('first_input') == 'accepted'
                    and continuation.get('status') == 'accepted' and type(window_start) is int
                    and _wall_remaining(configuration, window_start) <= 0):
                _require_expired_window_budget(configuration, budget)
                remaining = _wall_remaining(configuration, window_start)
            else:
                remaining = _require_window_budget(configuration, budget)
            deadline = time.monotonic() + remaining + 15
        if continuation.get('kind') == 'max_tokens':
            budget = read_ledger(configuration, execution)
            started_at_ms = (budget.get('renewWindow') or budget['state'])['started_at_ms']
            deadline = time.monotonic() + _wall_remaining(configuration, started_at_ms) + 15
        if not execution.get('first_input') or pending_continuation:
            phase = 'observer'
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
            phase = 'execution_resources'
            await asyncio.to_thread(prepare_shared_github, intake, record)
            execution['verification_prefix'] = prepare_shared_verification(intake, record, configuration)
            intake._save_execution(record, fields=['verification_prefix'])
            phase = 'prompt_preparation'
            if pending_continuation:
                if continuation.get('kind') == 'max_tokens':
                    budget = read_ledger(configuration, execution)
                    _require_max_tokens_budget(configuration, budget['state'], budget.get('renewWindow'))
                elif continuation.get('kind') in {'renew_window', 'resume_window', 'qa_rework'}:
                    _require_window_budget(configuration, read_ledger(configuration, execution))
                request_id = execution['continuation_request_id']
                reason = ('its max-tokens turn boundary' if continuation.get('kind') == 'max_tokens'
                    else 'its pre-model failure and verified maintenance restart')
                prompt = ('Continue the original accepted task in this Session after ' + reason + '. Keep the original scope, frozen Issue, rules and remaining '
                    'budget. Resume that task in this original Session.\n\n' + _prompt(intake, record))
                if continuation.get('kind') == 'qa_rework':
                    original_report = _report([row['event'] for row in snapshot.get('records', [])]) or {}
                    prompt = _qa_rework_prompt(intake, record, continuation.get('qa_feedback', ''),
                        test_files=original_report.get('test_files'))
                    if ('取消30分钟截止，保留80次请求' in continuation.get('qa_feedback', '')
                            and 'max_wall_seconds' not in configuration['budget']):
                        row = read_ledger(configuration, execution)
                        remaining = row['budget']['max_model_requests'] - (
                            row['state']['requests'] - row['renewWindow']['request_base'])
                        prompt = ('Current policy: no plugin wall deadline; ' + str(remaining)
                            + ' model requests remain from the original 80-request allowance. '
                            'The frozen Issue wall, output-token and cumulative-token budget text is historical; '
                            'do not apply those old plugin quotas. Keep the remaining request cap.\n\n' + prompt)
                elif continuation.get('kind') == 'resume_window':
                    prompt = ('Continue the original accepted task in this same Session after verified normal maintenance. '
                        'The original finite execution window is still active: preserve its existing start time, request base, '
                        'cumulative usage and remaining allowance. Do not create or extend any budget window.\n\n' + _prompt(intake, record))
                elif read_ledger(configuration, execution).get('renewWindow'):
                    prompt = ('The owner explicitly authorized one new finite execution window in this same Session. '
                        'Its explicitly configured hard limits are ' + json.dumps(configuration['budget']) + '. It starts at the first actual native request. '
                        'These current instructions override all old budget and token quota text in the frozen Issue: no plugin output-token or cumulative-token quota. '
                        'Native model limits still apply. Preserve the original game scope, history and cumulative usage.\n\n' + _prompt(intake, record))
            else:
                execution['baseline'] = await asyncio.to_thread(repository_source_state, record['target'])
                request_id, prompt = execution['request_id'], _prompt(intake, record)
            if pending_continuation:
                continuation['status'] = 'input_intent'
                intake._save_execution(record, fields=['continuation_control'])
            else:
                execution.update(first_input='intent', state='input_intent')
                intake._save_execution(record)
            phase = 'prompt_admission'
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
        if continuation.get('kind') not in {'max_tokens', 'renew_window', 'resume_window', 'qa_rework'}:
            budget = read_ledger(configuration, execution)
            started_at_ms = budget['state']['started_at_ms']
            remaining = _wall_remaining(configuration, started_at_ms)
            deadline = time.monotonic() + remaining + 15
        observation_deadline = min(deadline, time.monotonic() + SUPERVISE_OBSERVATION_SLICE_SECONDS)
        observed = False
        foreground_review = None
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
            phase = 'history'
            events, projections = await asyncio.to_thread(original_history, carrier, execution['session_id'], configuration['workspace'])
            phase = 'budget'
            budget = read_ledger(configuration, execution)
            active_budget = budget.get('renewWindow') or budget['state']
            if active_budget['started_at_ms'] is not None:
                deadline = time.monotonic() + _wall_remaining(configuration, active_budget['started_at_ms']) + 15
                observation_deadline = min(observation_deadline, deadline)
            if continuation.get('kind') in {'renew_window', 'resume_window', 'qa_rework'} and window_start is None and active_budget['started_at_ms'] is not None:
                window_start = active_budget['started_at_ms']
                deadline = time.monotonic() + _wall_remaining(configuration, window_start) + 15
            execution.update(journal_cursor=events[-1]['seq'] if events else -1, usage=projections.get('tokenUsage'),
                observed_events=execution_event_references(events), shared_budget=budget['state'])
            intake._save_execution(record)
            cursor = (continuation['cursor'] if continuation.get('kind') in {'max_tokens', 'renew_window', 'resume_window', 'qa_rework'}
                else execution['maintenance_checkpoint']['cursor']) if continuation else -1
            if execution.get('owner_context_control', {}).get('status') == 'accepted':
                cursor = execution['owner_context_control']['cursor']
                submitted = {execution.get('request_id'), execution.get('continuation_request_id'),
                    execution['owner_context_control'].get('request_id')}
                submitted.update(control.get('request_id') for control in execution.get('owner_context_history', []))
                submitted.update(question.get('request_id') for question in execution.get('questions', []))
                submitted.discard(None)
                if any((item.get('source', {}).get('kind') != 'user' or item.get('source', {}).get('rpcId') not in submitted)
                        and not _verified_background_notice(execution, event, item)
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
                reason = ends[-1]['data'].get('reason', {})
                active_jobs = any(job['status'] in {'running', 'stopping'} for job in jobs)
                if active_jobs:
                    registered = {receipt['job_id'] for receipt in shared_receipts(configuration, execution, '.jobs.jsonl')
                        if receipt['type'] == 'registered' and receipt.get('owner') == execution['session_id']}
                    if reason.get('kind') != 'completed' or any(job['id'] not in registered for job in jobs):
                        raise ManagementError('outcome_unknown', 'Original active background job coverage is unconfirmed.')
                stop_reason = (budget.get('renewWindow') or execution['shared_budget']).get('stop_reason')
                state = 'awaiting_acceptance' if reason.get('kind') == 'completed' else 'budget_stopped' if stop_reason else 'execution_failed'
                execution.update(state=state, terminal_reason=reason, jobs=[{'id': job['id'], 'status': job['status']} for job in jobs],
                    tool_receipts=shared_receipts(configuration, execution, '.tools.jsonl'))
                intake._save_execution(record)
                if state == 'awaiting_acceptance':
                    phase = 'acceptance'
                    review = (events[-1]['seq'], tuple((job['id'], job['status']) for job in jobs))
                    if not active_jobs or review != foreground_review:
                        result = await accept_repository_work(intake, record, carrier, events, projections, generation,
                            current_budget=_active_budget_snapshot(configuration, budget))
                        foreground_review = review
                    if not active_jobs:
                        if result.get('status') == 'accepted':
                            await finish_delivery_range(intake, record, carrier, generation)
                            await asyncio.to_thread(cleanup_shared_github, intake, record)
                        return result
                else:
                    return {'status': state, 'next_action': 'kanban_block', 'block_kind': 'needs_input', 'delivered': False}
            checkpoint = supervision_checkpoint(intake, record, configuration, budget, events)
            if checkpoint is not None:
                return {'status': 'review_required', 'next_action': 'hermes_pm_supervise',
                    'checkpoint': checkpoint, 'delivered': False,
                    'instruction': 'Summarize the verified checkpoint and decide whether immediate owner feedback is needed. '
                        'Use checkpoint budget, pending counts and acceptance_status as the authoritative current facts. '
                        'Limits absent from budget.limits are not configured; no plugin token quota applies. '
                        'Progress with from_current_turn=false is historical reference, not this turn\'s acceptance or active preview evidence. '
                        'A running turn is unverified; do not reuse old report prose as current acceptance. '
                        'Explain progress, blockers and next steps only; the deterministic header supplies budget and pending counts. '
                        'Do not restate or guess those numbers or infer an active preview without current-turn evidence. '
                        'Call this same tool with checkpoint_id, summary and notify_owner. Do not stop DSH, renew a budget or send another prompt.'}
            await asyncio.sleep(.25)
        if time.monotonic() < deadline:
            return {'status': 'running', 'next_action': 'hermes_pm_supervise', 'delivered': False}
        raise ManagementError('outcome_unknown', 'Original shared execution has no confirmed terminal boundary.')
    except (ManagementError, OSError, TimeoutError, ValueError, KeyError) as error:
        execution['state'] = 'outcome_unknown'
        intake._save_execution(record, fields=['state'])
        _record_shared_failure(intake, record, error, phase)
        if isinstance(error, ManagementError):
            raise
        raise ManagementError('outcome_unknown', 'Original shared execution remains unconfirmed.') from None
    finally:
        if carrier is not None:
            carrier.close()


def _terminal_shared_job_notices(carrier, execution, inbox, jobs):
    receipts = shared_receipts(carrier.config, execution, '.tools.jsonl')
    background = {row.get('job_id') for row in receipts if row.get('name') == 'bash'
        and row.get('kind') in {'background', 'promoted'} and row.get('execution_admission') == 'admitted'}
    registered = {row['job_id'] for row in shared_receipts(carrier.config, execution, '.jobs.jsonl')
        if row['type'] == 'registered' and row.get('owner') == execution['session_id']}
    return (isinstance(inbox, dict) and set(inbox) == {'next-turn', 'next-step'}
        and inbox['next-turn'] == [] and isinstance(inbox['next-step'], list) and bool(inbox['next-step'])
        and bool(jobs) and all(job['status'] in {'completed', 'killed', 'failed'}
            and job['id'] in registered and job['id'] in background for job in jobs)
        and all(isinstance(item, dict) and item.get('role') == 'user'
            and isinstance(item.get('id'), str) and bool(item['id']) and isinstance(item.get('source'), dict)
            and set(item['source']) == {'kind', 'form', 'summary'}
            and item['source'].get('kind') == 'tool-jobs' and item['source'].get('form') == 'notice'
            and isinstance(item['source'].get('summary'), str) and bool(item['source']['summary'])
            for item in inbox['next-step']))


async def end_shared_background(intake, record, message_id, generation):
    """End registered background jobs after foreground completion; retain the work."""
    from .repository_control import _card, _worker_end
    from .repository_supervision import owned_call, original_history
    from .repository_acceptance import repository_source_state
    from .trusted_controller import controller_identity
    execution = record['dsh_execution']
    if (execution.get('mode') != 'shared_native' or execution.get('first_input') != 'accepted'
            or execution.get('stop_requested') or execution.get('lifecycle_end')
            or execution.get('state') not in {'running', 'awaiting_acceptance', 'outcome_unknown'}):
        raise ManagementError('outcome_unknown', 'Only the original completed work may end its background jobs.')
    card = _card(intake, record)
    claim = execution.get('supervisor_claim', {})
    worker = None
    if card['status'] == 'running':
        from hermes_cli import kanban_db as kb
        from hermes_cli.kanban_db_connect import connect_closing
        with connect_closing(board=intake.board) as connection:
            active = kb.get_task(connection, record['card_id'])
        if (active is None or active.status != 'running' or active.current_run_id != claim.get('run_id')
                or active.worker_pid != claim.get('identity', {}).get('pid')
                or controller_identity(active.worker_pid) != claim['identity']):
            raise ManagementError('binding_conflict', 'The original background supervisor claim changed.')
    else:
        worker = _worker_end(intake, record)
    carrier = shared_attach(intake, record, observe=False)
    try:
        budget = read_ledger(carrier.config, execution)
        control = execution.get('stop_control', {})
        if control.get('status') == 'background_ended':
            if (shared_range_proof(intake, record, control['session_end']) != control['proof']['session']
                    or await asyncio.to_thread(repository_source_state, record['target']) != control['proof']['repository']
                    or not _same_budget_state_observation(execution['shared_budget'], budget['state'])):
                raise ManagementError('outcome_unknown', 'The original background cleanup proof changed.')
            if worker is not None:
                if execution.get('continuation_control') and execution['continuation_control'].get('status') != 'accepted':
                    raise ManagementError('outcome_unknown', 'The original background continuation remains pending.')
                control['proof']['worker'] = worker
                execution.update(state='awaiting_acceptance', shared_budget=budget['state'])
                intake._save_execution(record, fields=['stop_control', 'state', 'shared_budget'])
                if card['status'] == 'triage':
                    from hermes_cli import kanban_db as kb
                    from hermes_cli.kanban_db_connect import connect_closing
                    with connect_closing(board=intake.board) as connection:
                        if not kb.specify_triage_task(connection, record['card_id'], author='verified-owner'):
                            raise ManagementError('outcome_unknown', 'Original background supervisor dispatch is unconfirmed.')
                else:
                    intake._kanban('kanban_unblock', {'task_id': record['card_id']})
            return record
        deadline = time.monotonic() + 30
        while time.monotonic() < deadline:
            intake.require_active(generation)
            listing = await asyncio.to_thread(owned_call, carrier, 'session/list', {})
            sessions = [row for row in listing['items'] if row['sessionId'] == execution['session_id']]
            events, projections = await asyncio.to_thread(original_history, carrier, execution['session_id'], record['target']['repo_path'])
            starts = [event for event in events if event['type'] == 'turn/start']
            ends = [event for event in events if event['type'] == 'turn/end']
            current_budget = read_ledger(carrier.config, execution)
            if (len(sessions) != 1 or sessions[0].get('cwd') != record['target']['repo_path']
                    or sessions[0].get('running') is not False or sessions[0].get('agentAvailable') is not True
                    or not starts or not ends or ends[-1]['seq'] <= starts[-1]['seq']
                    or ends[-1]['data'].get('reason') != {'kind': 'completed'}
                    or not _same_budget_state_observation(budget['state'], current_budget['state'])
                    or {**budget, 'state': current_budget['state']} != current_budget):
                raise ManagementError('outcome_unknown', 'The completed original foreground, input or budget changed.')
            budget = current_budget
            jobs = await asyncio.to_thread(carrier.owned_jobs)
            receipts = shared_receipts(carrier.config, execution, '.tools.jsonl')
            background = {row.get('job_id') for row in receipts if row.get('name') == 'bash'
                and row.get('kind') in {'background', 'promoted'} and row.get('execution_admission') == 'admitted'}
            registered = {row['job_id'] for row in shared_receipts(carrier.config, execution, '.jobs.jsonl')
                if row['type'] == 'registered' and row.get('owner') == execution['session_id']}
            inbox = projections.get('inbox')
            if inbox != {'next-turn': [], 'next-step': []}:
                if not _terminal_shared_job_notices(carrier, execution, inbox, jobs):
                    raise ManagementError('outcome_unknown', 'Foreign or unconfirmed input in the shared Session was preserved.')
                execution['stop_control'] = control
                for item in inbox['next-step']:
                    removed = control.setdefault('queue_remove', {})
                    if removed.get(item['id']) == 'intent':
                        raise ManagementError('outcome_unknown', 'Original job notice removal is unconfirmed; no replay.')
                    if item['id'] not in removed:
                        removed[item['id']] = 'intent'
                        intake._save_execution(record, fields=['stop_control'])
                        answer = await asyncio.to_thread(owned_call, carrier, 'session/updateQueue',
                            {'sessionId': execution['session_id'], 'itemId': item['id'], 'action': {'kind': 'remove'}})
                        if answer.get('accepted') is not True:
                            raise ManagementError('outcome_unknown', 'Original job notice removal admission is unknown.')
                        removed[item['id']] = 'accepted'
                        intake._save_execution(record, fields=['stop_control'])
                continue
            active = [job for job in jobs if job['status'] in {'running', 'stopping'}]
            if any(job['id'] not in registered or job['id'] not in background for job in active):
                raise ManagementError('coverage_unverified', 'An unregistered original background job was preserved.')
            control.update(message_id=message_id, prior_state=execution['state'], status='intent')
            execution['stop_control'] = control
            intake._save_execution(record, fields=['stop_control'])
            for job in active:
                cancelled = control.setdefault('job_cancel', {})
                if cancelled.get(job['id']) == 'intent':
                    raise ManagementError('outcome_unknown', 'Original background cancellation is unconfirmed; no replay.')
                if job['id'] not in cancelled:
                    cancelled[job['id']] = 'intent'
                    intake._save_execution(record, fields=['stop_control'])
                    answer = await asyncio.to_thread(owned_call, carrier, 'job/kill',
                        {'sessionId': execution['session_id'], 'jobId': job['id']})
                    if answer.get('outcome') not in {'requested', 'already-finished'}:
                        raise ManagementError('outcome_unknown', 'Owned background cancellation admission is unknown.')
                    cancelled[job['id']] = 'accepted'
                    intake._save_execution(record, fields=['stop_control'])
            if not active:
                end = {'session_id': execution['session_id'], 'generation': execution['generation'],
                    'journal_cursor': events[-1]['seq'],
                    'tool_calls': [{'id': event['data']['callId'], 'name': event['data']['name']}
                        for event in events if event['type'] == 'tool/call'],
                    'jobs': [{'id': job['id'], 'status': job['status']} for job in jobs]}
                proof = await asyncio.to_thread(shared_range_proof, intake, record, end)
                source = await asyncio.to_thread(repository_source_state, record['target'])
                control.update(status='background_ended', session_end=end, proof={'session': proof, 'repository': source})
                if worker is not None:
                    control['proof']['worker'] = worker
                execution.update(state='awaiting_acceptance', terminal_reason={'kind': 'completed'},
                    jobs=end['jobs'], tool_receipts=receipts, shared_budget=budget['state'])
                intake._save_execution(record, fields=['stop_control', 'state', 'terminal_reason', 'jobs', 'tool_receipts', 'shared_budget'])
                if worker is not None:
                    if card['status'] == 'triage':
                        from hermes_cli import kanban_db as kb
                        from hermes_cli.kanban_db_connect import connect_closing
                        with connect_closing(board=intake.board) as connection:
                            if not kb.specify_triage_task(connection, record['card_id'], author='verified-owner'):
                                raise ManagementError('outcome_unknown', 'Original background supervisor dispatch is unconfirmed.')
                    else:
                        intake._kanban('kanban_unblock', {'task_id': record['card_id']})
                return record
            await asyncio.sleep(.05)
        raise ManagementError('outcome_unknown', 'Original background termination remains unconfirmed.')
    finally:
        carrier.close()


def _unstarted_shared_worker_end(intake, record, *, statuses=frozenset({'blocked', 'triage'})):
    """Close only a native worker which ended before any DSH creation intent."""
    import psutil
    from hermes_cli import kanban_db as kb
    from hermes_cli.kanban_db_connect import connect_closing
    from gateway.drain_control import current_instantiation_epoch
    from gateway.status import get_process_start_time
    execution = record['dsh_execution']
    if (execution.get('mode') != 'shared_native' or any(execution.get(key) for key in
            ('generation', 'session_id', 'request_id', 'first_input', 'native_identity', 'supervisor_claim',
             'baseline', 'workspace_id', 'session_created_at', 'github_reference'))):
        raise ManagementError('outcome_unknown', 'Original pre-input worker scope is unverified.')
    with connect_closing(board=intake.board) as connection:
        card = kb.get_task(connection, record['card_id'])
        runs = kb.list_runs(connection, record['card_id'])
        events = kb.list_events(connection, record['card_id'])
    if (not _original_shared_card(intake, record, card) or card.status not in statuses or card.current_run_id is not None
            or card.worker_pid is not None or len(runs) != 1 or runs[0].ended_at is None
            or runs[0].status == 'running' or runs[0].profile != record['target']['native_profile']):
        raise ManagementError('outcome_unknown', 'Original pre-input worker end is unverified.')
    spawned = [event.payload for event in events if event.run_id == runs[0].id and event.kind == 'spawned']
    if (len(spawned) != 1 or type(spawned[0].get('pid')) is not int or spawned[0]['pid'] <= 0
            or not isinstance(spawned[0].get('started_at'), str) or '|' not in spawned[0]['started_at']
            or not spawned[0]['started_at'].split('|', 1)[1]):
        raise ManagementError('outcome_unknown', 'Original pre-input worker birth is unavailable.')
    identity = spawned[0]
    if psutil.pid_exists(identity['pid']):
        current = get_process_start_time(identity['pid'])
        if current is None or f'{current_instantiation_epoch()}|{current}' == identity['started_at']:
            raise ManagementError('outcome_unknown', 'Original pre-input worker process remains unconfirmed.')
    text = kb.read_worker_log(card.id, tail_bytes=4000, board=intake.board)
    if not isinstance(text, str) or re.findall(r'^\[kanban-worker-exit\] rc=(-?\d+)\s*$', text, re.MULTILINE) != ['0']:
        raise ManagementError('outcome_unknown', 'Original pre-input worker has no unique successful native exit.')
    return {'run_id': runs[0].id, 'identity': identity, 'exit_code': 0, 'not_started': True}


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
                jobs = await asyncio.to_thread(carrier.owned_jobs)
                notices = [item for item in inbox['next-step'] if item.get('source', {}).get('kind') == 'tool-jobs']
                if (any(item.get('source', {}).get('kind') != 'user'
                        or item.get('source', {}).get('rpcId') not in submitted
                        for item in [*inbox['next-turn'], *inbox['next-step']] if item not in notices)
                        or notices and not _terminal_shared_job_notices(carrier, execution,
                            {'next-turn': [], 'next-step': notices}, jobs)):
                    raise ManagementError('outcome_unknown', 'Foreign input in the shared Session was preserved.')
                for item in [*inbox['next-turn'], *inbox['next-step']]:
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
                if not execution.get('generation') and not execution.get('session_id'):
                    worker = _unstarted_shared_worker_end(intake, record)
                    break
            await asyncio.sleep(.05)
        else:
            raise ManagementError('outcome_unknown', 'The original outer worker exit is unconfirmed.')
        source = await asyncio.to_thread(repository_source_state, record['target'])
        await asyncio.to_thread(cleanup_shared_github, intake, record)
        descriptor = os.open(intake.lock_path, os.O_RDWR | os.O_NOFOLLOW)
        try:
            lock_deadline = time.monotonic() + SHARED_INTAKE_LOCK_TIMEOUT_SECONDS
            while not _try_shared_intake_lock(intake, descriptor, generation, lock_deadline):
                await asyncio.sleep(.02)
            from hermes_cli import kanban_db as kb
            from hermes_cli.kanban_db_connect import connect_closing
            with connect_closing(board=intake.board) as connection:
                card = kb.get_task(connection, record['card_id'])
                if card is None or card.current_run_id is not None or card.worker_pid is not None or card.status not in {'blocked', 'triage'}:
                    raise ManagementError('outcome_unknown', 'The stopped original outer card changed before archival.')
                if not kb.archive_task(connection, record['card_id']):
                    raise ManagementError('outcome_unknown', 'Original stopped outer card archival is unconfirmed.')
            control.update(status='stopped', proof={'session': proof, 'worker': worker, 'repository': source})
            execution.update(state='stopped', stop_control=control)
            intake._save_execution(record, fields=['state', 'stop_control'])
        finally:
            fcntl.flock(descriptor, fcntl.LOCK_UN)
            os.close(descriptor)
        return record
    finally:
        if carrier is not None:
            carrier.close()
