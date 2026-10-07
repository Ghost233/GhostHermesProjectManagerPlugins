"""Exact reviewed migration IDs/digests in verified Owner group messages."""
import asyncio
from dataclasses import dataclass
import re

from .manager import ManagementError
from .messages import PreparedMessage


@dataclass(frozen=True)
class PreparedMigrationMessage(PreparedMessage):
    migration_details: dict


def parse(command):
    match = re.fullmatch(r'(准备迁移|核对迁移|回退迁移)\s+([A-Za-z0-9_.:-]+)\s+([a-f0-9]{64})(?:\s+会话\s+([A-Za-z0-9_.:-]+))?(?:\s+已处理\s+([A-Za-z0-9_,.:-]+))?', command)
    if match:
        action = {'准备迁移': 'prepare', '核对迁移': 'check', '回退迁移': 'rollback'}[match[1]]
        if (match[4] or match[5]) and action != 'check':
            return None
        return action, {'plan_id': match[2], 'digest': match[3], **({'session_id': match[4]} if match[4] else {}),
            **({'handled_manual_execution_ids': match[5].split(',')} if match[5] else {})}
    match = re.fullmatch(r'切换迁移\s+([A-Za-z0-9_.:-]+)\s+([a-f0-9]{64})\s+版本\s+(\d+)\s+封存\s+([A-Za-z0-9_.:-]+)\s+会话\s+([A-Za-z0-9_.:-]+)(?:\s+单入口\s+([A-Za-z0-9_.:-]+))?', command)
    if match:
        return 'activate', {'plan_id': match[1], 'digest': match[2], 'expected_version': int(match[3]),
            'archive_operation_id': match[4], 'session_id': match[5], **({'expected_old_profile_ids': [match[6]]} if match[6] else {})}


def reviewed(snapshot, binding, command):
    action, details = parse(command)
    plan = next((p for p in snapshot['migration_plans'] if p['id'] == details['plan_id'] and p['digest'] == details['digest']), None)
    entry = next((p for p in snapshot['profiles'] if p['id'] == binding['profile_id']), None)
    if not plan or not entry or not (entry['role'] == 'steward' and binding['project_id'] is None or
            entry['id'] in plan['approved_scope']['expected_profile_ids'] and entry['project_id'] == binding['project_id']):
        raise ManagementError('forbidden', 'Migration message must reference the exact plan in its registered public responsibility scope.')
    if action == 'activate':
        details['expected_profile_ids'] = plan['approved_scope']['expected_profile_ids']
    return details


async def process(entry, identity, prepared, generation):
    action, _ = parse(prepared.command)
    def call():
        with entry.lifecycle_lock:
            entry.require_active(generation)
            reviewed(entry.manager().read_snapshot(identity), prepared.binding, prepared.command)
            return entry.manager().migrate_profile(identity, action, prepared.migration_details)
    result = await asyncio.to_thread(call)
    entry.require_active(generation)
    from .questions import claim_reply_feedback, record_reply_feedback
    text = '迁移 ' + result['id'] + '：' + result['status'] + '。\n审定摘要：' + result['digest'] + '\n' + '\n'.join(result['needs_human'])
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
