# Databricks notebook source
# MAGIC %md
# MAGIC # Register the route solver as a UC MLflow PyFunc model
# MAGIC
# MAGIC Packages `src/solver_core.py` + `src/solver_endpoint_model.py` into a Unity
# MAGIC Catalog registered model for Model Serving. OR-Tools lives in the serving
# MAGIC environment — not the SQL warehouse.

# COMMAND ----------

# MAGIC %pip install ortools==9.14.6206 mlflow pandas numpy --quiet
# MAGIC dbutils.library.restartPython()

# COMMAND ----------

dbutils.widgets.text("catalog", "supplychain", "Catalog")
dbutils.widgets.text("schema", "route_optimizer_accelerator", "Schema")
dbutils.widgets.text("solver_model_name", "", "Registered model (blank = catalog.schema.route_solver)")
dbutils.widgets.text("solver_model_alias", "champion", "Model alias")

CATALOG = dbutils.widgets.get("catalog")
SCHEMA = dbutils.widgets.get("schema")
SOLVER_MODEL_NAME = (
    dbutils.widgets.get("solver_model_name").strip()
    or f"{CATALOG}.{SCHEMA}.route_solver"
)
SOLVER_MODEL_ALIAS = dbutils.widgets.get("solver_model_alias").strip() or "champion"

# COMMAND ----------

import json
import sys
import time
from pathlib import Path, PurePosixPath

import mlflow
import pandas as pd
from mlflow.models.signature import ModelSignature
from mlflow.tracking import MlflowClient
from mlflow.types.schema import ColSpec, Schema

try:
    notebook_path = (
        dbutils.notebook.entry_point.getDbutils()
        .notebook()
        .getContext()
        .notebookPath()
        .get()
    )
    bundle_root = str(PurePosixPath(notebook_path).parent.parent)
    candidates = [bundle_root]
    if not bundle_root.startswith("/Workspace/"):
        candidates.append(f"/Workspace{bundle_root}")
    for candidate in candidates:
        src_dir = str(PurePosixPath(candidate) / "src")
        if src_dir not in sys.path:
            sys.path.insert(0, src_dir)
        if candidate not in sys.path:
            sys.path.append(candidate)
except Exception:
    candidates = ["."]
    sys.path.insert(0, "src")

from solver_endpoint_model import INPUT_COLUMNS, RouteSolverModel, make_input_row

# COMMAND ----------

sample_stops = [
    {"stop_id": "1", "lat": 29.7700, "lon": -95.3600, "demand": 10, "ready_minute": 0, "due_minute": 480},
    {"stop_id": "2", "lat": 29.7500, "lon": -95.3800, "demand": 20, "ready_minute": 0, "due_minute": 480},
    {"stop_id": "3", "lat": 29.7800, "lon": -95.3500, "demand": 15, "ready_minute": 0, "due_minute": 480},
]
input_example = pd.DataFrame(
    [
        make_input_row(
            sample_stops,
            depot_lat=29.7604,
            depot_lon=-95.3698,
            vehicle_count=2,
            vehicle_capacity=100,
            max_route_minutes=480,
            avg_speed_kph=50.0,
            service_minutes=10,
            solver_seconds=3,
            drop_penalty=1_000_000,
        )
    ]
)

started = time.perf_counter()
sample_output = RouteSolverModel().predict(None, input_example)
solve_time_ms = (time.perf_counter() - started) * 1000.0
plan = json.loads(sample_output.iloc[0]["plan_json"])
served = sum(1 for row in plan if not row["is_dropped"])
dropped = sum(1 for row in plan if row["is_dropped"])

signature = ModelSignature(
    inputs=Schema(
        [
            ColSpec("string", "stops_json"),
            ColSpec("double", "depot_lat"),
            ColSpec("double", "depot_lon"),
            ColSpec("long", "vehicle_count"),
            ColSpec("long", "vehicle_capacity"),
            ColSpec("long", "max_route_minutes"),
            ColSpec("double", "avg_speed_kph"),
            ColSpec("long", "service_minutes"),
            ColSpec("long", "solver_seconds"),
            ColSpec("long", "drop_penalty"),
        ]
    ),
    outputs=Schema([ColSpec("string", "plan_json")]),
)

assert list(input_example.columns) == INPUT_COLUMNS
print(
    {
        "sample_solve_time_ms": solve_time_ms,
        "served": served,
        "dropped": dropped,
        "plan_rows": len(plan),
    }
)

# COMMAND ----------

mlflow.set_registry_uri("databricks-uc")
code_root = next(
    (
        c
        for c in candidates
        if (Path(c) / "src" / "solver_core.py").exists()
        or str(c).startswith("/Workspace/")
    ),
    candidates[0],
)
# Pass individual modules so they land at code/solver_*.py and import as
# ``solver_core`` / ``solver_endpoint_model`` (not ``src.solver_core``).
code_paths = [
    str(PurePosixPath(code_root) / "src" / "solver_core.py"),
    str(PurePosixPath(code_root) / "src" / "solver_endpoint_model.py"),
]

with mlflow.start_run(run_name="register-route-solver-pyfunc") as run:
    model_kwargs = {
        "artifact_path": "route_solver",
        "python_model": RouteSolverModel(),
        "pip_requirements": [
            "mlflow==2.22.5",
            "cloudpickle==3.0.0",
            "pandas==2.2.3",
            "numpy<2",
            "ortools==9.14.6206",
            "protobuf",
            "absl-py",
        ],
        "signature": signature,
        "input_example": input_example,
        "registered_model_name": SOLVER_MODEL_NAME,
    }
    try:
        model_info = mlflow.pyfunc.log_model(code_paths=code_paths, **model_kwargs)
    except TypeError as exc:
        if "code_paths" not in str(exc):
            raise
        model_info = mlflow.pyfunc.log_model(code_path=code_paths, **model_kwargs)

    mlflow.log_params(
        {
            "catalog": CATALOG,
            "schema": SCHEMA,
            "solver": "ortools_cvrptw",
            "model_name": SOLVER_MODEL_NAME,
            "model_alias": SOLVER_MODEL_ALIAS,
            "ortools": "9.14.6206",
        }
    )
    mlflow.log_metrics(
        {
            "sample_solve_time_ms": solve_time_ms,
            "sample_served_stops": float(served),
            "sample_dropped_stops": float(dropped),
        }
    )
    mlflow.log_dict(input_example.iloc[0].to_dict(), "inputs/sample_request.json")
    mlflow.log_dict({"plan": plan}, "results/sample_plan.json")

    # Pre-deployment validation: simulate serving env and serving payload.
    model_uri = f"runs:/{run.info.run_id}/route_solver"
    try:
        mlflow.models.predict(
            model_uri=model_uri,
            input_data=input_example,
            env_manager="virtualenv",
            install_mlflow=False,
        )
        print("mlflow.models.predict OK")
    except Exception as exc:
        # virtualenv rebuild can be flaky on some runtimes; fall back to local load.
        print(f"mlflow.models.predict skipped/failed ({exc}); validating via load_model")
        loaded = mlflow.pyfunc.load_model(model_uri)
        loaded_out = loaded.predict(input_example)
        assert "plan_json" in loaded_out.columns
        assert len(json.loads(loaded_out.iloc[0]["plan_json"])) == len(plan)

client = MlflowClient()
registered_version = getattr(model_info, "registered_model_version", None)
if registered_version is None:
    versions = client.search_model_versions(f"name = '{SOLVER_MODEL_NAME}'")
    registered_version = max(int(v.version) for v in versions)
client.set_registered_model_alias(SOLVER_MODEL_NAME, SOLVER_MODEL_ALIAS, str(registered_version))

print(
    f"Registered {SOLVER_MODEL_NAME} version {registered_version} "
    f"as @{SOLVER_MODEL_ALIAS}"
)

dbutils.jobs.taskValues.set("solver_model_name", SOLVER_MODEL_NAME)
dbutils.jobs.taskValues.set("solver_model_alias", SOLVER_MODEL_ALIAS)
dbutils.jobs.taskValues.set("solver_model_version", str(registered_version))
