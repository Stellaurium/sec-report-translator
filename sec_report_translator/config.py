from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any
import re

import tomli


@dataclass(frozen=True)
class SecConfig:
    user_agent_name: str
    user_agent_email: str


@dataclass(frozen=True)
class ModelConfig:
    base_url: str
    api_key: str
    model: str
    temperature: float
    top_p: float
    timeout_seconds: int
    max_retries: int
    context_window_tokens: int
    max_output_tokens: int


@dataclass(frozen=True)
class FallbackModelConfig:
    enabled: bool
    base_url: str
    api_key: str
    model: str
    temperature: float
    top_p: float
    timeout_seconds: int
    max_retries: int
    context_window_tokens: int
    max_output_tokens: int

    def as_model_config(self) -> ModelConfig:
        return ModelConfig(
            base_url=self.base_url,
            api_key=self.api_key,
            model=self.model,
            temperature=self.temperature,
            top_p=self.top_p,
            timeout_seconds=self.timeout_seconds,
            max_retries=self.max_retries,
            context_window_tokens=self.context_window_tokens,
            max_output_tokens=self.max_output_tokens,
        )


@dataclass(frozen=True)
class BatchConfig:
    initial_units: int
    min_units: int
    max_units: int
    stable_successes_to_grow: int
    grow_factor: float
    shrink_factor: float
    max_chars_per_batch: int
    prev_context_chars: int
    next_context_chars: int


@dataclass(frozen=True)
class TranslationConfig:
    target_language: str
    conservative_table_translation: bool
    fallback_to_source_on_failure: bool
    skip_numeric_cells: bool
    preserve_company_names: bool


@dataclass(frozen=True)
class OutputConfig:
    default_suffix: str
    write_markdown_report: bool
    write_json_report: bool


@dataclass(frozen=True)
class CacheConfig:
    enabled: bool
    cache_dir: str


@dataclass(frozen=True)
class BlockConfig:
    enabled: bool
    max_input_ratio: float
    target_batch_units: int
    max_batch_chars: int
    max_batches_per_block: int
    max_workers: int
    warmup_first: bool
    warmup_delay_seconds: float
    before_context_ratio: float
    block_body_ratio: float
    after_context_ratio: float


@dataclass(frozen=True)
class AppConfig:
    sec: SecConfig | None
    model: ModelConfig | None
    fallback_model: FallbackModelConfig
    batch: BatchConfig
    translation: TranslationConfig
    output: OutputConfig
    cache: CacheConfig
    block: BlockConfig


class ConfigError(Exception):
    pass


def load_config(path: Path) -> AppConfig:
    if not path.exists():
        raise ConfigError(f"Config file does not exist: {path}")

    data: dict[str, Any] = tomli.loads(path.read_text(encoding="utf-8-sig"))

    sec_data = data.get("sec")
    sec = None
    if isinstance(sec_data, dict):
        name = str(sec_data.get("user_agent_name", "")).strip()
        email = str(sec_data.get("user_agent_email", "")).strip()
        if name and email:
            if not is_valid_email(email):
                raise ConfigError(
                    "Config [sec].user_agent_email must be a valid email address for SEC User-Agent."
                )
            sec = SecConfig(user_agent_name=name, user_agent_email=email)

    model = parse_model_config(data.get("model"), "model", required=False)
    fallback_model = parse_fallback_model_config(data.get("fallback_model"))
    batch = parse_batch_config(data.get("batch"))
    translation = parse_translation_config(data.get("translation"))
    output = parse_output_config(data.get("output"))
    cache = parse_cache_config(data.get("cache"))
    block = parse_block_config(data.get("block"))

    return AppConfig(
        sec=sec,
        model=model,
        fallback_model=fallback_model,
        batch=batch,
        translation=translation,
        output=output,
        cache=cache,
        block=block,
    )


def require_sec_config(config: AppConfig) -> SecConfig:
    if config.sec is None:
        raise ConfigError(
            "Config file must contain [sec] user_agent_name and user_agent_email for download."
        )
    return config.sec


def require_model_config(config: AppConfig) -> ModelConfig:
    if config.model is None:
        raise ConfigError(
            "Config file must contain [model] base_url, api_key, and model for translate. "
            "Run: sec-translator init-config -o sec-translator.toml"
        )
    if config.model.api_key == "YOUR_API_KEY":
        raise ConfigError("Config [model].api_key must be set before translate.")
    return config.model


def is_valid_email(email: str) -> bool:
    if ".." in email:
        return False
    return re.match(r"^[^@\s]+@[^@\s]+\.[^@\s]+$", email) is not None


def parse_model_config(data: Any, section: str, *, required: bool) -> ModelConfig | None:
    if not isinstance(data, dict):
        if required:
            raise ConfigError(f"Config file must contain [{section}].")
        return None
    base_url = str(data.get("base_url", "")).strip()
    api_key = str(data.get("api_key", "")).strip()
    model = str(data.get("model", "")).strip()
    if not base_url or not api_key or not model:
        if required:
            raise ConfigError(f"Config [{section}] must contain base_url, api_key, and model.")
        return None
    return ModelConfig(
        base_url=base_url.rstrip("/"),
        api_key=api_key,
        model=model,
        temperature=float(data.get("temperature", 0.1)),
        top_p=float(data.get("top_p", 0.95)),
        timeout_seconds=int(data.get("timeout_seconds", 300)),
        max_retries=int(data.get("max_retries", 3)),
        context_window_tokens=int(data.get("context_window_tokens", 128000)),
        max_output_tokens=int(data.get("max_output_tokens", 4096)),
    )


def parse_fallback_model_config(data: Any) -> FallbackModelConfig:
    if not isinstance(data, dict):
        return FallbackModelConfig(False, "", "", "", 0.1, 0.95, 300, 1, 128000, 4096)
    enabled = bool(data.get("enabled", False))
    if not enabled:
        return FallbackModelConfig(False, "", "", "", 0.1, 0.95, 300, 1, 128000, 4096)
    model = parse_model_config(data, "fallback_model", required=True)
    assert model is not None
    return FallbackModelConfig(
        enabled=True,
        base_url=model.base_url,
        api_key=model.api_key,
        model=model.model,
        temperature=model.temperature,
        top_p=model.top_p,
        timeout_seconds=model.timeout_seconds,
        max_retries=model.max_retries,
        context_window_tokens=model.context_window_tokens,
        max_output_tokens=model.max_output_tokens,
    )


def parse_batch_config(data: Any) -> BatchConfig:
    data = data if isinstance(data, dict) else {}
    batch = BatchConfig(
        initial_units=int(data.get("initial_units", 20)),
        min_units=int(data.get("min_units", 1)),
        max_units=int(data.get("max_units", 80)),
        stable_successes_to_grow=int(data.get("stable_successes_to_grow", 5)),
        grow_factor=float(data.get("grow_factor", 1.25)),
        shrink_factor=float(data.get("shrink_factor", 0.5)),
        max_chars_per_batch=int(data.get("max_chars_per_batch", 12000)),
        prev_context_chars=int(data.get("prev_context_chars", 3000)),
        next_context_chars=int(data.get("next_context_chars", 2000)),
    )
    if batch.min_units < 1:
        raise ConfigError("Config [batch].min_units must be at least 1.")
    if batch.max_units < batch.min_units:
        raise ConfigError("Config [batch].max_units must be greater than or equal to min_units.")
    if batch.initial_units < batch.min_units:
        raise ConfigError("Config [batch].initial_units must be greater than or equal to min_units.")
    if batch.max_chars_per_batch < 100:
        raise ConfigError("Config [batch].max_chars_per_batch is too small.")
    return batch


def parse_translation_config(data: Any) -> TranslationConfig:
    data = data if isinstance(data, dict) else {}
    return TranslationConfig(
        target_language=str(data.get("target_language", "Simplified Chinese")),
        conservative_table_translation=bool(data.get("conservative_table_translation", True)),
        fallback_to_source_on_failure=bool(data.get("fallback_to_source_on_failure", True)),
        skip_numeric_cells=bool(data.get("skip_numeric_cells", True)),
        preserve_company_names=bool(data.get("preserve_company_names", True)),
    )


def parse_output_config(data: Any) -> OutputConfig:
    data = data if isinstance(data, dict) else {}
    suffix = str(data.get("default_suffix", "_translated"))
    if not suffix:
        raise ConfigError("Config [output].default_suffix must not be empty.")
    return OutputConfig(
        default_suffix=suffix,
        write_markdown_report=bool(data.get("write_markdown_report", True)),
        write_json_report=bool(data.get("write_json_report", True)),
    )


def parse_cache_config(data: Any) -> CacheConfig:
    data = data if isinstance(data, dict) else {}
    cache_dir = str(data.get("cache_dir", ".sec-translator-cache")).strip()
    if not cache_dir:
        raise ConfigError("Config [cache].cache_dir must not be empty.")
    return CacheConfig(
        enabled=bool(data.get("enabled", True)),
        cache_dir=cache_dir,
    )


def parse_block_config(data: Any) -> BlockConfig:
    data = data if isinstance(data, dict) else {}
    config = BlockConfig(
        enabled=bool(data.get("enabled", False)),
        max_input_ratio=float(data.get("max_input_ratio", 0.6)),
        target_batch_units=int(data.get("target_batch_units", 40)),
        max_batch_chars=int(data.get("max_batch_chars", 20000)),
        max_batches_per_block=int(data.get("max_batches_per_block", 20)),
        max_workers=int(data.get("max_workers", 50)),
        warmup_first=bool(data.get("warmup_first", True)),
        warmup_delay_seconds=float(data.get("warmup_delay_seconds", 2.0)),
        before_context_ratio=float(data.get("before_context_ratio", 0.2)),
        block_body_ratio=float(data.get("block_body_ratio", 0.6)),
        after_context_ratio=float(data.get("after_context_ratio", 0.2)),
    )
    if not 0 < config.max_input_ratio <= 1:
        raise ConfigError("Config [block].max_input_ratio must be greater than 0 and at most 1.")
    if config.target_batch_units < 1:
        raise ConfigError("Config [block].target_batch_units must be at least 1.")
    if config.max_batch_chars < 100:
        raise ConfigError("Config [block].max_batch_chars is too small.")
    if config.max_batches_per_block < 1:
        raise ConfigError("Config [block].max_batches_per_block must be at least 1.")
    if config.max_workers < 1:
        raise ConfigError("Config [block].max_workers must be at least 1.")
    if config.warmup_delay_seconds < 0:
        raise ConfigError("Config [block].warmup_delay_seconds must not be negative.")
    ratio_total = config.before_context_ratio + config.block_body_ratio + config.after_context_ratio
    if ratio_total <= 0:
        raise ConfigError("Config [block] context/body ratios must sum to a positive value.")
    return config
