"""FastAPI 서버: MQTT 수신 → InfluxDB 저장 → WebSocket으로 브라우저에 실시간 전달

실행 (PET 폴더에서):  .venv\\Scripts\\python -m uvicorn server.main:app --port 8000
"""
import asyncio
import logging
import threading
import time
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from typing import Literal

from fastapi import FastAPI, HTTPException, WebSocket, WebSocketDisconnect
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from . import config, db
from .influx import InfluxStore
from .mqtt_bridge import MqttBridge
from .recommend import DecayTracker, recommend

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s")
log = logging.getLogger("server")


class WsManager:
    """접속한 브라우저(WebSocket) 목록을 관리하고 메시지를 한꺼번에 보낸다"""

    def __init__(self):
        self.clients: set[WebSocket] = set()

    async def broadcast(self, msg: dict):
        for ws in list(self.clients):
            try:
                await ws.send_json(msg)
            except Exception:
                self.clients.discard(ws)


# ---- 서버 전역 상태 (학생 프로젝트이므로 단순하게 메모리에 보관) ----
state = {
    "sensor": None,         # 마지막 센서 값
    "device_status": None,  # 마지막 장치 상태(하트비트)
    "device_online": False,
    "last_setpoint": None,  # 마지막으로 보낸 목표값 + 장치 적용 결과
    "batch": None,          # 진행 중인 배치 (한 번에 1개)
    "recommendation": None, # 현재 추천 조치
}
decay_tracker = DecayTracker()
batch_lock = threading.Lock()  # 카메라 집계(MQTT 스레드)와 웹 요청이 동시에 배치를 바꾸지 않도록
last_heartbeat = 0.0

ws_manager = WsManager()
influx = InfluxStore()
loop: asyncio.AbstractEventLoop | None = None


def push(msg: dict):
    """MQTT 스레드에서 호출: asyncio 루프로 넘겨서 브라우저에 전송"""
    if loop is not None:
        asyncio.run_coroutine_threadsafe(ws_manager.broadcast(msg), loop)


def on_mqtt_message(topic: str, payload: dict):
    global last_heartbeat
    if topic == config.TOPIC_SENSOR:
        state["sensor"] = payload
        influx.write_sensor(payload)
        push({"type": "sensor", "data": payload})
        track_decay(payload)
        update_recommendation()
    elif topic == config.TOPIC_CAMERA:
        influx.write_camera(payload)
        count_camera_result(payload)
    elif topic == config.TOPIC_STATUS:
        last_heartbeat = time.time()
        state["device_status"] = payload
        check_setpoint_result(payload)
        push({"type": "device_status", "data": payload})


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def check_setpoint_result(status: dict):
    """장치 상태 회신을 보고 마지막 목표값이 적용됐는지/거부됐는지 기록"""
    sp = state["last_setpoint"]
    cmd = status.get("last_command")
    if not sp or sp["result"] != "전송됨" or not cmd or cmd.get("issued_at") != sp["issued_at"]:
        return
    sp["result"] = "적용됨" if cmd["accepted"] else "거부됨"
    sp["result_at"] = status.get("ts")
    sp["result_message"] = cmd.get("message", "")
    db.update_setpoint_result(sp["issued_at"], sp["result"], sp["result_at"], sp["result_message"])
    log.info("목표값 %s: %s", sp["result"], sp["result_message"])
    push({"type": "setpoint", "data": sp})


# ---------------- 배치 ----------------

def batch_view(b: dict | None) -> dict | None:
    """DB 배치 행에 화면에 필요한 값(총 개수, 오염 비율, 제안 근거)을 덧붙인다"""
    if b is None:
        return None
    b = dict(b)
    total = b["contaminated_n"] + b["clean_n"]
    b["total_n"] = total
    b["ratio_pct"] = round(b["contaminated_n"] / total * 100, 1) if total else None
    b["suggest_reason"] = None
    if total:
        thresholds = config.load_recipes()["grade_thresholds_pct"]
        b["suggested_grade"], b["suggest_reason"] = config.suggest_grade(b["ratio_pct"], thresholds)
    return b


def set_current_batch(b: dict | None):
    state["batch"] = batch_view(b)
    push({"type": "batch", "data": state["batch"]})
    if b is None:
        decay_tracker.reset()
    update_recommendation()


def count_camera_result(msg: dict):
    """카메라 판정 1건을 진행 중 배치에 집계하고 제안 등급을 다시 계산"""
    with batch_lock:
        b = state["batch"]
        if b is None or msg.get("batch_id") != b["batch_id"]:
            return  # 진행 중 배치가 아니면 집계하지 않음 (InfluxDB에는 저장됨)
        key = "contaminated_n" if msg.get("label") == "contaminated" else "clean_n"
        b[key] += 1
        b = batch_view(b)
        db.update_batch(b["batch_id"], contaminated_n=b["contaminated_n"], clean_n=b["clean_n"],
                        suggested_grade=b["suggested_grade"])
        state["batch"] = b
    push({"type": "batch", "data": b})


# ---------------- 추천 ----------------

def track_decay(sensor: dict):
    """장치가 현재 배치를 세척 중일 때만 농도 감소 속도 계산용 데이터를 모은다"""
    b, st = state["batch"], state["device_status"]
    if b and st and st.get("state") == "세척 중" and st.get("batch_id") == b["batch_id"]:
        decay_tracker.add(b["batch_id"], time.time(), sensor["concentration_pct"], sensor["pump_on"])


def compute_recommendation() -> dict:
    b = state["batch"]
    if b is None:
        return {"available": False, "message": "진행 중인 배치가 없습니다."}
    if b["final_grade"] is None:
        hint = f" (시스템 제안 등급: {b['suggested_grade']})" if b["suggested_grade"] is not None else ""
        return {"available": False, "message": "배치 관리 화면에서 등급을 먼저 확정하세요." + hint}
    rec = recommend(b["final_grade"], config.load_recipes(), state["sensor"], decay_tracker.rate())
    rec["batch_id"] = b["batch_id"]
    return rec


def update_recommendation():
    state["recommendation"] = compute_recommendation()
    push({"type": "recommendation", "data": state["recommendation"]})


def log_decision(decision: str, applied: dict | None):
    """추천에 대한 작업자 결정을 SQLite에 기록"""
    rec = state["recommendation"] or {}
    target = rec.get("target") or {}
    needed = [i["action"] for i in rec.get("items", []) if i["level"] != "ok"]
    db.insert_decision({
        "ts": now_iso(),
        "batch_id": state["batch"]["batch_id"] if state["batch"] else None,
        "grade": rec.get("grade"),
        "recommended": " / ".join(needed) if needed else "유지",
        "proposed_concentration_pct": target.get("target_concentration_pct"),
        "proposed_temp_c": target.get("target_temp_c"),
        "proposed_wash_time_min": target.get("wash_time_min"),
        "decision": decision,
        "applied_concentration_pct": applied and applied["target_concentration_pct"],
        "applied_temp_c": applied and applied["target_temp_c"],
        "applied_wash_time_min": applied and applied["wash_time_min"],
    })
    push({"type": "decision"})


async def watch_heartbeat():
    """하트비트가 15초 이상 없으면 장치 연결 끊김으로 표시 + MQTT/InfluxDB 상태 변화 알림"""
    last_health = None
    while True:
        online = (time.time() - last_heartbeat) < config.HEARTBEAT_TIMEOUT_S
        if online != state["device_online"]:
            state["device_online"] = online
            log.info("장치 연결 상태: %s", "연결됨" if online else "끊김")
            await ws_manager.broadcast({"type": "device_online", "online": online})
        health = (mqtt_bridge.connected, influx.last_error)
        if health != last_health:
            last_health = health
            await ws_manager.broadcast({"type": "health", "mqtt_connected": health[0], "influx_error": health[1]})
        await asyncio.sleep(1)


mqtt_bridge = MqttBridge(on_mqtt_message)


@asynccontextmanager
async def lifespan(app: FastAPI):
    global loop
    loop = asyncio.get_running_loop()
    db.init_db()
    recent = db.recent_setpoints(1)
    state["last_setpoint"] = recent[0] if recent else None
    state["batch"] = batch_view(db.running_batch())  # 서버 재시작 시 진행 중 배치 복원
    state["recommendation"] = compute_recommendation()
    mqtt_bridge.start()
    task = asyncio.create_task(watch_heartbeat())
    yield
    task.cancel()
    mqtt_bridge.stop()


app = FastAPI(title="폐페트병 세척 공정 관리", lifespan=lifespan)


def snapshot() -> dict:
    return {
        **state,
        "mqtt_connected": mqtt_bridge.connected,
        "influx_error": influx.last_error,
        "recipes": config.load_recipes(),
    }


@app.get("/api/state")
def get_state():
    return snapshot()


@app.get("/api/sensor/history")
def sensor_history(minutes: int = 10):
    return influx.recent_sensor(minutes)


class SetpointIn(BaseModel):
    batch_id: str | None = None
    target_concentration_pct: float
    target_temp_c: float
    wash_time_min: float
    issued_by: Literal["operator", "auto"] = "operator"


def send_setpoint(sp_in: SetpointIn) -> dict:
    """안전 한계를 검사한 뒤 MQTT로 목표값을 장치에 보낸다"""
    recipes = config.load_recipes()
    errors = config.check_setpoint(
        sp_in.target_concentration_pct, sp_in.target_temp_c, sp_in.wash_time_min, recipes["safety"]
    )
    if errors:
        raise HTTPException(status_code=400, detail=errors)
    if not mqtt_bridge.connected:
        raise HTTPException(status_code=503, detail=["MQTT 브로커에 연결되지 않아 보낼 수 없습니다"])

    sp = sp_in.model_dump()
    # 배치를 지정하지 않으면 진행 중 배치로 보낸다 (없으면 "수동")
    sp["batch_id"] = sp["batch_id"] or (state["batch"]["batch_id"] if state["batch"] else "수동")
    sp["issued_at"] = now_iso()  # 장치가 회신할 때 이 값으로 어떤 명령인지 구분
    mqtt_bridge.publish(config.TOPIC_SETPOINT, sp)
    db.insert_setpoint(sp)
    state["last_setpoint"] = {**sp, "result": "전송됨", "result_at": None, "result_message": ""}
    push({"type": "setpoint", "data": state["last_setpoint"]})
    return state["last_setpoint"]


@app.post("/api/setpoint")
def post_setpoint(sp: SetpointIn):
    """작업자가 슬라이더로 값을 직접 정해서 보냄 (추천이 있으면 '수동 수정'으로 기록)"""
    result = send_setpoint(sp)
    if state["recommendation"] and state["recommendation"].get("available"):
        log_decision("수동 수정", result)
    return result


@app.get("/api/recommendation")
def get_recommendation():
    return compute_recommendation()


@app.post("/api/recommendation/approve")
def approve_recommendation():
    """추천 목표값(등급 레시피)을 승인해서 장치로 보낸다"""
    rec = compute_recommendation()
    if not rec.get("available"):
        raise HTTPException(status_code=409, detail=[rec.get("message", "추천이 없습니다")])
    state["recommendation"] = rec
    result = send_setpoint(SetpointIn(batch_id=rec["batch_id"], issued_by="operator", **rec["target"]))
    log_decision("추천 승인", result)
    return result


@app.post("/api/device/replace_water")
def replace_water():
    """세척수 교체 지시 (시뮬레이터는 농도·탁도·온도를 새 물 상태로 되돌림)"""
    require_mqtt()
    mqtt_bridge.publish(config.TOPIC_BATCH, {
        "batch_id": state["batch"]["batch_id"] if state["batch"] else None,
        "action": "replace_water", "ts": now_iso(),
    })
    decay_tracker.reset()  # 새 물이므로 감소 속도도 다시 계산
    log_decision("세척수 교체 지시", None)
    return {"ok": True}


@app.get("/api/decisions")
def get_decisions(limit: int = 10, batch_id: str | None = None):
    return db.recent_decisions(limit, batch_id)


@app.get("/api/setpoints")
def get_setpoints(limit: int = 10):
    return db.recent_setpoints(limit)


class BatchIn(BaseModel):
    note: str = ""


class GradeIn(BaseModel):
    grade: int


def require_mqtt():
    if not mqtt_bridge.connected:
        raise HTTPException(status_code=503, detail=["MQTT 브로커에 연결되지 않았습니다"])


def get_batch_or_404(batch_id: str) -> dict:
    b = db.get_batch(batch_id)
    if b is None:
        raise HTTPException(status_code=404, detail=["배치를 찾을 수 없습니다"])
    return b


@app.get("/api/batches")
def get_batches(limit: int = 30):
    return [batch_view(b) for b in db.list_batches(limit)]


@app.post("/api/batches")
def create_batch(body: BatchIn):
    # 배치 번호: B + 오늘 날짜 + 일련번호 (예: B20261118-001)
    prefix = "B" + datetime.now().strftime("%Y%m%d") + "-"
    batch_id = f"{prefix}{db.count_batches_with_prefix(prefix) + 1:03d}"
    b = db.create_batch(batch_id, now_iso(), body.note)
    push({"type": "batch", "data": state["batch"]})  # 목록 갱신 알림
    return batch_view(b)


@app.post("/api/batches/{batch_id}/start")
def start_batch(batch_id: str):
    require_mqtt()
    with batch_lock:
        b = get_batch_or_404(batch_id)
        if b["status"] != "생성됨":
            raise HTTPException(status_code=409, detail=[f"'{b['status']}' 상태의 배치는 시작할 수 없습니다"])
        if state["batch"] is not None:
            raise HTTPException(status_code=409, detail=[f"진행 중인 배치({state['batch']['batch_id']})를 먼저 종료하세요"])
        db.update_batch(batch_id, status="진행 중", started_at=now_iso())
        set_current_batch(db.get_batch(batch_id))
    # 장치에 배치 시작 알림 → 카메라 판정 시작
    mqtt_bridge.publish(config.TOPIC_BATCH, {"batch_id": batch_id, "action": "start", "ts": now_iso()})
    log.info("배치 시작: %s", batch_id)
    return state["batch"]


@app.post("/api/batches/{batch_id}/grade")
def set_grade(batch_id: str, body: GradeIn):
    if body.grade not in config.load_recipes()["grades"]:
        raise HTTPException(status_code=400, detail=["등급은 0~3 중 하나여야 합니다"])
    with batch_lock:
        b = get_batch_or_404(batch_id)
        if b["status"] != "진행 중":
            raise HTTPException(status_code=409, detail=["진행 중인 배치만 등급을 입력할 수 있습니다"])
        db.update_batch(batch_id, final_grade=body.grade, graded_at=now_iso())
        set_current_batch(db.get_batch(batch_id))
    log.info("배치 %s 등급 확정: %d", batch_id, body.grade)
    return state["batch"]


@app.post("/api/batches/{batch_id}/end")
def end_batch(batch_id: str):
    with batch_lock:
        b = get_batch_or_404(batch_id)
        if b["status"] != "진행 중":
            raise HTTPException(status_code=409, detail=["진행 중인 배치만 종료할 수 있습니다"])
        db.update_batch(batch_id, status="종료", ended_at=now_iso())
        set_current_batch(None)
    # 장치에 배치 종료 알림 → 펌프·히터 정지 (MQTT가 끊겨 있으면 알림만 생략)
    if mqtt_bridge.connected:
        mqtt_bridge.publish(config.TOPIC_BATCH, {"batch_id": batch_id, "action": "stop", "ts": now_iso()})
    log.info("배치 종료: %s", batch_id)
    return batch_view(db.get_batch(batch_id))


@app.websocket("/ws")
async def websocket_endpoint(ws: WebSocket):
    await ws.accept()
    ws_manager.clients.add(ws)
    await ws.send_json({"type": "snapshot", "data": snapshot()})
    try:
        while True:
            await ws.receive_text()  # 브라우저 → 서버 메시지는 쓰지 않음 (연결 유지용)
    except WebSocketDisconnect:
        ws_manager.clients.discard(ws)


# 정적 웹 파일은 API 경로 뒤에 마운트해야 /api, /ws가 가려지지 않는다
app.mount("/", StaticFiles(directory=config.WEB_DIR, html=True), name="web")
