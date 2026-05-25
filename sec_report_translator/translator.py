from __future__ import annotations

import copy
import hashlib
import json
import re
import threading
import time
from collections import Counter
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from bs4 import BeautifulSoup, NavigableString, Tag

from .config import AppConfig, ModelConfig, require_model_config
from .prompts import PROMPT_VERSION, SYSTEM_PROMPT, build_block_common_prompt
from .providers import OpenAICompatibleProvider


class TranslateError(Exception):
    pass


class TranslateAttemptError(TranslateError):
    def __init__(self, reason: str, response: str = ""):
        super().__init__(reason)
        self.response = response


@dataclass
class TranslationUnit:
    id: str
    element: Tag
    text: str
    source_text: str
    placeholders: dict[str, Tag] = field(default_factory=dict)


@dataclass
class UnitResult:
    unit: TranslationUnit
    text: str
    provider: str


@dataclass
class BatchPlan:
    id: str
    units: list[TranslationUnit]


@dataclass
class BlockPlan:
    id: str
    batches: list[BatchPlan]
    context_before: str
    context_after: str
    common_prompt: str

    @property
    def units(self) -> list[TranslationUnit]:
        return [unit for batch in self.batches for unit in batch.units]


@dataclass
class PendingFallback:
    unit: TranslationUnit
    primary_reason: str


@dataclass
class BlockBatchResult:
    results: list[UnitResult] = field(default_factory=list)
    pending_fallbacks: list[PendingFallback] = field(default_factory=list)
    context_limited: bool = False


@dataclass(frozen=True)
class CachedTranslation:
    text: str
    provider: str


class TranslationCache:
    def __init__(
        self,
        cache_path: Path,
        *,
        file_hash: str,
        prompt_hash: str,
        target_language: str,
        model: str,
    ):
        self.cache_path = cache_path
        self.file_hash = file_hash
        self.prompt_hash = prompt_hash
        self.target_language = target_language
        self.model = model
        self.entries: dict[str, CachedTranslation] = {}
        self.lock = threading.Lock()
        self.load()

    def load(self) -> None:
        if not self.cache_path.exists():
            return
        for line in self.cache_path.read_text(encoding="utf-8", errors="replace").splitlines():
            if not line.strip():
                continue
            try:
                item = json.loads(line)
            except json.JSONDecodeError:
                continue
            key = str(item.get("cache_key", ""))
            text = item.get("text")
            provider = str(item.get("provider", "cache"))
            if key and isinstance(text, str):
                if contains_replacement_character(text):
                    continue
                self.entries[key] = CachedTranslation(text=text, provider=provider)

    def get(self, unit: TranslationUnit) -> CachedTranslation | None:
        with self.lock:
            return self.entries.get(self.cache_key(unit))

    def put(self, unit: TranslationUnit, text: str, provider: str) -> None:
        key = self.cache_key(unit)
        record = {
            "cache_key": key,
            "file_hash": self.file_hash,
            "unit_id": unit.id,
            "source_hash": sha256_text(unit.text),
            "prompt_hash": self.prompt_hash,
            "target_language": self.target_language,
            "model": self.model,
            "provider": provider,
            "text": text,
            "source_preview": clip(unit.source_text),
        }
        with self.lock:
            self.entries[key] = CachedTranslation(text=text, provider=provider)
            self.cache_path.parent.mkdir(parents=True, exist_ok=True)
            with self.cache_path.open("a", encoding="utf-8") as f:
                f.write(json.dumps(record, ensure_ascii=False, separators=(",", ":")) + "\n")

    def cache_key(self, unit: TranslationUnit) -> str:
        placeholder_names_text = ",".join(sorted(unit.placeholders.keys()))
        return sha256_text(
            "\n".join(
                [
                    self.file_hash,
                    unit.id,
                    sha256_text(unit.text),
                    placeholder_names_text,
                    self.prompt_hash,
                    self.target_language,
                    self.model,
                ]
            )
        )


def sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def make_translation_cache(input_path: Path, html: str, config: AppConfig, model_config: ModelConfig) -> TranslationCache | None:
    if not config.cache.enabled:
        return None
    file_hash = sha256_text(html)
    cache_root = Path(config.cache.cache_dir)
    if not cache_root.is_absolute():
        cache_root = input_path.parent / cache_root
    prompt_hash = sha256_text(f"{PROMPT_VERSION}\n{SYSTEM_PROMPT}")
    cache_path = cache_root / f"{file_hash[:16]}.jsonl"
    return TranslationCache(
        cache_path,
        file_hash=file_hash,
        prompt_hash=prompt_hash,
        target_language=config.translation.target_language,
        model=model_config.model,
    )


BLOCK_TAGS = {
    "p",
    "li",
    "td",
    "th",
    "caption",
    "h1",
    "h2",
    "h3",
    "h4",
    "h5",
    "h6",
    "div",
}
SKIP_TAGS = {"script", "style", "meta", "link", "head", "noscript", "code", "pre"}
PROTECTED_TERMS = {
    "rmb",
    "us$",
    "$",
    "ads",
    "class a",
    "class b",
    "nasdaq",
    "nyse",
    "sec",
    "us gaap",
    "form 20-f",
    "form 10-k",
    "cik",
}
REFUSAL_PATTERNS = [
    r"\bi cannot\b",
    r"\bi can't\b",
    r"\bcannot comply\b",
    r"\bunable to assist\b",
    r"\bpolicy\b",
    r"抱歉[，,]?(我)?(无法|不能)",
    r"(我)?无法(协助|帮助|处理|翻译|回答)",
    r"(我)?不能(协助|帮助|处理|翻译|回答)",
    r"拒绝(回答|翻译|处理)",
]


class ProgressReporter:
    def __init__(self, total_units: int):
        self.total_units = total_units
        self.started_at = time.monotonic()
        self.completed = 0
        self.primary_success = 0
        self.fallback_success = 0
        self.retained = 0
        self.cache_hits = 0
        self.failures = 0
        self.batch_number = 0
        self.lock = threading.Lock()

    def start(self) -> None:
        self.write(f"Translation units: {self.total_units}")

    def batch_start(self, batch: list[TranslationUnit], current_size: int) -> int:
        with self.lock:
            self.batch_number += 1
            batch_number = self.batch_number
        self.write(
            f"Batch {batch_number}: start {batch[0].id}-{batch[-1].id} "
            f"({len(batch)} units, target_size={current_size})"
        )
        return batch_number

    def batch_success(self, count: int, elapsed: float, provider_name: str, batch_number: int | None = None) -> None:
        with self.lock:
            self.completed += count
            self.primary_success += count
            batch_number = batch_number or self.batch_number
        self.write_progress(f"Batch {batch_number}: success {count} units via {provider_name} in {elapsed:.1f}s")

    def batch_failure(
        self,
        reason: str,
        elapsed: float,
        next_size: int | None = None,
        batch_number: int | None = None,
    ) -> None:
        with self.lock:
            self.failures += 1
            batch_number = batch_number or self.batch_number
        suffix = f"; retry target_size={next_size}" if next_size else ""
        self.write(f"Batch {batch_number}: failed after {elapsed:.1f}s ({reason}){suffix}")

    def fallback_success_result(self, elapsed: float, provider_name: str, batch_number: int | None = None) -> None:
        with self.lock:
            self.completed += 1
            self.fallback_success += 1
            batch_number = batch_number or self.batch_number
        self.write_progress(f"Batch {batch_number}: fallback success via {provider_name} in {elapsed:.1f}s")

    def primary_single_success(self, elapsed: float, provider_name: str, batch_number: int | None = None) -> None:
        with self.lock:
            self.completed += 1
            self.primary_success += 1
            batch_number = batch_number or self.batch_number
        self.write_progress(f"Batch {batch_number}: single-unit success via {provider_name} in {elapsed:.1f}s")

    def retained_source(self, elapsed: float, batch_number: int | None = None) -> None:
        with self.lock:
            self.completed += 1
            self.retained += 1
            batch_number = batch_number or self.batch_number
        self.write_progress(f"Batch {batch_number}: source retained in {elapsed:.1f}s")

    def cached(self, count: int, first_id: str, last_id: str) -> None:
        with self.lock:
            self.completed += count
            self.cache_hits += count
        self.write_progress(f"Cache hit {first_id}-{last_id}: reused {count} units")

    def write_progress(self, prefix: str) -> None:
        with self.lock:
            completed = self.completed
            primary_success = self.primary_success
            fallback_success = self.fallback_success
            cache_hits = self.cache_hits
            retained = self.retained
            failures = self.failures
        elapsed = max(time.monotonic() - self.started_at, 0.001)
        units_per_minute = completed / elapsed * 60
        remaining = max(self.total_units - completed, 0)
        eta_seconds = remaining / (units_per_minute / 60) if units_per_minute > 0 else None
        eta_text = format_duration(eta_seconds) if eta_seconds is not None else "unknown"
        self.write(
            f"{prefix}. Progress {completed}/{self.total_units} "
            f"({units_per_minute:.1f} units/min, ETA {eta_text}, "
            f"primary={primary_success}, fallback={fallback_success}, "
            f"cache={cache_hits}, retained={retained}, failures={failures})"
        )

    def write(self, message: str) -> None:
        print(f"[sec-translator] {message}", flush=True)


def translate_html_file(
    input_path: Path,
    output_path: Path | None,
    config: AppConfig,
    *,
    overwrite: bool = False,
) -> Path:
    model_config = require_model_config(config)
    if not input_path.exists():
        raise TranslateError(f"Input HTML does not exist: {input_path}")

    explicit_output_path = output_path is not None
    output_path = output_path or default_output_path(input_path, config.output.default_suffix)
    if output_path.resolve() == input_path.resolve():
        raise TranslateError("Output file must not be the same as the input file.")

    report_paths = default_report_paths(output_path if explicit_output_path else input_path)
    planned_outputs = [output_path]
    if config.output.write_json_report:
        planned_outputs.append(report_paths["json"])
    if config.output.write_markdown_report:
        planned_outputs.append(report_paths["markdown"])
    ensure_can_write(planned_outputs, overwrite)

    html = input_path.read_text(encoding="utf-8", errors="replace")
    cache = make_translation_cache(input_path, html, config, model_config)
    soup = BeautifulSoup(html, "lxml")
    units = extract_translation_units(soup)
    report = make_report(input_path, output_path, config, units)
    progress = ProgressReporter(len(units))
    progress.start()

    primary = OpenAICompatibleProvider(model_config, config.translation)
    fallback = None
    if config.fallback_model.enabled:
        fallback = OpenAICompatibleProvider(config.fallback_model.as_model_config(), config.translation)

    results = run_translation(units, config, model_config, primary, fallback, report, progress, cache)
    report["usage"] = collect_usage(primary, fallback)
    fallback_provider_name = config.fallback_model.model if config.fallback_model.enabled else ""
    has_fallback_results = any(result.provider == fallback_provider_name for result in results)
    if has_fallback_results:
        inject_fallback_style(soup)
    for result in results:
        apply_translation(result.unit, result.text, is_fallback=result.provider == fallback_provider_name)

    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(str(soup), encoding="utf-8")

    if config.output.write_json_report:
        report_paths["json"].write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    if config.output.write_markdown_report:
        report_paths["markdown"].write_text(format_markdown_report(report), encoding="utf-8")

    return output_path


def default_output_path(input_path: Path, suffix: str) -> Path:
    return input_path.with_name(f"{input_path.stem}{suffix}{input_path.suffix or '.html'}")


def default_report_paths(input_path: Path) -> dict[str, Path]:
    return {
        "json": input_path.with_name(f"{input_path.stem}_translation_report.json"),
        "markdown": input_path.with_name(f"{input_path.stem}_translation_report.md"),
    }


def ensure_can_write(paths: list[Path], overwrite: bool) -> None:
    for path in paths:
        if path.exists() and not overwrite:
            raise TranslateError(f"Output file already exists: {path}. Add --overwrite to replace it.")
        path.parent.mkdir(parents=True, exist_ok=True)


def extract_translation_units(soup: BeautifulSoup) -> list[TranslationUnit]:
    units: list[TranslationUnit] = []
    for tag in soup.find_all(BLOCK_TAGS):
        if not isinstance(tag, Tag) or should_skip_element(tag):
            continue
        if has_nested_block_candidate(tag):
            continue
        source_text = normalize_spaces(tag.get_text(" ", strip=True))
        if should_skip_text(source_text):
            continue
        target = single_inline_text_target(tag)
        if target is not None:
            text, placeholders = target.get_text(" ", strip=True), {}
            element = target
        else:
            text, placeholders = serialize_unit_text(tag)
            element = tag
        if should_skip_text(strip_placeholder_markup(text)):
            continue
        units.append(
            TranslationUnit(
                id=f"u_{len(units) + 1:04d}",
                element=element,
                text=normalize_spaces(text),
                source_text=source_text,
                placeholders=placeholders,
            )
        )
    return units


def should_skip_element(tag: Tag) -> bool:
    if tag.name and tag.name.lower() in SKIP_TAGS:
        return True
    if is_display_none(tag):
        return True
    for parent in tag.parents:
        name = str(getattr(parent, "name", "")).lower()
        if name in SKIP_TAGS or name in {"ix:header", "ix:hidden"}:
            return True
        if isinstance(parent, Tag) and is_display_none(parent):
            return True
    name = str(tag.name).lower()
    return name.startswith("ix:hidden") or name.startswith("ix:header")


def is_display_none(tag: Tag) -> bool:
    style = str(tag.get("style", "")).replace(" ", "").lower()
    return "display:none" in style


def has_nested_block_candidate(tag: Tag) -> bool:
    for child in tag.find_all(BLOCK_TAGS):
        if child is not tag:
            return True
    return False


def serialize_unit_text(tag: Tag) -> tuple[str, dict[str, Tag]]:
    chunks: list[str] = []
    placeholders: dict[str, Tag] = {}
    index = 0
    for child in tag.contents:
        if isinstance(child, NavigableString):
            chunks.append(str(child))
            continue
        if isinstance(child, Tag):
            if child.name and child.name.lower() in BLOCK_TAGS:
                continue
            placeholder = f"PH_{index}"
            placeholders[placeholder] = child
            chunks.append(f"<{placeholder}>{child.get_text(' ', strip=True)}</{placeholder}>")
            index += 1
    return "".join(chunks), placeholders


def single_inline_text_target(tag: Tag) -> Tag | None:
    original = tag
    current = tag
    while True:
        meaningful = [
            child
            for child in current.contents
            if not (isinstance(child, NavigableString) and not str(child).strip())
        ]
        if current is not original and meaningful and all(isinstance(child, NavigableString) for child in meaningful):
            return current
        if len(meaningful) != 1 or not isinstance(meaningful[0], Tag):
            return None
        child = meaningful[0]
        name = str(child.name).lower()
        if name in BLOCK_TAGS or name in SKIP_TAGS:
            return None
        current = child


def should_skip_text(text: str) -> bool:
    text = normalize_spaces(text)
    if not text:
        return True
    lowered = text.lower()
    if lowered in PROTECTED_TERMS:
        return True
    if re.fullmatch(r"[\d\s,.$()%/\-–—]+", text):
        return True
    if re.fullmatch(r"[A-Z]{1,6}", text):
        return True
    if re.fullmatch(r"[A-Z]{1,4}-?\d{1,4}", text):
        return True
    if re.fullmatch(r"[Ff]-?\d+", text):
        return True
    if re.fullmatch(r"https?://\S+|www\.\S+", text):
        return True
    if re.fullmatch(r"[^@\s]+@[^@\s]+\.[^@\s]+", text):
        return True
    return re.search(r"[A-Za-z]", text) is None


def run_translation(
    units: list[TranslationUnit],
    config: AppConfig,
    model_config: ModelConfig,
    primary: Any,
    fallback: Any | None,
    report: dict[str, Any],
    progress: ProgressReporter,
    cache: TranslationCache | None = None,
) -> list[UnitResult]:
    if config.block.enabled:
        return run_block_translation(units, config, model_config, primary, fallback, report, progress, cache)
    return run_linear_translation(units, config, model_config, primary, fallback, report, progress, cache)


def run_linear_translation(
    units: list[TranslationUnit],
    config: AppConfig,
    model_config: ModelConfig,
    primary: Any,
    fallback: Any | None,
    report: dict[str, Any],
    progress: ProgressReporter,
    cache: TranslationCache | None = None,
) -> list[UnitResult]:
    results: list[UnitResult] = []
    index = 0
    current_size = max(config.batch.min_units, min(config.batch.initial_units, config.batch.max_units))
    stable_successes = 0

    while index < len(units):
        cached_results: list[UnitResult] = []
        while cache is not None and index < len(units):
            cached = cache.get(units[index])
            if cached is None:
                break
            cached_results.append(UnitResult(unit=units[index], text=cached.text, provider=cached.provider))
            index += 1
        if cached_results:
            results.extend(cached_results)
            report["summary"]["cache_hits"] += len(cached_results)
            record_cached_results(report, cached_results)
            progress.cached(len(cached_results), cached_results[0].unit.id, cached_results[-1].unit.id)
            if index >= len(units):
                break

        batch = choose_batch(units, index, current_size, config.batch.max_chars_per_batch)
        if cache is not None:
            batch = trim_batch_before_cached_unit(batch, cache)
        progress_batch_number = progress.batch_start(batch, current_size)
        batch_started = time.monotonic()
        context_before = make_context(units[:index], config.batch.prev_context_chars, from_end=True)
        context_after = make_context(units[index + len(batch) :], config.batch.next_context_chars, from_end=False)
        try:
            translated = request_and_validate_with_format_retry(primary, batch, context_before, context_after)
        except TranslateError as exc:
            primary_reason = str(exc)
            add_failure(report, primary_reason)
            if len(batch) > 1:
                current_size = shrink_batch_size(current_size, config.batch.min_units, config.batch.shrink_factor)
                stable_successes = 0
                progress.batch_failure(
                    primary_reason,
                    time.monotonic() - batch_started,
                    current_size,
                    progress_batch_number,
                )
                continue
            result = translate_single_with_fallback(
                batch[0],
                primary,
                fallback,
                report,
                progress,
                primary_reason=primary_reason,
                started_at=batch_started,
                context_before=context_before,
                context_after=context_after,
                cache=cache,
                progress_batch_number=progress_batch_number,
            )
            results.append(result)
            index += 1
            continue

        if cache is not None:
            for unit in batch:
                cache.put(unit, translated[unit.id], model_config.model)
        results.extend(UnitResult(unit=unit, text=translated[unit.id], provider=model_config.model) for unit in batch)
        report["summary"]["primary_success"] += len(batch)
        index += len(batch)
        stable_successes += 1
        progress.batch_success(
            len(batch),
            time.monotonic() - batch_started,
            model_config.model,
            progress_batch_number,
        )
        if stable_successes >= config.batch.stable_successes_to_grow:
            current_size = grow_batch_size(current_size, config.batch.max_units, config.batch.grow_factor)
            stable_successes = 0

    report["summary"]["source_retained"] = len(report["retained_units"])
    report["summary"]["fallback_success"] = len(report["fallback_units"])
    return results


def run_block_translation(
    units: list[TranslationUnit],
    config: AppConfig,
    model_config: ModelConfig,
    primary: Any,
    fallback: Any | None,
    report: dict[str, Any],
    progress: ProgressReporter,
    cache: TranslationCache | None = None,
) -> list[UnitResult]:
    results: list[UnitResult] = []
    if cache is not None:
        cached_results: list[UnitResult] = []
        pending_units: list[TranslationUnit] = []
        for unit in units:
            cached = cache.get(unit)
            if cached is None:
                pending_units.append(unit)
                continue
            cached_results.append(UnitResult(unit=unit, text=cached.text, provider=cached.provider))
        if cached_results:
            results.extend(cached_results)
            report["summary"]["cache_hits"] += len(cached_results)
            record_cached_results(report, cached_results)
            progress.cached(len(cached_results), cached_results[0].unit.id, cached_results[-1].unit.id)
        units = pending_units
        if not units:
            report["summary"]["source_retained"] = len(report["retained_units"])
            report["summary"]["fallback_success"] = len(report["fallback_units"])
            return results

    index = 0
    block_number = 0
    report_lock = threading.Lock()
    current_max_batches = config.block.max_batches_per_block
    stable_blocks = 0
    pending_fallbacks: list[PendingFallback] = []

    while index < len(units):
        block_number += 1
        block = plan_block(units, index, block_number, config, model_config, cache, current_max_batches)
        if not block.batches:
            fallback_results = run_linear_translation(
                [units[index]],
                config,
                model_config,
                primary,
                fallback,
                report,
                progress,
                cache,
            )
            results.extend(fallback_results)
            index += 1
            continue

        progress.write(
            f"Block {block.id}: {len(block.batches)} batches, "
            f"{len(block.units)} units, workers={min(config.block.max_workers, len(block.batches))}, "
            f"estimated_input={estimate_tokens(SYSTEM_PROMPT + block.common_prompt)} tokens"
        )
        block_units_count = len(block.units)

        block_results, context_limited, block_pending_fallbacks = translate_block_batches(
            block,
            config,
            model_config,
            primary,
            fallback,
            report,
            progress,
            cache,
            report_lock,
        )
        if context_limited:
            smaller = shrink_batch_size(current_max_batches, 1, config.batch.shrink_factor)
            if smaller < current_max_batches:
                current_max_batches = smaller
                stable_blocks = 0
                progress.write(
                    f"Block {block.id}: context limit reached; shrinking block to "
                    f"{current_max_batches} batches and retrying"
                )
                continue
            progress.write(f"Block {block.id}: context limit reached; retrying block with linear batches")
            fallback_results = run_linear_translation(
                block.units,
                config,
                model_config,
                primary,
                fallback,
                report,
                progress,
                cache,
            )
            results.extend(fallback_results)
            index += block_units_count
            continue

        results.extend(block_results)
        pending_fallbacks.extend(block_pending_fallbacks)
        index += block_units_count
        stable_blocks += 1
        if stable_blocks >= config.batch.stable_successes_to_grow:
            current_max_batches = grow_batch_size(
                current_max_batches,
                config.block.max_batches_per_block,
                config.batch.grow_factor,
            )
            stable_blocks = 0

    if pending_fallbacks:
        progress.write(f"Deferred fallback: processing {len(pending_fallbacks)} units")
        results.extend(
            process_deferred_fallbacks(
                pending_fallbacks,
                primary,
                fallback,
                report,
                progress,
                cache,
                report_lock,
            )
        )

    report["summary"]["source_retained"] = len(report["retained_units"])
    report["summary"]["fallback_success"] = len(report["fallback_units"])
    return results


def translate_block_batches(
    block: BlockPlan,
    config: AppConfig,
    model_config: ModelConfig,
    primary: Any,
    fallback: Any | None,
    report: dict[str, Any],
    progress: ProgressReporter,
    cache: TranslationCache | None,
    report_lock: threading.Lock,
) -> tuple[list[UnitResult], bool, list[PendingFallback]]:
    if config.block.max_workers <= 1 or len(block.batches) <= 1:
        results: list[UnitResult] = []
        pending_fallbacks: list[PendingFallback] = []
        for batch in block.batches:
            batch_index = block.batches.index(batch)
            outcome = translate_block_batch_with_retries(
                batch.units,
                batch.id,
                block.common_prompt,
                build_mini_block_prompt(block, batch_index, config),
                config,
                model_config,
                primary,
                fallback,
                report,
                progress,
                cache,
                report_lock,
            )
            if outcome.context_limited:
                return results, True, pending_fallbacks
            results.extend(outcome.results)
            pending_fallbacks.extend(outcome.pending_fallbacks)
        return results, False, pending_fallbacks

    results_by_id: dict[str, list[UnitResult]] = {}
    pending_fallbacks: list[PendingFallback] = []
    context_limited = False
    parallel_batches = block.batches
    if config.block.warmup_first:
        prime_block_cache(primary, block.common_prompt, block.id, config, report, progress, report_lock)
        warmup_batch = block.batches[0]
        progress.write(f"Block {block.id}: warmup {warmup_batch.id} before parallel requests")
        warmup_outcome = translate_block_batch_with_retries(
            warmup_batch.units,
            warmup_batch.id,
            block.common_prompt,
            build_mini_block_prompt(block, 0, config),
            config,
            model_config,
            primary,
            fallback,
            report,
            progress,
            cache,
            report_lock,
        )
        if warmup_outcome.context_limited:
            return warmup_outcome.results, True, warmup_outcome.pending_fallbacks
        results_by_id[warmup_batch.id] = warmup_outcome.results
        pending_fallbacks.extend(warmup_outcome.pending_fallbacks)
        parallel_batches = block.batches[1:]

    if not parallel_batches:
        ordered_results: list[UnitResult] = []
        for batch in block.batches:
            ordered_results.extend(results_by_id.get(batch.id, []))
        return ordered_results, False, pending_fallbacks

    max_workers = min(config.block.max_workers, len(parallel_batches))
    with ThreadPoolExecutor(max_workers=max_workers) as executor:
        futures = {
            executor.submit(
                translate_block_batch_with_retries,
                batch.units,
                batch.id,
                block.common_prompt,
                build_mini_block_prompt(block, block.batches.index(batch), config),
                config,
                model_config,
                primary,
                fallback,
                report,
                progress,
                cache,
                report_lock,
            ): batch
            for batch in parallel_batches
        }
        for future in as_completed(futures):
            batch = futures[future]
            outcome = future.result()
            if outcome.context_limited:
                context_limited = True
                continue
            results_by_id[batch.id] = outcome.results
            pending_fallbacks.extend(outcome.pending_fallbacks)

    ordered_results: list[UnitResult] = []
    for batch in block.batches:
        ordered_results.extend(results_by_id.get(batch.id, []))
    return ordered_results, context_limited, pending_fallbacks


def prime_block_cache(
    primary: Any,
    block_prompt: str,
    block_id: str,
    config: AppConfig,
    report: dict[str, Any],
    progress: ProgressReporter,
    report_lock: threading.Lock,
) -> None:
    primer = getattr(primary, "prime_block_cache", None)
    if not callable(primer):
        return
    started_at = time.monotonic()
    progress.write(f"Block {block_id}: priming shared prompt cache")
    try:
        primer(block_prompt)
    except Exception as exc:
        add_failure(report, f"cache_warmup_failed: {exc}", report_lock)
        progress.write(f"Block {block_id}: cache warmup failed after {time.monotonic() - started_at:.1f}s ({exc})")
        return
    if config.block.warmup_delay_seconds:
        time.sleep(config.block.warmup_delay_seconds)
    progress.write(f"Block {block_id}: shared prompt cache primed in {time.monotonic() - started_at:.1f}s")


def translate_block_batch_with_retries(
    units: list[TranslationUnit],
    batch_id: str,
    block_prompt: str,
    mini_block_prompt: str,
    config: AppConfig,
    model_config: ModelConfig,
    primary: Any,
    fallback: Any | None,
    report: dict[str, Any],
    progress: ProgressReporter,
    cache: TranslationCache | None,
    report_lock: threading.Lock | None = None,
) -> BlockBatchResult:
    progress_batch_number = progress.batch_start(units, len(units))
    batch_started = time.monotonic()
    try:
        translated = request_and_validate_with_format_retry(
            primary,
            units,
            block_prompt=block_prompt,
            batch_id=batch_id,
        )
    except TranslateError as exc:
        reason = str(exc)
        previous_response = getattr(exc, "response", "")
        add_failure(report, reason, report_lock)
        progress.batch_failure(reason, time.monotonic() - batch_started, batch_number=progress_batch_number)
        if is_context_limit_error(reason):
            return BlockBatchResult(context_limited=True)
        if is_repairable_error(reason):
            repair_result = try_repair_block_batch(
                units,
                batch_id,
                block_prompt,
                reason,
                previous_response,
                model_config,
                primary,
                report,
                progress,
                cache,
                report_lock,
                progress_batch_number,
            )
            if repair_result is not None:
                return repair_result
        if len(units) > 1:
            retry_size = shrink_batch_size(len(units), config.batch.min_units, config.batch.shrink_factor)
            outcome = BlockBatchResult()
            index = 0
            while index < len(units):
                retry_units = units[index : index + retry_size]
                retry_results = translate_block_batch_with_retries(
                    retry_units,
                    batch_id,
                    block_prompt,
                    mini_block_prompt,
                    config,
                    model_config,
                    primary,
                    fallback,
                    report,
                    progress,
                    cache,
                    report_lock,
                )
                if retry_results.context_limited:
                    return BlockBatchResult(context_limited=True)
                outcome.results.extend(retry_results.results)
                outcome.pending_fallbacks.extend(retry_results.pending_fallbacks)
                index += len(retry_units)
            return outcome
        if is_repairable_error(reason) and mini_block_prompt and mini_block_prompt != block_prompt:
            mini_result = try_mini_block_retry(
                units,
                batch_id,
                mini_block_prompt,
                reason,
                model_config,
                primary,
                report,
                progress,
                cache,
                report_lock,
                progress_batch_number,
            )
            if mini_result is not None:
                return mini_result
        if fallback is not None:
            return BlockBatchResult(pending_fallbacks=[PendingFallback(unit=units[0], primary_reason=reason)])
        single_result = translate_single_with_fallback(
            units[0],
            primary,
            fallback,
            report,
            progress,
            primary_reason=reason,
            started_at=batch_started,
            cache=cache,
            report_lock=report_lock,
            progress_batch_number=progress_batch_number,
        )
        return BlockBatchResult(results=[single_result])

    if cache is not None:
        for unit in units:
            cache.put(unit, translated[unit.id], model_config.model)
    increment_summary(report, "primary_success", len(units), report_lock)
    progress.batch_success(
        len(units),
        time.monotonic() - batch_started,
        model_config.model,
        progress_batch_number,
    )
    return BlockBatchResult(
        results=[UnitResult(unit=unit, text=translated[unit.id], provider=model_config.model) for unit in units]
    )


def try_repair_block_batch(
    units: list[TranslationUnit],
    batch_id: str,
    block_prompt: str,
    reason: str,
    previous_response: str,
    model_config: ModelConfig,
    primary: Any,
    report: dict[str, Any],
    progress: ProgressReporter,
    cache: TranslationCache | None,
    report_lock: threading.Lock | None,
    progress_batch_number: int | None,
) -> BlockBatchResult | None:
    started_at = time.monotonic()
    try:
        translated = request_and_validate(
            primary,
            units,
            block_prompt=block_prompt,
            batch_id=batch_id,
            strict_json=True,
            repair_reason=reason,
            previous_response=previous_response,
        )
    except TranslateError as exc:
        add_failure(report, f"repair_failed: {exc}", report_lock)
        return None
    if cache is not None:
        for unit in units:
            cache.put(unit, translated[unit.id], model_config.model)
    increment_summary(report, "primary_success", len(units), report_lock)
    increment_summary(report, "repair_success", len(units), report_lock)
    for unit in units:
        append_report_item(
            report,
            "repaired_units",
            {"id": unit.id, "source": clip(unit.source_text), "initial_failure": reason},
            report_lock,
        )
    progress.batch_success(len(units), time.monotonic() - started_at, model_config.model, progress_batch_number)
    return BlockBatchResult(
        results=[UnitResult(unit=unit, text=translated[unit.id], provider=model_config.model) for unit in units]
    )


def try_mini_block_retry(
    units: list[TranslationUnit],
    batch_id: str,
    mini_block_prompt: str,
    reason: str,
    model_config: ModelConfig,
    primary: Any,
    report: dict[str, Any],
    progress: ProgressReporter,
    cache: TranslationCache | None,
    report_lock: threading.Lock | None,
    progress_batch_number: int | None,
) -> BlockBatchResult | None:
    started_at = time.monotonic()
    try:
        translated = request_and_validate_with_format_retry(
            primary,
            units,
            block_prompt=mini_block_prompt,
            batch_id=batch_id,
        )
    except TranslateError as exc:
        add_failure(report, f"mini_block_failed: {exc}", report_lock)
        return None
    if cache is not None:
        for unit in units:
            cache.put(unit, translated[unit.id], model_config.model)
    increment_summary(report, "primary_success", len(units), report_lock)
    increment_summary(report, "mini_block_success", len(units), report_lock)
    for unit in units:
        append_report_item(
            report,
            "mini_block_units",
            {"id": unit.id, "source": clip(unit.source_text), "initial_failure": reason},
            report_lock,
        )
    progress.batch_success(len(units), time.monotonic() - started_at, model_config.model, progress_batch_number)
    return BlockBatchResult(
        results=[UnitResult(unit=unit, text=translated[unit.id], provider=model_config.model) for unit in units]
    )


def process_deferred_fallbacks(
    pending_fallbacks: list[PendingFallback],
    primary: Any,
    fallback: Any | None,
    report: dict[str, Any],
    progress: ProgressReporter,
    cache: TranslationCache | None,
    report_lock: threading.Lock,
) -> list[UnitResult]:
    results: list[UnitResult] = []
    for pending in pending_fallbacks:
        results.append(
            translate_single_with_fallback(
                pending.unit,
                primary,
                fallback,
                report,
                progress,
                primary_reason=pending.primary_reason,
                started_at=time.monotonic(),
                cache=cache,
                report_lock=report_lock,
            )
        )
    return results


def plan_block(
    units: list[TranslationUnit],
    start: int,
    block_number: int,
    config: AppConfig,
    model_config: ModelConfig,
    cache: TranslationCache | None,
    max_batches_per_block: int | None = None,
) -> BlockPlan:
    input_budget = block_input_token_budget(config, model_config)
    ratio_total = (
        config.block.before_context_ratio
        + config.block.block_body_ratio
        + config.block.after_context_ratio
    )
    before_budget = int(input_budget * config.block.before_context_ratio / ratio_total)
    body_budget = max(1, int(input_budget * config.block.block_body_ratio / ratio_total))
    after_budget = int(input_budget * config.block.after_context_ratio / ratio_total)

    batches: list[BatchPlan] = []
    cursor = start
    target_units = max(1, min(config.block.target_batch_units, config.batch.max_units))
    max_batches = max_batches_per_block or config.block.max_batches_per_block
    while cursor < len(units) and len(batches) < max_batches:
        if cache is not None and cache.get(units[cursor]) is not None:
            break
        batch_units = choose_batch(units, cursor, target_units, config.block.max_batch_chars)
        if cache is not None:
            batch_units = trim_batch_before_cached_unit(batch_units, cache)
        candidate = batches + [BatchPlan(id=f"batch_{len(batches) + 1:03d}", units=batch_units)]
        body_tokens = estimate_tokens(build_batches_json(candidate))
        if batches and body_tokens > body_budget:
            break
        batches = candidate
        cursor += len(batch_units)

    block_end = start + sum(len(batch.units) for batch in batches)
    context_before = make_context(units[:start], before_budget * 2, from_end=True)
    context_after = make_context(units[block_end:], after_budget * 2, from_end=False)
    common_prompt = build_block_common_prompt(
        build_batches_json(batches),
        target_language=config.translation.target_language,
        context_before=context_before,
        context_after=context_after,
    )
    return BlockPlan(
        id=f"block_{block_number:03d}",
        batches=batches,
        context_before=context_before,
        context_after=context_after,
        common_prompt=common_prompt,
    )


def build_mini_block_prompt(block: BlockPlan, batch_index: int, config: AppConfig) -> str:
    start = max(0, batch_index - 2)
    end = min(len(block.batches), batch_index + 3)
    return build_block_common_prompt(
        build_batches_json(block.batches[start:end]),
        target_language=config.translation.target_language,
        context_before="",
        context_after="",
    )


def block_input_token_budget(config: AppConfig, model_config: ModelConfig) -> int:
    context_window = max(model_config.context_window_tokens, 1024)
    max_input = int(context_window * config.block.max_input_ratio)
    hard_limit = context_window - model_config.max_output_tokens
    prompt_tokens = estimate_tokens(SYSTEM_PROMPT)
    return max(64, min(max_input, hard_limit) - prompt_tokens)


def is_context_limit_error(reason: str) -> bool:
    lowered = reason.lower()
    return any(
        marker in lowered
        for marker in [
            "context length",
            "context_length",
            "maximum context",
            "context window",
            "too many tokens",
            "token limit",
        ]
    )


def is_repairable_error(reason: str) -> bool:
    key = reason.split(":", 1)[0]
    return key in {
        "model_response_invalid",
        "id_validation_failed",
        "count_validation_failed",
        "empty_translation",
        "placeholder_validation_failed",
        "number_validation_failed",
        "replacement_character_validation_failed",
    }


def build_batches_json(batches: list[BatchPlan]) -> str:
    payload = [
        {
            "batch_id": batch.id,
            "items": [{"id": unit.id, "text": unit.text} for unit in batch.units],
        }
        for batch in batches
    ]
    return json.dumps(payload, ensure_ascii=False)


def estimate_tokens(text: str) -> int:
    return max(1, (len(text) + 1) // 2)


def trim_batch_before_cached_unit(batch: list[TranslationUnit], cache: TranslationCache) -> list[TranslationUnit]:
    trimmed: list[TranslationUnit] = []
    for unit in batch:
        if cache.get(unit) is not None:
            break
        trimmed.append(unit)
    return trimmed or batch[:1]


def record_cached_results(report: dict[str, Any], cached_results: list[UnitResult]) -> None:
    for result in cached_results:
        if result.provider != "source":
            continue
        if any(item["id"] == result.unit.id for item in report["retained_units"]):
            continue
        report["retained_units"].append(
            {
                "id": result.unit.id,
                "source": clip(result.unit.source_text),
                "failure_reason": "cached_source_retained",
            }
        )


def translate_single_with_fallback(
    unit: TranslationUnit,
    primary: Any,
    fallback: Any | None,
    report: dict[str, Any],
    progress: ProgressReporter,
    primary_reason: str = "",
    started_at: float | None = None,
    context_before: str = "",
    context_after: str = "",
    cache: TranslationCache | None = None,
    report_lock: threading.Lock | None = None,
    progress_batch_number: int | None = None,
) -> UnitResult:
    started_at = started_at or time.monotonic()
    if not primary_reason:
        try:
            translated = request_and_validate_with_format_retry(primary, [unit], context_before, context_after)
            provider_name = getattr(primary.config, "model", "primary")
            if cache is not None:
                cache.put(unit, translated[unit.id], provider_name)
            increment_summary(report, "primary_success", 1, report_lock)
            progress.primary_single_success(time.monotonic() - started_at, provider_name, progress_batch_number)
            return UnitResult(unit=unit, text=translated[unit.id], provider=provider_name)
        except TranslateError as exc:
            primary_reason = str(exc)
            add_failure(report, primary_reason, report_lock)

    if fallback is not None:
        try:
            translated = request_and_validate(fallback, [unit], context_before, context_after)
            provider_name = getattr(fallback.config, "model", "fallback")
            if cache is not None:
                cache.put(unit, translated[unit.id], provider_name)
            append_report_item(
                report,
                "fallback_units",
                {
                    "id": unit.id,
                    "source": clip(unit.source_text),
                    "primary_failure": primary_reason,
                    "fallback_model": provider_name,
                },
                report_lock,
            )
            progress.fallback_success_result(time.monotonic() - started_at, provider_name, progress_batch_number)
            return UnitResult(unit=unit, text=translated[unit.id], provider=provider_name)
        except TranslateError as exc:
            add_failure(report, str(exc), report_lock)

    append_report_item(
        report,
        "retained_units",
        {
            "id": unit.id,
            "source": clip(unit.source_text),
            "failure_reason": primary_reason or "translation_failed",
        },
        report_lock,
    )
    if cache is not None:
        cache.put(unit, unit.text, "source")
    progress.retained_source(time.monotonic() - started_at, progress_batch_number)
    return UnitResult(unit=unit, text=unit.text, provider="source")


def choose_batch(
    units: list[TranslationUnit],
    start: int,
    max_units: int,
    max_chars: int,
) -> list[TranslationUnit]:
    batch: list[TranslationUnit] = []
    chars = 0
    for unit in units[start : start + max_units]:
        projected = chars + len(unit.text)
        if batch and projected > max_chars:
            break
        batch.append(unit)
        chars = projected
    return batch


def shrink_batch_size(current_size: int, min_units: int, shrink_factor: float) -> int:
    smaller = max(min_units, int(current_size * shrink_factor))
    if smaller >= current_size:
        smaller = max(min_units, current_size - 1)
    return smaller


def grow_batch_size(current_size: int, max_units: int, grow_factor: float) -> int:
    bigger = min(max_units, max(current_size + 1, int(current_size * grow_factor)))
    return max(current_size, bigger)


def request_and_validate(
    provider: Any,
    units: list[TranslationUnit],
    context_before: str = "",
    context_after: str = "",
    block_prompt: str = "",
    batch_id: str = "",
    strict_json: bool = False,
    repair_reason: str = "",
    previous_response: str = "",
) -> dict[str, str]:
    try:
        kwargs = {
            "context_before": context_before,
            "context_after": context_after,
            "block_prompt": block_prompt,
            "batch_id": batch_id,
            "strict_json": strict_json,
        }
        if repair_reason or previous_response:
            kwargs["repair_reason"] = repair_reason
            kwargs["previous_response"] = previous_response
        response = provider.translate(units, **kwargs)
    except Exception as exc:
        raise TranslateError(f"provider_error: {exc}") from exc
    try:
        translations = parse_translation_response(response)
        validate_translations(units, translations)
    except TranslateError as exc:
        raise TranslateAttemptError(str(exc), response) from exc
    return {item["id"]: item["text"] for item in translations}


def request_and_validate_with_format_retry(
    provider: Any,
    units: list[TranslationUnit],
    context_before: str = "",
    context_after: str = "",
    block_prompt: str = "",
    batch_id: str = "",
) -> dict[str, str]:
    try:
        return request_and_validate(provider, units, context_before, context_after, block_prompt, batch_id)
    except TranslateError as exc:
        if not is_format_validation_error(str(exc)):
            raise
        return request_and_validate(
            provider,
            units,
            context_before,
            context_after,
            block_prompt,
            batch_id,
            strict_json=True,
        )


def is_format_validation_error(reason: str) -> bool:
    key = reason.split(":", 1)[0]
    return key in {"model_response_invalid", "id_validation_failed", "count_validation_failed", "empty_translation"}


def make_context(units: list[TranslationUnit], max_chars: int, *, from_end: bool) -> str:
    if max_chars <= 0 or not units:
        return ""
    selected: list[str] = []
    total = 0
    iterable = reversed(units) if from_end else iter(units)
    for unit in iterable:
        line = f"{unit.id}: {unit.source_text}"
        projected = total + len(line) + 1
        if selected and projected > max_chars:
            break
        selected.append(line)
        total = projected
    if from_end:
        selected.reverse()
    return "\n".join(selected)


def parse_translation_response(response: str) -> list[dict[str, str]]:
    if looks_like_refusal(response):
        raise TranslateError("model_refusal")
    payload = extract_json_payload(response)
    try:
        data = json.loads(payload)
    except json.JSONDecodeError as exc:
        raise TranslateError("model_response_invalid") from exc
    if isinstance(data, dict) and isinstance(data.get("translations"), list):
        data = data["translations"]
    elif isinstance(data, dict) and "id" in data and "text" in data:
        data = [data]
    if not isinstance(data, list):
        raise TranslateError("model_response_invalid")
    translations: list[dict[str, str]] = []
    for item in data:
        if not isinstance(item, dict) or "id" not in item or "text" not in item:
            raise TranslateError("model_response_invalid")
        translations.append({"id": str(item["id"]), "text": str(item["text"])})
    return translations


def extract_json_payload(response: str) -> str:
    text = response.strip()
    if text.startswith("```"):
        text = re.sub(r"^```(?:json)?", "", text, flags=re.IGNORECASE).strip()
        text = re.sub(r"```$", "", text).strip()
    start_candidates = [pos for pos in (text.find("["), text.find("{")) if pos >= 0]
    if not start_candidates:
        return text
    start = min(start_candidates)
    end = max(text.rfind("]"), text.rfind("}"))
    if end > start:
        return text[start : end + 1]
    return text


def validate_translations(units: list[TranslationUnit], translations: list[dict[str, str]]) -> None:
    expected_ids = [unit.id for unit in units]
    actual_ids = [item["id"] for item in translations]
    if actual_ids != expected_ids:
        raise TranslateError("id_validation_failed")
    if len(translations) != len(units):
        raise TranslateError("count_validation_failed")
    for unit, item in zip(units, translations):
        text = item["text"].strip()
        if not text:
            raise TranslateError("empty_translation")
        if looks_like_refusal(text):
            raise TranslateError("model_refusal")
        if contains_replacement_character(text):
            raise TranslateError("replacement_character_validation_failed")
        if Counter(placeholder_names(text)) != Counter(unit.placeholders.keys()):
            raise TranslateError("placeholder_validation_failed")
        if not numbers_preserved(unit.text, text):
            raise TranslateError("number_validation_failed")


def contains_replacement_character(text: str) -> bool:
    return "\ufffd" in text


def placeholder_names(text: str) -> list[str]:
    return re.findall(r"<(PH_\d+)>.*?</\1>", text, flags=re.DOTALL)


def numbers_preserved(source: str, translated: str) -> bool:
    source_numbers = normalized_numbers(strip_placeholder_markup(source))
    translated_numbers = normalized_numbers(strip_placeholder_markup(translated))
    remaining = list(translated_numbers)
    for number in source_numbers:
        if number not in remaining:
            return False
        remaining.remove(number)
    return True


def normalized_numbers(text: str) -> list[str]:
    values = []
    for raw in re.findall(r"\d[\d,]*(?:\.\d+)?", text):
        values.append(raw.replace(",", ""))
    return values


def looks_like_refusal(text: str) -> bool:
    lowered = text.lower()
    return any(re.search(pattern, lowered) for pattern in REFUSAL_PATTERNS)


FALLBACK_TRANSLATION_CLASS = "sec-translator-fallback"
FALLBACK_TRANSLATION_CSS = (
    ".sec-translator-fallback{background:#fff3bf;"
    "box-shadow:inset 0 -2px 0 #f08c00;}"
)


def inject_fallback_style(soup: BeautifulSoup) -> None:
    if soup.find("style", attrs={"data-sec-translator": "fallback"}):
        return
    style = soup.new_tag("style")
    style["data-sec-translator"] = "fallback"
    style.string = FALLBACK_TRANSLATION_CSS
    head = soup.head
    if head is None:
        head = soup.new_tag("head")
        if soup.html is not None:
            soup.html.insert(0, head)
        else:
            soup.insert(0, head)
    head.append(style)


def apply_translation(unit: TranslationUnit, translated_text: str, *, is_fallback: bool = False) -> None:
    if not unit.placeholders:
        unit.element.clear()
        append_translation_fragment(unit.element, NavigableString(translated_text), is_fallback)
        return

    unit.element.clear()
    fragments: list[NavigableString | Tag] = []
    position = 0
    pattern = re.compile(r"<(PH_\d+)>(.*?)</\1>", flags=re.DOTALL)
    for match in pattern.finditer(translated_text):
        if match.start() > position:
            fragments.append(NavigableString(translated_text[position : match.start()]))
        name = match.group(1)
        inner = match.group(2)
        original = unit.placeholders[name]
        clone = copy.copy(original)
        clone.clear()
        clone.append(NavigableString(inner))
        fragments.append(clone)
        position = match.end()
    if position < len(translated_text):
        fragments.append(NavigableString(translated_text[position:]))
    append_translation_fragment(unit.element, fragments, is_fallback)


def append_translation_fragment(parent: Tag, fragment: NavigableString | Tag | list[NavigableString | Tag], is_fallback: bool) -> None:
    fragments = fragment if isinstance(fragment, list) else [fragment]
    if not is_fallback:
        for item in fragments:
            parent.append(item)
        return
    wrapper = BeautifulSoup("", "html.parser").new_tag(
        "span",
        attrs={"class": FALLBACK_TRANSLATION_CLASS, "title": "fallback model translation"},
    )
    for item in fragments:
        wrapper.append(item)
    parent.append(wrapper)


def strip_placeholder_markup(text: str) -> str:
    return re.sub(r"</?PH_\d+>", "", text)


def normalize_spaces(text: str) -> str:
    return re.sub(r"\s+", " ", text).strip()


def increment_summary(
    report: dict[str, Any],
    key: str,
    amount: int,
    report_lock: threading.Lock | None = None,
) -> None:
    if report_lock is None:
        report["summary"][key] += amount
        return
    with report_lock:
        report["summary"][key] += amount


def append_report_item(
    report: dict[str, Any],
    key: str,
    item: dict[str, Any],
    report_lock: threading.Lock | None = None,
) -> None:
    if report_lock is None:
        report[key].append(item)
        return
    with report_lock:
        report[key].append(item)


def add_failure(report: dict[str, Any], reason: str, report_lock: threading.Lock | None = None) -> None:
    key = reason.split(":", 1)[0]
    if report_lock is None:
        report["summary"]["failure_reasons"][key] = report["summary"]["failure_reasons"].get(key, 0) + 1
        return
    with report_lock:
        report["summary"]["failure_reasons"][key] = report["summary"]["failure_reasons"].get(key, 0) + 1


def make_report(input_path: Path, output_path: Path, config: AppConfig, units: list[TranslationUnit]) -> dict[str, Any]:
    return {
        "input_file": str(input_path),
        "output_file": str(output_path),
        "model": model_summary(config.model),
        "fallback_model": fallback_summary(config),
        "summary": {
            "total_units": len(units),
            "cache_hits": 0,
            "primary_success": 0,
            "repair_success": 0,
            "mini_block_success": 0,
            "fallback_success": 0,
            "source_retained": 0,
            "failure_reasons": {},
        },
        "usage": {},
        "fallback_units": [],
        "repaired_units": [],
        "mini_block_units": [],
        "retained_units": [],
        "validation_warnings": [],
    }


def collect_usage(primary: Any, fallback: Any | None) -> dict[str, Any]:
    usage: dict[str, Any] = {}
    primary_usage = provider_usage(primary)
    if primary_usage:
        usage["primary"] = primary_usage
    fallback_usage = provider_usage(fallback)
    if fallback_usage:
        usage["fallback"] = fallback_usage
    return usage


def provider_usage(provider: Any | None) -> dict[str, Any]:
    if provider is None:
        return {}
    usage_summary = getattr(provider, "usage_summary", None)
    if not callable(usage_summary):
        return {}
    summary = usage_summary()
    if not isinstance(summary, dict):
        return {}
    return summary


def model_summary(model: ModelConfig | None) -> dict[str, Any] | None:
    if model is None:
        return None
    return {
        "base_url": model.base_url,
        "model": model.model,
        "temperature": model.temperature,
        "top_p": model.top_p,
        "context_window_tokens": model.context_window_tokens,
        "max_output_tokens": model.max_output_tokens,
        "api_key": mask_secret(model.api_key),
    }


def fallback_summary(config: AppConfig) -> dict[str, Any]:
    if not config.fallback_model.enabled:
        return {"enabled": False}
    return {
        "enabled": True,
        "base_url": config.fallback_model.base_url,
        "model": config.fallback_model.model,
        "temperature": config.fallback_model.temperature,
        "top_p": config.fallback_model.top_p,
        "context_window_tokens": config.fallback_model.context_window_tokens,
        "max_output_tokens": config.fallback_model.max_output_tokens,
        "api_key": mask_secret(config.fallback_model.api_key),
    }


def mask_secret(secret: str) -> str:
    if not secret:
        return ""
    if len(secret) <= 6:
        return "***"
    return f"{secret[:2]}***{secret[-2:]}"


def format_duration(seconds: float | None) -> str:
    if seconds is None:
        return "unknown"
    seconds = max(int(seconds), 0)
    hours, remainder = divmod(seconds, 3600)
    minutes, secs = divmod(remainder, 60)
    if hours:
        return f"{hours}h{minutes:02d}m"
    if minutes:
        return f"{minutes}m{secs:02d}s"
    return f"{secs}s"


def format_markdown_report(report: dict[str, Any]) -> str:
    summary = report["summary"]
    lines = [
        "# Translation Report",
        "",
        f"- Input: `{report['input_file']}`",
        f"- Output: `{report['output_file']}`",
        f"- Total units: {summary['total_units']}",
        f"- Cache hits: {summary.get('cache_hits', 0)}",
        f"- Primary success: {summary['primary_success']}",
        f"- Repair success: {summary.get('repair_success', 0)}",
        f"- Mini-block success: {summary.get('mini_block_success', 0)}",
        f"- Fallback success: {summary['fallback_success']}",
        f"- Source retained: {summary['source_retained']}",
        "",
        "## Failure Reasons",
    ]
    if summary["failure_reasons"]:
        for reason, count in sorted(summary["failure_reasons"].items()):
            lines.append(f"- {reason}: {count}")
    else:
        lines.append("- None")
    if report.get("usage"):
        lines.extend(["", "## Token Usage"])
        for name, usage in report["usage"].items():
            lines.append(
                f"- {name}: requests={usage.get('requests', 0)}, "
                f"prompt={usage.get('prompt_tokens', 0)}, "
                f"completion={usage.get('completion_tokens', 0)}, "
                f"cache_hit={usage.get('prompt_cache_hit_tokens', 0)}, "
                f"cache_miss={usage.get('prompt_cache_miss_tokens', 0)}, "
                f"hit_ratio={usage.get('prompt_cache_hit_ratio', 0)}"
            )
    if report["fallback_units"]:
        lines.extend(["", "## Fallback Units"])
        for item in report["fallback_units"]:
            lines.append(f"- {item['id']}: {item['source']}")
    if report.get("repaired_units"):
        lines.extend(["", "## Repaired Units"])
        for item in report["repaired_units"]:
            lines.append(f"- {item['id']}: {item['source']} ({item['initial_failure']})")
    if report.get("mini_block_units"):
        lines.extend(["", "## Mini-Block Units"])
        for item in report["mini_block_units"]:
            lines.append(f"- {item['id']}: {item['source']} ({item['initial_failure']})")
    if report["retained_units"]:
        lines.extend(["", "## Retained Source Units"])
        for item in report["retained_units"]:
            lines.append(f"- {item['id']}: {item['source']} ({item['failure_reason']})")
    lines.append("")
    return "\n".join(lines)


def clip(text: str, limit: int = 160) -> str:
    text = normalize_spaces(text)
    if len(text) <= limit:
        return text
    return text[: limit - 3] + "..."
