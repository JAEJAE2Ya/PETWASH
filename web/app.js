// 폐페트병 세척 공정 관리 - 프론트엔드 (바닐라 JS + Chart.js)

const MAX_POINTS = 300; // 그래프에 남길 점 개수 (2초 간격 → 약 10분)
const $ = (id) => document.getElementById(id);

const app = {
  recipes: null,
  sensor: null,
  deviceStatus: null,
  deviceOnline: false,
  mqttConnected: true,
  influxError: null,
  wsOpen: false,
};

// ---------------- 탭 전환 ----------------
document.querySelectorAll(".tab").forEach((btn) => {
  btn.addEventListener("click", () => {
    document.querySelectorAll(".tab").forEach((b) => b.classList.toggle("active", b === btn));
    document.querySelectorAll(".page").forEach((p) =>
      p.classList.toggle("active", p.id === "tab-" + btn.dataset.tab));
  });
});

// ---------------- 그래프 ----------------
function makeChart(canvasId, color) {
  return new Chart($(canvasId), {
    type: "line",
    data: {
      labels: [],
      datasets: [
        { label: "측정값", data: [], borderColor: color, backgroundColor: color,
          borderWidth: 2, pointRadius: 0, tension: 0.2 },
        { label: "목표", data: [], borderColor: "#888", borderDash: [6, 4],
          borderWidth: 1.5, pointRadius: 0, spanGaps: true },
      ],
    },
    options: {
      animation: false,
      maintainAspectRatio: false,
      interaction: { mode: "index", intersect: false },
      scales: { x: { ticks: { maxTicksLimit: 6, autoSkip: true } } },
      plugins: { legend: { position: "bottom", labels: { boxWidth: 12 } } },
    },
  });
}

const charts = {
  conc: makeChart("c-conc", "#1f6feb"),
  temp: makeChart("c-temp", "#d9480f"),
  turb: makeChart("c-turb", "#7048e8"),
};

function timeLabel(iso) {
  const d = new Date(iso);
  return [d.getHours(), d.getMinutes(), d.getSeconds()].map((n) => String(n).padStart(2, "0")).join(":");
}

// 현재 목표값: 장치가 적용 중인 setpoint (없으면 null)
function currentTargets() {
  const sp = app.deviceStatus && app.deviceStatus.applied;
  return {
    conc: sp ? sp.target_concentration_pct : null,
    temp: sp ? sp.target_temp_c : null,
    turb: app.recipes ? app.recipes.turbidity_limit_ntu : null, // 탁도는 교체 임계값을 선으로 표시
  };
}

function addPoint(d, update = true) {
  const t = currentTargets();
  const values = { conc: d.concentration_pct, temp: d.temp_c, turb: d.turbidity_ntu };
  for (const key of Object.keys(charts)) {
    const c = charts[key];
    c.data.labels.push(timeLabel(d.ts));
    c.data.datasets[0].data.push(values[key]);
    c.data.datasets[1].data.push(t[key]);
    if (c.data.labels.length > MAX_POINTS) {
      c.data.labels.shift();
      c.data.datasets.forEach((ds) => ds.data.shift());
    }
    if (update) c.update();
  }
}

async function loadHistory() {
  try {
    const rows = await (await fetch("/api/sensor/history?minutes=10")).json();
    for (const c of Object.values(charts)) {
      c.data.labels = [];
      c.data.datasets.forEach((ds) => (ds.data = []));
    }
    rows.forEach((r) => addPoint(r, false));
    Object.values(charts).forEach((c) => c.update());
  } catch (e) {
    console.warn("이력 불러오기 실패", e);
  }
}

// ---------------- 화면 갱신 ----------------
function onOff(el, on) {
  el.textContent = on ? "켜짐" : "꺼짐";
  el.className = "value " + (on ? "on-text" : "off-text");
}

function renderSensor() {
  const d = app.sensor;
  if (!d) return;
  $("v-conc").textContent = d.concentration_pct.toFixed(2);
  $("v-temp").textContent = d.temp_c.toFixed(1);
  $("v-turb").textContent = d.turbidity_ntu.toFixed(1);
  $("v-level").textContent = d.level_pct.toFixed(0);
  onOff($("v-pump"), d.pump_on);
  onOff($("v-heater"), d.heater_on);
}

function renderDevice() {
  const s = app.deviceStatus;
  const t = currentTargets();
  $("t-conc").textContent = t.conc != null ? `목표 ${t.conc}%` : "목표 없음";
  $("t-temp").textContent = t.temp != null ? `목표 ${t.temp}°C` : "목표 없음";
  $("t-turb").textContent = t.turb != null ? `교체 기준 ${t.turb} NTU` : "";

  if (!app.deviceOnline) {
    $("v-device").textContent = "연결 끊김";
    $("v-device").className = "value off-text";
  } else if (s) {
    $("v-device").textContent = s.state;
    $("v-device").className = "value on-text";
  }
  let msg = s ? s.message || "" : "";
  if (s && s.remaining_s != null && s.state === "세척 중") {
    const m = Math.floor(s.remaining_s / 60), sec = s.remaining_s % 60;
    msg += ` · 남은 시간 ${m}분 ${sec}초`;
  }
  $("v-device-msg").textContent = msg;
  $("v-batch").textContent = s && s.batch_id ? s.batch_id : "없음";
}

function renderAlerts() {
  const list = [];
  if (!app.wsOpen) list.push(["bad", "서버와 연결이 끊겼습니다. 다시 연결하는 중…"]);
  else {
    if (!app.deviceOnline) list.push(["bad", "장치 연결 끊김: 15초 이상 하트비트가 없습니다"]);
    if (!app.mqttConnected) list.push(["bad", "서버가 MQTT 브로커에 연결되지 않았습니다"]);
    if (app.influxError) list.push(["warn", "InfluxDB 저장 문제: " + app.influxError]);
    const d = app.sensor;
    if (d && app.recipes && d.turbidity_ntu > app.recipes.turbidity_limit_ntu)
      list.push(["warn", `탁도 ${d.turbidity_ntu.toFixed(1)} NTU: 교체 기준(${app.recipes.turbidity_limit_ntu}) 초과`]);
  }
  $("alerts").innerHTML = list.map(([cls, text]) => `<div class="alert ${cls}">${text}</div>`).join("");
}

// ---------------- 세척 제어: 수동 조절 ----------------
const sliders = [
  { key: "concentration_pct", el: "s-conc", out: "o-conc", digits: 1 },
  { key: "temp_c", el: "s-temp", out: "o-temp", digits: 0 },
  { key: "wash_time_min", el: "s-time", out: "o-time", digits: 0 },
];
let slidersReady = false;

function setupSliders() {
  if (!app.recipes || slidersReady) return;
  const safety = app.recipes.safety;
  for (const s of sliders) {
    const input = $(s.el);
    [input.min, input.max] = safety[s.key];
    input.oninput = () => ($(s.out).textContent = Number(input.value).toFixed(s.digits));
  }
  // 등급 레시피 버튼
  $("preset-row").innerHTML = Object.entries(app.recipes.grades)
    .map(([g, r]) => `<button data-grade="${g}">등급 ${g} (${r.name})</button>`).join("");
  $("preset-row").querySelectorAll("button").forEach((b) => (b.onclick = () => fillSliders(app.recipes.grades[b.dataset.grade])));
  fillSliders(app.recipes.grades[0]);
  slidersReady = true;
}

function fillSliders(v) {
  const values = { concentration_pct: v.concentration_pct, temp_c: v.temp_c, wash_time_min: v.wash_time_min };
  for (const s of sliders) {
    $(s.el).value = values[s.key];
    $(s.out).textContent = Number(values[s.key]).toFixed(s.digits);
  }
}

async function sendSetpoint(body) {
  $("send-error").textContent = "";
  const res = await fetch("/api/setpoint", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
  });
  if (!res.ok) {
    const err = await res.json();
    $("send-error").textContent = "전송 실패: " + [].concat(err.detail).map((d) => d.msg || d).join(", ");
    return null;
  }
  return res.json();
}

$("btn-send").onclick = () =>
  sendSetpoint({
    target_concentration_pct: Number($("s-conc").value),
    target_temp_c: Number($("s-temp").value),
    wash_time_min: Number($("s-time").value),
    issued_by: "operator",
  });

function fmtTime(iso) {
  return iso ? new Date(iso).toLocaleString("ko-KR", { hour12: false }) : "-";
}

function spRows(sp) {
  return `<tr><th>배치</th><td>${sp.batch_id ?? "-"}</td></tr>
    <tr><th>약품 농도</th><td>${sp.target_concentration_pct} %</td></tr>
    <tr><th>온도</th><td>${sp.target_temp_c} °C</td></tr>
    <tr><th>세척 시간</th><td>${sp.wash_time_min} 분</td></tr>
    <tr><th>보낸 사람</th><td>${sp.issued_by === "auto" ? "자동" : "작업자"}</td></tr>
    <tr><th>보낸 시각</th><td>${fmtTime(sp.issued_at)}</td></tr>`;
}

function renderControl() {
  setupSliders();
  const sp = app.lastSetpoint;
  if (!sp) {
    $("last-sp").innerHTML = `<tr><td>아직 보낸 목표값이 없습니다</td></tr>`;
  } else {
    const cls = { "적용됨": "st-ok", "거부됨": "st-bad", "전송됨": "st-wait" }[sp.result];
    const label = sp.result === "전송됨" ? "전송됨 · 장치 응답 대기 중" : sp.result;
    $("last-sp").innerHTML = spRows(sp) +
      `<tr><th>장치 적용 여부</th><td class="${cls}">${label}</td></tr>` +
      (sp.result_at ? `<tr><th>응답 시각</th><td>${fmtTime(sp.result_at)}</td></tr>` : "") +
      (sp.result_message ? `<tr><th>장치 메시지</th><td>${sp.result_message}</td></tr>` : "");
  }
  const st = app.deviceStatus;
  if (!app.deviceOnline) {
    $("applied-sp").innerHTML = `<tr><td class="st-bad">장치 연결 끊김</td></tr>`;
  } else if (!st || !st.applied) {
    $("applied-sp").innerHTML = `<tr><td>적용 중인 목표값 없음 (${st ? st.state : "-"})</td></tr>`;
  } else {
    $("applied-sp").innerHTML = spRows(st.applied) + `<tr><th>장치 상태</th><td>${st.state}</td></tr>`;
  }
}

function renderAll() {
  renderSensor();
  renderDevice();
  renderAlerts();
  renderControl();
}

// ---------------- WebSocket ----------------
function connect() {
  const ws = new WebSocket(`ws://${location.host}/ws`);
  ws.onopen = () => {
    app.wsOpen = true;
    $("conn").textContent = "서버 연결됨";
    $("conn").className = "badge on";
    loadHistory();
  };
  ws.onclose = () => {
    app.wsOpen = false;
    $("conn").textContent = "서버 연결 끊김";
    $("conn").className = "badge off";
    renderAlerts();
    setTimeout(connect, 2000); // 2초 뒤 재연결
  };
  ws.onmessage = (ev) => {
    const msg = JSON.parse(ev.data);
    switch (msg.type) {
      case "snapshot":
        app.recipes = msg.data.recipes;
        app.sensor = msg.data.sensor;
        app.deviceStatus = msg.data.device_status;
        app.deviceOnline = msg.data.device_online;
        app.mqttConnected = msg.data.mqtt_connected;
        app.influxError = msg.data.influx_error;
        app.lastSetpoint = msg.data.last_setpoint;
        break;
      case "setpoint":
        app.lastSetpoint = msg.data;
        break;
      case "sensor":
        app.sensor = msg.data;
        addPoint(msg.data);
        break;
      case "device_status":
        app.deviceStatus = msg.data;
        break;
      case "device_online":
        app.deviceOnline = msg.online;
        break;
      case "health":
        app.mqttConnected = msg.mqtt_connected;
        app.influxError = msg.influx_error;
        break;
    }
    renderAll();
  };
}

connect();
