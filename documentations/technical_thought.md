### 1. 근본적인 설계 원칙과 직면한 문제

이 파이프라인은 외부 API의 불안정성을 격리하고, 책임 분리 원칙을 기반으로 시스템을 구축했음.

*   **Exactly-Once 보장:** API 재시도나 클러스터 장애 시에도 데이터 중복이나 누락을 방지하는 것을 최우선 과제로 함
*   **성능 유지:** Spark Job 성능 저하의 주범인 **Shuffle** 비용을 복잡한 Join/Aggregation 단계에서 최소화하도록 설계.
*   **유연성 확보:** 외부 API 스키마 변경에 Downstream 파이프라인이 취약해지지 않도록 구조 설계.

### 2. 데이터 수집 및 API 안정성 확보 (Bronze Layer)

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