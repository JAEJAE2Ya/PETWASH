"""InfluxDB 3 Core 저장·조회 (시계열: 센서 값, 카메라 판정)"""
import logging
import os
from datetime import datetime, timezone

from influxdb_client_3 import InfluxDBClient3, Point

log = logging.getLogger("influx")

SENSOR_FIELDS = ("concentration_pct", "temp_c", "turbidity_ntu", "level_pct")


class InfluxStore:
    def __init__(self):
        host = os.getenv("INFLUX_HOST", "http://localhost:8181")
        token = os.getenv("INFLUX_TOKEN")
        self.database = os.getenv("INFLUX_DB", "petwash")
        self.client = None
        self.last_error = None
        if not token or token.startswith("여기에"):
            self.last_error = ".env에 INFLUX_TOKEN이 없습니다"
            log.warning(self.last_error + " → InfluxDB 저장을 건너뜁니다")
            return
        self.client = InfluxDBClient3(host=host, token=token, database=self.database)

    def _write(self, point: Point):
        if self.client is None:
            return
        try:
            self.client.write(record=point)
            self.last_error = None
        except Exception as e:  # DB가 꺼져 있어도 서버는 계속 동작
            if self.last_error != str(e):
                log.error("InfluxDB 쓰기 실패: %s", e)
            self.last_error = str(e)

    def write_sensor(self, d: dict):
        p = Point("wash_sensor").time(datetime.fromisoformat(d["ts"]))
        for f in SENSOR_FIELDS:
            p = p.field(f, float(d[f]))
        p = p.field("pump_on", bool(d["pump_on"])).field("heater_on", bool(d["heater_on"]))
        self._write(p)

    def write_camera(self, d: dict):
        p = (
            Point("camera_result")
            .tag("batch_id", str(d["batch_id"]))
            .field("label", d["label"])
            .field("contaminated", d["label"] == "contaminated")
            .field("confidence", float(d["confidence"]))
            .time(datetime.fromisoformat(d["ts"]))
        )
        self._write(p)

    def query(self, sql: str) -> list[dict]:
        """SQL 조회 결과를 dict 목록으로 돌려준다 (실패하면 빈 목록)"""
        if self.client is None:
            return []
        try:
            table = self.client.query(sql, language="sql")
            return table.to_pylist()
        except Exception as e:
            log.warning("InfluxDB 조회 실패: %s", e)
            return []

    def recent_sensor(self, minutes: int = 10) -> list[dict]:
        cols = ", ".join(("time",) + SENSOR_FIELDS + ("pump_on", "heater_on"))
        sql = (
            f"SELECT {cols} FROM wash_sensor "
            f"WHERE time >= now() - INTERVAL '{int(minutes)} minutes' ORDER BY time"
        )
        rows = self.query(sql)
        for r in rows:
            t = r.pop("time")
            if t.tzinfo is None:  # InfluxDB는 UTC 시간을 시간대 정보 없이 돌려준다
                t = t.replace(tzinfo=timezone.utc)
            r["ts"] = t.isoformat()
        return rows
