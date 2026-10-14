"""Known DSH durable Session payloads mapped to the plugin's task vocabulary."""
import json
import os
from pathlib import Path

from .manager import ManagementError


def approval_notice_status(events, request, turn, expected_id=None):
    """Read the original installed ApprovalService's turn-enclosed audit pair."""
    window = [event for event in events if turn['startSeq'] < event['seq'] < turn.get('endSeq', float('inf'))]
    matches = []
    for event in window:
        data = event.get('data')
        if (event['type'] == 'approval/asked' and isinstance(data, dict)
                and isinstance(data.get('id'), str) and data['id']
                and data.get('toolName') == request.get('toolName')
                and data.get('callId') == request.get('callId')
                and data.get('reason') == request.get('reason')):
            matches.append(event)
    if len(matches) != 1:
        return {'state': 'unverified', 'reason': 'approval_audit_not_unique'}
    asked = matches[0]
    audit_id = asked['data']['id']
    if expected_id is not None and audit_id != expected_id:
        return {'state': 'unverified', 'reason': 'approval_audit_changed'}
    decisions = [event for event in window if event['type'] == 'approval/decided'
        and isinstance(event.get('data'), dict) and event['data'].get('id') == audit_id]
    if not decisions:
        return {'state': 'pending', 'approval_id': audit_id}
    if (len(decisions) != 1 or decisions[0]['seq'] <= asked['seq']
            or not isinstance(decisions[0]['data'].get('outcome'), str)
            or decisions[0]['data']['outcome'] not in {'allowed-once', 'rejected', 'cancelled', 'unavailable'}):
        return {'state': 'unverified', 'reason': 'approval_decision_unverified', 'approval_id': audit_id}
    return {'state': 'resolved', 'approval_id': audit_id, 'outcome': decisions[0]['data']['outcome']}


def _text(content):
    if not isinstance(content, list) or any(not isinstance(block, dict) for block in content):
        raise ManagementError('capability_unverified', 'The original DSH message content is incomplete.')
    parts = [block.get('text') for block in content if block.get('type') == 'text']
    if any(not isinstance(part, str) for part in parts):
        raise ManagementError('capability_unverified', 'The original DSH text block is malformed.')
    return ''.join(parts)


def _tool_item(call_id, name, arguments, event, cwd):
    if not isinstance(call_id, str) or not call_id or not isinstance(name, str) or not name:
        raise ManagementError('capability_unverified', 'The original DSH tool call identity is incomplete.')
    item = {'type': 'dynamicToolCall', 'id': call_id, 'tool': name, 'status': 'inProgress',
            'nativeSeq': event['seq'], 'nativeType': event['type'], 'nativeArguments': arguments}
    if name == 'bash' and isinstance(arguments, dict) and isinstance(arguments.get('command'), str):
        workdir = arguments.get('workdir', cwd)
        if isinstance(workdir, str) and not Path(workdir).is_absolute() and isinstance(cwd, str):
            workdir = os.path.normpath(str(Path(cwd) / workdir))
        item.update(type='commandExecution', command=arguments['command'], cwd=workdir,
                    exitCode=None, aggregatedOutput='', nativeBackground=arguments.get('run_in_background') is True)
    return item


def fold_turns(events, cwd):
    """No exit-code inference from rendered shell output or job detail text."""
    turns, open_turn, calls, surface, surface_order = [], None, {}, {}, []
    for event in events:
        data, kind = event['data'], event['type']
        operation = event.get('surfaceOp')
        if isinstance(operation, dict) and operation.get('op') == 'replace' and kind == 'tool/result':
            start, end = operation.get('startSeq'), operation.get('endSeq')
            sources = event.get('sourceEventSeqs')
            if (set(operation) != {'op', 'startSeq', 'endSeq'} or type(start) is not int or type(end) is not int
                    or start != end or not 0 <= start < event['seq'] or start not in surface
                    or not isinstance(sources, list) or not sources or any(type(seq) is not int or not 0 <= seq < event['seq'] for seq in sources)
                    or len(set(sources)) != len(sources) or start not in sources):
                raise ManagementError('capability_unverified', 'The DSH result replacement has an invalid current surface range.')
            original = surface[start]
            if original['type'] != 'tool/result' or not isinstance(data.get('message'), dict):
                raise ManagementError('capability_unverified', 'The DSH result replacement does not name an original tool result.')
            def immutable(value):
                message = {**value['message'], 'content': None}
                return json.dumps({**value, 'message': message}, sort_keys=True, allow_nan=False)
            if immutable(data) != immutable(original['data']):
                raise ManagementError('capability_unverified', 'A DSH result replacement changed execution identity or outcome.')
            call_id = data['message'].get('toolCallId')
            if not isinstance(call_id, str) or call_id not in calls:
                raise ManagementError('capability_unverified', 'The DSH result replacement has no known original call.')
            item = calls[call_id][0]
            if item.get('nativeResultSeq') is None:
                raise ManagementError('capability_unverified', 'The DSH result replacement precedes execution settlement.')
            item.setdefault('surfaceRewrites', []).append({'nativeSeq': event['seq'], 'sourceSeq': start,
                'content': data['message'].get('content'), 'text': _text(data['message'].get('content'))})
            # Rendering may change; original execution output remains the receipt source.
            del surface[start]
            surface[event['seq']] = event
            surface_order[surface_order.index(start)] = event['seq']
            continue
        if kind in {'user/message', 'assistant/message', 'tool/result', 'system/message', 'developer/message'}:
            if isinstance(operation, dict):
                start, end = operation.get('startSeq'), operation.get('endSeq')
                sources = event.get('sourceEventSeqs')
                if (set(operation) != {'op', 'startSeq', 'endSeq'} or operation.get('op') != 'replace'
                        or type(start) is not int or type(end) is not int or not 0 <= start < event['seq'] or not 0 <= end < event['seq']
                        or start not in surface or end not in surface):
                    raise ManagementError('capability_unverified', 'The DSH surface replacement has an invalid current range.')
                first, last = surface_order.index(start), surface_order.index(end)
                if first > last:
                    raise ManagementError('capability_unverified', 'The DSH surface replacement reverses its current range.')
                shadowed = surface_order[first:last + 1]
                if (not isinstance(sources, list) or any(type(seq) is not int or not 0 <= seq < event['seq'] for seq in sources)
                        or len(set(sources)) != len(sources) or not set(shadowed) <= set(sources)):
                    raise ManagementError('capability_unverified', 'The DSH surface replacement lacks its original source range.')
                for seq in shadowed:
                    del surface[seq]
                surface_order[first:last + 1] = [event['seq']]
            else:
                surface_order.append(event['seq'])
            surface[event['seq']] = event
        if kind == 'turn/start':
            index = data.get('turn')
            if type(index) is not int or index < 1 or open_turn is not None or any(turn['nativeTurn'] == index for turn in turns):
                raise ManagementError('capability_unverified', 'The DSH turn journal has conflicting boundaries.')
            open_turn = {'id': 'dsh-turn:' + str(index), 'nativeTurn': index, 'startSeq': event['seq'],
                         'status': 'inProgress', 'items': [], 'itemsView': 'full'}
            turns.append(open_turn)
        elif kind == 'turn/end':
            if open_turn is None or type(data.get('turn')) is not int or data.get('turn') != open_turn['nativeTurn']:
                raise ManagementError('capability_unverified', 'The DSH turn ending has no matching start.')
            native_reason = data.get('reason')
            if not isinstance(native_reason, dict) or not isinstance(native_reason.get('kind'), str) or not native_reason['kind']:
                raise ManagementError('capability_unverified', 'The native DSH turn ending has a malformed reason.')
            reason = native_reason['kind']
            open_turn['status'] = {'completed': 'completed', 'error': 'failed', 'blocked': 'failed',
                                   'aborted': 'interrupted', 'interrupted': 'interrupted'}.get(reason, 'unknown')
            open_turn['endSeq'], open_turn['nativeEndReason'] = event['seq'], data.get('reason')
            open_turn = None
        elif kind == 'user/message' and open_turn is not None:
            if data.get('turn') not in (None, open_turn['nativeTurn']):
                raise ManagementError('capability_unverified', 'The DSH input journal cannot identify its original turn.')
            source = data.get('source', {})
            open_turn['items'].append({'type': 'userMessage', 'id': data.get('id', 'dsh-event:' + str(event['seq'])),
                                       'text': _text(data.get('content')), 'nativeSource': source,
                                       'nativeSeq': event['seq'], 'requestId': source.get('rpcId') if isinstance(source, dict) else None})
        elif kind == 'assistant/message' and open_turn is not None:
            message = data.get('message')
            if (data.get('turn') != open_turn['nativeTurn'] or not isinstance(message, dict)
                    or message.get('role') != 'assistant' or not isinstance(message.get('id'), str) or not message['id']):
                raise ManagementError('capability_unverified', 'The native DSH assistant message identity is incomplete.')
            open_turn['items'].append({'type': 'agentMessage', 'id': message['id'], 'text': _text(message.get('content')),
                                       'status': 'interrupted' if data.get('interrupted') is True else 'completed',
                                       'nativeSeq': event['seq'], 'nativeType': kind})
        elif kind in {'tool/call', 'tool/ptc-dispatch-start'} and open_turn is not None:
            nested = kind == 'tool/ptc-dispatch-start'
            call_id = data.get('subCallId') if nested else data.get('callId')
            if not isinstance(call_id, str) or not call_id:
                raise ManagementError('capability_unverified', 'The original DSH tool call identity is malformed.')
            if call_id in calls or not nested and (type(data.get('turn')) is not int or data.get('turn') != open_turn['nativeTurn']):
                raise ManagementError('capability_unverified', 'The original DSH tool has a duplicate or foreign turn identity.')
            if nested and (not isinstance(data.get('rootCallId'), str) or data['rootCallId'] not in calls
                           or not isinstance(data.get('parentCallId'), str)):
                raise ManagementError('capability_unverified', 'The DSH nested tool has no known root call.')
            arguments = data.get('arguments')
            if not nested:
                if not isinstance(arguments, str):
                    raise ManagementError('capability_unverified', 'The native DSH tool arguments are not recorded raw JSON.')
                try:
                    arguments = json.loads(arguments)
                except ValueError:
                    arguments = None
            item = _tool_item(call_id, data.get('name'), arguments, event, cwd)
            calls[call_id] = (item, open_turn['nativeTurn'], data.get('step'))
            open_turn['items'].append(item)
        elif kind in {'tool/result', 'tool/ptc-dispatch'}:
            nested = kind == 'tool/ptc-dispatch'
            message = data if nested else data.get('message')
            if not isinstance(message, dict):
                raise ManagementError('capability_unverified', 'The native DSH tool result is malformed.')
            call_id = data.get('subCallId') if nested else message.get('toolCallId')
            if not isinstance(call_id, str) or not call_id:
                raise ManagementError('capability_unverified', 'The original DSH tool result identity is malformed.')
            known = calls.get(call_id)
            if known is None or not nested and (type(data.get('turn')) is not int or data.get('turn') != known[1]
                                               or type(data.get('step')) is not int or data.get('step') != known[2]):
                raise ManagementError('capability_unverified', 'The DSH tool result has no matching original call.')
            item = known[0]
            if item['status'] != 'inProgress':
                raise ManagementError('capability_unverified', 'The DSH tool settled more than once.')
            if type(message.get('isError', False)) is not bool:
                raise ManagementError('capability_unverified', 'The DSH tool settlement did not confirm its outcome.')
            item.update(status='failed' if message.get('isError') is True else 'completed', nativeResultSeq=event['seq'],
                        nativeResultText=_text(message.get('content')))
            if item['type'] == 'commandExecution':
                item['aggregatedOutput'] = item['nativeResultText']
            if 'meta' in data:
                item['nativeMeta'] = data['meta']
        elif open_turn is not None:
            # Extensible events keep their producer payload and cannot prove tool settlement.
            open_turn['items'].append({'type': 'dshEvent', 'id': 'dsh-event:' + str(event['seq']),
                                       'nativeType': kind, 'nativeData': data, 'nativeSeq': event['seq'], 'status': 'unknown'})
    return turns
