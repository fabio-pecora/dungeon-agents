from __future__ import annotations

import os
from typing import Any

from dotenv import load_dotenv
from openai import OpenAI

from .models import AgentDecision

load_dotenv()


class LLMConfigError(RuntimeError):
    pass


class LLMDecisionClient:
    def __init__(self) -> None:
        api_key = os.getenv("OPENAI_API_KEY")
        if not api_key:
            raise LLMConfigError(
                "OPENAI_API_KEY is missing. Add it to your environment or .env file before running."
            )
        self.model = os.getenv("OPENAI_MODEL", "gpt-5.2")
        timeout = float(os.getenv("OPENAI_TIMEOUT_SECONDS", "45"))
        max_retries = int(os.getenv("OPENAI_MAX_RETRIES", "2"))
        self.client = OpenAI(api_key=api_key, timeout=timeout, max_retries=max_retries)

    def decide(self, *, system_prompt: str, user_payload: dict[str, Any]) -> AgentDecision:
        response = self.client.responses.parse(
            model=self.model,
            input=[
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": self._render_payload(user_payload)},
            ],
            text_format=AgentDecision,
            store=False,
        )
        parsed = response.output_parsed
        if parsed is None:
            raise RuntimeError("Model did not return a structured decision.")
        return parsed

    @staticmethod
    def _render_payload(payload: dict[str, Any]) -> str:
        import json

        return json.dumps(payload, indent=2, sort_keys=True)
