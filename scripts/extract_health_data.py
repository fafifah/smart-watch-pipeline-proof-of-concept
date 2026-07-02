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
# APPLE HEALTH PARSER
# ─────────────────────────────────────────────────────────────────────────────

def parse_iso(ts_str):
    """Parse an ISO-8601 timestamp string into a timezone-aware datetime."""
    try:
        return datetime.fromisoformat(ts_str.replace("Z", "+00:00"))
    except Exception:
        return None


def extract_health_metrics(health_export, start_ts, stop_ts):
    """
    Search the Health Auto Export JSON for samples that fall between
    start_ts and stop_ts (both datetime objects).

    The Health Auto Export format uses:
        health_export["data"]["metrics"]  — list of metric objects
        metric["name"]                    — e.g. "heart_rate"
        metric["data"]                    — list of {date, qty, ...} samples

    Returns a dict with hr_avg, hr_min, hr_max, spo2_avg, steps, active_energy_kcal.
    """
    metrics_raw = health_export.get("data", {}).get("metrics", [])

    # Build quick lookup by metric name, also store the units
    metrics = {m["name"]: {"data": m.get("data", []), "units": m.get("units", "")} for m in metrics_raw}

    walk_date = start_ts.date()

    def samples_in_window(name):
        """Return samples whose timestamp falls within the walk window (per-sample data)."""
        out = []
        for sample in metrics.get(name, {}).get("data", []):
            ts = parse_iso(sample.get("date", ""))
            if ts and start_ts <= ts <= stop_ts:
                out.append(sample)
        return out

    def samples_on_date(name):
        """Return samples whose date matches the walk date (daily-total data)."""
        out = []
        for sample in metrics.get(name, {}).get("data", []):
            ts = parse_iso(sample.get("date", ""))
            if ts and ts.date() == walk_date:
                out.append(sample)
        return out

    def get_samples(name):
        """Try per-sample window first; fall back to daily-date match."""
        windowed = samples_in_window(name)
        return windowed if windowed else samples_on_date(name)

    # Heart rate
    hr_samples = [s["qty"] for s in get_samples("heart_rate") if "qty" in s]
    hr_avg = round(sum(hr_samples) / len(hr_samples), 1) if hr_samples else None
    hr_min = round(min(hr_samples), 1) if hr_samples else None
    hr_max = round(max(hr_samples), 1) if hr_samples else None

    # SpO2
    spo2_samples = [s["qty"] for s in get_samples("oxygen_saturation") if "qty" in s]
    spo2_avg = round(sum(spo2_samples) / len(spo2_samples), 1) if spo2_samples else None

    # Steps — sum; Health Auto Export may use "step_count" or "steps"
    step_samples = [s.get("qty", 0) for s in get_samples("step_count") or get_samples("steps")]
    steps = int(sum(step_samples)) if step_samples else None

    # Active energy — Health Auto Export may export in kJ ("active_energy") or kcal ("active_energy_burned")
    energy_name = "active_energy_burned" if "active_energy_burned" in metrics else "active_energy"
    energy_unit = metrics.get(energy_name, {}).get("units", "kcal")
    energy_samples = [s.get("qty", 0) for s in get_samples(energy_name)]
    if energy_samples:
        total_energy = sum(energy_samples)
        # Convert kJ → kcal if needed
        if energy_unit.lower() in ("kj", "kilojoules"):
            total_energy = total_energy / 4.184
        active_energy = round(total_energy, 1)
    else:
        active_energy = None

    return {
        "hr_avg_bpm":           hr_avg,
        "hr_min_bpm":           hr_min,
        "hr_max_bpm":           hr_max,
        "spo2_avg_pct":         spo2_avg,
        "steps":                steps,
        "active_energy_kcal":   active_energy,
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
        help="Path to Health Auto Export JSON file. If omitted, watch metrics will be blank.",
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
            with open(health_path, "r") as f:
                health_export = json.load(f)
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
