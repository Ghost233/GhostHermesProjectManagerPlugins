"""Durable project notifications derived from verified management facts."""
import hashlib
import json

from .manager import ManagementError


def state(data):
    return data.setdefault('notifications', {'events': {}, 'projects': {}, 'tasks': {}, 'health': {
        'supervision': 'unverified', 'delivery': 'unverified', 'last_checked_at': None}})


def snapshot(data, project_ids):
    saved = state(data)
    return {'events': [e for e in saved['events'].values() if e['project_id'] in project_ids],
            'health': dict(saved['health'])}


def run(manager, identity):
    with manager._lock, manager._db:
        version, data = manager._load()
        principal = manager._principal(identity, data)
        if principal is not None:
            raise ManagementError('forbidden', 'Only the manager supervision entry schedules global notifications.')
        now = manager.notification_clock()
        saved = state(data)
        active = {}
        for task in data['requests'].values():
            if not task.get('repository_released') and task.get('outer_task_status') != 'stopped':
                active.setdefault(task['project_id'], []).append(task)
        for project_id in list(saved['projects']):
            if project_id not in active:
                del saved['projects'][project_id]
        for project_id, tasks in active.items():
            schedule = saved['projects'].setdefault(project_id, {'summary_at': now})
            if now - schedule['summary_at'] < 900:
                continue
            text = '项目汇总：' + project_id + '\n' + '\n'.join(
                '负责人：' + t['profile_id'] + '；状态：' + t['execution'] + '\n进展：无新进展；交付：' + t.get('task_delivery', 'unmet') +
                '；PR：' + t.get('pr_status', 'none') + '\n阻塞：' + (t.get('unexecuted_reason') or '无已核实阻塞') +
                '\n待处理：' + ', '.join(q['id'] for q in t.get('human_requests', []) if q['resolution'] == 'pending') +
                '\n下一步：核对原服务与当前有效请求\nIssue：' + t['accepted_scope']['url'] for t in tasks)
            key = hashlib.sha256(json.dumps(['summary', project_id, now]).encode()).hexdigest()
            saved['events'][key] = {'id': key, 'kind': 'summary', 'project_id': project_id,
                'request_ids': [t['id'] for t in tasks], 'text': text, 'created_at': now, 'delivery': 'pending'}
            schedule['summary_at'] = now
        saved['health'].update(supervision='running', last_checked_at=now)
        manager._save(version, data)
        return {'status': 'completed', 'notifications': list(saved['events'].values()), 'health': saved['health']}
