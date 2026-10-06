"""Check an authenticated Dashboard export against observed test-group IDs.

This does not authenticate a JSON file or enable any runtime capability.
"""
import argparse
import json
from pathlib import Path


def check_snapshot(snapshot, *, chat_id, source_message_id, start_message_id, issue_url):
    if snapshot.get('status') != 'completed':
        raise ValueError('The management snapshot is unverified or offline.')
    matches = [r for r in snapshot.get('requests', []) if r.get('source_anchor', {}).get('chat_id') == chat_id
               and r['source_anchor'].get('message_id') == source_message_id]
    if len(matches) != 1:
        raise ValueError('The observed source must correspond to exactly one accepted request.')
    record = matches[0]
    anchor = record.get('task_start_anchor') or {}
    if record.get('acceptance') != 'accepted' or record.get('delivery') != 'delivered':
        raise ValueError('Acceptance and complete message delivery must both have evidence.')
    if record.get('execution') != 'waiting' or not record.get('unexecuted_reason'):
        raise ValueError('This slice must retain waiting execution and its reason.')
    if anchor.get('chat_id') != chat_id or anchor.get('message_id') != start_message_id:
        raise ValueError('The observed local task start anchor does not match.')
    if record.get('accepted_scope', {}).get('url') != issue_url:
        raise ValueError('The frozen Issue does not match the approved test Issue.')
    segments = [s for p in record.get('outbox', []) for s in p.get('segments', [])]
    if not segments or len({s.get('uuid') for s in segments}) != len(segments):
        raise ValueError('Each logical segment requires a unique persistent UUID.')
    receipt_ids = []
    for segment in segments:
        attempts = segment.get('attempts') or []
        receipt = attempts[-1] if attempts else {}
        if segment.get('status') != 'delivered' or receipt.get('status') != 'delivered' or receipt.get('chat_id') != chat_id or not receipt.get('message_id'):
            raise ValueError('Every segment requires its own actual delivery receipt.')
        receipt_ids.append(receipt['message_id'])
    if len(set(receipt_ids)) != len(receipt_ids):
        raise ValueError('Different logical segments cannot share a delivery message ID.')
    return {'status': 'passed', 'check': 'exported_snapshot_structure', 'request_id': record['id'],
            'segment_count': len(segments), 'real_group_verified': False}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('snapshot', type=Path)
    for name in ('chat-id', 'source-message-id', 'start-message-id', 'issue-url'):
        parser.add_argument('--' + name, required=True)
    args = parser.parse_args()
    try:
        snapshot = json.loads(args.snapshot.read_text())
        result = check_snapshot(snapshot, chat_id=args.chat_id, source_message_id=args.source_message_id,
                                start_message_id=args.start_message_id, issue_url=args.issue_url)
    except (OSError, ValueError, KeyError, TypeError):
        parser.exit(1, 'FAIL: snapshot or observed IDs do not establish the required record structure.\n')
    print(json.dumps(result))
    print('Real group observation and native authorization require separate evidence.')


if __name__ == '__main__':
    main()
