from __future__ import annotations

import json
import logging
import time
from typing import Any

from cairn.dispatcher.config import DispatchConfig, WorkerConfig
from cairn.dispatcher.harness import (
    AllowlistFence,
    execute_allowed_request,
    harness_result_to_observations,
    resolve_credentials_ref,
    select_verifies_target,
)
from cairn.dispatcher.protocol.client import CairnClient
from cairn.dispatcher.runtime.cancellation import TaskCancellation
from cairn.dispatcher.runtime.containers import ContainerManager
from cairn.dispatcher.runtime.heartbeat import HeartbeatLease
from cairn.dispatcher.tasks.common import (
    best_effort_release,
    write_conclude_result_with_observations,
)
from cairn.server.models import Intent, ProjectDetail

LOG = logging.getLogger(__name__)


def run_verify_task(
    config: DispatchConfig,
    client: CairnClient,
    container_manager: ContainerManager,
    project: ProjectDetail,
    export_yaml: str,
    intent: Intent,
    worker: WorkerConfig,
    cancellation: TaskCancellation,
) -> str:
    """Verify task: dispatcher-owned harness fire path (decision #8/#9/#11).

    Model does not open sockets. Multi-round iteration adjusts payload only;
    each round goes through execute_allowed_request (allowlist hard fence).
    """
    _ = export_yaml  # graph context available for future payload instantiation
    task_started = time.perf_counter()
    lease = HeartbeatLease.for_intent(client, project.project.id, intent.id, worker.name, config.runtime.interval)
    lease.start()
    verify_container: str | None = None
    try:
        if client.get_verify_control(project.project.id).get("kill_requested") or cancellation.is_cancelled:
            best_effort_release(client, project.project.id, intent.id, worker.name)
            return "killed"

        require_approval = config.tasks.verify.require_fire_approval
        if require_approval and intent.fire_status not in ("approved", "fired"):
            # should be filtered pre-claim; still fail-closed
            best_effort_release(client, project.project.id, intent.id, worker.name)
            return "awaiting_approval"

        brief = _brief_dict(intent)
        origin_desc = next((f.description for f in project.facts if f.id == "origin"), None)
        allowlist = _origin_allowlist(origin_desc)
        base_url = _origin_base_url(origin_desc) or ""
        endpoint = _brief_endpoint(brief)
        fence = AllowlistFence(entries=allowlist)

        # Pre-fire hard block (no container, no socket)
        for target in filter(None, [endpoint, base_url]):
            blocked = fence.check_or_block(target)
            if blocked is not None and not fence.allows(target):
                # if endpoint is relative path, only base_url matters
                if target == endpoint and not endpoint.startswith("http"):
                    continue
                client.record_proxy_traffic(
                    project.project.id,
                    intent_id=intent.id,
                    request=str(blocked.request),
                    status="blocked",
                )
                return _conclude_harness(
                    client, project, intent, worker, brief, blocked, source="verify_allowlist", phase_ms=0
                )

        codebase_path = _origin_codebase_path(origin_desc)
        creds_env = resolve_credentials_ref(_origin_credentials_ref(origin_desc))
        # verify profile: short-lived, credentials only here, codebase RO
        verify_container = container_manager.ensure_running(
            project.project.id,
            profile="verify",
            codebase_host_path=codebase_path,
            extra_env=creds_env,
        )

        chain = list(brief.get("chain") or intent.from_)
        verifies = select_verifies_target(chain, [f.model_dump() for f in project.facts]) or (
            chain[-1] if chain else "goal"
        )
        success_sig = brief.get("success_signature") or {}
        if not isinstance(success_sig, dict):
            success_sig = {}
        payload = _initial_payload(brief, project)
        headers = _auth_headers(creds_env)
        max_rounds = config.tasks.verify.max_rounds
        proxy_url = config.tasks.verify.proxy_url
        last_result = None
        round_evidence: list[str] = []
        model_instantiate_used = False

        for round_idx in range(max_rounds):
            if client.get_verify_control(project.project.id).get("kill_requested") or cancellation.is_cancelled:
                best_effort_release(client, project.project.id, intent.id, worker.name)
                return "killed"

            # Audit rail: record intent-to-fire BEFORE opening socket
            planned = f"ROUND {round_idx + 1}/{max_rounds}\npayload_body={payload[:800]}"
            client.record_proxy_traffic(
                project.project.id,
                intent_id=intent.id,
                request=f"PLANNED {endpoint}\n{planned}",
                status="recorded",
            )

            if not config.tasks.verify.force_harness:
                # legacy escape hatch only — still allowlist pre-checked above
                LOG.warning("force_harness=false is non-compliant; still using dispatcher harness")

            last_result = execute_allowed_request(
                base_url=base_url,
                endpoint=endpoint,
                allowlist=allowlist,
                payload_body=payload,
                headers=headers,
                success_signature=success_sig,
                proxy_url=proxy_url,
            )
            client.record_proxy_traffic(
                project.project.id,
                intent_id=intent.id,
                request=str(last_result.request or f"POST {endpoint}\n{payload}"),
                response=str(last_result.response or "") or None,
                status="blocked" if (last_result.why_failed or {}).get("reason") == "allowlist_blocked" else "recorded",
            )
            round_evidence.append(
                f"round={round_idx + 1} triggered={last_result.triggered} "
                f"why={(last_result.why_failed or {}).get('reason')} "
                f"payload_body={payload[:200]}"
            )
            if last_result.triggered:
                break
            if (last_result.why_failed or {}).get("reason") == "allowlist_blocked":
                break
            # multi-round: local template tweak, then optional one-shot restricted model instantiate
            if round_idx + 1 < max_rounds:
                why_reason = (last_result.why_failed or {}).get("reason")
                if (
                    why_reason == "no_signal"
                    and config.tasks.verify.allow_model_instantiate
                    and not model_instantiate_used
                ):
                    instantiated = _restricted_model_instantiate(brief, last_result, codebase_path)
                    model_instantiate_used = True
                    if instantiated:
                        body, extra_headers = instantiated
                        payload = body
                        if extra_headers:
                            headers = {**headers, **extra_headers}
                        continue
                payload = _adjust_payload(payload, last_result, round_idx, brief)

        assert last_result is not None
        if last_result.evidence:
            last_result.evidence = last_result.evidence + "\n" + "\n".join(round_evidence)
        else:
            last_result.evidence = "\n".join(round_evidence)

        # observed_routing → routing_map patch
        if last_result.observed_routing:
            _patch_observed_routing(client, project.project.id, brief, last_result.observed_routing, verifies)

        phase_ms = int((time.perf_counter() - task_started) * 1000)
        return _conclude_harness(
            client,
            project,
            intent,
            worker,
            brief,
            last_result,
            source="verify_harness",
            phase_ms=phase_ms,
            verifies=verifies,
        )
    finally:
        lease.stop()
        if verify_container:
            try:
                container_manager.remove_container(verify_container, force=True)
            except Exception as exc:
                LOG.warning("failed to remove verify container=%s error=%s", verify_container, exc)


def _conclude_harness(
    client: CairnClient,
    project: ProjectDetail,
    intent: Intent,
    worker: WorkerConfig,
    brief: dict[str, Any],
    result: Any,
    *,
    source: str,
    phase_ms: int,
    verifies: str | None = None,
) -> str:
    chain = list(brief.get("chain") or intent.from_)
    target = verifies or select_verifies_target(chain, [f.model_dump() for f in project.facts]) or (
        chain[-1] if chain else "goal"
    )
    observations = harness_result_to_observations(
        result, verifies_fact_id=target, description_prefix=f"verify {intent.id}"
    )
    status = write_conclude_result_with_observations(
        client,
        project.project.id,
        intent.id,
        worker.name,
        observations,
        source=source,
        phase_ms=phase_ms,
    )
    return status


def _patch_observed_routing(
    client: CairnClient,
    project_id: str,
    brief: dict[str, Any],
    observed: str,
    verifies: str,
) -> None:
    try:
        bk = client.get_base_knowledge(project_id)
    except Exception:
        return
    routes = list(bk.get("routing_map") or [])
    # attach observed live path for first chain location if any
    src = ""
    for loc in _chain_locations_from_brief(brief):
        src = loc
        break
    if not src:
        src = f"verify:{verifies}"
    # skip if already present
    for r in routes:
        if r.get("live") == observed or r.get("src") == src:
            return
    routes.append(
        {
            "src": src,
            "live": observed if " " not in observed else observed.split()[-1],
            "via": "direct",
            "confidence": "live-confirmed",
        }
    )
    client.put_base_knowledge(
        project_id,
        entries=list(bk.get("entries") or []),
        routing_map=routes,
        expected_version=bk.get("version"),
        actor="verify.harness",
    )


def _chain_locations_from_brief(brief: dict[str, Any]) -> list[str]:
    dataflow = brief.get("dataflow") or ""
    # locations appear as file:line in dataflow text
    out: list[str] = []
    for part in dataflow.replace("→", " ").split():
        if ":" in part and "/" in part or (".py:" in part):
            out.append(part.strip(" ,;"))
    return out


def _brief_dict(intent: Intent) -> dict[str, Any]:
    brief = intent.poc_brief if isinstance(intent.poc_brief, dict) else {}
    if hasattr(intent.poc_brief, "model_dump"):
        brief = intent.poc_brief.model_dump()  # type: ignore[union-attr]
    return brief or {}


def _brief_endpoint(brief: dict[str, Any]) -> str:
    entry = brief.get("entry") or {}
    if isinstance(entry, dict):
        return str(entry.get("endpoint") or "")
    return ""


def _initial_payload(brief: dict[str, Any], project: ProjectDetail | None = None) -> str:
    """Build fire body from Brief shape/gadget, then demo template fallback.

    shape is already preferred from chain payload_draft by assemble_poc_brief.
    """
    recipe = brief.get("payload_recipe") or {}
    shape = ""
    gadget = ""
    if isinstance(recipe, dict):
        shape = str(recipe.get("shape") or "").strip()
        gadget = str(recipe.get("gadget") or "").strip()
    # Prefer concrete body when shape looks like payload (not long prose)
    if shape and _looks_like_payload_body(shape):
        return shape
    if shape and not _looks_like_prose(shape):
        return shape
    templated = _template_payload(brief, project, shape=shape, gadget=gadget)
    if templated:
        return templated
    if shape:
        return shape
    if gadget:
        return gadget
    return "CAIRN_POC_OK"


def _looks_like_payload_body(text: str) -> bool:
    lowered = text.lower()
    markers = (
        "!!python",
        "cairn_poc_ok",
        "config=",
        "{",
        "application/",
        "yaml",
        "<?xml",
        "base64",
        "payload",
        "=",
    )
    if any(m in lowered for m in markers) and len(text) < 4000:
        # exclude pure "description of yaml.load..." prose without body markers beyond words
        if " " in text and text.count(" ") > 12 and "=" not in text and "!!" not in text:
            return False
        return True
    return False


def _looks_like_prose(text: str) -> bool:
    if len(text) > 280 and text.count(" ") > 20:
        return True
    return text.lower().startswith("verify ") or " | " in text


def _template_payload(
    brief: dict[str, Any],
    project: ProjectDetail | None,
    *,
    shape: str,
    gadget: str,
) -> str | None:
    """Minimal sink-keyword template table for demo / no-LLM verify."""
    blob_parts = [shape, gadget, str(brief.get("dataflow") or ""), str(brief.get("success_signature") or "")]
    if project is not None:
        for fact in project.facts:
            if fact.id in (brief.get("chain") or []):
                blob_parts.append(fact.description or "")
                if fact.payload_draft:
                    return fact.payload_draft
    blob = " ".join(blob_parts).lower()
    success = brief.get("success_signature") or {}
    check = ""
    if isinstance(success, dict):
        check = str(success.get("check") or "")
    marker = check if check and " " not in check.strip() else "CAIRN_POC_OK"
    if "yaml" in blob or "!!python" in blob or "deserialize" in blob:
        return f"config=!!python/object/apply:os.system ['echo {marker}']\n"
    if "ssti" in blob or "jinja" in blob or "template" in blob:
        return f"name={{{{{marker}}}}}"
    if "rce" in blob or "exec" in blob or "command" in blob:
        return f"cmd=echo {marker}"
    if marker and marker != "CAIRN_POC_OK":
        return marker
    # last-resort demo probe that many lab targets accept
    if "import" in blob or "/api/" in blob:
        return f"config={marker}"
    return None


def _adjust_payload(payload: str, result: Any, round_idx: int, brief: dict[str, Any] | None = None) -> str:
    why = (result.why_failed or {}) if hasattr(result, "why_failed") else {}
    reason = why.get("reason") if isinstance(why, dict) else ""
    # deterministic local mutation — not model-driven network
    if reason == "sanitized":
        return payload.replace("!!python", "!!str") + f"\n#round{round_idx + 1}"
    if reason == "auth_blocked":
        return payload
    if reason == "no_signal":
        # escalate to demo templates
        templated = _template_payload(brief or {}, None, shape=payload, gadget="")
        if templated and templated != payload:
            return templated
        if "CAIRN_POC_OK" not in payload:
            return f"{payload}\nCAIRN_POC_OK"
    return f"{payload}\n#retry{round_idx + 1}"


def _restricted_model_instantiate(
    brief: dict[str, Any],
    last_result: Any,
    codebase_path: str | None,
) -> tuple[str, dict[str, str]] | None:
    """Optional one-round payload instantiation. Does not open sockets.

    v1: deterministic template re-instantiation from Brief + why_failed.
    (Full worker-driver call can plug in later behind the same return shape.)
    """
    why = getattr(last_result, "why_failed", None) or {}
    detail = why.get("detail") if isinstance(why, dict) else ""
    _ = codebase_path  # reserved for future model context
    recipe = brief.get("payload_recipe") or {}
    shape = str((recipe or {}).get("shape") or "")
    gadget = str((recipe or {}).get("gadget") or "")
    body = _template_payload(brief, None, shape=shape or str(detail or ""), gadget=gadget)
    if not body:
        return None
    return body, {"Content-Type": "application/x-www-form-urlencoded"}


def _auth_headers(creds_env: dict[str, str]) -> dict[str, str]:
    headers: dict[str, str] = {"Content-Type": "application/x-www-form-urlencoded"}
    token = creds_env.get("CAIRN_TARGET_CREDENTIAL")
    if token:
        headers["Authorization"] = f"Bearer {token}"
    return headers


def _origin_allowlist(origin_description: str | None) -> list[str]:
    origin = _parse_origin(origin_description)
    raw = (origin or {}).get("allowlist") or []
    if not isinstance(raw, list):
        return []
    return [str(x).strip() for x in raw if str(x).strip()]


def _origin_base_url(origin_description: str | None) -> str | None:
    origin = _parse_origin(origin_description)
    target = (origin or {}).get("target") or {}
    if isinstance(target, dict):
        base = target.get("base_url")
        if isinstance(base, str) and base.strip():
            return base.strip()
    return None


def _origin_codebase_path(origin_description: str | None) -> str | None:
    origin = _parse_origin(origin_description)
    cb = (origin or {}).get("codebase") or {}
    if isinstance(cb, dict):
        path = cb.get("path")
        if isinstance(path, str) and path.strip():
            return path.strip()
    return None


def _origin_credentials_ref(origin_description: str | None) -> str | None:
    origin = _parse_origin(origin_description)
    target = (origin or {}).get("target") or {}
    if isinstance(target, dict):
        ref = target.get("credentials_ref")
        if isinstance(ref, str) and ref.strip():
            return ref.strip()
    return None


def _parse_origin(origin_description: str | None) -> dict | None:
    if not origin_description:
        return None
    try:
        parsed = json.loads(origin_description)
    except (json.JSONDecodeError, TypeError):
        return None
    return parsed if isinstance(parsed, dict) else None
