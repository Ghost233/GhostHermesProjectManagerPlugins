"""Current Hermes-specific entry adapters; no actor fields become authorization."""
import json

from .manager import Manager, ManagementError, VerifiedIdentity
from .transport import ManagementClient, ManagementServer

PLUGIN_ID = 'ghost-hermes-pm'


def _credential(reference):
    from agent.secret_scope import get_secret
    if not isinstance(reference, str) or not reference.startswith('native:'):
        return None
    return get_secret(reference.removeprefix('native:'))


def register_native(ctx):
    state_dir = ctx.get_config('state_dir')
    manager_profile = ctx.get_config('manager_profile')
    owner = ctx.get_config('owner_identity_ref')
    runtime = 'configuring'
    if manager_profile == ctx.profile_name and state_dir and owner:
        token = _credential(ctx.get_config('dashboard_credential_ref'))
        if token:
            credentials = {token: VerifiedIdentity(owner, 'authenticated-dashboard-bridge')}
            for entry in ctx.get_config('participant_entries', []):
                participant_token = _credential(entry.get('credential_ref'))
                if not participant_token or participant_token in credentials or entry.get('identity_ref') == owner:
                    raise ManagementError('invalid_change', 'Participant credentials must be distinct from the owner bridge.')
                credentials[participant_token] = VerifiedIdentity(entry['identity_ref'], 'configured-native-profile-bridge')
            manager = Manager(state_dir, owner_identity_ref=owner)
            server = ManagementServer(manager, credentials)
            try:
                server.start()
            except Exception:
                manager.close()
                raise
            def close():
                server.close()
                manager.close()
            ctx.on_unload(close)
            runtime = 'directory_available'

    def snapshot(args=None):
        if args:
            return json.dumps({'status': 'rejected', 'code': 'invalid_change', 'message': 'Snapshot accepts no actor or role.'})
        reference = ctx.get_config('participant_credential_ref')
        token = _credential(reference) if reference else None
        if not token or reference == ctx.get_config('dashboard_credential_ref'):
            return json.dumps({'status': 'unverified', 'runtime': runtime, 'execution': 'not_enabled',
                               'needs_human': ['Configure a distinct registered participant credential for directory visibility.']})
        try:
            return json.dumps(ManagementClient(state_dir, token).read_snapshot())
        except ManagementError as exc:
            return json.dumps({'status': 'unverified', 'code': exc.code, 'runtime': runtime, 'execution': 'not_enabled'})

    ctx.register_tool(name='hermes_pm_snapshot', toolset='hermes_pm',
                      schema={'name': 'hermes_pm_snapshot', 'description': 'Read the verified project directory; does not execute tasks.',
                              'parameters': {'type': 'object', 'properties': {}, 'additionalProperties': False}},
                      handler=snapshot, description='Project directory and verified capability status')
    ctx.register_command('hermes-pm', lambda raw_args: snapshot({} if not raw_args.strip() else {'unsupported': True}),
                         description='Read project directory and runtime status')


class NativeDashboardEntry:
    """Authenticate the host's interactive session before borrowing the owner bridge."""
    def __call__(self, request):
        from fastapi import HTTPException
        from hermes_cli.web_server import _require_token
        from hermes_cli.config import load_config_readonly
        _require_token(request)
        if request.query_params.get('profile') is not None or getattr(request.state, 'token_authenticated', False):
            raise HTTPException(403, 'Only the serving management Profile and interactive owner entry are authorized.')
        settings = load_config_readonly().get('plugins', {}).get('entries', {}).get(PLUGIN_ID, {}).get('settings', {})
        if getattr(request.app.state, 'auth_required', False):
            session = getattr(request.state, 'session', None)
            selectors = settings.get('dashboard_owner_users', [])
            if session is None or not any(s.get('provider') == session.provider and s.get('user_id') == session.user_id
                                           and s.get('org_id', '') == session.org_id for s in selectors):
                raise HTTPException(403, 'This authenticated user is not the configured owner.')
        elif settings.get('allow_local_dashboard_owner') is not True:
            raise HTTPException(403, 'Local Dashboard owner access requires explicit trusted configuration.')
        token = _credential(settings.get('dashboard_credential_ref'))
        if not token or not settings.get('state_dir'):
            raise HTTPException(503, 'The owner bridge is not configured.')
        return ManagementClient(settings['state_dir'], token)
