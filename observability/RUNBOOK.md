# RUNBOOK: Crypto Insight Dashboard Data Pipeline

**문서 목적:** 본 문서는 Crypto Insight Dashboard 데이터 파이프라인(Bronze → Silver → Gold)에서 발생하는 일반적인 오류 및 성능 문제를 진단하고 해결하기 위한 단계별 절차를 제공합니다.
**적용 범위:** Databricks Workflow Jobs, Delta Lake Tables (`bronze_charts`, `silver_charts`, `gold_signals`).
**최종 목표:** 데이터 파이프라인의 **신뢰성(Reliability)** 과 **가용성(Availability)** 확보.

---

## 시나리오 1: [CRITICAL] Silver Transformation Job 실패

### 1. 개요 (Incident Summary)

| 항목 | 내용 |
| :--- | :--- |
| **Alert/증상** | Databricks Workflow Job **`02_transform_silver_binance`** 이 **FAILED** 상태로 종료됨. |
| **영향** | Gold 테이블 업데이트 중단. 대시보드 데이터 신선도(Freshness) 저하. |
| **주요 원인** | 스키마 불일치 (Schema Drift), 데이터 타입 캐스팅 오류, Spark OutOfMemory (OOM), Skew 발생. |

### 2. 진단 및 조사 (Investigation Steps)

| 단계 | 조치 사항 | 예상 결과 및 확인 포인트 |
| :--- | :--- | :--- |
| **2.1 오류 메시지 확인** | 해당 Job의 **Run Details**로 이동하여 **Exception Traceback**을 확인합니다. | `AnalysisException: Cannot resolve '...'` (스키마 문제) 또는 `java.lang.OutOfMemoryError` (메모리 문제) 등 구체적인 오류 유형을 파악합니다. |
| **2.2 스키마 불일치 확인** | **(스키마 문제인 경우)** 오류를 유발한 Bronze 테이블의 최신 데이터를 쿼리합니다. <br> `SELECT raw_json, _rescued_data FROM demo_catalog.demo_schema.bronze_charts ORDER BY ingest_time DESC LIMIT 10;` | `_rescued_data` 컬럼에 새로 들어온 데이터의 불량 필드가 담겨있는지 확인합니다. API에서 새로운 필드가 추가/변경되었을 가능성이 높습니다. |
| **2.3 성능 병목 확인** | **(OOM/Timeout 문제인 경우)** Spark UI의 **Stages 탭**과 **Executors 탭**을 확인합니다. | **Task Duration 편차**가 심하거나, 특정 Task의 **Shuffle Read Size**가 비정상적으로 크다면 **데이터 Skew**가 발생한 것입니다. Executor의 **GC Time**이 높으면 메모리 부족이 원인입니다. |

### 3. 해결 (Resolution Steps)

| 단계 | 조치 사항 | 근거 및 설명 |
| :--- | :--- | :--- |
| **3.1 스키마 문제 해결** | Silver Transformation 노트북(`02_transform_silver_binance.py`)을 열고, `from_json`이나 `cast` 구문에서 **새로 발견된 컬럼**을 처리하도록 스키마를 명시적으로 업데이트합니다. | Bronze는 `mergeSchema=true`로 유연하게 데이터를 받았지만, Silver는 정제 계층이므로 **명시적 타입 캐스팅**이 필요합니다. |
| **3.2 성능 문제 해결 (Skew)** | 1) 클러스터 설정을 **Worker 수를 늘리거나** 메모리를 증설합니다. <br> 2) 데이터 Skew가 심각한 경우, 해당 Job에 **Skew Join Hint**를 적용하거나, 조인 키에 **Salt Key**를 추가하는 전략을 적용 후 재실행합니다. | Skew를 완화하여 작업을 여러 Executor에 균등하게 분산시킵니다. |
| **3.3 재실행** | Databricks Workflow에서 실패한 Job을 **Repair and Restart** 합니다. | Structured Streaming은 **Checkpoint**를 활용하여 마지막 성공 시점부터 안전하게 처리를 이어갑니다. |

### 4. 검증 및 다음 조치 (Verification & Follow-up)

1.  Job이 **Succeeded** 상태로 종료되었는지 확인합니다.
2.  SQL Editor에서 `SELECT MAX(open_time) FROM demo_catalog.demo_schema.silver_charts;` 쿼리를 실행하여 최신 데이터가 정상적으로 적재되었는지 확인합니다.
3.  **Gold Job이 자동으로 재개**되어 대시보드에 최신 데이터가 반영되는지 확인합니다.

---

## 시나리오 2: [WARNING] 데이터 신선도 저하 (Freshness Lag)

### 1. 개요 (Incident Summary)

| 항목 | 내용 |
| :--- | :--- |
| **Alert/증상** | Gold 테이블의 최종 타임스탬프(`MAX(bucket_start)`)가 현재 시각(UTC)보다 **15분 이상 지연**되어 보임. (SLO 위반) |
| **영향** | 대시보드에 실시간 정보가 반영되지 않아 의사 결정에 오류 유발 가능. |
| **주요 원인** | Small Files Problem으로 인한 I/O 병목, Watermark 설정 부족으로 인한 상태 폭주, 클러스터 리소스 부족. |

### 2. 진단 및 조사 (Investigation Steps)

| 단계 | 조치 사항 | 예상 결과 및 확인 포인트 |
| :--- | :--- | :--- |
| **2.1 스트리밍 지연 확인** | Databricks Monitoring Dashboard에서 **Watermark Delay** 지표를 확인합니다. | Watermark가 설정된 임계치(예: 10분)를 초과하여 증가하는지 확인합니다. |
| **2.2 I/O 병목 확인** | **Bronze 테이블**에서 **Small Files Problem**이 발생하는지 확인합니다. <br> `DESCRIBE DETAIL demo_catalog.demo_schema.bronze_charts` | `numFiles` (파일 개수)는 많으나, `sizeInBytes` 대비 **`avgFileSize`** 가 너무 작은지 (예: 수십 KB) 확인합니다. Small File이 많으면 I/O 오버헤드가 커집니다. |
| **2.3 Upstream API Rate Limit 확인** | **`01a_ingest_bronze_binance`** Job 로그에서 **429 (Too Many Requests) 에러 발생 비율** 및 **사용된 API Weight** 관련 로그를 확인합니다. | Bronze 적재 자체가 느려져서 Downstream에 데이터가 적게 공급되고 있는지 확인합니다. |

### 3. 해결 (Resolution Steps)

| 단계 | 조치 사항 | 근거 및 설명 |
| :--- | :--- | :--- |
| **3.1 Small Files 문제 해결** | Databricks Jobs를 사용하여 **`OPTIMIZE`** 명령을 실행합니다. <br> `OPTIMIZE demo_catalog.demo_schema.bronze_charts;` | OPTIMIZE는 작은 파일들을 큰 파일로 병합하여 **메타데이터 처리 비용 및 I/O 비용**을 절감하여 쿼리/스트리밍 읽기 속도를 개선합니다. |
| **3.2 클러스터 리소스 조정** | 클러스터의 **Auto Scaling 설정**을 확인하고, `min_workers`를 늘리거나, Executor의 **메모리(GB)** 를 증가시켜 처리량을 높입니다. | 리소스를 늘려 병목 Stage의 처리 속도를 높입니다. |
| **3.3 Watermark 조정** | **(극히 예외적인 경우)** Silver/Gold 파이프라인의 Watermark 기간(예: 10분)이 너무 짧다면, 이를 15분 또는 20분으로 늘려 상태(State) 폭발 위험 없이 지연 데이터를 처리하도록 조정합니다. | Watermark를 늘리면 상태 저장 공간이 늘어나 비용이 증가할 수 있으므로 신중하게 적용해야 합니다. |

### 4. 검증 및 다음 조치 (Verification & Follow-up)

1.  Gold 테이블의 `MAX(bucket_start)`가 현재 시각(UTC)에 근접하는지 확인합니다.
2.  Monitoring Dashboard에서 **Watermark Delay** 지표가 정상 범위로 돌아왔는지 확인합니다.
3.  **Follow-up:** OPTIMIZE Job이 정기적으로 예약되어 있는지 확인하고, 필요 시 **ZORDER**를 `symbol` 및 `bucket_start` 컬럼에 적용하도록 권장합니다.

---

## 5. 비상 연락망 (Escalation Path)

위의 절차를 2회 시도했음에도 문제가 해결되지 않거나, 원인을 알 수 없는 **서비스 중단(Outage)** 이 발생한 경우, 다음 연락망으로 즉시 상황을 보고하고 지원을 요청합니다.

| 담당자/팀 | 역할 | 연락처 (이메일 / Slack 채널) |
| :--- | :--- | :--- |
| **Yongjun Jeong (SE)** | 파이프라인 설계 및 소유자 | `aaa@gmail.com` |
| **Data Platform Team** | 인프라 및 클러스터 관리 | `#dataplatform-support` (Slack) |
| **Databricks Support** | Runtime 및 Delta Lake Core 문제 | (엔터프라이즈 지원 포털) |