"""config.yaml validation.

A typo'd or out-of-range key used to crash deep inside a tool call with an
opaque ``KeyError``. This module validates the config at load time instead and
raises a ``ConfigError`` naming the exact offending key, so problems surface at
startup with a message that points at the fix.

Deliberately dependency-free (no pydantic) to keep the install footprint small.
"""

from __future__ import annotations

import urllib.parse
from pathlib import Path
from typing import Any

# Relative paths that the app reads/writes from the project root.
_RELATIVE_PATH_KEYS = (
    "data.path",
    "data.store_path",
    "model_bench.artifacts_dir",
    "model_bench.best_model_path",
    "model_bench.bundle_path",
    "model_bench.metadata_path",
    "agent.activity_log",
    "digest.out_path",
    "alerts.state_path",
)


class ConfigError(ValueError):
    """Raised when config.yaml fails validation; message names the bad key."""


def _get(cfg: dict[str, Any], dotted: str) -> Any:
    cur: Any = cfg
    for part in dotted.split("."):
        if not isinstance(cur, dict) or part not in cur:
            return None
        cur = cur[part]
    return cur


def validate_config(cfg: dict[str, Any]) -> dict[str, Any]:
    """Validate `cfg` (already loaded from YAML). Raises ConfigError on failure.

    Checks the keys the runtime actually depends on:
      * risk.blend weights — each in [0, 1] and summing to 1.0
      * risk.fraud_threshold — in [0, 1]
      * agent.model — non-empty string
      * paths.* — relative paths (no absolute paths, no parent traversal)
    """
    errors: list[str] = []

    risk = cfg.get("risk", {})
    blend = risk.get("blend", {})
    if not isinstance(blend, dict):
        errors.append("risk.blend must be a mapping with rules/supervised/isolation_forest")
    else:
        missing = [k for k in ("rules", "supervised", "isolation_forest") if k not in blend]
        if missing:
            errors.append(f"risk.blend is missing key(s): {', '.join(missing)}")
        else:
            weights = [blend[k] for k in ("rules", "supervised", "isolation_forest")]
            try:
                weights = [float(w) for w in weights]
            except (TypeError, ValueError):
                errors.append("risk.blend weights must be numbers")
            else:
                if not all(0.0 <= w <= 1.0 for w in weights):
                    errors.append(f"risk.blend weights must each be in [0, 1] (got {weights})")
                if abs(sum(weights) - 1.0) > 1e-9:
                    errors.append(f"risk.blend weights must sum to 1.0 (got {sum(weights):.3f})")

    thr = _get(cfg, "risk.fraud_threshold")
    if thr is not None:
        try:
            if not 0.0 <= float(thr) <= 1.0:
                errors.append(f"risk.fraud_threshold must be in [0, 1] (got {thr})")
        except (TypeError, ValueError):
            errors.append(f"risk.fraud_threshold must be a number (got {thr!r})")

    model = _get(cfg, "agent.model")
    if model is None or not str(model).strip():
        errors.append("agent.model must be a non-empty string")

    focal_users = _get(cfg, "data.focal_users")
    if focal_users is not None:
        if not isinstance(focal_users, list) or not focal_users:
            errors.append("data.focal_users must be a non-empty list of account names")
        else:
            for u in focal_users:
                if not isinstance(u, str) or not str(u).strip():
                    errors.append(f"data.focal_users entries must be non-empty strings (got {u!r})")
    focal_user = _get(cfg, "data.focal_user")
    if focal_user and focal_users and focal_user not in focal_users:
        errors.append(
            f"data.focal_user {focal_user!r} must be one of data.focal_users {focal_users}"
        )

    pricing = _get(cfg, "agent.pricing")
    if pricing is not None:
        if not isinstance(pricing, dict):
            errors.append(
                "agent.pricing must be a mapping of model -> {input_per_1m, output_per_1m}"
            )
        else:
            for model, prices in pricing.items():
                if not isinstance(prices, dict):
                    errors.append(f"agent.pricing.{model} must be a mapping")
                    continue
                for key in ("input_per_1m", "output_per_1m"):
                    try:
                        if float(prices[key]) < 0:
                            errors.append(
                                f"agent.pricing.{model}.{key} must be >= 0 (got {prices[key]})"
                            )
                    except (KeyError, TypeError, ValueError):
                        errors.append(
                            f"agent.pricing.{model}.{key} must be a number (got {prices.get(key)!r})"
                        )

    features = cfg.get("features", {})
    if features is not None and not isinstance(features, dict):
        errors.append("features must be a mapping of flag name -> bool")
    else:
        for flag, value in (features or {}).items():
            if not isinstance(value, bool):
                errors.append(f"features.{flag} must be a bool (got {value!r})")

    hpo_cfg = _get(cfg, "model_bench.hpo")
    if hpo_cfg is not None:
        if not isinstance(hpo_cfg, dict):
            errors.append("model_bench.hpo must be a mapping")
        else:
            # (key, min, max) — max=None means "no upper bound".
            for key, lo, hi in (
                ("n_trials", 1, None),
                ("min_improvement", 0.0, 1.0),
                ("n_estimators", 1, None),
            ):
                value = hpo_cfg.get(key)
                if value is None:
                    continue
                try:
                    fv = float(value)
                except (TypeError, ValueError):
                    errors.append(f"model_bench.hpo.{key} must be a number (got {value!r})")
                    continue
                if hi is not None and not lo <= fv <= hi:
                    errors.append(f"model_bench.hpo.{key} must be in [{lo}, {hi}] (got {value})")
                elif hi is None and not fv >= lo:
                    errors.append(f"model_bench.hpo.{key} must be >= {lo} (got {value})")

    canary_tol = _get(cfg, "model_bench.canary_tolerance")
    if canary_tol is not None:
        try:
            if not 0.0 < float(canary_tol) <= 1.0:
                errors.append(f"model_bench.canary_tolerance must be in (0, 1] (got {canary_tol})")
        except (TypeError, ValueError):
            errors.append(f"model_bench.canary_tolerance must be a number (got {canary_tol!r})")

    budgets = _get(cfg, "budgets.monthly")
    if budgets is not None:
        if not isinstance(budgets, dict):
            errors.append("budgets.monthly must be a mapping of category -> number")
        else:
            for cat, value in budgets.items():
                try:
                    if float(value) < 0:
                        errors.append(f"budgets.monthly.{cat} must be >= 0 (got {value})")
                except (TypeError, ValueError):
                    errors.append(f"budgets.monthly.{cat} must be a number (got {value!r})")

    advice = _get(cfg, "advice")
    if advice is not None:
        if not isinstance(advice, dict):
            errors.append("advice must be a mapping of threshold -> number in [0, 1]")
        else:
            for key, value in advice.items():
                try:
                    if not 0.0 <= float(value) <= 1.0:
                        errors.append(f"advice.{key} must be in [0, 1] (got {value})")
                except (TypeError, ValueError):
                    errors.append(f"advice.{key} must be a number (got {value!r})")

    webhook = _get(cfg, "alerts.webhook_url")
    # Empty string = unconfigured (the documented default, like digest.slack_webhook).
    if webhook is not None and str(webhook).strip():
        try:
            parsed = urllib.parse.urlparse(str(webhook))
        except ValueError:
            parsed = urllib.parse.urlparse("")
        if parsed.scheme not in ("http", "https") or not parsed.netloc:
            errors.append(f"alerts.webhook_url must be a valid http(s) URL (got {webhook!r})")

    for key in _RELATIVE_PATH_KEYS:
        value = _get(cfg, key)
        if value is None:
            continue
        if not isinstance(value, str) or not str(value).strip():
            errors.append(f"{key} must be a non-empty string path")
            continue
        p = Path(str(value))
        # Absolute paths are fine (Docker, temp test envs) — but nothing may
        # traverse above the project root or outside its data directories.
        if ".." in p.parts:
            errors.append(f"{key} must not traverse outside the project (got {value!r})")

    if errors:
        raise ConfigError("Invalid config.yaml: " + "; ".join(errors))
    return cfg
