"""센서·구동기·카메라·선별 게이트 인터페이스와 시뮬레이션 구현

나중에 라즈베리파이로 옮길 때는 아래 인터페이스(SensorReader, Actuators, CameraReader, Sorter)를
실제 하드웨어 클래스로 구현하면 된다. 예)
  - 농도: 전도도 센서 + ADS1115(ADC)  → 전도도를 농도(%)로 환산
  - 온도: DS18B20 (1-Wire)
  - 수위: HX711 로드셀 (무게 → 수위 %)
  - 카메라: Intel RealSense + 분류 모델
  - 선별 게이트: 카메라 뒤 솔레노이드 밸브 + 에어 노즐(또는 서보 플랩) → 오염/비오염 저장함
  - 펌프·히터: GPIO 릴레이
"""
import random
from abc import ABC, abstractmethod
from collections import deque


class SensorReader(ABC):
    @abstractmethod
    def read(self) -> dict:
        """{"concentration_pct", "temp_c", "turbidity_ntu", "level_pct"} 를 돌려준다"""


class Actuators(ABC):
    @abstractmethod
    def set_pump(self, on: bool): ...

    @abstractmethod
    def set_heater(self, on: bool): ...


class CameraReader(ABC):
    @abstractmethod
    def classify(self) -> tuple[str, float]:
        """병 1개를 판정해서 ("contaminated" 또는 "clean", 신뢰도) 를 돌려준다"""


class Sorter(ABC):
    @abstractmethod
    def route(self, label: str):
        """카메라 판정 결과대로 병을 오염("contaminated") 또는 비오염("clean") 저장함으로 보낸다"""


# ---------------- 시뮬레이션 구현 ----------------

class SimulatedWashTank(SensorReader, Actuators):
    """가짜 세척조: 펌프·히터 상태와 숨겨진 오염도에 따라 값이 변한다"""

    def __init__(self):
        self.concentration = 0.8  # 약품 농도 %
        self.temp = 30.0          # 세척수 온도 °C
        self.turbidity = 5.0      # 탁도 NTU
        self.level = 80.0         # 수위 %
        self.pump_on = False
        self.heater_on = False
        self.washing = False      # 병을 세척하는 중인지
        self.hidden_ratio = 0.0   # 세척조에 넣은 병의 실제 오염 비율 (0~1), 서버는 모름

    def set_pump(self, on: bool):
        self.pump_on = on

    def set_heater(self, on: bool):
        self.heater_on = on

    def replace_water(self):
        """세척수 교체: 새 물로 바뀌므로 농도·탁도가 내려가고 온도도 떨어진다"""
        self.concentration = 0.0
        self.turbidity = 3.0
        self.temp = 25.0
        self.level = 80.0

    def step(self, dt: float):
        """dt초 만큼 시간을 진행"""
        minutes = dt / 60
        # 약품 농도: 펌프가 켜지면 상승(분당 1.8%p), 세척 중엔 오염이 심할수록 빨리 소모
        if self.pump_on:
            self.concentration += 1.8 * minutes
        decay = (0.03 + 0.15 * self.hidden_ratio) if self.washing else 0.005
        self.concentration -= decay * minutes

        # 온도: 히터 가열(분당 24°C) - 주변으로 빠져나가는 열(온도가 높을수록 큼)
        if self.heater_on:
            self.temp += 24 * minutes
        self.temp -= 0.12 * (self.temp - 20) * minutes

        # 탁도: 세척 중 오염도에 비례해서 서서히 상승
        if self.washing:
            self.turbidity += 4.5 * self.hidden_ratio * minutes

        # 수위: 약품 투입 시 조금 오르고, 세척 중 병에 묻어 나가며 조금 줄어듦
        if self.pump_on:
            self.level += 0.6 * minutes
        if self.washing:
            self.level -= 0.3 * minutes

        self.concentration = max(0.0, self.concentration)
        self.turbidity = max(0.0, self.turbidity)
        self.level = min(100.0, max(0.0, self.level))

    def read(self) -> dict:
        # 실제 센서처럼 약간의 노이즈를 더해서 읽는다
        return {
            "concentration_pct": round(self.concentration + random.gauss(0, 0.01), 3),
            "temp_c": round(self.temp + random.gauss(0, 0.1), 2),
            "turbidity_ntu": round(max(0.0, self.turbidity + random.gauss(0, 0.3)), 2),
            "level_pct": round(self.level + random.gauss(0, 0.2), 1),
        }


class SimulatedCamera(CameraReader):
    """수거 배치의 숨겨진 오염 비율에 맞게 병을 하나씩 만들고 판정한다.

    판정은 신뢰도만큼 맞힌다 (신뢰도 0.9 → 90% 확률로 정답).
    틀린 판정은 반대쪽 저장함으로 가므로, 비오염 저장함에도 오염 병이 조금 섞인다.
    """

    def __init__(self):
        self.hidden_ratio = 0.0
        self.last_truth = False  # 마지막 병이 실제로 오염됐는지 (시뮬레이션 전용, 실제 장치는 알 수 없음)

    def classify(self) -> tuple[str, float]:
        dirty = random.random() < self.hidden_ratio
        conf = round(random.uniform(0.75, 0.99), 2)
        correct = random.random() < conf
        self.last_truth = dirty
        label = "contaminated" if dirty == correct else "clean"
        return label, conf


class SimulatedSorter(Sorter):
    """가짜 선별 게이트 + 저장함 2개. 병마다 실제 오염 여부를 기억해 둔다 (세척조의 숨겨진 오염도 계산용)"""

    def __init__(self):
        self.bins = {"contaminated": deque(), "clean": deque()}

    def route(self, label: str, truth: bool = False):
        self.bins["contaminated" if label == "contaminated" else "clean"].append(truth)

    def take(self, stream: str, count: int) -> list[bool]:
        """저장함에서 먼저 들어온 병부터 count개를 꺼낸다 (찬 저장함을 빼서 세척 배치로 표시)"""
        b = self.bins[stream]
        return [b.popleft() for _ in range(min(count, len(b)))]
