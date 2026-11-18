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

CREATE TABLE IF NOT EXISTS batches (
    batch_id TEXT PRIMARY KEY,        -- 예: B20261118-001
    created_at TEXT NOT NULL,
    started_at TEXT,
    ended_at TEXT,
    status TEXT NOT NULL DEFAULT '생성됨',  -- 생성됨 / 진행 중 / 종료
    note TEXT,
    contaminated_n INTEGER NOT NULL DEFAULT 0,  -- 카메라 판정: 오염 병 수
    clean_n INTEGER NOT NULL DEFAULT 0,         -- 카메라 판정: 비오염 병 수
    suggested_grade INTEGER,          -- 시스템 제안 등급
    final_grade INTEGER,              -- 작업자가 확정한 등급
    graded_at TEXT
);

CREATE TABLE IF NOT EXISTS decisions (    -- 추천에 대한 작업자 결정 이력
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts TEXT NOT NULL,
    batch_id TEXT,
    grade INTEGER,
    recommended TEXT,                 -- 당시 추천 조치 요약 (조치가 필요한 항목만)
    proposed_concentration_pct REAL,  -- 추천 목표값
    proposed_temp_c REAL,
    proposed_wash_time_min REAL,
    decision TEXT NOT NULL,           -- 추천 승인 / 수동 수정 / 세척수 교체 지시
    applied_concentration_pct REAL,   -- 실제로 보낸 목표값
    applied_temp_c REAL,
    applied_wash_time_min REAL
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


# ---------------- 배치 ----------------

def create_batch(batch_id: str, created_at: str, note: str = "") -> dict:
    with connect() as conn:
        conn.execute("INSERT INTO batches (batch_id, created_at, note) VALUES (?, ?, ?)",
                     (batch_id, created_at, note))
    return get_batch(batch_id)


def get_batch(batch_id: str) -> dict | None:
    with connect() as conn:
        row = conn.execute("SELECT * FROM batches WHERE batch_id = ?", (batch_id,)).fetchone()
    return dict(row) if row else None


def update_batch(batch_id: str, **fields):
    cols = ", ".join(f"{k} = ?" for k in fields)
    with connect() as conn:
        conn.execute(f"UPDATE batches SET {cols} WHERE batch_id = ?", (*fields.values(), batch_id))


def list_batches(limit: int = 30) -> list[dict]:
    with connect() as conn:
        rows = conn.execute("SELECT * FROM batches ORDER BY created_at DESC LIMIT ?", (limit,)).fetchall()
    return [dict(r) for r in rows]


def running_batch() -> dict | None:
    with connect() as conn:
        row = conn.execute("SELECT * FROM batches WHERE status = '진행 중' LIMIT 1").fetchone()
    return dict(row) if row else None


def count_batches_with_prefix(prefix: str) -> int:
    with connect() as conn:
        return conn.execute("SELECT COUNT(*) FROM batches WHERE batch_id LIKE ?", (prefix + "%",)).fetchone()[0]


# ---------------- 추천 결정 이력 ----------------

def insert_decision(d: dict):
    cols = ", ".join(d.keys())
    marks = ", ".join("?" for _ in d)
    with connect() as conn:
        conn.execute(f"INSERT INTO decisions ({cols}) VALUES ({marks})", tuple(d.values()))


def recent_decisions(limit: int = 10, batch_id: str | None = None) -> list[dict]:
    with connect() as conn:
        if batch_id:
            rows = conn.execute("SELECT * FROM decisions WHERE batch_id = ? ORDER BY id DESC LIMIT ?",
                                (batch_id, limit)).fetchall()
        else:
            rows = conn.execute("SELECT * FROM decisions ORDER BY id DESC LIMIT ?", (limit,)).fetchall()
    return [dict(r) for r in rows]
