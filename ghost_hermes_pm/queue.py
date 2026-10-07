"""Durable outer-task ordering for each canonical Git common directory."""
from datetime import datetime, timezone

from .manager import ManagementError


def enroll(data, record, repository):
    data['queue_sequence'] = data.get('queue_sequence', 0) + 1
    record['accepted_repository'] = repository
    record['queue'] = {'logical_repository': repository['logical_id'], 'sequence': data['queue_sequence'],
                       'arranged_at': datetime.now(timezone.utc).isoformat(), 'status': 'accepted',
                       'blocked_by': [], 'reason': 'Awaiting an explicit execution request.'}
    refresh(data)


def refresh(data):
    records = sorted((r for r in data['requests'].values() if r.get('queue')), key=lambda r: r['queue']['sequence'])
    for record in records:
        queue = record['queue']
        if record.get('repository_released'):
            queue.update(status='released', blocked_by=[], reason=None)
            continue
        if record.get('session'):
            queue.update(status='occupied', blocked_by=[], reason=record.get('unexecuted_reason'))
            continue
        blockers = [r['id'] for r in records if r['id'] != record['id'] and not r.get('repository_released') and
                    r['queue']['logical_repository'] == queue['logical_repository'] and
                    (r.get('session') or r['queue']['sequence'] < queue['sequence'])]
        queue.update(status='queued' if blockers else 'accepted', blocked_by=blockers,
                     reason='Waiting for earlier outer work or unknown execution in this logical repository.' if blockers else 'Awaiting an explicit execution request.')


def require_turn(manager, identity, record, version, data):
    refresh(data)
    queue = record.get('queue')
    if queue and queue['blocked_by']:
        record.update(execution='waiting', unexecuted_reason=queue['reason'])
        with manager._db:
            manager._save(version, data)
        manager.publish_request_message(identity, record['id'], 'progress',
            '仓库排队：' + queue['reason'] + '\n前项：' + ', '.join(queue['blocked_by']))
        raise ManagementError('repository_busy', queue['reason'])
