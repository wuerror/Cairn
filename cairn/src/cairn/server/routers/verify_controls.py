from __future__ import annotations

from fastapi import APIRouter

from cairn.server.db import get_conn
from cairn.server.models import (
    FireApprovalRequest,
    Intent,
    KillVerifyRequest,
    ProxyTrafficEntry,
    RecordProxyTrafficRequest,
    VerifyControlState,
)
from cairn.server.services import (
    check_project_active,
    clear_verify_kill,
    get_intent_or_404,
    get_project_or_404,
    get_verify_control,
    intent_to_model,
    list_proxy_traffic,
    origin_allowlist,
    record_proxy_traffic,
    request_verify_kill,
    set_intent_fire_status,
)

router = APIRouter(tags=["verify"])


@router.get("/projects/{project_id}/verify/control", response_model=VerifyControlState)
def get_control(project_id: str):
    with get_conn() as conn:
        get_project_or_404(conn, project_id)
        return get_verify_control(conn, project_id)


@router.post("/projects/{project_id}/verify/kill", response_model=VerifyControlState)
def kill_verify(project_id: str, body: KillVerifyRequest):
    with get_conn() as conn:
        get_project_or_404(conn, project_id)
        return request_verify_kill(conn, project_id, actor=body.actor, reason=body.reason)


@router.post("/projects/{project_id}/verify/kill/clear", response_model=VerifyControlState)
def clear_kill(project_id: str):
    with get_conn() as conn:
        get_project_or_404(conn, project_id)
        return clear_verify_kill(conn, project_id)


@router.post(
    "/projects/{project_id}/intents/{intent_id}/fire",
    response_model=Intent,
)
def fire_approval(project_id: str, intent_id: str, body: FireApprovalRequest):
    with get_conn() as conn:
        check_project_active(conn, project_id)
        get_intent_or_404(conn, project_id, intent_id)
        status = "approved" if body.action == "approve" else "denied"
        set_intent_fire_status(conn, project_id, intent_id, status)
        updated = conn.execute(
            "SELECT * FROM intents WHERE id = ? AND project_id = ?",
            (intent_id, project_id),
        ).fetchone()
        return intent_to_model(conn, updated, project_id)


@router.get("/projects/{project_id}/verify/allowlist")
def get_allowlist(project_id: str):
    with get_conn() as conn:
        get_project_or_404(conn, project_id)
        origin = conn.execute(
            "SELECT description FROM facts WHERE project_id = ? AND id = 'origin'",
            (project_id,),
        ).fetchone()
        desc = origin["description"] if origin else None
        return {"allowlist": origin_allowlist(desc)}


@router.get(
    "/projects/{project_id}/verify/proxy_traffic",
    response_model=list[ProxyTrafficEntry],
)
def get_proxy_traffic(project_id: str):
    with get_conn() as conn:
        get_project_or_404(conn, project_id)
        return list_proxy_traffic(conn, project_id)


@router.post(
    "/projects/{project_id}/verify/proxy_traffic",
    response_model=ProxyTrafficEntry,
    status_code=201,
)
def post_proxy_traffic(project_id: str, body: RecordProxyTrafficRequest):
    with get_conn() as conn:
        check_project_active(conn, project_id)
        return record_proxy_traffic(
            conn,
            project_id,
            intent_id=body.intent_id,
            request=body.request,
            response=body.response,
            baseline=body.baseline,
            status=body.status,
        )
