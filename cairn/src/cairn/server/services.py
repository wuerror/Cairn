from __future__ import annotations

import hashlib
import json
import sqlite3
from datetime import datetime, timezone

from fastapi import HTTPException

from cairn.server.models import ConfidenceLevel, Fact, FactType, Intent, Observation, ProjectMeta, ProjectReason

CONFIDENCE_LEVEL_ORDER: dict[ConfidenceLevel, int] = {
    "hypothesized": 0,
    "static-confirmed": 1,
    "reachable-confirmed": 2,
    "poc-confirmed": 3,
    "refuted": -1,
}

AUDIT_MAX_CONFIDENCE: ConfidenceLevel = "static-confirmed"
VERIFICATION_CONFIDENCE_LEVELS: set[ConfidenceLevel] = {
    "reachable-confirmed",
    "poc-confirmed",
    "refuted",
}

RESERVED_FACT_IDS = {"origin", "goal"}


def utcnow() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def compute_code_version(locations: list[str] | None, origin_description: str | None = None) -> str:
    if locations:
        payload = json.dumps(sorted(locations), sort_keys=True)
    elif origin_description:
        try:
            origin = json.loads(origin_description)
            commit = origin.get("codebase", {}).get("commit")
            if commit:
                return str(commit)
        except (json.JSONDecodeError, TypeError):
            pass
        payload = origin_description
    else:
        payload = utcnow()
    return hashlib.sha256(payload.encode()).hexdigest()[:12]


def canonical_fact_key(fact_type: str | None, locations: list[str] | None) -> str:
    loc_key = json.dumps(sorted(locations)) if locations else "[]"
    return f"{fact_type or ''}:{loc_key}"


def effective_confidence(
    conn: sqlite3.Connection,
    project_id: str,
    own_confidence: ConfidenceLevel | None,
    own_code_version: str | None,
    own_type: str | None,
    fact_id: str | None = None,
) -> tuple[ConfidenceLevel | None, bool]:
    if fact_id is None:
        return own_confidence, False
    if own_type == "verification":
        return own_confidence, False
    verifications = conn.execute(
        """SELECT confidence, code_version, rowid FROM facts
           WHERE project_id = ? AND verifies = ? AND type = 'verification'
           ORDER BY rowid DESC""",
        (project_id, fact_id),
    ).fetchall()
    if not verifications:
        return own_confidence, False
    latest = verifications[0]
    v_conf = latest["confidence"]
    if v_conf == "refuted":
        return "refuted", False
    if latest["code_version"] and own_code_version and latest["code_version"] != own_code_version:
        return own_confidence, True
    return v_conf, False


def gate_confidence(fact_type: str | None, confidence: ConfidenceLevel | None) -> None:
    if confidence is None:
        return
    if fact_type == "verification":
        if confidence not in VERIFICATION_CONFIDENCE_LEVELS:
            raise HTTPException(400, f"Verification fact confidence must be one of: {sorted(VERIFICATION_CONFIDENCE_LEVELS)}")
        return
    if confidence in VERIFICATION_CONFIDENCE_LEVELS:
        raise HTTPException(400, f"Audit-side facts cannot claim {confidence}; max is {AUDIT_MAX_CONFIDENCE}")


def find_existing_fact(
    conn: sqlite3.Connection,
    project_id: str,
    fact_type: str | None,
    locations: list[str] | None,
) -> sqlite3.Row | None:
    if fact_type == "verification" or (fact_type is None and locations is None):
        return None
    key = canonical_fact_key(fact_type, locations)
    rows = conn.execute(
        "SELECT * FROM facts WHERE project_id = ? AND type IS NOT NULL",
        (project_id,),
    ).fetchall()
    for row in rows:
        existing_key = canonical_fact_key(row["type"], parse_json_list(row["locations"]))
        if existing_key == key and row["id"] not in RESERVED_FACT_IDS:
            return row
    return None


def merge_locations(existing: list[str] | None, incoming: list[str] | None) -> str | None:
    merged = set()
    if existing:
        merged.update(existing)
    if incoming:
        merged.update(incoming)
    if not merged:
        return None
    return json.dumps(sorted(merged))


def parse_json_list(value: str | None) -> list[str] | None:
    if not value:
        return None
    try:
        parsed = json.loads(value)
        if isinstance(parsed, list):
            return parsed
    except json.JSONDecodeError:
        pass
    return None


def fact_from_row(row: sqlite3.Row, conn: sqlite3.Connection | None = None, project_id: str | None = None) -> Fact:
    eff_conf = None
    stale = False
    if conn is not None and project_id is not None and row["type"] is not None and row["type"] != "verification":
        eff_conf, stale = effective_confidence(
            conn, project_id,
            own_confidence=row["confidence"],
            own_code_version=row["code_version"],
            own_type=row["type"],
            fact_id=row["id"],
        )
    return Fact(
        id=row["id"],
        description=row["description"],
        type=row["type"],
        confidence=row["confidence"],
        locations=parse_json_list(row["locations"]),
        code_version=row["code_version"],
        evidence=row["evidence"],
        verifies=row["verifies"],
        intent_id=row["intent_id"],
        batch_id=row["batch_id"],
        effective_confidence=eff_conf,
        stale=stale,
    )


def next_project_id(conn: sqlite3.Connection) -> str:
    conn.execute("UPDATE counters SET value = value + 1 WHERE name = 'project'")
    row = conn.execute("SELECT value FROM counters WHERE name = 'project'").fetchone()
    return f"proj_{row['value']:03d}"


def _next_scoped_id(
    conn: sqlite3.Connection, kind: str, prefix: str, project_id: str
) -> str:
    conn.execute(
        "INSERT OR IGNORE INTO scoped_counters (project_id, kind, value) VALUES (?, ?, 0)",
        (project_id, kind),
    )
    conn.execute(
        "UPDATE scoped_counters SET value = value + 1 WHERE project_id = ? AND kind = ?",
        (project_id, kind),
    )
    row = conn.execute(
        "SELECT value FROM scoped_counters WHERE project_id = ? AND kind = ?",
        (project_id, kind),
    ).fetchone()
    assert row is not None
    return f"{prefix}{row['value']:03d}"


def next_fact_id(conn: sqlite3.Connection, project_id: str) -> str:
    return _next_scoped_id(conn, "fact", "f", project_id)


def next_intent_id(conn: sqlite3.Connection, project_id: str) -> str:
    return _next_scoped_id(conn, "intent", "i", project_id)


def next_hint_id(conn: sqlite3.Connection, project_id: str) -> str:
    return _next_scoped_id(conn, "hint", "h", project_id)


def get_project_or_404(conn: sqlite3.Connection, project_id: str) -> sqlite3.Row:
    row = conn.execute("SELECT * FROM projects WHERE id = ?", (project_id,)).fetchone()
    if row is None:
        raise HTTPException(404, "Project not found")
    return row


def check_project_active(conn: sqlite3.Connection, project_id: str) -> sqlite3.Row:
    row = get_project_or_404(conn, project_id)
    if row["status"] != "active":
        raise HTTPException(403, f"Project is {row['status']}")
    return row


def check_project_hint_writable(conn: sqlite3.Connection, project_id: str) -> sqlite3.Row:
    row = get_project_or_404(conn, project_id)
    if row["status"] not in ("active", "stopped", "completed"):
        raise HTTPException(403, f"Project is {row['status']}")
    return row


def check_project_completed(conn: sqlite3.Connection, project_id: str) -> sqlite3.Row:
    row = get_project_or_404(conn, project_id)
    if row["status"] != "completed":
        raise HTTPException(403, f"Project is {row['status']}")
    return row


def validate_facts_exist(
    conn: sqlite3.Connection, project_id: str, fact_ids: list[str]
) -> None:
    for fid in fact_ids:
        row = conn.execute(
            "SELECT 1 FROM facts WHERE id = ? AND project_id = ?", (fid, project_id)
        ).fetchone()
        if row is None:
            raise HTTPException(404, f"Fact {fid} not found")


def validate_goal_not_in_sources(fact_ids: list[str]) -> None:
    if "goal" in fact_ids:
        raise HTTPException(400, "goal cannot be used in from")


def validate_intent_creator_worker(creator: str, worker: str | None) -> None:
    if worker is not None and worker != creator:
        raise HTTPException(400, "worker must be null or equal to creator")


def get_intent_or_404(
    conn: sqlite3.Connection, project_id: str, intent_id: str
) -> sqlite3.Row:
    row = conn.execute(
        "SELECT * FROM intents WHERE id = ? AND project_id = ?",
        (intent_id, project_id),
    ).fetchone()
    if row is None:
        raise HTTPException(404, "Intent not found")
    return row


def get_claimable_open_intent_or_404(
    conn: sqlite3.Connection, project_id: str, intent_id: str, worker: str
) -> sqlite3.Row:
    expire_workers(conn, project_id)
    row = get_intent_or_404(conn, project_id, intent_id)
    if row["to_fact_id"] is not None:
        raise HTTPException(409, "Intent already concluded")
    if row["worker"] is not None and row["worker"] != worker:
        raise HTTPException(409, f"Intent is currently claimed by {row['worker']}")
    return row


def get_releasable_open_intent_or_404(
    conn: sqlite3.Connection, project_id: str, intent_id: str, worker: str
) -> sqlite3.Row:
    expire_workers(conn, project_id)
    row = get_intent_or_404(conn, project_id, intent_id)
    if row["to_fact_id"] is not None:
        raise HTTPException(409, "Intent already concluded")
    if row["worker"] is None:
        return row
    if row["worker"] != worker:
        raise HTTPException(409, f"Intent is currently claimed by {row['worker']}")
    return row


def get_completion_intent_or_409(conn: sqlite3.Connection, project_id: str) -> sqlite3.Row:
    rows = conn.execute(
        "SELECT * FROM intents WHERE project_id = ? AND to_fact_id = 'goal'",
        (project_id,),
    ).fetchall()
    if not rows:
        raise HTTPException(409, "Completed project is missing its completion intent")
    if len(rows) != 1:
        raise HTTPException(409, "Completed project has multiple completion intents")
    return rows[0]


def intent_to_model(conn: sqlite3.Connection, row: sqlite3.Row, project_id: str) -> Intent:
    sources = conn.execute(
        "SELECT fact_id FROM intent_sources WHERE intent_id = ? AND project_id = ? ORDER BY rowid",
        (row["id"], project_id),
    ).fetchall()
    return Intent(
        id=row["id"],
        **{"from": [s["fact_id"] for s in sources]},
        to=row["to_fact_id"],
        description=row["description"],
        creator=row["creator"],
        worker=row["worker"],
        last_heartbeat_at=row["last_heartbeat_at"],
        created_at=row["created_at"],
        concluded_at=row["concluded_at"],
    )


def build_intents(conn: sqlite3.Connection, project_id: str) -> list[Intent]:
    rows = conn.execute(
        "SELECT * FROM intents WHERE project_id = ? ORDER BY created_at",
        (project_id,),
    ).fetchall()
    return [intent_to_model(conn, r, project_id) for r in rows]


def get_intent_timeout(conn: sqlite3.Connection) -> int:
    row = conn.execute("SELECT intent_timeout FROM settings WHERE rowid = 1").fetchone()
    return row["intent_timeout"]


def get_reason_timeout(conn: sqlite3.Connection) -> int:
    row = conn.execute("SELECT reason_timeout FROM settings WHERE rowid = 1").fetchone()
    return row["reason_timeout"]


def project_reason_from_row(row: sqlite3.Row) -> ProjectReason | None:
    if row["reason_worker"] is None:
        return None
    return ProjectReason(
        worker=row["reason_worker"],
        trigger=row["reason_trigger"],
        started_at=row["reason_started_at"],
        last_heartbeat_at=row["reason_last_heartbeat_at"],
    )


def project_meta_from_row(row: sqlite3.Row) -> ProjectMeta:
    return ProjectMeta(
        id=row["id"],
        title=row["title"],
        status=row["status"],
        bootstrap_enabled=bool(row["bootstrap_enabled"]),
        created_at=row["created_at"],
        reason=project_reason_from_row(row),
    )


def clear_project_reason(conn: sqlite3.Connection, project_id: str) -> None:
    conn.execute(
        """
        UPDATE projects
        SET reason_worker = NULL,
            reason_trigger = NULL,
            reason_started_at = NULL,
            reason_last_heartbeat_at = NULL
        WHERE id = ?
        """,
        (project_id,),
    )


def expire_workers(conn: sqlite3.Connection, project_id: str | None = None) -> None:
    timeout = get_intent_timeout(conn)
    now = utcnow()
    query = """
        UPDATE intents
        SET worker = NULL
        WHERE to_fact_id IS NULL
          AND worker IS NOT NULL
          AND last_heartbeat_at IS NOT NULL
          AND (julianday(?) - julianday(last_heartbeat_at)) * 86400 > ?
    """
    params: tuple = (now, timeout)
    if project_id is not None:
        query = query.replace("WHERE ", "WHERE project_id = ? AND ", 1)
        params = (project_id, now, timeout)
    conn.execute(query, params)


def expire_reason_leases(conn: sqlite3.Connection, project_id: str | None = None) -> None:
    timeout = get_reason_timeout(conn)
    now = utcnow()
    query = """
        UPDATE projects
        SET reason_worker = NULL,
            reason_trigger = NULL,
            reason_started_at = NULL,
            reason_last_heartbeat_at = NULL
        WHERE reason_worker IS NOT NULL
          AND reason_last_heartbeat_at IS NOT NULL
          AND (julianday(?) - julianday(reason_last_heartbeat_at)) * 86400 > ?
    """
    params: tuple = (now, timeout)
    if project_id is not None:
        query = query.replace("WHERE ", "WHERE id = ? AND ", 1)
        params = (project_id, now, timeout)
    conn.execute(query, params)
