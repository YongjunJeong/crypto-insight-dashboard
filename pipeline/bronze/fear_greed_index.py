# Databricks 노트북: Fear & Greed Index API → Bronze 적재 (일별 Append-Only)
import datetime as dt
import json
import time
from typing import Dict, List, Tuple

import requests
from pyspark.sql import Row
from pyspark.sql.functions import col, to_timestamp
from pyspark.sql.types import StructField, StructType, StringType

# Delta Lake 소파일 자동 병합 및 백그라운드 컴팩션
spark.conf.set("spark.databricks.delta.optimizeWrite","true")
spark.conf.set("spark.databricks.delta.autoCompact","true")

# =========================
# (A) 실행 설정
# =========================
MODE = "backfill"                   # once | poll | forever | backfill
LIMIT_ONCE = 2                      # 1회 수집 최신 데이터 수
BACKFILL_LIMIT = 200                # 백필 시 과거 데이터 수 (API 최대값)
API_REFRESH_SECONDS = 24 * 60 * 60  # FNG API는 하루 1회 업데이트 (00:00 UTC)
POLL_SECONDS = API_REFRESH_SECONDS  # 폴링 간격: API 갱신 주기에 맞춤
MAX_POLLS = 7                       # poll 모드 최대 반복 횟수

# =========================
# (B) 프로젝트 설정
# =========================
CATALOG = "demo_catalog"
SCHEMA = "demo_schema"
TABLE = f"{CATALOG}.{SCHEMA}.bronze_fear_greed"

BASE_URL = "https://api.alternative.me/fng/"

# =========================
# (C) 테이블 준비 (자가 부트스트래핑)
# =========================
spark.sql(f"CREATE CATALOG IF NOT EXISTS {CATALOG}")
spark.sql(f"CREATE SCHEMA  IF NOT EXISTS {CATALOG}.{SCHEMA}")

spark.sql(f"""
CREATE TABLE IF NOT EXISTS {TABLE} (
  source               STRING,      -- 데이터 출처 (alt.fear_greed)
  event_time           TIMESTAMP,   -- 지수 기준 시각 (UTC)
  ingest_time          TIMESTAMP,   -- 적재 시각 (UTC)
  unique_key           STRING,      -- 'fear_greed|<unix_ts>'
  raw_json             STRING,      -- 원본 JSON 문자열 (Audit Trail)
  api_endpoint         STRING,      -- 호출한 API 엔드포인트
  api_params_hash      STRING,      -- 요청 파라미터 SHA256 해시
  index_value          STRING,      -- Fear & Greed 지수 (0~100)
  value_classification STRING,      -- 등급 ("Extreme Fear" ~ "Extreme Greed")
  time_until_update    STRING,      -- 다음 업데이트까지 남은 시간
  dt                   DATE         -- 파티션 컬럼 (Partition Pruning)
) USING DELTA
PARTITIONED BY (dt)
TBLPROPERTIES (
  'delta.logRetentionDuration'         = 'interval 7 days',
  'delta.deletedFileRetentionDuration' = 'interval 7 days'
)
""")

# =========================
# (D) 유틸리티 함수
# =========================
def _params_hash(params: Dict) -> str:
    """요청 파라미터를 정렬 후 SHA256 해시 → 동일 요청 식별용"""
    import hashlib
    payload = json.dumps(params, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()

def _fetch_fear_greed(limit: int) -> Tuple[List[Dict], Dict[str, str], Dict[str, str]]:
    """Fear & Greed Index API 호출. 무인증이지만 Rate Limit 존재 (헤더 모니터링)."""
    params = {"limit": limit, "format": "json"}
    response = requests.get(BASE_URL, params=params, timeout=30)
    response.raise_for_status()
    payload = response.json()
    data = payload.get("data", [])
    return data, response.headers, params

def _rows_to_bronze(rows: List[Dict], endpoint: str, params: Dict[str, str]) -> int:
    """Fear & Greed 데이터를 Bronze 테이블에 Append (unique_key 기준 중복 제거)"""
    if not rows:
        return 0

    now = dt.datetime.now(dt.timezone.utc)
    now_s = now.strftime("%Y-%m-%d %H:%M:%S")
    param_hash = _params_hash(params)
    records = []

    for item in rows:
        ts = int(item["timestamp"])
        event_time = dt.datetime.fromtimestamp(ts, tz=dt.timezone.utc)
        unique_key = f"fear_greed|{ts}"
        records.append({
            "source":               "alt.fear_greed",
            "event_time":           event_time.strftime("%Y-%m-%d %H:%M:%S"),
            "ingest_time":          now_s,
            "unique_key":           unique_key,
            "raw_json":             json.dumps(item, separators=(",", ":")),
            "api_endpoint":         endpoint,
            "api_params_hash":      param_hash,
            "index_value":          item.get("value"),
            "value_classification": item.get("value_classification"),
            "time_until_update":    item.get("time_until_update"),
            "dt":                   event_time.date().isoformat(),
        })

    schema = StructType([
        StructField("source",               StringType(), True),
        StructField("event_time",           StringType(), True),
        StructField("ingest_time",          StringType(), True),
        StructField("unique_key",           StringType(), True),
        StructField("raw_json",             StringType(), True),
        StructField("api_endpoint",         StringType(), True),
        StructField("api_params_hash",      StringType(), True),
        StructField("index_value",          StringType(), True),
        StructField("value_classification", StringType(), True),
        StructField("time_until_update",    StringType(), True),
        StructField("dt",                   StringType(), True),
    ])

    df = (spark.createDataFrame([Row(**r) for r in records], schema)
            .withColumn("event_time",  to_timestamp(col("event_time")))
            .withColumn("ingest_time", to_timestamp(col("ingest_time")))
            .withColumn("dt",          col("dt").cast("date"))
            .dropDuplicates(["unique_key"])
            .repartition("dt"))

    count = df.count()
    df.writeTo(TABLE).append()
    return count

def _ingest_once(limit: int) -> int:
    """1회 수집 → Bronze 적재"""
    rows, headers, params = _fetch_fear_greed(limit)
    count = _rows_to_bronze(rows, BASE_URL, params)
    remaining = headers.get("X-RateLimit-Remaining")
    if remaining is not None:
        print(f"[공포탐욕] API 잔여 할당량: {remaining}")
    print(f"[공포탐욕] +{count}행 적재 (limit={limit})")
    return count

# =========================
# (E) 모드별 동작
# =========================
if MODE == "backfill":
    # 최대 200일 과거 데이터 일괄 수집 (최초 실행 시)
    _ingest_once(BACKFILL_LIMIT)
    dbutils.notebook.exit("공포탐욕 백필 완료")
elif MODE == "poll":
    # API 갱신 주기(24h)에 맞춰 최대 MAX_POLLS회 반복
    poll_interval = max(POLL_SECONDS, API_REFRESH_SECONDS)
    for _ in range(MAX_POLLS):
        _ingest_once(LIMIT_ONCE)
        time.sleep(poll_interval)
    dbutils.notebook.exit("공포탐욕 폴링 완료")
elif MODE == "forever":
    poll_interval = max(POLL_SECONDS, API_REFRESH_SECONDS)
    print(f"[공포탐욕 라이브] {poll_interval}초 간격 무한 폴링 시작")
    while True:
        try:
            _ingest_once(LIMIT_ONCE)
        except Exception as exc:
            print(f"[경고] {exc}")
            time.sleep(5)
        time.sleep(poll_interval)
else:  # once
    _ingest_once(LIMIT_ONCE)
    dbutils.notebook.exit("공포탐욕 1회 수집 완료")

