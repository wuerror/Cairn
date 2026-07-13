# Task
Conclude the verification attempt for intent {intent_id}. Summarize the final harness outcome only — no new exploration.

# PoC Brief
```
{poc_brief}
```

# Intent description
{intent_description}

# Graph
```
{graph_yaml}
```

# Output
Return only one raw JSON object with harness_result (and verifies = terminal sink fact id):

```json
{"accepted": true, "data": {"harness_result": {"triggered": false, "evidence": null, "request": "...", "response": "...", "why_failed": {"reason": "no_signal", "detail": "..."}, "observed_routing": null}, "verifies": "f001"}}
```
