"""Owner requests end original execution before releasing its repository."""
import asyncio
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import time

import psutil

from .manager import ManagementError
from .repository_execution import execution_configuration, private_file
from .repository_supervision import attach_existing_owned, original_history, original_jobs, owned_call, owned_instance


def _record(intake, work_id):
    return next(r for r in intake.snapshot()['work'] if r['id'] == work_id)


def _card(intake, record):
    path = Path(record['target']['repo_path']).resolve(strict=True)
    if intake._verified_card(record, path, record['card_id']) != record['card_id']:
        raise ManagementError('binding_conflict', 'The original outer card changed; repository remains occupied.')
    return intake._kanban('kanban_show', {'task_id': record['card_id']})['task']


def _worker_end(intake, record, statuses=frozenset({'blocked', 'triage'}), *, allow_later_terminal=False):
    from hermes_cli import kanban_db as kb
    from hermes_cli.kanban_db_connect import connect_closing
    from .trusted_controller import controller_identity
    with connect_closing(board=intake.board) as connection:
        card = kb.get_task(connection, record['card_id'])
        latest = kb.latest_run(connection, record['card_id'])
        runs = kb.list_runs(connection, record['card_id'])
    if not card or card.status not in statuses or card.current_run_id is not None or card.worker_pid is not None:
        raise ManagementError('outcome_unknown', 'The original Kanban worker has not ended.')
    claim = record['dsh_execution'].get('supervisor_claim')
    if claim is None:
        if latest is not None:
            prior = record.get('execution_attempts', [])
            previous = prior[-1]['execution'].get('supervisor_claim') if prior else None
            if not previous or latest.id != previous['run_id'] or latest.status == 'running' or latest.ended_at is None:
                raise ManagementError('outcome_unknown', 'An unregistered original worker requires reconciliation.')
        return {'run_id': None, 'exit_code': None, 'not_started': True}
    run = next((candidate for candidate in runs if candidate.id == claim['run_id']), None) if allow_later_terminal else latest
    identity = claim['identity']
    if (not run or run.id != claim['run_id'] or run.task_id != card.id
        or type(identity.get('pid')) is not int or identity['pid'] <= 0
        or run.worker_pid not in {None, identity['pid']} or run.status == 'running' or run.ended_at is None):
        raise ManagementError('outcome_unknown', 'The original claimed run has not ended.')
    terminal_statuses = {'done', 'blocked', 'crashed', 'timed_out', 'failed', 'released'}
    if allow_later_terminal and any(candidate.task_id != card.id or candidate.status not in terminal_statuses
                                    or candidate.ended_at is None for candidate in runs if candidate.id > run.id):
        raise ManagementError('outcome_unknown', 'A later original worker remains active or unknown.')
    if psutil.pid_exists(identity['pid']):
        process = psutil.Process(identity['pid'])
        birth = float(identity['start_sec']) + float(identity['start_usec']) / 1000000
        if abs(process.create_time() - birth) < .000001:
            if process.status() != psutil.STATUS_ZOMBIE and controller_identity(identity['pid']) != identity:
                raise ManagementError('outcome_unknown', 'The original worker kernel binding differs.')
            try:
                process.wait(timeout=.05)
            except psutil.TimeoutExpired:
                raise ManagementError('outcome_unknown', 'The original worker process still exists.') from None
    offset = claim.get('exit_log_offset')
    path = kb.worker_log_path(card.id, board=intake.board)
    size = path.stat().st_size
    if offset is None:
        if len(runs) != 1 or allow_later_terminal:
            raise ManagementError('outcome_unknown', 'Original worker exit has no unambiguous run log boundary.')
        offset = 0
    if type(offset) is not int or offset < 0 or offset > size:
        raise ManagementError('outcome_unknown', 'Original worker log boundary changed.')
    text = kb.read_worker_log(card.id, tail_bytes=min(4000, size - offset), board=intake.board)
    if not isinstance(text, str):
        raise ManagementError('outcome_unknown', 'The original worker log is unavailable.')
    codes = re.findall(r'^\[kanban-worker-exit\] rc=(-?\d+)\s*$', text, re.MULTILINE)
    if not codes or (codes[0] if allow_later_terminal else codes[-1]) != '0':
        raise ManagementError('outcome_unknown', 'The original worker has no successful native exit receipt.')
    return {'run_id': run.id, 'identity': identity, 'exit_code': 0}


def _carrier_end(intake, record):
    if record['dsh_execution'].get('mode') == 'shared_native':
        from .shared_repository import shared_range_proof
        control = record['dsh_execution'].get('stop_control', {})
        ended = record['dsh_execution'].get('lifecycle_end', {})
        return shared_range_proof(intake, record, control.get('session_end', ended.get('session_end', {})))
    directory = owned_instance(intake, record)
    execution = record['dsh_execution']
    configuration = json.loads(private_file(directory / 'native-configuration.json').read_text())
    identity = json.loads(private_file(directory / 'native-identity.json').read_text())
    birth = json.loads(private_file(directory / 'process-birth.json').read_text())
    receipt = json.loads(private_file(directory / 'native-exit.json').read_text())
    if (identity != execution['native_identity'] or configuration['generation'] != execution['generation']
        or configuration['session_id'] != execution['session_id'] or birth['generation'] != execution['generation']
        or birth['pid'] != identity['pid'] or receipt.get('exit_code') != 0 or receipt.get('shutdown_requested') is not True
        or any(receipt.get(k) != identity[k] for k in ('pid', 'generation', 'created_at_ms', 'configuration_sha256'))
        or Path(configuration['socket_path']).exists()):
        raise ManagementError('outcome_unknown', 'The original carrier has no verified normal exit.')
    try:
        process = psutil.Process(birth['pid'])
        if process.create_time() == birth['created_at']:
            try:
                process.wait(timeout=2)
            except psutil.TimeoutExpired:
                raise ManagementError('outcome_unknown', 'The original carrier generation still exists.') from None
    except psutil.NoSuchProcess:
        pass
    return receipt


def _execution_coverage(intake, record, control):
    """Verify original job settlement and awaited native Mac managed-range cleanup."""
    if record['dsh_execution'].get('mode') == 'shared_native':
        from .shared_repository import shared_range_proof
        return shared_range_proof(intake, record, control.get('session_end', {}))
    from collections import Counter
    directory = owned_instance(intake, record)
    execution = record['dsh_execution']
    path = directory / 'home/.hermes-tool-receipts.jsonl'
    receipts = [json.loads(line) for line in private_file(path).read_text().splitlines()] if path.exists() else []
    calls = control.get('session_end', {}).get('tool_calls', [])
    if (control.get('session_end', {}).get('session_id') != execution['session_id']
        or control.get('session_end', {}).get('generation') != execution['generation']):
        raise ManagementError('coverage_unverified', 'Original session end belongs to another execution.')
    if sorted((row['call_id'], row['name']) for row in receipts) != sorted((row['id'], row['name']) for row in calls):
        raise ManagementError('coverage_unverified', 'Original tool settlement coverage is incomplete.')
    path = directory / 'home/.hermes-native-jobs.jsonl'
    events = [json.loads(line) for line in private_file(path).read_text().splitlines()] if path.exists() else []
    for row in receipts + events:
        if row.get('generation') != execution['generation'] or row.get('session_id') != execution['session_id']:
            raise ManagementError('coverage_unverified', 'Original managed lifecycle belongs to another execution.')
    registered = [row for row in events if row['type'] == 'registered']
    settled = [row for row in events if row['type'] == 'settled']
    if (any(row.get('owner') != execution['session_id'] or row.get('kind') != 'bash' for row in events)
        or Counter(row['command_sha256'] for row in receipts if row['name'] == 'bash' and row.get('execution_admission') != 'rejected')
            != Counter(row['command_sha256'] for row in registered)
        or Counter(row['job_id'] for row in registered) != Counter(row['job_id'] for row in settled)
        or any(row.get('status') not in {'completed', 'killed', 'failed'} or row.get('cause') == 'teardown' for row in settled)
        or any(row.get('execution_admission') == 'rejected' and row.get('is_error') is not True for row in receipts)):
        raise ManagementError('coverage_unverified', 'Original registered job settlement is incomplete or unknown.')
    cleanup = json.loads(private_file(directory / 'home/.hermes-native-cleanup.json').read_text())
    identity = execution['native_identity']
    if (cleanup.get('schema') != 1 or cleanup.get('status') != 'completed'
        or cleanup.get('scope') != 'darwin_process_group' or cleanup.get('provider') != '@deepseek-ai/dsh-subprocess-local'
        or cleanup.get('scope_disposal') != 'returned' or cleanup.get('escaped_descendants') != 'unverified'
        or cleanup.get('session_id') != execution['session_id']
        or any(cleanup.get(key) != identity[key] for key in ('pid', 'generation', 'instance_id', 'created_at_ms', 'configuration_sha256'))):
        raise ManagementError('coverage_unverified', 'Original native managed-range cleanup is unconfirmed.')
    return {'scope': 'darwin_process_group', 'escaped_descendants': 'unverified',
            'settled_call_ids': [row['call_id'] for row in receipts],
            'settled_job_ids': [row['job_id'] for row in settled], 'runtime_cleanup': cleanup}


def native_range_proof(intake, record, session_end):
    """One original ended instance, independent of stop or delivery semantics."""
    if record['dsh_execution'].get('mode') == 'shared_native':
        from .shared_repository import shared_range_proof
        return shared_range_proof(intake, record, session_end)
    return {'carrier': _carrier_end(intake, record),
            'coverage': _execution_coverage(intake, record, {'session_end': session_end})}


async def close_native_range(intake, record, carrier, session_end, generation):
    intake.require_active(generation)
    record['dsh_execution']['lifecycle_end'] = {'status': 'intent', 'session_end': session_end}
    intake._save_execution(record, fields=['lifecycle_end'])
    if record['dsh_execution'].get('mode') != 'shared_native':
        await asyncio.to_thread(carrier.shutdown_owned)
    proof = await asyncio.to_thread(native_range_proof, intake, record, session_end)
    intake.require_active(generation)
    record['dsh_execution']['lifecycle_end'].update(status='ended', proof=proof)
    intake._save_execution(record, fields=['lifecycle_end'])
    return proof


async def _stop(intake, record, generation):
    execution = record.get('dsh_execution')
    if execution and execution.get('mode') == 'shared_native':
        from .shared_repository import stop_shared_work
        return await stop_shared_work(intake, record, generation)
    if not execution:
        raise ManagementError('configuration_missing', 'No dedicated execution is registered.')
    if execution['state'] == 'stopped':
        return record
    control = execution.get('stop_control', {})
    carrier = None
    try:
        card = _card(intake, record)
        if card['status'] == 'ready':
            intake._kanban('kanban_block', {'task_id': record['card_id'], 'kind': 'needs_input',
                'reason': 'Owner requested termination; original execution cleanup must be verified.'})
        deadline = time.monotonic() + 30
        while time.monotonic() < deadline:
            intake.require_active(generation)
            record = _record(intake, record['id'])
            execution = record['dsh_execution']
            if execution.get('native_identity') or not execution.get('generation'):
                break
            await asyncio.sleep(.05)
        if execution.get('native_identity'):
            directory = owned_instance(intake, record)
            if not (directory / 'native-exit.json').exists():
                carrier = attach_existing_owned(intake, record)
                while time.monotonic() < deadline:
                    intake.require_active(generation)
                    sessions = await asyncio.to_thread(owned_call, carrier, 'session/list', {})
                    rows = [row for row in sessions['items'] if row['sessionId'] == execution['session_id']]
                    if len(rows) != 1 or rows[0].get('cwd') != record['target']['repo_path'] or rows[0].get('agentAvailable') is not True:
                        raise ManagementError('outcome_unknown', 'The original session identity is unavailable.')
                    if rows[0].get('running') is True and not control.get('cancel_status'):
                        control['cancel_status'] = 'intent'
                        execution['stop_control'] = control
                        intake._save_execution(record, fields=['stop_control'])
                        answer = await asyncio.to_thread(owned_call, carrier, 'session/cancel', {'sessionId': execution['session_id']})
                        if answer.get('accepted') is not True:
                            raise ManagementError('outcome_unknown', 'Original cancel admission remains unknown.')
                        control['cancel_status'] = 'accepted'
                        intake._save_execution(record, fields=['stop_control'])
                    events, projections = await asyncio.to_thread(original_history, carrier, execution['session_id'], record['target']['repo_path'])
                    starts = [event for event in events if event['type'] == 'turn/start']
                    ends = [event for event in events if event['type'] == 'turn/end']
                    inbox = projections.get('inbox')
                    jobs = await asyncio.to_thread(original_jobs, carrier, execution['session_id'])
                    for job in jobs:
                        if job.get('owner') != execution['session_id']:
                            raise ManagementError('coverage_unverified', 'Unowned background work requires reconciliation.')
                        if job['status'] not in {'running', 'stopping'}:
                            continue
                        cancelled = control.setdefault('job_cancel', {})
                        if cancelled.get(job['id']) == 'intent':
                            raise ManagementError('outcome_unknown', 'Original job cancel admission remains unknown.')
                        if job['id'] not in cancelled:
                            cancelled[job['id']] = 'intent'
                            execution['stop_control'] = control
                            intake._save_execution(record, fields=['stop_control'])
                            answer = await asyncio.to_thread(owned_call, carrier, 'job/kill',
                                {'sessionId': execution['session_id'], 'jobId': job['id']})
                            if answer.get('outcome') not in {'requested', 'already-finished'}:
                                raise ManagementError('outcome_unknown', 'Original job cancel admission remains unknown.')
                            cancelled[job['id']] = 'accepted'
                            intake._save_execution(record, fields=['stop_control'])
                    if (rows[0].get('running') is False and (not starts or ends and ends[-1]['seq'] > starts[-1]['seq'])
                        and isinstance(inbox, dict) and set(inbox) == {'next-turn', 'next-step'} and not any(inbox.values())
                        and not any(job['status'] in {'running', 'stopping'} for job in jobs)):
                        control['session_end'] = {'session_id': execution['session_id'], 'generation': execution['generation'],
                            'journal_cursor': events[-1]['seq'] if events else -1,
                            'tool_calls': [{'id': event['data']['callId'], 'name': event['data']['name']}
                                for event in events if event['type'] == 'tool/call'],
                            'jobs': [{'id': job['id'], 'status': job['status']} for job in jobs]}
                        execution['stop_control'] = control
                        intake._save_execution(record, fields=['stop_control'])
                        await close_native_range(intake, record, carrier, control['session_end'], generation)
                        break
                    await asyncio.sleep(.05)
                else:
                    raise ManagementError('outcome_unknown', 'Original execution or background jobs remain unconfirmed.')
            if not control.get('session_end'):
                raise ManagementError('outcome_unknown', 'No original session and background-task end proof exists.')
            exit_receipt = await asyncio.to_thread(_carrier_end, intake, record)
        elif execution.get('generation'):
            raise ManagementError('outcome_unknown', 'Original native startup remains unconfirmed.')
        else:
            exit_receipt = {'not_started': True}
        coverage = _execution_coverage(intake, record, control) if execution.get('native_identity') else {'scope': 'not_started'}
        while time.monotonic() < deadline:
            intake.require_active(generation)
            try:
                worker = _worker_end(intake, _record(intake, record['id']))
                break
            except ManagementError as error:
                if error.code != 'outcome_unknown':
                    raise
            await asyncio.sleep(.05)
        else:
            raise ManagementError('outcome_unknown', 'Original outer worker exit remains unconfirmed.')
        from .repository_acceptance import repository_source_state
        source = await asyncio.to_thread(repository_source_state, record['target'])
        intake.require_active(generation)
        record = _record(intake, record['id'])
        execution = record['dsh_execution']
        control.update(status='stopped', proof={'carrier': exit_receipt, 'worker': worker, 'repository': source, 'coverage': coverage})
        execution.update(state='stopped', stop_control=control)
        intake._save_execution(record, fields=['state', 'stop_control'])
        return record
    finally:
        if carrier is not None:
            carrier.close()


async def _retry(intake, record, prepared, generation):
    execution = record['dsh_execution']
    if execution.get('retry_control', {}).get('message_id') == prepared.envelope['message_id']:
        return record
    if execution.get('state') != 'stopped' or execution.get('stop_control', {}).get('status') != 'stopped':
        raise ManagementError('outcome_unknown', 'Explicit retry requires confirmed original execution cleanup.')
    card = _card(intake, record)
    _worker_end(intake, record)
    if execution.get('native_identity'):
        _carrier_end(intake, record)
        _execution_coverage(intake, record, execution['stop_control'])
    from .manager import _git
    def repository_key(path):
        path = Path(path).resolve(strict=True)
        common = Path(_git(path, 'rev-parse', '--git-common-dir'))
        return (path / common).resolve(strict=True) if not common.is_absolute() else common.resolve(strict=True)
    key = repository_key(record['target']['repo_path'])
    for other in intake.snapshot()['work']:
        if other['id'] != record['id'] and repository_key(other['target']['repo_path']) == key and other.get('dsh_execution', {}).get('state') not in {'stopped', 'released'}:
            raise ManagementError('repository_busy', 'Earlier repository work must be reconciled before retry.')
    listing = intake._kanban('kanban_list', {'include_archived': True, 'limit': 200})
    if listing.get('truncated') is not False:
        raise ManagementError('outcome_unknown', 'Native repository queue coverage is incomplete.')
    for queued in listing.get('tasks', []):
        if queued['id'] != record['card_id'] and queued.get('status') in {'ready', 'running', 'todo', 'scheduled'} and queued.get('workspace_kind') == 'dir':
            if repository_key(queued['workspace_path']) == key:
                raise ManagementError('repository_busy', 'Native repository work is already queued; retry cannot bypass it.')
    target, _ = intake._target(prepared.binding, '派发 ' + record['profile_id'] + ' ' + record['issue']['url'])
    if target is None or any(target.get(field) != record['target'].get(field) for field in ('profile_id', 'native_profile', 'repository')) or repository_key(target['repo_path']) != key:
        raise ManagementError('binding_conflict', 'Current registered responsibility differs from the original work.')
    target = dict(record['target'], execution_ref=target.get('execution_ref'))
    admitted, _ = execution_configuration(target)
    issue = await asyncio.to_thread(intake.github.read_issue, record['issue']['url'])
    intake.require_active(generation)
    if any(issue.get(field) != record['issue'].get(field) for field in ('url', 'title', 'body', 'updated_at')):
        raise ManagementError('binding_conflict', 'The original frozen Issue changed; accept a new scope explicitly.')
    from .repository_acceptance import repository_source_state
    from .trusted_controller import controller_identity
    source = repository_source_state(target)
    with intake.lifecycle_lock:
        intake.require_active(generation)
        history = record.setdefault('execution_attempts', [])
        history.append({'execution': execution, 'target': record['target'], 'gateway_controller': record['gateway_controller']})
        record.update(target=target, gateway_controller=controller_identity(), dsh_execution={
            'state': 'admitted', 'attempt': len(history) + 1, 'reference_sha256': admitted['reference_sha256'],
            **({'mode': 'shared_native'} if admitted.get('mode') == 'shared_native' else {}),
            'control_message_ids': [prepared.envelope['message_id']],
            'admitted_budget': admitted['runtime_configuration']['budget'], 'retry_control': {
                'message_id': prepared.envelope['message_id'], 'status': 'accepted', 'dispatch_status': 'intent', 'repository': source}})
        intake._save(record)
        if card['status'] == 'triage':
            from hermes_cli import kanban_db as kb
            from hermes_cli.kanban_db_connect import connect_closing
            with connect_closing(board=intake.board) as connection:
                if not kb.specify_triage_task(connection, record['card_id'], author='verified-owner'):
                    raise ManagementError('outcome_unknown', 'Original native triage respecification remains unconfirmed.')
                if kb.get_task(connection, record['card_id']).status not in {'todo', 'ready'}:
                    raise ManagementError('outcome_unknown', 'Original native parent-gated retry phase remains unconfirmed.')
        else:
            intake._kanban('kanban_unblock', {'task_id': record['card_id']})
        current = _record(intake, record['id'])
        current['dsh_execution']['retry_control']['dispatch_status'] = 'accepted'
        intake._save_execution(current, fields=['retry_control'])
        record = current
    return record


async def process_control(intake, prepared, generation):
    async with intake.lock:
        descriptor = os.open(intake.lock_path, os.O_RDWR | os.O_NOFOLLOW)
        try:
            while True:
                intake.require_active(generation)
                try:
                    fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    break
                except BlockingIOError:
                    await asyncio.sleep(.02)
            intake.require_active(generation)
            record = _record(intake, prepared.work_id)
            execution = record['dsh_execution']
            message_id = prepared.envelope['message_id']
            historical = any(message_id in previous['execution'].get('control_message_ids', [])
                or any(message_id == previous['execution'].get(field, {}).get('message_id') for field in ('stop_control', 'retry_control'))
                for previous in record.get('execution_attempts', []))
            duplicate = message_id in execution.get('control_message_ids', [])
            if prepared.command.startswith('补充 '):
                owner_controls = [execution.get('owner_context_control', {}), *execution.get('owner_context_history', [])]
                saved_owner = next((control for control in owner_controls if control.get('message_id') == message_id), {})
                duplicate = duplicate or bool(saved_owner)
            if not historical and not duplicate:
                execution.setdefault('control_message_ids', []).append(message_id)
                intake._save_execution(record, fields=['control_message_ids'])
            if historical:
                text, result = '这是前轮已登记的控制消息；本轮执行没有改变。原操作结果仍按原记录核对。', {'status': 'duplicate', 'work_id': record['id']}
            elif duplicate and prepared.command.startswith('补充 '):
                saved = saved_owner
                text = '这条补充控制消息已登记；不会重送要求或新增预算。原 SID 受理结果仍按记录核对。'
                result = {'status': 'duplicate', 'work_id': record['id'], 'original_status': saved.get('status', 'unverified')}
            elif duplicate and (prepared.command.startswith(('结束后台 ', '重试 ', '继续 ', '续预算 ')) or execution.get('state') == 'stopped'):
                field = ('continuation_control' if execution.get('continuation_control', {}).get('message_id') == message_id
                    else 'window_resume_checkpoint' if execution.get('window_resume_checkpoint', {}).get('message_id') == message_id
                    else 'renewal_window_checkpoint' if execution.get('renewal_window_checkpoint', {}).get('message_id') == message_id
                    else 'renew_window_checkpoint' if prepared.command.startswith('续预算 ') else 'maintenance_checkpoint') if prepared.command.startswith(('继续 ', '续预算 ')) else 'retry_control' if prepared.command.startswith('重试 ') else 'stop_control'
                saved = execution.get(field, {})
                text = '这条控制消息已登记；不会重送操作或新增预算。原结果仍按记录核对，未受理或未知时请用新的消息明确请求。'
                result = {'status': 'duplicate', 'work_id': record['id'],
                    'original_status': saved.get('status', 'unverified') if saved.get('message_id') == message_id else 'unverified'}
                if saved.get('message_id') == message_id and 'dispatch_status' in saved:
                    result['dispatch_status'] = saved['dispatch_status']
            elif prepared.command.startswith('结束后台 '):
                from .shared_repository import end_shared_background
                record = await end_shared_background(intake, record, message_id, generation)
                text = '原工作已登记后台作业已核实终结；外层任务、原 SID 与预算保留，原报告仍按实际验收核对。'
                result = {'status': 'background_ended', 'work_id': record['id'], 'delivered': False}
            elif prepared.command.startswith('补充 '):
                from .shared_repository import admit_shared_owner_context
                record = await asyncio.to_thread(admit_shared_owner_context, intake, record, prepared, generation)
                text = '补充要求已由原 SID 受理，计入已授权新窗口；结果待核。原仓库占用与累计预算保留。'
                result = {'status': 'accepted', 'work_id': record['id']}
            elif prepared.command.startswith('续预算 '):
                from .shared_repository import _repeat_budget_stopped_window
                if (execution.get('renewal_window_checkpoint', {}).get('status') == 'admitted'
                        and _repeat_budget_stopped_window(execution)):
                    from .shared_repository import prepare_shared_window_renewal
                    record = await asyncio.to_thread(prepare_shared_window_renewal, intake, record, message_id)
                    text = '原到期窗口续预算准备已保存；未新增输入、预算或期限。正常维护后须再次明确续预算，原 SID 与旧窗口完整保留。'
                    result = {'status': 'prepared', 'work_id': record['id']}
                elif execution.get('renewal_window_checkpoint'):
                    from .shared_repository import admit_shared_window_renewal
                    record = await asyncio.to_thread(admit_shared_window_renewal, intake, record, message_id, generation)
                    text = '原 SID 新有限窗口已受理；旧窗口完整备份保留，实际请求前才开始新窗口，等待原生派发。'
                    result = {'status': 'admitted', 'work_id': record['id']}
                elif (execution.get('state') == 'awaiting_acceptance'
                        and execution.get('terminal_reason', {}).get('kind') == 'completed'):
                    from .shared_repository import prepare_shared_window_renewal
                    record = await asyncio.to_thread(prepare_shared_window_renewal, intake, record, message_id)
                    text = '原到期窗口续预算准备已保存；未新增输入、预算或期限。正常维护后须再次明确续预算，原 SID 与旧窗口完整保留。'
                    result = {'status': 'prepared', 'work_id': record['id']}
                else:
                    from .shared_repository import prepare_shared_window, admit_shared_window
                    if execution.get('renew_window_checkpoint'):
                        record = await asyncio.to_thread(admit_shared_window, intake, record, message_id, generation)
                        text = '原 SID 新有限窗口已受理；旧窗口与累计用量完整保留，实际请求前才开始新窗口，等待原生派发。'
                        result = {'status': 'admitted', 'work_id': record['id']}
                    else:
                        record = await asyncio.to_thread(prepare_shared_window, intake, record, message_id)
                        text = '续预算准备已保存；未新增输入或启动窗口。正常维护后须再次明确续预算，原 SID、任务与旧窗口完整保留。'
                        result = {'status': 'prepared', 'work_id': record['id']}
            elif prepared.command.startswith('重试 '):
                record = await _retry(intake, record, prepared, generation)
                text = '已受理独立新一轮执行；沿用原总 Issue 和外层卡，旧会话、预算与失败记录完整保留，等待原生派发。'
                result = {'status': record['dsh_execution']['state'], 'work_id': record['id']}
            elif prepared.command.startswith('继续 '):
                if (execution.get('state') in {'running', 'outcome_unknown'} and any(
                        question.get('kind') == 'structured' and question.get('state') == 'open'
                        for question in execution.get('questions', []))):
                    from .shared_repository import continue_shared_pending_question
                    record = await asyncio.to_thread(continue_shared_pending_question, intake, record, message_id)
                    text = '原待答会话的外层卡已重新受理，等待原生派发；原 SID、问题、历史与预算保持，尚未重发输入或答案。'
                    result = {'status': 'queued', 'work_id': record['id'], 'delivered': False}
                elif execution.get('owner_context_maintenance_checkpoint'):
                    from .shared_repository import admit_shared_owner_maintenance
                    record = await asyncio.to_thread(admit_shared_owner_maintenance, intake, record, message_id, generation)
                    text = '原 SID 正常维护接纳已核实；原窗口与累计用量保持，卡片保持待处理，尚未发送输入。请明确补充最终要求。'
                    result = {'status': 'admitted', 'work_id': record['id']}
                elif len(execution.get('owner_context_history', [])) == 1 and execution.get('owner_context_control'):
                    from .shared_repository import admit_shared_owner_context
                    record = await asyncio.to_thread(admit_shared_owner_context, intake, record, prepared, generation, prepare_maintenance=True)
                    text = '原 SID 最终输入维护准备已保存；未发送输入、派发卡片或改变窗口。正常维护后须用新的继续消息明确接纳。'
                    result = {'status': 'prepared', 'work_id': record['id']}
                elif execution.get('terminal_reason', {}).get('kind') == 'max-tokens':
                    from .shared_repository import admit_shared_max_tokens
                    record = await asyncio.to_thread(admit_shared_max_tokens, intake, record, message_id, generation)
                    text = '原任务截断后续接已受理；原 SID、任务、已用量与预算窗口保持，原外层卡等待原生派发。'
                    result = {'status': 'admitted', 'work_id': record['id']}
                elif execution.get('window_resume_checkpoint'):
                    from .shared_repository import admit_shared_window_resume
                    record = await asyncio.to_thread(admit_shared_window_resume, intake, record, message_id, generation)
                    text = '原会话维护后在原有限窗口内继续已受理；原 SID、窗口起点、累计用量与剩余额度保持，原外层卡等待原生派发。'
                    result = {'status': 'admitted', 'work_id': record['id']}
                elif (execution.get('state') == 'awaiting_acceptance'
                        and execution.get('terminal_reason', {}).get('kind') == 'completed'):
                    from .shared_repository import prepare_shared_window_resume
                    record = await asyncio.to_thread(prepare_shared_window_resume, intake, record, message_id)
                    text = '原有限窗口维护续接准备已保存；未新增输入、预算或期限。正常维护后须再次明确继续，原 SID 与窗口完整保留。'
                    result = {'status': 'prepared', 'work_id': record['id']}
                elif execution.get('maintenance_checkpoint'):
                    from .shared_repository import admit_shared_continuation
                    record = await asyncio.to_thread(admit_shared_continuation, intake, record, message_id, generation)
                    text = '原会话维护后续接已受理；原 SID、任务与预算保持，原外层卡等待原生派发，随后向原会话发送明确续接。'
                    result = {'status': 'admitted', 'work_id': record['id']}
                else:
                    from .shared_repository import prepare_shared_continuation
                    record = await asyncio.to_thread(prepare_shared_continuation, intake, record, message_id)
                    text = '原会话续接准备已保存；原 SID、历史、任务与预算保持，尚未派发或重送输入。维护重载后须再次明确继续。'
                    result = {'status': 'prepared', 'work_id': record['id']}
            else:
                if not execution.get('stop_requested'):
                    _card(intake, record)
                    control = execution.get('stop_control', {})
                    control.update(message_id=prepared.envelope['message_id'], prior_state=execution['state'], status='intent')
                    execution.update(stop_requested=True, stop_control=control)
                    intake._save_execution(record, fields=['stop_requested', 'stop_control'])
                fcntl.flock(descriptor, fcntl.LOCK_UN)
                record = await _stop(intake, record, generation)
                text = '停止已核实；改动、原会话、预算与失败记录保留。原外层卡保持待处理，可安排后续仓库工作。'
                result = {'status': 'stopped', 'work_id': record['id']}
        except (ManagementError, ValueError, KeyError, OSError, TimeoutError, psutil.NoSuchProcess, psutil.AccessDenied) as error:
            intake.require_active(generation)
            record = _record(intake, prepared.work_id)
            execution = record.get('dsh_execution', {})
            if prepared.command.startswith(('停止 ', '结束后台 ')):
                from .shared_repository import _record_shared_failure
                _record_shared_failure(intake, record, error,
                    'background_cleanup' if prepared.command.startswith('结束后台 ') else 'whole_stop')
            if prepared.command.startswith(('停止 ', '结束后台 ')) and execution.get('stop_control'):
                execution['stop_control'].update(status='unverified',
                    last_failure={key: execution['last_failure'][key] for key in ('code', 'type', 'message')})
                intake._save_execution(record, fields=['stop_control'])
            text = ('后台结束尚未核实；保留原工作、仓库占用与预算，不重发未知取消。' if prepared.command.startswith('结束后台 ')
                else '补充要求未核实受理；原 SID、仓库占用与预算保留，不会重发，请核对原记录。' if prepared.command.startswith('补充 ')
                else '继续未受理；原 SID、历史与预算保留，先核对原失败、维护重载及连接身份。' if prepared.command.startswith(('继续 ', '续预算 '))
                else '重试未受理；原工作与记录保留，先核对停止、当前配置、Issue 和仓库队列。' if prepared.command.startswith('重试 ')
                else '停止尚未核实；保留仓库占用，不启动重试或后续工作。原记录与改动保留。')
            result = {'status': 'unverified', 'code': error.code if isinstance(error, ManagementError) else 'outcome_unknown',
                'repository_retained': True}
        finally:
            fcntl.flock(descriptor, fcntl.LOCK_UN)
            os.close(descriptor)
        await prepared.transport.send({'uuid': hashlib.sha256(('work-control:' + prepared.envelope['message_id']).encode()).hexdigest()[:32],
            'text': text, 'chat_id': prepared.envelope['chat_id'], 'reply_to': prepared.envelope['message_id']})
        intake.require_active(generation)
        return result
