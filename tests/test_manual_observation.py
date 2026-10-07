"""Observe registered original-service peers through the public credential bridge."""
import json
import sys
from pathlib import Path

import pytest

from ghost_hermes_pm import Manager, ManagementError
from ghost_hermes_pm.transport import ManagementClient, ManagementServer
from test_directory import OWNER, make_repo
from test_task_execution import accepted, adapter_for

READ_ONLY = {'initialize', 'initialized', 'thread/list', 'thread/loaded/list', 'thread/read', 'thread/backgroundTerminals/list'}


def observer(root, kind='daemon', executor='synthetic-original', coverage='complete'):
    from ghost_hermes_pm.observation import ReadOnlyCodexAdapter
    root.mkdir(exist_ok=True)
    def verifier(binding):
        return {**binding, 'original_executor_id': executor, 'provenance': 'synthetic-original-service-peer',
                'supported_methods': list(READ_ONLY), 'source_kinds': ['cli', 'appServer', 'exec', 'vscode', 'subAgent'],
                'runtime_coverage': coverage, 'evidence_ref': 'synthetic-host-binding'}
    return ReadOnlyCodexAdapter([sys.executable, str(Path(__file__).with_name('manual_fixture_server.py')), str(root), 'app-server', 'proxy', '--sock', str(root / 'registered-synthetic.sock')],
        cwd=root, env={'PATH': '/usr/bin:/bin', 'CODEX_HOME': str(root / 'synthetic-home')},
        service_ref='local:manual-' + kind, source_kind=kind, endpoint_ref='local:registered-' + kind, verifier=verifier, timeout=1)


def source(kind='daemon'):
    return {'id': 'manual-' + kind, 'kind': kind, 'project_ids': ['mono'], 'adapter_ref': 'local:manual-' + kind}


def manual_state(root, repo, status='active', thread_id='manual-thread', complete=True):
    root.mkdir(exist_ok=True)
    turn_status = 'inProgress' if status == 'active' else 'completed'
    (root / 'manual-state.json').write_text(json.dumps({'threads': {thread_id: {'id': thread_id, 'cwd': str(repo), 'source': 'cli',
        'status': {'type': status, 'activeFlags': []}, 'turns': [{'id': 'manual-turn', 'status': turn_status,
            'itemsView': 'full' if complete else 'partial', 'items': []}]}}, 'loaded': [thread_id], 'listed': [thread_id]}))


def test_public_original_service_discovery_is_read_only_and_active_blocks_repository(tmp_path):
    repo = make_repo(tmp_path / 'repo')
    peer = tmp_path / 'manual-peer'
    manual_state(peer, repo)
    with Manager(tmp_path / 'state', owner_identity_ref=OWNER.subject, codex_adapter=adapter_for(tmp_path), observation_adapters={'local:manual-daemon': observer(peer)}) as manager:
        request_id = accepted(manager, repo)
        with ManagementServer(manager, {'observer-owner': OWNER}):
            client = ManagementClient(tmp_path / 'state', 'observer-owner')
            client.register_observation_source(source())
            result = client.refresh_manual_sessions('mono')
            assert result['manual_sources'][0]['scope']['executor'] == 'synthetic-original'
            session = result['manual_sessions'][0]
            assert session['state'] == 'active'
            assert session['control'] == 'observe_only'
            assert session['logical_repository'] == str(repo / '.git')
            assert session['last_verified_at']
            with pytest.raises(ManagementError) as busy:
                client.start_task(request_id)
            assert busy.value.code == 'repository_busy'
            queued = client.read_snapshot()['requests'][0]
            assert queued['queue']['manual_blockers'] == [session['id']]
            assert any('手动' in s['text'] for p in queued['outbox'] for s in p['segments'])
        wire = [json.loads(line) for line in (peer / 'manual-wire.jsonl').read_text().splitlines()]
        assert all(message.get('method') in READ_ONLY for message in wire)
        lists = [r['params'] for r in wire if r['method'] == 'thread/list']
        assert lists and all(p['sourceKinds'] and p['useStateDbOnly'] is True for p in lists)
        assert {p['archived'] for p in lists} == {False, True}
