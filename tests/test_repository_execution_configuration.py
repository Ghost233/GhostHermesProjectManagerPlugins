"""Explicit bounded workflow configuration must match its actual tool catalog."""
import json
import os
from pathlib import Path

import pytest

from ghost_hermes_pm.manager import ManagementError
from ghost_hermes_pm.repository_execution import execution_configuration
from simple_worker_model import execution_reference


@pytest.fixture
def workflow_reference(tmp_path):
    sdk = os.environ.get('DSH_TEST_SDK_ROOT')
    if not sdk:
        if os.environ.get('DSH_REQUIRE_SDK_SMOKE') == '1':
            pytest.fail('Original DSH SDK is required for exact composition admission.')
        pytest.skip('Original DSH SDK is required.')
    reference = Path(execution_reference(tmp_path, 'http://127.0.0.1:9/v1', Path(sdk)))
    return reference


def set_scope(reference, skills, catalog):
    configuration = json.loads(reference.read_text())
    if not skills:
        configuration['skill_directories'] = []
    receipt = Path(configuration['tool_receipt_ref'])
    proof = json.loads(receipt.read_text())
    proof['tool_catalog'] = catalog
    receipt.write_text(json.dumps(proof))
    reference.write_text(json.dumps(configuration))


def test_explicit_question_only_scope_admits_exact_five_tools(workflow_reference):
    set_scope(workflow_reference, False, ['ask_user_question', 'bash', 'job_kill', 'job_list', 'job_output'])
    configuration, _ = execution_configuration({'execution_ref': str(workflow_reference)})
    assert configuration['runtime_configuration']['skill_directories'] == []


def test_configured_workflow_skill_requires_exact_six_tools(workflow_reference):
    set_scope(workflow_reference, True, ['ask_user_question', 'bash', 'job_kill', 'job_list', 'job_output', 'skill'])
    configuration, _ = execution_configuration({'execution_ref': str(workflow_reference)})
    assert configuration['runtime_configuration']['skill_directories']


@pytest.mark.parametrize('skills,catalog', [
    (False, ['ask_user_question', 'bash', 'job_kill', 'job_list', 'job_output', 'skill']),
    (True, ['ask_user_question', 'bash', 'job_kill', 'job_list', 'job_output']),
    (False, ['ask_user_question', 'bash', 'job_kill', 'job_list', 'job_output', 'unverified_tool']),
])
def test_mismatched_or_unverified_tool_catalog_stays_unadmitted(workflow_reference, skills, catalog):
    set_scope(workflow_reference, skills, catalog)
    with pytest.raises(ManagementError) as error:
        execution_configuration({'execution_ref': str(workflow_reference)})
    assert error.value.code == 'configuration_missing'
