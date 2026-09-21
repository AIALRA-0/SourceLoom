"""Small, provider-neutral route, price and usage contracts.

The module deliberately contains no persistence or network code.  A caller may
import a ReadWeave registry export into these contracts, but SourceLoom never
opens ReadWeave's database at runtime.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
from copy import deepcopy
from datetime import datetime, timezone
from hashlib import sha256
import json
import os
import tempfile
from pathlib import Path
from typing import Any, Mapping
from urllib.parse import urlsplit


PROTOCOL_CHAT_COMPLETIONS = "chat_completions"
PROTOCOL_RESPONSES = "responses"
PROTOCOL_REST_SEARCH = "rest_search"
_PROTOCOLS = {PROTOCOL_CHAT_COMPLETIONS, PROTOCOL_RESPONSES, PROTOCOL_REST_SEARCH}
SETTINGS_FILENAME = "provider-settings.json"
RUNTIME_SETTING_KEYS = (
    "search_order", "evidence_query_limit", "evidence_open_limit",
    "active_content_patch_limit", "active_format_patch_limit",
    "default_heading_numbering", "media_collapsed_default", "fx_rate",
    "readweave_profile_id", "readweave_profile_digest", "readweave_registry_path",
    "staged_primary_provider",
)


def _number(value: Any) -> int | None:
    if isinstance(value, bool):
        return None
    try:
        number = int(value)
    except (TypeError, ValueError):
        return None
    return number if number >= 0 else None


def _first_number(mapping: Mapping[str, Any], *keys: str) -> int | None:
    for key in keys:
        number = _number(mapping.get(key))
        if number is not None:
            return number
    return None


def _nested_number(mapping: Mapping[str, Any], *paths: tuple[str, ...]) -> int | None:
    for path in paths:
        current: Any = mapping
        for key in path:
            if not isinstance(current, Mapping):
                current = None
                break
            current = current.get(key)
        number = _number(current)
        if number is not None:
            return number
    return None


@dataclass(frozen=True)
class PriceSnapshot:
    """Immutable effective prices used by the local ledger.

    ``display_multiplier`` is descriptive metadata only and is intentionally
    excluded from all cost calculations in :mod:`sourceloom.money`.
    """

    provider_id: str
    model: str
    currency: str = "CNY"
    cache_hit_input_per_million: float = 0.0
    cache_miss_input_per_million: float = 0.0
    output_per_million: float = 0.0
    source: str = "configured"
    version: str = ""
    effective_at: str = ""
    display_multiplier: float | None = None

    def __post_init__(self) -> None:
        for field in (
            "cache_hit_input_per_million",
            "cache_miss_input_per_million",
            "output_per_million",
        ):
            if getattr(self, field) < 0:
                raise ValueError("价格不能为负数")
        if self.display_multiplier is not None and self.display_multiplier < 0:
            raise ValueError("展示倍率不能为负数")

    @property
    def snapshot_id(self) -> str:
        payload = json.dumps(asdict(self), ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        return sha256(payload.encode("utf-8")).hexdigest()

    def rates(self) -> dict[str, float]:
        return {
            "input": self.cache_miss_input_per_million,
            "cached_input": self.cache_hit_input_per_million,
            "output": self.output_per_million,
        }

    def public(self) -> dict[str, Any]:
        return {
            "provider_id": self.provider_id,
            "model": self.model,
            "currency": self.currency,
            "cache_hit_input_per_million": self.cache_hit_input_per_million,
            "cache_miss_input_per_million": self.cache_miss_input_per_million,
            "output_per_million": self.output_per_million,
            "source": self.source,
            "version": self.version,
            "effective_at": self.effective_at,
            "snapshot_id": self.snapshot_id,
            "display_multiplier": self.display_multiplier,
        }


@dataclass(frozen=True)
class ProviderRoute:
    """Runtime-safe route metadata; credentials remain in the server config."""

    provider_id: str
    provider_type: str
    model: str
    protocol: str = PROTOCOL_CHAT_COMPLETIONS
    base_url: str = ""
    endpoint: str = ""
    role: str = "primary"
    priority: int = 0
    enabled: bool = True
    credential_ref: str | None = None
    price_snapshot: PriceSnapshot | None = None
    route_kind: str = "model"
    auth_type: str = "bearer"
    model_parameters: dict[str, Any] | None = None
    search_per_request: float | None = None

    def __post_init__(self) -> None:
        if self.protocol not in _PROTOCOLS:
            raise ValueError(f"不支持的模型协议：{self.protocol}")

    @property
    def has_credentials(self) -> bool:
        return bool(self.credential_ref)

    def public(self) -> dict[str, Any]:
        return {
            "provider_id": self.provider_id,
            "provider_type": self.provider_type,
            "model": self.model,
            "protocol": self.protocol,
            "role": self.role,
            "priority": self.priority,
            "enabled": self.enabled,
            "has_credentials": self.has_credentials,
            "route_kind": self.route_kind,
            "auth_type": self.auth_type,
            "model_parameters": deepcopy(self.model_parameters or {}),
            "search_per_request": self.search_per_request,
            "pricing": self.price_snapshot.public() if self.price_snapshot else None,
        }


@dataclass(frozen=True)
class NormalizedUsage:
    input_tokens: int | None
    cache_hit_input_tokens: int | None
    cache_miss_input_tokens: int | None
    output_tokens: int | None
    complete: bool

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class UsageReceipt:
    """A non-secret, serializable ledger receipt for one provider call."""

    request_id: str
    provider_id: str
    model: str
    protocol: str
    pricing_version: str
    price_snapshot_id: str
    pricing_source: str
    currency: str
    cache_hit_input_tokens: int | None
    cache_miss_input_tokens: int | None
    output_tokens: int | None
    local_estimate_cny: float | None
    no_cache_upper_bound_cny: float | None
    reservation_cny: float
    provider_actual_cny: float | None = None
    billing_status: str = "unsettled"
    upstream_id: str | None = None

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


def normalize_protocol(config: Mapping[str, Any]) -> str:
    value = config.get("protocol", config.get("request_protocol", config.get("transport")))
    if value in {"responses", "response"}:
        return PROTOCOL_RESPONSES
    if value in {"rest_search", "rest-search", "search"}:
        return PROTOCOL_REST_SEARCH
    if value in {"chat_completions", "chat-completions", "chat", None, ""}:
        return PROTOCOL_CHAT_COMPLETIONS
    raise ValueError(f"不支持的模型协议：{value}")


def normalize_usage(usage: Mapping[str, Any] | None) -> NormalizedUsage:
    """Normalize Responses and Chat Completions usage without guessing absence."""

    usage = usage if isinstance(usage, Mapping) else {}
    input_tokens = _first_number(usage, "input_tokens", "prompt_tokens", "inputTokens")
    output_tokens = _first_number(usage, "output_tokens", "completion_tokens", "outputTokens")
    cache_hit = _first_number(
        usage, "cache_hit_input_tokens", "prompt_cache_hit_tokens", "cache_read_input_tokens", "cached_tokens",
        "cacheHitInputTokens",
    )
    nested_cache = _nested_number(
        usage,
        ("input_tokens_details", "cached_tokens"),
        ("prompt_tokens_details", "cached_tokens"),
    )
    if cache_hit is None:
        cache_hit = nested_cache
    elif nested_cache is not None:
        cache_hit = max(cache_hit, nested_cache)
    if input_tokens is not None:
        cache_hit = min(input_tokens, cache_hit or 0)
        explicit_miss = _first_number(usage, "prompt_cache_miss_tokens", "cache_miss_input_tokens")
        cache_miss = explicit_miss if explicit_miss is not None and cache_hit + explicit_miss == input_tokens else input_tokens - cache_hit
    else:
        cache_hit = None
        cache_miss = None
    return NormalizedUsage(input_tokens, cache_hit, cache_miss, output_tokens, input_tokens is not None and output_tokens is not None)


def price_snapshot_from_config(config: Mapping[str, Any]) -> PriceSnapshot:
    rates = config.get("pricing_cny") if isinstance(config.get("pricing_cny"), Mapping) else {}
    provider_id = str(config.get("provider_id") or config.get("provider") or "unknown")
    model = str(config.get("model") or "unknown")
    return PriceSnapshot(
        provider_id=provider_id,
        model=model,
        currency=str(config.get("pricing_currency") or "CNY"),
        cache_hit_input_per_million=float(rates.get("cached_input", config.get("cached_input_price", 0)) or 0),
        # Never reinterpret legacy USD input_price/output_price as CNY.
        cache_miss_input_per_million=float(rates.get("input", config.get("input_price_cny", 0)) or 0),
        output_per_million=float(rates.get("output", config.get("output_price_cny", 0)) or 0),
        source=str(config.get("pricing_source") or "configured"),
        version=str(config.get("pricing_version") or ""),
        effective_at=str(config.get("pricing_effective_at") or ""),
        display_multiplier=(float(config["display_multiplier"]) if config.get("display_multiplier") is not None else None),
    )


def route_from_config(config: Mapping[str, Any]) -> ProviderRoute:
    return ProviderRoute(
        provider_id=str(config.get("provider_id") or config.get("provider") or "unknown"),
        provider_type=str(config.get("provider") or "unknown"),
        model=str(config.get("model") or "unknown"),
        protocol=normalize_protocol(config),
        base_url=str(config.get("base_url") or ""),
        endpoint=str(config.get("endpoint") or ""),
        role=str(config.get("route_role") or config.get("role") or "primary"),
        priority=int(config.get("priority", 0) or 0),
        enabled=bool(config.get("enabled", True)),
        credential_ref=str(config.get("credential_ref") or "api_key") if config.get("api_key") else None,
        price_snapshot=price_snapshot_from_config(config),
        route_kind=str(config.get("route_kind") or config.get("kind") or "model"),
        auth_type=str(config.get("auth_type") or config.get("authType") or "bearer"),
        model_parameters=deepcopy(config.get("model_parameters") or config.get("modelParameters") or {}),
        search_per_request=(float(config["search_per_request"]) if config.get("search_per_request") is not None
                            else float(config["searchPerRequest"]) if config.get("searchPerRequest") is not None else None),
    )


def public_provider_metadata(config: Mapping[str, Any]) -> dict[str, Any]:
    """Return route metadata without API keys, credential refs or private URLs."""

    route = route_from_config(config)
    roles = config.get("role_providers") if isinstance(config.get("role_providers"), Mapping) else {}
    staged = config.get("provider_routes") if isinstance(config.get("provider_routes"), Mapping) else {}
    credentials = config.get("provider_credentials") if isinstance(config.get("provider_credentials"), Mapping) else {}
    staged_public = {}
    for provider_id, value in staged.items():
        if not isinstance(value, Mapping):
            continue
        staged_config = dict(config) | dict(value) | {"provider_id": provider_id}
        staged_config.pop("api_key", None)
        if credentials.get(provider_id):
            staged_config["api_key"] = credentials[provider_id]
        staged_public[str(provider_id)] = route_from_config(staged_config).public()
    return {
        "active": route.public(),
        "roles": {
            str(role): route_from_config(dict(config) | dict(value)).public()
            for role, value in roles.items()
            if isinstance(value, Mapping)
        },
        "staged": staged_public,
        "settings_state": str(config.get("provider_settings_state") or "active"),
        "settings_revision": str(config.get("provider_settings_revision") or ""),
    }


def _search_route_entry(route: ProviderRoute) -> dict[str, Any]:
    """Return the non-secret shape consumed by active composition search."""

    return {
        "provider_id": route.provider_id,
        "id": route.provider_id,
        "kind": "search",
        "route_kind": "search",
        "provider": route.provider_id,
        "base_url": route.base_url,
        "endpoint": route.endpoint,
        "enabled": route.enabled,
        "priority": route.priority,
        "role": route.role,
        "auth_type": route.auth_type,
        "authType": route.auth_type,
        "model_parameters": deepcopy(route.model_parameters or {}),
        "modelParameters": deepcopy(route.model_parameters or {}),
        "search_per_request": route.search_per_request,
        "searchPerRequest": route.search_per_request,
        "search_currency": route.price_snapshot.currency if route.price_snapshot else "USD",
        "credential_ref": route.credential_ref,
    }


def _search_routes_by_id(value: Any) -> dict[str, Any]:
    """Keep legacy list and current map search settings mergeable."""

    if isinstance(value, Mapping):
        return {str(key): deepcopy(item) for key, item in value.items() if isinstance(item, Mapping)}
    if isinstance(value, list):
        result: dict[str, Any] = {}
        for item in value:
            if not isinstance(item, Mapping):
                continue
            provider_id = item.get("provider_id") or item.get("id") or item.get("provider")
            if provider_id:
                result[str(provider_id)] = deepcopy(dict(item))
        return result
    return {}


def stage_imported_routes(
    config: Mapping[str, Any],
    routes: Mapping[str, ProviderRoute],
    credentials: Mapping[str, str] | None = None,
) -> dict[str, Any]:
    """Create a local draft snapshot; secrets stay in a private credential map."""

    next_config = deepcopy(dict(config))
    staged = dict(next_config.get("provider_routes") or {})
    search_routes = _search_routes_by_id(next_config.get("search_routes"))
    for provider_id, route in routes.items():
        staged[provider_id] = {
            "provider_id": route.provider_id,
            "provider": "openai-compatible" if route.provider_type == "model" else route.provider_type,
            "model": route.model,
            "protocol": route.protocol,
            "base_url": route.base_url,
            "endpoint": route.endpoint,
            "route_role": route.role,
            "priority": route.priority,
            "enabled": route.enabled,
            "pricing_currency": route.price_snapshot.currency if route.price_snapshot else "CNY",
            "pricing_cny": route.price_snapshot.rates() if route.price_snapshot else {},
            "pricing_source": route.price_snapshot.source if route.price_snapshot else "readweave-export",
            "pricing_version": route.price_snapshot.version if route.price_snapshot else "readweave-export",
            "pricing_effective_at": route.price_snapshot.effective_at if route.price_snapshot else "",
            "display_multiplier": route.price_snapshot.display_multiplier if route.price_snapshot else None,
            "route_kind": route.route_kind,
            "auth_type": route.auth_type,
            "authType": route.auth_type,
            "model_parameters": deepcopy(route.model_parameters or {}),
            "modelParameters": deepcopy(route.model_parameters or {}),
            "search_per_request": route.search_per_request,
            "searchPerRequest": route.search_per_request,
        }
        if route.route_kind == "search" or route.protocol == PROTOCOL_REST_SEARCH:
            search_routes[str(provider_id)] = _search_route_entry(route)
    next_config["provider_routes"] = staged
    next_config["search_routes"] = search_routes
    if credentials:
        private = dict(next_config.get("provider_credentials") or {})
        private.update({str(key): str(value) for key, value in credentials.items() if value})
        next_config["provider_credentials"] = private
    next_config["provider_settings_state"] = "draft"
    next_config["provider_settings_revision"] = sha256(
        json.dumps(staged, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()
    return next_config


def _apply_role_provider_overrides(config: Mapping[str, Any]) -> dict[str, Any]:
    next_config = deepcopy(dict(config))
    overrides = config.get("role_provider_overrides")
    if not isinstance(overrides, Mapping):
        return next_config
    role_providers = dict(next_config.get("role_providers") or {})
    for role, route in overrides.items():
        if isinstance(route, Mapping):
            role_providers[str(role)] = deepcopy(dict(route))
    next_config["role_providers"] = role_providers
    return next_config


def activate_staged_route(config: Mapping[str, Any], provider_id: str) -> dict[str, Any]:
    """Promote a reviewed local snapshot without copying credentials to metadata."""

    staged = config.get("provider_routes") if isinstance(config.get("provider_routes"), Mapping) else {}
    raw = staged.get(provider_id)
    if not isinstance(raw, Mapping):
        raise KeyError(provider_id)
    if str(raw.get("route_kind") or raw.get("kind") or "model") == "search" or raw.get("protocol") == PROTOCOL_REST_SEARCH:
        raise ValueError("搜索 route 只能作为搜索资源通道，不能激活为模型生成通道")
    next_config = deepcopy(dict(config))
    previous_provider_id = next_config.get("provider_id")
    route = dict(raw)
    route_host=(urlsplit(str(route.get('base_url') or '')).hostname or '').casefold()
    billing_mode=str(route.get('billing_mode') or
        ('metered' if route_host=='api.kuafushe.cc' else next_config.get('billing_mode','metered')))
    frozen_route = {
        key: route[key]
        for key in ("provider", "provider_id", "model", "protocol", "endpoint", "base_url",
                    "pricing_currency", "pricing_cny", "pricing_source", "pricing_version",
                    "pricing_effective_at", "display_multiplier", "route_role", "priority", "enabled", "route_kind",
                    "auth_type", "authType", "model_parameters", "modelParameters", "billing_mode",
                    "search_per_request", "searchPerRequest")
        if key in route
    }
    frozen_route['billing_mode']=billing_mode
    next_config.update({
        "provider": route.get("provider", "openai-compatible"),
        "provider_id": provider_id,
        "model": route.get("model", next_config.get("model")),
        "protocol": route.get("protocol", PROTOCOL_CHAT_COMPLETIONS),
        "endpoint": route.get("endpoint", "/responses"),
        "base_url": route.get("base_url", next_config.get("base_url", "")),
        "pricing_currency": route.get("pricing_currency", "CNY"),
        "pricing_cny": route.get("pricing_cny", {}),
        "pricing_source": route.get("pricing_source", "configured"),
        "pricing_version": route.get("pricing_version", ""),
        "pricing_effective_at": route.get("pricing_effective_at", ""),
        "display_multiplier": route.get("display_multiplier"),
        "billing_mode": billing_mode,
        "route_kind": route.get("route_kind", route.get("kind", "model")),
        "auth_type": route.get("auth_type", route.get("authType", "bearer")),
        "model_parameters": deepcopy(route.get("model_parameters", route.get("modelParameters", {})) or {}),
        "search_per_request": route.get("search_per_request", route.get("searchPerRequest")),
        "provider_settings_state": "active",
        "active_route_id": provider_id,
    })
    credentials = next_config.get("provider_credentials")
    if isinstance(credentials, Mapping) and credentials.get(provider_id):
        next_config["api_key"] = credentials[provider_id]
    elif previous_provider_id != provider_id:
        next_config.pop("api_key", None)
    # Content roles share one reviewed route snapshot. Visual work keeps its
    # existing route until the imported provider is explicitly image-probed.
    content_roles = {
        "active_plan", "active_write", "active_review", "active_revision",
        "active_patch", "active_protocol", "active_format",
    }
    role_providers = dict(next_config.get("role_providers") or {})
    route_identity_keys = {
        "provider", "provider_id", "model", "protocol", "endpoint", "base_url",
        "api_key", "token", "credential_ref", "billing_mode", "structured_output",
        "execution_channel", "responses_profile", "quota_fallback",
    }
    for role in content_roles:
        # Preserve role-local limits and effort, but never carry another
        # provider's credential or transport identity into the activated route.
        local = {key:value for key,value in dict(role_providers.get(role) or {}).items()
                 if key not in route_identity_keys}
        role_providers[role] = local | frozen_route
    next_config["role_providers"] = role_providers
    return _apply_role_provider_overrides(next_config)


def provider_settings_path(data_dir: str | Path) -> Path:
    return Path(data_dir) / SETTINGS_FILENAME


def private_settings_snapshot(config: Mapping[str, Any]) -> dict[str, Any]:
    """Build the private on-disk overlay; callers must never return this object."""

    routes = config.get("provider_routes") if isinstance(config.get("provider_routes"), Mapping) else {}
    credentials = config.get("provider_credentials") if isinstance(config.get("provider_credentials"), Mapping) else {}
    return {
        "version": 1,
        "provider_routes": deepcopy(dict(routes)),
        "search_routes": _search_routes_by_id(config.get("search_routes")),
        "provider_credentials": {str(key): str(value) for key, value in credentials.items() if value},
        "active_route_id": config.get("active_route_id") or config.get("provider_id") or "",
        "state": str(config.get("provider_settings_state") or "active"),
        "revision": str(config.get("provider_settings_revision") or ""),
        "probe": deepcopy(config.get("provider_settings_probe") or {}),
        "runtime_settings": {key: deepcopy(config.get(key)) for key in RUNTIME_SETTING_KEYS if key in config},
    }


def save_private_settings(data_dir: str | Path, config: Mapping[str, Any]) -> Path:
    """Atomically save the server-only settings overlay with restrictive mode."""

    target = provider_settings_path(data_dir)
    target.parent.mkdir(parents=True, exist_ok=True)
    payload = json.dumps(private_settings_snapshot(config), ensure_ascii=False, sort_keys=True, indent=2).encode("utf-8")
    descriptor, temporary = tempfile.mkstemp(prefix=".provider-settings-", suffix=".tmp", dir=target.parent)
    try:
        os.chmod(temporary, 0o600)
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, target)
        try:
            os.chmod(target, 0o600)
        except OSError:
            pass
    except Exception:
        try:
            os.close(descriptor)
        except OSError:
            pass
        try:
            Path(temporary).unlink()
        except OSError:
            pass
        raise
    return target


def load_private_settings(data_dir: str | Path) -> dict[str, Any]:
    path = provider_settings_path(data_dir)
    if not path.is_file():
        return {}
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return value if isinstance(value, dict) and value.get("version") == 1 else {}


def apply_private_settings(config: Mapping[str, Any], settings: Mapping[str, Any]) -> dict[str, Any]:
    """Restore the private overlay into a runtime config after process restart."""

    next_config = deepcopy(dict(config))
    routes = settings.get("provider_routes")
    if isinstance(routes, Mapping):
        next_config["provider_routes"] = deepcopy(dict(routes))
    search_routes = settings.get("search_routes")
    if isinstance(search_routes, Mapping):
        next_config["search_routes"] = deepcopy(dict(search_routes))
    else:
        next_config["search_routes"] = _search_routes_by_id(next_config.get("search_routes"))
    credentials = settings.get("provider_credentials")
    if isinstance(credentials, Mapping):
        next_config["provider_credentials"] = {str(key): str(value) for key, value in credentials.items() if value}
    next_config["provider_settings_state"] = str(settings.get("state") or "active")
    next_config["provider_settings_revision"] = str(settings.get("revision") or "")
    next_config["provider_settings_probe"] = deepcopy(settings.get("probe") or {})
    runtime=settings.get("runtime_settings")
    if isinstance(runtime, Mapping):
        for key in RUNTIME_SETTING_KEYS:
            if key in runtime:next_config[key]=deepcopy(runtime[key])
    active_id = str(settings.get("active_route_id") or "")
    if active_id and isinstance(next_config.get("provider_routes"), Mapping) and active_id in next_config["provider_routes"]:
        next_config = activate_staged_route(next_config, active_id)
        next_config["active_route_id"] = active_id
        next_config["provider_settings_state"] = str(settings.get("state") or "active")
    # A deployment may pin a small number of roles to a subscription router
    # while the settings window continues to manage the ordinary primary route.
    # Apply those explicit overrides last so restarting the detached worker does
    # not silently replace them with the active settings-window route.
    return _apply_role_provider_overrides(next_config)


def stage_route(config: Mapping[str, Any], route: Mapping[str, Any], api_key: str | None = None) -> dict[str, Any]:
    """Stage one hand-authored route while keeping its key out of route metadata."""

    if not isinstance(route, Mapping) or not route.get("provider_id"):
        raise ValueError("provider_id 是必填设置")
    provider_id = str(route["provider_id"])
    route_data = dict(route)
    route_data.pop("api_key", None)
    route_data.pop("token", None)
    snapshot = route_from_config(route_data)
    staged = dict(config.get("provider_routes") or {})
    staged[provider_id] = route_data | {
        "provider_id": provider_id,
        "provider": route_data.get("provider", "openai-compatible"),
        "protocol": snapshot.protocol,
        "pricing_cny": snapshot.price_snapshot.rates() if snapshot.price_snapshot else {},
    }
    next_config = deepcopy(dict(config))
    next_config["provider_routes"] = staged
    search_routes = _search_routes_by_id(next_config.get("search_routes"))
    if snapshot.route_kind == "search" or snapshot.protocol == PROTOCOL_REST_SEARCH:
        search_routes[provider_id] = _search_route_entry(snapshot)
    next_config["search_routes"] = search_routes
    credentials = dict(next_config.get("provider_credentials") or {})
    if api_key:
        credentials[provider_id] = api_key
    next_config["provider_credentials"] = credentials
    next_config["provider_settings_state"] = "draft"
    next_config["provider_settings_probe"] = {}
    next_config["provider_settings_revision"] = sha256(
        json.dumps(staged, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()
    return next_config


def probe_staged_route(config: Mapping[str, Any], provider_id: str) -> dict[str, Any]:
    staged = config.get("provider_routes") if isinstance(config.get("provider_routes"), Mapping) else {}
    raw = staged.get(provider_id)
    if not isinstance(raw, Mapping):
        raise KeyError(provider_id)
    route = route_from_config(dict(config) | dict(raw) | {"provider_id": provider_id})
    parsed = urlsplit(route.base_url)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname or parsed.username or parsed.password:
        raise ValueError("provider base_url 必须是无用户密码的 HTTP(S) 地址")
    credentials = config.get("provider_credentials") if isinstance(config.get("provider_credentials"), Mapping) else {}
    legacy_key = config.get("api_key") if config.get("provider_id") == provider_id else None
    auth_requires_key = route.auth_type.casefold() not in {"", "none", "public", "anonymous", "optional"}
    if (route.provider_type == "openai-compatible" or auth_requires_key) and not (credentials.get(provider_id) or legacy_key):
        raise ValueError("provider 尚未配置服务器端 API key")
    if route.price_snapshot and route.price_snapshot.currency != "CNY":
        raise ValueError("当前本地预算账本只接受 CNY 价格快照")
    next_config = deepcopy(dict(config))
    next_config["provider_settings_state"] = "probed"
    next_config["provider_settings_probe"] = {
        "provider_id": provider_id,
        "protocol": route.protocol,
        "base_url_host": parsed.hostname,
        "has_credentials": bool(credentials.get(provider_id) or legacy_key),
        "price_snapshot_id": route.price_snapshot.snapshot_id if route.price_snapshot else None,
    }
    return next_config


def import_readweave_registry(source: Mapping[str, Any] | list[Mapping[str, Any]] | str | Path) -> dict[str, ProviderRoute]:
    """Import an exported ReadWeave registry without opening its database."""

    if isinstance(source, (str, Path)):
        source = json.loads(Path(source).read_text(encoding="utf-8"))
    if isinstance(source, Mapping):
        definitions = source.get("providers", source.get("definitions", source.get("routes", [])))
    else:
        definitions = source
    if not isinstance(definitions, list):
        raise ValueError("ReadWeave registry export 必须包含 providers/definitions/routes 数组")
    result: dict[str, ProviderRoute] = {}
    for raw in definitions:
        if not isinstance(raw, Mapping) or not raw.get("id"):
            continue
        pricing = raw.get("pricing") if isinstance(raw.get("pricing"), Mapping) else {}
        snapshot = PriceSnapshot(
            provider_id=str(raw["id"]),
            model=str(raw.get("model") or "unknown"),
            currency=str(pricing.get("currency") or "CNY"),
            cache_hit_input_per_million=float(pricing.get("cacheHitInputPerMillion", 0) or 0),
            cache_miss_input_per_million=float(pricing.get("cacheMissInputPerMillion", 0) or 0),
            output_per_million=float(pricing.get("outputPerMillion", 0) or 0),
            source=str(pricing.get("source") or "readweave-export"),
            version=str(pricing.get("version") or "readweave-export"),
            effective_at=str(pricing.get("effectiveAt") or datetime.now(timezone.utc).isoformat()),
            display_multiplier=(float(pricing["displayMultiplier"])
                                if pricing.get("displayMultiplier") is not None else None),
        )
        protocol = raw.get("requestProtocol", raw.get("protocol", PROTOCOL_CHAT_COMPLETIONS))
        if protocol in {"responses", "response"}:
            protocol = PROTOCOL_RESPONSES
        elif protocol in {"rest_search", "rest-search", "search"}:
            protocol = PROTOCOL_REST_SEARCH
        else:
            protocol = PROTOCOL_CHAT_COMPLETIONS
        route_kind = str(raw.get("kind") or ("search" if protocol == PROTOCOL_REST_SEARCH else "model"))
        provider_type = "search" if protocol == PROTOCOL_REST_SEARCH and not raw.get("kind") else str(raw.get("kind") or "model")
        result[str(raw["id"])] = ProviderRoute(
            provider_id=str(raw["id"]),
            provider_type=provider_type,
            model=str(raw.get("model") or "unknown"),
            protocol=protocol,
            base_url=str(raw.get("baseUrl") or ""),
            endpoint=str(raw.get("endpoint") or ""),
            role=str(raw.get("role") or "supplemental"),
            priority=int(raw.get("priority", 0) or 0),
            enabled=bool(raw.get("enabled", False)),
            credential_ref=None,
            price_snapshot=snapshot,
            route_kind=route_kind,
            auth_type=str(raw.get("authType") or raw.get("auth_type") or "bearer"),
            model_parameters=deepcopy(raw.get("modelParameters") or raw.get("model_parameters") or {}),
            search_per_request=(float(raw["searchPerRequest"]) if raw.get("searchPerRequest") is not None else
                                float(raw["search_per_request"]) if raw.get("search_per_request") is not None else None),
        )
    return result
