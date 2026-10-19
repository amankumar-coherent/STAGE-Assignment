# One User, One Agent

A per-viewer decision agent for the STAGE take-home. For every app-open or login trigger, the viewer's agent:

1. assembles what is known about the viewer, with source, time, freshness and conflicts for every fact;
2. keeps or chooses **one measurable goal** (with deadline, success signal and rule version), or records why none applies;
3. proposes a proportionate intervention (home page, at most one message, in-app message) or **nothing**;
4. has an **independent verifier** check the proposal before anything is published;
5. publishes idempotently within the latency budget, and reacts to purchases, opt-outs, title withdrawals, rule
   corrections and dependency outages;
6. judges each goal from attributable simulator events and measures impact against the **randomized holdout**.

Everything runs locally with the Python standard library, a deterministic mock model provider, and SQLite. No API key
and no network access are needed.

## Requirements

* Python 3.10+ (developed and tested on CPython 3.14.8, Windows 11). No third-party packages.
* The data pack (not committed: it counts as personal data). Put it at `./pack`, or set `STAGE_PACK=/path/to/pack`,
  or pass `--pack`.

```
submission/
  pack/                     <- the unzipped data pack (data/, config/, replay/, pilot/, simulator/)
```

## One command

```
python run.py
```

This replays `replay/triggers.jsonl` with the live and control events through the agents. It then runs the supplied
`simulator/simulate_outcomes.py` on the published actions for five seeds (`oua-practice` is the headline seed, and four
more show seed sensitivity), judges every goal, computes the randomized intention-to-treat impact, audits the pilot
report and writes every artifact to `sample_run/`. It takes about 80 s on a laptop. Each run starts from a fresh database.

Useful variants:

```
python run.py --seeds my-seed,another-seed        # evaluator seed(s); the first is the headline
python run.py --pack /path/to/other_pack          # an unseen replay day (same file layout)
python run.py --keep                              # rerun against the existing DB: already-decided triggers are skipped
python run.py inventory                           # print the measured data-quality inventory only
python -m stage_agent run ...                     # same CLI after `pip install -e .` (or with PYTHONPATH=src)
```

## Tests

```
python -m unittest discover -s tests -v
```

48 tests, about 15 s. They use synthetic packs in temporary directories with temporary SQLite databases, so they never
read or modify the real pack. The pack's config files are copied into `tests/fixtures/config` because they are part of
the specification.

## Outputs (`sample_run/`)

| File | Content |
|---|---|
| `decisions.jsonl` | One record per trigger delivery (plus withdrawal repairs): goal, action or `no_action_reason`, dialect and conflicts, content, confidence and reason, evidence refs, facts with provenance and freshness, assumptions, verifier checks, model/tool calls, latency, status |
| `actions.jsonl` | Published actions in the simulator's input schema (cancelled actions excluded) |
| `action_log.jsonl` | Every action with its lifecycle status (`published`, `scheduled`, `sent`, `cancelled`, `superseded`) and reason |
| `outcomes.jsonl` | One judgement per goal: `succeeded` / `failed` / `inconclusive`, evidence events, attribution |
| `viewer_state/<user>.json` | Per-viewer durable state: facts, goal history, decisions, actions, outcomes, learning, audit trail |
| `latency_report.json` | Latency distribution and its semantics, fallbacks, model/store statistics, fault and control handling |
| `impact_report.md` | Randomized ITT impact, secondary outcomes, goal-level rows, seed sensitivity, pilot audit |
| `final_report.md` | Generated summary of this run |
| `data_quality_report.md` / `.json` | Measured data-quality inventory |
| `audit_log.jsonl` | Purchases after decisions, cancellations, rule corrections, withdrawals, dependency windows |
| `sim/` | Simulator output for the headline seed; `sim_seeds/*/simulator_report.json` for the others |
| `state.db` | The SQLite store (agent memory). Not committed |
| `agent.log` | Structured JSON log (for example the catalogue prompt-injection flags) |

## Configuration

* `config/agent_config.json` overrides the defaults in `src/stage_agent/config.py` (`AgentConfig`): staleness
  threshold, model/tool budgets (8/20), timeouts, retry counts, worker count, load-shedding depth, confidence
  thresholds, goal priority and message timing. Unknown keys are rejected.
* Policy comes from the pack (`config/interventions.json`, `pricing.json`, `goals.json`) and is validated at start-up.

## Documentation

* [ARCHITECTURE.md](ARCHITECTURE.md): ownership, data flow, fact precedence, goals, verification, latency, failure
  recovery, measurement, plus a walkthrough of one viewer from app open to judged outcome.
* [DATA_QUALITY.md](DATA_QUALITY.md): what is wrong with the pack and how each problem is handled.
* [final_report.md](final_report.md): results of the sample run.
* [KNOWN_LIMITATIONS.md](KNOWN_LIMITATIONS.md) and [AI_COLLABORATION.md](AI_COLLABORATION.md).

## Troubleshooting

* `FileNotFoundError ... pack/data/events.jsonl`: the pack is not at `./pack`. Use `--pack` or `STAGE_PACK`.
* Windows has no tz database, so IST falls back to a fixed +05:30 offset. That is exact for India (no DST since 1945).
  Decisions record which one was used (`ist_source`).
* A run takes noticeably longer on folders scanned by antivirus software (one run here spent 125 s instead of 16 s on
  ingestion). Results are unaffected.

## Layout

```
src/stage_agent/
  cli.py               one-command pipeline (replay -> simulator -> judge -> reports)
  config.py            AgentConfig (limits, budgets) + PackConfig (pack policy files, validated)
  models.py            Event, Title, Fact, Check, GoalRule; observable event vocabulary
  loader.py            JSONL/CSV readers with quarantine of malformed lines
  normalization.py     timestamp model (buffer delay, skew), dialect/deeplink parsing, injection flagging
  state_store.py       SQLite schema, transactions, idempotency constraints
  fact_resolver.py     per-viewer facts with provenance, freshness, precedence and conflicts
  goal_planner.py      goal rules + revisions, eligibility, ranking, success predicates
  content_selector.py  catalogue availability/withdrawals, popularity, page and message content
  intervention_planner.py  proportionate page / message / in-app proposals, confidence
  verifier.py          independent safety boundary (fails closed)
  model_provider.py    provider interface, deterministic mock, fault injection, outage gate, bounded client
  dependencies.py      simulated user-data-store latency / timeouts
  agent.py             the per-viewer agent: decide -> verify -> publish; live/control event reactions
  scheduler.py         UTC/IST, quiet hours, message slot planning
  replay.py            virtual-clock replay harness with worker pool, exports, latency report
  outcome_judge.py     goal judgement, attribution, learning
  impact_reporter.py   ITT + Newcombe intervals, pilot audit, report rendering, export audit
  data_quality.py      measured inventory
  final_report.py      generated run report
  telemetry.py         JSON logging, percentiles
tests/                 48 behavioural tests (unittest)
```
