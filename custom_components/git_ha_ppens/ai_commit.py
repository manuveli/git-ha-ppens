"""AI-powered commit message generation for git-ha-ppens."""

from __future__ import annotations

import asyncio
import logging

from homeassistant.core import HomeAssistant

from .ai_diff import prepare_ai_context

_LOGGER = logging.getLogger(__name__)

_SUCCESS_RESPONSE_TYPES = frozenset(
    {"action_done", "partial_action_done", "query_answer"}
)
_MAX_COMMIT_MESSAGE_LENGTH = 200

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


def _normalize_commit_message(speech: object) -> str | None:
    """Normalize agent speech to the legacy single-line message format."""
    if not isinstance(speech, str):
        return None

    message = speech.strip().strip('"').strip("'").split("\n")[0].strip()
    if not message:
        return None

    return message[:_MAX_COMMIT_MESSAGE_LENGTH]


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

    prepared_status, prepared_diff = await hass.async_add_executor_job(
        prepare_ai_context, diff, porcelain
    )

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

        response_payload = response["response"]
        if not isinstance(response_payload, dict):
            _LOGGER.warning(
                "Conversation agent returned a malformed response, "
                "falling back to default"
            )
            return None

        response_type = response_payload.get("response_type")
        if response_type == "error":
            error_data = response_payload.get("data")
            error_code = (
                error_data.get("code")
                if isinstance(error_data, dict)
                and isinstance(error_data.get("code"), str)
                else "unknown"
            )
            _LOGGER.warning(
                "Conversation agent returned an error (code: %s), "
                "falling back to default",
                error_code,
            )
            return None

        if response_type not in _SUCCESS_RESPONSE_TYPES:
            _LOGGER.warning(
                "Conversation agent returned a missing or unsupported response type, "
                "falling back to default"
            )
            return None

        speech_payload = response_payload.get("speech")
        plain_speech = (
            speech_payload.get("plain")
            if isinstance(speech_payload, dict)
            else None
        )
        speech = (
            plain_speech.get("speech")
            if isinstance(plain_speech, dict)
            else None
        )
        message = _normalize_commit_message(speech)
        if message is None:
            _LOGGER.warning(
                "Conversation agent returned an invalid commit message, "
                "falling back to default"
            )
            return None

        _LOGGER.debug("AI generated commit message: %s", message)
        return message

    except Exception as err:  # noqa: BLE001
        _LOGGER.warning(
            "AI commit message generation failed (%s), falling back to default",
            type(err).__name__,
        )
        return None
