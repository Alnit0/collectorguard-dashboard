"""
CollectorGuard live dashboard.

Reads the latest telemetry that the Pi uploads to ThingsBoard and shows it live.
Settings come from environment variables on Render:

    TB_DEVICE_ID    the ThingsBoard device ID (required)
    TB_API_KEY      the ThingsBoard API key that can read telemetry (required)
    STALE_SECONDS   seconds without new data before the device shows as offline (default 30).
                    Use about 150 when the Pi logs every 60 seconds.
    TB_BASE         only if you use a different ThingsBoard address (default https://thingsboard.cloud)
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

LIVE_KEYS = [
    "light_raw", "light_percent", "uv_raw", "uv_percent",
    "lid_closed", "tilt_deg", "knock_peak",
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
    problem = config_problem()
    if problem:
        return jsonify({"error": problem})
    try:
        raw = tb_get({"keys": ",".join(LIVE_KEYS)})
        events, truncated = fetch_events()
    except Exception as e:
        return jsonify({"error": f"Could not read from ThingsBoard: {e}"})

    latest = {}
    newest_ts = 0
    for key in LIVE_KEYS:
        points = raw.get(key) or []
        if not points:
            continue
        value = num(points[0].get("value"))
        if value is None:
            continue
        latest[key] = value
        newest_ts = max(newest_ts, int(points[0]["ts"]))

    age = None if not newest_ts else max(0.0, time.time() - newest_ts / 1000)
    return jsonify({
        "latest": latest,
        "age_seconds": age,
        "device_online": age is not None and age <= STALE_SECONDS,
        "events": events,
        "events_truncated": truncated,
    })


@app.route("/api/chart")
def api_chart():
    problem = config_problem()
    if problem:
        return jsonify({"error": problem})
    minutes = request.args.get("minutes", default=60, type=int)
    minutes = max(5, min(minutes, 1440))

    end = bucket_ms(5)
    start = end - minutes * 60 * 1000
    try:
        # DESC + limit returns the newest points, which are the ones that matter for a live view
        data = tb_get({"keys": "light_percent,uv_percent", "startTs": start, "endTs": end,
                       "limit": 5000, "orderBy": "DESC"}, ttl=5)
    except Exception as e:
        return jsonify({"error": f"Could not read from ThingsBoard: {e}"})

    light = {p["ts"]: num(p["value"]) for p in (data.get("light_percent") or [])}
    uv = {p["ts"]: num(p["value"]) for p in (data.get("uv_percent") or [])}
    rows = [{"ts": ts, "light": light.get(ts), "uv": uv.get(ts)} for ts in sorted(set(light) | set(uv))]

    step = max(1, -(-len(rows) // 400))   # keep the chart to about 400 points, counting back from the newest
    rows = rows[::-1][::step][::-1]
    return jsonify({"minutes": minutes, "rows": rows})


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
  .wrap { max-width: 900px; margin: 0 auto; }
  header { display: flex; flex-wrap: wrap; align-items: baseline; gap: 6px 18px; margin-bottom: 14px; }
  h1 { margin: 0; font-size: 26px; }
  h2 { font-size: 16px; margin: 0 0 10px; }
  .muted { color: #6b7280; font-size: 14px; }
  #deviceStatus { font-weight: 600; font-size: 15px; }
  #countdown { position: fixed; top: 8px; right: 12px; font: 12px monospace; color: #6b7280;
               background: rgba(255,255,255,0.85); padding: 2px 6px; border-radius: 4px; }
  #errorBanner { background: #fde8e8; color: #9b1c1c; border: 1px solid #f5b5b5; padding: 10px 14px;
                 border-radius: 8px; margin-bottom: 14px; }
  .grid { display: grid; grid-template-columns: repeat(auto-fit, minmax(200px, 1fr)); gap: 14px; margin-bottom: 14px; }
  .card { background: #fff; border-radius: 10px; padding: 14px; box-shadow: 0 1px 3px rgba(0,0,0,0.08); text-align: center; }
  .gaugeWrap { position: relative; height: 110px; }
  .gaugeWrap canvas { width: 100% !important; height: 100% !important; }
  .gaugeValue { position: absolute; left: 0; right: 0; bottom: 2px; font-size: 26px; font-weight: 700; }
  .label { font-weight: 600; margin-top: 6px; }
  .sub { color: #6b7280; font-size: 13px; margin-top: 2px; min-height: 1.2em; }
  .big { font-size: 30px; font-weight: 700; margin: 16px 0 6px; }
  .panel { background: #fff; border-radius: 10px; padding: 14px; box-shadow: 0 1px 3px rgba(0,0,0,0.08); margin-bottom: 14px; }
  .chartHead { display: flex; justify-content: space-between; align-items: center; margin-bottom: 8px; }
  .chartHead button { border: 1px solid #d1d5db; background: #fff; border-radius: 6px; padding: 4px 10px;
                      margin-left: 6px; cursor: pointer; font-size: 13px; }
  .chartHead button.active { background: #1f2933; color: #fff; border-color: #1f2933; }
  #chartBox { position: relative; height: 260px; }
  table { width: 100%; border-collapse: collapse; font-size: 14px; }
  th { text-align: left; color: #6b7280; font-weight: 600; padding: 4px 6px; }
  td { padding: 6px; border-top: 1px solid #eef0f3; vertical-align: top; }
  .dot { display: inline-block; width: 9px; height: 9px; border-radius: 50%; margin-right: 6px; }
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
    <div class="chartHead">
      <h2>Light and UV over time (% scale)</h2>
      <div id="windowButtons">
        <button data-min="15">15 min</button>
        <button data-min="60" class="active">1 hour</button>
        <button data-min="180">3 hours</button>
      </div>
    </div>
    <div id="chartBox"><canvas id="trendChart"></canvas></div>
  </div>

  <div class="panel">
    <h2>Recent events</h2>
    <table>
      <thead><tr><th>Time</th><th>Event</th><th>Detail</th></tr></thead>
      <tbody id="eventRows"><tr><td colspan="3" class="muted">Loading...</td></tr></tbody>
    </table>
  </div>
</div>

<script>
  const REFRESH_MS = __REFRESH_MS__;
  const TILT_GAUGE_MAX = 45;
  const gauges = {};
  let trendChart = null;
  let chartMinutes = 60;
  let lastChartLoad = 0;
  let remaining = REFRESH_MS;

  const EVENT_LABELS = {
    start: 'Recorder started', stop: 'Recorder stopped', lid_opened: 'Lid opened', lid_closed: 'Lid closed',
    knock: 'Knock', tilt: 'Tilted', tilt_cleared: 'Tilt cleared', rebaseline: 'New resting position',
    clock_synced: 'Clock synced', baseline: 'Baseline captured'
  };
  const EVENT_COLOURS = {
    lid_opened: '#d43f3f', lid_closed: '#2e9e4f', knock: '#e08a1e', tilt: '#7c5cd6', tilt_cleared: '#7c5cd6',
    rebaseline: '#6b7280', start: '#3b82f6', stop: '#3b82f6', clock_synced: '#6b7280', baseline: '#6b7280'
  };

  function showError(text) {
    const el = document.getElementById('errorBanner');
    el.hidden = !text;
    el.textContent = text || '';
  }

  function fmt(value, digits) {
    return (value === undefined || value === null || isNaN(value)) ? '--' : Number(value).toFixed(digits);
  }

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

  function ageText(seconds) {
    if (seconds === null || seconds === undefined) return 'no data yet';
    if (seconds < 90) return Math.round(seconds) + ' s ago';
    if (seconds < 5400) return Math.round(seconds / 60) + ' min ago';
    return (seconds / 3600).toFixed(1) + ' h ago';
  }

  function whenText(ts) {
    return new Date(ts).toLocaleString([], { day: 'numeric', month: 'short', hour: '2-digit', minute: '2-digit', second: '2-digit' });
  }

  function renderEvents(events) {
    const body = document.getElementById('eventRows');
    body.innerHTML = '';
    if (!events.length) {
      const tr = document.createElement('tr');
      const td = document.createElement('td');
      td.colSpan = 3; td.className = 'muted'; td.textContent = 'No events in the last 24 hours';
      tr.appendChild(td); body.appendChild(tr);
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

  async function loadLive() {
    try {
      const res = await fetch('/api/live');
      const data = await res.json();
      showError(data.error || '');
      if (data.error) return;

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
    } catch (err) {
      showError('Cannot reach the dashboard server: ' + err);
    }
  }

  async function loadChart() {
    lastChartLoad = Date.now();
    try {
      const res = await fetch('/api/chart?minutes=' + chartMinutes);
      const data = await res.json();
      if (data.error) { showError(data.error); return; }
      const rows = data.rows || [];
      const labels = rows.map(r => new Date(r.ts).toLocaleTimeString([], { hour: '2-digit', minute: '2-digit', second: '2-digit' }));
      const light = rows.map(r => r.light);
      const uv = rows.map(r => r.uv);

      if (!trendChart) {
        trendChart = new Chart(document.getElementById('trendChart'), {
          type: 'line',
          data: {
            labels: labels,
            datasets: [
              { label: 'Light %', data: light, borderColor: '#e0a020', backgroundColor: '#e0a020', pointRadius: 0, tension: 0.2, spanGaps: true },
              { label: 'UV %', data: uv, borderColor: '#7c5cd6', backgroundColor: '#7c5cd6', pointRadius: 0, tension: 0.2, spanGaps: true }
            ]
          },
          options: {
            responsive: true, maintainAspectRatio: false, animation: false,
            interaction: { mode: 'index', intersect: false },
            scales: {
              y: { min: 0, max: 100, title: { display: true, text: '%' } },
              x: { ticks: { maxTicksLimit: 8, autoSkip: true } }
            }
          }
        });
      } else {
        trendChart.data.labels = labels;
        trendChart.data.datasets[0].data = light;
        trendChart.data.datasets[1].data = uv;
        trendChart.update('none');
      }
    } catch (err) {
      showError('Cannot reach the dashboard server: ' + err);
    }
  }

  function refreshAll() {
    loadLive();
    if (Date.now() - lastChartLoad > 14000) loadChart();
    remaining = REFRESH_MS;
  }

  document.getElementById('windowButtons').addEventListener('click', function (ev) {
    const btn = ev.target.closest('button');
    if (!btn) return;
    chartMinutes = parseInt(btn.dataset.min, 10);
    for (const b of this.querySelectorAll('button')) b.classList.toggle('active', b === btn);
    loadChart();
  });

  if (typeof Chart === 'undefined') {
    showError('The chart library did not load. Check the internet connection and refresh.');
  } else {
    gauges.light = makeGauge('gLight', '#e0a020');
    gauges.uv = makeGauge('gUv', '#7c5cd6');
    gauges.tilt = makeGauge('gTilt', '#0e9aa7');
  }

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
