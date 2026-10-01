"use strict";

const API = "/api/v1";
const $ = (id) => document.getElementById(id);
const LANGS = { en: "English", ur: "Urdu", hi: "Hindi", ar: "Arabic", fa: "Persian", pa: "Punjabi", ps: "Pashto", roman_ur: "Roman Urdu" };
const ENV_NAMES = { sudden_sound: "Sudden sound", raised_voice: "Raised voice", ambient_shift: "Ambient change", input_silent: "Input silent" };

const store = {
  get(k) { try { return localStorage.getItem(k); } catch { return null; } },
  set(k, v) { try { localStorage.setItem(k, v); } catch { /* storage unavailable */ } },
};
const session = {
  get(k) { try { return sessionStorage.getItem(k); } catch { return null; } },
  set(k, v) { try { sessionStorage.setItem(k, v); } catch { /* storage unavailable */ } },
};

const state = {
  token: session.get("ag.token"),
  source: store.get("ag.source") || "browser",
  monitoring: false,
  runningSource: null,
  startedAt: null,
  lastSegment: null,
  languages: new Set(),
  sessionRows: 0,
  seenAlerts: new Set(),
  endpointDevices: [],
  browserDevices: [],
  permission: "unknown",
  policy: null,
  alerts: [],
};

/* ---------------------------------------------------------------- helpers */

function esc(s) {
  return String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
}
function isRtl(s) { return /[؀-ۿݐ-ݿﭐ-﷿ﹰ-﻿]/.test(s || ""); }
// digit runs inside right-to-left text keep the order they were spoken in
function bidi(s) { return esc(s).replace(/\d[\d\s\-.,/*]*\d|\d/g, (m) => `<bdi dir="ltr">${m}</bdi>`); }
function langName(c) { return LANGS[c] || (c ? c.toUpperCase() : "—"); }
function hhmmss(ts) { return new Date(ts * 1000).toLocaleTimeString([], { hour: "2-digit", minute: "2-digit", second: "2-digit" }); }
function sevBadge(sev) { return `<span class="sev sev-${esc(sev)}">${esc(sev)}</span>`; }
function secs(ms) { return ms == null ? "—" : `${(ms / 1000).toFixed(1)} s`; }

function toast(text, kind = "", action = null) {
  const el = document.createElement("div");
  el.className = `toast ${kind}`;
  el.innerHTML = `<span>${esc(text)}</span>`;
  if (action) {
    const b = document.createElement("button");
    b.textContent = action.label;
    b.onclick = () => { action.run(); el.remove(); };
    el.appendChild(b);
  }
  $("toasts").appendChild(el);
  setTimeout(() => el.remove(), action ? 12000 : 5000);
}

async function api(path, opts = {}) {
  const headers = { ...(opts.headers || {}) };
  if (state.token) headers.Authorization = `Bearer ${state.token}`;
  if (opts.json !== undefined) { headers["Content-Type"] = "application/json"; opts.body = JSON.stringify(opts.json); }
  const res = await fetch(API + path, { ...opts, headers });
  if (res.status === 401) { askToken(true); throw new Error("access token required"); }
  const body = (res.headers.get("content-type") || "").includes("json") ? await res.json() : await res.text();
  if (!res.ok && ![409, 428].includes(res.status)) throw new Error((body && (body.detail || body.reason)) || `HTTP ${res.status}`);
  return body;
}

/* ---------------------------------------------------------------- theme + routing */

function applyTheme(choice) {
  const dark = choice === "dark" || (choice === "system" && matchMedia("(prefers-color-scheme: dark)").matches);
  document.documentElement.dataset.theme = dark ? "dark" : "light";
  $("themeSelect").value = choice;
  store.set("ag.theme", choice);
}
applyTheme(store.get("ag.theme") || "light");
$("themeSelect").onchange = (e) => applyTheme(e.target.value);
matchMedia("(prefers-color-scheme: dark)").addEventListener("change", () => { if ($("themeSelect").value === "system") applyTheme("system"); });

const loaders = {};
function route() {
  const page = (location.hash || "#monitor").slice(1);
  document.querySelectorAll(".page").forEach((p) => { p.hidden = p.id !== `page-${page}`; });
  document.querySelectorAll(".nav a").forEach((a) => a.classList.toggle("active", a.dataset.page === page));
  if (loaders[page]) loaders[page]();
}
window.addEventListener("hashchange", route);

/* ---------------------------------------------------------------- token */

function askToken(wrong = false) {
  const d = $("tokenDialog");
  $("tokenError").hidden = !(wrong && state.token);
  if (!d.open) d.showModal();
}
$("tokenDialog").addEventListener("close", () => {
  const value = $("tokenInput").value.trim();
  if (!value) { setTimeout(() => askToken(), 50); return; }
  state.token = value;
  session.set("ag.token", value);
  $("tokenInput").value = "";
  if (live.ws) live.ws.close();
  boot();
});

/* ---------------------------------------------------------------- live feed (/ws) */

const live = { ws: null, retry: 1000 };

function connectLive() {
  const proto = location.protocol === "https:" ? "wss" : "ws";
  const q = state.token ? `?token=${encodeURIComponent(state.token)}` : "";
  const ws = new WebSocket(`${proto}://${location.host}${API}/ws${q}`);
  live.ws = ws;
  ws.onopen = () => { live.retry = 1000; setConn("live", "Connected"); };
  ws.onclose = (e) => {
    if (live.ws !== ws) return;
    setConn("down", "Disconnected");
    if (e.code === 4401) { askToken(true); return; }
    setTimeout(connectLive, live.retry);
    live.retry = Math.min(live.retry * 2, 15000);
  };
  ws.onmessage = (m) => {
    let ev;
    try { ev = JSON.parse(m.data); } catch { return; }
    if (handlers[ev.type]) handlers[ev.type](ev);
  };
}

function setConn(cls, text) {
  $("conn").className = `conn ${cls}`;
  $("connText").textContent = text;
}

const handlers = {
  hello(ev) { syncRunning(ev.monitoring_active, ev.source, ev.started_at); },
  status(ev) {
    if (ev.status === "idle") syncRunning(false);
    else if (ev.status === "listening" && ev.device) { syncRunning(true, ev.source); $("engInput").textContent = ev.device; }
    if (ev.status === "analysing") $("runLine").textContent = runText("analysing");
    if (ev.models_ready) toast("Speech models loaded.");
  },
  level(ev) {
    const pct = Math.max(0, Math.min(100, ((ev.db + 80) / 80) * 100));
    $("meterBar").style.width = `${pct}%`;
    $("meterBar").classList.toggle("hot", ev.peak > 0.98);
    $("meterDb").textContent = `${Math.round(ev.db)} dB`;
    if (ev.baseline_db != null) $("meterFloor").style.left = `${Math.max(0, Math.min(100, ((ev.baseline_db + 80) / 80) * 100))}%`;
    $("voiceDot").classList.toggle("on", !!ev.speech);
    $("mutedNotice").hidden = !ev.muted;
    $("envLevel").textContent = `${Math.round(ev.db)} dB`;
    if (ev.baseline_db != null) $("envFloor").textContent = `${Math.round(ev.baseline_db)} dB`;
  },
  transcript(ev) {
    state.lastSegment = ev.segment_id ?? state.lastSegment;
    showCurrent(ev);
    addSessionRow(ev);
    $("earlyWarning").hidden = true;
    if (state.monitoring) $("runLine").textContent = runText();
    if (ev.alert_id) refreshAlerts(true);
  },
  verified(ev) {
    const note = $("curNote");
    if (ev.segment_id != null && ev.segment_id !== state.lastSegment) {
      if (ev.upgraded) toast(`Second opinion raised an earlier sentence to ${ev.severity}.`, "err");
    } else if (ev.upgraded) {
      showCurrent(ev);
      note.hidden = false;
      note.className = "small note raised";
      note.textContent = `Second opinion (${ev.model}) raised this from ${ev.previous_severity} to ${ev.severity}.`;
    } else {
      note.hidden = false;
      note.className = "small note";
      note.textContent = `Second opinion (${ev.model}) agrees: ${ev.severity}.`;
    }
    if (ev.upgraded) { addSessionRow(ev, true); if (ev.alert_id) refreshAlerts(true); }
  },
  early_warning(ev) {
    const box = $("earlyWarning");
    box.hidden = false;
    box.innerHTML = `<strong>Heard while still speaking:</strong> <bdi>${esc(ev.matches.map((m) => m.phrase).join(", "))}</bdi> — ${esc(ev.category.replace(/_/g, " "))}, likely ${esc(ev.estimated_severity)}.`;
  },
  environment(ev) { addEnvRow(ev); if (ev.kind === "input_silent") $("mutedNotice").hidden = false; },
  hardware() { loadEndpointDevices(); },
};

/* ---------------------------------------------------------------- monitor view */

function runText(extra) {
  if (!state.monitoring) return "Not monitoring.";
  const src = state.runningSource === "browser" ? "this device's microphone" : "the endpoint microphone";
  const since = state.startedAt ? ` since ${new Date(state.startedAt * 1000).toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" })}` : "";
  return `Listening on ${src}${since}${extra ? ` · ${extra}` : ""}.`;
}

function syncRunning(on, source, startedAt) {
  const was = state.monitoring;
  state.monitoring = !!on;
  if (on) {
    state.runningSource = source || state.runningSource;
    state.startedAt = startedAt || state.startedAt || Date.now() / 1000;
  } else {
    state.runningSource = null;
    state.startedAt = null;
    if (was && capture.active) stopBrowserCapture();
    $("voiceDot").classList.remove("on");
    $("meterBar").style.width = "0";
    $("mutedNotice").hidden = true;
  }
  $("startBtn").hidden = state.monitoring;
  $("stopBtn").hidden = !state.monitoring;
  document.querySelectorAll("#sourceSeg input").forEach((i) => { i.disabled = state.monitoring; });
  $("runLine").textContent = runText();
}

function showCurrent(ev) {
  const t = $("curText");
  t.innerHTML = ev.transcript ? bidi(ev.transcript) : "—";
  t.dir = isRtl(ev.transcript) ? "rtl" : "auto";
  $("curTrans").hidden = !ev.translation;
  $("curTrans").textContent = ev.translation || "";
  const sev = $("curSev");
  sev.className = `sev sev-${ev.severity}`;
  sev.textContent = ev.severity;
  $("curScore").textContent = Number(ev.threat_score || 0).toFixed(2);
  const tr = ev.transcription || {};
  const lang = ev.language || tr.language;
  if (lang) state.languages.add(lang);
  $("curLang").textContent = lang ? `${langName(lang)}${tr.language_probability ? ` · ${Math.round(tr.language_probability * 100)}%` : ""}` : "";
  const meta = [];
  if (ev.latency_ms != null) meta.push(`result in ${secs(ev.latency_ms)}`);
  if (ev.review_only) meta.push("for human review");
  $("curMeta").textContent = meta.join(" · ");
  const rows = (ev.matches || []).slice(0, 8).map((m) =>
    `<tr><td><bdi>${bidi(m.phrase)}</bdi></td><td>${esc(m.category.replace(/_/g, " "))}</td><td class="muted">${esc(m.match || "exact")}</td></tr>`).join("");
  $("curMatches").hidden = !rows;
  $("curMatches").querySelector("tbody").innerHTML = rows;
  const note = $("curNote");
  if (ev.risk_factors && ev.risk_factors.length) { note.hidden = false; note.className = "small note"; note.textContent = ev.risk_factors.join("; "); }
  else if (ev.type === "transcript") note.hidden = true;
}

function addSessionRow(ev, verified = false) {
  const tbody = $("sessionTable").querySelector("tbody");
  if (!state.sessionRows) tbody.innerHTML = "";
  state.sessionRows++;
  const tr = document.createElement("tr");
  tr.className = "fresh";
  const text = ev.transcript || "";
  tr.innerHTML = `<td class="time">${hhmmss(ev.timestamp || Date.now() / 1000)}</td>
    <td>${sevBadge(ev.severity)}${verified ? '<span class="review">second opinion</span>' : ""}</td>
    <td>${esc((ev.language || "—").toUpperCase())}</td>
    <td dir="${isRtl(text) ? "rtl" : "auto"}">${bidi(text)}${ev.translation ? `<span class="sub" dir="ltr">${esc(ev.translation)}</span>` : ""}</td>`;
  tbody.prepend(tr);
  setTimeout(() => tr.classList.remove("fresh"), 50);
  while (tbody.children.length > 100) tbody.lastChild.remove();
}

/* ---------------------------------------------------------------- microphone selection */

const secureOk = Boolean(window.isSecureContext && navigator.mediaDevices && navigator.mediaDevices.getUserMedia);

function setSource(src) {
  state.source = src;
  store.set("ag.source", src);
  document.querySelector(`#sourceSeg input[value="${src}"]`).checked = true;
  $("autoSwitchWrap").hidden = src !== "browser";
  renderMicSelect();
  micNotice();
}
document.querySelectorAll("#sourceSeg input").forEach((i) => { i.onchange = () => setSource(i.value); });
$("autoSwitch").checked = store.get("ag.autoSwitch") !== "0";
$("autoSwitch").onchange = (e) => store.set("ag.autoSwitch", e.target.checked ? "1" : "0");

function micNotice() {
  const n = $("micNotice");
  n.className = "notice";
  $("allowBtn").hidden = true;
  if (state.source === "browser") {
    if (!secureOk) {
      n.hidden = false;
      n.className = "notice err";
      n.innerHTML = `Browsers only allow microphone access on secure pages. Open this dashboard at <code>https://${esc(location.host)}</code> (or on <code>localhost</code>), or switch to <strong>Endpoint microphone</strong>.`;
      return;
    }
    if (state.permission === "denied") {
      n.hidden = false;
      n.className = "notice err";
      n.innerHTML = "Microphone access is blocked for this site.<ol><li>Click the site settings icon at the left end of the address bar.</li><li>Set <strong>Microphone</strong> to <strong>Allow</strong>.</li><li>Reload this page.</li></ol>";
      return;
    }
    if (state.permission !== "granted") {
      n.hidden = false;
      n.textContent = "The browser will ask for permission to use the microphone. Allow it to see the microphones on this device by name.";
      $("allowBtn").hidden = false;
      return;
    }
    if (!state.browserDevices.length) {
      n.hidden = false;
      n.className = "notice warn";
      n.textContent = "No microphone found on this device. Plug one in; it will appear here automatically.";
      return;
    }
  } else if (!state.endpointDevices.length) {
    n.hidden = false;
    n.className = "notice warn";
    n.textContent = "The endpoint reports no microphone. It re-checks every few seconds.";
    return;
  }
  n.hidden = true;
}

function renderMicSelect() {
  const sel = $("micSelect");
  if (state.source === "browser") {
    const keep = sel.dataset.src === "browser" ? sel.value : (store.get("ag.browserMic") || "default");
    sel.innerHTML = state.browserDevices.length
      ? state.browserDevices.map((d, i) => `<option value="${esc(d.deviceId)}">${esc(d.label || `Microphone ${i + 1}`)}</option>`).join("")
      : '<option value="default">Default microphone</option>';
    sel.value = [...sel.options].some((o) => o.value === keep) ? keep : sel.options[0].value;
  } else {
    const keep = sel.dataset.src === "endpoint" ? sel.value : (store.get("ag.endpointMic") || "");
    sel.innerHTML = '<option value="">System default</option>' +
      state.endpointDevices.map((d) => `<option value="${esc(d.name)}">${esc(d.name)}${d.is_default ? " (default)" : ""}</option>`).join("");
    sel.value = [...sel.options].some((o) => o.value === keep) ? keep : "";
  }
  sel.dataset.src = state.source;
}
$("micSelect").onchange = (e) => {
  store.set(state.source === "browser" ? "ag.browserMic" : "ag.endpointMic", e.target.value);
  if (state.monitoring && state.runningSource === "browser") switchBrowserMic(e.target.value, "selected");
};

async function readPermission() {
  if (!secureOk) return;
  try {
    const p = await navigator.permissions.query({ name: "microphone" });
    state.permission = p.state;
    p.onchange = () => { state.permission = p.state; refreshBrowserDevices(); };
  } catch {
    state.permission = "unknown";
  }
}

async function refreshBrowserDevices(announce = false) {
  if (!secureOk) { micNotice(); return; }
  const before = new Map(state.browserDevices.map((d) => [d.deviceId, d.label]));
  const all = await navigator.mediaDevices.enumerateDevices();
  state.browserDevices = all.filter((d) => d.kind === "audioinput");
  if (state.browserDevices.some((d) => d.label)) state.permission = "granted";
  if (announce && before.size) {
    const added = state.browserDevices.filter((d) => !before.has(d.deviceId) && d.deviceId !== "default" && d.deviceId !== "communications");
    const removed = [...before.keys()].filter((id) => !state.browserDevices.some((d) => d.deviceId === id));
    for (const d of added) {
      const name = d.label || "a new microphone";
      if (state.monitoring && state.runningSource === "browser" && $("autoSwitch").checked) {
        switchBrowserMic(d.deviceId, "connected");
      } else {
        toast(`Microphone connected: ${name}`, "", { label: "Use it", run: () => { $("micSelect").value = d.deviceId; $("micSelect").onchange({ target: $("micSelect") }); } });
      }
    }
    if (capture.active && removed.includes(capture.deviceId)) {
      toast(`${before.get(capture.deviceId) || "The microphone"} was disconnected. Using the default microphone.`, "err");
      switchBrowserMic("default", "fallback");
    }
  }
  if (state.source === "browser") renderMicSelect();
  micNotice();
}
if (secureOk) navigator.mediaDevices.addEventListener("devicechange", () => refreshBrowserDevices(true));

$("allowBtn").onclick = async () => {
  try {
    const s = await navigator.mediaDevices.getUserMedia({ audio: true });
    s.getTracks().forEach((t) => t.stop());
    state.permission = "granted";
  } catch (e) {
    if (e.name === "NotAllowedError") state.permission = "denied";
    else toast(`Could not open the microphone: ${e.message}`, "err");
  }
  refreshBrowserDevices();
};

async function loadEndpointDevices() {
  try {
    const c = await api("/capabilities");
    state.endpointDevices = c.hardware.devices || [];
    $("engKeywords").textContent = `${c.keyword_registry.phrases} phrases, ${c.keyword_registry.languages.length} languages`;
    if (state.source === "endpoint") renderMicSelect();
    micNotice();
  } catch { /* shown by api() */ }
}

/* ---------------------------------------------------------------- browser capture + streaming */

const capture = { active: false, ctx: null, node: null, src: null, stream: null, ws: null, deviceId: null };

function micConstraints(deviceId) {
  const audio = { channelCount: 1, echoCancellation: true, noiseSuppression: false, autoGainControl: true };
  if (deviceId && deviceId !== "default") audio.deviceId = { exact: deviceId };
  return { audio };
}

async function openMic(deviceId) {
  const stream = await navigator.mediaDevices.getUserMedia(micConstraints(deviceId));
  state.permission = "granted";
  return stream;
}

// The audio context runs at the microphone's own rate: Firefox refuses to connect a microphone
// to a context with a different sample rate. The worklet converts to 16 kHz itself.
async function makeContext(stream, rate) {
  const ctx = rate ? new AudioContext({ sampleRate: rate }) : new AudioContext();
  try {
    return { ctx, src: ctx.createMediaStreamSource(stream) };
  } catch (e) {
    ctx.close().catch(() => {});
    throw e;
  }
}

async function buildGraph(stream, onFrame) {
  const track = stream.getAudioTracks()[0];
  const rate = track && track.getSettings ? track.getSettings().sampleRate : undefined;
  let made;
  try {
    made = await makeContext(stream, rate);
  } catch {
    made = await makeContext(stream, undefined);
  }
  const { ctx, src } = made;
  await ctx.audioWorklet.addModule("/static/capture-worklet.js?v=2.1.3");
  const node = new AudioWorkletNode(ctx, "pcm16k-capture");
  node.port.onmessage = (e) => onFrame(e.data);
  const sink = ctx.createGain();
  sink.gain.value = 0;
  src.connect(node);
  node.connect(sink);
  sink.connect(ctx.destination);
  if (ctx.state === "suspended") await ctx.resume();
  return { ctx, node, src };
}

function trackLabel(stream) {
  const t = stream.getAudioTracks()[0];
  return (t && t.label) || "Browser microphone";
}

async function startBrowserCapture(operator) {
  const deviceId = $("micSelect").value || "default";
  let stream;
  try {
    stream = await openMic(deviceId);
  } catch (e) {
    if (e.name === "NotAllowedError") { state.permission = "denied"; micNotice(); throw new Error("microphone permission was denied"); }
    if (e.name === "NotFoundError" || e.name === "OverconstrainedError") throw new Error("that microphone is not available any more");
    throw e;
  }
  await refreshBrowserDevices();

  const proto = location.protocol === "https:" ? "wss" : "ws";
  const q = state.token ? `?token=${encodeURIComponent(state.token)}` : "";
  const ws = new WebSocket(`${proto}://${location.host}${API}/audio${q}`);
  ws.binaryType = "arraybuffer";
  capture.ws = ws;

  let started;
  try {
    started = await new Promise((resolve, reject) => {
      ws.onopen = () => ws.send(JSON.stringify({ type: "start", consent_acknowledged: true, operator, label: trackLabel(stream) }));
      ws.onmessage = (m) => {
        const msg = JSON.parse(m.data);
        if (msg.type === "started") resolve(msg);
        else if (msg.type === "error") reject(new Error((msg.reason || msg.status || "").replace(/_/g, " ")));
      };
      ws.onclose = (e) => reject(new Error(e.code === 4401 ? "access token required" : "the server closed the audio connection"));
    });
  } catch (err) {
    stream.getTracks().forEach((t) => t.stop());
    throw err;
  }

  const onFrame = (buf) => {
    if (ws.readyState === 1 && ws.bufferedAmount < 1000000) ws.send(buf);
  };
  const graph = await buildGraph(stream, onFrame);
  Object.assign(capture, { active: true, stream, deviceId, onFrame, ...graph });
  ws.onmessage = (m) => { const msg = JSON.parse(m.data); if (msg.type === "stopped") stopBrowserCapture(); };
  ws.onclose = () => {
    if (capture.active) { toast("The audio connection to the server was lost.", "err"); stopBrowserCapture(); syncRunning(false); }
  };
  return started;
}

async function switchBrowserMic(deviceId, why) {
  if (!capture.active) return;
  try {
    const stream = await openMic(deviceId);
    // a new microphone can run at a different rate, so it gets its own audio graph
    const graph = await buildGraph(stream, capture.onFrame);
    const old = { ctx: capture.ctx, stream: capture.stream };
    Object.assign(capture, { stream, deviceId, ...graph });
    old.stream.getTracks().forEach((t) => t.stop());
    old.ctx.close().catch(() => {});
    const label = trackLabel(stream);
    if (capture.ws && capture.ws.readyState === 1) capture.ws.send(JSON.stringify({ type: "device", label }));
    $("micSelect").value = deviceId;
    toast(why === "connected" ? `Switched to the new microphone: ${label}` : `Now using ${label}`);
  } catch (e) {
    toast(`Could not switch microphone: ${e.message}`, "err");
  }
}

function stopBrowserCapture() {
  capture.active = false;
  try {
    if (capture.ws && capture.ws.readyState === 1) capture.ws.send(JSON.stringify({ type: "stop" }));
    if (capture.ws) capture.ws.close();
  } catch { /* already closing */ }
  if (capture.stream) capture.stream.getTracks().forEach((t) => t.stop());
  if (capture.ctx) capture.ctx.close().catch(() => {});
  Object.assign(capture, { ws: null, stream: null, ctx: null, node: null, src: null });
}

/* ---------------------------------------------------------------- start / stop */

$("startBtn").onclick = () => {
  if (state.source === "browser" && !secureOk) { micNotice(); return; }
  $("operatorInput").value = store.get("ag.operator") || "";
  $("consentBox").checked = false;
  $("consentGo").disabled = true;
  $("consentDialog").showModal();
};
$("consentBox").onchange = () => { $("consentGo").disabled = !$("consentBox").checked; };
$("consentDialog").addEventListener("close", async () => {
  if ($("consentDialog").returnValue !== "ok") return;
  const operator = $("operatorInput").value.trim() || null;
  if (operator) store.set("ag.operator", operator);
  $("startBtn").disabled = true;
  try {
    if (state.source === "browser") {
      const r = await startBrowserCapture(operator);
      syncRunning(true, "browser");
      $("engInput").textContent = r.device;
    } else {
      const r = await api("/stream/start", { method: "POST", json: { consent_acknowledged: true, operator, device: $("micSelect").value || null } });
      if (r.status !== "monitoring_started" && r.status !== "already_running") throw new Error((r.reason || r.status).replace(/_/g, " "));
      syncRunning(true, "endpoint");
      $("engInput").textContent = r.device || "endpoint microphone";
    }
    toast("Monitoring started. The first sentence takes longer while the speech models load.");
  } catch (e) {
    toast(`Could not start: ${e.message}`, "err");
  } finally {
    $("startBtn").disabled = false;
  }
});

$("stopBtn").onclick = async () => {
  const operator = store.get("ag.operator");
  try {
    if (state.runningSource === "browser" && capture.active) stopBrowserCapture();
    await api("/stream/stop", { method: "POST", json: { operator } });
  } catch (e) { toast(e.message, "err"); }
  syncRunning(false);
};

/* ---------------------------------------------------------------- summary + engine */

async function refreshStatus() {
  try {
    const s = await api("/status");
    $("endpointName").textContent = s.endpoint_id;
    syncRunning(s.monitoring_active, s.source, s.started_at);
    $("engSpeech").textContent = `Whisper ${s.models.speech}`;
    $("engVerifier").textContent = s.models.verifier ? `Whisper ${s.models.verifier}` : "off";
    $("engSemantic").textContent = s.models.semantic.replace("paraphrase-", "");
    $("engBackend").textContent = s.backend.enabled ? `${s.backend.pending} queued, ${s.backend.delivered} sent` : "not configured";
    $("engInput").textContent = s.monitoring_active && s.device ? (s.device.name || "—") : "—";
  } catch { /* offline */ }
}

async function refreshStats() {
  try {
    const s = await api("/stats");
    $("sumTotal").textContent = s.total_alerts;
    $("sumSev").textContent = `${s.by_severity.CRITICAL || 0} / ${s.by_severity.HIGH || 0}`;
    $("sum24").textContent = s.alerts_last_24h;
    $("sumLatency").textContent = secs(s.avg_alert_latency_ms);
    Object.keys(s.by_language || {}).forEach((l) => { if (l !== "unknown") state.languages.add(l); });
    $("sumLangs").textContent = state.languages.size ? [...state.languages].map(langName).join(", ") : "—";
    $("navAlerts").textContent = s.total_alerts || "";
    const lf = $("fLanguage");
    const have = new Set([...lf.options].map((o) => o.value));
    state.languages.forEach((l) => { if (!have.has(l)) lf.add(new Option(langName(l), l)); });
  } catch { /* offline */ }
}

/* ---------------------------------------------------------------- alerts page */

async function refreshAlerts(fresh = false) {
  const q = new URLSearchParams({ limit: "200" });
  if ($("fSeverity").value) q.set("severity", $("fSeverity").value);
  if ($("fLanguage").value) q.set("language", $("fLanguage").value);
  try {
    const data = await api(`/alerts?${q}`);
    state.alerts = data.alerts;
    renderAlerts(fresh);
    refreshStats();
  } catch (e) { toast(`Could not load alerts: ${e.message}`, "err"); }
}

function renderAlerts(fresh = false) {
  const needle = $("fText").value.trim().toLowerCase();
  const rows = state.alerts.filter((a) => !needle ||
    (a.transcript || "").toLowerCase().includes(needle) ||
    (a.translation || "").toLowerCase().includes(needle) ||
    a.matches.some((m) => m.phrase.toLowerCase().includes(needle)));
  $("alertCount").textContent = `${rows.length} shown`;
  const tbody = $("alertTable").querySelector("tbody");
  if (!rows.length) { tbody.innerHTML = '<tr class="empty"><td colspan="6">No alerts.</td></tr>'; return; }
  tbody.innerHTML = rows.map((a) => {
    const isNew = fresh && !state.seenAlerts.has(a.id);
    state.seenAlerts.add(a.id);
    const what = a.matches.slice(0, 3).map((m) => `<bdi>${bidi(m.phrase)}</bdi>`).join(", ") || esc(a.categories.join(", "));
    const text = a.transcript != null
      ? `<span dir="${isRtl(a.transcript) ? "rtl" : "auto"}">${bidi(a.transcript)}</span>`
      : `<span class="muted">not stored · ${esc(a.transcript_hash)}</span>`;
    return `<tr class="${isNew ? "fresh" : ""}">
      <td class="time">${hhmmss(a.timestamp)}</td>
      <td>${sevBadge(a.severity)}${a.review_only ? '<span class="review">human review</span>' : ""}</td>
      <td>${esc((a.language || "—").toUpperCase())}</td>
      <td>${what}</td>
      <td>${text}${a.translation ? `<span class="sub" dir="ltr">${esc(a.translation)}</span>` : ""}</td>
      <td class="signals">${esc((a.sources || []).join(", "))}</td>
    </tr>`;
  }).join("");
  setTimeout(() => tbody.querySelectorAll("tr.fresh").forEach((r) => r.classList.remove("fresh")), 50);
}
$("fSeverity").onchange = () => refreshAlerts();
$("fLanguage").onchange = () => refreshAlerts();
$("fText").oninput = () => renderAlerts();
$("exportBtn").onclick = async (e) => {
  if (!state.token) return;
  e.preventDefault();
  const res = await fetch(`${API}/alerts/export.csv`, { headers: { Authorization: `Bearer ${state.token}` } });
  const url = URL.createObjectURL(await res.blob());
  Object.assign(document.createElement("a"), { href: url, download: "acoustic_alerts.csv" }).click();
  URL.revokeObjectURL(url);
};
loaders.alerts = () => refreshAlerts();

/* ---------------------------------------------------------------- environment page */

function addEnvRow(ev) {
  const tbody = $("envTable").querySelector("tbody");
  if (tbody.querySelector(".empty")) tbody.innerHTML = "";
  const tr = document.createElement("tr");
  tr.innerHTML = `<td class="time">${hhmmss(ev.timestamp || ev.ts)}</td><td>${esc(ENV_NAMES[ev.kind] || ev.kind)}</td>
    <td class="mono">${esc(ev.level_db)} dB</td><td class="mono">${ev.delta_db > 0 ? "+" : ""}${esc(ev.delta_db)} dB</td>`;
  tbody.prepend(tr);
  while (tbody.children.length > 100) tbody.lastChild.remove();
}
loaders.environment = async () => {
  try {
    const e = await api("/environment?limit=50");
    const tbody = $("envTable").querySelector("tbody");
    tbody.innerHTML = e.events.length ? "" : '<tr class="empty"><td colspan="4">No events.</td></tr>';
    e.events.slice().reverse().forEach((ev) => addEnvRow({ ...ev, timestamp: ev.ts }));
    if (e.live.baseline_db != null) $("envFloor").textContent = `${e.live.baseline_db} dB`;
    $("envSpeech").textContent = e.live.speech_level_db != null ? `${e.live.speech_level_db} dB` : "—";
  } catch { /* offline */ }
};

/* ---------------------------------------------------------------- policy page */

loaders.policy = async () => {
  try {
    if (!state.policy) state.policy = await api("/policy/keywords");
  } catch { return; }
  const cats = state.policy.categories;
  const langs = new Set();
  $("catTable").querySelector("tbody").innerHTML = Object.entries(cats).map(([name, c]) => {
    const own = Object.keys(c).filter((k) => !["weight", "note"].includes(k) && (c[k] || []).length);
    own.forEach((l) => langs.add(l));
    const n = own.reduce((acc, l) => acc + c[l].length, 0);
    return `<tr><td>${esc(name.replace(/_/g, " "))}</td><td class="mono">${esc(c.weight)}</td><td class="mono">${n}</td>
      <td>${esc(own.map(langName).join(", "))}</td><td class="muted small">${esc(c.note || "")}</td></tr>`;
  }).join("");
  const tabs = $("langTabs");
  if (!tabs.children.length) {
    tabs.innerHTML = [...langs].map((l) => `<button role="tab" data-lang="${esc(l)}">${esc(langName(l))}</button>`).join("");
    tabs.querySelectorAll("button").forEach((b) => { b.onclick = () => showPhrases(b.dataset.lang); });
  }
  showPhrases(tabs.querySelector("button.active")?.dataset.lang || "en");
};

function showPhrases(lang) {
  $("langTabs").querySelectorAll("button").forEach((b) => b.classList.toggle("active", b.dataset.lang === lang));
  const list = $("phraseList");
  list.dir = ["ur", "ar", "fa"].includes(lang) ? "rtl" : "ltr";
  list.innerHTML = Object.entries(state.policy.categories).map(([name, c]) => {
    const items = c[lang] || [];
    if (!items.length) return "";
    return `<h3 dir="ltr">${esc(name.replace(/_/g, " "))}</h3><ul>${items.map((p) => `<li>${esc(p)}</li>`).join("")}</ul>`;
  }).join("") || '<p class="muted">No phrases in this language.</p>';
}

/* ---------------------------------------------------------------- audit page */

async function verifyAudit() {
  const box = $("auditStatus");
  try {
    const r = await api("/audit/verify");
    box.className = `audit-status ${r.valid ? "ok" : "bad"}`;
    box.innerHTML = r.valid
      ? `<strong>Intact.</strong> ${r.entries} records chained; latest hash <code>${esc((r.head || "none").slice(0, 16))}</code>.`
      : `<strong>Integrity problem.</strong> ${esc(r.problems.join("; "))}`;
  } catch (e) { toast(`Audit check failed: ${e.message}`, "err"); }
}
$("verifyBtn").onclick = verifyAudit;

loaders.audit = async () => {
  verifyAudit();
  try {
    const [p, c] = await Promise.all([api("/privacy/manifest"), api("/consent")]);
    const items = [
      "No audio is ever written to disk or sent anywhere.",
      "All speech processing runs on this endpoint.",
      p.transcript_stored ? "The words of flagged sentences are kept for review." : "Only a hash of flagged sentences is kept.",
      `Stored: ${p.fields_persisted.join(", ")}.`,
      p.fields_sent_to_backend.length ? `Sent to the ExfilGuard server: ${p.fields_sent_to_backend.join(", ")}.` : "Nothing is sent to a server (no backend configured).",
      `Alerts are deleted after ${p.retention_days} days.`,
      p.consent_required ? "Monitoring cannot start without recorded consent." : "The consent gate is switched off.",
    ];
    $("privacyList").innerHTML = items.map((i) => `<li>${esc(i)}</li>`).join("");
    $("consentTable").querySelector("tbody").innerHTML = c.records.length
      ? c.records.map((r) => `<tr><td class="time">${hhmmss(r.ts)}</td><td>${esc(r.action.replace("stream_", "").replace(":", " · "))}</td><td>${esc(r.operator || "—")}</td><td class="muted">${esc(r.endpoint_id || "")}</td></tr>`).join("")
      : '<tr class="empty"><td colspan="4">No records.</td></tr>';
  } catch { /* offline */ }
};

$("eraseBtn").onclick = () => $("eraseDialog").showModal();
$("eraseDialog").addEventListener("close", async () => {
  if ($("eraseDialog").returnValue !== "ok") return;
  try {
    const r = await api(`/alerts?operator=${encodeURIComponent(store.get("ag.operator") || "")}`, { method: "DELETE" });
    toast(`Erased ${r.status.replace("purged_", "")} alerts. The erasure is in the audit chain.`);
    state.seenAlerts.clear();
    refreshAlerts();
    loaders.audit();
  } catch (e) { toast(e.message, "err"); }
});

/* ---------------------------------------------------------------- test page */

function showTest(r) {
  const box = $("testResult");
  box.hidden = false;
  const lang = r.language || (r.transcription && r.transcription.language);
  const text = r.transcript || "(no speech found)";
  box.innerHTML = `<div class="current-head">${sevBadge(r.severity)}<span class="mono">${Number(r.threat_score || 0).toFixed(2)}</span>
      <span class="muted">${lang ? esc(langName(lang)) : ""}</span><span class="muted small push">${r.latency_ms ? `analysed in ${secs(r.latency_ms)}` : ""}</span></div>
    <p class="utterance" dir="${isRtl(text) ? "rtl" : "auto"}">${bidi(text)}</p>
    ${r.translation ? `<p class="translation">${esc(r.translation)}</p>` : ""}
    ${(r.matches || []).length ? `<table class="detected"><thead><tr><th>Detected</th><th>Category</th><th>How</th></tr></thead><tbody>${r.matches.slice(0, 8).map((m) => `<tr><td><bdi>${bidi(m.phrase)}</bdi></td><td>${esc(m.category.replace(/_/g, " "))}</td><td class="muted">${esc(m.match || "exact")}</td></tr>`).join("")}</tbody></table>` : ""}`;
}

$("testTextBtn").onclick = async () => {
  const text = $("testText").value.trim();
  if (!text) return;
  try { showTest(await api("/analyze", { method: "POST", json: { text } })); } catch (e) { toast(e.message, "err"); }
};

async function sendClip(blob, name) {
  const fd = new FormData();
  fd.append("file", blob, name);
  fd.append("store", "true");
  $("testResult").hidden = false;
  $("testResult").textContent = "Analysing…";
  try {
    const r = await api("/analyze/audio", { method: "POST", body: fd });
    showTest(r);
    if (r.alert_id) refreshAlerts(true);
  } catch (e) { $("testResult").hidden = true; toast(e.message, "err"); }
}

$("wavBtn").onclick = () => {
  const f = $("wavInput").files[0];
  if (!f) { toast("Choose a .wav file first."); return; }
  sendClip(f, f.name);
};

function wav16k(frames) {
  const n = frames.reduce((a, f) => a + f.length, 0);
  const buf = new ArrayBuffer(44 + n * 2);
  const v = new DataView(buf);
  const w = (o, s) => [...s].forEach((c, i) => v.setUint8(o + i, c.charCodeAt(0)));
  w(0, "RIFF"); v.setUint32(4, 36 + n * 2, true); w(8, "WAVE"); w(12, "fmt ");
  v.setUint32(16, 16, true); v.setUint16(20, 1, true); v.setUint16(22, 1, true);
  v.setUint32(24, 16000, true); v.setUint32(28, 32000, true); v.setUint16(32, 2, true); v.setUint16(34, 16, true);
  w(36, "data"); v.setUint32(40, n * 2, true);
  let o = 44;
  for (const f of frames) for (let i = 0; i < f.length; i++, o += 2) v.setInt16(o, f[i], true);
  return new Blob([buf], { type: "audio/wav" });
}

const clip = { active: false };
$("recBtn").onclick = async () => {
  if (clip.active) { finishClip(); return; }
  if (!secureOk) { toast("Recording needs https:// or localhost.", "err"); return; }
  try {
    const deviceId = state.source === "browser" ? $("micSelect").value : "default";
    clip.stream = await openMic(deviceId);
    clip.frames = [];
    Object.assign(clip, await buildGraph(clip.stream, (buf) => clip.frames.push(new Int16Array(buf))));
    clip.active = true;
    clip.started = Date.now();
    $("recBtn").textContent = "Stop and analyse";
    clip.timer = setInterval(() => {
      const s = (Date.now() - clip.started) / 1000;
      $("recState").textContent = `recording ${s.toFixed(0)} s — ${trackLabel(clip.stream)}`;
      if (s >= 15) finishClip();
    }, 250);
    refreshBrowserDevices();
  } catch (e) {
    if (e.name === "NotAllowedError") { state.permission = "denied"; micNotice(); location.hash = "#monitor"; }
    toast(`Could not record: ${e.message}`, "err");
  }
};
function finishClip() {
  clip.active = false;
  clearInterval(clip.timer);
  clip.stream.getTracks().forEach((t) => t.stop());
  clip.ctx.close().catch(() => {});
  $("recBtn").textContent = "Record";
  $("recState").textContent = "";
  sendClip(wav16k(clip.frames), "recording.wav");
}

/* ---------------------------------------------------------------- start-up */

async function boot() {
  connectLive();
  await readPermission();
  await Promise.all([refreshBrowserDevices(), loadEndpointDevices(), refreshStatus(), refreshStats()]);
  setSource(state.source);
  route();
}
boot();
setInterval(refreshStats, 15000);
setInterval(refreshStatus, 10000);
