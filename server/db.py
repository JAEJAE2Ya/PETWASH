"""SQLite 저장소

배치는 두 종류로 관리한다.
  - 수거 배치 (lots, L로 시작): 수거된 순서대로 만든 단위. 카메라 선별까지만 쓴다.
  - 세척 배치 (wash_batches, W로 시작): 선별 후 오염/비오염 저장함에 모인 병을 묶은 단위.
    세척 조건(레시피), 목표값, 결정 이력, 요약은 모두 세척 배치 기준이다.
어느 수거 배치의 병이 어느 세척 배치로 몇 개 갔는지는 wash_batch_sources에 남는다.
"""
import logging
import sqlite3
from datetime import datetime

from .config import DATA_DIR, STREAMS

log = logging.getLogger("db")

DB_PATH = DATA_DIR / "petwash.db"
SCHEMA_VERSION = 2  # 1: 수거 배치 하나로 세척까지 관리하던 이전 구조

SCHEMA = """
CREATE TABLE IF NOT EXISTS lots (           -- 수거 배치: 선별 전 단위
    lot_id TEXT PRIMARY KEY,                -- 예: L20261119-001
    created_at TEXT NOT NULL,
    started_at TEXT,                        -- 선별 시작
    ended_at TEXT,                          -- 선별 완료
    status TEXT NOT NULL DEFAULT '생성됨',   -- 생성됨 / 선별 중 / 선별 완료
    note TEXT,
    contaminated_n INTEGER NOT NULL DEFAULT 0,  -- 오염 판정 → 오염 저장함으로 간 병 수
    clean_n INTEGER NOT NULL DEFAULT 0          -- 비오염 판정 → 비오염 저장함으로 간 병 수
);

CREATE TABLE IF NOT EXISTS bin_contents (   -- 선별 저장함에 지금 들어 있는 병 (수거 배치별 개수)
    stream TEXT NOT NULL,                   -- contaminated / clean
    lot_id TEXT NOT NULL,
    n INTEGER NOT NULL,
    PRIMARY KEY (stream, lot_id)
);

CREATE TABLE IF NOT EXISTS wash_batches (   -- 세척 배치: 저장함 하나를 통째로 묶은 단위
    batch_id TEXT PRIMARY KEY,              -- 예: W20261119-001
    stream TEXT NOT NULL,                   -- 어느 저장함에서 왔는지: contaminated / clean
    recipe TEXT NOT NULL,                   -- 적용할 레시피 (기본은 stream과 같음, 작업자가 바꿀 수 있음)
    bottle_n INTEGER NOT NULL,
    created_at TEXT NOT NULL,               -- 묶은 시각
    created_by TEXT NOT NULL,               -- auto(임계치 도달) / operator(작업자가 지금 묶기)
    started_at TEXT,                        -- 세척 시작
    ended_at TEXT,                          -- 세척 종료
    status TEXT NOT NULL DEFAULT '대기'      -- 대기 / 세척 중 / 종료
);

CREATE TABLE IF NOT EXISTS wash_batch_sources (  -- 세척 배치 ← 수거 배치 추적 (몇 병씩 왔는지)
    batch_id TEXT NOT NULL,
    lot_id TEXT NOT NULL,
    n INTEGER NOT NULL,
    PRIMARY KEY (batch_id, lot_id)
);

CREATE TABLE IF NOT EXISTS setpoints (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    issued_at TEXT NOT NULL,          -- 서버가 보낸 시각 (ISO8601)
    batch_id TEXT,                    -- 세척 배치
    target_concentration_pct REAL NOT NULL,
    target_temp_c REAL NOT NULL,
    wash_time_min REAL NOT NULL,
    issued_by TEXT NOT NULL,          -- operator / auto
    result TEXT NOT NULL DEFAULT '전송됨',  -- 전송됨 / 적용됨 / 거부됨
    result_at TEXT,
    result_message TEXT
);

CREATE TABLE IF NOT EXISTS decisions (    -- 추천에 대한 작업자 결정 이력
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts TEXT NOT NULL,
    batch_id TEXT,                    -- 세척 배치
    recipe TEXT,                      -- 당시 적용 중이던 레시피 (contaminated / clean)
    recommended TEXT,                 -- 당시 추천 조치 요약 (조치가 필요한 항목만)
    proposed_concentration_pct REAL,  -- 추천 목표값
    proposed_temp_c REAL,
    proposed_wash_time_min REAL,
    decision TEXT NOT NULL,           -- 추천 승인 / 수동 수정 / 세척수 교체 지시 / 레시피 변경
    applied_concentration_pct REAL,   -- 실제로 보낸 목표값
    applied_temp_c REAL,
    applied_wash_time_min REAL
);

CREATE TABLE IF NOT EXISTS batch_summaries (  -- 세척 배치 종료 시 저장하는 요약 (InfluxDB는 긴 기간 조회가 어려우므로)
    batch_id TEXT PRIMARY KEY,        -- 세척 배치
    wash_start TEXT,                  -- 요약 계산 시작 시각 (첫 목표값 적용 시각, 없으면 세척 시작)
    wash_end TEXT,
    samples INTEGER,                  -- 센서 측정 개수
    avg_concentration_pct REAL,
    avg_temp_c REAL,
    avg_turbidity_ntu REAL,
    max_turbidity_ntu REAL,
    pump_on_s REAL,                   -- 약품 투입(펌프 가동) 시간, 초
    heater_on_s REAL,                 -- 히터 가동 시간, 초
    applied_concentration_pct REAL,   -- 마지막으로 장치에 적용된 목표값
    applied_temp_c REAL,
    applied_wash_time_min REAL,
    setpoint_count INTEGER            -- 이 배치에서 적용된 목표값 개수
);
"""


def connect() -> sqlite3.Connection:
    # 호출할 때마다 새 연결을 연다 (MQTT 스레드와 웹 요청 스레드에서 함께 써도 안전)
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def init_db():
    """DB를 만든다. 이전 구조의 DB가 있으면 지우지 않고 백업 파일로 옮긴 뒤 새로 만든다"""
    DATA_DIR.mkdir(exist_ok=True)
    if DB_PATH.exists():
        conn = connect()
        try:
            version = conn.execute("PRAGMA user_version").fetchone()[0]
            tables = conn.execute("SELECT COUNT(*) FROM sqlite_master WHERE type = 'table'").fetchone()[0]
        finally:
            conn.close()
        if tables and version < SCHEMA_VERSION:
            backup = DB_PATH.with_name(f"petwash.v{version or 1}-backup-{datetime.now():%Y%m%d_%H%M%S}.db")
            DB_PATH.rename(backup)
            log.warning("이전 구조(버전 %d)의 DB를 %s 로 옮기고 새로 만듭니다", version or 1, backup.name)
    conn = connect()
    try:
        with conn:
            conn.executescript(SCHEMA)
        conn.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")
    finally:
        conn.close()


def count_with_prefix(table: str, column: str, prefix: str) -> int:
    # table, column은 코드 안의 고정값만 넘긴다 (사용자 입력 아님)
    with connect() as conn:
        return conn.execute(f"SELECT COUNT(*) FROM {table} WHERE {column} LIKE ?", (prefix + "%",)).fetchone()[0]


# ---------------- 목표값 ----------------

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


def applied_setpoints(batch_id: str) -> list[dict]:
    with connect() as conn:
        rows = conn.execute("SELECT * FROM setpoints WHERE batch_id = ? AND result = '적용됨' ORDER BY id",
                            (batch_id,)).fetchall()
    return [dict(r) for r in rows]


# ---------------- 수거 배치 ----------------

def create_lot(lot_id: str, created_at: str, note: str = "") -> dict:
    with connect() as conn:
        conn.execute("INSERT INTO lots (lot_id, created_at, note) VALUES (?, ?, ?)", (lot_id, created_at, note))
    return get_lot(lot_id)


def get_lot(lot_id: str) -> dict | None:
    with connect() as conn:
        row = conn.execute("SELECT * FROM lots WHERE lot_id = ?", (lot_id,)).fetchone()
    return dict(row) if row else None


def update_lot(lot_id: str, **fields):
    cols = ", ".join(f"{k} = ?" for k in fields)
    with connect() as conn:
        conn.execute(f"UPDATE lots SET {cols} WHERE lot_id = ?", (*fields.values(), lot_id))


def list_lots(limit: int = 30) -> list[dict]:
    with connect() as conn:
        rows = conn.execute("SELECT * FROM lots ORDER BY created_at DESC LIMIT ?", (limit,)).fetchall()
    return [dict(r) for r in rows]


def sorting_lot() -> dict | None:
    with connect() as conn:
        row = conn.execute("SELECT * FROM lots WHERE status = '선별 중' LIMIT 1").fetchone()
    return dict(row) if row else None


# ---------------- 선별 저장함 ----------------

def record_bottle(lot_id: str, stream: str):
    """병 1개가 선별 게이트를 지나 저장함에 들어감: 수거 배치 집계 +1, 저장함 +1 (한 트랜잭션)"""
    col = "contaminated_n" if stream == "contaminated" else "clean_n"
    with connect() as conn:
        conn.execute(f"UPDATE lots SET {col} = {col} + 1 WHERE lot_id = ?", (lot_id,))
        conn.execute("INSERT INTO bin_contents (stream, lot_id, n) VALUES (?, ?, 1) "
                     "ON CONFLICT (stream, lot_id) DO UPDATE SET n = n + 1", (stream, lot_id))


def bin_counts() -> dict:
    counts = {s: 0 for s in STREAMS}
    with connect() as conn:
        for r in conn.execute("SELECT stream, SUM(n) AS n FROM bin_contents GROUP BY stream"):
            counts[r["stream"]] = r["n"]
    return counts


def bundle_bin(stream: str, batch_id: str, created_at: str, created_by: str) -> dict | None:
    """저장함의 병을 전부 세척 배치 1개로 묶는다 (출처 수거 배치별 개수도 기록). 비어 있으면 None"""
    with connect() as conn:
        rows = conn.execute("SELECT lot_id, n FROM bin_contents WHERE stream = ?", (stream,)).fetchall()
        total = sum(r["n"] for r in rows)
        if total == 0:
            return None
        conn.execute("INSERT INTO wash_batches (batch_id, stream, recipe, bottle_n, created_at, created_by)"
                     " VALUES (?, ?, ?, ?, ?, ?)", (batch_id, stream, stream, total, created_at, created_by))
        conn.executemany("INSERT INTO wash_batch_sources (batch_id, lot_id, n) VALUES (?, ?, ?)",
                         [(batch_id, r["lot_id"], r["n"]) for r in rows])
        conn.execute("DELETE FROM bin_contents WHERE stream = ?", (stream,))
    return get_wash_batch(batch_id)


# ---------------- 세척 배치 ----------------

# 출처 수거 배치를 "L20261119-001(40), L20261119-002(60)" 형태의 글자로 붙여서 조회
WASH_SELECT = ("SELECT w.*, (SELECT group_concat(src.lot_id || '(' || src.n || ')', ', ')"
               " FROM wash_batch_sources src WHERE src.batch_id = w.batch_id) AS sources FROM wash_batches w ")


def get_wash_batch(batch_id: str) -> dict | None:
    with connect() as conn:
        row = conn.execute(WASH_SELECT + "WHERE w.batch_id = ?", (batch_id,)).fetchone()
    return dict(row) if row else None


def update_wash_batch(batch_id: str, **fields):
    cols = ", ".join(f"{k} = ?" for k in fields)
    with connect() as conn:
        conn.execute(f"UPDATE wash_batches SET {cols} WHERE batch_id = ?", (*fields.values(), batch_id))


def list_wash_batches(limit: int = 50) -> list[dict]:
    """세척 중 → 대기(먼저 묶은 순) → 종료(최근 순)"""
    with connect() as conn:
        rows = conn.execute(
            WASH_SELECT + "ORDER BY CASE w.status WHEN '세척 중' THEN 0 WHEN '대기' THEN 1 ELSE 2 END,"
            " CASE WHEN w.status = '종료' THEN w.ended_at END DESC, w.created_at LIMIT ?", (limit,)).fetchall()
    return [dict(r) for r in rows]


def washing_batch() -> dict | None:
    with connect() as conn:
        row = conn.execute(WASH_SELECT + "WHERE w.status = '세척 중' LIMIT 1").fetchone()
    return dict(row) if row else None


def wash_sources(batch_id: str) -> list[dict]:
    with connect() as conn:
        rows = conn.execute("SELECT lot_id, n FROM wash_batch_sources WHERE batch_id = ? ORDER BY lot_id",
                            (batch_id,)).fetchall()
    return [dict(r) for r in rows]


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


# ---------------- 세척 배치 요약·이력 ----------------

def save_summary(summary: dict):
    cols = ", ".join(summary.keys())
    marks = ", ".join("?" for _ in summary)
    with connect() as conn:
        conn.execute(f"INSERT OR REPLACE INTO batch_summaries ({cols}) VALUES ({marks})", tuple(summary.values()))


def history(limit: int = 200) -> list[dict]:
    """종료된 세척 배치 + 요약 (요약이 없으면 요약 칸은 비어 있음)"""
    with connect() as conn:
        rows = conn.execute(
            "SELECT w.*, s.*, w.batch_id AS batch_id,"
            " (SELECT group_concat(src.lot_id || '(' || src.n || ')', ', ')"
            "  FROM wash_batch_sources src WHERE src.batch_id = w.batch_id) AS sources"
            " FROM wash_batches w LEFT JOIN batch_summaries s ON s.batch_id = w.batch_id"
            " WHERE w.status = '종료' ORDER BY w.ended_at DESC LIMIT ?", (limit,)).fetchall()
    return [dict(r) for r in rows]
