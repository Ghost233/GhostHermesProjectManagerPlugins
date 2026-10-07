"""Actual named multiplex Profile RPCs; runtime execution proofs remain independent."""
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path

from .manager import ManagementError


class NativeMultiplexLifecycleHost:
    """Trusted host configuration, never caller supplied paths or service-manager verbs."""
    def __init__(self, host_home, profile_homes, verifier):
        self.host_home = self._directory(host_home)
        self.profile_homes = {name: self._directory(home) for name, home in profile_homes.items()}
        self.verifier = verifier

    @staticmethod
    def _directory(value):
        supplied = Path(value)
        if not supplied.is_absolute() or supplied != supplied.resolve() or not supplied.is_dir():
            raise ManagementError('capability_unverified', 'Native lifecycle requires explicit existing canonical host/Profile homes.')
        return supplied

    def _binding(self, profile, component, desired, operation_id):
        from hermes_cli.profiles import get_profile_dir, profile_is_standalone
        from gateway.control_socket import identify_gateway, CONTROL_PROTOCOL_VERSION
        name = profile['native_profile']
        home = self.profile_homes.get(name)
        if name == 'default' or home is None or home == self.host_home or get_profile_dir(name).resolve() != home or profile_is_standalone(home):
            raise ManagementError('capability_unverified', 'Only explicitly bound named satellites of the original multiplexer support this scoped lifecycle; standalone and launch Profiles are blocked.')
        host = identify_gateway(self.host_home)
        if not isinstance(host, dict) or host.get('protocol') != CONTROL_PROTOCOL_VERSION or Path(host.get('hermes_home', '')).resolve() != self.host_home or not isinstance(host.get('pid'), int) or not host.get('start_time') or host.get('profile') == name or not isinstance(host.get('served_profiles'), list) or any(not isinstance(i, str) for i in host['served_profiles']):
            raise ManagementError('capability_unverified', 'The original live multiplexer identity and complete served Profile set are unverified.')
        import hermes_cli.profiles as profiles
        import gateway.control_socket as control
        import gateway.run_profile_reconcile as reconcile
        sdk = {key: hashlib.sha256(Path(module.__file__).read_bytes()).hexdigest() for key, module in (
            ('profiles', profiles), ('control_socket', control), ('profile_reconcile', reconcile))}
        context = {'profile_id': profile['id'], 'native_profile': name, 'project_id': profile['project_id'],
            'component': component, 'operation_id': operation_id, 'desired_state': desired,
            'host': {k: host[k] for k in ('protocol', 'pid', 'start_time', 'hermes_home', 'profile')},
            'profile_home': str(home), 'sdk_sha256': sdk}
        return home, host, context

    def _report(self, context, phase):
        if not callable(self.verifier):
            raise ManagementError('capability_unverified', 'No independent current host proof verifies Profile control and execution coverage.')
        report = self.verifier({**context, 'phase': phase})
        required = ('scoped_control_no_host_stop',) if phase == 'capability' else (
            'profile_runtime', 'bot_routing', 'scheduled_admission', 'related_process_coverage')
        if not isinstance(report, dict) or report.get('binding') != {**context, 'phase': phase} or report.get('scope') != 'profile' or report.get('status') != 'verified' or not isinstance(report.get('cases'), dict) or any(report['cases'].get(case) != 'PASS' for case in required) or not report.get('evidence'):
            raise ManagementError('capability_unverified', 'Independent current scoped Profile/runtime evidence is missing; native unserve alone does not prove chat or cron execution stopped.')
        at = datetime.fromisoformat(report['verified_at'])
        if not at.tzinfo or not 0 <= (datetime.now(timezone.utc) - at).total_seconds() <= 300:
            raise ManagementError('capability_unverified', 'Native lifecycle host evidence is stale.')
        return report

    def capability(self, profile, component, desired, operation_id):
        _, _, context = self._binding(profile, component, desired, operation_id)
        report = self._report(context, 'capability')
        return self._fact(context, report) | {'action': desired}

    def request(self, profile, component, desired, operation_id):
        home, host, context = self._binding(profile, component, desired, operation_id)
        self._report(context, 'capability')
        if component != 'profile_service':
            # The native scoped RPC handles route teardown jointly. It does not cancel
            # arbitrary chat/cron work; inspect requires separate current process proof.
            return {'status': 'accepted' if (profile['native_profile'] in host['served_profiles']) == (desired == 'ready') else 'outcome_unknown'}
        from hermes_cli.profiles import parked_marker_path
        from gateway.control_socket import request_unserve_profile, request_serve_profile_hot
        marker = parked_marker_path(home)
        if marker.is_symlink():
            raise ManagementError('capability_unverified', 'Native parked marker is an unknown alias; it was preserved.')
        if desired == 'stopped':
            marker.touch()
            answer = request_unserve_profile(self.host_home, profile['native_profile'])
            field = 'unserved'
        else:
            marker.unlink(missing_ok=True)
            answer = request_serve_profile_hot(self.host_home, profile['native_profile'])
            field = 'served'
        confirmed = isinstance(answer, dict) and answer.get(field) == profile['native_profile'] and not answer.get('error')
        return {'status': 'accepted' if confirmed else 'outcome_unknown'}

    @staticmethod
    def _fact(context, report):
        return {k: context[k] for k in ('profile_id', 'native_profile', 'project_id', 'component', 'operation_id')} | {
            'scope': 'profile', 'status': 'verified', 'evidence': report['evidence'], 'verified_at': report['verified_at']}

    def inspect(self, profile, component, desired, operation_id):
        home, host, context = self._binding(profile, component, desired, operation_id)
        report = self._report(context, 'state')
        from hermes_cli.profiles import profile_is_parked
        expected_served = desired == 'ready' and component != 'manual_execution'
        if component != 'manual_execution' and ((profile['native_profile'] in host['served_profiles']) != expected_served or profile_is_parked(home) == expected_served):
            raise ManagementError('capability_unverified', 'Native current serving/parked state has not reached the requested Profile lifecycle.')
        if report.get('state') != desired or component == 'manual_execution' and report.get('execution_coverage') != 'complete':
            raise ManagementError('capability_unverified', 'Current component/related process termination is unverified.')
        return self._fact(context, report) | {'state': desired, 'execution_coverage': report.get('execution_coverage', 'unknown')}


def configured_lifecycle_host(config, state_dir):
    if not config:
        return None
    if not isinstance(config, dict) or set(config) != {'host_home', 'profile_homes'} or not isinstance(config['profile_homes'], dict) or any(not isinstance(name, str) or not isinstance(home, str) for name, home in config['profile_homes'].items()):
        raise ManagementError('invalid_change', 'Native lifecycle configuration requires one original multiplex host and explicit named Profile homes.')
    base = Path(state_dir).resolve()
    def verifier(binding):
        try:
            manifest = base / 'native-lifecycle.json'
            if manifest != manifest.resolve() or manifest.stat().st_size > 65536:
                raise ValueError('Invalid lifecycle manifest.')
            key = ':'.join(binding[k] for k in ('operation_id', 'profile_id', 'component', 'phase'))
            reference = json.loads(manifest.read_text())[key]
            report = base / reference['path']
            if report != report.resolve() or not report.is_relative_to(base / 'native-lifecycle-evidence') or report.stat().st_size > 65536:
                raise ValueError('Invalid lifecycle evidence path.')
            raw = report.read_bytes()
            if hashlib.sha256(raw).hexdigest() != reference['sha256']:
                raise ValueError('Lifecycle evidence digest mismatch.')
            return json.loads(raw)
        except (OSError, KeyError, TypeError, ValueError) as exc:
            raise ManagementError('capability_unverified', 'No current hashed native host lifecycle evidence verifies this operation and original scope.') from exc
    return NativeMultiplexLifecycleHost(config['host_home'], config['profile_homes'], verifier)
