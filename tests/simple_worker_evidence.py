"""Read original public worker/Session evidence; never replace SDK behavior."""
import hashlib
import json
import re
import psutil
import time


def verify_dsh_work(execution, configuration, *, skill_name):
    events = execution['observed_events']
    headers = [e['data']['header'] for e in events if e['type'] == 'request/header']
    contexts = [e['data'] for e in events if e['type'] == 'request/context']
    model = configuration['runtime_configuration']['model']
    budget = configuration['runtime_configuration']['budget']
    assert headers and contexts, 'Original request header/context must prove the model route.'
    for header in headers:
        assert header['config']['provider'] == model['provider']
        assert header['config']['model'] == model['model']
        assert header['config']['maxTokens'] == budget['max_output_tokens_per_request']
        assert {t['name'] for t in header['tools']} == {'ask_user_question', 'bash', 'job_kill', 'job_list', 'job_output', 'skill'}, {
            'original_catalogs': [[t['name'] for t in h['tools']] for h in headers]}
    assert any(c['provider'] == model['provider'] and c['model'] == model['model'] and
               c['contextWindow'] == model['configuration']['models'][0]['contextWindow'] for c in contexts)
    receipts = execution['tool_receipts']
    assert all(r['generation'] == execution['generation'] and r['session_id'] == execution['session_id'] for r in receipts)
    expected = b'native-worker\n'
    assert any(r['name'] == 'bash' and r['is_error'] is False and r.get('exit_code') == 0
        and r.get('stdout', {}).get('sha256') == hashlib.sha256(expected).hexdigest()
        and r['stdout']['byte_length'] == len(expected) and r['stdout']['truncated'] is False
        and r['sandbox']['mode'] == 'workspace-write' and r['sandbox']['denied'] is False for r in receipts), 'Original successful untruncated Bash verification output is required.'
    assert any(r['name'] == 'skill' and r['is_error'] is False and r.get('skill_name') == skill_name
               for r in receipts), 'The original skill tool must actually load the approved workflow.'
    return {'headers': [{'provider': h['config']['provider'], 'model': h['config']['model'],
                        'max_tokens': h['config']['maxTokens'], 'tools': [t['name'] for t in h['tools']]} for h in headers],
            'contexts': contexts, 'tool_results': receipts, 'generation': execution['generation'],
            'session_id': execution['session_id'], 'request_id': execution['request_id'],
            'first_input': execution['first_input'], 'skill': skill_name}


def verify_hermes_worker(home, task_id, *, model_name, base_url):
    from hermes_state import SessionDB
    text = (home / 'kanban/logs' / (task_id + '.log')).read_text()
    identifiers = re.findall(r'^Session: *([^\s]+)', text, re.MULTILINE)
    assert len(identifiers) == 1 and '[kanban-worker-exit] rc=0' in text
    database = SessionDB(db_path=home / 'state.db', read_only=True)
    try:
        session = database.get_session(identifiers[0])
        route = database.get_recent_session_model_route(identifiers[0])
        messages = database.get_messages(identifiers[0])
    finally:
        database.close()
    assert session and session['source'] == 'kanban'
    assert route and route['model'] == model_name and route['billing_base_url'].rstrip('/') == base_url.rstrip('/'), {
        'session_model': session.get('model'), 'actual_route': route, 'expected_model': model_name, 'expected_endpoint': base_url}
    assert route['api_call_count'] > 0
    calls, blocked, terminal, rejected_blocks = [], [], [], []
    for message in messages:
        value = message.get('tool_calls') or []
        value = json.loads(value) if isinstance(value, str) else value
        calls.extend(call['function']['name'] for call in value)
        if message['role'] == 'tool' and message.get('tool_name') in {'kanban_create', 'kanban_complete'}:
            response = json.loads(message.get('content') or '{}')
            assert 'Call hermes_pm_supervise({})' in response.get('error', '')
            blocked.append(message['tool_name'])
        if message['role'] == 'tool' and message.get('tool_name') == 'kanban_block':
            response = json.loads(message.get('content') or '{}')
            if response.get('ok') is True and response.get('task_id') == task_id:
                terminal.append('kanban_block')
            elif 'Original worker claim and supervision outcome must be verified' in response.get('error', ''):
                rejected_blocks.append('kanban_block')
    assert 'hermes_pm_supervise' in calls, 'The original worker must actually call supervision.'
    assert terminal, 'The original transcript must contain a successful native terminal handoff.'
    if model_name == 'fixture-model':
        assert set(blocked) == {'kanban_create', 'kanban_complete'}
        assert len(rejected_blocks) == 3, 'Premature, wrong-target and stale-claim block calls must be rejected by the registered Hook.'
    assert 'then call hermes_pm_supervise' in session['system_prompt']
    return {'session_id': identifiers[0], 'model': route['model'], 'api_calls': route['api_call_count'], 'tool_calls': calls,
            'blocked_calls': blocked, 'terminal_handoffs': terminal, 'rejected_block_calls': rejected_blocks,
            'supervision_prompt_present': True}


def remember_worker(connection, task_id):
    from hermes_cli import kanban_db as kb
    run = kb.latest_run(connection, task_id)
    assert run and run.worker_pid, 'Original default dispatcher must persist the actual worker.'
    process = psutil.Process(run.worker_pid)
    command = process.cmdline()
    cli_toolsets = command[command.index('--toolsets') + 1].split(',') if '--toolsets' in command else None
    return {'task_id': task_id, 'run_id': run.id, 'pid': run.worker_pid, 'birth': process.create_time(),
            'cli_toolsets': cli_toolsets}


def cleanup_worker(worker, home):
    from hermes_cli import kanban_db as kb
    from hermes_cli.kanban_db_connect import connect_closing
    from hermes_cli.kanban_db_dispatch import reap_terminal_workers
    try:
        process = psutil.Process(worker['pid'])
        assert process.create_time() == worker['birth'], 'Refuse cleanup of a different process generation.'
        try:
            code = process.wait(timeout=5)
            return {**worker, 'exit_code': code, 'cleanup': 'original_exit'}
        except psutil.TimeoutExpired:
            with connect_closing() as connection:
                run = kb.get_run(connection, worker['run_id'])
                assert run and run.task_id == worker['task_id'] and run.worker_pid == worker['pid']
                if run.ended_at is None:
                    assert kb.reclaim_task(connection, worker['task_id'], reason='Bounded synthetic fixture cleanup')
                else:
                    reap_terminal_workers(connection)
            code = process.wait(timeout=15)
            return {**worker, 'exit_code': code, 'cleanup': 'native_scope_cleanup'}
    except psutil.NoSuchProcess:
        text = (home / 'kanban/logs' / (worker['task_id'] + '.log')).read_text()
        codes = re.findall(r'\[kanban-worker-exit\] rc=(-?\d+)', text)
        return {**worker, 'exit_code': int(codes[-1]) if codes else None, 'cleanup': 'already_exited'}


def carrier_exit_evidence(saved):
    """An original exit receipt and missing exact generation, not a stop request."""
    config = json.loads(saved.read_text())
    identity = json.loads((saved.parent / 'native-identity.json').read_text())
    birth = json.loads((saved.parent / 'process-birth.json').read_text())
    receipt = json.loads((saved.parent / 'native-exit.json').read_text())
    assert all(receipt[k] == identity[k] for k in ('pid', 'generation', 'created_at_ms', 'configuration_sha256'))
    assert receipt['shutdown_requested'] is True and receipt['exit_code'] == 0
    from pathlib import Path
    assert not Path(config['socket_path']).exists(), 'The original generation socket must be gone.'
    deadline = time.monotonic() + 5
    while True:
        try:
            same = psutil.Process(birth['pid']).create_time() == birth['created_at']
        except psutil.NoSuchProcess:
            same = False
        if not same:
            break
        assert time.monotonic() < deadline, 'Original carrier still exists after its exit receipt.'
        time.sleep(.02)
    return {'pid': identity['pid'], 'generation': identity['generation'], 'exit_code': receipt['exit_code'],
            'creation_time': birth['created_at'], 'socket_removed': True, 'generation_gone': True}


def worker_failure_diagnostics(home, workers):
    """Retain original public route/tool feedback without archiving messages or credentials."""
    from hermes_state import SessionDB
    result = []
    for worker in workers:
        text = (home / 'kanban/logs' / (worker['task_id'] + '.log')).read_text()
        ids = re.findall(r'^Session: *([^\s]+)', text, re.MULTILINE)
        entry = dict(worker, original_log_sha256=hashlib.sha256(text.encode()).hexdigest(), sessions=[])
        if ids:
            database = SessionDB(db_path=home / 'state.db', read_only=True)
            try:
                for identity in ids:
                    route = database.get_recent_session_model_route(identity)
                    messages = database.get_messages(identity)
                    calls, feedback = [], []
                    for message in messages:
                        values = message.get('tool_calls') or []
                        values = json.loads(values) if isinstance(values, str) else values
                        calls.extend(call['function']['name'] for call in values)
                        if message['role'] == 'tool':
                            try:
                                payload = json.loads(message.get('content') or '{}')
                            except (TypeError, ValueError):
                                payload = {}
                            feedback.append({'name': message.get('tool_name'),
                                **{k: payload[k] for k in ('status', 'code', 'delivered', 'repository_retained') if k in payload}})
                    entry['sessions'].append({'id': identity, 'actual_route': route, 'tool_calls': calls, 'feedback': feedback})
            finally:
                database.close()
        result.append(entry)
    return result
