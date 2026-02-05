#!/usr/bin/env python3

import os
import time
import requests
from bs4 import BeautifulSoup
from datetime import datetime, timezone
from urllib.parse import urlparse, parse_qs
from flask import Flask, jsonify
from threading import Lock

# ================= CONFIG =================



AUTH_URL = (
    "https://sis.anatolia.edu.gr/auth/realms/Anatolia/protocol/openid-connect/auth"
    "?response_type=token"
    "&client_id=athena-act-student-portal"
    "&redirect_uri=https%3A%2F%2Fportal.student.act.edu%2Fauth%2Fopenid%2Fcallback%2Findex.html"
)

ME_URL = "https://api.anatolia.edu.gr/api/users/me/?$top=1&$skip=0&$count=false"

ACADEMIC_REVIEW_URL = (
    "https://api.anatolia.edu.gr/athena-conductor/rest/persons/students/{student_id}/academic-review"
)

PERIODS_URL = (
    "https://api.anatolia.edu.gr/athena-conductor/rest/study-profiles/{study_profile_id}/periods"
)

PROGRESS_URL = (
    "https://api.anatolia.edu.gr/athena-conductor/rest/study-profiles/periods/{period_id}/progress"
)

# Optional: override auto-detection
STUDY_PROFILE_ID = os.environ.get("STUDY_PROFILE_ID")


REFRESH_INTERVAL = 1800  # seconds

# ================= APP =================

app = Flask(__name__)
session = requests.Session()
cache = {"data": None, "last_fetch": 0, "last_error": 0}
refresh_lock = Lock()
ERROR_BACKOFF = 300  # seconds

# ================= AUTH =================

def extract_token_from_location(location):
    fragment = urlparse(location).fragment
    params = parse_qs(fragment)
    return params.get("access_token", [None])[0]

def keycloak_login(username: str, password: str) -> str:
    r = session.get(AUTH_URL, timeout=10, allow_redirects=True)
    r.raise_for_status()

    soup = BeautifulSoup(r.text, "html.parser")
    form = soup.find("form")
    if not form:
        snippet = r.text[:500].replace("\n", " ")
        raise RuntimeError(
            f"Login form not found. status={r.status_code} url={r.url} body_start={snippet}"
        )

    action_attr = form.get("action")
    if not action_attr:
        raise RuntimeError("Login form action missing")
    action = str(action_attr)

    data = {}
    for inp in form.select("input"):
        name = inp.get("name")
        if name:
            data[name] = inp.get("value", "")

    data["username"] = username
    data["password"] = password

    post = session.post(action, data=data, allow_redirects=False, timeout=10)
    if post.status_code != 302 or "Location" not in post.headers:
        raise RuntimeError(f"Login failed, status={post.status_code}")

    token = extract_token_from_location(post.headers["Location"])
    if not token:
        raise RuntimeError("No access_token in redirect")

    return token

def fetch_student_uuid(token: str) -> str:
    headers = {"Authorization": f"Bearer {token}"}
    resp = session.get(ME_URL, headers=headers, timeout=10)
    resp.raise_for_status()

    data = resp.json()
    same_as = data.get("sameAs")
    if not same_as:
        raise RuntimeError("sameAs not found in /api/users/me response")
    return same_as

def fetch_study_profile_id(token: str, student_id: str, username: str) -> str:
    if STUDY_PROFILE_ID:
        return STUDY_PROFILE_ID

    headers = {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json"
    }
    resp = session.post(
        ACADEMIC_REVIEW_URL.format(student_id=student_id),
        headers=headers,
        json={}
    )
    resp.raise_for_status()
    data = resp.json()

    profiles = data.get("profiles", [])
    if not profiles:
        raise RuntimeError("No profiles found in academic-review response")

    for profile in profiles:
        if not profile.get("activeProfile"):
            continue

        study = profile.get("study", {})
        if study.get("traineeRegistrationNumber") != username:
            continue

        profile_id = profile.get("id")
        assigned_id = study.get("assignedProfileId")
        if profile_id:
            return profile_id
        if assigned_id:
            return assigned_id

    raise RuntimeError("Study profile ID not found in academic-review response")

# ================= PERIOD LOOKUP =================

def parse_dt(value):
    if not value:
        return None
    return datetime.fromisoformat(value)

def fetch_current_period_id(token: str, study_profile_id: str) -> str:
    headers = {"Authorization": f"Bearer {token}"}
    resp = session.get(PERIODS_URL.format(study_profile_id=study_profile_id), headers=headers, timeout=10)
    resp.raise_for_status()
    periods = resp.json()

    now = datetime.now(timezone.utc)

    for period in periods:
        conv = period.get("academicConvergence") or {}
        date_from = parse_dt(conv.get("dateFrom"))
        date_to = parse_dt(conv.get("dateTo"))

        if date_from and date_to:
            if date_from.astimezone(timezone.utc) <= now <= date_to.astimezone(timezone.utc):
                return period["id"]

    def date_from_key(p):
        conv = p.get("academicConvergence") or {}
        dt = parse_dt(conv.get("dateFrom"))
        return dt or datetime.min.replace(tzinfo=timezone.utc)

    latest = max(periods, key=date_from_key)
    return latest["id"]

# ================= PROGRESS =================

def fetch_progress_json(token: str, period_id: str):
    headers = {"Authorization": f"Bearer {token}"}
    url = PROGRESS_URL.format(period_id=period_id)
    resp = session.get(url, headers=headers, timeout=10)
    resp.raise_for_status()
    return resp.json()

def parse_progress_absences(data):
    results = []
    for module in data.get("modules", []):
        course = module.get("name") or module.get("studyPeriodModule", {}).get("module", {}).get("title")
        absences = module.get("absences", {}).get("absences", 0)
        if absences and absences > 0:
            results.append({
                "course": course,
                "value": float(absences)
            })
    return results

def summarize(absences):
    used = {}
    for a in absences:
        used[a["course"]] = used.get(a["course"], 0) + a["value"]

    per_course = {}

    for course, used_val in used.items():
        per_course[course] = {
            "used": round(used_val, 2),
        }


    return {
        "total_used": round(sum(used.values()), 2),
        "per_course": per_course,
        "last_updated": datetime.utcnow().isoformat() + "Z"
    }

# ================= DATA REFRESH =================

def refresh_data():
    username = os.environ["PORTAL_USERNAME"]
    password = os.environ["PORTAL_PASSWORD"]


    token = keycloak_login(username, password)
    student_id = fetch_student_uuid(token)

    study_profile_id = fetch_study_profile_id(token, student_id, username)
    period_id = fetch_current_period_id(token, study_profile_id)

    progress = fetch_progress_json(token, period_id)
    absences = parse_progress_absences(progress)

    return summarize(absences)

# ================= API =================

@app.route("/absences")
def absences_endpoint():
    now = time.time()

    # If last refresh failed, back off
    if cache["last_error"] and now - cache["last_error"] < ERROR_BACKOFF:
        if cache["data"]:
            return jsonify(cache["data"])
        return jsonify({"error": "temporary backoff"}), 503

    # Refresh if stale, but only one at a time
    if not cache["data"] or now - cache["last_fetch"] > REFRESH_INTERVAL:
        if refresh_lock.acquire(blocking=False):
            try:
                cache["data"] = refresh_data()
                cache["last_fetch"] = now
                cache["last_error"] = 0
            except Exception:
                cache["last_error"] = now
                raise
            finally:
                refresh_lock.release()

    if cache["data"]:
        return jsonify(cache["data"])
    return jsonify({"error": "no data yet"}), 503

# ================= MAIN =================

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5002)