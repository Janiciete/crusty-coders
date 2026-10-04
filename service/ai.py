"""P17/F5-simple (Stretch, xAI track): "Describe what matters" -> suggested
chips. See CLAUDE.md §7/§11 and §2 ("AI (optional, Stretch): xAI API over
httpx; everything must work with XAI_API_KEY unset").

Privacy (non-negotiable, per the team decision in this prompt): the user's
free-text sentence is sent to xAI only when they press Suggest. It is never
stored, never logged, and this is the only module/UI surface allowed to say
"AI". The system prompt also tells the model never to name medical
conditions in its reply, and the reply is validated against an allow-list of
chip ids/levels before use -- xAI never computes routes.

Endpoint: POST /preferences/parse  body: {"text": "..."} (1-300 chars)
Success: {"priorities": {chip_id: level}} (only valid pairs kept)
Failure (no key / HTTP error / timeout / nothing valid): 503
  {"error": "Suggestions aren't available right now. Choose below."}
"""

from __future__ import annotations

import os

import httpx
from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

router = APIRouter()

# Allow-list (CLAUDE.md / docs/API.md priority chips) -- the xAI reply is
# validated against exactly these; anything else is dropped, never trusted.
VALID_CHIPS = {"avoid_stairs", "avoid_steep", "curb_cuts", "accessible_entrance", "well_lit"}
VALID_LEVELS = {"essential", "important", "nice"}

XAI_URL = "https://api.x.ai/v1/responses"
XAI_TIMEOUT_S = 8.0

UNAVAILABLE = {"error": "Suggestions aren't available right now. Choose below."}

SYSTEM_PROMPT = (
    "You convert a short sentence describing someone's walking/accessibility "
    "needs into a JSON object only -- no prose, no markdown fences. "
    "Reply with exactly: {\"priorities\": {<chip>: <level>}}. "
    "<chip> must be one of: avoid_stairs, avoid_steep, curb_cuts, "
    "accessible_entrance, well_lit. <level> must be one of: essential, "
    "important, nice. Only include chips the sentence actually implies; omit "
    "the rest. Never name or infer a specific medical condition, diagnosis, "
    "or disability label anywhere in your reply -- describe needs only in "
    "terms of the listed chips."
)


class PreferencesParseRequest(BaseModel):
    text: str = Field(min_length=1, max_length=300)


def _unavailable() -> HTTPException:
    return HTTPException(status_code=503, detail=UNAVAILABLE)


def _extract_json_text(data: dict) -> str | None:
    """Pull the model's text reply out of a /v1/responses-shaped payload.
    Tries the documented `output[].content[].text` (type "output_text")
    path first, then falls back to a couple of other common shapes so a
    minor API version difference doesn't take the whole feature down.
    """
    try:
        for item in data.get("output", []):
            for c in item.get("content", []):
                if c.get("type") == "output_text" and c.get("text"):
                    return c["text"]
    except (AttributeError, TypeError):
        pass

    # Fallbacks seen on OpenAI-compatible-style responses, just in case.
    if isinstance(data.get("output_text"), str):
        return data["output_text"]
    try:
        return data["choices"][0]["message"]["content"]
    except (KeyError, IndexError, TypeError):
        return None


def _parse_priorities(raw_text: str) -> dict:
    """Validate the model's reply against the chip/level allow-list. Any
    parse failure or empty result yields {}; the caller turns that into a
    503."""
    import json

    text = raw_text.strip()
    # Tolerate a stray ```json ... ``` fence even though we ask for none.
    if text.startswith("```"):
        text = text.strip("`")
        if text.lower().startswith("json"):
            text = text[4:]
        text = text.strip()

    try:
        parsed = json.loads(text)
    except (ValueError, TypeError):
        return {}

    if not isinstance(parsed, dict):
        return {}
    raw_priorities = parsed.get("priorities")
    if not isinstance(raw_priorities, dict):
        return {}

    out = {}
    for chip, level in raw_priorities.items():
        if chip in VALID_CHIPS and level in VALID_LEVELS:
            out[chip] = level
    return out


@router.post("/preferences/parse")
async def parse_preferences(req: PreferencesParseRequest):
    api_key = os.getenv("XAI_API_KEY")
    model = os.getenv("XAI_MODEL")
    if not api_key or not model:
        raise _unavailable()

    body = {
        "model": model,
        "instructions": SYSTEM_PROMPT,
        "input": req.text,
    }

    try:
        async with httpx.AsyncClient(timeout=XAI_TIMEOUT_S) as client:
            resp = await client.post(
                XAI_URL,
                headers={
                    "Authorization": f"Bearer {api_key}",
                    "Content-Type": "application/json",
                },
                json=body,
            )
        resp.raise_for_status()
        data = resp.json()
    except (httpx.HTTPError, ValueError):
        # Network error, timeout, non-2xx, or bad JSON -- never log req.text
        # or the reply (CLAUDE.md §7/§11).
        raise _unavailable()

    reply_text = _extract_json_text(data)
    if not reply_text:
        raise _unavailable()

    priorities = _parse_priorities(reply_text)
    if not priorities:
        raise _unavailable()

    return {"priorities": priorities}
