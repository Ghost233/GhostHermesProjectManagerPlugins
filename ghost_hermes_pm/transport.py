"""Private, token-authenticated local bridge to the one Gateway manager."""
from pathlib import Path
import fcntl
import hmac
import json
import os
import socket
import socketserver
import threading
import struct

from .manager import ManagementError


def _read_frame(reader, limit=None):
    def read_exact(size):
        chunks = []
        remaining = size
        while remaining:
            chunk = reader.read(min(remaining, 65536))
            if not chunk:
                raise ValueError('Incomplete management frame.')
            chunks.append(chunk)
            remaining -= len(chunk)
        return b''.join(chunks)
    size = struct.unpack('!Q', read_exact(8))[0]
    if limit is not None and size > limit:
        raise ValueError('Management request frame exceeds its bound.')
    return json.loads(read_exact(size))


def _frame(value):
    payload = json.dumps(value).encode()
    return struct.pack('!Q', len(payload)) + payload


class ManagementServer:
    def __init__(self, manager, credentials):
        self.manager = manager
        self.credentials = credentials
        self.path = manager.state_dir / 'manager.sock'
        self._server = None
        self._lease = None

    def start(self):
        self._lease = open(self.manager.state_dir / 'manager.lock', 'a')
        try:
            fcntl.flock(self._lease, fcntl.LOCK_EX | fcntl.LOCK_NB)
            if self.path.exists() or self.path.is_symlink():
                from .recovery import reclaim_manager_socket
                reclaim_manager_socket(self.path)
            bridge = self

            class Handler(socketserver.StreamRequestHandler):
                def handle(self):
                    self.request.settimeout(3)
                    try:
                        payload = _read_frame(self.rfile, limit=1024 * 1024)
                        if not isinstance(payload, dict) or set(payload) - {'token', 'operation', 'expected_version', 'change', 'scope', 'request_id', 'report', 'action', 'instruction_id', 'text', 'expected_turn_id', 'human_request_id', 'reply_id', 'response', 'plan', 'source', 'source_id', 'query_id', 'question', 'scope_ids', 'channel_id', 'auto_supplement', 'material_ids', 'registration', 'details', 'profile_id', 'entry_id', 'selection', 'supersedes', 'include_superseded', 'entry_ids', 'statement', 'manual_session_id', 'grant_id', 'complete', 'backup_id', 'restore_id', 'protection_id', 'kind'}:
                            raise ManagementError('invalid_change', 'Unknown bridge fields; caller identity is not a body field.')
                        token = payload.get('token', '')
                        identity = next((identity for secret, identity in bridge.credentials.items()
                                         if isinstance(token, str) and hmac.compare_digest(secret.encode(), token.encode())), None)
                        if identity is None:
                            raise ManagementError('unauthorized', 'Invalid entry credential.')
                        if payload.get('operation') in {'read_snapshot', 'read_participant_snapshot'}:
                            if payload['operation'] == 'read_participant_snapshot' and identity.subject == bridge.manager.owner_identity_ref:
                                raise ManagementError('forbidden', 'The participant entry cannot borrow owner authority.')
                            result = bridge.manager.read_snapshot(identity, payload.get('scope'))
                        elif payload.get('operation') == 'manage_notifications':
                            result = bridge.manager.manage_notifications(identity, payload.get('action'), payload.get('details'))
                        elif payload.get('operation') == 'run_notifications':
                            result = bridge.manager.run_notifications(identity)
                        elif payload.get('operation') == 'global_validation':
                            result = bridge.manager.global_validation(identity, payload.get('action'), payload.get('details'))
                        elif payload.get('operation') == 'lifecycle':
                            result = bridge.manager.lifecycle(identity, payload.get('action'), payload.get('details'))
                        elif payload.get('operation') == 'take_over_session':
                            result = bridge.manager.take_over_session(identity, payload.get('request_id'), payload.get('manual_session_id'), payload.get('grant_id'), payload.get('expected_turn_id'))
                        elif payload.get('operation') == 'return_session_control':
                            result = bridge.manager.return_session_control(identity, payload.get('request_id'), payload.get('grant_id'))
                        elif payload.get('operation') == 'collaborate':
                            result = bridge.manager.collaborate(identity, payload.get('action'), payload.get('details'))
                        elif payload.get('operation') == 'register_observation_source':
                            result = bridge.manager.register_observation_source(identity, payload.get('registration'))
                        elif payload.get('operation') == 'refresh_manual_sessions':
                            result = bridge.manager.refresh_manual_sessions(identity, payload.get('scope'))
                        elif payload.get('operation') == 'apply_directory_change':
                            result = bridge.manager.apply_directory_change(identity, payload.get('expected_version'), payload.get('change'))
                        elif payload.get('operation') == 'prepare_task':
                            result = bridge.manager.prepare_task(identity, payload.get('request_id'), payload.get('plan'))
                        elif payload.get('operation') in {'start_task', 'refresh_task', 'reconcile_task', 'verify_task_execution', 'refresh_task_source'}:
                            result = getattr(bridge.manager, payload['operation'])(identity, payload.get('request_id'))
                        elif payload.get('operation') == 'control_task':
                            result = bridge.manager.control_task(identity, payload.get('request_id'), payload.get('action'),
                                payload.get('instruction_id'), payload.get('text'), payload.get('expected_turn_id'))
                        elif payload.get('operation') == 'answer_human_request':
                            result = bridge.manager.answer_human_request(identity, payload.get('request_id'), payload.get('human_request_id'), payload.get('reply_id'), payload.get('response'))
                        elif payload.get('operation') == 'answer_from_knowledge':
                            result = bridge.manager.answer_from_knowledge(identity, payload.get('request_id'), payload.get('human_request_id'), payload.get('query_id'), payload.get('material_ids'))
                        elif payload.get('operation') == 'curate_project_memory':
                            result = bridge.manager.curate_project_memory(identity, payload.get('profile_id'), payload.get('entry_id'), payload.get('request_id'), payload.get('selection'), payload.get('supersedes'))
                        elif payload.get('operation') == 'read_project_memory':
                            result = bridge.manager.read_project_memory(identity, payload.get('profile_id'), payload.get('include_superseded', False))
                        elif payload.get('operation') == 'load_project_memory':
                            result = bridge.manager.load_project_memory(identity, payload.get('request_id'), payload.get('entry_ids'))
                        elif payload.get('operation') == 'record_memory_preference':
                            result = bridge.manager.record_memory_preference(identity, payload.get('profile_id'), payload.get('entry_id'), payload.get('statement'), payload.get('scope'), payload.get('supersedes'))
                        elif payload.get('operation') == 'supplement_project_memory':
                            result = bridge.manager.supplement_project_memory(identity, payload.get('request_id'), payload.get('entry_ids'), payload.get('expected_turn_id'))
                        elif payload.get('operation') == 'manage_memory':
                            result = bridge.manager.manage_memory(identity, payload.get('action'), payload.get('details'))
                        elif payload.get('operation') == 'record_task_delivery':
                            result = bridge.manager.record_task_delivery(identity, payload.get('request_id'), payload.get('report'))
                        elif payload.get('operation') == 'register_knowledge_source':
                            result = bridge.manager.register_knowledge_source(identity, payload.get('expected_version'), payload.get('source'))
                        elif payload.get('operation') == 'query_knowledge':
                            result = bridge.manager.query_knowledge(identity, payload.get('source_id'), payload.get('query_id'), payload.get('question'), payload.get('scope_ids'), payload.get('request_id'), payload.get('channel_id'), payload.get('auto_supplement', False))
                        elif payload.get('operation') == 'resolve_knowledge':
                            result = bridge.manager.resolve_knowledge(identity, payload.get('query_id'))
                        elif payload.get('operation') == 'supplement_knowledge':
                            result = bridge.manager.supplement_knowledge(identity, payload.get('query_id'), payload.get('material_ids'))
                        elif payload.get('operation') == 'backup_archive':
                            result = bridge.manager.backup_archive(identity, payload.get('source_id'), payload.get('backup_id'), payload.get('kind', 'checkpoint'))
                        elif payload.get('operation') == 'restore_archive':
                            result = bridge.manager.restore_archive(identity, payload.get('backup_id'), payload.get('restore_id'))
                        elif payload.get('operation') == 'protect_archive':
                            result = bridge.manager.protect_archive(identity, payload.get('source_id'), payload.get('protection_id'))
                        elif payload.get('operation') == 'register_archive_source':
                            result = bridge.manager.register_archive_source(identity, payload.get('registration'))
                        elif payload.get('operation') == 'query_archive':
                            result = bridge.manager.query_archive(identity, payload.get('source_id'), payload.get('query_id'), payload.get('question'), payload.get('scope_ids'), payload.get('complete', False))
                        else:
                            raise ManagementError('unsupported', 'This management operation is not enabled.')
                        response = {'result': result}
                    except ManagementError as exc:
                        response = {'error': {'code': exc.code, 'message': str(exc)}}
                    except (ValueError, TypeError, KeyError):
                        response = {'error': {'code': 'invalid_change', 'message': 'Malformed management input.'}}
                    try:
                        self.wfile.write(_frame(response))
                    except OSError:
                        pass  # The durable operation remains authoritative after caller disconnect.

            class Server(socketserver.ThreadingUnixStreamServer):
                daemon_threads = False
                block_on_close = True

            self._server = Server(str(self.path), Handler)
            os.chmod(self.path, 0o600)
            actual = self.path.stat()
            self._inode = actual.st_ino
            receipt_path = self.path.with_name('manager-runtime.json')
            if receipt_path.is_symlink():
                raise ManagementError('unavailable', 'Manager runtime receipt is an unknown alias; it was preserved.')
            receipt_path.write_text(json.dumps({'pid': os.getpid(), 'inode': actual.st_ino, 'device': actual.st_dev, 'uid': actual.st_uid}))
            os.chmod(receipt_path, 0o600)
            self._thread = threading.Thread(target=self._server.serve_forever,
                                            kwargs={'poll_interval': 0.05}, name='hermes-pm-directory', daemon=True)
            self._thread.start()
            return self
        except (OSError, ManagementError) as exc:
            self._lease.close()
            self._lease = None
            if isinstance(exc, ManagementError):
                raise
            raise ManagementError('unavailable', 'Another manager owns this state directory or the bridge cannot bind.') from exc

    def close(self):
        if self._server is None:
            return
        self._server.shutdown()
        self._server.server_close()
        self._thread.join(timeout=3)
        if self.path.exists() and self.path.stat().st_ino == self._inode:
            self.path.unlink()
        self._server = None
        self._lease.close()
        self._lease = None

    def __enter__(self):
        return self.start()

    def __exit__(self, *_):
        self.close()


class ManagementClient:
    def __init__(self, state_dir, token):
        self.path = Path(state_dir) / 'manager.sock'
        self.token = token

    def _call(self, operation, **args):
        try:
            with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as connection:
                connection.settimeout(30 if operation in {'start_task', 'refresh_task', 'reconcile_task', 'verify_task_execution', 'record_task_delivery', 'control_task', 'answer_human_request', 'prepare_task', 'refresh_task_source', 'refresh_manual_sessions', 'take_over_session', 'return_session_control', 'query_archive', 'protect_archive', 'backup_archive', 'restore_archive', 'collaborate', 'answer_from_knowledge', 'supplement_project_memory', 'manage_memory', 'global_validation', 'lifecycle', 'query_knowledge', 'resolve_knowledge'} else 3)
                connection.connect(str(self.path))
                connection.sendall(_frame({'token': self.token, 'operation': operation, **args}))
                with connection.makefile('rb') as reader:
                    response = _read_frame(reader)
        except (OSError, ValueError) as exc:
            if operation in {'query_knowledge', 'resolve_knowledge'}:
                raise ManagementError('outcome_unknown', 'The knowledge response was not confirmed; inspect the original durable query ID in the snapshot before deciding further action. Do not submit a new query or replay the source call.') from exc
            if operation == 'lifecycle':
                raise ManagementError('outcome_unknown', 'Lifecycle response was not confirmed; inspect the same durable operation ID and original approved scope before any new decision.') from exc
            if operation in {'query_archive', 'protect_archive', 'backup_archive', 'restore_archive'}:
                raise ManagementError('outcome_unknown', 'The archive operation response was not confirmed; inspect the same durable query/protection/backup/restore ID before retrying. Original entries remain inactive.') from exc
            if operation in {'start_task', 'control_task', 'answer_human_request', 'reconcile_task', 'answer_from_knowledge', 'supplement_project_memory', 'manage_memory', 'global_validation'}:
                raise ManagementError('outcome_unknown', 'Task start response was not confirmed; read the same durable request before retrying. Repository occupancy is retained.') from exc
            raise ManagementError('unavailable', 'The management instance is unavailable; no operation was confirmed.') from exc
        if 'error' in response:
            raise ManagementError(response['error']['code'], response['error']['message'])
        return response['result']

    def manage_notifications(self, action, details):
        return self._call('manage_notifications', action=action, details=details)

    def run_notifications(self):
        return self._call('run_notifications')

    def read_snapshot(self, scope=None):
        return self._call('read_snapshot', scope=scope)

    def read_participant_snapshot(self):
        return self._call('read_participant_snapshot')

    def global_validation(self, action, details):
        return self._call('global_validation', action=action, details=details)

    def lifecycle(self, action, details):
        return self._call('lifecycle', action=action, details=details)

    def collaborate(self, action, details):
        return self._call('collaborate', action=action, details=details)

    def apply_directory_change(self, expected_version, change):
        return self._call('apply_directory_change', expected_version=expected_version, change=change)

    def take_over_session(self, request_id, manual_session_id, grant_id, expected_turn_id):
        return self._call('take_over_session', request_id=request_id, manual_session_id=manual_session_id, grant_id=grant_id, expected_turn_id=expected_turn_id)

    def return_session_control(self, request_id, grant_id):
        return self._call('return_session_control', request_id=request_id, grant_id=grant_id)

    def register_observation_source(self, registration):
        return self._call('register_observation_source', registration=registration)

    def refresh_manual_sessions(self, scope=None):
        return self._call('refresh_manual_sessions', scope=scope)

    def refresh_task_source(self, request_id):
        return self._call('refresh_task_source', request_id=request_id)

    def prepare_task(self, request_id, plan):
        return self._call('prepare_task', request_id=request_id, plan=plan)

    def start_task(self, request_id):
        return self._call('start_task', request_id=request_id)

    def control_task(self, request_id, action, instruction_id, text=None, expected_turn_id=None):
        return self._call('control_task', request_id=request_id, action=action, instruction_id=instruction_id,
                          text=text, expected_turn_id=expected_turn_id)

    def answer_human_request(self, request_id, human_request_id, reply_id, response):
        return self._call('answer_human_request', request_id=request_id, human_request_id=human_request_id, reply_id=reply_id, response=response)

    def reconcile_task(self, request_id):
        return self._call('reconcile_task', request_id=request_id)

    def refresh_task(self, request_id):
        return self._call('refresh_task', request_id=request_id)

    def record_task_delivery(self, request_id, report):
        return self._call('record_task_delivery', request_id=request_id, report=report)

    def verify_task_execution(self, request_id):
        return self._call('verify_task_execution', request_id=request_id)

    def register_knowledge_source(self, expected_version, source):
        return self._call('register_knowledge_source', expected_version=expected_version, source=source)

    def query_knowledge(self, source_id, query_id, question, scope_ids, request_id=None, channel_id=None, auto_supplement=False):
        return self._call('query_knowledge', source_id=source_id, query_id=query_id, question=question, scope_ids=scope_ids,
                          request_id=request_id, channel_id=channel_id, auto_supplement=auto_supplement)

    def resolve_knowledge(self, query_id):
        return self._call('resolve_knowledge', query_id=query_id)

    def supplement_knowledge(self, query_id, material_ids=None):
        return self._call('supplement_knowledge', query_id=query_id, material_ids=material_ids)

    def answer_from_knowledge(self, request_id, human_request_id, query_id, material_ids):
        return self._call('answer_from_knowledge', request_id=request_id, human_request_id=human_request_id,
                          query_id=query_id, material_ids=material_ids)

    def curate_project_memory(self, profile_id, entry_id, request_id, selection, supersedes=None):
        return self._call('curate_project_memory', profile_id=profile_id, entry_id=entry_id, request_id=request_id, selection=selection, supersedes=supersedes)

    def read_project_memory(self, profile_id, include_superseded=False):
        return self._call('read_project_memory', profile_id=profile_id, include_superseded=include_superseded)

    def load_project_memory(self, request_id, entry_ids):
        return self._call('load_project_memory', request_id=request_id, entry_ids=entry_ids)

    def record_memory_preference(self, profile_id, entry_id, statement, scope, supersedes=None):
        return self._call('record_memory_preference', profile_id=profile_id, entry_id=entry_id, statement=statement, scope=scope, supersedes=supersedes)

    def register_archive_source(self, registration):
        return self._call('register_archive_source', registration=registration)

    def query_archive(self, source_id, query_id, question, scope_ids, complete=False):
        return self._call('query_archive', source_id=source_id, query_id=query_id, question=question, scope_ids=scope_ids, complete=complete)

    def backup_archive(self, source_id, backup_id, kind='checkpoint'):
        return self._call('backup_archive', source_id=source_id, backup_id=backup_id, kind=kind)

    def restore_archive(self, backup_id, restore_id):
        return self._call('restore_archive', backup_id=backup_id, restore_id=restore_id)

    def protect_archive(self, source_id, protection_id):
        return self._call('protect_archive', source_id=source_id, protection_id=protection_id)

    def supplement_project_memory(self, request_id, entry_ids, expected_turn_id):
        return self._call('supplement_project_memory', request_id=request_id, entry_ids=entry_ids, expected_turn_id=expected_turn_id)

    def manage_memory(self, action, details):
        return self._call('manage_memory', action=action, details=details)
