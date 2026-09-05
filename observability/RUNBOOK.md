# RUNBOOK: Crypto Insight Dashboard Data Pipeline

**문서 목적:** 본 문서는 Crypto Insight Dashboard 데이터 파이프라인(Bronze → Silver → Gold, 배치 폴링 방식)에서 발생하는 일반적인 오류 및 성능 문제를 진단하고 해결하기 위한 단계별 절차를 제공합니다.
**적용 범위:** Databricks Workflow Jobs, Delta Lake Tables (`bronze_charts`, `silver_charts`, `gold_prices_4h`, `gold_price_positions_4h`).
**최종 목표:** 데이터 파이프라인의 **신뢰성(Reliability)** 과 **가용성(Availability)** 확보.

---

## 시나리오 1: [CRITICAL] Silver 변환 Job 실패 (DQ 어서션 실패 포함)

### 1. 개요 (Incident Summary)

| 항목 | 내용 |
| :--- | :--- |
| **Alert/증상** | `pipeline/silver/transform_charts.ipynb` (또는 `transform_fear_greed.ipynb`)가 **FAILED** 상태로 종료됨. |
| **영향** | Gold 테이블 업데이트 중단. 대시보드 데이터 신선도(Freshness) 저하. |
| **주요 원인** | (a) 데이터 품질(DQ) 어서션 실패 — 오염된 데이터가 Silver에 유입된 경우, (b) 스키마 불일치, (c) Spark OutOfMemory(OOM)/Skew. |

### 2. 진단 및 조사 (Investigation Steps)

| 단계 | 조치 사항 | 예상 결과 및 확인 포인트 |
| :--- | :--- | :--- |
| **2.1 오류 메시지 확인** | 해당 Job의 **Run Details**로 이동하여 **Exception Traceback**을 확인합니다. | `AssertionError: [DQ FAIL] ...` (품질 게이트 위반), `AnalysisException` (스키마 문제), `java.lang.OutOfMemoryError` (메모리 문제) 등 구체적인 오류 유형을 파악합니다. 각 Silver 노트북 마지막 셀에 DQ 체크(null/범위 검증)가 있으므로, 여기서 실패했다면 오염 데이터가 원인입니다. |
| **2.2 DQ 실패인 경우** | 실패한 노트북의 DQ 셀 출력(`[DQ] total_rows`, `null_*`, `nonpositive_*` 등)을 확인합니다. | 어떤 컬럼이 위반됐는지 로그에 그대로 출력됩니다. 원인이 된 Bronze 원본을 조회: `SELECT raw_json FROM demo_catalog.demo_schema.bronze_charts ORDER BY ingest_time DESC LIMIT 10;` |
| **2.3 스키마 불일치 확인** | Bronze 테이블의 최신 `raw_json`을 조회해 API 응답 구조가 바뀌었는지 확인합니다. | API에서 새 필드가 추가/변경되었을 가능성이 높습니다. |
| **2.4 성능 병목 확인** | (OOM/Timeout 문제인 경우) Spark UI의 **Stages 탭**과 **Executors 탭**을 확인합니다. | Task Duration 편차가 심하거나 특정 Task의 Shuffle Read Size가 비정상적으로 크면 데이터 Skew입니다. Executor의 GC Time이 높으면 메모리 부족이 원인입니다. |

### 3. 해결 (Resolution Steps)

| 단계 | 조치 사항 | 근거 및 설명 |
| :--- | :--- | :--- |
| **3.1 DQ 실패 해결** | 원인이 된 Bronze 레코드를 식별해 상류(API/수집기) 문제인지 확인. 일시적 API 이상이면 재수집 후 재실행, 구조적 변경이면 Silver 파싱 로직 수정. | DQ 어서션은 오염 데이터가 Gold로 전파되는 것을 막기 위한 안전장치이므로, 어서션 자체를 완화하기보다 원인 데이터를 고치는 것이 우선입니다. |
| **3.2 스키마 문제 해결** | 해당 Silver 노트북의 `select`/`cast` 구문에서 새로 발견된 컬럼을 처리하도록 스키마를 명시적으로 업데이트합니다. | Bronze는 `raw_json`으로 유연하게 원본을 보존하지만, Silver는 정제 계층이므로 명시적 타입 캐스팅이 필요합니다. |
| **3.3 성능 문제 해결 (Skew)** | 클러스터 Worker 수/메모리를 늘리거나, `spark.sql.adaptive.skewJoin.enabled`(이미 기본 활성화)가 정상 동작하는지 확인합니다. | Skew를 완화하여 작업을 여러 Executor에 균등하게 분산시킵니다. |
| **3.4 재실행** | Databricks Workflow에서 실패한 Job을 **Repair and Restart** 합니다. | 모든 Silver/Gold 노트북은 `MERGE INTO` 기반이라 재실행해도 중복이 발생하지 않습니다(멱등). |

### 4. 검증 및 다음 조치 (Verification & Follow-up)

1.  Job이 **Succeeded** 상태로 종료되었는지 확인합니다.
2.  `SELECT MAX(open_time) FROM demo_catalog.demo_schema.silver_charts;` 로 최신 데이터가 정상 적재되었는지 확인합니다.
3.  **Gold Job이 자동으로 재개**되어 대시보드에 최신 데이터가 반영되는지 확인합니다.

---

## 시나리오 2: [WARNING] 데이터 신선도 저하 (Freshness Lag)

### 1. 개요 (Incident Summary)

| 항목 | 내용 |
| :--- | :--- |
| **Alert/증상** | `gold_prices_4h` 또는 `gold_price_positions_4h`의 최종 `bucket_start`가 예상 폴링 주기보다 크게 지연되어 보임 (SLO 위반). |
| **영향** | 대시보드에 최신 정보가 반영되지 않아 의사 결정에 오류 유발 가능. |
| **주요 원인** | Small Files Problem으로 인한 I/O 병목, Bronze 수집 Job의 API Rate Limit(429) 지속 발생, 클러스터 리소스 부족, `joined_dashboard.ipynb`의 선행 조건(§3 참고) 미충족으로 인한 실행 실패. |

### 2. 진단 및 조사 (Investigation Steps)

| 단계 | 조치 사항 | 예상 결과 및 확인 포인트 |
| :--- | :--- | :--- |
| **2.1 Bronze 수집 확인** | `pipeline/bronze/binance_klines.ipynb` Job 로그에서 **429 에러 발생 비율** 및 `X-MBX-USED-WEIGHT` 로그를 확인합니다. | Bronze 적재 자체가 느려져서 Downstream에 데이터가 적게 공급되고 있는지 확인합니다. |
| **2.2 상태 테이블 확인** | `SELECT * FROM demo_catalog.demo_schema.bronze_ingest_state ORDER BY updated_at DESC;` | 특정 심볼/인터벌의 `last_open_time_ms`가 갱신되지 않고 멈춰 있는지 확인합니다. |
| **2.3 I/O 병목 확인** | `DESCRIBE DETAIL demo_catalog.demo_schema.bronze_charts` | `numFiles` 대비 `sizeInBytes`가 작다면(소파일 다수) I/O 오버헤드가 큽니다. |
| **2.4 joined_dashboard 선행조건 확인** | `joined_dashboard.ipynb`는 `gold_fear_greed`가 없으면 즉시 예외를 던지도록 구현되어 있습니다. Job 로그에서 `[선행 조건 미충족]` 메시지를 확인합니다. | `gold/fear_greed_metrics.ipynb`가 먼저 성공했는지 확인합니다. |

### 3. 해결 (Resolution Steps)

| 단계 | 조치 사항 | 근거 및 설명 |
| :--- | :--- | :--- |
| **3.1 Small Files 문제 해결** | `pipeline/maintenance/delta_optimize_vacuum.ipynb`를 실행하거나 `OPTIMIZE demo_catalog.demo_schema.bronze_charts;`를 직접 실행합니다. | 작은 파일들을 큰 파일로 병합해 메타데이터 처리 비용 및 I/O 비용을 절감합니다. |
| **3.2 Rate Limit 완화** | Bronze 노트북의 `SYMBOLS`/`INTERVALS` 수를 줄이거나 `BACKFILL_DAYS`를 단축합니다. | 요청 수를 줄여 429 발생 빈도를 낮춥니다. 스크립트는 Exponential Backoff로 자동 재시도하지만, 근본적으로 요청량을 줄이는 것이 더 안정적입니다. |
| **3.3 클러스터 리소스 조정** | 클러스터의 Auto Scaling 설정을 확인하고 `min_workers`를 늘리거나 Executor 메모리를 증가시켜 처리량을 높입니다. | 리소스를 늘려 병목 Stage의 처리 속도를 높입니다. |
| **3.4 실행 순서 준수** | `gold/fear_greed_metrics.ipynb` → `gold/joined_dashboard.ipynb` 순서로 재실행합니다. | README §6, `joined_dashboard.ipynb`의 선행 조건 검증과 일치하는 순서입니다. |

### 4. 검증 및 다음 조치 (Verification & Follow-up)

1.  `gold_price_positions_4h`의 `MAX(bucket_start)`가 예상 폴링 주기 이내로 돌아왔는지 확인합니다.
2.  **Follow-up:** `delta_optimize_vacuum.ipynb`가 주 1회 정기 스케줄로 예약되어 있는지 확인합니다.

---

## 3. Job 실행 순서 (Workflow 의존성)

Databricks Workflow Job으로 스케줄링할 경우 아래 순서를 지켜야 합니다 (README.md와 동일):

1.  `pipeline/bronze/binance_klines.ipynb`, `pipeline/bronze/fear_greed_index.py` (병렬 가능)
2.  `pipeline/silver/transform_charts.ipynb`, `transform_fear_greed.ipynb` (각각 대응하는 Bronze 완료 후, 병렬 가능)
3.  `pipeline/gold/price_signals.ipynb`, `fear_greed_metrics.ipynb` (각각 대응하는 Silver 완료 후, 병렬 가능)
4.  `pipeline/gold/joined_dashboard.ipynb` (3번의 `fear_greed_metrics` 완료 후 — 코드에서 선행 테이블 존재 여부를 검증하므로, 순서가 틀리면 조용히 넘어가지 않고 즉시 실패합니다)
5.  `pipeline/maintenance/delta_optimize_vacuum.ipynb` (주 1회, 전체 파이프라인과 별도 스케줄)

---

## 4. 비상 연락망 (Escalation Path)

위의 절차를 2회 시도했음에도 문제가 해결되지 않거나, 원인을 알 수 없는 **서비스 중단(Outage)** 이 발생한 경우, 다음 연락망으로 즉시 상황을 보고하고 지원을 요청합니다.

| 담당자/팀 | 역할 | 연락처 (이메일 / Slack 채널) |
| :--- | :--- | :--- |
| **Yongjun Jeong** | 파이프라인 설계 및 소유자 | `joon7239@gmail.com` |
| **Databricks Support** | Runtime 및 Delta Lake Core 문제 | (엔터프라이즈 지원 포털) |
