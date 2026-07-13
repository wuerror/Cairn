from __future__ import annotations

import json

import pytest

from cairn.dispatcher.contracts import (
    parse_json_output,
    validate_explore_payload,
    validate_reason_payload,
)
from cairn.dispatcher.runtime.process import ManagedProcess
from cairn.dispatcher.workers.adapters.pi import PiDriver


def test_parse_json_output_extracts_object_from_markdown_noise() -> None:
    assert parse_json_output('result:\n```json\n{"accepted": true, "data": {}}\n```') == {
        "accepted": True,
        "data": {},
    }


def test_reason_payload_limits_number_of_intents() -> None:
    kind, intents = validate_reason_payload(
        {
            "accepted": True,
            "data": {
                "intents": [
                    {"from": ["f001"], "description": "one"},
                    {"from": ["f001"], "description": "two"},
                ]
            },
        },
        open_intents_empty=True,
        max_intents=1,
    )

    assert kind == "intents"
    assert intents == [{"from": ["f001"], "description": "one"}]


def test_reason_payload_requires_intent_when_none_are_open() -> None:
    with pytest.raises(ValueError, match="intents is required"):
        validate_reason_payload(
            {"accepted": True, "data": {}},
            open_intents_empty=True,
            max_intents=3,
        )


def test_explore_payload_rejects_planning_text() -> None:
    with pytest.raises(ValueError):
        validate_explore_payload(parse_json_output("Need inspect files and keep working."))


def test_explore_payload_accepts_base_knowledge_patches() -> None:
    kind, emit = validate_explore_payload(
        {
            "accepted": True,
            "data": {
                "observations": [
                    {
                        "type": "constraint",
                        "description": "import skips auth",
                        "locations": ["a.py:1"],
                    }
                ],
                "base_knowledge_patches": [
                    {
                        "entry_id": "bk001",
                        "statement": "auth is per-route with exception",
                        "confidence": "code-confirmed",
                    }
                ],
            },
        }
    )
    assert kind == "observations"
    assert emit is not None
    assert len(emit["observations"]) == 1
    assert emit["base_knowledge_patches"][0]["entry_id"] == "bk001"


def test_explore_payload_rejects_live_confirmed_patch() -> None:
    with pytest.raises(ValueError, match="assumed or code-confirmed"):
        validate_explore_payload(
            {
                "accepted": True,
                "data": {
                    "observations": [{"description": "x"}],
                    "base_knowledge_patches": [
                        {"entry_id": "bk001", "confidence": "live-confirmed"}
                    ],
                },
            }
        )


def test_pi_driver_extracts_session_and_last_assistant_text() -> None:
    driver = PiDriver()
    stdout = "\n".join(
        [
            json.dumps({"type": "session", "id": "session-123"}),
            json.dumps(
                {
                    "type": "turn_end",
                    "message": {
                        "role": "assistant",
                        "content": [{"type": "text", "text": '{"accepted":true,"data":{}}'}],
                    },
                }
            ),
        ]
    )

    assert driver.extract_session(None, stdout, "") == "session-123"
    assert driver.extract_response_text(stdout, "") == '{"accepted":true,"data":{}}'


def test_close_stream_closes_response_even_when_stream_close_fails() -> None:
    class Response:
        def __init__(self) -> None:
            self.closed = False

        def close(self) -> None:
            self.closed = True

    class Stream:
        def __init__(self) -> None:
            self._response = Response()

        def close(self) -> None:
            raise ValueError("already closed")

    stream = Stream()
    ManagedProcess._close_stream(stream)

    assert stream._response.closed

