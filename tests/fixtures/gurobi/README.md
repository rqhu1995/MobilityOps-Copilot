# Gurobi synthetic candidate fixture

`native.sol` is the unmodified 165-variable Gurobi solution from the 2026-09-13
stage-4A probe at `runs/stage4a-audit-20260913T082102Z-eaf16585/tiny-probes/`.
It was generated with the audited original builders, using one synthetic station,
four 600-second periods, one truck of capacity 3, and one repairer. Initial
station usable/broken inventory is 1/1, target 2, capacity 3; both nonzero directed
travel times are 60 seconds. Per-bike loading/repair times are 60/300 seconds.
The synthetic dissatisfaction plane is `F >= -2*usable + 3*broken + 10`.

`worker-result.json` is an equivalent envelope built from these original values
and observed SDK attributes. `build_sec=0` is a placeholder, not a measurement;
the test fills `imported_modules` with the isolated fake repository paths.
The hand-recomputed objective is 8.0331762396. No solver source is included.
Ordinary tests use this data with a fake Python executable, never real Gurobi.
