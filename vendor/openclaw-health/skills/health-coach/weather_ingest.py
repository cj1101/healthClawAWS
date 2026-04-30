#!/usr/bin/env python3
"""
Weather + AQI ingestion for Fort Greene, Brooklyn.

Uses the Open-Meteo API (https://open-meteo.com / https://air-quality-api.open-meteo.com).
No API key required. Runs as a standalone CLI or importable module.

Usage:
    python weather_ingest.py fetch          # fetch + ingest now
    python weather_ingest.py status         # show latest stored record
"""
from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Optional

import requests

SKILL_DIR = Path(__file__).resolve().parent

# Fort Greene, Brooklyn coordinates
LAT = 40.6892
LON = -73.9442
LOCATION = "Fort Greene, Brooklyn"

WEATHER_URL = "https://api.open-meteo.com/v1/forecast"
AQI_URL = "https://air-quality-api.open-meteo.com/v1/air-quality"

WMO_CONDITIONS: Dict[int, str] = {
    0: "Clear sky", 1: "Mainly clear", 2: "Partly cloudy", 3: "Overcast",
    45: "Fog", 48: "Icy fog",
    51: "Light drizzle", 53: "Moderate drizzle", 55: "Heavy drizzle",
    61: "Light rain", 63: "Moderate rain", 65: "Heavy rain",
    71: "Light snow", 73: "Moderate snow", 75: "Heavy snow",
    77: "Snow grains",
    80: "Light showers", 81: "Moderate showers", 82: "Heavy showers",
    85: "Slight snow showers", 86: "Heavy snow showers",
    95: "Thunderstorm", 96: "Thunderstorm + hail", 99: "Thunderstorm + heavy hail",
}


def _aqi_category(us_aqi: Optional[int]) -> str:
    if us_aqi is None:
        return "Unknown"
    if us_aqi <= 50:
        return "Good"
    if us_aqi <= 100:
        return "Moderate"
    if us_aqi <= 150:
        return "Unhealthy for Sensitive Groups"
    if us_aqi <= 200:
        return "Unhealthy"
    if us_aqi <= 300:
        return "Very Unhealthy"
    return "Hazardous"


def fetch_weather() -> Dict[str, Any]:
    """Fetch current weather from Open-Meteo and return parsed dict."""
    params = {
        "latitude": LAT,
        "longitude": LON,
        "current": [
            "temperature_2m",
            "apparent_temperature",
            "relative_humidity_2m",
            "weather_code",
            "wind_speed_10m",
        ],
        "timezone": "America/New_York",
    }
    resp = requests.get(WEATHER_URL, params=params, timeout=15)
    resp.raise_for_status()
    data = resp.json()
    current = data.get("current", {})

    code = current.get("weather_code")
    condition = WMO_CONDITIONS.get(int(code), f"Code {code}") if code is not None else None

    return {
        "temp_c": current.get("temperature_2m"),
        "feels_like_c": current.get("apparent_temperature"),
        "humidity_pct": current.get("relative_humidity_2m"),
        "wind_kph": current.get("wind_speed_10m"),
        "condition": condition,
        "raw": data,
    }


def fetch_aqi() -> Dict[str, Any]:
    """Fetch current AQI from Open-Meteo Air Quality and return parsed dict."""
    params = {
        "latitude": LAT,
        "longitude": LON,
        "current": ["us_aqi", "pm10", "pm2_5"],
        "timezone": "America/New_York",
    }
    resp = requests.get(AQI_URL, params=params, timeout=15)
    resp.raise_for_status()
    data = resp.json()
    current = data.get("current", {})

    aqi_val = current.get("us_aqi")
    pm25 = current.get("pm2_5")
    pm10 = current.get("pm10")
    category = _aqi_category(aqi_val)

    return {
        "aqi": int(aqi_val) if aqi_val is not None else None,
        "pm25": pm25,
        "pm10": pm10,
        "category": category,
        "raw": data,
    }


def fetch_and_ingest() -> Dict[str, Any]:
    """Fetch weather + AQI and write both to the SQLite store."""
    from db_ingest import ingest_aqi_record, ingest_weather_record

    obs_ts = datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")
    result: Dict[str, Any] = {"obs_ts": obs_ts}

    try:
        weather = fetch_weather()
        ingest_weather_record(
            obs_ts,
            temp_c=weather["temp_c"],
            feels_like_c=weather["feels_like_c"],
            humidity_pct=weather["humidity_pct"],
            condition=weather["condition"],
            wind_kph=weather["wind_kph"],
            location=LOCATION,
            lat=LAT,
            lon=LON,
            raw=weather["raw"],
        )
        result["weather"] = {
            "temp_c": weather["temp_c"],
            "feels_like_c": weather["feels_like_c"],
            "humidity_pct": weather["humidity_pct"],
            "condition": weather["condition"],
            "wind_kph": weather["wind_kph"],
        }
    except Exception as exc:
        result["weather_error"] = str(exc)

    try:
        aqi = fetch_aqi()
        ingest_aqi_record(
            obs_ts,
            aqi=aqi["aqi"],
            pm25=aqi["pm25"],
            pm10=aqi["pm10"],
            category=aqi["category"],
            location=LOCATION,
            source="open-meteo",
            raw=aqi["raw"],
        )
        result["aqi"] = {
            "aqi": aqi["aqi"],
            "pm25": aqi["pm25"],
            "pm10": aqi["pm10"],
            "category": aqi["category"],
        }
    except Exception as exc:
        result["aqi_error"] = str(exc)

    return result


def cmd_fetch(_args: argparse.Namespace) -> int:
    result = fetch_and_ingest()
    print(json.dumps(result, ensure_ascii=False))
    return 0 if "weather_error" not in result else 1


def cmd_status(_args: argparse.Namespace) -> int:
    from health_db import get_latest_aqi, get_latest_weather
    weather = get_latest_weather()
    aqi = get_latest_aqi()
    print(json.dumps({"weather": weather, "aqi": aqi}, ensure_ascii=False))
    return 0


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Weather + AQI ingestion for Fort Greene, Brooklyn")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("fetch", help="Fetch and ingest current conditions").set_defaults(func=cmd_fetch)
    sub.add_parser("status", help="Show latest stored observation").set_defaults(func=cmd_status)
    return parser


def main() -> int:
    parser = _build_parser()
    args = parser.parse_args()
    try:
        return args.func(args)
    except Exception as exc:
        print(json.dumps({"error": str(exc)}))
        return 1


if __name__ == "__main__":
    sys.exit(main())
