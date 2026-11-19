"""설정 파일(.env, recipes.yaml) 읽기·쓰기와 안전 한계 검사"""
import os
from pathlib import Path

import yaml
from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent.parent
RECIPES_PATH = ROOT / "config" / "recipes.yaml"
DATA_DIR = ROOT / "data"
WEB_DIR = ROOT / "web"

load_dotenv(ROOT / ".env")

MQTT_HOST = os.getenv("MQTT_HOST", "localhost")
MQTT_PORT = int(os.getenv("MQTT_PORT", "1883"))

# MQTT 토픽
TOPIC_SENSOR = "petwash/sensor/wash"
TOPIC_CAMERA = "petwash/camera/result"
TOPIC_SETPOINT = "petwash/control/setpoint"
TOPIC_STATUS = "petwash/device/status"
TOPIC_BATCH = "petwash/control/batch"  # 서버→장치: 선별·저장함·세척 배치 명령 (추가 토픽)
TOPIC_SIM = "petwash/control/sim"      # 서버→시뮬레이터: 시연용 배속 (추가 토픽, 실제 장치는 무시)

HEARTBEAT_TIMEOUT_S = 15  # 이 시간 이상 하트비트가 없으면 "장치 연결 끊김"
SENSOR_INTERVAL_S = 2     # 장치가 센서 값을 보내는 간격, 장치 시간 기준 (가동 시간 계산에 사용)

# 선별 결과 = 저장함 = 레시피 키
STREAMS = ("contaminated", "clean")
STREAM_NAMES = {"contaminated": "오염", "clean": "비오염"}

HEADER = (
    "# 세척 레시피와 임계값 (웹 설정 화면에서 저장하면 이 파일이 다시 써집니다)\n"
    "# recipes: 선별 결과(오염/비오염)별 세척 레시피. expected_decay_pct_per_min = 세척 중 약품 농도가 분당 줄어드는 기대 속도(%p/분)\n"
    "# bin_threshold: 선별 저장함에 병이 이만큼 모이면 세척 배치 1개로 자동으로 묶는다 (병 수)\n"
    "# safety: 안전 한계 (장치 코드 device/controller.py 와 같은 값이어야 함)\n"
)


def load_recipes() -> dict:
    with open(RECIPES_PATH, encoding="utf-8") as f:
        return yaml.safe_load(f)


def save_recipes(data: dict) -> None:
    # newline="\n": 윈도우에서도 줄바꿈을 LF로 저장 (CRLF로 바뀌면 git에서 파일 전체가 바뀐 것으로 보임)
    with open(RECIPES_PATH, "w", encoding="utf-8", newline="\n") as f:
        f.write(HEADER)
        yaml.safe_dump(data, f, allow_unicode=True, sort_keys=False)


def check_setpoint(conc: float, temp: float, minutes: float, safety: dict) -> list[str]:
    """안전 한계 밖의 값이 있으면 오류 메시지 목록을 돌려준다 (빈 목록이면 통과)"""
    errors = []
    checks = [
        ("농도", conc, safety["concentration_pct"], "%"),
        ("온도", temp, safety["temp_c"], "°C"),
        ("세척 시간", minutes, safety["wash_time_min"], "분"),
    ]
    for name, value, (lo, hi), unit in checks:
        if not (lo <= value <= hi):
            errors.append(f"{name} {value}{unit}는 안전 한계({lo}~{hi}{unit}) 밖입니다")
    return errors
