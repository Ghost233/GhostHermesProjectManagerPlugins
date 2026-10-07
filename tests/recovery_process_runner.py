"""Owned synthetic manager used for an abrupt supervisor-process death."""
from pathlib import Path
import json
import sys
import time

from ghost_hermes_pm import Manager
from ghost_hermes_pm.transport import ManagementServer
from test_directory import OWNER

state = Path(sys.argv[1])
with Manager(state, owner_identity_ref=OWNER.subject) as manager:
    with ManagementServer(manager, {'recovery-owner': OWNER}):
        print(json.dumps({'ready': True}), flush=True)
        while True:
            time.sleep(.1)
