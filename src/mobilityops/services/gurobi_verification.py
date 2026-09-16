"""Independent algebraic verification of the audited Gurobi period model.

Evaluates candidate numbers against snapshot-derived equations. It never imports
Gurobi, builds an optimization model, or uses the solver's own violation report.
"""

from itertools import product
import math

from mobilityops.domain import ScenarioSpec
from mobilityops.services.gurobi_inputs import GurobiInput
from mobilityops.solvers.gurobi.contracts import GurobiOptions
from mobilityops.solvers.gurobi.result import RawGurobiResult


MODEL_WARNING = (
    "Verified against the Gurobi time-indexed linear model, including same-period inventory aggregation. "
    "Routes are per-period projections, not continuous arrival schedules. Bounds and gaps apply only "
    "to this model, not HGS or physical within-service feasibility."
)


def verify_candidate(s: ScenarioSpec, options: GurobiOptions, data: GurobiInput, raw: RawGurobiResult) -> dict:
    nodes = range(s.number_of_stations + 1)
    stations = range(1, s.number_of_stations + 1)
    trucks, repairers = range(s.number_of_trucks), range(s.number_of_repairers)
    horizon = int(s.operation_time_budget_sec / options.time_interval_sec)
    periods = range(1, horizon + 1)
    tau, cap = options.time_interval_sec, s.truck_capacity
    travel = data.travel
    man = tuple(tuple(math.ceil(x * 1.68) for x in row) for row in travel)
    steps = tuple(tuple(max(1, math.ceil(x / tau)) for x in row) for row in travel)
    man_steps = tuple(tuple(max(1, math.ceil(x / tau)) for x in row) for row in man)
    values = raw.variables
    checks = 0
    max_residual = 0.0
    expected: set[str] = set()

    def check(label: str, left: float, right: float = 0.0, equality: bool = True) -> None:
        nonlocal checks, max_residual
        checks += 1
        if not math.isfinite(left) or not math.isfinite(right):
            raise ValueError(f"Non-finite recomputation: {label}")
        residual = abs(left - right) if equality else max(left - right, 0.0)
        tolerance = 1e-6 + 64 * (math.ulp(left) + math.ulp(right))
        max_residual = max(max_residual, residual)
        if residual > tolerance:
            raise ValueError(f"Gurobi constraint {label}: residual {residual} exceeds {tolerance}")

    def v(name: str, *key: int) -> float:
        return values[f"{name}[{','.join(map(str, key))}]"]

    domains = [
        ("visit_truck", (nodes, nodes, periods, trucks), "binary"),
        ("visit_man", (nodes, nodes, periods, repairers), "binary"),
        *((name, (nodes, periods), "real") for name in ("station_inv_usable", "station_inv_broken")),
        *((name, (nodes, nodes, periods, trucks), "real") for name in ("prev_truck_inv_usable", "prev_truck_inv_broken", "emission")),
        *((name, (nodes, periods, trucks), "integer") for name in ("reb_amount_usable+", "reb_amount_usable-", "reb_amount_broken+", "reb_amount_broken-")),
        ("repaired_amount_man", (nodes, periods, repairers), "integer"),
        *((name, (periods, trucks), "real") for name in ("upper_bound", "lower_bound")),
        *((name, (periods, repairers), "real") for name in ("upper_bound_man", "lower_bound_man")),
        ("stop_time", (trucks,), "integer"), ("stop_time_rpm", (repairers,), "integer"),
        ("aux_stop", (periods, trucks), "binary"), ("aux_stop_rpm", (periods, repairers), "binary"),
        ("operating_time_truck", (trucks,), "real"), ("operating_time_rpm", (repairers,), "real"),
        ("dissat", (stations,), "real"),
    ]
    for name, dimensions, kind in domains:
        for key in product(*dimensions):
            variable = f"{name}[{','.join(map(str, key))}]"
            expected.add(variable)
            if variable not in values:
                raise ValueError(f"Missing Gurobi variable: {variable}")
            value = values[variable]
            check(variable + " lower bound", -value, equality=False)
            if kind != "real" and abs(value - round(value)) > 1e-5:
                raise ValueError(f"Non-integral Gurobi variable: {variable}")
            if kind == "binary":
                check(variable + " upper bound", value, 1, equality=False)
    if set(values) != expected or raw.model_variables != len(expected):
        raise ValueError("Unexpected Gurobi variable set or model size")

    def operation(name: str, i: int, t: int, k: int) -> float:
        return v("reb_amount_" + name, i, t, k)

    def outgoing(name: str, i: int, t: int, k: int) -> float:
        return sum(v(name, i, j, t, k) for j in nodes)

    def incoming(name: str, i: int, t: int, k: int, grid: tuple) -> float:
        return sum(v(name, j, i, t - grid[j][i], k) for j in nodes if t > grid[j][i])

    station_report = []
    for i in nodes:
        timeline = []
        for t in periods:
            repaired = sum(v("repaired_amount_man", i, t, r) for r in repairers)
            for color, name, initial, sign in (
                ("usable", "station_inv_usable", data.usable[i], 1),
                ("broken", "station_inv_broken", data.broken[i], -1),
            ):
                before = initial if t == 1 else v(name, i, t - 1)
                delta = sum(operation(color + "+", i, t, k) - operation(color + "-", i, t, k) for k in trucks)
                check(f"station {i}/{t} {color} balance", v(name, i, t), before + delta + sign * repaired)
            if i:
                check(f"station {i}/{t} capacity", v("station_inv_usable", i, t) + v("station_inv_broken", i, t), data.capacity[i], False)
            timeline.append({"period": t, "usable": v("station_inv_usable", i, t), "broken": v("station_inv_broken", i, t)})
        station_report.append({"station_id": i, "timeline": timeline})

    for k in trucks:
        check("initial truck usable", outgoing("prev_truck_inv_usable", 0, 1, k), operation("usable-", 0, 1, k) - operation("usable+", 0, 1, k))
        for i, j, t in product(nodes, nodes, periods):
            u, b = v("prev_truck_inv_usable", i, j, t, k), v("prev_truck_inv_broken", i, j, t, k)
            arc = v("visit_truck", i, j, t, k)
            check("truck usable flow capacity", u, cap * arc, False)
            check("truck broken flow capacity", b, cap * arc, False)
            check("truck combined capacity", u + b, cap, False)
            if t == 1:
                check("initial truck broken", b)
            if t == horizon:
                check("terminal truck usable", u); check("terminal truck broken", b)
            check("arc emission", v("emission", i, j, t, k), 2.61 * (.252 * arc + .0003 * (u + b)) * travel[i][j] / 3600 * 25.2)
        for i, t in product(nodes, periods):
            active = outgoing("visit_truck", i, t, k)
            limit = min(data.capacity[i], cap)
            check("usable unload presence", operation("usable+", i, t, k), active * limit, False)
            if i:
                check("usable load presence", operation("usable-", i, t, k), active * limit, False)
                check("no broken station unloading", operation("broken+", i, t, k))
                check("broken load presence", operation("broken-", i, t, k), active * min(data.broken[i], cap), False)
            else:
                check("broken depot unload presence", operation("broken+", i, t, k), active * cap, False)
                check("no broken depot loading", operation("broken-", i, t, k))
            if t > 1:
                for color, name in (("usable", "prev_truck_inv_usable"), ("broken", "prev_truck_inv_broken")):
                    check("truck inventory conservation", outgoing(name, i, t, k), incoming(name, i, t, k, steps) - operation(color + "+", i, t, k) + operation(color + "-", i, t, k))
    for i in stations:
        check("initial usable surplus limit", sum(operation("usable-", i, t, k) for t, k in product(periods, trucks)), max(data.usable[i] - data.target[i], 0), False)
        check("initial broken total limit", sum(operation("broken-", i, t, k) for t, k in product(periods, trucks)) + sum(v("repaired_amount_man", i, t, r) for t, r in product(periods, repairers)), data.broken[i], False)
        for t, r in product(periods, repairers):
            check("repair presence", v("repaired_amount_man", i, t, r), outgoing("visit_man", i, t, r) * data.broken[i], False)
        entrances = sum(v("visit_man", j, i, t - man_steps[j][i], r) for j, t, r in product(nodes, periods, repairers) if i != j and t > man_steps[j][i])
        check("single repairer station entry", entrances, 1, False)
    for t, r in product(periods, repairers):
        check("no depot repair", v("repaired_amount_man", 0, t, r))

    route_report = []
    totals = dict(truck_travel_sec=0.0, repair_travel_sec=0.0, truck_service_sec=0.0, repair_service_sec=0.0)
    # Verify both resource families with common route/time equations, then their
    # distinct operation contributions. No arrival-time interpretation is added.
    for truck, resources, arc_name, distances, grid in (
        (True, trucks, "visit_truck", travel, steps), (False, repairers, "visit_man", man, man_steps),
    ):
        stop = "stop_time" if truck else "stop_time_rpm"
        aux = "aux_stop" if truck else "aux_stop_rpm"
        upper = "upper_bound" if truck else "upper_bound_man"
        lower = "lower_bound" if truck else "lower_bound_man"
        operating = "operating_time_truck" if truck else "operating_time_rpm"
        for resource in resources:
            def service(t: int, i: int) -> float:
                if truck:
                    return s.loading_time_sec * sum(operation(name, i, t, resource) for name in ("usable+", "usable-", "broken+", "broken-"))
                return s.repair_time_sec * v("repaired_amount_man", i, t, resource)

            check("initial departure", sum(v(arc_name, 0, j, 1, resource) for j in (nodes if truck else stations)), 1)
            check("terminal depot return", sum(v(arc_name, i, 0, horizon - grid[i][0], resource) for i in nodes if horizon > grid[i][0]), 1)
            all_service = sum(service(t, i) for t, i in product(periods, nodes))
            all_travel = sum(distances[i][j] * v(arc_name, i, j, t, resource) for i, j, t in product(nodes, nodes, periods))
            check("total operating time", v(operating, resource), all_service)
            schedule = []
            for t in periods:
                check("one arc per period", sum(v(arc_name, i, j, t, resource) for i, j in product(nodes, nodes)), 1, False)
                if t > 1:
                    for i in nodes:
                        check("route flow conservation", incoming(arc_name, i, t, resource, grid), outgoing(arc_name, i, t, resource))
                a = v(aux, t, resource)
                check("stop indicator lower", (t - v(stop, resource) + 1) / horizon, a, False)
                check("stop indicator upper", a, (t - v(stop, resource) + .5) / horizon + 1, False)
                cumulative_service = sum(service(w, i) for w, i in product(range(1, t + 1), nodes))
                arrived_travel = sum(distances[i][j] * v(arc_name, i, j, w - grid[i][j], resource)
                                     for i, j, w in product(nodes, nodes, range(1, t + 1)) if w > grid[i][j])
                departed_travel = sum(distances[i][j] * v(arc_name, i, j, w, resource) for i, j, w in product(nodes, nodes, range(1, t + 1)))
                check("cumulative upper definition", v(upper, t, resource), arrived_travel + cumulative_service)
                check("cumulative lower definition", v(lower, t, resource), departed_travel + cumulative_service)
                check("period upper budget", v(upper, t, resource), t * tau, False)
                check("period lower budget", (t - 2) * tau - horizon * tau * a, v(lower, t, resource), False)
                check("stopped at depot", a, v(arc_name, 0, 0, t, resource), False)
                if t < horizon:
                    check("stop transition", v(arc_name, 0, 0, t, resource), 1 - (v(aux, t + 1, resource) - a), False)
                for i in stations:
                    operations = service(t, i) / (s.loading_time_sec if truck else s.repair_time_sec)
                    check("station self loop", v(arc_name, i, i, t, resource), operations + 100 * a, False)
                schedule.extend({"period": t, "origin": i, "destination": j} for i, j in product(nodes, nodes) if v(arc_name, i, j, t, resource) > .5)
            family = "truck" if truck else "repair"
            totals[family + "_travel_sec"] += all_travel
            totals[family + "_service_sec"] += all_service
            route_report.append({"resource": family, "resource_id": resource, "travel_sec": all_travel, "service_sec": all_service, "arcs": schedule})

    envelope = []
    for i in stations:
        u, b, f = v("station_inv_usable", i, horizon), v("station_inv_broken", i, horizon), v("dissat", i)
        minimum = max(0.0, *(a * u + beta * b + c for a, beta, c in data.planes[i]))
        check("linear dissatisfaction envelope", minimum, f, False)
        envelope.append({"station_id": i, "reported": f, "envelope": minimum, "slack": f - minimum})
    dissatisfaction = sum(v("dissat", i) for i in stations)
    emission = sum(v("emission", i, j, t, k) for i, j, t, k in product(nodes, nodes, periods, trucks))
    objective = 2 * dissatisfaction + .06 * emission + 1e-8 * sum(totals.values())
    check("reported objective", raw.objective_value, objective)
    if raw.best_bound is not None:
        check("bound exceeds incumbent", raw.best_bound, raw.objective_value, False)
    if raw.optimality_gap is not None:
        if raw.best_bound is None:
            raise ValueError("Gap supplied without a finite bound")
        if raw.objective_value == 0:
            if raw.best_bound != 0 or raw.optimality_gap != 0:
                raise ValueError("Invalid finite zero-objective gap")
        else:
            check("reported gap", raw.optimality_gap, abs(raw.objective_value - raw.best_bound) / abs(raw.objective_value))
    if raw.status == 2 and (raw.optimality_gap is None or raw.optimality_gap > 1e-5 + 1e-8):
        raise ValueError("Optimal status is inconsistent with the configured gap tolerance")
    return {"status": "passed", "verifier": "gurobi-period-v1", "semantics": "gurobi_time_indexed_linear",
            "checks": checks, "maximum_constraint_residual": max_residual,
            "feasibility_tolerance": 1e-6, "integer_tolerance": 1e-5,
            "time_interval_sec": tau, "periods": horizon, "stations": station_report, "routes": route_report,
            "dissatisfaction_envelopes": envelope,
            "recomputed_metrics": {**totals, "dissatisfaction": dissatisfaction, "emission": emission, "objective_value": objective}}
