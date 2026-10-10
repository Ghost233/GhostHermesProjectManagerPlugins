"""Authenticated Issue operations, with durable markers for uncertain creation."""
import json
import os
from pathlib import Path
import re
import subprocess

from .manager import ManagementError


class GitHubWorkSource:
    def __init__(self, account, config_dir):
        if not isinstance(account, str) or not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9-]{0,38}', account):
            raise ManagementError('configuration_missing', 'Configure the GitHub account for repository work.')
        self.account = account
        path = Path(config_dir).expanduser()
        if path != path.resolve(strict=True) or not path.is_dir() or path.stat().st_uid != os.getuid() or path.stat().st_mode & 0o077:
            raise ManagementError('unsafe_state', 'GitHub configuration must use its existing private local directory.')
        self.environment = dict(os.environ, GH_CONFIG_DIR=str(path))

    def _run(self, *args, payload=None):
        try:
            result = subprocess.run(['gh', *args], env=self.environment, capture_output=True,
                                    input=json.dumps(payload) if payload is not None else None,
                                    text=True, timeout=30)
        except (OSError, subprocess.TimeoutExpired):
            raise ManagementError('source_unavailable', 'The GitHub operation needs reconciliation.') from None
        if result.returncode:
            raise ManagementError('source_unavailable', 'The GitHub operation needs reconciliation.')
        return result.stdout

    def _business(self, *args, payload=None):
        self._run('auth', 'switch', '--hostname', 'github.com', '--user', self.account)
        if self._run('api', 'user', '--hostname', 'github.com', '--jq', '.login').strip() != self.account:
            raise ManagementError('unauthorized', 'The actual GitHub account differs from the configured account.')
        return self._run(*args, payload=payload)

    def read_issue(self, url):
        value = json.loads(self._business('issue', 'view', url, '--json', 'url,title,body,updatedAt'))
        return {'url': value['url'], 'title': value['title'], 'body': value['body'], 'updated_at': value['updatedAt']}

    def create_issue(self, repository, title, body, marker):
        value = json.loads(self._business('api', 'repos/' + repository + '/issues', '--hostname', 'github.com',
            '--method', 'POST', '--input', '-', payload={'title': title, 'body': body + '\n\n' + marker}))
        return {'url': value['html_url'], 'title': value['title'], 'body': value['body'], 'updated_at': value['updated_at']}

    def find_issue(self, repository, marker):
        pages = json.loads(self._business('api', 'repos/' + repository + '/issues?state=all&per_page=100',
                                         '--hostname', 'github.com', '--paginate', '--slurp'))
        matches = [i for page in pages for i in page if not i.get('pull_request') and marker in (i.get('body') or '')]
        if len(matches) != 1:
            return None
        value = matches[0]
        return {'url': value['html_url'], 'title': value['title'], 'body': value['body'], 'updated_at': value['updated_at']}
