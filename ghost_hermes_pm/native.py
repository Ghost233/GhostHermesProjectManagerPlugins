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
    def collaboration_client():
        token = _credential(ctx.get_config('collaboration_credential_ref'))
        participant = _credential(ctx.get_config('participant_credential_ref'))
        if not state_dir or not token or token == participant:
            raise ManagementError('unauthorized', 'A distinct native collaboration ingress credential is required.')
        return ManagementClient(state_dir, token)

    intake = FeishuEntry(lambda: resources[0] if resources else None, owner,
                         ctx.get_config('feishu_intake', {}), read_github_issue,
                         collaboration_identity_ref=ctx.get_config('collaboration_identity_ref'),
                         collaboration_client=collaboration_client if ctx.get_config('collaboration_credential_ref') else None)
    ctx.register_platform_handler(OWNED_PLATFORM, lambda native, adapter: intake.attach_transport(adapter, NativeFeishuTransport(native)))
    if state_dir and (ctx.get_config('participant_credential_ref') or (manager_profile and owner)):
        runtime = 'manager_unavailable'
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
            for entry in ctx.get_config('collaboration_entries', []):
                native_token = _credential(entry.get('credential_ref'))
                if not native_token or native_token in credentials or entry.get('identity_ref') == owner:
                    raise ManagementError('invalid_change', 'Native role ingress credentials must be distinct from Owner and model-facing credentials.')
                credentials[native_token] = VerifiedIdentity(entry['identity_ref'], 'native-collaboration-ingress')
            intake.secret_values = tuple(dict.fromkeys((*intake.secret_values, *credentials)))
            from .codex import configured_adapter
            from .github import GitHubDeliverySource
            from .observation import configured_observation_adapters
            from .knowledge import configured_providers
            from .archives import configured_providers as configured_archives
            observation_adapters = configured_observation_adapters(ctx.get_config('codex_observation', []), state_dir)
            from .takeover import configured_control_adapters
            control_adapters = configured_control_adapters(ctx.get_config('codex_manual_control', []), state_dir)
            codex_adapter = configured_adapter(ctx.get_config('codex_stdio', {}), state_dir)
            manager = Manager(state_dir, owner_identity_ref=owner, sensitive_values=lambda: intake.secret_values,
                              codex_adapter=codex_adapter, delivery_source=GitHubDeliverySource(state_dir), observation_adapters=observation_adapters, control_adapters=control_adapters,
                              knowledge_providers=configured_providers(ctx.get_config('knowledge_providers', {})),
                              archive_providers=configured_archives(ctx.get_config('archive_providers', {})))
            for registration in ctx.get_config('manual_sources', []):
                manager.register_observation_source(VerifiedIdentity(owner, 'trusted-native-source-registration'), registration)
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

            if codex_adapter is not None or observation_adapters or manager.knowledge_providers or control_adapters or manager.archive_providers or intake.collaboration_entry:
                async def supervise_single_issue():
                    import asyncio
                    identity = VerifiedIdentity(owner, 'verified-manager-supervision')
                    generation = intake.generation
                    last_archive_day = None
                    while resources is not None and not intake.closed:
                        def poll_if_active():
                            with intake.lifecycle_lock:
                                intake.require_active(generation)
                                manager.refresh_manual_sessions(identity)
                                manager.dispatch_tasks()
                        await asyncio.to_thread(poll_if_active)
                        tasks = manager.read_snapshot(identity)['requests']
                        for record in tasks:
                            session = record.get('session')
                            from .takeover import executor_for
                            actual_executor = executor_for(manager, record)
                            if actual_executor is not None and session and session.get('thread_id') and session['generation'] == actual_executor.generation and not record.get('repository_released'):
                                def observe_if_active(request_id=record['id']):
                                    with intake.lifecycle_lock:
                                        intake.require_active(generation)
                                        return manager.refresh_task(identity, request_id)
                                await asyncio.to_thread(observe_if_active)
                            anchor = record['source_anchor']
                            bindings = [b for b in intake.settings.get('bindings', []) if b.get('profile_id') == record['profile_id']
                                and all(b.get(k) == anchor.get(k) for k in ('app_id', 'chat_id', 'recipient_open_id', 'recipient_tenant_key', 'transport_tenant_key'))]
                            if len(bindings) != 1:
                                continue
                            for _, transport in tuple(intake.transports):
                                intake.require_active(generation)
                                recipient = await transport.verify_identity(bindings[0])
                                if recipient and recipient.get('app_id') == anchor['app_id'] and recipient.get('open_id') == anchor['recipient_open_id']:
                                    async with intake.lock:
                                        await intake.deliver(identity, record['id'], transport, generation)
                                    break
                        if intake.collaboration_entry:
                            for handoff in manager.read_snapshot(identity)['collaboration']['handoffs']:
                                if handoff['delivery'] == 'pending':
                                    try:
                                        async with intake.lock:
                                            await intake.collaboration_entry.deliver(handoff['id'], generation)
                                    except ManagementError:
                                        continue
                        knowledge = manager.read_snapshot(identity)['knowledge_queries']
                        for query in knowledge:
                            if query.get('auto_supplement') and query.get('result_received') and not query.get('supplement'):
                                try:
                                    manager.supplement_knowledge(VerifiedIdentity(query['requester'], 'registered-knowledge-request-delegation'), query['id'])
                                except ManagementError:
                                    pass
                            for _, transport in tuple(intake.transports):
                                try:
                                    await intake.deliver_knowledge(VerifiedIdentity(query['requester'], 'registered-knowledge-publication'), query['id'], transport, generation)
                                except ManagementError:
                                    continue
                        from .archives import _now
                        archive_day = _now()[:10]
                        if archive_day != last_archive_day:
                            def snapshot_archives_if_active():
                                with intake.lifecycle_lock:
                                    intake.require_active(generation)
                                    return manager.run_archive_daily()
                            await asyncio.to_thread(snapshot_archives_if_active)
                            last_archive_day = archive_day
                        from .archive_delivery import deliver as deliver_archive
                        for query in manager.read_snapshot(identity)['archive_queries']:
                            for _, transport in tuple(intake.transports):
                                try:
                                    await deliver_archive(intake, VerifiedIdentity(query['requester'], 'registered-archive-publication'), query['id'], transport, generation)
                                except ManagementError:
                                    continue
                        await asyncio.sleep(5)
                ctx.spawn_task(supervise_single_issue(), name='hermes-pm-single-issue-supervision')

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
    def observe(args):
        try:
            if not isinstance(args, dict) or set(args) - {'scope'}:
                raise ManagementError('invalid_change', 'Observation accepts only the registered project scope.')
            reference = ctx.get_config('participant_credential_ref')
            token = _credential(reference) if reference else None
            if not state_dir or not token:
                raise ManagementError('unauthorized', 'A configured participant bridge is required.')
            client = ManagementClient(state_dir, token)
            client.read_participant_snapshot()
            return json.dumps(client.refresh_manual_sessions(args.get('scope')))
        except ManagementError as exc:
            return json.dumps({'status': 'rejected', 'code': exc.code, 'message': str(exc)})

    ctx.register_tool(name='hermes_pm_observe', toolset='hermes_pm',
        schema={'name': 'hermes_pm_observe', 'description': 'Read registered original Codex sources without session control.',
            'parameters': {'type': 'object', 'properties': {'scope': {'type': 'string'}}, 'additionalProperties': False}},
        handler=observe, description='Observe registered original executors only')

    def task_operation(args):
        try:
            if not isinstance(args, dict) or set(args) - {'action', 'request_id', 'report', 'instruction_id', 'text', 'expected_turn_id', 'human_request_id', 'reply_id', 'response', 'plan', 'manual_session_id', 'grant_id'}:
                raise ManagementError('invalid_change', 'Task input cannot assert actor, permission or capability.')
            reference = ctx.get_config('participant_credential_ref')
            token = _credential(reference) if reference else None
            if not state_dir or not token:
                raise ManagementError('unauthorized', 'A configured participant bridge is required.')
            client = ManagementClient(state_dir, token)
            client.read_participant_snapshot()  # Reject owner aliases at the authoritative bridge.
            action = args.get('action')
            if action not in {'takeover', 'return'} and any(args.get(k) is not None for k in ('manual_session_id', 'grant_id')):
                raise ManagementError('invalid_change', 'Manual grant fields require takeover or return.')
            if action != 'answer' and any(args.get(k) is not None for k in ('human_request_id', 'reply_id', 'response')):
                raise ManagementError('invalid_change', 'Human response fields require answer action.')
            if action != 'prepare' and args.get('plan') is not None:
                raise ManagementError('invalid_change', 'Baseline plan requires preparation.')
            if action in {'takeover', 'return'}:
                if any(args.get(k) is not None for k in ('report', 'plan', 'instruction_id', 'text', 'human_request_id', 'reply_id', 'response')):
                    raise ManagementError('invalid_change', 'Current-work grant fields cannot carry another operation.')
                if action == 'takeover':
                    result = client.take_over_session(args.get('request_id'), args.get('manual_session_id'), args.get('grant_id'), args.get('expected_turn_id'))
                else:
                    if args.get('manual_session_id') is not None or args.get('expected_turn_id') is not None:
                        raise ManagementError('invalid_change', 'Return accepts the existing grant only.')
                    result = client.return_session_control(args.get('request_id'), args.get('grant_id'))
            elif action == 'answer':
                if any(args.get(k) is not None for k in ('report', 'instruction_id', 'text', 'expected_turn_id')):
                    raise ManagementError('invalid_change', 'Human response fields cannot carry other operations.')
                result = client.answer_human_request(args.get('request_id'), args.get('human_request_id'), args.get('reply_id'), args.get('response'))
            elif action == 'prepare':
                if args.get('report') is not None or any(args.get(k) is not None for k in ('instruction_id', 'text', 'expected_turn_id')):
                    raise ManagementError('invalid_change', 'Preparation accepts only the explicit baseline plan.')
                result = client.prepare_task(args.get('request_id'), args.get('plan'))
            elif action in {'append', 'stop', 'continue'}:
                if args.get('report') is not None:
                    raise ManagementError('invalid_change', 'Control cannot assert delivery evidence.')
                result = client.control_task(args.get('request_id'), action, args.get('instruction_id'), args.get('text'), args.get('expected_turn_id'))
            elif any(args.get(k) is not None for k in ('instruction_id', 'text', 'expected_turn_id')):
                raise ManagementError('invalid_change', 'Control fields require a control action.')
            elif action == 'delivery':
                result = client.record_task_delivery(args.get('request_id'), args.get('report'))
            else:
                operation = {'verify': 'verify_task_execution', 'start': 'start_task', 'refresh': 'refresh_task', 'source': 'refresh_task_source'}.get(action)
                if operation is None or args.get('report') is not None:
                    raise ManagementError('invalid_change', 'Unsupported task operation.')
                result = getattr(client, operation)(args.get('request_id'))
            return json.dumps(result)
        except ManagementError as exc:
            return json.dumps({'status': 'rejected', 'code': exc.code, 'message': str(exc)})

    ctx.register_tool(name='hermes_pm_task', toolset='hermes_pm',
                      schema={'name': 'hermes_pm_task', 'description': 'Verify, start, observe or record evidence for one accepted Issue.',
                              'parameters': {'type': 'object', 'properties': {'action': {'type': 'string', 'enum': ['verify', 'start', 'refresh', 'delivery', 'append', 'stop', 'continue', 'answer', 'prepare', 'source', 'takeover', 'return']},
                                  'request_id': {'type': 'string'}, 'report': {'type': 'object'}, 'plan': {'type': 'object'},
                                  'instruction_id': {'type': 'string'}, 'text': {'type': 'string'}, 'expected_turn_id': {'type': 'string'}, 'human_request_id': {'type': 'string'}, 'reply_id': {'type': 'string'}, 'response': {'type': 'object'}, 'manual_session_id': {'type': 'string'}, 'grant_id': {'type': 'string'}},
                                  'required': ['action', 'request_id'], 'additionalProperties': False}},
                      handler=task_operation, description='Single Issue execution and evidence')
    def role_operation(args):
        try:
            allowed = {'read_routes', 'delegate_issue', 'report_result', 'report_progress', 'report_summary', 'publish_owner_summary'}
            if not isinstance(args, dict) or set(args) != {'action', 'details'} or args['action'] not in allowed:
                raise ManagementError('invalid_change', 'Participant collaboration accepts only scoped role work and summaries; the original native ingress supplies reception.')
            token = _credential(ctx.get_config('participant_credential_ref'))
            if not state_dir or not token:
                raise ManagementError('unauthorized', 'A distinct registered participant bridge is required.')
            client = ManagementClient(state_dir, token)
            client.read_participant_snapshot()
            return json.dumps(client.collaborate(args['action'], args['details']))
        except ManagementError as exc:
            return json.dumps({'status': 'rejected', 'code': exc.code, 'message': str(exc)})

    ctx.register_tool(name='hermes_pm_collaborate', toolset='hermes_pm',
        schema={'name': 'hermes_pm_collaborate', 'description': 'Read registered roles, delegate an explicit own child Issue, and report original work or project summaries.',
            'parameters': {'type': 'object', 'properties': {'action': {'type': 'string', 'enum': ['read_routes', 'delegate_issue', 'report_result', 'report_progress', 'report_summary', 'publish_owner_summary']}, 'details': {'type': 'object'}},
                'required': ['action', 'details'], 'additionalProperties': False}}, handler=role_operation,
        description='Public scoped role collaboration')
    def validation_operation(args):
        try:
            allowed = {'plan', 'start', 'finish', 'reconcile', 'check', 'rework', 'complete'}
            if not isinstance(args, dict) or set(args) != {'action', 'details'} or args['action'] not in allowed:
                raise ManagementError('invalid_change', 'The participant may validate its mono task; independent child materialization requires the original Owner entry.')
            token = _credential(ctx.get_config('participant_credential_ref'))
            if not state_dir or not token:
                raise ManagementError('unauthorized', 'A distinct registered participant bridge is required.')
            client = ManagementClient(state_dir, token)
            client.read_participant_snapshot()
            return json.dumps(client.global_validation(args['action'], args['details']))
        except ManagementError as exc:
            return json.dumps({'status': 'rejected', 'code': exc.code, 'message': str(exc)})

    ctx.register_tool(name='hermes_pm_global_validation', toolset='hermes_pm',
        schema={'name': 'hermes_pm_global_validation', 'description': 'Freeze related child deliveries, validate an actual stable mono combination, and return concrete repair Issues.',
            'parameters': {'type': 'object', 'properties': {'action': {'type': 'string', 'enum': ['plan', 'start', 'finish', 'reconcile', 'check', 'rework', 'complete']}, 'details': {'type': 'object'}},
                'required': ['action', 'details'], 'additionalProperties': False}}, handler=validation_operation,
        description='Stable mono global validation and Issue rework')
    def knowledge_operation(args):
        try:
            if not isinstance(args, dict) or set(args) - {'action', 'source_id', 'query_id', 'question', 'scope_ids', 'request_id', 'channel_id', 'auto_supplement', 'material_ids'}:
                raise ManagementError('invalid_change', 'Knowledge input cannot assert requester, role or source authority.')
            token = _credential(ctx.get_config('participant_credential_ref'))
            if not state_dir or not token:
                raise ManagementError('unauthorized', 'A distinct registered participant bridge is required.')
            client = ManagementClient(state_dir, token)
            client.read_participant_snapshot()
            if args.get('action') == 'query':
                if not args.get('request_id') or not args.get('channel_id'):
                    raise ManagementError('forbidden', 'A participant tool query requires an original task and explicit approved public sharing channel.')
                result = client.query_knowledge(args.get('source_id'), args.get('query_id'), args.get('question'), args.get('scope_ids'),
                    args.get('request_id'), args.get('channel_id'), args.get('auto_supplement', False))
            elif args.get('action') == 'supplement':
                result = client.supplement_knowledge(args.get('query_id'), args.get('material_ids'))
            else:
                raise ManagementError('invalid_change', 'A participant may query or submit allowed task facts, not grant source access.')
            return json.dumps(result)
        except ManagementError as exc:
            return json.dumps({'status': 'rejected', 'code': exc.code, 'message': str(exc)})

    ctx.register_tool(name='hermes_pm_knowledge', toolset='hermes_pm',
        schema={'name': 'hermes_pm_knowledge', 'description': 'Request explicitly shareable source material for an original task, or submit verified facts.',
            'parameters': {'type': 'object', 'properties': {'action': {'type': 'string', 'enum': ['query', 'supplement']},
                'source_id': {'type': 'string'}, 'query_id': {'type': 'string'}, 'question': {'type': 'string'},
                'scope_ids': {'type': 'array', 'items': {'type': 'string'}}, 'request_id': {'type': 'string'},
                'channel_id': {'type': 'string'}, 'auto_supplement': {'type': 'boolean'}, 'material_ids': {'type': 'array', 'items': {'type': 'string'}}},
                'required': ['action', 'query_id'], 'additionalProperties': False}}, handler=knowledge_operation,
        description='Scoped original-requester Wiki query and task facts')
    def archive_operation(args):
        try:
            if not isinstance(args, dict) or set(args) - {'source_id', 'query_id', 'question', 'scope_ids', 'complete'}:
                raise ManagementError('invalid_change', 'Archive tools cannot assert caller, migration grants, paths, protection or restore authority.')
            token = _credential(ctx.get_config('participant_credential_ref'))
            if not state_dir or not token:
                raise ManagementError('unauthorized', 'A distinct registered participant bridge is required.')
            client = ManagementClient(state_dir, token)
            client.read_participant_snapshot()
            return json.dumps(client.query_archive(args.get('source_id'), args.get('query_id'), args.get('question'), args.get('scope_ids'), args.get('complete', False)))
        except ManagementError as exc:
            return json.dumps({'status': 'rejected', 'code': exc.code, 'message': str(exc)})

    ctx.register_tool(name='hermes_pm_archive', toolset='hermes_pm',
        schema={'name': 'hermes_pm_archive', 'description': 'Read only this Profile’s explicitly authorized migration archive; never start old entries.',
            'parameters': {'type': 'object', 'properties': {'source_id': {'type': 'string'}, 'query_id': {'type': 'string'},
                'question': {'type': 'string'}, 'scope_ids': {'type': 'array', 'items': {'type': 'string'}}, 'complete': {'type': 'boolean'}},
                'required': ['source_id', 'query_id', 'question', 'scope_ids'], 'additionalProperties': False}},
        handler=archive_operation, description='Read explicitly registered migration archive data')
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
