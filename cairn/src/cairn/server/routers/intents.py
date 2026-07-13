from fastapi import APIRouter
import json

from cairn.server.db import get_conn
from cairn.server.models import (
    ConcludeRequest,
    ConcludeResponse,
    CreateIntentRequest,
    Fact,
    HeartbeatRequest,
    Intent,
    Observation,
)
from cairn.server.services import (
    AUDIT_MAX_CONFIDENCE,
    check_project_active,
    compute_code_version,
    find_existing_fact,
    gate_confidence,
    get_claimable_open_intent_or_404,
    get_releasable_open_intent_or_404,
    intent_to_model,
    merge_locations,
    next_fact_id,
    next_intent_id,
    parse_json_list,
    utcnow,
    validate_facts_exist,
    validate_intent_creator_worker,
    validate_goal_not_in_sources,
)

router = APIRouter(tags=["intents"])

MAIN_FACT_TYPE_PRIORITY: list[str] = ["dataflow", "sink", "source", "constraint", "verification"]


@router.post(
    "/projects/{project_id}/intents",
    response_model=Intent,
    status_code=201,
)
def create_intent(project_id: str, body: CreateIntentRequest):
    with get_conn() as conn:
        check_project_active(conn, project_id)
        validate_facts_exist(conn, project_id, body.from_)
        validate_goal_not_in_sources(body.from_)
        validate_intent_creator_worker(body.creator, body.worker)

        now = utcnow()
        iid = next_intent_id(conn, project_id)
        claimed = body.worker is not None
        conn.execute(
            "INSERT INTO intents (id, project_id, to_fact_id, description, creator, worker, last_heartbeat_at, created_at, concluded_at) VALUES (?, ?, NULL, ?, ?, ?, ?, ?, NULL)",
            (
                iid,
                project_id,
                body.description,
                body.creator,
                body.worker,
                now if claimed else None,
                now,
            ),
        )
        for fid in body.from_:
            conn.execute(
                "INSERT INTO intent_sources (intent_id, project_id, fact_id) VALUES (?, ?, ?)",
                (iid, project_id, fid),
            )

        return Intent(
            id=iid,
            **{"from": body.from_},
            to=None,
            description=body.description,
            creator=body.creator,
            worker=body.worker,
            last_heartbeat_at=now if claimed else None,
            created_at=now,
            concluded_at=None,
        )


@router.post(
    "/projects/{project_id}/intents/{intent_id}/heartbeat",
    response_model=Intent,
)
def heartbeat(project_id: str, intent_id: str, body: HeartbeatRequest):
    with get_conn() as conn:
        check_project_active(conn, project_id)
        get_claimable_open_intent_or_404(conn, project_id, intent_id, body.worker)

        now = utcnow()
        conn.execute(
            "UPDATE intents SET worker = ?, last_heartbeat_at = ? WHERE id = ? AND project_id = ?",
            (body.worker, now, intent_id, project_id),
        )

        updated = conn.execute(
            "SELECT * FROM intents WHERE id = ? AND project_id = ?",
            (intent_id, project_id),
        ).fetchone()
        return intent_to_model(conn, updated, project_id)


@router.post(
    "/projects/{project_id}/intents/{intent_id}/release",
    response_model=Intent,
)
def release(project_id: str, intent_id: str, body: HeartbeatRequest):
    with get_conn() as conn:
        check_project_active(conn, project_id)
        row = get_releasable_open_intent_or_404(conn, project_id, intent_id, body.worker)

        if row["worker"] == body.worker:
            conn.execute(
                "UPDATE intents SET worker = NULL WHERE id = ? AND project_id = ?",
                (intent_id, project_id),
            )
            row = conn.execute(
                "SELECT * FROM intents WHERE id = ? AND project_id = ?",
                (intent_id, project_id),
            ).fetchone()

        return intent_to_model(conn, row, project_id)


@router.post(
    "/projects/{project_id}/intents/{intent_id}/conclude",
    response_model=ConcludeResponse,
)
def conclude(project_id: str, intent_id: str, body: ConcludeRequest):
    with get_conn() as conn:
        check_project_active(conn, project_id)
        get_claimable_open_intent_or_404(conn, project_id, intent_id, body.worker)

        now = utcnow()
        batch_id = f"b{intent_id}_{utcnow().replace('-', '').replace(':', '').replace('T', '').replace('Z', '')}"

        if body.observations is not None:
            observations = body.observations
        else:
            observations = [Observation(description=body.description)]

        origin_row = conn.execute(
            "SELECT description FROM facts WHERE project_id = ? AND id = 'origin'",
            (project_id,),
        ).fetchone()
        origin_desc = origin_row["description"] if origin_row else None

        created_facts: list[Fact] = []
        for obs in observations:
            obs_confidence = obs.type is not None and AUDIT_MAX_CONFIDENCE or None
            gate_confidence(obs.type, obs_confidence)

            existing = find_existing_fact(conn, project_id, obs.type, obs.locations)
            if existing is not None:
                merged_locations_json = merge_locations(
                    parse_json_list(existing["locations"]),
                    obs.locations,
                )
                merged_code_version = compute_code_version(
                    parse_json_list(merged_locations_json) if merged_locations_json else None,
                    origin_desc,
                )
                conn.execute(
                    "UPDATE facts SET locations = ?, code_version = ? WHERE id = ? AND project_id = ?",
                    (merged_locations_json, merged_code_version, existing["id"], project_id),
                )
                created_facts.append(_row_to_fact(
                    id=existing["id"],
                    description=existing["description"],
                    obs_type=existing["type"],
                    obs_confidence=existing["confidence"],
                    obs_locations=merged_locations_json,
                    code_version=merged_code_version,
                    evidence=existing["evidence"],
                    verifies=existing["verifies"],
                    intent_id=existing["intent_id"],
                    batch_id=existing["batch_id"],
                ))
                continue

            fid = next_fact_id(conn, project_id)
            code_version = compute_code_version(obs.locations, origin_desc)
            locations_json = json.dumps(sorted(obs.locations)) if obs.locations else None

            conn.execute(
                """INSERT INTO facts (id, project_id, description, type, confidence,
                   locations, code_version, evidence, verifies, intent_id, batch_id)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    fid, project_id, obs.description, obs.type, obs_confidence,
                    locations_json, code_version, obs.evidence, None, intent_id, batch_id,
                ),
            )
            created_facts.append(_row_to_fact(
                id=fid, description=obs.description, obs_type=obs.type,
                obs_confidence=obs_confidence, obs_locations=locations_json,
                code_version=code_version, evidence=obs.evidence,
                verifies=None, intent_id=intent_id, batch_id=batch_id,
            ))

        main_fact = _select_main_fact(created_facts)
        conn.execute(
            "UPDATE intents SET to_fact_id = ?, worker = ?, last_heartbeat_at = ?, concluded_at = ? WHERE id = ? AND project_id = ?",
            (main_fact.id, body.worker, now, now, intent_id, project_id),
        )

        updated = conn.execute(
            "SELECT * FROM intents WHERE id = ? AND project_id = ?",
            (intent_id, project_id),
        ).fetchone()

        return ConcludeResponse(
            fact=main_fact,
            facts=created_facts,
            intent=intent_to_model(conn, updated, project_id),
        )


def _row_to_fact(
    *,
    id: str,
    description: str,
    obs_type: str | None,
    obs_confidence: str | None = None,
    obs_locations: str | None = None,
    code_version: str | None = None,
    evidence: str | None = None,
    verifies: str | None = None,
    intent_id: str | None = None,
    batch_id: str | None = None,
) -> Fact:
    return Fact(
        id=id,
        description=description,
        type=obs_type,
        confidence=obs_confidence,
        locations=parse_json_list(obs_locations) if obs_locations else None,
        code_version=code_version,
        evidence=evidence,
        verifies=verifies,
        intent_id=intent_id,
        batch_id=batch_id,
    )


def _select_main_fact(facts: list[Fact]) -> Fact:
    for priority_type in MAIN_FACT_TYPE_PRIORITY:
        for f in facts:
            if f.type == priority_type:
                return f
    return facts[0]
