# FACTORY.md: how the CResTE factory works

## Seats

Three agents run in one BAND room. All use Claude Code as the harness. The mandates are generic: they describe a way of working, not the product being built.

| Seat | Job |
|---|---|
| **Planner** | Reads the task and spec, turns them into a requirements checklist and numbered work items, assigns each item to the next seat by @mention, tracks progress, pings a seat that goes quiet, and decides on its own when the team is blocked (and records the decision) |
| **Implementer** | Writes the code and the tests, runs the checks, and posts evidence (command, output, test counts) before handing off |
| **Reviewer** | Does not write application code. Re-runs the checks independently, tests against the spec, and either accepts or rejects with numbered findings that route back to the right work item |

Mandates are in `mandates/`. They are re-read by BAND when an agent starts.

## How work moves

1. The human sends one task message per stage, addressed to the Planner.
2. The Planner posts the checklist and assigns work to the Implementer with an @mention.
3. The Implementer builds, runs the harness and its own tests, and posts evidence with an @mention of the Reviewer.
4. The Reviewer verifies independently and replies ACCEPTED or REJECTED. A rejection goes back to the Implementer with reproduction steps.
5. Every handoff and every decision is a room message that @mentions the next seat, so nothing depends on anyone's memory. The full history is in `room.json`.

## How bad work is caught

- **Independent review.** The Implementer cannot mark its own work done. The Reviewer re-runs the harness and the tests itself and reads the spec, rather than trusting the Implementer's report. The room contains real rejections, not only acceptances.
- **The shipped harness.** Each stage is checked with `python -m harness run --track tablekeeper --stage N`. We read `claimed stage: N` and the per-stage report. We treated a passing run as necessary but not sufficient, because the shipped tests are only a part of the full tests.
- **Evidence before handoff.** The mandates require pasted command output with counts, not "it works".
- **Regression guard.** Stage 2 was built from the accepted stage 1, and the full unmodified stage 1 test suite was run against stage 2 (all passing).
- **No-network check.** After the run, each stage image was rebuilt and started with `--network none`, 2 CPUs and 2 GiB memory, and answered `/health`.

## What we changed after practice runs

Before the final run we ran the factory twice on practice work. Two failures led to version 2 of the mandates:

- A Reviewer rejection never reached the Implementer, because the rejection did not @mention anyone. Rule added: every handoff and decision is a room message that @mentions the next seat.
- A background tool crashed and the agent went silent. Rule added: retry a crashed tool once in the foreground, never go silent, and the Planner pings any silent seat.

## Costs and limits

- The factory ran on a Claude Pro subscription. Estimated cost at API prices for the whole effort, as reported by BAND analytics, was about 47 US dollars.
- The Pro session limit stopped the run once, in the middle of stage 2. We waited for the limit to reset and continued in the same room. The model for the agents was set to `claude-sonnet-5` in the Claude Code settings file before the run continued.

## Honest limitations

- Stage 1 started from the code of an earlier practice run in the same workspace. The agents re-verified it against the spec in the final run, and the harness result above is from that verification.
- The Planner did not post a closing summary for stage 1 or stage 2. The Reviewer's ACCEPTED messages are the record.
- The agents had no tool to look at the website. They checked behavior with tests and the harness, but nobody inspected visual quality. The site has a plain look: table options are shown as numbers without drawings, and the default date comes from the server (UTC), not the visitor's local date.
- The spec says a booking must not be rejected only because its start is in the past, so the API accepts past dates on purpose.
- We started a polish round (table drawings with seat counts, a legend, local default date, a clearer mobile and desktop layout, team name in the header) in a new room. The Claude session limit stopped it before any code changed, so none of it is in this submission. The accepted stage 1 and stage 2 are submitted unchanged.
- There is no browser-driven test suite in the repository.
- Stages 3 and 4 were not attempted.
- The human did not write or edit application code. The human built and installed the mandates, sent the task messages, restarted tools that crashed, and ran the final checks.
