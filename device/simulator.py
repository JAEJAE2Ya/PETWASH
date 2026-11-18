"""가짜 장치(시뮬레이터): 실제 장비 없이 전체 흐름을 시연한다

실행 (PET 폴더에서):  .venv\\Scripts\\python -m device.simulator
"""
import json
import logging
import os
import queue
import random
import time
from datetime import datetime, timezone

import paho.mqtt.client as mqtt

from .controller import LocalController
from .sensors import SimulatedCamera, SimulatedWashTank

logging.basicConfig(level=logging.INFO, format="%(asctime)s sim %(levelname)s %(message)s")
log = logging.getLogger("sim")

MQTT_HOST = os.getenv("MQTT_HOST", "localhost")
MQTT_PORT = int(os.getenv("MQTT_PORT", "1883"))

TOPIC_SENSOR = "petwash/sensor/wash"
TOPIC_CAMERA = "petwash/camera/result"
TOPIC_SETPOINT = "petwash/control/setpoint"
TOPIC_STATUS = "petwash/device/status"
TOPIC_BATCH = "petwash/control/batch"

SENSOR_INTERVAL_S = 2.0
STATUS_INTERVAL_S = 5.0
BOTTLES_PER_BATCH = 100
BOTTLE_INTERVAL_S = 0.3


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


class Simulator:
    def __init__(self):
        self.tank = SimulatedWashTank()
        self.camera = SimulatedCamera()
        self.ctrl = LocalController()
        self.inbox: queue.Queue = queue.Queue()  # MQTT 스레드 → 메인 루프로 명령 전달
        self.batch_id = None
        self.bottles_left = 0

        self.client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2, client_id="petwash-sim")
        self.client.on_connect = self._on_connect
        self.client.on_message = self._on_message

    # ---- MQTT ----
    def _on_connect(self, client, userdata, flags, reason_code, properties):
        if reason_code.is_failure:
            log.error("MQTT 연결 실패: %s", reason_code)
            return
        log.info("MQTT 연결됨")
        client.subscribe([(TOPIC_SETPOINT, 1), (TOPIC_BATCH, 1)])

    def _on_message(self, client, userdata, msg):
        try:
            self.inbox.put((msg.topic, json.loads(msg.payload.decode("utf-8"))))
        except (UnicodeDecodeError, json.JSONDecodeError):
            log.warning("잘못된 메시지 무시: %s", msg.topic)

    def publish(self, topic: str, payload: dict):
        self.client.publish(topic, json.dumps(payload, ensure_ascii=False), qos=1)

    # ---- 명령 처리 ----
    def handle(self, topic: str, msg: dict):
        if topic == TOPIC_SETPOINT:
            ok = self.ctrl.apply_setpoint(msg)
            log.info("목표값 수신 %s → %s", msg, "적용" if ok else self.ctrl.message)
            self.publish_status()  # 적용 결과를 바로 회신
        elif topic == TOPIC_BATCH:
            action = msg.get("action")
            if action == "start":
                self.start_batch(msg["batch_id"])
            elif action == "stop":
                self.ctrl.stop("배치 종료")
                self.batch_id = None
                self.bottles_left = 0
                self.publish_status()
            elif action == "replace_water":
                self.tank.replace_water()
                log.info("세척수 교체")

    def start_batch(self, batch_id):
        # 이 배치의 "숨겨진 실제 오염도"를 랜덤으로 정한다 (서버는 카메라 판정으로만 추정)
        ratio = random.uniform(0.0, 0.9)
        self.batch_id = batch_id
        self.tank.hidden_ratio = ratio
        self.camera.hidden_ratio = ratio
        self.bottles_left = BOTTLES_PER_BATCH
        self.ctrl.stop("새 배치: 카메라 판정 중")
        log.info("배치 %s 시작, 숨겨진 오염 비율 %.0f%%", batch_id, ratio * 100)

    def publish_status(self):
        sp = self.ctrl.setpoint
        remain = self.ctrl.remaining_s()
        self.publish(TOPIC_STATUS, {
            "ts": now_iso(),
            "batch_id": self.batch_id,
            "state": self.ctrl.state,
            "message": self.ctrl.message,
            "applied": sp,  # 현재 적용 중인 목표값 (없으면 null)
            "remaining_s": None if remain is None else round(remain),
            "pump_on": self.ctrl.pump_on,
            "heater_on": self.ctrl.heater_on,
        })

    # ---- 메인 루프 ----
    def run(self):
        self.client.connect_async(MQTT_HOST, MQTT_PORT, keepalive=30)
        self.client.loop_start()  # 브로커가 끊겨도 자동 재연결, 로컬 제어는 계속됨
        next_sensor = next_status = next_bottle = time.time()
        last_step = time.time()
        try:
            while True:
                now = time.time()
                while not self.inbox.empty():
                    self.handle(*self.inbox.get())

                # 병 판정 결과 발행
                if self.bottles_left > 0 and now >= next_bottle:
                    label, conf = self.camera.classify()
                    self.publish(TOPIC_CAMERA, {
                        "ts": now_iso(), "batch_id": self.batch_id,
                        "label": label, "confidence": conf,
                    })
                    self.bottles_left -= 1
                    next_bottle = now + BOTTLE_INTERVAL_S
                    if self.bottles_left == 0:
                        log.info("카메라 판정 완료")

                # 센서 읽기 → 로컬 제어 → 발행 (2초마다)
                if now >= next_sensor:
                    self.tank.washing = self.ctrl.state == "세척 중"
                    self.tank.step(now - last_step)
                    last_step = now
                    reading = self.tank.read()
                    pump, heater = self.ctrl.update(reading)
                    self.tank.set_pump(pump)
                    self.tank.set_heater(heater)
                    self.publish(TOPIC_SENSOR, {
                        "ts": now_iso(), **reading, "pump_on": pump, "heater_on": heater,
                    })
                    next_sensor = now + SENSOR_INTERVAL_S

                # 하트비트 겸 상태 (5초마다)
                if now >= next_status:
                    self.publish_status()
                    next_status = now + STATUS_INTERVAL_S

                time.sleep(0.05)
        except KeyboardInterrupt:
            log.info("종료")
        finally:
            self.client.loop_stop()
            self.client.disconnect()


if __name__ == "__main__":
    Simulator().run()
