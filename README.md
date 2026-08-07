# 📅 Smart Schedule Finder

A Streamlit-based employee scheduling tool that finds the best appointment times for patients who still need to be scheduled.

The app connects to **Google Calendar** to read existing employee schedules and **Google Maps** to calculate realistic drive times. It then recommends open appointment slots that fit within working hours while minimizing unnecessary driving.

---

## ✨ Features

- 🔐 **Employee password authentication**
- 📅 **Google Calendar integration**
  - Reads existing employee schedules
  - Supports multiple employee calendars
  - Detects overlapping/double-booked appointments
- 🚗 **Google Maps driving-time calculations**
  - Uses real driving times and traffic estimates
  - Enforces a maximum 30-minute drive between stops
  - Caches previous drive-time lookups to reduce API usage
- 👤 **Individual patient searches**
  - Search for a specific patient
  - Optionally provide an address for patients not yet on the calendar
  - Override the automatically estimated appointment length
- 👥 **Batch scheduling**
  - Finds the next patients waiting to be scheduled
  - Can skip patients checked within the previous 7 days
- ⏰ **Smart appointment recommendations**
  - Finds available gaps in existing schedules
  - Considers appointment duration
  - Considers travel time before and after an appointment
  - Shows the earliest available appointment
  - Shows the appointment with the least additional driving
- 🗺️ **Google Maps route links**
  - Open the complete suggested route directly in Google Maps
- ⚙️ **Configurable business rules**
  - Working hours
  - Appointment duration rules
  - Default appointment length
  - Do-not-schedule keywords
- 📜 **Search history**
  - Saves previous scheduling searches locally
  - Revisit previous searches without making new Maps API calls
  - Stores up to 100 searches
- 📥 **CSV export**
  - Download appointment recommendations for printing or sharing
- 💾 **SQLite persistence**
  - Stores search history
  - Stores settings
  - Stores cached drive times
  - Tracks recently checked patients

---

## 🧠 How It Works

The scheduler follows roughly this process:

```text
Google Calendar
      │
      ▼
Find unscheduled patients
      │
      ├── Remove "do not schedule" patients
      ├── Remove patients without addresses
      └── Determine appointment duration
      │
      ▼
Read employee schedules
      │
      ▼
Find available gaps
      │
      ▼
Calculate driving times
      │
      ├── Previous appointment → Patient
      ├── Patient → Next appointment
      └── Existing route without patient
      │
      ▼
Apply scheduling rules
      │
      ├── Working hours
      ├── Appointment duration
      └── Maximum 30-minute drive between stops
      │
      ▼
Rank valid appointments
      │
      ├── Earliest available
      └── Least additional driving
      │
      ▼
Display recommendations
```

The scheduler uses the existing route as a baseline and calculates how much additional driving would be introduced by adding a patient to that route.

---

## 👤 Search Modes

### One Specific Patient

Search for a particular patient by name.

The app searches the unscheduled patient list and can look up appointments up to **31 days** into the future.

You can also:

- Enter an address if the patient isn't on the calendar yet
- Manually specify the appointment length
- Skip the current day

### Batch — Next 5 Patients

The batch mode automatically selects up to **5 patients who have been waiting the longest**.

By default, the app can skip patients who were already checked within the previous **7 days**.

Batch searches look up available appointments over the next **14 days**.

---

## 🚗 Drive-Time Logic

Drive times are calculated using the Google Maps Distance Matrix API.

The application:

1. Checks the local SQLite cache first.
2. Uses cached results when they are less than **6 hours old**.
3. Batches Google Maps requests when possible.
4. Uses traffic-aware travel times when available.
5. Saves new results to the database for future searches.

The scheduler also calculates the **detour** caused by adding a patient:

```text
Detour =
    Drive to Patient
  + Drive from Patient
  - Original Drive Between Stops
```

This allows the application to distinguish between a slot that is merely available and a slot that makes operational sense.

---

## ⚙️ Business Rules

Business rules can be changed directly from the **Settings** tab without modifying the code.

### Working Hours

Default schedule:

| Day | Working |
|---|---|
| Monday | 9:00 AM – 5:30 PM |
| Tuesday | 9:00 AM – 5:30 PM |
| Wednesday | 9:00 AM – 5:30 PM |
| Thursday | 9:00 AM – 5:30 PM |
| Friday | 9:00 AM – 5:30 PM |
| Saturday | Off |
| Sunday | Off |

### Appointment Duration

The application estimates appointment length based on keywords in the patient's information.

Default rules include:

| Keyword | Duration |
|---|---:|
| `hearing test` | 60 min |
| `test appt` | 60 min |
| `cleaning` | 60 min |
| `earwax` | 30 min |
| `service` | 30 min |

If no keyword matches, the default appointment length is **60 minutes**.

These rules can be edited from the Settings tab.

### Do-Not-Schedule Keywords

Patients are automatically excluded when their notes contain configured phrases such as:

- `do not schedule`
- `don't schedule`
- `outside service area`
- `out of area`

These keywords can also be customized.

---

## 🔐 Authentication & Security

The application includes a simple employee password portal.

The password is stored through **Streamlit secrets** rather than directly in the source code.

Google credentials, the Google Maps API key, and the office address are also loaded from Streamlit secrets.

**Do not commit your `secrets.toml` or other credential files to GitHub.**

---

## 🛠️ Tech Stack

- **Python**
- **Streamlit** — web application interface
- **Google Calendar API** — calendar and appointment data
- **Google Maps Distance Matrix API** — driving-time calculations
- **SQLite** — persistent local storage
- **Pandas** — settings/data editing
- **Google OAuth 2.0** — Google authentication

---

## 📦 Installation

### 1. Clone the repository

```bash
git clone https://github.com/YOUR_USERNAME/YOUR_REPOSITORY.git
cd YOUR_REPOSITORY
```

### 2. Create a virtual environment

```bash
python -m venv .venv
```

Activate it:

**Windows**

```bash
.venv\Scripts\activate
```

**macOS/Linux**

```bash
source .venv/bin/activate
```

### 3. Install dependencies

Create a `requirements.txt` containing:

```txt
streamlit
pandas
googlemaps
google-auth
google-auth-oauthlib
google-api-python-client
```

Then run:

```bash
pip install -r requirements.txt
```

### 4. Configure Streamlit secrets

Create:

```text
.streamlit/
└── secrets.toml
```

The application expects the following configuration:

```toml
APP_PASSWORD = "your_employee_password"
MAPS_API_KEY = "your_google_maps_api_key"
OFFICE_ADDRESS = "your_office_address"

[GOOGLE_CREDENTIALS.installed]
client_id = "your_google_client_id"
client_secret = "your_google_client_secret"
auth_uri = "https://accounts.google.com/o/oauth2/auth"
token_uri = "https://oauth2.googleapis.com/token"
```

After completing Google OAuth authentication, the application can generate a `GOOGLE_TOKEN` block that can be saved to your Streamlit secrets.

**Never commit this file to GitHub.**

Add it to `.gitignore`:

```gitignore
.streamlit/secrets.toml
*.db
__pycache__/
.venv/
```

---

## ▶️ Running the App

Start Streamlit with:

```bash
streamlit run app.py
```

Then open the local URL provided by Streamlit.

---

## 🔑 Google API Setup

The application requires access to:

### Google Calendar API

Used to:

- Read employee calendars
- Find existing appointments
- Find unscheduled patients
- Identify scheduling conflicts

The application requests the read-only Calendar scope:

```text
https://www.googleapis.com/auth/calendar.readonly
```

### Google Maps API

Used to calculate driving times between:

- The office
- Existing appointments
- Potential patient appointments

The application also tracks Google Maps API calls and displays an estimated usage cost after each search.

---

## 💾 Local Database

The application automatically creates:

```text
schedule_history.db
```

The SQLite database contains several tables:

### `runs`

Stores previous scheduler searches and their results.

### `drive_time_cache`

Stores previously calculated routes and their timestamps.

### `settings`

Stores configurable business rules.

### `patient_lookups`

Tracks when patients were recently checked.

The database is created automatically when the application starts.

---

## 📊 Results

For each valid recommendation, the app displays:

- Patient
- Employee
- Date
- Appointment time
- Drive time before the appointment
- Drive time after the appointment
- Additional driving caused by the appointment
- Scheduling notes
- Full Google Maps route

The application highlights two useful choices:

### ⏰ Earliest Available

The earliest appointment that satisfies the scheduling rules.

### 🚗 Least Extra Driving

The appointment that adds the least additional driving to the employee's route.

This gives employees flexibility between **getting someone scheduled sooner** and **keeping routes efficient**.

---

## ⚠️ Scheduling Limitations

The recommendations are designed to assist employees rather than automatically book appointments.

The application does **not** automatically modify Google Calendar appointments.

Employees should verify the recommended appointment before booking it.

Potential issues are surfaced in the interface, including:

- Existing calendar conflicts
- Patients without addresses
- Patients marked as do-not-schedule
- Appointments whose duration had to be estimated
- Patients for whom no valid slot could be found

---

## 📁 Suggested Repository Structure

```text
smart-schedule-finder/
│
├── app.py
├── requirements.txt
├── README.md
├── .gitignore
│
└── .streamlit/
    └── secrets.toml    # Not committed
```

The SQLite database is generated automatically at runtime.

---

## 🚀 Deployment

This application can be deployed to a Streamlit-compatible hosting environment.

When deploying:

1. Add the repository to your hosting provider.
2. Configure the required Streamlit secrets.
3. Make sure the Google APIs are enabled.
4. Add the required Google OAuth credentials.
5. Start the application with:

```bash
streamlit run app.py
```

For production use, make sure API keys and OAuth credentials are stored as secure environment/secrets values rather than in the repository.

---

## 📌 Project Status

This project is designed as an internal scheduling tool for employees.

It focuses on making appointment scheduling faster by combining:

**calendar availability + appointment duration + working hours + real driving time + route efficiency.**

---

## 📄 License

Copyright © 2026 Ashton Hendrickson. All rights reserved.

This project is proprietary software. The source code may be viewed for reference, but may not be copied, modified, distributed, published, or used without prior written permission from the copyright holder.

See the [`LICENSE`](LICENSE) file for the complete license terms.



