# Crypto Data Ingest → Transform → Aggregate → Signals (Using Databricks)

This project provides a 4-step pipeline for ingesting Binance Kline (candlestick) data into Databricks, 
transforming it into structured tables, aggregating into time windows, and generating trading signals.

- `01_ingest_bronze_binance.py` — Ingest Binance REST data into a Bronze Delta table
- `01b_ingest_bronze_fear_greed.py` — Ingest Fear & Greed Index data into a Bronze Delta table
- `02_transform_silver_binance.py` — Transform Bronze → Silver structured schema
- `03_aggregate_gold_binance.py` — Aggregate Silver → Gold
- `04_generate_signals_binance.py` — Generate trading signals from Gold

---

## 0) Prerequisites

- Databricks workspace
- A cluster
- Unity Catalog enabled with permission to create catalog/schema
- Outbound internet access to `api.binance.com` and `api.alternative.me`

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
4. After the repo folder appears in Workspace, verify the files exist:
- `01_ingest_bronze_binance.py`
- `01b_ingest_bronze_fear_greed.py`
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
- `INTERVALS` = `["15m"]`
- `BACKFILL_HOURS` = `168`

Recommended first run
```python
MODE = "backfill"
SYMBOLS = ["BTCUSDT","ETHUSDT"]
INTERVALS = ["15m"]
BACKFILL_HOURS = 168
```

Then **Run all**.

Validate:
```sql
SELECT COUNT(*) FROM demo_catalog.demo_schema.bronze_charts;
SELECT * FROM demo_catalog.demo_schema.bronze_charts ORDER BY event_time DESC LIMIT 10;
```

## 3b) Run `01b_ingest_bronze_fear_greed.py`
This notebook pulls the latest Fear & Greed Index snapshot from `api.alternative.me` and stores it in a dedicated Bronze table.
- Bronze: `demo_catalog.demo_schema.bronze_fear_greed`

Key parameters:
- `MODE` = `"backfill" | "once" | "poll" | "forever"`
- `LIMIT_ONCE` = `2`
- `BACKFILL_LIMIT` = `200`
- `API_REFRESH_SECONDS` = `86_400` (공식 API는 하루에 한 번 업데이트)

Recommended first run
```python
MODE = "backfill"
LIMIT_ONCE = 2
BACKFILL_LIMIT = 200
```

> `poll` / `forever` 모드에서는 최소 24시간 간격(`API_REFRESH_SECONDS`)으로 호출되도록 자동 보정됩니다.

Then **Run all**.

Validate:
```sql
SELECT COUNT(*) FROM demo_catalog.demo_schema.bronze_fear_greed;
SELECT event_time, index_value, value_classification
FROM demo_catalog.demo_schema.bronze_fear_greed
ORDER BY event_time DESC LIMIT 10;
```

> Fear & Greed 데이터는 Binance 캔들 테이블과 별도 관리되며, 필요 시 상위 레이어에서 조인해 분석할 수 있습니다.

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

## 8) Cluster Configuration (권장 설정)

| 설정 | 권장값 | 이유 |
|:---|:---|:---|
| **Runtime** | Databricks Runtime 14.x LTS (또는 15.x) | Delta 3.x + Spark 3.5.x 호환성 보장 |
| **Node type** | Memory-optimized (예: AWS `r5d.xlarge`) | Window 함수 및 MERGE 연산이 셔플 데이터를 메모리에 보유 |
| **Autoscaling** | Min: 2, Max: 8 workers | 백필(Backfill) 시 일시적 스파이크; 정상 운영은 낮은 수준 유지 |
| **Auto-termination** | 20분 | 폴링 간격 동안 유휴 클러스터 비용 방지 |
| **Photon** | 활성화 (가능한 경우) | 벡터화된 스캔 기반 Gold 쿼리에서 2~4배 속도 향상 |

각 Transform/Gold 노트북 상단에 설정된 Spark 성능 구성:
```python
spark.conf.set("spark.databricks.delta.optimizeWrite", "true")  # 소파일 자동 병합
spark.conf.set("spark.databricks.delta.autoCompact",   "true")  # 백그라운드 컴팩션
spark.conf.set("spark.sql.adaptive.enabled",           "true")  # AQE 활성화
spark.conf.set("spark.sql.adaptive.coalescePartitions.enabled", "true")  # 셔플 후 소파티션 병합
spark.conf.set("spark.sql.adaptive.skewJoin.enabled",  "true")  # 데이터 Skew 자동 처리
```

---

## 8a) Time Travel & Delta Lake 운영

Delta Lake는 모든 쓰기를 버전화된 트랜잭션으로 기록한다. `DESCRIBE HISTORY`로 이력을 조회하고 `VERSION AS OF` / `TIMESTAMP AS OF`로 특정 시점을 재현하거나 `RESTORE`로 롤백할 수 있다.

```sql
-- Gold 테이블의 전체 쓰기 이력 조회 (version, timestamp, operation, metrics 포함)
DESCRIBE HISTORY demo_catalog.demo_schema.gold_prices_4h;

-- 특정 버전의 Gold 테이블 쿼리 (파이프라인 실행 전후 비교)
SELECT * FROM demo_catalog.demo_schema.gold_prices_4h VERSION AS OF 3
WHERE symbol = 'BTCUSDT'
ORDER BY bucket_start DESC LIMIT 20;

-- 특정 타임스탬프 기준 Gold 스냅샷 재현 (대시보드 상태 감사)
SELECT * FROM demo_catalog.demo_schema.gold_prices_4h
TIMESTAMP AS OF '2025-12-01 00:00:00'
WHERE symbol = 'ETHUSDT';

-- 잘못된 파이프라인 실행 후 이전 버전으로 원자적 롤백
RESTORE TABLE demo_catalog.demo_schema.gold_prices_4h TO VERSION AS OF 5;
```

정기 유지보수는 `pipeline/04_maintenance_optimize_vacuum.ipynb`에서 주 1회 실행:
- **OPTIMIZE + ZORDER**: 소파일 병합 및 데이터 클러스터링 (쿼리 성능 향상)
- **VACUUM**: 보존 기간(Gold: 14일, Bronze/Silver: 7일) 초과 파일 삭제
- **DESCRIBE HISTORY**: Gold 테이블 트랜잭션 이력 감사

---

## 8b) Troubleshooting
- **429 Too Many Requests**: Reduce symbols/intervals, shorten backfill hours. Script retries with backoff.
- **Catalog/Schema permission error**: Request admin permissions or use existing catalog/schema.
- **Binance API connection fail**: Check VPC/firewall outbound rules.
- **Duplicate data**: All stages use MERGE and dropDuplicates → safe idempotency.
- **Fear & Greed cadence**: API는 하루에 한 번(약 00:00 UTC) 업데이트되므로 24시간 이하 간격으로 반복 호출해도 동일한 값이 반환됩니다. 스크립트는 최소 호출 간격을 자동으로 24시간으로 맞춥니다.
- **DLQ 모니터링**: `SELECT * FROM demo_catalog.demo_schema.bronze_dlq ORDER BY failed_at DESC LIMIT 20`으로 4xx 실패 내역 확인. `ingest_run_id`로 특정 실패 배치를 추적하고 재처리 가능.
- **Bronze 유틸 재사용**: `PoC/bronze_ingest_utils.py`의 `BronzeTableWriter`를 이용해 새로운 소스도 동일한 방식으로 Delta MERGE 할 수 있습니다. Fear & Greed 수집 노트북은 Binance 노트북과 같은 헬퍼를 공유합니다.
- **Performance**: Gold/Silver 테이블에 `04_maintenance_optimize_vacuum.ipynb`을 주 1회 실행하여 ZORDER (`symbol`, `bucket_start`) 적용 및 소파일 최적화.

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
