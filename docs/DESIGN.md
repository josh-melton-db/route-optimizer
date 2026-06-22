# Route Optimizer Accelerator — Design

**Drag-and-drop mathematical solvers in Lakeflow Designer, demonstrated with parcel-delivery route optimization on Google OR-Tools (CPU).**

Status: approved design, implementation in progress.
Workspace: `fevm-supply-chain-demo` (`https://fevm-supply-chain-demo.cloud.databricks.com`).
Target UC location: catalog `supply_chain`, schema `route_optimizer_accelerator` (already created).
Serverless SQL warehouse: `Serverless Starter Warehouse` (`0637298ed28a3c81`).

---

## 1. Goal

Show how to add a **custom mathematical-solver operator** to Lakeflow Designer that an
analyst drags onto the canvas, wires an input table into, configures with side-panel
widgets, and runs to produce results — using **parcel-delivery route optimization** as the
worked example. The solver is **Google OR-Tools** (CPU), chosen over NVIDIA cuOpt
specifically because it runs on commodity CPU compute with no GPU requirement.

The accelerator ships three things that share one solver core:

1. A Lakeflow Designer custom operator — **"Route Optimizer"**.
2. A guided demo flow: synthetic parcel data → Delta tables → urgent stops → the
   configured operator solving a CVRPTW → an optimized-routes table. Packaged as a
   Databricks Asset Bundle (DAB).
3. A **route-optimization Table-Valued Function (TVF)** in Unity Catalog, registered as a
   **Genie Space tool** so an analyst can request a route plan in natural language.

---

## 2. Locked design decisions

These are settled. Do not reopen them during implementation.

| # | Decision | Choice |
|---|----------|--------|
| 1 | Routing problem | **Plain CVRPTW** — capacitated vehicle routing with time windows. Stops are independent (no pickup→delivery precedence pairs / PDPTW). |
| 2 | Distance/cost | **Haversine** great-circle distance → travel minutes via an average-speed constant. OSRM road-network distances are documented as an optional upgrade only (a pointer, not implemented). |
| 3 | Operator code packaging | **Inline** `run_function.code` in the operator YAML. Self-contained; no external module import required at operator runtime. |
| 4 | MLflow / model serving | **Out of scope.** No PyFunc wrapper, no served endpoint. |
| 5 | Domain theme | **Parcel deliveries** throughout (depot, parcels/stops with demand + due-by, a fleet of delivery vans). |

---

## 3. Grounding (reference repos)

All under `/home/sandbox-agent/workspace/` as read-only references. Do **not** modify them.

- **`gas-tank-delivery`** — the primary template. A working OR-Tools CVRPTW on Databricks:
  deterministic synthetic data (`random.seed(42)`), haversine→travel-minutes matrix,
  `pywrapcp.RoutingIndexManager` + `RoutingModel`, capacity dimension
  (`AddDimensionWithVehicleCapacity`), time dimension (`AddDimension` + per-node
  `CumulVar(...).SetRange`), drop-penalty disjunctions (`AddDisjunction`), and
  `GUIDED_LOCAL_SEARCH` + `PARALLEL_CHEAPEST_INSERTION`. Pinned `ortools==9.14.6206`.
  Reuse its solver construction and data-gen structure; re-theme tanks→parcels.
- **`cuOpt-dais/or-ops`** — lifecycle pattern reference only. **Not used** here (it is GPU
  cuOpt + MLflow, both out of scope).
- **`dynamic-dispatch-control-tower`, `routing`, `gpu-routing`** — context only; not used
  for this build (dynamic insertion, OSRM, and GPU respectively).

Note: OR-Tools is a **custom pip dependency**. Unity Catalog Python **UDTFs cannot install
custom dependencies** (only base-runtime libs like scikit-learn — which is why the
Designer k-means *uc-udtf* tutorial works but does not carry over to OR-Tools). Two surfaces
*can* declare `ortools`: the Designer **`python-run-function`** operator (`environment`
block) and a UC Python **scalar UDF** (`ENVIRONMENT` clause). This drives the architecture
below.

---

## 4. Architecture — one core, two surfaces

```
              src/solver_core.py   (pure Python — NO Spark, NO Databricks imports)
              ────────────────────────────────────────────────────────────────────
              haversine_matrix(lats, lons, avg_speed_kph) -> travel_minutes[][]
              solve_cvrptw(stops, params) -> route_plan rows
                  • OR-Tools RoutingIndexManager + RoutingModel
                  • arc cost = travel minutes + per-stop service time
                  • AddDimensionWithVehicleCapacity   (capacity)
                  • AddDimension "Time" + CumulVar.SetRange   (time windows)
                  • AddDisjunction(drop_penalty)   (optional/skippable stops)
                  • PARALLEL_CHEAPEST_INSERTION + GUIDED_LOCAL_SEARCH + time limit
                          │
            ┌─────────────┴──────────────┐
            ▼                             ▼
   Surface 1: Designer operator    Surface 2: Genie TVF
   python-run-function             UC scalar UDF (ENVIRONMENT: ortools)
   environment: ortools==9.14...     returns route plan as JSON
   run(config, inputs, spark):     + SQL TVF wrapper that selects urgent
     inline copy of solver_core      parcels, calls the UDF, from_json +
     toPandas -> solve -> output      explode -> TABLE  ← this is the Genie tool
```

`solver_core.py` is the single source of truth for the optimization. The inline operator
embeds a verbatim copy of its functions (decision #3), and the scalar UDF imports/embeds the
same logic. The two surfaces differ only in their data adapter, never in the math.

### 4.1 Shared solver-core contract (`src/solver_core.py`)

Pure Python, no platform imports, unit-testable locally with just `ortools` + `numpy`.

```python
def haversine_matrix(lats: list[float], lons: list[float],
                     avg_speed_kph: float) -> list[list[int]]:
    """Symmetric travel-time matrix in whole minutes; diagonal = 0.
    Node order is the caller's order (node 0 must be the depot)."""

def solve_cvrptw(stops: list[dict], params: dict) -> list[dict]:
    """stops: rows with keys stop_id, lat, lon, demand, due_minute (ready_minute optional, default 0).
       Node 0 of the matrix is the depot; the depot is NOT a member of `stops`
       (the depot's lat/lon come from params).
    params keys:
       depot_lat, depot_lon : float
       vehicle_count        : int
       vehicle_capacity     : int          (uniform across the fleet for v1)
       max_route_minutes    : int
       avg_speed_kph        : float
       service_minutes      : int           (per-stop dwell)
       solver_seconds       : int
       drop_penalty         : int
    returns route-plan rows, one per visited stop, ordered by (vehicle_id, stop_sequence):
       { vehicle_id:int, stop_sequence:int, stop_id, lat:float, lon:float,
         arrival_minute:int, load_after:int, is_dropped:bool }
    Dropped stops (skipped via disjunction) are returned with vehicle_id = -1,
    stop_sequence = -1, is_dropped = True so the caller can report unserved parcels."""
```

Determinism: with fixed input and a fixed solver time limit OR-Tools is deterministic enough
for a demo; tests should assert structural invariants (all non-dropped stops appear once;
per-vehicle load never exceeds capacity; arrival minutes within `[ready, due]`), not exact
routes.

---

## 5. Surface 1 — Lakeflow Designer operator

**Type: `python-run-function`** (NOT `uc-udtf` — it cannot install ortools).

- File: `operators/route_optimizer.yaml` — schema `user-defined-operator-v0.1.0`.
- Input port `stops` (one table). Output port `routes` (one table).
- `environment.dependencies: ['ortools==9.14.6206']`, `environment_version: '4'`.
- `run_function.type: inline`; `code` defines `run(config, inputs, spark)` which:
  `inputs["stops"].toPandas()` → `solve_cvrptw(...)` (solver code inlined in the same
  `code` block) → `spark.createDataFrame(plan)` returned under key `"routes"`.
- Config widgets (side panel):
  - Column pickers using `x-ui.widget: select` + `optionsSource: {type: inputColumns,
    port: stops}` for: `id_column`, `lat_column`, `lon_column`, `demand_column`,
    `due_minutes_column`. (`ready_minutes_column` optional.)
  - Numeric inputs: `depot_lat`, `depot_lon`, `vehicle_count` (default 5),
    `vehicle_capacity` (default 100), `max_route_minutes` (default 480),
    `avg_speed_kph` (default 50), `service_minutes` (default 10),
    `solver_seconds` (default 10), `drop_penalty` (default 1000000).
  - `required`: id/lat/lon/demand/due columns + depot_lat/depot_lon. `additionalProperties: false`.
- Output `routes` schema: `vehicle_id INT, stop_sequence INT, stop_id STRING,
  lat DOUBLE, lon DOUBLE, arrival_minute INT, load_after INT, is_dropped BOOLEAN`.
- Registration: `operators/.user_defined_operators.yaml` listing catalog
  `supply_chain`, schema `route_optimizer_accelerator`, the operator id. Document the
  required grants (USE SCHEMA + EXECUTE) in the operator README/notebook.

The drag-and-drop reusability thesis: because column pickers are driven by
`inputColumns`, the same operator runs on any table with location + demand + due-by columns —
the optimization never changes.

---

## 6. Surface 2 — Genie Space TVF

UC UDTFs can't import ortools, so use the supported two-step pattern:

1. **`functions/solve_routes_udf.sql`** — UC **Python scalar UDF**
   `supply_chain.route_optimizer_accelerator.solve_routes_json(stops ..., params ...)
   RETURNS STRING`, with `ENVIRONMENT(dependencies='["ortools==9.14.6206"]',
   environment_version='...')`. Body runs the same `solve_cvrptw` logic and returns the
   route plan as a JSON string.
2. **`functions/optimize_routes_tvf.sql`** — SQL **TVF**
   `supply_chain.route_optimizer_accelerator.optimize_routes(depot_id, horizon_days,
   vehicle_count, vehicle_capacity, ...) RETURNS TABLE(...)` that selects today's urgent
   parcels from the Delta tables, calls `solve_routes_json(...)`, then `from_json` +
   `explode`s the result into route rows. **This SQL TVF is the Genie tool.**

Genie setup (`notebooks/04_genie_tvf_demo.py` + a short doc): register the TVF as a Genie
Space tool; provide 8–10 sample natural-language utterances (e.g. *"plan delivery routes for
depot 1 today with 4 vans"*) and tool instructions. Demo both a direct SQL call and an NL ask.

---

## 7. Synthetic data model (parcel deliveries)

Adapt `gas-tank-delivery/notebooks/00_setup_synthetic_data.py` (seed 42), re-themed:

- `depot(depot_id, name, lat, lon)` — one or a few depots around a metro area.
- `customers(customer_id, customer_name, lat, lon, city, state)`.
- `parcels(parcel_id, customer_id, depot_id, demand_units, ready_minute, due_minute,
  created_date)` — the stops to route; `demand_units` ~ small ints, `due_minute` within a
  shift (0–480), some tight windows to make time constraints bite.
- `vehicles(vehicle_id, depot_id, vehicle_name, capacity_units, max_route_minutes)` — the van
  fleet.
- `urgent_parcels_today` (view or table from `01_*`) — parcels due within the horizon,
  the operator's and TVF's input.

All written as Delta/UC tables via `saveAsTable` into
`supply_chain.route_optimizer_accelerator`. Notebooks parameterized with `dbutils.widgets`
for `catalog` (default `supply_chain`) and `schema` (default `route_optimizer_accelerator`).

---

## 8. Repository layout

```
route-optimizer-accelerator/
├── databricks.yml                          # DAB: vars (catalog/schema/warehouse), jobs
├── docs/DESIGN.md                          # this file
├── src/solver_core.py                      # pure-Python OR-Tools CVRPTW (shared core)
├── src/tests/test_solver_core.py           # local unit tests (ortools + numpy only)
├── operators/route_optimizer.yaml          # python-run-function operator (inline code)
├── operators/.user_defined_operators.yaml  # Designer registration manifest
├── functions/solve_routes_udf.sql          # UC scalar UDF (ENVIRONMENT: ortools)
├── functions/optimize_routes_tvf.sql       # SQL TVF wrapper = Genie tool
├── notebooks/00_setup_synthetic_data.py    # seed=42 parcel data → Delta
├── notebooks/01_compute_urgent_parcels.py  # urgency rule → urgent_parcels_today
├── notebooks/02_register_operator.py       # CREATE FUNCTIONs + register operator for Designer
├── notebooks/03_run_in_designer.md         # guided clicks: drag, wire, configure, run
├── notebooks/04_genie_tvf_demo.py          # SQL call + Genie Space setup + sample asks
├── resources/setup.job.yml                 # DAB job: 00 -> 01
└── README.md                               # narrative + prerequisites + run order
```

---

## 9. Build plan (delegated; cross-vendor reviewed)

All code is delegated to coding sub-agents; every PR is reviewed by the *opposite* vendor
(`claude_code` ⇄ `codex`; `pi` is not installed on this host). polly never merges — the human
merges each PR.

- **PR A — solver core + synthetic data + urgency** (implement: `claude_code`; review: `codex`):
  `src/solver_core.py`, `src/tests/test_solver_core.py`, `notebooks/00_*`, `notebooks/01_*`.
  Foundation; lands first.
- **PR B — Designer operator** (implement: `codex`; review: `claude_code`):
  `operators/route_optimizer.yaml`, `operators/.user_defined_operators.yaml`,
  `notebooks/02_*`, `notebooks/03_run_in_designer.md`.
- **PR C — Genie TVF** (implement: `claude_code`; review: `codex`):
  `functions/solve_routes_udf.sql`, `functions/optimize_routes_tvf.sql`,
  `notebooks/04_genie_tvf_demo.py`.
- **PR D — DAB packaging + README** (implement: `codex`; review: `claude_code`):
  `databricks.yml`, `resources/setup.job.yml`, top-level `README.md`.

B, C, D depend on A's solver-core contract (§4.1) and data model (§7), which are frozen by
this document so they can proceed in parallel once A's contract is in.

---

## 10. Acceptance criteria

- `src/solver_core.py` imports with only `ortools` + `numpy`; `test_solver_core.py` passes
  locally (capacity never exceeded; each non-dropped stop visited once; arrivals within
  windows; dropped stops flagged).
- Operator YAML validates against `user-defined-operator-v0.1.0`; column pickers populate
  from the `stops` port; running it on `urgent_parcels_today` yields the `routes` schema.
- The UC scalar UDF and SQL TVF create successfully in
  `supply_chain.route_optimizer_accelerator` and return route rows for a sample call on the
  serverless warehouse.
- Notebooks run top-to-bottom against catalog `supply_chain`, schema
  `route_optimizer_accelerator`, parameterized by widgets.
- README walks data → operator → solver → Genie in order, with the grants and prerequisites
  (DBR/runtime notes, USE SCHEMA + EXECUTE) called out.
