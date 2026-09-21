"""Durable SQLite state: run intents, order intents, fill deltas, ownership.

Every order intent is persisted BEFORE any POST is attempted, keyed by a
deterministic client order ID. A restart reconciles saved intents against the
broker by that ID instead of blindly resubmitting, so an accepted-but-timed-out
order can never be duplicated.

This module is pure storage: no network calls, no clock reads. Callers (e.g.
bot.run) decide when to sync against Alpaca.
"""
from __future__ import annotations

import hashlib
import json
import sqlite3
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

DEFAULT_STATE_PATH = Path("state") / "bot-state.sqlite3"

# Intent status vocabulary:
#   pending          recorded, PROVEN never attempted (submitted_at IS NULL;
#                    the POST was not sent or crashed before it could be)
#   attempted        transmission attempt durably stamped BEFORE the POST;
#                    on restart must be resolved by broker client-ID lookup
#   submitted/live   POST succeeded or broker confirmed via client-ID lookup
#   unknown          transmission attempted but unresolved (lookup failed or
#                    broker has no such order); conservative, never reposted
#   abandoned        recorded but never sent; superseded (safe: broker has no order)
#   filled/canceled/expired/rejected   terminal broker outcomes
TERMINAL_STATUSES = frozenset({"filled", "canceled", "expired", "rejected", "abandoned"})

# Broker order statuses that still sit on the book (not terminal).
LIVE_BROKER_STATUSES = frozenset(
    {"new", "accepted", "pending_new", "partially_filled", "held", "accepted_for_bidding", "done_for_day", "pending_cancel", "calculated"}
)

# Fractional-share float tolerance for ownership-vs-broker position matching.
QTY_MATCH_TOLERANCE = 0.0005


def now_utc() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def normalize_broker_status(status: str | None) -> str:
    """Map a broker order status onto our intent status vocabulary."""
    lowered = (status or "").lower()
    if lowered in TERMINAL_STATUSES:
        return lowered
    if lowered in LIVE_BROKER_STATUSES or lowered == "partially_filled":
        return lowered if lowered in {"partially_filled"} else "submitted"
    # Unrecognized statuses are treated as still live: never assume terminal.
    return "submitted"


def client_order_id(month_key: str, attempt: int, symbol: str, side: str) -> str:
    """Deterministic ID: the same intended trade always maps to the same ID."""
    digest = hashlib.sha256(f"{month_key}|{attempt}|{symbol}|{side}".encode("utf-8")).hexdigest()[:10]
    return f"v1-{month_key}-a{attempt}-{symbol.lower()}-{side}-{digest}"


@dataclass(frozen=True)
class OrderIntent:
    client_order_id: str
    run_id: str
    symbol: str
    side: str
    qty: float | None
    notional: float | None
    status: str
    broker_order_id: str | None
    submitted_at: str | None
    recorded_filled_qty: float
    note: str | None


@dataclass(frozen=True)
class RunRecord:
    run_id: str
    kind: str
    month_key: str
    trade_date: str
    target_weights: dict[str, float]
    managed_equity: float
    status: str


@dataclass(frozen=True)
class ExecutionLease:
    """Opaque ownership token for the single active execution process."""

    token: str
    fence: int


_SCHEMA = """
CREATE TABLE IF NOT EXISTS runs (
    run_id TEXT PRIMARY KEY,
    kind TEXT NOT NULL,
    month_key TEXT NOT NULL,
    trade_date TEXT NOT NULL,
    target_weights TEXT NOT NULL,
    managed_equity REAL NOT NULL,
    status TEXT NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_runs_month ON runs(month_key);

CREATE TABLE IF NOT EXISTS order_intents (
    client_order_id TEXT PRIMARY KEY,
    run_id TEXT NOT NULL REFERENCES runs(run_id),
    symbol TEXT NOT NULL,
    side TEXT NOT NULL CHECK (side IN ('buy', 'sell')),
    qty REAL,
    notional REAL,
    status TEXT NOT NULL,
    broker_order_id TEXT,
    submitted_at TEXT,
    recorded_filled_qty REAL NOT NULL DEFAULT 0,
    note TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    UNIQUE (run_id, symbol, side)
);

CREATE TABLE IF NOT EXISTS order_fills (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    client_order_id TEXT NOT NULL REFERENCES order_intents(client_order_id),
    broker_order_id TEXT,
    cumulative_filled_qty REAL NOT NULL,
    delta_qty REAL NOT NULL,
    fill_price REAL,
    recorded_at TEXT NOT NULL,
    UNIQUE (client_order_id, cumulative_filled_qty)
);

CREATE TABLE IF NOT EXISTS ownership (
    symbol TEXT PRIMARY KEY,
    qty REAL NOT NULL DEFAULT 0,
    updated_at TEXT NOT NULL
);

-- A lease is never auto-expired: allowing a stale process to resume after an
-- expiry could overlap order submission. A crash therefore fails closed until
-- an operator investigates the durable state.
CREATE TABLE IF NOT EXISTS execution_leases (
    name TEXT PRIMARY KEY CHECK (name = 'execution'),
    token TEXT NOT NULL,
    fence INTEGER NOT NULL,
    acquired_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS execution_lease_fences (
    name TEXT PRIMARY KEY CHECK (name = 'execution'),
    last_fence INTEGER NOT NULL
);
"""


class StateStore:
    def __init__(self, path: Path = DEFAULT_STATE_PATH) -> None:
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        self._connection = sqlite3.connect(path)
        self._connection.execute("PRAGMA foreign_keys = ON")
        self._connection.executescript(_SCHEMA)
        self._connection.commit()

    def close(self) -> None:
        self._connection.close()

    # ----- execution lease -----

    def acquire_execution_lease(self) -> ExecutionLease | None:
        """Atomically acquire the single execution lease, or fail closed.

        SQLite's write transaction serializes competing processes. The
        monotonically increasing fence makes a released-and-reacquired lease
        distinguishable from its former holder.
        """
        lease = ExecutionLease(token=uuid.uuid4().hex, fence=0)
        try:
            self._connection.execute("BEGIN IMMEDIATE")
            held = self._connection.execute("SELECT 1 FROM execution_leases WHERE name = 'execution'").fetchone()
            if held is not None:
                self._connection.rollback()
                return None
            self._connection.execute(
                "INSERT OR IGNORE INTO execution_lease_fences (name, last_fence) VALUES ('execution', 0)"
            )
            self._connection.execute(
                "UPDATE execution_lease_fences SET last_fence = last_fence + 1 WHERE name = 'execution'"
            )
            fence = int(
                self._connection.execute(
                    "SELECT last_fence FROM execution_lease_fences WHERE name = 'execution'"
                ).fetchone()[0]
            )
            self._connection.execute(
                "INSERT INTO execution_leases (name, token, fence, acquired_at) VALUES ('execution', ?, ?, ?)",
                (lease.token, fence, now_utc()),
            )
            self._connection.commit()
        except Exception:
            self._connection.rollback()
            raise
        return ExecutionLease(token=lease.token, fence=fence)

    def holds_execution_lease(self, lease: ExecutionLease) -> bool:
        """Fence every POST against a replacement execution process."""
        return (
            self._connection.execute(
                "SELECT 1 FROM execution_leases WHERE name = 'execution' AND token = ? AND fence = ?",
                (lease.token, lease.fence),
            ).fetchone()
            is not None
        )

    def release_execution_lease(self, lease: ExecutionLease) -> None:
        """Release only this holder's lease; never delete a newer fence."""
        self._connection.execute(
            "DELETE FROM execution_leases WHERE name = 'execution' AND token = ? AND fence = ?",
            (lease.token, lease.fence),
        )
        self._connection.commit()

    # ----- runs -----

    def begin_run(
        self, *, run_id: str, kind: str, month_key: str, trade_date: str, target_weights: dict[str, float], managed_equity: float
    ) -> None:
        """Persist the monthly intent (targets) BEFORE any order is planned."""
        timestamp = now_utc()
        self._connection.execute(
            "INSERT OR IGNORE INTO runs (run_id, kind, month_key, trade_date, target_weights, managed_equity, status, created_at, updated_at)"
            " VALUES (?, ?, ?, ?, ?, ?, 'open', ?, ?)",
            (run_id, kind, month_key, trade_date, json.dumps(target_weights, sort_keys=True), managed_equity, timestamp, timestamp),
        )
        self._connection.commit()

    def run_for_month(self, month_key: str) -> RunRecord | None:
        row = self._connection.execute(
            "SELECT run_id, kind, month_key, trade_date, target_weights, managed_equity, status FROM runs WHERE month_key = ? ORDER BY rowid DESC LIMIT 1",
            (month_key,),
        ).fetchone()
        if row is None:
            return None
        return RunRecord(row[0], row[1], row[2], row[3], json.loads(row[4]), row[5], row[6])

    def runs_for_month(self, month_key: str) -> list[RunRecord]:
        rows = self._connection.execute(
            "SELECT run_id, kind, month_key, trade_date, target_weights, managed_equity, status FROM runs WHERE month_key = ? ORDER BY run_id",
            (month_key,),
        ).fetchall()
        return [RunRecord(r[0], r[1], r[2], r[3], json.loads(r[4]), r[5], r[6]) for r in rows]

    def count_runs_for_month(self, month_key: str) -> int:
        return int(self._connection.execute("SELECT COUNT(*) FROM runs WHERE month_key = ?", (month_key,)).fetchone()[0])

    def set_run_status(self, run_id: str, status: str) -> None:
        self._connection.execute("UPDATE runs SET status = ?, updated_at = ? WHERE run_id = ?", (status, now_utc(), run_id))
        self._connection.commit()

    # ----- order intents -----

    def record_intent(
        self, *, client_order_id: str, run_id: str, symbol: str, side: str, qty: float | None, notional: float | None
    ) -> None:
        """Persist an order intent. Called BEFORE the POST. Idempotent: an
        identical re-record (restart retry) is a no-op; a conflicting one raises."""
        existing = self.intent(client_order_id)
        if existing is not None:
            same = (
                existing.run_id == run_id
                and existing.symbol == symbol
                and existing.side == side
                and existing.qty == qty
                and existing.notional == notional
            )
            if not same:
                raise ValueError(f"Conflicting intent for existing client order id {client_order_id}")
            return
        timestamp = now_utc()
        self._connection.execute(
            "INSERT INTO order_intents (client_order_id, run_id, symbol, side, qty, notional, status, created_at, updated_at)"
            " VALUES (?, ?, ?, ?, ?, ?, 'pending', ?, ?)",
            (client_order_id, run_id, symbol, side, qty, notional, timestamp, timestamp),
        )
        self._connection.commit()

    def intent(self, client_order_id: str) -> OrderIntent | None:
        row = self._connection.execute(
            "SELECT client_order_id, run_id, symbol, side, qty, notional, status, broker_order_id, submitted_at, recorded_filled_qty, note"
            " FROM order_intents WHERE client_order_id = ?",
            (client_order_id,),
        ).fetchone()
        return self._intent_from_row(row) if row else None

    def intents(self, *, statuses: set[str] | None = None, month_key: str | None = None) -> list[OrderIntent]:
        query = (
            "SELECT i.client_order_id, i.run_id, i.symbol, i.side, i.qty, i.notional, i.status, i.broker_order_id,"
            " i.submitted_at, i.recorded_filled_qty, i.note FROM order_intents i"
        )
        params: list = []
        clauses: list[str] = []
        if statuses is not None:
            clauses.append(f"i.status IN ({', '.join('?' for _ in statuses)})")
            params.extend(sorted(statuses))
        if month_key is not None:
            clauses.append("EXISTS (SELECT 1 FROM runs r WHERE r.run_id = i.run_id AND r.month_key = ?)")
            params.append(month_key)
        if clauses:
            query += " WHERE " + " AND ".join(clauses)
        query += " ORDER BY i.created_at, i.client_order_id"
        return [self._intent_from_row(row) for row in self._connection.execute(query, params).fetchall()]

    def _intent_from_row(self, row: tuple) -> OrderIntent:
        return OrderIntent(row[0], row[1], row[2], row[3], row[4], row[5], row[6], row[7], row[8], row[9], row[10])

    def mark_submitted(self, client_order_id: str, broker_order_id: str | None) -> None:
        self._connection.execute(
            "UPDATE order_intents SET status = 'submitted', broker_order_id = COALESCE(?, broker_order_id),"
            " submitted_at = COALESCE(submitted_at, ?), updated_at = ? WHERE client_order_id = ?",
            (broker_order_id, now_utc(), now_utc(), client_order_id),
        )
        self._connection.commit()

    def mark_attempted(self, client_order_id: str) -> None:
        """Durably stamp a transmission attempt BEFORE the order POST is sent.

        Committed as its own transaction. On restart, any 'attempted' intent is
        resolved by broker client-order-ID lookup — never blindly re-POSTed —
        so only intents that are still 'pending' (proven never attempted) may
        be posted automatically. Idempotent and never downgrades a stronger
        state (submitted/live, unknown, or a terminal outcome).
        """
        timestamp = now_utc()
        self._connection.execute(
            "UPDATE order_intents SET status = 'attempted', submitted_at = COALESCE(submitted_at, ?), updated_at = ?"
            " WHERE client_order_id = ? AND status = 'pending'",
            (timestamp, timestamp, client_order_id),
        )
        self._connection.commit()

    def mark_status(self, client_order_id: str, status: str, *, note: str | None = None) -> None:
        self._connection.execute(
            "UPDATE order_intents SET status = ?, note = COALESCE(?, note), updated_at = ? WHERE client_order_id = ?",
            (status, note, now_utc(), client_order_id),
        )
        self._connection.commit()

    def add_note(self, client_order_id: str, note: str) -> None:
        self._connection.execute(
            "UPDATE order_intents SET note = COALESCE(note || ' | ', '') || ?, updated_at = ? WHERE client_order_id = ?",
            (note, now_utc(), client_order_id),
        )
        self._connection.commit()

    # ----- fill-delta ledger (idempotent reconciliation) -----

    def apply_broker_order(self, client_order_id: str, broker_order: dict) -> float:
        """Fold one broker order snapshot into state. Returns the NEW fill qty delta.

        Idempotent: replaying the same snapshot yields delta 0 because the
        fill ledger is keyed by cumulative broker filled_qty.

        ATOMICITY: the fill row, the ownership delta, the cumulative filled
        quantity, and the order status update are written in ONE SQLite
        transaction, so a crash mid-write cannot leave the ledger, ownership,
        and intent disagreeing. Any failure rolls the whole snapshot back.
        """
        intent = self.intent(client_order_id)
        if intent is None:
            raise KeyError(f"No recorded intent for client order id {client_order_id}")

        broker_id = broker_order.get("id")
        cumulative = float(broker_order.get("filled_qty") or 0)
        price = float(broker_order["filled_avg_price"]) if broker_order.get("filled_avg_price") is not None else None
        status = normalize_broker_status(broker_order.get("status"))

        # A broker cumulative filled qty below what we already recorded is a
        # stale or contradictory snapshot (e.g. replayed out of order): it must
        # never produce a negative fill delta that corrupts ownership.
        if cumulative < intent.recorded_filled_qty - 1e-12:
            raise ValueError(
                f"Broker filled qty {cumulative} is below already-recorded {intent.recorded_filled_qty} "
                f"for {client_order_id}; refusing regressive fill snapshot."
            )

        delta = cumulative - intent.recorded_filled_qty
        try:
            if abs(delta) > 1e-12:
                # UNIQUE(client_order_id, cumulative_filled_qty) makes replay a no-op.
                self._connection.execute(
                    "INSERT OR IGNORE INTO order_fills (client_order_id, broker_order_id, cumulative_filled_qty, delta_qty, fill_price, recorded_at)"
                    " VALUES (?, ?, ?, ?, ?, ?)",
                    (client_order_id, broker_id, cumulative, delta, price, now_utc()),
                )
                sign = 1.0 if intent.side == "buy" else -1.0
                self._connection.execute(
                    "INSERT INTO ownership (symbol, qty, updated_at) VALUES (?, ?, ?)"
                    " ON CONFLICT(symbol) DO UPDATE SET qty = qty + excluded.qty, updated_at = excluded.updated_at",
                    (intent.symbol, sign * delta, now_utc()),
                )
            self._connection.execute(
                "UPDATE order_intents SET status = ?, broker_order_id = COALESCE(?, broker_order_id), recorded_filled_qty = ?, updated_at = ?"
                " WHERE client_order_id = ?",
                (status, broker_id, cumulative, now_utc(), client_order_id),
            )
            # Single commit: all four writes above land together or not at all.
            self._connection.commit()
        except Exception:
            self._connection.rollback()
            raise
        return delta

    def fills(self, client_order_id: str) -> list[dict]:
        rows = self._connection.execute(
            "SELECT cumulative_filled_qty, delta_qty, fill_price, recorded_at FROM order_fills WHERE client_order_id = ? ORDER BY id",
            (client_order_id,),
        ).fetchall()
        return [
            {"cumulative_filled_qty": r[0], "delta_qty": r[1], "fill_price": r[2], "recorded_at": r[3]} for r in rows
        ]

    # ----- tracked ownership ledger -----

    def apply_fill(self, symbol: str, side: str, delta_qty: float) -> None:
        """A buy fill adds owned shares; a sell fill removes them. Own orders only."""
        sign = 1.0 if side == "buy" else -1.0
        self._connection.execute(
            "INSERT INTO ownership (symbol, qty, updated_at) VALUES (?, ?, ?)"
            " ON CONFLICT(symbol) DO UPDATE SET qty = qty + excluded.qty, updated_at = excluded.updated_at",
            (symbol, sign * delta_qty, now_utc()),
        )
        self._connection.commit()

    def ownership(self) -> dict[str, float]:
        return {row[0]: row[1] for row in self._connection.execute("SELECT symbol, qty FROM ownership").fetchall()}
