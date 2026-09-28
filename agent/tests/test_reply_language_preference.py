"""ZT add-on (2026-09-28): a remembered reply-language preference wins.

The system prompt answers "in the same language the user used" by default. An
operator who needs English replies stores one ``user`` memory entry; its index
line is injected through ``{memory_section}`` and the language guideline now
defers to it, so the preference is no longer in conflict with the default.
"""

from __future__ import annotations

from pathlib import Path

from src.agent.context import _SYSTEM_PROMPT, ContextBuilder
from src.agent.memory import WorkspaceMemory
from src.agent.tools import ToolRegistry
from src.memory.persistent import PersistentMemory

_TITLE = "Always answer in English"
_DESCRIPTION = (
    "Reply in English in every answer, report and summary, whatever language the user, "
    "a tool result or a source uses; translate quoted non-English text."
)


def test_the_language_guideline_defers_to_a_remembered_preference() -> None:
    assert "Respond in the same language the user used, unless a reply-language preference" in _SYSTEM_PROMPT
    guideline = _SYSTEM_PROMPT.index("reply-language preference")
    assert guideline < _SYSTEM_PROMPT.index("{memory_section}")


def test_an_english_preference_reaches_the_system_prompt(tmp_path: Path) -> None:
    PersistentMemory(memory_dir=tmp_path).add(
        name=_TITLE, description=_DESCRIPTION, memory_type="user",
        content="Always respond in English, even when the question is in Chinese.",
    )
    memory = PersistentMemory(memory_dir=tmp_path)  # a new session freezes the snapshot

    prompt = ContextBuilder(
        ToolRegistry(), WorkspaceMemory(), persistent_memory=memory
    ).build_system_prompt("上涨概率多少？")

    section = prompt[prompt.index("## Persistent Memory (cross-session)"):]
    assert f"[{_TITLE}]" in section
    assert _DESCRIPTION in section
    assert prompt.index("unless a reply-language preference") < prompt.index(f"[{_TITLE}]")


def test_without_a_preference_the_default_is_unchanged(tmp_path: Path) -> None:
    prompt = ContextBuilder(
        ToolRegistry(), WorkspaceMemory(), persistent_memory=PersistentMemory(memory_dir=tmp_path)
    ).build_system_prompt("hello")

    assert "## Persistent Memory (cross-session)" not in prompt
    assert "Respond in the same language the user used" in prompt
