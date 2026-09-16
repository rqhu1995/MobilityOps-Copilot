# Domain model and HGS contract

Stage 4B (2026-09-14) added the Gurobi adapter with explicit service registration
and independent period-model verification. Its implemented contract and actual
acceptance are in [gurobi-adapter.md](gurobi-adapter.md); the original semantic
audit remains in [gurobi-integration-audit.md](gurobi-integration-audit.md).
The sections below describe HGS. Gurobi uses `gurobi_linear_default`, separate
verification and candidate-aware timeout handling, and makes no cross-backend
optimality claim.

This document records the domain contract and implemented stage-3 HGS Python
integration. The solver remains an independent repository: the adapter executes
an existing binary, reads only the necessary inputs, and writes artifacts in an
isolated run directory. It neither builds nor changes either solver core.

## ScenarioSpec to HGS mapping

The mapping was checked against
`/home/runqiu/BRPWR-HGSADC-SBC/Program/helpers/Args.cpp`.

| `ScenarioSpec` field | HGS CLI | HGS parser type | Unit and validation | Contract note |
| --- | --- | --- | --- | --- |
| `scenario_name` | — | — | Non-empty string | Orchestration metadata only. |
| `instance_id` | `-i` / `--inst_no` | `int` | Integer `> 0` | Selects the suffix in `Instances/<stations>_<instance>/`. |
| `number_of_stations` | `-ns` / `--num_stations` | `int` | Integer `> 0` | Number of non-depot stations. Preflight checks the instance and required input files. |
| `number_of_trucks` | `-ntrk` / `--num_trucks` | `int` | Integer `> 0` | HGS README documents positive truck counts. |
| `number_of_repairers` | `-nrpm` / `--num_repairer` | `int` | Integer `> 0` | HGS is the repairer-enabled solver; zero is not accepted by this contract. |
| `truck_capacity` | `-vcap` / `--vehicle_capacity` | `int` | Bikes, integer `> 0` | Capacity per truck. |
| `operation_time_budget_sec` | `-tb` / `--time_budget` | `double` | Seconds, finite float `> 0` | HGS accepts a negative sentinel for an automatic default; the contract requires an explicit deterministic value. |
| `solver_runtime_limit_sec` | `-tl` / `--timeLimit` | `double` | Seconds, finite float `> 0` | HGS treats zero as unlimited. The contract rejects zero; the adapter also enforces an external timeout. |
| `loading_time_sec` | `-ldT` / `--loading_time` | `int` | Seconds per bike, integer `> 0` | Used for both loading and unloading operations. |
| `repair_time_sec` | `-rpT` / `--repair_time` | `int` | Seconds per bike, integer `> 0` | On-site repair service time. |
| `broken_bike_proportion` | `-bprop` / `--broken_proportion` | `double` | Optional finite ratio in `[0, 1]` | `None` means omit the flag and preserve `curBroken` from the instance files. A supplied ratio makes HGS derive broken inventory. |
| `seed` | — | — | Optional 32-bit unsigned integer | Current HGS seeds `std::mt19937` from system time and exposes no seed CLI. `HGS_CAPABILITY.supports_seed` is therefore `false`. |
| `objective_profile` | — | — | Only `eudf_default` | HGS objective is compiled into the solver; there is no objective selector CLI. |
| `solution_requirement` | — | — | `fast_feasible` or `best_available` | Orchestration policy only; it must not silently rewrite runtime fields. |

The HGS-only algorithm tuning flags `-pnt`, `-noimp`, `-num_pnt_manage`,
`-fesP`, `-mu`, `-lambda`, and `-edu` intentionally remain backend defaults.
They are not scenario-domain facts and are not exposed in stage 3.

The example uses the real directory
`/home/runqiu/BRPWR-HGSADC-SBC/Instances/6_1`. Its 7200-second operation
budget, 60-second loading time, 300-second repair time, and capacity 25 match
the HGS defaults for this small-instance family. The five-second solver limit
is an explicit smoke-test policy, validated through the real stage-3 service.

`build_hgs_argv(executable, scenario)` returns a tuple of strings in the table's
mapped-field order. It performs no I/O, and never constructs a shell command.
`check_constraints` precedes execution in both the service and direct adapter.
It records objective/profile support, unchanged solution-policy runtime,
seed rejection, optional broken-inventory behavior, truck/repairer resources,
and absent bound/gap reports. Any `REJECTED` record raises
`ConstraintRejectedError` with the complete records before creating a run or
starting HGS. Seed absence and absent bound/gap are explicit `IGNORED` records
with `requested_value=None`; they do not describe discarded user requests.
Signed C++ integer overflow is also rejected for integer CLI fields and the
operation budget (which FileHelper casts to `int`).

## Audited HGS entry point and execution

The 2026-09-10 audit read `Program/helpers/Args.cpp`, `Program/main.cpp`,
`Program/Instance.cpp`, `Program/helpers/FileHelper.h`,
`Program/Genetic.cpp`, and the directly relevant route/parameter definitions.
The available executable is
`/home/runqiu/BRPWR-HGSADC-SBC/build/main`; a different existing absolute
executable can be supplied to `HgsBackend`. There is no output-path CLI.

HGS resolves inputs as `../Instances/<stations>_<instance>/` and writes
`../Solutions/YYYY-MM-DD/<stations>_<instance>_t<trucks>_r<repairers>_<hours>h_<counter>.txt`.
The date uses solver-local time; hours is the integer operation budget divided
by 3600; the suffix is a file counter, not runtime. Running in the original
`build/` would write into the HGS repo, so the adapter instead uses
`runs/<run_id>/work/` as its explicit working directory. It copies only the
required data files into the sibling instance snapshot:

- `time_matrix_<stations>.txt` and `station_info_<stations>.txt`;
- `dissat_table_i.txt`, `BCRF_i.txt`, and `BCRFR_i.txt` for each station `i`.

`linear_diss_i.txt` and `BCRFT_i.txt` are not required by this HGS reader.
Preflight checks repo/directory existence, executable permissions, every
required input file, absolute runs root, containment, and run-ID uniqueness.
It rejects a runs root inside either configured solver repository. It checks
file availability, not full instance-table dimensions or mathematical validity.
The executable and instance files are trusted local inputs; this directory
isolation is not an operating-system sandbox for arbitrary executables.

Execution uses `subprocess.run(argv, cwd=..., timeout=..., capture_output=True,
shell=False, check=False)`. The external wall timeout is the unchanged internal
limit plus a finite grace period (default 5 seconds; configurable in `(0, 60]`).
`subprocess.run` kills and waits for the child on timeout. The audited binary
does not spawn solver subprocesses. Both streams stay in bytes, including
partial timeout output. Result bytes are persisted before strict UTF-8 decoding
and parsing. A decoding failure preserves the bytes and returns `failed`.

HGS normally returns exit code 0 from `main`. Argument parser failures and
uncaught input errors can exit nonzero/terminate abnormally. Some input/output
errors only print to stderr, and saving the result does not reliably alter the
exit code. Consequently exit code 0 alone is insufficient: exactly one result
file matching the scenario prefix must be found in this run, parsed, and
validated. Files from another run, symlinks, ambiguous files, or missing output
cannot become a successful solution. No scanning of the HGS repo's Solutions
directory occurs during execution.

The HGS initial-population loop does not enforce the internal runtime limit;
some iterations can also overrun it. Its stagnation limit may end search early.
Stdout contains population progress, iteration diagnostics and final summaries;
stderr contains diagnostics. `Genetic::saveResults` produces the authoritative
route and metric text. `CPU time:` is actually elapsed
`high_resolution_clock` time measured from `Params` construction, including
initial population; it is not operating-system CPU usage. The adapter also
records its separately measured process wall time.

## Run artifacts and persistence failures

```text
runs/<run_id>/
├── scenario.json          # validated full scenario, including defaults
├── command.json           # actual argv JSON array
├── process.json           # cwd, timing, exit status, constraints, SHA-256 provenance
├── stdout.log             # original bytes, even for failure/timeout
├── stderr.log             # original bytes
├── Instances/<n>_<i>/     # only required input snapshots
├── work/                  # child cwd
├── Solutions/<date>/*.txt  # untouched native solver output, if produced
├── solver-result.txt      # exact copy of the sole accepted native file, if present
├── validation.json        # independent verification report or failure/not-performed reason
└── solution.json          # final validated normalized result
```

`process.json` is written before launch and updated after completion; metadata
records executable SHA-256, input-file SHA-256, source instance, cwd, timeout,
actual return code, elapsed wall time, and constraint records. It does not
capture environment variables. `returncode=None` on timeout/launch error means
no exit code was returned by `subprocess.run`; no exit code is fabricated.
The canonical result file is copied even on nonzero/timeout when a single safe
native file exists, but that output is not promoted to a feasible solution.

`SolutionResult.raw_artifact_paths` contains absolute file paths belonging to
this run, with required roles `scenario`, `command`, `stdout`, `stderr`, plus
`process`, `validation`, safe `native_result_N` files and `solver_result` when available.
`solution.json` is written via an atomic temporary-file replacement only after
raw persistence and validation; it is not itself listed as a raw artifact.
Failed runs may have no native/canonical result, and those paths are not
invented. Native files remain available on parsing/validation failure.
`validation.json` mirrors `solver_metadata.validation`. On success it contains
recomputed metrics, comparisons/tolerances, route arrivals/departures and station
inventory timelines. On failure its status is `failed` for rejected validation,
or `not_performed` when execution/output parsing did not produce a candidate.
An artifact-write exception may leave incomplete files and no final result.

Run IDs match `[A-Za-z0-9][A-Za-z0-9._-]{0,199}` without whitespace normalization.
Both the domain result and the execution boundary enforce this. Resolved run
paths must remain below the resolved runs root, and symlink run directories
are rejected. Directory creation reserves an ID atomically with no overwrite.
Reusing a completed, failed or partially prepared run requires a new ID.

Configuration/preflight errors raise `HgsPreflightError` before a run is
reserved (unsafe IDs raise `ValueError`). Artifact I/O errors raise
`ArtifactError` carrying `run_dir`, and keep existing evidence. There is no
false claim that `solution.json` was saved when its filesystem is unwritable.
These are caller-visible exceptions rather than fictitious solver results.

## SolutionResult semantics

- `status` uses `pending`, `running`, `succeeded`, `infeasible`, `timeout`, or
  `failed`.
- `feasible` is optional because feasibility is unknown before or after some
  failed/timeout runs. `succeeded` requires `true`; `infeasible` requires
  `false`.
- HGS text output includes objective value, dissatisfaction, emission, CPU
  time, truck operations, and repairer operations. Those values may be filled
  only after successful parsing.
- Current HGS output does not include a mathematical best bound or optimality
  gap. `best_bound` and `optimality_gap` must remain `null` for HGS rather than
  being estimated.
- `optimality_gap`, when supplied by a future exact backend, is a fraction
  rather than a percentage.
- `raw_artifact_paths` maps artifact roles such as `raw_log` or
  `solver_result` to paths. `solver_metadata` contains only JSON-compatible
  backend details.

| HGS observation | Result status | `feasible` | Treatment |
| --- | --- | --- | --- |
| Exit 0, one valid native result, parser and validator pass | `succeeded` | `true` | Report parsed finite objective, dissatisfaction, emission, runtime and routes. |
| External timeout | `timeout` | `null` | Preserve partial streams and any native output; no candidate promotion. |
| Launch failure or nonzero exit | `failed` | `null` | Preserve diagnostics and actual exit code when known. |
| Missing/ambiguous/invalid output, invalid UTF-8, parsing, structural or independent verification failure | `failed` | `null` | Preserve raw output and reason; do not return invalid parsed routes/metrics. |
| No feasible solution found before timeout | `timeout` | `null` | HGS cannot certify infeasibility. |

The domain's `infeasible` state requires `feasible=False`, but this HGS adapter
never infers that state from heuristic failure. `pending` and `running` remain
domain lifecycle values; the synchronous service returns terminal results.
HGS emits no explicit feasibility boolean. The source audit shows that
`Genetic::run` selects and updates its incumbent from feasible solutions.
Since the 2026-09-11 extension, a successful result must also pass independent
input-based verification. `feasible=True` refers to the audited HGS atomic
arrival-event model and explicit route budgets, not a continuous physical
service schedule or a proof of optimality.

## Parsing and deterministic validation

`parse_hgs_output(text)` is pure and never reads paths. It requires unique
objective/dissatisfaction/emission metrics, CPU time, and nonempty truck and
repairer sections. Each `=======` starts a route; routes receive zero-based
IDs in output order because HGS does not print vehicle IDs. Stops preserve
station IDs (`0` is the depot), the four load/unload quantities, or repaired
bikes. Malformed operations, missing metrics, duplicate records, truncated
blocks, non-finite numbers and unrecognized text fail parsing. CRLF and
scientific numeric notation are accepted. Optional aggregate travel/operation
metrics and per-station dissatisfaction are JSON-compatible metadata; omission
produces warnings. HGS's printed numeric precision is retained without
claiming extra precision or deriving bound/gap.

`validate_result(scenario, result, run_dir=...)` revalidates domain fields and
checks station ranges, unique/in-range truck and repairer IDs, nonnegative
integer operations, route/resource counts, depot endpoints, and successful
metric/feasibility consistency. Current HGS must provide one block per resource,
including idle `[0, 0]` routes. Truck inventory starts empty before depot
loading; unloads cannot exceed available usable/broken inventory, and combined
inventory after each stop cannot exceed capacity. The final depot must empty
the truck. These are stop-level inventory checks, without inventing within-stop
loading timestamps. Artifact paths must be absolute existing files inside the
specified run. HGS bound/gap must always be null.

For successful HGS candidates these checks are followed by independent
verification described below. The service checks constraints before dispatch
and validates the returned backend/run identity and result; direct
`HgsBackend.solve` also verifies before persisting `solution.json`. Verification
is always recomputed, even if candidate metadata already claims `passed`.
Reports/warnings are idempotent, so the service result matches the persisted
result. Revalidating historical output returns an updated object in memory;
the validator does not overwrite historical files.

An explicitly injected `HgsBackend` must use equal `Settings` values to its
`SolverService`. Mismatched configuration raises `ValueError` during service
construction, before input copying or solver execution. This prevents a run
from being executed under one configuration and validated under another.

## Independent mathematical verification (`hgs-math-v1`)

`services/hgs_instance.py` reads only fixed paths in this run: `scenario.json`,
the station table, time matrix and one dissatisfaction table per station.
It checks that the saved scenario equals the supplied scenario and verifies the
recorded `input_sha256` values against the snapshot bytes. Missing files/hashes,
changed inputs, symlink files/directories, malformed UTF-8, invalid headers,
wrong row/column counts, non-finite/negative table values and invalid initial
inventories reject verification. The time matrix includes depot 0, must have
zero diagonal and may be asymmetric. Station-table row order defines internal
station IDs 1..N; external IDs are preserved in the report. Dissatisfaction
tables are `(capacity + 1)` square arrays indexed `[usable][broken]`.

When `broken_bike_proportion=None`, `curBroken` is used. Otherwise the reader
matches HGS's double multiplication and `ceil`: broken inventory is zero if
usable exceeds target; otherwise it is
`min(ceil(proportion * (target - usable)), capacity - usable)`. Four-column
station tables are accepted only in proportion mode. Priority tables affect
HGS's search, not the independent candidate calculation, and are not parsed
by the verifier. File availability remains a preflight check; full mathematical
input verification runs after a candidate is available.

`services/hgs_verification.py` reconstructs each route from time zero, with no
waiting. Truck travel uses the directed time matrix; repairer travel is 1.68
times that matrix. Truck service is `loading_time_sec` times all loaded and
unloaded quantities; repair service is `repair_time_sec` times repaired bikes.
Initial depot loading and final depot unloading are included. Each resource's
final completion time must not exceed `operation_time_budget_sec` (absolute
floating arithmetic allowance `1e-7` seconds). The depot supplies usable bikes,
receives returned bikes, and cannot supply broken bikes or perform repairs in
this model.

Station inventories are updated atomically at reconstructed arrival times,
matching `Individual::feasibilityCheckOfSolution`. Every intermediate usable
and broken inventory must be nonnegative and their sum must not exceed station
capacity. All resources share the same station state; final feasibility cannot
hide an earlier deficit. Per-station event groups whose neighboring arrival
times differ by at most `1e-7` seconds are conservatively treated as simultaneous.
The verifier preserves each resource's stop order and bounds all interleavings
of different resources using per-resource prefix minima/maxima. It accepts a
group only when **every** such ordering respects inventory bounds. If final
inventory is valid but an intermediate ordering could violate a bound, the
candidate fails verification with an explicit ambiguous-order diagnostic;
there is no invented solver tie-break or inferred problem infeasibility.

For each truck leg, the previous station's departure load determines emission:

```text
leg_emission = 2.61 * (0.252 + 0.0003 * departure_load)
              * truck_travel_seconds / 60 * 0.42
dissatisfaction = sum(table_i[final_usable_i][final_broken_i])
objective = 2 * dissatisfaction + 0.06 * emission
            + 1e-8 * (truck_travel + repair_travel + truck_service + repair_service)
```

The HGS capacity-violation penalty is zero for independently valid candidates;
invalid inventory is rejected before scoring. Main metrics must match these
recomputations. Optional reported aggregate timings and per-station
dissatisfaction are compared when present; absent optional reports produce
warnings, while their values are still recomputed. A supplied per-station report
must cover exactly the instance IDs.

HGS's `ofstream` prints six significant digits. Comparisons allow half a unit
at that precision, plus 64 ULPs of the recomputed number for binary arithmetic;
this is not a broad percentage tolerance. Original printed values remain in
`SolutionResult`; higher-precision recomputations and each tolerance are stored
separately in the validation report. This does not create a mathematical bound
or gap. Runtime remains an observed solver/process metric, not a quantity
derived from the route model.

The report explicitly limits verification to HGS's atomic-at-arrival model.
It does not simulate individual bike transfers during loading, availability
only after a repair finishes, traffic uncertainty, waiting decisions or
optimality. Missing or inconsistent evidence fails verification; it never
silently falls back to structural-only success.

## Verification scope

The stable fixture `tests/fixtures/hgs/6_1.txt` is copied from actual HGS output
dated 2026-08-05, with provenance recorded beside it. Unit tests use that text,
mutated malformed/multi-resource variants, mocks and a temporary Python fake
executable. One real fake-child timeout checks that the child is reaped and
partial stdout/stderr survive. Ordinary pytest never invokes the real solver.

Real HGS `6_1` acceptance passed on 2026-09-10 through `SolverService`, with
the unchanged example's 5-second internal and a 10-second external limit.
Run `hgs-6-1-20260910T150133Z-9fffd7ee` returned exit 0, `succeeded`,
`feasible=True`, objective `104.04`, dissatisfaction `51.7808`, emission
`7.96796`, and reported runtime `1.03258` seconds. It stopped before the upper
limit; no objective equality across runs is expected. Both route types, finite
metrics, null bound/gap, original bytes, JSON round-trip and same-ID rejection
without changed file hashes were verified. Local evidence is under `runs/`,
including `stage3-smoke-acceptance.json`. It is ignored by Git. This first run
covered one truck and one repairer; later real multi-resource acceptance is
recorded below. Larger-instance and Gurobi integration are not covered.

On 2026-09-11 the new mathematical verifier also accepted that historical run,
without changing any original files. A fresh 5-second/10-second real run,
`hgs-math-6-1-20260911T135733Z-6df16b93`, passed through the full updated service:
reported objective `104.042` versus recomputed `104.04162346259199`, dissatisfaction
`51.7808` versus `51.780823999999996`, emission `7.99806` versus
`7.998057169199999`, and observed runtime `1.39315` seconds. Truck completion
`5537.1` and repairer completion `3666.144` seconds both satisfy the 7200-second
budget. The report includes 13 successful numeric comparisons and all six
station timelines. JSON round-trip, verification idempotence and same-ID
rejection without file changes also passed. Evidence is in that run and
`runs/hgs-math-development-20260911T134914Z/`.

The 214-test suite additionally uses eight small actual mathematical input
fixtures (76,148 bytes total) and an independently hand-calculated two-station
case. It covers input integrity/shape failures, ratio-derived inventory,
intermediate inventory violations, simultaneous-event ambiguity, multiple
active trucks sharing inventory, route-budget boundaries including final depot
service, objective/emission/time corruption, and printing tolerances. Fake
process tests confirm rejected candidates retain raw output and a failed
`validation.json`; ordinary pytest still never starts the real HGS executable.

The 2026-09-11 pre-commit review added a fail-before-execution regression for
injected backend configuration mismatch and eight real `6_1` runs. All eight
passed the full service contract with five-second internal and ten-second
external limits. Configurations cover one/two trucks, one/two repairers,
explicit proportions 0/0.3/1, and a 3600-second operation budget. The shorter
budget run used both trucks, including nonzero operations at shared station 3;
the two-hour two-of-each run used both repairers. Idle route blocks were also
observed and verified. Each result passed 13 numeric comparisons, route budgets,
station timelines, input hashes, raw-byte preservation, JSON round-trip,
verification idempotence, and same-ID refusal without file changes. See
[stage3-acceptance.md](stage3-acceptance.md) for exact configurations, observed
values, review findings and evidence locations. These runs establish bounded
integration acceptance, not solution-quality benchmarks or exhaustive coverage.

## 阶段 5 应用层补充

现有 backend 的约束和求解契约继续适用。`SolverService` 已增加无副作用的
`assess()`、带理由的 `select()` 和通过现有执行路径求解的 `solve_selected()`；
`ScenarioAnalysisService.compare()` 从保存的运行独立重验后做同模型情景分析。
能力兼容不代表环境已就绪，跨模型不产生指标差值。详见[能力选择与情景分析](scenario-analysis.md)。
