# Impact report: agent vs randomized holdout

Simulator seed for the headline numbers: `oua-practice`. All numbers are computed by `stage_agent.impact_reporter` from `sample_run/outcomes.jsonl`, `replay/arms.csv` and the simulator output; nothing in this file is typed by hand.

## 1. Estimand and method

* **Unit of randomization and analysis:** the user (`replay/arms.csv`). Each user counts once, however many triggers or events they produced.
* **Population:** every randomized user with at least one replay-day trigger (agent n = 863, holdout n = 883; arms file overall: {'holdout': 2487, 'agent': 2513}). Triggers are fixed inputs that do not depend on the arm, and the simulator only produces outcomes for triggered users, so this is a pre-treatment restriction.
* **Analysis by assigned arm (intention to treat):** agent-arm users the agent abstained on, could not serve, or found ineligible are all included.
* **Outcome:** the user's first goal (chosen by the same procedure in both arms; never acted on in the holdout) succeeded within its window, judged from simulator events. Users with no goal count as not succeeded. Users whose window extends past the observed data are inconclusive and also count as not succeeded in the primary analysis (sensitivity below excludes them).
* **Interval:** Newcombe hybrid score interval (Wilson-based) for the difference of proportions; two-sided pooled z-test p-value for reference.

## 2. Primary result

| Outcome | Agent k/n | Agent rate | Holdout k/n | Holdout rate | Diff (agent - holdout) | 95% CI (Newcombe) | Relative | p |
|---|---|---|---|---|---|---|---|---|
| goal reached (first goal; no goal = not reached) | 71/863 | 8.2% | 66/883 | 7.5% | +0.75 pp | [-1.78, +3.31] pp | +10.1% | 0.559 |
| goal reached, excluding inconclusive first goals | 71/863 | 8.2% | 66/883 | 7.5% | +0.75 pp | [-1.78, +3.31] pp | +10.1% | 0.559 |
| goal reached, users with a goal only | 71/749 | 9.5% | 66/735 | 9.0% | +0.50 pp | [-2.47, +3.46] pp | +5.6% | 0.740 |

Balance check (pre-treatment): users with no goal - agent 114/863 (13.2%) vs holdout 148/883 (16.8%), p = 0.038. No-goal status is fixed by the replay fixture before any treatment (almost always: every trigger was an offline-buffered open), so a difference here is chance imbalance, not an agent effect; the goal-holder row above removes it.

Interpretation: an estimate of +0.75 pp whose 95% interval [-1.78, +3.31] includes zero: the data do not distinguish the agent from no agent at this sample size.
Absolute difference: **+0.75 pp**. Relative difference: +10.1% (relative to the holdout rate; the absolute difference is the decision-relevant number).

First-goal result counts by arm:

* agent: {'failed': 678, 'succeeded': 71, 'no_goal': 114}
* holdout: {'failed': 669, 'succeeded': 66, 'no_goal': 148}

## 3. Goal-agnostic secondary outcomes (same population, same ITT comparison)

| Outcome | Agent k/n | Agent rate | Holdout k/n | Holdout rate | Diff (agent - holdout) | 95% CI (Newcombe) | Relative | p |
|---|---|---|---|---|---|---|---|---|
| any play_started in the 14-day horizon | 665/863 | 77.1% | 669/883 | 75.8% | +1.29 pp | [-2.69, +5.27] pp | +1.7% | 0.525 |
| any episode_completed | 488/863 | 56.5% | 502/883 | 56.9% | -0.30 pp | [-4.94, +4.33] pp | -0.5% | 0.898 |
| paid purchase (is_trial = false) | 35/863 | 4.1% | 36/883 | 4.1% | -0.02 pp | [-1.91, +1.88] pp | -0.5% | 0.982 |
| notification opt-out (harm metric) | 0/863 | 0.0% | 0/883 | 0.0% | +0.00 pp | [-0.43, +0.44] pp | n/a | 1.000 |

## 4. By first goal

Goals are assigned before any treatment by the same deterministic procedure in both arms, so each row is a randomized comparison within a pre-treatment stratum. Samples are small, intervals wide and there is no multiplicity correction: read these as descriptive.

| Goal | Agent k/n | Agent rate | Holdout k/n | Holdout rate | Diff (agent - holdout) | 95% CI (Newcombe) | Relative | p |
|---|---|---|---|---|---|---|---|---|
| G01 | 22/59 | 37.3% | 15/51 | 29.4% | +7.88 pp | [-9.73, +24.53] pp | +26.8% | 0.383 |
| G02 | 5/24 | 20.8% | 1/23 | 4.3% | +16.49 pp | [-3.79, +36.45] pp | +379.2% | 0.090 |
| G03 | 1 | - | 0 | - | - | - | - | - |
| G04 | 0/1 | 0.0% | 0/1 | 0.0% | +0.00 pp | [-79.35, +79.35] pp | n/a | 1.000 |
| G05 | 5/130 | 3.8% | 6/114 | 5.3% | -1.42 pp | [-7.57, +4.19] pp | -26.9% | 0.595 |
| G06 | 0/10 | 0.0% | 1/11 | 9.1% | -9.09 pp | [-37.74, +19.65] pp | -100.0% | 0.329 |
| G07 | 12/438 | 2.7% | 18/440 | 4.1% | -1.35 pp | [-3.91, +1.13] pp | -33.0% | 0.270 |
| G08 | 9/34 | 26.5% | 10/42 | 23.8% | +2.66 pp | [-16.25, +22.25] pp | +11.2% | 0.790 |
| G09 | 4/22 | 18.2% | 2/15 | 13.3% | +4.85 pp | [-22.00, +27.33] pp | +36.4% | 0.694 |
| G12 | 14/30 | 46.7% | 13/38 | 34.2% | +12.46 pp | [-10.41, +34.01] pp | +36.4% | 0.297 |

## 5. Sensitivity to the simulator seed

The agent's actions are fixed; only the simulator seed changes.

| Seed | Agent rate | Holdout rate | Diff | 95% CI |
|---|---|---|---|---|
| `oua-practice` | 8.2% | 7.5% | +0.75 pp | [-1.78, +3.31] |
| `seed-a` | 8.6% | 7.7% | +0.87 pp | [-1.70, +3.47] |
| `seed-b` | 9.3% | 7.0% | +2.25 pp | [-0.32, +4.85] |
| `seed-c` | 8.5% | 7.9% | +0.53 pp | [-2.06, +3.13] |
| `seed-d` | 8.2% | 9.6% | -1.40 pp | [-4.09, +1.29] |

## 6. Pilot report audit

The internal pilot summary compares users **served** by the agent with **everyone else**. Recomputed from `pilot/pilot_outcomes.csv` (3891 users; arm/served mismatches against `pilot_assignment.csv`: 0):

| Comparison | Agent k/n | Agent rate | Holdout k/n | Holdout rate | Diff (agent - holdout) | 95% CI (Newcombe) | Relative | p |
|---|---|---|---|---|---|---|---|---|
| served vs everyone else (pilot report method) | 87/217 | 40.1% | 649/3674 | 17.7% | +22.43 pp | [+16.01, +29.17] pp | +127.0% | 0.000 |
| treatment arm vs holdout arm (ITT) | 349/1912 | 18.3% | 387/1979 | 19.6% | -1.30 pp | [-3.76, +1.16] pp | -6.7% | 0.300 |

* The flawed comparison reproduces the report's headline (40.1% vs 17.7%). Its 'everyone else' group mixes the holdout arm with treatment users who were not served; served users in the holdout arm: 0.
* **Correct randomized estimate (ITT):** treatment 18.3% vs holdout 19.6%, difference -1.30 pp (95% CI [-3.76, +1.16] pp): an estimate of -1.30 pp whose 95% interval [-3.76, +1.16] includes zero: the data do not distinguish the agent from no agent at this sample size.
* **Why the headline misleads (selection bias):** serving was not random. Served users' goal mix was {'G09': 105, 'G05': 84, 'G02': 15, 'G06': 12, 'G04': 1} against {'G05': 2034, 'G09': 848, 'G02': 345, 'G06': 319, 'G04': 128} for everyone else. Holdout users (who never saw the agent) with the served users' goal mix would have succeeded 34.4% of the time, so most of the gap is who was served, not what serving did. Treatment users who were not served succeeded 15.5% (n = 1695).

Pilot by goal (treatment vs holdout, with the share of treatment users served):

| Goal | Agent k/n | Agent rate | Holdout k/n | Holdout rate | Diff (agent - holdout) | 95% CI (Newcombe) | Relative | p |
|---|---|---|---|---|---|---|---|---|
| G02 | 34/177 | 19.2% | 36/183 | 19.7% | -0.46 pp | [-8.63, +7.75] pp | -2.4% | 0.912 |
| G04 | 15/63 | 23.8% | 15/66 | 22.7% | +1.08 pp | [-13.36, +15.61] pp | +4.8% | 0.884 |
| G05 | 13/1045 | 1.2% | 6/1073 | 0.6% | +0.68 pp | [-0.15, +1.61] pp | +122.5% | 0.095 |
| G06 | 1/157 | 0.6% | 5/174 | 2.9% | -2.24 pp | [-5.95, +1.08] pp | -77.8% | 0.128 |
| G09 | 286/470 | 60.9% | 325/483 | 67.3% | -6.44 pp | [-12.47, -0.35] pp | -9.6% | 0.038 |

## 7. Policy audit of the simulated run

* Simulator `policy_problem_count`: **0**; holdout actions ignored by the simulator: 0.
* Independent post-hoc audit of `actions.jsonl` against the pack rules: {'actions_checked': 1304, 'problems_total': 0, 'problems': {}}

## 8. Limits of this evidence

* The outcome model is the supplied simulator, not real users. Its effect sizes (page multipliers, click rates) are assumptions of the simulator; a simulated uplift is evidence that the agent's actions reach the mechanisms the simulator models, not proof of real-world impact.
* One replay day, ~1.7k triggered users: power to detect a few-point difference is limited, as the interval width shows.
* The goal outcome depends on the goal mix, which is the same in both arms by construction; goal-agnostic secondary outcomes are reported so that the conclusion does not rest on the goal definition alone.
* Goal-level rows are small subgroups without multiplicity correction.
