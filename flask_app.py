"""
CollectorGuard live dashboard.

Reads the latest telemetry that the Pi uploads to ThingsBoard and shows it live.
Settings come from environment variables on Render:

    TB_DEVICE_ID    the ThingsBoard device ID (required)
    TB_API_KEY      the ThingsBoard API key that can read telemetry (required)
    STALE_SECONDS   seconds without new data before the device shows as offline (default 30).
                    Use about 150 when the Pi logs every 60 seconds.
    TB_BASE         only if you use a different ThingsBoard address (default https://thingsboard.cloud)

Three views, switched with the tabs at the top:
  Overview  - gauges and current status, same as before
  Raw data  - every value from the latest poll shown plainly, plus the full JSON response,
              for debugging what is actually arriving
  History   - pick a sensor and a time window (last hour/24 hours/7 days, or a specific day),
              see it as a chart, and a table of the exact points behind it
"""

import os
import time
import threading
import requests
from flask import Flask, jsonify, request

app = Flask(__name__)

TB_BASE = os.environ.get("TB_BASE", "https://thingsboard.cloud").rstrip("/")
DEVICE_ID = os.environ.get("TB_DEVICE_ID", "").strip()
API_KEY = os.environ.get("TB_API_KEY", "").strip()
try:
    STALE_SECONDS = int(os.environ.get("STALE_SECONDS", "30"))
except ValueError:
    STALE_SECONDS = 30

REFRESH_MS = 5000
TB_TIMEOUT = 10
EVENT_LIMIT = 30
EVENT_HOURS = 24
MAX_CHART_ROWS = 3000          # most rows ever returned for one chart/table request
MAX_RANGE_MS = 8 * 24 * 3600 * 1000   # 8 days, a safety cap on how wide a single query can be

# Every sensor the Pi currently writes. If another one gets added later, add its name here too,
# the rest of the page (the picker, the raw table) adapts on its own.
KNOWN_SENSORS = [
    "light_raw", "light_percent", "uv_raw", "uv_percent",
    "temperature_c", "humidity_percent",
    "lid_closed", "tilt_deg", "knock_peak",
    "accel_x", "accel_y", "accel_z",
]

_cache = {}
_cache_lock = threading.Lock()


def config_problem():
    if not DEVICE_ID:
        return "TB_DEVICE_ID is not set on the server"
    if not API_KEY:
        return "TB_API_KEY is not set on the server"
    return ""


def num(value):
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def bucket_ms(seconds):
    """Round the current time down, so viewers within the same few seconds share one cached request."""
    return int(time.time() // seconds * seconds * 1000)


def tb_get(params, ttl=2):
    key = tuple(sorted(params.items()))
    now = time.time()
    with _cache_lock:
        hit = _cache.get(key)
        if hit and now - hit[0] < ttl:
            return hit[1]

    url = f"{TB_BASE}/api/plugins/telemetry/DEVICE/{DEVICE_ID}/values/timeseries"
    resp = requests.get(url, params=params, headers={"X-Authorization": f"ApiKey {API_KEY}"}, timeout=TB_TIMEOUT)
    if resp.status_code == 401:
        raise RuntimeError("ThingsBoard rejected the API key (401)")
    if resp.status_code == 404:
        raise RuntimeError("ThingsBoard could not find that device (404), check TB_DEVICE_ID")
    if resp.status_code != 200:
        raise RuntimeError(f"ThingsBoard answered {resp.status_code}")
    data = resp.json()

    with _cache_lock:
        if len(_cache) > 60:
            _cache.clear()
        _cache[key] = (now, data)
    return data


def fetch_events():
    end = bucket_ms(2)
    start = end - EVENT_HOURS * 3600 * 1000
    data = tb_get({"keys": "event_log", "startTs": start, "endTs": end,
                   "limit": EVENT_LIMIT, "orderBy": "DESC"})
    events = []
    for point in data.get("event_log") or []:
        kind, _, detail = str(point.get("value", "")).partition(" | ")
        events.append({"ts": point["ts"], "type": kind, "detail": detail})
    return events, len(events) >= EVENT_LIMIT


@app.after_request
def no_store(response):
    if request.path.startswith("/api/"):
        response.headers["Cache-Control"] = "no-store"
    return response


@app.route("/api/live")
def api_live():
    """Everything the Overview and Raw tabs need: the latest value of every known sensor,
    plus recent events. 'latest' is deliberately plain, key -> number, so the Raw tab can show
    exactly what arrived with no formatting or interpretation applied."""
    problem = config_problem()
    if problem:
        return jsonify({"error": problem})
    try:
        raw = tb_get({"keys": ",".join(KNOWN_SENSORS)})
        events, truncated = fetch_events()
    except Exception as e:
        return jsonify({"error": f"Could not read from ThingsBoard: {e}"})

    latest = {}
    latest_ts = {}
    newest_ts = 0
    for key in KNOWN_SENSORS:
        points = raw.get(key) or []
        if not points:
            continue
        value = num(points[0].get("value"))
        if value is None:
            continue
        latest[key] = value
        latest_ts[key] = int(points[0]["ts"])
        newest_ts = max(newest_ts, latest_ts[key])

    age = None if not newest_ts else max(0.0, time.time() - newest_ts / 1000)
    return jsonify({
        "latest": latest,
        "latest_ts": latest_ts,
        "age_seconds": age,
        "device_online": age is not None and age <= STALE_SECONDS,
        "events": events,
        "events_truncated": truncated,
        "known_sensors": KNOWN_SENSORS,
    })


@app.route("/api/chart")
def api_chart():
    """One sensor's history, for the History tab's chart and its drill-down table underneath.

    Either pass `minutes` (a quick window ending now), or both `start_ts` and `end_ts` in
    milliseconds (for a specific calendar day, computed in the browser so it matches the
    viewer's own timezone, not the server's).
    """
    problem = config_problem()
    if problem:
        return jsonify({"error": problem})

    sensor = request.args.get("sensor", default="light_percent")
    if sensor not in KNOWN_SENSORS:
        return jsonify({"error": f"Unknown sensor '{sensor}'"})

    start_ts = request.args.get("start_ts", type=int)
    end_ts = request.args.get("end_ts", type=int)
    if start_ts is not None and end_ts is not None:
        start, end = start_ts, end_ts
    else:
        minutes = request.args.get("minutes", default=60, type=int)
        minutes = max(5, min(minutes, 10080))   # 5 minutes to 7 days
        end = bucket_ms(5)
        start = end - minutes * 60 * 1000

    if end <= start:
        return jsonify({"error": "End of range must be after the start"})
    if end - start > MAX_RANGE_MS:
        start = end - MAX_RANGE_MS

    try:
        # DESC + limit keeps the newest points when a range holds more than the limit allows,
        # which matters most for "last 24 hours" on a busy sensor, the recent end is what a
        # live view needs most.
        data = tb_get({"keys": sensor, "startTs": start, "endTs": end,
                       "limit": MAX_CHART_ROWS, "orderBy": "DESC"}, ttl=5)
    except Exception as e:
        return jsonify({"error": f"Could not read from ThingsBoard: {e}"})

    points = data.get(sensor) or []
    rows = [{"ts": int(p["ts"]), "value": num(p["value"])} for p in points if num(p["value"]) is not None]
    rows.sort(key=lambda r: r["ts"])

    return jsonify({
        "sensor": sensor,
        "start_ts": start,
        "end_ts": end,
        "rows": rows,
        "truncated": len(rows) >= MAX_CHART_ROWS,
    })


PAGE = r"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>CollectorGuard live</title>
<script src="https://cdnjs.cloudflare.com/ajax/libs/Chart.js/4.4.0/chart.umd.min.js"></script>
<style>
  * { box-sizing: border-box; }
  body { font-family: system-ui, -apple-system, "Segoe UI", sans-serif; margin: 0; padding: 20px;
         background: #f5f6f8; color: #1f2933; }
  .wrap { max-width: 920px; margin: 0 auto; }
  header { display: flex; flex-wrap: wrap; align-items: baseline; gap: 6px 18px; margin-bottom: 10px; }
  h1 { margin: 0; font-size: 26px; }
  h2 { font-size: 16px; margin: 0 0 10px; }
  .muted { color: #6b7280; font-size: 14px; }
  #deviceStatus { font-weight: 600; font-size: 15px; }
  #countdown { position: fixed; top: 8px; right: 12px; font: 12px monospace; color: #6b7280;
               background: rgba(255,255,255,0.85); padding: 2px 6px; border-radius: 4px; }
  #errorBanner { background: #fde8e8; color: #9b1c1c; border: 1px solid #f5b5b5; padding: 10px 14px;
                 border-radius: 8px; margin-bottom: 14px; }

  .tabs { display: flex; gap: 6px; margin-bottom: 16px; border-bottom: 1px solid #e2e5e9; }
  .tabs button { border: none; background: none; padding: 10px 14px; font-size: 14px; cursor: pointer;
                 color: #6b7280; border-bottom: 2px solid transparent; }
  .tabs button.active { color: #1f2933; border-bottom-color: #1f2933; font-weight: 600; }
  .tabpanel { display: none; }
  .tabpanel.active { display: block; }

  .grid { display: grid; grid-template-columns: repeat(auto-fit, minmax(200px, 1fr)); gap: 14px; margin-bottom: 14px; }
  .card { background: #fff; border-radius: 10px; padding: 14px; box-shadow: 0 1px 3px rgba(0,0,0,0.08); text-align: center; }
  .gaugeWrap { position: relative; height: 110px; }
  .gaugeWrap canvas { width: 100% !important; height: 100% !important; }
  .gaugeValue { position: absolute; left: 0; right: 0; bottom: 2px; font-size: 26px; font-weight: 700; }
  .label { font-weight: 600; margin-top: 6px; }
  .sub { color: #6b7280; font-size: 13px; margin-top: 2px; min-height: 1.2em; }
  .big { font-size: 30px; font-weight: 700; margin: 16px 0 6px; }
  .panel { background: #fff; border-radius: 10px; padding: 14px; box-shadow: 0 1px 3px rgba(0,0,0,0.08); margin-bottom: 14px; }

  .chartHead { display: flex; flex-wrap: wrap; justify-content: space-between; align-items: center; gap: 8px; margin-bottom: 8px; }
  .chartHead select, .chartHead button { border: 1px solid #d1d5db; background: #fff; border-radius: 6px;
                      padding: 5px 10px; cursor: pointer; font-size: 13px; }
  .chartHead button.active { background: #1f2933; color: #fff; border-color: #1f2933; }
  #chartBox { position: relative; height: 280px; }

  #dayPicker { display: flex; gap: 6px; overflow-x: auto; padding: 4px 0 10px; }
  #dayPicker button { flex: 0 0 auto; white-space: nowrap; }

  table { width: 100%; border-collapse: collapse; font-size: 13px; }
  th { text-align: left; color: #6b7280; font-weight: 600; padding: 4px 6px; position: sticky; top: 0; background: #fff; }
  td { padding: 5px 6px; border-top: 1px solid #eef0f3; vertical-align: top; font-variant-numeric: tabular-nums; }
  .dot { display: inline-block; width: 9px; height: 9px; border-radius: 50%; margin-right: 6px; }
  .scrollTable { max-height: 360px; overflow-y: auto; border: 1px solid #eef0f3; border-radius: 8px; }
  pre.rawJson { background: #11161d; color: #c9d1d9; padding: 12px; border-radius: 8px; overflow-x: auto;
                font-size: 12px; max-height: 320px; }

  .orientWrap { display: flex; flex-wrap: wrap; gap: 20px; align-items: center; justify-content: center; }
  .scene3d { width: 160px; height: 160px; perspective: 500px; flex: 0 0 auto; }
  .cubeSpin { width: 100%; height: 100%; position: relative; transform-style: preserve-3d;
              transition: transform 0.4s ease-out; transform: rotateX(0deg) rotateZ(0deg); }
  .cubeFace { position: absolute; width: 90px; height: 90px; left: 35px; top: 35px;
              display: flex; align-items: center; justify-content: center;
              font-size: 11px; font-weight: 700; color: #fff; border: 1px solid rgba(255,255,255,0.25); }
  .faceTop    { background: #2e9e4f; transform: rotateX(90deg) translateZ(45px); }
  .faceBottom { background: #6b7280; transform: rotateX(-90deg) translateZ(45px); }
  .faceFront  { background: #2e6fe0; transform: translateZ(45px); }
  .faceBack   { background: #2e6fe0; transform: rotateY(180deg) translateZ(45px); }
  .faceRight  { background: #7c5cd6; transform: rotateY(90deg) translateZ(45px); }
  .faceLeft   { background: #7c5cd6; transform: rotateY(-90deg) translateZ(45px); }
  .orientReadout { min-width: 160px; font-size: 14px; line-height: 1.8; }
  .orientReadout .big { font-size: 20px; margin: 0; }
</style>
</head>
<body>
<div id="countdown">Next refresh in <span id="countdownValue">__REFRESH_MS__</span> ms</div>
<div class="wrap">
  <header>
    <h1>CollectorGuard</h1>
    <span id="deviceStatus">Checking...</span>
    <span id="lastData" class="muted"></span>
  </header>

  <div id="errorBanner" hidden></div>

  <div class="tabs">
    <button data-tab="overview" class="active">Overview</button>
    <button data-tab="raw">Raw data</button>
    <button data-tab="history">History</button>
  </div>

  <!-- ===================== Overview ===================== -->
  <div id="tab-overview" class="tabpanel active">
    <div class="grid">
      <div class="card">
        <div class="gaugeWrap"><canvas id="gLight"></canvas><div class="gaugeValue" id="vLight">--</div></div>
        <div class="label">Light</div>
        <div class="sub" id="sLight">raw --</div>
      </div>
      <div class="card">
        <div class="gaugeWrap"><canvas id="gUv"></canvas><div class="gaugeValue" id="vUv">--</div></div>
        <div class="label">UV</div>
        <div class="sub" id="sUv">raw --</div>
      </div>
      <div class="card">
        <div class="gaugeWrap"><canvas id="gTilt"></canvas><div class="gaugeValue" id="vTilt">--</div></div>
        <div class="label">Tilt</div>
        <div class="sub">degrees from resting position</div>
      </div>
      <div class="card">
        <div class="big" id="vTemp">--</div>
        <div class="label">Temperature</div>
        <div class="sub" id="sTemp">&nbsp;</div>
      </div>
      <div class="card">
        <div class="big" id="vHumidity">--</div>
        <div class="label">Humidity</div>
        <div class="sub" id="sHumidity">&nbsp;</div>
      </div>
      <div class="card">
        <div class="big" id="vLid">--</div>
        <div class="label">Lid</div>
        <div class="sub">reed switch</div>
      </div>
      <div class="card">
        <div class="big" id="vKnocks">--</div>
        <div class="label">Knocks, last hour</div>
        <div class="sub" id="sKnock">latest interval peak --</div>
      </div>
    </div>

    <div class="panel">
      <h2>Box orientation</h2>
      <p class="muted">The green face is the top of the box, worked out live from the accelerometer. If the box is lying on its side or upside down, the green face moves to show it.</p>
      <div class="orientWrap">
        <div class="scene3d">
          <div class="cubeSpin" id="orientCube">
            <div class="cubeFace faceTop">TOP</div>
            <div class="cubeFace faceBottom"></div>
            <div class="cubeFace faceFront"></div>
            <div class="cubeFace faceBack"></div>
            <div class="cubeFace faceRight"></div>
            <div class="cubeFace faceLeft"></div>
          </div>
        </div>
        <div class="orientReadout" id="orientText">Waiting for accelerometer data...</div>
      </div>
    </div>

    <div class="panel">
      <h2>Recent events</h2>
      <div class="scrollTable">
        <table>
          <thead><tr><th>Time</th><th>Event</th><th>Detail</th></tr></thead>
          <tbody id="eventRows"><tr><td colspan="3" class="muted">Loading...</td></tr></tbody>
        </table>
      </div>
    </div>
  </div>

  <!-- ===================== Raw data ===================== -->
  <div id="tab-raw" class="tabpanel">
    <div class="panel">
      <h2>Latest value per sensor</h2>
      <p class="muted">Exactly what the last poll returned, with no formatting or conversion applied.</p>
      <table>
        <thead><tr><th>Sensor key</th><th>Raw value</th><th>Age</th></tr></thead>
        <tbody id="rawRows"><tr><td colspan="3" class="muted">Loading...</td></tr></tbody>
      </table>
    </div>
    <div class="panel">
      <h2>Full response (/api/live)</h2>
      <p class="muted">The complete JSON this page is reading from, for when the table above isn't enough.</p>
      <pre class="rawJson" id="rawJson">Loading...</pre>
    </div>
  </div>

  <!-- ===================== History ===================== -->
  <div id="tab-history" class="tabpanel">
    <div class="panel">
      <div class="chartHead">
        <div>
          <select id="sensorSelect"></select>
        </div>
        <div id="rangeButtons">
          <button data-minutes="60">Last hour</button>
          <button data-minutes="1440" class="active">Last 24 hours</button>
          <button data-minutes="10080">Last 7 days</button>
        </div>
      </div>
      <div id="dayPicker"></div>
      <div id="chartBox"><canvas id="historyChart"></canvas></div>
      <p class="muted" id="chartRangeText">&nbsp;</p>
    </div>

    <div class="panel">
      <h2>Data behind this chart</h2>
      <p class="muted" id="drillInfo">&nbsp;</p>
      <div class="scrollTable">
        <table>
          <thead><tr><th>Time</th><th>Value</th></tr></thead>
          <tbody id="historyRows"><tr><td colspan="2" class="muted">Loading...</td></tr></tbody>
        </table>
      </div>
    </div>
  </div>
</div>

<script>
  const REFRESH_MS = __REFRESH_MS__;
  const TILT_GAUGE_MAX = 45;
  const gauges = {};
  let remaining = REFRESH_MS;
  let currentTab = 'overview';

  // ---------- Tabs ----------
  document.querySelectorAll('.tabs button').forEach(btn => {
    btn.addEventListener('click', () => {
      currentTab = btn.dataset.tab;
      document.querySelectorAll('.tabs button').forEach(b => b.classList.toggle('active', b === btn));
      document.querySelectorAll('.tabpanel').forEach(p => p.classList.toggle('active', p.id === 'tab-' + currentTab));
      if (currentTab === 'history') loadHistory();
    });
  });

  function fmt(value, digits) {
    return (value === undefined || value === null || isNaN(value)) ? '--' : Number(value).toFixed(digits);
  }

  function ageText(seconds) {
    if (seconds === null || seconds === undefined) return 'no data yet';
    if (seconds < 90) return Math.round(seconds) + ' s ago';
    if (seconds < 5400) return Math.round(seconds / 60) + ' min ago';
    return (seconds / 3600).toFixed(1) + ' h ago';
  }

  function whenText(ts) {
    return new Date(ts).toLocaleString([], { day: 'numeric', month: 'short', hour: '2-digit', minute: '2-digit', second: '2-digit' });
  }

  function showError(text) {
    const el = document.getElementById('errorBanner');
    el.hidden = !text;
    el.textContent = text || '';
  }

  // ---------- Overview ----------
  function makeGauge(canvasId, colour) {
    return new Chart(document.getElementById(canvasId), {
      type: 'doughnut',
      data: { datasets: [{ data: [0, 1], backgroundColor: [colour, '#e5e7eb'], borderWidth: 0 }] },
      options: {
        circumference: 180, rotation: 270, cutout: '72%', animation: false,
        responsive: true, maintainAspectRatio: false,
        plugins: { legend: { display: false }, tooltip: { enabled: false } }
      }
    });
  }

  function setGauge(name, value, max) {
    const g = gauges[name];
    if (!g) return;
    const v = (value === undefined || value === null || isNaN(value)) ? 0 : Math.max(0, Math.min(max, value));
    g.data.datasets[0].data = [v, max - v];
    g.update('none');
  }

  const EVENT_LABELS = {
    start: 'Recorder started', stop: 'Recorder stopped', lid_opened: 'Lid opened', lid_closed: 'Lid closed',
    knock: 'Knock', tilt: 'Tilted', tilt_cleared: 'Tilt cleared', rebaseline: 'New resting position',
    clock_synced: 'Clock synced', baseline: 'Baseline captured'
  };
  const EVENT_COLOURS = {
    lid_opened: '#d43f3f', lid_closed: '#2e9e4f', knock: '#e08a1e', tilt: '#7c5cd6', tilt_cleared: '#7c5cd6',
    rebaseline: '#6b7280', start: '#3b82f6', stop: '#3b82f6', clock_synced: '#6b7280', baseline: '#6b7280'
  };

  function renderEvents(events) {
    const body = document.getElementById('eventRows');
    body.innerHTML = '';
    if (!events.length) {
      body.innerHTML = '<tr><td colspan="3" class="muted">No events in the last 24 hours</td></tr>';
      return;
    }
    for (const e of events) {
      const tr = document.createElement('tr');
      const t1 = document.createElement('td'); t1.textContent = whenText(e.ts);
      const t2 = document.createElement('td');
      const dot = document.createElement('span'); dot.className = 'dot';
      dot.style.background = EVENT_COLOURS[e.type] || '#9ca3af';
      t2.appendChild(dot); t2.appendChild(document.createTextNode(EVENT_LABELS[e.type] || e.type));
      const t3 = document.createElement('td'); t3.textContent = e.detail;
      tr.appendChild(t1); tr.appendChild(t2); tr.appendChild(t3);
      body.appendChild(tr);
    }
  }

  let lastLiveData = null;

  function renderRaw(data) {
    const body = document.getElementById('rawRows');
    const L = data.latest || {};
    const LT = data.latest_ts || {};
    const keys = (data.known_sensors || Object.keys(L));
    body.innerHTML = '';
    for (const key of keys) {
      const tr = document.createElement('tr');
      const k = document.createElement('td'); k.textContent = key;
      const v = document.createElement('td');
      v.textContent = (key in L) ? L[key] : 'no data';
      v.style.fontFamily = 'monospace';
      const a = document.createElement('td');
      a.textContent = (key in LT) ? ageText((Date.now() - LT[key]) / 1000) : '--';
      tr.appendChild(k); tr.appendChild(v); tr.appendChild(a);
      body.appendChild(tr);
    }
    document.getElementById('rawJson').textContent = JSON.stringify(data, null, 2);
  }

  function updateOrientation(L) {
    const cube = document.getElementById('orientCube');
    const text = document.getElementById('orientText');
    const ax = L.accel_x, ay = L.accel_y, az = L.accel_z;
    if (ax === undefined || ay === undefined || az === undefined) {
      text.textContent = 'Waiting for accelerometer data...';
      return;
    }
    // Y is the sensor's "up" axis when the box is sitting level. These two angles describe
    // how far it leans away from that, in the two directions perpendicular to Y.
    const pitch = Math.atan2(ax, Math.sqrt(ay * ay + az * az)) * 180 / Math.PI;
    const roll = Math.atan2(az, Math.sqrt(ax * ax + ay * ay)) * 180 / Math.PI;

    // Mapping these two angles onto the cube's rotation is a best guess at which way is
    // "forward" on the physical box. If the cube leans the wrong way compared to the real
    // box, swap the sign on one of the two lines below, that's the only thing to adjust.
    cube.style.transform = `rotateX(${roll}deg) rotateZ(${-pitch}deg)`;

    const upsideDown = ay < 0;
    text.innerHTML =
      `<div class="big">${upsideDown ? 'UPSIDE DOWN' : 'Right side up'}</div>` +
      `Lean one way: ${pitch.toFixed(1)}&deg;<br>` +
      `Lean the other way: ${roll.toFixed(1)}&deg;<br>` +
      `Raw: X ${fmt(ax, 2)} / Y ${fmt(ay, 2)} / Z ${fmt(az, 2)}`;
  }

  async function loadLive() {
    try {
      const res = await fetch('/api/live');
      const data = await res.json();
      showError(data.error || '');
      if (data.error) return;
      lastLiveData = data;

      const L = data.latest || {};
      const statusEl = document.getElementById('deviceStatus');
      statusEl.textContent = data.device_online ? '\u25CF Device online' : '\u25CF Device offline';
      statusEl.style.color = data.device_online ? '#2e9e4f' : '#d43f3f';
      document.getElementById('lastData').textContent = 'Last data ' + ageText(data.age_seconds);

      setGauge('light', L.light_percent, 100);
      document.getElementById('vLight').textContent = fmt(L.light_percent, 0) + '%';
      document.getElementById('sLight').textContent = 'raw ' + fmt(L.light_raw, 0) + ' (lower = brighter)';

      setGauge('uv', L.uv_percent, 100);
      document.getElementById('vUv').textContent = fmt(L.uv_percent, 0) + '%';
      document.getElementById('sUv').textContent = 'raw ' + fmt(L.uv_raw, 0);

      setGauge('tilt', L.tilt_deg, TILT_GAUGE_MAX);
      document.getElementById('vTilt').textContent = fmt(L.tilt_deg, 1) + '\u00B0';

      updateOrientation(L);

      document.getElementById('vTemp').textContent = (L.temperature_c === undefined) ? '--' : fmt(L.temperature_c, 1) + '\u00B0C';
      document.getElementById('vHumidity').textContent = (L.humidity_percent === undefined) ? '--' : fmt(L.humidity_percent, 0) + '%';

      const lidEl = document.getElementById('vLid');
      if (L.lid_closed === undefined) {
        lidEl.textContent = '--'; lidEl.style.color = '';
      } else if (L.lid_closed >= 0.5) {
        lidEl.textContent = 'Closed'; lidEl.style.color = '#2e9e4f';
      } else {
        lidEl.textContent = 'Open'; lidEl.style.color = '#d43f3f';
      }

      const events = data.events || [];
      const hourAgo = Date.now() - 3600 * 1000;
      const recentKnocks = events.filter(e => e.type === 'knock' && e.ts >= hourAgo).length;
      const oldest = events.length ? events[events.length - 1].ts : null;
      const capped = data.events_truncated && oldest !== null && oldest >= hourAgo;
      document.getElementById('vKnocks').textContent = recentKnocks + (capped ? '+' : '');
      document.getElementById('sKnock').textContent = 'latest interval peak ' + fmt(L.knock_peak, 2) + ' m/s\u00B2';

      renderEvents(events);
      renderRaw(data);
    } catch (err) {
      showError('Cannot reach the dashboard server: ' + err);
    }
  }

  // ---------- History ----------
  let historyChart = null;
  let selectedMinutes = 1440;
  let selectedRange = null;   // {start, end} when a specific day is picked instead of a quick range

  function buildSensorPicker() {
    const sel = document.getElementById('sensorSelect');
    const names = {
      light_raw: 'Light (raw)', light_percent: 'Light (%)',
      uv_raw: 'UV (raw)', uv_percent: 'UV (%)',
      temperature_c: 'Temperature (\u00B0C)', humidity_percent: 'Humidity (%)',
      lid_closed: 'Lid closed (1/0)', tilt_deg: 'Tilt (degrees)', knock_peak: 'Knock peak (m/s\u00B2)',
      accel_x: 'Accelerometer X (raw)', accel_y: 'Accelerometer Y (raw)', accel_z: 'Accelerometer Z (raw)'
    };
    const known = (lastLiveData && lastLiveData.known_sensors) ||
      ['light_percent', 'uv_percent', 'temperature_c', 'humidity_percent', 'tilt_deg', 'knock_peak', 'lid_closed', 'light_raw', 'uv_raw'];
    sel.innerHTML = '';
    for (const key of known) {
      const opt = document.createElement('option');
      opt.value = key; opt.textContent = names[key] || key;
      sel.appendChild(opt);
    }
    sel.value = 'light_percent';
    sel.addEventListener('change', loadHistory);
  }

  function buildDayPicker() {
    const el = document.getElementById('dayPicker');
    el.innerHTML = '';
    const today = new Date();
    for (let i = 0; i < 10; i++) {
      const d = new Date(today);
      d.setDate(d.getDate() - i);
      const btn = document.createElement('button');
      btn.textContent = i === 0 ? 'Today' : d.toLocaleDateString([], { weekday: 'short', day: 'numeric', month: 'short' });
      btn.addEventListener('click', () => {
        const start = new Date(d); start.setHours(0, 0, 0, 0);
        const end = new Date(d); end.setHours(23, 59, 59, 999);
        selectedRange = { start: start.getTime(), end: Math.min(end.getTime(), Date.now()) };
        document.querySelectorAll('#rangeButtons button').forEach(b => b.classList.remove('active'));
        document.querySelectorAll('#dayPicker button').forEach(b => b.classList.remove('active'));
        btn.classList.add('active');
        loadHistory();
      });
      el.appendChild(btn);
    }
  }

  document.getElementById('rangeButtons').addEventListener('click', (ev) => {
    const btn = ev.target.closest('button');
    if (!btn) return;
    selectedRange = null;
    selectedMinutes = parseInt(btn.dataset.minutes, 10);
    document.querySelectorAll('#rangeButtons button').forEach(b => b.classList.toggle('active', b === btn));
    document.querySelectorAll('#dayPicker button').forEach(b => b.classList.remove('active'));
    loadHistory();
  });

  function renderHistoryTable(rows) {
    const body = document.getElementById('historyRows');
    body.innerHTML = '';
    if (!rows.length) {
      body.innerHTML = '<tr><td colspan="2" class="muted">No data in this range</td></tr>';
      return;
    }
    const frag = document.createDocumentFragment();
    for (let i = rows.length - 1; i >= 0; i--) {   // newest first in the table
      const tr = document.createElement('tr');
      const t1 = document.createElement('td'); t1.textContent = whenText(rows[i].ts);
      const t2 = document.createElement('td'); t2.textContent = rows[i].value;
      tr.appendChild(t1); tr.appendChild(t2);
      frag.appendChild(tr);
    }
    body.appendChild(frag);
  }

  async function loadHistory() {
    const sensor = document.getElementById('sensorSelect').value || 'light_percent';
    const params = new URLSearchParams({ sensor });
    if (selectedRange) {
      params.set('start_ts', selectedRange.start);
      params.set('end_ts', selectedRange.end);
    } else {
      params.set('minutes', selectedMinutes);
    }

    try {
      const res = await fetch('/api/chart?' + params.toString());
      const data = await res.json();
      if (data.error) { showError(data.error); return; }
      showError('');

      const rows = data.rows || [];
      document.getElementById('chartRangeText').textContent =
        whenText(data.start_ts) + ' to ' + whenText(data.end_ts) +
        (data.truncated ? '  (showing the most recent points, the full range has more than fits)' : '');
      document.getElementById('drillInfo').textContent = rows.length + ' point(s) in this range';

      const step = Math.max(1, Math.ceil(rows.length / 500));
      const thinned = rows.filter((_, i) => i % step === 0 || i === rows.length - 1);
      const labels = thinned.map(r => new Date(r.ts).toLocaleString([], { month: 'short', day: 'numeric', hour: '2-digit', minute: '2-digit' }));
      const values = thinned.map(r => r.value);

      if (!historyChart) {
        historyChart = new Chart(document.getElementById('historyChart'), {
          type: 'line',
          data: { labels, datasets: [{ label: sensor, data: values, borderColor: '#2e6fe0',
                   backgroundColor: '#2e6fe0', pointRadius: 0, tension: 0.15, spanGaps: true }] },
          options: {
            responsive: true, maintainAspectRatio: false, animation: false,
            interaction: { mode: 'index', intersect: false },
            scales: { x: { ticks: { maxTicksLimit: 8, autoSkip: true } } }
          }
        });
      } else {
        historyChart.data.labels = labels;
        historyChart.data.datasets[0].data = values;
        historyChart.data.datasets[0].label = sensor;
        historyChart.update('none');
      }

      renderHistoryTable(rows);
    } catch (err) {
      showError('Cannot reach the dashboard server: ' + err);
    }
  }

  function refreshAll() {
    loadLive();
    remaining = REFRESH_MS;
  }

  if (typeof Chart === 'undefined') {
    showError('The chart library did not load. Check the internet connection and refresh.');
  } else {
    gauges.light = makeGauge('gLight', '#e0a020');
    gauges.uv = makeGauge('gUv', '#7c5cd6');
    gauges.tilt = makeGauge('gTilt', '#0e9aa7');
  }

  buildSensorPicker();
  buildDayPicker();

  setInterval(function () {
    remaining -= 100;
    if (remaining < 0) remaining = 0;
    document.getElementById('countdownValue').textContent = remaining;
  }, 100);

  refreshAll();
  setInterval(refreshAll, REFRESH_MS);
</script>
</body>
</html>
"""


@app.route("/")
def dashboard():
    return PAGE.replace("__REFRESH_MS__", str(REFRESH_MS))


if __name__ == "__main__":
    app.run()
