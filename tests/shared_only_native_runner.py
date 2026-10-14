"""Exercise retired work through the unchanged Hermes tool and Kanban APIs."""
import json
import os
from pathlib import Path
import sqlite3
import sys


scratch = Path(sys.argv[1]).resolve()
mode = sys.argv[2]
home = scratch / 'hermes-fixture'
home.mkdir(mode=0o700)
os.environ.update(HOME=str(scratch), HERMES_HOME=str(home), HERMES_SKIP_PM_BOOTSTRAP='1',
                  HERMES_DISABLE_PROJECT_PLUGINS='1')
(home / 'config.yaml').write_text('kanban:\n  auto_decompose: false\n')

import hermes_bootstrap  # noqa: E402,F401
from hermes_cli.plugins import PluginContext, PluginManager, PluginManifest  # noqa: E402
from tools import kanban_tools  # noqa: E402,F401
from tools.registry import registry  # noqa: E402
from ghost_hermes_pm.simple_development import RepositoryIntake  # noqa: E402
from ghost_hermes_pm.repository_supervision import register_repository_supervision  # noqa: E402

manager = PluginManager(scope_key=str(home))
context = PluginContext(PluginManifest(name='fixture-shared-only'), manager)
workspace = scratch / 'repository'
workspace.mkdir()
retained = scratch / 'retired-home'
retained.mkdir(mode=0o700)
history = retained / 'history.json'
history.write_text('{"fixture":"retained execution history"}')
github = scratch / 'github-fixture'
github.mkdir(mode=0o700)
intake = RepositoryIntake('fixture:owner', {}, {'board': 'default',
    'github_account': 'fixture-user', 'github_config_dir': str(github)}, scratch / 'state')
created = json.loads(registry.dispatch('kanban_create', {'title': 'Retained fixture work',
    'assignee': context.profile_name, 'initial_status': 'blocked',
    'workspace_kind': 'dir', 'workspace_path': str(workspace)}))
assert 'error' not in created, created
card_id = created['task_id']
execution = {'state': 'budget_stopped', 'generation': 'fixture-old-generation',
    'session_id': 'fixture-old-session', 'instance_dir': str(retained)}
if mode != 'default':
    execution['mode'] = mode
record = {'id': 'fixture-history', 'card_id': card_id,
    'target': {'native_profile': context.profile_name, 'repo_path': str(workspace),
               'execution_ref': str(scratch / 'retired-reference.json')},
    'issue': {'url': 'https://github.com/fixture/fixture/issues/1', 'title': 'Retained fixture work',
              'body': 'Keep old execution evidence unchanged.'}, 'dsh_execution': execution}
# Restore a historical fixture in its existing durable format; public reads
# and the registered tool below supply the behavioral assertions.
with sqlite3.connect(intake.database) as database:
    database.execute('INSERT INTO work(id,issue_url,data) VALUES(?,?,?)',
        (record['id'], record['issue']['url'], json.dumps(record)))
register_repository_supervision(context, intake)
before = intake.snapshot()
database_before, history_before = intake.database.read_bytes(), history.read_bytes()
environment_before = os.environ.get('DSH_HOME')
os.environ['HERMES_KANBAN_TASK'] = card_id
launches, legacy_imports, home_accesses = [], [], []
active = True


def observe(event, args):
    if not active:
        return
    if event == 'subprocess.Popen':
        launches.append(args[0])
    if event == 'import' and args[0] == 'ghost_hermes_pm.dsh_owned_transport':
        legacy_imports.append(args[0])
    if event == 'open' and isinstance(args[0], (str, bytes)):
        if Path(os.fsdecode(args[0])).is_relative_to(retained):
            home_accesses.append(os.fsdecode(args[0]))


sys.addaudithook(observe)
try:
    response = json.loads(registry.dispatch('hermes_pm_supervise', {}))
finally:
    active = False
evidence = {'response': response, 'launches': launches, 'legacy_imports': legacy_imports,
    'old_home_accesses': home_accesses, 'record_unchanged': intake.snapshot() == before,
    'database_bytes_unchanged': intake.database.read_bytes() == database_before,
    'history_bytes_unchanged': history.read_bytes() == history_before,
    'dsh_home_unchanged': os.environ.get('DSH_HOME') == environment_before,
    'card': json.loads(registry.dispatch('kanban_show', {'task_id': card_id}))['task']['status']}
manager.unload()
intake.deactivate()
print(json.dumps(evidence))
