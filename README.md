# Route Optimizer — Genie UC Functions

End-to-end example: Unity Catalog table-valued functions that run a CVRPTW route
solver (Google OR-Tools on CPU) and are callable from **Genie** as function tools.

Two equivalent solver surfaces share `src/solver_core.py`:

1. **Python UDF path** — `solve_routes_json` installs `ortools` in the UC UDF environment.
2. **Model Serving + `ai_query` path** — OR-Tools runs on a custom serving endpoint; SQL calls it via `ai_query`, decoupling the warehouse from the solver runtime.

## What ships

| Path | Purpose |
|------|---------|
| `src/solver_core.py` | Pure-Python solver source of truth |
| `src/solver_endpoint_model.py` | MLflow PyFunc wrapper for Model Serving |
| `functions/solve_routes_udf.sql` | UC Python scalar UDF |
| `functions/optimize_routes_tvf.sql` | Genie TVF (Python UDF path) |
| `functions/solve_routes_endpoint_udf.sql` | SQL scalar wrapping `ai_query` |
| `functions/optimize_routes_endpoint_tvf.sql` | Genie TVF (endpoint path) |
| `notebooks/00_setup_synthetic_data.py` | Demo data |
| `notebooks/01_register_functions.py` | Register Python-UDF functions |
| `notebooks/02_register_solver_model.py` | Log + register UC PyFunc model |
| `notebooks/03_deploy_solver_endpoint.py` | Create/update serving endpoint |
| `notebooks/04_register_endpoint_functions.py` | Register `ai_query` functions |
| `notebooks/05_verify_endpoint_sql.py` | Serverless warehouse verification |
| `resources/deploy.job.yml` | Deploy Python-UDF path |
| `resources/endpoint_test.job.yml` | Deploy + verify endpoint path |
| `resources/genie_space.json` | Genie Space for `optimize_routes` |
| `resources/genie_space_endpoint_test.json` | Genie Space for `optimize_routes_via_endpoint` |
| `databricks.yml` | Asset Bundle |

## Deploy (Python UDF path)

```bash
databricks bundle validate -t dev
databricks bundle deploy -t dev
databricks bundle run -t dev deploy
```

## Deploy (Model Serving + ai_query path)

Requires a **serverless SQL warehouse** (`ai_query` is not available on Classic/Pro warehouses).

```bash
databricks bundle deploy -t dev
databricks bundle run -t dev endpoint_test
```

Then create the endpoint Genie Space:

```bash
SERIALIZED=$(python3 -c "import json; print(json.dumps(json.load(open('resources/genie_space_endpoint_test.json'))))")

databricks genie create-space <WAREHOUSE_ID> "$SERIALIZED" \
  --title "Route Optimizer (Endpoint)" \
  --description "Plan routes via optimize_routes_via_endpoint (ai_query → Model Serving)." \
  --profile DEFAULT
```

Ask Genie:

```bash
databricks genie start-conversation <SPACE_ID> \
  "Plan delivery routes for depot 1 today with 10 vans." \
  --profile DEFAULT
```

Genie should call:

```sql
SELECT * FROM supplychain.route_optimizer_accelerator.optimize_routes_via_endpoint(
  depot_id => 1, vehicle_count => 10
)
```

## Grants

Python UDF path: `EXECUTE` on `solve_routes_json` and `optimize_routes`.

Endpoint path: `EXECUTE` on `solve_routes_endpoint_json` and `optimize_routes_via_endpoint`, plus the function definer needs **`CAN QUERY`** on the Model Serving endpoint.

## Local tests

```bash
pip install ortools==9.14.6206 numpy pandas
pytest src/tests/
```
