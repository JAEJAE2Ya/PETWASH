// PETWASH - 프론트엔드 (바닐라 JS + Chart.js)

const MAX_POINTS = 300; // 그래프에 남길 점 개수 (2초 간격 → 약 10분)
const $ = (id) => document.getElementById(id);
const STREAMS = ["contaminated", "clean"]; // 선별 결과 = 저장함 = 레시피 키
const STREAM_NAME = { contaminated: "오염", clean: "비오염" };

const app = {
  recipes: null,
  sensor: null,
  deviceStatus: null,
  deviceOnline: false,
  mqttConnected: true,
  influxError: null,
  wsOpen: false,
  lot: null,    // 선별 중인 수거 배치
  bins: null,   // 저장함 {contaminated: {n, threshold}, clean: {...}}
  wash: null,   // 세척 중인 세척 배치
  recommendation: null,
  lastSetpoint: null,
};

// 작업자가 입력한 글자(메모, 레시피 이름)를 HTML에 넣기 전에 바꿔 둔다
function esc(s) {
  return String(s ?? "").replace(/[&<>"']/g, (c) =>
    ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
}

const recipeName = (k) =>
  (app.recipes && app.recipes.recipes[k] ? app.recipes.recipes[k].name : k ?? "-");

// ---------------- 탭 전환 ----------------
const tabs = [...document.querySelectorAll(".tab")];
function showTab(btn) {
  tabs.forEach((b) => b.classList.toggle("active", b === btn));
  document.querySelectorAll(".page").forEach((p) =>
    p.classList.toggle("active", p.id === "tab-" + btn.dataset.tab));
}
tabs.forEach((btn) => btn.addEventListener("click", () => showTab(btn)));

// 바이오스처럼 숫자키 1~5로 화면 이동 (입력칸에 글자를 치는 중에는 동작하지 않음)
document.addEventListener("keydown", (e) => {
  const typing = ["INPUT", "SELECT", "TEXTAREA"].includes(document.activeElement.tagName)
    && document.activeElement.type !== "range";
  if (typing || e.ctrlKey || e.altKey || e.metaKey) return;
  const n = Number(e.key);
  if (n >= 1 && n <= tabs.length) {
    showTab(tabs[n - 1]);
    window.scrollTo(0, 0);
  }
});

// 제목 줄 시계
function tickClock() {
  const d = new Date();
  const p2 = (n) => String(n).padStart(2, "0");
  $("clock").textContent = `${d.getFullYear()}-${p2(d.getMonth() + 1)}-${p2(d.getDate())} ` +
    `${p2(d.getHours())}:${p2(d.getMinutes())}:${p2(d.getSeconds())}`;
}
tickClock();
setInterval(tickClock, 1000);

// ---------------- 그래프 ----------------
// 화면에 맞춘 그래프 기본값: 픽셀 글꼴, 진회색 글자, 흐린 격자
Chart.defaults.font.family = "Galmuri11, DotumChe, monospace";
Chart.defaults.font.size = 12;
Chart.defaults.color = "#444444";
Chart.defaults.borderColor = "rgba(0, 0, 0, 0.1)";

function makeChart(canvasId, color) {
  return new Chart($(canvasId), {
    type: "line",
    data: {
      labels: [],
      datasets: [
        { label: "측정값", data: [], borderColor: color, backgroundColor: color,
          borderWidth: 2, pointRadius: 0, tension: 0 },
        { label: "목표", data: [], borderColor: "#000000", backgroundColor: "#000000", borderDash: [4, 4],
          borderWidth: 1, pointRadius: 0, spanGaps: true },
      ],
    },
    options: {
      animation: false,
      maintainAspectRatio: false,
      interaction: { mode: "index", intersect: false },
      scales: { x: { ticks: { maxTicksLimit: 4, autoSkip: true, maxRotation: 0 } } },
      plugins: {
        legend: { position: "bottom", labels: { boxWidth: 12, boxHeight: 2 } },
        tooltip: { backgroundColor: "#0000aa", titleColor: "#ffffff", bodyColor: "#ffffff",
                   cornerRadius: 0, borderColor: "#ffffff", borderWidth: 1 },
      },
    },
  });
}

const charts = {
  conc: makeChart("c-conc", "#0000aa"), // 파랑
  temp: makeChart("c-temp", "#c40000"), // 빨강
  turb: makeChart("c-turb", "#a85a00"), // 갈색
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
    const scale = s.time_scale || 1;
    if (scale > 1) msg += ` (실제 약 ${fmtDuration(s.remaining_s / scale)})`;
  }
  $("v-device-msg").textContent = msg;
}

function renderCards() {
  const lot = app.lot;
  $("v-lot").textContent = lot ? lot.lot_id : "없음";
  $("v-lot-sub").textContent = lot
    ? `판정 ${lot.total_n}병 · 오염 비율 ${lot.ratio_pct == null ? "-" : lot.ratio_pct + "%"}` : "";
  if (app.bins) {
    const c = app.bins.contaminated, n = app.bins.clean;
    $("v-bins").textContent = `${c.n} / ${n.n}`;
    $("v-bins-sub").textContent = `임계치 ${c.threshold} / ${n.threshold}병`;
  }
  const w = app.wash;
  $("v-wash").textContent = w ? w.batch_id : "없음";
  $("v-wash-sub").textContent = w ? `${STREAM_NAME[w.stream]} ${w.bottle_n}병 · ${recipeName(w.recipe)} 레시피` : "";
}

function renderAlerts() {
  const list = [];
  if (!app.wsOpen) list.push(["bad", "서버와 연결이 끊겼습니다. 다시 연결하는 중…"]);
  else {
    if (!app.deviceOnline) list.push(["bad", "장치 연결 끊김: 15초 이상 하트비트가 없습니다"]);
    if (!app.mqttConnected) list.push(["bad", "서버가 MQTT 브로커에 연결되지 않았습니다"]);
    if (app.influxError) list.push(["warn", "InfluxDB 저장 문제: " + esc(app.influxError)]);
    // 추천 중 "주의(alert)" 항목은 대시보드에도 경고로 띄운다 (탁도 초과, 선별 오류 의심 등)
    const r = app.recommendation;
    if (r && r.available)
      r.items.filter((i) => i.level === "alert").forEach((i) => list.push(["warn", `${esc(i.action)} — ${esc(i.reason)}`]));
    else if (app.sensor && app.recipes && app.sensor.turbidity_ntu > app.recipes.turbidity_limit_ntu)
      list.push(["warn", `탁도 ${app.sensor.turbidity_ntu.toFixed(1)} NTU: 교체 기준(${app.recipes.turbidity_limit_ntu}) 초과`]);
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
  // 레시피 버튼
  $("preset-row").innerHTML = STREAMS
    .map((k) => `<button data-recipe="${k}">${esc(recipeName(k))} 레시피</button>`).join("");
  $("preset-row").querySelectorAll("button").forEach((b) =>
    (b.onclick = () => fillSliders(app.recipes.recipes[b.dataset.recipe])));
  fillSliders(app.recipes.recipes[app.wash ? app.wash.recipe : "clean"]);
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
  return `<tr><th>세척 배치</th><td>${esc(sp.batch_id ?? "-")}</td></tr>
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
      (sp.result_message ? `<tr><th>장치 메시지</th><td>${esc(sp.result_message)}</td></tr>` : "");
  }
  const st = app.deviceStatus;
  if (!app.deviceOnline) {
    $("applied-sp").innerHTML = `<tr><td class="st-bad">장치 연결 끊김</td></tr>`;
  } else if (!st || !st.applied) {
    $("applied-sp").innerHTML = `<tr><td>적용 중인 목표값 없음 (${st ? esc(st.state) : "-"})</td></tr>`;
  } else {
    $("applied-sp").innerHTML = spRows(st.applied) + `<tr><th>장치 상태</th><td>${esc(st.state)}</td></tr>`;
  }
}

// 되돌리기 어려운 버튼: 첫 클릭은 확인 문구로 바뀌고, 4초 안에 한 번 더 눌러야 실행된다.
// (브라우저 confirm() 팝업은 일부 내장 브라우저에서 막혀서 항상 "취소"로 처리되므로 쓰지 않는다)
function confirmButton(btn, confirmText, action) {
  const original = btn.textContent;
  let timer = null;
  btn.onclick = () => {
    if (!timer) {
      btn.textContent = confirmText;
      btn.classList.add("confirming");
      timer = setTimeout(reset, 4000);
      return;
    }
    reset();
    action();
  };
  function reset() {
    clearTimeout(timer);
    timer = null;
    btn.textContent = original;
    btn.classList.remove("confirming");
  }
}

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

// 자주 오는 알림(카메라 판정은 0.3초마다)에 목록을 매번 다시 불러오지 않도록 묶어서 한 번만
function throttle(fn, ms) {
  let timer = null;
  return () => {
    if (timer) return;
    timer = setTimeout(() => { timer = null; fn(); }, ms);
  };
}

// ---------------- 배치 관리: 선별 중 수거 배치 ----------------
let renderedLotId; // 현재 그려 둔 수거 배치 (바뀔 때만 구조를 새로 그림, 확인 버튼 상태 유지)

function renderLot() {
  const lot = app.lot;
  const box = $("cur-lot");
  const id = lot ? lot.lot_id : null;
  if (id !== renderedLotId) {
    renderedLotId = id;
    if (!lot) {
      box.innerHTML = `<p class="hint">선별 중인 수거 배치가 없습니다. 아래 수거 배치 목록에서 만들고 시작하세요.</p>`;
    } else {
      box.innerHTML = `
        <table class="kv">
          <tr><th>수거 배치</th><td><b>${esc(lot.lot_id)}</b></td></tr>
          <tr><th>메모</th><td>${esc(lot.note) || "-"}</td></tr>
          <tr><th>선별 시작</th><td>${fmtTime(lot.started_at)}</td></tr>
          <tr><th>카메라</th><td id="l-remain">-</td></tr>
        </table>
        <div class="counts">
          <div class="count-box"><div class="n" id="l-cont">0</div><div class="l">오염 → 오염 저장함</div></div>
          <div class="count-box"><div class="n" id="l-clean">0</div><div class="l">비오염 → 비오염 저장함</div></div>
          <div class="count-box"><div class="n" id="l-ratio">-</div><div class="l">오염 비율</div></div>
        </div>
        <div class="ratio-bar"><div id="l-bar" style="width:0%"></div></div>
        <div class="btn-row"><button id="btn-lot-end" class="danger">선별 종료 (수동)</button></div>`;
      confirmButton($("btn-lot-end"), "한 번 더 누르면 선별 종료 (카메라 정지)",
        () => api("POST", `/api/lots/${encodeURIComponent(lot.lot_id)}/end`, null, "err-lot"));
    }
  }
  if (!lot) return;
  $("l-cont").textContent = lot.contaminated_n;
  $("l-clean").textContent = lot.clean_n;
  $("l-ratio").textContent = lot.ratio_pct == null ? "-" : lot.ratio_pct + "%";
  $("l-bar").style.width = (lot.ratio_pct || 0) + "%";
  const st = app.deviceStatus;
  $("l-remain").textContent = !st || st.lot_id !== lot.lot_id ? "장치 응답 대기"
    : st.lot_remaining > 0 ? `판정 중 · 남은 병 ${st.lot_remaining}개` : "판정 끝";
}

// ---------------- 배치 관리: 선별 저장함 ----------------
let binsBuilt = false;

function renderBins() {
  if (!app.bins) return;
  if (!binsBuilt) {
    $("bins").innerHTML = STREAMS.map((s) => `
      <div class="bin-box">
        <div class="l">${STREAM_NAME[s]} 저장함</div>
        <div class="n"><span id="bin-n-${s}">0</span><span class="of"> / <span id="bin-t-${s}">-</span>병</span></div>
        <div class="ratio-bar ${s}"><div id="bin-bar-${s}" style="width:0%"></div></div>
        <button id="btn-bundle-${s}">지금 묶기</button>
      </div>`).join("");
    STREAMS.forEach((s) => confirmButton($(`btn-bundle-${s}`), "한 번 더 누르면 세척 배치로 묶음",
      () => api("POST", `/api/bins/${s}/bundle`, null, "err-bins")));
    binsBuilt = true;
  }
  for (const s of STREAMS) {
    const b = app.bins[s];
    $(`bin-n-${s}`).textContent = b.n;
    $(`bin-t-${s}`).textContent = b.threshold;
    $(`bin-bar-${s}`).style.width = Math.min(100, (b.n / b.threshold) * 100) + "%";
    $(`btn-bundle-${s}`).disabled = b.n === 0;
  }
}

// ---------------- 배치 관리: 세척 중 배치 ----------------
let renderedWashId;

function renderWash() {
  const w = app.wash;
  const box = $("cur-wash");
  const id = w ? w.batch_id : null;
  if (id !== renderedWashId) {
    renderedWashId = id;
    if (!w) {
      box.innerHTML = `<p class="hint">세척 중인 배치가 없습니다. 아래 세척 배치 목록에서 대기 중인 배치를 시작하세요.</p>`;
    } else {
      const options = STREAMS.map((k) => `<option value="${k}">${esc(recipeName(k))} 레시피</option>`).join("");
      const sources = w.sources.map((s) => `${esc(s.lot_id)} (${s.n}병)`).join(", ") || "-";
      box.innerHTML = `
        <table class="kv">
          <tr><th>세척 배치</th><td><b>${esc(w.batch_id)}</b></td></tr>
          <tr><th>구분</th><td>${STREAM_NAME[w.stream]} 저장함 · ${w.bottle_n}병</td></tr>
          <tr><th>출처 수거 배치</th><td>${sources}</td></tr>
          <tr><th>세척 시작</th><td>${fmtTime(w.started_at)}</td></tr>
          <tr><th>적용 레시피</th><td id="w-recipe">-</td></tr>
        </table>
        <div class="grade-row">
          <label for="w-recipe-sel">레시피 변경</label>
          <select id="w-recipe-sel">${options}</select>
          <button id="btn-recipe" class="primary">변경</button>
        </div>
        <div class="btn-row"><button id="btn-wash-end" class="danger">세척 배치 종료</button></div>`;
      $("w-recipe-sel").value = w.recipe;
      $("btn-recipe").onclick = () => api("POST", `/api/wash_batches/${encodeURIComponent(w.batch_id)}/recipe`,
        { recipe: $("w-recipe-sel").value }, "err-wash");
      confirmButton($("btn-wash-end"), "한 번 더 누르면 종료 (펌프·히터 정지)",
        () => api("POST", `/api/wash_batches/${encodeURIComponent(w.batch_id)}/end`, null, "err-wash"));
    }
  }
  if (!w) return;
  $("w-recipe").innerHTML = `${esc(recipeName(w.recipe))} 레시피` +
    (w.recipe !== w.stream ? ` <span class="warn-text">(저장함 구분과 다르게 변경됨)</span>` : "");
}

// ---------------- 배치 관리: 목록 ----------------
async function loadWashBatches() {
  const rows = await (await fetch("/api/wash_batches")).json();
  $("wash-list").innerHTML = rows.length === 0
    ? `<tr><td colspan="10">세척 배치가 없습니다. 저장함이 임계치에 도달하면 만들어집니다.</td></tr>`
    : rows.map((b) => `<tr>
        <td>${esc(b.batch_id)}</td>
        <td class="${b.status === "세척 중" ? "st-ok" : ""}">${esc(b.status)}</td>
        <td>${STREAM_NAME[b.stream] ?? esc(b.stream)}</td>
        <td>${b.bottle_n}</td>
        <td>${esc(b.sources || "-")}</td>
        <td>${esc(recipeName(b.recipe))}</td>
        <td>${fmtTime(b.created_at)} <span class="hint">(${b.created_by === "auto" ? "임계치" : "지금 묶기"})</span></td>
        <td>${fmtTime(b.started_at)}</td>
        <td>${fmtTime(b.ended_at)}</td>
        <td>${b.status === "대기" ? `<button data-wash="${esc(b.batch_id)}">세척 시작</button>` : ""}</td>
      </tr>`).join("");
  $("wash-list").querySelectorAll("[data-wash]").forEach((btn) =>
    (btn.onclick = () => api("POST", `/api/wash_batches/${encodeURIComponent(btn.dataset.wash)}/start`, null, "err-washlist")));
}

async function loadLots() {
  const rows = await (await fetch("/api/lots")).json();
  $("lot-list").innerHTML = rows.length === 0
    ? `<tr><td colspan="8">수거 배치가 없습니다</td></tr>`
    : rows.map((l) => `<tr>
        <td>${esc(l.lot_id)}</td>
        <td class="${l.status === "선별 중" ? "st-ok" : ""}">${esc(l.status)}</td>
        <td>${esc(l.note)}</td>
        <td>${l.contaminated_n} / ${l.clean_n}</td>
        <td>${l.ratio_pct == null ? "-" : l.ratio_pct + "%"}</td>
        <td>${fmtTime(l.started_at)}</td>
        <td>${fmtTime(l.ended_at)}</td>
        <td>${l.status === "생성됨" ? `<button data-start="${esc(l.lot_id)}">선별 시작</button>` : ""}</td>
      </tr>`).join("");
  $("lot-list").querySelectorAll("[data-start]").forEach((btn) =>
    (btn.onclick = () => api("POST", `/api/lots/${encodeURIComponent(btn.dataset.start)}/start`, null, "err-lotlist")));
}

const scheduleLoadLots = throttle(loadLots, 1500);
const scheduleLoadWash = throttle(loadWashBatches, 300);

$("btn-new-lot").onclick = async () => {
  const lot = await api("POST", "/api/lots", { note: $("new-note").value.trim() }, "err-lotlist");
  if (lot) { $("new-note").value = ""; loadLots(); }
};

// 세척 배치의 레시피가 정해지거나 바뀌면 세척 제어 슬라이더를 그 레시피로 채워 둔다
let filledRecipe = null;
function syncSlidersToRecipe() {
  const k = app.wash ? app.wash.recipe : null;
  if (k != null && k !== filledRecipe && app.recipes && slidersReady) {
    fillSliders(app.recipes.recipes[k]);
  }
  filledRecipe = k;
}

// ---------------- 추천 조치 ----------------
function renderRecommendation() {
  const r = app.recommendation;
  if (!r || !r.available) {
    $("rec-head").textContent = r ? r.message : "-";
    $("rec-items").innerHTML = "";
    $("btn-approve").disabled = true;
    return;
  }
  const t = r.target;
  // 승인 버튼: 이미 장치가 이 목표값으로 제어 중이면 "적용됨"으로 잠근다
  const btn = $("btn-approve");
  if (r.applied) {
    app.approving = false;
    btn.disabled = true;
    btn.className = "done";
    btn.textContent = "✓ 장치에 적용됨 · 이 목표값으로 제어 중";
  } else if (app.approving) {
    btn.disabled = true;
    btn.className = "primary";
    btn.textContent = "전송 중… 장치 응답 대기";
  } else {
    btn.disabled = false;
    btn.className = "primary";
    btn.textContent = "추천 승인 (레시피 목표값 전송)";
  }
  $("rec-head").innerHTML = `세척 배치 <b>${esc(r.batch_id)}</b> (${STREAM_NAME[r.stream]} 저장함) · ` +
    `${esc(r.recipe_name)} 레시피 목표: ` +
    `농도 ${t.target_concentration_pct}% · 온도 ${t.target_temp_c}°C · 세척 ${t.wash_time_min}분`;
  $("rec-items").innerHTML = r.items
    .map((i) => `<li class="${i.level}"><div class="act">${esc(i.action)}</div><div class="why">근거: ${esc(i.reason)}</div></li>`)
    .join("");
}

async function loadDecisions() {
  const rows = await (await fetch("/api/decisions?limit=8")).json();
  const v = (x, unit) => (x == null ? "-" : x + unit);
  $("decision-list").innerHTML = rows.length === 0
    ? `<tr><td colspan="6">아직 결정 이력이 없습니다</td></tr>`
    : rows.map((d) => `<tr>
        <td>${fmtTime(d.ts)}</td><td>${esc(d.batch_id ?? "-")}</td><td>${d.recipe ? esc(recipeName(d.recipe)) : "-"}</td>
        <td>${esc(d.recommended ?? "-")}</td><td>${esc(d.decision)}</td>
        <td>${d.applied_concentration_pct == null ? "-" :
          `${v(d.applied_concentration_pct, "%")} / ${v(d.applied_temp_c, "°C")} / ${v(d.applied_wash_time_min, "분")}`}</td>
      </tr>`).join("");
}

$("btn-approve").onclick = async () => {
  app.approving = true;
  renderRecommendation();
  const ok = await api("POST", "/api/recommendation/approve", null, "rec-error");
  if (!ok) { app.approving = false; renderRecommendation(); return; }
  // 10초 안에 장치 응답이 없으면 버튼을 다시 풀어 준다 (장치가 꺼져 있는 경우 등)
  setTimeout(() => {
    if (app.approving) {
      app.approving = false;
      renderRecommendation();
      showToast("장치 응답이 없습니다. 장치 연결 상태를 확인하세요.", "bad");
    }
  }, 10000);
};

// 화면 위쪽에 잠깐 나타나는 알림 (브라우저 팝업 대신 사용)
let toastTimer = null;
function showToast(text, kind = "ok") {
  const el = $("toast");
  el.textContent = text;
  el.className = `toast ${kind} show`;
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => el.classList.remove("show"), 3500);
}
confirmButton($("btn-water"), "한 번 더 누르면 교체 (농도·온도가 내려감)",
  () => api("POST", "/api/device/replace_water", null, "rec-error"));

// ---------------- 이력 ----------------
function fmtDuration(s) {
  if (s == null) return "-";
  const m = Math.floor(s / 60);
  return `${m}분 ${Math.round(s % 60)}초`;
}

async function loadBatchHistory() {
  const rows = await (await fetch("/api/history")).json();
  const n = (x, d, unit = "") => (x == null ? "-" : Number(x).toFixed(d) + unit);
  $("history-list").innerHTML = rows.length === 0
    ? `<tr><td colspan="12">종료된 세척 배치가 없습니다</td></tr>`
    : rows.map((r) => `<tr>
        <td>${esc(r.batch_id)}</td>
        <td>${fmtTime(r.ended_at)}</td>
        <td>${esc(r.stream_name)} · ${r.bottle_n}병</td>
        <td>${esc(r.sources || "-")}</td>
        <td>${esc(r.recipe_name)}</td>
        <td>${r.applied_concentration_pct == null ? "적용 없음"
          : `${r.applied_concentration_pct}% · ${r.applied_temp_c}°C · ${r.applied_wash_time_min}분`}</td>
        <td>${n(r.avg_concentration_pct, 2, "%")}</td>
        <td>${n(r.avg_temp_c, 1, "°C")}</td>
        <td>${n(r.avg_turbidity_ntu, 1)} / ${n(r.max_turbidity_ntu, 1)}</td>
        <td>${fmtDuration(r.pump_on_s)}</td>
        <td>${fmtDuration(r.heater_on_s)}</td>
        <td>${r.samples == null ? `<button data-sum="${esc(r.batch_id)}">요약 계산</button>` : ""}</td>
      </tr>`).join("");
  $("history-list").querySelectorAll("[data-sum]").forEach((b) =>
    (b.onclick = async () => {
      await api("POST", `/api/wash_batches/${encodeURIComponent(b.dataset.sum)}/summary`);
      loadBatchHistory();
    }));
}

// ---------------- 설정 ----------------
function fillSettings(r) {
  $("set-recipes").innerHTML = STREAMS.map((k) => {
    const x = r.recipes[k];
    return `<tr data-recipe="${k}">
      <td>${STREAM_NAME[k]} 저장함</td>
      <td><input class="name" data-k="name" value="${esc(x.name)}"></td>
      <td><input type="number" step="0.1" data-k="concentration_pct" value="${x.concentration_pct}"></td>
      <td><input type="number" step="1" data-k="temp_c" value="${x.temp_c}"></td>
      <td><input type="number" step="1" data-k="wash_time_min" value="${x.wash_time_min}"></td>
      <td><input type="number" step="0.01" data-k="expected_decay_pct_per_min" value="${x.expected_decay_pct_per_min}"></td>
    </tr>`;
  }).join("");
  $("set-bin-c").value = r.bin_threshold.contaminated;
  $("set-bin-n").value = r.bin_threshold.clean;
  $("set-tol-c").value = r.tolerance.concentration_pct;
  $("set-tol-t").value = r.tolerance.temp_c;
  $("set-turb").value = r.turbidity_limit_ntu;
  $("set-decay").value = r.decay_alert_factor;
  const s = r.safety;
  $("set-safety").textContent = `농도 ${s.concentration_pct[0]}~${s.concentration_pct[1]}% · ` +
    `온도 ${s.temp_c[0]}~${s.temp_c[1]}°C · 세척 시간 ${s.wash_time_min[0]}~${s.wash_time_min[1]}분`;
}

function readSettings() {
  const recipes = {};
  document.querySelectorAll("#set-recipes tr").forEach((tr) => {
    const g = {};
    tr.querySelectorAll("input").forEach((i) => (g[i.dataset.k] = i.dataset.k === "name" ? i.value.trim() : Number(i.value)));
    recipes[tr.dataset.recipe] = g;
  });
  return {
    recipes,
    bin_threshold: { contaminated: Number($("set-bin-c").value), clean: Number($("set-bin-n").value) },
    tolerance: { concentration_pct: Number($("set-tol-c").value), temp_c: Number($("set-tol-t").value) },
    turbidity_limit_ntu: Number($("set-turb").value),
    decay_alert_factor: Number($("set-decay").value),
  };
}

$("btn-save-settings").onclick = async () => {
  $("settings-msg").textContent = "";
  const r = await api("PUT", "/api/settings", readSettings(), "settings-error");
  if (r) $("settings-msg").textContent = "저장했습니다.";
};
$("btn-reset-settings").onclick = () => {
  $("settings-error").textContent = "";
  $("settings-msg").textContent = "";
  if (app.recipes) fillSettings(app.recipes);
};

// ---------------- (테스트) 시연용 가속 ----------------
// 실제 공정은 세척에 10~20분이 걸리므로, 시연 때는 시뮬레이터 안의 시간을 10배 빠르게 돌린다.
function currentSpeed() {
  return (app.deviceStatus && app.deviceStatus.time_scale) || 1;
}

function renderSpeed() {
  const fast = currentSpeed() > 1;
  $("btn-speed").textContent = fast ? "실제 시간으로 되돌리기" : "(테스트) 10배 가속";
  $("btn-speed").classList.toggle("on", fast);
  $("speed-badge").hidden = !fast;
}

$("btn-speed").onclick = () => api("POST", "/api/device/speed", { speed: currentSpeed() > 1 ? 1 : 10 });

function renderAll() {
  renderSensor();
  renderSpeed();
  renderRecommendation();
  renderDevice();
  renderCards();
  renderAlerts();
  renderControl();
  renderLot();
  renderBins();
  renderWash();
  syncSlidersToRecipe();
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
        app.lot = msg.data.lot;
        app.bins = msg.data.bins;
        app.wash = msg.data.wash;
        app.recommendation = msg.data.recommendation;
        renderedLotId = renderedWashId = undefined; // 다시 연결되면 화면 구조를 새로 그린다
        loadLots();
        loadWashBatches();
        loadDecisions();
        loadBatchHistory();
        fillSettings(app.recipes);
        break;
      case "history":
        loadBatchHistory();
        break;
      case "recipes":
        app.recipes = msg.data;
        slidersReady = false;        // 레시피 버튼·슬라이더를 새 이름·값으로 다시 만든다
        renderedWashId = undefined;  // 레시피 선택 상자의 이름도 새로
        fillSettings(app.recipes);
        loadWashBatches();
        break;
      case "recommendation":
        app.recommendation = msg.data;
        break;
      case "decision":
        loadDecisions();
        break;
      case "lot":
        app.lot = msg.data;
        scheduleLoadLots();
        break;
      case "lots":
        scheduleLoadLots();
        break;
      case "bins":
        app.bins = msg.data;
        break;
      case "wash":
        app.wash = msg.data;
        scheduleLoadWash();
        break;
      case "wash_batches":
        scheduleLoadWash();
        break;
      case "setpoint":
        app.lastSetpoint = msg.data;
        // 장치가 응답하면 결과를 알림으로 보여 준다
        if (msg.data.result === "적용됨") {
          showToast(`장치에 적용되었습니다: 농도 ${msg.data.target_concentration_pct}% · ${msg.data.target_temp_c}°C · ${msg.data.wash_time_min}분`);
        } else if (msg.data.result === "거부됨") {
          app.approving = false;
          showToast("장치가 목표값을 거부했습니다: " + msg.data.result_message, "bad");
        }
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
