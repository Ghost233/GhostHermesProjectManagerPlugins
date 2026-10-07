"""Fixed-source plugin reload and data-only rollback on the original SDK host."""
from datetime import datetime, timezone
import hashlib
import json
import marshal
from pathlib import Path
import sqlite3
import threading
import types

from .manager import ManagementError

PLUGIN = 'ghost-hermes-pm'


def _now():
    return datetime.now(timezone.utc).isoformat()


def _ordinary(path, directory=False):
    path = Path(path)
    if not path.is_absolute() or path != path.resolve() or not (path.is_dir() if directory else path.is_file()) or not directory and path.stat().st_nlink != 1:
        raise ManagementError('capability_unverified', 'Maintenance paths must be fixed canonical ordinary files/directories; unknown aliases were preserved.')
    return path


def inventory(root):
    root = _ordinary(root, True)
    files = {}
    for name in ('__init__.py', 'plugin.yaml', 'ghost_hermes_pm', 'dashboard'):
        path = root / name
        for child in sorted(path.rglob('*')) if path.is_dir() else [path]:
            if '__pycache__' in child.parts or child.suffix == '.pyc' or child.is_dir():
                continue
            file = _ordinary(child)
            files[file.relative_to(root).as_posix()] = hashlib.sha256(file.read_bytes()).hexdigest()
    if '__init__.py' not in files or 'plugin.yaml' not in files:
        raise ManagementError('unknown_version', 'A fixed standalone plugin source is incomplete.')
    return files


def source_digest(root):
    return hashlib.sha256(json.dumps(inventory(root), sort_keys=True).encode()).hexdigest()


def code_digest(code):
    # Source line/filename changes do not change execution; bytecode and nested constants do.
    constants = tuple(code_digest(item) if isinstance(item, types.CodeType) else item for item in code.co_consts)
    return hashlib.sha256(marshal.dumps((code.co_code, constants, code.co_names, code.co_varnames, code.co_freevars, code.co_cellvars,
                                        code.co_argcount, code.co_posonlyargcount, code.co_kwonlyargcount, code.co_nlocals,
                                        code.co_stacksize, code.co_flags, code.co_exceptiontable))).hexdigest()


def loaded_fact(ctx, entry_function):
    manifest = getattr(ctx, 'manifest', None)
    if manifest is None or not getattr(manifest, 'path', None):
        return {'status': 'unverified', 'plugin_version': 'unknown', 'reason': 'No actual native manifest registration.'}
    root = _ordinary(Path(manifest.path), True)
    compiled = compile((root / 'ghost_hermes_pm' / 'native.py').read_text(), str(root / 'ghost_hermes_pm' / 'native.py'), 'exec')
    expected = next(code for code in compiled.co_consts if isinstance(code, types.CodeType) and code.co_name == 'register_native')
    import uuid
    return {'status': 'registered', 'plugin_version': manifest.version, 'source_digest': source_digest(root),
            'entry_code_digest': code_digest(entry_function.__code__), 'entry_source_code_digest': code_digest(expected),
            'load_id': str(uuid.uuid4()), 'registered_at': _now(), 'source': manifest.source}


class NativeMaintenanceHost:
    """One existing plugin installation and explicitly reviewed release/data scope."""
    def __init__(self, host_home, profile_home, installed_path, releases, files, verifier):
        self.host_home = _ordinary(host_home, True)
        self.profile_home = _ordinary(profile_home, True)
        self.installed_path = _ordinary(installed_path, True)
        if self.installed_path != self.profile_home / 'plugins' / PLUGIN:
            raise ManagementError('capability_unverified', 'Reload must target this explicitly bound native plugin installation.')
        self.releases = dict(releases)
        self.files = {entry['id']: {**entry, 'path': str(_ordinary(entry['path']))} for entry in files}
        if len(self.files) != len(files) or set(entry['kind'] for entry in files) != {'config', 'data', 'archive'} or any(entry['kind'] not in {'config', 'data', 'archive'} or entry['format'] not in {'file', 'sqlite'} for entry in files):
            raise ManagementError('capability_unverified', 'Explicit unique configuration/data/archive backup coverage is required.')
        self.verifier = verifier
        self._reloads = {}

    def _binding(self, phase, plan=None):
        import gateway.control_socket as control
        import hermes_cli.plugins as plugins
        import gateway.run_plugin_rewire as rewire
        from gateway.control_socket import identify_gateway, CONTROL_PROTOCOL_VERSION
        host = identify_gateway(self.host_home)
        if not isinstance(host, dict) or host.get('protocol') != CONTROL_PROTOCOL_VERSION or Path(host.get('hermes_home', '')).resolve() != self.host_home or not isinstance(host.get('pid'), int) or not host.get('start_time'):
            raise ManagementError('capability_unverified', 'The original live gateway identity is unavailable; loss of supervision is not termination.')
        sdk_digest = hashlib.sha256(json.dumps({name: hashlib.sha256(Path(module.__file__).read_bytes()).hexdigest() for name, module in (
            ('plugins', plugins), ('control_socket', control), ('plugin_rewire', rewire))}, sort_keys=True).encode()).hexdigest()
        binding = {'phase': phase, 'host': {key: host.get(key) for key in ('protocol', 'pid', 'start_time', 'hermes_home', 'profile', 'code_sha', 'code_version')},
                   'profile_home': str(self.profile_home), 'installed_path': str(self.installed_path), 'sdk_source_digest': sdk_digest,
                   'files': self.files}
        if plan:
            binding.update(operation_id=plan['id'], scope_profile_ids=plan['approved_scope']['expected_profile_ids'],
                           expected_release=plan['expected_release'], target_release=plan.get('target_release'))
        return binding

    def _report(self, binding, cases):
        if not callable(self.verifier):
            raise ManagementError('capability_unverified', 'Independent current native runtime and original execution scope evidence is required.')
        proof = self.verifier(binding)
        if not isinstance(proof, dict) or proof.get('binding') != binding or proof.get('status') != 'verified' or not proof.get('evidence') or not isinstance(proof.get('cases'), dict) or any(proof['cases'].get(case) != 'PASS' for case in cases):
            raise ManagementError('capability_unverified', 'Current native complete-scope verification is missing; ACK, parked state and active_agents=0 are insufficient.')
        try:
            at = datetime.fromisoformat(proof['verified_at'])
            if not at.tzinfo or not -5 <= (datetime.now(timezone.utc) - at).total_seconds() <= 300:
                raise ValueError
        except (KeyError, TypeError, ValueError):
            raise ManagementError('capability_unverified', 'Native maintenance verification is stale.')
        return proof

    @staticmethod
    def _fact(binding, proof):
        host = binding['host']
        return {'status': 'verified', 'phase': binding['phase'], 'operation_id': binding.get('operation_id'),
                'scope_profile_ids': binding.get('scope_profile_ids'), 'service_id': str(host['hermes_home']),
                'generation': str(host['pid']) + ':' + str(host['start_time']), 'evidence': proof['evidence'], 'verified_at': proof['verified_at']}

    def current(self):
        binding = self._binding('current')
        from hermes_constants import get_hermes_home
        if get_hermes_home().resolve() != self.profile_home:
            raise ManagementError('capability_unverified', 'Current plugin registry must be queried in the original native Profile scope.')
        from hermes_cli.plugins import get_plugin_manager
        from tools.registry import registry
        registered = next((plugin for plugin in get_plugin_manager().list_plugins() if plugin['name'] == PLUGIN), None)
        entry = registry.get_entry('hermes_pm_loaded_version', scope=str(self.profile_home))
        actual = entry.handler.__globals__.get('register_native') if entry else None
        compiled = compile((self.installed_path / 'ghost_hermes_pm' / 'native.py').read_text(), str(self.installed_path / 'ghost_hermes_pm' / 'native.py'), 'exec')
        expected = next(code for code in compiled.co_consts if isinstance(code, types.CodeType) and code.co_name == 'register_native')
        if not isinstance(actual, types.FunctionType) or Path(actual.__globals__.get('__file__', '')).resolve() != self.installed_path / 'ghost_hermes_pm' / 'native.py' or code_digest(actual.__code__) != code_digest(expected):
            raise ManagementError('unknown_version', 'Actual SDK registry callback reaches stale or different native entry code; reload acknowledgement is insufficient.')
        loaded = json.loads(registry.dispatch('hermes_pm_loaded_version', {}, scope=str(self.profile_home)))
        if loaded.get('entry_code_digest') != code_digest(actual.__code__):
            raise ManagementError('unknown_version', 'The captured loaded receipt differs from the executing SDK registry object.')
        binding['registry_code_digest'] = code_digest(entry.handler.__code__)
        if not registered or registered.get('enabled') is not True or registered.get('error') or loaded.get('status') != 'registered' or loaded.get('plugin_version') != registered['version'] or loaded.get('entry_code_digest') != loaded.get('entry_source_code_digest'):
            raise ManagementError('unknown_version', 'Actual loaded native registration differs from the compiled entry source; stale bytecode or unloaded plugins are unverified.')
        binding['loaded_plugin'] = loaded
        proof = self._report(binding, ('loaded_registry_source',))
        version = binding['host'].get('code_version')
        if not isinstance(version, str) or version in {'', 'unknown', '0.0.0'} or not isinstance(binding['host'].get('code_sha'), str) or len(binding['host']['code_sha']) != 40:
            raise ManagementError('unknown_version', 'Original SDK runtime version/code identity is unknown; no switch or dispatch is permitted.')
        return self._fact(binding, proof) | {'loaded': True, 'plugin_version': loaded['plugin_version'], 'source_digest': loaded['source_digest'],
                'sdk_version': version, 'sdk_source_digest': binding['sdk_source_digest'], 'load_id': loaded['load_id'],
                'entry_code_digest': loaded['entry_code_digest'], 'registry_source': registered['source']}

    def inspect(self, plan):
        binding = self._binding('handoff', plan)
        proof = self._report(binding, ('all_profile_rounds', 'all_original_process_paths', 'all_inflight_requests', 'current_authorization'))
        return self._fact(binding, proof) | {key: proof.get(key) for key in ('active_turns', 'inflight_requests', 'execution_coverage', 'authorization_digest')}

    def checkpoint(self, plan, directory):
        binding = self._binding('checkpoint', plan)
        proof = self._report(binding, ('all_config_data_archive_scope', 'current_authorization'))
        if source_digest(self.installed_path) != plan['expected_release']['source_digest']:
            raise ManagementError('unknown_version', 'Installed source changed since the loaded version was reviewed.')
        artifacts = {}
        sources = {**self.files, **{'code:' + name: {'path': str(self.installed_path / name), 'kind': 'code', 'format': 'file'} for name in inventory(self.installed_path)}}
        for identifier, source in sources.items():
            path = _ordinary(source['path'])
            target = directory / (hashlib.sha256(identifier.encode()).hexdigest() + '.data')
            if target.exists() or target.is_symlink():
                raise ManagementError('outcome_unknown', 'An earlier native checkpoint artifact was preserved.')
            if source['format'] == 'sqlite':
                with sqlite3.connect(path.as_uri() + '?mode=ro', uri=True) as original, sqlite3.connect(target) as copied:
                    before = original.execute('PRAGMA data_version').fetchone()
                    original.backup(copied)
                    if before != original.execute('PRAGMA data_version').fetchone() or copied.execute('PRAGMA integrity_check').fetchall() != [('ok',)]:
                        raise ManagementError('handoff_blocked', 'Native data changed during the consistency checkpoint.')
            else:
                before = path.read_bytes()
                target.write_bytes(before)
                if before != path.read_bytes():
                    raise ManagementError('handoff_blocked', 'Native configuration/archive/source changed during checkpoint.')
            target.chmod(0o600)
            artifacts[identifier] = {'path': str(target), 'sha256': hashlib.sha256(target.read_bytes()).hexdigest(), 'kind': source['kind']}
        return self._fact(binding, proof) | {'artifacts': artifacts, 'categories': ['config', 'data', 'archive'],
                'authorization_digest': proof.get('authorization_digest'), 'consistency': 'sqlite_backup_and_stable_files'}

    def _reload(self, plan, phase):
        key = (plan['id'], phase)
        if key in self._reloads:
            return {'status': 'outcome_unknown'}
        from gateway.control_socket import reload_gateway_plugins
        # This one owned thread issues only the already persisted intent. It does not
        # replay after reload/restart, and no new worker task is created.
        def request():
            reload_gateway_plugins(self.host_home, profile_home=self.profile_home)
        thread = threading.Thread(target=request, name='hermes-maintenance-' + phase, daemon=True)
        self._reloads[key] = thread
        thread.start()
        return {'status': 'accepted'}

    def switch(self, plan):
        target = self.releases.get(plan['target_release']['id'])
        if not isinstance(target, dict) or set(target) != {'path', 'plugin_version', 'source_digest'} or any(target.get(key) != plan['target_release'][key] for key in ('plugin_version', 'source_digest')):
            raise ManagementError('unknown_version', 'Target must match one trusted fixed release and exact source digest.')
        release = _ordinary(target['path'], True)
        if source_digest(release) != target['source_digest'] or set(inventory(release)) != set(inventory(self.installed_path)):
            raise ManagementError('unknown_version', 'Target bytes or file inventory differ; use an explicit installation arrangement instead of generic cleanup.')
        self._report(self._binding('switch', plan), ('fixed_target_source', 'all_config_data_archive_scope', 'current_authorization'))
        for name in inventory(release):
            destination = _ordinary(self.installed_path / name)
            destination.write_bytes((release / name).read_bytes())
        if source_digest(self.installed_path) != target['source_digest']:
            raise ManagementError('outcome_unknown', 'Installed target source digest is incomplete; checkpoint rollback remains available.')
        return self._reload(plan, 'switch')

    def verify_switch(self, plan):
        binding = self._binding('switch', plan)
        proof = self._report(binding, ('loaded_target_registry', 'config_data_archive_grants'))
        return self._fact(binding, proof) | {'categories': ['config', 'data', 'archive', 'grants'],
                'configuration_verified': proof.get('configuration_verified'), 'authorization_digest': proof.get('authorization_digest')}

    def restore(self, plan, checkpoint):
        binding = self._binding('restore', plan)
        proof = self._report(binding, ('bounded_restore_no_entry_activation', 'current_authorization'))
        if proof.get('authorization_digest') != checkpoint['authorization_digest']:
            raise ManagementError('binding_conflict', 'Current native authorization changed; old configuration cannot overwrite it.')
        for identifier, artifact in checkpoint['artifacts'].items():
            if identifier.startswith('code:'):
                target = _ordinary(self.installed_path / identifier.removeprefix('code:'))
            elif identifier in self.files:
                target = _ordinary(self.files[identifier]['path'])
            else:
                raise ManagementError('binding_conflict', 'Checkpoint references material outside the original fixed restore scope.')
            backup = _ordinary(artifact['path'])
            if hashlib.sha256(backup.read_bytes()).hexdigest() != artifact['sha256']:
                raise ManagementError('capability_unverified', 'Native restore backup digest changed.')
            if identifier in self.files and self.files[identifier]['format'] == 'sqlite':
                for suffix in ('-wal', '-shm'):
                    if Path(str(target) + suffix).exists():
                        raise ManagementError('handoff_blocked', 'An open native database may retain WAL state; close it explicitly before bounded data restore.')
            target.write_bytes(backup.read_bytes())
        return self._reload(plan, 'restore')

    def verify_restore(self, plan, checkpoint):
        binding = self._binding('restore', plan)
        proof = self._report(binding, ('restored_data_query', 'grants_not_widened', 'old_entries_tasks_inactive', 'health_not_restored'))
        digests = {}
        for identifier in checkpoint['artifacts']:
            path = self.installed_path / identifier.removeprefix('code:') if identifier.startswith('code:') else Path(self.files[identifier]['path'])
            digests[identifier] = hashlib.sha256(_ordinary(path).read_bytes()).hexdigest()
        return self._fact(binding, proof) | {'categories': ['config', 'data', 'archive'], 'authorization_digest': proof.get('authorization_digest'),
                'entries_inactive': proof.get('entries_inactive'), 'old_tasks_started': proof.get('old_tasks_started'), 'restored_sha256': digests}


def configured_maintenance_host(config, state_dir):
    if not config:
        return None
    if not isinstance(config, dict) or set(config) != {'host_home', 'profile_home', 'installed_path', 'releases', 'files'} or not isinstance(config['releases'], dict) or not isinstance(config['files'], list) or any(not isinstance(entry, dict) or set(entry) != {'id', 'path', 'kind', 'format'} for entry in config['files']):
        raise ManagementError('invalid_change', 'Maintenance requires a trusted original host, one plugin installation, fixed releases and explicit config/data/archive files.')
    base = Path(state_dir).resolve()
    def verifier(binding):
        try:
            manifest = _ordinary(base / 'native-maintenance.json')
            if manifest.stat().st_size > 65536:
                raise ValueError
            reference = json.loads(manifest.read_text())[binding.get('operation_id', 'runtime') + ':' + binding['phase']]
            path = _ordinary(base / reference['path'])
            if not path.is_relative_to(base / 'native-maintenance-evidence') or path.stat().st_size > 65536:
                raise ValueError
            raw = path.read_bytes()
            if hashlib.sha256(raw).hexdigest() != reference['sha256']:
                raise ValueError
            return json.loads(raw)
        except (OSError, KeyError, TypeError, ValueError) as exc:
            raise ManagementError('capability_unverified', 'Current complete-scope native maintenance evidence is unavailable; static configuration does not enable capability.') from exc
    return NativeMaintenanceHost(**config, verifier=verifier)
