#!/usr/bin/env python3
"""
glooko-nightscout-sync
Polls Glooko for Huxley's Omnipod 5 bolus data and pushes it to Nightscout.
"""

import os
import time
import logging
import hashlib
import json
from datetime import datetime, timezone, timedelta

import requests

# ── CONFIG ────────────────────────────────────────────────────────────────────
GLOOKO_EMAIL    = os.environ["GLOOKO_EMAIL"]
GLOOKO_PASSWORD = os.environ["GLOOKO_PASSWORD"]
NS_URL          = os.environ["NS_URL"].rstrip("/")
NS_SECRET       = os.environ["NS_SECRET"]
POLL_INTERVAL   = int(os.environ.get("POLL_INTERVAL_MINS", "30")) * 60
LOOKBACK_HOURS  = int(os.environ.get("LOOKBACK_HOURS", "26"))
DEBUG_RAW       = os.environ.get("DEBUG_RAW", "false").lower() == "true"

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [glooko-sync] %(levelname)s: %(message)s",
    datefmt="%Y-%m-%dT%H:%M:%S"
)
log = logging.getLogger(__name__)

NS_HEADERS = {
    "api-secret": hashlib.sha1(NS_SECRET.encode()).hexdigest(),
    "Content-Type": "application/json",
    "Accept": "application/json",
}

def ns_get_existing_treatments(since_iso):
    url = f"{NS_URL}/api/v1/treatments.json"
    params = {"find[created_at][$gte]": since_iso, "count": 500}
    r = requests.get(url, headers=NS_HEADERS, params=params, timeout=15)
    r.raise_for_status()
    return r.json()

def ns_upload_treatments(treatments):
    if not treatments:
        return
    url = f"{NS_URL}/api/v1/treatments"
    r = requests.post(url, headers=NS_HEADERS, json=treatments, timeout=15)
    r.raise_for_status()
    return r.json()

def bolus_to_treatment(bolus):
    ts = bolus.timestamp
    if isinstance(ts, (int, float)):
        ts = datetime.fromtimestamp(ts / 1000, tz=timezone.utc)

    has_carbs = bolus.carbs_input and float(bolus.carbs_input) > 0
    is_correction = (bolus.correction_units and float(bolus.correction_units) > 0) and not has_carbs

    if has_carbs:
        event_type = "Meal Bolus"
    elif is_correction:
        event_type = "Correction Bolus"
    else:
        event_type = "Combo Bolus"

    treatment = {
        "eventType": event_type,
        "created_at": ts.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "insulin": float(bolus.units),
        "enteredBy": "glooko-sync",
    }
    if has_carbs:
        treatment["carbs"] = float(bolus.carbs_input)
    if bolus.blood_glucose_input:
        treatment["glucose"] = int(bolus.blood_glucose_input)
        treatment["glucoseType"] = bolus.blood_glucose_source or "Finger"
    if bolus.insulin_on_board:
        treatment["iob"] = float(bolus.insulin_on_board)
    if bolus.is_manual is not None:
        treatment["notes"] = "manual bolus" if bolus.is_manual else "auto bolus"
    return treatment

def make_dedup_key(treatment):
    return f"{treatment['created_at']}_{treatment['insulin']}"

def sync_once(client):
    from glooko import parse_bolus_entries

    now = datetime.now(timezone.utc)
    start = now - timedelta(hours=LOOKBACK_HOURS)
    start_iso = start.strftime("%Y-%m-%dT%H:%M:%S.000Z")
    end_iso = now.strftime("%Y-%m-%dT%H:%M:%S.000Z")

    log.info(f"Fetching Glooko data from {start_iso} to {end_iso}")

    try:
        data = client.get_graph_data(start_date=start_iso, end_date=end_iso)
    except Exception as e:
        log.error(f"Glooko fetch failed: {e}")
        return 0

    # Debug: show raw bolus structure so we can see what fields are present
    if DEBUG_RAW:
        raw_boluses = data.get("bolus_entries", data.get("bolusEntries", data.get("boluses", [])))
        log.info(f"RAW bolus keys in response: {list(data.keys())}")
        if raw_boluses:
            log.info(f"RAW first bolus entry: {json.dumps(raw_boluses[0] if isinstance(raw_boluses[0], dict) else raw_boluses[0].__dict__, default=str)}")
            log.info(f"RAW total raw bolus count: {len(raw_boluses)}")

    boluses = parse_bolus_entries(data)
    log.info(f"Found {len(boluses)} bolus entries from parse_bolus_entries")

    # Debug: log each parsed bolus
    for b in boluses:
        log.info(f"  Bolus: {b.timestamp} — {b.units}u carbs={b.carbs_input} correction={b.correction_units} manual={b.is_manual}")

    # Also try to extract boluses directly from raw data as fallback
    raw_count = 0
    for key in ["bolusEntries", "bolus_entries", "boluses", "insulinDeliveries", "insulin_deliveries"]:
        if key in data:
            raw_count = len(data[key])
            log.info(f"Raw key '{key}' contains {raw_count} entries")

    if not boluses:
        log.warning("parse_bolus_entries returned 0 — checking raw data for direct extraction")
        # Try direct extraction from common Glooko response keys
        for key in ["bolusEntries", "bolus_entries", "boluses"]:
            if key in data and data[key]:
                log.info(f"Found {len(data[key])} entries under key '{key}' — sample: {json.dumps(data[key][0], default=str)[:300]}")
        return 0

    try:
        existing = ns_get_existing_treatments(start_iso)
        existing_keys = {
            make_dedup_key(t)
            for t in existing
            if t.get("insulin")
        }
        log.info(f"Found {len(existing_keys)} existing glooko-sync treatments in Nightscout")
    except Exception as e:
        log.warning(f"Could not fetch existing treatments: {e}")
        existing_keys = set()

    new_treatments = []
    for bolus in boluses:
        treatment = bolus_to_treatment(bolus)
        key = make_dedup_key(treatment)
        if key not in existing_keys:
            new_treatments.append(treatment)

    log.info(f"Uploading {len(new_treatments)} new treatments to Nightscout")

    if new_treatments:
        try:
            ns_upload_treatments(new_treatments)
            log.info(f"Successfully uploaded {len(new_treatments)} treatments")
        except Exception as e:
            log.error(f"Nightscout upload failed: {e}")
            return 0

    return len(new_treatments)

def main():
    from glooko import GlookoClient

    log.info("glooko-nightscout-sync starting")
    log.info(f"Nightscout URL: {NS_URL}")
    log.info(f"Poll interval: {POLL_INTERVAL // 60} minutes")
    log.info(f"Lookback: {LOOKBACK_HOURS} hours")

    log.info("Authenticating with Glooko...")
    try:
        client = GlookoClient(email=GLOOKO_EMAIL, password=GLOOKO_PASSWORD)
        ok = client.test_connection()
        if not ok:
            log.error("Glooko authentication failed")
            raise SystemExit(1)
        log.info("Glooko authentication successful")
    except Exception as e:
        log.error(f"Failed to connect to Glooko: {e}")
        raise SystemExit(1)

    while True:
        try:
            uploaded = sync_once(client)
            log.info(f"Sync complete. {uploaded} new treatments uploaded.")
        except Exception as e:
            log.error(f"Unexpected error during sync: {e}")

        log.info(f"Sleeping {POLL_INTERVAL // 60} minutes until next sync...")
        time.sleep(POLL_INTERVAL)

if __name__ == "__main__":
    main()
