from fastapi import APIRouter, HTTPException
import json
import logging

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
    VERIFICATION_CONFIDENCE_LEVELS,
    assemble_poc_brief,
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
    patch_base_knowledge_entry,
    utcnow,
    validate_facts_exist,
    validate_intent_creator_worker,
    validate_goal_not_in_sources,
)

LOG = logging.getLogger(__name__)

router = APIRouter(tags=["intents"])

MAIN_FACT_TYPE_PRIORITY: list[str] = ["dataflow", "sink", "source", "constraint", "verification"]
VERIFY_MAIN_FACT_TYPE_PRIORITY: list[str] = ["verification", "constraint", "dataflow", "sink", "source"]


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
        task_kind = body.task_kind
        # Heuristic: description starting with VERIFY: also marks verify intent
        if task_kind is None and body.description.upper().startswith("VERIFY"):
            task_kind = "verify"
        if task_kind is None:
            task_kind = "explore"

        poc_brief_json = None
        fire_status = None
        if task_kind == "verify":
            brief = assemble_poc_brief(conn, project_id, body.from_, body.description)
            poc_brief_json = brief.model_dump_json()
            fire_status = "pending"

        conn.execute(
            """INSERT INTO intents
               (id, project_id, to_fact_id, description, creator, worker, last_heartbeat_at, created_at, concluded_at, task_kind, poc_brief, fire_status)
               VALUES (?, ?, NULL, ?, ?, ?, ?, ?, NULL, ?, ?, ?)""",
            (
                iid,
                project_id,
                body.description,
                body.creator,
                body.worker,
                now if claimed else None,
                now,
                task_kind,
                poc_brief_json,
                fire_status,
            ),
        )
        for fid in body.from_:
            conn.execute(
                "INSERT INTO intent_sources (intent_id, project_id, fact_id) VALUES (?, ?, ?)",
                (iid, project_id, fid),
            )

        row = conn.execute(
            "SELECT * FROM intents WHERE id = ? AND project_id = ?",
            (iid, project_id),
        ).fetchone()
        return intent_to_model(conn, row, project_id)


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
        intent_row = get_claimable_open_intent_or_404(conn, project_id, intent_id, body.worker)
        intent_keys = intent_row.keys()
        is_verify = ("task_kind" in intent_keys and intent_row["task_kind"] == "verify")

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
            is_verification = obs.type == "verification"
            target_code_version: str | None = None
            if is_verification:
                obs_confidence = obs.confidence or "poc-confirmed"
                if obs_confidence not in VERIFICATION_CONFIDENCE_LEVELS:
                    obs_confidence = "poc-confirmed"
                verifies = obs.verifies
                if not verifies:
                    raise HTTPException(400, "verification observation requires verifies")
                # ensure target exists; stamp verification with target code_version for folding
                target = conn.execute(
                    "SELECT id, code_version FROM facts WHERE project_id = ? AND id = ?",
                    (project_id, verifies),
                ).fetchone()
                if target is None:
                    raise HTTPException(400, f"verifies target not found: {verifies}")
                target_code_version = target["code_version"]
            else:
                # audit path: force max static-confirmed when typed
                if obs.confidence and obs.confidence in VERIFICATION_CONFIDENCE_LEVELS:
                    raise HTTPException(
                        400,
                        f"Audit-side facts cannot claim {obs.confidence}; use type=verification",
                    )
                obs_confidence = AUDIT_MAX_CONFIDENCE if obs.type is not None else None
                verifies = None
            gate_confidence(obs.type, obs_confidence)

            # verification never dedupes; constraint with empty locations + why_failed uses degenerate key
            if not is_verification:
                existing = find_existing_fact(conn, project_id, obs.type, obs.locations)
            else:
                existing = None
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
                    oracle_draft=existing["oracle_draft"] if "oracle_draft" in existing.keys() else None,
                    payload_draft=existing["payload_draft"] if "payload_draft" in existing.keys() else None,
                ))
                continue

            fid = next_fact_id(conn, project_id)
            # verification inherits target stamp so effective_confidence folding is not false-stale
            if is_verification and target_code_version:
                code_version = target_code_version
            else:
                code_version = compute_code_version(obs.locations, origin_desc)
            locations_json = json.dumps(sorted(obs.locations)) if obs.locations else None
            evidence = obs.evidence
            if obs.why_failed and not is_verification:
                why = json.dumps(obs.why_failed, ensure_ascii=False)
                evidence = f"{evidence}\n[why_failed] {why}" if evidence else f"[why_failed] {why}"

            conn.execute(
                """INSERT INTO facts (id, project_id, description, type, confidence,
                   locations, code_version, evidence, verifies, intent_id, batch_id,
                   oracle_draft, payload_draft)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    fid, project_id, obs.description, obs.type, obs_confidence,
                    locations_json, code_version, evidence, verifies, intent_id, batch_id,
                    obs.oracle_draft, obs.payload_draft,
                ),
            )
            created_facts.append(_row_to_fact(
                id=fid, description=obs.description, obs_type=obs.type,
                obs_confidence=obs_confidence, obs_locations=locations_json,
                code_version=code_version, evidence=evidence,
                verifies=verifies, intent_id=intent_id, batch_id=batch_id,
                oracle_draft=obs.oracle_draft,
                payload_draft=obs.payload_draft,
            ))

        main_fact = _select_main_fact(created_facts, verify=is_verify)
        conn.execute(
            """UPDATE intents SET to_fact_id = ?, worker = ?, last_heartbeat_at = ?, concluded_at = ?,
               fire_status = CASE WHEN task_kind = 'verify' THEN 'fired' ELSE fire_status END
               WHERE id = ? AND project_id = ?""",
            (main_fact.id, body.worker, now, now, intent_id, project_id),
        )

        if body.base_knowledge_patches:
            _apply_base_knowledge_patches(
                conn,
                project_id,
                patches=body.base_knowledge_patches,
                revised_by=main_fact.id,
                actor=body.worker,
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


def _apply_base_knowledge_patches(
    conn,
    project_id: str,
    *,
    patches,
    revised_by: str,
    actor: str,
) -> None:
    """Apply explore-emitted BK patches; skip bad entry_id; never roll back facts."""
    for patch in patches:
        try:
            patch_base_knowledge_entry(
                conn,
                project_id,
                patch.entry_id,
                statement=patch.statement,
                evidence=patch.evidence,
                confidence=patch.confidence,
                revised_by=revised_by,
                actor=actor,
            )
        except Exception as exc:
            LOG.warning(
                "base_knowledge patch skipped project=%s entry=%s revised_by=%s error=%s",
                project_id,
                patch.entry_id,
                revised_by,
                exc,
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
    oracle_draft: str | None = None,
    payload_draft: str | None = None,
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
        oracle_draft=oracle_draft,
        payload_draft=payload_draft,
    )


def _select_main_fact(facts: list[Fact], *, verify: bool = False) -> Fact:
    priority = VERIFY_MAIN_FACT_TYPE_PRIORITY if verify else MAIN_FACT_TYPE_PRIORITY
    for priority_type in priority:
        for f in facts:
            if f.type == priority_type:
                return f
    return facts[0]
