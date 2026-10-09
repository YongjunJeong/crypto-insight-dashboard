# Operations and implementation guide

[Project overview](../README.md) | [Preserved Korean guide](../README.ko.md)

## Workspace setup and execution order

Use Databricks compute with compatible Spark/Delta support, access to Unity Catalog
and outbound HTTPS to `api.binance.com` and `api.alternative.me`. Runtime and cloud
sizing have not been validated here. Keep catalog/schema constants consistent.
The following creates only demo resources in the selected workspace:

```sql
CREATE CATALOG IF NOT EXISTS demo_catalog;
CREATE SCHEMA IF NOT EXISTS demo_catalog.demo_schema;
```

Import the Git repository, attach compute, and run notebooks in this order:

| Stage | File under `pipeline/` | Output in `demo_catalog.demo_schema` |
|---|---|---|
| Bronze prices | `bronze/binance_klines.ipynb` | `bronze_charts`, `bronze_ingest_state` |
| Bronze sentiment | `bronze/fear_greed_index.py` | `bronze_fear_greed` |
| Silver prices | `silver/transform_charts.ipynb` | `silver_charts` |
| Silver sentiment | `silver/transform_fear_greed.ipynb` | `silver_fear_greed` |
| Gold prices | `gold/price_signals.ipynb` | `gold_prices_4h` |
| Gold sentiment | `gold/fear_greed_metrics.ipynb` | `gold_fear_greed` |
| Combined dashboard | `gold/joined_dashboard.ipynb` | `gold_price_positions_4h` |

Independent price/sentiment branches may run separately, but both Gold inputs must
exist before the join. The join explicitly fails when `gold_fear_greed` is absent.

## Parameters and ingestion

| Component | Defaults and interpretation |
|---|---|
| Binance | `MODE="once"`, BTCUSDT/ETHUSDT/SOLUSDT, `INTERVALS=["4h"]`, `LIMIT_ONCE=1000`, `BACKFILL_DAYS=200` |
| Fear & Greed | `MODE="backfill"`, `LIMIT_ONCE=2`, `BACKFILL_LIMIT=200`, `API_REFRESH_SECONDS=86400`; 200 is the configured history count, not a verified API maximum |
| Silver charts | `DAYS_BACK=14`; on the first historical build use `None` or the intended history range, rather than silently discarding most of the backfill |
| Gold prices | `DAYS_BACK=200`; MA50/MA200 are 50/200 rows of four-hour candles, not 50/200 days |
| Joined output | `DAYS_BACK=120`, `FNG_FRESH_DAYS=3`; older matching sentiment becomes null |

For initial Binance history, set `MODE="backfill"` and the chosen range. Bronze
retains raw JSON and keys; the Binance state table tracks the last open timestamp
per symbol/interval. Retry/backoff code exists, but provider availability and rate
limits require runtime verification. API timestamps and Spark processing use UTC.

Silver parses JSON into typed fields, removes duplicate keys and MERGEs records.
Rerun safety depends on the source keys and records actually supplied; it is not
proof that an interrupted multi-notebook workflow recovers automatically.

## Metrics and temporal alignment

Price windows are ordered per symbol. MA50/MA200 use `rowsBetween`; startup averages
have fewer observations and are not gated by a minimum history count. Cross events
compare previous Bearish/Bullish state to the current state; transitions involving
Neutral do not produce those events. Percentage change uses `lag(close, 6)`.

Sentiment computes row-based MA7/MA30, sample-standard-deviation Z30, one/seven-row
changes and a same-class streak. Missing dates invalidate a literal “days” reading
of these row counts. Z30 is null when deviation is absent or zero.

The join broadcasts sentiment, selects records with `fng_ts <= bucket_end`, then
uses `row_number()` partitioned by **symbol and bucket_start** to retain the latest
match. Omitting symbol would drop other symbols in the same bucket. Freshness uses
`datediff`, a calendar-date comparison. This is a dashboard alignment, not a
causal prediction dataset. The output keeps the legacy `positions` name.

## Inspect each layer

```sql
SELECT COUNT(*), MIN(event_time), MAX(event_time)
FROM demo_catalog.demo_schema.bronze_charts;
SELECT event_time, index_value, value_classification
FROM demo_catalog.demo_schema.bronze_fear_greed
ORDER BY event_time DESC LIMIT 10;
SELECT symbol, interval, open_time, close
FROM demo_catalog.demo_schema.silver_charts
ORDER BY open_time DESC LIMIT 10;
SELECT symbol, bucket_start, close_4h, ma50_4h, ma200_4h,
       trend_state, cross_signal, pct_change_24h
FROM demo_catalog.demo_schema.gold_prices_4h
ORDER BY bucket_start DESC LIMIT 10;
SELECT symbol, bucket_start, close_4h, fng_value, fng_label
FROM demo_catalog.demo_schema.gold_price_positions_4h
ORDER BY bucket_start DESC LIMIT 10;
```

Compare count and timestamp ranges on both branches. Inspect duplicate keys and
missing periods explicitly; nonzero counts alone do not validate the pipeline.

## Dashboard SQL examples

These are documentation examples, not automatically deployed views. The 24-hour
example fixes the original guide's missing time filter. It compares the first and
last available observations **inside** that window; it is not an exact boundary
price lookup and excludes symbols with no observations in that window.

```sql
CREATE OR REPLACE VIEW demo_catalog.demo_schema.v_latest_price AS
WITH last AS (
  SELECT symbol, MAX(open_time) AS last_ts
  FROM demo_catalog.demo_schema.silver_charts GROUP BY symbol
)
SELECT s.symbol, s.close AS last_price, s.open_time AS last_ts
FROM demo_catalog.demo_schema.silver_charts s
JOIN last l ON s.symbol = l.symbol AND s.open_time = l.last_ts;

CREATE OR REPLACE VIEW demo_catalog.demo_schema.v_summary_24h AS
WITH base AS (
  SELECT symbol, close, open_time
  FROM demo_catalog.demo_schema.silver_charts
  WHERE open_time >= current_timestamp() - INTERVAL 24 HOURS
), first_last AS (
  SELECT symbol,
    FIRST_VALUE(close) OVER (PARTITION BY symbol ORDER BY open_time) AS first_close,
    LAST_VALUE(close) OVER (PARTITION BY symbol ORDER BY open_time
      ROWS BETWEEN UNBOUNDED PRECEDING AND UNBOUNDED FOLLOWING) AS last_close,
    AVG(close) OVER (PARTITION BY symbol) AS avg_24h
  FROM base
)
SELECT DISTINCT symbol, last_close AS last_price, avg_24h AS avg_price_24h,
  last_close-first_close AS abs_change_24h,
  (last_close-first_close)/NULLIF(first_close, 0)*100 AS pct_change_24h
FROM first_last;

CREATE OR REPLACE VIEW demo_catalog.demo_schema.v_signals AS
SELECT symbol, bucket_start, close_4h, ma50_4h, ma200_4h,
       cross_signal, pct_change_24h
FROM demo_catalog.demo_schema.gold_prices_4h;
```

The configured source contains only four-hour bars. Adding other intervals would
require interval-aware dashboard queries to avoid mixing grains.

## Maintenance and history

`pipeline/maintenance/delta_optimize_vacuum.ipynb` includes OPTIMIZE, selected ZORDER
columns, VACUUM, DESCRIBE HISTORY and DESCRIBE DETAIL. Bronze/Silver retention is
168 hours; Gold retention is 336 hours. It catches per-table errors and continues,
so a completion printout does not mean every operation succeeded.

**Its current `DRY_RUN_ONLY=False` default performs actual VACUUM after reporting
candidates.** Set it to `True` before an inspection run. Earlier OPTIMIZE cells
still write files. Dry-run counts are not an approval gate or deletion threshold.
VACUUM can remove files needed by older snapshots; log retention alone cannot
preserve time travel. Never disable retention checks to copy a demo command.

```sql
DESCRIBE HISTORY demo_catalog.demo_schema.gold_prices_4h;
DESCRIBE DETAIL demo_catalog.demo_schema.gold_prices_4h;
-- Choose a version that exists and whose files are retained.
SELECT * FROM demo_catalog.demo_schema.gold_prices_4h VERSION AS OF 3
WHERE symbol='BTCUSDT' ORDER BY bucket_start DESC LIMIT 20;
```

Timestamp snapshots, RESTORE and DROP examples remain in the Korean reference.
RESTORE changes table state; DROP removes objects. Review the exact target,
retained versions and downstream consumers before performing either. No maintenance,
rollback or cleanup was executed for this portfolio refinement.

Spark settings enable optimizeWrite, autoCompact and adaptive execution/coalescing/
skew handling. They are tuning choices, not measured improvements. Compare query
plans, execution time and file counts on a fixed workload before sizing compute or
claiming a Photon speedup. The old cluster-size and speed figures were not reproduced.

## Diagnosis and next validation

| Symptom | Check |
|---|---|
| HTTP 429 or connection failure | Source logs, retry exhaustion, egress rules and requested range; reduce requests before rerunning |
| Catalog/schema permission error | Selected catalog, schema and permissions; do not assume admin rights |
| Short MA history | Bronze and Silver ranges, the Silver 14-day default and missing candles |
| Join failure or stale null sentiment | Run both Silver/Gold branches; compare timestamp ranges and freshness |
| Repeated records | Group by actual MERGE keys; review incoming duplicate handling |
| Excess small files | Inspect DETAIL before/after OPTIMIZE; review retention separately from compaction |

There is no automated suite or fresh workspace result. Start with synthetic tests
for keys, partial windows, missing bars, symbol-preserving joins and stale sentiment,
then record one controlled Databricks run. Existing Korean design/lesson documents
are preserved rather than replaced with unsupported operating claims.
