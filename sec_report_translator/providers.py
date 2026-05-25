from __future__ import annotations

import json
import threading
import time
from dataclasses import dataclass, field
from typing import Sequence

import httpx
from openai import OpenAI

from .config import ModelConfig, TranslationConfig
from .prompts import SYSTEM_PROMPT, build_block_user_prompt, build_user_prompt


class ProviderError(Exception):
    pass


@dataclass
class UsageTotals:
    requests: int = 0
    prompt_tokens: int = 0
    completion_tokens: int = 0
    total_tokens: int = 0
    prompt_cache_hit_tokens: int = 0
    prompt_cache_miss_tokens: int = 0

    def as_dict(self) -> dict[str, int | float]:
        total_prompt_cache = self.prompt_cache_hit_tokens + self.prompt_cache_miss_tokens
        hit_ratio = self.prompt_cache_hit_tokens / total_prompt_cache if total_prompt_cache else 0.0
        return {
            "requests": self.requests,
            "prompt_tokens": self.prompt_tokens,
            "completion_tokens": self.completion_tokens,
            "total_tokens": self.total_tokens,
            "prompt_cache_hit_tokens": self.prompt_cache_hit_tokens,
            "prompt_cache_miss_tokens": self.prompt_cache_miss_tokens,
            "prompt_cache_hit_ratio": round(hit_ratio, 4),
        }


@dataclass
class ProviderStats:
    totals: UsageTotals = field(default_factory=UsageTotals)
    lock: threading.Lock = field(default_factory=threading.Lock)

    def record(self, usage: object | None) -> None:
        if usage is None:
            return
        with self.lock:
            self.totals.requests += 1
            self.totals.prompt_tokens += usage_value(usage, "prompt_tokens")
            self.totals.completion_tokens += usage_value(usage, "completion_tokens")
            self.totals.total_tokens += usage_value(usage, "total_tokens")
            self.totals.prompt_cache_hit_tokens += usage_value(usage, "prompt_cache_hit_tokens")
            self.totals.prompt_cache_miss_tokens += usage_value(usage, "prompt_cache_miss_tokens")

    def summary(self) -> dict[str, int | float]:
        with self.lock:
            return UsageTotals(
                requests=self.totals.requests,
                prompt_tokens=self.totals.prompt_tokens,
                completion_tokens=self.totals.completion_tokens,
                total_tokens=self.totals.total_tokens,
                prompt_cache_hit_tokens=self.totals.prompt_cache_hit_tokens,
                prompt_cache_miss_tokens=self.totals.prompt_cache_miss_tokens,
            ).as_dict()


def usage_value(usage: object, key: str) -> int:
    if isinstance(usage, dict):
        return int(usage.get(key) or 0)
    return int(getattr(usage, key, 0) or 0)


class OpenAICompatibleProvider:
    def __init__(self, config: ModelConfig, translation: TranslationConfig):
        self.config = config
        self.translation = translation
        self.stats = ProviderStats()
        self.client = OpenAI(
            api_key=config.api_key,
            base_url=config.base_url,
            timeout=config.timeout_seconds,
            http_client=httpx.Client(trust_env=False),
        )

    def translate(
        self,
        units: Sequence[object],
        context_before: str = "",
        context_after: str = "",
        block_prompt: str = "",
        batch_id: str = "",
        strict_json: bool = False,
        repair_reason: str = "",
        previous_response: str = "",
    ) -> str:
        units_json = json.dumps(
            [{"id": unit.id, "text": unit.text} for unit in units],
            ensure_ascii=False,
        )
        if block_prompt:
            user_prompt = build_block_user_prompt(
                block_prompt,
                batch_id=batch_id,
                item_ids=[unit.id for unit in units],
                strict_json=strict_json,
                repair_reason=repair_reason,
                previous_response=previous_response,
            )
        else:
            user_prompt = build_user_prompt(
                units_json,
                target_language=self.translation.target_language,
                context_before=context_before,
                context_after=context_after,
                strict_json=strict_json,
            )
        last_error: Exception | None = None
        for attempt in range(max(1, self.config.max_retries)):
            try:
                response = self.client.chat.completions.create(
                    model=self.config.model,
                    temperature=self.config.temperature,
                    top_p=self.config.top_p,
                    messages=[
                        {"role": "system", "content": SYSTEM_PROMPT},
                        {"role": "user", "content": user_prompt},
                    ],
                )
                content = response.choices[0].message.content
                if not content:
                    raise ProviderError("Model returned an empty response.")
                self.stats.record(response.usage)
                return content
            except Exception as exc:  # pragma: no cover - OpenAI client errors vary by provider.
                last_error = exc
                if attempt + 1 < max(1, self.config.max_retries):
                    time.sleep(min(2**attempt, 8))
        raise ProviderError(str(last_error) if last_error else "Model request failed.")

    def prime_block_cache(self, block_prompt: str) -> None:
        last_error: Exception | None = None
        for attempt in range(max(1, self.config.max_retries)):
            try:
                response = self.client.chat.completions.create(
                    model=self.config.model,
                    temperature=0,
                    top_p=self.config.top_p,
                    messages=[
                        {"role": "system", "content": SYSTEM_PROMPT},
                        {"role": "user", "content": block_prompt},
                    ],
                    max_tokens=16,
                )
                self.stats.record(response.usage)
                return
            except Exception as exc:  # pragma: no cover - OpenAI client errors vary by provider.
                last_error = exc
                if attempt + 1 < max(1, self.config.max_retries):
                    time.sleep(min(2**attempt, 8))
        raise ProviderError(str(last_error) if last_error else "Cache warmup request failed.")

    def usage_summary(self) -> dict[str, int | float]:
        return self.stats.summary()
