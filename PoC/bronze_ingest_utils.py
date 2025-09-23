"""Shared helpers for writing Bronze-layer Delta tables."""
from __future__ import annotations

import json
import hashlib
from dataclasses import dataclass
from typing import Dict, Optional

from delta.tables import DeltaTable
from pyspark.sql import DataFrame


def params_hash(params: Dict) -> str:
    """Return a stable hash for request parameters."""
    payload = json.dumps(params, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


@dataclass
class BronzeTableConfig:
    table_name: str
    merge_condition: Optional[str] = None
    update_ingest_time: bool = True


class BronzeTableWriter:
    """Utility to upsert rows into a Bronze Delta table."""

    def __init__(self, spark, config: BronzeTableConfig):
        self.spark = spark
        self.config = config

    @property
    def _merge_condition(self) -> str:
        if self.config.merge_condition:
            return self.config.merge_condition
        return "t.unique_key = s.unique_key"

    def upsert(self, df: DataFrame) -> int:
        """Merge the given dataframe into the configured table."""
        count = df.count()
        if count == 0:
            return 0

        delta_table = DeltaTable.forName(self.spark, self.config.table_name)

        set_map = {col: f"s.{col}" for col in df.columns}
        if not self.config.update_ingest_time and "ingest_time" in df.columns:
            set_map["ingest_time"] = "t.ingest_time"

        (delta_table.alias("t")
            .merge(df.alias("s"), self._merge_condition)
            .whenMatchedUpdate(set=set_map)
            .whenNotMatchedInsertAll()
            .execute())

        return count
