"""Tests for service/ai.py (P17/F5-simple, Stretch xAI track).

httpx is mocked throughout -- these tests never make a real network call to
xAI. See CLAUDE.md §7/§11: the endpoint must work fully with XAI_API_KEY
unset, and only ever return the validated {chip: level} allow-list, never
raw model output.
"""

from __future__ import annotations

import json

import httpx
import pytest
from fastapi.testclient import TestClient

from service import ai as ai_module
from service.app import app

client = TestClient(app)


def _responses_payload(reply_text: str) -> dict:
    """Shape of a real /v1/responses reply (output[].content[].text)."""
    return {
        "output": [
            {
                "type": "message",
                "content": [{"type": "output_text", "text": reply_text}],
            }
        ]
    }


class _FakeResponse:
    def __init__(self, payload: dict, status_code: int = 200):
        self._payload = payload
        self.status_code = status_code

    def raise_for_status(self):
        if self.status_code >= 400:
            raise httpx.HTTPStatusError("error", request=None, response=self)

    def json(self):
        return self._payload


class _FakeAsyncClient:
    def __init__(self, response: _FakeResponse):
        self._response = response

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def post(self, *args, **kwargs):
        return self._response


def test_valid_reply_maps_to_priorities(monkeypatch):
    monkeypatch.setenv("XAI_API_KEY", "fake-key")
    monkeypatch.setenv("XAI_MODEL", "grok-4.7")

    reply = json.dumps({"priorities": {"avoid_steep": "essential", "well_lit": "important"}})
    fake_response = _FakeResponse(_responses_payload(reply))
    monkeypatch.setattr(
        ai_module.httpx, "AsyncClient", lambda *a, **k: _FakeAsyncClient(fake_response)
    )

    res = client.post("/preferences/parse", json={"text": "Hills are hard and I need lit paths"})
    assert res.status_code == 200
    assert res.json() == {"priorities": {"avoid_steep": "essential", "well_lit": "important"}}


def test_invalid_chips_and_levels_are_dropped(monkeypatch):
    monkeypatch.setenv("XAI_API_KEY", "fake-key")
    monkeypatch.setenv("XAI_MODEL", "grok-4.7")

    # "diabetes" isn't an allowed chip, "mild" isn't an allowed level, and
    # the whole "diabetes" entry should be dropped even though the chip
    # name itself leaked through the model's reply (never trusted raw).
    reply = json.dumps(
        {
            "priorities": {
                "avoid_stairs": "essential",
                "diabetes": "essential",
                "well_lit": "mild",
                "curb_cuts": "nice",
            }
        }
    )
    fake_response = _FakeResponse(_responses_payload(reply))
    monkeypatch.setattr(
        ai_module.httpx, "AsyncClient", lambda *a, **k: _FakeAsyncClient(fake_response)
    )

    res = client.post("/preferences/parse", json={"text": "some text"})
    assert res.status_code == 200
    assert res.json() == {"priorities": {"avoid_stairs": "essential", "curb_cuts": "nice"}}


def test_no_key_returns_503(monkeypatch):
    monkeypatch.delenv("XAI_API_KEY", raising=False)
    monkeypatch.delenv("XAI_MODEL", raising=False)

    res = client.post("/preferences/parse", json={"text": "anything"})
    assert res.status_code == 503
    assert res.json()["detail"] == {
        "error": "Suggestions aren't available right now. Choose below."
    }


def test_timeout_returns_503(monkeypatch):
    monkeypatch.setenv("XAI_API_KEY", "fake-key")
    monkeypatch.setenv("XAI_MODEL", "grok-4.7")

    class _TimeoutClient:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return False

        async def post(self, *args, **kwargs):
            raise httpx.TimeoutException("timed out")

    monkeypatch.setattr(ai_module.httpx, "AsyncClient", lambda *a, **k: _TimeoutClient())

    res = client.post("/preferences/parse", json={"text": "anything"})
    assert res.status_code == 503


def test_empty_text_is_422():
    res = client.post("/preferences/parse", json={"text": ""})
    assert res.status_code == 422


def test_text_over_300_chars_is_422():
    res = client.post("/preferences/parse", json={"text": "x" * 301})
    assert res.status_code == 422
