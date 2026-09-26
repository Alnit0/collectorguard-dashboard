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


@app.route("/")
def dashboard():
    resp = requests.get(f"{TB_API}?keys=light", headers=HEADERS)
    data = resp.json()
    latest_value = data.get("light", [{}])[0].get("value") if data.get("light") else None

    if latest_value is not None:
        value = float(latest_value)
        status = "SAFE" if SAFE_RANGE[0] <= value <= SAFE_RANGE[1] else "ALERT"
        colour = "green" if status == "SAFE" else "red"
    else:
        value, status, colour = "no data", "-", "grey"

    return f"""
    <html>
    <head>
        <script src="https://cdnjs.cloudflare.com/ajax/libs/Chart.js/4.4.0/chart.umd.min.js"></script>
    </head>
    <body style="font-family: sans-serif; text-align: center; margin-top: 50px;">
        <h1>CollectorGuard Dashboard</h1>
        <p style="font-size: 48px;">Light: {value}</p>
        <p style="font-size: 32px; color: {colour};">{status}</p>

        <canvas id="lightChart" width="600" height="300" style="margin: 0 auto; display: block;"></canvas>

        <script>
            async function loadChart() {{
                const res = await fetch('/chart-data');
                const data = await res.json();

                const labels = data.labels.map(ts => new Date(ts).toLocaleTimeString());

                new Chart(document.getElementById('lightChart'), {{
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
                        scales: {{ y: {{ beginAtZero: true }} }}
                    }}
                }});
            }}
            loadChart();
        </script>

        <p><a href="/">Refresh</a></p>
    </body>
    </html>
    """


@app.route("/chart-data")
def chart_data():
    end_ts = int(time.time() * 1000)
    start_ts = end_ts - (60 * 60 * 1000)  # last 60 minutes

    url = f"{TB_API}?keys=light&startTs={start_ts}&endTs={end_ts}&limit=1000&orderBy=ASC"
    resp = requests.get(url, headers=HEADERS)
    data = resp.json()

    points = data.get("light", [])
    labels = [p["ts"] for p in points]
    values = [float(p["value"]) for p in points]

    return jsonify({"labels": labels, "values": values})


if __name__ == "__main__":
    app.run()
