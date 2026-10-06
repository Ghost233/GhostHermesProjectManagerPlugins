"""Current Hermes-specific entry adapters; no actor fields become authorization."""
import json
import hmac
from pathlib import Path

from .manager import Manager, ManagementError, VerifiedIdentity
from .transport import ManagementClient, ManagementServer
from .messages import FeishuEntry, OWNED_PLATFORM
from .feishu import NativeFeishuTransport, read_github_issue

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
    registered_profile = ctx.profile_name
    registered_home = None
    runtime = 'configuring'
    resources = None
    intake = FeishuEntry(lambda: resources[0] if resources else None, owner,
                         ctx.get_config('feishu_intake', {}), read_github_issue)
    ctx.register_platform_handler(OWNED_PLATFORM, lambda native, adapter: intake.attach_transport(adapter, NativeFeishuTransport(native)))
    if state_dir and (ctx.get_config('participant_credential_ref') or (manager_profile and owner)):
        runtime = 'manager_unavailable'
    if manager_profile == registered_profile:
        from hermes_constants import get_hermes_home
        registered_home = get_hermes_home().resolve()

    def close():
        nonlocal resources, runtime
        with intake.lifecycle_lock:
            held, resources = resources, None
            intake.deactivate()
            if held is not None:
                manager, server = held
                server.close()
                manager.close()
            runtime = 'manager_unavailable'

    async def start_for_gateway(event=None, gateway=None):
        nonlocal resources, runtime
        with intake.lifecycle_lock:
            if intake.closed or resources is not None or event is None or gateway is None or not callable(getattr(gateway, 'wait_for_shutdown', None)):
                return
            if manager_profile != registered_profile or not state_dir or not owner:
                return
            from hermes_constants import get_hermes_home
            from agent.secret_scope import current_secret_scope, current_secret_scope_home
            if ctx.profile_name != registered_profile or get_hermes_home().resolve() != registered_home:
                return
            secret_home = current_secret_scope_home()
            if current_secret_scope() is not None and (not secret_home or Path(secret_home).resolve() != registered_home):
                return
            token = _credential(ctx.get_config('dashboard_credential_ref'))
            if not token:
                runtime = 'configuring'
                return
            credentials = {token: VerifiedIdentity(owner, 'authenticated-dashboard-bridge')}
            for entry in ctx.get_config('participant_entries', []):
                participant_token = _credential(entry.get('credential_ref'))
                if not participant_token or participant_token in credentials or entry.get('identity_ref') == owner:
                    raise ManagementError('invalid_change', 'Participant credentials must be distinct from the owner bridge.')
                credentials[participant_token] = VerifiedIdentity(entry['identity_ref'], 'configured-native-profile-bridge')
            intake.secret_values = tuple(dict.fromkeys((*intake.secret_values, *credentials)))
            manager = Manager(state_dir, owner_identity_ref=owner, sensitive_values=lambda: intake.secret_values)
            server = ManagementServer(manager, credentials)
            try:
                server.start()
            except Exception:
                manager.close()
                raise
            resources = manager, server
            ctx.on_unload(close)
            runtime = 'directory_available'

            async def gateway_lifetime():
                try:
                    await gateway.wait_for_shutdown()
                finally:
                    close()

            ctx.spawn_task(gateway_lifetime(), name='hermes-pm-gateway-lifetime')

    async def dispatch(event=None, gateway=None):
        await start_for_gateway(event, gateway)

    def owned_factory(config):
        from .owned_feishu import OwnedFeishuAdapter
        adapter = OwnedFeishuAdapter(config, intake, start_for_gateway, registered_home)
        adapter.bind_lifecycle(ctx)
        ctx.on_unload(intake.deactivate)
        return adapter

    def owned_dependencies():
        import importlib.util
        return importlib.util.find_spec('lark_oapi') is not None

    ctx.register_platform(name=OWNED_PLATFORM, label='Hermes project Feishu', adapter_factory=owned_factory,
                          check_fn=owned_dependencies, allowed_users_env='HERMES_PM_FEISHU_ALLOWED_USERS',
                          allow_all_env='', max_message_length=8000)

    ctx.register_hook('pre_gateway_dispatch', dispatch)

    def snapshot(args=None):
        if args:
            return json.dumps({'status': 'rejected', 'code': 'invalid_change', 'message': 'Snapshot accepts no actor or role.'})
        reference = ctx.get_config('participant_credential_ref')
        token = _credential(reference) if reference else None
        owner_reference = ctx.get_config('dashboard_credential_ref')
        owner_token = _credential(owner_reference) if owner_reference else None
        if not state_dir or not token or (owner_token and hmac.compare_digest(token.encode(), owner_token.encode())):
            return json.dumps({'status': 'unverified', 'runtime': runtime, 'execution': 'not_enabled',
                               'needs_human': ['Configure a distinct registered participant credential for directory visibility.']})
        try:
            return json.dumps(ManagementClient(state_dir, token).read_participant_snapshot())
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
