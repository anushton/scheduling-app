import os
import re
import io
import csv
import json
import sqlite3
import datetime as dt
from dataclasses import dataclass, field
import streamlit as st

# Try importing googlemaps; fall back gracefully if missing
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
    """Returns `True` if the user enters the correct password."""
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

WORKDAY_START = dt.time(9, 0)
WORKDAY_END = dt.time(17, 30)

WORK_WEEKDAYS = {0, 1, 2, 3, 4}
UNSCHEDULED_COLOR_ID = "5"
DEFAULT_DURATION_MIN = 60
DURATION_RULES = [
    ("hearing test", 60),
    ("test appt", 60),
    ("cleaning", 60),
    ("earwax", 30),
    ("service", 30),
]
SCOPES = ["https://www.googleapis.com/auth/calendar.readonly"]

# Approximate published Google Maps Distance Matrix pricing: ~$5 per 1000
# elements (1 element = 1 origin/destination pair). Rough estimate only —
# not an official billing figure.
DISTANCE_MATRIX_COST_PER_1000 = 5.0

# How long a cached drive time is considered good before we'll ask Google
# Maps for a fresh one again. Traffic patterns repeat roughly by time of
# day, so a few hours is a reasonable balance between accuracy and not
# re-paying for the same route over and over in one workday.
DRIVE_TIME_CACHE_MAX_AGE_HOURS = 6

# How many past searches to keep in history before old ones are cleaned up.
MAX_SAVED_RUNS = 100

# SQLite file sits next to this script. NOTE: if this app is deployed on a
# host with an ephemeral filesystem (e.g. it gets rebuilt/redeployed from
# scratch periodically), this file — and the history/cache in it — can get
# wiped on redeploy. It persists fine across normal day-to-day usage and
# restarts in between. For guaranteed permanence, this would need to move
# to an external database instead.
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
    duration_guessed: bool = False  # True if no keyword matched, used the default

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
    """Collapse messy whitespace/line breaks from raw calendar notes and
    trim to a readable length. Only ever pass this to st.text()/st.code()
    (never st.markdown) — raw notes can contain $ signs or stray */_
    characters that Streamlit's markdown renderer would try to interpret
    as formatting."""
    text = " ".join((text or "").split())
    if len(text) > max_len:
        text = text[:max_len].rstrip() + "…"
    return text


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
    conn.commit()
    return conn


def save_run(conn, mode: str, calendar_label: str, data: dict) -> int:
    now = dt.datetime.now().isoformat()
    cur = conn.execute(
        "INSERT INTO runs (created_at, mode, calendar_label, api_call_count, data_json) VALUES (?, ?, ?, ?, ?)",
        (now, mode, calendar_label, data.get("api_call_count", 0), json.dumps(data)),
    )
    conn.commit()
    conn.execute(
        "DELETE FROM runs WHERE id NOT IN (SELECT id FROM runs ORDER BY id DESC LIMIT ?)",
        (MAX_SAVED_RUNS,),
    )
    conn.commit()
    return cur.lastrowid


def list_runs(conn, limit: int = 25):
    cur = conn.execute(
        "SELECT id, created_at, mode, calendar_label FROM runs ORDER BY id DESC LIMIT ?",
        (limit,),
    )
    return cur.fetchall()


def load_run(conn, run_id: int) -> dict | None:
    cur = conn.execute("SELECT data_json FROM runs WHERE id = ?", (run_id,))
    row = cur.fetchone()
    return json.loads(row[0]) if row else None


def delete_run(conn, run_id: int):
    conn.execute("DELETE FROM runs WHERE id = ?", (run_id,))
    conn.commit()


def get_cached_drive_time(conn, origin: str, destination: str, max_age_hours: float) -> float | None:
    cur = conn.execute(
        "SELECT minutes, cached_at FROM drive_time_cache WHERE origin = ? AND destination = ?",
        (origin, destination),
    )
    row = cur.fetchone()
    if not row:
        return None
    minutes, cached_at = row
    cached_dt = dt.datetime.fromisoformat(cached_at)
    if dt.datetime.now() - cached_dt > dt.timedelta(hours=max_age_hours):
        return None
    return minutes


def save_drive_time_to_cache(conn, origin: str, destination: str, minutes: float):
    now = dt.datetime.now().isoformat()
    conn.execute(
        """INSERT INTO drive_time_cache (origin, destination, minutes, cached_at)
           VALUES (?, ?, ?, ?)
           ON CONFLICT(origin, destination) DO UPDATE SET minutes = excluded.minutes, cached_at = excluded.cached_at""",
        (origin, destination, minutes, now),
    )
    conn.commit()

# ============================================================================


def get_calendar_service():
    creds = None

    # Load from Streamlit secrets token block
    if "GOOGLE_TOKEN" in st.secrets:
        token_info = {
            "token": st.secrets["GOOGLE_TOKEN"].get("token"),
            "refresh_token": st.secrets["GOOGLE_TOKEN"].get("refresh_token"),
            "token_uri": st.secrets["GOOGLE_TOKEN"].get("token_uri"),
            "client_id": st.secrets["GOOGLE_TOKEN"].get("client_id"),
            "client_secret": st.secrets["GOOGLE_TOKEN"].get("client_secret"),
            "scopes": list(st.secrets["GOOGLE_TOKEN"].get("scopes", SCOPES)),
            "universe_domain": st.secrets["GOOGLE_TOKEN"].get("universe_domain", "googleapis.com"),
            "account": st.secrets["GOOGLE_TOKEN"].get("account", ""),
            "expiry": st.secrets["GOOGLE_TOKEN"].get("expiry")
        }
        creds = Credentials.from_authorized_user_info(token_info, SCOPES)

    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            try:
                creds.refresh(Request())
            except Exception:
                creds = None

        if not creds:
            client_config = {
                "installed": {
                    "client_id": st.secrets["GOOGLE_CREDENTIALS"]["installed"]["client_id"],
                    "client_secret": st.secrets["GOOGLE_CREDENTIALS"]["installed"]["client_secret"],
                    "auth_uri": st.secrets["GOOGLE_CREDENTIALS"]["installed"]["auth_uri"],
                    "token_uri": st.secrets["GOOGLE_CREDENTIALS"]["installed"]["token_uri"],
                    "redirect_uris": ["http://localhost"]
                }
            }
            flow = Flow.from_client_config(client_config, scopes=SCOPES)
            flow.redirect_uri = "urn:ietf:wg:oauth:2.0:oob"
            auth_url, _ = flow.authorization_url(prompt='consent')
            st.warning(f"One-time setup needed. Open this link, sign in, and copy the code it gives you: {auth_url}")
            code = st.text_input("Paste the code here:")
            if code:
                try:
                    flow.fetch_token(code=code)
                    creds = flow.credentials
                except Exception:
                    st.error(
                        "That code didn't work. If this keeps happening, contact "
                        "whoever set up this app — the sign-in link may need to be "
                        "regenerated on Google's side."
                    )
                    st.stop()
            else:
                st.stop()

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


def guess_duration(text: str) -> tuple[int, bool]:
    """Returns (minutes, was_guessed). was_guessed=True means no keyword
    matched and we fell back to the default — flagged as lower confidence."""
    text_lower = text.lower()
    for keyword, minutes in DURATION_RULES:
        if keyword in text_lower:
            return minutes, False
    return DEFAULT_DURATION_MIN, True


DO_NOT_SCHEDULE_MARKERS = ["do not schedule", "don't schedule", "outside service area", "out of area"]


def get_unscheduled_patients(service) -> tuple[list[Patient], list[str], list[str]]:
    """Returns (patients, skipped_no_address, skipped_do_not_schedule)."""
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
                calendarId=target_cal_id,
                timeMin=time_min,
                timeMax=time_max,
                singleEvents=True,
                orderBy="startTime",
                maxResults=250,
                pageToken=page_token,
            ).execute()
        except Exception:
            try:
                resp = service.events().list(
                    calendarId="primary",
                    timeMin=time_min,
                    timeMax=time_max,
                    singleEvents=True,
                    orderBy="startTime",
                    maxResults=250,
                    pageToken=page_token,
                ).execute()
            except Exception:
                st.error(
                    "Couldn't read the calendar to find unscheduled patients. "
                    "The list below may be empty or incomplete — this is a "
                    "connection problem, not 'no patients found.'"
                )
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

            if any(marker in combined_lower for marker in DO_NOT_SCHEDULE_MARKERS):
                skipped_do_not_schedule.append(summary or "(untitled event)")
                continue

            if not location:
                skipped_no_address.append(summary or "(untitled event)")
                continue

            duration, was_guessed = guess_duration(combined_text)
            patients.append(Patient(
                event_id=e["id"],
                name=summary,
                location=location,
                duration_min=duration,
                raw_summary=summary,
                calendar_id=target_cal_id,
                duration_guessed=was_guessed,
            ))
        page_token = resp.get("nextPageToken")
        if not page_token:
            break
    return patients, skipped_no_address, skipped_do_not_schedule


# In-memory cache (this run only) of already-fetched calendar schedules,
# keyed by (calendar_id, start_date, end_date) — avoids re-fetching the
# same calendar's events once per patient when batching multiple patients.
_schedule_cache: dict[tuple[str, dt.date, dt.date], tuple[dict[dt.date, list[Stop]], list[ScheduleConflict]]] = {}


def get_days_schedule(service, calendar_id: str, start_date: dt.date, end_date: dt.date) -> tuple[dict[dt.date, list[Stop]], list[ScheduleConflict]]:
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
                calendarId=calendar_id,
                timeMin=time_min,
                timeMax=time_max,
                singleEvents=True,
                orderBy="startTime",
                maxResults=250,
                pageToken=page_token,
            ).execute()
        except Exception:
            st.error(
                "Couldn't read the existing schedule for one of the calendars. "
                "Recommendations for that calendar may be treating busy days "
                "as open — double-check before booking anything from this run."
            )
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
                start=s, end=en,
                location=e.get("location", OFFICE_ADDRESS),
                summary=e.get("summary", ""),
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
                    day=day,
                    event1_summary=stops[i].summary,
                    event1_start=stops[i].start,
                    event1_end=stops[i].end,
                    event2_summary=stops[i + 1].summary,
                    event2_start=stops[i + 1].start,
                ))

    _schedule_cache[cache_key] = (schedule, conflicts)
    return schedule, conflicts


class DriveTimeEstimator:
    """Checks an in-memory cache first, then the persistent SQLite cache
    (shared across runs/sessions), and only calls the Google Maps API if
    neither has a recent-enough answer. Real API results get written back
    to the persistent cache so future searches can reuse them."""

    def __init__(self, api_key: str, status_box, conn):
        self.client = googlemaps.Client(key=api_key) if (api_key and googlemaps) else None
        self.cache: dict[tuple[str, str], float] = {}
        self.api_call_count = 0
        self.cache_hit_count = 0
        self.status_box = status_box
        self.conn = conn

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
                    origins=[origin],
                    destinations=[destination],
                    departure_time=depart_at,
                    traffic_model="best_guess",
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
            # only persist real API results, not the flat 25-min fallback guess
            save_drive_time_to_cache(self.conn, origin, destination, minutes)

        self.cache[key] = minutes
        return minutes


def area_match(loc1: str, loc2: str) -> tuple[bool, str]:
    t1 = loc1.lower()
    t2 = loc2.lower()
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


def find_slots_for_patient_on_calendar(
    patient: Patient,
    calendar_name: str,
    schedule: dict[dt.date, list[Stop]],
    estimator: DriveTimeEstimator,
    start_date: dt.date,
    end_date: dt.date,
    max_slots: int
) -> list[SlotRecommendation]:
    candidates: list[SlotRecommendation] = []
    duration = dt.timedelta(minutes=patient.duration_min)
    now = dt.datetime.now()

    day = start_date
    while day <= end_date:
        if day.weekday() not in WORK_WEEKDAYS:
            day += dt.timedelta(days=1)
            continue

        day_start = dt.datetime.combine(day, WORKDAY_START)
        day_end = dt.datetime.combine(day, WORKDAY_END)

        stops = schedule.get(day, [])
        timeline = (
            [Stop(day_start, day_start, OFFICE_ADDRESS, "Leave office")]
            + stops
            + [Stop(day_end, day_end, OFFICE_ADDRESS, "Back at office")]
        )

        for i in range(len(timeline) - 1):
            prev_stop = timeline[i]
            next_stop = timeline[i + 1]
            gap_start = prev_stop.end
            gap_end = next_stop.start

            if gap_start < now: gap_start = now + dt.timedelta(minutes=1)
            if gap_start < day_start: gap_start = day_start
            if gap_end <= gap_start: continue

            if prev_stop.location != OFFICE_ADDRESS:
                matched, _ = area_match(prev_stop.location, patient.location)
                if not matched:
                    continue
            if next_stop.location != OFFICE_ADDRESS:
                matched, _ = area_match(next_stop.location, patient.location)
                if not matched:
                    continue

            drive_to = estimator.minutes_between(prev_stop.location, patient.location, gap_start)
            earliest_arrival = gap_start + dt.timedelta(minutes=drive_to)
            appt_end = earliest_arrival + duration
            drive_from = estimator.minutes_between(patient.location, next_stop.location, appt_end)

            required_departure = appt_end + dt.timedelta(minutes=drive_from)

            if required_departure <= gap_end:
                baseline = estimator.minutes_between(prev_stop.location, next_stop.location, gap_start)
                detour = (drive_to + drive_from) - baseline

                notes = []
                if patient.duration_guessed:
                    notes.append("We guessed how long this appointment should be — please confirm.")

                candidates.append(SlotRecommendation(
                    patient=patient, calendar_name=calendar_name, day=day, start=earliest_arrival, end=appt_end,
                    drive_before_min=drive_to, drive_after_min=drive_from, detour_min=max(detour, 0),
                    notes=notes,
                ))

                if len(candidates) >= max_slots:
                    return candidates

        day += dt.timedelta(days=1)

    return candidates


# ============================= DICT CONVERSION ================================
# Recommendations/conflicts get converted to plain dicts (with ISO date/time
# strings) right after they're computed, so the exact same rendering code
# and CSV export can work whether the data just came from a fresh run or
# was loaded back out of the history database.

def rec_to_dict(cal_name: str, rec: SlotRecommendation) -> dict:
    return {
        "cal_name": cal_name,
        "patient_name": rec.patient.name,
        "day": rec.day.isoformat(),
        "start": rec.start.isoformat(),
        "end": rec.end.isoformat(),
        "drive_before": rec.drive_before_min,
        "drive_after": rec.drive_after_min,
        "detour": rec.detour_min,
        "notes": rec.notes,
    }


def conflict_to_dict(cal_name: str, c: ScheduleConflict) -> dict:
    return {
        "cal_name": cal_name,
        "day": c.day.isoformat(),
        "event1_summary": c.event1_summary,
        "event1_start": c.event1_start.isoformat(),
        "event1_end": c.event1_end.isoformat(),
        "event2_summary": c.event2_summary,
        "event2_start": c.event2_start.isoformat(),
    }


def fmt_dict_time(iso_str: str) -> str:
    return dt.datetime.fromisoformat(iso_str).strftime("%I:%M %p").lstrip("0")


def fmt_dict_day(iso_str: str, fmt: str = "%A, %B %d") -> str:
    return dt.date.fromisoformat(iso_str).strftime(fmt)


def recs_to_csv(recommendations: list[dict]) -> str:
    buf = io.StringIO()
    writer = csv.writer(buf)
    writer.writerow([
        "Patient", "Employee", "Date", "Start Time", "End Time",
        "Drive There (min)", "Drive To Next Stop (min)", "Extra Drive Time Added (min)",
        "Notes",
    ])
    for r in recommendations:
        writer.writerow([
            clean_display(r["patient_name"], max_len=200),
            r["cal_name"],
            fmt_dict_day(r["day"], "%A, %b %d, %Y"),
            fmt_dict_time(r["start"]),
            fmt_dict_time(r["end"]),
            f"{r['drive_before']:.0f}",
            f"{r['drive_after']:.0f}",
            f"{r['detour']:.0f}",
            " / ".join(r["notes"]),
        ])
    return buf.getvalue()


def render_results(data: dict):
    """Renders a full results view from a plain dict — used for both a
    freshly-completed run and a run reloaded from history."""

    conflicts = data.get("conflicts", [])
    if conflicts:
        with st.expander(f"⚠️ Heads up: {len(conflicts)} double-booked time slot(s) found on the calendar", expanded=True):
            st.caption("These are two appointments already scheduled at the same time. "
                       "Suggestions near these times might not be reliable until this is fixed.")
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
    cache_note = f" (plus {cache_hits} reused from previous searches, no charge)" if cache_hits else ""
    st.caption(f"Checked {api_calls} new driving routes this search{cache_note} "
               f"— roughly ${estimated_cost:.2f} in Google Maps usage, before any free monthly credit.")

    recommendations = data.get("recommendations", [])
    if not recommendations:
        st.warning("No open time slots were found. Try a wider date range, or check the notes above for excluded patients.")
        return

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

    # Compare employees, when more than one calendar contributed results
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
                st.markdown(f"Better fit: **{best_tech}** — "
                            f"{fmt_dict_day(best_tech_rec['day'], '%a, %b %d')} at "
                            f"{fmt_dict_time(best_tech_rec['start'])} "
                            f"(+{best_tech_rec['detour']:.0f} min extra driving)")
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

    st.markdown("---")
    csv_data = recs_to_csv(sorted_recs)
    st.download_button(
        "⬇️ Download this list (for printing or texting to someone)",
        data=csv_data,
        file_name=f"appointment_suggestions_{dt.date.today():%Y-%m-%d}.csv",
        mime="text/csv",
        key=f"download_{data.get('generated_at', 'current')}",
    )


# ============================= STREAMLIT UI =================================

st.title("📅 Smart Schedule Finder")
st.caption("Finds the best open time slot for patients who still need an appointment — based on your existing route and drive times.")

db_conn = get_db_connection()

if "history_view" not in st.session_state:
    st.session_state.history_view = None

with st.spinner("Connecting to your calendars…"):
    try:
        service = get_calendar_service()
        calendar_map = get_user_calendars(service)
    except Exception:
        st.warning("Couldn't connect to Google Calendar right now. Using the saved default calendars — "
                   "results may not be accurate until this is fixed.")
        calendar_map = {
            "Wade Hendrickson": "w1a9d7e4@gmail.com",
            "Dylan Hendrickson-Work Schedule": "primary"
        }

with st.sidebar.expander("📖 How This Works", expanded=False):
    st.markdown("""
    **What it does:** Looks at patients who still need an appointment, checks
    your existing schedule and real drive times, and suggests the best open
    time slot that doesn't add much extra driving to your day.

    **Steps:**
    1. Pick **which calendar** to search (Wade's, Dylan's, or both).
    2. Choose a **search type**:
       - *One specific patient* — look up one person by name (up to 31 days out).
       - *Batch* — automatically check the next 14 days for 5 people who still need an appointment.
    3. Check **"Skip today"** if you're working from home and don't want today included.
    4. Click **Run Scheduler**.

    **Good to know:**
    - Patients marked "do not schedule" (like out-of-area patients) are automatically left out.
    - Patients with no address on file are left out too, but you'll see who they are.
    - A note under a suggested time means the appointment length was a guess — worth confirming.
    - Drive times are always calculated from real addresses via Google Maps — those numbers are accurate.
    - Every search you run is saved. Use **Past Searches** below to look at one again without
      running it a second time (saves time and doesn't use up any Google Maps lookups).
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
    st.info(f"Showing a saved search from earlier — no new lookups were made.")
    if st.button("🔙 Back to a new search"):
        st.session_state.history_view = None
        st.rerun()
    render_results(st.session_state.history_view)
    st.stop()

st.sidebar.header("Search Settings")
cal_options = list(calendar_map.keys())
if len(cal_options) > 1:
    cal_options.insert(0, "Both Employees Combined")
selected_calendar = st.sidebar.selectbox("Whose calendar?", cal_options)

mode = st.sidebar.radio("Look for", ["One specific patient", "Batch — next 5 patients"])
target_name = ""
override_address = ""

if mode == "One specific patient":
    target_name = st.sidebar.text_input("Patient's name").strip().lower()
    override_address = st.sidebar.text_input("Address (only if this patient isn't on the calendar yet)").strip()

skip_today = st.sidebar.checkbox("Skip today (e.g. working from home)")

if st.sidebar.button("Run Scheduler", type="primary"):
    if mode == "One specific patient" and not target_name:
        st.error("Please type a patient name first.")
    else:
        status_box = st.empty()
        status_box.text("Getting started…")

        target_cal_dict = calendar_map if selected_calendar == "Both Employees Combined" else {selected_calendar: calendar_map[selected_calendar]}
        max_slots = 3 if mode == "One specific patient" else 1

        try:
            service = get_calendar_service()
            estimator = DriveTimeEstimator(MAPS_API_KEY, status_box, db_conn)

            today = dt.date.today()
            base_start_date = today + dt.timedelta(days=1) if skip_today else today

            status_box.text("Looking up patients who still need an appointment…")
            patients, skipped_no_address, skipped_dns = get_unscheduled_patients(service)

            if target_name:
                filtered = [p for p in patients if target_name in p.name.lower()]
                if not filtered:
                    if override_address:
                        patients = [Patient(
                            event_id="phone_in",
                            name=target_name,
                            location=override_address,
                            duration_min=DEFAULT_DURATION_MIN,
                            raw_summary=target_name,
                            calendar_id="w1a9d7e4@gmail.com",
                            duration_guessed=True,
                        )]
                    else:
                        st.error(f"Couldn't find a patient matching '{target_name}'. If they're not on the calendar yet, "
                                 "enter their address in the box on the left and try again.")
                        st.stop()
                else:
                    patients = filtered[:1]
            else:
                patients = patients[:5]

            recommendations_out: list[dict] = []
            conflicts_out: list[dict] = []
            seen_conflicts = set()

            for patient in patients:
                for cal_name, cal_id in target_cal_dict.items():
                    status_box.text(f"Checking {cal_name}'s schedule for {clean_display(patient.name, 40)}…")

                    if mode == "One specific patient":
                        current_start_date = base_start_date
                        current_end_date = current_start_date + dt.timedelta(days=31)
                    else:
                        current_start_date = base_start_date
                        current_end_date = base_start_date + dt.timedelta(days=14)

                    schedule, conflicts = get_days_schedule(service, cal_id, current_start_date, current_end_date)
                    for c in conflicts:
                        dedupe_key = (cal_name, c.day, c.event1_summary, c.event2_summary)
                        if dedupe_key not in seen_conflicts:
                            seen_conflicts.add(dedupe_key)
                            conflicts_out.append(conflict_to_dict(cal_name, c))

                    recs = find_slots_for_patient_on_calendar(
                        patient, cal_name, schedule, estimator, current_start_date, current_end_date, max_slots
                    )
                    for r in recs:
                        recommendations_out.append(rec_to_dict(cal_name, r))

            status_box.empty()
            st.success("Done!")

            data = {
                "generated_at": dt.datetime.now().isoformat(),
                "mode": mode,
                "calendar_label": selected_calendar,
                "api_call_count": estimator.api_call_count,
                "cache_hit_count": estimator.cache_hit_count,
                "conflicts": conflicts_out,
                "skipped_dns": skipped_dns,
                "skipped_no_address": skipped_no_address,
                "recommendations": recommendations_out,
            }
            save_run(db_conn, mode, selected_calendar, data)
            render_results(data)

        except Exception as e:
            st.error(f"Something went wrong: {e}")
