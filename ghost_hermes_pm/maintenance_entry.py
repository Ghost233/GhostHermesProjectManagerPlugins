"""Exact Owner maintenance decisions at the verified public group boundary."""
import asyncio
from dataclasses import dataclass
import json
import re

from .manager import ManagementError
from .messages import PreparedMessage


@dataclass(frozen=True)
class PreparedMaintenanceMessage(PreparedMessage):
    maintenance_details: dict


def parse(command):
    actions = {'进入维护': 'enter', '核对维护': 'check', '建立维护检查点': 'checkpoint',
               '切换维护版本': 'switch', '回退维护': 'rollback', '重新启用': 'reenable', '主动停用': 'deactivate'}
    match = re.fullmatch(r'(' + '|'.join(actions) + r')\s+(\{.*\})', command, re.DOTALL)
    if match:
        details = json.loads(match[2])
        if isinstance(details, dict):
            return actions[match[1]], details
    return None


def reviewed(snapshot, binding, command):
    action, details = parse(command)
    profile = next((p for p in snapshot['profiles'] if p['id'] == binding['profile_id']), None)
    if not profile or profile['role'] != 'steward' or profile['project_id'] is not None or binding['project_id'] is not None:
        raise ManagementError('forbidden', 'Instance maintenance must use the verified Owner public steward entry.')
    if action not in {'check', 'checkpoint'} and (type(details.get('expected_version')) is not int or details['expected_version'] != snapshot['version'] or details.get('expected_profile_ids') != sorted(p['id'] for p in snapshot['profiles'])):
        raise ManagementError('binding_conflict', 'The original Owner message must explicitly review the current directory version and complete Profile scope.')
    return details


async def process(entry, identity, prepared, generation):
    action, _ = parse(prepared.command)
    def call():
        with entry.lifecycle_lock:
            entry.require_active(generation)
            reviewed(entry.manager().read_snapshot(identity), prepared.binding, prepared.command)
            return entry.manager().maintenance(identity, action, prepared.maintenance_details)
    result = await asyncio.to_thread(call)
    entry.require_active(generation)
    from .questions import claim_reply_feedback, record_reply_feedback
    text = '维护 ' + result['id'] + '：' + result['status'] + '。\n' + '\n'.join(result['needs_human'])
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
