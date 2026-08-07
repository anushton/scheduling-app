import os
import re
import io
import csv
import json
import sqlite3
import urllib.parse
import datetime as dt
from dataclasses import dataclass, field
import pandas as pd
import streamlit as st

try:
    import googlemaps
except ImportError:
    googlemaps = None

from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import Flow
from googleapiclient.discovery import build

# ============================= PASSWORD AUTHENTICATION ======================

def check_password():
    correct_password = st.secrets["APP_PASSWORD"]

    def password_entered():
        if st.session_state["password"] == correct_password:
            st.session_state["password_correct"] = True
            del st.session_state["password"]
        else:
            st.session_state["password_correct"] = False

    if "password_correct" not in st.session_state:
        st.subheader("Employee Portal Login")
        st.text_input("Password Required", type="password", on_change=password_entered, key="password")
        return False
    elif not st.session_state["password_correct"]:
        st.subheader("Employee Portal Login")
        st.text_input("Password Required", type="password", on_change=password_entered, key="password")
        st.error("😕 Password incorrect")
        return False
    else:
        return True

if not check_password():
    st.stop()


# ============================= SECURE CONFIG SETUP ==========================

MAPS_API_KEY = st.secrets.get("MAPS_API_KEY", "")
OFFICE_ADDRESS = st.secrets.get("OFFICE_ADDRESS", "")

SCOPES = ["https://www.googleapis.com/auth/calendar.readonly"]
UNSCHEDULED_COLOR_ID = "5"

DISTANCE_MATRIX_COST_PER_1000 = 5.0
DRIVE_TIME_CACHE_MAX_AGE_HOURS = 6
MAX_SAVED_RUNS = 100

WEEKDAY_NAMES = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]

DEFAULT_SETTINGS = {
    "workday_hours": {
        "Mon": {"enabled": True, "start": "09:00", "end": "17:30"},
        "Tue": {"enabled": True, "start": "09:00", "end": "17:30"},
        "Wed": {"enabled": True, "start": "09:00", "end": "17:30"},
        "Thu": {"enabled": True, "start": "09:00", "end": "17:30"},
        "Fri": {"enabled": True, "start": "09:00", "end": "17:30"},
        "Sat": {"enabled": False, "start": "09:00", "end": "17:30"},
        "Sun": {"enabled": False, "start": "09:00", "end": "17:30"},
    },
    "duration_rules": [
        ["hearing test", 60], ["test appt", 60], ["cleaning", 60],
        ["earwax", 30], ["service", 30],
    ],
    "default_duration_min": 60,
    "do_not_schedule_markers": [
        "do not schedule", "don't schedule", "outside service area", "out of area",
    ],
}

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
DB_PATH = os.path.join(SCRIPT_DIR, "schedule_history.db")

# ============================================================================

@dataclass
class Patient:
    event_id: str
    name: str
    location: str
    duration_min: int
    raw_summary: str
    calendar_id: str
    created_at: str = ""
    duration_guessed: bool = False
    duration_overridden: bool = False

@dataclass
class Stop:
    start: dt.datetime
    end: dt.datetime
    location: str
    summary: str

@dataclass
class SlotRecommendation:
    patient: Patient
    calendar_name: str
    day: dt.date
    start: dt.datetime
    end: dt.datetime
    drive_before_min: float
    drive_after_min: float
    detour_min: float
    notes: list = field(default_factory=list)

@dataclass
class ScheduleConflict:
    day: dt.date
    event1_summary: str
    event1_start: dt.datetime
    event1_end: dt.datetime
    event2_summary: str
    event2_start: dt.datetime


def clean_display(text: str, max_len: int = 100) -> str:
    text = " ".join((text or "").split())
    if len(text) > max_len:
        text = text[:max_len].rstrip() + "…"
    return text


def parse_hhmm(s: str) -> dt.time:
    h, m = s.split(":")
    return dt.time(int(h), int(m))


# ============================= PERSISTENT STORAGE ============================

@st.cache_resource
def get_db_connection():
    conn = sqlite3.connect(DB_PATH, check_same_thread=False)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS runs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            created_at TEXT NOT NULL,
            mode TEXT NOT NULL,
            calendar_label TEXT NOT NULL,
            api_call_count INTEGER,
            data_json TEXT NOT NULL
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS drive_time_cache (
            origin TEXT NOT NULL,
            destination TEXT NOT NULL,
            minutes REAL NOT NULL,
            cached_at TEXT NOT NULL,
            PRIMARY KEY (origin, destination)
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS settings (
            key TEXT PRIMARY KEY,
            value TEXT NOT NULL
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS patient_lookups (
            event_id TEXT PRIMARY KEY,
            last_checked_at TEXT NOT NULL
        )
    """)
    conn.commit()
    return conn


def save_run(conn, mode: str, calendar_label: str, data: dict) -> int:
    now = dt.datetime.now().isoformat()
    cur = conn.execute(
        "INSERT INTO runs (created_at, mode, calendar_label, api_call_count, data_json) VALUES (?, ?, ?, ?, ?)",
        (now, mode, calendar_label, data.get("api_call_count", 0), json.dumps(data)),
    )
    conn.commit()
    conn.execute("DELETE FROM runs WHERE id NOT IN (SELECT id FROM runs ORDER BY id DESC LIMIT ?)", (MAX_SAVED_RUNS,))
    conn.commit()
    return cur.lastrowid


def list_runs(conn, limit: int = 25):
    cur = conn.execute("SELECT id, created_at, mode, calendar_label FROM runs ORDER BY id DESC LIMIT ?", (limit,))
    return cur.fetchall()


def load_run(conn, run_id: int):
    cur = conn.execute("SELECT data_json FROM runs WHERE id = ?", (run_id,))
    row = cur.fetchone()
    return json.loads(row[0]) if row else None


def delete_run(conn, run_id: int):
    conn.execute("DELETE FROM runs WHERE id = ?", (run_id,))
    conn.commit()


def get_cached_drive_time(conn, origin: str, destination: str, max_age_hours: float):
    cur = conn.execute("SELECT minutes, cached_at FROM drive_time_cache WHERE origin = ? AND destination = ?", (origin, destination))
    row = cur.fetchone()
    if not row:
        return None
    minutes, cached_at = row
    if dt.datetime.now() - dt.datetime.fromisoformat(cached_at) > dt.timedelta(hours=max_age_hours):
        return None
    return minutes


def save_drive_time_to_cache(conn, origin: str, destination: str, minutes: float):
    now = dt.datetime.now().isoformat()
    conn.execute(
        """INSERT INTO drive_time_cache (origin, destination, minutes, cached_at) VALUES (?, ?, ?, ?)
           ON CONFLICT(origin, destination) DO UPDATE SET minutes = excluded.minutes, cached_at = excluded.cached_at""",
        (origin, destination, minutes, now),
    )
    conn.commit()


def get_settings(conn) -> dict:
    cur = conn.execute("SELECT value FROM settings WHERE key = 'app_settings'")
    row = cur.fetchone()
    if row:
        try:
            loaded = json.loads(row[0])
            return {**DEFAULT_SETTINGS, **loaded}
        except Exception:
            pass
    save_settings(conn, DEFAULT_SETTINGS)
    return json.loads(json.dumps(DEFAULT_SETTINGS))  


def save_settings(conn, settings: dict):
    conn.execute(
        "INSERT INTO settings (key, value) VALUES ('app_settings', ?) "
        "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
        (json.dumps(settings),),
    )
    conn.commit()


def record_lookup(conn, event_id: str):
    now = dt.datetime.now().isoformat()
    conn.execute(
        "INSERT INTO patient_lookups (event_id, last_checked_at) VALUES (?, ?) "
        "ON CONFLICT(event_id) DO UPDATE SET last_checked_at = excluded.last_checked_at",
        (event_id, now),
    )
    conn.commit()


def recently_checked_ids(conn, days: int) -> set:
    cutoff = (dt.datetime.now() - dt.timedelta(days=days)).isoformat()
    cur = conn.execute("SELECT event_id FROM patient_lookups WHERE last_checked_at >= ?", (cutoff,))
    return {row[0] for row in cur.fetchall()}

# ============================================================================

def get_calendar_service():
    if "valid_creds" in st.session_state:
        return build("calendar", "v3", credentials=st.session_state["valid_creds"])

    creds = None
    if "GOOGLE_TOKEN" in st.secrets:
        token_data = dict(st.secrets["GOOGLE_TOKEN"])
        
        expiry_val = token_data.get("expiry")
        if isinstance(expiry_val, dt.datetime):
            expiry_val = expiry_val.isoformat().replace("+00:00", "")
            if not expiry_val.endswith("Z"):
                expiry_val += "Z"
        elif isinstance(expiry_val, str) and expiry_val:
            if not expiry_val.endswith("Z") and "+" not in expiry_val:
                expiry_val += "Z"
                
        token_info = {
            "token": token_data.get("token"),
            "refresh_token": token_data.get("refresh_token"),
            "token_uri": token_data.get("token_uri"),
            "client_id": token_data.get("client_id"),
            "client_secret": token_data.get("client_secret"),
            "scopes": list(token_data.get("scopes", SCOPES)),
            "universe_domain": token_data.get("universe_domain", "googleapis.com"),
            "account": token_data.get("account", ""),
            "expiry": str(expiry_val) if expiry_val else None
        }
        creds = Credentials.from_authorized_user_info(token_info, SCOPES)

    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            try:
                creds.refresh(Request())
            except Exception:
                creds = None

        if not creds:
            if "oauth_flow" not in st.session_state:
                client_config = {
                    "installed": {
                        "client_id": st.secrets["GOOGLE_CREDENTIALS"]["installed"]["client_id"],
                        "client_secret": st.secrets["GOOGLE_CREDENTIALS"]["installed"]["client_secret"],
                        "auth_uri": st.secrets["GOOGLE_CREDENTIALS"]["installed"]["auth_uri"],
                        "token_uri": st.secrets["GOOGLE_CREDENTIALS"]["installed"]["token_uri"],
                        "redirect_uris": ["http://localhost"]
                    }
                }
                new_flow = Flow.from_client_config(client_config, scopes=SCOPES)
                new_flow.redirect_uri = "http://localhost"  
                auth_url, _ = new_flow.authorization_url(prompt='consent', access_type='offline')
                st.session_state["oauth_flow"] = new_flow
                st.session_state["oauth_url"] = auth_url

            flow = st.session_state["oauth_flow"]
            st.warning(f"One-time setup needed. Open this link, sign in, and copy the code it gives you: {st.session_state['oauth_url']}")
            code = st.text_input("Paste the code here:")
            
            if code:
                try:
                    flow.fetch_token(code=code.strip())
                    creds = flow.credentials
                    del st.session_state["oauth_flow"]
                    del st.session_state["oauth_url"]
                    
                    st.success("Successfully authenticated!")
                    st.markdown("### ⚠️ Final Step: Save Your New Token")
                    st.write("To prevent having to do this again, copy the block below and replace the `[GOOGLE_TOKEN]` section in your `.streamlit/secrets.toml` or Streamlit Cloud Settings:")
                    
                    expiry_str = creds.expiry.isoformat() + "Z" if creds.expiry else ""
                    scopes_str = str(creds.scopes).replace("'", '"')
                    
                    toml_code = f"""[GOOGLE_TOKEN]
token = "{creds.token}"
refresh_token = "{creds.refresh_token}"
token_uri = "{creds.token_uri}"
client_id = "{creds.client_id}"
client_secret = "{creds.client_secret}"
scopes = {scopes_str}
universe_domain = "{creds.universe_domain}"
account = ""
expiry = "{expiry_str}"
"""
                    st.code(toml_code, language="toml")
                    st.info("Once you have updated your secrets, **clear the text box above** and hit Enter to use the app.")
                    st.stop()
                    
                except Exception:
                    st.error(
                        "That code didn't work. Codes can only be used once — if you "
                        "reloaded the page or tried more than once, click the button "
                        "below to get a fresh link and try again with a brand new code."
                    )
                    if st.button("Get a new sign-in link"):
                        del st.session_state["oauth_flow"]
                        del st.session_state["oauth_url"]
                        st.rerun()
                    st.stop()
            else:
                st.stop()

    st.session_state["valid_creds"] = creds
    return build("calendar", "v3", credentials=creds)


def get_user_calendars(service) -> dict[str, str]:
    calendars = {}
    try:
        page_token = None
        while True:
            calendar_list = service.calendarList().list(pageToken=page_token).execute()
            for entry in calendar_list.get("items", []):
                summary = entry.get("summary", "").strip()
                cal_id = entry.get("id", "")
                if "dylan hendrickson-work schedule" in summary.lower() or cal_id == "w1a9d7e4@gmail.com":
                    display_name = "Wade Hendrickson" if cal_id == "w1a9d7e4@gmail.com" else summary
                    calendars[display_name] = cal_id
            page_token = calendar_list.get("nextPageToken")
            if not page_token:
                break
    except Exception:
        st.warning("Couldn't load the calendar list from Google — using the saved default calendars instead.")

    if not calendars:
        calendars["Wade Hendrickson"] = "w1a9d7e4@gmail.com"
        calendars["Dylan Hendrickson-Work Schedule"] = "primary"
    return calendars


def guess_duration(text: str, duration_rules, default_minutes: int) -> tuple[int, bool]:
    text_lower = text.lower()
    for keyword, minutes in duration_rules:
        if keyword in text_lower:
            return minutes, False
    return default_minutes, True


def get_unscheduled_patients(service, do_not_schedule_markers, duration_rules, default_duration_min) -> tuple[list[Patient], list[str], list[str]]:
    now = dt.datetime.now(dt.timezone.utc).replace(tzinfo=None)
    time_min = (now - dt.timedelta(days=60)).isoformat() + "Z"
    time_max = (now + dt.timedelta(days=60)).isoformat() + "Z"

    patients = []
    skipped_no_address = []
    skipped_do_not_schedule = []
    target_cal_id = "w1a9d7e4@gmail.com"

    page_token = None
    while True:
        try:
            resp = service.events().list(
                calendarId=target_cal_id, timeMin=time_min, timeMax=time_max,
                singleEvents=True, orderBy="startTime", maxResults=250, pageToken=page_token,
            ).execute()
        except Exception:
            try:
                resp = service.events().list(
                    calendarId="primary", timeMin=time_min, timeMax=time_max,
                    singleEvents=True, orderBy="startTime", maxResults=250, pageToken=page_token,
                ).execute()
            except Exception:
                st.error("Couldn't read the calendar to find unscheduled patients. Results below may be incomplete.")
                break

        for e in resp.get("items", []):
            if e.get("colorId") != UNSCHEDULED_COLOR_ID:
                continue
            if "date" not in e.get("start", {}):
                continue
            summary = e.get("summary", "").strip()
            description = e.get("description", "")
            location = e.get("location", "").strip()
            combined_text = f"{summary} {description}"
            combined_lower = combined_text.lower()

            if any(marker in combined_lower for marker in do_not_schedule_markers):
                skipped_do_not_schedule.append(summary or "(untitled event)")
                continue
            if not location:
                skipped_no_address.append(summary or "(untitled event)")
                continue

            duration, was_guessed = guess_duration(combined_text, duration_rules, default_duration_min)
            patients.append(Patient(
                event_id=e["id"], name=summary, location=location, duration_min=duration,
                raw_summary=summary, calendar_id=target_cal_id, created_at=e.get("created", ""),
                duration_guessed=was_guessed,
            ))
        page_token = resp.get("nextPageToken")
        if not page_token:
            break
    return patients, skipped_no_address, skipped_do_not_schedule


_schedule_cache: dict[tuple[str, dt.date, dt.date], tuple[dict[dt.date, list[Stop]], list[ScheduleConflict]]] = {}


def get_days_schedule(service, calendar_id: str, start_date: dt.date, end_date: dt.date):
    cache_key = (calendar_id, start_date, end_date)
    if cache_key in _schedule_cache:
        return _schedule_cache[cache_key]

    time_min = dt.datetime.combine(start_date, dt.time.min).isoformat() + "Z"
    time_max = dt.datetime.combine(end_date, dt.time.max).isoformat() + "Z"

    schedule: dict[dt.date, list[Stop]] = {}
    page_token = None
    while True:
        try:
            resp = service.events().list(
                calendarId=calendar_id, timeMin=time_min, timeMax=time_max,
                singleEvents=True, orderBy="startTime", maxResults=250, pageToken=page_token,
            ).execute()
        except Exception:
            st.error("Couldn't read the existing schedule for one of the calendars. Double-check before booking from this run.")
            break

        for e in resp.get("items", []):
            start = e.get("start", {})
            end = e.get("end", {})
            if "dateTime" not in start:
                continue
            s = dt.datetime.fromisoformat(start["dateTime"]).replace(tzinfo=None)
            en = dt.datetime.fromisoformat(end["dateTime"]).replace(tzinfo=None)
            day = s.date()
            schedule.setdefault(day, []).append(Stop(
                start=s, end=en, location=e.get("location", OFFICE_ADDRESS), summary=e.get("summary", ""),
            ))
        page_token = resp.get("nextPageToken")
        if not page_token:
            break

    conflicts: list[ScheduleConflict] = []
    for day in schedule:
        schedule[day].sort(key=lambda st: st.start)
        stops = schedule[day]
        for i in range(len(stops) - 1):
            if stops[i + 1].start < stops[i].end:
                conflicts.append(ScheduleConflict(
                    day=day, event1_summary=stops[i].summary, event1_start=stops[i].start, event1_end=stops[i].end,
                    event2_summary=stops[i + 1].summary, event2_start=stops[i + 1].start,
                ))

    _schedule_cache[cache_key] = (schedule, conflicts)
    return schedule, conflicts


def combined_schedule(service, cal_ids: list[str], start_date: dt.date, end_date: dt.date) -> dict[dt.date, list[Stop]]:
    combined: dict[dt.date, list[Stop]] = {}
    for cal_id in cal_ids:
        sched, _ = get_days_schedule(service, cal_id, start_date, end_date)
        for day, stops in sched.items():
            combined.setdefault(day, []).extend(stops)
    for day in combined:
        combined[day].sort(key=lambda s: s.start)
    return combined


def _chunked(lst, n):
    for i in range(0, len(lst), n):
        yield lst[i:i + n]


class DriveTimeEstimator:
    def __init__(self, api_key: str, status_box, conn):
        self.client = googlemaps.Client(key=api_key) if (api_key and googlemaps) else None
        self.cache: dict[tuple[str, str], float] = {}
        self.api_call_count = 0
        self.cache_hit_count = 0
        self.status_box = status_box
        self.conn = conn

    def batch_prime(self, pairs: list[tuple[str, str, dt.datetime]]):
        if not self.client or not pairs:
            return
        buckets: dict[int, set] = {}
        bucket_time: dict[int, dt.datetime] = {}
        for origin, destination, depart_at in pairs:
            if origin == destination:
                continue
            key = (origin, destination)
            if key in self.cache:
                continue
            cached = get_cached_drive_time(self.conn, origin, destination, DRIVE_TIME_CACHE_MAX_AGE_HOURS)
            if cached is not None:
                self.cache[key] = cached
                self.cache_hit_count += 1
                continue
            hour_key = depart_at.hour
            buckets.setdefault(hour_key, set()).add((origin, destination))
            bucket_time.setdefault(hour_key, depart_at.replace(minute=0, second=0, microsecond=0))

        batch_num = 0
        for hour_key, bucket_pairs in buckets.items():
            origins = sorted({o for o, d in bucket_pairs})
            destinations = sorted({d for o, d in bucket_pairs})
            for origin_chunk in _chunked(origins, 10):
                for dest_chunk in _chunked(destinations, 10):
                    batch_num += 1
                    try:
                        self.status_box.text(f"Checking {len(origin_chunk)}×{len(dest_chunk)} routes at once (batch #{batch_num})…")
                        result = self.client.distance_matrix(
                            origins=origin_chunk, destinations=dest_chunk,
                            departure_time=bucket_time[hour_key], traffic_model="best_guess",
                        )
                        self.api_call_count += 1
                        for oi, row in enumerate(result.get("rows", [])):
                            for di, element in enumerate(row.get("elements", [])):
                                if element.get("status") != "OK":
                                    continue
                                o, d = origin_chunk[oi], dest_chunk[di]
                                duration = element.get("duration_in_traffic", element["duration"])
                                minutes = duration["value"] / 60.0
                                self.cache[(o, d)] = minutes
                                save_drive_time_to_cache(self.conn, o, d, minutes)
                    except Exception:
                        pass  

    def minutes_between(self, origin: str, destination: str, depart_at: dt.datetime) -> float:
        if origin == destination:
            return 0.0
        key = (origin, destination)
        if key in self.cache:
            return self.cache[key]

        cached = get_cached_drive_time(self.conn, origin, destination, DRIVE_TIME_CACHE_MAX_AGE_HOURS)
        if cached is not None:
            self.cache[key] = cached
            self.cache_hit_count += 1
            return cached

        minutes = None
        if self.client:
            try:
                self.api_call_count += 1
                self.status_box.text(f"Checking traffic for route #{self.api_call_count}…")
                result = self.client.distance_matrix(
                    origins=[origin], destinations=[destination],
                    departure_time=depart_at, traffic_model="best_guess",
                )
                element = result["rows"][0]["elements"][0]
                if element.get("status") == "OK":
                    duration = element.get("duration_in_traffic", element["duration"])
                    minutes = duration["value"] / 60.0
            except Exception:
                pass

        if minutes is None:
            minutes = 25.0
        else:
            save_drive_time_to_cache(self.conn, origin, destination, minutes)

        self.cache[key] = minutes
        return minutes


def area_match(loc1: str, loc2: str) -> tuple[bool, str]:
    t1, t2 = loc1.lower(), loc2.lower()
    z1 = set(re.findall(r'\b85\d{3}\b', t1))
    z2 = set(re.findall(r'\b85\d{3}\b', t2))
    if z1 and z2 and (z1 & z2):
        return True, "zip"
    cities = [
        "glendale", "goodyear", "peoria", "phoenix", "phx", "scottsdale", "scotts",
        "sun city", "surprise", "avondale", "mesa", "tempe", "chandler", "gilbert",
        "buckeye", "litchfield park", "litchfield", "paradise valley", "queen creek",
        "tucson", "youngtown", "el mirage", "waddell", "apache junction", "tolleson",
        "fountain hills", "cave creek", "carefree", "wickenburg", "florence", "casa grande",
    ]
    for c in cities:
        if c in t1 and c in t2:
            return True, "city"
    return False, "none"


def _enumerate_valid_gaps(schedule, start_date, end_date, workday_hours, patient_location, now):
    results = []
    day = start_date
    while day <= end_date:
        cfg = workday_hours.get(WEEKDAY_NAMES[day.weekday()])
        if not cfg or not cfg.get("enabled"):
            day += dt.timedelta(days=1)
            continue

        day_start = dt.datetime.combine(day, parse_hhmm(cfg["start"]))
        day_end = dt.datetime.combine(day, parse_hhmm(cfg["end"]))
        stops = schedule.get(day, [])
        timeline = (
            [Stop(day_start, day_start, OFFICE_ADDRESS, "Leave office")]
            + stops
            + [Stop(day_end, day_end, OFFICE_ADDRESS, "Back at office")]
        )

        for i in range(len(timeline) - 1):
            prev_stop, next_stop = timeline[i], timeline[i + 1]
            gap_start, gap_end = prev_stop.end, next_stop.start

            if gap_start < now: gap_start = now + dt.timedelta(minutes=1)
            if gap_start < day_start: gap_start = day_start
            if gap_end <= gap_start: continue

            if prev_stop.location != OFFICE_ADDRESS:
                matched, _ = area_match(prev_stop.location, patient_location)
                if not matched: continue
            if next_stop.location != OFFICE_ADDRESS:
                matched, _ = area_match(next_stop.location, patient_location)
                if not matched: continue

            results.append((day, prev_stop, next_stop, gap_start, gap_end))
        day += dt.timedelta(days=1)
    return results


def find_slots_for_patient_on_calendar(
    patient: Patient, calendar_name: str, schedule, estimator: DriveTimeEstimator,
    start_date: dt.date, end_date: dt.date, max_slots: int, workday_hours: dict,
) -> list[SlotRecommendation]:
    duration = dt.timedelta(minutes=patient.duration_min)
    now = dt.datetime.now()

    valid_gaps = _enumerate_valid_gaps(schedule, start_date, end_date, workday_hours, patient.location, now)

    pairs_needed = []
    for day, prev_stop, next_stop, gap_start, gap_end in valid_gaps:
        pairs_needed.append((prev_stop.location, patient.location, gap_start))
        pairs_needed.append((patient.location, next_stop.location, gap_start))
        pairs_needed.append((prev_stop.location, next_stop.location, gap_start))
    estimator.batch_prime(pairs_needed)

    candidates: list[SlotRecommendation] = []
    for day, prev_stop, next_stop, gap_start, gap_end in valid_gaps:
        drive_to = estimator.minutes_between(prev_stop.location, patient.location, gap_start)
        earliest_arrival = gap_start + dt.timedelta(minutes=drive_to)
        appt_end = earliest_arrival + duration
        drive_from = estimator.minutes_between(patient.location, next_stop.location, appt_end)
        required_departure = appt_end + dt.timedelta(minutes=drive_from)

        if required_departure <= gap_end:
            baseline = estimator.minutes_between(prev_stop.location, next_stop.location, gap_start)
            detour = (drive_to + drive_from) - baseline

            notes = []
            if patient.duration_overridden:
                notes.append(f"Appointment length manually set to {patient.duration_min} minutes.")
            elif patient.duration_guessed:
                notes.append("We guessed how long this appointment should be — please confirm.")

            candidates.append(SlotRecommendation(
                patient=patient, calendar_name=calendar_name, day=day, start=earliest_arrival, end=appt_end,
                drive_before_min=drive_to, drive_after_min=drive_from, detour_min=max(detour, 0), notes=notes,
            ))
            if len(candidates) >= max_slots:
                return candidates

    return candidates


def explain_no_slot(patient: Patient, combined_stops, start_date: dt.date, end_date: dt.date, workday_hours: dict) -> str:
    duration = dt.timedelta(minutes=patient.duration_min)
    any_area_match = False
    any_big_gap = False

    day = start_date
    while day <= end_date:
        cfg = workday_hours.get(WEEKDAY_NAMES[day.weekday()])
        if not cfg or not cfg.get("enabled"):
            day += dt.timedelta(days=1)
            continue

        stops = combined_stops.get(day, [])
        for s in stops:
            if s.location == OFFICE_ADDRESS:
                continue
            matched, _ = area_match(s.location, patient.location)
            if matched:
                any_area_match = True

        day_start = dt.datetime.combine(day, parse_hhmm(cfg["start"]))
        day_end = dt.datetime.combine(day, parse_hhmm(cfg["end"]))
        timeline = [Stop(day_start, day_start, OFFICE_ADDRESS, "")] + stops + [Stop(day_end, day_end, OFFICE_ADDRESS, "")]
        for i in range(len(timeline) - 1):
            if timeline[i + 1].start - timeline[i].end >= duration:
                any_big_gap = True

        day += dt.timedelta(days=1)

    if not any_area_match:
        return "None of your scheduled route days in this window pass near this patient's area (no matching city or zip code found nearby)."
    if not any_big_gap:
        return "Your route days near this area are already full — no gap long enough for this appointment without bumping something else."
    return "A time gap exists nearby, but the drive time to/from this stop doesn't fit in the available window. Try a different day or check manually."


# ============================= DICT CONVERSION ================================

def rec_to_dict(cal_name: str, rec: SlotRecommendation, day_stops: list[Stop]) -> dict:
    return {
        "cal_name": cal_name,
        "patient_name": rec.patient.name,
        "patient_location": rec.patient.location,
        "day": rec.day.isoformat(),
        "start": rec.start.isoformat(),
        "end": rec.end.isoformat(),
        "drive_before": rec.drive_before_min,
        "drive_after": rec.drive_after_min,
        "detour": rec.detour_min,
        "notes": rec.notes,
        "day_stops": [
            {"summary": s.summary, "location": s.location, "start": s.start.isoformat(), "end": s.end.isoformat()}
            for s in day_stops
        ],
    }


def conflict_to_dict(cal_name: str, c: ScheduleConflict) -> dict:
    return {
        "cal_name": cal_name, "day": c.day.isoformat(),
        "event1_summary": c.event1_summary, "event1_start": c.event1_start.isoformat(), "event1_end": c.event1_end.isoformat(),
        "event2_summary": c.event2_summary, "event2_start": c.event2_start.isoformat(),
    }


def fmt_dict_time(iso_str: str) -> str:
    return dt.datetime.fromisoformat(iso_str).strftime("%I:%M %p").lstrip("0")


def fmt_dict_day(iso_str: str, fmt: str = "%A, %B %d") -> str:
    return dt.date.fromisoformat(iso_str).strftime(fmt)


def recs_to_csv(recommendations: list[dict]) -> str:
    buf = io.StringIO()
    writer = csv.writer(buf)
    writer.writerow(["Patient", "Employee", "Date", "Start Time", "End Time",
                      "Drive There (min)", "Drive To Next Stop (min)", "Extra Drive Time Added (min)", "Notes"])
    for r in recommendations:
        writer.writerow([
            clean_display(r["patient_name"], max_len=200), r["cal_name"],
            fmt_dict_day(r["day"], "%A, %b %d, %Y"), fmt_dict_time(r["start"]), fmt_dict_time(r["end"]),
            f"{r['drive_before']:.0f}", f"{r['drive_after']:.0f}", f"{r['detour']:.0f}",
            " / ".join(r["notes"]),
        ])
    return buf.getvalue()


# ============================= GOOGLE MAPS LINK GENERATOR =====================

def get_google_maps_url(rec: dict) -> str:
    day_stops = rec.get("day_stops", [])
    suggested_loc = rec.get("patient_location", "")
    
    # Combine existing day stops and the suggested stop, sorted chronologically by start time
    all_stops = [dict(s, is_suggested=False) for s in day_stops]
    if suggested_loc:
        all_stops.append({
            "summary": f"{rec['patient_name']} (suggested)",
            "location": suggested_loc,
            "start": rec["start"],
            "is_suggested": True
        })
    all_stops.sort(key=lambda s: s.get("start") or "")
    
    waypoints = [s["location"] for s in all_stops if s.get("location")]
    
    base_url = "https://www.google.com/maps/dir/?api=1"
    params = {
        "origin": OFFICE_ADDRESS,
        "destination": OFFICE_ADDRESS,
    }
    if waypoints:
        params["waypoints"] = "|".join(waypoints)
        
    return base_url + "&" + urllib.parse.urlencode(params)


# ============================= RESULTS RENDERING ================================

def render_results(data: dict, conn):
    conflicts = data.get("conflicts", [])
    if conflicts:
        with st.expander(f"⚠️ Heads up: {len(conflicts)} double-booked time slot(s) found on the calendar", expanded=True):
            st.caption("These are two appointments already scheduled at the same time. Suggestions near these times might not be reliable until this is fixed.")
            for c in conflicts:
                st.markdown(f"**{c['cal_name']} — {fmt_dict_day(c['day'])}**")
                col1, col2 = st.columns(2)
                with col1:
                    st.caption(f"Starts {fmt_dict_time(c['event1_start'])}")
                    st.text(clean_display(c["event1_summary"], 90))
                with col2:
                    st.caption(f"Overlaps — starts {fmt_dict_time(c['event2_start'])}")
                    st.text(clean_display(c["event2_summary"], 90))
                st.divider()

    skipped_dns = data.get("skipped_dns", [])
    skipped_no_address = data.get("skipped_no_address", [])
    if skipped_dns or skipped_no_address:
        with st.expander(f"ℹ️ {len(skipped_dns) + len(skipped_no_address)} patient(s) not included in this search", expanded=False):
            if skipped_dns:
                st.markdown("**Marked \"do not schedule\" (e.g. outside our service area):**")
                for name in skipped_dns:
                    st.text(clean_display(name, 90))
            if skipped_no_address:
                st.markdown("**No address on file — can't calculate driving directions for them:**")
                for name in skipped_no_address:
                    st.text(clean_display(name, 90))

    st.markdown("---")
    st.subheader("📋 Suggested Appointment Times")

    api_calls = data.get("api_call_count", 0)
    cache_hits = data.get("cache_hit_count", 0)
    estimated_cost = (api_calls / 1000) * DISTANCE_MATRIX_COST_PER_1000
    cache_note = f" (plus {cache_hits} reused from previous lookups, no charge)" if cache_hits else ""
    st.caption(f"Checked {api_calls} new driving routes this search{cache_note} — roughly ${estimated_cost:.2f} in Google Maps usage, before any free monthly credit.")

    recommendations = data.get("recommendations", [])
    if not recommendations:
        st.warning("No open time slots were found. Try a wider date range, or check the notes above for excluded patients.")
    else:
        sorted_recs = sorted(recommendations, key=lambda r: r["start"])
        best = sorted_recs[0]

        with st.container(border=True):
            st.markdown("### ⭐ Best Option")
            st.markdown(f"**Patient:** {clean_display(best['patient_name'], 80)}")
            st.markdown(f"**Employee:** {best['cal_name']}")
            st.markdown(f"**When:** {fmt_dict_day(best['day'])}, {fmt_dict_time(best['start'])} – {fmt_dict_time(best['end'])}")
            st.caption(f"Adds about {best['detour']:.0f} extra minutes of driving to that day "
                       f"({best['drive_before']:.0f} min drive there, {best['drive_after']:.0f} min to the next stop)")
            for note in best["notes"]:
                st.warning(note, icon="⚠️")
            
            map_url = get_google_maps_url(best)
            st.link_button("🗺️ Open full route in Google Maps", map_url, use_container_width=True)

        by_patient: dict[str, dict[str, dict]] = {}
        for r in recommendations:
            slot = by_patient.setdefault(r["patient_name"], {})
            if r["cal_name"] not in slot or r["detour"] < slot[r["cal_name"]]["detour"]:
                slot[r["cal_name"]] = r

        multi_tech_patients = {n: c for n, c in by_patient.items() if len(c) > 1}
        if multi_tech_patients:
            st.markdown("### 👥 Which Employee Should Take This?")
            for pname, cal_recs in multi_tech_patients.items():
                ranked = sorted(cal_recs.items(), key=lambda kv: kv[1]["detour"])
                best_tech, best_tech_rec = ranked[0]
                with st.container(border=True):
                    st.markdown(f"**{clean_display(pname, 80)}**")
                    st.markdown(f"Better fit: **{best_tech}** — {fmt_dict_day(best_tech_rec['day'], '%a, %b %d')} at "
                                f"{fmt_dict_time(best_tech_rec['start'])} (+{best_tech_rec['detour']:.0f} min extra driving)")
                    for cal_name, rec in ranked[1:]:
                        st.caption(f"{cal_name} could also do {fmt_dict_day(rec['day'], '%a, %b %d')} at "
                                   f"{fmt_dict_time(rec['start'])} (+{rec['detour']:.0f} min extra driving)")

        if len(sorted_recs) > 1:
            st.markdown("### Other Times That Would Work")
            for rec in sorted_recs[1:]:
                with st.container(border=True):
                    st.markdown(f"**{clean_display(rec['patient_name'], 80)}** — {rec['cal_name']}")
                    st.markdown(f"{fmt_dict_day(rec['day'], '%a, %b %d')}, {fmt_dict_time(rec['start'])} – "
                                f"{fmt_dict_time(rec['end'])}  ·  +{rec['detour']:.0f} min extra driving")
                    for note in rec["notes"]:
                        st.caption(f"⚠️ {note}")
                    
                    rec_map_url = get_google_maps_url(rec)
                    st.link_button("🗺️ Open full route in Google Maps", rec_map_url, use_container_width=True)

        st.markdown("---")
        csv_data = recs_to_csv(sorted_recs)
        st.download_button("⬇️ Download this list (for printing or texting to someone)", data=csv_data,
                            file_name=f"appointment_suggestions_{dt.date.today():%Y-%m-%d}.csv", mime="text/csv",
                            key=f"download_{data.get('generated_at', 'current')}")

    no_slot_reasons = data.get("no_slot_reasons", {})
    if no_slot_reasons:
        st.markdown("### Why some patients didn't get a suggested time")
        for name, reason in no_slot_reasons.items():
            with st.container(border=True):
                st.markdown(f"**{clean_display(name, 80)}**")
                st.caption(reason)


# ============================= SETTINGS TAB ================================

def render_settings_tab(conn):
    st.header("⚙️ Business Rules")
    st.caption("Changes here apply the next time the scheduler is run — no code changes needed.")
    settings = get_settings(conn)

    st.subheader("Working Hours")
    hours_rows = [
        {"Day": d, "Working?": cfg["enabled"], "Start Time": parse_hhmm(cfg["start"]), "End Time": parse_hhmm(cfg["end"])}
        for d, cfg in settings["workday_hours"].items()
    ]
    hours_df = pd.DataFrame(hours_rows)
    edited_hours = st.data_editor(
        hours_df, hide_index=True, num_rows="fixed", key="hours_editor",
        column_config={
            "Working?": st.column_config.CheckboxColumn("Working?"),
            "Start Time": st.column_config.TimeColumn("Start Time"),
            "End Time": st.column_config.TimeColumn("End Time"),
        },
    )

    st.subheader("Appointment Length Rules")
    st.caption("If a patient's notes contain one of these keywords, that length is used. First match (top to bottom) wins.")
    dur_df = pd.DataFrame(settings["duration_rules"], columns=["Keyword", "Minutes"])
    edited_dur = st.data_editor(dur_df, hide_index=True, num_rows="dynamic", key="duration_editor")

    default_dur = st.number_input("Default length if no keyword matches (minutes)", min_value=15, max_value=180,
                                   step=15, value=settings["default_duration_min"])

    st.subheader("Do-Not-Schedule Keywords")
    st.caption("If a patient's notes contain any of these phrases (one per line), they're automatically excluded from searches.")
    dns_text = st.text_area("Keywords", value="\n".join(settings["do_not_schedule_markers"]), height=100, key="dns_editor")

    if st.button("💾 Save Settings", type="primary"):
        try:
            new_settings = {
                "workday_hours": {
                    row["Day"]: {
                        "enabled": bool(row["Working?"]),
                        "start": row["Start Time"].strftime("%H:%M") if hasattr(row["Start Time"], "strftime") else str(row["Start Time"]),
                        "end": row["End Time"].strftime("%H:%M") if hasattr(row["End Time"], "strftime") else str(row["End Time"]),
                    }
                    for _, row in edited_hours.iterrows()
                },
                "duration_rules": [
                    [str(r["Keyword"]).strip().lower(), int(r["Minutes"])]
                    for _, r in edited_dur.iterrows() if str(r["Keyword"]).strip()
                ],
                "default_duration_min": int(default_dur),
                "do_not_schedule_markers": [line.strip().lower() for line in dns_text.splitlines() if line.strip()],
            }
            save_settings(conn, new_settings)
            st.success("Settings saved — these will apply the next time you run the scheduler.")
        except Exception as e:
            st.error(f"Couldn't save settings: {e}")


# ============================= STREAMLIT UI =================================

st.title("📅 Smart Schedule Finder")
st.caption("Finds the best open time slot for patients who still need an appointment — based on your existing route and drive times.")

db_conn = get_db_connection()

if "history_view" not in st.session_state:
    st.session_state.history_view = None

tab_scheduler, tab_settings = st.tabs(["📅 Scheduler", "⚙️ Settings"])

with tab_settings:
    render_settings_tab(db_conn)

with tab_scheduler:
    with st.spinner("Connecting to your calendars…"):
        service = get_calendar_service() 
        try:
            calendar_map = get_user_calendars(service)
        except Exception as e:
            st.warning("Couldn't connect to Google Calendar right now. Using the saved default calendars — results may not be accurate until this is fixed.")
            calendar_map = {"Wade Hendrickson": "w1a9d7e4@gmail.com", "Dylan Hendrickson-Work Schedule": "primary"}

with st.sidebar.expander("📖 How This Works", expanded=False):
    st.markdown("""
    **What it does:** Looks at patients who still need an appointment, checks
    your existing schedule and real drive times, and suggests the best open
    time slot that doesn't add much extra driving to your day.

    **Steps:**
    1. Pick **which calendar** to search (Wade's, Dylan's, or both).
    2. Choose a **search type**:
       - *One specific patient* — look up one person by name (up to 31 days out). You can also set a specific appointment length instead of letting it guess.
       - *Batch* — automatically checks the next 14 days for 5 people who've been waiting longest.
    3. Check **"Skip today"** if you're working from home and don't want today included.
    4. Click **Run Scheduler**.

    **Good to know:**
    - Patients marked "do not schedule" are automatically left out.
    - Patients with no address on file are left out too, but you'll see who they are.
    - Drive times are always calculated from real addresses via Google Maps — those numbers are accurate.
    - Click **"Open full route in Google Maps"** on any suggestion to instantly open that day's complete route in Google Maps.
    - Business rules (working hours, appointment lengths, do-not-schedule keywords) live under the **⚙️ Settings** tab.
    - Every search is saved under **Past Searches** below — revisit one anytime without using any new Google Maps lookups.
    """)

with st.sidebar.expander("📜 Past Searches", expanded=False):
    runs = list_runs(db_conn, limit=25)
    if not runs:
        st.caption("No searches yet — run one and it'll show up here.")
    else:
        for run_id, created_at, mode, calendar_label in runs:
            when = dt.datetime.fromisoformat(created_at).strftime("%a %b %d, %I:%M %p").replace(" 0", " ")
            col1, col2 = st.columns([4, 1])
            with col1:
                if st.button(f"{when} — {mode} ({calendar_label})", key=f"load_{run_id}", use_container_width=True):
                    st.session_state.history_view = load_run(db_conn, run_id)
                    st.rerun()
            with col2:
                if st.button("🗑️", key=f"del_{run_id}"):
                    delete_run(db_conn, run_id)
                    st.rerun()

if st.session_state.history_view:
    with tab_scheduler:
        st.info("Showing a saved search from earlier — no new lookups were made.")
        if st.button("🔙 Back to a new search"):
            st.session_state.history_view = None
            st.rerun()
        render_results(st.session_state.history_view, db_conn)
else:
    st.sidebar.header("Search Settings")
    cal_options = list(calendar_map.keys())
    if len(cal_options) > 1:
        cal_options.insert(0, "Both Employees Combined")
    selected_calendar = st.sidebar.selectbox("Whose calendar?", cal_options)

    mode = st.sidebar.radio("Look for", ["One specific patient", "Batch — next 5 patients"])
    target_name = ""
    override_address = ""
    override_minutes = None
    skip_recent = False

    if mode == "One specific patient":
        target_name = st.sidebar.text_input("Patient's name").strip().lower()
        override_address = st.sidebar.text_input("Address (only if this patient isn't on the calendar yet)").strip()
        if st.sidebar.checkbox("Set a specific appointment length instead of guessing"):
            override_minutes = st.sidebar.number_input("Length (minutes)", min_value=15, max_value=180, value=60, step=15)
    else:
        skip_recent = st.sidebar.checkbox("Skip patients I've already checked in the last 7 days", value=True)
        st.sidebar.caption("Patients are checked longest-waiting first.")

    skip_today = st.sidebar.checkbox("Skip today (e.g. working from home)")

    if st.sidebar.button("Run Scheduler", type="primary"):
        if mode == "One specific patient" and not target_name:
            st.error("Please type a patient name first.")
        else:
            with tab_scheduler:
                status_box = st.empty()
                status_box.text("Getting started…")

                settings = get_settings(db_conn)
                target_cal_dict = calendar_map if selected_calendar == "Both Employees Combined" else {selected_calendar: calendar_map[selected_calendar]}
                max_slots = 3 if mode == "One specific patient" else 1

                try:
                    service = get_calendar_service()
                    estimator = DriveTimeEstimator(MAPS_API_KEY, status_box, db_conn)

                    today = dt.date.today()
                    base_start_date = today + dt.timedelta(days=1) if skip_today else today
                    if mode == "One specific patient":
                        search_start, search_end = base_start_date, base_start_date + dt.timedelta(days=31)
                    else:
                        search_start, search_end = base_start_date, base_start_date + dt.timedelta(days=14)

                    status_box.text("Looking up patients who still need an appointment…")
                    patients, skipped_no_address, skipped_dns = get_unscheduled_patients(
                        service, settings["do_not_schedule_markers"], settings["duration_rules"], settings["default_duration_min"]
                    )

                    if target_name:
                        filtered = [p for p in patients if target_name in p.name.lower()]
                        if not filtered:
                            if override_address:
                                patients = [Patient(
                                    event_id="phone_in", name=target_name, location=override_address,
                                    duration_min=override_minutes or settings["default_duration_min"],
                                    raw_summary=target_name, calendar_id="w1a9d7e4@gmail.com",
                                    duration_guessed=not override_minutes, duration_overridden=bool(override_minutes),
                                )]
                            else:
                                st.error(f"Couldn't find a patient matching '{target_name}'. If they're not on the calendar yet, enter their address and try again.")
                                st.stop()
                        else:
                            patients = filtered[:1]
                            if override_minutes:
                                patients[0].duration_min = override_minutes
                                patients[0].duration_guessed = False
                                patients[0].duration_overridden = True
                    else:
                        patients.sort(key=lambda p: p.created_at or "")  
                        if skip_recent:
                            recent_ids = recently_checked_ids(db_conn, days=7)
                            patients = [p for p in patients if p.event_id not in recent_ids]
                        patients = patients[:5]

                    recommendations_out: list[dict] = []
                    conflicts_out: list[dict] = []
                    seen_conflicts = set()

                    for patient in patients:
                        for cal_name, cal_id in target_cal_dict.items():
                            status_box.text(f"Checking {cal_name}'s schedule for {clean_display(patient.name, 40)}…")

                            schedule, conflicts = get_days_schedule(service, cal_id, search_start, search_end)
                            for c in conflicts:
                                dedupe_key = (cal_name, c.day, c.event1_summary, c.event2_summary)
                                if dedupe_key not in seen_conflicts:
                                    seen_conflicts.add(dedupe_key)
                                    conflicts_out.append(conflict_to_dict(cal_name, c))

                            recs = find_slots_for_patient_on_calendar(
                                patient, cal_name, schedule, estimator, search_start, search_end, max_slots, settings["workday_hours"]
                            )
                            for r in recs:
                                recommendations_out.append(rec_to_dict(cal_name, r, schedule.get(r.day, [])))

                        if patient.event_id != "phone_in":
                            record_lookup(db_conn, patient.event_id)

                    processed_names = {p.name for p in patients}
                    recommended_names = {r["patient_name"] for r in recommendations_out}
                    no_slot_names = processed_names - recommended_names
                    no_slot_reasons = {}
                    if no_slot_names:
                        cal_ids_list = list(target_cal_dict.values())
                        combined = combined_schedule(service, cal_ids_list, search_start, search_end)
                        for p in patients:
                            if p.name in no_slot_names:
                                no_slot_reasons[p.name] = explain_no_slot(p, combined, search_start, search_end, settings["workday_hours"])

                    status_box.empty()
                    st.success("Done!")

                    data = {
                        "generated_at": dt.datetime.now().isoformat(),
                        "mode": mode, "calendar_label": selected_calendar,
                        "api_call_count": estimator.api_call_count, "cache_hit_count": estimator.cache_hit_count,
                        "conflicts": conflicts_out, "skipped_dns": skipped_dns, "skipped_no_address": skipped_no_address,
                        "recommendations": recommendations_out, "no_slot_reasons": no_slot_reasons,
                    }
                    save_run(db_conn, mode, selected_calendar, data)
                    render_results(data, db_conn)

                except Exception as e:
                    st.error(f"Something went wrong: {e}")
