"""Provider-reported usage and monotonic latency logs; never tokenize message text."""

from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from functools import lru_cache
import json
import logging
from logging.handlers import RotatingFileHandler
import os
from pathlib import Path
import sys
from time import perf_counter
import uuid

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict

ROOT = Path(__file__).resolve().parents[1]
PRICING_SOURCE = "https://docs.sarvam.ai/api/getting-started/pricing"
PRICING_CHECKED = "2026-10-30"


class BenchmarkSettings(BaseSettings):
    BENCHMARK_ENABLED: bool = True
    BENCHMARK_LOG_TO_CONSOLE: bool = True
    BENCHMARK_LOG_PATH: str = "logs/benchmarks-{pid}.jsonl"
    SARVAM_PRICING_MODEL: str = "sarvam-105b"
    SARVAM_INPUT_PRICE_PER_1M_TOKENS: Decimal = Field(default=Decimal("15"), ge=0)
    SARVAM_CACHED_INPUT_PRICE_PER_1M_TOKENS: Decimal = Field(default=Decimal("5"), ge=0)
    SARVAM_OUTPUT_PRICE_PER_1M_TOKENS: Decimal = Field(default=Decimal("60"), ge=0)
    model_config = SettingsConfigDict(env_file=ROOT / ".env", extra="ignore", hide_input_in_errors=True)


@lru_cache(maxsize=1)
def settings():
    return BenchmarkSettings()


@lru_cache(maxsize=1)
def _logger():
    config = settings()
    logger = logging.getLogger(f"retrieva.benchmark.{os.getpid()}")
    logger.setLevel(logging.INFO)
    logger.propagate = False
    formatter = logging.Formatter("%(message)s")
    if config.BENCHMARK_LOG_TO_CONSOLE:
        handler = logging.StreamHandler(sys.stdout)
        handler.setFormatter(formatter)
        logger.addHandler(handler)
    if config.BENCHMARK_LOG_PATH:
        path = Path(config.BENCHMARK_LOG_PATH.replace("{pid}", str(os.getpid())))
        if not path.is_absolute():
            path = ROOT / path
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            handler = RotatingFileHandler(path, maxBytes=10 * 1024 * 1024, backupCount=5, encoding="utf-8")
            handler.setFormatter(formatter)
            logger.addHandler(handler)
        except OSError:
            # File permissions/disk failures must not break a customer's response.
            sys.stderr.write('{"event":"benchmark_file_unavailable"}\n')
    if not logger.handlers:
        logger.addHandler(logging.NullHandler())
    return logger


def emit_event(record):
    try:
        if settings().BENCHMARK_ENABLED:
            event = {"timestamp": datetime.now(timezone.utc).isoformat(), "pid": os.getpid(), **record}
            _logger().info(json.dumps(event, ensure_ascii=False, allow_nan=False))
    except Exception:
        # Telemetry is best effort, and never logs prompts, raw provider errors, or keys.
        pass


def _count(value):
    return value if type(value) is int and value >= 0 else None


def _amount(value):
    if isinstance(value, bool) or value is None:
        return None
    try:
        result = Decimal(str(value))
        return result if result.is_finite() and result >= 0 else None
    except (InvalidOperation, ValueError):
        return None


def provider_usage(data):
    usage = data.get("usage") if isinstance(data, dict) else None
    usage = usage if isinstance(usage, dict) else {}
    prompt_details = usage.get("prompt_tokens_details") or {}
    completion_details = usage.get("completion_tokens_details") or {}
    return {
        "prompt_tokens": _count(usage.get("prompt_tokens")),
        "completion_tokens": _count(usage.get("completion_tokens")),
        "total_tokens": _count(usage.get("total_tokens")),
        "cached_prompt_tokens": _count(prompt_details.get("cached_tokens")) if isinstance(prompt_details, dict) else None,
        "reasoning_tokens": _count(completion_details.get("reasoning_tokens")) if isinstance(completion_details, dict) else None,
    }


def sarvam_cost(model, usage):
    config = settings()
    models = {config.SARVAM_PRICING_MODEL}
    if config.SARVAM_PRICING_MODEL == "sarvam-105b":
        models.add("sarvam-105b-conversations")
    prompt, output, cached = (usage[key] for key in ("prompt_tokens", "completion_tokens", "cached_prompt_tokens"))
    if model not in models or prompt is None or output is None or (cached is not None and cached > prompt):
        return None
    # Unknown cache usage is NOT silently reported as zero provider-reported cache tokens.
    known_cached = cached if cached is not None else 0
    cost = ((prompt - known_cached) * config.SARVAM_INPUT_PRICE_PER_1M_TOKENS
            + known_cached * config.SARVAM_CACHED_INPUT_PRICE_PER_1M_TOKENS
            + output * config.SARVAM_OUTPUT_PRICE_PER_1M_TOKENS) / Decimal(1_000_000)
    return {"amount": str(cost), "currency": "INR", "kind": "estimate", "excludes_tax": True,
            "cache_assumption": "provider_reported" if cached is not None else "all_input_uncached",
            "input_rate_per_1m": str(config.SARVAM_INPUT_PRICE_PER_1M_TOKENS),
            "cached_input_rate_per_1m": str(config.SARVAM_CACHED_INPUT_PRICE_PER_1M_TOKENS),
            "output_rate_per_1m": str(config.SARVAM_OUTPUT_PRICE_PER_1M_TOKENS),
            "pricing_source": PRICING_SOURCE, "default_rates_checked": PRICING_CHECKED}


@dataclass
class BenchmarkRun:
    operation: str
    trace_id: str = field(default_factory=lambda: str(uuid.uuid4()))
    identifiers: dict = field(default_factory=dict)
    calls: list = field(default_factory=list)
    stages: dict = field(default_factory=dict)
    metrics: dict = field(default_factory=dict)
    outcome: str = "success"
    error_type: str | None = None

    @contextmanager
    def stage(self, name):
        started = perf_counter()
        try:
            yield
        finally:
            self.stages[name] = round(self.stages.get(name, 0) + (perf_counter() - started) * 1000, 3)

    def failed(self, error, outcome="error"):
        self.outcome = outcome
        self.error_type = type(error).__name__


_current_run = ContextVar("retrieva_benchmark_run", default=None)


@contextmanager
def benchmark_stage(name):
    """Attach nested search timings to the current request when one exists."""
    run = _current_run.get()
    if run is None:
        yield
    else:
        with run.stage(name):
            yield


def _totals(calls):
    providers = {}
    for call in calls:
        provider = call["provider"]
        target = providers.setdefault(provider, {"call_count": 0, "error_count": 0, "usage_missing_calls": 0, "usage_incomplete_calls": 0,
            "reported_prompt_tokens": 0, "reported_completion_tokens": 0, "reported_total_tokens": 0,
            "estimated_cost_inr": Decimal(0), "estimated_cost_calls": 0,
            "reported_cost_usd": Decimal(0), "reported_cost_calls": 0, "_token_fields": set()})
        target["call_count"] += 1
        target["error_count"] += call["status"] != "success"
        target["usage_missing_calls"] += not call["usage_reported"]
        target["usage_incomplete_calls"] += not call["usage_complete"]
        for key in ("prompt_tokens", "completion_tokens", "total_tokens"):
            value = call["usage"][key]
            if value is not None:
                target[f"reported_{key}"] += value
                target["_token_fields"].add(key)
        if call.get("estimated_cost"):
            target["estimated_cost_inr"] += Decimal(call["estimated_cost"]["amount"])
            target["estimated_cost_calls"] += 1
        if call.get("reported_cost"):
            target["reported_cost_usd"] += Decimal(call["reported_cost"]["amount"])
            target["reported_cost_calls"] += 1
    for target in providers.values():
        token_fields = target.pop("_token_fields")
        for key in ("prompt_tokens", "completion_tokens", "total_tokens"):
            if key not in token_fields:
                target[f"reported_{key}"] = None
        target["estimated_cost_inr"] = str(target["estimated_cost_inr"]) if target["estimated_cost_calls"] else None
        target["reported_cost_usd"] = str(target["reported_cost_usd"]) if target["reported_cost_calls"] else None
        # These are observed sums, not invented counts for missing/failed requests.
        target["usage_complete"] = target["usage_incomplete_calls"] == 0 and target["error_count"] == 0
    return providers


@contextmanager
def benchmark_run(operation, **identifiers):
    run = BenchmarkRun(operation=operation, identifiers=identifiers)
    token = _current_run.set(run)
    started = perf_counter()
    try:
        yield run
    except BaseException as error:
        run.failed(error, "cancelled" if type(error).__name__ == "CancelledError" else "error")
        raise
    finally:
        try:
            emit_event({"event": "benchmark_summary", "operation": run.operation, "trace_id": run.trace_id,
                **run.identifiers, "outcome": run.outcome, "error_type": run.error_type,
                "server_total_ms": round((perf_counter() - started) * 1000, 3),
                "stages_ms": run.stages, **run.metrics, "providers": _totals(run.calls),
                "latency_scope": "server_work_through_emit_not_browser_delivery" if operation == "chat_turn" else "server_work"})
        except Exception:
            pass
        finally:
            _current_run.reset(token)


def record_provider_call(*, provider, operation, model, duration_ms, data=None, status="success",
                         error_type=None, http_status=None, provider_request_id=None, **measurements):
    """Only read reported counters; no tokenizer and no text-length estimates."""
    try:
        data = data if isinstance(data, dict) else {}
        usage = provider_usage(data)
        run = _current_run.get()
        cost = sarvam_cost(model, usage) if provider == "sarvam" else None
        raw_usage = data.get("usage") if isinstance(data.get("usage"), dict) else {}
        raw_cost = _amount(raw_usage.get("cost")) if provider == "openrouter" else None
        record = {"event": "provider_call", "provider": provider, "operation": operation,
            "trace_id": run.trace_id if run else None, **(run.identifiers if run else {}),
            "model": model, "response_model": data.get("model"), "completion_id": data.get("id"),
            "provider_request_id": provider_request_id, "status": status, "http_status": http_status,
            "error_type": error_type, "duration_ms": round(duration_ms, 3), "usage": usage,
            "usage_reported": any(value is not None for value in usage.values()),
            "usage_complete": usage["prompt_tokens"] is not None and usage["total_tokens"] is not None
                and (operation == "embedding" or usage["completion_tokens"] is not None),
            "latency_scope": "http_request_and_response_validation" if provider == "openrouter" else "sdk_call",
            "usage_source": "provider_response", "estimated_cost": cost,
            "reported_cost": {"amount": str(raw_cost), "currency": "USD"} if raw_cost is not None else None,
            "effective_output_tokens_per_second": round(usage["completion_tokens"] * 1000 / duration_ms, 3)
                if usage["completion_tokens"] is not None and duration_ms > 0 else None,
            **measurements}
        if run:
            run.calls.append(record)
        emit_event(record)
    except Exception:
        # Bad/missing telemetry must never turn a successful provider response into an app error.
        pass
