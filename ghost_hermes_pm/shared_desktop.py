"""Attach to the Launcher-owned Desktop; own only one new Session and this client."""
import hashlib
import http.client
import json
import os
from pathlib import Path
import stat
import threading
from urllib.parse import urlsplit
import uuid

import psutil

from .dsh_remote import NativeRemoteTransport
from .manager import ManagementError


def desktop_identity(reference, expected_lease):
    """Consume the existing native receipt, without publishing its login URL."""
    path = Path(reference)
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
        try:
            info = os.fstat(fd)
            if (not path.is_absolute() or str(path.resolve(strict=True)) != str(path)
                or not stat.S_ISREG(info.st_mode) or info.st_nlink != 1
                or info.st_uid != os.getuid() or info.st_mode & 0o077):
                raise ValueError('Private canonical receipt required.')
            with os.fdopen(fd, closefd=False) as stream:
                row = json.load(stream)
        finally:
            os.close(fd)
        url = urlsplit(row['url'])
        from urllib.parse import parse_qs
        token = parse_qs(url.query)
        if (row.get('schema') != 1 or row.get('lease') != expected_lease
            or type(row.get('pid')) is not int or row['pid'] < 2
            or url.scheme != 'http' or url.hostname != '127.0.0.1' or not url.port
            or url.path not in {'', '/'} or url.username or url.password or url.fragment
            or set(token) != {'token'} or len(token['token']) != 1 or not token['token'][0]):
            raise ValueError('Original Launcher identity differs.')
        process = psutil.Process(row['pid'])
        birth = process.create_time()
        if process.status() == psutil.STATUS_ZOMBIE:
            raise ValueError('Desktop is no longer live.')
        endpoint = 'http://127.0.0.1:' + str(url.port)
        identity = {'kind': 'shared_native', 'lease': expected_lease, 'pid': row['pid'],
            'created_at': birth, 'endpoint_sha256': hashlib.sha256(endpoint.encode()).hexdigest(),
            'runtime_version': row['runtimeVersion']}
        return identity, endpoint, url.path + '?' + url.query
    except (OSError, KeyError, ValueError, TypeError, psutil.Error):
        raise ManagementError('binding_conflict', 'The existing Desktop receipt or live identity is unverified.') from None


class SharedDesktopTransport:
    """Original HTTP/Remote mux with a strict owned-session mutation fence."""
    _reads = {'session/list', 'session/projections', 'session/page', 'commands/list'}
    _mutations = {'workspace/create', 'session/create', 'session/prompt', 'session/cancel', 'session/rename',
                  'session/updateQueue', 'job/kill', 'userQuestions/answer', '$events/result', 'commands/execute'}

    def __init__(self, configuration, timeout=15):
        self.config, self.timeout = dict(configuration), timeout
        self.native_identity, self.base_url, login = desktop_identity(
            self.config['desktop_receipt'], self.config['host_lease'])
        self.native_identity['service_ref'] = self.config['service_ref']
        self._lock = threading.RLock()
        self._frames, self._pending, self._resolved = [], {}, set()
        self._jobs, self._stream, self._reader = {}, None, None
        self._closed, self._event_failed, self.keep_open = False, False, False
        self.client_id = None
        parsed = urlsplit(self.base_url)
        connection = http.client.HTTPConnection(parsed.hostname, parsed.port, timeout=timeout)
        try:
            connection.request('GET', login)
            response = connection.getresponse()
            cookies = response.headers.get_all('set-cookie', [])
            if response.status != 303 or response.getheader('location') != './' or len(cookies) != 1:
                raise ValueError('Native login exchange was not confirmed.')
            cookie = cookies[0].split(';', 1)[0]
            if not cookie or '=' not in cookie:
                raise ValueError('Native authentication cookie is absent.')
            response.read(1024)
        except (OSError, ValueError, http.client.HTTPException):
            raise ManagementError('unavailable', 'The existing Desktop native authentication exchange failed.') from None
        finally:
            connection.close()
        self._transport = NativeRemoteTransport(self.base_url, cookie, timeout)
        self._alive()

    def _alive(self):
        current, endpoint, _ = desktop_identity(self.config['desktop_receipt'], self.config['host_lease'])
        current['service_ref'] = self.config['service_ref']
        if self._closed or current != self.native_identity or endpoint != self.base_url:
            raise ManagementError('binding_conflict', 'The original shared Desktop generation changed.')

    def request(self, method, args, rpc_id):
        self._alive()
        if method not in self._reads | self._mutations:
            raise ManagementError('unauthorized', 'Only original owned Session methods are permitted.')
        request = args.get('request', args) if isinstance(args, dict) else None
        if not isinstance(request, dict):
            raise ManagementError('invalid_change', 'An original native request is required.')
        if method == 'workspace/create':
            if (request != {'path': self.config['workspace']}
                    or str(Path(self.config['workspace']).resolve(strict=True)) != self.config['workspace']):
                raise ManagementError('unauthorized', 'Only the bound canonical repository Workspace may be resolved.')
        elif method in self._mutations and method != '$events/result':
            target = request.get('agentId') if method in {'userQuestions/answer', 'commands/execute'} else request.get('sessionId')
            if target != self.config['session_id']:
                raise ManagementError('unauthorized', 'The mutation is outside the owned shared Session.')
            if method == 'session/create':
                location = ({'workspaceId': self.config['workspace_id']} if 'workspaceId' in request
                            and self.config.get('workspace_id') else {'cwd': self.config['workspace']})
                if request != {'sessionId': self.config['session_id'], 'agentPreset': self.config['agent_preset'], **location}:
                    raise ManagementError('binding_conflict', 'Owned Session creation scope differs.')
            if method == 'job/kill' and self._jobs.get(request.get('jobId')) != target:
                raise ManagementError('unauthorized', 'Only an exactly owned native job may be cancelled.')
            if method == 'commands/execute' and request != {'agentId': self.config['session_id'],
                    'line': '/permission workspace-write', 'submittedAttachments': []}:
                raise ManagementError('unauthorized', 'Only the exact owned native workspace permission command is permitted.')
        if method == 'commands/list' and request != {'agentId': self.config['session_id']}:
            raise ManagementError('unauthorized', 'Only the owned Session command catalog is available.')
        if method in {'session/projections', 'session/page'}:
            address = request.get('address', {})
            if address.get('kind') != 'session' or address.get('sessionId') != self.config['session_id']:
                raise ManagementError('unauthorized', 'Only the owned Session history is visible to this worker.')
        if method == '$events/result':
            with self._lock:
                frame = self._pending.get(request.get('eventId'))
                if (request.get('clientId') != self.client_id or not frame
                        or frame['agentId'] != self.config['session_id']
                        or request['eventId'] in self._resolved):
                    raise ManagementError('binding_conflict', 'The original owned human request is no longer pending.')
                outcome = request.get('outcome', {})
                if set(outcome) != {'kind', 'value'} or outcome.get('kind') != 'result':
                    raise ManagementError('invalid_change', 'Only an explicit original human response is permitted.')
                if frame['event'] == 'approval/request' and outcome['value'] not in {'allowed-once', 'rejected'}:
                    raise ManagementError('unauthorized', 'Shared execution approvals permit only this operation or rejection.')
                if frame['event'] == 'user-questions/request' and (not isinstance(outcome['value'], dict)
                        or set(outcome['value']) != {'answers'} or not isinstance(outcome['value']['answers'], list)):
                    raise ManagementError('invalid_change', 'The original question requires its explicit answer batch.')
                self._resolved.add(request['eventId'])
            native_args = request
        elif method.startswith('commands/'):
            native_args = request
        else:
            native_args = {'_request': {}} if method == 'session/list' else {'request': request}
        packet = self._transport.request(method, native_args, rpc_id)
        if method == 'workspace/create' and packet.get('result', {}).get('ok') is True:
            value = packet['result'].get('value', {})
            workspace = value.get('workspace', {})
            identifiers = workspace.get('sessionIds')
            identifier = workspace.get('workspaceId')
            if (packet.get('type') != 'server-response' or packet.get('rpcId') != rpc_id
                    or type(value.get('created')) is not bool or not isinstance(identifier, str) or not identifier
                    or workspace.get('path') != self.config['workspace']
                    or not isinstance(identifiers, list) or any(not isinstance(item, str) or not item for item in identifiers)
                    or len(identifiers) != len(set(identifiers))
                    or self.config.get('workspace_id', identifier) != identifier):
                raise ManagementError('binding_conflict', 'The original bound Workspace identity or Session account differs.')
            self.config['workspace_id'] = identifier
        return packet

    def stream(self, endpoint, args):
        self._alive()
        request = args.get('request', {})
        target = request.get('sessionId', request.get('address', {}).get('sessionId'))
        if endpoint not in {'session/follow', 'job/list', 'job/follow'} or target != self.config['session_id']:
            raise ManagementError('unauthorized', 'Only owned original streams are permitted.')
        return self._transport.stream(endpoint, args)

    def owned_jobs(self):
        source = self.stream('job/list', {'request': {'sessionId': self.config['session_id']}})
        try:
            roster = next(source)
        finally:
            source.close()
        rows = roster.get('jobs')
        if roster.get('type') != 'rows' or not isinstance(rows, list):
            raise ManagementError('outcome_unknown', 'Native shared job roster is unavailable.')
        if any(row.get('owner') not in {None, self.config['session_id']} for row in rows):
            raise ManagementError('binding_conflict', 'Native job visibility crossed the owned Session.')
        owned = [row for row in rows if row.get('owner') == self.config['session_id']]
        if any(not isinstance(row.get('id'), str) or row.get('status') not in
               {'running', 'stopping', 'completed', 'killed', 'failed'} for row in owned):
            raise ManagementError('outcome_unknown', 'Original owned job lifecycle is malformed.')
        self._jobs = {row['id']: row['owner'] for row in owned}
        return owned

    def start_events(self):
        """One gateway client holds this work's waterfalls; workers never subscribe."""
        with self._lock:
            self._alive()
            if self.client_id is not None:
                return self.event_frames()
            self._stream = self._transport.stream('$events', {})
            ready = next(self._stream)
            if ready.get('type') != 'ready' or not isinstance(ready.get('clientId'), str) or not ready['clientId']:
                self._stream.close()
                raise ManagementError('outcome_unknown', 'Original shared event opening is unconfirmed.')
            self.client_id = ready['clientId']
            self._reader = threading.Thread(target=self._read_events, name='hermes-pm-shared-events', daemon=True)
            self._reader.start()
            return self.event_frames()

    def _read_events(self):
        try:
            for frame in self._stream:
                self._alive()
                if frame.get('type') == 'waterfall':
                    if (frame.get('agentId') != self.config['session_id']
                            or frame.get('event') not in {'approval/request', 'user-questions/request'}):
                        self._transport.request('$events/result', {'clientId': self.client_id,
                            'eventId': frame['eventId'], 'outcome': {'kind': 'next'}}, str(uuid.uuid4()))
                        continue
                    with self._lock:
                        self._pending[frame['eventId']] = frame
                        self._frames.append(frame)
                elif frame.get('type') == 'resolved':
                    with self._lock:
                        self._pending.pop(frame['eventId'], None)
                        self._frames.append(frame)
                if len(self._frames) > 10000:
                    raise ValueError('Original event history exceeded its bound.')
            if not self._closed:
                self._event_failed = True
        except Exception:
            if not self._closed:
                self._event_failed = True

    def event_frames(self):
        self._alive()
        with self._lock:
            if self._event_failed or self.client_id is None:
                raise ManagementError('outcome_unknown', 'The original shared human observer is unconfirmed.')
            return {'generation': self.config['generation'], 'session_id': self.config['session_id'],
                    'client_id': self.client_id, 'frames': list(self._frames)}

    def admit_controller(self, identity):
        self._alive()
        return identity  # The original Kanban claim is checked by the calling gateway.

    def close(self):
        if not self.keep_open:
            self.dispose()

    def dispose(self):
        with self._lock:
            self._closed = True
        self._transport.close()
        if self._reader and self._reader is not threading.current_thread():
            self._reader.join(timeout=2)
