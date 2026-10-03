"""Private structured adapter over the existing OpenRouter HTTP transport."""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation

from infrastructure.llm.client import LLMClient
from pastoral_bot.config import BotSettings

REPLY_SCHEMA = {
    "type": "object",
    "properties": {
        "text": {"type": "string", "minLength": 1, "maxLength": 12000},
        "source_ids": {"type": "array", "items": {"type": "string"}, "maxItems": 6},
        "referral": {"type": ["string", "null"], "enum": [None, "priest", "professional", "emergency"]},
    },
    "required": ["text", "source_ids", "referral"],
    "additionalProperties": False,
}


class LLMUnavailable(RuntimeError):
    def __init__(self, *, cost: Decimal | None = None):
        self.cost = cost
        super().__init__("provider_unavailable")


@dataclass(frozen=True)
class Completion:
    content: str = field(repr=False)
    cost: Decimal | None = None
    finish_reason: str | None = None


class PastoralLLM:
    def __init__(self, settings: BotSettings, *, client: LLMClient | None = None):
        self.settings = settings
        self.client = client or LLMClient(
            api_key=settings.openrouter_api_key.get_secret_value(), model=settings.model,
            temperature=0.4, private_transport=True,
        )
        self._pricing_breach = False

    def input_size(self, messages: list[dict]) -> int:
        # A conservative token ceiling: one token per UTF-8 byte, including
        # JSON/schema overhead, plus framing. No story needs a remote tokenizer.
        body = {"messages": messages, "schema": REPLY_SCHEMA}
        return len(json.dumps(body, ensure_ascii=False).encode("utf-8")) + 512

    def upper_cost(self, messages: list[dict]) -> Decimal:
        return (
            Decimal(self.input_size(messages)) * self.settings.input_price_per_million
            + Decimal(self.settings.max_output_tokens) * self.settings.output_price_per_million
        ) / Decimal(1_000_000)

    async def complete(self, messages: list[dict]) -> Completion:
        if self._pricing_breach or self.input_size(messages) > self.settings.max_input_tokens:
            raise LLMUnavailable()
        provider = {
            "zdr": True,
            "require_parameters": True,
            "max_price": {
                "prompt": float(self.settings.input_price_per_million),
                "completion": float(self.settings.output_price_per_million),
                "request": 0,
            },
        }
        try:
            content, finish, usage = await self.client.complete_private(
                messages, schema=REPLY_SCHEMA, provider=provider,
                max_tokens=self.settings.max_output_tokens,
                timeout_s=self.settings.request_timeout_seconds,
            )
        except Exception:
            # Never expose provider errors, exception messages, or raw bodies.
            raise LLMUnavailable() from None
        cost = None
        try:
            value = Decimal(str(usage.get("cost")))
            if value.is_finite() and value >= 0:
                cost = value
        except (InvalidOperation, TypeError, ValueError):
            pass
        if cost is not None and cost > self.upper_cost(messages):
            # If the provider violates the reserved pricing ceiling, stop all
            # further calls in this process until an operator investigates.
            self._pricing_breach = True
            raise LLMUnavailable(cost=cost)
        return Completion(content=content, cost=cost, finish_reason=finish)
