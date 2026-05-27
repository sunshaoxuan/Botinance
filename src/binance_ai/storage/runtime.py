from __future__ import annotations

import json
import os
import time
from dataclasses import asdict, is_dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Protocol

from binance_ai.config import Settings, load_settings

try:  # optional runtime dependency so file mode still works without PostgreSQL driver
    import psycopg  # type: ignore
except Exception:  # pragma: no cover - exercised when dependency is absent
    psycopg = None  # type: ignore


def to_plain(value: Any) -> Any:
    if is_dataclass(value):
        return asdict(value)
    if isinstance(value, dict):
        return {str(k): to_plain(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [to_plain(item) for item in value]
    return value


def month_suffix(timestamp_ms: int) -> str:
    if timestamp_ms <= 0:
        timestamp_ms = int(time.time() * 1000)
    dt = datetime.fromtimestamp(timestamp_ms / 1000, tz=timezone.utc)
    return f"{dt.year:04d}_{dt.month:02d}"


def table_name(prefix: str, timestamp_ms: int) -> str:
    return f"{prefix}_{month_suffix(timestamp_ms)}"


def database_url_from_settings(settings: Settings) -> str:
    password = os.getenv(settings.db_password_env, "")
    auth = settings.db_user if not password else f"{settings.db_user}:{password}"
    return f"postgresql://{auth}@{settings.db_host}:{settings.db_port}/{settings.db_name}"


class StorageUnavailable(RuntimeError):
    pass


class RuntimeStore(Protocol):
    def write_cycle_report(self, report: Any) -> None:
        ...

    def write_portfolio_snapshot(self, snapshot: Any) -> None:
        ...

    def query_order_records(self, limit: int = 1000, status: str = "all") -> Dict[str, Any]:
        ...

    def storage_status(self) -> Dict[str, Any]:
        ...


class NullRuntimeStore:
    def write_cycle_report(self, report: Any) -> None:
        return

    def write_portfolio_snapshot(self, snapshot: Any) -> None:
        return

    def query_order_records(self, limit: int = 1000, status: str = "all") -> Dict[str, Any]:
        raise StorageUnavailable("postgres storage is disabled")

    def query_ops_summary(self, hours: int) -> Dict[str, Any]:
        raise StorageUnavailable("postgres storage is disabled")

    def storage_status(self) -> Dict[str, Any]:
        return {
            "enabled": False,
            "driver": "none",
            "read_mode": "file",
            "write_mode": "file",
            "ok": False,
            "error": "postgres storage is disabled",
            "last_query_ms": 0,
            "partitions": [],
        }


class SafeRuntimeStore:
    def __init__(self, inner: RuntimeStore | None, settings: Settings | None = None) -> None:
        self.inner = inner
        self.settings = settings or load_settings()
        self.last_error = ""

    def write_cycle_report(self, report: Any) -> None:
        if self.inner is None:
            return
        try:
            self.inner.write_cycle_report(report)
            self.last_error = ""
        except Exception as exc:  # noqa: BLE001
            self.last_error = str(exc)[:240]

    def write_portfolio_snapshot(self, snapshot: Any) -> None:
        if self.inner is None:
            return
        try:
            self.inner.write_portfolio_snapshot(snapshot)
            self.last_error = ""
        except Exception as exc:  # noqa: BLE001
            self.last_error = str(exc)[:240]

    def query_order_records(self, limit: int = 1000, status: str = "all") -> Dict[str, Any]:
        if self.inner is None:
            raise StorageUnavailable(self.last_error or "postgres storage is disabled")
        return self.inner.query_order_records(limit=limit, status=status)

    def query_ops_summary(self, hours: int) -> Dict[str, Any]:
        if self.inner is None or not hasattr(self.inner, "query_ops_summary"):
            raise StorageUnavailable(self.last_error or "postgres summary is disabled")
        return getattr(self.inner, "query_ops_summary")(hours)

    def storage_status(self) -> Dict[str, Any]:
        if self.inner is None:
            status = NullRuntimeStore().storage_status()
            status["enabled"] = self.settings.db_enabled
            status["driver"] = self.settings.db_driver
            status["error"] = self.last_error or status["error"]
            return status
        try:
            status = self.inner.storage_status()
            if self.last_error:
                status["write_warning"] = self.last_error
            return status
        except Exception as exc:  # noqa: BLE001
            return {
                "enabled": self.settings.db_enabled,
                "driver": self.settings.db_driver,
                "read_mode": self.settings.db_read_mode,
                "write_mode": self.settings.db_write_mode,
                "ok": False,
                "error": str(exc)[:240],
                "last_query_ms": 0,
                "partitions": [],
            }


class PostgresRuntimeStore:
    def __init__(self, settings: Settings | None = None, database_url: str | None = None) -> None:
        self.settings = settings or load_settings()
        self.database_url = database_url or database_url_from_settings(self.settings)
        self.last_error = ""
        self.last_query_ms = 0
        self._partition_cache: set[str] = set()
        if psycopg is None:
            raise StorageUnavailable("psycopg is not installed")

    def connect(self):
        if psycopg is None:
            raise StorageUnavailable("psycopg is not installed")
        return psycopg.connect(self.database_url, connect_timeout=self.settings.db_query_timeout_seconds)

    def ping(self) -> bool:
        try:
            with self.connect() as conn:
                with conn.cursor() as cur:
                    cur.execute("select 1")
                    cur.fetchone()
            self.last_error = ""
            return True
        except Exception as exc:
            self.last_error = str(exc)[:240]
            return False

    def ensure_schema(self) -> None:
        with self.connect() as conn:
            with conn.cursor() as cur:
                self._ensure_meta(cur)
            conn.commit()

    def _ensure_meta(self, cur: Any) -> None:
        cur.execute(
            """
            create table if not exists storage_meta (
              key text primary key,
              value text not null,
              updated_at timestamptz not null default now()
            )
            """
        )

    def ensure_month(self, timestamp_ms: int) -> None:
        suffix = month_suffix(timestamp_ms)
        if suffix in self._partition_cache:
            return
        with self.connect() as conn:
            with conn.cursor() as cur:
                self._ensure_meta(cur)
                self._ensure_month_tables(cur, suffix)
            conn.commit()
        self._partition_cache.add(suffix)

    def _ensure_month_tables(self, cur: Any, suffix: str) -> None:
        cur.execute(
            f"""
            create table if not exists cycle_reports_{suffix} (
              id bigserial primary key,
              timestamp_ms bigint not null,
              cycle_mode text,
              cycle_reason text,
              symbol text,
              payload jsonb not null,
              created_at timestamptz not null default now(),
              unique(timestamp_ms, symbol)
            )
            """
        )
        cur.execute(
            f"""
            create table if not exists orders_{suffix} (
              client_order_id text primary key,
              symbol text,
              side text,
              status text,
              quantity numeric,
              limit_price numeric,
              trigger text,
              pair_id text,
              created_at_ms bigint,
              updated_at_ms bigint,
              payload jsonb not null
            )
            """
        )
        cur.execute(
            f"""
            create table if not exists fills_{suffix} (
              client_order_id text not null,
              symbol text,
              side text,
              quantity numeric,
              fill_price numeric,
              fee numeric,
              realized_pnl_delta numeric,
              trigger text,
              pair_id text,
              timestamp_ms bigint not null,
              payload jsonb not null,
              primary key(client_order_id, timestamp_ms, side)
            )
            """
        )
        cur.execute(
            f"""
            create table if not exists order_events_{suffix} (
              client_order_id text not null,
              symbol text,
              side text,
              status text,
              event_type text,
              reason text,
              timestamp_ms bigint not null,
              payload jsonb not null,
              primary key(client_order_id, status, timestamp_ms)
            )
            """
        )
        cur.execute(
            f"""
            create table if not exists decision_ledger_{suffix} (
              timestamp_ms bigint not null,
              cycle_mode text,
              symbol text,
              price numeric,
              final_action text,
              scenario text,
              payload jsonb not null,
              primary key(timestamp_ms, symbol)
            )
            """
        )
        cur.execute(
            f"""
            create table if not exists candles_{suffix} (
              symbol text not null,
              interval text not null,
              open_time_ms bigint not null,
              close_time_ms bigint,
              open numeric,
              high numeric,
              low numeric,
              close numeric,
              volume numeric,
              primary key(symbol, interval, open_time_ms)
            )
            """
        )
        for table in ("cycle_reports", "orders", "fills", "order_events", "decision_ledger"):
            cur.execute(f"create index if not exists {table}_{suffix}_ts_idx on {table}_{suffix} (timestamp_ms desc)" if table != "orders" else f"create index if not exists orders_{suffix}_updated_idx on orders_{suffix} (updated_at_ms desc)")
        cur.execute(f"create index if not exists orders_{suffix}_symbol_status_idx on orders_{suffix} (symbol, status, updated_at_ms desc)")
        cur.execute(f"create index if not exists orders_{suffix}_pair_idx on orders_{suffix} (pair_id)")
        cur.execute(f"create index if not exists fills_{suffix}_symbol_ts_idx on fills_{suffix} (symbol, timestamp_ms desc)")
        cur.execute(f"create index if not exists order_events_{suffix}_symbol_status_idx on order_events_{suffix} (symbol, status, timestamp_ms desc)")
        cur.execute(f"create index if not exists candles_{suffix}_symbol_interval_idx on candles_{suffix} (symbol, interval, open_time_ms desc)")
        cur.execute(f"create index if not exists decision_ledger_{suffix}_symbol_ts_idx on decision_ledger_{suffix} (symbol, timestamp_ms desc)")

    def write_cycle_report(self, report: Any) -> None:
        payload = to_plain(report)
        timestamp_ms = int(payload.get("timestamp_ms") or int(time.time() * 1000))
        self.ensure_month(timestamp_ms)
        suffix = month_suffix(timestamp_ms)
        with self.connect() as conn:
            with conn.cursor() as cur:
                self._insert_cycle(cur, suffix, payload)
                self._insert_order_events(cur, suffix, payload.get("order_lifecycle_events", []))
                self._upsert_orders_from_events(cur, suffix, payload.get("order_lifecycle_events", []))
                self._upsert_open_orders_from_list(cur, suffix, payload.get("open_orders", []))
                self._insert_decision_ledger(cur, suffix, payload)
                self._insert_candles(cur, suffix, payload)
            conn.commit()

    def write_portfolio_snapshot(self, snapshot: Any) -> None:
        payload = to_plain(snapshot)
        now_ms = int(time.time() * 1000)
        self.ensure_month(now_ms)
        suffix = month_suffix(now_ms)
        with self.connect() as conn:
            with conn.cursor() as cur:
                self._upsert_open_orders(cur, suffix, payload.get("open_orders", {}))
                self._upsert_fills(cur, suffix, payload.get("fills", []))
            conn.commit()

    def _json(self, payload: Dict[str, Any]) -> str:
        return json.dumps(payload, ensure_ascii=True)

    def _insert_cycle(self, cur: Any, suffix: str, payload: Dict[str, Any]) -> None:
        timestamp_ms = int(payload.get("timestamp_ms") or 0)
        symbols = list((payload.get("market_prices") or {}).keys()) or [""]
        for symbol in symbols:
            cur.execute(
                f"""
                insert into cycle_reports_{suffix} (timestamp_ms, cycle_mode, cycle_reason, symbol, payload)
                values (%s,%s,%s,%s,%s::jsonb)
                on conflict (timestamp_ms, symbol) do update set
                  cycle_mode=excluded.cycle_mode,
                  cycle_reason=excluded.cycle_reason,
                  payload=excluded.payload
                """,
                (timestamp_ms, payload.get("cycle_mode", ""), payload.get("cycle_reason", ""), symbol, self._json(payload)),
            )

    def _insert_order_events(self, cur: Any, suffix: str, events: Iterable[Dict[str, Any]]) -> None:
        for event in events or []:
            if not isinstance(event, dict):
                continue
            cur.execute(
                f"""
                insert into order_events_{suffix} (client_order_id, symbol, side, status, event_type, reason, timestamp_ms, payload)
                values (%s,%s,%s,%s,%s,%s,%s,%s::jsonb)
                on conflict (client_order_id, status, timestamp_ms) do update set payload=excluded.payload, reason=excluded.reason
                """,
                (
                    str(event.get("client_order_id") or ""), str(event.get("symbol") or ""), str(event.get("side") or ""),
                    str(event.get("status") or ""), str(event.get("event_type") or ""), str(event.get("reason") or ""),
                    int(event.get("timestamp_ms") or 0), self._json(event),
                ),
            )

    def _insert_decision_ledger(self, cur: Any, suffix: str, payload: Dict[str, Any]) -> None:
        cycle_mode = str(payload.get("cycle_mode") or "")
        for entry in payload.get("decision_ledger", []) or []:
            if not isinstance(entry, dict):
                continue
            timestamp_ms = int(entry.get("timestamp_ms") or payload.get("timestamp_ms") or 0)
            cur.execute(
                f"""
                insert into decision_ledger_{suffix} (timestamp_ms, cycle_mode, symbol, price, final_action, scenario, payload)
                values (%s,%s,%s,%s,%s,%s,%s::jsonb)
                on conflict (timestamp_ms, symbol) do update set payload=excluded.payload, final_action=excluded.final_action, scenario=excluded.scenario
                """,
                (
                    timestamp_ms, cycle_mode, str(entry.get("symbol") or ""), _num(entry.get("price")),
                    str(entry.get("final_action") or entry.get("execution_status") or ""), str(entry.get("scenario") or entry.get("policy_state") or ""),
                    self._json(entry),
                ),
            )

    def _insert_candles(self, cur: Any, suffix: str, payload: Dict[str, Any]) -> None:
        for snapshot in payload.get("market_snapshots", []) or []:
            if not isinstance(snapshot, dict):
                continue
            symbol = str(snapshot.get("symbol") or "")
            candles = list(snapshot.get("klines", []) or [])
            candles.extend(snapshot.get("main_interval_bars", []) or [])
            for candle in candles:
                if not isinstance(candle, dict):
                    continue
                open_time = int(candle.get("open_time") or candle.get("open_time_ms") or 0)
                if not symbol or open_time <= 0:
                    continue
                interval = str(candle.get("interval") or payload.get("kline_interval") or "1m")
                cur.execute(
                    f"""
                    insert into candles_{suffix} (symbol, interval, open_time_ms, close_time_ms, open, high, low, close, volume)
                    values (%s,%s,%s,%s,%s,%s,%s,%s,%s)
                    on conflict (symbol, interval, open_time_ms) do update set
                      close_time_ms=excluded.close_time_ms, open=excluded.open, high=excluded.high, low=excluded.low, close=excluded.close, volume=excluded.volume
                    """,
                    (symbol, interval, open_time, int(candle.get("close_time") or candle.get("close_time_ms") or 0), _num(candle.get("open")), _num(candle.get("high")), _num(candle.get("low")), _num(candle.get("close")), _num(candle.get("volume"))),
                )

    def _upsert_open_orders(self, cur: Any, suffix: str, orders: Dict[str, Dict[str, Any]]) -> None:
        for order in (orders or {}).values():
            if not isinstance(order, dict):
                continue
            self._upsert_order(cur, suffix, order)

    def _upsert_open_orders_from_list(self, cur: Any, suffix: str, orders: Iterable[Dict[str, Any]]) -> None:
        for order in orders or []:
            if isinstance(order, dict):
                self._upsert_order(cur, suffix, order)

    def _upsert_orders_from_events(self, cur: Any, suffix: str, events: Iterable[Dict[str, Any]]) -> None:
        for event in events or []:
            if isinstance(event, dict):
                self._upsert_order(cur, suffix, event)

    def _upsert_order(self, cur: Any, suffix: str, order: Dict[str, Any]) -> None:
        if not str(order.get("client_order_id") or ""):
            return
        timestamp_ms = int(order.get("updated_at_ms") or order.get("timestamp_ms") or order.get("created_at_ms") or 0)
        target_suffix = month_suffix(timestamp_ms)
        if target_suffix != suffix:
            self.ensure_month(timestamp_ms)
        cur.execute(
            f"""
            insert into orders_{target_suffix} (client_order_id, symbol, side, status, quantity, limit_price, trigger, pair_id, created_at_ms, updated_at_ms, payload)
            values (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s::jsonb)
            on conflict (client_order_id) do update set
              status=excluded.status, quantity=excluded.quantity, limit_price=excluded.limit_price,
              updated_at_ms=excluded.updated_at_ms, payload=excluded.payload
            """,
            (
                str(order.get("client_order_id") or ""),
                str(order.get("symbol") or ""),
                str(order.get("side") or ""),
                str(order.get("status") or "OPEN"),
                _num(order.get("quantity")),
                _num(order.get("limit_price") or order.get("price")),
                str(order.get("trigger") or ""),
                str(order.get("pair_id") or ""),
                int(order.get("created_at_ms") or timestamp_ms),
                timestamp_ms,
                self._json(order),
            ),
        )

    def _upsert_fills(self, cur: Any, suffix: str, fills: Iterable[Dict[str, Any]]) -> None:
        for fill in fills or []:
            if not isinstance(fill, dict):
                continue
            timestamp_ms = int(fill.get("timestamp_ms") or fill.get("timestamp") or 0)
            if timestamp_ms <= 0:
                continue
            fill_suffix = month_suffix(timestamp_ms)
            if fill_suffix != suffix:
                self.ensure_month(timestamp_ms)
            target_suffix = fill_suffix
            cur.execute(
                f"""
                insert into fills_{target_suffix} (client_order_id, symbol, side, quantity, fill_price, fee, realized_pnl_delta, trigger, pair_id, timestamp_ms, payload)
                values (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s::jsonb)
                on conflict (client_order_id, timestamp_ms, side) do update set payload=excluded.payload, fee=excluded.fee
                """,
                (
                    str(fill.get("client_order_id") or ""), str(fill.get("symbol") or ""), str(fill.get("side") or fill.get("action") or ""),
                    _num(fill.get("quantity") or fill.get("filled_quantity")), _num(fill.get("fill_price") or fill.get("price") or fill.get("limit_price")),
                    _num(fill.get("fee")), _num(fill.get("realized_pnl_delta") or fill.get("realized_pnl")), str(fill.get("trigger") or ""),
                    str(fill.get("pair_id") or ""), timestamp_ms, self._json(fill),
                ),
            )

    def query_order_records(self, limit: int = 1000, status: str = "all") -> Dict[str, Any]:
        started = time.monotonic()
        self.ensure_month(int(time.time() * 1000))
        with self.connect() as conn:
            with conn.cursor() as cur:
                suffixes = self._list_suffixes(cur)
                records: List[Dict[str, Any]] = []
                fills: List[Dict[str, Any]] = []
                events: List[Dict[str, Any]] = []
                for suffix in suffixes:
                    fills.extend(self._query_payloads(cur, f"fills_{suffix}", "timestamp_ms", limit))
                    events.extend(self._query_payloads(cur, f"order_events_{suffix}", "timestamp_ms", limit))
                    orders = self._query_payloads(cur, f"orders_{suffix}", "updated_at_ms", limit)
                    records.extend(orders)
        normalized_fills = sorted(fills, key=lambda item: int(item.get("timestamp_ms") or 0), reverse=True)[:limit]
        normalized_events = sorted(events, key=lambda item: int(item.get("timestamp_ms") or 0), reverse=True)[:limit]
        merged = [*records, *normalized_events, *normalized_fills]
        merged = sorted(merged, key=lambda item: int(item.get("timestamp_ms") or item.get("updated_at_ms") or item.get("created_at_ms") or 0), reverse=True)
        if status and status.lower() != "all":
            merged = [item for item in merged if _matches_status(item, status)]
        self.last_query_ms = int((time.monotonic() - started) * 1000)
        return {
            "recent_fills": normalized_fills,
            "order_lifecycle_events": normalized_events,
            "raw_order_records": merged[:limit],
            "trade_records_complete": True,
            "storage_source": "postgres",
            "order_records_meta": {
                "source": "postgres",
                "query_ms": self.last_query_ms,
                "raw_record_count": len(merged[:limit]),
                "recent_fill_count": len(normalized_fills),
                "order_lifecycle_event_count": len(normalized_events),
            },
        }

    def query_ops_summary(self, hours: int) -> Dict[str, Any]:
        started = time.monotonic()
        cutoff_ms = int(time.time() * 1000) - max(1, hours) * 60 * 60 * 1000
        self.ensure_month(int(time.time() * 1000))
        fills: List[Dict[str, Any]] = []
        events: List[Dict[str, Any]] = []
        with self.connect() as conn:
            with conn.cursor() as cur:
                for suffix in self._list_suffixes(cur):
                    fills.extend(self._query_payloads_since(cur, f"fills_{suffix}", "timestamp_ms", cutoff_ms, 5000))
                    events.extend(self._query_payloads_since(cur, f"order_events_{suffix}", "timestamp_ms", cutoff_ms, 5000))
        rejected_count = sum(1 for item in events if str(item.get("status", "")).upper() == "REJECTED")
        canceled_count = sum(1 for item in events if str(item.get("status", "")).upper() in {"CANCELED", "EXPIRED"})
        fee_total = sum(_num(item.get("fee")) for item in fills)
        self.last_query_ms = int((time.monotonic() - started) * 1000)
        return {
            "status": "ok",
            "storage_source": "postgres",
            "hours": hours,
            "fill_count": len(fills),
            "cancel_count": canceled_count,
            "reject_count": rejected_count,
            "fee_total": fee_total,
            "completed_pair_count": 0,
            "positive_pair_count": 0,
            "negative_pair_count": 0,
            "current_pair_locks": {},
            "current_open_pairs": [],
            "query_ms": self.last_query_ms,
        }

    def _query_payloads(self, cur: Any, table: str, order_col: str, limit: int) -> List[Dict[str, Any]]:
        cur.execute(f"select payload from {table} order by {order_col} desc limit %s", (limit,))
        rows = cur.fetchall()
        return [row[0] if isinstance(row[0], dict) else json.loads(row[0]) for row in rows]

    def _query_payloads_since(self, cur: Any, table: str, order_col: str, cutoff_ms: int, limit: int) -> List[Dict[str, Any]]:
        cur.execute(f"select payload from {table} where {order_col} >= %s order by {order_col} desc limit %s", (cutoff_ms, limit))
        rows = cur.fetchall()
        return [row[0] if isinstance(row[0], dict) else json.loads(row[0]) for row in rows]

    def _list_suffixes(self, cur: Any) -> List[str]:
        cur.execute("select tablename from pg_tables where schemaname='public' and tablename like 'fills_%' order by tablename desc limit 12")
        rows = cur.fetchall()
        suffixes = [str(row[0]).replace("fills_", "") for row in rows]
        if suffixes:
            return suffixes
        return [month_suffix(int(time.time() * 1000))]

    def storage_status(self) -> Dict[str, Any]:
        ok = self.ping()
        partitions: List[str] = []
        if ok:
            try:
                with self.connect() as conn:
                    with conn.cursor() as cur:
                        cur.execute("select tablename from pg_tables where schemaname='public' and (tablename like 'orders_%' or tablename like 'fills_%') order by tablename")
                        partitions = [row[0] for row in cur.fetchall()]
            except Exception as exc:
                self.last_error = str(exc)[:240]
        return {
            "enabled": self.settings.db_enabled,
            "driver": self.settings.db_driver,
            "read_mode": self.settings.db_read_mode,
            "write_mode": self.settings.db_write_mode,
            "ok": ok,
            "error": self.last_error,
            "last_query_ms": self.last_query_ms,
            "partitions": partitions,
        }


def _num(value: Any) -> float:
    try:
        if value in (None, ""):
            return 0.0
        return float(value)
    except (TypeError, ValueError):
        return 0.0


def _matches_status(item: Dict[str, Any], status: str) -> bool:
    normalized = status.lower()
    item_status = str(item.get("status") or "").upper()
    side = str(item.get("side") or item.get("action") or "").upper()
    if normalized == "filled":
        return item_status in {"FILLED", "PAPER_FILLED"}
    if normalized == "open":
        return item_status in {"OPEN", "NEW", "PARTIALLY_FILLED", "UNKNOWN"}
    if normalized in {"closed", "canceled"}:
        return item_status in {"CANCELED", "EXPIRED", "REJECTED"}
    if normalized == "buy":
        return side == "BUY"
    if normalized == "sell":
        return side == "SELL"
    return True


def build_postgres_store(settings: Settings | None = None) -> Optional[PostgresRuntimeStore]:
    settings = settings or load_settings()
    if not settings.db_enabled or settings.db_driver != "postgres":
        return None
    try:
        return PostgresRuntimeStore(settings)
    except Exception:
        return None


def build_runtime_store(settings: Settings | None = None) -> SafeRuntimeStore:
    settings = settings or load_settings()
    if not settings.db_enabled or settings.db_driver != "postgres" or settings.db_write_mode == "file":
        return SafeRuntimeStore(None, settings)
    return SafeRuntimeStore(build_postgres_store(settings), settings)
