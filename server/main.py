"""FastAPI 서버: MQTT 수신 → InfluxDB 저장 → WebSocket으로 브라우저에 실시간 전달

배치는 두 단계로 관리한다.
  수거 배치(L…) ─카메라 선별─▶ 오염/비오염 저장함 ─임계치 도달─▶ 세척 배치(W…) ─세척─▶ 이력
  - 수거 배치: 수거된 순서대로 만든 단위. 선별까지만 쓴다.
  - 세척 배치: 저장함에 모인 병을 묶은 단위. 세척 조건(레시피)·목표값·요약은 이 단위로 관리한다.
선별(카메라)과 세척(세척조)은 다른 설비이므로 동시에 진행할 수 있다.

실행 (PET 폴더에서):  .venv\\Scripts\\python -m uvicorn server.main:app --port 8000
"""
import asyncio
import csv
import io
import logging
import threading
import time
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from typing import Literal

from fastapi import FastAPI, HTTPException, WebSocket, WebSocketDisconnect
from fastapi.responses import Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from . import config, db
from .influx import InfluxStore
from .mqtt_bridge import MqttBridge
from .recommend import DecayTracker, recommend

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s")
log = logging.getLogger("server")

Stream = Literal["contaminated", "clean"]


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
    "lot": None,            # 선별 중인 수거 배치 (카메라가 1대이므로 한 번에 1개)
    "bins": {s: 0 for s in config.STREAMS},  # 선별 저장함에 지금 모여 있는 병 수
    "wash": None,           # 세척 중인 세척 배치 (세척조가 1개이므로 한 번에 1개)
    "recommendation": None, # 현재 추천 조치
}
decay_tracker = DecayTracker()
batch_lock = threading.Lock()  # 카메라 집계(MQTT 스레드)와 웹 요청이 동시에 배치·저장함을 바꾸지 않도록
last_heartbeat = 0.0

# 장치 시간 시계: 시연용 가속(배속) 중에는 실제 시간보다 빨리 흐른다.
# 농도 감소 속도(%p/분)를 "장치 시간" 기준으로 계산해야 배속과 상관없이 같은 판단이 나온다.
device_clock = 0.0
last_sensor_real = None

ws_manager = WsManager()
influx = InfluxStore()
loop: asyncio.AbstractEventLoop | None = None


def push(msg: dict):
    """MQTT 스레드에서 호출: asyncio 루프로 넘겨서 브라우저에 전송"""
    if loop is not None:
        asyncio.run_coroutine_threadsafe(ws_manager.broadcast(msg), loop)


def on_mqtt_message(topic: str, payload: dict):
    global last_heartbeat, device_clock, last_sensor_real
    if topic == config.TOPIC_SENSOR:
        now = time.time()
        if last_sensor_real is not None:
            device_clock += (now - last_sensor_real) * payload.get("time_scale", 1)
        last_sensor_real = now
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
        check_lot_done(payload)
        push({"type": "device_status", "data": payload})
        update_recommendation()  # 장치 적용 여부가 바뀌면 추천 화면도 바로 갱신


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def new_id(letter: str, table: str, column: str) -> str:
    """배치 번호: 종류 글자 + 오늘 날짜 + 일련번호 (수거 배치 L20261119-001, 세척 배치 W20261119-001)"""
    prefix = letter + datetime.now().strftime("%Y%m%d") + "-"
    return f"{prefix}{db.count_with_prefix(table, column, prefix) + 1:03d}"


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


# ---------------- 수거 배치 (선별) ----------------

def lot_view(lot: dict | None) -> dict | None:
    """DB 수거 배치 행에 화면에 필요한 값(판정 합계, 오염 비율)을 덧붙인다"""
    if lot is None:
        return None
    lot = dict(lot)
    total = lot["contaminated_n"] + lot["clean_n"]
    lot["total_n"] = total
    lot["ratio_pct"] = round(lot["contaminated_n"] / total * 100, 1) if total else None
    return lot


def count_camera_result(msg: dict):
    """카메라 판정 1건 = 병 1개가 선별 게이트를 지나 저장함으로 들어감.
    수거 배치 집계와 저장함에 더하고, 저장함이 임계치에 도달하면 세척 배치로 묶는다."""
    stream = "contaminated" if msg.get("label") == "contaminated" else "clean"
    lot_id = msg.get("batch_id")  # 카메라 토픽의 batch_id = 수거 배치 번호
    with batch_lock:
        lot = state["lot"]
        current = lot is not None and lot["lot_id"] == lot_id
        if not current and (not lot_id or db.get_lot(lot_id) is None):
            return  # 모르는 수거 배치는 집계하지 않음 (InfluxDB에는 저장됨)
        db.record_bottle(lot_id, stream)
        state["bins"][stream] += 1
        if current:
            lot[f"{stream}_n"] += 1
            state["lot"] = lot = lot_view(lot)
        made = check_bins()
    if current:
        push({"type": "lot", "data": lot})
    push({"type": "bins", "data": bins_payload()})
    if made:
        push({"type": "wash_batches"})


def finish_lot(lot_id: str, reason: str) -> bool:
    """수거 배치를 선별 완료로 바꾼다"""
    with batch_lock:
        lot = db.get_lot(lot_id)
        if lot is None or lot["status"] != "선별 중":
            return False
        db.update_lot(lot_id, status="선별 완료", ended_at=now_iso())
        if state["lot"] and state["lot"]["lot_id"] == lot_id:
            state["lot"] = None
    push({"type": "lot", "data": None})
    push({"type": "lots"})
    log.info("수거 배치 선별 완료: %s (%s)", lot_id, reason)
    return True


def check_lot_done(status: dict):
    """장치가 수거 배치의 병을 모두 판정했다고 알리면(남은 병 0) 선별 완료로 바꾼다"""
    lot = state["lot"]
    if (lot and lot["total_n"] > 0 and status.get("lot_id") == lot["lot_id"]
            and status.get("lot_remaining") == 0):
        finish_lot(lot["lot_id"], "장치 판정 완료")


# ---------------- 선별 저장함 ----------------

def bins_payload() -> dict:
    """저장함별 병 수와 임계치 (화면 표시용)"""
    thresholds = config.load_recipes()["bin_threshold"]
    return {s: {"n": state["bins"][s], "threshold": thresholds[s]} for s in config.STREAMS}


def bundle(stream: str, created_by: str) -> dict | None:
    """저장함의 병을 전부 세척 배치 1개로 묶는다 (호출하는 쪽에서 batch_lock을 잡고 있어야 함)"""
    batch_id = new_id("W", "wash_batches", "batch_id")
    b = db.bundle_bin(stream, batch_id, now_iso(), created_by)
    if b is None:
        return None
    state["bins"][stream] = 0
    # 장치: 찬 저장함을 이 세척 배치로 표시해 빼 두고 빈 저장함으로 바꾼다
    if mqtt_bridge.connected:
        mqtt_bridge.publish(config.TOPIC_BATCH, {
            "batch_id": batch_id, "action": "bundle", "stream": stream, "count": b["bottle_n"], "ts": now_iso(),
        })
    log.info("세척 배치 생성: %s (%s 저장함 %d병, %s)", batch_id, config.STREAM_NAMES[stream], b["bottle_n"],
             "임계치 도달" if created_by == "auto" else "작업자 지금 묶기")
    return b


def check_bins() -> list[dict]:
    """임계치에 도달한 저장함을 세척 배치로 묶는다 (호출하는 쪽에서 batch_lock을 잡고 있어야 함)"""
    thresholds = config.load_recipes()["bin_threshold"]
    made = []
    for stream in config.STREAMS:
        if state["bins"][stream] >= thresholds[stream]:
            b = bundle(stream, "auto")
            if b:
                made.append(b)
    return made


# ---------------- 세척 배치 ----------------

def wash_view(b: dict | None) -> dict | None:
    """DB 세척 배치 행에 출처 수거 배치 목록을 붙인다"""
    if b is None:
        return None
    b = dict(b)
    b["sources"] = db.wash_sources(b["batch_id"])
    return b


def set_current_wash(b: dict | None):
    state["wash"] = wash_view(b)
    push({"type": "wash", "data": state["wash"]})
    if b is None:
        decay_tracker.reset()
    update_recommendation()


# ---------------- 추천 ----------------

def track_decay(sensor: dict):
    """장치가 현재 세척 배치를 세척 중일 때만 농도 감소 속도 계산용 데이터를 모은다"""
    w, st = state["wash"], state["device_status"]
    if w and st and st.get("state") == "세척 중" and st.get("batch_id") == w["batch_id"]:
        decay_tracker.add(w["batch_id"], device_clock, sensor["concentration_pct"], sensor["pump_on"])


def compute_recommendation() -> dict:
    w = state["wash"]
    if w is None:
        return {"available": False, "message": "세척 중인 배치가 없습니다. 배치 관리에서 대기 중인 세척 배치를 시작하세요."}
    st = state["device_status"] or {}
    if st.get("state") == "세척 완료" and st.get("batch_id") == w["batch_id"]:
        return {"available": False, "message": "설정한 세척 시간이 끝났습니다. 배치 관리에서 세척 배치를 종료하세요."}
    recipes = config.load_recipes()
    r = recipes["recipes"][w["recipe"]]
    # 장치가 이 세척 배치에 이 레시피 목표값을 이미 적용해서 세척 중인지 확인
    ap = st.get("applied") or {}
    applied = (
        state["device_online"] and st.get("state") == "세척 중" and ap.get("batch_id") == w["batch_id"]
        and float(ap.get("target_concentration_pct", -1)) == r["concentration_pct"]
        and float(ap.get("target_temp_c", -1)) == r["temp_c"]
        and float(ap.get("wash_time_min", -1)) == r["wash_time_min"]
    )
    rec = recommend(w["recipe"], recipes, state["sensor"], decay_tracker.rate(), applied)
    rec["batch_id"] = w["batch_id"]
    rec["stream"] = w["stream"]
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
        "batch_id": state["wash"]["batch_id"] if state["wash"] else None,
        "recipe": rec.get("recipe"),
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
    # 서버 재시작 시 선별 중 수거 배치, 저장함, 세척 중 배치 복원
    state["lot"] = lot_view(db.sorting_lot())
    state["bins"] = db.bin_counts()
    state["wash"] = wash_view(db.washing_batch())
    state["recommendation"] = compute_recommendation()
    mqtt_bridge.start()
    task = asyncio.create_task(watch_heartbeat())
    yield
    task.cancel()
    mqtt_bridge.stop()


app = FastAPI(title="PETWASH", lifespan=lifespan)


def snapshot() -> dict:
    return {
        **state,
        "bins": bins_payload(),
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


def require_mqtt():
    if not mqtt_bridge.connected:
        raise HTTPException(status_code=503, detail=["MQTT 브로커에 연결되지 않았습니다"])


# ---------------- 목표값·추천 ----------------

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
    # 배치를 지정하지 않으면 세척 중인 세척 배치로 보낸다 (없으면 "수동")
    sp["batch_id"] = sp["batch_id"] or (state["wash"]["batch_id"] if state["wash"] else "수동")
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
    """추천 목표값(세척 배치 레시피)을 승인해서 장치로 보낸다"""
    rec = compute_recommendation()
    if not rec.get("available"):
        raise HTTPException(status_code=409, detail=[rec.get("message", "추천이 없습니다")])
    state["recommendation"] = rec
    result = send_setpoint(SetpointIn(batch_id=rec["batch_id"], issued_by="operator", **rec["target"]))
    log_decision("추천 승인", result)
    return result


class SpeedIn(BaseModel):
    speed: Literal[1, 10]


@app.post("/api/device/speed")
def set_speed(body: SpeedIn):
    """(테스트) 시연용 가속: 시뮬레이터 안의 시간을 10배 빠르게 / 실제 시간으로"""
    require_mqtt()
    mqtt_bridge.publish(config.TOPIC_SIM, {"speed": body.speed, "ts": now_iso()})
    log.info("시연용 배속 요청: x%d", body.speed)
    return {"ok": True, "speed": body.speed}


@app.post("/api/device/replace_water")
def replace_water():
    """세척수 교체 지시 (시뮬레이터는 농도·탁도·온도를 새 물 상태로 되돌림)"""
    require_mqtt()
    mqtt_bridge.publish(config.TOPIC_BATCH, {
        "batch_id": state["wash"]["batch_id"] if state["wash"] else None,
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


# ---------------- 수거 배치 API ----------------

class LotIn(BaseModel):
    note: str = ""


def get_lot_or_404(lot_id: str) -> dict:
    lot = db.get_lot(lot_id)
    if lot is None:
        raise HTTPException(status_code=404, detail=["수거 배치를 찾을 수 없습니다"])
    return lot


@app.get("/api/lots")
def get_lots(limit: int = 30):
    return [lot_view(lot) for lot in db.list_lots(limit)]


@app.post("/api/lots")
def create_lot(body: LotIn):
    lot_id = new_id("L", "lots", "lot_id")
    lot = db.create_lot(lot_id, now_iso(), body.note)
    push({"type": "lots"})
    return lot_view(lot)


@app.post("/api/lots/{lot_id}/start")
def start_lot(lot_id: str):
    """수거 배치 선별 시작: 장치가 카메라 판정을 시작하고 병을 저장함으로 보낸다"""
    require_mqtt()
    with batch_lock:
        lot = get_lot_or_404(lot_id)
        if lot["status"] != "생성됨":
            raise HTTPException(status_code=409, detail=[f"'{lot['status']}' 상태의 수거 배치는 시작할 수 없습니다"])
        if state["lot"] is not None:
            raise HTTPException(status_code=409, detail=[f"선별 중인 수거 배치({state['lot']['lot_id']})를 먼저 끝내세요"])
        db.update_lot(lot_id, status="선별 중", started_at=now_iso())
        state["lot"] = lot_view(db.get_lot(lot_id))
    mqtt_bridge.publish(config.TOPIC_BATCH, {"batch_id": lot_id, "action": "sort_start", "ts": now_iso()})
    push({"type": "lot", "data": state["lot"]})
    push({"type": "lots"})
    log.info("수거 배치 선별 시작: %s", lot_id)
    return state["lot"]


@app.post("/api/lots/{lot_id}/end")
def end_lot(lot_id: str):
    """선별 수동 종료 (병이 다 떨어졌거나 중간에 멈춰야 할 때)"""
    lot = get_lot_or_404(lot_id)
    if lot["status"] != "선별 중":
        raise HTTPException(status_code=409, detail=["선별 중인 수거 배치만 종료할 수 있습니다"])
    if mqtt_bridge.connected:
        mqtt_bridge.publish(config.TOPIC_BATCH, {"batch_id": lot_id, "action": "sort_stop", "ts": now_iso()})
    finish_lot(lot_id, "작업자 종료")
    return lot_view(db.get_lot(lot_id))


# ---------------- 저장함·세척 배치 API ----------------

class RecipeIn(BaseModel):
    recipe: Stream


def get_wash_or_404(batch_id: str) -> dict:
    b = db.get_wash_batch(batch_id)
    if b is None:
        raise HTTPException(status_code=404, detail=["세척 배치를 찾을 수 없습니다"])
    return b


@app.post("/api/bins/{stream}/bundle")
def bundle_now(stream: Stream):
    """임계치에 못 미쳐도 지금 저장함의 병을 세척 배치로 묶는다 (작업 마감 때 남은 병 등)"""
    with batch_lock:
        b = bundle(stream, "operator")
    if b is None:
        raise HTTPException(status_code=409, detail=[f"{config.STREAM_NAMES[stream]} 저장함이 비어 있습니다"])
    push({"type": "bins", "data": bins_payload()})
    push({"type": "wash_batches"})
    return b


@app.get("/api/wash_batches")
def get_wash_batches(limit: int = 50):
    return db.list_wash_batches(limit)


@app.post("/api/wash_batches/{batch_id}/start")
def start_wash(batch_id: str):
    """세척 배치를 세척조에 넣고 세척을 시작한다 (목표값은 추천 승인으로 따로 보냄)"""
    require_mqtt()
    with batch_lock:
        b = get_wash_or_404(batch_id)
        if b["status"] != "대기":
            raise HTTPException(status_code=409, detail=[f"'{b['status']}' 상태의 세척 배치는 시작할 수 없습니다"])
        if state["wash"] is not None:
            raise HTTPException(status_code=409, detail=[f"세척 중인 배치({state['wash']['batch_id']})를 먼저 종료하세요"])
        db.update_wash_batch(batch_id, status="세척 중", started_at=now_iso())
        set_current_wash(db.get_wash_batch(batch_id))
    mqtt_bridge.publish(config.TOPIC_BATCH, {
        "batch_id": batch_id, "action": "wash_start", "stream": b["stream"], "count": b["bottle_n"], "ts": now_iso(),
    })
    push({"type": "wash_batches"})
    log.info("세척 배치 시작: %s", batch_id)
    return state["wash"]


@app.post("/api/wash_batches/{batch_id}/recipe")
def set_wash_recipe(batch_id: str, body: RecipeIn):
    """세척 배치에 적용할 레시피를 바꾼다 (예: 비오염 배치인데 약품 소모가 빨라 선별 오류가 의심될 때)"""
    with batch_lock:
        b = get_wash_or_404(batch_id)
        if b["status"] == "종료":
            raise HTTPException(status_code=409, detail=["종료된 세척 배치는 레시피를 바꿀 수 없습니다"])
        db.update_wash_batch(batch_id, recipe=body.recipe)
        if state["wash"] and state["wash"]["batch_id"] == batch_id:
            log_decision(f"레시피 변경 → {config.STREAM_NAMES[body.recipe]}", None)
            set_current_wash(db.get_wash_batch(batch_id))
    push({"type": "wash_batches"})
    log.info("세척 배치 %s 레시피: %s", batch_id, body.recipe)
    return wash_view(db.get_wash_batch(batch_id))


@app.post("/api/wash_batches/{batch_id}/end")
def end_wash(batch_id: str):
    with batch_lock:
        b = get_wash_or_404(batch_id)
        if b["status"] != "세척 중":
            raise HTTPException(status_code=409, detail=["세척 중인 배치만 종료할 수 있습니다"])
        db.update_wash_batch(batch_id, status="종료", ended_at=now_iso())
        set_current_wash(None)
    # 장치에 세척 종료 알림 → 펌프·히터 정지 (MQTT가 끊겨 있으면 알림만 생략)
    if mqtt_bridge.connected:
        mqtt_bridge.publish(config.TOPIC_BATCH, {"batch_id": batch_id, "action": "stop", "ts": now_iso()})
    log.info("세척 배치 종료: %s", batch_id)
    summarize_batch(batch_id)
    push({"type": "wash_batches"})
    return db.get_wash_batch(batch_id)


def summarize_batch(batch_id: str) -> dict:
    """세척 배치 요약을 계산해서 SQLite에 저장 (세척 구간 = 첫 목표값 적용 ~ 세척 종료)"""
    b = db.get_wash_batch(batch_id)
    applied = db.applied_setpoints(batch_id)
    start = applied[0]["issued_at"] if applied else b["started_at"]
    end = b["ended_at"]
    summary = {"batch_id": batch_id, "wash_start": start, "wash_end": end, "setpoint_count": len(applied)}
    if applied:
        last = applied[-1]
        summary.update(applied_concentration_pct=last["target_concentration_pct"],
                       applied_temp_c=last["target_temp_c"],
                       applied_wash_time_min=last["wash_time_min"])
    stats = influx.summarize(start, end, config.SENSOR_INTERVAL_S) if start and end else None
    if stats:
        summary.update(stats)
    else:
        log.warning("세척 배치 %s: 요약할 센서 데이터가 없습니다", batch_id)
    db.save_summary(summary)
    push({"type": "history"})
    return summary


@app.post("/api/wash_batches/{batch_id}/summary")
def recompute_summary(batch_id: str):
    """종료된 세척 배치의 요약을 다시 계산 (InfluxDB에 데이터가 남아 있을 때만 가능)"""
    b = get_wash_or_404(batch_id)
    if b["status"] != "종료":
        raise HTTPException(status_code=409, detail=["종료된 세척 배치만 요약할 수 있습니다"])
    return summarize_batch(batch_id)


# ---------------- 이력·CSV ----------------

HISTORY_COLUMNS = [  # (키, CSV 머리글)
    ("batch_id", "세척 배치"), ("stream_name", "선별 구분"), ("bottle_n", "병 수"),
    ("sources", "출처 수거 배치(병 수)"), ("created_by_name", "묶은 방식"), ("created_at", "묶은 시각"),
    ("started_at", "세척 시작"), ("ended_at", "세척 종료"), ("recipe_name", "레시피"),
    ("applied_concentration_pct", "적용 농도(%)"), ("applied_temp_c", "적용 온도(°C)"),
    ("applied_wash_time_min", "적용 세척 시간(분)"), ("setpoint_count", "목표값 적용 횟수"),
    ("avg_concentration_pct", "평균 농도(%)"), ("avg_temp_c", "평균 온도(°C)"),
    ("avg_turbidity_ntu", "평균 탁도(NTU)"), ("max_turbidity_ntu", "최대 탁도(NTU)"),
    ("pump_on_s", "약품 투입 시간(초)"), ("heater_on_s", "히터 가동 시간(초)"), ("samples", "센서 측정 수"),
]


def history_rows() -> list[dict]:
    recipes = config.load_recipes()["recipes"]
    rows = []
    for r in db.history():
        r["stream_name"] = config.STREAM_NAMES.get(r["stream"], r["stream"])
        r["recipe_name"] = recipes.get(r["recipe"], {}).get("name", r["recipe"])
        r["created_by_name"] = "임계치 도달" if r["created_by"] == "auto" else "작업자 지금 묶기"
        rows.append(r)
    return rows


@app.get("/api/history")
def get_history():
    return history_rows()


@app.get("/api/history.csv")
def get_history_csv():
    buf = io.StringIO()
    writer = csv.writer(buf)
    writer.writerow([title for _, title in HISTORY_COLUMNS])
    for r in history_rows():
        writer.writerow(["" if r.get(k) is None else r.get(k) for k, _ in HISTORY_COLUMNS])
    # utf-8-sig: 엑셀에서 열었을 때 한글이 깨지지 않도록 BOM을 붙인다
    filename = f"petwash_history_{datetime.now().strftime('%Y%m%d_%H%M')}.csv"
    return Response(buf.getvalue().encode("utf-8-sig"), media_type="text/csv",
                    headers={"Content-Disposition": f'attachment; filename="{filename}"'})


# ---------------- 설정 (레시피·임계값) ----------------

class RecipeModel(BaseModel):
    name: str = Field(min_length=1)
    concentration_pct: float
    temp_c: float
    wash_time_min: float
    expected_decay_pct_per_min: float = Field(gt=0)


class SettingsIn(BaseModel):
    recipes: dict[str, RecipeModel]
    bin_threshold: dict[str, int]
    tolerance: dict[str, float]
    turbidity_limit_ntu: float = Field(gt=0)
    decay_alert_factor: float = Field(gt=1)


@app.get("/api/settings")
def get_settings():
    return config.load_recipes()


@app.put("/api/settings")
def put_settings(body: SettingsIn):
    current = config.load_recipes()
    errors = []
    if set(body.recipes) != set(config.STREAMS):
        errors.append("오염·비오염 레시피가 모두 있어야 합니다")
    for key, r in body.recipes.items():
        # 레시피도 안전 한계 안이어야 한다 (안전 한계 자체는 장치와 맞춰야 하므로 화면에서 바꾸지 않음)
        name = config.STREAM_NAMES.get(key, key)
        errors += [f"{name} 레시피: {e}" for e in
                   config.check_setpoint(r.concentration_pct, r.temp_c, r.wash_time_min, current["safety"])]
    t = body.bin_threshold
    if set(t) != set(config.STREAMS) or not all(1 <= v <= 10000 for v in t.values()):
        errors.append("저장함 임계치는 오염·비오염 모두 1~10000병이어야 합니다")
    if set(body.tolerance) != {"concentration_pct", "temp_c"} or min(body.tolerance.values()) <= 0:
        errors.append("허용오차(농도·온도)는 0보다 커야 합니다")
    if errors:
        raise HTTPException(status_code=400, detail=errors)

    data = body.model_dump()
    data["recipes"] = {k: data["recipes"][k] for k in config.STREAMS}
    data["bin_threshold"] = {k: data["bin_threshold"][k] for k in config.STREAMS}
    data["safety"] = current["safety"]
    config.save_recipes(data)
    log.info("설정 저장됨")
    # 임계치를 낮췄으면 이미 넘은 저장함을 바로 묶고, 추천을 새 설정으로 다시 계산한다
    with batch_lock:
        made = check_bins()
    update_recommendation()
    push({"type": "recipes", "data": config.load_recipes()})
    push({"type": "bins", "data": bins_payload()})
    if made:
        push({"type": "wash_batches"})
    return config.load_recipes()


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
