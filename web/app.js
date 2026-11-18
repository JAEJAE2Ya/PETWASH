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

// ---------------- 배치 관리 ----------------
async function api(method, url, body, errEl) {
  if (errEl) $(errEl).textContent = "";
  const res = await fetch(url, {
    method,
    headers: { "Content-Type": "application/json" },
    body: body ? JSON.stringify(body) : undefined,
  });
  const data = await res.json();
  if (!res.ok) {
    const msg = [].concat(data.detail).map((d) => d.msg || d).join(", ");
    if (errEl) $(errEl).textContent = msg;
    else alert(msg);
    return null;
  }
  return data;
}

const gradeName = (g) => (g == null ? "-" : `${g} (${app.recipes ? app.recipes.grades[g].name : ""})`);

let renderedBatchId = undefined; // 현재 그려 둔 배치 (바뀔 때만 구조를 새로 그림)

function renderBatch() {
  const b = app.batch;
  const box = $("cur-batch");
  const id = b ? b.batch_id : null;

  // 배치가 바뀌었을 때만 화면 구조를 다시 만든다 (등급 선택 상자가 초기화되지 않도록)
  if (id !== renderedBatchId) {
    renderedBatchId = id;
    if (!b) {
      box.innerHTML = `<p class="hint">진행 중인 배치가 없습니다. 아래에서 새 배치를 만들고 "시작"을 누르세요.</p>`;
    } else {
      const options = [0, 1, 2, 3].map((g) => `<option value="${g}">등급 ${gradeName(g)}</option>`).join("");
      box.innerHTML = `
        <table class="kv">
          <tr><th>배치</th><td><b>${b.batch_id}</b></td></tr>
          <tr><th>메모</th><td>${b.note || "-"}</td></tr>
          <tr><th>시작 시각</th><td>${fmtTime(b.started_at)}</td></tr>
        </table>
        <div class="counts">
          <div class="count-box"><div class="n" id="b-cont">0</div><div class="l">오염 판정</div></div>
          <div class="count-box"><div class="n" id="b-clean">0</div><div class="l">비오염 판정</div></div>
          <div class="count-box"><div class="n" id="b-total">0</div><div class="l">합계</div></div>
          <div class="count-box"><div class="n" id="b-ratio">-</div><div class="l">오염 비율</div></div>
        </div>
        <div class="ratio-bar"><div id="b-bar" style="width:0%"></div></div>
        <table class="kv">
          <tr><th>시스템 제안 등급</th><td id="b-suggest">-</td></tr>
          <tr><th>제안 근거</th><td id="b-reason">-</td></tr>
          <tr><th>작업자 확정 등급</th><td id="b-final">-</td></tr>
        </table>
        <div class="grade-row">
          <label for="b-grade">최종 등급</label>
          <select id="b-grade">${options}</select>
          <button id="btn-grade" class="primary">등급 확정</button>
        </div>
        <div class="btn-row"><button id="btn-end" class="danger">배치 종료</button></div>`;
      $("b-grade").value = b.final_grade ?? b.suggested_grade ?? 0;
      $("btn-grade").onclick = async () => {
        const ok = await api("POST", `/api/batches/${b.batch_id}/grade`, { grade: Number($("b-grade").value) }, "batch-error");
        if (ok) $("b-grade").dataset.touched = ""; // 확정 후에는 다시 확정 등급을 따라감
      };
      $("btn-end").onclick = () => {
        if (confirm(`${b.batch_id} 배치를 종료할까요? 장치의 펌프·히터가 정지됩니다.`))
          api("POST", `/api/batches/${b.batch_id}/end`, null, "batch-error");
      };
      $("b-grade").dataset.touched = "";
      $("b-grade").onchange = () => ($("b-grade").dataset.touched = "1");
    }
  }
  if (!b) return;

  // 숫자 갱신
  $("b-cont").textContent = b.contaminated_n;
  $("b-clean").textContent = b.clean_n;
  $("b-total").textContent = b.total_n;
  $("b-ratio").textContent = b.ratio_pct == null ? "-" : b.ratio_pct + "%";
  $("b-bar").style.width = (b.ratio_pct || 0) + "%";
  $("b-suggest").textContent = b.suggested_grade == null ? "판정 결과 대기 중" : `등급 ${gradeName(b.suggested_grade)}`;
  $("b-reason").textContent = b.suggest_reason || "-";
  $("b-final").innerHTML = b.final_grade == null
    ? `<span class="st-wait">미확정</span>`
    : `<span class="st-ok">등급 ${gradeName(b.final_grade)}</span> (${fmtTime(b.graded_at)})`;
  // 작업자가 선택 상자를 건드리지 않았다면 확정 등급(없으면 제안 등급)을 따라가게 한다
  const follow = b.final_grade ?? b.suggested_grade;
  if (!$("b-grade").dataset.touched && follow != null) $("b-grade").value = follow;
}

async function loadBatches() {
  const rows = await (await fetch("/api/batches")).json();
  $("batch-list").innerHTML = rows.length === 0
    ? `<tr><td colspan="10">배치가 없습니다</td></tr>`
    : rows.map((b) => `<tr>
        <td>${b.batch_id}</td>
        <td>${b.status}</td>
        <td>${b.note || ""}</td>
        <td>${b.contaminated_n} / ${b.clean_n}</td>
        <td>${b.ratio_pct == null ? "-" : b.ratio_pct + "%"}</td>
        <td>${b.suggested_grade ?? "-"}</td>
        <td>${b.final_grade ?? "-"}</td>
        <td>${b.started_at ? fmtTime(b.started_at) : "-"}</td>
        <td>${b.ended_at ? fmtTime(b.ended_at) : "-"}</td>
        <td>${b.status === "생성됨" ? `<button data-start="${b.batch_id}">시작</button>` : ""}</td>
      </tr>`).join("");
  $("batch-list").querySelectorAll("[data-start]").forEach((btn) =>
    (btn.onclick = () => api("POST", `/api/batches/${btn.dataset.start}/start`, null, "batch-error")));
}

// 카메라 판정이 0.3초마다 들어오므로 목록 새로고침은 최대 1.5초에 한 번만
let listTimer = null;
function scheduleLoadBatches() {
  if (listTimer) return;
  listTimer = setTimeout(() => { listTimer = null; loadBatches(); }, 1500);
}

$("btn-new-batch").onclick = async () => {
  const b = await api("POST", "/api/batches", { note: $("new-note").value.trim() }, "batch-error");
  if (b) { $("new-note").value = ""; loadBatches(); }
};

// 등급이 확정되면 세척 제어 슬라이더를 그 등급 레시피로 채워 둔다
let filledGrade = null;
function syncSlidersToGrade() {
  const g = app.batch ? app.batch.final_grade : null;
  if (g != null && g !== filledGrade && app.recipes && slidersReady) {
    fillSliders(app.recipes.grades[g]);
  }
  filledGrade = g;
}

function renderAll() {
  renderSensor();
  renderDevice();
  renderAlerts();
  renderControl();
  renderBatch();
  syncSlidersToGrade();
  const b = app.batch;
  $("v-batch").textContent = b ? b.batch_id : "없음";
  $("v-batch-sub").textContent = !b ? "" : b.final_grade != null
    ? `확정 등급 ${gradeName(b.final_grade)}`
    : b.suggested_grade != null ? `제안 등급 ${b.suggested_grade} (미확정)` : "카메라 판정 대기";
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
        app.batch = msg.data.batch;
        loadBatches();
        break;
      case "batch":
        app.batch = msg.data;
        scheduleLoadBatches();
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
