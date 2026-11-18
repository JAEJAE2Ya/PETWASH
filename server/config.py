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
TOPIC_BATCH = "petwash/control/batch"  # 서버→장치: 배치 시작/종료 알림 (추가 토픽)

HEARTBEAT_TIMEOUT_S = 15  # 이 시간 이상 하트비트가 없으면 "장치 연결 끊김"


def load_recipes() -> dict:
    with open(RECIPES_PATH, encoding="utf-8") as f:
        data = yaml.safe_load(f)
    # YAML 키가 숫자로 읽히므로 등급 키를 정수로 통일
    data["grades"] = {int(k): v for k, v in data["grades"].items()}
    return data


def save_recipes(data: dict) -> None:
    with open(RECIPES_PATH, "w", encoding="utf-8") as f:
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


def suggest_grade(ratio_pct: float, thresholds: list) -> tuple[int, str]:
    """오염 비율(%)로 등급을 제안하고, 근거 문장도 함께 돌려준다"""
    t0, t1, t2 = thresholds
    if ratio_pct < t0:
        return 0, f"오염 비율 {ratio_pct:.1f}% < {t0}% → 등급 0"
    if ratio_pct < t1:
        return 1, f"오염 비율 {ratio_pct:.1f}%: {t0}% 이상 {t1}% 미만 → 등급 1"
    if ratio_pct < t2:
        return 2, f"오염 비율 {ratio_pct:.1f}%: {t1}% 이상 {t2}% 미만 → 등급 2"
    return 3, f"오염 비율 {ratio_pct:.1f}% ≥ {t2}% → 등급 3"
