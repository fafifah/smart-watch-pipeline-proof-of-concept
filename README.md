# Wearable Sensing Pipeline — Proof of Concept

**University of Strathclyde**
*Developing an End-to-End Wearable Sensing Pipeline to Measure Health and Mobility Burden in Real-World Walking Environments*

---

## What is working and what is manual

| Step | Status | Notes |
|------|--------|-------|
| Pre-walk questionnaire | **Automatic** | Saved to Firebase the moment you tap Submit |
| GPS recording during walk | **Automatic** | Recorded every 10 seconds, saved to Firebase |
| Post-walk questionnaire | **Automatic** | Saved to Firebase on Submit |
| Heart rate / SpO₂ / steps from watch | **Manual** | Must export from Apple Health after each walk |
| Python merge script | **Manual** | Run once after exporting health data |
| Dashboard update | **Automatic** | Deploys on GitHub Pages when you push sessions.json |

The wearable data step is manual because the Redmi Watch 5 Lite does not have an open API.
The production study will use a device with a real-time API (Polar H10 or Garmin) to automate this.

---

## Repository structure

```
├── app/
│   └── index.html              ← Mobile web app (hosted online — works on mobile data)
├── scripts/
│   └── extract_health_data.py  ← Python merge script (run after each walk)
│   └── firebase_key.json       ← YOU ADD THIS — never commit to git
├── data/
│   └── sessions.json           ← Updated by Python script — read by dashboard
├── dashboard/
│   └── index.html              ← Research dashboard (GitHub Pages)
└── .github/
    └── workflows/
        └── pages.yml           ← Auto-deploys when you push to main
```

---

## Before your first walk — one-time setup

### 1. The mobile app is already hosted (once you activate GitHub Pages)

When you are ready to go public:
- Make the repo **public**: GitHub → Settings → Change repository visibility
- Enable Pages: GitHub → Settings → Pages → Source → **GitHub Actions**
- Push any change to trigger the first deploy

Your app will then be live at:
```
https://fafifah.github.io/smart-watch-pipeline-proof-of-concept/app/
```
Open that URL in Safari on your iPhone → Share → **Add to Home Screen** → Add.
It appears as an icon on your home screen. Works anywhere on mobile data, no Wi-Fi needed.

### 2. Install Health Auto Export on your iPhone

- Download **Health Auto Export** from the App Store (free tier is enough)
- Open it → go to Exports → set up a custom export with these metrics:
  - Heart Rate
  - Oxygen Saturation
  - Step Count
  - Active Energy Burned
- Leave the format as **JSON**

---

## Every walk — step by step

### During the walk (all automatic)

1. Open the Walk Tracker app from your iPhone home screen
2. Fill in the **pre-walk questionnaire** (sleep, pain, energy, medication) → tap **START WALK**
3. Allow location access when Safari asks — do this once and it remembers
4. Walk your route — GPS records every 10 seconds, shown live on the map
5. Tap **STOP WALK** when you finish
6. Fill in the **post-walk questionnaire** (exertion, fatigue, confidence, pain) → tap **SUBMIT**
7. Done — questionnaire and GPS data are now in Firebase

### After the walk (manual — takes about 5 minutes)

**Step A — Sync your watch**
- Open **Mi Fitness** on your iPhone and wait for it to sync with your watch
- Then open **Apple Health** → confirm heart rate data from the walk is visible

**Step B — Export health data**
- Open **Health Auto Export** → tap Export → save the JSON file to your laptop
  (AirDrop it, email it to yourself, or save to iCloud Drive)

**Step C — Run the Python script**
On your laptop, in the project folder:
```
python scripts/extract_health_data.py \
    --firebase-key scripts/firebase_key.json \
    --health-export PATH_TO_YOUR_EXPORT.json \
    --output-dir data
```
This creates `data/sessions.json` with all walk data merged.

**Step D — Push to GitHub**
```
git add data/sessions.json
git commit -m "Add walk session - DATE"
git push
```
The dashboard updates automatically within about 1 minute.

---

## Getting the Firebase service account key (for the Python script)

1. Go to Firebase console → gear icon → **Project settings**
2. Click **Service accounts** tab
3. Click **Generate new private key** → download the JSON file
4. Save it as `scripts/firebase_key.json` in the project folder
5. This file is in `.gitignore` — it will never be committed to GitHub

---

## Installing Python dependencies (one-time)

```
pip install firebase-admin pandas
```

---

## When you are ready to go public

1. Make the repository **public** on GitHub
2. Go to Settings → Pages → Source → GitHub Actions
3. Push any change — the dashboard and app both deploy automatically
4. Share this link in your funding proposal:
   `https://fafifah.github.io/smart-watch-pipeline-proof-of-concept/`

---

## Limitations acknowledged in the proof-of-concept

- Wearable data extraction is manual (no open API on Redmi Watch 5 Lite)
- Production study will use Polar H10 or Garmin for real-time automated extraction
- Single researcher dataset — scaling to a participant cohort is the next phase
