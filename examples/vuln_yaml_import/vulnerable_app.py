"""Sample source tree for static explore (intentionally unsafe patterns).

This file is what explore workers should read under /workspace/codebase.
The live target is app.py (marker/oracle mode); this module documents the
vulnerable pattern for fact locations like `vulnerable_app.py:18`.
"""

from __future__ import annotations

# Simulated Flask-style import endpoint (not executed by the demo server).


def import_config(raw_yaml: str):
    """POST /api/import — unauthenticated; unsafe yaml.load sink."""
    import yaml  # noqa: F401

    # Intentionally unsafe for static analysis demos:
    return yaml.load(raw_yaml, Loader=yaml.Loader)  # sink: unsafe deserialization


def register_routes(app):
    # No @login_required — source: unauthenticated entry
    @app.route("/api/import", methods=["POST"])
    def handle_import():
        body = app.request.get_data(as_text=True)
        return import_config(body)
