"""Synthetic human-side SDK actions; these are not plugin migration permissions."""
import json
import sys

from tools.memory_tool import apply_memory_pending, load_on_disk_store
from tools.write_approval import MEMORY, discard_pending, get_pending

record = get_pending(MEMORY, sys.argv[2])
assert record is not None
if sys.argv[1] == 'approve':
    outcome = apply_memory_pending(record['payload'], load_on_disk_store())
    assert outcome['success'], outcome
else:
    outcome = {'success': False, 'denied': True}
assert discard_pending(MEMORY, sys.argv[2])
print(json.dumps(outcome))
