# Route Optimizer — Genie UC Function

End-to-end example: a **Unity Catalog table-valued function** that runs a CVRPTW route solver (Google OR-Tools on CPU) and is callable from **Genie** as a function tool.

## What ships

| Path | Purpose |
|------|---------|
| `src/solver_core.py` | Pure-Python solver source of truth (local unit tests) |
| `functions/solve_routes_udf.sql` | UC Python scalar UDF — carries the `ortools` dependency, returns JSON |
| `functions/optimize_routes_tvf.sql` | SQL TVF wrapper — **the Genie tool** |
| `notebooks/00_setup_synthetic_data.py` | Deterministic demo data (`depot`, `customers`, `parcels`) |
| `notebooks/01_register_functions.py` | Applies the `.sql` files and smoke-tests `optimize_routes` |
| `resources/genie_space.json` | Serialized Genie Space config (tables + `optimize_routes` tool) |
| `databricks.yml` | Asset Bundle — deploy job + workspace variables |

UC Python **UDTFs cannot install OR-Tools**, so the pattern is: scalar UDF (solver + JSON) → SQL TVF (shape input, explode output) → Genie function tool.

## Deploy

```bash
databricks bundle validate -t dev
databricks bundle deploy -t dev
databricks bundle run -t dev deploy
```

Defaults (override with bundle variables): catalog `supplychain`, schema `route_optimizer_accelerator`, profile `DEFAULT`.

## Register the Genie Space

After the deploy job finishes:

```bash
SERIALIZED=$(python3 -c "import json; print(json.dumps(json.load(open('resources/genie_space.json'))))")

databricks genie create-space <WAREHOUSE_ID> "$SERIALIZED" \
  --title "Route Optimizer" \
  --description "Plan parcel-delivery routes via optimize_routes UC function." \
  --profile DEFAULT
```

Update `resources/genie_space.json` if your catalog/schema differ from `supplychain.route_optimizer_accelerator`.

Grant callers `USE CATALOG`, `USE SCHEMA`, `SELECT` on the demo tables, and `EXECUTE` on **both** `solve_routes_json` and `optimize_routes`.

## Test in Genie

```bash
databricks genie start-conversation <SPACE_ID> \
  "Plan delivery routes for depot 1 today with 10 vans." \
  --profile DEFAULT
```

Genie should call:

```sql
SELECT * FROM supplychain.route_optimizer_accelerator.optimize_routes(
  depot_id => 1, vehicle_count => 10
)
```

With only 4–5 vans, many parcels are dropped (`is_dropped = true`) — that is expected with tight time windows in the demo data.

## Local solver tests

```bash
pip install ortools==9.14.6206 numpy
pytest src/tests/test_solver_core.py
```
