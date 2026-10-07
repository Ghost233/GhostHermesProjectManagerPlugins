"""Fixed-host native Profile provisioning, isolated from inherited credentials."""
import importlib.util
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


def configured_migration_host(config, state_dir):
    if not config:
        return None
    if not isinstance(config, dict) or set(config) - {'host_home', 'memory_write_approval'} or 'host_home' not in config or type(config.get('memory_write_approval', False)) is not bool:
        raise ManagementError('invalid_change', 'Native migration configuration names one host and its native memory approval policy.')
    return NativeMigrationHost(config['host_home'], Path(state_dir).resolve() / 'migration-native',
                               memory_write_approval=config.get('memory_write_approval', False))
