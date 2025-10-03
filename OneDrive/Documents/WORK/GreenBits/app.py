# app.py
from flask import Flask, render_template, request, jsonify
import requests
import math
import os
import json
from datetime import datetime, timedelta

app = Flask(__name__)

# === CONFIG ===
OPENWEATHER_API_KEY = os.getenv("OPENWEATHER_API_KEY", "YOUR_OPENWEATHER_API_KEY")
# simple path to mock data
MOCK_DATA_PATH = "data/mock_local_data.json"


# === UTILITIES ===
def geocode_address(address):
    """Use Nominatim to geocode address -> (lat, lon)"""
    url = "https://nominatim.openstreetmap.org/search"
    params = {"q": address, "format": "json", "limit": 1}
    r = requests.get(url, params=params, headers={"User-Agent": "hackathon-proto/1.0"})
    r.raise_for_status()
    results = r.json()
    if not results:
        return None
    return float(results[0]["lat"]), float(results[0]["lon"]), results[0].get("display_name", "")


def fetch_aqi(lat, lon):
    """
    Use OpenWeatherMap Air Pollution API.
    Returns dict with 'aqi' (1-5) and 'components' and 'pm2_5' if available.
    """
    url = "http://api.openweathermap.org/data/2.5/air_pollution"
    params = {"lat": lat, "lon": lon, "appid": OPENWEATHER_API_KEY}
    r = requests.get(url, params=params)
    r.raise_for_status()
    data = r.json()
    if "list" in data and data["list"]:
        item = data["list"][0]
        # OpenWeather aqi scale 1(good) - 5(very poor)
        aqi_index = item.get("main", {}).get("aqi")
        components = item.get("components", {})
        pm25 = components.get("pm2_5")
        return {"aqi_index": aqi_index, "components": components, "pm2_5": pm25, "raw": item}
    return None


def load_mock_local_data():
    with open(MOCK_DATA_PATH, "r", encoding="utf-8") as f:
        return json.load(f)


def get_mock_localities_for_location(lat, lon, lookup):
    """
    Simple approach: try to match a nearby pincode in the mock lookup by the
    smallest haversine distance to sample lat/lon pairs contained in lookup.
    The mock file can include sample lat/lon for each pincode or we simply fall back
    to matching by pincode string if user input was pincode.
    For this prototype, we'll look for the closest sample coordinate stored in lookup.
    """
    # if lookup contains 'samples' each with lat/lon use that, else fallback to first entry
    best_key = None
    best_dist = float("inf")

    for key, value in lookup.items():
        sample = value.get("sample_latlon")
        if sample:
            d = (lat - sample[0]) ** 2 + (lon - sample[1]) ** 2
            if d < best_dist:
                best_dist = d
                best_key = key

    if best_key:
        return lookup[best_key]
    # fallback: return a random-ish first entry
    first_key = next(iter(lookup))
    return lookup[first_key]


def compute_health_score(aqi_pm25, noise_db, complaints_count):
    """
    Simple formula (tune as needed):
      - AQI contribution: scale PM2.5 (µg/m3) to 0-60 impact
      - Noise contribution: scale dB typical range 40-100 to 0-20 impact
      - Complaints: each complaint - up to 20 impact (capped)
    Output: score 0-100 (higher = healthier)
    """
    # guard
    if aqi_pm25 is None:
        aqi_pm25 = 35.0

    # AQI impact: assume 0µg/m3 -> 0 impact, 150µg/m3 -> 60 impact
    aqi_impact = min(max(aqi_pm25, 0) / 150.0 * 60.0, 60.0)

    # Noise impact: map 40-100 dB to 0-20
    noise_impact = 0.0
    if noise_db is not None:
        noise_impact = min(max(noise_db - 40.0, 0) / 60.0 * 20.0, 20.0)

    # Complaints: each complaint counts up to a cap
    complaints_impact = min(complaints_count * 2.0, 20.0)

    total_impact = aqi_impact + noise_impact + complaints_impact
    raw_score = max(0.0, 100.0 - total_impact)
    # clamp
    score = max(0.0, min(100.0, raw_score))
    return round(score, 1), {"aqi_impact": round(aqi_impact, 2),
                              "noise_impact": round(noise_impact, 2),
                              "complaints_impact": round(complaints_impact, 2)}


def generate_mock_aqi_trend(pm25):
    """
    Create 7-day mock history around current pm2_5 for frontend chart (simple).
    """
    base = pm25 if pm25 is not None else 40.0
    days = []
    values = []
    for i in range(6, -1, -1):
        dt = (datetime.utcnow() - timedelta(days=i)).strftime("%Y-%m-%d")
        # small sinusoidal + noise
        v = max(5.0, base + (math.sin(i) * 5.0) + (i - 3) * 1.5)
        v = round(v + (i % 3 - 1), 1)
        days.append(dt)
        values.append(v)
    return {"dates": days, "pm25": values}


# === ROUTES ===
@app.route("/")
def index():
    return render_template("index.html")


@app.route("/api/report", methods=["POST"])
def api_report():
    data = request.json or {}
    address = data.get("address", "").strip()
    if not address:
        return jsonify({"error": "address required"}), 400

    try:
        geocode = geocode_address(address)
        if geocode is None:
            return jsonify({"error": "could not geocode address"}), 400
        lat, lon, display = geocode

        # fetch AQI
        try:
            aqi = fetch_aqi(lat, lon)
        except Exception as e:
            # keep going with None if API fails
            aqi = None

        mock = load_mock_local_data()
        local = get_mock_localities_for_location(lat, lon, mock)

        # get convenient values
        noise_db = local.get("noise_db", None)
        complaints = local.get("complaints", 0)
        sample_pincode = local.get("pincode", None)

        # pick pm25 from external aqi if present
        pm25 = None
        if aqi and "pm2_5" in aqi:
            pm25 = aqi.get("pm2_5")
        elif aqi and "components" in aqi:
            pm25 = aqi["components"].get("pm2_5")

        score, breakdown = compute_health_score(pm25, noise_db, complaints)

        trend = generate_mock_aqi_trend(pm25 if pm25 is not None else 40.0)

        resp = {
            "address": display,
            "lat": lat,
            "lon": lon,
            "pincode": sample_pincode,
            "aqi": aqi,
            "pm25": pm25,
            "noise_db": noise_db,
            "complaints": complaints,
            "score": score,
            "breakdown": breakdown,
            "trend": trend
        }
        return jsonify(resp)
    except Exception as exc:
        return jsonify({"error": str(exc)}), 500


if __name__ == "__main__":
    app.run(debug=True)
