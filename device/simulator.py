"""가짜 장치(시뮬레이터): 실제 장비 없이 전체 흐름을 시연한다

실행 (PET 폴더에서):  .venv\\Scripts\\python -m device.simulator

시연용 가속: 웹 화면의 "(테스트) 10배 가속" 버튼 → petwash/control/sim 토픽으로 배속이 온다.
장치 안의 시간(가열, 약품 소모, 세척 타이머, 카메라 판정)이 배속만큼 빨리 흐른다.
실제 장치(라즈베리파이)는 이 토픽을 구독하지 않는다.
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
TOPIC_SIM = "petwash/control/sim"  # 시연용 배속 (시뮬레이터 전용)

# 아래 간격은 "장치 안의 시간" 기준. 10배속이면 실제로는 1/10 간격으로 일어난다.
CONTROL_PERIOD_S = 2.0   # 센서 읽기 → 제어 → 센서 값 발행 주기
BOTTLE_INTERVAL_S = 0.3  # 병 1개 판정 간격
BOTTLES_PER_BATCH = 100
STATUS_INTERVAL_S = 5.0  # 하트비트는 실제 시간 기준 (서버의 연결 끊김 판단과 맞춰야 함)
ALLOWED_SPEEDS = (1, 10)


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


class Simulator:
    def __init__(self):
        self.tank = SimulatedWashTank()
        self.camera = SimulatedCamera()
        self.speed = 1          # 배속 (1 = 실제 시간)
        self.sim_now = 0.0      # 시뮬레이션 시계 (초). 배속만큼 빨리 흐른다
        self.ctrl = LocalController(clock=lambda: self.sim_now)
        # 메인 루프 시각 기록
        self.last_real = None    # 마지막 tick의 실제 시각
        self.next_status = 0.0   # 다음 하트비트 (실제 시각)
        self.next_control = 0.0  # 다음 제어 단계 (장치 시각)
        self.next_bottle = 0.0   # 다음 병 판정 (장치 시각)
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
        client.subscribe([(TOPIC_SETPOINT, 1), (TOPIC_BATCH, 1), (TOPIC_SIM, 1)])

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
            if ok and msg.get("batch_id"):
                self.batch_id = msg["batch_id"]  # 장치가 재시작돼도 목표값의 배치를 따라간다
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
        elif topic == TOPIC_SIM:
            speed = msg.get("speed")
            if speed in ALLOWED_SPEEDS:
                self.speed = speed
                log.info("시연용 배속: x%d", speed)
                self.publish_status()

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
            "last_command": self.ctrl.last_command,  # 마지막 명령의 적용/거부 결과
            "remaining_s": None if remain is None else round(remain),
            "pump_on": self.ctrl.pump_on,
            "heater_on": self.ctrl.heater_on,
            "time_scale": self.speed,  # 시연용 배속 (실제 장치는 항상 1)
        })

    # ---- 메인 루프 ----
    def control_step(self):
        """센서 읽기 → 로컬 제어 → 센서 값 발행 (장치 시간 2초마다)"""
        self.tank.washing = self.ctrl.state == "세척 중"
        self.tank.step(CONTROL_PERIOD_S)
        reading = self.tank.read()
        pump, heater = self.ctrl.update(reading)
        self.tank.set_pump(pump)
        self.tank.set_heater(heater)
        self.publish(TOPIC_SENSOR, {
            "ts": now_iso(), **reading, "pump_on": pump, "heater_on": heater,
            "time_scale": self.speed,
        })

    def publish_bottle(self):
        label, conf = self.camera.classify()
        self.publish(TOPIC_CAMERA, {
            "ts": now_iso(), "batch_id": self.batch_id, "label": label, "confidence": conf,
        })
        self.bottles_left -= 1
        if self.bottles_left == 0:
            log.info("카메라 판정 완료")

    def tick(self, now: float):
        """메인 루프 한 바퀴. now = 실제 시각(time.time())"""
        if self.last_real is None:
            self.last_real = self.next_status = now
        # 시뮬레이션 시계를 "실제로 흐른 시간 x 배속"만큼 진행
        self.sim_now += (now - self.last_real) * self.speed
        self.last_real = now
        while not self.inbox.empty():
            self.handle(*self.inbox.get())

        # 병 판정 결과 발행 (밀린 만큼 한꺼번에)
        if self.bottles_left == 0:
            self.next_bottle = self.sim_now
        while self.bottles_left > 0 and self.sim_now >= self.next_bottle:
            self.publish_bottle()
            self.next_bottle += BOTTLE_INTERVAL_S

        # 제어는 항상 장치 시간 2초 간격으로 한 단계씩 진행
        # (배속을 올려도 한 번에 크게 건너뛰지 않으므로 온도·농도 제어가 실제와 같게 동작)
        steps = 0
        while self.sim_now >= self.next_control and steps < 50:
            self.control_step()
            self.next_control += CONTROL_PERIOD_S
            steps += 1
        if steps == 50:  # 너무 밀리면 따라잡기를 포기하고 현재 시각부터 다시
            self.next_control = self.sim_now + CONTROL_PERIOD_S

        # 하트비트 겸 상태 (실제 시간 5초마다)
        if now >= self.next_status:
            self.publish_status()
            self.next_status = now + STATUS_INTERVAL_S

    def run(self):
        self.client.connect_async(MQTT_HOST, MQTT_PORT, keepalive=30)
        self.client.loop_start()  # 브로커가 끊겨도 자동 재연결, 로컬 제어는 계속됨
        try:
            while True:
                self.tick(time.time())
                time.sleep(0.02)
        except KeyboardInterrupt:
            log.info("종료")
        finally:
            self.client.loop_stop()
            self.client.disconnect()


if __name__ == "__main__":
    Simulator().run()
