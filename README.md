# CResTE: Dark Factory entry (track: tablekeeper)

**Team:** CResTE (Waqas Ullah)
**Track:** tablekeeper, a restaurant reservation system where one table is never double-booked
**Hackathon:** WeAreDevelopers x BAND "Dark Factory"

The application code in this repository was written by a small team of AI coding agents running in BAND Desktop. No human wrote any application code. The human's job was to build the factory (the agents and their mandates), send one task message per stage, and keep the machine running.

## How to read this repo

| Path | What it is |
|---|---|
| `FACTORY.md` | The factory: the three agent seats, how they hand work to each other, how bad work is caught, costs, and honest limitations |
| `mandates/` | One mandate file per agent seat (`planner.md`, `implementer.md`, `reviewer.md`). Each starts with `Harness: Claude Code` and `Model: claude-sonnet-5` |
| `room.json` | The BAND room export for the final run (message history of the three agents) |
| `stage-1/` | Stage 1: the JSON API service. Has its own `Dockerfile` and `RUN.md` |
| `stage-2/` | Stage 2: Stage 1 plus the browser website and combinable tables. Has its own `Dockerfile` and `RUN.md` |

Stages 3 and 4 were not attempted. Each stage folder is a full service on its own.

## Run a stage

From inside `stage-1/` or `stage-2/`:

```sh
docker build -t tablekeeper .
docker run --rm -p 8080:8080 tablekeeper
```

Then open `http://localhost:8080/health`. It returns `{"status":"ok"}`. Stage 2 also serves the website at `http://localhost:8080/`. The service needs no network at run time. We checked this by running both images with `--network none`, `--cpus 2` and `--memory 2g`. Each answered `/health` with `{"status":"ok"}`, and stage 2 served `/` with status 200.

See each stage's `RUN.md` for details and for how to run its tests.

## Results we measured

- Stage 1 passed the shipped harness tests (120 of 120), and the harness reported `claimed stage: 1`.
- Stage 2 passed the shipped harness tests (25 of 25), and the harness reported `claimed stage: 2`.
- The agents' own test suite for stage 2 passes (282 tests), including the full unmodified stage 1 suite.
- The shipped harness tests are only a fraction of the full judging tests, so these numbers are evidence, not a guarantee.
