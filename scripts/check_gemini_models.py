#!/usr/bin/env python3
"""Короткая проверка моделей Gemini по GOOGLE_API_KEY из .env (корень проекта)."""

from __future__ import annotations

import asyncio
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

try:
    from dotenv import load_dotenv

    load_dotenv(os.path.join(ROOT, ".env"))
except ImportError:
    pass

from backend.services.ai_agent_llm import GEMINI_FALLBACK_MODELS, LLMError, complete_gemini


async def main() -> None:
    key = (os.environ.get("GOOGLE_API_KEY") or "").strip()
    if not key:
        print("GOOGLE_API_KEY не задан (добавьте в .env)")
        sys.exit(1)
    system = "Reply with only the word OK."
    user = "Ping"
    for model in GEMINI_FALLBACK_MODELS:
        try:
            text, _meta = await complete_gemini(key, model, system, user, timeout_s=60.0)
            print(f"OK   {model}  -> {text[:120]!r}")
        except LLMError as e:
            print(f"FAIL {model}  -> {str(e)[:240]}")
    print("Готово.")


if __name__ == "__main__":
    asyncio.run(main())
