"""Verify one original repository work; native card completion remains the worker's job."""
import asyncio
from datetime import datetime, timezone
import json
from pathlib import Path
import re

from .delivery import acceptance_criteria, delivery_requirements, source_state
from .manager import ManagementError, _git


DELIVERY_MARKER = 'HERMES_REPOSITORY_DELIVERY_JSON'


def repository_source_state(target):
    value = source_state({'worktree': target['repo_path'], 'test_artifact_paths': []})
    entries = iter(_git(target['repo_path'], 'status', '--porcelain=v1', '-z').split('\0'))
    preserved = {}
    for entry in entries:
        if not entry:
            continue
        name = entry[3:]
        preserved[name] = value['file_digests'].get(name)
        if entry[0] in 'RC' or entry[1] in 'RC':
            previous = next(entries)
            preserved[previous] = value['file_digests'].get(previous)
    return {**value, 'preserved_files': preserved}


def _report(events):
    end = next((e for e in reversed(events) if e['type'] == 'turn/end'), None)
    if end is None or end['data'].get('reason', {}).get('kind') != 'completed':
        return None
    messages = [e for e in events if e['type'] == 'assistant/message'
                and e['data'].get('turn') == end['data'].get('turn') and e['seq'] < end['seq']]
    candidates = []
    for event in messages:
        for block in event['data'].get('message', {}).get('content', []):
            if block.get('type') == 'text' and DELIVERY_MARKER in block.get('text', ''):
                raw = block['text'].split(DELIVERY_MARKER, 1)[1].strip()
                try:
                    candidates.append(json.JSONDecoder().raw_decode(raw)[0])
                except ValueError:
                    return None
    return candidates[0] if len(candidates) == 1 and isinstance(candidates[0], dict) else None


def _human_pending(execution):
    return (any(q.get('state') in {'open', 'continued'} and q.get('reply_status') != 'accepted'
                for q in execution.get('questions', []))
            or any(a.get('state') == 'pending' or a.get('operation_state') == 'pending'
                   for a in execution.get('approvals', [])))


async def accept_repository_work(intake, record, carrier, events, projections, generation):
    intake.require_active(generation)
    execution = record['dsh_execution']
    report = _report(events)
    unmet = []
    result = {'status': 'unmet', 'delivered': False, 'card_id': record['card_id'],
              'session_id': execution['session_id'], 'pr_status': 'none', 'review_status': 'not_provided',
              'next_action': 'kanban_block', 'block_kind': 'needs_input'}
    if report is None:
        unmet.append('delivery_report_missing')
    receipts = execution.get('tool_receipts', [])
    if not receipts:
        unmet.append('test_evidence_missing')
    if report is not None and receipts:
        try:
            await _verify(intake, record, carrier, events, projections, report, result, unmet, generation)
        except ManagementError as error:
            unmet.append(error.code)
        except (KeyError, ValueError, TypeError, OSError):
            unmet.append('evidence_malformed')
    result.update(unmet=list(dict.fromkeys(unmet)), verified_at=datetime.now(timezone.utc).isoformat())
    if not unmet:
        result.update(status='accepted', delivered=True, next_action='kanban_complete')
        result.pop('block_kind', None)
    intake.require_active(generation)
    execution['acceptance'] = result
    intake._save_execution(record, fields=['acceptance'])
    return result


async def _verify(intake, record, carrier, events, projections, report, result, unmet, generation):
    execution, issue, target = record['dsh_execution'], record['issue'], record['target']
    if (execution['state'] != 'awaiting_acceptance' or any(e['seq'] != i for i, e in enumerate(events))
        or projections.get('inbox') != {'next-turn': [], 'next-step': []}
        or any(j.get('status') in {'running', 'stopping'} for j in execution.get('jobs', []))
        or _human_pending(execution)):
        unmet.append('execution_unconfirmed')
    allowed = {'issue_updated_at', 'criteria', 'source_commit', 'pr_url', 'sync_branches',
               'fine_issue_urls', 'review_call_ids', 'leftovers', 'test_files'}
    if set(report) - allowed:
        unmet.append('unsupported_delivery_claim')
    expected = acceptance_criteria(issue['body'])
    criteria = report.get('criteria', [])
    if (report.get('issue_updated_at') != issue['updated_at'] or not isinstance(criteria, list)
        or [c.get('text') for c in criteria if isinstance(c, dict)] != expected):
        unmet.append('scope_evidence_missing')
    calls = {e['data'].get('callId'): e['data'] for e in events if e['type'] == 'tool/call'}
    settled = {e['data'].get('message', {}).get('toolCallId'): e['data'].get('message', {})
               for e in events if e['type'] == 'tool/result'}
    receipts = {r.get('call_id'): r for r in execution['tool_receipts']}
    test_ids = []
    for criterion in criteria:
        ids = criterion.get('test_call_ids')
        if set(criterion) - {'text', 'test_call_ids'} or not isinstance(ids, list) or not ids:
            unmet.append('test_evidence_missing')
            continue
        for identifier in ids:
            receipt, call, settlement = receipts.get(identifier), calls.get(identifier), settled.get(identifier)
            arguments = call.get('arguments', {}) if call else {}
            if isinstance(arguments, str):
                arguments = json.loads(arguments)
            if (not receipt or not call or not settlement or receipt.get('generation') != execution['generation']
                or receipt.get('session_id') != execution['session_id'] or receipt.get('name') != call.get('name')
                or receipt.get('name') != 'bash' or receipt.get('command') != arguments.get('command')
                or receipt.get('cwd') != target['repo_path'] or receipt.get('is_error') is not False
                or type(receipt.get('exit_code')) is not int or receipt['exit_code'] != 0
                or receipt.get('timed_out') is not False or receipt.get('aborted') is not False
                or settlement.get('isError') is not False):
                unmet.append('test_evidence_missing')
            else:
                test_ids.append(identifier)
    current = await asyncio.to_thread(repository_source_state, target)
    baseline = execution.get('baseline') or {}
    fixed = report.get('source_commit')
    result.update(source_commit=fixed, source_digest=current['source_digest'], workspace_status=current['workspace_status'])
    if not isinstance(fixed, str) or not re.fullmatch('[a-f0-9]{40}', fixed) or fixed != current['head']:
        unmet.append('source_version_mismatch')
    if not baseline or baseline.get('workspace_status') != current['workspace_status']:
        unmet.append('workspace_handoff_unconfirmed')
    if any(current['file_digests'].get(p) != digest for p, digest in baseline.get('preserved_files', {}).items()):
        unmet.append('preserved_user_content_changed')
    test_files = report.get('test_files')
    if not carrier or not isinstance(test_files, list) or not test_files or not test_ids:
        unmet.append('test_evidence_missing')
    elif not unmet:
        receipt = await asyncio.to_thread(carrier.verify_repository_tests, test_files, carrier.config['test_python'])
        intake.require_active(generation)
        artifacts = Path(carrier.config['dsh_home']) / '.hermes-verification-artifacts'
        roots = receipt.get('artifact_roots') if isinstance(receipt, dict) else None
        artifact_valid = (isinstance(roots, list) and len(roots) == 1 and isinstance(roots[0], str)
                          and Path(roots[0]).parent == artifacts and Path(roots[0]).is_dir()
                          and Path(roots[0]).resolve(strict=True) == Path(roots[0]))
        if (not isinstance(receipt, dict) or receipt.get('session_id') != execution['session_id']
            or receipt.get('generation') != execution['generation'] or receipt.get('exit_code') != 0
            or type(receipt.get('exit_code')) is not int or type(receipt.get('executed_tests')) is not int
            or receipt['executed_tests'] < 1 or receipt.get('source_commit') != fixed
            or receipt.get('before_source_digest') != current['source_digest']
            or receipt.get('after_source_digest') != current['source_digest']
            or receipt.get('before_workspace_status') != current['workspace_status']
            or receipt.get('after_workspace_status') != current['workspace_status']
            or receipt.get('source_access') != 'read-only' or receipt.get('git_access') != 'read-only'
            or not artifact_valid
            or receipt.get('actual_exit_code') != 0 or type(receipt.get('actual_exit_code')) is not int
            or receipt.get('runner') != 'pytest'):
            unmet.append('test_runner_unconfirmed')
        else:
            result['test_evidence'] = {'original_call_ids': list(dict.fromkeys(test_ids)), 'runner_receipt': receipt}
    actual_issue = await asyncio.to_thread(intake.github.read_issue, issue['url'])
    intake.require_active(generation)
    if any(actual_issue.get(k) != issue[k] for k in ('url', 'title', 'body')):
        unmet.append('accepted_scope_changed')
    children = await asyncio.to_thread(intake.github.read_subissues, issue['url'])
    intake.require_active(generation)
    urls = report.get('fine_issue_urls', [])
    prefix = 'https://github.com/' + target['repository'] + '/issues/'
    if (not isinstance(urls, list) or len(urls) != len(set(urls))
        or any(not isinstance(u, str) or not re.fullmatch(re.escape(prefix) + '[1-9][0-9]*', u) for u in urls)
        or set(urls) != {c['url'] for c in children} or any(c['state'] != 'closed' for c in children)):
        unmet.append('fine_issue_work_unconfirmed')
    result.update(fine_issues=children, leftovers=report.get('leftovers'))
    if not isinstance(report.get('leftovers'), list):
        unmet.append('leftovers_missing')
    pr = None
    url = report.get('pr_url')
    if url is not None:
        if not isinstance(url, str) or not re.fullmatch('https://github.com/' + re.escape(target['repository']) + '/pull/[1-9][0-9]*', url):
            unmet.append('pr_repository_mismatch')
        else:
            pr = await asyncio.to_thread(intake.github.read_pr, url)
            intake.require_active(generation)
            if pr.get('url') != url or pr.get('state') not in {'open', 'merged'}:
                unmet.append('pr_unconfirmed')
            if pr.get('head_commit') != fixed:
                if pr.get('state') != 'merged' or not re.fullmatch('[a-f0-9]{40}', pr.get('head_commit', '')):
                    unmet.append('pr_version_mismatch')
                else:
                    _git(target['repo_path'], 'merge-base', '--is-ancestor', pr['head_commit'], fixed)
                    _git(target['repo_path'], 'merge-base', '--is-ancestor', pr['merge_commit'], fixed)
            review = pr.get('review')
            result.update(pr=pr, pr_status='merged' if pr['state'] == 'merged' else 'awaiting_merge' if review == 'approved' else 'awaiting_review',
                          review_status='approved' if review == 'approved' else 'changes_requested' if review == 'changes_requested' else 'pending')
    branches = report.get('sync_branches', [])
    if not isinstance(branches, list) or len(branches) != len(set(branches)):
        unmet.append('branch_sync_unconfirmed')
        branches = []
    sync = []
    for branch in branches:
        if not isinstance(branch, str) or not re.fullmatch('[A-Za-z0-9_./-]+', branch):
            unmet.append('branch_sync_unconfirmed')
            continue
        _git(target['repo_path'], 'check-ref-format', '--branch', branch)
        local = _git(target['repo_path'], 'rev-parse', 'refs/heads/' + branch)
        remote = await asyncio.to_thread(intake.github.read_branch, target['repository'], branch)
        intake.require_active(generation)
        if not re.fullmatch('[a-f0-9]{40}', local) or local != remote:
            unmet.append('branch_sync_unconfirmed')
        sync.append({'branch': branch, 'local_commit': local, 'remote_commit': remote})
    result['sync'] = sync
    if pr and (pr.get('head_branch') not in branches or pr['state'] == 'merged' and pr.get('base_branch') not in branches):
        unmet.append('branch_sync_missing')
    requirements = delivery_requirements(issue['body'])
    if requirements['clarification_criteria']:
        unmet.append('merge_obligation_unclear')
    if requirements['merge_required'] and (not pr or pr['state'] != 'merged'):
        unmet.append('required_merge_missing')
    if requirements['merge_forbidden'] and pr and pr['state'] == 'merged':
        unmet.append('merge_forbidden')
    if re.search(r'(?:must|shall|requires?)\b.{0,30}\b(?:approved?\s+review|review\s+approval)\b|(?:必须|需要|要求).{0,20}(?:审查通过|审查批准)', issue['body'], re.IGNORECASE) and result['review_status'] != 'approved':
        unmet.append('required_review_missing')
    after = await asyncio.to_thread(repository_source_state, target)
    if after != current:
        unmet.append('delivery_source_changed')


def completion_ready(intake, record):
    """The native Hook uses this after validating the current original worker claim."""
    execution = record.get('dsh_execution', {})
    acceptance = execution.get('acceptance', {})
    if (acceptance.get('status') != 'accepted' or acceptance.get('delivered') is not True
        or execution.get('state') != 'awaiting_acceptance'
        or _human_pending(execution)
        or execution.get('stop_requested') or any(j.get('status') in {'running', 'stopping'} for j in execution.get('jobs', []))):
        return False
    try:
        current = repository_source_state(record['target'])
        return (current['head'] == acceptance.get('source_commit')
                and current['source_digest'] == acceptance.get('source_digest')
                and current['workspace_status'] == acceptance.get('workspace_status'))
    except (ManagementError, OSError, ValueError):
        return False
