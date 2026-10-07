"""Read-only delivery evidence; authenticate Ghost233 before every business read."""
import json
import re
import subprocess
from pathlib import Path
from urllib.parse import quote

from .manager import ManagementError


class GitHubDeliverySource:
    def __init__(self, state_dir=None, *, error_code="evidence_missing"):
        self.error_code = error_code
        self.state_dir = Path(state_dir).resolve() if state_dir is not None else None

    def read_test_version(self, session, item_id):
        if self.state_dir is None or not isinstance(item_id, str) or not re.fullmatch(r'[A-Za-z0-9_-]{1,128}', item_id):
            raise ManagementError('evidence_missing', 'A registered test runner receipt is required.')
        path = self.state_dir / 'test-evidence' / (session['thread_id'] + '-' + session['turn_id'] + '-' + item_id + '.json')
        try:
            if path != path.resolve() or path.stat().st_size > 65536:
                raise ValueError('Invalid test receipt.')
            receipt = json.loads(path.read_text())
            if not isinstance(receipt, dict) or receipt.get('thread_id') != session['thread_id']:
                raise ValueError('Receipt thread mismatch.')
            return receipt
        except (OSError, ValueError) as exc:
            raise ManagementError('evidence_missing', 'No trusted runner receipt binds this test to the fixed delivery source.') from exc

    def _run(self, *args):
        result = subprocess.run(['gh', *args], capture_output=True, text=True, timeout=30)
        if result.returncode:
            raise ManagementError(self.error_code, 'GitHub delivery evidence could not be read.')
        return result.stdout

    def _business(self, *args):
        try:
            self._run('auth', 'switch', '--hostname', 'github.com', '--user', 'Ghost233')
            if self._run('api', '--hostname', 'github.com', 'user', '--jq', '.login').strip() != 'Ghost233':
                raise ManagementError('unauthorized', 'The actual GitHub account must be Ghost233.')
            return self._run(*args)
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise ManagementError(self.error_code, 'GitHub evidence is unavailable; the operation remains pending.') from exc

    def read_issue(self, url):
        if not re.fullmatch(r'https://github\.com/[\w.-]+/[\w.-]+/issues/[1-9]\d*', url):
            raise ManagementError(self.error_code, 'The original GitHub Issue URL is required.')
        try:
            value = json.loads(self._business('issue', 'view', url, '--json', 'url,title,body,updatedAt'))
            return {'url': value['url'], 'title': value['title'], 'body': value['body'], 'updated_at': value['updatedAt']}
        except (ValueError, KeyError, TypeError) as exc:
            raise ManagementError(self.error_code, 'The actual Issue source could not be verified.') from exc

    def read_pr(self, url):
        try:
            value = json.loads(self._business('pr', 'view', url, '--json', 'url,state,headRefOid,reviewDecision,mergedAt,mergeCommit,baseRefName'))
            state = {'OPEN': 'open', 'MERGED': 'merged', 'CLOSED': 'closed'}.get(value['state'])
            return {'url': value['url'], 'head_commit': value['headRefOid'], 'state': state,
                    'review': 'approved' if value.get('reviewDecision') == 'APPROVED' else 'pending',
                    'merge_commit': (value.get('mergeCommit') or {}).get('oid'), 'base_branch': value['baseRefName'],
                    'merged_at': value.get('mergedAt'), 'source': 'github_Ghost233_read'}
        except (ValueError, KeyError, TypeError) as exc:
            raise ManagementError('evidence_missing', 'The actual PR result is incomplete.') from exc

    def read_branch(self, repository_url, branch):
        if not re.fullmatch(r'https://github\.com/[\w.-]+/[\w.-]+', repository_url):
            raise ManagementError('evidence_missing', 'Only a verified GitHub repository is supported.')
        name = repository_url.removeprefix('https://github.com/')
        return self._business('api', '--hostname', 'github.com', 'repos/' + name + '/git/ref/heads/' + quote(branch, safe=''), '--jq', '.object.sha').strip()
