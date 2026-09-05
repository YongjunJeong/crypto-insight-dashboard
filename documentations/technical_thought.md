### 1. 근본적인 설계 원칙과 직면한 문제

이 파이프라인은 외부 API의 불안정성을 격리하고, 스트리밍 인프라 없이도 멱등성(Idempotency)을 확보하는 것을 최우선 과제로 삼아 구축했음.

*   **멱등성 우선:** Structured Streaming의 Checkpoint/Watermark 없이도, 상태 테이블 체크포인트 + `MERGE`만으로 "몇 번을 재실행해도 같은 결과"를 얻는 것을 목표로 함.
*   **성능 유지:** Spark Job 성능 저하의 주범인 Shuffle 비용을 Join/Aggregation 단계에서 최소화하도록 설계.
*   **유연성 확보:** 외부 API 스키마 변경에 Downstream 파이프라인이 취약해지지 않도록 구조 설계.

### 2. 왜 스트리밍이 아니라 배치 폴링인가

처음엔 "그럴싸해 보이는" Auto Loader + Structured Streaming 구성을 고려했지만, 실제 데이터 소스(Binance/alternative.me REST API)는 클라우드 스토리지에 파일이 떨어지는 방식이 아니라서 Auto Loader의 강점(파일 이벤트 기반 증분 감지)이 애초에 발휘될 지점이 없었음. 그리고 Gold의 최소 단위가 4시간 봉이라 분 단위 실시간성이 요구되지도 않았음. 그래서 "REST 폴링 + 상태 테이블 체크포인트 + MERGE" 조합으로 방향을 잡았고, 결과적으로 상시 클러스터 없이 Job 실행 시간만 과금되는 구조가 되어 비용 면에서도 이득이었음. 분 단위 이하 신선도가 필요해지는 시점이 오면 Bronze 수집기만 Auto Loader/Structured Streaming으로 교체하면 되도록 Bronze의 출력 스키마(`raw_json`, `unique_key`, `event_time`)를 소스 방식에 무관하게 고정해뒀음.

### 3. 데이터 수집 및 API 안정성 확보 (Bronze Layer)

Bronze 단계에서는 어떤 변환도 시도하지 않고 원본 데이터를 그대로 수용함.

#### 3.1. API 호출 안정성 및 Rate Limit 방어

*   **Rate Limit 대응:** Binance API 호출 시 발생하는 **429 에러**에 대응하기 위해 **Exponential Backoff** 정책을 적용.
    *   호출 간 **Jitter** (무작위 지연)를 추가하여 순간적인 **버스트(Burst)** 를 회피.
    *   API 응답 헤더에서 `X-MBX-USED-WEIGHT`를 읽어 로그로 남기고, 가중치 초과 전에 속도를 조절할 수 있는 근거로 사용.
*   **보안:** 민감한 API 키가 필요한 경우 코드에 노출하지 않고 **Databricks Secrets**로 관리.

#### 3.2. 재개 가능성과 원본 보존

*   **상태 테이블 체크포인트:** Binance 수집기는 `bronze_ingest_state`에 심볼/인터벌별 마지막 `open_time`(ms)을 기록해, Job이 중단돼도 그 지점부터 재개하고 중복 수집을 방지함.
*   **원본 보존:** Silver에서 파싱 I/O 비용이 늘어나더라도 Auditability를 위해, 원본 JSON 전체를 `raw_json` STRING 컬럼에 저장하는 것을 규칙으로 삼았음.
*   **시간 표준화:** 모든 `event_time`을 Bronze에서부터 UTC TIMESTAMP으로 통일함. (`spark.sql.session.timeZone=UTC`를 모든 노트북 상단에 명시해둠. 세션 타임존이 UTC가 아니면 문자열→timestamp 파싱 시 값이 왜곡되는 문제를 초기에 겪어서, 이후 모든 노트북에 이 설정을 통일함.)

### 4. 데이터 정제 및 통합 (Silver/Gold Layer)

이 단계에서 **Catalyst Optimizer**의 최적화 이점을 극대화.

#### 4.1. 멱등성 및 Upsert 처리

*   **De-dup & MERGE:** Silver 테이블에서 `dropDuplicates("unique_key")`로 배치 내 중복을 제거한 뒤 `MERGE INTO`로 `unique_key` 기준 원자적 갱신을 수행함.
*   **품질 게이트(DQ):** MERGE 직후 최근 데이터에 대해 null/범위(예: `high < low`, `close <= 0`, `leverage <= 0`) 위반을 검사하고, 위반 시 `assert`로 노트북을 즉시 중단시켜 오염 데이터가 Gold로 전파되지 않게 함.

#### 4.2. Spark 성능 최적화 및 Shuffle 관리

*   **UDF 배제:** 정제 로직에서 UDF(사용자 정의 함수)를 사용하지 않음. UDF는 Catalyst Optimizer의 최적화를 차단하는 블랙박스로 작동하기 때문. 대신 내장 함수와 SQL 표현식으로 구현.
*   **Small Files Problem 해결:** 배치 Job으로 발생하는 작은 파일 폭증을 해결하기 위해 `OPTIMIZE` 명령을 주기적으로 실행하여 파일들을 큰 파일로 병합(Compaction).
*   **쿼리 가속:** Gold 테이블에 `ZORDER BY (symbol, bucket_start)`를 적용. 대시보드 필터링 시 불필요한 데이터 스캔을 최소화하여 쿼리 응답 속도 향상.
*   **데이터 Skew 대비:** Gold Join 로직에서 Task Duration 편차(Skew)가 발생할 경우, `spark.sql.adaptive.skewJoin.enabled`를 활성화해 AQE가 자동으로 감지·처리하도록 함.

### 5. Gold Layer: 통합, 신호 생성, 그리고 겪었던 버그

Gold 레이어는 Silver 데이터를 기반으로 분석 지표와 트레이딩 신호를 생성하는 데이터 마트 역할 수행.

#### 5.1. 버그 사후분석: "신호"와 "상태"를 혼동했던 cross_signal

초기 구현에서는 `ma50 > ma200`이면 매 바(bar)를 그대로 "Golden Cross"로 라벨링했음. 그런데 이렇게 하면 상승 추세가 지속되는 내내 수백 개 바가 전부 "Golden Cross"로 찍혀서, 사실상 "이동평균 상태"를 보여주는 것이지 "골든크로스 이벤트가 발생했다"는 신호가 아니었음. 이 둘을 혼동한 것이 근본 원인. 수정 방향은 직전 바의 추세(`trend_state`)와 현재 바의 추세를 비교해서, **전환이 일어난 바에서만** `cross_signal`을 채우고, 나머지는 `trend_state`(현재 상태)로 따로 노출하는 것. "상태"와 "이벤트"를 같은 컬럼에 담지 않는다는 원칙을 여기서 얻음.

#### 5.2. 스코프 결정: 선물 리더보드(다른 트레이더 포지션) 기능을 왜 뺐는가

초기 버전은 Binance 선물 리더보드에서 다른 트레이더들의 실시간 포지션을 가져와 가격 차트에 마커로 얹는 기능까지 있었음(Apyflux라는 유료 게이트웨이를 통해 접근). 그 과정에서 "계정(uid)×심볼(symbol)별 최신 포지션 1건"만 남기는 `row_number()` 윈도우 로직에 실제 버그도 있었음. Binance 선물 헤지모드에서는 동일 계정·심볼에 LONG/SHORT이 동시에 존재할 수 있는데, 파티션 키에 `positionSide`가 빠져 있어 한쪽이 조용히 사라지는 문제였음. ("row_number 기반 최신값 추출"을 쓸 때 엔티티의 자연키에 빠진 차원이 없는지부터 의심해야 한다는 교훈은 남음.)

그런데 이 버그를 고치던 중 더 근본적인 문제를 발견함: 애초에 이 데이터 소스(`getOtherPosition`) 자체가 Binance가 공식 문서화하지 않은 내부 API이고, Apyflux는 그걸 리버스 엔지니어링해서 재판매하는 유료 게이트웨이였음. 실제로 Binance는 2024년부터 이 엔드포인트를 인증 필요(비공개)로 전환했고, 대체재로 찾아본 RapidAPI/Apify 등도 전부 같은 방식(비공식 엔드포인트 스크래핑)이었음. 즉 버그를 고쳐도 기반 자체가 "언제든 차단될 수 있는 비공식 API에 의존"하는 구조라, 다른 스크래퍼로 갈아타는 건 같은 리스크를 이름만 바꿔 반복하는 것이었음. 그래서 이 기능은 고치는 대신 스코프에서 제외했음. 공식 문서화된 API로 대체 가능한 지점(예: 본인 계정의 `positionRisk` 조회)이 생기면 재검토할 여지는 남겨둠. "고칠 수 있는 버그"와 "고쳐도 기반이 무너지는 의존성"을 구분해야 한다는 게 이 결정에서 얻은 교훈.

#### 5.3. 이질적 데이터 통합 및 Join 전략

*   **Time-Window Join:** 4시간 단위 Binance 데이터와 일 단위 F&G Index를 통합하기 위해 `bucket_end` 이전 최신 1건을 as-of join으로 매핑하고, 신선도(`FNG_FRESH_DAYS`)를 초과하면 null 처리함.
*   **Broadcast Join:** F&G Index 테이블이 연간 최대 365행 수준으로 작다는 점을 이용해 `broadcast()`를 명시적으로 지정하여 대용량 가격 테이블과의 Shuffle을 제거함.

#### 5.4. 거버넌스 및 복구 원칙

*   **Time Travel 복구:** 잘못된 계산 로직으로 오류 데이터가 적재되었을 경우, `DESCRIBE HISTORY`로 버전을 확인하고 `VERSION AS OF N-1`으로 이전 상태로 복구하는 절차를 `RUNBOOK.md`에 명시함.
*   **VACUUM 안전장치:** 실제 삭제(`VACUUM ... RETAIN`) 전에 `DRY RUN`으로 삭제 대상 파일 수를 먼저 확인하는 단계를 추가함. 보존 기간 설정을 잘못 잡으면 필요한 파일까지 삭제될 수 있어서, 그런 사고를 미리 걸러내기 위함.
*   **장애 진단 루틴:** 성능 저하 발생 시 Spark UI에서 다음 순서로 원인을 진단: (1) SQL 탭에서 Exchange 노드(Shuffle) 확인, (2) Stages 탭에서 Task Duration 편차 확인, (3) Executors 탭에서 OOM/GC 시간 확인.

### 6. 정직하게 밝히는 한계와 다음 단계

*   DLQ(실패 레코드 격리 테이블)는 아직 구현하지 않음. 현재는 로그 기반 `try/except`로 실패를 흡수하는 수준.
*   RSI 등 추가 기술 지표는 아직 없음. MA50/MA200/Cross 신호까지만 구현됨.
*   자세한 로드맵은 `documentations/design.md` §5 참고.
