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

# goal → sink description keywords (v1 small map + keyword fallback)
GOAL_SINK_KEYWORDS: dict[str, list[str]] = {
    "rce": ["exec", "deserialize", "ssti", "template", "command", "rce", "eval", "pickle", "yaml.load", "os.system", "subprocess"],
    "sqli": ["sql", "query", "injection", "sqli", "execute", "cursor"],
    "xss": ["xss", "html", "innerhtml", "render", "template", "escape"],
    "ssrf": ["ssrf", "request", "urlopen", "fetch", "http", "proxy"],
    "lfi": ["path", "file", "read", "include", "lfi", "traversal", "open("],
    "auth": ["auth", "bypass", "login", "session", "permission", "authorize"],
    "idor": ["idor", "authorization", "object", "access control", "horizontal"],
    "deserialize": ["deserialize", "pickle", "yaml", "unserialize", "objectinput"],
}

DEFAULT_SUBGRAPH_HOPS = 8


def utcnow() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def parse_origin_json(origin_description: str | None) -> dict | None:
    if not origin_description:
        return None
    try:
        parsed = json.loads(origin_description)
    except (json.JSONDecodeError, TypeError):
        return None
    return parsed if isinstance(parsed, dict) else None


def codebase_path_from_origin(origin_description: str | None) -> str | None:
    origin = parse_origin_json(origin_description)
    if not origin:
        return None
    path = (origin.get("codebase") or {}).get("path")
    if isinstance(path, str) and path.strip():
        return path.strip()
    return None


def current_codebase_version(origin_description: str | None) -> str | None:
    origin = parse_origin_json(origin_description)
    if not origin:
        return None
    commit = (origin.get("codebase") or {}).get("commit")
    if commit:
        return str(commit)
    return None


def compute_code_version(locations: list[str] | None, origin_description: str | None = None) -> str:
    # Prefer origin commit so cross-run anti-corruption has a stable stamp.
    commit = current_codebase_version(origin_description)
    if commit:
        return commit
    if locations:
        payload = json.dumps(sorted(locations), sort_keys=True)
    elif origin_description:
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
    current_code_version: str | None = None,
) -> tuple[ConfidenceLevel | None, bool]:
    """Fold verification facts into effective confidence.

    stale is verification_stale only: true when a verification existed but is
    expired (code_version mismatch). Unverified static nodes never go stale
    solely because their own code_version differs from the current commit.
    """
    if fact_id is None:
        return own_confidence, False
    if own_type == "verification":
        stale = bool(
            current_code_version
            and own_code_version
            and own_code_version != current_code_version
        )
        return own_confidence, stale

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
    v_cv = latest["code_version"]
    if current_code_version and v_cv and v_cv != current_code_version:
        return own_confidence, True
    if latest["code_version"] and own_code_version and latest["code_version"] != own_code_version:
        return own_confidence, True
    if v_conf == "refuted":
        return "refuted", False
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


def get_origin_description(conn: sqlite3.Connection, project_id: str) -> str | None:
    row = conn.execute(
        "SELECT description FROM facts WHERE project_id = ? AND id = 'origin'",
        (project_id,),
    ).fetchone()
    return row["description"] if row else None


def fact_from_row(row: sqlite3.Row, conn: sqlite3.Connection | None = None, project_id: str | None = None) -> Fact:
    eff_conf = None
    stale = False
    if conn is not None and project_id is not None and row["type"] is not None:
        current_cv = current_codebase_version(get_origin_description(conn, project_id))
        eff_conf, stale = effective_confidence(
            conn, project_id,
            own_confidence=row["confidence"],
            own_code_version=row["code_version"],
            own_type=row["type"],
            fact_id=row["id"],
            current_code_version=current_cv,
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


def goal_sink_keywords(goal_text: str) -> list[str]:
    text = (goal_text or "").lower()
    keywords: list[str] = []
    for key, words in GOAL_SINK_KEYWORDS.items():
        if key in text or any(w in text for w in words[:3]):
            keywords.extend(words)
    if not keywords:
        # keyword fallback: use non-stop tokens from goal itself
        tokens = [t for t in text.replace("/", " ").replace("-", " ").split() if len(t) > 2]
        keywords = tokens
    # de-dupe preserving order
    seen: set[str] = set()
    result: list[str] = []
    for k in keywords:
        if k not in seen:
            seen.add(k)
            result.append(k)
    return result


def match_goal_sinks(
    facts: list[sqlite3.Row],
    goal_text: str,
) -> list[sqlite3.Row]:
    keywords = goal_sink_keywords(goal_text)
    sinks = [f for f in facts if f["type"] == "sink"]
    if not sinks:
        return []
    if not keywords:
        return sinks
    matched = []
    for sink in sinks:
        desc = (sink["description"] or "").lower()
        locs = (sink["locations"] or "").lower()
        blob = f"{desc} {locs}"
        if any(k in blob for k in keywords):
            matched.append(sink)
    return matched


def relevant_subgraph(
    conn: sqlite3.Connection,
    project_id: str,
    goal_text: str | None = None,
    max_hops: int = DEFAULT_SUBGRAPH_HOPS,
) -> tuple[set[str], set[str]]:
    """Return (fact_ids, intent_ids) for the goal-relevant subgraph.

    Walk Intent provenance (from[] → to) reverse from goal-matched sinks,
    drop paths whose terminal sink is effectively refuted, then attach
    same-batch satellite facts.
    """
    facts = conn.execute(
        "SELECT * FROM facts WHERE project_id = ?", (project_id,)
    ).fetchall()
    if not facts:
        return set(), set()

    if goal_text is None:
        goal_row = next((f for f in facts if f["id"] == "goal"), None)
        goal_text = goal_row["description"] if goal_row else ""

    current_cv = current_codebase_version(get_origin_description(conn, project_id))
    fact_by_id = {f["id"]: f for f in facts}

    matched_sinks = match_goal_sinks(facts, goal_text)
    all_typed = [f for f in facts if f["type"] is not None]
    if not matched_sinks:
        # No typed facts yet → full graph (P0-compatible empty graph).
        if not all_typed:
            return {f["id"] for f in facts}, {
                r["id"]
                for r in conn.execute(
                    "SELECT id FROM intents WHERE project_id = ?", (project_id,)
                ).fetchall()
            }
        # Typed facts exist but no goal-matched sinks: fail-closed.
        # Do NOT seed all sinks (would break multi-goal filtering after cross-run import).
        selected = {"origin", "goal"}
        for f in facts:
            if f["type"] == "constraint":
                selected.add(f["id"])
        open_intents = conn.execute(
            "SELECT * FROM intents WHERE project_id = ? AND to_fact_id IS NULL",
            (project_id,),
        ).fetchall()
        selected_intents: set[str] = set()
        for intent in open_intents:
            sources = conn.execute(
                "SELECT fact_id FROM intent_sources WHERE intent_id = ? AND project_id = ?",
                (intent["id"], project_id),
            ).fetchall()
            from_ids = [s["fact_id"] for s in sources]
            if any(fid in selected for fid in from_ids) or not from_ids:
                selected_intents.add(intent["id"])
                selected.update(from_ids)
        return selected, selected_intents

    # Filter refuted sinks out of seeds
    seed_ids: list[str] = []
    for sink in matched_sinks:
        eff, _ = effective_confidence(
            conn, project_id,
            own_confidence=sink["confidence"],
            own_code_version=sink["code_version"],
            own_type=sink["type"],
            fact_id=sink["id"],
            current_code_version=current_cv,
        )
        if eff == "refuted":
            continue
        seed_ids.append(sink["id"])

    if not seed_ids:
        # All matched sinks refuted — still include origin/goal + constraints
        selected = {"origin", "goal"}
        for f in facts:
            if f["type"] == "constraint":
                selected.add(f["id"])
        return selected, set()

    # Build reverse adjacency: to_fact_id → list of (intent_id, from_fact_ids)
    intents = conn.execute(
        "SELECT * FROM intents WHERE project_id = ? AND to_fact_id IS NOT NULL",
        (project_id,),
    ).fetchall()
    reverse: dict[str, list[tuple[str, list[str]]]] = {}
    for intent in intents:
        sources = conn.execute(
            "SELECT fact_id FROM intent_sources WHERE intent_id = ? AND project_id = ? ORDER BY rowid",
            (intent["id"], project_id),
        ).fetchall()
        from_ids = [s["fact_id"] for s in sources]
        reverse.setdefault(intent["to_fact_id"], []).append((intent["id"], from_ids))

    selected_facts: set[str] = set(seed_ids)
    selected_intents: set[str] = set()
    frontier = list(seed_ids)
    for _ in range(max_hops):
        next_frontier: list[str] = []
        for node in frontier:
            for intent_id, from_ids in reverse.get(node, []):
                selected_intents.add(intent_id)
                for fid in from_ids:
                    if fid not in selected_facts:
                        # Skip if this node is a refuted sink (shouldn't seed further)
                        row = fact_by_id.get(fid)
                        if row is not None and row["type"] == "sink":
                            eff, _ = effective_confidence(
                                conn, project_id,
                                own_confidence=row["confidence"],
                                own_code_version=row["code_version"],
                                own_type=row["type"],
                                fact_id=row["id"],
                                current_code_version=current_cv,
                            )
                            if eff == "refuted":
                                continue
                        selected_facts.add(fid)
                        next_frontier.append(fid)
        frontier = next_frontier
        if not frontier:
            break

    # Attach same-batch satellites (source/constraint not on Intent spine)
    batch_ids = {
        fact_by_id[fid]["batch_id"]
        for fid in selected_facts
        if fid in fact_by_id and fact_by_id[fid]["batch_id"]
    }
    for f in facts:
        if f["batch_id"] and f["batch_id"] in batch_ids:
            selected_facts.add(f["id"])

    # Always keep origin + goal; attach verifications for selected nodes
    selected_facts.add("origin")
    selected_facts.add("goal")
    for f in facts:
        if f["type"] == "verification" and f["verifies"] in selected_facts:
            selected_facts.add(f["id"])

    # Open intents that touch selected facts (for reason context)
    open_intents = conn.execute(
        "SELECT * FROM intents WHERE project_id = ? AND to_fact_id IS NULL",
        (project_id,),
    ).fetchall()
    for intent in open_intents:
        sources = conn.execute(
            "SELECT fact_id FROM intent_sources WHERE intent_id = ? AND project_id = ?",
            (intent["id"], project_id),
        ).fetchall()
        from_ids = [s["fact_id"] for s in sources]
        if any(fid in selected_facts for fid in from_ids):
            selected_intents.add(intent["id"])
            selected_facts.update(from_ids)

    return selected_facts, selected_intents


def list_codebase_sibling_projects(
    conn: sqlite3.Connection,
    project_id: str,
    origin_description: str,
) -> list[str]:
    path = codebase_path_from_origin(origin_description)
    if not path:
        return []
    rows = conn.execute(
        """SELECT f.project_id, f.description, p.created_at
           FROM facts f
           JOIN projects p ON p.id = f.project_id
           WHERE f.id = 'origin' AND f.project_id != ?
           ORDER BY p.created_at DESC""",
        (project_id,),
    ).fetchall()
    return [
        row["project_id"]
        for row in rows
        if codebase_path_from_origin(row["description"]) == path
    ]


def import_codebase_facts(
    conn: sqlite3.Connection,
    target_project_id: str,
    origin_description: str,
) -> int:
    """Import durable facts (+ concluded intent spine) from sibling projects sharing codebase.path.

    Returns number of facts imported. goal-tag is the target project's own goal;
    imported facts are goal-agnostic capabilities.
    """
    siblings = list_codebase_sibling_projects(conn, target_project_id, origin_description)
    if not siblings:
        return 0

    # Prefer newest sibling that already has typed facts
    source_project_id = None
    for sibling_id in siblings:
        count = conn.execute(
            "SELECT COUNT(*) AS c FROM facts WHERE project_id = ? AND type IS NOT NULL",
            (sibling_id,),
        ).fetchone()["c"]
        if count > 0:
            source_project_id = sibling_id
            break
    if source_project_id is None:
        return 0

    source_facts = conn.execute(
        "SELECT * FROM facts WHERE project_id = ? AND id NOT IN ('origin', 'goal')",
        (source_project_id,),
    ).fetchall()
    if not source_facts:
        return 0

    id_map: dict[str, str] = {}
    imported = 0
    for row in source_facts:
        # Cross-run hard dedup by canonical key
        existing = find_existing_fact(
            conn, target_project_id, row["type"], parse_json_list(row["locations"])
        )
        if existing is not None:
            id_map[row["id"]] = existing["id"]
            continue
        new_id = next_fact_id(conn, target_project_id)
        id_map[row["id"]] = new_id
        conn.execute(
            """INSERT INTO facts (id, project_id, description, type, confidence,
               locations, code_version, evidence, verifies, intent_id, batch_id)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                new_id,
                target_project_id,
                row["description"],
                row["type"],
                row["confidence"],
                row["locations"],
                row["code_version"],
                row["evidence"],
                None,  # remapped below for verification
                None,
                row["batch_id"],
            ),
        )
        imported += 1

    # Second pass: fix verifies pointers for verification facts
    for row in source_facts:
        if row["type"] != "verification" or not row["verifies"]:
            continue
        new_id = id_map.get(row["id"])
        target_verifies = id_map.get(row["verifies"])
        if new_id and target_verifies:
            conn.execute(
                "UPDATE facts SET verifies = ? WHERE id = ? AND project_id = ?",
                (target_verifies, new_id, target_project_id),
            )

    # Copy concluded intents whose to/from are all mapped (preserve provenance spine)
    source_intents = conn.execute(
        "SELECT * FROM intents WHERE project_id = ? AND to_fact_id IS NOT NULL AND to_fact_id != 'goal'",
        (source_project_id,),
    ).fetchall()
    for intent in source_intents:
        to_id = intent["to_fact_id"]
        if to_id not in id_map:
            continue
        sources = conn.execute(
            "SELECT fact_id FROM intent_sources WHERE intent_id = ? AND project_id = ? ORDER BY rowid",
            (intent["id"], source_project_id),
        ).fetchall()
        from_ids = [s["fact_id"] for s in sources]
        if not from_ids or any(fid not in id_map and fid not in RESERVED_FACT_IDS for fid in from_ids):
            continue
        new_intent_id = next_intent_id(conn, target_project_id)
        mapped_from = [
            id_map[fid] if fid in id_map else fid for fid in from_ids
        ]
        now = utcnow()
        conn.execute(
            """INSERT INTO intents (id, project_id, to_fact_id, description, creator, worker,
               last_heartbeat_at, created_at, concluded_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                new_intent_id,
                target_project_id,
                id_map[to_id],
                intent["description"],
                intent["creator"],
                intent["worker"] or intent["creator"],
                now,
                now,
                now,
            ),
        )
        for fid in mapped_from:
            conn.execute(
                "INSERT INTO intent_sources (intent_id, project_id, fact_id) VALUES (?, ?, ?)",
                (new_intent_id, target_project_id, fid),
            )
        # stamp intent_id on main fact if empty
        conn.execute(
            "UPDATE facts SET intent_id = COALESCE(intent_id, ?) WHERE id = ? AND project_id = ?",
            (new_intent_id, id_map[to_id], target_project_id),
        )

    # Import base_knowledge if target empty and source has it
    target_bk = conn.execute(
        "SELECT 1 FROM base_knowledge WHERE project_id = ?", (target_project_id,)
    ).fetchone()
    if target_bk is None:
        source_bk = conn.execute(
            "SELECT version, data FROM base_knowledge WHERE project_id = ?",
            (source_project_id,),
        ).fetchone()
        if source_bk is not None:
            conn.execute(
                "INSERT INTO base_knowledge (project_id, version, data, updated_at) VALUES (?, ?, ?, ?)",
                (target_project_id, source_bk["version"], source_bk["data"], utcnow()),
            )

    return imported


def empty_base_knowledge() -> dict:
    return {"version": 0, "entries": [], "routing_map": [], "audit": []}


def load_base_knowledge(conn: sqlite3.Connection, project_id: str) -> dict:
    row = conn.execute(
        "SELECT version, data FROM base_knowledge WHERE project_id = ?",
        (project_id,),
    ).fetchone()
    if row is None:
        return empty_base_knowledge()
    try:
        data = json.loads(row["data"])
    except (json.JSONDecodeError, TypeError):
        data = {}
    if not isinstance(data, dict):
        data = {}
    return {
        "version": row["version"],
        "entries": data.get("entries") or [],
        "routing_map": data.get("routing_map") or [],
        "audit": data.get("audit") or [],
    }


def save_base_knowledge(
    conn: sqlite3.Connection,
    project_id: str,
    *,
    entries: list[dict],
    routing_map: list[dict],
    audit: list[dict],
    expected_version: int | None = None,
    actor: str = "system",
) -> dict:
    current = load_base_knowledge(conn, project_id)
    if expected_version is not None and current["version"] != expected_version:
        raise HTTPException(
            409,
            f"base_knowledge version conflict: expected {expected_version}, current {current['version']}",
        )
    new_version = current["version"] + 1
    payload = {
        "entries": entries,
        "routing_map": routing_map,
        "audit": audit,
    }
    now = utcnow()
    conn.execute(
        """INSERT INTO base_knowledge (project_id, version, data, updated_at)
           VALUES (?, ?, ?, ?)
           ON CONFLICT(project_id) DO UPDATE SET
             version = excluded.version,
             data = excluded.data,
             updated_at = excluded.updated_at""",
        (project_id, new_version, json.dumps(payload, ensure_ascii=False), now),
    )
    return {
        "version": new_version,
        "entries": entries,
        "routing_map": routing_map,
        "audit": audit,
        "updated_at": now,
        "actor": actor,
    }


def patch_base_knowledge_entry(
    conn: sqlite3.Connection,
    project_id: str,
    entry_id: str,
    *,
    statement: str | None = None,
    evidence: list[str] | None = None,
    confidence: str | None = None,
    revised_by: str,
    actor: str,
    expected_version: int | None = None,
) -> dict:
    """Patch a single entry; revised_by (conflicting fact id) is required for audit."""
    if not revised_by or not revised_by.strip():
        raise HTTPException(400, "revised_by fact id is required for base_knowledge patch")
    validate_facts_exist(conn, project_id, [revised_by])

    current = load_base_knowledge(conn, project_id)
    entries = list(current["entries"])
    found = False
    for i, entry in enumerate(entries):
        if entry.get("id") != entry_id:
            continue
        updated = dict(entry)
        if statement is not None:
            updated["statement"] = statement
        if evidence is not None:
            updated["evidence"] = evidence
        if confidence is not None:
            updated["confidence"] = confidence
        updated["revised_by"] = revised_by
        entries[i] = updated
        found = True
        break
    if not found:
        raise HTTPException(404, f"base_knowledge entry {entry_id} not found")

    audit = list(current["audit"])
    audit.append({
        "entry_id": entry_id,
        "revised_by": revised_by,
        "actor": actor,
        "action": "patch",
        "at": utcnow(),
    })
    return save_base_knowledge(
        conn,
        project_id,
        entries=entries,
        routing_map=list(current["routing_map"]),
        audit=audit,
        expected_version=expected_version,
        actor=actor,
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
