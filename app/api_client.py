"""HTTP client for the FinSight Agent API — the app's data source in service mode.

Implements the same surface the pages use on ``FinanceFacts`` (``df``, ``cfg``,
``rule_only()``, ``monthly_summary()``, ``risk_scored_transactions()``, …) so
swapping the data source is a one-line change in ``app/common.py::get_facts``.
Uses only the standard library (``urllib``) — no httpx/requests dependency.

Enabled by setting ``FINSIGHT_API_URL`` (e.g. ``http://api:8000`` in docker
compose); ``FINSIGHT_API_KEY`` optionally gates requests via ``X-API-Key``.
See docs/technical/API.md for the HTTP contract.
"""

from __future__ import annotations

import json
from functools import cached_property
from typing import Any

import httpx

import pandas as pd


class ApiClientError(RuntimeError):
    """Raised when the API is unreachable or returns a non-2xx response."""


class ApiClient:
    """Read-only client mirroring the ``FinanceFacts`` interface over HTTP."""

    def __init__(
        self, base_url: str, api_key: str = "", timeout: float = 10.0, focal_user: str = ""
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key
        self.timeout = timeout
        self.focal_user = focal_user or ""
        # `focal_users` (all accounts) is served by the API meta endpoint; when
        # no focal user is selected we resolve the API's default lazily.
        self.focal_users: list[str] = []

    # ------------------------------------------------------------- transport
    def _request(self, path: str, method: str = "GET", params: dict[str, Any] | None = None) -> Any:
        url = f"{self.base_url}{path}"
        if params:
            query = httpx.QueryParams(self._clean(params))
            if str(query):
                url += "?" + str(query)
        headers: dict[str, str] = {}
        if self.api_key:
            headers["X-API-Key"] = self.api_key
        try:
            # The API is a trusted local service (own container / localhost).
            with httpx.Client(timeout=self.timeout) as client:
                resp = client.request(method, url, headers=headers)
                resp.raise_for_status()
                return resp.json()
        except httpx.HTTPStatusError as exc:
            body = exc.response.text[:500]
            raise ApiClientError(f"{exc.response.status_code} from {method} {path}: {body}") from exc
        except (httpx.RequestError, TimeoutError) as exc:
            raise ApiClientError(f"API unreachable at {self.base_url} ({exc})") from exc

    @staticmethod
    def _clean(params: dict[str, Any]) -> dict[str, str]:
        out: dict[str, str] = {}
        for key, value in params.items():
            if value is None:
                continue
            if isinstance(value, bool):
                out[key] = "true" if value else "false"
            else:
                out[key] = str(value)
        return out

    def ping(self) -> dict[str, Any]:
        """Lightweight reachability check (used by get_facts for fallback)."""
        return self._request("/api/v1/health")

    def reload(self) -> None:
        """Ask the server to drop its facts snapshot, then drop local caches."""
        self._request("/api/v1/reload", method="POST")
        for key in ("df", "_focal_df", "_meta", "cfg"):
            vars(self).pop(key, None)

    # ----------------------------------------------------------- facts mirror
    @cached_property
    def _meta(self) -> dict[str, Any]:
        return self._request("/api/v1/meta")

    @cached_property
    def cfg(self) -> dict[str, Any]:
        return dict(self._meta["config"])

    def resolve_focal(self) -> tuple[str, list[str]]:
        """(selected focal_user, all focal_users) — resolved from the API meta."""
        all_users = [str(u) for u in self._meta.get("focal_users") or []]
        default = str(self._meta.get("focal_user") or (all_users[0] if all_users else ""))
        selected = self.focal_user or default
        return selected, all_users or [selected]

    # Keep private alias for internal callers that predate the rename.
    _resolve_focal = resolve_focal

    def rule_only(self) -> bool:
        return bool(self._meta["rule_only"])

    @cached_property
    def df(self) -> pd.DataFrame:
        return self._frame(self._load_ledger())

    @cached_property
    def _focal_df(self) -> pd.DataFrame:
        selected, _ = self._resolve_focal()
        return self._frame(self._load_ledger({"focal_only": True, "user": selected}))

    def _load_ledger(self, params: dict[str, Any] | None = None) -> dict[str, Any]:
        """Fetch a full ledger by paging /api/v1/transactions (bounded pages).

        The API caps any single response at 5000 rows (C.1.3); this client
        pages through until `total` is reached and reassembles the complete
        frame. The demo ledger needs only a handful of requests, and the result
        is cached per session (cached_property).
        """
        page_size = 5000
        merged: list[dict[str, Any]] = []
        columns: list[str] = []
        dtypes: dict[str, str] = {}
        offset = 0
        while True:
            page = self._request(
                "/api/v1/transactions",
                params={**(params or {}), "limit": page_size, "offset": offset},
            )
            if not columns:
                columns = page["columns"]
                dtypes = page["dtypes"]
            merged.extend(page["data"])
            total = int(page.get("total", len(merged)))
            offset += len(page["data"])
            if offset >= total or not page["data"]:
                break
        return {"columns": columns, "dtypes": dtypes, "data": merged, "total": total}

    def account_types(self) -> list[str]:
        """Account types of the selected persona's accounts (dashboard filter)."""
        if "account_type" not in self.df.columns:
            return ["checking"]
        selected, _ = self._resolve_focal()
        if "persona_id" in self.df.columns:
            d = self.df[self.df["persona_id"] == selected]
        else:
            d = self.df[self.df["nameOrig"] == selected]
        return [str(t) for t in sorted(d["account_type"].dropna().unique())]

    def focal(self, account_type: str | None = None) -> pd.DataFrame:
        """The focal frame, optionally narrowed to one account channel.

        Mirrors ``FinanceFacts.focal``: the default (None / "checking") is the
        user's primary checking account; ``"all"`` returns every account of
        the persona, and a specific type narrows to that channel. The API's
        ``focal_only`` frame only carries the primary account, so persona-level
        views are filtered locally from the full ledger (which carries
        ``persona_id`` / ``account_type``).
        """
        if account_type in (None, "", "checking"):
            return self._focal_df.copy()
        d = self.df
        if "persona_id" not in d.columns:
            return self._focal_df.copy()
        selected, _ = self.resolve_focal()
        d = d[d["persona_id"] == selected]
        if account_type == "all" or "account_type" not in d.columns:
            return d
        return d[d["account_type"] == account_type]

    # Keep private alias for internal callers that predate the rename.
    _focal = focal

    @staticmethod
    def _frame(payload: dict[str, Any]) -> pd.DataFrame:
        """Rebuild a DataFrame from the API's {columns, dtypes, data} payload."""
        df = pd.DataFrame(payload["data"], columns=payload["columns"])
        casts = {c: t for c, t in payload["dtypes"].items() if t in ("int64", "float64", "bool")}
        if casts:
            df = df.astype(casts)
        if "datetime" in df.columns:
            df["datetime"] = pd.to_datetime(df["datetime"])
        return df

    # -------------------------------------------------------------- tools
    def _user_params(self, **extra: Any) -> dict[str, Any]:
        """Merge the selected focal user into per-user endpoint params."""
        params = dict(extra)
        if self.focal_user:
            params["user"] = self.focal_user
        return params

    def monthly_summary(
        self, month: str | None = None, account_type: str | None = None
    ) -> dict[str, Any]:
        return self._request(
            "/api/v1/monthly-summary",
            params=self._user_params(month=month, account_type=account_type),
        )

    def category_breakdown(
        self, month: str | None = None, account_type: str | None = None
    ) -> dict[str, Any]:
        return self._request(
            "/api/v1/category-breakdown",
            params=self._user_params(month=month, account_type=account_type),
        )

    def budget_status(
        self, month: str | None = None, account_type: str | None = None
    ) -> dict[str, Any]:
        return self._request(
            "/api/v1/budget-status",
            params=self._user_params(month=month, account_type=account_type),
        )

    def recurring_payments(self) -> dict[str, Any]:
        return self._request("/api/v1/recurring-payments", params=self._user_params())

    def spend_spikes(self) -> dict[str, Any]:
        return self._request("/api/v1/spend-spikes")

    def financial_health(self) -> dict[str, Any]:
        return self._request("/api/v1/financial-health", params=self._user_params())

    def forecast_next_month(self) -> dict[str, Any]:
        return self._request("/api/v1/forecast", params=self._user_params())

    def risk_scored_transactions(
        self,
        limit: int = 15,
        threshold: float | None = None,
        focal_only: bool = False,
        include_explanations: bool = False,
        account_type: str | None = None,
    ) -> dict[str, Any]:
        return self._request(
            "/api/v1/risk-scored",
            params=self._user_params(
                limit=limit,
                threshold=threshold,
                focal_only=focal_only,
                include_explanations=include_explanations,
                account_type=account_type,
            ),
        )

    def top_tips(self) -> dict[str, Any]:
        return self._request("/api/v1/tips", params=self._user_params())

    def similar_transactions(self, transaction_id: int | None = None, k: int = 5) -> dict[str, Any]:
        """Phase B.1 — nearest transactions in feature space with fraud labels.

        Mirrors ``FinanceFacts.find_similar_transactions`` so the Fraud page
        renders the same comparison set in service mode. Ledger-wide, so no
        focal-user param is needed (matches the facts implementation).
        """
        return self._request(
            "/api/v1/similar-transactions",
            params={"transaction_id": transaction_id, "k": k},
        )
