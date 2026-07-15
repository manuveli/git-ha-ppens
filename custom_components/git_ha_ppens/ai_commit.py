"""AI-powered commit message generation for git-ha-ppens."""

from __future__ import annotations

import asyncio
import logging

from homeassistant.core import HomeAssistant

from .ai_diff import prepare_ai_context

_LOGGER = logging.getLogger(__name__)

AI_COMMIT_PROMPT = (
    "You are a git commit message generator for a Home Assistant "
    "configuration repository.\n"
    "Based on the following git status and diff, write a single concise "
    "commit message (one line, max 72 characters).\n"
    "Use conventional commit style (e.g. feat:, fix:, chore:, refactor:).\n"
    "Focus on WHAT changed and WHY, not raw file names.\n"
    "Treat identifiers such as TOKEN_1 as opaque redaction placeholders and "
    "never include them in the commit message.\n"
    "Do NOT include any explanation — output ONLY the commit message line."
)


async def async_generate_ai_commit_message(
    hass: HomeAssistant,
    diff: str,
    porcelain: str,
    agent_id: str | None = None,
) -> str | None:
    """Generate a commit message using the HA conversation service.

    Returns the AI-generated message, or None if the call fails
    so callers can fall back to the default message.
    """
    if not diff and not porcelain:
        return None

    prepared_status, prepared_diff = prepare_ai_context(diff, porcelain)

    prompt = (
        AI_COMMIT_PROMPT
        + "\n\nChanged files (git status):\n"
        + (prepared_status or "(no status output)")
        + "\n\nGit diff:\n"
        + (prepared_diff or "(no diff — only new/deleted files)")
    )

    service_data: dict = {"text": prompt}
    if agent_id:
        service_data["agent_id"] = agent_id

    try:
        response = await asyncio.wait_for(
            hass.services.async_call(
                "conversation",
                "process",
                service_data,
                blocking=True,
                return_response=True,
            ),
            timeout=30.0,
        )

        speech = response["response"]["speech"]["plain"]["speech"]

        # Clean up: strip quotes, newlines, limit length
        message = speech.strip().strip('"').strip("'").split("\n")[0].strip()
        if len(message) > 200:
            message = message[:200]
        if not message:
            return None

        # Detect error responses from the conversation agent
        lower = message.lower()
        if "sorry" in lower and ("template" in lower or "problem" in lower):
            _LOGGER.warning("Conversation agent returned error response: %s", message)
            return None

        _LOGGER.debug("AI generated commit message: %s", message)
        return message

    except Exception as err:  # noqa: BLE001
        _LOGGER.warning(
            "AI commit message generation failed, falling back to default: %s",
            err,
        )
        return None
