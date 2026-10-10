"""Original SDK request observation; never creates a session or an Owner decision."""
import hashlib
import json
from pathlib import Path


def capture_request(**event):
    from hermes_constants import get_hermes_home
    from hermes_state import SessionDB
    from utils import atomic_json_write
    home = get_hermes_home().resolve()
    marker = home / 'migration-plan.json'
    if marker != marker.resolve() or not marker.is_file() or marker.stat().st_size > 100000:
        return
    plan = json.loads(marker.read_text())
    session_id = event.get('session_id')
    request = event.get('request')
    prompt = event.get('system_prompt')
    if not isinstance(session_id, str) or not session_id or not isinstance(request, dict) or not isinstance(prompt, str) or not prompt:
        return
    db = SessionDB(db_path=home / 'state.db')
    try:
        session = db.get_session(session_id)
    finally:
        db.close()
    if not session or session.get('profile_name') != home.name or session.get('system_prompt') != prompt:
        return
    body = request.get('body')
    if not isinstance(body, dict) or request.get('_truncated') or body.get('_truncated'):
        return
    tools = body.get('tools') or []
    if not isinstance(tools, list) or len(tools) > 200 or len(tools) != event.get('tool_count'):
        return
    names = sorted(t.get('function', t).get('name') for t in tools if isinstance(t, dict) and isinstance(t.get('function', t).get('name'), str))
    capture = {'session_id': session_id, 'native_profile': home.name, 'plan_digest': plan['digest'],
        'prompt_digest': hashlib.sha256(prompt.encode()).hexdigest(), 'model': event.get('model'), 'provider': event.get('provider'),
        'tool_names': names, 'tools_digest': hashlib.sha256(json.dumps(tools, sort_keys=True).encode()).hexdigest(),
        'api_request_id': event.get('api_request_id'), 'started_at': event.get('started_at'),
        'origin': 'actual_sdk_pre_api_request', 'request_output': 'not_yet_verified'}
    directory = home / 'migration-session-captures'
    if directory != directory.resolve():
        return
    directory.mkdir(exist_ok=True)
    path = directory / (hashlib.sha256(session_id.encode()).hexdigest() + '.json')
    if path != path.resolve():
        return
    atomic_json_write(path, capture)
