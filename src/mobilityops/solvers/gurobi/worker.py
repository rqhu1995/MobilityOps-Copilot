"""Private subprocess worker. Stdlib until licensing output has been silenced."""

import hashlib
import json
import math
import os
from pathlib import Path
import sys
import time
from types import SimpleNamespace


def main() -> None:
    # Never echo Python/native diagnostics or exception messages from licensing.
    with open(os.devnull, "wb") as sink:
        os.dup2(sink.fileno(), 1)
        os.dup2(sink.fileno(), 2)
    sys.dont_write_bytecode = True
    run_dir = Path.cwd().parent
    phase = "request"

    def write(name: str, value: object) -> None:
        path = run_dir / name
        temporary = path.with_suffix(path.suffix + ".tmp")
        temporary.write_text(json.dumps(value, allow_nan=False, indent=2) + "\n")
        temporary.replace(path)

    def progress(name: str) -> None:
        write("worker-phase.json", {"phase": name})

    try:
        request = json.loads((run_dir / "request.json").read_bytes())
        repo = Path(request["repo"])
        for name, digest in request["source_sha256"].items():
            if hashlib.sha256((repo / name).read_bytes()).hexdigest() != digest:
                raise ValueError("Source changed")
        phase = "license"
        progress(phase)
        import gurobipy as gp

        with gp.Env(empty=True) as env:
            env.setParam("OutputFlag", 0)
            env.setParam("LogToConsole", 0)
            env.setParam("LogFile", "")
            env.start()
            phase = "import_builders"
            progress(phase)
            sys.path.insert(0, str(repo))
            sys.argv = request["solver_argv"]
            from model.variables import addRouteVars, addInventoryVars, addAuxiliaryVars, addLoadingQuantityVars, addObjVars
            from model.constraints import addRouteConstraints, addLoadingQuantityConstraints, addInventoryConstraints, addTimeConstraints, addAuxiliaryConstraints
            from model.objective import addObjective

            imported = {name: str(Path(sys.modules[name].__file__).resolve()) for name in (
                "model", "model.variables", "model.constraints", "model.objective", "parameters",
                "parameters.config", "parameters.sets", "parameters.dataloader",
            )}
            if any(not Path(path).is_relative_to(repo.resolve()) for path in imported.values()):
                raise ValueError("Unexpected builder module")
            with gp.Model("mobilityops", env=env) as model:
                phase = "build"
                progress(phase)
                started = time.monotonic()
                solver = SimpleNamespace(m=model)
                for builder in (addRouteVars, addInventoryVars, addAuxiliaryVars, addLoadingQuantityVars, addObjVars,
                                addRouteConstraints, addLoadingQuantityConstraints, addInventoryConstraints, addTimeConstraints, addAuxiliaryConstraints, addObjective):
                    builder(solver)
                model.update()
                build_sec = time.monotonic() - started
                model.Params.TimeLimit = request["runtime_sec"]
                model.Params.Threads = request["threads"]
                model.Params.MIPGap = 1e-5
                model.Params.FeasibilityTol = 1e-6
                model.Params.IntFeasTol = 1e-5
                phase = "optimize"
                progress(phase)
                model.optimize()
                phase = "save"
                progress(phase)

                def finite_attr(name: str) -> float | None:
                    try:
                        value = float(getattr(model, name))
                        return value if math.isfinite(value) and abs(value) < gp.GRB.INFINITY else None
                    except (gp.GurobiError, AttributeError):
                        return None

                report = {
                    "format": "gurobi_raw_v1", "status": int(model.Status), "solution_count": int(model.SolCount),
                    "runtime_sec": float(model.Runtime), "build_sec": build_sec,
                    "best_bound": finite_attr("ObjBound"), "objective_value": None, "optimality_gap": None,
                    "variables": {}, "gurobi_version": list(gp.gurobi.version()), "imported_modules": imported,
                    "model_variables": int(model.NumVars), "model_constraints": int(model.NumConstrs),
                    "feasibility_tolerance": 1e-6, "integer_tolerance": 1e-5,
                }
                if model.SolCount:
                    model.write(str(run_dir / "native.sol"))
                    report.update(objective_value=finite_attr("ObjVal"), optimality_gap=finite_attr("MIPGap"),
                                  variables={var.VarName: float(var.X) for var in model.getVars()})
                write("worker-result.json", report)
                progress("completed")
    except Exception as exc:
        # Only allowlisted error type and numeric code; never exception text.
        error = {"worker_error": type(exc).__name__, "phase": phase}
        if isinstance(getattr(exc, "errno", None), int):
            error["code"] = exc.errno
        write("worker-result.json", error)


if __name__ == "__main__":
    main()
