"""Read-only delivery evidence; authenticate Ghost233 before every business read."""
import json
import re
import subprocess
from urllib.parse import quote

from .manager import ManagementError


class GitHubDeliverySource:
    def _run(self, *args):
        result = subprocess.run(['gh', *args], capture_output=True, text=True, timeout=30)
        if result.returncode:
            raise ManagementError('evidence_missing', 'GitHub delivery evidence could not be read.')
        return result.stdout

    def _business(self, *args):
        try:
            self._run('auth', 'switch', '--hostname', 'github.com', '--user', 'Ghost233')
            if self._run('api', '--hostname', 'github.com', 'user', '--jq', '.login').strip() != 'Ghost233':
                raise ManagementError('unauthorized', 'The actual GitHub account must be Ghost233.')
            return self._run(*args)
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise ManagementError('evidence_missing', 'GitHub evidence is unavailable; delivery remains pending.') from exc

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
