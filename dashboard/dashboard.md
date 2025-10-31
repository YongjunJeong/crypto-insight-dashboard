# 1. Crypto Insight Dashboard
![](/Workspace/Users/joon7239@gmail.com/crypto-insight-dashboard/dashboard/dashboard_1.png)
![](/Workspace/Users/joon7239@gmail.com/crypto-insight-dashboard/dashboard/dashboard_2.png)
## 1. 개요 및 대시보드 설계 목표

- 본 대시보드는 **Databricks Lakeview**를 사용하여 **Gold Layer**에 저장된 최종 분석 데이터를 시각화합니다. 

**핵심 설계 목표:**

1.  **Gold 레이어 가치 증명:** 복잡한 MA/RSI 계산을 파이프라인 단계에서 완료하여, BI 사용자가 **ZORDER 최적화**된 Gold 테이블만을 조회하도록 함으로써 쿼리 지연 시간(Latency)을 최소화했습니다.
2.  **이질적 데이터 통합 시연:** **수집 주기가 다른** Binance 가격 데이터(고빈도)와 F&G Index(일별) 데이터를 **시간 윈도우 조인(Time-Window Join)** 을 통해 통합 분석하는 결과를 시연합니다.
3.  **운영 투명성 확보:** 대시보드 내에 **데이터 신선도(Freshness)** 및 **파이프라인 건강 지표**를 포함하여, 운영팀이 데이터 신뢰도를 즉시 확인할 수 있도록 했습니다.

## 2. 대시보드 구성 및 설계 의도

| 구성 요소 | 설계 의도 |
| :--- | :--- |
| **Top KPIs & Header** | **Freshness SLO 준수 확인:** 단순히 최신 가격을 보여주는 것을 넘어, **Binance와 F&G Index의 마지막 업데이트 시각**을 명시합니다. 이는 파이프라인이에 명시된 **신선도(Freshness)** 서비스 수준 목표(SLO)를 유지하고 있음을 보여줍니다. |
| **Price & Technical Indicators** | **성능 최적화 증명:** MA(50/200), RSI 등 복잡한 지표 계산은 **Gold 테이블에서 선계산**되었으므로, 대시보드 쿼리는 단순 `SELECT` 및 `FILTER`만 수행합니다. 이는 대용량 데이터 환경에서도 빠른 응답 속도를 보장하기 위한 결정입니다. |
| **Sentiment & Trend** | **복잡한 데이터 통합 시연:** 일별로 업데이트되는 **F&G Index**를 분/시간 단위의 **가격 시계열**과 **Time-Window Join**으로 통합하여, 시장 심리가 가격 변동에 미치는 영향을 맥락적으로 분석할 수 있게 합니다. |

## 3. 데이터 소스 및 쿼리 전략

모든 시각화 타일은 Gold 레이어의 **`gold_signals`** 테이블 또는 이 테이블을 기반으로 생성된 Databricks SQL 뷰를 조회합니다.

*   **Gold 테이블 구조:** `symbol, bucket_start, avg_price, ma_50, ma_200, cross_signal` 등을 포함합니다.
*   **쿼리 최적화:** Gold 테이블에 **`ZORDER BY (symbol, bucket_start)`** 를 적용하여, 사용자가 종목과 기간을 변경할 때 쿼리 스캔 범위를 최소화합니다.
*   **코드 관리:** 모든 노트북 코드는 **Databricks Repos**를 통해 Git과 연동되며, 실패 시 **RUNBOOK.md**를 통해 누구나 디버깅할 수 있도록 문서화되어 있습니다.