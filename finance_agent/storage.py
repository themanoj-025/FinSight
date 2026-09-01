"""SQLite persistence layer (Phase 6 stretch goal).

This resolves the CSV scaling ceiling at the root by moving the hot read
paths onto a real, queryable store:

  * ``transactions`` table — the full ledger, synced from the generated CSV.
  * ``risk_scores`` — a *materialized* per-transaction scoring table. The
    expensive path (rules + features + model inference over the whole ledger)
    runs once per (data, model) fingerprint; the interactive risk scan then
    becomes a SQL point query (threshold / focal / limit) instead of
    re-filtering a full in-memory scored frame on every Streamlit rerun.
  * Hand-rolled migrations — ``PRAGMA user_version`` plus an ordered
    migration list, so schema evolution is explicit and auditable (no alembic
    dependency needed for a schema this small).

Deliberately stdlib-only (``sqlite3``): the project removed xgboost to keep
the install lean, and this feature does not justify a new wheel. DuckDB would
be the natural upgrade if the ledger ever outgrows SQLite's single-writer
model — see docs/KNOWN_LIMITATIONS.md.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Any

import pandas as pd

# Canonical transaction columns, in the exact order generate_data writes them:
# the legacy PaySim-style columns first (existing consumers), then the data-gen
# v2 additive columns (persona/account/region/subcategory/archetype metadata).
TX_COLUMNS = [
    "step",
    "type",
    "amount",
    "nameOrig",
    "oldbalanceOrg",
    "newbalanceOrig",
    "nameDest",
    "oldbalanceDest",
    "newbalanceDest",
    "merchant",
    "category",
    "datetime",
    "date",
    "is_focal_user",
    "isFraud",
    "isFlaggedFraud",
    "is_anomaly",
    "anomaly_type",
    # data-gen v2 (finance_agent/datagen.py NEW_COLUMNS)
    "persona_id",
    "persona_archetype",
    "account_type",
    "merchant_region",
    "transaction_region",
    "home_region",
    "category_group",
    "subcategory",
    "fraud_archetype",
    "label_reported_at_step",
    "simulation_year",
]

# Defaults applied to TX_COLUMNS missing from an input frame (legacy CSVs).
_DEFAULT_FOR_COLUMN: dict[str, object] = {
    "persona_id": "",
    "persona_archetype": "",
    "account_type": "checking",
    "merchant_region": "",
    "transaction_region": "",
    "home_region": "",
    "category_group": "",
    "subcategory": "",
    "fraud_archetype": "",
    "label_reported_at_step": 0,
    "simulation_year": 0,
}

SCHEMA_VERSION = 2

_V2_COLUMNS = [
    ("persona_id", "TEXT NOT NULL DEFAULT ''"),
    ("persona_archetype", "TEXT NOT NULL DEFAULT ''"),
    ("account_type", "TEXT NOT NULL DEFAULT 'checking'"),
    ("merchant_region", "TEXT NOT NULL DEFAULT ''"),
    ("transaction_region", "TEXT NOT NULL DEFAULT ''"),
    ("home_region", "TEXT NOT NULL DEFAULT ''"),
    ("category_group", "TEXT NOT NULL DEFAULT ''"),
    ("subcategory", "TEXT NOT NULL DEFAULT ''"),
    ("fraud_archetype", "TEXT NOT NULL DEFAULT ''"),
    ("label_reported_at_step", "INTEGER"),
    ("simulation_year", "INTEGER"),
]

_MIGRATIONS: list[tuple[int, str, list[str]]] = [
    (
        1,
        "initial schema: meta, transactions, risk_scores",
        [
            """CREATE TABLE IF NOT EXISTS meta (
                   key   TEXT PRIMARY KEY,
                   value TEXT NOT NULL
               )""",
            """CREATE TABLE IF NOT EXISTS transactions (
                   id               INTEGER PRIMARY KEY,
                   step             INTEGER NOT NULL,
                   type             TEXT    NOT NULL,
                   amount           REAL    NOT NULL,
                   nameOrig         TEXT    NOT NULL,
                   oldbalanceOrg    REAL    NOT NULL,
                   newbalanceOrig   REAL    NOT NULL,
                   nameDest         TEXT    NOT NULL,
                   oldbalanceDest   REAL    NOT NULL,
                   newbalanceDest   REAL    NOT NULL,
                   isFraud          INTEGER NOT NULL,
                   isFlaggedFraud   INTEGER NOT NULL,
                   merchant         TEXT    NOT NULL,
                   category         TEXT    NOT NULL,
                   datetime         TEXT    NOT NULL,
                   date             TEXT    NOT NULL,
                   is_focal_user    INTEGER NOT NULL,
                   is_anomaly       INTEGER NOT NULL,
                   anomaly_type     TEXT    NOT NULL
               )""",
            "CREATE INDEX IF NOT EXISTS idx_tx_step ON transactions(step)",
            "CREATE INDEX IF NOT EXISTS idx_tx_focal ON transactions(is_focal_user)",
            "CREATE INDEX IF NOT EXISTS idx_tx_category ON transactions(category)",
            "CREATE INDEX IF NOT EXISTS idx_tx_type ON transactions(type)",
            "CREATE INDEX IF NOT EXISTS idx_tx_merchant ON transactions(merchant)",
            "CREATE INDEX IF NOT EXISTS idx_tx_nameorig ON transactions(nameOrig)",
            """CREATE TABLE IF NOT EXISTS risk_scores (
                   transaction_id  INTEGER PRIMARY KEY
                                   REFERENCES transactions(id) ON DELETE CASCADE,
                   risk_score      REAL NOT NULL,
                   rule_score      REAL NOT NULL,
                   model_score     REAL NOT NULL,
                   isolation_score REAL NOT NULL,
                   rule_reason     TEXT NOT NULL DEFAULT '',
                   reason          TEXT NOT NULL DEFAULT ''
               )""",
            "CREATE INDEX IF NOT EXISTS idx_risk_score ON risk_scores(risk_score)",
        ],
    ),
    (
        2,
        "data-gen v2: persona/account/region/subcategory/archetype columns",
        [
            *(f"ALTER TABLE transactions ADD COLUMN {name} {dtype}" for name, dtype in _V2_COLUMNS),
            "CREATE INDEX IF NOT EXISTS idx_tx_account_type ON transactions(account_type)",
            "CREATE INDEX IF NOT EXISTS idx_tx_region ON transactions(transaction_region)",
            "CREATE INDEX IF NOT EXISTS idx_tx_category_group ON transactions(category_group)",
        ],
    ),
]

_SCORE_COLUMNS = [
    "risk_score",
    "rule_score",
    "model_score",
    "isolation_score",
    "rule_reason",
    "reason",
]


def _fetchall_df(conn: sqlite3.Connection, sql: str, params: list[Any]) -> pd.DataFrame:
    """Run a query and return the result as a DataFrame (no pandas read_sql)."""
    cur = conn.execute(sql, params)
    columns = [desc[0] for desc in cur.description]
    return pd.DataFrame(cur.fetchall(), columns=columns)


class TransactionStore:
    """SQLite persistence for the ledger + materialized risk scores.

    Fingerprints gate every expensive operation: syncing from the CSV and
    materializing scores both record their inputs' fingerprints, so repeated
    work is skipped unless the data or the model bundle actually changed.
    """

    def __init__(self, path: str) -> None:
        self.path = str(path)
        if self.path != ":memory:":
            Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        self.migrate()

    # ------------------------------------------------------------ connection
    def _connect(self) -> sqlite3.Connection:
        return sqlite3.connect(self.path)

    def migrate(self) -> None:
        """Apply any pending migrations (gated by PRAGMA user_version)."""
        conn = self._connect()
        try:
            current = conn.execute("PRAGMA user_version").fetchone()[0]
            for version, _desc, statements in _MIGRATIONS:
                if version <= current:
                    continue
                cur = conn.cursor()
                for ddl in statements:
                    cur.execute(ddl)
                # PRAGMA user_version does not accept bind parameters; the
                # version is an int literal from the migration table above.
                cur.execute(f"PRAGMA user_version = {int(version)}")
            conn.commit()
        finally:
            conn.close()

    # ------------------------------------------------------------------ meta
    def _get_meta(self, conn: sqlite3.Connection, key: str) -> str | None:
        row = conn.execute("SELECT value FROM meta WHERE key = ?", (key,)).fetchone()
        return str(row[0]) if row else None

    def _set_meta(self, conn: sqlite3.Connection, key: str, value: str) -> None:
        conn.execute("INSERT OR REPLACE INTO meta(key, value) VALUES (?, ?)", (key, value))

    def schema_version(self) -> int:
        conn = self._connect()
        try:
            return int(conn.execute("PRAGMA user_version").fetchone()[0])
        finally:
            conn.close()

    # -------------------------------------------------------- transactions
    def is_synced(self, csv_fingerprint: str) -> bool:
        conn = self._connect()
        try:
            return self._get_meta(conn, "csv_fingerprint") == csv_fingerprint
        finally:
            conn.close()

    def sync_from_frame(self, df: pd.DataFrame, csv_fingerprint: str) -> bool:
        """Bulk-replace the transactions table from `df` (id = row position).

        Returns True when rows were actually (re)loaded, False when the table
        was already synced for this fingerprint.
        """
        conn = self._connect()
        try:
            if self._get_meta(conn, "csv_fingerprint") == csv_fingerprint:
                count = conn.execute("SELECT COUNT(*) FROM transactions").fetchone()[0]
                if count:
                    return False
            df = df.reset_index(drop=True)
            # Tolerate legacy CSVs missing the v2 columns: fill defaults so the
            # NOT NULL schema constraint is satisfied (migration 2 added them).
            for col in TX_COLUMNS:
                if col not in df.columns:
                    df[col] = _DEFAULT_FOR_COLUMN.get(col, "")
            # read_csv turns empty cells (e.g. anomaly_type="") into NaN/<NA>;
            # text columns are NOT NULL, so normalize non-numeric NaN back to
            # "" first (handles both the legacy object and pandas-3 `str` dtypes).
            for col in df.columns:
                if not pd.api.types.is_numeric_dtype(df[col]) and df[col].isna().any():
                    df[col] = df[col].fillna("")
            cols = ", ".join(TX_COLUMNS)
            placeholders = ", ".join("?" * (len(TX_COLUMNS) + 1))
            payload: list[tuple[Any, ...]] = [
                (int(i),) + tuple(None if pd.isna(v) else v for v in row)
                for i, row in zip(
                    df.index, df[TX_COLUMNS].itertuples(index=False, name=None), strict=True
                )
            ]
            cur = conn.cursor()
            cur.execute("DELETE FROM risk_scores")
            cur.execute("DELETE FROM transactions")
            cur.executemany(
                f"INSERT INTO transactions(id, {cols}) VALUES ({placeholders})", payload
            )
            self._set_meta(conn, "csv_fingerprint", csv_fingerprint)
            conn.commit()
            return True
        finally:
            conn.close()

    def sync_from_csv(self, csv_path: str, csv_fingerprint: str) -> bool:
        return self.sync_from_frame(pd.read_csv(csv_path), csv_fingerprint)

    def total_rows(self) -> int:
        conn = self._connect()
        try:
            return int(conn.execute("SELECT COUNT(*) FROM transactions").fetchone()[0])
        finally:
            conn.close()

    def transactions_df(self) -> pd.DataFrame:
        conn = self._connect()
        try:
            df = _fetchall_df(
                conn, f"SELECT id, {', '.join(TX_COLUMNS)} FROM transactions ORDER BY id", []
            )
            return df.drop(columns=["id"]) if "id" in df.columns else df
        finally:
            conn.close()

    # ----------------------------------------------------- materialized risk
    def is_risk_materialized(self, risk_fingerprint: str) -> bool:
        conn = self._connect()
        try:
            return self._get_meta(conn, "risk_fingerprint") == risk_fingerprint
        finally:
            conn.close()

    def materialize_risk_scores(self, risk_fingerprint: str, scored: pd.DataFrame) -> None:
        """Replace the materialized scores with a freshly computed scored frame.

        `scored` must carry a `_row_index` column whose values match the
        transactions table ids (original CSV row positions).
        """
        conn = self._connect()
        try:
            keys = ["_row_index", *_SCORE_COLUMNS]
            payload: list[tuple[Any, ...]] = []
            for rec in scored[keys].to_dict(orient="records"):
                payload.append(
                    (
                        int(rec["_row_index"]),
                        float(rec["risk_score"]),
                        float(rec["rule_score"]),
                        float(rec["model_score"]),
                        float(rec["isolation_score"]),
                        str(rec["rule_reason"] or ""),
                        str(rec["reason"] or ""),
                    )
                )
            cur = conn.cursor()
            cur.execute("DELETE FROM risk_scores")
            cur.executemany(
                "INSERT OR REPLACE INTO risk_scores(transaction_id, risk_score, rule_score,"
                " model_score, isolation_score, rule_reason, reason) VALUES (?,?,?,?,?,?,?)",
                payload,
            )
            self._set_meta(conn, "risk_fingerprint", risk_fingerprint)
            conn.commit()
        finally:
            conn.close()

    def risk_scores(
        self,
        threshold: float,
        focal_only: bool,
        limit: int,
        focal_user: str | None = None,
        account_type: str | None = None,
    ) -> pd.DataFrame -> None:
        """Top-risk rows with the same columns the in-memory path returns.

        `_row_index` aliases `transactions.id` (0-based CSV row position), so
        downstream SHAP mapping and the row-dict build stay identical to the
        pandas path. Ties are broken by id (CSV order) for determinism.

        `focal_only` narrows to the focal users; when `focal_user` is also given
        it narrows to that single selected user (multi-user mode).
        `account_type` (when given) narrows to one account channel.
        """
        cols = [f"t.{c} AS {c}" for c in TX_COLUMNS] + [f"r.{c}" for c in _SCORE_COLUMNS]
        sql = (
            f"SELECT t.id AS _row_index, {', '.join(cols)}"
            " FROM risk_scores r JOIN transactions t ON t.id = r.transaction_id"
            " WHERE r.risk_score >= ?"
        )
        params: list[Any] = [float(threshold)]
        if focal_only:
            if focal_user:
                sql += " AND t.nameOrig = ?"
                params.append(focal_user)
            else:
                sql += " AND t.is_focal_user = 1"
        if account_type:
            sql += " AND t.account_type = ?"
            params.append(account_type)
        sql += " ORDER BY r.risk_score DESC, t.id ASC LIMIT ?"
        params.append(int(limit))
        conn = self._connect()
        try:
            df = _fetchall_df(conn, sql, params)
        finally:
            conn.close()
        if not df.empty:
            df["is_focal_user"] = df["is_focal_user"].astype(bool)
        return df

    def flagged_count(
        self,
        threshold: float,
        focal_only: bool,
        focal_user: str | None = None,
        account_type: str | None = None,
    ) -> int -> None:
        sql = (
            "SELECT COUNT(*) FROM risk_scores r"
            " JOIN transactions t ON t.id = r.transaction_id"
            " WHERE r.risk_score >= ?"
        )
        params: list[Any] = [float(threshold)]
        if focal_only:
            if focal_user:
                sql += " AND t.nameOrig = ?"
                params.append(focal_user)
            else:
                sql += " AND t.is_focal_user = 1"
        if account_type:
            sql += " AND t.account_type = ?"
            params.append(account_type)
        conn = self._connect()
        try:
            return int(conn.execute(sql, params).fetchone()[0])
        finally:
            conn.close()

    # -------------------------------------------------------------- lifecycle
    def reset(self) -> None:
        """Drop all data + fingerprints (called after regeneration/retrain)."""
        conn = self._connect()
        try:
            cur = conn.cursor()
            cur.execute("DELETE FROM risk_scores")
            cur.execute("DELETE FROM transactions")
            cur.execute("DELETE FROM meta")
            conn.commit()
        finally:
            conn.close()


class SessionBudgetStore:
    """Persisted per-session LLM budget + exact token usage (C.1.2).

    Keeps a small SQLite table keyed by session id (config ``agent.budget_store``,
    default ``data/session_usage.db``) recording turn counts and the *exact*
    input/output token counts the Anthropic API reports. Because it lives on
    disk, a Streamlit page reload (which resets ``st.session_state``) can no
    longer silently reset the LLM cost cap — enforcement is server-side.
    """

    def __init__(self, path: str) -> None:
        self.path = str(path)
        if self.path != ":memory:":
            Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(self.path)
        try:
            conn.execute(
                """CREATE TABLE IF NOT EXISTS session_usage (
                       session_id   TEXT PRIMARY KEY,
                       turns        INTEGER NOT NULL DEFAULT 0,
                       input_tokens INTEGER NOT NULL DEFAULT 0,
                       output_tokens INTEGER NOT NULL DEFAULT 0,
                       est_cost     REAL NOT NULL DEFAULT 0.0,
                       updated_at   TEXT NOT NULL DEFAULT (datetime('now'))
                   )"""
            )
            conn.commit()
        finally:
            conn.close()

    def record_turn(self, session_id: str) -> None:
        conn = sqlite3.connect(self.path)
        try:
            conn.execute(
                "INSERT INTO session_usage(session_id, turns) VALUES (?, 1) "
                "ON CONFLICT(session_id) DO UPDATE SET turns = turns + 1, "
                "updated_at = datetime('now')",
                (session_id,),
            )
            conn.commit()
        finally:
            conn.close()

    def record_usage(
        self, session_id: str, input_tokens: int, output_tokens: int, est_cost: float = 0.0
    ) -> None -> None:
        conn = sqlite3.connect(self.path)
        try:
            conn.execute(
                "INSERT INTO session_usage(session_id, input_tokens, output_tokens, est_cost) "
                "VALUES (?, ?, ?, ?) "
                "ON CONFLICT(session_id) DO UPDATE SET "
                "  input_tokens = input_tokens + excluded.input_tokens, "
                "  output_tokens = output_tokens + excluded.output_tokens, "
                "  est_cost = est_cost + excluded.est_cost, "
                "  updated_at = datetime('now')",
                (session_id, int(input_tokens), int(output_tokens), float(est_cost)),
            )
            conn.commit()
        finally:
            conn.close()

    def totals(self, session_id: str) -> dict[str, float]:
        conn = sqlite3.connect(self.path)
        try:
            row = conn.execute(
                "SELECT turns, input_tokens, output_tokens, est_cost FROM session_usage "
                "WHERE session_id = ?",
                (session_id,),
            ).fetchone()
        finally:
            conn.close()
        if row is None:
            return {"turns": 0, "input_tokens": 0, "output_tokens": 0, "est_cost": 0.0}
        return {
            "turns": int(row[0]),
            "input_tokens": int(row[1]),
            "output_tokens": int(row[2]),
            "est_cost": float(row[3]),
        }

    def sessions(self, limit: int = 100) -> list[dict[str, Any]]:
        """Recent per-session totals (used by the Trust/Settings dashboards)."""
        conn = sqlite3.connect(self.path)
        try:
            rows = conn.execute(
                "SELECT session_id, turns, input_tokens, output_tokens, est_cost, updated_at "
                "FROM session_usage ORDER BY updated_at DESC LIMIT ?",
                (int(limit),),
            ).fetchall()
        finally:
            conn.close()
        return [
            {
                "session_id": r[0],
                "turns": int(r[1]),
                "input_tokens": int(r[2]),
                "output_tokens": int(r[3]),
                "est_cost": round(float(r[4]), 6),
                "updated_at": r[5],
            }
            for r in rows
        ]


def reset_store_for_config(config_path: str) -> None:
    """Best-effort reset of the store referenced by `config_path` (no-op if none)."""
    import yaml

    try:
        with open(config_path, encoding="utf-8") as fh:
            cfg = yaml.safe_load(fh) or {}
        store_path = (cfg.get("data") or {}).get("store_path")
        if store_path:
            TransactionStore(str(store_path)).reset()
    except (OSError, ValueError):
        pass
