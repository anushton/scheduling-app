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
            st.warning(f"Authorization required. Please visit this URL: {auth_url}")
            code = st.text_input("Enter the authorization code:")
            if code:
                try:
                    flow.fetch_token(code=code)
                    creds = flow.credentials
                except Exception as e:
                    st.error(
                        "Couldn't exchange that code for a token "
                        f"({e}). Note: Google has been phasing out the "
                        "copy/paste 'out-of-band' code flow for OAuth clients "
                        "created after Feb 2022 — if this keeps failing, the "
                        "fix is to generate a token.json once locally (e.g. "
                        "using InstalledAppFlow.run_local_server() in a "
                        "one-off script on your own machine) and paste its "
                        "contents into the GOOGLE_TOKEN secret instead."
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
    except Exception as e:
        st.warning(f"Couldn't load your calendar list ({e}). Using default calendars instead — "
                   "if these aren't right, check that the account has calendar access.")

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
            except Exception as e2:
                st.error(
                    f"Couldn't fetch unscheduled patients from either "
                    f"'{target_cal_id}' or 'primary' ({e2}). Results below "
                    "will be incomplete or empty — this isn't a 'no patients "
                    "found' result, it's a failed fetch."
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

            # Improvement #1: auto-skip patients explicitly marked as
            # not schedulable (e.g. outside service area).
            if any(marker in combined_lower for marker in DO_NOT_SCHEDULE_MARKERS):
                skipped_do_not_schedule.append(summary or "(untitled event)")
                continue

            # Improvement #2: track (rather than silently drop) patients
            # with no address on file, so nobody vanishes without a trace.
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
_schedule_cache: dict[tuple[str, dt.date, dt.date], tuple[dict[dt.date, list[Stop]], list[str]]] = {}


def get_days_schedule(service, calendar_id: str, start_date: dt.date, end_date: dt.date) -> tuple[dict[dt.date, list[Stop]], list[str]]:
    """Returns (schedule, conflict_warnings). conflict_warnings lists any
    overlapping events found on the calendar (Improvement #3) so bad source
    data gets flagged instead of silently producing bad recommendations."""
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
        except Exception as e:
            st.error(
                f"Couldn't fetch the existing schedule for calendar "
                f"'{calendar_id}' ({e}). Any recommendation below for this "
                "calendar may be treating busy days as open — verify before "
                "booking."
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

    conflicts = []
    for day in schedule:
        schedule[day].sort(key=lambda st: st.start)
        stops = schedule[day]
        for i in range(len(stops) - 1):
            if stops[i + 1].start < stops[i].end:
                conflicts.append(
                    f"{day:%a %b %d}: \"{stops[i].summary}\" "
                    f"({stops[i].start:%I:%M %p}-{stops[i].end:%I:%M %p}) overlaps "
                    f"\"{stops[i+1].summary}\" (starts {stops[i+1].start:%I:%M %p})"
                )

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
                self.status_box.text(f"Checking traffic route #{self.api_call_count}...")
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
                    notes.append("⚠ duration assumed (no keyword match)")
                if overall_confidence == "city":
                    notes.append("⚠ approximate area match (text-based, not geocoded)")

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
    """Improvement #8: plain CSV export of every recommendation, for
    printing or working from outside the app."""
    buf = io.StringIO()
    writer = csv.writer(buf)
    writer.writerow([
        "Patient", "Calendar", "Date", "Start", "End",
        "Drive Before (min)", "Drive After (min)", "Detour Added (min)",
        "Area Match Confidence", "Notes",
    ])
    for cal_name, rec in all_recs_list:
        writer.writerow([
            rec.patient.name,
            cal_name,
            rec.day.strftime("%A, %b %d, %Y"),
            rec.start.strftime("%I:%M %p").lstrip("0"),
            rec.end.strftime("%I:%M %p").lstrip("0"),
            f"{rec.drive_before_min:.0f}",
            f"{rec.drive_after_min:.0f}",
            f"{rec.detour_min:.0f}",
            rec.area_confidence,
            "; ".join(rec.notes),
        ])
    return buf.getvalue()


# ============================= STREAMLIT UI =================================

st.title("Smart Schedule Finder")
st.markdown("*Google Calendar Route Optimizer (Mobile & Web App)*")

with st.spinner("Connecting to calendars..."):
    try:
        service = get_calendar_service()
        calendar_map = get_user_calendars(service)
    except Exception as e:
        st.warning(f"Couldn't connect to Google Calendar ({e}). Falling back to default calendar list — "
                   "the app may not be able to fetch real data until this is resolved.")
        calendar_map = {
            "Wade Hendrickson": "w1a9d7e4@gmail.com",
            "Dylan Hendrickson-Work Schedule": "primary"
        }

# Sidebar - Instructions for New Users
with st.sidebar.expander("📖 How to Use & What It Does", expanded=False):
    st.markdown("""
    **What this app does:**
    This tool optimizes travel schedules by automatically scanning your Google Calendars and checking live traffic data (via Google Maps) to find the most efficient appointment slots for patients, minimizing unnecessary drive time and detours.

    **How to use it:**
    1. **Target Calendar:** Select the calendar you want to scan (or combine both).
    2. **Search Mode:**
       * *Specific Patient by Name:* Type a patient's name to search for open slots up to 31 days out. You can also supply an optional override address.
       * *Batch Schedule:* Automatically scans the next 14 days for your top unscheduled patients.
    3. **Skip Today:** Check this box if working from home today to push route calculations starting tomorrow.
    4. **Run Scheduler:** Click the button to calculate optimal driving routes and view recommended appointment windows instantly.

    **Notes:**
    - Patients whose event is tagged "do not schedule" / "outside service area" are automatically excluded.
    - Patients with no address on file are excluded but listed separately so nothing gets lost.
    - ⚠ tags on a recommendation mean the appointment duration was assumed (no keyword match) or the area match was based on text, not a real geocoded distance — double check those before booking.
    """)

st.sidebar.header("Search Configuration")
cal_options = list(calendar_map.keys())
if len(cal_options) > 1:
    cal_options.insert(0, "Both Calendars Combined")
selected_calendar = st.sidebar.selectbox("Target Calendar", cal_options)

mode = st.sidebar.radio("Search Mode", ["Specific Patient by Name", "Batch Schedule (5 Patients)"])
target_name = ""
override_address = ""

if mode == "Specific Patient by Name":
    target_name = st.sidebar.text_input("Patient Name").strip().lower()
    override_address = st.sidebar.text_input("Override Address (Optional)").strip()

skip_today = st.sidebar.checkbox("Skip today (e.g., working from home)")

if st.sidebar.button("Run Scheduler", type="primary"):
    if mode == "Specific Patient by Name" and not target_name:
        st.error("Please enter a patient name.")
    else:
        status_box = st.empty()
        status_box.text("Initializing scheduler...")

        target_cal_dict = calendar_map if selected_calendar == "Both Calendars Combined" else {selected_calendar: calendar_map[selected_calendar]}
        max_slots = 3 if mode == "Specific Patient by Name" else 1

        try:
            service = get_calendar_service()
            estimator = DriveTimeEstimator(MAPS_API_KEY, status_box)

            today = dt.date.today()
            base_start_date = today + dt.timedelta(days=1) if skip_today else today

            status_box.text("Fetching unscheduled patients...")
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
                        st.error(f"Could not find patient '{target_name}' and no Override Address was provided.")
                        st.stop()
                else:
                    patients = filtered[:1]
            else:
                patients = patients[:5]

            all_recs_list = []
            all_conflicts = []
            # patient_name -> {cal_name: best_rec}, for the technician comparison (Improvement #4)
            per_patient_by_cal: dict[str, dict[str, SlotRecommendation]] = {}

            for patient in patients:
                for cal_name, cal_id in target_cal_dict.items():
                    status_box.text(f"Checking calendar: {cal_name} for {patient.name}...")

                    if mode == "Specific Patient by Name":
                        current_start_date = base_start_date
                        current_end_date = current_start_date + dt.timedelta(days=31)
                    else:
                        current_start_date = base_start_date
                        current_end_date = base_start_date + dt.timedelta(days=14)

                    schedule, conflicts = get_days_schedule(service, cal_id, current_start_date, current_end_date)
                    for c in conflicts:
                        tagged = f"[{cal_name}] {c}"
                        if tagged not in all_conflicts:
                            all_conflicts.append(tagged)

                    recs = find_slots_for_patient_on_calendar(
                        patient, cal_name, schedule, estimator, current_start_date, current_end_date, max_slots
                    )
                    for r in recs:
                        all_recs_list.append((cal_name, r))

                    if recs:
                        per_patient_by_cal.setdefault(patient.name, {})[cal_name] = recs[0]

            status_box.empty()
            st.success("Run Completed Successfully!")

            # Improvement #3: surface schedule data problems
            if all_conflicts:
                with st.expander(f"⚠️ {len(all_conflicts)} scheduling conflict(s) found in your existing calendar", expanded=True):
                    st.caption("These are overlapping events already on the calendar — recommendations around "
                               "these days may be unreliable until the conflict is resolved.")
                    for c in all_conflicts:
                        st.markdown(f"- {c}")

            # Improvement #1 & #2: show what got excluded and why
            if skipped_dns or skipped_no_address:
                with st.expander(f"ℹ️ {len(skipped_dns) + len(skipped_no_address)} patient(s) excluded from this run", expanded=False):
                    if skipped_dns:
                        st.markdown("**Marked do-not-schedule / outside service area:**")
                        for name in skipped_dns:
                            st.markdown(f"- {name}")
                    if skipped_no_address:
                        st.markdown("**No address on file (can't route to them):**")
                        for name in skipped_no_address:
                            st.markdown(f"- {name}")

            st.markdown("---")
            st.subheader("📋 Scheduling Recommendations")

            # Improvement #6: realistic cost estimate instead of a threshold
            # that never triggers. Still just an estimate — actual billing
            # depends on your Google Cloud account's monthly free credit.
            estimated_cost = (estimator.api_call_count / 1000) * DISTANCE_MATRIX_COST_PER_1000
            st.info(
                f"**API Usage:** {estimator.api_call_count} Distance Matrix requests made | "
                f"Approx. cost at published rates: ${estimated_cost:.2f} "
                f"(before any monthly free credit — not an official billing figure)"
            )

            if not all_recs_list:
                st.warning("No available slots were found across the selected calendars.")
            else:
                sorted_recs = sorted(all_recs_list, key=lambda item: item[1].start)

                best_cal, best = sorted_recs[0]
                start_str = best.start.strftime("%I:%M %p").lstrip("0")
                end_str = best.end.strftime("%I:%M %p").lstrip("0")
                date_str = best.day.strftime("%A, %B %d, %Y")

                st.markdown("### ⭐ Overall Earliest Recommendation")
                st.markdown(f"**Patient:** {best.patient.name}")
                st.markdown(f"**Calendar:** {best_cal}")
                st.markdown(f"**Optimal Slot:** `{date_str} — {start_str} to {end_str}`")
                st.markdown(f"*Route:* Drive there {best.drive_before_min:.0f} min | Drive next {best.drive_after_min:.0f} min | Detour +{best.detour_min:.0f} min")
                for note in best.notes:
                    st.caption(note)

                # Improvement #4: technician comparison when combining calendars
                if selected_calendar == "Both Calendars Combined" and len(target_cal_dict) > 1:
                    multi_tech_patients = {n: c for n, c in per_patient_by_cal.items() if len(c) > 1}
                    if multi_tech_patients:
                        st.markdown("### 👥 Technician Comparison")
                        st.caption("For patients where both techs have an available slot, here's how they compare.")
                        for pname, cal_recs in multi_tech_patients.items():
                            ranked = sorted(cal_recs.items(), key=lambda kv: kv[1].detour_min)
                            best_tech, best_tech_rec = ranked[0]
                            st.markdown(f"**{pname}** — recommended: **{best_tech}** "
                                        f"({best_tech_rec.day:%a %b %d}, "
                                        f"{best_tech_rec.start.strftime('%I:%M %p').lstrip('0')}, "
                                        f"+{best_tech_rec.detour_min:.0f} min detour)")
                            for cal_name, rec in ranked[1:]:
                                st.caption(f"  vs. {cal_name}: {rec.day:%a %b %d}, "
                                           f"{rec.start.strftime('%I:%M %p').lstrip('0')}, "
                                           f"+{rec.detour_min:.0f} min detour")

                if len(sorted_recs) > 1:
                    st.markdown("### Other Available Options")
                    for cal_name, rec in sorted_recs[1:]:
                        s_str = rec.start.strftime("%I:%M %p").lstrip("0")
                        e_str = rec.end.strftime("%I:%M %p").lstrip("0")
                        d_str = rec.day.strftime("%A, %b %d")
                        note_str = f" — {'; '.join(rec.notes)}" if rec.notes else ""
                        st.markdown(f"- **[{cal_name}]** {rec.patient.name}: `{d_str}, {s_str}–{e_str}` (+{rec.detour_min:.0f} min detour){note_str}")

                # Improvement #8: export/print view
                st.markdown("---")
                csv_data = recs_to_csv(sorted_recs)
                st.download_button(
                    "⬇️ Download recommendations as CSV",
                    data=csv_data,
                    file_name=f"scheduling_recommendations_{dt.date.today():%Y-%m-%d}.csv",
                    mime="text/csv",
                )

        except Exception as e:
            st.error(f"An error occurred during execution: {e}")
