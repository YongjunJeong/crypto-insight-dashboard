# Crypto Insight Dashboard 데이터 모델 및 아키텍처

## 1. 아키텍처 개요 및 핵심 설계 원칙

본 프로젝트의 데이터 모델은 **Databricks Lakehouse Platform**을 기반으로 하는 **메달리온 아키텍처(Bronze → Silver → Gold)** 를 따르며, **운영 안정성(Observability)** 및 **데이터 정합성** 확보에 중점을 둡니다.
![](/Workspace/Users/joon7239@gmail.com/crypto-insight-dashboard/documentations/Architecture)

### 1.1. 핵심 설계 원칙 (Core Design Principles)

1.  **ACID 트랜잭션:** Delta Lake를 사용하여 데이터 삽입, 갱신, 삭제 시에도 원자성(Atomicity) 및 일관성(Consistency)을 보장합니다.
2.  **Exactly-Once 처리:** Structured Streaming의 **Watermark**와 **Checkpoint**를 활용하여 데이터 중복이나 누락 없이 처리합니다.
3.  **성능 최적화:** Gold 레이어에서 **OPTIMIZE** 및 **ZORDER**를 적용하여 대규모 데이터 분석 쿼리 속도를 높입니다.
4.  **추적성:** 모든 원본 데이터는 Bronze에 보존되며, Downstream 파이프라인은 이 원본에 기반하여 재처리가 가능하도록 설계됩니다.

## 2. 레이어별 데이터 모델 상세 정의

### 2.1. Bronze Layer (Raw Zone)

**목적:** 외부 API로부터 수집된 원본 데이터(Raw JSON)를 **Append-Only** 형태로 보존하여 재처리를 위한 안전한 감사 증적(Audit Trail)을 제공합니다.

| 엔티티명 (테이블) | 목적 | Unique Key (PK 역할) | 주요 거버넌스 컬럼 |
| :--- | :--- | :--- | :--- |
| `bronze_binance` | Binance Kline 데이터 원본 보존 | `symbol` \| `interval` \| `open_time` | `raw_json`, `event_time`(UTC), `ingest_time` |
| `bronze_fear_greed` | F&G Index 데이터 원본 보존 | `event_time` (timestamp) | `raw_json`, `api_params_json`, `ingest_run_id` |

**품질 및 거버넌스:**
*   **스키마 유연성:** **Auto Loader**를 사용하여 API 응답 스키마 변경 시에도 **파이프라인 중단 없이** 데이터를 수용하며, 예외 레코드는 `_rescued_data` 컬럼으로 격리 저장하여 유실을 방지합니다.
*   **시간 표준화:** 모든 `event_time`은 **UTC TIMESTAMP**으로 저장됩니다.

### 2.2. Silver Layer (Clean Zone)

**목적:** Bronze 데이터를 읽어 데이터 타입 표준화, 명시적 스키마 적용, 결측치 처리, 그리고 **중복 제거(De-duplication)** 를 수행합니다.

| 엔티티명 (테이블) | 역할 | Unique Key (De-dup 기준) | 핵심 품질 관리 |
| :--- | :--- | :--- | :--- |
| `silver_charts` | 정제된 Binance 캔들 | `symbol` + `interval` + `open_time` | Watermark 기반 중복 제거, MERGE Upsert 적용 |
| `silver_fear_greed` | 정제된 F&G 지수 | `date(fgi_ts)` (일별 고유성) | 0~100 범위 체크, INT 타입 캐스팅 |

**핵심 정제 메커니즘:**
*   **Exactly-Once 처리:** Structured Streaming에서 **`withWatermark("event_time", "X minutes")`**를 설정하여 지연 도착 데이터(Late Data)를 허용하고, **`dropDuplicates(unique_key)`**를 적용하여 재시도로 인한 중복 입력을 제거합니다.
*   **Upsert 지원:** 원본 소스가 과거 데이터를 수정할 수 있는 경우, **`MERGE INTO`** 연산을 사용하여 `unique_key` 기준으로 **원자적 갱신(Atomic Update)** 을 보장합니다.
*   **타입 정규화:** `raw_json`을 파싱할 때 **UDF 남용을 피하고**, `from_json` 등의 내장 함수와 명시적 스키마를 사용하여 Catalyst Optimizer의 최적화 이점을 유지합니다.

### 2.3. Gold Layer (Curated Zone / Data Mart)

**목적:** Silver 데이터를 통합하여 **분석 지표(MA, RSI)** 및 **트레이딩 신호**를 생성하고, BI 분석 쿼리에 최적화된 **데이터 마트** 역할을 수행합니다.

| 엔티티명 (테이블) | 역할 | Primary Key (Idempotency 기준) | 최적화 전략 |
| :--- | :--- | :--- | :--- |
| `gold_signals` | 가격 지표 및 통합 분석 신호 저장 | `symbol` + `bucket_start` (Window Start Time) | OPTIMIZE, ZORDER 적용 |

## 3. 핵심 관계 및 최적화 전략

### 3.1. 데이터 통합: Time-Window Join

*   **문제:** Silver의 `silver_charts` (분 단위 고빈도)와 `silver_fear_greed` (일별 저빈도)는 수집 주기가 다릅니다.
*   **해결:** Gold 레이어에서 **시간 윈도우(Time Window)** (예: 5분, 1시간)를 기준으로 데이터를 집계하고 조인하여 통합합니다. `gold_signals` 테이블의 Primary Key인 **`bucket_start`** 가 이 조인의 기준점이 됩니다.
*   **Idempotency:** Gold 테이블에 데이터를 쓸 때, **`symbol`과 `bucket_start`** 복합 키를 기준으로 **`MERGE INTO`** 를 수행하여 재계산 시 중복이 발생하지 않도록 멱등성(Idempotency)을 보장합니다.

### 3.2. 성능 최적화 메커니즘

*   **Small Files Problem 완화:** 스트리밍 쓰기(Streaming Write)로 인해 Bronze나 Silver에 작은 파일이 다량 발생하면 I/O 비용 및 메타데이터 처리 비용이 증가합니다. 이를 방지하기 위해 **`OPTIMIZE`** 명령을 주기적으로 실행하여 작은 파일들을 큰 파일로 병합합니다.
*   **쿼리 가속:** 대시보드에서 주로 필터링 및 범위 스캔에 사용되는 컬럼인 **`symbol`** 과 **`bucket_start`** 에 **`ZORDER`** 를 적용하여 클러스터링을 수행함으로써 검색 성능을 획기적으로 향상시킵니다.
*   **파티셔닝:** 모든 레이어는 **`dt = date(event_time)`** 기준으로 파티셔닝되어 쿼리 시 불필요한 데이터 스캔(Partition Pruning)을 최소화합니다.

## 4. 운영 및 거버넌스 원칙

이 모델은 Databricks SE가 필수적으로 관리해야 하는 운영 기능을 포함합니다.

*   **Time Travel 및 복구:** 모든 Delta Table은 변경 이력을 자동으로 저장하며, 잘못된 데이터가 적재되었을 경우 **`VERSION AS OF`** 또는 **`TIMESTAMP AS OF`** 명령을 사용하여 특정 시점의 데이터로 롤백하거나 과거 상태를 재현할 수 있습니다.
*   **Fault Tolerance:** Structured Streaming은 **Checkpoint**를 사용하여 Job 중단 시에도 안전하게 복구되어 데이터 유실 없이 처리를 이어갑니다.
*   **보안 및 비밀 관리:** Binance API 키와 같은 민감 정보는 코드에 노출하지 않고 **Databricks Secrets**로 관리됩니다.
*   **소스 코드 관리:** 모든 노트북 및 파이프라인 스크립트는 **Databricks Repos**를 통해 Git과 동기화되어 버전 관리 및 협업을 지원합니다.