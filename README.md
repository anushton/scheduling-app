# Appointment Scheduling Finder

Finds the best slot for every unscheduled (yellow, all-day) patient on your
Google Calendar over the next 2 weeks — factoring in existing appointments,
drive time between stops, appointment-length rules (60 min hearing
tests/cleanings, 30 min services), and your 9:00 AM–5:30 PM office window.

**It's read-only.** It never touches your calendar — it writes a report
(`scheduling_recommendations.md`) that you review and then place the
appointments yourself.

## Setup (one time, ~10 minutes)

1. **Install Python packages:**
   ```
   pip install --break-system-packages -r requirements.txt
   ```

2. **Get Google Calendar API access:**
   - Go to https://console.cloud.google.com/apis/credentials
   - Create a project (any name) if you don't have one
   - Enable the "Google Calendar API"
   - Create credentials → OAuth Client ID → Application type: **Desktop app**
   - Download the JSON, rename it `credentials.json`, put it in this folder

3. **(Strongly recommended) Get a Google Maps API key for real drive times:**
   - Same Google Cloud project → enable "Distance Matrix API"
   - Create an API key, restrict it to Distance Matrix API
   - Set it as an environment variable before running:
     ```
     export GOOGLE_MAPS_API_KEY="your-key-here"
     ```
   - Without this, the script uses a flat 25-minute guess for every drive,
     which will be wrong for a lot of stops — good enough to test the
     script works, not good enough to actually schedule from.

4. **Check the CONFIG section at the top of `schedule_finder.py`** —
   office address, work hours, lookahead window, and duration keyword
   rules are all editable there.

## Running it

```
python3 schedule_finder.py
```

First run opens a browser window to log into the Google account that owns
the business calendar. After that, it's cached in `token.json` and runs
without prompting.

## Output

`scheduling_recommendations.md` — one section per unscheduled patient, with
the best slot (day/time), how much drive time it adds to that route day,
and a few backup options. Patients with no location on file are listed
separately at the bottom since there's nothing to route to.

## Notes / things to sanity-check

- Duration rules are simple keyword matching on the event title/description
  ("hearing test", "cleaning" → 60 min; "earwax", "service" → 30 min).
  If a patient's event doesn't clearly say which, it defaults to 30 min —
  worth a glance before booking.
- The "detour" ranking picks the slot that adds the *least* extra driving to
  an existing route day. It doesn't know about lunch breaks, personal
  preferences, or which patients you'd rather see sooner — it's a starting
  recommendation, not the final word.
- This only reads events with colorId `5` (yellow/Banana) that are marked
  all-day — that matches how your backlog is currently color-coded. If that
  convention changes, update `UNSCHEDULED_COLOR_ID` in the config.
- Re-run it whenever you want a fresh pass (e.g., every morning) — it always
  looks at the live calendar state.

## Once calendar write-access is sorted out

Right now booking still has to be done by hand because the calendar
connector didn't have write approval when we tried it. Once that's fixed,
this script can be extended to create the event directly (with your
confirmation) instead of just reporting it — happy to add that when you're
ready.
