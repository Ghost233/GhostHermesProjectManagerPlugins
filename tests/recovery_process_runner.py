"""Owned synthetic manager used for an abrupt supervisor-process death."""
from pathlib import Path
import json
import sys
import time

from ghost_hermes_pm import Manager
from ghost_hermes_pm.transport import ManagementServer
from test_directory import OWNER

state = Path(sys.argv[1])
settings = {}
if len(sys.argv) > 2:
    from recovery_service_support import fixture_adapter
    settings['codex_adapter'] = fixture_adapter(Path(sys.argv[2]), sys.argv[3])
with Manager(state, owner_identity_ref=OWNER.subject, **settings) as manager:
    with ManagementServer(manager, {'recovery-owner': OWNER}):
        request_id = None
        if settings:
            from test_task_execution import accepted
            from test_directory import make_repo
            request_id = accepted(manager, make_repo(state.parent / 'repo'))
            manager.start_task(OWNER, request_id)
        print(json.dumps({'ready': True, 'request_id': request_id}), flush=True)
        while True:
            time.sleep(.1)
