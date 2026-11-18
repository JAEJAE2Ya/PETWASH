"""장치 로컬 제어: 목표값을 향해 펌프·히터를 on/off (히스테리시스) + 안전 한계 검사

서버나 네트워크가 끊겨도 마지막으로 받은 목표값과 안전 한계 안에서 계속 동작한다.
"""
import time

# 장치 쪽 안전 한계 (서버와 같은 값, 서버 설정과 무관하게 장치가 한 번 더 검사)
SAFETY = {
    "concentration_pct": (0.5, 3.0),
    "temp_c": (40.0, 85.0),
    "wash_time_min": (5.0, 30.0),
}
MIN_LEVEL_PCT = 20.0  # 수위가 이보다 낮으면 히터·펌프를 끈다 (공회전·과열 방지)

# 히스테리시스 폭: 목표값 ± 이 값 사이에서는 현재 상태 유지
CONC_BAND = 0.05
TEMP_BAND = 0.5


class LocalController:
    def __init__(self, clock=time.time):
        # clock: 현재 시각(초)을 돌려주는 함수. 실제 장치는 time.time,
        # 시뮬레이터는 시연용 가속을 위해 "시뮬레이션 시계"를 넘겨준다.
        self.clock = clock
        self.setpoint = None     # 현재 적용 중인 목표값 (dict)
        self.wash_started = None  # 세척 시작 시각 (clock 기준)
        self.state = "대기"
        self.message = "목표값을 기다리는 중"
        self.pump_on = False
        self.heater_on = False
        # 마지막으로 받은 명령의 처리 결과 (서버가 적용/거부 여부를 확인하는 데 사용)
        self.last_command = None

    def apply_setpoint(self, sp: dict) -> bool:
        ok = self._apply(sp)
        if not ok and self.setpoint is not None:
            self.message += " → 이전 목표값 유지"
        self.last_command = {"issued_at": sp.get("issued_at"), "accepted": ok, "message": self.message}
        return ok

    def _apply(self, sp: dict) -> bool:
        """목표값을 검사해서 통과하면 적용. 범위 밖이면 거부하고 이전 목표값 유지"""
        try:
            values = {
                "concentration_pct": float(sp["target_concentration_pct"]),
                "temp_c": float(sp["target_temp_c"]),
                "wash_time_min": float(sp["wash_time_min"]),
            }
        except (KeyError, TypeError, ValueError):
            self.message = "목표값 거부: 형식 오류"
            return False
        for key, value in values.items():
            lo, hi = SAFETY[key]
            if not (lo <= value <= hi):
                self.message = f"목표값 거부: {key}={value} (한계 {lo}~{hi})"
                return False

        same_batch = self.setpoint is not None and self.setpoint.get("batch_id") == sp.get("batch_id")
        self.setpoint = dict(sp)
        # 같은 배치 안에서 목표값만 바뀐 경우에는 세척 타이머를 이어간다
        if not same_batch or self.wash_started is None:
            self.wash_started = self.clock()
        self.state = "세척 중"
        self.message = f"목표값 적용 ({sp.get('issued_by', '?')})"
        return True

    def stop(self, reason: str):
        """배치 종료 등으로 세척을 멈춘다"""
        self.setpoint = None
        self.wash_started = None
        self.state = "대기"
        self.message = reason

    def remaining_s(self) -> float | None:
        if self.setpoint is None or self.wash_started is None:
            return None
        total = float(self.setpoint["wash_time_min"]) * 60
        return max(0.0, total - (self.clock() - self.wash_started))

    def update(self, reading: dict) -> tuple[bool, bool]:
        """센서 값을 보고 (펌프, 히터) on/off 를 결정"""
        if self.setpoint is None or self.state != "세척 중":
            self.pump_on = self.heater_on = False
            return False, False

        if self.remaining_s() == 0:
            self.state = "세척 완료"
            self.message = "설정한 세척 시간이 끝났습니다"
            self.pump_on = self.heater_on = False
            return False, False

        conc = reading["concentration_pct"]
        temp = reading["temp_c"]
        t_conc = float(self.setpoint["target_concentration_pct"])
        t_temp = float(self.setpoint["target_temp_c"])

        # 히스테리시스 on/off 제어
        if conc < t_conc - CONC_BAND:
            self.pump_on = True
        elif conc > t_conc + CONC_BAND:
            self.pump_on = False
        if temp < t_temp - TEMP_BAND:
            self.heater_on = True
        elif temp > t_temp + TEMP_BAND:
            self.heater_on = False

        # 안전 한계: 목표와 상관없이 강제로 끈다
        if conc >= SAFETY["concentration_pct"][1]:
            self.pump_on = False
        if temp >= SAFETY["temp_c"][1]:
            self.heater_on = False
        if reading["level_pct"] < MIN_LEVEL_PCT:
            self.pump_on = self.heater_on = False
            self.message = "수위 낮음: 펌프·히터 정지"

        return self.pump_on, self.heater_on
