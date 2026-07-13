from __future__ import annotations

from typing import Any

from cairn.dispatcher.output_parser import extract_json_object


def parse_json_output(stdout: str) -> dict[str, Any]:
    return extract_json_object(stdout)


def _unwrap_wrapped_payload(payload: dict[str, Any]) -> tuple[bool | None, dict[str, Any] | None]:
    accepted = payload.get("accepted")
    if accepted is False:
        return False, None
    if accepted is True:
        data = payload.get("data")
        if not isinstance(data, dict):
            raise ValueError("data must be an object")
        return True, data
    return None, None


def _is_dict(value: Any) -> bool:
    return isinstance(value, dict)


def _looks_like_reason_data(payload: dict[str, Any]) -> bool:
    if not isinstance(payload, dict):
        return False
    keys = set(payload)
    if keys == {"complete"}:
        complete = payload["complete"]
        return isinstance(complete, dict) and "from" in complete and "description" in complete
    if keys == {"intents"}:
        return isinstance(payload["intents"], list)
    if keys == {"intent"}:
        intent = payload["intent"]
        return isinstance(intent, dict) and "from" in intent and "description" in intent
    return False


def _looks_like_bootstrap_execute_data(payload: dict[str, Any]) -> bool:
    if not isinstance(payload, dict):
        return False
    keys = set(payload)
    if keys not in ({"fact", "complete"}, {"fact", "complete", "base_knowledge"}):
        return False
    return _is_dict(payload.get("fact")) and _is_dict(payload.get("complete"))


def _looks_like_bootstrap_conclude_data(payload: dict[str, Any]) -> bool:
    if not isinstance(payload, dict):
        return False
    keys = set(payload)
    if keys not in ({"fact"}, {"fact", "complete"}, {"fact", "base_knowledge"}, {"fact", "complete", "base_knowledge"}):
        return False
    return _is_dict(payload.get("fact"))


def _normalize_base_knowledge(value: Any) -> dict[str, Any] | None:
    if value is None:
        return None
    if not isinstance(value, dict):
        raise ValueError("base_knowledge must be an object")
    entries = value.get("entries")
    if entries is None:
        entries = []
    if not isinstance(entries, list):
        raise ValueError("base_knowledge.entries must be an array")
    normalized_entries: list[dict[str, Any]] = []
    for i, entry in enumerate(entries):
        if not isinstance(entry, dict):
            raise ValueError(f"base_knowledge.entries[{i}] must be an object")
        entry_id = entry.get("id")
        kind = entry.get("kind")
        statement = entry.get("statement")
        if not isinstance(entry_id, str) or not entry_id.strip():
            raise ValueError(f"base_knowledge.entries[{i}].id is required")
        if not isinstance(kind, str) or not kind.strip():
            raise ValueError(f"base_knowledge.entries[{i}].kind is required")
        if not isinstance(statement, str) or not statement.strip():
            raise ValueError(f"base_knowledge.entries[{i}].statement is required")
        item: dict[str, Any] = {
            "id": entry_id.strip(),
            "kind": kind.strip(),
            "statement": statement.strip(),
            "confidence": entry.get("confidence") or "assumed",
            "evidence": entry.get("evidence") or [],
            "revised_by": entry.get("revised_by"),
        }
        if not isinstance(item["evidence"], list):
            raise ValueError(f"base_knowledge.entries[{i}].evidence must be an array")
        normalized_entries.append(item)

    routing_map = value.get("routing_map") or []
    if not isinstance(routing_map, list):
        raise ValueError("base_knowledge.routing_map must be an array")
    normalized_routes: list[dict[str, Any]] = []
    for i, route in enumerate(routing_map):
        if not isinstance(route, dict):
            raise ValueError(f"base_knowledge.routing_map[{i}] must be an object")
        src = route.get("src")
        live = route.get("live")
        if not isinstance(src, str) or not src.strip():
            raise ValueError(f"base_knowledge.routing_map[{i}].src is required")
        if not isinstance(live, str) or not live.strip():
            raise ValueError(f"base_knowledge.routing_map[{i}].live is required")
        normalized_routes.append({
            "src": src.strip(),
            "live": live.strip(),
            "via": route.get("via") or "direct",
            "confidence": route.get("confidence") or "assumed",
        })
    return {"entries": normalized_entries, "routing_map": normalized_routes}


def _looks_like_explore_data(payload: dict[str, Any]) -> bool:
    return isinstance(payload, dict) and set(payload) == {"description"}


def _looks_like_rich_explore_data(payload: dict[str, Any]) -> bool:
    if not isinstance(payload, dict):
        return False
    keys = set(payload)
    return keys == {"observations"} or keys == {"observations", "base_knowledge_patches"}


def _normalize_base_knowledge_patches(value: Any) -> list[dict[str, Any]]:
    if value is None:
        return []
    if not isinstance(value, list):
        raise ValueError("base_knowledge_patches must be an array")
    result: list[dict[str, Any]] = []
    for i, item in enumerate(value):
        if not isinstance(item, dict):
            raise ValueError(f"base_knowledge_patches[{i}] must be an object")
        entry_id = item.get("entry_id")
        if not isinstance(entry_id, str) or not entry_id.strip():
            raise ValueError(f"base_knowledge_patches[{i}].entry_id is required")
        normalized: dict[str, Any] = {"entry_id": entry_id.strip()}
        if "statement" in item and item["statement"] is not None:
            statement = item["statement"]
            if not isinstance(statement, str) or not statement.strip():
                raise ValueError(f"base_knowledge_patches[{i}].statement must be a non-empty string")
            normalized["statement"] = statement.strip()
        if "evidence" in item and item["evidence"] is not None:
            evidence = item["evidence"]
            if not isinstance(evidence, list) or not all(isinstance(e, str) for e in evidence):
                raise ValueError(f"base_knowledge_patches[{i}].evidence must be an array of strings")
            normalized["evidence"] = evidence
        if "confidence" in item and item["confidence"] is not None:
            confidence = item["confidence"]
            if confidence not in ("assumed", "code-confirmed"):
                raise ValueError(
                    f"base_knowledge_patches[{i}].confidence must be assumed or code-confirmed"
                )
            normalized["confidence"] = confidence
        # Model must not fill revised_by / version / live-confirmed
        result.append(normalized)
    return result


def validate_reason_payload(
    payload: dict[str, Any], open_intents_empty: bool, max_intents: int,
) -> tuple[str, dict[str, Any] | list[dict[str, Any]] | None]:
    accepted, data = _unwrap_wrapped_payload(payload)
    if accepted is False:
        return "rejected", None
    if accepted is None:
        if not _looks_like_reason_data(payload):
            raise ValueError("accepted must be true or false")
        data = payload
    if not isinstance(data, dict):
        raise ValueError("accepted must be true or false")
    complete = data.get("complete")
    intents = data.get("intents")
    # backward compat: accept singular "intent" key from LLMs
    if intents is None:
        singular = data.get("intent")
        if isinstance(singular, dict):
            intents = [singular]
    if complete is not None:
        if intents is not None:
            raise ValueError("complete and intents cannot coexist")
        if not isinstance(complete, dict) or "from" not in complete or "description" not in complete:
            raise ValueError("invalid complete payload")
        return "complete", complete
    if intents is not None:
        if not isinstance(intents, list):
            raise ValueError("intents must be an array")
        for i, intent in enumerate(intents):
            if not isinstance(intent, dict) or "from" not in intent or "description" not in intent:
                raise ValueError(f"invalid intent at index {i}")
        if not intents and open_intents_empty:
            raise ValueError("intents must not be empty when open_intents is empty")
        intents = intents[:max_intents]
        if not intents:
            return "noop", None
        return "intents", intents
    if open_intents_empty:
        raise ValueError("intents is required when open_intents is empty")
    return "noop", None


def validate_bootstrap_execute_payload(payload: dict[str, Any]) -> tuple[str, dict[str, Any] | None]:
    accepted, data = _unwrap_wrapped_payload(payload)
    if accepted is False:
        return "rejected", None
    if accepted is None:
        if not _looks_like_bootstrap_execute_data(payload):
            raise ValueError("accepted must be true or false")
        data = payload
    if not isinstance(data, dict):
        raise ValueError("accepted must be true or false")

    fact = data.get("fact")
    if not isinstance(fact, dict):
        raise ValueError("fact is required")
    fact_description = fact.get("description")
    if not isinstance(fact_description, str) or not fact_description.strip():
        raise ValueError("fact.description is required")

    result: dict[str, Any] = {"fact_description": fact_description.strip()}
    complete = data.get("complete")
    if complete is None:
        raise ValueError("complete is required")
    if not isinstance(complete, dict):
        raise ValueError("complete must be an object")
    complete_description = complete.get("description")
    if not isinstance(complete_description, str) or not complete_description.strip():
        raise ValueError("complete.description is required")
    result["complete_description"] = complete_description.strip()
    base_knowledge = _normalize_base_knowledge(data.get("base_knowledge"))
    if base_knowledge is not None:
        result["base_knowledge"] = base_knowledge
    return "complete", result


def validate_bootstrap_conclude_payload(payload: dict[str, Any]) -> tuple[str, dict[str, Any] | None]:
    accepted, data = _unwrap_wrapped_payload(payload)
    if accepted is False:
        return "rejected", None
    if accepted is None:
        if not _looks_like_bootstrap_conclude_data(payload):
            raise ValueError("accepted must be true or false")
        data = payload
    if not isinstance(data, dict):
        raise ValueError("accepted must be true or false")
    extra_keys = set(data) - {"fact", "complete", "base_knowledge"}
    if extra_keys:
        raise ValueError("unexpected keys in conclude payload")
    fact = data.get("fact")
    if not isinstance(fact, dict):
        raise ValueError("fact is required")
    fact_description = fact.get("description")
    if not isinstance(fact_description, str) or not fact_description.strip():
        raise ValueError("fact.description is required")
    result: dict[str, Any] = {"fact_description": fact_description.strip()}
    base_knowledge = _normalize_base_knowledge(data.get("base_knowledge"))
    if base_knowledge is not None:
        result["base_knowledge"] = base_knowledge
    return "fact", result


def validate_explore_payload(
    payload: dict[str, Any],
) -> tuple[str, dict[str, Any] | None]:
    """Return (kind, emit) where emit is {observations, base_knowledge_patches}."""
    accepted, data = _unwrap_wrapped_payload(payload)
    if accepted is False:
        return "rejected", None
    if accepted is None:
        if not _looks_like_explore_data(payload) and not _looks_like_rich_explore_data(payload):
            raise ValueError("accepted must be true or false")
        data = payload
    if not isinstance(data, dict):
        raise ValueError("accepted must be true or false")

    patches = _normalize_base_knowledge_patches(data.get("base_knowledge_patches"))

    if "observations" in data:
        observations = data["observations"]
        if not isinstance(observations, list) or len(observations) == 0:
            raise ValueError("observations must be a non-empty array")
        result: list[dict[str, Any]] = []
        for i, obs in enumerate(observations):
            if not isinstance(obs, dict):
                raise ValueError(f"observation at index {i} must be an object")
            description = obs.get("description")
            if not isinstance(description, str) or not description.strip():
                raise ValueError(f"observation[{i}].description is required")
            normalized: dict[str, Any] = {"description": description.strip()}
            obs_type = obs.get("type")
            if obs_type is not None:
                if not isinstance(obs_type, str) or not obs_type.strip():
                    raise ValueError(f"observation[{i}].type must be a non-empty string")
                normalized["type"] = obs_type.strip()
            locations = obs.get("locations")
            if locations is not None:
                if not isinstance(locations, list) or not all(isinstance(l, str) for l in locations):
                    raise ValueError(f"observation[{i}].locations must be an array of strings")
                normalized["locations"] = locations
            evidence = obs.get("evidence")
            if evidence is not None:
                if not isinstance(evidence, str) or not evidence.strip():
                    raise ValueError(f"observation[{i}].evidence must be a non-empty string")
                normalized["evidence"] = evidence.strip()
            oracle_draft = obs.get("oracle_draft")
            if oracle_draft is not None:
                if not isinstance(oracle_draft, str) or not oracle_draft.strip():
                    raise ValueError(f"observation[{i}].oracle_draft must be a non-empty string")
                normalized["oracle_draft"] = oracle_draft.strip()
            result.append(normalized)
        return "observations", {"observations": result, "base_knowledge_patches": patches}

    description = data.get("description")
    if not isinstance(description, str) or not description.strip():
        raise ValueError("description is required")
    return "fact", {
        "observations": [{"description": description.strip()}],
        "base_knowledge_patches": patches,
    }
