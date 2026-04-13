from __future__ import annotations

import json
import os
from typing import Any

from dotenv import load_dotenv
from pydantic import BaseModel, Field

from .models import AgentAction, AgentDecision

load_dotenv()

try:
    from openai import OpenAI
except Exception:
    OpenAI = None


class LLMConfigError(RuntimeError):
    pass


class DecisionNarrative(BaseModel):
    intent_summary: str = Field(max_length=180)
    rationale: str = Field(max_length=500)


class LLMDecisionClient:
    def __init__(self) -> None:
        self.api_key = os.getenv("OPENAI_API_KEY")
        self.model = os.getenv("OPENAI_MODEL", "gpt-5.2")
        timeout = float(os.getenv("OPENAI_TIMEOUT_SECONDS", "45"))
        max_retries = int(os.getenv("OPENAI_MAX_RETRIES", "2"))
        self.client = (
            OpenAI(api_key=self.api_key, timeout=timeout, max_retries=max_retries)
            if self.api_key and OpenAI is not None
            else None
        )

    def build_decision(
        self,
        *,
        action: AgentAction,
        fallback_intent: str,
        fallback_rationale: str,
        context: dict[str, Any],
    ) -> AgentDecision:
        narrative = self._narrate(
            action=action,
            fallback_intent=fallback_intent,
            fallback_rationale=fallback_rationale,
            context=context,
        )
        return AgentDecision(
            intent_summary=narrative.intent_summary,
            rationale=narrative.rationale,
            action=action,
        )

    def _narrate(
        self,
        *,
        action: AgentAction,
        fallback_intent: str,
        fallback_rationale: str,
        context: dict[str, Any],
    ) -> DecisionNarrative:
        if self.client is None:
            return DecisionNarrative(
                intent_summary=fallback_intent,
                rationale=fallback_rationale,
            )

        payload = {
            "chosen_action": action.model_dump(),
            "fallback_intent": fallback_intent,
            "fallback_rationale": fallback_rationale,
            "context": context,
            "instructions": [
                "Do not change the action. The action is already fixed by deterministic policy.",
                "Write only a short intent_summary and rationale grounded in observation, beliefs, and messages.",
                "Be honest about uncertainty from delayed messages or partial visibility.",
                "Do not mention hidden information or invent facts.",
            ],
        }

        try:
            response = self.client.responses.parse(
                model=self.model,
                input=[
                    {
                        "role": "system",
                        "content": (
                            "You are writing trace narration for a tiny two-agent dungeon simulation. "
                            "The navigation policy is deterministic. "
                            "Your job is only to explain the already chosen action briefly and faithfully."
                        ),
                    },
                    {
                        "role": "user",
                        "content": json.dumps(payload, indent=2, sort_keys=True),
                    },
                ],
                text_format=DecisionNarrative,
                store=False,
            )
            parsed = response.output_parsed
            if parsed is None:
                raise RuntimeError("No parsed narrative returned.")
            return parsed
        except Exception:
            return DecisionNarrative(
                intent_summary=fallback_intent,
                rationale=fallback_rationale,
            )