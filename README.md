# 폐페트병 세척 공정 지능화 관리 시스템

"지능형공정데이터관리" 팀 프로젝트 프로토타입.
배치별 오염도를 판정하고, 세척수 센서 값과 함께 보고 세척 조건(약품 농도·온도·시간)을 추천·조절하는 웹 관리 시스템이다.
깨끗한 배치는 세척 자원을 덜 쓰고, 더러운 배치는 충분히 씻어서 플레이크 품질 편차를 줄이는 것이 목표다.

```
[장치/시뮬레이터] --MQTT--> [Mosquitto] --MQTT--> [FastAPI 서버] --WebSocket--> [웹 브라우저]
   센서·카메라·상태                                  │  ├─ InfluxDB 3 (센서 값, 카메라 판정)
   ← 목표값(setpoint)                                │  └─ SQLite (배치, 목표값·결정 이력, 배치 요약)
```

## 1. 준비물 (Windows 11 기준)

| 항목 | 확인 방법 |
|---|---|
| Python 3.10 이상 | `python --version` |
| Mosquitto (localhost:1883, 익명 접속) | `Get-Service mosquitto` → Running |
| InfluxDB 3 Core (http://localhost:8181) | 실행 중이어야 하고 관리자 토큰이 필요 |

InfluxDB 토큰이 없으면 InfluxDB 폴더에서 한 번만 만든다. 출력된 토큰은 다시 볼 수 없으니 바로 `.env`에 저장한다.

```powershell
.\influxdb3.exe create token --admin
```

`petwash` 데이터베이스는 첫 데이터가 들어올 때 자동으로 만들어진다.

## 2. 설치 (처음 한 번)

PET 폴더에서 실행한다.

```powershell
python -m venv .venv
```

```powershell
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
```

```powershell
Copy-Item .env.example .env
```

`.env`를 열어 `INFLUX_TOKEN=` 뒤에 토큰을 넣는다. `.env`는 git에 올라가지 않는다.

```powershell
notepad .env
```

## 3. 실행

터미널 두 개를 열고 각각 PET 폴더에서 실행한다.

**터미널 1: 서버**

```powershell
.\.venv\Scripts\python.exe -m uvicorn server.main:app --port 8000
```

**터미널 2: 장치 시뮬레이터**

```powershell
.\.venv\Scripts\python.exe -m device.simulator
```

브라우저에서 http://localhost:8000 을 연다. 종료는 각 터미널에서 Ctrl+C.

## 4. 시연 순서

화면은 옛날 PC 바이오스 설정 화면 느낌으로 꾸몄다. 바이오스처럼 숫자키 1~5로 화면을 이동할 수 있다.
글꼴은 jsDelivr에서 받는 한글 픽셀 글꼴 Galmuri(SIL OFL)이고, 인터넷이 없으면 윈도우 기본 돋움체로 대신 표시된다.

1. **대시보드**: 2초마다 농도·온도·탁도 그래프가 갱신되는지 본다. 시뮬레이터를 끄면 15초 뒤 "장치 연결 끊김" 경고가 뜬다.
2. **배치 관리**: 메모를 입력하고 "새 배치 만들기" → 목록에서 "시작".
   시뮬레이터가 약 30초 동안 병 100개 판정을 보내고, 오염 비율과 제안 등급(근거 포함)이 실시간으로 바뀐다.
   최종 등급을 고르고 "등급 확정".
3. **세척 제어**: 추천 조치와 근거를 확인하고 "추천 승인"을 누르면 등급 레시피 목표값이 장치로 간다.
   "장치 적용 여부"가 "적용됨"으로 바뀌고, 대시보드 그래프에 목표 점선이 생긴다.
   값을 바꾸고 싶으면 슬라이더(안전 한계 범위)로 조절해 "이 값으로 전송 (수동)".
4. 등급을 실제보다 낮게 확정하면, 2~3분 뒤 "예상보다 오염이 심함, 등급 상향 검토"가 뜬다.
   약품이 기대보다 빨리 소모되기 때문이다.
5. 탁도가 기준을 넘으면 "세척수 교체 권장"이 뜨고, "세척수 교체 지시"로 새 물로 바꿀 수 있다.
6. **배치 관리**에서 "배치 종료" → **이력**에서 평균 농도·온도, 약품 투입 시간 등 요약 확인 → "CSV 내보내기"(엑셀에서 바로 열림).
7. **설정**에서 레시피와 임계값을 바꾸고 저장하면 등급 제안과 추천에 바로 반영된다.

## 5. 폴더 구조

```
server/
  main.py         FastAPI 앱: API, WebSocket, MQTT 수신 처리, 배치·이력·설정
  mqtt_bridge.py  MQTT 구독·발행 (paho-mqtt 2.x, CallbackAPIVersion.VERSION2)
  influx.py       InfluxDB 3 저장·SQL 조회, 배치 요약 계산
  db.py           SQLite (data/petwash.db)
  recommend.py    규칙 기반 추천 로직
  config.py       .env·recipes.yaml 읽기/쓰기, 안전 한계 검사, 등급 제안
device/
  simulator.py    가짜 장치 (MQTT 통신 + 메인 루프)
  sensors.py      SensorReader / Actuators / CameraReader 인터페이스와 시뮬레이션 구현
  controller.py   로컬 제어 (히스테리시스 on/off + 안전 한계)
web/              index.html, app.js, style.css (빌드 도구 없음, Chart.js CDN)
config/recipes.yaml  등급별 레시피, 임계값, 안전 한계
data/             SQLite 파일 (git 제외)
```

## 6. MQTT 토픽 (JSON, 시간은 ISO8601)

| 토픽 | 방향 | 내용 |
|---|---|---|
| `petwash/sensor/wash` | 장치→서버, 2초 | concentration_pct, temp_c, turbidity_ntu, level_pct, pump_on, heater_on |
| `petwash/camera/result` | 장치→서버, 병마다 | batch_id, label(contaminated/clean), confidence |
| `petwash/control/setpoint` | 서버→장치 | batch_id, target_concentration_pct, target_temp_c, wash_time_min, issued_by, issued_at |
| `petwash/device/status` | 장치→서버, 5초 (하트비트) | state, message, applied(적용 중 목표값), last_command(마지막 명령 적용/거부), remaining_s |
| `petwash/control/batch` | 서버→장치 (**추가**) | batch_id, action: start / stop / replace_water |

`petwash/control/batch`는 원래 명세에 없던 토픽이다. 장치가 배치 시작(카메라 판정 시작), 종료(펌프·히터 정지), 세척수 교체를 알 수 있도록 추가했다.

## 7. 데이터 저장

- **InfluxDB (`petwash`)**: `wash_sensor`(센서 값), `camera_result`(병 판정, batch_id 태그)
- **SQLite (`data/petwash.db`)**
  - `batches`: 배치, 판정 집계, 제안·확정 등급
  - `setpoints`: 보낸 목표값과 장치 적용/거부 결과
  - `decisions`: 추천에 대한 작업자 결정(추천 승인 / 수동 수정 / 세척수 교체 지시)
  - `batch_summaries`: 배치 종료 시 요약. InfluxDB 3 Core는 긴 기간 조회에 제약이 있어서 따로 저장한다.

처음부터 다시 시작하려면 서버를 끄고 `data\petwash.db`를 지운다.

## 8. 라즈베리파이로 옮길 때

`device/sensors.py`의 인터페이스를 실제 하드웨어 클래스로 구현하고, `simulator.py`에서 `SimulatedWashTank`·`SimulatedCamera` 대신 그 클래스를 쓰면 된다. 통신과 로컬 제어 코드는 그대로 쓸 수 있다.

- `SensorReader.read()`: 전도도 센서 + ADS1115(농도), DS18B20(온도), HX711 로드셀(수위), 탁도 센서
- `Actuators.set_pump()` / `set_heater()`: GPIO 릴레이
- `CameraReader.classify()`: Intel RealSense + 분류 모델

서버나 네트워크가 끊겨도 장치는 마지막 목표값과 안전 한계 안에서 계속 제어한다. 다만 지금은 목표값을 메모리에만 들고 있어서, 장치가 재부팅되면 목표값이 사라진다.

## 9. 알려진 한계

- **안전 한계 이중 관리**: `config/recipes.yaml`과 `device/controller.py` 두 곳에 같은 값이 있다. 웹 설정 화면에서는 바꿀 수 없고, 바꿀 때는 두 곳을 함께 수정해야 한다.
- **농도 감소 속도**: 서버 메모리에서 최근 10분 데이터로 계산하므로, 서버를 재시작하면 2분 정도 "판단 보류"가 된다.
- **가동 시간 근사**: 약품 투입·히터 가동 시간은 "켜져 있던 측정 수 × 2초"로 계산한 근사값이다.
- **시뮬레이터 시간**: 실제 시간으로 동작하므로 세척 시간(10~20분)을 끝까지 보려면 그만큼 기다려야 한다.
- **보안**: 로그인·권한 기능이 없다. 수업용 로컬 프로토타입 전제다.
