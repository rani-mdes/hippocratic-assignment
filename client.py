"""Single point of contact with the OpenAI Chat Completions API.

`chat()` is the only call site for the SDK in the rest of the codebase.
The legacy `openai==0.28.0` SDK is sync, which lets us fan out support
agents with a thread pool elsewhere without retrofitting async.
"""

from __future__ import annotations

import os

import openai
from dotenv import load_dotenv

from config import MODEL_ID

# Load .env once at import time so module consumers don't need to think
# about it. dotenv silently no-ops when the file is absent.
load_dotenv()


class MissingAPIKeyError(RuntimeError):
    """Raised by `chat()` when OPENAI_API_KEY is not configured."""


def chat(
    messages: list[dict[str, str]],
    *,
    temperature: float,
    max_tokens: int,
) -> str:
    """Send a chat completion request and return the assistant message text."""
    api_key = os.getenv("OPENAI_API_KEY")
    if not api_key:
        raise MissingAPIKeyError(
            "OPENAI_API_KEY is not set. Add it to your shell environment or "
            "to a `.env` file at the project root."
        )
    openai.api_key = api_key

    response = openai.ChatCompletion.create(
        model=MODEL_ID,
        messages=messages,
        temperature=temperature,
        max_tokens=max_tokens,
        stream=False,
    )
    return response.choices[0].message["content"]  # type: ignore[index, return-value]
