# ReliefMatch

A disaster-relief coordination web app that matches incoming help requests to the nearest available resources, built as an Anna University mini-project.

Field volunteers submit requests from a phone. Coordinators see them on a live dashboard, ranked by urgency, with suggested resource matches they can approve, reject, override or undo.

## How to run

Requires Python 3.10+.

```powershell
# 1. Install dependencies
pip install flask pytest

# 2. Set a session secret (the app will not start without it)
$env:SESSION_SECRET = "any-long-random-string"

# 3. Create and seed the database (first run only)
python seed.py

# 4. Start the app
python app.py
```

Open http://localhost:5000. To use it from a phone on the same Wi-Fi, open `http://<your-PC-IP>:5000` and allow port 5000 through the Windows firewall.

On macOS/Linux, set the secret with `export SESSION_SECRET=...` instead.

### Demo accounts

| Username | Role | Password |
|---|---|---|
| `coordinator` | Coordinator | `relief123` |
| `volunteer` | Volunteer | `relief123` |

These are demo credentials only. Change them before any real use.

### Run the tests

```powershell
python -m pytest
```

The suite has 37 tests covering the matching logic (`test_logic.py`) and the API (`test_api.py`).

## What is implemented

- **Login with roles:** coordinators manage requests; volunteers submit them.
- **Urgency scoring:** every request gets a score so the most critical appear first.
- **Nearest-resource matching:** requests are matched to the closest resources, and a request can be split across several resources when one is not enough.
- **Coordinator actions:** approve, reject, override, undo and mark delivered.
- **Audit log:** every action is recorded.
- **Rate limit:** 10 requests per minute.
- **Request expiry:** requests expire after 24 hours.
- **Duplicate detection:** a request within 200 m and 30 minutes of an existing one is flagged.
- **Location validation:** coordinates are checked against a Tamil Nadu bounding box in code.
- **Dashboard alerts:** banner, sound and speech alerts for new requests, escalation, and a stale flag for requests left unhandled for 60 minutes.

## Limitations

- **No offline queue.** Submitting a request needs a working connection. A planned design was to queue requests in the browser (IndexedDB) with a `client_uuid` so retries do not create duplicates.
- **No printable paper form** for areas with no connectivity.
- **No automatic backup.** The SQLite database is a single file and must be copied manually.
- **Demo-grade security.** Seeded passwords are shared and the app is intended to run on a local network, not the public internet.
- **Bounding box only.** Location checks use a rectangle around Tamil Nadu, so points just outside the state border but inside the rectangle are accepted.

## Future work

1. Offline queue using IndexedDB and `client_uuid` for idempotent sync.
2. Printable paper form for manual field reporting.
3. Scheduled automatic database backups.
4. Production deployment with a proper database and HTTPS.

## Project structure

| File | Purpose |
|---|---|
| `app.py` | Web app, routes, authentication, alerts |
| `logic.py` | Urgency scoring, matching, duplicate detection |
| `db.py` | Database setup and access |
| `seed.py` | Creates demo users and sample resources |
| `test_api.py`, `test_logic.py` | Automated tests |
| `templates/`, `static/` | Pages, scripts and styles |
