"""Supervise managed outer work only from its original native Kanban worker."""
import asyncio
import fcntl
import hashlib
import json
import os
from pathlib import Path
import time
import uuid

from .manager import ManagementError
from .repository_execution import execution_configuration


def activate_work(intake, record, generation):
    if not record.get('card_id') or not record['target'].get('execution_ref') or record.get('dsh_execution'):
        return
    configuration, _ = execution_configuration(record['target'])
    intake.require_active(generation)
    if intake._verified_card(record, Path(record['target']['repo_path']).resolve(strict=True), record['card_id']) is None:
        raise ManagementError('binding_conflict', 'Original outer work needs reconciliation.')
    with intake.lifecycle_lock:
        intake.require_active(generation)
        record['dsh_execution'] = {'state': 'admitted', 'reference_sha256': configuration['reference_sha256']}
        record['execution'] = 'owned_dsh'
        intake._save(record)
        intake._kanban('kanban_unblock', {'task_id': record['card_id']})


def current_worker(ctx, intake, record):
    from agent.delegation_context import owned_kanban_task
    from hermes_cli import kanban_db as kb
    from hermes_cli.kanban_db_connect import connect_closing
    try:
        run_id = int(os.environ['HERMES_KANBAN_RUN_ID'])
        claim = os.environ['HERMES_KANBAN_CLAIM_LOCK']
        with connect_closing(board=intake.board) as connection:
            card = kb.get_task(connection, record['card_id'])
            run = kb.get_run(connection, run_id)
        if (owned_kanban_task() != record['card_id'] or card is None or run is None
            or ctx.profile_name != record['target']['native_profile'] or card.assignee != ctx.profile_name
            or card.status != 'running' or card.workspace_kind != 'dir'
            or Path(card.workspace_path).resolve(strict=True) != Path(record['target']['repo_path']).resolve(strict=True)
            or card.title != record['issue']['title'] or card.body != intake._card_body(record)
            or card.current_run_id != run_id or card.claim_lock != claim or card.worker_pid != os.getpid()
            or run.task_id != card.id or run.status != 'running' or run.claim_lock != claim
            or run.worker_pid != os.getpid() or card.claim_expires is None or card.claim_expires <= time.time()):
            raise ValueError('Original worker binding differs.')
        return run_id
    except (KeyError, TypeError, ValueError, OSError):
        raise ManagementError('unauthorized', 'Only the current original claimed worker may supervise this work.') from None


def native_call(ctx, name, args):
    result = ctx.dispatch_tool(name, args)
    result = json.loads(result) if isinstance(result, str) else result
    if not isinstance(result, dict) or 'error' in result:
        raise ManagementError('outcome_unknown', 'Original native work transition needs reconciliation.')
    return result


def owned_call(carrier, method, request):
    rpc_id = str(uuid.uuid4())
    envelope = carrier.request(method, {'_request': request} if method == 'session/list' else {'request': request}, rpc_id)
    if (envelope.get('type') != 'server-response' or envelope.get('rpcId') != rpc_id
        or envelope.get('result', {}).get('ok') is not True):
        raise ManagementError('outcome_unknown', 'Original owned execution response is unconfirmed.')
    return envelope['result']['value']


def original_history(carrier, session_id, workspace):
    stream = carrier.stream('session/follow', {'request': {'address': {'kind': 'session', 'sessionId': session_id}, 'maxMessages': 500}})
    try:
        snapshot = next(stream)
    finally:
        stream.close()
    if snapshot.get('header', {}).get('id') != session_id or snapshot['header'].get('cwd') != workspace or snapshot.get('hasMore') is not False:
        raise ManagementError('outcome_unknown', 'Original execution history is incomplete or belongs to another work.')
    from .dsh import DshRemoteAdapter
    events = [DshRemoteAdapter._journal_event(row) for row in snapshot['records']]
    if len(events) != snapshot['cursor'] + 1 or any(e['seq'] != i for i, e in enumerate(events)):
        raise ManagementError('outcome_unknown', 'Original execution journal has an unverified gap.')
    return events, snapshot['projections']['values']


def original_jobs(carrier, session_id):
    stream = carrier.stream('job/list', {'request': {'sessionId': session_id}})
    try:
        roster = next(stream)
    finally:
        stream.close()
    if roster.get('type') != 'rows' or not isinstance(roster.get('jobs'), list) or any(
        job.get('owner') not in {None, session_id} or job.get('status') not in {'running', 'stopping', 'completed', 'killed', 'failed'}
        for job in roster['jobs']):
        raise ManagementError('outcome_unknown', 'Original job identities or lifecycle are unavailable.')
    return roster['jobs']


def register_repository_supervision(ctx, intake):
    connections = set()
    def detach():
        for carrier in tuple(connections):
            carrier.close()
    ctx.on_unload(detach)

    def guard(tool_name=None, args=None, **_):
        from agent.delegation_context import owned_kanban_task
        card_id = owned_kanban_task()
        record = next((r for r in intake.snapshot()['work'] if r.get('card_id') == card_id), None) if card_id else None
        if record:
            if tool_name == 'kanban_block':
                try:
                    intake.require_active(intake.generation)
                    current_worker(ctx, intake, record)
                    supplied = args or {}
                    if (supplied.get('task_id', card_id) != card_id or supplied.get('board', intake.board) != intake.board
                        or supplied.get('kind') != 'needs_input'
                        or record.get('dsh_execution', {}).get('state') not in {'awaiting_acceptance', 'budget_stopped', 'execution_failed', 'outcome_unknown'}):
                        raise ManagementError('unauthorized', 'Original supervision handoff is unverified.')
                    return
                except ManagementError:
                    return {'action': 'block', 'message': 'Original worker claim and supervision outcome must be verified before kanban_block; repository occupancy is retained.'}
            if tool_name not in {'kanban_show', 'kanban_heartbeat', 'kanban_comment', 'hermes_pm_supervise', 'hermes_pm_snapshot'}:
                return {'action': 'block', 'message': 'This managed outer worker only supervises original DSH. Call hermes_pm_supervise({}) now; do not try alternative development tools or complete this card.'}
    ctx.register_hook('pre_tool_call', guard)
    ctx.register_system_prompt_section('hermes-pm-supervision',
        'For a plugin-managed Kanban outer card, read kanban_show, then call hermes_pm_supervise with no arguments. '
        'After supervision returns next_action=kanban_block, call kanban_block(kind="needs_input", reason="Dedicated execution awaits acceptance or reconciliation"), then end. '
        'Do not develop directly, create/link cards, or declare delivery. DSH owns the detailed repository work; the plugin records its real status.')

    async def run_work(record, generation):
        from .dsh_owned_transport import PersistentOwnedTransport
        current_worker(ctx, intake, record)
        admitted, environment = execution_configuration(record['target'])
        if record.get('dsh_execution', {}).get('reference_sha256') != admitted['reference_sha256']:
            raise ManagementError('configuration_missing', 'Original admitted execution reference changed.')
        lock = os.open(intake.lock_path, os.O_RDWR | os.O_NOFOLLOW)
        fcntl.flock(lock, fcntl.LOCK_EX)
        try:
            intake.require_active(generation)
            record = next(r for r in intake.snapshot()['work'] if r['id'] == record['id'])
            execution = record['dsh_execution']
            if execution['state'] in {'awaiting_acceptance', 'budget_stopped', 'execution_failed'}:
                current_worker(ctx, intake, record)
                return {'status': execution['state'], 'card_id': record['card_id'],
                        'next_action': 'kanban_block', 'block_kind': 'needs_input', 'delivered': False}
            occupied = [r for r in intake.snapshot()['work'] if r['id'] != record['id'] and r['target'].get('repo_path') == record['target']['repo_path']
                and r.get('dsh_execution', {}).get('state') not in {None, 'admitted', 'released'}]
            if occupied:
                raise ManagementError('repository_busy', 'Earlier original work still retains this repository.')
            if 'generation' not in execution:
                execution.update(state='startup_intent', generation=str(uuid.uuid4()), instance_id=record['id'],
                    session_id='hermes-' + uuid.uuid4().hex, request_id='input-' + uuid.uuid4().hex)
                intake._save_execution(record)
            instance = intake.state_dir / 'owned-work' / record['id']
            configuration = dict(admitted, dsh_home=str(instance / 'home'), workspace=record['target']['repo_path'],
                instance_id=execution['instance_id'], generation=execution['generation'], session_id=execution['session_id'])
            node_bin = configuration.pop('node_bin')
            carrier = PersistentOwnedTransport(instance_dir=str(instance), configuration=configuration,
                node_bin=node_bin, timeout=15, environment=environment)
            connections.add(carrier)
        finally:
            fcntl.flock(lock, fcntl.LOCK_UN)
            os.close(lock)
        try:
            sid = execution['session_id']
            items = await asyncio.to_thread(owned_call, carrier, 'session/list', {})
            existing = [row for row in items['items'] if row['sessionId'] == sid]
            if not existing:
                if execution.get('session_creation'):
                    raise ManagementError('outcome_unknown', 'Original Session creation remains unconfirmed; do not replace it.')
                execution['session_creation'] = 'intent'
                intake._save_execution(record)
                created = await asyncio.to_thread(owned_call, carrier, 'session/create',
                    {'sessionId': sid, 'cwd': configuration['workspace'], 'agentPreset': 'hermes-owned'})
                if created.get('sessionId') != sid or created.get('agentPreset') != 'hermes-owned':
                    raise ManagementError('outcome_unknown', 'Original Session creation identity differs.')
            elif len(existing) != 1 or existing[0].get('cwd') != configuration['workspace'] or existing[0].get('agentPreset') != 'hermes-owned':
                raise ManagementError('binding_conflict', 'Original visible Session binding differs.')
            execution['session_creation'] = 'registered'
            execution['native_identity'] = carrier.native_identity
            intake._save_execution(record)
            if not execution.get('first_input'):
                current_worker(ctx, intake, record)
                intake.require_active(generation)
                execution.update(first_input='intent', state='input_intent')
                intake._save_execution(record)
                answer = await asyncio.to_thread(owned_call, carrier, 'session/prompt', {'sessionId': sid,
                    'requestId': execution['request_id'], 'mode': 'queue', 'content': [{'type': 'text',
                    'text': 'Work only in this bound repository. Use the installed Matt skills to host the detailed workflow.\n'
                            + 'Frozen Issue: ' + record['issue']['url'] + '\n' + record['issue']['title'] + '\n' + record['issue']['body']
                            + '\nReport development and test results; do not claim outer delivery.'}]})
                if answer.get('accepted') is not True:
                    raise ManagementError('outcome_unknown', 'Original first input acceptance is unconfirmed.')
                execution.update(first_input='accepted', state='running')
                intake._save_execution(record)
            elif execution['first_input'] != 'accepted':
                raise ManagementError('outcome_unknown', 'Original input outcome needs reconciliation; no input was resent.')
            deadline = time.monotonic() + admitted['runtime_configuration']['budget']['max_wall_seconds'] + 15
            heartbeat_at = 0
            while time.monotonic() < deadline:
                intake.require_active(generation)
                current_worker(ctx, intake, record)
                if time.monotonic() - heartbeat_at >= 30:
                    native_call(ctx, 'kanban_heartbeat', {'task_id': record['card_id'], 'board': intake.board})
                    heartbeat_at = time.monotonic()
                items = await asyncio.to_thread(owned_call, carrier, 'session/list', {})
                item = next((row for row in items['items'] if row['sessionId'] == sid), None)
                events, projections = await asyncio.to_thread(original_history, carrier, sid, configuration['workspace'])
                execution.update(journal_cursor=events[-1]['seq'] if events else -1, usage=projections.get('tokenUsage'),
                    observed_events=events)
                intake._save_execution(record)
                ends = [event for event in events if event['type'] == 'turn/end']
                starts = [event for event in events if event['type'] == 'turn/start']
                if item and item.get('agentAvailable') is True and item.get('running') is False and starts and ends and ends[-1]['seq'] > starts[-1]['seq']:
                    inbox = projections.get('inbox')
                    if not isinstance(inbox, dict) or set(inbox) != {'next-turn', 'next-step'} or any(inbox.values()):
                        raise ManagementError('outcome_unknown', 'Original pending-input state is unavailable.')
                    jobs = await asyncio.to_thread(original_jobs, carrier, sid)
                    if any(job['status'] in {'running', 'stopping'} for job in jobs):
                        raise ManagementError('outcome_unknown', 'Original registered background work has not ended.')
                    reason = ends[-1]['data'].get('reason', {})
                    state = ('budget_stopped' if reason.get('kind') == 'aborted' and reason.get('reason', {}).get('kind') == 'hook'
                        else 'awaiting_acceptance' if reason.get('kind') == 'completed' else 'execution_failed')
                    current_worker(ctx, intake, record)
                    intake.require_active(generation)
                    receipt_path = Path(configuration['dsh_home']) / '.hermes-tool-receipts.jsonl'
                    tool_receipts = [json.loads(line) for line in receipt_path.read_text().splitlines()] if receipt_path.exists() else []
                    if any(row.get('generation') != execution['generation'] or row.get('session_id') != sid for row in tool_receipts):
                        raise ManagementError('outcome_unknown', 'Original tool settlement identity differs.')
                    execution.update(state=state, terminal_reason=reason, jobs=[{'id': row['id'], 'status': row['status']} for row in jobs],
                        tool_receipts=tool_receipts)
                    intake._save_execution(record)
                    return {'status': state, 'card_id': record['card_id'], 'session_id': sid,
                            'next_action': 'kanban_block', 'block_kind': 'needs_input', 'delivered': False}
                await asyncio.sleep(.25)
            raise ManagementError('outcome_unknown', 'Original owned execution has no confirmed terminal boundary.')
        except (ManagementError, OSError, ValueError, KeyError, TimeoutError):
            try:
                intake.require_active(generation)
                current_worker(ctx, intake, record)
                execution['state'] = 'outcome_unknown'
                intake._save_execution(record)
            except ManagementError:
                pass
            raise
        finally:
            carrier.close()
            connections.discard(carrier)

    async def supervise(args):
        if args:
            return json.dumps({'status': 'rejected', 'code': 'invalid_change'})
        from agent.delegation_context import owned_kanban_task
        card_id = owned_kanban_task()
        if not card_id:
            return json.dumps({'status': 'rejected', 'code': 'worker_required'})
        records = [r for r in intake.snapshot()['work'] if r.get('card_id') == card_id]
        if len(records) != 1 or records[0]['target']['native_profile'] != ctx.profile_name:
            return json.dumps({'status': 'rejected', 'code': 'unauthorized'})
        try:
            return json.dumps(await run_work(records[0], intake.generation))
        except ManagementError as error:
            if error.code in {'repository_busy', 'configuration_missing', 'outcome_unknown'}:
                try:
                    intake.require_active(intake.generation)
                    current_worker(ctx, intake, records[0])
                    record = next(r for r in intake.snapshot()['work'] if r['id'] == records[0]['id'])
                    record['dsh_execution']['state'] = 'outcome_unknown'
                    intake._save_execution(record)
                    return json.dumps({'status': 'unverified', 'code': error.code, 'repository_retained': True,
                        'next_action': 'kanban_block', 'block_kind': 'needs_input', 'delivered': False})
                except ManagementError:
                    pass
            return json.dumps({'status': 'unverified', 'code': error.code, 'repository_retained': True, 'delivered': False})

    ctx.register_tool(name='hermes_pm_supervise', toolset='hermes_pm_supervision',
        schema={'name': 'hermes_pm_supervise',
                'description': 'Supervise the current dispatcher-owned outer card with its dedicated original DSH instance. Caller identity and repository are never arguments.',
                'parameters': {'type': 'object', 'properties': {}, 'additionalProperties': False}},
        handler=supervise, is_async=True, description='Supervise original dedicated repository execution')
