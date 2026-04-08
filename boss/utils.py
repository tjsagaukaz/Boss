"""Shared utilities for Boss Assistant."""

from __future__ import annotations

import re

# --- Thinking token parser ---

_THINK_OPEN = re.compile(r"<think>", re.IGNORECASE)
_THINK_CLOSE = re.compile(r"</think>", re.IGNORECASE)


class ThinkingFilter:
    """Accumulates streaming tokens, separating <think>…</think> from visible text."""

    def __init__(self):
        self.inside_think = False
        self.thinking_buffer: list[str] = []
        self.pending = ""

    def feed(self, token: str) -> str:
        text = self.pending + token
        self.pending = ""
        visible = []

        while text:
            if self.inside_think:
                match = _THINK_CLOSE.search(text)
                if match:
                    self.thinking_buffer.append(text[: match.start()])
                    text = text[match.end() :]
                    self.inside_think = False
                else:
                    if "<" in text and text.rstrip().endswith(tuple("<</</t</th</thi</thin</think".split("/"))):
                        cut = text.rfind("<")
                        self.thinking_buffer.append(text[:cut])
                        self.pending = text[cut:]
                    else:
                        self.thinking_buffer.append(text)
                    text = ""
            else:
                match = _THINK_OPEN.search(text)
                if match:
                    visible.append(text[: match.start()])
                    text = text[match.end() :]
                    self.inside_think = True
                else:
                    if text.endswith("<") or re.search(r"<t(h(i(n(k)?)?)?)?\Z", text):
                        cut = text.rfind("<")
                        visible.append(text[:cut])
                        self.pending = text[cut:]
                    else:
                        visible.append(text)
                    text = ""

        return "".join(visible)

    def flush(self) -> str:
        out = self.pending
        self.pending = ""
        return out

    @property
    def thinking_text(self) -> str:
        return "".join(self.thinking_buffer).strip()


# --- Dangerous pattern pre-filter ---

_DANGEROUS_PATTERNS = re.compile(
    r"""
    rm\s+-rf\s+/                |
    rm\s+-rf\s+~                |
    mkfs\.                      |
    dd\s+if=.*of=/dev/          |
    >\s*/dev/sd[a-z]            |
    curl.*\|\s*(ba)?sh          |
    wget.*\|\s*(ba)?sh          |
    :\(\)\{\s*:\|:\s*&\s*\};:   |
    /etc/passwd                 |
    /etc/shadow
    """,
    re.VERBOSE | re.IGNORECASE,
)


def is_obviously_dangerous(text: str) -> str | None:
    match = _DANGEROUS_PATTERNS.search(text)
    if match:
        return f"Blocked: dangerous pattern detected (`{match.group().strip()}`)"
    return None
