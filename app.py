import os
import re
import io
import csv
import json
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
# elements (1 element = 1 origin/destination pair). This is a rough estimate
# of what this run *would* cost outside any free monthly credit — not an
# official billing figure.
DISTANCE_MATRIX_COST_PER_1000 = 5.0

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
    area_confidence: str = "zip"   # "zip" | "city" | "office" (office/zip = strong, city = weak)
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
    trim to a readable length for on-screen display. Safe to pass to
    st.text()/st.code() (never st.markdown, to avoid Streamlit interpreting
    $ signs or stray */_ characters in real notes as formatting)."""
    text = " ".join((text or "").split())
    if len(text) > max_len:
        text = text[:max_len].rstrip() + "…"
    return text


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
                except Exception as e:
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

            # Automatically skip patients explicitly marked not schedulable
            # (e.g. outside our service area) instead of trying to route them.
            if any(marker in combined_lower for marker in DO_NOT_SCHEDULE_MARKERS):
                skipped_do_not_schedule.append(summary or "(untitled event)")
                continue

            # Track (rather than silently drop) patients with no address on
            # file, so nobody just disappears without anyone noticing.
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


# Cache of already-fetched calendar schedules for this run, keyed by
# (calendar_id, start_date, end_date) — avoids re-fetching the same
# calendar's events once per patient when batching multiple patients.
_schedule_cache: dict[tuple[str, dt.date, dt.date], tuple[dict[dt.date, list[Stop]], list[ScheduleConflict]]] = {}


def get_days_schedule(service, calendar_id: str, start_date: dt.date, end_date: dt.date) -> tuple[dict[dt.date, list[Stop]], list[ScheduleConflict]]:
    """Returns (schedule, conflicts). Conflicts are pairs of events already
    on the calendar that overlap each other — flagged instead of silently
    treated as normal, since overlapping data makes recommendations
    unreliable around those times."""
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
                f"Couldn't read the existing schedule for one of the calendars. "
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
    def __init__(self, api_key: str, status_box):
        self.client = googlemaps.Client(key=api_key) if (api_key and googlemaps) else None
        self.cache: dict[tuple[str, str], float] = {}
        self.api_call_count = 0
        self.status_box = status_box

    def minutes_between(self, origin: str, destination: str, depart_at: dt.datetime) -> float:
        if origin == destination:
            return 0.0
        key = (origin, destination)
        if key in self.cache:
            return self.cache[key]

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

        self.cache[key] = minutes
        return minutes


def area_match(loc1: str, loc2: str) -> tuple[bool, str]:
    """Returns (matched, confidence). confidence is 'zip' (strong,
    matching 5-digit zip) or 'city' (weaker, matched only on a city-name
    substring) or 'none' if no match found."""
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


# Strength ordering for combining two confidence readings (weakest wins)
_CONFIDENCE_RANK = {"office": 3, "zip": 2, "city": 1, "none": 0}


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

            if prev_stop.location == OFFICE_ADDRESS:
                prev_conf = "office"
            else:
                matched, prev_conf = area_match(prev_stop.location, patient.location)
                if not matched:
                    continue

            if next_stop.location == OFFICE_ADDRESS:
                next_conf = "office"
            else:
                matched, next_conf = area_match(next_stop.location, patient.location)
                if not matched:
                    continue

            overall_confidence = min(prev_conf, next_conf, key=lambda c: _CONFIDENCE_RANK[c])

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
                    area_confidence=overall_confidence, notes=notes,
                ))

                if len(candidates) >= max_slots:
                    return candidates

        day += dt.timedelta(days=1)

    return candidates


def recs_to_csv(all_recs_list: list[tuple[str, SlotRecommendation]]) -> str:
    """Plain CSV export of every recommendation, for printing or working
    from outside the app."""
    buf = io.StringIO()
    writer = csv.writer(buf)
    writer.writerow([
        "Patient", "Employee", "Date", "Start Time", "End Time",
        "Drive There (min)", "Drive To Next Stop (min)", "Extra Drive Time Added (min)",
        "Notes",
    ])
    for cal_name, rec in all_recs_list:
        writer.writerow([
            clean_display(rec.patient.name, max_len=200),
            cal_name,
            rec.day.strftime("%A, %b %d, %Y"),
            rec.start.strftime("%I:%M %p").lstrip("0"),
            rec.end.strftime("%I:%M %p").lstrip("0"),
            f"{rec.drive_before_min:.0f}",
            f"{rec.drive_after_min:.0f}",
            f"{rec.detour_min:.0f}",
            " / ".join(rec.notes),
        ])
    return buf.getvalue()


# ============================= STREAMLIT UI =================================

st.title("📅 Smart Schedule Finder")
st.caption("Finds the best open time slot for patients who still need an appointment — based on your existing route and drive times.")

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

# Sidebar - Instructions for New Users
with st.sidebar.expander("📖 How This Works", expanded=False):
    st.markdown("""
    **What it does:** Looks at patients who still need an appointment, checks
    your existing schedule and real drive times, and suggests the best open
    time slot that doesn't add much extra driving to your day.

    **Steps:**
    1. Pick **which calendar** to search (Wade's, Dylan's, or both).
    2. Choose a **search type**:
       - *Specific Patient by Name* — look up one person by name (up to 31 days out).
       - *Batch Schedule* — automatically check the next 14 days for 5 people who still need an appointment.
    3. Check **"Skip today"** if you're working from home and don't want today included.
    4. Click **Run Scheduler**.

    **Good to know:**
    - Patients marked "do not schedule" (like out-of-area patients) are automatically left out.
    - Patients with no address on file are left out too, but you'll see who they are.
    - A note under a suggested time means: double-check that one before booking —
      it means the appointment length was a guess (no keyword like "hearing test"
      or "cleaning" was found in the notes).
    - Drive times are always calculated from real addresses via Google Maps — those numbers are accurate.
    """)

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
            estimator = DriveTimeEstimator(MAPS_API_KEY, status_box)

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

            all_recs_list = []
            all_conflicts: list[tuple[str, ScheduleConflict]] = []
            seen_conflicts = set()
            # patient_name -> {cal_name: best_rec}, for comparing employees
            per_patient_by_cal: dict[str, dict[str, SlotRecommendation]] = {}

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
                            all_conflicts.append((cal_name, c))

                    recs = find_slots_for_patient_on_calendar(
                        patient, cal_name, schedule, estimator, current_start_date, current_end_date, max_slots
                    )
                    for r in recs:
                        all_recs_list.append((cal_name, r))

                    if recs:
                        per_patient_by_cal.setdefault(patient.name, {})[cal_name] = recs[0]

            status_box.empty()
            st.success("Done!")

            # Surface schedule data problems in plain language, with raw
            # calendar text rendered safely (st.text, never st.markdown).
            if all_conflicts:
                with st.expander(f"⚠️ Heads up: {len(all_conflicts)} double-booked time slot(s) found on the calendar", expanded=True):
                    st.caption("These are two appointments already scheduled at the same time. "
                               "Suggestions near these times might not be reliable until this is fixed.")
                    for cal_name, c in all_conflicts:
                        st.markdown(f"**{cal_name} — {c.day:%A, %B %d}**")
                        col1, col2 = st.columns(2)
                        with col1:
                            st.caption(f"Starts {c.event1_start.strftime('%I:%M %p').lstrip('0')}")
                            st.text(clean_display(c.event1_summary, 90))
                        with col2:
                            st.caption(f"Overlaps — starts {c.event2_start.strftime('%I:%M %p').lstrip('0')}")
                            st.text(clean_display(c.event2_summary, 90))
                        st.divider()

            # Show what got excluded and why, in plain language.
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

            estimated_cost = (estimator.api_call_count / 1000) * DISTANCE_MATRIX_COST_PER_1000
            st.caption(
                f"Checked {estimator.api_call_count} driving routes this search "
                f"(roughly ${estimated_cost:.2f} in Google Maps usage, before any free monthly credit)."
            )

            if not all_recs_list:
                st.warning("No open time slots were found. Try a wider date range, or check the notes above for excluded patients.")
            else:
                sorted_recs = sorted(all_recs_list, key=lambda item: item[1].start)

                best_cal, best = sorted_recs[0]
                start_str = best.start.strftime("%I:%M %p").lstrip("0")
                end_str = best.end.strftime("%I:%M %p").lstrip("0")
                date_str = best.day.strftime("%A, %B %d")

                with st.container(border=True):
                    st.markdown("### ⭐ Best Option")
                    st.markdown(f"**Patient:** {clean_display(best.patient.name, 80)}")
                    st.markdown(f"**Employee:** {best_cal}")
                    st.markdown(f"**When:** {date_str}, {start_str} – {end_str}")
                    st.caption(f"Adds about {best.detour_min:.0f} extra minutes of driving to that day "
                               f"({best.drive_before_min:.0f} min drive there, {best.drive_after_min:.0f} min to the next stop)")
                    for note in best.notes:
                        st.warning(note, icon="⚠️")

                # Compare employees when both calendars were checked
                if selected_calendar == "Both Employees Combined" and len(target_cal_dict) > 1:
                    multi_tech_patients = {n: c for n, c in per_patient_by_cal.items() if len(c) > 1}
                    if multi_tech_patients:
                        st.markdown("### 👥 Which Employee Should Take This?")
                        for pname, cal_recs in multi_tech_patients.items():
                            ranked = sorted(cal_recs.items(), key=lambda kv: kv[1].detour_min)
                            best_tech, best_tech_rec = ranked[0]
                            with st.container(border=True):
                                st.markdown(f"**{clean_display(pname, 80)}**")
                                st.markdown(f"Better fit: **{best_tech}** — "
                                            f"{best_tech_rec.day:%a, %b %d} at "
                                            f"{best_tech_rec.start.strftime('%I:%M %p').lstrip('0')} "
                                            f"(+{best_tech_rec.detour_min:.0f} min extra driving)")
                                for cal_name, rec in ranked[1:]:
                                    st.caption(f"{cal_name} could also do {rec.day:%a, %b %d} at "
                                               f"{rec.start.strftime('%I:%M %p').lstrip('0')} "
                                               f"(+{rec.detour_min:.0f} min extra driving)")

                if len(sorted_recs) > 1:
                    st.markdown("### Other Times That Would Work")
                    for cal_name, rec in sorted_recs[1:]:
                        s_str = rec.start.strftime("%I:%M %p").lstrip("0")
                        e_str = rec.end.strftime("%I:%M %p").lstrip("0")
                        d_str = rec.day.strftime("%a, %b %d")
                        with st.container(border=True):
                            st.markdown(f"**{clean_display(rec.patient.name, 80)}** — {cal_name}")
                            st.markdown(f"{d_str}, {s_str} – {e_str}  ·  +{rec.detour_min:.0f} min extra driving")
                            for note in rec.notes:
                                st.caption(f"⚠️ {note}")

                st.markdown("---")
                csv_data = recs_to_csv(sorted_recs)
                st.download_button(
                    "⬇️ Download this list (for printing or texting to someone)",
                    data=csv_data,
                    file_name=f"appointment_suggestions_{dt.date.today():%Y-%m-%d}.csv",
                    mime="text/csv",
                )

        except Exception as e:
            st.error(f"Something went wrong: {e}")
