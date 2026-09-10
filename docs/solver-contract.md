# Domain model and HGS contract

This document records the deterministic contract established in stage 2. It
does not authorize or implement an HGS process invocation.

## ScenarioSpec to HGS mapping

The mapping was checked against
`/home/runqiu/BRPWR-HGSADC-SBC/Program/helpers/Args.cpp`.

| `ScenarioSpec` field | HGS CLI | HGS parser type | Unit and validation | Contract note |
| --- | --- | --- | --- | --- |
| `scenario_name` | — | — | Non-empty string | Orchestration metadata only. |
| `instance_id` | `-i` / `--inst_no` | `int` | Integer `> 0` | Selects the suffix in `Instances/<stations>_<instance>/`. |
| `number_of_stations` | `-ns` / `--num_stations` | `int` | Integer `> 0` | Number of non-depot stations. Filesystem availability is checked later by an adapter. |
| `number_of_trucks` | `-ntrk` / `--num_trucks` | `int` | Integer `> 0` | HGS README documents positive truck counts. |
| `number_of_repairers` | `-nrpm` / `--num_repairer` | `int` | Integer `> 0` | HGS is the repairer-enabled solver; zero is not accepted by this contract. |
| `truck_capacity` | `-vcap` / `--vehicle_capacity` | `int` | Bikes, integer `> 0` | Capacity per truck. |
| `operation_time_budget_sec` | `-tb` / `--time_budget` | `double` | Seconds, finite float `> 0` | HGS accepts a negative sentinel for an automatic default; the contract requires an explicit deterministic value. |
| `solver_runtime_limit_sec` | `-tl` / `--timeLimit` | `double` | Seconds, finite float `> 0` | HGS treats zero as unlimited. The contract rejects unbounded internal runtime; a future adapter must still enforce an external timeout. |
| `loading_time_sec` | `-ldT` / `--loading_time` | `int` | Seconds per bike, integer `> 0` | Used for both loading and unloading operations. |
| `repair_time_sec` | `-rpT` / `--repair_time` | `int` | Seconds per bike, integer `> 0` | On-site repair service time. |
| `broken_bike_proportion` | `-bprop` / `--broken_proportion` | `double` | Optional finite ratio in `[0, 1]` | `None` means omit the flag and preserve `curBroken` from the instance files. A supplied ratio makes HGS derive broken inventory. |
| `seed` | — | — | Optional 32-bit unsigned integer | Current HGS seeds `std::mt19937` from system time and exposes no seed CLI. `HGS_CAPABILITY.supports_seed` is therefore `false`. |
| `objective_profile` | — | — | Only `eudf_default` | HGS objective is compiled into the solver; there is no objective selector CLI. |
| `solution_requirement` | — | — | `fast_feasible` or `best_available` | Orchestration policy only; it must not silently rewrite runtime fields. |

The HGS-only algorithm tuning flags `-pnt`, `-noimp`, `-num_pnt_manage`,
`-fesP`, `-mu`, `-lambda`, and `-edu` intentionally remain backend defaults.
They are not scenario-domain facts and are not exposed in stage 2.

The example uses the real directory
`/home/runqiu/BRPWR-HGSADC-SBC/Instances/6_1`. Its 7200-second operation
budget, 60-second loading time, 300-second repair time, and capacity 25 match
the HGS defaults for this small-instance family. The five-second solver limit
is an explicit smoke-test policy; no solver is invoked in stage 2.

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
