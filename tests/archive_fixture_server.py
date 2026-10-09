"""Synthetic DSH durable-journal source at the read-only archive boundary.

This fixture never speaks JSONL or claims real Desktop/interface acceptance.
The native HTTP/Remote-mux wire is independently tested by dsh_fixture_server.
"""
from copy import deepcopy

from ghost_hermes_pm.manager import ManagementError
from ghost_hermes_pm.observation import ReadOnlyDshAdapter, READ_METHODS


def archive_journal():
    """Literal DSH native compaction events retain the shadowed original row."""
    return [
        {'type': 'turn/start', 'seq': 0, 'time': 1, 'data': {'turn': 1}},
        {'type': 'user/message', 'seq': 1, 'time': 2, 'data': {
            'turn': 1, 'content': [{'type': 'text', 'text': 'Original retry requirement'}],
            'source': {'kind': 'user', 'rpcId': 'original-input'}}},
        {'type': 'compaction/start', 'seq': 2, 'time': 3,
         'data': {'compactionId': 'fixture-compaction', 'turn': 1}},
        {'type': 'compaction/summary', 'seq': 3, 'time': 4, 'data': {
            'compactionId': 'fixture-compaction',
            'summary': [{'type': 'text', 'text': 'Earlier retry summary'}],
            'shadowedRange': {'start': 1, 'end': 1}, 'shadowedSeqs': [1],
            'shadowedTokenCount': 10, 'provider': 'fixture', 'model': 'fixture'}},
        {'type': 'compaction/end', 'seq': 4, 'time': 5,
         'data': {'compactionId': 'fixture-compaction', 'turn': 1}},
        {'type': 'turn/end', 'seq': 5, 'time': 6,
         'data': {'turn': 1, 'reason': {'kind': 'completed'}}},
    ]


def archive_thread(events=None, *, cursor=5, history_mode='full'):
    return {'id': 'archive-session', 'status': {'type': 'idle'},
            'historyMode': history_mode,
            'nativeEvents': archive_journal() if events is None else deepcopy(events),
            'journalCursor': cursor}


class JournalReadOnlySource(ReadOnlyDshAdapter):
    """External history-source double; not a transport or capability probe."""
    def __init__(self, *snapshots):
        super().__init__('http://127.0.0.1:1', service_ref='local:synthetic-archive',
                         source_kind='desktop', endpoint_ref='local:synthetic-archive-endpoint')
        self.snapshots = list(snapshots) or [archive_thread()]
        self.reads = []

    def proof(self):
        if self._closed:
            raise ManagementError('unavailable', 'Synthetic journal source is closed.')
        return {'engine': 'dsh', 'original_executor_id': 'synthetic-archive-instance',
                'supported_methods': sorted(READ_METHODS),
                'evidence_ref': 'fixture:synthetic-durable-journal', 'generation': self.generation}

    def read_thread(self, thread_id, include_turns=True):
        if self._closed:
            raise ManagementError('unavailable', 'Synthetic journal source is closed.')
        if thread_id != 'archive-session' or include_turns is not True:
            raise ManagementError('forbidden', 'Synthetic source permits only its explicit archive Session.')
        self.reads.append((thread_id, include_turns))
        return deepcopy(self.snapshots[min(len(self.reads) - 1, len(self.snapshots) - 1)])
