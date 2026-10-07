"""Dashboard routes delegate every operation to the authenticated local bridge."""
from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, ConfigDict, Field

from .manager import ManagementError


class DirectoryChange(BaseModel):
    model_config = ConfigDict(extra='forbid')
    expected_version: int = Field(strict=True, ge=0)
    change: dict



class TaskOperation(BaseModel):
    model_config = ConfigDict(extra='forbid')
    action: str
    request_id: str
    report: dict | None = None
    plan: dict | None = None
    manual_session_id: str | None = None
    grant_id: str | None = None
    instruction_id: str | None = None
    text: str | None = None
    expected_turn_id: str | None = None
    human_request_id: str | None = None
    reply_id: str | None = None
    response: dict | None = None


class KnowledgeOperation(BaseModel):
    model_config = ConfigDict(extra='forbid')
    action: str
    source: dict | None = None
    expected_version: int | None = Field(default=None, strict=True, ge=0)
    source_id: str | None = None
    query_id: str | None = None
    question: str | None = None
    scope_ids: list[str] | None = None
    request_id: str | None = None
    channel_id: str | None = None
    auto_supplement: bool = False
    material_ids: list[str] | None = None

class ArchiveOperation(BaseModel):
    model_config = ConfigDict(extra='forbid')
    action: str
    registration: dict | None = None
    source_id: str | None = None
    query_id: str | None = None
    question: str | None = None
    scope_ids: list[str] | None = None
    complete: bool = False
    backup_id: str | None = None
    restore_id: str | None = None
    protection_id: str | None = None
    kind: str = 'checkpoint'

class CollaborationOperation(BaseModel):
    model_config = ConfigDict(extra='forbid')
    action: str
    details: dict

class ObservationOperation(BaseModel):
    model_config = ConfigDict(extra='forbid')
    action: str
    scope: str | None = None
    registration: dict | None = None

def create_router(authenticated_client):
    router = APIRouter()
    last_snapshot = None

    def failure(exc):
        status = {'unauthorized': 403, 'forbidden': 403, 'version_conflict': 409,
                  'binding_conflict': 409, 'repository_busy': 409, 'handoff_blocked': 409, 'unavailable': 503}.get(exc.code, 422)
        return HTTPException(status, {'code': exc.code, 'message': str(exc)})

    @router.get('/snapshot')
    def snapshot(request: Request):
        nonlocal last_snapshot
        client = authenticated_client(request)
        try:
            last_snapshot = client.read_snapshot()
            return last_snapshot
        except ManagementError as exc:
            if exc.code == 'unavailable' and last_snapshot is not None:
                return {**last_snapshot, 'status': 'unverified', 'runtime': 'manager_unavailable',
                        'needs_human': ['Manager unavailable; showing the last verified directory.']}
            raise failure(exc) from exc

    @router.post('/directory')
    def directory(body: DirectoryChange, request: Request):
        client = authenticated_client(request)
        try:
            return client.apply_directory_change(body.expected_version, body.change)
        except ManagementError as exc:
            raise failure(exc) from exc

    @router.post('/archives')
    def archives(body: ArchiveOperation, request: Request):
        client = authenticated_client(request)
        try:
            if body.action == 'register':
                return client.register_archive_source(body.registration)
            if body.action == 'query':
                return client.query_archive(body.source_id, body.query_id, body.question, body.scope_ids, body.complete)
            if body.action == 'protect':
                return client.protect_archive(body.source_id, body.protection_id)
            if body.action == 'backup':
                return client.backup_archive(body.source_id, body.backup_id, body.kind)
            if body.action == 'restore':
                return client.restore_archive(body.backup_id, body.restore_id)
            raise ManagementError('invalid_change', 'Select a registered archive query, explicit protection, checkpoint or data-only restore.')
        except ManagementError as exc:
            raise failure(exc) from exc

    @router.post('/collaboration')
    def collaboration(body: CollaborationOperation, request: Request):
        client = authenticated_client(request)
        try:
            return client.collaborate(body.action, body.details)
        except ManagementError as exc:
            raise failure(exc) from exc

    @router.post('/observations')
    def observations(body: ObservationOperation, request: Request):
        client = authenticated_client(request)
        try:
            if body.action == 'register' and body.scope is None:
                return client.register_observation_source(body.registration)
            if body.action == 'refresh' and body.registration is None:
                return client.refresh_manual_sessions(body.scope)
            raise ManagementError('invalid_change', 'Select an explicit source registration or read-only scope refresh.')
        except ManagementError as exc:
            raise failure(exc) from exc

    @router.post('/task')
    def task(body: TaskOperation, request: Request):
        client = authenticated_client(request)
        try:
            if body.action in {'takeover', 'return'}:
                if any(v is not None for v in (body.report, body.plan, body.instruction_id, body.text, body.human_request_id, body.reply_id, body.response)):
                    raise ManagementError('invalid_change', 'A current-work grant cannot carry other task operations.')
                if body.action == 'takeover':
                    return client.take_over_session(body.request_id, body.manual_session_id, body.grant_id, body.expected_turn_id)
                if body.manual_session_id is not None or body.expected_turn_id is not None:
                    raise ManagementError('invalid_change', 'Return identifies the existing grant only.')
                return client.return_session_control(body.request_id, body.grant_id)
            if body.manual_session_id is not None or body.grant_id is not None:
                raise ManagementError('invalid_change', 'Manual grant fields require takeover or return.')
            if body.action == 'answer':
                if any(v is not None for v in (body.report, body.plan, body.instruction_id, body.text, body.expected_turn_id)):
                    raise ManagementError('invalid_change', 'Human response fields cannot carry other task operations.')
                return client.answer_human_request(body.request_id, body.human_request_id, body.reply_id, body.response)
            if any(v is not None for v in (body.human_request_id, body.reply_id, body.response)):
                raise ManagementError('invalid_change', 'Human response fields require answer action.')
            if body.action == 'prepare':
                if body.report is not None or any(v is not None for v in (body.instruction_id, body.text, body.expected_turn_id)):
                    raise ManagementError('invalid_change', 'Preparation accepts only the explicit baseline plan.')
                return client.prepare_task(body.request_id, body.plan)
            if body.plan is not None:
                raise ManagementError('invalid_change', 'Baseline plan requires preparation.')
            if body.action in {'append', 'stop', 'continue'}:
                if body.report is not None:
                    raise ManagementError('invalid_change', 'Control requests cannot assert delivery evidence.')
                return client.control_task(body.request_id, body.action, body.instruction_id, body.text, body.expected_turn_id)
            if any(v is not None for v in (body.instruction_id, body.text, body.expected_turn_id)):
                raise ManagementError('invalid_change', 'Control fields require a control action.')
            if body.action == 'delivery':
                return client.record_task_delivery(body.request_id, body.report)
            operation = {'verify': 'verify_task_execution', 'start': 'start_task', 'refresh': 'refresh_task', 'reconcile': 'reconcile_task', 'source': 'refresh_task_source'}.get(body.action)
            if operation is None or body.report is not None:
                raise ManagementError('invalid_change', 'Select verify, start, refresh or evidence-based delivery.')
            return getattr(client, operation)(body.request_id)
        except ManagementError as exc:
            raise failure(exc) from exc

    @router.post('/knowledge')
    def knowledge(body: KnowledgeOperation, request: Request):
        client = authenticated_client(request)
        try:
            if body.action == 'register':
                return client.register_knowledge_source(body.expected_version, body.source)
            if body.action == 'query':
                return client.query_knowledge(body.source_id, body.query_id, body.question, body.scope_ids,
                    body.request_id, body.channel_id, body.auto_supplement)
            if body.action == 'supplement':
                return client.supplement_knowledge(body.query_id, body.material_ids)
            raise ManagementError('invalid_change', 'Select source registration, scoped query or original-task fact supplement.')
        except ManagementError as exc:
            raise failure(exc) from exc

    return router
