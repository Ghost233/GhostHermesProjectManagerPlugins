"""Trusted boundary for the explicitly spawned artificial test service only."""
import json
import os
from pathlib import Path
import sys

from normalized_executor_fixture import SyntheticExecutorAdapter
from ghost_hermes_pm.dsh import repository_fingerprint
from ghost_hermes_pm.manager import ManagementError
from normalized_executor_fixture import SyntheticRecoveryAdapter as OriginalRecoveryAdapter


def fixture_adapter(root, original_service_id, *, recover=False):
    def proof(binding, repository, context=None):
        origin = json.loads((root / 'origin.json').read_text())
        os.kill(origin['pid'], 0)
        if origin['service_id'] != original_service_id or original_service_id != 'local:owned-original:' + str(origin['pid']):
            raise ManagementError('binding_conflict', 'The separately spawned original service changed.')
        result = {**binding, 'service_id': original_service_id, 'repository_fingerprint': repository_fingerprint(repository),
            'permission_profile': 'fixture-boundary', 'runtime_roots': [repository['worktree']], 'policy_digest': 'fixture-policy',
            'platform_enforcement': 'controlled-test-service-only', 'tool_paths': 'controlled-test-service-only',
            'task_start': 'controlled-test-service-only', 'manual_execution_coverage': 'controlled-test-service-only', 'model': 'fixture-model',
            'task_control': {k: 'controlled-test-service-only' for k in ('append', 'stop', 'continue', 'idle_input', 'related_execution', 'human_response')},
            'process_coverage': {'kind': 'no_unregistered_process_paths', 'evidence': 'owned-sleep-child-only'},
            'recovery': 'controlled-test-service-only', 'control_access': 'verified-original-input-path'}
        if context is not None:
            result['recovery_binding'] = context
        return result
    command = [sys.executable, str(Path(__file__).with_name('recovery_service_fixture.py')), 'proxy', str(root)]
    settings = {'cwd': root, 'env': {'PATH': '/usr/bin:/bin', 'FIXTURE_HOME': str(root / 'isolated-home')}, 'service_ref': 'local:fixture-stdio', 'timeout': 1}
    if recover:
        return OriginalRecoveryAdapter(command, **settings, source_kind='desktop', endpoint_ref='local:owned-original', verifier=proof)
    class InitialProxy(SyntheticExecutorAdapter):
        def connect(self):
            super().connect()
            self.connection.update(service_id=original_service_id, endpoint_ref='local:owned-original', transport='synthetic_domain_pipe')
            return dict(self.connection)
    return InitialProxy(command, **settings, verifier=proof)
