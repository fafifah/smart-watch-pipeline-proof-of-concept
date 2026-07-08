"""
extract_health_data.py
======================
Wearable Sensing Pipeline — Data Extraction & Merge Script

What this script does
---------------------
1. Reads your Firebase walk sessions (session ID, timestamps, GPS, questionnaires).
2. Reads a JSON export from the "Health Auto Export" iPhone app.
3. For each walk session, extracts from Apple Health:
   - Average, min, max heart rate
   - Average SpO2
   - Step count
   - Active energy burned
4. Calculates from GPS coordinates:
   - Total distance walked (metres)
   - Average walking speed (m/s)
5. Merges everything into one clean CSV and JSON file.
   The JSON is written to data/sessions.json — the path the dashboard reads.

Requirements
------------
    pip install firebase-admin pandas geopy python-dotenv

Setup
-----
1.  Go to Firebase console → Project settings → Service accounts
    → Generate new private key → save as firebase_key.json in this folder.
2.  Export data from "Health Auto Export" app on your iPhone (JSON format).
    Save the file anywhere and pass its path with --health-export.
3.  Run:
        python scripts/extract_health_data.py \\
            --firebase-key scripts/firebase_key.json \\
            --health-export ~/Downloads/health_export.json \\
            --output-dir data
"""

import argparse
import json
import math
import os
import sys
import zipfile
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from pathlib import Path

# Always resolve paths relative to the project root (one level up from this script)
PROJECT_ROOT = Path(__file__).resolve().parent.parent

# ── Optional deps — give friendly errors ──────────────────────────────────────
try:
    import pandas as pd
except ImportError:
    sys.exit("Missing dependency: run  pip install pandas")

try:
    import firebase_admin
    from firebase_admin import credentials, firestore
except ImportError:
    sys.exit("Missing dependency: run  pip install firebase-admin")


# ─────────────────────────────────────────────────────────────────────────────
# GPS UTILITIES
# ─────────────────────────────────────────────────────────────────────────────

def haversine_metres(lat1, lon1, lat2, lon2):
    """Return the great-circle distance in metres between two GPS points."""
    R = 6_371_000  # Earth radius in metres
    phi1, phi2 = math.radians(lat1), math.radians(lat2)
    dphi = math.radians(lat2 - lat1)
    dlam = math.radians(lon2 - lon1)
    a = math.sin(dphi / 2) ** 2 + math.cos(phi1) * math.cos(phi2) * math.sin(dlam / 2) ** 2
    return 2 * R * math.asin(math.sqrt(a))


def compute_gps_stats(gps_points):
    """
    Given a list of {lat, lon, ts} dicts, return:
        total_distance_m  — total path length in metres
        avg_speed_ms      — average speed in m/s
    """
    if len(gps_points) < 2:
        return 0.0, 0.0

    total_m = 0.0
    for i in range(1, len(gps_points)):
        p1, p2 = gps_points[i - 1], gps_points[i]
        total_m += haversine_metres(p1["lat"], p1["lon"], p2["lat"], p2["lon"])

    try:
        t_start = datetime.fromisoformat(gps_points[0]["ts"].replace("Z", "+00:00"))
        t_end   = datetime.fromisoformat(gps_points[-1]["ts"].replace("Z", "+00:00"))
        elapsed = (t_end - t_start).total_seconds()
        avg_speed = total_m / elapsed if elapsed > 0 else 0.0
    except Exception:
        avg_speed = 0.0

    return round(total_m, 1), round(avg_speed, 3)


# ─────────────────────────────────────────────────────────────────────────────
# FIREBASE READER
# ─────────────────────────────────────────────────────────────────────────────

def load_firebase_sessions(key_path):
    """Fetch all walk sessions from Firestore and return as a list of dicts."""
    cred = credentials.Certificate(key_path)
    if not firebase_admin._apps:
        firebase_admin.initialize_app(cred)

    db   = firestore.client()
    docs = db.collection("walk_sessions").stream()

    sessions = []
    for doc in docs:
        data = doc.to_dict()
        data["session_id"] = doc.id
        sessions.append(data)

    print(f"  Loaded {len(sessions)} session(s) from Firebase.")
    return sessions


# ─────────────────────────────────────────────────────────────────────────────
# APPLE HEALTH PARSER  (supports JSON from Health Auto Export OR native XML/ZIP)
# ─────────────────────────────────────────────────────────────────────────────

def parse_iso(ts_str):
    """Parse an ISO-8601 or Apple Health timestamp into a timezone-aware datetime."""
    if not ts_str:
        return None
    try:
        # Apple native XML uses "2026-07-01 14:02:47 +0100"
        ts_str = ts_str.strip()
        if " " in ts_str and not ts_str[10] == "T":
            ts_str = ts_str[:19].replace(" ", "T") + ts_str[19:]
        return datetime.fromisoformat(ts_str.replace("Z", "+00:00"))
    except Exception:
        return None


def load_health_export(path: Path):
    """
    Load Apple Health data from:
      - Apple's native export.zip  (exported from the Health app)
      - Apple's native export.xml  (unzipped from the above)
      - Health Auto Export .json   (third-party app, now replaced)

    Returns a unified dict: { metric_type: [(start_dt, end_dt, value, unit), ...] }
    """
    suffix = path.suffix.lower()

    if suffix == ".zip":
        print("  Detected Apple Health ZIP export — extracting XML…")
        with zipfile.ZipFile(path) as z:
            xml_names = [n for n in z.namelist() if n.endswith("export.xml")]
            if not xml_names:
                sys.exit("  Could not find export.xml inside the ZIP file.")
            with z.open(xml_names[0]) as f:
                tree = ET.parse(f)
        return _parse_apple_xml(tree)

    if suffix == ".xml":
        print("  Detected Apple Health XML export…")
        tree = ET.parse(path)
        return _parse_apple_xml(tree)

    if suffix == ".json":
        print("  Detected Health Auto Export JSON…")
        with open(path, "r") as f:
            raw = json.load(f)
        return _parse_hae_json(raw)

    sys.exit(f"  Unrecognised health export format: {suffix}. Expected .zip, .xml, or .json")


def _parse_apple_xml(tree):
    """
    Parse Apple's native export.xml.
    Each <Record> element has: type, startDate, endDate, value, unit
    """
    # Map Apple type identifiers → friendly names
    TYPE_MAP = {
        "HKQuantityTypeIdentifierHeartRate":         "heart_rate",
        "HKQuantityTypeIdentifierOxygenSaturation":  "oxygen_saturation",
        "HKQuantityTypeIdentifierStepCount":         "step_count",
        "HKQuantityTypeIdentifierActiveEnergyBurned":"active_energy_burned",
    }

    metrics = {v: [] for v in TYPE_MAP.values()}

    for record in tree.getroot().iter("Record"):
        rtype = TYPE_MAP.get(record.attrib.get("type"))
        if not rtype:
            continue
        try:
            val   = float(record.attrib["value"])
            start = parse_iso(record.attrib.get("startDate"))
            end   = parse_iso(record.attrib.get("endDate"))
            unit  = record.attrib.get("unit", "")
            if start:
                metrics[rtype].append({"start": start, "end": end, "qty": val, "unit": unit})
        except (ValueError, KeyError):
            continue

    print(f"  Parsed XML: " + ", ".join(f"{k}={len(v)}" for k, v in metrics.items()))
    return {"_format": "xml", "metrics": metrics}


def _parse_hae_json(raw):
    """Convert Health Auto Export JSON into the same unified format."""
    metrics_raw = raw.get("data", {}).get("metrics", [])
    unified = {}
    for m in metrics_raw:
        name  = m["name"]
        unit  = m.get("units", "")
        items = []
        for s in m.get("data", []):
            ts = parse_iso(s.get("date", ""))
            if ts:
                items.append({"start": ts, "end": ts, "qty": s.get("qty", 0), "unit": unit})
        unified[name] = items
    return {"_format": "json", "metrics": unified}


def extract_health_metrics(health_data, start_ts, stop_ts):
    """
    Extract HR, SpO2, steps, and active energy for a walk window.
    Works with both Apple XML and Health Auto Export JSON unified format.
    """
    metrics  = health_data.get("metrics", {})
    walk_date = start_ts.date()

    def samples_in_window(name):
        out = []
        for s in metrics.get(name, []):
            t = s["start"]
            if start_ts <= t <= stop_ts:
                out.append(s)
        return out

    def samples_on_date(name):
        return [s for s in metrics.get(name, []) if s["start"].date() == walk_date]

    def get_samples(name):
        windowed = samples_in_window(name)
        return windowed if windowed else samples_on_date(name)

    # Heart rate
    hr_vals = [s["qty"] for s in get_samples("heart_rate")]
    hr_avg  = round(sum(hr_vals) / len(hr_vals), 1) if hr_vals else None
    hr_min  = round(min(hr_vals), 1) if hr_vals else None
    hr_max  = round(max(hr_vals), 1) if hr_vals else None

    # SpO2 — Apple XML stores as fraction (0.97), HAE as percentage (97)
    spo2_raw = [s["qty"] for s in get_samples("oxygen_saturation")]
    if spo2_raw:
        avg = sum(spo2_raw) / len(spo2_raw)
        spo2_avg = round(avg * 100 if avg < 2 else avg, 1)
    else:
        spo2_avg = None

    # Steps
    step_vals = [s["qty"] for s in get_samples("step_count")]
    steps = int(sum(step_vals)) if step_vals else None

    # Active energy — convert kJ → kcal if needed
    energy_name = "active_energy_burned" if "active_energy_burned" in metrics else "active_energy"
    energy_samples = get_samples(energy_name)
    if energy_samples:
        total = sum(s["qty"] for s in energy_samples)
        unit  = energy_samples[0].get("unit", "kcal")
        if unit.lower() in ("kj", "kilojoules"):
            total = total / 4.184
        active_energy = round(total, 1)
    else:
        active_energy = None

    return {
        "hr_avg_bpm":         hr_avg,
        "hr_min_bpm":         hr_min,
        "hr_max_bpm":         hr_max,
        "spo2_avg_pct":       spo2_avg,
        "steps":              steps,
        "active_energy_kcal": active_energy,
    }


# ─────────────────────────────────────────────────────────────────────────────
# MAIN
# ─────────────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(
        description="Extract, merge, and export wearable sensing pipeline data."
    )
    parser.add_argument(
        "--firebase-key",
        default="scripts/firebase_key.json",
        help="Path to Firebase service account JSON key (default: scripts/firebase_key.json)",
    )
    parser.add_argument(
        "--health-export",
        default=None,
        help="Path to Apple Health export — .zip or export.xml (from Health app) or .json (Health Auto Export). If omitted, watch metrics will be blank.",
    )
    parser.add_argument(
        "--output-dir",
        default="data",
        help="Directory for output files (default: data/)",
    )
    args = parser.parse_args()

    output_dir = PROJECT_ROOT / args.output_dir
    output_dir.mkdir(parents=True, exist_ok=True)

    # ── 1. Load Firebase sessions ────────────────────────────────────────────
    print("\n[1/4] Loading walk sessions from Firebase…")
    key_path = PROJECT_ROOT / args.firebase_key
    if not key_path.exists():
        sys.exit(
            f"\nFirebase key not found at {key_path}\n"
            "Export it from Firebase console → Project settings → Service accounts."
        )
    sessions = load_firebase_sessions(str(key_path))

    if not sessions:
        print("  No sessions found. Have you completed a walk yet?")
        sys.exit(0)

    # ── 2. Load Apple Health export ──────────────────────────────────────────
    print("\n[2/4] Loading Apple Health export…")
    health_export = {}
    if args.health_export:
        health_path = PROJECT_ROOT / args.health_export
        if not health_path.exists():
            print(f"  Warning: health export not found at {health_path}. Skipping watch metrics.")
        else:
            health_export = load_health_export(health_path)
            print(f"  Loaded health export from {health_path}")
    else:
        print("  No --health-export path given. Watch metrics will be empty.")

    # ── 3. Process each session ──────────────────────────────────────────────
    print("\n[3/4] Processing sessions…")
    rows = []

    for s in sessions:
        sid = s.get("session_id", "unknown")
        print(f"  → {sid}")

        start_str = s.get("start_timestamp")
        stop_str  = s.get("stop_timestamp")
        start_dt  = parse_iso(start_str) if start_str else None
        stop_dt   = parse_iso(stop_str)  if stop_str  else None

        # GPS stats
        gps_points              = s.get("gps_points", [])
        total_distance_m, avg_speed_ms = compute_gps_stats(gps_points)

        # Apple Health metrics
        watch_metrics = {}
        if health_export and start_dt and stop_dt:
            watch_metrics = extract_health_metrics(health_export, start_dt, stop_dt)

        # Pre/post questionnaire
        pre  = s.get("pre_walk",  {})
        post = s.get("post_walk", {})

        row = {
            "session_id":           sid,
            "date":                 start_dt.strftime("%Y-%m-%d") if start_dt else None,
            "start_time":           start_dt.strftime("%H:%M:%S") if start_dt else None,
            "stop_time":            stop_dt.strftime("%H:%M:%S")  if stop_dt  else None,
            "duration_seconds":     s.get("duration_seconds"),
            "duration_formatted":   _fmt_duration(s.get("duration_seconds")),
            "gps_point_count":      len(gps_points),
            "total_distance_m":     total_distance_m,
            "avg_speed_ms":         avg_speed_ms,
            # Pre-walk
            "pre_sleep_quality":    pre.get("sleep_quality"),
            "pre_pain_level":       pre.get("pain_level"),
            "pre_energy_level":     pre.get("energy_level"),
            "pre_medication_taken": pre.get("medication_taken"),
            "pre_notes":            pre.get("notes", ""),
            # Post-walk
            "post_exertion":        post.get("exertion"),
            "post_fatigue":         post.get("fatigue"),
            "post_confidence":      post.get("confidence"),
            "post_pain_triggered":  post.get("pain_triggered"),
            "post_notes":           post.get("notes", ""),
            # Watch metrics
            **watch_metrics,
            # GPS path (for dashboard map)
            "gps_path":             [{"lat": p["lat"], "lon": p["lon"]} for p in gps_points],
        }
        rows.append(row)

    # Sort chronologically
    rows.sort(key=lambda r: r["date"] or "")

    # ── 4. Write outputs ─────────────────────────────────────────────────────
    print("\n[4/4] Writing output files…")

    # CSV (without gps_path column — too large for spreadsheet)
    csv_rows = [{k: v for k, v in r.items() if k != "gps_path"} for r in rows]
    df = pd.DataFrame(csv_rows)
    csv_path = output_dir / "sessions.csv"
    df.to_csv(csv_path, index=False)
    print(f"  CSV → {csv_path}")

    # JSON (includes gps_path for dashboard maps)
    json_path = output_dir / "sessions.json"
    with open(json_path, "w") as f:
        json.dump(rows, f, indent=2, default=str)
    print(f"  JSON → {json_path}")

    print(f"\nDone. {len(rows)} session(s) exported.\n")


def _fmt_duration(seconds):
    if not seconds:
        return "—"
    m, s = divmod(int(seconds), 60)
    h, m = divmod(m, 60)
    if h:
        return f"{h}h {m}m {s}s"
    return f"{m}m {s}s"


if __name__ == "__main__":
    main()
