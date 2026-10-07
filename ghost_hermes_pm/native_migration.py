"""Fixed-host native Profile provisioning, isolated from inherited credentials."""
import importlib.util
import asyncio
import json
import os
from pathlib import Path
import subprocess
import sys

from .manager import ManagementError


class NativeMigrationHost:
    def __init__(self, host_home, work_dir, *, sdk_root=None, memory_write_approval=False, verifier=None):
        self.host_home = Path(host_home)
        self.work_dir = Path(work_dir)
        if not self.host_home.is_absolute() or self.host_home != self.host_home.resolve() or not self.host_home.is_dir() or not self.work_dir.is_absolute() or self.work_dir != self.work_dir.resolve():
            raise ManagementError('capability_unverified', 'Migration requires one explicit canonical native host and isolated work directory.')
        self.work_dir.mkdir(parents=True, exist_ok=True)
        if sdk_root is None:
            spec = importlib.util.find_spec('hermes_cli')
            if spec is None:
                raise ManagementError('capability_unverified', 'The actual native SDK installation is unavailable.')
            sdk_root = Path(spec.origin).parent.parent
        self.sdk_root = Path(sdk_root).resolve()
        self.memory_write_approval = memory_write_approval
        self.verifier = verifier

    def _run(self, action, **payload):
        env = {'PATH': os.environ.get('PATH', '/usr/bin:/bin'), 'HOME': str(self.work_dir / 'isolated-home'),
            'HERMES_HOME': str(self.host_home), 'HERMES_SKIP_PM_BOOTSTRAP': '1', 'HERMES_DISABLE_PROJECT_PLUGINS': '1',
            'PYTHONDONTWRITEBYTECODE': '1', 'PYTHONPATH': os.pathsep.join((str(Path(__file__).resolve().parents[1]), str(self.sdk_root)))}
        try:
            completed = subprocess.run([sys.executable, '-m', 'ghost_hermes_pm.native_migration_worker'],
                input=json.dumps({'action': action, 'host_home': str(self.host_home), 'work_dir': str(self.work_dir), **payload}),
                env=env, cwd=self.work_dir, text=True, capture_output=True, timeout=25)
            answer = json.loads(completed.stdout.strip().splitlines()[-1])
            if completed.returncode or 'error' in answer:
                raise ManagementError(answer.get('code', 'capability_unverified'), answer.get('error', 'Native migration action failed.'))
            return answer
        except (OSError, ValueError, IndexError, subprocess.TimeoutExpired) as exc:
            raise ManagementError('outcome_unknown', 'Native migration receipt is unavailable; reconcile the same plan before retrying.') from exc

    def preview(self, source):
        return self._run('preview', source=source)

    def prepare(self, operation):
        if operation.get('native_state') == 'parked_created' and operation['status'] == 'preparing' and operation.get('material_receipt'):
            return self._run('inspect', operation=operation)
        return self._run('prepare', operation=operation, memory_write_approval=self.memory_write_approval)

    def check(self, operation, session_id=None):
        return self._run('inspect', operation=operation, session_id=session_id)

    def checkpoint(self, operation):
        return self._run('checkpoint', operation=operation)

    def rollback(self, operation):
        return self._run('rollback', operation=operation)

    def activate(self, operation):
        if not callable(self.verifier):
            raise ManagementError('capability_unverified', 'Actual new bot identity, channel permission and credential ownership are unverified.')
        verification = self.verifier(operation)
        if not isinstance(verification, dict) or verification.get('status') != 'verified' or verification.get('plan_digest') != operation['digest'] or verification.get('target_identity_ref') != _target_identity(operation):
            raise ManagementError('capability_unverified', 'Current new bot and channel identity do not match the reviewed migration.')
        return self._run('activate', operation=operation, identity_receipt=verification)


def _target_identity(operation):
    return operation['bindings'][operation['plan']['target_profile_id']]['identity_ref']


def verify_new_bot(intake, operation):
    """Use the actual configured native SDK transport, never a caller's bot assertion."""
    from .feishu import NativeFeishuTransport
    plan = operation['plan']
    target = operation['bindings'][plan['target_profile_id']]
    source = operation['bindings'][plan['source_profile_id']]
    bindings = intake.settings.get('bindings', [])
    old = [b for b in bindings if b['profile_id'] == source['id']]
    new = [b for b in bindings if b['profile_id'] == target['id'] and b['project_id'] == target['project_id']]
    if not old or not new:
        raise ManagementError('capability_unverified', 'Register exact old/new bot app/open identities and target channel permission before switch.')
    expected = []
    for binding in new:
        if any(binding['app_id'] == b['app_id'] or binding['recipient_open_id'] == b['recipient_open_id'] for b in old):
            raise ManagementError('binding_conflict', 'A new Profile requires its independent new bot app and open identity.')
        if target['connection_refs']['bot'] != 'identity:' + binding['app_id'] + ':' + binding['recipient_open_id']:
            raise ManagementError('binding_conflict', 'Directory bot reference must name the reviewed actual app/open identity.')
        matched = None
        for _, transport in intake.transports:
            if isinstance(transport, NativeFeishuTransport) and transport.native.config.app_id == binding['app_id']:
                verified = asyncio.run(transport.verify_identity(binding))
                if verified == {'app_id': binding['app_id'], 'open_id': binding['recipient_open_id']}:
                    matched = verified
                    break
        if matched is None:
            raise ManagementError('capability_unverified', 'The real new native bot transport has not verified its own independent credentials and app/open identity.')
        expected.append({**matched, 'chat_id': binding['chat_id'], 'recipient_tenant_key': binding['recipient_tenant_key'],
                         'transport_tenant_key': binding['transport_tenant_key']})
    return {'status': 'verified', 'plan_digest': operation['digest'], 'target_identity_ref': target['identity_ref'],
            'bots': expected, 'credentials': 'actual_native_bot_info_success', 'channel': 'exact_owner_registered_scope'}


def configured_migration_host(config, state_dir, intake=None):
    if not config:
        return None
    if not isinstance(config, dict) or set(config) - {'host_home', 'memory_write_approval'} or 'host_home' not in config or type(config.get('memory_write_approval', False)) is not bool:
        raise ManagementError('invalid_change', 'Native migration configuration names one host and its native memory approval policy.')
    return NativeMigrationHost(config['host_home'], Path(state_dir).resolve() / 'migration-native',
        memory_write_approval=config.get('memory_write_approval', False),
        verifier=(lambda operation: verify_new_bot(intake, operation)) if intake else None)
