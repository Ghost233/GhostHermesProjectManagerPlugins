"""Validate host-owned, hashed capability receipts; text verdicts are insufficient."""
import hashlib
import json
from pathlib import Path

from .manager import ManagementError
from .dsh import repository_fingerprint


KINDS = ('platform_enforcement', 'tool_paths', 'task_start', 'manual_execution_coverage')
CONTROL_METHODS = {'session/projections', 'session/follow', 'session/prompt', 'session/cancel'}
CONTROL_CHECKS = {'active_append', 'idle_input', 'interrupt', 'stop_verification', 'explicit_continue', 'wrong_turn',
                  'duplicate_instruction', 'disconnect', 'background_pagination', 'related_children', 'exclusive_input'}
ALLOWED = {'mono_source_write', 'mono_git_index', 'mono_git_commit', 'mono_gitlink', 'test_artifact_write'}
DENIED = {'child_source_write', 'child_git_write', 'child_root_rename', 'ancestor_rename', 'atomic_replace',
          'symlink_alias', 'preexisting_hardlink_alias', 'new_hardlink_alias', 'unregistered_path_write',
          'test_source_write', 'test_git_write', 'descendant_process_escape'}
TOOLS = {'model_files', 'shell_git', 'test_process', 'code_mode', 'local_mcp', 'dynamic_tools',
         'filesystem_rpc', 'process_spawn', 'thread_shell'}


def validate_receipts(report, connection, repository, configuration, state_dir, *, startup_kind='task_start'):
    if startup_kind not in {'task_start', 'manual_takeover', 'recovery'}:
        raise ManagementError('capability_unverified', 'Unknown execution evidence kind.')
    kinds = tuple(startup_kind if k == 'task_start' else k for k in KINDS)
    receipts = report.get('receipts') if isinstance(report, dict) else None
    if not isinstance(receipts, dict) or not set(kinds).issubset(receipts) or set(receipts) - set(kinds) - {'task_control', 'human_response'}:
        raise ManagementError('capability_unverified', 'Hashed current-service enforcement, tool, startup and executor-coverage receipts are required.')
    from .observation import fresh_report
    fresh_report(report)
    if connection.get('engine') != 'dsh' or not isinstance(configuration, dict) or not isinstance(configuration.get('base_url'), str):
        raise ManagementError('capability_unverified', 'Only registered DSH backend connection receipts are supported.')
    configuration_digest = hashlib.sha256(json.dumps(configuration, sort_keys=True).encode()).hexdigest()
    endpoint_digest = hashlib.sha256(configuration['base_url'].encode()).hexdigest()
    expected = {'engine': 'dsh', 'generation': connection['generation'], 'service_id': connection['service_id'],
                'repository_fingerprint': repository_fingerprint(repository), 'policy_digest': report.get('policy_digest'),
                'platform': connection['platform'], 'endpoint_sha256': endpoint_digest, 'configuration_sha256': configuration_digest}
    if connection.get('client_id'):
        expected['client_id'] = connection['client_id']
    if any(report.get(k) != v for k, v in expected.items()) or not isinstance(report.get('permission_profile'), str) or not report['permission_profile'].startswith('host:') or not isinstance(report.get('policy_digest'), str) or not report['policy_digest'] or report.get('runtime_roots') != [repository['worktree']]:
        raise ManagementError('capability_unverified', 'DSH requires a current external host enforcement policy; native permission-profile echoes do not prove a write boundary.')
    verified = {}
    evidence_root = (Path(state_dir) / 'validation-evidence').resolve()
    control = None
    human_response = None
    startup_input = None
    for kind in (*kinds, *(k for k in ('task_control', 'human_response') if k in receipts)):
        reference = receipts[kind]
        if not isinstance(reference, dict) or set(reference) != {'path', 'sha256'}:
            raise ManagementError('capability_unverified', 'Each validation receipt needs a fixed local digest.')
        path = Path(reference['path'])
        if path != path.resolve() or not path.is_relative_to(evidence_root) or not path.is_file() or path.stat().st_size > 1024 * 1024:
            raise ManagementError('capability_unverified', 'Validation receipts must be bounded host-owned regular files without aliases.')
        raw = path.read_bytes()
        if hashlib.sha256(raw).hexdigest() != reference['sha256']:
            raise ManagementError('capability_unverified', 'A validation receipt changed after verification.')
        try:
            receipt = json.loads(raw)
        except ValueError as exc:
            raise ManagementError('capability_unverified', 'A validation receipt is malformed.') from exc
        if not isinstance(receipt, dict) or receipt.get('kind') != kind or receipt.get('result') != 'PASS' or any(receipt.get(k) != v for k, v in expected.items()):
            raise ManagementError('capability_unverified', 'A receipt failed or belongs to another binary, configuration, platform, repository or service generation.')
        if kind == 'platform_enforcement':
            checks = receipt.get('checks', [])
            if not isinstance(checks, list) or {c.get('operation') for c in checks if isinstance(c, dict)} != ALLOWED | DENIED:
                raise ManagementError('capability_unverified', 'The filesystem operation matrix is incomplete.')
            for check in checks:
                allowed = check['operation'] in ALLOWED
                if check.get('outcome') != ('allowed' if allowed else 'denied') or (not allowed and (not check.get('before_sha256') or check.get('before_sha256') != check.get('after_sha256'))):
                    raise ManagementError('capability_unverified', 'Filesystem enforcement did not preserve every protected boundary.')
        elif kind == 'tool_paths':
            paths = receipt.get('paths', {})
            if not isinstance(paths, dict) or set(paths) != TOOLS or any(v not in {'enforced', 'disabled'} for v in paths.values()):
                raise ManagementError('capability_unverified', 'A tool write path is unverified or could bypass the task policy.')
        elif kind == 'task_start':
            if receipt.get('actual_methods') != ['session/create', 'session/prompt', 'session/projections', 'session/follow'] or receipt.get('runtime_roots') != report.get('runtime_roots') or receipt.get('host_policy_ref') != report.get('permission_profile') or not receipt.get('thread_id') or not receipt.get('turn_id') or receipt.get('native_turn_id_source') != 'follow_event':
                raise ManagementError('capability_unverified', 'Actual controlled task-start receipts are incomplete.')
            from .dsh import validate_exclusive_input
            startup_input = receipt.get('exclusive_input')
            if not isinstance(startup_input, dict) or startup_input.get('scope') != 'create_and_first_input' or not {'thread_id', 'turn_id'} <= set(startup_input):
                raise ManagementError('capability_unverified', 'DSH startup requires one external exclusive create-and-first-input lease.')
            validate_exclusive_input({'exclusive_input': startup_input}, connection, repository, None, None)
        elif kind == 'manual_takeover':
            context = report.get('grant_binding', {})
            checks = {'owner_only', 'original_executor', 'same_thread', 'single_controller', 'scope_bound', 'return_running', 'completion_expiry', 'wrong_turn', 'foreign_service', 'unsupported_scope', 'no_new_thread', 'no_replayed_approval', 'original_request_routing', 'exclusive_input'}
            binding = {k: v for k, v in context.items() if k != 'current_turn_id'}
            if receipt.get('actual_methods') != ['session/list', 'session/projections', 'session/follow'] or receipt.get('grant_binding') != binding or receipt.get('thread_id') != context.get('thread_id') or receipt.get('turn_id') != context.get('original_turn_id') or receipt.get('runtime_roots') != report.get('runtime_roots') or receipt.get('host_policy_ref') != report.get('permission_profile') or receipt.get('control_access') != 'verified-original-input-path' or not isinstance(receipt.get('checks'), dict) or set(receipt['checks']) != checks or any(v != 'PASS' for v in receipt['checks'].values()):
                raise ManagementError('capability_unverified', 'Actual original current-work takeover, return, scope and no-replay evidence is incomplete.')
        elif kind == 'recovery':
            context = report.get('recovery_binding', {})
            checks = {'durable_intent_first', 'original_executor', 'same_thread', 'occupancy_preserved', 'running_observation_only',
                'incomplete_only', 'explicit_stop', 'archive_intent', 'unknown_start', 'unknown_append', 'unknown_answer',
                'live_rpc_only', 'manual_return', 'disconnect', 'persistent_restart', 'no_duplicate_execution', 'no_duplicate_reply'}
            if receipt.get('actual_methods') != ['session/list', 'session/projections', 'session/follow'] or receipt.get('recovery_binding') != context or receipt.get('thread_id') != context.get('thread_id') or receipt.get('turn_id') != context.get('turn_id') or receipt.get('original_executor_id') != connection['service_id'] or receipt.get('endpoint_ref') != report.get('endpoint_ref') or receipt.get('runtime_roots') != report.get('runtime_roots') or receipt.get('host_policy_ref') != report.get('permission_profile') or receipt.get('control_access') != 'verified-original-input-path' or not isinstance(receipt.get('checks'), dict) or set(receipt['checks']) != checks or any(v != 'PASS' for v in receipt['checks'].values()):
                raise ManagementError('capability_unverified', 'Original-service restart, stop intent, no-replay, manual return and occupancy evidence is incomplete.')
        elif kind == 'task_control':
            methods, checks = receipt.get('actual_methods'), receipt.get('checks')
            if not isinstance(methods, list) or any(not isinstance(m, str) for m in methods) or set(methods) != CONTROL_METHODS or not isinstance(checks, dict) or set(checks) != CONTROL_CHECKS or any(v != 'PASS' for v in checks.values()) or not receipt.get('thread_id') or not receipt.get('turn_id') or not receipt.get('new_turn_id') or receipt['turn_id'] == receipt['new_turn_id']:
                raise ManagementError('capability_unverified', 'Actual task control, exclusive idle input and related-execution coverage are incomplete.')
            control = receipt
        elif kind == 'human_response':
            methods = {'session/follow', 'user-questions/request', '$events/result'}
            checks = {'question', 'nonblocking', 'unsupported_approval', 'owner_only', 'wrong_request', 'duplicate', 'resolved_race', 'disconnect', 'secret', 'unknown_no_replay'}
            if set(receipt.get('actual_methods', [])) != methods or not isinstance(receipt.get('checks'), dict) or set(receipt['checks']) != checks or any(v != 'PASS' for v in receipt['checks'].values()) or not receipt.get('thread_id') or not receipt.get('turn_id') or receipt.get('original_connection_responses') is not True:
                raise ManagementError('capability_unverified', 'Actual original-connection human response and race evidence is incomplete.')
            human_response = receipt
        elif receipt.get('registered_executors_complete') is not True or receipt.get('competing_execution') != 'none':
            raise ManagementError('capability_unverified', 'Other execution in this logical repository cannot be excluded.')
        verified[kind] = reference['sha256']
    # Persist only necessary provenance; no full configuration or raw test outputs.
    result = {k: report[k] for k in ('generation', 'service_id', 'repository_fingerprint', 'permission_profile',
                                   'runtime_roots', 'policy_digest', 'model')} | {
        'platform_enforcement': 'receipt:' + verified['platform_enforcement'],
        'tool_paths': 'receipt:' + verified['tool_paths'], startup_kind: 'receipt:' + verified[startup_kind],
        'manual_execution_coverage': 'receipt:' + verified['manual_execution_coverage'],
        'engine': 'dsh', 'endpoint_sha256': endpoint_digest, 'configuration_sha256': configuration_digest}

    if control is not None:
        digest = 'receipt:' + verified['task_control']
        result['task_control'] = {action: digest for action in ('append', 'stop', 'continue', 'related_execution', 'idle_input')}
        from .dsh import validate_exclusive_input
        exclusive = control.get('exclusive_input')
        if not isinstance(exclusive, dict):
            raise ManagementError('capability_unverified', 'DSH native input lacks compare-and-swap; a live exclusive-input lease is required.')
        validate_exclusive_input({'exclusive_input': exclusive}, connection, repository, exclusive.get('thread_id'), exclusive.get('turn_id'))
        result['exclusive_input'] = dict(exclusive)
        if control.get('unregistered_process_paths') == 'disabled_and_verified':
            result['process_coverage'] = {'kind': 'no_unregistered_process_paths', 'evidence': digest}
        elif isinstance(control.get('process_coverage'), dict):
            coverage = control['process_coverage']
            if coverage.get('kind') == 'task_processes_stopped' and coverage.get('thread_id') == control['thread_id'] and coverage.get('turn_id') == control['turn_id'] and coverage.get('all_registered_processes_exited') is True:
                result['process_coverage'] = {**coverage, 'evidence': digest}
    if human_response is not None:
        result.setdefault('task_control', {})['human_response'] = 'receipt:' + verified['human_response']
    if startup_input is not None:
        result['startup_exclusive_input'] = dict(startup_input)
    return result
