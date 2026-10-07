"""Short-lived native SDK process: one target home, no inherited secret scope."""
import asyncio
import hashlib
import json
import os
from pathlib import Path
import re
import sys


def _hash(raw):
    return hashlib.sha256(raw).hexdigest()


def _home(root, profile):
    name = profile['native_profile']
    if not re.fullmatch(r'[a-z0-9][a-z0-9_-]{0,63}', name) or name == 'default':
        raise ValueError('Only explicitly named Profiles support selective migration.')
    home = root / 'profiles' / name
    if home != home.resolve():
        raise ValueError('Native Profile is an unknown alias; it was preserved.')
    return home


def _preview(root, source):
    home = _home(root, source)
    materials = []
    for kind, relative in (('knowledge', 'memories/MEMORY.md'), ('persona', 'SOUL.md')):
        path = home / relative
        if path != path.resolve() or path.stat().st_size > 40000:
            raise ValueError('Original material is not a bounded canonical file.')
        raw = path.read_bytes()
        materials.append({'kind': kind, 'locator': relative, 'source_version': _hash(raw), 'source_digest': _hash(raw), 'text': raw.decode('utf-8')})
    return {'status': 'preview', 'source_profile_id': source['id'], 'materials': materials,
            'credentials': 'not_read', 'source_profile': 'not_modified'}


def _target(operation):
    return operation['bindings'][operation['plan']['target_profile_id']]


def _inspect(root, operation, session_id=None):
    target = _target(operation)
    home = _home(root, target)
    marker = home / 'migration-plan.json'
    receipt = json.loads(marker.read_text())
    if receipt['digest'] != operation['digest'] or not (home / 'gateway.parked').is_file():
        raise ValueError('Target material receipt or parked admission gate changed.')
    for relative, expected in receipt['files'].items():
        if relative.startswith('memories/'):
            continue  # Native approval may legitimately add only the reviewed staged entry.
        path = home / relative
        if path != path.resolve() or _hash(path.read_bytes()) != expected:
            raise ValueError('Prepared native target bytes changed; preserve them and review before retrying.')
    result = receipt['result']
    os.environ['HERMES_HOME'] = str(home)
    from tools.memory_tool import load_on_disk_store
    from tools.write_approval import MEMORY, get_pending
    store = load_on_disk_store()
    expected = {'knowledge': [e['text'] for e in operation['plan']['selection'] if e['kind'] == 'knowledge'],
                'preference': [p['statement'] for p in operation['plan']['preferences']]}
    actual = {'knowledge': store.memory_entries, 'preference': store.user_entries}
    if any(set(actual[k]) - set(expected[k]) for k in expected):
        raise ValueError('Prepared target contains unselected material; preserve it and review a new checkpoint.')
    for entry in result['selection_ledger']:
        if entry['kind'] == 'persona':
            continue
        text = next(t for t in expected[entry['kind']] if _hash(t.encode()) == entry['content_digest'])
        pending_id = entry['native_receipt'].get('pending_id')
        entry['status'] = ('written' if text in actual[entry['kind']] else 'pending_approval'
                           if pending_id and get_pending(MEMORY, pending_id) else 'rejected')
    result['status'] = ('prepared' if all(e['status'] == 'written' for e in result['selection_ledger']) else
                        'pending_approval' if any(e['status'] == 'pending_approval' for e in result['selection_ledger']) else 'blocked')
    result['needs_human'] = list(operation['plan']['human_steps']) + ([] if result['status'] == 'prepared' else ['Native memory remains pending or was denied/rejected; do not switch.'])
    result['switch_state'] = 'not_switched'
    if session_id:
        from hermes_state import SessionDB
        if not (home / 'state.db').is_file():
            raise ValueError('No actual target native session store is available.')
        db = SessionDB(db_path=home / 'state.db')
        try:
            session = db.get_session(session_id)
            messages = db.get_messages(session_id)
            route = db.get_recent_session_model_route(session_id)
        finally:
            db.close()
        plan = operation['plan']
        selected = [e['text'] for e in plan['selection']] + [p['statement'] for p in plan['preferences']]
        if not session or session.get('profile_name') != target['native_profile'] or session.get('model') != plan['execution']['model'] or not session.get('system_prompt') or not all(t in session['system_prompt'] for t in selected) or not any(m['role'] == 'user' for m in messages) or not any(m['role'] == 'assistant' and m.get('content') and m.get('display_kind') != 'failed_turn' for m in messages):
            raise ValueError('Actual new target session has not proved the selected persisted prompt and first request/output.')
        capture_path = home / 'migration-session-captures' / (_hash(session_id.encode()) + '.json')
        if capture_path != capture_path.resolve() or not capture_path.is_file() or capture_path.stat().st_size > 50000:
            raise ValueError('Normal plugin request capture is unavailable; enable the target plugin and run a new approved native session.')
        capture = json.loads(capture_path.read_text())
        if capture.get('origin') != 'actual_sdk_pre_api_request' or capture.get('session_id') != session_id or capture.get('native_profile') != target['native_profile'] or capture.get('plan_digest') != operation['digest'] or capture.get('prompt_digest') != _hash(session['system_prompt'].encode()):
            raise ValueError('Original target native request capture does not match the actual persisted session.')
        tools = capture['tool_names']
        from toolsets import resolve_toolset
        wanted = sorted({name for toolset in plan['execution']['toolsets'] for name in resolve_toolset(toolset)})
        if not route or route.get('billing_provider') != plan['execution']['provider'] or route.get('model') != plan['execution']['model'] or capture.get('model') != plan['execution']['model'] or capture.get('provider') != plan['execution']['provider'] or tools != wanted:
            raise ValueError('Actual new-session model/provider/tool configuration differs from the reviewed new responsibility.')
        from datetime import datetime
        created = datetime.fromisoformat(operation['created_at']).timestamp()
        if session.get('started_at', 0) < created:
            raise ValueError('Old native session cannot prove newly selected memory loading.')
        result['session_receipt'] = {'status': 'verified_persisted_prompt', 'session_id': session_id,
            'native_profile': target['native_profile'], 'prompt_digest': _hash(session['system_prompt'].encode()),
            'selected_digest': operation['digest'], 'request_output': 'observed', 'model': session['model'],
            'provider': route['billing_provider'], 'tool_names': tools}
        result['session_receipt']['request_capture'] = {k: capture[k] for k in ('origin', 'api_request_id', 'tools_digest', 'prompt_digest')}
    receipt['result'] = result
    marker.write_text(json.dumps(receipt))
    return result


def _prepare(root, work, operation, approval):
    plan = operation['plan']
    source = operation['bindings'][plan['source_profile_id']]
    target = _target(operation)
    home = _home(root, target)
    # Validate source bytes before touching the new target. A plan is frozen, never silently rebased.
    current = _preview(root, source)
    for entry in plan['selection']:
        original = next((m for m in current['materials'] if m['kind'] == entry['kind'] and m['locator'] == entry['locator']), None)
        if not original or any(entry[k] != original[k] for k in ('source_version', 'source_digest')) or entry['text'] not in original['text']:
            raise ValueError('Selected original material changed or the text is outside the reviewed source.')
    if home.exists():
        if (home / 'migration-plan.json').is_file():
            return _inspect(root, operation)
        raise ValueError('Target native Profile already exists without this migration receipt; it was preserved.')
    # Fresh SDK create notifies its own host. Provision under a private root without a gateway,
    # add the native parked marker BEFORE publishing to the actual multiplexer profiles root.
    preparation = root / '.migration-preparation' / operation['digest']
    if preparation != preparation.resolve():
        raise ValueError('Native preparation root is an unknown alias.')
    preparation.mkdir(parents=True, exist_ok=True)
    os.environ['HERMES_HOME'] = str(preparation)
    from hermes_cli.profiles import create_profile, parked_marker_path
    staged = preparation / 'profiles' / target['native_profile']
    if staged.exists():
        raise ValueError('Interrupted native preparation exists; preserve and reconcile the original intent.')
    staged = create_profile(target['native_profile'], no_skills=True)
    parked_marker_path(staged).touch()
    from hermes_cli.config import atomic_config_write
    config = {'model': {'default': plan['execution']['model'], 'provider': plan['execution']['provider']},
        'toolsets': plan['execution']['toolsets'], 'memory': {'write_approval': approval},
        'gateway': {'standalone': False}, 'plugins': {'enabled': ['ghost-hermes-pm']}}
    atomic_config_write(staged / 'config.yaml', config)
    os.environ['HERMES_HOME'] = str(staged)
    from hermes_constants import get_hermes_home
    if get_hermes_home().resolve() != staged:
        raise ValueError('Target native identity scope did not bind.')
    from tools.memory_tool import load_on_disk_store, memory_tool
    ledger = []
    selected = [(e['kind'], e['text'], e['locator'], e['source_digest']) for e in plan['selection'] if e['kind'] == 'knowledge']
    selected += [('preference', p['statement'], 'owner:' + operation['owner_origin']['subject'], operation['digest']) for p in plan['preferences']]
    for kind, text, locator, source_digest in selected:
        result = json.loads(memory_tool(action='add', target='user' if kind == 'preference' else 'memory', content=text, store=load_on_disk_store()))
        ledger.append({'kind': kind, 'locator': locator, 'source_digest': source_digest, 'content_digest': _hash(text.encode()),
            'target_identity_ref': target['identity_ref'], 'status': 'pending_approval' if result.get('staged') else 'written' if result.get('success') else 'rejected',
            'native_receipt': result})
    soul = '\n\n'.join(e['text'] for e in plan['selection'] if e['kind'] == 'persona')
    if soul:
        from hermes_cli.web_models import ProfileSoulUpdate
        from hermes_cli.web_routers.profiles import update_profile_soul, get_profile_soul
        asyncio.run(update_profile_soul(target['native_profile'], ProfileSoulUpdate(content=soul)))
        if asyncio.run(get_profile_soul(target['native_profile']))['content'] != soul:
            raise ValueError('Native SOUL write was not confirmed.')
        ledger.append({'kind': 'persona', 'content_digest': _hash(soul.encode()), 'target_identity_ref': target['identity_ref'], 'status': 'written'})
    else:
        # Owner chose no old persona; retain a new role statement rather than seeded source identity.
        from utils import atomic_write_text
        atomic_write_text(staged / 'SOUL.md', 'Independent ' + target['role'] + ' for ' + target['id'] + '.', preserve_mode=True)
    state = 'prepared' if all(e['status'] == 'written' for e in ledger) else 'pending_approval' if any(e['status'] == 'pending_approval' for e in ledger) else 'blocked'
    needs = list(plan['human_steps']) + ([] if state == 'prepared' else ['Review native pending or rejected memory writes; target remains parked.'])
    result = {'status': state, 'native_state': 'parked_created', 'selection_ledger': ledger, 'needs_human': needs,
        'material_receipt': {'profile': target['native_profile'], 'identity_ref': target['identity_ref'], 'plan_digest': operation['digest'],
                             'execution_config': config, 'credentials': 'fresh_not_copied', 'external_bank': 'not_copied', 'session': 'not_yet_verified'}}
    files = {str(p.relative_to(staged)): _hash(p.read_bytes()) for p in (staged / 'config.yaml', staged / 'SOUL.md')}
    for relative in ('memories/MEMORY.md', 'memories/USER.md'):
        if (staged / relative).exists():
            files[relative] = _hash((staged / relative).read_bytes())
    (staged / 'migration-plan.json').write_text(json.dumps({'digest': operation['digest'], 'files': files, 'result': result}))
    home.parent.mkdir(exist_ok=True)
    os.rename(staged, home)
    return result


def _checkpoint(root, work, operation):
    _inspect(root, operation)
    home = _home(root, _target(operation))
    directory = work / 'material-checkpoints' / operation['digest']
    if directory != directory.resolve():
        raise ValueError('Native material checkpoint is an unknown alias.')
    directory.mkdir(parents=True, exist_ok=True)
    files = {}
    for relative in ('config.yaml', 'SOUL.md', 'memories/MEMORY.md', 'memories/USER.md'):
        original = home / relative
        if not original.exists():
            continue
        raw = original.read_bytes()
        destination = directory / relative
        if destination.exists() and destination.read_bytes() != raw:
            raise ValueError('Immutable native material checkpoint differs; preserve both states for Owner review.')
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_bytes(raw)
        files[relative] = _hash(raw)
    return {'status': 'verified_selected_native_files', 'artifact_ref': str(directory.relative_to(work)),
        'files': files, 'excluded': ['.env', 'provider_banks', 'channels', 'state.db', 'cron', 'PIDs', 'source_profile', 'repositories']}


def _rollback(root, work, operation):
    checkpoint = operation.get('native_checkpoint')
    if not checkpoint:
        raise ValueError('Prepared native material checkpoint is unavailable; target remains gated.')
    home = _home(root, _target(operation))
    directory = work / checkpoint['artifact_ref']
    for relative, expected in checkpoint['files'].items():
        original, current = directory / relative, home / relative
        if original != original.resolve() or current != current.resolve() or _hash(original.read_bytes()) != expected or current.read_bytes() != original.read_bytes():
            raise ValueError('Native checkpoint or current target material changed; preserve manual data without overwriting it.')
    from hermes_cli.profiles import parked_marker_path, profiles_to_serve
    from gateway.control_socket import identify_gateway, request_unserve_profile
    marker = parked_marker_path(home)
    if marker != marker.resolve():
        raise ValueError('Target native admission marker is an unknown alias.')
    marker.touch()
    host = identify_gateway(root)
    control = 'parked'
    if host and _target(operation)['native_profile'] in host.get('served_profiles', []):
        answer = request_unserve_profile(root, _target(operation)['native_profile'])
        control = 'parked_unserve_confirmed' if answer and answer.get('unserved') == _target(operation)['native_profile'] and not answer.get('error') else 'parked_unserve_unverified'
    if any(name == _target(operation)['native_profile'] for name, _ in profiles_to_serve(True)):
        raise ValueError('Future target gateway/ticker admission remains available.')
    return {'target_control': control, 'target_configuration': 'checkpoint_bytes_verified',
        'native_memory': 'checkpoint_bytes_verified', 'related_executions': 'unverified', 'external_services': 'unverified',
        'old_entry': 'not_started', 'old_tasks': 'not_resumed', 'repositories': 'not_modified'}


def _activate(root, operation, identity_receipt):
    result = _inspect(root, operation, operation['session_receipt']['session_id'])
    if result['status'] != 'prepared':
        raise ValueError('Native material writes have not all completed.')
    from gateway.control_socket import CONTROL_PROTOCOL_VERSION, identify_gateway, request_serve_profile_hot, request_unserve_profile
    from hermes_cli.profiles import parked_marker_path
    target = _target(operation)
    home = _home(root, target)
    host = identify_gateway(root)
    if not host or host.get('protocol') != CONTROL_PROTOCOL_VERSION or host.get('hermes_home') != str(root) or target['native_profile'] in host.get('served_profiles', []) or operation['bindings'][operation['plan']['source_profile_id']]['native_profile'] in host.get('served_profiles', []):
        raise ValueError('Original multiplexer and inactive old/new route scope are unverified.')
    parked_marker_path(home).unlink()
    answer = request_serve_profile_hot(root, target['native_profile'])
    current = identify_gateway(root)
    if not answer or answer.get('served') != target['native_profile'] or answer.get('error') or not current or any(current.get(k) != host.get(k) for k in ('pid', 'start_time', 'hermes_home', 'profile')) or target['native_profile'] not in current.get('served_profiles', []):
        parked_marker_path(home).touch()
        request_unserve_profile(root, target['native_profile'])
        return {'status': 'blocked', 'switch_state': 'outcome_unknown', 'needs_human': ['Target serving was not confirmed; native parked intent restored. Verify related executions before any retry.']}
    return {'status': 'switched', 'switch_state': 'verified_native_served', 'needs_human': [],
        'switch_receipt': {'plan_digest': operation['digest'], 'host': {k: current.get(k) for k in ('pid', 'start_time', 'hermes_home', 'profile')},
            'native_profile': target['native_profile'], 'identity_receipt': identity_receipt, 'session_receipt': result['session_receipt'],
            'old_entry': 'not_started', 'old_tasks': 'not_resumed', 'scheduler': 'new_empty_store;old_entry_independently_verified'}}


def main():
    payload = json.load(sys.stdin)
    root, work = Path(payload['host_home']), Path(payload['work_dir'])
    if payload['action'] == 'preview':
        answer = _preview(root, payload['source'])
    elif payload['action'] == 'prepare':
        answer = _prepare(root, work, payload['operation'], payload['memory_write_approval'])
    elif payload['action'] == 'inspect':
        answer = _inspect(root, payload['operation'], payload.get('session_id'))
    elif payload['action'] == 'checkpoint':
        answer = _checkpoint(root, work, payload['operation'])
    elif payload['action'] == 'rollback':
        answer = _rollback(root, work, payload['operation'])
    elif payload['action'] == 'activate':
        answer = _activate(root, payload['operation'], payload['identity_receipt'])
    else:
        raise ValueError('Unknown native migration operation.')
    print(json.dumps(answer))


if __name__ == '__main__':
    try:
        main()
    except Exception as exc:
        print(json.dumps({'error': str(exc), 'code': 'capability_unverified'}))
        sys.exit(1)
