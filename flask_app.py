import os
from flask import Flask
import requests

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
    <body style="font-family: sans-serif; text-align: center; margin-top: 100px;">
        <h1>CollectorGuard MVP</h1>
        <p style="font-size: 48px;">Light: {value}</p>
        <p style="font-size: 32px; color: {colour};">{status}</p>
        <p><a href="/">Refresh</a></p>
    </body>
    </html>
    """

if __name__ == "__main__":
    app.run()
