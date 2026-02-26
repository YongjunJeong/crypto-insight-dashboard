# Databricks notebook source
# Bronze ingest for Fear & Greed (append-only)
import datetime as dt
import json
import time
from typing import Dict, List, Tuple

import requests
from pyspark.sql import Row
from pyspark.sql.functions import col, to_timestamp
from pyspark.sql.types import StructField, StructType, StringType

# ===== (0) 권장 클러스터 설정 =====
spark.conf.set("spark.databricks.delta.optimizeWrite","true")
spark.conf.set("spark.databricks.delta.autoCompact","true")

# =========================
# (A) 실행 설정
# =========================
MODE = "backfill"                 # once | poll | forever | backfill
LIMIT_ONCE = 2
BACKFILL_LIMIT = 200
API_REFRESH_SECONDS = 24 * 60 * 60
POLL_SECONDS = API_REFRESH_SECONDS
MAX_POLLS = 7

# =========================
# (B) 프로젝트 설정
# =========================
CATALOG = "demo_catalog"
SCHEMA = "demo_schema"
TABLE = f"{CATALOG}.{SCHEMA}.bronze_fear_greed"

BASE_URL = "https://api.alternative.me/fng/"

# =========================
# (C) 테이블 준비
# =========================
spark.sql(f"CREATE CATALOG IF NOT EXISTS {CATALOG}")
spark.sql(f"CREATE SCHEMA  IF NOT EXISTS {CATALOG}.{SCHEMA}")

spark.sql(f"""
CREATE TABLE IF NOT EXISTS {TABLE} (
  source               STRING,
  event_time           TIMESTAMP,
  ingest_time          TIMESTAMP,
  unique_key           STRING,
  raw_json             STRING,
  api_endpoint         STRING,
  api_params_hash      STRING,
  index_value          STRING,
  value_classification STRING,
  time_until_update    STRING,
  dt                   DATE
) USING DELTA
PARTITIONED BY (dt)
""")

# =========================
# (D) 유틸리티
# =========================
def _params_hash(params: Dict) -> str:
    import hashlib
    payload = json.dumps(params, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()

def _fetch_fear_greed(limit: int) -> Tuple[List[Dict], Dict[str, str], Dict[str, str]]:
    params = {"limit": limit, "format": "json"}
    response = requests.get(BASE_URL, params=params, timeout=30)
    response.raise_for_status()
    payload = response.json()
    data = payload.get("data", [])
    return data, response.headers, params

def _rows_to_bronze(rows: List[Dict], endpoint: str, params: Dict[str, str]) -> int:
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
            "source": "alt.fear_greed",
            "event_time": event_time.strftime("%Y-%m-%d %H:%M:%S"),
            "ingest_time": now_s,
            "unique_key": unique_key,
            "raw_json": json.dumps(item, separators=(",", ":")),
            "api_endpoint": endpoint,
            "api_params_hash": param_hash,
            "index_value": item.get("value"),
            "value_classification": item.get("value_classification"),
            "time_until_update": item.get("time_until_update"),
            "dt": event_time.date().isoformat(),
        })

    schema = StructType([
        StructField("source", StringType(), True),
        StructField("event_time", StringType(), True),
        StructField("ingest_time", StringType(), True),
        StructField("unique_key", StringType(), True),
        StructField("raw_json", StringType(), True),
        StructField("api_endpoint", StringType(), True),
        StructField("api_params_hash", StringType(), True),
        StructField("index_value", StringType(), True),
        StructField("value_classification", StringType(), True),
        StructField("time_until_update", StringType(), True),
        StructField("dt", StringType(), True),
    ])

    df = (spark.createDataFrame([Row(**r) for r in records], schema)
            .withColumn("event_time", to_timestamp(col("event_time")))
            .withColumn("ingest_time", to_timestamp(col("ingest_time")))
            .withColumn("dt", col("dt").cast("date"))
            .dropDuplicates(["unique_key"])
            .repartition("dt"))

    count = df.count()
    df.writeTo(TABLE).append()
    return count

def _ingest_once(limit: int) -> int:
    rows, headers, params = _fetch_fear_greed(limit)
    count = _rows_to_bronze(rows, BASE_URL, params)
    remaining = headers.get("X-RateLimit-Remaining")
    if remaining is not None:
        print(f"[FNG] remaining quota: {remaining}")
    print(f"[FNG] +{count} rows (limit={limit})")
    return count

# =========================
# (E) 모드별 동작
# =========================
if MODE == "backfill":
    _ingest_once(BACKFILL_LIMIT)
    dbutils.notebook.exit("fng backfill done")
elif MODE == "poll":
    poll_interval = max(POLL_SECONDS, API_REFRESH_SECONDS)
    for _ in range(MAX_POLLS):
        _ingest_once(LIMIT_ONCE)
        time.sleep(poll_interval)
    dbutils.notebook.exit("fng poll done")
elif MODE == "forever":
    poll_interval = max(POLL_SECONDS, API_REFRESH_SECONDS)
    print(f"[FNG] start polling every {poll_interval}s")
    while True:
        try:
            _ingest_once(LIMIT_ONCE)
        except Exception as exc:
            print(f"[WARN] {exc}")
            time.sleep(5)
        time.sleep(poll_interval)
else:  # once
    _ingest_once(LIMIT_ONCE)
    dbutils.notebook.exit("fng once done")

