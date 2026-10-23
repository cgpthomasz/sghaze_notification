#!/usr/bin/env python3
"""
Singapore Haze PSI + PM2.5 Monitor

Pulls 24-hour PSI, 24-hour PM2.5, 1-hour PM2.5, air temperature, relative
humidity, and 2-hour rain forecast from data.gov.sg (NEA data), and sends a
Telegram message only when the API's timestamp changes.

Runs hourly via GitHub Actions. Credentials come from environment variables
(set as GitHub Secrets):
    TELEGRAM_BOT_TOKEN
    TELEGRAM_CHAT_ID

CLI:
    python psi_monitor.py                          # normal run
    python psi_monitor.py --preview                # print only, no send, no state
    python psi_monitor.py --force                  # send even if unchanged
    python psi_monitor.py --to 123456789           # override chat target (testing)
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import requests

# Optional: town-to-region mapping for rain forecast. If missing, rain
# information is silently skipped — the rest of the script runs normally.
try:
    from regions import region_for_town
    _REGIONS_AVAILABLE = True
except ImportError:
    region_for_town = None
    _REGIONS_AVAILABLE = False

# =============================================================================
# Configuration
# =============================================================================

# Metric toggles — flip to True to include in the message.
SEND_PSI_24H  = True
SEND_PM25_24H = True
SEND_PM25_1H  = True

# --- Alert gating (optional) ---
# If SEND_ONLY_ON_ALERT is True, the script sends only when EITHER:
#   - any region's 1-hour PM2.5 exceeds ALERT_1H_PM25_THRESHOLD, OR
#   - any region has heavy rain / thundery showers forecast in the next 2h
#
# Default: False (send on any timestamp change, current behaviour).
SEND_ONLY_ON_ALERT         = True
ALERT_1H_PM25_THRESHOLD    = 0      # µg/m³ — raise to e.g. 56 for "Elevated and above"

# Endpoints (verified 2026-10)
PSI_URL           = "https://api-open.data.gov.sg/v2/real-time/api/psi"
PM25_URL          = "https://api-open.data.gov.sg/v2/real-time/api/pm25"
PM25_FALLBACK_URL = "https://api.data.gov.sg/v1/environment/pm25"
TEMP_URL          = "https://api-open.data.gov.sg/v2/real-time/api/air-temperature"
HUMIDITY_URL      = "https://api-open.data.gov.sg/v2/real-time/api/relative-humidity"
FORECAST_URL      = "https://api-open.data.gov.sg/v2/real-time/api/two-hr-forecast"

# Runtime settings
HTTP_TIMEOUT       = 30          # seconds
MAX_RETRIES        = 3
MAX_DATA_AGE_HOURS = 3           # reject stale readings
TELEGRAM_RETRIES   = 3

# Singapore time
SGT = timezone(timedelta(hours=8))

# State file (cached between GitHub Actions runs, not committed)
STATE_FILE = Path(__file__).with_name(".psi_state.json")

# Telegram credentials
TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID   = os.environ.get("TELEGRAM_CHAT_ID")

# Region display order (left to right) and abbreviations
REGION_ORDER = ["central", "west", "south", "east", "north"]
REGION_ABBR  = {"central": "C", "west": "W", "south": "S", "east": "E", "north": "N"}

# Horizontal rule — 50% width, used between title and table, and above the rain line
HR = "━" * 18

# =============================================================================
# Logging
# =============================================================================

logging.basicConfig(level=logging.INFO,
                    format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("psi_monitor")

if not _REGIONS_AVAILABLE:
    log.warning("regions.py not found — rain forecast will be skipped")

# =============================================================================
# Air quality bands
# =============================================================================

# Severity rank used to pick the "worst" emoji for the title
COLOR_RANK = {"🟢": 0, "🟡": 1, "🟠": 2, "🔴": 3, "💀": 4, "⚪": -1}


def band_emoji(value: float, is_psi: bool = False) -> str:
    """Return a single colored circle representing the air-quality band.

    PSI bands (NEA):
        0-50    🟢 Good
        51-100  🟡 Moderate
        101-200 🟠 Unhealthy
        201-300 🔴 Very Unhealthy
        >=301   💀 Hazardous

    PM2.5 bands (NEA 1h):
        0-55    🟢 Normal
        56-100  🟡 Elevated (lower)
        101-150 🟠 Elevated (upper)
        151-250 🔴 High
        >=251   💀 Very High
    """
    if is_psi:
        if value <= 50:   return "🟢"
        if value <= 100:  return "🟡"
        if value <= 200:  return "🟠"
        if value <= 300:  return "🔴"
        return "💀"

    if value <= 55:   return "🟢"
    if value <= 100:  return "🟡"
    if value <= 150:  return "🟠"
    if value <= 250:  return "🔴"
    return "💀"


def worst_color(emojis: list[str]) -> str:
    """Return the emoji with the highest severity rank."""
    return max(emojis, key=lambda e: COLOR_RANK.get(e, -1))


def trend_arrow(current: float | None, previous: float | None) -> str:
    """Return ↑ (rose), ↓ (fell), or ' ' (unchanged / no previous)."""
    if current is None or previous is None:
        return " "
    if current > previous:
        return "↑"
    if current < previous:
        return "↓"
    return " "

# =============================================================================
# Rain forecast helpers
# =============================================================================

RAIN_KEYWORDS = ("rain", "shower")

FORECAST_SEVERITY = {
    "heavy thundery showers with gusty winds": 6,
    "heavy thundery showers":                  5,
    "thundery showers":                        4,
    "heavy showers":                           3,
    "heavy rain":                              3,
    "showers":                                 2,
    "moderate rain":                           2,
    "light showers":                           1,
    "light rain":                              1,
    "passing showers":                         1,
}


def is_rainy(forecast: str) -> bool:
    """True if the forecast text mentions rain or showers."""
    f = forecast.lower()
    return any(kw in f for kw in RAIN_KEYWORDS)


def is_heavy_or_thundery(forecast: str) -> bool:
    """True if the forecast mentions heavy rain/showers or thundery showers."""
    f = forecast.lower()
    if not is_rainy(f):
        return False
    return "heavy" in f or "thundery" in f


def rain_emoji(forecast: str) -> str:
    """Pick an emoji based on the nature of the rain forecast."""
    f = forecast.lower()
    if "thundery" in f:
        return "⛈️"
    if "heavy" in f:
        return "🌧️"
    return "🌦️"


def forecast_severity(forecast: str) -> int:
    """Return a numeric severity rank for a forecast string."""
    return FORECAST_SEVERITY.get(forecast.lower(), 0)


# =============================================================================
# Alert gating
# =============================================================================

def is_alert_condition(readings: dict) -> bool:
    """Return True if alert conditions are met:
    - Any region's 1h PM2.5 exceeds ALERT_1H_PM25_THRESHOLD, OR
    - Any region has heavy rain / thundery showers in the forecast.
    """
    # 1h PM2.5 check
    pm1_vals = readings.get("pm25_1h", {})
    if any(v > ALERT_1H_PM25_THRESHOLD for v in pm1_vals.values()):
        return True

    # Rain check
    forecast = readings.get("forecast", {})
    for fc in forecast.values():
        if is_heavy_or_thundery(fc):
            return True

    return False

# =============================================================================
# HTTP helpers
# =============================================================================

def get_json(url: str) -> dict:
    """GET a URL and return JSON, with retries and exponential backoff."""
    last_error: Exception | None = None
    for attempt in range(1, MAX_RETRIES + 1):
        try:
            r = requests.get(url, timeout=HTTP_TIMEOUT,
                             headers={"Accept": "application/json"})
            r.raise_for_status()
            return r.json()
        except (requests.RequestException, ValueError) as e:
            last_error = e
            log.warning("Attempt %d/%d %s failed: %s",
                        attempt, MAX_RETRIES, url, e)
            time.sleep(2 ** attempt)
    raise RuntimeError(f"Failed to fetch {url}: {last_error}")

# =============================================================================
# Data fetching
# =============================================================================

def _parse_v2(payload: dict) -> tuple[str, dict]:
    """Return (timestamp, readings)."""
    item = payload["data"]["items"][0]
    return item["timestamp"], item["readings"]


def _check_fresh(ts_str: str) -> datetime:
    ts = datetime.fromisoformat(ts_str.replace("Z", "+00:00"))
    age = datetime.now(SGT) - ts.astimezone(SGT)
    if age > timedelta(hours=MAX_DATA_AGE_HOURS):
        raise RuntimeError(f"Data is stale: {ts_str} ({age} old)")
    return ts


def _parse_stations(payload: dict) -> list[float]:
    """Extract all station values from a v2 weather API response."""
    item = payload["data"]["readings"][0]
    return [float(r["value"]) for r in item["data"]]


def _parse_forecast(payload: dict) -> dict[str, str]:
    """Return {town_name: forecast_string} from a two-hr-forecast response."""
    item = payload["data"]["items"][0]
    return {f["area"]: f["forecast"] for f in item["forecasts"]}


def fetch_all() -> dict:
    """Fetch PSI, PM2.5, temperature, humidity, and 2-hour rain forecast."""
    result: dict[str, Any] = {"psi_24h": {}, "pm25_24h": {}, "pm25_1h": {}}
    timestamps: list[str] = []

    ts, readings = _parse_v2(get_json(PSI_URL))
    _check_fresh(ts)
    result["psi_24h"]  = {k.title(): int(v)
                          for k, v in readings["psi_twenty_four_hourly"].items()}
    result["pm25_24h"] = {k.title(): int(v)
                          for k, v in readings["pm25_twenty_four_hourly"].items()}
    timestamps.append(ts)
    log.info("PSI fetched from data.gov.sg v2 (timestamp %s)", ts)

    try:
        ts, readings = _parse_v2(get_json(PM25_URL))
        _check_fresh(ts)
        result["pm25_1h"] = {k.title(): float(v)
                             for k, v in readings["pm25_one_hourly"].items()}
        timestamps.append(ts)
        log.info("PM2.5 fetched from data.gov.sg v2 (timestamp %s)", ts)
    except Exception as e:
        log.warning("v2 PM2.5 failed (%s); trying v1 fallback", e)
        fallback = get_json(PM25_FALLBACK_URL)
        item = fallback["items"][0]
        ts = item["timestamp"]
        _check_fresh(ts)
        result["pm25_1h"] = {k.title(): float(v)
                             for k, v in item["readings"]["pm25_one_hourly"].items()}
        timestamps.append(ts)
        log.info("PM2.5 fetched from v1 fallback (timestamp %s)", ts)

    result["timestamp"] = max(timestamps)

    try:
        temp_values = _parse_stations(get_json(TEMP_URL))
        result["temp_min"] = min(temp_values)
        result["temp_max"] = max(temp_values)
        log.info("Temperature fetched: %.1f-%.1f°C",
                 result["temp_min"], result["temp_max"])
    except Exception as e:
        log.warning("Temperature fetch failed: %s", e)
        result["temp_min"] = None
        result["temp_max"] = None

    try:
        hum_values = _parse_stations(get_json(HUMIDITY_URL))
        result["humidity_min"] = min(hum_values)
        result["humidity_max"] = max(hum_values)
        log.info("Humidity fetched: %.0f-%.0f%%",
                 result["humidity_min"], result["humidity_max"])
    except Exception as e:
        log.warning("Humidity fetch failed: %s", e)
        result["humidity_min"] = None
        result["humidity_max"] = None

    if _REGIONS_AVAILABLE:
        try:
            result["forecast"] = _parse_forecast(get_json(FORECAST_URL))
            log.info("Forecast fetched (%d towns)", len(result["forecast"]))
        except Exception as e:
            log.warning("Forecast fetch failed: %s", e)
            result["forecast"] = {}
    else:
        result["forecast"] = {}

    return result

# =============================================================================
# State persistence
# =============================================================================

def load_state() -> dict:
    if STATE_FILE.exists():
        try:
            return json.loads(STATE_FILE.read_text())
        except (json.JSONDecodeError, OSError):
            log.warning("State file corrupt; starting fresh")
    return {}


def save_state(state: dict) -> None:
    tmp = STATE_FILE.with_suffix(".tmp")
    tmp.write_text(json.dumps(state, indent=2))
    tmp.replace(STATE_FILE)


def state_key(data: dict | None) -> str:
    """Return the field used to detect changes: the API's timestamp."""
    if not data:
        return ""
    return data.get("timestamp", "")

# =============================================================================
# Telegram
# =============================================================================

def send_telegram(text: str, chat_id: str | None = None) -> None:
    target = chat_id or TELEGRAM_CHAT_ID
    if not TELEGRAM_BOT_TOKEN or not target:
        raise RuntimeError("TELEGRAM_BOT_TOKEN and a chat ID must be set")

    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    payload = {"chat_id": target, "text": text,
               "disable_web_page_preview": True,
               "parse_mode": "HTML"}

    for attempt in range(1, TELEGRAM_RETRIES + 1):
        try:
            r = requests.post(url, json=payload, timeout=HTTP_TIMEOUT)
            data = r.json()

            if data.get("ok"):
                return

            if r.status_code == 429:
                wait = data.get("parameters", {}).get("retry_after", 5)
                log.warning("Telegram rate-limited; sleeping %ss", wait)
                time.sleep(wait)
                continue

            raise RuntimeError(f"Telegram error: {data}")

        except requests.RequestException as e:
            log.warning("Telegram attempt %d/%d failed: %s",
                        attempt, TELEGRAM_RETRIES, e)
            time.sleep(2 ** attempt)

    raise RuntimeError("Telegram send failed after retries")

# =============================================================================
# Message formatting
# =============================================================================

def fmt_timestamp(ts_iso: str) -> str:
    """Format the API's timestamp as '10 Oct, 08:01 pm' (SGT)."""
    ts = datetime.fromisoformat(ts_iso.replace("Z", "+00:00"))
    dt = ts.astimezone(SGT)
    ampm = "am" if dt.hour < 12 else "pm"
    return f"{dt.strftime('%d %b, %I:%M')} {ampm}"


def fmt_weather(readings: dict) -> str:
    """Return the temp/humidity line, or '' if unavailable."""
    t_min = readings.get("temp_min")
    t_max = readings.get("temp_max")
    h_min = readings.get("humidity_min")
    h_max = readings.get("humidity_max")

    if t_min is None and h_min is None:
        return ""

    parts = []
    if t_min is not None and t_max is not None:
        parts.append(f"🌡️ {t_min:.1f}-{t_max:.1f}°c")
    if h_min is not None and h_max is not None:
        parts.append(f"💧 {h_min:.0f}-{h_max:.0f}%")

    return " | ".join(parts)


def fmt_rain_forecast(readings: dict) -> str:
    """Return a rain line if any region is forecast to get rain in the next 2h.

    The umbrella emoji (☔) is appended only when the worst forecast is
    heavy rain or thundery showers — light/moderate rain gets the rain
    emoji without the umbrella.
    """
    if not _REGIONS_AVAILABLE:
        return ""

    forecast = readings.get("forecast", {})
    if not forecast:
        return ""

    region_worst: dict[str, tuple[int, str]] = {}
    for town, fc in forecast.items():
        if not is_rainy(fc):
            continue
        region = region_for_town(town)
        if region is None:
            continue
        sev = forecast_severity(fc)
        prev = region_worst.get(region)
        if prev is None or sev > prev[0]:
            region_worst[region] = (sev, fc)

    if not region_worst:
        return ""

    overall_worst_fc = max(region_worst.values(), key=lambda x: x[0])[1]
    emoji = rain_emoji(overall_worst_fc)

    ordered = [r for r in REGION_ORDER if r in region_worst]
    region_names = "/".join(r.title() for r in ordered)
    area_word = "area" if len(ordered) == 1 else "areas"

    line = f"{emoji} Next 2hrs in {region_names} {area_word}"
    if is_heavy_or_thundery(overall_worst_fc):
        line += " ☔"
    return line


def _pad(value: float | None, arrow: str, num_width: int) -> str:
    """Right-justify the number, add a space, then arrow, then trailing space."""
    if value is None:
        return "-" * (num_width + 3)
    return f"{int(value):>{num_width}} {arrow} "


def format_message(readings: dict, prev: dict | None = None) -> str:
    """Build the Telegram message (HTML)."""
    lines: list[str] = []

    # Rain forecast block — at the very top, if applicable
    rain = fmt_rain_forecast(readings)
    if rain:
        lines.append(rain)
        lines.append(HR)

    # Pull readings per metric
    psi_vals  = readings.get("psi_24h",  {})
    pm1_vals  = readings.get("pm25_1h", {})
    pm24_vals = readings.get("pm25_24h", {})

    # Previous readings (for trend arrows)
    prev = prev or {}
    prev_psi  = prev.get("psi_24h",  {})
    prev_pm1  = prev.get("pm25_1h", {})
    prev_pm24 = prev.get("pm25_24h", {})

    psi_max  = max(psi_vals.values())  if psi_vals  else None
    pm1_max  = max(pm1_vals.values())  if pm1_vals  else None
    pm24_max = max(pm24_vals.values()) if pm24_vals else None

    psi_dot  = band_emoji(psi_max,  is_psi=True)  if psi_max  is not None else "⚪"
    pm1_dot  = band_emoji(pm1_max,  is_psi=False) if pm1_max  is not None else "⚪"
    pm24_dot = band_emoji(pm24_max, is_psi=False) if pm24_max is not None else "⚪"

    title_dot = worst_color([psi_dot, pm1_dot, pm24_dot])

    if readings.get("timestamp"):
        lines.append(f"{title_dot} <b>PSI Updated:</b> {fmt_timestamp(readings['timestamp'])}")
        lines.append(HR)

    header     = "Reg  | 1h 2.5 | 24h 2.5 | 24h PSI"
    circle_row = f"     |   {pm1_dot}   |   {pm24_dot}    |   {psi_dot}   "
    sep        = "-----+--------+---------+--------"

    table = [
        f"<code>{header}</code>",
        f"<code>{circle_row}</code>",
        f"<code>{sep}</code>",
    ]

    for region_key in REGION_ORDER:
        region_title = region_key.title()
        abbr = REGION_ABBR[region_key]

        psi_v  = psi_vals.get(region_title)
        pm1_v  = pm1_vals.get(region_title)
        pm24_v = pm24_vals.get(region_title)

        pm1_arrow  = trend_arrow(pm1_v,  prev_pm1.get(region_title))
        pm24_arrow = trend_arrow(pm24_v, prev_pm24.get(region_title))
        psi_arrow  = trend_arrow(psi_v,  prev_psi.get(region_title))

        reg_cell  = f" {abbr}   "
        pm1_cell  = _pad(pm1_v,  pm1_arrow,  5)
        pm24_cell = _pad(pm24_v, pm24_arrow, 6)
        psi_cell  = _pad(psi_v,  psi_arrow,  5)

        table.append(f"<code>{reg_cell}|{pm1_cell}|{pm24_cell}|{psi_cell}</code>")

    lines.append("<blockquote>" + "\n".join(table) + "</blockquote>")

    weather = fmt_weather(readings)
    if weather:
        lines.append(f"<code>{weather}</code>")

    return "\n".join(lines)

# =============================================================================
# Entry point
# =============================================================================

def main() -> int:
    ap = argparse.ArgumentParser(description="Singapore haze monitor")
    ap.add_argument("--preview", action="store_true",
                    help="Print the message without sending or saving state")
    ap.add_a
