# Crypto Insight Dashboard — Databricks Medallion Pipeline

Databricks + Delta Lake + Spark 기반의 **메달리온 아키텍처(Bronze → Silver → Gold)** 파이프라인.
Binance Kline(캔들)과 Fear & Greed Index를 수집해 거래 신호와 시장 심리 지표를 하나의 대시보드로 통합한다.

## 왜 만들었는가 (Use Case)

암호화폐 트레이더가 의사결정을 내릴 때 필요한 정보 — **가격 추세(MA50/200, 골든·데드 크로스)**, **시장 심리(Fear & Greed Index)** — 는 보통 서로 다른 사이트에 흩어져 있어 따로 확인해야 한다. 이 프로젝트는 두 신호를 하나의 시간축(4시간 봉)으로 정렬해 한 화면에서 보여주는 것을 목표로 한다.

> 초기 버전은 선물 리더보드(다른 트레이더의 포지션) 데이터도 통합했으나, 그 데이터 소스가 Binance의 비공식·비공개 내부 API를 리셀하는 유료 게이트웨이(Apyflux)에 의존하고 있었다. Binance가 2024년부터 해당 엔드포인트를 인증 필요로 전환해 더 이상 공식적으로 접근할 수 없어, 대체 스크래퍼로 땜질하는 대신 이 기능 자체를 스코프에서 제외했다. 자세한 배경은 `documentations/technical_thought.md` 참고.

**아키텍처 선택의 트레이드오프:**

| 결정 | 선택 | 이유 |
| :--- | :--- | :--- |
| 수집 방식 | REST API 배치 폴링 (Auto Loader/Structured Streaming 아님) | 소스가 파일 드롭이 아닌 REST 엔드포인트이고, Gold의 최소 단위가 4시간 봉이라 분 단위 실시간성이 필요하지 않음. 상시 스트리밍 클러스터 비용을 피할 수 있음. 자세한 근거는 `documentations/design.md` §1.3 참고. |
| 신뢰성 확보 방식 | 상태 테이블 체크포인트 + `unique_key` 기준 `MERGE` | Streaming Checkpoint 없이도 "재실행해도 안전한" 멱등성을 배치로 달성. |
| 시각화 계층 | Databricks Lakeview (Streamlit 아님) | 별도 앱 서버/인증 없이 Databricks 워크스페이스 안에서 바로 공유·권한관리 가능. |

이 프로젝트에서 실제로 겪은 설계 실수와 그 교훈(추세를 신호로 착각한 버그 등)은 `documentations/technical_thought.md`에 정리했다.

## 파이프라인 구성

```
pipeline/
  bronze/                         # 외부 API → Delta Lake 원본 적재 (Append-Only)
    binance_klines.ipynb          # Binance /api/v3/klines → bronze_charts
    fear_greed_index.py           # api.alternative.me → bronze_fear_greed
  silver/                         # Bronze 정제 · 타입 표준화 · 중복 제거
    transform_charts.ipynb        # bronze_charts → silver_charts
    transform_fear_greed.ipynb    # bronze_fear_greed → silver_fear_greed
  gold/                           # Silver 집계 · 지표 계산 · 대시보드 뷰
    price_signals.ipynb           # silver_charts → gold_prices_4h (MA50/200, Cross)
    fear_greed_metrics.ipynb      # silver_fear_greed → gold_fear_greed (MA7/30, Z-Score)
    joined_dashboard.ipynb        # 2-way join → gold_price_positions_4h
  maintenance/                    # 운영 유지보수
    delta_optimize_vacuum.ipynb   # OPTIMIZE + ZORDER + VACUUM + DESCRIBE HISTORY
```

---

## 0) 사전 요구사항

- Databricks 워크스페이스
- 클러스터 (Databricks Runtime 14.x LTS 이상 권장)
- Unity Catalog 활성화 및 카탈로그/스키마 생성 권한
- `api.binance.com`, `api.alternative.me`로의 아웃바운드 인터넷 접근

---

## 1) 카탈로그 및 스키마 생성

**SQL 편집기**에서 실행:

```sql
CREATE CATALOG IF NOT EXISTS demo_catalog;
CREATE SCHEMA  IF NOT EXISTS demo_catalog.demo_schema;
```

> 다른 이름을 사용할 경우, 모든 스크립트의 `CATALOG` / `SCHEMA` 상수를 동일하게 변경할 것.

---

## 2) Databricks Workspace에 리포지토리 연결

1. 왼쪽 사이드바 → **Repos** → **Add Repo** 클릭.
2. Git URL 입력 (예: `https://github.com/your-username/crypto-insight-dashboard.git`).
3. 브랜치 선택 후 **Create**.
4. Workspace에 폴더가 나타나면 파이프라인 파일 존재 여부 확인:
   - `pipeline/bronze/binance_klines.ipynb`
   - `pipeline/bronze/fear_greed_index.py`
   - `pipeline/silver/transform_charts.ipynb`
   - `pipeline/silver/transform_fear_greed.ipynb`
   - `pipeline/gold/price_signals.ipynb`
   - `pipeline/gold/fear_greed_metrics.ipynb`
   - `pipeline/gold/joined_dashboard.ipynb`
   - `pipeline/maintenance/delta_optimize_vacuum.ipynb`
5. 각 파일을 열고 우측 상단 클러스터 선택기에서 클러스터를 연결.
6. 기본 언어가 **Python**으로 설정되어 있는지 확인.

---

## 3) Bronze 수집: `pipeline/bronze/binance_klines.ipynb`

Binance `/api/v3/klines` 에서 캔들 데이터를 가져와 Bronze Delta 테이블에 적재.

- **Bronze 테이블**: `demo_catalog.demo_schema.bronze_charts`
- **상태 추적**: `demo_catalog.demo_schema.bronze_ingest_state` (심볼/인터벌별 마지막 `open_time`)

주요 파라미터:

| 파라미터 | 기본값 | 설명 |
|:---|:---|:---|
| `MODE` | `"once"` | `backfill` \| `once` \| `poll` \| `forever` |
| `SYMBOLS` | `["BTCUSDT","ETHUSDT","SOLUSDT"]` | 수집 대상 심볼 목록 |
| `INTERVALS` | `["4h"]` | 캔들 인터벌 (4시간 봉만 수집) |
| `LIMIT_ONCE` | `1000` | 1회 수집 최대 캔들 수 |
| `BACKFILL_DAYS` | `200` | 백필 범위 (일) — MA200 안정화에 필요한 과거 데이터 기간 |

최초 실행 권장 설정:

```python
MODE = "backfill"
SYMBOLS = ["BTCUSDT", "ETHUSDT", "SOLUSDT"]
INTERVALS = ["4h"]
BACKFILL_DAYS = 200
```

**Run all** 실행 후 검증:

```sql
SELECT COUNT(*) FROM demo_catalog.demo_schema.bronze_charts;
SELECT * FROM demo_catalog.demo_schema.bronze_charts ORDER BY event_time DESC LIMIT 10;
```

---

## 3b) Bronze 수집: `pipeline/bronze/fear_greed_index.py`

`api.alternative.me` 에서 Fear & Greed Index 스냅샷을 수집해 전용 Bronze 테이블에 적재.

- **Bronze 테이블**: `demo_catalog.demo_schema.bronze_fear_greed`

주요 파라미터:

| 파라미터 | 기본값 | 설명 |
|:---|:---|:---|
| `MODE` | `"backfill"` | `backfill` \| `once` \| `poll` \| `forever` |
| `LIMIT_ONCE` | `2` | 1회 수집 최신 데이터 수 |
| `BACKFILL_LIMIT` | `200` | 백필 시 과거 데이터 수 (API 최대값) |
| `API_REFRESH_SECONDS` | `86_400` | API는 하루 1회 업데이트 |

최초 실행 권장 설정:

```python
MODE = "backfill"
LIMIT_ONCE = 2
BACKFILL_LIMIT = 200
```

> `poll` / `forever` 모드에서는 최소 24시간 간격(`API_REFRESH_SECONDS`)으로 호출되도록 자동 보정됩니다.

**Run all** 실행 후 검증:

```sql
SELECT COUNT(*) FROM demo_catalog.demo_schema.bronze_fear_greed;
SELECT event_time, index_value, value_classification
FROM demo_catalog.demo_schema.bronze_fear_greed
ORDER BY event_time DESC LIMIT 10;
```

> Fear & Greed 데이터는 Binance 캔들 테이블과 별도 관리되며, Gold 레이어에서 Time-Window Join으로 통합됩니다.

---

## 4) Silver 변환: `pipeline/silver/transform_charts.ipynb`

Bronze `raw_json`을 파싱해 정형 컬럼으로 변환, Silver에 MERGE upsert.

- **입력**: `demo_catalog.demo_schema.bronze_charts`
- **출력**: `demo_catalog.demo_schema.silver_charts`

**Run all** 실행 후 검증:

```sql
SELECT COUNT(*) FROM demo_catalog.demo_schema.silver_charts;
SELECT symbol, interval, open_time, close
FROM demo_catalog.demo_schema.silver_charts
ORDER BY open_time DESC LIMIT 10;
```

---

## 5) Gold 집계: `pipeline/gold/price_signals.ipynb`

Silver 캔들을 4시간 윈도우로 집계, MA50/MA200 및 골든/데드 크로스 신호를 계산해 Gold에 upsert.

- **입력**: `demo_catalog.demo_schema.silver_charts`
- **출력**: `demo_catalog.demo_schema.gold_prices_4h`

**Run all** 실행 후 검증:

```sql
SELECT COUNT(*) FROM demo_catalog.demo_schema.gold_prices_4h;
SELECT symbol, bucket_start, close_4h, ma50_4h, ma200_4h, cross_signal
FROM demo_catalog.demo_schema.gold_prices_4h
ORDER BY bucket_start DESC LIMIT 10;
```

---

## 6) Gold 통합: `pipeline/gold/joined_dashboard.ipynb`

4h 가격 × Fear & Greed 2-way join → 대시보드 통합 뷰 생성.

> **실행 순서 주의:** 이 노트북 실행 전에 `gold/fear_greed_metrics.ipynb` 를 먼저 실행하세요.

- **출력**: `demo_catalog.demo_schema.gold_price_positions_4h`

**Run all** 실행 후 검증:

```sql
SELECT symbol, bucket_start, close_4h, fng_value, fng_label
FROM demo_catalog.demo_schema.gold_price_positions_4h
ORDER BY bucket_start DESC LIMIT 10;
```

---

## 7) 시각화용 뷰 생성

SQL 편집기에 아래 블록을 붙여넣고 실행.

**최신 가격 뷰**

```sql
CREATE OR REPLACE VIEW demo_catalog.demo_schema.v_latest_price AS
WITH last AS (
  SELECT symbol, MAX(open_time) AS last_ts
  FROM demo_catalog.demo_schema.silver_charts
  GROUP BY symbol
)
SELECT s.symbol, s.close AS last_price, s.open_time AS last_ts
FROM demo_catalog.demo_schema.silver_charts s
JOIN last l ON s.symbol = l.symbol AND s.open_time = l.last_ts;
```

**24시간 요약 뷰**

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
  (last_close - first_close)                                   AS abs_change_24h,
  (last_close - first_close) / NULLIF(first_close, 0) * 100   AS pct_change_24h
FROM first_last;
```

**신호 뷰**

```sql
CREATE OR REPLACE VIEW demo_catalog.demo_schema.v_signals AS
SELECT symbol, bucket_start, close_4h, ma50_4h, ma200_4h, cross_signal, pct_change_24h
FROM demo_catalog.demo_schema.gold_prices_4h;
```

---

## 8) 클러스터 권장 설정

| 설정 | 권장값 | 이유 |
|:---|:---|:---|
| **Runtime** | Databricks Runtime 14.x LTS (또는 15.x) | Delta 3.x + Spark 3.5.x 호환성 보장 |
| **Node type** | Memory-optimized (예: AWS `r5d.xlarge`) | Window 함수 및 MERGE 연산이 셔플 데이터를 메모리에 보유 |
| **Autoscaling** | Min: 2, Max: 8 workers | 백필(Backfill) 시 일시적 스파이크; 정상 운영은 낮은 수준 유지 |
| **Auto-termination** | 20분 | 폴링 간격 동안 유휴 클러스터 비용 방지 |
| **Photon** | 활성화 (가능한 경우) | 벡터화된 스캔 기반 Gold 쿼리에서 2~4배 속도 향상 |

모든 Silver/Gold 노트북 상단에 설정된 Spark 성능 구성:

```python
spark.conf.set("spark.databricks.delta.optimizeWrite", "true")  # 소파일 자동 병합
spark.conf.set("spark.databricks.delta.autoCompact",   "true")  # 백그라운드 컴팩션
spark.conf.set("spark.sql.adaptive.enabled",           "true")  # AQE 활성화
spark.conf.set("spark.sql.adaptive.coalescePartitions.enabled", "true")  # 셔플 후 소파티션 병합
spark.conf.set("spark.sql.adaptive.skewJoin.enabled",  "true")  # 데이터 Skew 자동 처리
```

---

## 8a) 정기 유지보수: `pipeline/maintenance/delta_optimize_vacuum.ipynb`

주 1회 Databricks Workflow Job으로 스케줄 권장 (일요일 02:00 UTC).

| 단계 | 내용 |
|:---|:---|
| **OPTIMIZE + ZORDER** | 소파일 병합 및 데이터 클러스터링 (`symbol`, `bucket_start` 기준) |
| **VACUUM** | 보존 기간(Gold: 14일, Bronze/Silver: 7일) 초과 파일 물리 삭제 |
| **DESCRIBE HISTORY** | Gold 테이블 트랜잭션 이력 감사 |
| **DESCRIBE DETAIL** | OPTIMIZE 전후 `numFiles` 감소 확인 |

---

## 8b) Time Travel & Delta Lake 운영

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

---

## 8c) 트러블슈팅

| 증상 | 원인 및 해결 방법 |
|:---|:---|
| **429 Too Many Requests** | 심볼/인터벌 수를 줄이거나 백필 시간을 단축. 스크립트는 Exponential Backoff로 자동 재시도. |
| **카탈로그/스키마 권한 오류** | 어드민에게 권한 요청 또는 기존 카탈로그/스키마 사용. |
| **Binance API 연결 실패** | VPC/방화벽 아웃바운드 규칙 확인. |
| **중복 데이터** | 모든 단계에서 MERGE와 dropDuplicates 적용 → 멱등성 보장. |
| **Fear & Greed 갱신 주기** | API는 하루 1회(약 00:00 UTC) 업데이트. 24시간 이하 반복 호출 시 동일 값 반환; 스크립트가 최소 간격을 자동 보정. |
| **joined_dashboard 실행 실패** | 선행 조건(`gold_fear_greed`)이 없으면 노트북이 즉시 예외를 던짐. `gold/fear_greed_metrics.ipynb`를 먼저 실행. |
| **소파일 과다** | `maintenance/delta_optimize_vacuum.ipynb` 을 주 1회 실행하여 OPTIMIZE + ZORDER 적용. VACUUM은 DRY RUN으로 삭제 대상을 먼저 확인 후 실제 삭제. |

---

## 9) 정리 (Cleanup)

```sql
DROP VIEW  IF EXISTS demo_catalog.demo_schema.v_latest_price;
DROP VIEW  IF EXISTS demo_catalog.demo_schema.v_summary_24h;
DROP VIEW  IF EXISTS demo_catalog.demo_schema.v_signals;

DROP TABLE IF EXISTS demo_catalog.demo_schema.gold_price_positions_4h;
DROP TABLE IF EXISTS demo_catalog.demo_schema.gold_prices_4h;
DROP TABLE IF EXISTS demo_catalog.demo_schema.gold_fear_greed;
DROP TABLE IF EXISTS demo_catalog.demo_schema.silver_charts;
DROP TABLE IF EXISTS demo_catalog.demo_schema.silver_fear_greed;
DROP TABLE IF EXISTS demo_catalog.demo_schema.bronze_charts;
DROP TABLE IF EXISTS demo_catalog.demo_schema.bronze_fear_greed;
DROP TABLE IF EXISTS demo_catalog.demo_schema.bronze_ingest_state;
```
