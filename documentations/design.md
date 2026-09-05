## 1. 프로젝트 개요 및 설계 원칙 (Design Overview and Tenets)

### 1.1 프로젝트 목표

- 본 Crypto Insight Dashboard 파이프라인은 Binance(캔들), Fear & Greed Index와 같은 **다중 외부 API 소스**로부터 데이터를 수집, 정제, 통합하여 **신뢰할 수 있는 투자 신호**를 생성하는 것을 목표로 합니다.
- 모든 데이터 파이프라인과 거버넌스는 Databricks Lakehouse Platform 위에서 Apache Spark, Delta Lake 기술을 사용하여 자동화됩니다.

### 1.2 핵심 설계 원칙 (Core Design Tenets)

*   **데이터 신뢰성 (Data Reliability):** Delta Lake의 ACID 트랜잭션과 MERGE 기반 Upsert를 활용하여 재실행/백필 시에도 데이터 무결성을 보장.
*   **멱등성 우선 (Idempotency First):** 스트리밍 없이도 Exactly-once에 준하는 안전성을 얻기 위해, 모든 계층을 "몇 번을 다시 돌려도 같은 결과"가 나오도록 설계 (상태 테이블 체크포인트 + `unique_key` 기준 MERGE).
*   **운영 안정성 (Observability):** Rate-limit 백오프, 데이터 품질(DQ) 어서션, Delta Time Travel을 통해 장애 시 진단·복구 능력을 확보.
*   **성능 최적화 (Optimization):** Gold 레이어에서 OPTIMIZE 및 ZORDER를 적용하여 BI 쿼리 성능을 극대화.

### 1.3 왜 스트리밍이 아닌 배치 폴링인가 (트레이드오프)

이 프로젝트는 Auto Loader/Structured Streaming이 아니라 **REST API 폴링 배치 + 상태 테이블 체크포인트** 방식을 의도적으로 선택했습니다.

| 고려 사항 | 배치 폴링(현재 구현) | Structured Streaming |
| :--- | :--- | :--- |
| **데이터 소스 특성** | Binance/Fear&Greed 모두 파일 드롭이 아닌 REST 엔드포인트 → Auto Loader의 강점(클라우드 스토리지 파일 이벤트 감지)이 발휘될 지점이 없음 | 클라우드 스토리지에 스트리밍으로 파일이 적재되는 시나리오에 적합 |
| **필요 지연시간(SLA)** | Gold의 최소 단위가 4시간 봉이라 분 단위 실시간성이 비즈니스 요구사항이 아님 | 초/분 단위 신선도가 필요한 경우에 정당화됨 |
| **API Rate Limit** | 폴링 간격을 코드에서 직접 제어해 429를 예방하는 편이 스트리밍 트리거 주기 제어보다 단순 | 마이크로배치 트리거 주기를 Rate Limit에 맞춰 세밀 조정 필요 |
| **운영 복잡도/비용** | 클러스터를 상시 띄워둘 필요 없이 Job 실행 시간만 과금 | 상시 실행 클러스터 또는 서버리스 스트리밍 비용 발생 |
| **재현성** | 상태 테이블(`bronze_ingest_state`)에 마지막 처리 위치를 명시적으로 저장 → 실패 시 그 지점부터 재개 | Checkpoint 디렉터리가 같은 역할을 하지만 스트림 전용 런타임이 필요 |

즉, "쓸 수 있어서" Auto Loader/Streaming을 쓰는 대신, **이 데이터 소스와 SLA 조합에서는 배치 폴링이 더 단순하고 저렴하며 충분히 안전하다**는 판단을 내렸습니다. 향후 분 단위 이하의 신선도가 요구되는 유스케이스가 생기면 Bronze 수집기를 Auto Loader/Structured Streaming으로 교체하는 것을 로드맵으로 남겨둡니다 (§5 참고).

## 2. 레이크하우스 아키텍처 (The Medallion Model)

파이프라인은 Databricks의 표준인 **메달리온 아키텍처** (Bronze → Silver → Gold)를 따릅니다.

### 2.1 Bronze Layer (Raw Zone)

Bronze 레이어는 모든 **원본 데이터의 무결성**과 **추적성(Auditability)** 을 보장하는 역할을 합니다.

| 설계 항목 | 역할 및 상세 원칙 | 적용 기술 (Databricks / Spark) |
| :--- | :--- | :--- |
| **데이터 수집** | REST API를 폴링하여 새로 들어온 데이터만 Delta Lake에 적재합니다 (`MODE`: `backfill`/`once`/`poll`/`forever`). | `requests` 기반 REST 호출 + PySpark `DataFrame.writeTo(...).append()` 또는 `DeltaTable.merge()`. |
| **재개 가능성** | 클러스터/Job이 중단돼도 마지막으로 수집한 위치부터 재개해 중복 수집을 방지합니다. | `bronze_ingest_state` 테이블에 심볼/인터벌별 마지막 `open_time`(ms)을 저장하고, 다음 실행 시 그 지점부터 조회. |
| **원본 보존** | 원본 API 응답 전체를 변환 없이 **`raw_json`** 이라는 단일 STRING 컬럼에 저장합니다. | `event_time`(UTC), `unique_key`, `ingest_time`, `api_params_hash` 등 트레이스 컬럼을 함께 추출·저장. |
| **거버넌스** | API 응답의 `api_endpoint`와 `api_params_hash`(요청 파라미터 SHA256)를 기록해 동일 요청 재현(Replay) 가능성을 확보합니다. | 필요 시 API 키는 **Databricks Secrets**를 통해 관리해 코드에 노출되지 않도록 함. |

### 2.2 Silver Layer (Clean Zone)

Silver 레이어는 Bronze의 원본 데이터를 읽어 **표준화(Standardization)**, **정제(Cleansing)**, 그리고 **중복 제거(De-duplication)** 를 수행하여 분석 가능한 형태로 만듭니다.

| 설계 항목 | 역할 및 상세 원칙 | 적용 기술 (Databricks / Spark) |
| :--- | :--- | :--- |
| **데이터 정제** | `raw_json`을 파싱하여 모든 컬럼을 명시적인 타입(TIMESTAMP, DOUBLE 등)으로 캐스팅합니다. | **UDF 남용 금지:** Catalyst Optimizer의 최적화 이점을 유지하기 위해 UDF 대신 내장 함수(`from_json`, `withColumn`, `when`)를 사용. |
| **중복 제거** | 배치 재실행/백필로 인한 중복 데이터를 `unique_key` 기준으로 제거합니다. | `dropDuplicates(["unique_key"])` 후 `MERGE INTO`. 배치 잡을 몇 번 재실행해도 결과는 동일합니다. |
| **품질 게이트 (DQ)** | MERGE 완료 후 최근 데이터에 대해 null/범위 위반을 검사하고, 위반 시 `assert`로 노트북을 즉시 중단시켜 오염 데이터가 Gold로 전파되는 것을 차단합니다. | `silver/transform_charts.ipynb`, `transform_fear_greed.ipynb` 모두 동일한 DQ 셀 패턴 적용. |
| **Upsert 지원** | Binance처럼 과거 데이터가 재조회 시 달라질 수 있는 경우, 수정을 반영하여 정합성을 유지합니다. | **`MERGE INTO`** 연산으로 `unique_key` 기준 원자적 Upsert. |

### 2.3 Gold Layer (Curated Zone / Data Mart)

Gold 레이어는 비즈니스 분석가와 대시보드 소비자를 위한 최종 **데이터 마트** 역할을 합니다.

| 설계 항목 | 역할 및 상세 원칙 | 적용 기술 (Databricks / Spark) |
| :--- | :--- | :--- |
| **신호 생성** | Silver 데이터를 기반으로 MA50/MA200과 **전환 이벤트**(Golden/Dead Cross)를 계산합니다. | `Window` 함수로 이동평균을 구하고, 직전 바의 추세(`trend_state`)와 비교해 전환이 발생한 바에서만 `cross_signal`을 표시(상태가 아닌 이벤트로 모델링). |
| **이질적 데이터 통합** | 4h 가격 × 일단위 Fear&Greed를 하나의 대시보드 뷰로 결합합니다. | `gold/joined_dashboard.ipynb`가 as-of join(bucket_end 이전 최신 FNG)을 수행. |
| **성능 최적화** | Small Files Problem을 완화하고 메타데이터 I/O 비용을 줄입니다. | `OPTIMIZE` 명령을 정기적으로 수행. |
| **쿼리 가속** | 대시보드에서 자주 필터링되는 컬럼의 검색 속도를 높입니다. | `ZORDER BY (symbol, bucket_start)` 적용. |
| **Idempotency** | 재계산/백필이 발생해도 데이터 중복 없이 정확히 업데이트되도록 합니다. | Gold 테이블의 PK(`symbol` + `bucket_start`)를 기준으로 `MERGE INTO` 수행. |

## 3. 핵심 기술 요소 및 운영 안정성

### 3.1 Time Window Join

Binance 4h 캔들과 Fear & Greed Index(일 단위)처럼 **서로 다른 수집 주기**를 가진 데이터 소스를 안전하게 통합하기 위해, Gold 레이어에서 as-of join(신선도 제한 포함)과 시간 윈도우(`[bucket_start, bucket_end)`)를 조인 키로 사용합니다.

### 3.2 거버넌스 및 디버깅 (Governance & Debugging)

*   **Time Travel:** Delta Lake의 모든 변경 이력이 `_delta_log`에 버전으로 기록되므로, `VERSION AS OF` 또는 `TIMESTAMP AS OF`로 특정 시점의 데이터를 조회하거나 `RESTORE`로 잘못된 적재를 롤백할 수 있습니다.
*   **Git 연동:** 모든 파이프라인 노트북과 문서는 **Databricks Repos**를 통해 Git(GitHub)과 연동되어 버전 관리 및 협업이 용이합니다.

### 3.3 클러스터 및 성능 관리

*   **Autoscaling:** Databricks 클러스터는 Task 대기열이 쌓이면 자동으로 Worker 노드를 추가(Scale Up)하고, 유휴 상태일 때는 Auto-Termination으로 종료되어 비용 효율성을 높입니다.
*   **Spark UI 진단:** 성능 병목(Bottleneck) 발생 시, **SQL 탭**에서 Shuffle 노드(Exchange)를 확인하고, **Stages 탭**에서 Task Duration 편차(Skew)를 확인하여 문제 원인을 신속하게 진단합니다.

## 4. 정직하게 밝히는 한계 (Known Limitations)

포트폴리오로서 과장하지 않기 위해, 현재 구현되지 않은 부분을 명시합니다.

*   **DLQ(Dead Letter Queue) 미구현:** 4xx/파싱 실패 레코드를 별도 격리 테이블에 적재하는 기능은 아직 없습니다. 현재는 `try/except` 로깅으로 실패를 흡수합니다.
*   **선물 리더보드(다른 트레이더 포지션) 기능 제외:** 초기 버전에서 시도했으나, 데이터 소스가 Binance의 비공개 내부 API를 리셀하는 유료 게이트웨이(Apyflux)에 의존했습니다. 2024년 Binance가 해당 엔드포인트를 인증 필요로 전환해 공식적으로 접근 불가능해졌고, 다른 스크래퍼로 교체해도 동일한 리스크(비공식 API 의존, 언제든 차단 가능)가 반복되므로 기능 자체를 스코프에서 제외했습니다.
*   **RSI 등 추가 기술 지표 미구현:** 현재 Gold 레이어는 MA50/MA200/Cross 신호만 계산합니다.
*   **Structured Streaming 미사용:** §1.3에서 설명한 이유로 배치 폴링을 선택했습니다.

## 5. 향후 로드맵 (Roadmap)

*   분 단위 이하 신선도가 필요해지면 Bronze 수집기를 Auto Loader/Structured Streaming으로 교체.
*   4xx/파싱 실패 레코드를 위한 실제 DLQ 테이블 및 재처리 워크플로우 구현.
*   RSI 등 추가 기술 지표 도입.
