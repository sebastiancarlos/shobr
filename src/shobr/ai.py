"""AI integration (via `any-llm`).

This is the glue to get chat completions. The domain logic lives elsewhere.
"""

import json
import os
import re
from typing import cast

from .core import ShobrError

DEFAULT_MODEL = os.environ.get("SHOBR_AI_MODEL", "openai:gpt-4o-mini")


def chat_completion(
    message: str,
    model: str | None = None,
    max_tokens: int = 512,
) -> str:
    """Return the text of one chat completion via any-llm.

    Sends `message` as a single user message.

    `model` is `provider:model` (also accepts `provider/model`, or a bare model
    name which implies the openai provider), or when omitted it resolves to
    `DEFAULT_MODEL`.

    Keys and base URLs come from the provider env vars (for example
    `OPENAI_API_KEY`, `OPENAI_BASE_URL`). A missing `OPENAI_API_KEY` gets a
    dummy default (local endpoints rarely check auth).
    """
    from any_llm import completion
    from any_llm.types.completion import ChatCompletion

    os.environ.setdefault("OPENAI_API_KEY", "shobr-dummy-key")

    model = model or DEFAULT_MODEL
    provider: str | None = None
    if ":" in model:
        provider, model = model.split(":", 1)
    elif "/" in model:
        provider, model = model.split("/", 1)
    else:
        provider = "openai"  # bare model name -> OpenAI

    try:
        resp = cast(
            ChatCompletion,
            completion(
                model=model,
                provider=provider,
                messages=[{"role": "user", "content": message}],
                max_tokens=max_tokens,
            ),
        )
    except Exception as e:
        raise ShobrError(f"any-llm error: {e}") from e

    try:
        return (resp.choices[0].message.content or "").strip()
    except KeyError, IndexError, TypeError, AttributeError:
        # note: Valid except syntax as of 3.14 (PEP 758)
        raise ShobrError(f"unexpected any-llm response: {resp!r}")


def parse_json_object(text: str) -> dict | None:
    """JSON-object parse of an LLM reply (fences tolerated, first `{}` fallback)."""
    cleaned = re.sub(r"^```(json)?|```$", "", text or "", flags=re.MULTILINE).strip()
    try:
        data = json.loads(cleaned)
    except json.JSONDecodeError:
        match = re.search(r"\{.*\}", cleaned, flags=re.DOTALL)
        if not match:
            return None
        try:
            data = json.loads(match.group(0))
        except json.JSONDecodeError:
            return None
    return data if isinstance(data, dict) else None


def smoke_test_completion() -> None:
    """Print one LLM completion, proving the any-llm flow works end to end."""
    text = chat_completion("shobr llm smoke test: reply with a short greeting")
    print(DEFAULT_MODEL)
    print(text)
