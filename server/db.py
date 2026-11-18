"""SQLite 저장소 (배치, 목표값 전송 이력, 추천·승인 이력, 배치 요약)"""
import sqlite3

from .config import DATA_DIR

DB_PATH = DATA_DIR / "petwash.db"

SCHEMA = """
CREATE TABLE IF NOT EXISTS setpoints (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    issued_at TEXT NOT NULL,          -- 서버가 보낸 시각 (ISO8601)
    batch_id TEXT,
    target_concentration_pct REAL NOT NULL,
    target_temp_c REAL NOT NULL,
    wash_time_min REAL NOT NULL,
    issued_by TEXT NOT NULL,          -- operator / auto
    result TEXT NOT NULL DEFAULT '전송됨',  -- 전송됨 / 적용됨 / 거부됨
    result_at TEXT,
    result_message TEXT
);
"""


def connect() -> sqlite3.Connection:
    # 호출할 때마다 새 연결을 연다 (MQTT 스레드와 웹 요청 스레드에서 함께 써도 안전)
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def init_db():
    DATA_DIR.mkdir(exist_ok=True)
    with connect() as conn:
        conn.executescript(SCHEMA)


def insert_setpoint(sp: dict) -> int:
    with connect() as conn:
        cur = conn.execute(
            "INSERT INTO setpoints (issued_at, batch_id, target_concentration_pct, target_temp_c,"
            " wash_time_min, issued_by) VALUES (?, ?, ?, ?, ?, ?)",
            (sp["issued_at"], sp["batch_id"], sp["target_concentration_pct"],
             sp["target_temp_c"], sp["wash_time_min"], sp["issued_by"]),
        )
        return cur.lastrowid


def update_setpoint_result(issued_at: str, result: str, at: str, message: str = ""):
    with connect() as conn:
        conn.execute(
            "UPDATE setpoints SET result = ?, result_at = ?, result_message = ? WHERE issued_at = ?",
            (result, at, message, issued_at),
        )


def recent_setpoints(limit: int = 10) -> list[dict]:
    with connect() as conn:
        rows = conn.execute("SELECT * FROM setpoints ORDER BY id DESC LIMIT ?", (limit,)).fetchall()
    return [dict(r) for r in rows]
