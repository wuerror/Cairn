from fastapi import APIRouter, HTTPException
from fastapi.responses import JSONResponse, Response
from datetime import datetime
import yaml

from cairn.server.db import get_conn
from cairn.server.services import (
    current_codebase_version,
    effective_confidence,
    expire_reason_leases,
    expire_workers,
    get_origin_description,
    get_project_or_404,
    load_base_knowledge,
    parse_json_list,
    relevant_subgraph,
)

router = APIRouter(tags=["export"])


def format_export_timestamp(value: str | None) -> str | None:
    if not value:
        return value
    try:
        dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return value
    return dt.astimezone().strftime("%Y-%m-%d %H:%M:%S")


def _load_project_data(conn, project_id: str):
    expire_workers(conn, project_id)
    expire_reason_leases(conn, project_id)
    proj = get_project_or_404(conn, project_id)

    facts = conn.execute(
        "SELECT * FROM facts WHERE project_id = ?", (project_id,)
    ).fetchall()
    hints = conn.execute(
        "SELECT content, creator, created_at FROM hints WHERE project_id = ? ORDER BY created_at",
        (project_id,),
    ).fetchall()
    intents = conn.execute(
        "SELECT * FROM intents WHERE project_id = ? ORDER BY created_at",
        (project_id,),
    ).fetchall()

    sources_by_intent = {}
    for i in intents:
        rows = conn.execute(
            "SELECT fact_id FROM intent_sources WHERE intent_id = ? AND project_id = ? ORDER BY rowid",
            (i["id"], project_id),
        ).fetchall()
        sources_by_intent[i["id"]] = [r["fact_id"] for r in rows]

    return proj, facts, hints, intents, sources_by_intent


def _build_export_dict(
    conn,
    project_id: str,
    *,
    fact_ids: set[str] | None = None,
    intent_ids: set[str] | None = None,
    include_base_knowledge: bool = False,
) -> dict:
    proj, facts, hints, intents, sources_by_intent = _load_project_data(conn, project_id)
    current_cv = current_codebase_version(get_origin_description(conn, project_id))

    origin_desc = ""
    goal_desc = ""
    for f in facts:
        if f["id"] == "origin":
            origin_desc = f["description"]
        elif f["id"] == "goal":
            goal_desc = f["description"]

    if fact_ids is not None:
        facts = [f for f in facts if f["id"] in fact_ids]
    if intent_ids is not None:
        intents = [i for i in intents if i["id"] in intent_ids]

    data: dict = {
        "project": {
            "title": proj["title"],
            "origin": origin_desc,
            "goal": goal_desc,
            "bootstrap_enabled": bool(proj["bootstrap_enabled"]),
        }
    }

    if hints:
        data["hints"] = [
            {
                "content": h["content"],
                "creator": h["creator"],
                "created_at": format_export_timestamp(h["created_at"]),
            }
            for h in hints
        ]

    data["facts"] = []
    for f in facts:
        fact_entry: dict = {"id": f["id"], "description": f["description"]}
        if f["type"]:
            fact_entry["type"] = f["type"]
        locations = parse_json_list(f["locations"])
        if locations:
            fact_entry["locations"] = locations
        if f["confidence"]:
            fact_entry["confidence"] = f["confidence"]
        if f["code_version"]:
            fact_entry["code_version"] = f["code_version"]
        if f["verifies"]:
            fact_entry["verifies"] = f["verifies"]
        if f["batch_id"]:
            fact_entry["batch_id"] = f["batch_id"]
        if f["evidence"]:
            fact_entry["evidence"] = f["evidence"]
        eff_conf, stale = effective_confidence(
            conn, project_id,
            own_confidence=f["confidence"],
            own_code_version=f["code_version"],
            own_type=f["type"],
            fact_id=f["id"],
            current_code_version=current_cv,
        )
        if eff_conf:
            fact_entry["effective_confidence"] = eff_conf
        if stale:
            fact_entry["stale"] = True
        data["facts"].append(fact_entry)

    intent_list = []
    for i in intents:
        entry: dict = {
            "from": sources_by_intent.get(i["id"], []),
            "to": i["to_fact_id"],
            "description": i["description"],
            "creator": i["creator"],
            "worker": i["worker"],
            "created_at": format_export_timestamp(i["created_at"]),
            "concluded_at": format_export_timestamp(i["concluded_at"]),
        }
        intent_list.append(entry)

    if intent_list:
        data["intents"] = intent_list

    if include_base_knowledge:
        bk = load_base_knowledge(conn, project_id)
        if bk["version"] > 0 or bk["entries"] or bk["routing_map"]:
            data["base_knowledge"] = {
                "version": bk["version"],
                "entries": bk["entries"],
                "routing_map": bk["routing_map"],
            }

    return data


def _export_yaml(
    conn,
    project_id: str,
    *,
    fact_ids: set[str] | None = None,
    intent_ids: set[str] | None = None,
    include_base_knowledge: bool = False,
) -> str:
    data = _build_export_dict(
        conn,
        project_id,
        fact_ids=fact_ids,
        intent_ids=intent_ids,
        include_base_knowledge=include_base_knowledge,
    )
    return yaml.dump(data, allow_unicode=True, default_flow_style=False, sort_keys=False)


def _export_timeline(conn, project_id: str) -> str:
    proj, facts, hints, intents, sources_by_intent = _load_project_data(conn, project_id)

    facts_by_id = {f["id"]: f["description"] for f in facts}

    events: list[tuple[str, int, str]] = []  # (timestamp, order, text)
    order = 0

    origin_desc = facts_by_id.get("origin", "")
    goal_desc = facts_by_id.get("goal", "")
    ts = format_export_timestamp(proj["created_at"]) or ""
    block = f"[{ts}] PROJECT CREATED\n  origin: {origin_desc}\n  goal: {goal_desc}"
    events.append((proj["created_at"] or "", order, block))
    order += 1

    for h in hints:
        ts = format_export_timestamp(h["created_at"]) or ""
        block = f"[{ts}] HINT by {h['creator']}\n  {h['content']}"
        events.append((h["created_at"] or "", order, block))
        order += 1

    for i in intents:
        src = sources_by_intent.get(i["id"], [])
        from_str = ", ".join(src)

        ts = format_export_timestamp(i["created_at"]) or ""
        meta = f"  from: {from_str}"
        if i["worker"] and not i["concluded_at"]:
            meta += f"\n  worker: {i['worker']} (in progress)"
        block = f"[{ts}] INTENT DECLARED {i['id']} by {i['creator']}\n{meta}\n  {i['description']}"
        events.append((i["created_at"] or "", order, block))
        order += 1

        if not i["concluded_at"] or not i["to_fact_id"]:
            continue

        ts = format_export_timestamp(i["concluded_at"]) or ""
        actor = i["worker"] or i["creator"]

        if i["to_fact_id"] == "goal":
            block = f"[{ts}] PROJECT COMPLETED by {actor}\n  via: {i['id']} from {from_str}"
        else:
            fact_desc = facts_by_id.get(i["to_fact_id"], "")
            block = f"[{ts}] INTENT CONCLUDED {i['id']} by {actor}\n  from: {from_str}\n  produced: {i['to_fact_id']}\n  {fact_desc}"

        events.append((i["concluded_at"] or "", order, block))
        order += 1

    events.sort(key=lambda e: (e[0], e[1]))

    return "\n\n".join(e[2] for e in events) + "\n"


@router.get("/projects/{project_id}/export")
def export_project(project_id: str, format: str = "yaml"):
    if format not in ("yaml", "timeline"):
        raise HTTPException(400, "Supported formats: yaml, timeline")

    with get_conn() as conn:
        if format == "timeline":
            text = _export_timeline(conn, project_id)
        else:
            text = _export_yaml(conn, project_id, include_base_knowledge=True)

        return Response(content=text, media_type="text/plain")


@router.get("/projects/{project_id}/relevant_subgraph")
def export_relevant_subgraph(project_id: str, format: str = "yaml", max_hops: int = 8):
    if format not in ("yaml", "json"):
        raise HTTPException(400, "Supported formats: yaml, json")
    if max_hops < 1 or max_hops > 32:
        raise HTTPException(400, "max_hops must be between 1 and 32")

    with get_conn() as conn:
        get_project_or_404(conn, project_id)
        fact_ids, intent_ids = relevant_subgraph(conn, project_id, max_hops=max_hops)
        if format == "json":
            data = _build_export_dict(
                conn,
                project_id,
                fact_ids=fact_ids,
                intent_ids=intent_ids,
                include_base_knowledge=True,
            )
            data["fact_ids"] = sorted(fact_ids)
            data["intent_ids"] = sorted(intent_ids)
            return JSONResponse(content=data)
        text = _export_yaml(
            conn,
            project_id,
            fact_ids=fact_ids,
            intent_ids=intent_ids,
            include_base_knowledge=True,
        )
        return Response(content=text, media_type="text/plain")
