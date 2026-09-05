# 1. Crypto Insight Dashboard
![](/Workspace/Users/joon7239@gmail.com/crypto-insight-dashboard/dashboard/dashboard_1.png)
![](/Workspace/Users/joon7239@gmail.com/crypto-insight-dashboard/dashboard/dashboard_2.png)
## 1. 개요 및 대시보드 설계 목표

- 본 대시보드는 **Databricks Lakeview**를 사용하여 **Gold Layer**에 저장된 최종 분석 데이터를 시각화합니다.

**핵심 설계 목표:**

1.  **Gold 레이어 가치 증명:** MA50/MA200/전환 신호(Golden·Dead Cross) 계산을 파이프라인 단계에서 완료하여, BI 사용자가 **ZORDER 최적화**된 Gold 테이블만을 조회하도록 함으로써 쿼리 지연 시간(Latency)을 최소화했습니다.
2.  **이질적 데이터 통합 시연:** **수집 주기가 다른** Binance 4시간봉 가격 데이터와 F&G Index(일별) 데이터를 **as-of Time-Window Join** 을 통해 하나의 뷰로 통합한 결과를 시연합니다.
3.  **운영 투명성 확보:** 대시보드 내에 **데이터 최신 시각**을 노출하여, 파이프라인이 정상적으로 갱신되고 있는지 즉시 확인할 수 있도록 했습니다.

## 2. 대시보드 구성 및 설계 의도

| 구성 요소 | 설계 의도 |
| :--- | :--- |
| **Top KPIs & Header** | **최신성 확인:** 단순히 최신 가격을 보여주는 것을 넘어, Binance와 F&G Index의 마지막 업데이트 시각(`bucket_start`, FNG 기준일)을 명시해 파이프라인이 예상 폴링 주기 내에 갱신되고 있음을 보여줍니다. |
| **Price & Technical Indicators** | **성능 최적화 증명:** MA(50/200), 전환 신호(cross_signal) 계산은 **Gold 테이블(`gold_prices_4h`)에서 선계산**되었으므로, 대시보드 쿼리는 단순 `SELECT`/`FILTER`만 수행합니다. |
| **Sentiment & Trend** | **복잡한 데이터 통합 시연:** 일별로 업데이트되는 F&G Index를 4시간 단위 가격 시계열과 as-of Time-Window Join으로 통합하여, 시장 심리가 가격 변동에 미치는 영향을 맥락적으로 분석할 수 있게 합니다. |

## 3. 데이터 소스 및 쿼리 전략

모든 시각화 타일은 Gold 레이어의 다음 테이블 또는 이를 기반으로 생성된 Databricks SQL 뷰를 조회합니다.

*   **`gold_price_positions_4h`:** 대시보드 메인 소스. `symbol, bucket_start, close_4h, ma50_4h, ma200_4h, cross_signal, pct_change_24h, fng_value, fng_label` 등을 포함.
*   **`gold_prices_4h`:** 조인 이전의 순수 가격/신호 테이블. `v_signals` 뷰(README §7)의 소스.
*   **쿼리 최적화:** 위 테이블 모두 `ZORDER BY (symbol, bucket_start)`가 적용되어, 사용자가 종목과 기간을 변경할 때 쿼리 스캔 범위를 최소화합니다.
*   **코드 관리:** 모든 노트북 코드는 **Databricks Repos**를 통해 Git과 연동되며, 실패 시 **RUNBOOK.md**를 통해 디버깅할 수 있도록 문서화되어 있습니다.

## 4. 알려진 한계 (스크린샷 갱신 필요)

이 문서와 스크린샷(`dashboard_1.png`, `dashboard_2.png`)은 파이프라인 리팩터링(테이블명 변경: `gold_signals` → `gold_prices_4h`/`gold_price_positions_4h`, 컬럼명 변경: `fgi_*` → `fng_*`, 선물 리더보드 기능 제거) 이전에 캡처된 것입니다. 현재 파이프라인을 워크스페이스에서 재실행한 뒤, Lakeview 대시보드에서 포지션 마커 위젯을 제거하고 새 테이블/컬럼명 기준으로 재구성한 다음 스크린샷을 다시 캡처해야 실제 코드와 화면이 일치합니다.
