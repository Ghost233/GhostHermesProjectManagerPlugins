"""Exercise the external gh CLI boundary with a synthetic executable, never GitHub."""
import json
import os
import sys
import pytest

from ghost_hermes_pm import Manager
from readiness_support import ReadyManager as Manager
from ghost_hermes_pm.feishu import read_github_issue
from ghost_hermes_pm.messages import FeishuEntry
from test_directory import OWNER, make_repo, registration
from test_requests import ISSUE
from test_feishu_entry import CONFIG, Gateway, Transport, event


@pytest.mark.asyncio
@pytest.mark.parametrize('login', ['Ghost233', 'another-account', 'switch-failed'])
async def test_issue_source_switches_and_verifies_effective_account_before_read(tmp_path, monkeypatch, login):
    binary = tmp_path / 'bin'
    binary.mkdir()
    calls = tmp_path / 'gh-calls.jsonl'
    fixture = binary / 'gh'
    fixture.write_text('#!' + sys.executable + '\n' +
        'import json, os, sys\n'
        'args=sys.argv[1:]\n'
        'with open(os.environ["FIXTURE_GH_CALLS"], "a") as log: log.write(json.dumps(args)+"\\n")\n'
        'if args[:2]==["auth","switch"]:\n'
        '    sys.exit(1 if os.environ["FIXTURE_GH_LOGIN"]=="switch-failed" else 0)\n'
        'elif args[0]=="api": print(os.environ["FIXTURE_GH_LOGIN"])\n'
        'elif args[:2]==["issue","view"]: print(os.environ["FIXTURE_GH_ISSUE"])\n'
        'else: sys.exit(90)\n')
    fixture.chmod(0o700)
    monkeypatch.setenv('PATH', str(binary) + os.pathsep + os.environ.get('PATH', ''))
    monkeypatch.setenv('FIXTURE_GH_CALLS', str(calls))
    monkeypatch.setenv('FIXTURE_GH_LOGIN', login)
    monkeypatch.setenv('FIXTURE_GH_ISSUE', json.dumps({**ISSUE, 'updatedAt': ISSUE['updated_at']}))
    monkeypatch.setenv('GH_TOKEN', 'synthetic-unused-token-override')
    adapter, transport = object(), Transport()
    with Manager(tmp_path / 'state', owner_identity_ref=OWNER.subject) as manager:
        manager.apply_directory_change(OWNER, 0, registration(make_repo(tmp_path / 'repo')))
        intake = FeishuEntry(lambda: manager, OWNER.subject, CONFIG, read_github_issue)
        intake.attach_transport(adapter, transport)
        result = await intake.receive(event(), Gateway(adapter))
        snapshot = manager.read_snapshot(OWNER)
    operations = [json.loads(line) for line in calls.read_text().splitlines()]
    assert operations[0] == ['auth', 'switch', '--hostname', 'github.com', '--user', 'Ghost233']
    if login != 'switch-failed':
        assert operations[1] == ['api', '--hostname', 'github.com', 'user', '--jq', '.login']
    if login == 'Ghost233':
        assert operations[2] == ['issue', 'view', ISSUE['url'], '--json', 'url,title,body,updatedAt']
        assert result == {'action': 'skip'}
        assert snapshot['requests'][0]['accepted_scope'] == ISSUE
    else:
        assert len(operations) == (1 if login == 'switch-failed' else 2)
        assert result is None and snapshot['requests'] == [] and transport.sent == []
