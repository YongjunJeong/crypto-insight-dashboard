# Crypto Insight Dashboard 데이터 모델 및 아키텍처

## 1. 아키텍처 개요 및 핵심 설계 원칙

본 프로젝트의 데이터 모델은 **Databricks Lakehouse Platform**을 기반으로 하는 **메달리온 아키텍처(Bronze → Silver → Gold)** 를 따르며, **멱등성(Idempotency)** 및 **데이터 정합성** 확보에 중점을 둡니다.
![](/Workspace/Users/joon7239@gmail.com/crypto-insight-dashboard/documentations/Architecture)

### 1.1. 핵심 설계 원칙 (Core Design Principles)

1.  **ACID 트랜잭션:** Delta Lake를 사용하여 데이터 삽입, 갱신, 삭제 시에도 원자성(Atomicity) 및 일관성(Consistency)을 보장합니다.
2.  **멱등성(Idempotency):** 상태 테이블 체크포인트(`bronze_ingest_state`)와 `unique_key` 기준 `MERGE INTO`를 조합하여, 재실행/백필이 몇 번 발생해도 결과가 동일하도록 설계합니다.
3.  **성능 최적화:** Gold 레이어에서 **OPTIMIZE** 및 **ZORDER**를 적용하여 대규모 데이터 분석 쿼리 속도를 높입니다.
4.  **추적성:** 모든 원본 데이터는 Bronze에 `raw_json`으로 보존되며, Downstream 파이프라인은 이 원본에 기반하여 재처리가 가능하도록 설계됩니다.

## 2. 레이어별 데이터 모델 상세 정의

### 2.1. Bronze Layer (Raw Zone)

**목적:** 외부 API로부터 수집된 원본 데이터(Raw JSON)를 REST 폴링으로 적재하여 재처리를 위한 안전한 감사 증적(Audit Trail)을 제공합니다.

| 엔티티명 (테이블) | 소스 | 목적 | Unique Key | 주요 거버넌스 컬럼 |
| :--- | :--- | :--- | :--- | :--- |
| `bronze_charts` | Binance `/api/v3/klines` | 캔들(4h) 원본 보존 | `symbol｜interval｜open_time(ms)` | `raw_json`, `event_time`(UTC), `ingest_time`, `api_params_hash` |
| `bronze_ingest_state` | (내부 상태) | 심볼/인터벌별 마지막 수집 위치 체크포인트 | `symbol + interval` | `last_open_time_ms`, `updated_at` |
| `bronze_fear_greed` | api.alternative.me | Fear & Greed Index 원본 보존 | `fear_greed｜unix_ts` | `raw_json`, `api_params_hash` |

**품질 및 거버넌스:**
*   **재개 가능성:** `bronze_ingest_state`가 심볼/인터벌별 마지막 처리 위치를 저장해, Job 재시작 시 그 지점부터 재개하고 중복 수집을 방지합니다.
*   **시간 표준화:** 모든 `event_time`은 **UTC TIMESTAMP**으로 저장됩니다 (`spark.sql.session.timeZone=UTC` 명시).

### 2.2. Silver Layer (Clean Zone)

**목적:** Bronze 데이터를 읽어 데이터 타입 표준화, 명시적 스키마 적용, 그리고 **중복 제거(De-duplication)** 를 수행합니다.

| 엔티티명 (테이블) | 역할 | Unique Key (De-dup 기준) | 핵심 품질 관리 |
| :--- | :--- | :--- | :--- |
| `silver_charts` | 정제된 Binance 4h 캔들 | `unique_key` (`symbol｜interval｜open_time`) | DQ 어서션(null/high<low/close<=0 검사), MERGE Upsert |
| `silver_fear_greed` | 정제된 F&G 지수 | `unique_key` (`fear_greed｜unix_ts`) | DQ 어서션, INT 타입 캐스팅 |

**핵심 정제 메커니즘:**
*   **중복 제거:** `dropDuplicates(["unique_key"])`로 배치 재실행 시 중복 입력을 제거하고, `MERGE INTO`로 `unique_key` 기준 원자적 갱신을 수행합니다.
*   **품질 게이트:** MERGE 직후 최근 데이터에 대해 null/범위 위반을 검사하고, 위반 시 `assert`로 즉시 실패시켜 오염 데이터가 Gold로 전파되지 않도록 합니다.
*   **타입 정규화:** `raw_json`을 파싱할 때 UDF 대신 `from_json` 등 내장 함수와 명시적 스키마를 사용해 Catalyst Optimizer 최적화 이점을 유지합니다.

### 2.3. Gold Layer (Curated Zone / Data Mart)

**목적:** Silver 데이터를 통합하여 **분석 지표(MA50/MA200, Cross Signal)** 를 생성하고, BI 분석 쿼리에 최적화된 **데이터 마트** 역할을 수행합니다.

| 엔티티명 (테이블) | 역할 | Primary Key (Idempotency 기준) | 최적화 전략 |
| :--- | :--- | :--- | :--- |
| `gold_prices_4h` | 4h 가격 + MA50/MA200 + 전환 이벤트(cross_signal) | `symbol` + `bucket_start` | OPTIMIZE, ZORDER(`symbol`, `bucket_start`) |
| `gold_fear_greed` | F&G 지수 + MA7/MA30 + Z-Score + streak | `ts_utc` + `dt` | OPTIMIZE, ZORDER(`dt`) |
| `gold_price_positions_4h` | 가격×FNG 2-way 조인 집계 (대시보드 메인 소스) | `symbol` + `bucket_start` | OPTIMIZE, ZORDER(`symbol`, `bucket_start`) |

## 3. 핵심 관계 및 최적화 전략

### 3.1. 데이터 통합: Time-Window Join

*   **문제:** Silver의 `silver_charts` (4시간 캔들)와 `silver_fear_greed` (일별 저빈도)는 수집 주기가 다릅니다.
*   **해결:** `gold/joined_dashboard.ipynb`에서 FNG를 `bucket_end` 이전 최신 1건을 as-of join으로 매핑합니다(신선도 제한 포함).
*   **Idempotency:** `gold_price_positions_4h`에 데이터를 쓸 때, `symbol`과 `bucket_start` 복합 키를 기준으로 `MERGE INTO`를 수행하여 재계산 시 중복이 발생하지 않도록 합니다.

### 3.2. 성능 최적화 메커니즘

*   **Small Files Problem 완화:** 배치 Job이 자주 실행되며 발생하는 작은 파일들을 `OPTIMIZE`로 주기적으로 병합합니다.
*   **쿼리 가속:** 대시보드에서 주로 필터링에 사용되는 `symbol`, `bucket_start`에 `ZORDER`를 적용해 Data Skipping 효과를 높입니다.
*   **파티셔닝:** 모든 레이어는 `dt = date(event_time)` 기준으로 파티셔닝되어 쿼리 시 불필요한 데이터 스캔(Partition Pruning)을 최소화합니다.

## 4. 운영 및 거버넌스 원칙

*   **Time Travel 및 복구:** 모든 Delta Table은 변경 이력을 자동으로 저장하며, 잘못된 데이터가 적재되었을 경우 `VERSION AS OF` 또는 `TIMESTAMP AS OF` 명령을 사용하여 특정 시점의 데이터로 롤백하거나 과거 상태를 재현할 수 있습니다.
*   **재개 가능성:** Bronze 수집 Job이 중단되어도 `bronze_ingest_state` 체크포인트를 기반으로 마지막 성공 지점부터 안전하게 재개됩니다.
*   **소스 코드 관리:** 모든 노트북 및 파이프라인 스크립트는 **Databricks Repos**를 통해 Git과 동기화되어 버전 관리 및 협업을 지원합니다.
