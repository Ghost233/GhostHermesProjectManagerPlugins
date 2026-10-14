"""Artificial external maintenance service with independently inspectable release/files."""
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path


def digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


class MaintenanceHost:
    def __init__(self, root):
        self.root = root
        root.mkdir()
        self.release = {'plugin_version': '0.1.0', 'source_digest': 'a' * 64, 'sdk_version': '0.21.5', 'sdk_source_digest': 'b' * 64}
        self.native_authorization = 'grant-v1'
        self.pending = False
        self.fail_switch = False
        self.files = {}
        for kind in ('config', 'data', 'archive'):
            path = root / (kind + '.json')
            path.write_text(json.dumps({'version': kind + '-before'}))
            self.files[kind] = path
        self.effects = []

    def _fact(self, plan, phase, **extra):
        return {'status': 'verified', 'operation_id': plan['id'], 'phase': phase,
                'scope_profile_ids': plan['approved_scope']['expected_profile_ids'],
                'service_id': 'synthetic-maintenance-peer', 'generation': 'original-generation',
                'verified_at': datetime.now(timezone.utc).isoformat(), 'evidence': 'artificial-native-maintenance-only', **extra}

    def current(self):
        return {'status': 'verified', **self.release, 'service_id': 'synthetic-maintenance-peer', 'generation': 'original-generation',
                'loaded': True, 'evidence': 'artificial-loaded-release', 'verified_at': datetime.now(timezone.utc).isoformat()}

    def inspect(self, plan):
        return self._fact(plan, 'handoff', active_turns=[], inflight_requests=['native-unknown'] if self.pending else [],
                          execution_coverage='complete', authorization_digest=self.native_authorization)

    def checkpoint(self, plan, directory):
        artifacts = {}
        for kind, path in self.files.items():
            target = directory / (kind + '.json')
            target.write_bytes(path.read_bytes())
            artifacts[kind] = {'path': str(target), 'sha256': digest(target)}
        return self._fact(plan, 'checkpoint', artifacts=artifacts, authorization_digest=self.native_authorization,
                          categories=['config', 'data', 'archive'], consistency='verified_stable_source')

    def switch(self, plan):
        self.effects.append('switch')
        self.release.update(plugin_version='0.2.0', source_digest='c' * 64)
        if self.fail_switch:
            for path in self.files.values():
                path.write_text('partial-upgrade')
        return {'status': 'accepted'}

    def restore(self, plan, checkpoint):
        self.effects.append('restore')
        for kind, artifact in checkpoint['artifacts'].items():
            self.files[kind].write_bytes(Path(artifact['path']).read_bytes())
        self.release = dict(plan['expected_release'])
        return self._fact(plan, 'restore', categories=['config', 'data', 'archive'], authorization_digest=self.native_authorization,
                          entries_inactive=True, old_tasks_started=False, restored_sha256={kind: digest(path) for kind, path in self.files.items()})


    def verify_switch(self, plan):
        return self._fact(plan, 'switch', categories=['config', 'data', 'archive', 'grants'],
                          configuration_verified=not self.fail_switch, authorization_digest=self.native_authorization)

    def verify_restore(self, plan, checkpoint):
        return self._fact(plan, 'restore', categories=['config', 'data', 'archive'], authorization_digest=self.native_authorization,
                          entries_inactive=True, old_tasks_started=False,
                          restored_sha256={kind: digest(path) for kind, path in self.files.items()})
