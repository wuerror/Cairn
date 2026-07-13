from __future__ import annotations

import json
import os
import re
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from typing import Any, Literal
from urllib.parse import urlparse


WhyFailedReason = Literal[
    "unreachable_route",
    "auth_blocked",
    "waf_blocked",
    "sanitized",
    "no_signal",
    "error",
    "allowlist_blocked",
    "fire_not_approved",
    "kill_switch",
]


@dataclass(slots=True)
class HarnessResult:
    triggered: bool
    evidence: str | None = None
    request: str | list[str] | None = None
    response: str | None = None
    why_failed: dict[str, Any] | None = None
    observed_routing: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "triggered": self.triggered,
            "evidence": self.evidence,
            "request": self.request,
            "response": self.response,
            "why_failed": self.why_failed,
            "observed_routing": self.observed_routing,
        }


@dataclass(slots=True)
class AllowlistFence:
    """Hard allowlist for outbound targets + OOB callbacks (decision #9)."""

    entries: list[str] = field(default_factory=list)

    def allows(self, target: str) -> bool:
        if not self.entries:
            # fail-closed: empty allowlist rejects all live targets
            return False
        normalized = _normalize_target(target)
        if not normalized:
            return False
        for entry in self.entries:
            if _match_allowlist_entry(normalized, entry.strip().lower()):
                return True
        return False

    def check_or_block(self, target: str) -> HarnessResult | None:
        if self.allows(target):
            return None
        return HarnessResult(
            triggered=False,
            evidence=None,
            request=f"BLOCKED target={target}",
            response=None,
            why_failed={
                "reason": "allowlist_blocked",
                "detail": f"target {target!r} not in origin.allowlist",
            },
        )


def extract_host_port(url_or_host: str) -> str:
    text = (url_or_host or "").strip()
    if not text:
        return ""
    if "://" not in text:
        return text.lower().split("/")[0]
    parsed = urlparse(text)
    host = (parsed.hostname or "").lower()
    if not host:
        return text.lower()
    if parsed.port:
        return f"{host}:{parsed.port}"
    return host


def _normalize_target(target: str) -> str:
    return extract_host_port(target)


def _match_allowlist_entry(normalized_target: str, entry: str) -> bool:
    entry = entry.strip().lower()
    if not entry:
        return False
    # entry may be host, host:port, or full URL
    entry_host = extract_host_port(entry)
    if not entry_host:
        return False
    if normalized_target == entry_host:
        return True
    # host-only entry matches any port on that host
    if ":" not in entry_host and normalized_target.split(":")[0] == entry_host:
        return True
    return False


def build_harness_script(
    *,
    base_url: str,
    allowlist: list[str],
    success_signature: dict[str, Any],
    payload_body: str,
    endpoint: str,
    method: str = "POST",
    headers: dict[str, str] | None = None,
    proxy_url: str | None = None,
) -> str:
    """Generate a Python harness script that enforces allowlist + proxy and returns harness_result JSON."""
    config = {
        "base_url": base_url,
        "allowlist": allowlist,
        "success_signature": success_signature,
        "payload_body": payload_body,
        "endpoint": endpoint,
        "method": method,
        "headers": headers or {},
        "proxy_url": proxy_url,
    }
    config_json = json.dumps(config, ensure_ascii=False)
    return f'''#!/usr/bin/env python3
import json, re, sys, urllib.error, urllib.request
from urllib.parse import urlparse

CFG = json.loads({config_json!r})

def host_port(url):
    if "://" not in url:
        return url.lower().split("/")[0]
    p = urlparse(url)
    h = (p.hostname or "").lower()
    if not h:
        return url.lower()
    return f"{{h}}:{{p.port}}" if p.port else h

def allowed(target, allowlist):
    if not allowlist:
        return False
    nt = host_port(target)
    for entry in allowlist:
        e = entry.strip().lower()
        eh = host_port(e) if "://" in e or ":" in e or "." in e else e
        if nt == eh:
            return True
        if ":" not in eh and nt.split(":")[0] == eh:
            return True
    return False

def result(**kwargs):
    print(json.dumps(kwargs, ensure_ascii=False))
    raise SystemExit(0)

base = CFG["base_url"].rstrip("/")
endpoint = CFG["endpoint"]
if endpoint.startswith("http"):
    url = endpoint
else:
    path = endpoint if endpoint.startswith("/") else "/" + endpoint.split(" ", 1)[-1] if " " in endpoint else "/" + endpoint
    if " " in endpoint:
        method, path = endpoint.split(" ", 1)
        CFG["method"] = method
    else:
        path = endpoint if endpoint.startswith("/") else "/" + endpoint
    url = base + path

if not allowed(url, CFG["allowlist"]):
    result(triggered=False, evidence=None, request=f"BLOCKED {{url}}", response=None,
           why_failed={{"reason":"allowlist_blocked","detail":f"target not in allowlist: {{url}}"}}, observed_routing=None)

headers = dict(CFG.get("headers") or {{}})
headers.setdefault("User-Agent", "cairn-verify-harness/1.0")
body = CFG.get("payload_body") or ""
data = body.encode("utf-8") if body else None
req = urllib.request.Request(url, data=data, headers=headers, method=CFG.get("method") or "POST")
handlers = []
if CFG.get("proxy_url"):
    handlers.append(urllib.request.ProxyHandler({{"http": CFG["proxy_url"], "https": CFG["proxy_url"]}}))
opener = urllib.request.build_opener(*handlers) if handlers else urllib.request.build_opener()
try:
    with opener.open(req, timeout=30) as resp:
        raw = resp.read().decode("utf-8", errors="replace")
        status = resp.status
except urllib.error.HTTPError as exc:
    raw = exc.read().decode("utf-8", errors="replace") if exc.fp else str(exc)
    status = exc.code
except Exception as exc:
    result(triggered=False, evidence=None, request=f"{{CFG.get('method')}} {{url}}\\n{{body}}",
           response=str(exc), why_failed={{"reason":"error","detail":str(exc)}}, observed_routing=None)

sig = CFG.get("success_signature") or {{}}
kind = sig.get("kind") or "response_match"
check = sig.get("check") or ""
triggered = False
evidence = None
if kind == "response_match" and check:
    try:
        triggered = re.search(check, raw) is not None or check in raw
    except re.error:
        triggered = check in raw
    if triggered:
        evidence = f"response matched check (status={{status}})"
elif kind == "side_effect":
    triggered = status < 500
    evidence = f"side_effect probe status={{status}}" if triggered else None
else:
    # oob/timing require external signal; harness alone cannot confirm
    triggered = False
    evidence = None

req_text = f"{{CFG.get('method')}} {{url}}\\n{{body}}"
if triggered:
    result(triggered=True, evidence=evidence, request=req_text, response=raw[:4000], why_failed=None, observed_routing=None)
result(triggered=False, evidence=None, request=req_text, response=raw[:4000],
       why_failed={{"reason":"no_signal","detail":f"status={{status}}; signature not met"}}, observed_routing=None)
'''


def harness_result_to_observations(
    result: dict[str, Any] | HarnessResult,
    *,
    verifies_fact_id: str,
    description_prefix: str = "verify",
) -> list[dict[str, Any]]:
    """Map harness_result → conclude observations (append-only verification + optional constraint)."""
    if isinstance(result, HarnessResult):
        data = result.to_dict()
    else:
        data = result
    triggered = bool(data.get("triggered"))
    evidence_parts = []
    if data.get("evidence"):
        evidence_parts.append(str(data["evidence"]))
    if data.get("request"):
        evidence_parts.append(f"request={data['request']}")
    if data.get("response"):
        evidence_parts.append(f"response={data['response']}")
    evidence = "\n".join(evidence_parts) if evidence_parts else None

    if triggered:
        return [
            {
                "type": "verification",
                "description": f"{description_prefix}: poc-confirmed",
                "confidence": "poc-confirmed",
                "verifies": verifies_fact_id,
                "evidence": evidence,
            }
        ]

    why = data.get("why_failed") or {"reason": "no_signal", "detail": "not triggered"}
    reason = why.get("reason") if isinstance(why, dict) else "no_signal"
    detail = why.get("detail") if isinstance(why, dict) else str(why)
    return [
        {
            "type": "verification",
            "description": f"{description_prefix}: refuted ({reason})",
            "confidence": "refuted",
            "verifies": verifies_fact_id,
            "evidence": evidence,
        },
        {
            "type": "constraint",
            "description": f"runtime negative: {reason} — {detail}",
            "locations": [],
            "evidence": evidence,
            "why_failed": why if isinstance(why, dict) else {"reason": str(reason), "detail": str(detail)},
        },
    ]


def select_verifies_target(chain: list[str], facts: list[dict[str, Any]] | None = None) -> str:
    """Pick terminal sink (or last chain id) as verification target."""
    if facts:
        by_id = {f.get("id"): f for f in facts if isinstance(f, dict)}
        for fid in reversed(chain):
            f = by_id.get(fid) or {}
            if f.get("type") == "sink":
                return fid
    if chain:
        return chain[-1]
    return ""


def parse_harness_stdout(stdout: str) -> dict[str, Any]:
    text = (stdout or "").strip()
    if not text:
        raise ValueError("empty harness output")
    # last JSON object wins
    for line in reversed(text.splitlines()):
        line = line.strip()
        if not line.startswith("{"):
            continue
        try:
            data = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(data, dict) and "triggered" in data:
            return data
    data = json.loads(text)
    if not isinstance(data, dict) or "triggered" not in data:
        raise ValueError("harness output missing triggered")
    return data


def resolve_request_url(base_url: str, endpoint: str) -> tuple[str, str]:
    """Return (method, absolute_url). Endpoint may be 'POST /path' or full URL."""
    method = "POST"
    ep = (endpoint or "").strip()
    if not ep:
        return method, (base_url or "").rstrip("/") + "/"
    if " " in ep and not ep.startswith("http"):
        maybe_method, rest = ep.split(" ", 1)
        if maybe_method.isalpha() and maybe_method.upper() == maybe_method:
            method = maybe_method.upper()
            ep = rest.strip()
    if ep.startswith("http://") or ep.startswith("https://"):
        return method, ep
    base = (base_url or "").rstrip("/")
    path = ep if ep.startswith("/") else f"/{ep}"
    if not base:
        return method, path
    return method, f"{base}{path}"


def execute_allowed_request(
    *,
    base_url: str,
    endpoint: str,
    allowlist: list[str],
    payload_body: str = "",
    method: str | None = None,
    headers: dict[str, str] | None = None,
    success_signature: dict[str, Any] | None = None,
    proxy_url: str | None = None,
    timeout_seconds: float = 30.0,
) -> HarnessResult:
    """Dispatcher-owned fire path: allowlist is mandatory; model never calls this.

    This is the hard fence (decision #8/#9): empty allowlist fails closed; off-list
    targets never open a socket.
    """
    fence = AllowlistFence(entries=list(allowlist or []))
    resolved_method, url = resolve_request_url(base_url, endpoint)
    if method:
        resolved_method = method.upper()

    blocked = fence.check_or_block(url)
    if blocked is not None:
        return blocked
    # also fence base_url host if endpoint is relative (already in url)
    if base_url:
        base_block = fence.check_or_block(base_url)
        if base_block is not None:
            return base_block

    hdrs = dict(headers or {})
    hdrs.setdefault("User-Agent", "cairn-verify-harness/1.0")
    body = payload_body or ""
    data = body.encode("utf-8") if body else None
    req = urllib.request.Request(url, data=data, headers=hdrs, method=resolved_method)
    handlers: list[Any] = []
    if proxy_url:
        handlers.append(urllib.request.ProxyHandler({"http": proxy_url, "https": proxy_url}))
    opener = urllib.request.build_opener(*handlers)

    req_text = f"{resolved_method} {url}\n{body}"
    try:
        with opener.open(req, timeout=timeout_seconds) as resp:
            raw = resp.read().decode("utf-8", errors="replace")
            status = getattr(resp, "status", 200)
    except urllib.error.HTTPError as exc:
        raw = exc.read().decode("utf-8", errors="replace") if exc.fp else str(exc)
        status = exc.code
    except Exception as exc:
        return HarnessResult(
            triggered=False,
            evidence=None,
            request=req_text,
            response=str(exc),
            why_failed={"reason": "error", "detail": str(exc)},
        )

    sig = success_signature or {}
    kind = sig.get("kind") or "response_match"
    check = sig.get("check") or ""
    triggered = False
    evidence = None
    if kind == "response_match" and check:
        try:
            triggered = re.search(check, raw) is not None or check in raw
        except re.error:
            triggered = check in raw
        if triggered:
            evidence = f"response matched check (status={status})"
    elif kind == "side_effect":
        triggered = int(status) < 500
        evidence = f"side_effect probe status={status}" if triggered else None
    else:
        # oob/timing require external signal; harness alone cannot confirm success
        triggered = False
        evidence = None

    if triggered:
        return HarnessResult(
            triggered=True,
            evidence=evidence,
            request=req_text,
            response=raw[:4000],
            why_failed=None,
            observed_routing=url,
        )
    return HarnessResult(
        triggered=False,
        evidence=None,
        request=req_text,
        response=raw[:4000],
        why_failed={"reason": "no_signal", "detail": f"status={status}; signature not met"},
        observed_routing=url,
    )


def resolve_credentials_ref(credentials_ref: str | None) -> dict[str, str]:
    """Resolve credentials_ref into env inject map. Supports:
    - secret:NAME → env CAIRN_SECRET_NAME or CAIRN_SECRET_<NAME>
    - env:VAR → os.environ[VAR]
    Never returns the ref string itself as a secret value.
    """
    if not credentials_ref or not credentials_ref.strip():
        return {}
    ref = credentials_ref.strip()
    if ref.startswith("env:"):
        var = ref[4:].strip()
        val = os.environ.get(var)
        return {var: val} if val is not None else {}
    if ref.startswith("secret:"):
        name = ref[7:].strip()
        for key in (f"CAIRN_SECRET_{name}", f"CAIRN_SECRET_{name.upper()}", name):
            val = os.environ.get(key)
            if val is not None:
                return {"CAIRN_TARGET_CREDENTIAL": val, "CAIRN_CREDENTIALS_REF": ref}
        return {"CAIRN_CREDENTIALS_REF": ref}
    # unknown scheme: pass ref only, not a secret body
    return {"CAIRN_CREDENTIALS_REF": ref}
