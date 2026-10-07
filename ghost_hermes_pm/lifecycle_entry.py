"""Verified Owner mention commands; the shared manager owns all lifecycle effects."""
import asyncio
import hashlib
import json
import re

from .manager import ManagementError


def parse(command):
    match = re.fullmatch(r'(封存项目|恢复负责人)\s+([A-Za-z0-9_.:-]+)', command)
    if match:
        return ('archive' if match[1] == '封存项目' else 'restore'), {'profile_id': match[2]}
    match = re.fullmatch(r'核对生命周期\s+([A-Za-z0-9_.:-]+)(?:\s+已处理\s+([A-Za-z0-9_,.:-]+))?', command)
    if match:
        return 'check', {'operation_id': match[1], 'handled_manual_session_ids': match[2].split(',') if match[2] else []}
    return None


def allowed(snapshot, binding, command):
    action, details = parse(command)
    entry = next((p for p in snapshot['profiles'] if p['id'] == binding['profile_id']), None)
    if not entry or entry.get('archive_intent') not in (None, False):
        return False
    target = details.get('profile_id')
    if action == 'check':
        operation = next((o for o in snapshot['lifecycle_operations'] if o['id'] == details['operation_id']), None)
        target = operation['profile_id'] if operation else None
    return target is not None and (entry['role'] == 'steward' and entry['project_id'] is None and binding['project_id'] is None or target == entry['id'] and entry['project_id'] == binding['project_id'])


async def process(entry, identity, prepared, generation):
    action, details = parse(prepared.command)
    if action != 'check':
        details['operation_id'] = 'group-' + hashlib.sha256(json.dumps(prepared.envelope, sort_keys=True).encode()).hexdigest()
    def call():
        with entry.lifecycle_lock:
            entry.require_active(generation)
            snapshot = entry.manager().read_snapshot(identity)
            # The original duplicate archive remains addressable after its own entry stopped.
            duplicate = next((o for o in snapshot['lifecycle_operations'] if o['id'] == details['operation_id']), None)
            if not duplicate and not allowed(snapshot, prepared.binding, prepared.command):
                raise ManagementError('forbidden', 'The verified lifecycle message no longer has its original project scope.')
            return entry.manager().lifecycle(identity, action, details)
    result = await asyncio.to_thread(call)
    entry.require_active(generation)
    from .questions import claim_reply_feedback, record_reply_feedback
    text = '生命周期 ' + result['id'] + '：' + result['status'] + '。\n' + '\n'.join(result['needs_human'])
    segment = claim_reply_feedback(entry.manager(), identity, prepared.envelope, text)
    if segment:
        try:
            receipt = await prepared.transport.send(segment)
            entry.require_active(generation)
        except Exception:
            entry.require_active(generation)
            receipt = {'status': 'unknown'}
        record_reply_feedback(entry.manager(), identity, segment['id'], receipt)
    return {'action': 'skip'}
