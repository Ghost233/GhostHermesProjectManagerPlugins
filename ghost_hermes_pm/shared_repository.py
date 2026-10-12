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
import time
import tempfile
import uuid

from .manager import ManagementError, _private_state_directory
from .repository_execution import private_file
from .shared_desktop import SharedDesktopTransport, desktop_identity


TOOLS = ['ask_user_question', 'bash', 'job_kill', 'job_list', 'job_output', 'skill']
CASES = {'allowed_write', 'outside_write_denied', 'symlink_write_denied', 'hardlink_write_denied',
         'danger_denied', 'foreground_job_registered', 'managed_background_stopped',
         'foreground_cancelled', 'foreign_job_kill_denied', 'background_write_boundary'}
CAPS = {'max_wall_seconds': 1800, 'max_model_requests': 80, 'max_reported_tokens': 300000,
        'max_output_tokens_per_request': 8192}


def shared_configuration(reference, configuration):
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
                or hashlib.sha256(path.read_bytes()).hexdigest() != source[name + '_sha256']):
            raise ManagementError('configuration_missing', 'The fixed source fingerprint entry changed.')
    if source['reader_sha256'] != hashlib.sha256(Path(__file__).with_name('shared_source_reader.py').read_bytes()).hexdigest():
        raise ManagementError('configuration_missing', 'The shared source reader must be this reviewed main plugin entry.')
    verify_configuration(configuration)
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
                runtime_configuration={'budget': budget})


def ledger_path(configuration, session_id, suffix='.json'):
    return Path(configuration['ledger_dir']) / (hashlib.sha256(session_id.encode()).hexdigest() + suffix)


def read_ledger(configuration, execution):
    row = json.loads(private_file(ledger_path(configuration, execution['session_id'])).read_text())
    expected = {'schema': 1, 'session_id': execution['session_id'], 'generation': execution['generation'],
                'session_created_at': execution['session_created_at'], 'workspace': configuration['workspace']}
    expected.update({key: configuration[key] for key in ('host_lease', 'service_ref', 'agent_preset', 'provider', 'model', 'budget')})
    if any(row.get(key) != value for key, value in expected.items()):
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
    rows = [json.loads(line) for line in path.read_text().splitlines()]
    expected = {'schema': 1, 'generation': execution['generation'], 'session_id': execution['session_id'],
        'session_created_at': execution['session_created_at'], 'service_ref': configuration['service_ref'],
        'host_lease': configuration['host_lease']}
    if any(any(row.get(key) != value for key, value in expected.items()) for row in rows):
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


def verify_configuration(configuration):
    verification = configuration.get('verification')
    if not isinstance(verification, dict) or set(verification) != {'python', 'runner', 'node', 'python_sha256', 'runner_sha256', 'node_sha256'}:
        raise ManagementError('configuration_missing', 'The fixed shared test entry is required.')
    for name in ('python', 'runner', 'node'):
        path = Path(verification[name])
        if (not path.is_absolute() or str(path.resolve(strict=True)) != str(path)
            or hashlib.sha256(path.read_bytes()).hexdigest() != verification[name + '_sha256']):
            raise ManagementError('configuration_missing', 'An original fixed shared test executable changed.')
    if verification['runner_sha256'] != hashlib.sha256(Path(__file__).with_name('repository_test_runner.py').read_bytes()).hexdigest():
        raise ManagementError('configuration_missing', 'The shared test entry must be this reviewed original runner.')
    return verification


def verification_command(configuration, execution, workspace, test_files):
    verification = verify_configuration(configuration)
    if (not isinstance(test_files, list) or not test_files or len(test_files) != len(set(test_files))
        or any(not isinstance(path, str) or path.startswith('-') or Path(path).is_absolute()
            or not (Path(workspace) / path).resolve(strict=True).is_relative_to(Path(workspace))
            or not (Path(workspace) / path).is_file() for path in test_files)):
        raise ManagementError('test_evidence_missing', 'Actual explicit in-repository test files are required.')
    work = execution['verification']
    return shlex.join([verification['python'], '-I', verification['runner'], workspace, work['artifact_root'],
        '--node', verification['node'], '--binding', execution['session_id'], execution['generation'], work['nonce'], '--', *test_files])


def prepare_shared_verification(intake, record, configuration):
    verification = verify_configuration(configuration)
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
    if execution.get('stop_requested') or execution['state'] in {'budget_stopped', 'execution_failed', 'stopped'}:
        return {'status': execution['state'], 'next_action': 'kanban_block', 'block_kind': 'needs_input', 'delivered': False}
    descriptor = os.open(intake.lock_path, os.O_RDWR | os.O_NOFOLLOW)
    await asyncio.to_thread(fcntl.flock, descriptor, fcntl.LOCK_EX)
    carrier = None
    try:
        intake.require_active(generation)
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
        listing = await asyncio.to_thread(owned_call, carrier, 'session/list', {})
        existing = [row for row in listing['items'] if row['sessionId'] == execution['session_id']]
        if existing and (len(existing) != 1 or existing[0].get('cwd') != configuration['workspace']):
            raise ManagementError('binding_conflict', 'Shared Session registration differs.')
        workspace = await asyncio.to_thread(resolve_shared_workspace, intake, record, carrier)
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
        if not execution.get('first_input'):
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
            await asyncio.to_thread(prepare_shared_github, intake, record)
            execution['verification_prefix'] = prepare_shared_verification(intake, record, configuration)
            intake._save_execution(record, fields=['verification_prefix'])
            execution['baseline'] = await asyncio.to_thread(repository_source_state, record['target'])
            execution.update(first_input='intent', state='input_intent')
            intake._save_execution(record)
            answer = await asyncio.to_thread(owned_call, carrier, 'session/prompt',
                {'sessionId': execution['session_id'], 'requestId': execution['request_id'], 'mode': 'queue',
                 'content': [{'type': 'text', 'text': _prompt(intake, record)}]})
            if answer.get('accepted') is not True:
                raise ManagementError('outcome_unknown', 'Original shared first input was not confirmed.')
            execution.update(first_input='accepted', state='running')
            intake._save_execution(record)
        elif execution['first_input'] != 'accepted':
            raise ManagementError('outcome_unknown', 'Original shared input needs reconciliation; no replay.')
        heartbeat_at = 0
        deadline = time.monotonic() + configuration['budget']['max_wall_seconds'] + 15
        while time.monotonic() < deadline:
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
            execution.update(journal_cursor=events[-1]['seq'] if events else -1, usage=projections.get('tokenUsage'),
                observed_events=execution_event_references(events), shared_budget=read_ledger(configuration, execution)['state'])
            intake._save_execution(record)
            starts = [event for event in events if event['type'] == 'turn/start']
            ends = [event for event in events if event['type'] == 'turn/end']
            if row and row.get('running') is False and starts and ends and ends[-1]['seq'] > starts[-1]['seq']:
                if projections.get('inbox') != {'next-turn': [], 'next-step': []}:
                    raise ManagementError('outcome_unknown', 'The shared Session retains pending input.')
                jobs = await asyncio.to_thread(carrier.owned_jobs)
                if any(job['status'] in {'running', 'stopping'} for job in jobs):
                    raise ManagementError('outcome_unknown', 'Owned shared background jobs remain active.')
                reason = ends[-1]['data'].get('reason', {})
                state = 'awaiting_acceptance' if reason.get('kind') == 'completed' else 'budget_stopped' if execution['shared_budget'].get('stop_reason') else 'execution_failed'
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
                submitted = {execution.get('request_id')}
                submitted.update(question.get('request_id') for question in execution.get('questions', []))
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
