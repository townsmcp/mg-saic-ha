# India telemetry validation for PR #439

## Scope and merge gate

This follow-up bounds India's optional measurement metadata and repairs the
offline tests for [PR #439](https://github.com/townsmcp/mg-saic-ha/pull/439).
The comparison base is beta commit
`ece4d726f0547bf579dcd8823324049b0e57c9b6`; the original PR head is
`d7963c3684c351515404f12eeb1d4d1c4066fd3f`.

**Offline verification does not complete the merge gate.** The changes still
need several days of live India vehicle testing, including driving, charging,
and a Home Assistant restart, as requested in
[the maintainer's review](https://github.com/townsmcp/mg-saic-ha/pull/439#issuecomment-6223566259).
No live Home Assistant installation, vehicle, SAIC account, or ABRP service was
used for the checks recorded here.

## Implementation boundaries

- India snapshots, trip storage, counter-guard metadata, and sensor attributes
  require explicit India context. The default persistence contract remains the
  global contract; it does not add a null India metadata field.
- Metadata uses optional `schema_version: 1`. Unversioned PR metadata is
  normalized on load. Unsupported metadata versions are ignored while the
  existing numerical trip, charge, baseline, and counter state is retained.
  The outer Home Assistant storage version and storage key are unchanged.
- One compact origin retains bounded scalar fields and at most four direct
  input origins. Reset offsets keep their accumulated numerical value and
  latest input summary. A driving tally keeps the latest trip summary instead
  of an ever-growing list of trip provenance.
- Compound provenance has a 6,144-byte ceiling when encoded as compact UTF-8
  JSON. Optional descriptions are reduced before essential source clocks.
  This ceiling applies to metadata, not numerical vehicle state or the entire
  Home Assistant storage document.
- The coordinator selects the dedicated India ABRP sender, which retains
  per-source watermarks per vehicle. A successful source remains acknowledged
  when another source fails, so the next poll retries the failed source.
  The generic sender retains the global combined-payload behavior.
- The global counter algorithm, SOC/range calculations, polling intervals,
  HTTP transport, integration version, workflow permissions, and client
  dependency pins are unchanged by this follow-up.

## Reproduce the offline checks

Run these commands from the PR checkout using Python 3.12. An isolated
environment keeps the integration's dependencies separate from other work:

```bash
python3.12 -m venv /tmp/mg-saic-validation
source /tmp/mg-saic-validation/bin/activate
python - <<'PY'
import json
from pathlib import Path
import subprocess
import sys

requirements = json.loads(
    Path("custom_components/mg_saic/manifest.json").read_text()
)["requirements"]
subprocess.check_call([
    sys.executable, "-m", "pip", "install", *requirements, "pyflakes",
])
PY
python -m unittest discover -s tests -p 'test_*.py'
```

Run the same undefined-name gate as the Python unit-test workflow. Existing
unused-import warnings are reported but do not fail that gate:

```bash
python - <<'PY'
import subprocess
import sys

result = subprocess.run(
    [sys.executable, "-m", "pyflakes", "custom_components/mg_saic"],
    capture_output=True, text=True,
)
print(result.stdout, end="")
print(result.stderr, end="", file=sys.stderr)
raise SystemExit(bool(result.stderr) or "undefined name" in result.stdout)
PY
git diff --check
```

Compare global outputs with the actual pinned beta checkout. The utility runs
the same probe in separate processes under the same interpreter and frozen
clock, and exits unsuccessfully if any observed output differs:

```bash
git worktree add --detach ../mg-saic-beta ece4d726f0547bf579dcd8823324049b0e57c9b6
python tools/compare_global_telemetry.py --base ../mg-saic-beta --candidate .
```

To reproduce the baseline failures, create another detached worktree at
`d7963c3684c351515404f12eeb1d4d1c4066fd3f` and run the same full discovery
command there with the same environment. Do not install different client
versions for the two checkouts.

## Recorded results

Validation date: 17 October 2026. Runtime: Python 3.12.14 on Linux. Both
published clients were installed and exercised: `mg-saic-client==0.9.6` and
`mg-ismart-india-client==0.2.1`. Supporting versions were
`pycryptodome==3.24.1`, `aiohttp==3.14.5`, and `pyflakes==4.0.3`.

| Check | Result |
|---|---|
| Pinned beta full suite | 1,195 tests passed |
| Original PR full suite | 1,203 tests; 29 errors and 1 failure reproduced |
| Updated PR full suite | 1,229 tests passed; no skips |
| Undefined-name gate | Passed; zero undefined names |
| Global differential probe | All four scenarios produce identical canonical JSON against beta |
| Global valid/missing/recovered charging-control behavior test on beta | Passed unchanged against beta |
| 100 India counter resets, with JSON storage restart at resets 50 and 100 | Passed; exact totals and original held clock preserved |
| 100 India trips, with JSON storage restart at trips 50 and 100 | Passed; 100 km and 15 kWh accumulated with fixed-size provenance |
| Legacy metadata, malformed clocks, unsupported versions, and full-field byte bounds | Passed; valid numerical state and recoverable selected clocks retained |
| Diff whitespace check | Passed |

The unit tests use the repository's Home Assistant stubs. HTTP, storage, and
lifecycle boundaries are substituted; live cloud responses and a running Home
Assistant instance are outside this evidence. The differential probe checks
representative global scenarios and is not a proof over every possible input.

The four global comparisons cover:

| Scenario | Observations that match beta |
|---|---|
| Sensors | Six sensor classes across charging, unplugging, dropout, recovery, and zero-SOC fallback; 36 value/availability/attribute observations |
| Storage and derived sensors | Two trips with parked energy; charge open/dropout/restart/close; guard wobble/reset/restart; full stored dictionaries and nonempty Last Trip/Last Charge attributes |
| ABRP | Combined and GPS-only payloads; empty data and missing source clock; HTTP 200/401/403/500 and connection failure; coordinator freshness gating and error handling |
| Polling | Real update/retry/state paths for fresh, cached, charging-dropout, recovered and ICE cycles; endpoint call counts, requested timer delays, freshness, events and polling intervals |

The matching BEV polling sequence uses 300/300/600/300-second intervals and
1/1/5/1 charging endpoint attempts. The dropout requests five 15-second retry
delays; the probe records these requests without waiting. The ICE scenario
does not call the charging endpoint. The storage comparison retains 26 km and
4.5 kWh of driving plus 1.7 kWh parked energy, and a completed 26.4 kWh charge.
These are synthetic fixture results, not vehicle measurements.

The final full run also exercises the inherited global default for manager
subclasses that bypass the constructor. None of the original numerical test
expectations were relaxed to obtain the passing result.

### Regression coverage

| Area | Test or utility |
|---|---|
| Fixed timestamp constants, controlled GPS clock, independent source clocks | `tests/test_india_gps.py`, `tests/test_india_tyre_pressure.py` |
| Valid charging data, endpoint dropout, recovery through real coordinator state updates | `tests/test_charging_controls_dropout.py` |
| India source selection, replay handling, stopped-charge origins, offset restoration | `tests/test_india_boundary_regressions.py` |
| Partial ABRP failure, retry, per-vehicle watermarks, global request contract | `tests/test_india_abrp_dispatch.py` |
| Backend-gated sensor attributes and cyclic legacy metadata | `tests/test_india_sensor_provenance.py` |
| Malformed stored source clocks, numeric legacy clocks, and unsupported metadata versions | `tests/test_india_snapshot_restore.py` |
| Repeated resets, numerical conservation, restart, clocks, stable size, duplicate-save suppression | `tests/test_india_counter_provenance.py` |
| Bounded trip metadata and compatible legacy storage | `tests/test_india_provenance.py` |
| Global outputs against the pinned beta implementation | `tools/compare_global_telemetry.py` |

## Live evidence still required before merge

Record the tested PR commit, vehicle model, Home Assistant version, and the
time of each observation. Use normal driving and charging; additional vehicle
polling or remote commands are not required for the offline procedure.

| Scenario | Evidence to retain |
|---|---|
| Several days of normal India use | SOC, range, odometer, charging duration, and completed trip/charge results remain plausible |
| Ordinary status and charging observations arrive at different times | Each selected value retains the measurement time of its actual source |
| Cached or replayed response | No false trip/charge transition; held readings keep their earlier clock |
| Multiple trips and charge sessions | Metadata size remains bounded and totals continue to accumulate correctly |
| Home Assistant restart during a trip or charge | Baselines, held offsets, totals, and measurement times survive without re-creating the integration |
| ABRP enabled for an existing India vehicle | Advancing source groups appear at their original times; failed groups can retry without repeating successful groups |

Keep the PR open until this evidence is reviewed. A global beta smoke test
before a release adds live evidence beyond the automated comparison recorded
above.

## Beta20 reconciliation — 22 October 2026 (IST)

This reconciliation starts from PR #439 at
`428b063957bd5d62f7a2b277d6231bd09a082de3` and incorporates beta20 plus the
approved changes in #449 and #450:

| Input | Commit |
|---|---|
| Upstream beta20 | `24a9e4d9e7cc0ea0ec063cfe004b4694e22217ef` |
| #449: India charging duration | `323589c69f1d0b30c8a4d2eca8f92255c82b8528` |
| #450: single India charging-cache read | `4f0c6fb36f036c42f06ec4636ee2751bdf58ec26` |

At validation time, #449 and #450 were still open upstream. The comparison
baseline is a local merge of those exact heads onto beta20, not a claim that
they had been released. The reconciliation records the real PR heads as
parents; local validation-only merge commits are not part of its ancestry.

### Changes checked

- Use beta20's general mileage availability and status-odometer fallback.
  The redundant India-specific availability exception is removed.
- Preserve elapsed charging seconds as in #449 and consume India's charging
  cache once per update as in #450, including after the startup network budget
  is exhausted. Global retry and timeout handling is unchanged.
- Route India charging success, replay rejection, and missing-data outcomes
  through beta20's `_record_charging_outcome` helper. Successful readings keep
  their source measurement time; `last_attempt` retains the receipt time.
- Add a real update-cycle regression for advancing measurement, replay, and
  recovery. It checks timestamps, stale/recovery INFO transitions, one cache
  read per cycle, and no retry sleeps.
- Adapt #450's existing status fixture to include `basicVehicleStatus`, without
  weakening its assertions. Update the older global-mileage test to beta20's
  behavior: missing status mileage remains unavailable, while valid status
  mileage works without charging data and stays retained after a failed poll.
- Remove the fragile assertion that the coordinator has exactly four helper
  call sites. Keep the guard against direct freshness-tracker writes; the new
  behavioral regression covers the two additional India outcomes.

### Results

Runtime: Python 3.12.14. Installed clients: `mg-saic-client==0.9.6` and
`mg-ismart-india-client==0.2.1`; `pyflakes==4.0.3`.

| Check | Result |
|---|---|
| Full offline suite on the reconciled code | 1,314 tests passed, no skips |
| Focused India missing-cache and freshness sequence | 2 tests passed |
| Global differential comparison against beta20 + #449 + #450 | All four scenarios matched: sensors, storage, ABRP, polling |
| Undefined-name gate | Passed; existing unused-import/variable warnings remain non-blocking |
| Diff whitespace check | Passed |

The first suite run exposed two outdated test expectations: beta20 now makes
global mileage available from good status data, and India adds two freshness
helper call sites. Only the test contracts described above changed to resolve
those failures; the production reconciliation was unchanged between runs.

### Reproduce against the exact baseline

Use the isolated Python environment and manifest dependencies described above.
The following baseline merges are local test setup only:

```bash
git worktree add --detach ../mg-saic-beta20-check 24a9e4d9e7cc0ea0ec063cfe004b4694e22217ef
git -C ../mg-saic-beta20-check merge --no-ff --no-edit 323589c69f1d0b30c8a4d2eca8f92255c82b8528
git -C ../mg-saic-beta20-check merge --no-ff --no-edit 4f0c6fb36f036c42f06ec4636ee2751bdf58ec26
python -m unittest discover -s tests -p 'test_*.py'
python tools/compare_global_telemetry.py --base ../mg-saic-beta20-check --candidate .
git diff --check
```

These results still use synthetic input and substituted HA/I/O boundaries.
No vehicle installation, live charge, trip, Home Assistant restart, or ABRP
service validation was performed. Publishing an experimental beta to gather
that evidence remains a proposal for the maintainer; this reconciliation does
not waive the outstanding field-validation requirement or merge #439.

## Captured-frame review follow-up — 22 October 2026

[Dr-Blank's review of `ecc7646`](https://github.com/townsmcp/mg-saic-ha/pull/439#pullrequestreview-5608834463)
reports comparison with 157 charging frames and 138 ordinary-status frames
from DC and AC charging sessions. That review supports the duration units,
SOC scaling, source selection and current sign, and identifies three bugs
that the earlier synthetic fixtures missed. These are the reviewer's reported
capture results; this follow-up did not independently collect those frames or
install the integration on a vehicle.

### Changes

- Valid India GPS groups now retain their own timestamp even when neither
  battery source has SOC at exactly that time. SOC is attached to GPS only
  when its source clock matches; older SOC is never relabelled. Position-only
  packets also follow the existing global sender's contract.
- Each poll sends its India groups in ascending timestamp order. Successful
  groups still retain separate watermarks and only failed groups are retried.
  This is ordering within a poll, not a guarantee of a globally monotonic
  stream across retries: an older failed source may be retried in a later poll.
- When no advancing India charging frame is available, the operational
  charging flag is held across short gaps. On the next processed update more
  than 30 minutes after the last accepted source timestamp, both flags clear,
  including when ordinary status also stops advancing. Replayed frames do
  not extend the hold. Expiry itself does not create a charge-end reading or
  activity timestamp, change the source watermark, or close a stored session.
  A fresh charging frame can re-enable the flag normally.

The named `INDIA_CHARGING_FLAG_MAX_AGE` is a proposed 30-minute hold policy
for maintainer review, not a duration established by the captures. This guard
addresses missing/non-advancing frames; it does not change how newly advancing
historical frames are admitted by the existing timestamp sanity checks.

### Validation

Four added test methods first failed against the production code at `ecc7646`
(nine failed assertions across their subcases), reproducing missing GPS,
out-of-order sends, and the indefinitely held flag. With the fixes:

- **1,318 offline tests pass, with no skips.** Python 3.12.14;
  `mg-saic-client==0.9.6`, `mg-ismart-india-client==0.2.1`,
  `aiohttp==3.14.5`, `pycryptodome==3.24.1`, and `pyflakes==4.0.3`.
- GPS regression coverage includes source-clock offsets of -1, +2 and +5
  seconds, no invented SOC, replay suppression and GPS-only advancement.
- Charging-flag checks cover missing/replayed frames with advancing/static
  status, holding at exactly 30 minutes, expiry immediately beyond it, recovery,
  and a real subsequent charge-stop retaining its measurement timestamp.
- Existing retry tests move their simulated failures with the new send order,
  preserving their independent-source retry assertions. One manually created
  coordinator fixture now initializes its normal charging flags.
- All four global differential scenarios match the beta20 plus #449/#450
  baseline described above. The undefined-name gate and `git diff --check`
  pass; pre-existing unused-import/variable warnings remain.

These remain offline checks with substituted Home Assistant and transport
boundaries. Live ABRP acceptance, vehicle charging/trips and a Home Assistant
restart on the updated integration still need validation. An experimental beta
remains a proposal for James to decide after reviewing the follow-up.
