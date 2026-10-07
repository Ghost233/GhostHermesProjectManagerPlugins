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

def create_router(authenticated_client):
    router = APIRouter()
    last_snapshot = None

    def failure(exc):
        status = {'unauthorized': 403, 'forbidden': 403, 'version_conflict': 409,
                  'binding_conflict': 409, 'repository_busy': 409, 'unavailable': 503}.get(exc.code, 422)
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

    @router.post('/task')
    def task(body: TaskOperation, request: Request):
        client = authenticated_client(request)
        try:
            if body.action == 'delivery':
                return client.record_task_delivery(body.request_id, body.report)
            operation = {'verify': 'verify_task_execution', 'start': 'start_task', 'refresh': 'refresh_task'}.get(body.action)
            if operation is None or body.report is not None:
                raise ManagementError('invalid_change', 'Select verify, start, refresh or evidence-based delivery.')
            return getattr(client, operation)(body.request_id)
        except ManagementError as exc:
            raise failure(exc) from exc

    return router
