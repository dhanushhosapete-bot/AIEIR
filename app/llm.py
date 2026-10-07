"""Thin model client. The app, the safety classifier and the eval grader all go through
`LLM.complete`, so tests can swap in `FakeLLM` and record exactly what each prompt contained."""
from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from typing import Any, Callable, Protocol


@dataclass
class LLMResult:
    text: str
    tool_input: dict | None = None
    raw: Any = None


class LLM(Protocol):
    def complete(self, *, system: str, messages: list[dict], model: str, max_tokens: int = 2000,
                 tool: dict | None = None) -> LLMResult: ...


class AnthropicLLM:
    """Calls the Anthropic Messages API. Needs ANTHROPIC_API_KEY in the environment."""

    def __init__(self) -> None:
        import anthropic
        self._client = anthropic.Anthropic()

    def complete(self, *, system: str, messages: list[dict], model: str, max_tokens: int = 2000,
                 tool: dict | None = None) -> LLMResult:
        kwargs: dict[str, Any] = dict(model=model, system=system, messages=messages, max_tokens=max_tokens)
        if tool:
            # tool_choice stays "auto": forcing a tool is not allowed while thinking is on,
            # and current models think by default. The prompt asks for the tool call, and
            # parse_json_reply() below falls back to JSON in plain text.
            kwargs["tools"] = [tool]
        resp = self._client.messages.create(**kwargs)
        text_parts, tool_input = [], None
        for block in resp.content:
            if block.type == "text":
                text_parts.append(block.text)
            elif block.type == "tool_use" and tool and block.name == tool["name"]:
                tool_input = block.input
        return LLMResult(text="".join(text_parts).strip(), tool_input=tool_input, raw=resp)


@dataclass
class FakeLLM:
    """Scripted model for tests. `responder(system, messages, model, tool)` returns either a
    string (text reply) or a dict (tool input). Every call is recorded in `.calls`."""
    responder: Callable[..., str | dict] = lambda **_: "ok"
    calls: list[dict] = field(default_factory=list)

    def complete(self, *, system: str, messages: list[dict], model: str, max_tokens: int = 2000,
                 tool: dict | None = None) -> LLMResult:
        self.calls.append({"system": system, "messages": messages, "model": model, "tool": tool})
        out = self.responder(system=system, messages=messages, model=model, tool=tool)
        if isinstance(out, dict):
            return LLMResult(text=json.dumps(out), tool_input=out)
        return LLMResult(text=out)

    def all_prompt_text(self) -> str:
        """Everything any prompt contained, for privacy assertions."""
        return "\n".join(c["system"] + "\n" + json.dumps(c["messages"]) for c in self.calls)


def parse_json_reply(result: LLMResult) -> Any:
    """Prefer the tool call; otherwise pull the first JSON object or array out of the text."""
    if result.tool_input is not None:
        return result.tool_input
    text = result.text.strip()
    fenced = re.search(r"```(?:json)?\s*(.+?)```", text, re.S)
    if fenced:
        text = fenced.group(1)
    for opener, closer in (("{", "}"), ("[", "]")):
        i, j = text.find(opener), text.rfind(closer)
        if i != -1 and j > i:
            try:
                return json.loads(text[i:j + 1])
            except json.JSONDecodeError:
                continue
    raise ValueError("model reply contained no JSON")


_default: LLM | None = None


def default_llm() -> LLM:
    global _default
    if _default is None:
        _default = AnthropicLLM()
    return _default


def set_default_llm(llm: LLM | None) -> None:
    global _default
    _default = llm
