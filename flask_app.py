import os
import time
import requests
from flask import Flask, jsonify

app = Flask(__name__)

DEVICE_ID = os.environ.get("TB_DEVICE_ID")
API_KEY = os.environ.get("TB_API_KEY")
TB_API = f"https://thingsboard.cloud/api/plugins/telemetry/DEVICE/{DEVICE_ID}/values/timeseries"
HEADERS = {"X-Authorization": f"ApiKey {API_KEY}"}

SAFE_RANGE = (0, 100)
REFRESH_INTERVAL_MS = 5000


@app.route("/")
def dashboard():
    return f"""
    <html>
    <head>
        <script src="https://cdnjs.cloudflare.com/ajax/libs/Chart.js/4.4.0/chart.umd.min.js"></script>
    </head>
    <body style="font-family: sans-serif; text-align: center; margin-top: 50px;">
        <div id="countdown" style="position: fixed; top: 10px; right: 15px; font-size: 14px; color: #666; font-family: monospace;">
            Next refresh in: <span id="countdownValue">{REFRESH_INTERVAL_MS}</span> ms
        </div>

        <h1>CollectorGuard Dashboard</h1>
        <p id="valueText" style="font-size: 48px;">Loading...</p>
        <p id="statusText" style="font-size: 32px;"></p>

        <canvas id="lightChart" width="600" height="300" style="margin: 0 auto; display: block;"></canvas>

        <script>
            let chart;
            const REFRESH_MS = {REFRESH_INTERVAL_MS};
            let remaining = REFRESH_MS;

            async function loadCurrent() {{
                const res = await fetch('/current');
                const data = await res.json();

                document.getElementById('valueText').innerText = 'Light: ' + data.value;
                const statusEl = document.getElementById('statusText');
                statusEl.innerText = data.status;
                statusEl.style.color = data.status === 'SAFE' ? 'green' : (data.status === 'ALERT' ? 'red' : 'grey');
            }}

            async function loadChart() {{
                const res = await fetch('/chart-data');
                const data = await res.json();
                const labels = data.labels.map(ts => new Date(ts).toLocaleTimeString());

                if (!chart) {{
                    chart = new Chart(document.getElementById('lightChart'), {{
                        type: 'line',
                        data: {{
                            labels: labels,
                            datasets: [{{
                                label: 'Light level',
                                data: data.values,
                                borderColor: 'green',
                                tension: 0.2
                            }}]
                        }},
                        options: {{
                            scales: {{ y: {{ beginAtZero: true }} }},
                            animation: false
                        }}
                    }});
                }} else {{
                    chart.data.labels = labels;
                    chart.data.datasets[0].data = data.values;
                    chart.update();
                }}
            }}

            function refreshAll() {{
                loadCurrent();
                loadChart();
                remaining = REFRESH_MS;
            }}

            // Countdown ticks every 100ms for a smooth display
            setInterval(() => {{
                remaining -= 100;
                if (remaining < 0) remaining = 0;
                document.getElementById('countdownValue').innerText = remaining;
            }}, 100);

            setInterval(refreshAll, REFRESH_MS);
            refreshAll();
        </script>
    </body>
    </html>
    """


@app.route("/current")
def current():
    resp = requests.get(f"{TB_API}?keys=light", headers=HEADERS)
    data = resp.json()
    latest_value = data.get("light", [{}])[0].get("value") if data.get("light") else None

    if latest_value is not None:
        value = float(latest_value)
        status = "SAFE" if SAFE_RANGE[0] <= value <= SAFE_RANGE[1] else "ALERT"
    else:
        value, status = "no data", "-"

    return jsonify({"value": value, "status": status})


@app.route("/chart-data")
def chart_data():
    end_ts = int(time.time() * 1000)
    start_ts = end_ts - (60 * 60 * 1000)

    url = f"{TB_API}?keys=light&startTs={start_ts}&endTs={end_ts}&limit=1000&orderBy=ASC"
    resp = requests.get(url, headers=HEADERS)
    data = resp.json()

    points = data.get("light", [])
    labels = [p["ts"] for p in points]
    values = [float(p["value"]) for p in points]

    return jsonify({"labels": labels, "values": values})


if __name__ == "__main__":
    app.run()
