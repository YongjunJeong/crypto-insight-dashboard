## 1. 프로젝트 개요 및 설계 원칙 (Design Overview and Tenets)

### 1.1 프로젝트 목표

- 본 Crypto Insight Dashboard 파이프라인은 Binance, Fear & Greed Index와 같은 **다중 외부 API 소스**로부터 데이터를 수집, 정제, 통합하여 **신뢰할 수 있는 투자 신호**를 생성하는 것을 목표로 합니다.
- 모든 데이터 파이프라인과 거버넌스는 Databricks Lakehouse Platform 위에서 Apache Spark, Delta Lake 기술을 사용하여 자동화됩니다.

### 1.2 핵심 설계 원칙 (Core Design Tenets)

*   **데이터 신뢰성 (Data Reliability):** Delta Lake의 ACID 트랜잭션과 스키마 관리 기능을 활용하여 데이터 무결성 보장.
*   **통합 API (Unified API):** Structured Streaming (Micro-Batch 방식)을 통해 배치(Batch)와 스트리밍(Streaming) 처리를 통합하여 코드를 일관성 있게 관리.
*   **운영 안정성 (Observability):** Checkpoint, Watermark, DLQ(Dead Letter Queue) 설계를 통해 장애 시 복구 능력 및 데이터 유실 방지.
*   **성능 최적화 (Optimization):** Gold 레이어에서 OPTIMIZE 및 ZORDER를 적용하여 BI 쿼리 성능을 극대화.

## 2. 레이크하우스 아키텍처 (The Medallion Model)

파이프라인은 Databricks의 표준인 **메달리온 아키텍처** (Bronze → Silver → Gold)를 따릅니다.

### 2.1 Bronze Layer (Raw Zone)

Bronze 레이어는 모든 **원본 데이터의 무결성**과 **추적성(Auditability)** 을 보장하는 역할을 합니다.

| 설계 항목 | 역할 및 상세 원칙 | 적용 기술 (Databricks / Spark) |
| :--- | :--- | :--- |
| **데이터 수집** | **Append-Only** 전략을 사용해 기존 데이터를 수정하지 않고 새로 들어온 데이터만 추가합니다. | **Auto Loader (JSON Source)** 또는 **REST API Polling Jobs**를 통해 데이터를 Delta Lake에 적재합니다. |
| **스키마 유연성** | API 응답 JSON의 **스키마가 수시로 바뀌어도** 파이프라인이 중단되지 않고 새로운 컬럼을 안전하게 수용해야 합니다. | **`cloudFiles.schemaEvolutionMode="addNewColumns"`** 옵션을 사용하여 새 컬럼을 자동으로 반영합니다. |
| **원본 보존** | 원본 API 응답 전체를 변환 없이 **`raw_json`** 이라는 단일 STRING 컬럼에 저장합니다. | **Shadow Columns:** 디버깅과 빠른 필터링을 위해 `event_time`(UTC), `unique_key`, `ingest_time` 등 핵심 트레이스 컬럼을 추출하여 저장합니다. |
| **거버넌스** | 모든 API 키는 **Databricks Secrets**를 통해 관리되어 코드에 노출되지 않도록 합니다. | API 호출의 `ingest_run_id`와 `api_params_json`을 기록하여 **재현(Replay) 가능성**을 확보합니다. |

### 2.2 Silver Layer (Clean Zone)

Silver 레이어는 Bronze의 원본 데이터를 읽어 **표준화(Standardization)**, **정제(Cleansing)**, 그리고 **중복 제거(De-duplication)** 를 수행하여 분석 가능한 형태로 만듭니다.

| 설계 항목 | 역할 및 상세 원칙 | 적용 기술 (Databricks / Spark) |
| :--- | :--- | :--- |
| **데이터 정제** | `raw_json`을 파싱하여 모든 컬럼을 명시적인 타입(TIMESTAMP, DOUBLE 등)으로 캐스팅하고 **타입 승격**을 일괄 처리합니다. | **UDF 남용 금지:** Catalyst Optimizer의 최적화 이점을 유지하기 위해 UDF 대신 내장 함수(`from_json`, `withColumn`, `when`)를 사용합니다. |
| **중복 제거** | 입력/재시도로 인한 중복 데이터를 제거하고 **Exactly-once Processing**을 달성합니다. | **`withWatermark`** 와 **`dropDuplicates(unique_key)`** 조합을 사용하여 지연 도착 데이터(Late Data)를 허용하면서도 안전하게 중복을 제거합니다. |
| **상태 관리** | 스트리밍 작업의 상태(State)와 복구 정보를 저장합니다. | **Checkpoint** 경로를 설정하여 클러스터 장애 시에도 마지막 성공 시점부터 안전하게 처리를 재개합니다. |
| **Upsert 지원** | Binance처럼 과거 데이터가 수정될 수 있는 경우, 수정을 반영하여 정합성을 유지합니다. | **`MERGE INTO`** 연산을 사용하여 기존 레코드를 업데이트하고 새로운 레코드를 삽입하는 **원자적 Upsert**를 구현합니다. |

### 2.3 Gold Layer (Curated Zone / Data Mart)

Gold 레이어는 비즈니스 분석가와 대시보드 소비자를 위한 최종 **데이터 마트** 역할을 합니다. 여기에는 **집계된 지표와 거래 신호**가 저장되며 쿼리 성능에 최적화됩니다.

| 설계 항목 | 역할 및 상세 원칙 | 적용 기술 (Databricks / Spark) |
| :--- | :--- | :--- |
| **신호 생성** | Silver 데이터를 기반으로 MA, RSI, Golden/Dead Cross 등 파생된 **기술적 지표 및 신호**를 계산합니다. | **Window Function** 및 복잡한 SQL/PySpark 집계 로직을 사용하여 시간 기반 지표를 계산합니다. |
| **성능 최적화** | Small Files Problem을 완화하고 메타데이터 I/O 비용을 줄입니다. | **`OPTIMIZE`** 명령을 정기적으로 수행하여 작은 파일들을 큰 파일로 병합합니다. |
| **쿼리 가속** | 대시보드에서 자주 필터링되는 컬럼의 검색 속도를 높입니다. | **`ZORDER BY (symbol, bucket_start)`** 를 적용하여 데이터를 클러스터링합니다. |
| **Idempotency** | 재계산/백필이 발생해도 데이터 중복 없이 정확히 업데이트되도록 합니다. | **Gold 테이블의 PK(예: `symbol | ts`)** 를 기준으로 **`MERGE INTO`** 를 수행합니다. |

## 3. 핵심 기술 요소 및 운영 안정성

### 3.1 Structured Streaming & Time Window

*   **Micro-Batch:** Structured Streaming은 들어오는 데이터를 짧은 배치 단위로 처리하며, 쿼리 실패 시 **Checkpoint**를 기반으로 마지막 처리 시점부터 복구하여 Exactly-once 처리를 보장합니다.
*   **Time Window Join:** Binance 캔들 데이터와 Fear & Greed Index와 같이 **서로 다른 수집 주기**를 가진 데이터 소스를 안전하게 통합하기 위해, Silver/Gold 레이어에서 **시간 윈도우(Time Window)** 를 설정하고 조인 키로 사용합니다.

### 3.2 거버넌스 및 디버깅 (Governance & Debugging)

*   **Time Travel:** Delta Lake의 모든 변경 이력이 `_delta_log`에 버전으로 기록되므로, 쿼리를 통해 특정 버전(`VERSION AS OF`) 또는 시점(`TIMESTAMP AS OF`)의 데이터를 조회하여 **잘못된 데이터 적재 시 롤백**할 수 있습니다.
*   **DLQ (Dead Letter Queue):** API 에러, 스키마 불일치 등으로 처리가 불가능한 레코드는 `_rescued_data` 컬럼에 수집되거나 별도 **DLQ 테이블에 격리 저장**되어 데이터 유실을 방지하고 추후 분석 및 재처리가 가능합니다.
*   **Git 연동:** 모든 파이프라인 노트북과 문서는 **Databricks Repos**를 통해 Git(GitHub)과 연동되어 버전 관리 및 협업이 용이합니다.

### 3.3 클러스터 및 성능 관리

*   **Autoscaling:** Databricks 클러스터는 Task 대기열이 쌓이면 자동으로 Worker 노드를 추가(Scale Up)하고, 유휴 상태일 때는 Auto-Termination으로 종료되어 비용 효율성을 높입니다.
*   **Spark UI 진단:** 성능 병목(Bottleneck) 발생 시, **SQL 탭**에서 Shuffle 노드(Exchange)를 확인하고, **Stages 탭**에서 Task Duration 편차(Skew)를 확인하여 문제 원인을 신속하게 진단합니다.