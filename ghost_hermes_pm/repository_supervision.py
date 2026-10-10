"""Supervise managed outer work only from its original native Kanban worker."""
import asyncio
import fcntl
import hashlib
import json
import os
from pathlib import Path
import queue
import sys
import time
import uuid

import psutil

from .manager import ManagementError
from .repository_execution import execution_configuration

_TRANSPORT_FAILURES = (OSError, TimeoutError, queue.Empty, psutil.NoSuchProcess, psutil.AccessDenied)


def _unknown_transport(error):
    return ManagementError('outcome_unknown', 'Original owned transport is unconfirmed (' + type(error).__name__ + '); reconcile the same execution.')


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
    try:
        envelope = carrier.request(method, {'_request': request} if method == 'session/list' else {'request': request}, rpc_id)
    except _TRANSPORT_FAILURES as error:
        raise _unknown_transport(error) from error
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


def execution_event_references(events):
    """Keep route facts and original log positions; question content stays native."""
    references = []
    for event in events:
        row = {'seq': event['seq'], 'type': event['type']}
        data = event.get('data', {})
        if event['type'] == 'request/header':
            header = data.get('header', {})
            configuration = header.get('config', {})
            row['data'] = {'header': {'config': {key: configuration[key] for key in ('provider', 'model', 'maxTokens')
                                              if key in configuration},
                                      'tools': [{'name': tool['name']} for tool in header.get('tools', [])]}}
        elif event['type'] == 'request/context':
            row['data'] = {key: data[key] for key in ('provider', 'model', 'contextWindow') if key in data}
        references.append(row)
    return references


def acceptance_text(record):
    acceptance = record['dsh_execution']['acceptance']
    text = '交付已通过总 Issue 验收。' if acceptance['status'] == 'accepted' else '开发与测试结果待验收，尚未交付；总 Issue 验收未满足。'
    text += '\n总 Issue：' + record['issue']['url']
    if acceptance.get('source_commit'):
        text += '\n交付版本：' + acceptance['source_commit']
    pr = {'none': '未提供 PR', 'awaiting_review': '待审查', 'awaiting_merge': '待合并', 'merged': '已合并'}
    review = {'not_provided': '未提供', 'pending': '待审查', 'changes_requested': '要求修改', 'approved': '审查通过'}
    text += '\nPR 状态：' + pr.get(acceptance.get('pr_status'), '待核对')
    text += '\n审查状态：' + review.get(acceptance.get('review_status'), '待核对')
    if acceptance.get('unmet'):
        text += '\n未满足项：' + '、'.join(acceptance['unmet'])
    if isinstance(acceptance.get('leftovers'), list):
        text += '\n遗留项：' + str(len(acceptance['leftovers'])) + ' 项'
    return text


async def notify_acceptance(intake, record, generation):
    from .repository_questions import question_destination
    execution = record['dsh_execution']
    acceptance = execution.get('acceptance')
    if not acceptance:
        return
    signature = hashlib.sha256(json.dumps({key: acceptance.get(key) for key in
        ('status', 'source_commit', 'pr_status', 'review_status', 'unmet')}, sort_keys=True).encode()).hexdigest()
    if execution.get('delivery_notification', {}).get('signature') == signature:
        return
    destination = question_destination(intake, record)
    if destination is None:
        return
    transport, binding = destination
    if await transport.verify_identity(binding) != {'app_id': binding['app_id'], 'open_id': binding['recipient_open_id']}:
        raise ManagementError('binding_conflict', 'Original delivery recipient is unverified.')
    intake.require_active(generation)
    execution['delivery_notification'] = {'signature': signature, 'status': 'intent'}
    intake._save_execution(record, fields=['delivery_notification'])
    anchor = record['source_anchor']
    result = await transport.send({'uuid': hashlib.sha256((record['id'] + ':acceptance:' + signature).encode()).hexdigest()[:32],
        'text': acceptance_text(record), 'chat_id': binding['chat_id'], 'reply_to': anchor['message_id'],
        'thread_id': anchor.get('thread_id')})
    intake.require_active(generation)
    execution['delivery_notification']['status'] = result.get('status', 'unknown')
    intake._save_execution(record, fields=['delivery_notification'])


def attach_existing_owned(intake, record):
    """Attach a registered generation; absence never starts replacement work."""
    from .dsh_owned_transport import PersistentOwnedTransport
    execution = record['dsh_execution']
    instance = intake.state_dir / 'owned-work' / record['id']
    if (not execution.get('native_identity') or not (instance / 'native-configuration.json').is_file()
        or not (instance / 'native-identity.json').is_file()):
        raise ManagementError('outcome_unknown', 'Original owned execution is not registered.')
    configuration, environment = execution_configuration(record['target'])
    if configuration['reference_sha256'] != execution['reference_sha256']:
        raise ManagementError('configuration_missing', 'Original admitted execution reference changed.')
    configuration.update(dsh_home=str(instance / 'home'), workspace=record['target']['repo_path'],
        instance_id=execution['instance_id'], generation=execution['generation'], session_id=execution['session_id'],
        trusted_controllers=[record['gateway_controller']],
        test_python=sys.executable,
        test_runner_path=str(Path(__file__).with_name('repository_test_runner.py').resolve(strict=True)))
    reference = execution.get('github_reference')
    if reference:
        environment.update(GH_CONFIG_DIR=reference['config_dir'], HERMES_OWNED_GITHUB_ACCOUNT=reference['account'],
            HERMES_OWNED_GITHUB_REPOSITORY=reference['repository'], GH_TOKEN='', GITHUB_TOKEN='',
            GH_ENTERPRISE_TOKEN='', GITHUB_ENTERPRISE_TOKEN='')
    return PersistentOwnedTransport(instance_dir=str(instance), configuration=configuration,
        node_bin=configuration.pop('node_bin'), timeout=15, environment=environment, attach_only=True)


def _admit_resumed_worker(intake, record, carrier):
    """Only the pinned gateway may admit a proven new claim of this same card."""
    from .trusted_controller import controller_identity
    from hermes_cli import kanban_db as kb
    from hermes_cli.kanban_db_connect import connect_closing
    if controller_identity() != record['gateway_controller']:
        raise ManagementError('unauthorized', 'Original gateway controller identity changed.')
    execution = record['dsh_execution']
    previous = execution.get('supervisor_claim')
    if not previous:
        return
    with connect_closing(board=intake.board) as connection:
        card = kb.get_task(connection, record['card_id'])
        old_run = kb.get_run(connection, previous['run_id'])
        run = kb.get_run(connection, card.current_run_id) if card and card.current_run_id else None
    if not card or card.status != 'running' or card.current_run_id == previous['run_id']:
        return
    replies = [*execution.get('questions', []), *execution.get('approvals', [])]
    resumes = [reply['resume_unblock'] for reply in replies
               if reply.get('reply_status') in {'accepted', 'settled'} and reply.get('admitted_seq')
               and reply.get('resume_unblock', {}).get('status') == 'accepted'
               and reply['resume_unblock'].get('previous_run_id') == previous['run_id']
               and reply['resume_unblock'].get('generation') == execution['generation']
               and reply['resume_unblock'].get('session_id') == execution['session_id']
               and reply['resume_unblock'].get('card_id') == record['card_id']]
    if (len(resumes) != 1 or old_run is None or old_run.status == 'running' or run is None
        or card.assignee != record['target']['native_profile'] or card.workspace_kind != 'dir'
        or Path(card.workspace_path).resolve(strict=True) != Path(record['target']['repo_path']).resolve(strict=True)
        or card.title != record['issue']['title'] or card.body != intake._card_body(record)
        or not card.claim_lock or card.claim_lock != run.claim_lock or card.worker_pid != run.worker_pid
        or run.task_id != card.id or run.status != 'running' or card.claim_expires is None
        or card.claim_expires <= time.time()):
        raise ManagementError('binding_conflict', 'Original same-card claim resumption is unverified.')
    old_identity = previous['identity']
    if psutil.pid_exists(old_identity['pid']):
        if controller_identity(old_identity['pid']) == old_identity:
            return
    identity = controller_identity(card.worker_pid)
    carrier.admit_controller(identity)
    execution['supervisor_claim'] = {'run_id': card.current_run_id, 'identity': identity}
    intake._save_execution(record, fields=['supervisor_claim'])


async def observe_human_requests(intake):
    """Gateway observes existing work because claimed workers have no group transport."""
    if os.environ.get('HERMES_KANBAN_RUN_ID'):
        return
    from .repository_questions import observe_questions
    from .repository_approvals import observe_approvals
    carriers = {}
    try:
        while not intake.closed:
            if intake.transports:
                for record in intake.snapshot()['work']:
                    execution = record.get('dsh_execution', {})
                    if execution.get('acceptance'):
                        try:
                            await notify_acceptance(intake, record, intake.generation)
                        except ManagementError:
                            pass
                    if not execution.get('native_identity') or execution.get('state') == 'released':
                        carrier = carriers.pop(record['id'], None)
                        if carrier is not None:
                            carrier.close()
                        continue
                    carrier = carriers.get(record['id'])
                    try:
                        if carrier is None:
                            carrier = attach_existing_owned(intake, record)
                            carriers[record['id']] = carrier
                        await asyncio.to_thread(_admit_resumed_worker, intake, record, carrier)
                        events, projections = await asyncio.to_thread(original_history, carrier,
                            execution['session_id'], record['target']['repo_path'])
                        await observe_questions(intake, record, carrier, events, projections, intake.generation)
                        await observe_approvals(intake, record, carrier, events, projections, intake.generation)
                    except (ManagementError, ValueError, KeyError, *_TRANSPORT_FAILURES):
                        if carrier is not None:
                            carrier.close()
                        carriers.pop(record['id'], None)
            await asyncio.sleep(.25)
    finally:
        for carrier in carriers.values():
            carrier.close()


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
            if tool_name == 'kanban_complete':
                try:
                    from .repository_acceptance import completion_ready
                    intake.require_active(intake.generation)
                    current_worker(ctx, intake, record)
                    supplied = args or {}
                    if (supplied.get('task_id', card_id) != card_id or supplied.get('board', intake.board) != intake.board
                        or not completion_ready(intake, record)):
                        raise ManagementError('unauthorized', 'Current original acceptance is unavailable.')
                    return
                except ManagementError:
                    return {'action': 'block', 'message': 'Original acceptance, fixed source and current worker claim must be verified before completion; repository occupancy is retained. Call hermes_pm_supervise({}) now.'}
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

    def completed(tool_name=None, args=None, status=None, **_):
        if tool_name != 'kanban_complete' or status != 'ok':
            return
        from agent.delegation_context import owned_kanban_task
        from hermes_cli import kanban_db as kb
        from hermes_cli.kanban_db_connect import connect_closing
        card_id = owned_kanban_task()
        record = next((r for r in intake.snapshot()['work'] if r.get('card_id') == card_id), None)
        if record is None or record.get('dsh_execution', {}).get('acceptance', {}).get('status') != 'accepted':
            return
        with connect_closing(board=intake.board) as connection:
            card = kb.get_task(connection, card_id)
        if card is not None and card.status == 'done':
            record['dsh_execution']['state'] = 'released'
            intake._save_execution(record, fields=['state'])
            reference = record['dsh_execution'].get('github_reference')
            if reference:
                from .repository_github import cleanup_github_execution
                try:
                    record['dsh_execution']['github_cleanup'] = cleanup_github_execution(reference, record['dsh_execution']['generation'])
                except ManagementError as error:
                    record['dsh_execution']['github_cleanup'] = {'status': 'unconfirmed', 'code': error.code}
                intake._save_execution(record, fields=['github_cleanup'])
    ctx.register_hook('post_tool_call', completed)
    ctx.register_system_prompt_section('hermes-pm-supervision',
        'For a plugin-managed Kanban outer card, read kanban_show, then call hermes_pm_supervise with no arguments. '
        'After supervision returns next_action=kanban_complete, call the original kanban_complete with the verified summary. '
        'After next_action=kanban_block, call kanban_block(kind="needs_input", reason="Dedicated execution awaits acceptance or reconciliation"), then end. '
        'Do not develop directly, create/link cards, or declare delivery. DSH owns the detailed repository work; the plugin records its real status.')

    async def run_work(record, generation):
        from .dsh_owned_transport import PersistentOwnedTransport
        from .trusted_controller import controller_identity
        run_id = current_worker(ctx, intake, record)
        admitted, environment = execution_configuration(record['target'])
        if record.get('dsh_execution', {}).get('reference_sha256') != admitted['reference_sha256']:
            raise ManagementError('configuration_missing', 'Original admitted execution reference changed.')
        lock = os.open(intake.lock_path, os.O_RDWR | os.O_NOFOLLOW)
        fcntl.flock(lock, fcntl.LOCK_EX)
        try:
            intake.require_active(generation)
            record = next(r for r in intake.snapshot()['work'] if r['id'] == record['id'])
            execution = record['dsh_execution']
            if execution['state'] in {'budget_stopped', 'execution_failed'}:
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
            if not execution.get('supervisor_claim'):
                execution['supervisor_claim'] = {'run_id': run_id, 'identity': controller_identity()}
                intake._save_execution(record, fields=['supervisor_claim'])
            instance = intake.state_dir / 'owned-work' / record['id']
            from .repository_github import prepare_github_execution
            prepared_github = await asyncio.to_thread(prepare_github_execution, intake.github, record['target'],
                instance, execution['generation'])
            if execution.get('github_reference') and execution['github_reference'] != prepared_github['reference']:
                raise ManagementError('binding_conflict', 'Original GitHub execution reference changed.')
            execution['github_reference'] = prepared_github['reference']
            intake._save_execution(record, fields=['github_reference'])
            environment.update(prepared_github['environment'])
            configuration = dict(admitted, dsh_home=str(instance / 'home'), workspace=record['target']['repo_path'],
                instance_id=execution['instance_id'], generation=execution['generation'], session_id=execution['session_id'],
                trusted_controllers=[record['gateway_controller']],
                test_python=sys.executable,
                test_runner_path=str(Path(__file__).with_name('repository_test_runner.py').resolve(strict=True)))
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
            elif len(existing) != 1 or existing[0].get('cwd') != configuration['workspace']:
                raise ManagementError('binding_conflict', 'Original visible Session binding differs.')
            else:
                _, original_projections = await asyncio.to_thread(original_history, carrier, sid, configuration['workspace'])
                if original_projections.get('agentPreset') != 'hermes-owned':
                    raise ManagementError('binding_conflict', 'Original Session preset projection differs.')
            execution['session_creation'] = 'registered'
            execution['native_identity'] = carrier.native_identity
            intake._save_execution(record)
            if not execution.get('native_title'):
                title = 'Hermes ' + record['id']
                await asyncio.to_thread(owned_call, carrier, 'session/rename', {'sessionId': sid, 'title': title})
                execution['native_title'] = title
                intake._save_execution(record)
            if not execution.get('first_input'):
                current_worker(ctx, intake, record)
                intake.require_active(generation)
                from .repository_acceptance import repository_source_state
                try:
                    execution['baseline'] = repository_source_state(record['target'])
                except ManagementError:
                    execution['baseline'] = None
                execution.update(first_input='intent', state='input_intent')
                intake._save_execution(record)
                answer = await asyncio.to_thread(owned_call, carrier, 'session/prompt', {'sessionId': sid,
                    'requestId': execution['request_id'], 'mode': 'queue', 'content': [{'type': 'text',
                    'text': 'Work only in this bound repository. Use the installed Matt skills to host the detailed workflow.\n'
                            + 'Frozen Issue: ' + record['issue']['url'] + '\n' + record['issue']['title'] + '\n' + record['issue']['body']
                            + '\nReport development and test results; do not claim outer delivery. '
                              'GitHub is limited to the bound repository in HERMES_OWNED_GITHUB_REPOSITORY. '
                              'Before every GitHub business operation switch gh to HERMES_OWNED_GITHUB_ACCOUNT, '
                              'read gh api user login and require an exact account match. Use the injected private GH_CONFIG_DIR; '
                              'never print credentials or copy them into repository files, reports or questions. '
                              'For acceptance finish with HERMES_REPOSITORY_DELIVERY_JSON followed by one JSON object with '
                              'source_commit, issue_updated_at, criteria [{text,test_call_ids}], test_files, fine_issue_urls, '
                              'pr_url (if any), sync_branches, and leftovers. Use original successful test tool call IDs. '
                              'The supervisor independently verifies the actual tests and fixed source.'}]})
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
                    observed_events=execution_event_references(events))
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
                    if state == 'awaiting_acceptance':
                        from .repository_acceptance import accept_repository_work
                        return await accept_repository_work(intake, record, carrier, events, projections, generation)
                    return {'status': state, 'card_id': record['card_id'], 'session_id': sid,
                            'next_action': 'kanban_block', 'block_kind': 'needs_input', 'delivered': False}
                await asyncio.sleep(.25)
            raise ManagementError('outcome_unknown', 'Original owned execution has no confirmed terminal boundary.')
        except (ManagementError, ValueError, KeyError, *_TRANSPORT_FAILURES) as error:
            original = error.__cause__ if isinstance(error, ManagementError) and isinstance(error.__cause__, _TRANSPORT_FAILURES) else error
            try:
                intake.require_active(generation)
                current_worker(ctx, intake, record)
                execution['state'] = 'outcome_unknown'
                if isinstance(error, ManagementError):
                    message = str(error)
                    for secret in environment.values():
                        if secret:
                            message = message.replace(secret, '[REDACTED_CREDENTIAL]')
                    execution['last_failure'] = {'code': error.code, 'message': message}
                if isinstance(original, _TRANSPORT_FAILURES):
                    message = str(original)
                    for secret in environment.values():
                        if secret:
                            message = message.replace(secret, '[REDACTED_CREDENTIAL]')
                    execution['transport_failure'] = {'type': type(original).__name__, 'message': message}
                intake._save_execution(record)
            except ManagementError:
                pass
            if isinstance(error, _TRANSPORT_FAILURES):
                raise _unknown_transport(error) from error
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
