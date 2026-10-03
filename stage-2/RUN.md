# Tablekeeper — stage 2

Build and start (from this folder):

```sh
docker build -t tablekeeper:stage-2 . && docker run --rm -e PORT=8080 -p 8080:8080 tablekeeper:stage-2
```

The service listens on `0.0.0.0:$PORT` (default `8080`) and answers
`GET /health` with `{"status":"ok"}` once it is ready (about 3 seconds). It needs no
network at run time: the Python dependencies and the IANA time-zone database
(`tzdata`, pinned) are installed during `docker build`; the browser UI is static HTML,
CSS and vanilla JS served from the same image, with no CDN or external font/icon
dependency.

Open `http://localhost:8080/` for the diner-facing site (`/signup`, `/login`, `/lookup`
are also routed there). The JSON API from stage 1 is unchanged in shape except where
noted below.

## Design notes

- Python 3.12, Starlette on uvicorn, a single worker process.
- All state is in memory and is replaced by `POST /_test/reset` or `POST /_test/import`.
  Every read-then-write runs under one `asyncio.Lock` and never awaits while holding it.
  This makes booking, idempotent replay, export and import atomic, so a table cannot be
  double-booked under concurrent requests. Combined-table bookings reuse the same lock:
  checking and reserving every table in a set happens inside one critical section, so no
  extra per-table locking was needed for the stage-2 combinable-tables work.
- Passwords are hashed with salted scrypt (n=2^12, r=8, p=1) on a small thread pool,
  so hashing never blocks the event loop.
- Local times resolve through `zoneinfo`. On a fall-back night the first occurrence
  is used, and a time inside a spring-forward gap does not exist.
- Occupancy is answered from a per-table index of confirmed bookings, kept sorted by
  start time. A combined-table booking occupies an entry under each of its tables;
  availability, create, PATCH and moves all check/update every table in a booking's set
  together, so cost does not grow with the number of stored reservations.
- A reset fixture may carry fields that stage 1 does not define. On a seeded
  reservation, `status` is honoured only when it is exactly `"cancelled"`, and
  `created_at` only when it is a valid RFC 3339 timestamp. Any other value is ignored
  and the defaults apply (`confirmed`, the reset time). A seeded cancelled reservation
  does not occupy its table(s). Import stays strict.
- `POST /reservation-moves` checks in this order:
  1. Batch shape (422).
  2. Every reference exists and belongs to the caller (404).
  3. All bookings are at one restaurant (422).
  4. Per booking, in input order: cancelled (409), cutoff (409), field and placement
     rules (including combination_not_allowed/party_exceeds_capacity for that booking's
     requested table set).
  5. Occupancy of the whole result (409 `table_unavailable`), checking every table any
     listed booking would hold against every other table in play.

  Steps 2 and 3 are whole-batch checks. They come before any per-booking cutoff or
  cancelled error.

### Combinable tables (stage 2)

- A restaurant's `combinable` field is a list of unordered table-id pairs; a pair not
  listed can never be booked together, and combining is not transitive.
- `table_id` (single) and `table_ids` (array of 1 or 2) are both accepted on
  `POST /reservations`, `PATCH /reservations/{reference}` and each item of
  `POST /reservation-moves`; sending both is `422 validation_failed`. A valid pair is
  normalised to its declared `combinable` order in storage and in every response.
  Responses always carry `table_ids`; `table_id` appears only when the set has exactly
  one member.
- `GET /availability` slots gain `available_options` (every single table, then every
  declared pair meeting capacity and with no overlapping booking on any member table,
  singles in fixture order then pairs in `combinable` order). `available_table_ids` is
  unchanged — single tables only.
- Export emits `table_ids`; import accepts a reservation with either `table_id` (the
  stage-1 shape) or `table_ids`, and a restaurant with no `combinable` key at all is
  treated as having no declared pairs — this is what lets a stage-2 instance accept an
  export produced by a stage-1 instance.

### Browser UI (stage 2)

- `app/web/pages/*.html` are static page shells; `app/web/static/app.js` does all
  rendering and API calls client-side (fetch to the same-origin JSON API), with
  `app/web/static/styles.css` for layout/visual states. Nothing is bundled from a CDN.
- Every element the spec marks "present only when ..." or "absent" (e.g. `current-user`,
  `auth-error`, `no-slots` vs. `availability-grid`, `booking-form`, `confirmation`,
  `reservation-detail`, `reservation-cancel-button`) is actually added to or removed from
  the DOM by `app.js`'s `presence()` helper, not merely CSS-hidden — a hidden-but-attached
  node still matches an automated test's selector wait.
- Each search request carries an increasing sequence number; a response is only
  rendered if it is still the latest request issued, so a late response from an
  earlier, superseded search can never overwrite a newer one's results (stage-2.md,
  "Competing clients").
- A booking attempt keeps its idempotency key and request body attached to the open
  booking form until a field changes. A lost response (network failure) shows
  `booking-uncertain` without touching the form; resubmitting unchanged retries with the
  same key and body, so it cannot create a duplicate booking and recovers the original
  reference once the server is reachable again. A confirmed `409 table_unavailable`
  shows `booking-error` and refreshes the grid without closing the form.

## Project tests

The tests in `tests/` run the app in-process (no browser; they exercise the JSON API
directly, including the combinable-table additions). They need Python 3.12:

```sh
python3 -m venv .venv && . .venv/bin/activate
pip install -r requirements.txt -r tests/requirements.txt
python -m pytest -q
```
