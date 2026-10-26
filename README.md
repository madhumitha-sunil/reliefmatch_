# ReliefMatch: Urgency-Aware Disaster Aid Matching with Human Approval

A disaster-aid coordination web app (water, food, medicine, shelter) built as an Anna University mini-project.

**Team:** Madhumitha S (lead), Mahalakshmi S, Kannika M

**The system suggests; the coordinator decides.** Field volunteers submit aid requests from a phone. The app scores each request by urgency, suggests the nearest resources, and a human coordinator approves, rejects, overrides or undoes every match.

**Live demo:** https://madhumithasunil.pythonanywhere.com (free hosted demo, see [Limitations](#limitations))

## Key features

### Matching and decisions

* **Urgency score:** `severity x min(people, 1000) x wait\_factor x type\_weight`, where `wait\_factor = (1 + min(wait\_hours, 24)) / 6`. Type weights: medicine 3.0, water 2.0, food 1.5, shelter 1.0. Scores are computed on demand, not stored.
* **Nearest-resource matching:** straight-line (haversine) distance, nearest resource with enough stock first. A request can be split across several resources, and any shortfall is reported.
* **Human approval:** suggestions are read-only. Status flow is Pending, Matched, Delivered. The coordinator can approve (stock is rechecked inside one transaction), reject, override, undo (restores stock) and mark delivered.

### Works with a weak or missing connection

* **Offline request queue:** a volunteer's request is saved on the phone first (IndexedDB) and sent when the connection returns. Each request carries a unique `client\_uuid`, so if a reply is lost and the phone re-sends, the server answers "already received" and never creates a duplicate. The most urgent queued requests are sent first.
* **Installable app (PWA):** manifest, icons and a service worker. On HTTPS, the app can be added to the phone's home screen and the request page opens with no connection. Live data (`/api/...`) and submissions are never cached.
* **Automatic backup:** the database is copied every 10 minutes using SQLite's online backup (safe in WAL mode), verified, and the newest 30 copies are kept.

### Safety and control

* Login with roles (coordinator, volunteer), hashed passwords, audit log of every action.
* Rate limit: 10 requests per minute per user. Requests expire after 24 hours.
* Duplicate detection: a request within 200 m and 30 minutes of an existing one is flagged.
* Location check: coordinates must fall in a Tamil Nadu bounding box (latitude 8 to 13.6, longitude 76 to 80.4), in code and in the database schema.
* Dashboard: critical-request banner, sound and speech alerts, escalation, a stale flag after 60 minutes, and a Leaflet map (Leaflet is stored locally).

## How to run (Windows PowerShell)

Requires Python 3.10+.

```powershell
git clone https://github.com/madhumitha-sunil/reliefmatch\_.git

cd reliefmatch\_
python -m venv venv
venv\\Scripts\\activate
pip install flask pytest

# The app refuses to start without a session secret
$env:SESSION\_SECRET = "any-long-random-string"

# First run only: create and seed the demo database
python seed.py

python app.py
```

Open http://localhost:5000. On macOS or Linux, set the secret with `export SESSION\_SECRET=...`.

To reset the demo data: stop the app, delete `reliefmatch.sqlite3\*`, then run `python seed.py` again.

### Demo accounts

|Username|Role|Password|
|-|-|-|
|`coordinator`|Coordinator|`relief123`|
|`volunteer`|Volunteer|`relief123`|

These are shared demo credentials. Change them before any real use.

### Using it from a phone on a local network

The server, database and Leaflet all run on the laptop, so a local network is enough and no internet is needed.

1. Put the laptop and phone on the same Wi-Fi or phone hotspot. For a true no-internet demo, use a hotspot with mobile data off, or a router with no internet. A Windows Mobile Hotspot often will not start without a connection to share.
2. Allow inbound TCP port 5000 through the Windows firewall.
3. Find the laptop address with `ipconfig` and open `http://<laptop-ip>:5000` on the phone.

The laptop address changes with each network, so check it again at the venue. On a plain `http://` address the browser does not run service workers, so the page cannot be saved for offline opening, but the offline request queue still works.

### Backup and restore

```powershell
python backup.py                # make a backup now
python backup.py list           # list saved backups, newest first
python backup.py restore NAME   # restore one (stop the app first)
```

Copies go to the `backups/` folder. Restoring first saves the current database as `before-restore-...`, so a mistaken restore can be undone. Settings: `RELIEFMATCH\_BACKUP\_MINUTES` (default 10, `0` turns it off) and `RELIEFMATCH\_BACKUP\_KEEP` (default 30).

### Run the tests

```powershell
python -m pytest
```

61 automated tests cover the matching logic, the API, the duplicate-safe endpoint and database migration, backup and restore, and the manifest and service worker. The offline queue and service worker were also tested against the running server, and checked by hand on a phone.

## Limitations

* **Straight-line distance.** Matching ignores roads, flooding and blocked routes.
* **Simulated data.** The demo flood requests and resources are generated by `seed.py`, not from a real event.
* **One server device.** The app runs on a single laptop or hosted account. If it is switched off, nobody can sync.
* **Development server, not load-tested.** It runs on the Flask development server and has not been tested with many simultaneous users.
* **Queue needs a signed-in session.** A volunteer must be signed in with the request page already loaded before the connection drops. If the session has expired, queued requests wait until they sign in again. The server limit of 10 requests per minute means a very large backlog is sent in batches. Requests the server will never accept (invalid, or a nearby duplicate) are dropped from the queue with a message.
* **Offline maps.** Map tiles load from OpenStreetMap, so with no connection the map background is blank, though pins and lines still draw. Offline tile packs for downloaded regions are not built.
* **Installable app needs HTTPS.** Service workers run only on HTTPS or localhost, so installing and opening the page with no connection works on the hosted link, not on a plain local-network address.
* **Hosted demo is not production.** The free PythonAnywhere account has a daily CPU allowance, expires unless renewed, and has no scheduled tasks, so the hosted copy does not back itself up. Automatic backup runs where `python app.py` is started.
* **Demo-grade security.** Seeded passwords are shared and public.
* **Bounding box only.** Location checks use a rectangle around Tamil Nadu, so points just outside the border but inside the rectangle are accepted.
* **Phone access depends on the network.** Hotspot behaviour, firewall settings and changing IP addresses can block the connection, so test at the venue first.

## Future work

1. Road-network distance and live flood or road-closure data.
2. Offline map tiles for chosen regions.
3. A production database (for example PostgreSQL), a production web server and HTTPS on the coordinator's own server.
4. Multiple coordinators with conflict handling, and sync across several field devices.
5. A printable paper form for areas with no phones at all.
6. Load testing, and individual accounts with password change and reset.

## Project structure

|File or folder|Purpose|
|-|-|
|`app.py`|Web app, routes, authentication, alerts, service worker and manifest routes|
|`logic.py`|Validation, urgency scoring, distance, matching|
|`db.py`|Database schema, safe migration (adds `client\_uuid`), connections|
|`seed.py`|Creates demo users and sample resources|
|`backup.py`|Automatic backup, rotation, restore|
|`templates/`|Pages (login, request, dashboard)|
|`static/js/queue.js`|Offline queue (IndexedDB) and safe re-send logic|
|`static/js/request.js`|Request form and queue status bar|
|`static/sw.js`|Service worker (caching rules for the installable app)|
|`static/manifest.webmanifest`, `static/icons/`|App name, icons and install settings|
|`static/css/`, `static/vendor/leaflet/`|Styles and the locally stored map library|
|`test\_api.py`, `test\_logic.py`, `test\_backup.py`|Automated tests|



