"""Native yuan estimates and subscription usage, without relabeling USD history."""

from __future__ import annotations

from typing import Any, Mapping

from .provider_settings import NormalizedUsage, normalize_usage


def _rates(rates: Mapping[str, Any] | None) -> tuple[float, float, float] | None:
    if not isinstance(rates, Mapping):
        return None
    try:
        # Keep historical names as an input compatibility boundary while
        # accepting the explicit names used by PriceSnapshot.
        hit = float(rates.get("cached_input", rates.get("cache_hit_input", 0)) or 0)
        miss = float(rates.get("input", rates.get("cache_miss_input", 0)) or 0)
        output = float(rates.get("output", 0) or 0)
    except (TypeError, ValueError):
        return None
    if min(hit, miss, output) < 0:
        return None
    return hit, miss, output


def _complete_usage(usage: NormalizedUsage) -> bool:
    return usage.complete and all(
        value is not None
        for value in (
            usage.cache_hit_input_tokens,
            usage.cache_miss_input_tokens,
            usage.output_tokens,
        )
    )


def usage_cost(usage: Mapping[str, Any] | None, rates: Mapping[str, Any] | None) -> float | None:
    """Calculate a configured-rate estimate; unknown usage remains ``None``.

    ``display_multiplier`` is deliberately not read here. Callers pass
    effective rates, so a descriptive commercial multiplier cannot be applied
    twice.
    """

    normalized = normalize_usage(usage)
    prices = _rates(rates)
    if not prices or not _complete_usage(normalized):
        return None
    hit, miss, output = prices
    return (
        normalized.cache_hit_input_tokens * hit
        + normalized.cache_miss_input_tokens * miss
        + normalized.output_tokens * output
    ) / 1e6


def no_cache_upper_bound(usage: Mapping[str, Any] | None, rates: Mapping[str, Any] | None) -> float | None:
    """Estimate complete usage with all input tokens treated as uncached."""

    normalized = normalize_usage(usage)
    prices = _rates(rates)
    if not prices or not normalized.complete or normalized.input_tokens is None or normalized.output_tokens is None:
        return None
    _, miss, output = prices
    return (normalized.input_tokens * miss + normalized.output_tokens * output) / 1e6


def usage_receipt(usage, rates, *, reserved_cny=0.0, provider_actual_cny=None):
    """Return serializable cost fields for a spending row."""

    normalized = normalize_usage(usage)
    estimate = usage_cost(usage, rates)
    return {
        "cache_hit_input_tokens": normalized.cache_hit_input_tokens,
        "cache_miss_input_tokens": normalized.cache_miss_input_tokens,
        "output_tokens": normalized.output_tokens,
        "local_estimate_cny": estimate,
        "no_cache_upper_bound_cny": no_cache_upper_bound(usage, rates),
        "reserved_cny": reserved_cny,
        "provider_actual_cny": provider_actual_cny,
        "billing_status": "settled_estimate" if estimate is not None else "unsettled",
    }


def summary(costs):
    # Preserve the existing public summary shape for callers and old records.
    return {'known_cny':sum(c['body'].get('actual_cny') or 0 for c in costs),
            'reserved_cny':sum(c['body'].get('reserved_cny') or 0 for c in costs if c['actual'] is None),
            'legacy_currency_records':sum(c['actual'] not in (None,0) and c['body'].get('actual_cny') is None for c in costs),
            'subscription_calls':sum(c['body'].get('channel') in {'subscription','router','codex-cli'} for c in costs)}
