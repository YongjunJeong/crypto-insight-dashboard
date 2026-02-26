### 1. 근본적인 설계 원칙과 직면한 문제

이 파이프라인은 외부 API의 불안정성을 격리하고, 책임 분리 원칙을 기반으로 시스템을 구축했음.

*   **Exactly-Once 보장:** API 재시도나 클러스터 장애 시에도 데이터 중복이나 누락을 방지하는 것을 최우선 과제로 함
*   **성능 유지:** Spark Job 성능 저하의 주범인 **Shuffle** 비용을 복잡한 Join/Aggregation 단계에서 최소화하도록 설계.
*   **유연성 확보:** 외부 API 스키마 변경에 Downstream 파이프라인이 취약해지지 않도록 구조 설계.

### 2. 데이터 수집 및 API 안정성 확보 (Bronze Layer)

#### 2.0. Auto Loader vs. REST Polling: 설계 결정 근거

**왜 Auto Loader(`cloudFiles`) 대신 REST Polling을 선택했는가:**

Auto Loader는 Databricks 네이티브 솔루션으로 클라우드 스토리지(S3/ADLS/GCS)의 파일을 수집할 때 탁월하다. 다음 조건에서 Auto Loader가 적합하다:
1. 데이터 소스가 클라우드 경로에 파일을 출력하는 경우 (파일 알림 또는 디렉토리 리스팅)
2. `cloudFiles.schemaEvolutionMode = "addNewColumns"`로 스키마 진화를 허용하는 경우
3. Structured Streaming으로 실시간 처리가 필요한 경우

**이 프로젝트는 REST Polling이 더 적합한 이유:**
- Binance, Fear & Greed Index, Apyflux는 모두 REST API로, JSON 페이로드를 직접 반환한다. 파일을 클라우드 경로에 쓰지 않으므로 Auto Loader가 직접 소비할 수 없다.
- Auto Loader를 사용하려면 "API 응답 → S3 파일 저장 → Auto Loader가 S3 읽기" 라는 불필요한 중간 단계가 추가되어 복잡도와 비용이 증가한다.
- 폴링 주기(4h 캔들 = 4시간 간격, FNG = 24시간 간격)는 마이크로배치 스트리밍 없이도 체크포인트 기반의 Exactly-Once를 충분히 보장한다.

**Auto Loader가 적합한 확장 시나리오:** Binance WebSocket 호가창 피드나 Kafka 스트림을 소비하는 경우, `spark.readStream.format("kafka")`나 Auto Loader(`cloudFiles`)가 올바른 선택이다. 이 경우에도 메달리온 아키텍처는 그대로 유지된다.

Bronze 단계에서는 어떤 변환도 시도하지 않고 원본 데이터를 그대로 수용함.

#### 2.1. API 호출 안정성 및 Rate Limit 방어

*   **Rate Limit 대응:** Binance/X API 호출 시 발생하는 **429 에러**에 대응하기 위해 **Exponential Backoff** 정책을 적용.
    *   호출 간 **Jitter** (무작위 지연)를 추가하여 순간적인 **버스트(Burst)** 를 회피하고 안정성을 확보.
    *   API 응답 헤더에서 `X-MBX-USED-WEIGHT`를 읽어 모니터링하고, 가중치 초과 전에 속도를 조절하는 **예측적 Cool-down** 로직을 구현.
*   **DLQ 구현:** 복구 불가능한 실패(Auth/4xx) 발생 시 즉시 중단하고, 해당 레코드를 **DLQ(Dead Letter Queue)** 테이블에 기록.
    *   DLQ에는 `error_code`, `payload_json`, `ingest_run_id`를 포함하여 **재현(Replay) 가능성**을 확보.
    *   DLQ가 영구적으로 커지는 것을 방지하기 위해 **30~90일의 Retention 정책** 설정.
*   **보안:** Binance API 키와 같은 민감 정보는 코드에 노출하지 않고 **Databricks Secrets**로 관리.

#### 2.2. 스키마 유연성 및 원본 보존

*   **스키마 진화:** Bronze 테이블에 **Auto Loader**를 사용하고 **`cloudFiles.schemaEvolutionMode="addNewColumns"`** 옵션을 활성화하여 새 컬럼 유입 시 오류 없이 수용.
    *   파싱 오류가 발생한 레코드는 **`_rescued_data`** 컬럼에 격리 저장하여 데이터 유실 방지.
*   **원본 보존:** Silver에서 파싱 I/O 비용을 증가시키지만 Auditability를 위해, 원본 JSON 전체를 **`raw_json`** STRING 컬럼에 저장하는 것을 규칙으로 삼았음.
*   **시간 표준화:** 모든 `event_time`을 Bronze에서 Silver로 올리기 전에 **UTC TIMESTAMP**으로 통일했습니다. KST 변환은 최종 대시보드 레이어에서만 수행하기로 함.

### 3. 데이터 정제 및 통합 (Silver/Gold Layer)

이 단계에서 **Structured Streaming**의 상태 관리 기능과 **Catalyst Optimizer**의 최적화 이점을 극대화.

#### 3.1. Exactly-Once 보장 및 Upsert 처리

*   **Watermark & 중복 제거:** Silver 테이블에서 `withWatermark("event_time", "X minutes")`를 설정하고 `dropDuplicates("unique_key")`를 사용하여 중복을 제거.
    *   Watermark는 늦은 데이터 허용 범위를 정의하고, 이 기간이 지난 상태는 정리하여 **State Store 메모리 폭발(OOM)** 위험을 방지하는 안전장치 역할.
    *   Binance(분 단위)는 **5~10분**으로, F&G Index(일별)는 **1~2일**로 Watermark 기간을 분리하여 효율을 높였음.
*   **Upsert 처리:** 과거 데이터 정정 가능성(예: 수정된 캔들 데이터)에 대비하여, **`MERGE INTO`** 연산을 사용하여 `unique_key` 기준으로 **원자적 갱신**을 수행함.

#### 3.2. Spark 성능 최적화 및 Shuffle 관리

*   **UDF 배제:** 정제 로직에서 **UDF(사용자 정의 함수)** 를 사용하지 않았음. UDF는 **Catalyst Optimizer**의 최적화를 차단하는 블랙박스로 작동하기 때문입니다. 대신 내장 함수와 SQL 표현식으로 구현.
*   **Small Files Problem 해결:** 스트리밍 Job으로 발생하는 작은 파일 폭증을 해결하기 위해 **`OPTIMIZE`** 명령을 주기적으로 실행하여 파일들을 큰 파일로 **병합(Compaction)**.
*   **쿼리 가속:** Gold 테이블에 **`ZORDER BY (symbol, bucket_start)`** 를 적용. 이는 대시보드 필터링 시 **불필요한 데이터 스캔**을 최소화하여 쿼리 응답 속도 향상.
*   **데이터 Skew 대비:** Gold Join 로직에서 **Task Duration 편차(Skew)** 가 발생할 경우, AQE(Adaptive Query Execution)가 자동으로 Skew를 감지해 처리하도록 함.

### 3.3. Adaptive Query Execution (AQE) 상세

AQE(`spark.sql.adaptive.enabled = true`)는 모든 Transform/Gold 노트북에 활성화되어 있다. AQE는 쿼리 플래닝 방식을 **정적(사전 분석) → 동적(런타임 피드백)** 으로 전환한다. 이 파이프라인에서 관련 있는 3가지 기능:

1. **Dynamic Partition Coalescing** (`coalescePartitions.enabled = true`): 셔플 후 Spark가 소규모 포스트-셔플 파티션을 더 적고 큰 파티션으로 병합한다. FNG silver 테이블처럼 하루 1건의 sparse 데이터는 셔플 후 대부분의 파티션이 비어있는데, AQE가 이를 자동으로 병합하여 Task 오버헤드를 줄인다.

2. **Skew Join Optimization** (`skewJoin.enabled = true`): `03d`의 3원 조인에서 prices(고카디널리티)와 positions(심볼별 편중 가능)의 조인 시 BTCUSDT 데이터가 편중되면 특정 Task가 중앙값 대비 5배 이상 커질 수 있다. AQE가 이를 감지하고 해당 파티션을 자동으로 서브태스크로 분할한다.

3. **Dynamic Join Strategy Switching**: AQE는 런타임 통계에서 한쪽이 충분히 작다고 판단되면 Sort-Merge Join을 Broadcast Join으로 전환할 수 있다. `03d`에서 명시적 `F.broadcast(fng)` 힌트와 함께 사용하여 **이중 안전장치**를 구성한다: 힌트가 정적 플래너에, AQE가 동적 플래너에 작동한다.

### 3.4. VACUUM 전략 및 Time Travel 창

VACUUM은 Delta 트랜잭션 로그에서 더 이상 참조하지 않는 Parquet 파일을 물리적으로 삭제한다. 보존 기간(Retention)이 Time Travel 조회 가능 범위를 결정한다.

| 레이어 | 테이블 | VACUUM 보존 기간 | 근거 |
|:---|:---|:---|:---|
| Bronze | 모든 Bronze 테이블 | 7일 (168h) | Append-Only; 재수집 비용 낮음 |
| Silver | 모든 Silver 테이블 | 7일 (168h) | Bronze에서 재구성 가능; 7일은 운영 롤백에 충분 |
| Gold | 모든 Gold 테이블 | 14일 (336h) | 대시보드 source of truth; 발생 수일 후 발견되는 데이터 이상 대응에 필요 |
| DLQ | bronze_dlq | 30일 (720h) | 감사 추적 및 재현(Replay) 목적 장기 보존 |

**스케줄링:** VACUUM과 OPTIMIZE는 `04_maintenance_optimize_vacuum.ipynb`에서 함께 실행되며, Databricks Workflow Job으로 주 1회(일요일 02:00 UTC) 스케줄된다. Gold 파이프라인(03a-03d) 완료 후 이 노트북이 실행된다. OPTIMIZE를 먼저 실행하여 소파일을 병합한 후, VACUUM이 OPTIMIZE로 대체된 구버전 파일을 정리한다.

**Time Travel 주요 활용 패턴:**
- **롤백:** `RESTORE TABLE gold_prices_4h TO VERSION AS OF <N-1>` — 잘못된 Gold 쓰기 복구
- **감사 쿼리:** `SELECT * FROM gold_prices_4h TIMESTAMP AS OF '2025-12-01'` — 특정 시점 스냅샷 재현
- **파이프라인 실행 비교:** `VERSION AS OF N` vs `VERSION AS OF N-1` 조인으로 변경사항 diff

### 4. Gold Layer: 통합, 신호 생성 및 거버넌스

Gold 레이어는 Silver 데이터를 기반으로 **분석 지표**와 **트레이딩 신호**를 생성하는 **데이터 마트(Data Mart)** 역할 수행.

#### 4.1. 이질적 데이터 통합 및 Join 전략

*   **Time-Window Join:** 분 단위 Binance 데이터와 일 단위 F&G Index를 통합하기 위해 **Fixed Time Window** (예: 5분, 1시간)를 기준으로 데이터를 집계하고 조인함.
*   **Shuffle 회피:** F&G Index 테이블이 작다는 점을 이용하여, Join 과정에서 **Network I/O** 비용이 큰 Shuffle을 회피하기 위해 Spark가 자동으로 **Broadcast Join**을 선택하도록 설계함.
*   **Idempotency:** Gold 테이블의 최종 Primary Key인 `symbol | bucket_start`를 기준으로 **`MERGE INTO`** 를 수행하여, 백필(Backfill)이나 재계산 시 데이터가 중복되지 않도록 멱등성을 보장함.

#### 4.2. 거버넌스 및 복구 원칙

*   **Time Travel 복구:** 잘못된 계산 로직으로 오류 데이터가 적재되었을 경우, **Delta Lake 버전 관리**를 활용함. `DESCRIBE HISTORY` 로 버전을 확인하고, `VERSION AS OF N-1` 명령을 통해 이전 상태로 안전하게 복구하는 절차를 `RUNBOOK.md`에 명시함.
*   **장애 진단 루틴:** 데이터 지연(Freshness Lag) 이슈 발생 시, **Spark UI**를 통해 다음 순서로 원인을 진단:
    1.  **SQL 탭:** **Exchange 노드** 개수 확인 (Shuffle 발생 여부).
    2.  **Stages 탭:** **Task Duration 편차(Skew)** 및 **Shuffle Read/Write 크기** 확인.
    3.  **Executors 탭:** OOM 또는 GC(가비지 컬렉션) 시간 확인.
*   **DLQ 관리:** DLQ 테이블에 **SLA/Retention**을 설정하고 `health_notifications.json`의 임계값을 초과하면 알림을 발생시켜 운영 안정성 확보.