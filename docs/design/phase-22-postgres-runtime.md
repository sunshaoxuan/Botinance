# P22 PostgreSQL Runtime Storage

P22 introduces PostgreSQL as the primary query path for long-running Botinance runtime data while preserving the existing JSON files as a compatibility and recovery layer.

## Goals

- Keep `latest_report.json`, `paper_state.json`, and `cycle_reports.jsonl` available.
- Write cycle reports, order events, fills, decision ledger entries, and candles into PostgreSQL.
- Use PostgreSQL first for order table and ops queries.
- Keep file fallback available, default `DB_FALLBACK_TO_FILE=false` for production hard boundary.
- Set `DB_FALLBACK_TO_FILE=true` only in local debug when PostgreSQL is not yet ready.
- Store data in monthly tables so multi-month runtime history remains queryable.

## Runtime Layout

Docker Compose starts `postgres:16` and mounts data under:

```text
runtime_postgres/data
```

The database password must come from `BOTINANCE_DB_PASSWORD`. It is not written into `.env`.

## Write Path

`ReportRecorder.record_cycle()` continues writing JSONL and also writes:

- `cycle_reports_YYYY_MM`
- `order_events_YYYY_MM`
- `decision_ledger_YYYY_MM`
- `candles_YYYY_MM`

`PaperPortfolio.save_snapshot()` continues writing `paper_state.json` and also writes:

- `orders_YYYY_MM`
- `fills_YYYY_MM`

PostgreSQL write failures are captured by `SafeRuntimeStore` and do not stop the monitor.

Set hard boundary mode to avoid silent fallback:

```env
DB_FALLBACK_TO_FILE=false
```

## Query Path

The dashboard order API uses PostgreSQL first when:

```env
DB_READ_MODE=prefer_db
DASHBOARD_ORDER_SOURCE=postgres
```

If fallback is enabled and the query fails, the API returns file results with:

```json
{
  "storage_source": "file_fallback",
  "storage_warning": "..."
}
```

## Operations

Storage health is exposed at:

```text
/api/ops/storage
```

Historical JSON runtime can be imported with:

```bash
PYTHONPATH=src python3 -m binance_ai.storage.migrate_runtime \
  --runtime-dir runtime_visual \
  --database-url postgresql://botinance:${BOTINANCE_DB_PASSWORD}@127.0.0.1:5432/botinance \
  --from-jsonl cycle_reports.jsonl
```

The migration command is repeatable because inserts use conflict keys for cycle reports, order events, fills, and candles.
