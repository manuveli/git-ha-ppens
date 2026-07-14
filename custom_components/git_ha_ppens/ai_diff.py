"""Prepare bounded, privacy-conscious git context for AI commit messages."""

from __future__ import annotations

from dataclasses import dataclass
import re

from .const import DEFAULT_AI_DIFF_MAX_CHARS, DEFAULT_AI_STATUS_MAX_CHARS

_REDACTED = "[REDACTED]"
_OMITTED = "[...content omitted...]"
_MAX_RENDERED_LINE_CHARS = 640
_LINE_CONTEXT_CHARS = 160
_MIN_SECTION_CHARS = 180

_SECTION_METADATA_PREFIXES = (
    "diff --git ",
    "old mode ",
    "new mode ",
    "new file mode ",
    "deleted file mode ",
    "similarity index ",
    "dissimilarity index ",
    "rename from ",
    "rename to ",
    "copy from ",
    "copy to ",
    "--- ",
    "+++ ",
    "Binary files ",
    "GIT binary patch",
)

_CHANGE_MARKER_RE = re.compile(r"\[-.*?-\]|\{\+.*?\+\}")
_PEM_PRIVATE_KEY_RE = re.compile(
    r"-----BEGIN [A-Z0-9 ]*PRIVATE KEY-----.*?"
    r"-----END [A-Z0-9 ]*PRIVATE KEY-----",
    re.DOTALL,
)
_URL_CREDENTIAL_RE = re.compile(r"(?P<scheme>[a-z][a-z0-9+.-]*://)[^/@\s]+@", re.I)
_BEARER_TOKEN_RE = re.compile(r"(?i)\bBearer\s+[A-Za-z0-9._~+/=-]{8,}")
_KNOWN_TOKEN_RES = (
    re.compile(r"\bgh[opusr]_[A-Za-z0-9]{20,}\b"),
    re.compile(r"\bglpat-[A-Za-z0-9_-]{20,}\b"),
    re.compile(r"\bsk-[A-Za-z0-9_-]{20,}\b"),
)
_STRUCTURED_SECRET_RE = re.compile(
    r"""
    (?P<prefix>
        (?P<key_quote>["']?)
        (?P<key>
            password|passwd|pwd|credential|authorization|
            api[_-]?key|apikey|secret|client[_-]?secret|
            token|access[_-]?token|refresh[_-]?token|private[_-]?key
        )
        (?P=key_quote)\s*[:=]\s*
    )
    (?P<value>
        "(?:\\.|[^"\r\n])*"|
        '(?:\\.|[^'\r\n])*'|
        (?![\[{])(?:\[-.*?-\]|\{\+.*?\+\}|\[REDACTED\]|[^\r\n,\#}\]])+
    )
    """,
    re.IGNORECASE | re.VERBOSE,
)


@dataclass(slots=True)
class _DiffSection:
    """One normalized file section from a word-diff."""

    lines: list[str]

    def render(self) -> str:
        """Render this section as prompt text."""
        return "\n".join(self.lines)


def prepare_ai_context(diff: str, porcelain: str) -> tuple[str, str]:
    """Return redacted and bounded status and diff context for an AI prompt."""
    prepared_status = _limit_status(
        redact_ai_text(porcelain), DEFAULT_AI_STATUS_MAX_CHARS
    )
    prepared_diff = _prepare_diff(diff, DEFAULT_AI_DIFF_MAX_CHARS)
    return prepared_status, prepared_diff


def redact_ai_text(text: str) -> str:
    """Best-effort redaction for sensitive values before sending AI context."""
    if not text:
        return text

    redacted = _PEM_PRIVATE_KEY_RE.sub(
        "-----BEGIN PRIVATE KEY-----\n[REDACTED]\n-----END PRIVATE KEY-----",
        text,
    )
    redacted = _URL_CREDENTIAL_RE.sub(r"\g<scheme>[REDACTED]@", redacted)
    redacted = _BEARER_TOKEN_RE.sub("Bearer [REDACTED]", redacted)
    for token_re in _KNOWN_TOKEN_RES:
        redacted = token_re.sub(_REDACTED, redacted)

    def _replace_structured_secret(match: re.Match[str]) -> str:
        raw_value = match.group("value")
        leading = raw_value[: len(raw_value) - len(raw_value.lstrip())]
        value_with_trailing = raw_value[len(leading) :]
        value = value_with_trailing.rstrip()
        trailing = value_with_trailing[len(value) :]
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            replacement = f"{value[0]}{_REDACTED}{value[-1]}"
        else:
            replacement = _REDACTED
        return f"{match.group('prefix')}{leading}{replacement}{trailing}"

    return _STRUCTURED_SECRET_RE.sub(_replace_structured_secret, redacted)


def _prepare_diff(diff: str, max_chars: int) -> str:
    """Normalize, redact, and fairly bound an AI word-diff."""
    if not diff:
        return ""

    sections = [_normalize_section(lines) for lines in _split_sections(diff)]
    sections = [section for section in sections if section.lines]
    if not sections:
        return ""

    max_sections = max(1, max_chars // _MIN_SECTION_CHARS)
    omitted_sections = max(0, len(sections) - max_sections)
    included = sections[:max_sections]
    omission = (
        f"[...{omitted_sections} file diff(s) omitted...]" if omitted_sections else ""
    )
    separator_chars = max(0, len(included) - 1) * 2
    reserved = separator_chars + (len(omission) + 2 if omission else 0)
    available = max(1, max_chars - reserved)

    candidates = [
        "\n".join(
            _limit_changed_line(line)
            for line in redact_ai_text(section.render()).splitlines()
        )
        for section in included
    ]
    limits = _fair_limits([len(candidate) for candidate in candidates], available)
    rendered = [
        _limit_section(candidate, limit)
        for candidate, limit in zip(candidates, limits, strict=True)
    ]
    result = "\n\n".join(rendered)
    if omission:
        result = f"{result}\n\n{omission}"
    return result[:max_chars]


def _split_sections(diff: str) -> list[list[str]]:
    """Split a combined word-diff into per-file sections."""
    sections: list[list[str]] = []
    current: list[str] = []
    for line in diff.splitlines():
        if line.startswith("diff --git ") and current:
            sections.append(current)
            current = []
        current.append(line)
    if current:
        sections.append(current)
    return sections


def _normalize_section(lines: list[str]) -> _DiffSection:
    """Turn porcelain word-diff records into compact, readable change lines."""
    normalized: list[str] = []
    logical_line: list[str] = []

    def _flush_logical_line() -> None:
        if not logical_line:
            return
        rendered = "".join(logical_line)
        normalized.append(rendered)
        logical_line.clear()

    for line in lines:
        if line.startswith("@@"):
            _flush_logical_line()
            normalized.append(line)
        elif line == "~":
            _flush_logical_line()
        elif line.startswith(" "):
            logical_line.append(line[1:])
        elif line.startswith("-") and not line.startswith("--- "):
            logical_line.append(f"[-{line[1:]}-]")
        elif line.startswith("+") and not line.startswith("+++ "):
            logical_line.append(f"{{+{line[1:]}+}}")
        elif line.startswith(_SECTION_METADATA_PREFIXES):
            _flush_logical_line()
            normalized.append(line)
        elif line.startswith("index "):
            continue
        else:
            _flush_logical_line()
            if line:
                normalized.append(line)

    _flush_logical_line()
    return _DiffSection(normalized)


def _limit_changed_line(line: str) -> str:
    """Keep context around every visible change marker in a long line."""
    if len(line) <= _MAX_RENDERED_LINE_CHARS:
        return line

    matches = list(_CHANGE_MARKER_RE.finditer(line))
    if not matches:
        return _limit_middle(line, _MAX_RENDERED_LINE_CHARS)

    windows: list[tuple[int, int]] = []
    for match in matches:
        start = max(0, match.start() - _LINE_CONTEXT_CHARS)
        end = min(len(line), match.end() + _LINE_CONTEXT_CHARS)
        if windows and start <= windows[-1][1]:
            windows[-1] = (windows[-1][0], max(windows[-1][1], end))
        else:
            windows.append((start, end))

    parts: list[str] = []
    previous_end = 0
    for start, end in windows:
        if start > previous_end:
            parts.append(_OMITTED)
        parts.append(line[start:end])
        previous_end = end
    if previous_end < len(line):
        parts.append(_OMITTED)

    rendered = "".join(parts)
    return _limit_middle(rendered, _MAX_RENDERED_LINE_CHARS)


def _fair_limits(lengths: list[int], total: int) -> list[int]:
    """Allocate a character budget fairly and redistribute unused shares."""
    limits = [0] * len(lengths)
    remaining = set(range(len(lengths)))
    budget = total
    while remaining and budget > 0:
        share = max(1, budget // len(remaining))
        completed: set[int] = set()
        for index in remaining:
            need = lengths[index] - limits[index]
            grant = min(need, share)
            limits[index] += grant
            budget -= grant
            if limits[index] >= lengths[index]:
                completed.add(index)
        remaining -= completed
        if not completed and budget < len(remaining):
            for index in sorted(remaining):
                if budget <= 0:
                    break
                limits[index] += 1
                budget -= 1
            break
    return limits


def _limit_section(section: str, max_chars: int) -> str:
    """Limit one file section while preserving its file header."""
    if len(section) <= max_chars:
        return section
    if max_chars <= len(_OMITTED):
        return _OMITTED[:max_chars]

    first_line, separator, remainder = section.partition("\n")
    if not separator:
        return _limit_middle(first_line, max_chars)

    header_budget = min(len(first_line), max(40, max_chars // 3))
    header = _limit_middle(first_line, header_budget)
    remaining = max_chars - len(header) - 1
    if remaining <= 0:
        return header[:max_chars]
    return f"{header}\n{_limit_middle(remainder, remaining)}"


def _limit_middle(value: str, max_chars: int) -> str:
    """Limit text with an explicit omission marker between both ends."""
    if len(value) <= max_chars:
        return value
    if max_chars <= len(_OMITTED):
        return _OMITTED[:max_chars]
    remaining = max_chars - len(_OMITTED)
    before = (remaining + 1) // 2
    after = remaining // 2
    return f"{value[:before]}{_OMITTED}{value[-after:] if after else ''}"


def _limit_status(status: str, max_chars: int) -> str:
    """Bound status output while retaining entries from both ends."""
    if len(status) <= max_chars:
        return status

    lines = status.splitlines()
    omitted = f"[...{len(lines)} status entries total; middle entries omitted...]"
    kept_start: list[str] = []
    kept_end: list[str] = []
    used = len(omitted) + 2
    left = 0
    right = len(lines) - 1
    take_start = True

    while left <= right:
        index = left if take_start else right
        candidate = lines[index]
        extra = len(candidate) + 1
        if used + extra > max_chars:
            break
        if take_start:
            kept_start.append(candidate)
            left += 1
        else:
            kept_end.append(candidate)
            right -= 1
        used += extra
        take_start = not take_start

    omitted_count = max(0, len(lines) - len(kept_start) - len(kept_end))
    marker = f"[...{omitted_count} status entries omitted...]"
    parts = [*kept_start, marker, *reversed(kept_end)]
    return "\n".join(parts)[:max_chars]
