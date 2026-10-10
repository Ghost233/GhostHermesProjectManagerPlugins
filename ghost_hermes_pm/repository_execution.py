"""Admit one explicitly approved, private original DSH execution reference."""
import hashlib
import json
import os
from pathlib import Path
import stat

from .manager import ManagementError


def private_file(reference):
    path = Path(reference)
    if not path.is_absolute() or path.is_symlink() or str(path.resolve(strict=True)) != str(path):
        raise ManagementError('unsafe_state', 'Execution references require canonical private files.')
    if any((parent / '.git').exists() for parent in path.parents):
        raise ManagementError('unsafe_state', 'Private execution references must remain outside source worktrees.')
    info = path.stat()
    if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077:
        raise ManagementError('unsafe_state', 'Execution references must be owner-only files.')
    return path


def execution_configuration(target):
    """Configuration approval is separate from live startup and stop evidence."""
    try:
        reference = private_file(target['execution_ref'])
        configuration = json.loads(reference.read_text())
        if (set(configuration) != {'schema', 'approved', 'runtime_package_root', 'node_bin', 'model', 'budget', 'skill_directories', 'tool_receipt_ref'}
            or configuration.get('schema') != 1 or configuration.get('approved') is not True):
            raise ValueError('Unapproved execution reference.')
        sdk = Path(configuration['runtime_package_root'])
        node = Path(configuration['node_bin'])
        if any(not p.is_absolute() or str(p.resolve(strict=True)) != str(p) for p in (sdk, node)):
            raise ValueError('Canonical original runtime paths required.')
        model = configuration['model']
        if (set(model) != {'api', 'base_url', 'model', 'context_window', 'api_key_env', 'env_file'}
                or model['api'] != 'openai-completions' or not model['base_url'].startswith('http://127.0.0.1:')
                or not isinstance(model['model'], str) or not model['model']
                or type(model['context_window']) is not int or model['context_window'] <= 0):
            raise ValueError('Approved local model configuration required.')
        env = {}
        for line in private_file(model['env_file']).read_text().splitlines():
            if line.strip() and not line.lstrip().startswith('#'):
                key, value = line.split('=', 1)
                env[key.strip()] = value.strip().strip('\"\'')
        key_name = model['api_key_env']
        if key_name not in env or not env[key_name]:
            raise ValueError('An approved private credential reference is required.')
        budget = configuration['budget']
        fields = {'max_wall_seconds', 'max_model_requests', 'max_reported_tokens', 'max_output_tokens_per_request'}
        if set(budget) != fields or any(type(budget[k]) is not int or not 0 < budget[k] <= 1000000 for k in fields):
            raise ValueError('Explicit finite execution limits required.')
        if budget['max_output_tokens_per_request'] > model['context_window']:
            raise ValueError('Output limit exceeds actual context.')
        skills = configuration['skill_directories']
        if not skills or any(not Path(p).is_absolute() or str(Path(p).resolve(strict=True)) != p or not Path(p).is_dir() for p in skills):
            raise ValueError('Approved original skill directories are required.')
        receipt = json.loads(private_file(configuration['tool_receipt_ref']).read_text())
        source = Path(__file__).with_name('owned_runtime.mjs')
        if (receipt.get('status') != 'passed' or receipt.get('foreground_only') is not True
            or sorted(receipt.get('tool_catalog', [])) != ['bash', 'job_kill', 'job_list', 'job_output']
            or not {'allowed_write', 'outside_write_denied', 'symlink_write_denied', 'hardlink_write_denied', 'danger_denied', 'runtime_directory'} <= set(receipt.get('passed_cases', []))
            or receipt.get('composition_sha256') != hashlib.sha256(source.read_bytes()).hexdigest()
            or receipt.get('node_sha256') != hashlib.sha256(node.read_bytes()).hexdigest()
            or not {'@deepseek-ai/dsh/lib/profile-boot.js', '@deepseek-ai/dsh-sandbox-policy/lib/index.js',
                    '@deepseek-ai/dsh-tool-bash/lib/index.js', '@deepseek-ai/dsh-tool-jobs/lib/index.js',
                    '@deepseek-ai/dsh-jobs-local/lib/index.js', '@deepseek-ai/dsh-agent-loop/lib/index.js'} <= set(receipt.get('sdk_sources', {}))):
            raise ValueError('Exact original tool composition has not been verified.')
        for relative, digest in receipt['sdk_sources'].items():
            path = sdk / relative
            if not path.resolve(strict=True).is_relative_to(sdk) or hashlib.sha256(path.read_bytes()).hexdigest() != digest:
                raise ValueError('Original SDK proof source changed.')
        runtime = {'model': {'provider': 'hermes-owned', 'model': model['model'], 'configuration': {
            'api': model['api'], 'baseURL': model['base_url'], 'apiKeyEnv': key_name,
            'models': [{'id': model['model'], 'contextWindow': model['context_window'],
                        'maxTokens': budget['max_output_tokens_per_request'], 'input': ['text']}]}},
                   'budget': budget, 'skill_directories': skills}
        return {'runtime_package_root': str(sdk), 'node_bin': str(node), 'runtime_configuration': runtime,
                'reference_sha256': hashlib.sha256(reference.read_bytes()).hexdigest()}, {key_name: env[key_name]}
    except ManagementError:
        raise
    except (KeyError, TypeError, ValueError, OSError):
        raise ManagementError('configuration_missing', 'The approved dedicated execution configuration is incomplete or unverified.') from None
