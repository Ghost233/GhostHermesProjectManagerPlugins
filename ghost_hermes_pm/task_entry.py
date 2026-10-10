"""Task shape validation and dispatch after each entry authenticates its client."""
from .manager import ManagementError


def dispatch_task(client, args):
    action = args.get('action')
    if action not in {'takeover', 'return'} and any(args.get(k) is not None for k in ('manual_session_id', 'grant_id')):
        raise ManagementError('invalid_change', 'Manual grant fields require takeover or return.')
    if action != 'answer' and any(args.get(k) is not None for k in ('human_request_id', 'reply_id', 'response')):
        raise ManagementError('invalid_change', 'Human response fields require answer action.')
    if action != 'prepare' and args.get('plan') is not None:
        raise ManagementError('invalid_change', 'Baseline plan requires preparation.')
    if action in {'takeover', 'return'}:
        if any(args.get(k) is not None for k in ('report', 'plan', 'instruction_id', 'text', 'human_request_id', 'reply_id', 'response')):
            raise ManagementError('invalid_change', 'Current-work grant fields cannot carry another operation.')
        if action == 'takeover':
            result = client.take_over_session(args.get('request_id'), args.get('manual_session_id'), args.get('grant_id'), args.get('expected_turn_id'))
        else:
            if args.get('manual_session_id') is not None or args.get('expected_turn_id') is not None:
                raise ManagementError('invalid_change', 'Return accepts the existing grant only.')
            result = client.return_session_control(args.get('request_id'), args.get('grant_id'))
    elif action == 'answer':
        if any(args.get(k) is not None for k in ('report', 'instruction_id', 'text', 'expected_turn_id')):
            raise ManagementError('invalid_change', 'Human response fields cannot carry other operations.')
        result = client.answer_human_request(args.get('request_id'), args.get('human_request_id'), args.get('reply_id'), args.get('response'))
    elif action == 'prepare':
        if args.get('report') is not None or any(args.get(k) is not None for k in ('instruction_id', 'text', 'expected_turn_id')):
            raise ManagementError('invalid_change', 'Preparation accepts only the explicit baseline plan.')
        result = client.prepare_task(args.get('request_id'), args.get('plan'))
    elif action in {'append', 'stop', 'continue'}:
        if args.get('report') is not None:
            raise ManagementError('invalid_change', 'Control cannot assert delivery evidence.')
        result = client.control_task(args.get('request_id'), action, args.get('instruction_id'), args.get('text'), args.get('expected_turn_id'))
    elif any(args.get(k) is not None for k in ('instruction_id', 'text', 'expected_turn_id')):
        raise ManagementError('invalid_change', 'Control fields require a control action.')
    elif action == 'delivery':
        result = client.record_task_delivery(args.get('request_id'), args.get('report'))
    else:
        operation = {'verify': 'verify_task_execution', 'start': 'start_task', 'refresh': 'refresh_task', 'reconcile': 'reconcile_task', 'source': 'refresh_task_source'}.get(action)
        if operation is None or args.get('report') is not None:
            raise ManagementError('invalid_change', 'Unsupported task operation.')
        result = getattr(client, operation)(args.get('request_id'))
    return result
