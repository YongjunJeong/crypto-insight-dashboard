# Crypto Data Ingest → Transform → Aggregate → Signals (Using Databricks)

This project provides a 4-step pipeline for ingesting Binance Kline (candlestick) data into Databricks, 
transforming it into structured tables, aggregating into time windows, and generating trading signals.

- `01_ingest_bronze_binance.py` — Ingest Binance REST data into a Bronze Delta table
- `02_transform_silver_binance.py` — Transform Bronze → Silver structured schema
- `03_aggregate_gold_binance.py` — Aggregate Silver → Gold
- `04_generate_signals_binance.py` — Generate trading signals from Gold

---

## 0) Prerequisites

- Databricks workspace
- A cluster
- Unity Catalog enabled with permission to create catalog/schema
- Outbound internet access to `api.binance.com`

---

## 1) Create Catalog & Schema

Open **SQL Editor** and run:

```sql
CREATE CATALOG IF NOT EXISTS demo_catalog;
CREATE SCHEMA  IF NOT EXISTS demo_catalog.demo_schema;
```
> If you use a different name, update CATALOG / SCHEMA constants in all scripts accordingly.

---

## 2) Import 4 Scripts to Databricks Workspace

1. In the left sidebar, go to Repos → Add Repo.
2. Paste your Git URL (e.g., https://github.com/your-username/crypto-insight-pipeline.git).
3. Choose the branch (e.g., main or dev) and Create.
4. After the repo folder appears in Workspace, verify the four files exist:
- `01_ingest_bronze_binance.py`
- `02_transform_silver_binance.py`
- `03_aggregate_gold_binance.py`
- `04_generate_signals_binance.py`
5. Open each file and attach a cluster (top-right cluster picker).
6. Ensure each notebook’s default language is Python (not SQL/Scala/R).

---

## 3) Run `01_ingest_bronze_binance.py`
This script fetches data from Binance `/api/v3/klines` and writes into a Bronze Delta table.
- Bronze: `demo_catalog.demo_schema.bronze_charts`
- State : `demo_catalog.demo_schema.bronze_ingest_state` (last open_time per symbol/interval)

Key parameters:
- `MODE` = `"backfill" | "once" | "poll" | "forever"`
- `SYMBOLS` = `["BTCUSDT","ETHUSDT"]`
- `INTERVALS` = `["1m"]`
- `BACKFILL_HOURS` = `720`

Recommended first run
```python
MODE = "backfill"
SYMBOLS = ["BTCUSDT","ETHUSDT"]
INTERVALS = ["1m"]
BACKFILL_HOURS = 720
```

Then **Run all**.

Validate:
```sql
SELECT COUNT(*) FROM demo_catalog.demo_schema.bronze_charts;
SELECT * FROM demo_catalog.demo_schema.bronze_charts ORDER BY event_time DESC LIMIT 10;
```

---

## 4) Run `02_transform_silver_binance.py`
Parses `raw_json` from Bronze into structured columns, MERGE upsert into Silver.
- Input: `demo_catalog.demo_schema.bronze_charts`
- Output: `demo_catalog.demo_schema.silver_charts`

**Run all**.

Validate:
```sql
SELECT COUNT(*) FROM demo_catalog.demo_schema.silver_charts;
SELECT symbol, interval, open_time, close
FROM demo_catalog.demo_schema.silver_charts
ORDER BY open_time DESC LIMIT 10;
```

---

## 5) Run `03_aggregate_gold_binance.py`
Aggregates Silver candles into fixed windows (default 5 minutes) and upserts into Gold.
- Input: `demo_catalog.demo_schema.silver_charts`
- Output: `demo_catalog.demo_schema.gold_signals`
- Window: `WINDOW_SPEC = "5 minutes"`

**Run all**.

Validate:
```sql
SELECT COUNT(*) FROM demo_catalog.demo_schema.gold_signals;
SELECT symbol, bucket_start, avg_price
FROM demo_catalog.demo_schema.gold_signals
ORDER BY bucket_start DESC LIMIT 10;
```

---

## 6) Run `04_generate_signals_binance.py`
Calculates MA(50/200), Golden/Dead Cross, and above/below MA200 flags, then MERGE-upserts to Signals.
- Input: `demo_catalog.demo_schema.gold_signals`
- Output: `demo_catalog.demo_schema.signals_charts`

**Run all**.

Validate:

```sql
SELECT COUNT(*) FROM demo_catalog.demo_schema.signals_charts;

WITH ranked AS (
  SELECT symbol, cross_signal, above_ma200, bucket_start,
         ROW_NUMBER() OVER (PARTITION BY symbol ORDER BY bucket_start DESC) rn
  FROM demo_catalog.demo_schema.signals_charts
)
SELECT symbol, cross_signal, above_ma200, bucket_start
FROM ranked WHERE rn = 1
ORDER BY symbol;
```

---

## 7) Create Views for Visualization
Copy each block into SQL Editor and run.

Latest Price
```sql
CREATE OR REPLACE VIEW demo_catalog.demo_schema.v_latest_price AS
WITH last AS (
  SELECT symbol, MAX(open_time) AS last_ts
  FROM demo_catalog.demo_schema.silver_charts
  GROUP BY symbol
)
SELECT s.symbol, s.close AS last_price, s.open_time AS last_ts
FROM demo_catalog.demo_schema.silver_charts s
JOIN last l
  ON s.symbol = l.symbol AND s.open_time = l.last_ts;
```

24h summary
```sql
CREATE OR REPLACE VIEW demo_catalog.demo_schema.v_summary_24h AS
WITH base AS (
  SELECT symbol, close, open_time
  FROM demo_catalog.demo_schema.silver_charts
),
first_last AS (
  SELECT
    symbol,
    FIRST_VALUE(close) OVER (PARTITION BY symbol ORDER BY open_time ASC) AS first_close,
    LAST_VALUE(close)  OVER (PARTITION BY symbol ORDER BY open_time ASC
                             ROWS BETWEEN UNBOUNDED PRECEDING AND UNBOUNDED FOLLOWING) AS last_close,
    AVG(close)         OVER (PARTITION BY symbol) AS avg_24h
  FROM base
)
SELECT DISTINCT
  symbol,
  last_close AS last_price,
  avg_24h    AS avg_price_24h,
  (last_close - first_close)         AS abs_change_24h,
  (last_close - first_close) / NULLIF(first_close, 0) * 100 AS pct_change_24h
FROM first_last;
```

Signals view
```sql
CREATE OR REPLACE VIEW demo_catalog.demo_schema.v_signals AS
SELECT
  symbol,
  bucket_start,
  bucket_end,
  avg_price,
  ma_50,
  ma_200,
  cross_signal,
  above_ma200
FROM demo_catalog.demo_schema.signals_charts;
```

---

## 8) Troubleshooting
- **429 Too Many Requests**: Reduce symbols/intervals, shorten backfill hours. Script retries with backoff.
- **Catalog/Schema permission error**: Request admin permissions or use existing catalog/schema.
- **Binance API connection fail**: Check VPC/firewall outbound rules.
- **Duplicate data**: All stages use MERGE and dropDuplicates → safe idempotency.
- **Performance**: Consider ZORDER (`symbol`, `bucket_start`) on Gold/Signals.

---

## 9) Cleanup
```sql
DROP VIEW  IF EXISTS demo_catalog.demo_schema.v_latest_price;
DROP VIEW  IF EXISTS demo_catalog.demo_schema.v_summary_24h;
DROP VIEW  IF EXISTS demo_catalog.demo_schema.v_signals;

DROP TABLE IF EXISTS demo_catalog.demo_schema.signals_charts;
DROP TABLE IF EXISTS demo_catalog.demo_schema.gold_signals;
DROP TABLE IF EXISTS demo_catalog.demo_schema.silver_charts;
DROP TABLE IF EXISTS demo_catalog.demo_schema.bronze_ingest_state;
DROP TABLE IF EXISTS demo_catalog.demo_schema.bronze_charts;
```
