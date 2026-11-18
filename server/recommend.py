"""규칙 기반 세척 조건 추천

입력: 배치 등급의 목표 레시피 + 최근 센서 값 (+ 세척 중 농도 감소 속도)
출력: 조치 목록. 각 조치에는 근거 문장이 붙는다. 추천은 자동 적용하지 않는다.
"""
from collections import deque

MIN_DECAY_MINUTES = 2.0  # 감소 속도를 판단하려면 펌프가 꺼진 구간이 최소 이만큼 있어야 함
DECAY_WINDOW_S = 600     # 최근 10분 데이터로 계산


class DecayTracker:
    """세척 중 약품 농도가 얼마나 빨리 줄어드는지 추적한다.

    펌프가 켜져 있으면 농도가 오르므로, 펌프가 꺼져 있던 구간의 변화만 모아서
    "분당 몇 %p 줄었는지"를 계산한다.
    """

    def __init__(self):
        self.batch_id = None
        self.samples = deque()  # (시각 초, 농도, 펌프 on 여부)

    def add(self, batch_id: str, t: float, conc: float, pump_on: bool):
        if batch_id != self.batch_id:  # 배치가 바뀌면 처음부터 다시
            self.batch_id = batch_id
            self.samples.clear()
        self.samples.append((t, conc, pump_on))
        while self.samples and t - self.samples[0][0] > DECAY_WINDOW_S:
            self.samples.popleft()

    def reset(self):
        self.batch_id = None
        self.samples.clear()

    def rate(self) -> tuple[float, float] | None:
        """(분당 감소량 %p, 계산에 쓴 시간(분)) 또는 데이터가 부족하면 None"""
        drop = 0.0
        seconds = 0.0
        prev = None
        for s in self.samples:
            # 앞뒤 두 측정 모두 펌프가 꺼져 있던 구간만 사용
            if prev is not None and not prev[2] and not s[2]:
                drop += prev[1] - s[1]
                seconds += s[0] - prev[0]
            prev = s
        minutes = seconds / 60
        if minutes < MIN_DECAY_MINUTES:
            return None
        return drop / minutes, minutes


def _item(kind: str, level: str, action: str, reason: str) -> dict:
    # level: ok(유지) / progress(장치가 자동으로 맞추는 중) / warn(조치 필요) / alert(주의 깊게 볼 것)
    return {"kind": kind, "level": level, "action": action, "reason": reason}


def recommend(grade: int, recipes: dict, sensor: dict | None, decay: tuple | None,
              applied: bool = False) -> dict:
    """applied=True 이면 장치가 이미 이 레시피 목표값으로 제어 중이므로,
    "필요" 대신 장치가 지금 하고 있는 일(가열 중, 투입 중)로 표시한다."""
    recipe = recipes["grades"][grade]
    tol = recipes["tolerance"]
    target = {
        "target_concentration_pct": recipe["concentration_pct"],
        "target_temp_c": recipe["temp_c"],
        "wash_time_min": recipe["wash_time_min"],
    }
    result = {"available": True, "grade": grade, "grade_name": recipe["name"], "target": target,
              "applied": applied, "items": []}
    if sensor is None:
        result["items"].append(_item("sensor", "alert", "센서 값 없음", "장치에서 센서 값을 아직 받지 못했습니다."))
        return result
    items = result["items"]

    # 1) 약품 농도
    conc, t_conc, c_tol = sensor["concentration_pct"], recipe["concentration_pct"], tol["concentration_pct"]
    diff = conc - t_conc
    basis = f"현재 농도 {conc:.2f}%, 목표 {t_conc}% (허용오차 ±{c_tol}%p)"
    if diff < -c_tol:
        if applied:
            items.append(_item("concentration", "progress", f"장치가 약품 투입 중 (목표까지 {-diff:.2f}%p)", basis))
        else:
            items.append(_item("concentration", "warn", f"약품 추가 필요 (약 {-diff:.2f}%p 부족)", basis + f" → {-diff:.2f}%p 낮음"))
    elif diff > c_tol:
        # 장치는 농도를 낮출 수 없다 (물 보충 장치 없음) → 적용 중이어도 작업자 조치가 필요할 수 있음
        if applied:
            items.append(_item("concentration", "warn", "농도 높음: 약품 투입 멈춤, 소모되며 내려가는 중 (빨리 낮추려면 물 보충)",
                               basis + f" → {diff:.2f}%p 높음"))
        else:
            items.append(_item("concentration", "warn", "약품 투입 중단, 필요 시 물 보충", basis + f" → {diff:.2f}%p 높음"))
    else:
        items.append(_item("concentration", "ok", "농도 유지", basis + " → 허용오차 이내"))

    # 2) 온도
    temp, t_temp, t_tol = sensor["temp_c"], recipe["temp_c"], tol["temp_c"]
    diff = temp - t_temp
    basis = f"현재 온도 {temp:.1f}°C, 목표 {t_temp}°C (허용오차 ±{t_tol}°C)"
    if diff < -t_tol:
        if applied:
            items.append(_item("temp", "progress", f"장치가 히터 가동 중 (목표까지 {-diff:.1f}°C)", basis))
        else:
            items.append(_item("temp", "warn", f"가열 필요 (약 {-diff:.1f}°C 부족)", basis + f" → {-diff:.1f}°C 낮음"))
    elif diff > t_tol:
        if applied:
            items.append(_item("temp", "progress", f"히터 꺼짐, 식는 중 (목표보다 {diff:.1f}°C 높음)", basis))
        else:
            items.append(_item("temp", "warn", "가열 중단, 자연 냉각 대기", basis + f" → {diff:.1f}°C 높음"))
    else:
        items.append(_item("temp", "ok", "온도 유지", basis + " → 허용오차 이내"))

    # 3) 탁도
    turb, limit = sensor["turbidity_ntu"], recipes["turbidity_limit_ntu"]
    if turb > limit:
        items.append(_item("turbidity", "alert", "세척수 교체 권장", f"탁도 {turb:.1f} NTU가 교체 기준 {limit} NTU를 넘었습니다."))
    else:
        items.append(_item("turbidity", "ok", "세척수 상태 양호", f"탁도 {turb:.1f} NTU ≤ 교체 기준 {limit} NTU"))

    # 4) 농도 감소 속도 (오염이 심하면 약품이 빨리 소모된다는 가정)
    expected = recipe["expected_decay_pct_per_min"]
    factor = recipes["decay_alert_factor"]
    if decay is None:
        items.append(_item("decay", "ok", "감소 속도 판단 보류",
                           f"세척 중 펌프가 꺼진 구간이 {MIN_DECAY_MINUTES:.0f}분 이상 쌓이면 판단합니다."))
    else:
        rate, minutes = decay
        basis = (f"최근 {minutes:.1f}분간 농도 감소 속도 {rate:.3f}%p/분, "
                 f"등급 {grade} 기대치 {expected}%p/분의 {rate / expected:.1f}배 (기준 {factor}배)")
        if rate > expected * factor:
            action = "예상보다 오염이 심함, 등급 상향 검토" if grade < 3 else "예상보다 오염이 심함, 세척 시간 연장 검토"
            items.append(_item("decay", "alert", action, basis))
        else:
            items.append(_item("decay", "ok", "약품 소모 속도 정상", basis))
    return result
