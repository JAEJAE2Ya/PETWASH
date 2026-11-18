"""FastAPI 서버: MQTT 수신 → InfluxDB 저장 → WebSocket으로 브라우저에 실시간 전달

실행 (PET 폴더에서):  .venv\\Scripts\\python -m uvicorn server.main:app --port 8000
"""
import asyncio
import logging
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
}
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
    elif topic == config.TOPIC_CAMERA:
        influx.write_camera(payload)
        push({"type": "camera", "data": payload})
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
    sp["batch_id"] = sp["batch_id"] or "TEST"
    sp["issued_at"] = now_iso()  # 장치가 회신할 때 이 값으로 어떤 명령인지 구분
    mqtt_bridge.publish(config.TOPIC_SETPOINT, sp)
    db.insert_setpoint(sp)
    state["last_setpoint"] = {**sp, "result": "전송됨", "result_at": None, "result_message": ""}
    push({"type": "setpoint", "data": state["last_setpoint"]})
    return state["last_setpoint"]


@app.post("/api/setpoint")
def post_setpoint(sp: SetpointIn):
    return send_setpoint(sp)


@app.get("/api/setpoints")
def get_setpoints(limit: int = 10):
    return db.recent_setpoints(limit)


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
