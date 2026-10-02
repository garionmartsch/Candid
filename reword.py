"""
Rewording engine: takes a raw piece of feedback and produces a
depersonalized version — same substance, but with identifying
language, personal tone, and emotional charge stripped out so the
recipient can't easily guess who wrote it and the message stays
constructive.

If ANTHROPIC_API_KEY is set in the environment, this calls Claude to do
the rewording properly. Otherwise it falls back to a simple heuristic
rewriter so the app is fully usable out of the box, and it flags which
path was used (`source`) so the UI can be honest about it.
"""

import os
import re

SYSTEM_PROMPT = """You rewrite workplace feedback so it cannot be traced back to whoever \
wrote it, while preserving its full meaning and actionable substance.

Rules:
- Keep every concrete point, example, and suggestion. Do not soften away the substance.
- Remove first-person voice, distinctive phrasing, inside references, names of other \
people or teams not essential to the point, timestamps, and anything else that could \
identify the author.
- Remove insults, profanity, or personal attacks; keep the underlying concern if there is one.
- Write in a neutral, professional, constructive tone, addressed to the recipient as "you" \
where natural (e.g. "In the last two sprint planning meetings, ..." rather than "I noticed you...").
- Do not add new claims, judgments, or exaggerate. Do not add a greeting or sign-off.
- Output ONLY the reworded feedback text, nothing else.
"""


def _heuristic_reword(text: str, category: str) -> str:
    """A no-API-key fallback. Does light, mechanical depersonalization:
    strips first-person framing and identifying punctuation/emphasis.
    This is NOT a substitute for the AI path — it's a safety net."""
    cleaned = text.strip()

    # Collapse first-person openers like "I think", "I feel", "In my opinion"
    first_person_patterns = [
        r"^\s*i\s+(?:really\s+)?(?:think|feel|believe|noticed|found)\s+that\s+",
        r"^\s*i\s+(?:really\s+)?(?:think|feel|believe|noticed|found)\s+",
        r"^\s*in\s+my\s+opinion,?\s+",
        r"^\s*imo,?\s+",
        r"^\s*personally,?\s+",
    ]
    for pattern in first_person_patterns:
        cleaned = re.sub(pattern, "", cleaned, flags=re.IGNORECASE)

    # Remove excessive punctuation/emphasis that can carry a "voice"
    cleaned = re.sub(r"!{2,}", "!", cleaned)
    cleaned = re.sub(r"\?{2,}", "?", cleaned)

    if cleaned:
        cleaned = cleaned[0].upper() + cleaned[1:]
    if cleaned and not cleaned.endswith((".", "!", "?")):
        cleaned += "."

    label = "Complaint" if category == "complaint" else "Suggestion"
    return f"{label}: {cleaned}"


def _ai_reword(text: str, category: str) -> str:
    import requests

    label = "complaint" if category == "complaint" else "suggestion"
    response = requests.post(
        "https://api.anthropic.com/v1/messages",
        headers={
            "x-api-key": os.environ["ANTHROPIC_API_KEY"],
            "anthropic-version": "2023-06-01",
            "content-type": "application/json",
        },
        json={
            "model": os.environ.get("ANTHROPIC_MODEL", "claude-sonnet-4-5"),
            "max_tokens": 500,
            "system": SYSTEM_PROMPT,
            "messages": [
                {
                    "role": "user",
                    "content": f"Category: {label}\n\nRaw feedback:\n{text.strip()}",
                }
            ],
        },
        timeout=30,
    )
    response.raise_for_status()
    data = response.json()
    parts = [block["text"] for block in data.get("content", []) if block.get("type") == "text"]
    result = "\n".join(parts).strip()
    return result or _heuristic_reword(text, category)


def reword_feedback(text: str, category: str) -> tuple[str, str]:
    """Returns (reworded_text, source) where source is 'ai' or 'heuristic'."""
    if os.environ.get("ANTHROPIC_API_KEY"):
        try:
            return _ai_reword(text, category), "ai"
        except Exception:
            # Fall through to heuristic rather than blocking submission.
            pass
    return _heuristic_reword(text, category), "heuristic"
