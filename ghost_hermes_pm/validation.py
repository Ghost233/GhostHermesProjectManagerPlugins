"""Validate host-owned, hashed capability receipts; text verdicts are insufficient."""
import hashlib
import json
from pathlib import Path

from .manager import ManagementError
from .codex import repository_fingerprint


KINDS = ('platform_enforcement', 'tool_paths', 'task_start', 'manual_execution_coverage')
CONTROL_METHODS = {'thread/read', 'turn/steer', 'turn/start', 'turn/interrupt', 'thread/backgroundTerminals/list', 'thread/loaded/list'}
CONTROL_CHECKS = {'active_append', 'idle_input', 'interrupt', 'stop_verification', 'explicit_continue', 'wrong_turn',
                  'duplicate_instruction', 'disconnect', 'background_pagination', 'related_children', 'exclusive_input'}
ALLOWED = {'mono_source_write', 'mono_git_index', 'mono_git_commit', 'mono_gitlink', 'test_artifact_write'}
DENIED = {'child_source_write', 'child_git_write', 'child_root_rename', 'ancestor_rename', 'atomic_replace',
          'symlink_alias', 'preexisting_hardlink_alias', 'new_hardlink_alias', 'unregistered_path_write',
          'test_source_write', 'test_git_write', 'descendant_process_escape'}
TOOLS = {'model_files', 'shell_git', 'test_process', 'code_mode', 'local_mcp', 'dynamic_tools',
         'filesystem_rpc', 'process_spawn', 'thread_shell'}


def validate_receipts(report, connection, repository, command, env, state_dir):
    receipts = report.get('receipts') if isinstance(report, dict) else None
    if not isinstance(receipts, dict) or not set(KINDS).issubset(receipts) or set(receipts) - set(KINDS) - {'task_control', 'human_response'}:
        raise ManagementError('capability_unverified', 'Hashed current-service enforcement, tool, startup and executor-coverage receipts are required.')
    binary_digest = hashlib.sha256(Path(command[0]).read_bytes()).hexdigest()
    configuration_digest = hashlib.sha256(json.dumps({'command': command, 'environment': env}, sort_keys=True).encode()).hexdigest()
    expected = {'generation': connection['generation'], 'service_id': connection['service_id'],
                'repository_fingerprint': repository_fingerprint(repository), 'policy_digest': report.get('policy_digest'),
                'platform': connection['platform'], 'binary_sha256': binary_digest, 'configuration_sha256': configuration_digest}
    verified = {}
    evidence_root = (Path(state_dir) / 'validation-evidence').resolve()
    control = None
    human_response = None
    for kind in (*KINDS, *(k for k in ('task_control', 'human_response') if k in receipts)):
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
            if receipt.get('actual_methods') != ['initialize', 'initialized', 'permissionProfile/list', 'thread/start', 'turn/start', 'thread/read'] or receipt.get('runtime_roots') != report.get('runtime_roots') or receipt.get('permission_profile') != report.get('permission_profile') or not receipt.get('thread_id') or not receipt.get('turn_id'):
                raise ManagementError('capability_unverified', 'Actual controlled task-start receipts are incomplete.')
        elif kind == 'task_control':
            methods, checks = receipt.get('actual_methods'), receipt.get('checks')
            if not isinstance(methods, list) or any(not isinstance(m, str) for m in methods) or set(methods) != CONTROL_METHODS or not isinstance(checks, dict) or set(checks) != CONTROL_CHECKS or any(v != 'PASS' for v in checks.values()) or not receipt.get('thread_id') or not receipt.get('turn_id') or not receipt.get('new_turn_id') or receipt['turn_id'] == receipt['new_turn_id']:
                raise ManagementError('capability_unverified', 'Actual task control, exclusive idle input and related-execution coverage are incomplete.')
            control = receipt
        elif kind == 'human_response':
            methods = {'thread/read', 'item/tool/requestUserInput', 'item/commandExecution/requestApproval', 'item/fileChange/requestApproval', 'item/permissions/requestApproval', 'serverRequest/resolved'}
            checks = {'question', 'nonblocking', 'command_approval', 'file_approval', 'permission_approval', 'owner_only', 'wrong_request', 'duplicate', 'resolved_race', 'disconnect', 'secret', 'unknown_no_replay'}
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
        'tool_paths': 'receipt:' + verified['tool_paths'], 'task_start': 'receipt:' + verified['task_start'],
        'manual_execution_coverage': 'receipt:' + verified['manual_execution_coverage'],
        'binary_sha256': binary_digest, 'configuration_sha256': configuration_digest}

    if control is not None:
        digest = 'receipt:' + verified['task_control']
        result['task_control'] = {action: digest for action in ('append', 'stop', 'continue', 'related_execution', 'idle_input')}
        if control.get('unregistered_process_paths') == 'disabled_and_verified':
            result['process_coverage'] = {'kind': 'no_unregistered_process_paths', 'evidence': digest}
        elif isinstance(control.get('process_coverage'), dict):
            coverage = control['process_coverage']
            if coverage.get('kind') == 'task_processes_stopped' and coverage.get('thread_id') == control['thread_id'] and coverage.get('turn_id') == control['turn_id'] and coverage.get('all_registered_processes_exited') is True:
                result['process_coverage'] = {**coverage, 'evidence': digest}
    if human_response is not None:
        result.setdefault('task_control', {})['human_response'] = 'receipt:' + verified['human_response']
    return result
