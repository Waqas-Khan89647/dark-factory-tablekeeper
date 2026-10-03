# Tablekeeper — stage 1

Build and start (from this folder):

```sh
docker build -t tablekeeper:stage-1 . && docker run --rm -e PORT=8080 -p 8080:8080 tablekeeper:stage-1
```

The service listens on `0.0.0.0:$PORT` (default `8080`) and answers
`GET /health` with `{"status":"ok"}` once it is ready (about 3 seconds). It needs no
network at run time: the Python dependencies and the IANA time-zone database
(`tzdata`, pinned) are installed during `docker build`.

## Design notes

- Python 3.12, Starlette on uvicorn, a single worker process.
- All state is in memory and is replaced by `POST /_test/reset` or `POST /_test/import`.
  Every read-then-write runs under one `asyncio.Lock` and never awaits while holding it.
  This makes booking, idempotent replay, export and import atomic, so a table cannot be
  double-booked under concurrent requests.
- Passwords are hashed with salted scrypt (n=2^12, r=8, p=1) on a small thread pool,
  so hashing never blocks the event loop.
- Local times resolve through `zoneinfo`. On a fall-back night the first occurrence
  is used, and a time inside a spring-forward gap does not exist.

- Occupancy is answered from a per-table index of confirmed bookings, kept sorted by
  start time. Availability, create, PATCH and moves all use it, so their cost does not
  grow with the number of stored reservations. Cancel, PATCH, moves, reset and import
  all update it through the same few `State` methods.
- A reset fixture may carry fields that stage 1 does not define. On a seeded
  reservation, `status` is honoured only when it is exactly `"cancelled"`, and
  `created_at` only when it is a valid RFC 3339 timestamp. Any other value is ignored
  and the defaults apply (`confirmed`, the reset time). A seeded cancelled reservation
  does not occupy its table. Import stays strict.
- `POST /reservation-moves` checks in this order:
  1. Batch shape (422).
  2. Every reference exists and belongs to the caller (404).
  3. All bookings are at one restaurant (422).
  4. Per booking, in input order: cancelled (409), cutoff (409), field and placement
     rules.
  5. Occupancy of the whole result (409 `table_unavailable`).

  Steps 2 and 3 are whole-batch checks. They come before any per-booking cutoff or
  cancelled error.

## Project tests

The tests in `tests/` run the app in-process. They need Python 3.12:

```sh
python3 -m venv .venv && . .venv/bin/activate
pip install -r requirements.txt -r tests/requirements.txt
python -m pytest -q
```
