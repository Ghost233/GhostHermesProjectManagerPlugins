"""Pristine native SDK registry drives original-session controls against a JSONL peer."""
from pathlib import Path
import json
import os
import sys
import yaml

scratch = Path(sys.argv[1]).resolve()
protected = [Path.home() / '.hermes', Path.home() / '.codex']

def audit(event, args):
    if event == 'open' and isinstance(args[0], (str, bytes)):
        path = Path(os.fsdecode(args[0])).resolve()
        if any(path.is_relative_to(p) for p in protected) or (path.name == '.env' and not path.is_relative_to(scratch)):
            raise RuntimeError('Native control smoke refused real home or credential access.')
    if event == 'socket.connect' and isinstance(args[1], tuple):
        raise RuntimeError('Native control smoke refused external network access.')

sys.addaudithook(audit)
os.environ['HERMES_SKIP_PM_BOOTSTRAP'] = '1'
os.environ['HERMES_DISABLE_PROJECT_PLUGINS'] = '1'
home = scratch / 'home'
state = scratch / 'state'
settings = {'state_dir': str(state), 'participant_credential_ref': 'native:HERMES_FIXTURE_PARTICIPANT_TOKEN'}
(home / 'config.yaml').write_text(yaml.safe_dump({'plugins': {'enabled': ['ghost-hermes-pm'],
    'entries': {'ghost-hermes-pm': {'settings': settings}}}}))
sys.path.insert(0, str(home / 'plugins' / 'ghost-hermes-pm'))
from ghost_hermes_pm import Manager, VerifiedIdentity
from ghost_hermes_pm.transport import ManagementServer
sys.path.insert(0, str(scratch / 'control-fixtures'))
from test_task_execution import accepted, adapter_for
from test_directory import OWNER, make_repo
from test_task_control import TURN, wire
from test_knowledge import WIKI, local_provider, public_grant, prepared_result
from hermes_cli.plugins import get_plugin_manager
from tools.registry import registry

participant = VerifiedIdentity('fixture:lead', 'fixture-native-participant')
provider = local_provider(scratch)
with Manager(state, owner_identity_ref=OWNER.subject, codex_adapter=adapter_for(scratch),
             knowledge_providers={'local:fixture-wiki': provider}) as authority:
    request_id = accepted(authority, make_repo(scratch / 'repo'))
    authority.start_task(OWNER, request_id)
    authority.apply_directory_change(OWNER, authority.read_snapshot(OWNER)['version'], {'profile': WIKI})
    with ManagementServer(authority, {os.environ['HERMES_FIXTURE_PARTICIPANT_TOKEN']: participant}):
        plugin_manager = get_plugin_manager()
        plugin_manager.discover_and_load()
        authority.register_knowledge_source(OWNER, authority.read_snapshot(OWNER)['version'], public_grant())
        query = json.loads(registry.dispatch('hermes_pm_knowledge', {'action': 'query', 'query_id': 'sdk-query',
            'source_id': 'fixture-wiki', 'question': 'retry delivery', 'scope_ids': ['public'], 'request_id': request_id,
            'channel_id': 'project-chat'}, scope=str(home)))
        assert query['status'] == 'awaiting_wiki' and query['requester'] == participant.subject, query
        private = json.loads(registry.dispatch('hermes_pm_knowledge', {'action': 'query', 'query_id': 'private-tool',
            'source_id': 'fixture-wiki', 'question': 'retry', 'scope_ids': ['public']}, scope=str(home)))
        assert private['status'] == 'rejected' and private['code'] == 'forbidden', private
        authority.register_knowledge_source(OWNER, authority.read_snapshot(OWNER)['version'], public_grant())
        from ghost_hermes_pm.transport import ManagementClient
        # Separate trusted owner entry prepares actual source/receipt fixture, never a tool owner alias.
        query_id = prepared_result(authority, type('OwnerFacade', (), {
            'register_knowledge_source': lambda self, version, source: authority.register_knowledge_source(OWNER, version, source),
            'read_snapshot': lambda self: authority.read_snapshot(OWNER),
            'query_knowledge': lambda self, *args, **kwargs: authority.query_knowledge(participant, *args, **kwargs)})(), scratch, request_id, query_id='sdk-facts')
        supplied = json.loads(registry.dispatch('hermes_pm_knowledge', {'action': 'supplement', 'query_id': query_id}, scope=str(home)))
        assert supplied['status'] == 'accepted', supplied
        assert len([r for r in wire(scratch) if r['method'] == 'turn/steer']) == 1
        assert plugin_manager.unload('ghost-hermes-pm')
        assert registry.get_entry('hermes_pm_knowledge', scope=str(home)) is None
print('native load, Dashboard bridge, restart, teardown: OK')
