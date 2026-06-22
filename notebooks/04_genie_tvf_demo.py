# Databricks notebook source
# MAGIC %md
# MAGIC # Genie TVF demo — route optimization as a natural-language tool
# MAGIC
# MAGIC This notebook is **Surface 2** of the Route Optimizer Accelerator (see
# MAGIC `docs/DESIGN.md` §6): a route-optimization **Table-Valued Function** that a
# MAGIC Genie Space can call so an analyst plans delivery routes in plain English.
# MAGIC
# MAGIC ## Why two functions instead of one UDTF
# MAGIC
# MAGIC Unity Catalog Python **UDTFs cannot install custom pip dependencies** — only
# MAGIC base-runtime libraries (which is exactly why the Designer
# MAGIC [k-means clustering *uc-udtf* tutorial](https://docs.databricks.com/aws/en/designer/tutorial-kmeans-clustering-udtf)
# MAGIC works with scikit-learn but does **not** carry over to OR-Tools). So we use
# MAGIC the supported **two-step pattern**:
# MAGIC
# MAGIC 1. **`solve_routes_json`** — a UC Python **scalar UDF** that CAN declare
# MAGIC    `ortools` via the `ENVIRONMENT` clause. It embeds the shared solver core
# MAGIC    (`src/solver_core.py`) and returns the route plan as a **JSON string**.
# MAGIC 2. **`optimize_routes`** — a **SQL TVF** that selects today's urgent parcels
# MAGIC    for a depot, calls `solve_routes_json(...)`, then `from_json` + `explode`s
# MAGIC    the result into a clean table. **This TVF is the Genie tool.**
# MAGIC
# MAGIC Both functions are created in `${catalog}.${schema}`
# MAGIC (default `supply_chain.route_optimizer_accelerator`).
# MAGIC
# MAGIC **Prerequisites:** run `00_setup_synthetic_data` and `01_compute_urgent_parcels`
# MAGIC first so the `depot`, `customers`, and `parcels` Delta tables exist.

# COMMAND ----------

dbutils.widgets.text("catalog", "supply_chain", "Catalog")
dbutils.widgets.text("schema", "route_optimizer_accelerator", "Schema")

CATALOG = dbutils.widgets.get("catalog")
SCHEMA = dbutils.widgets.get("schema")

spark.sql(f"USE CATALOG {CATALOG}")
spark.sql(f"USE SCHEMA {SCHEMA}")

print(f"Creating + demoing route-optimization functions in {CATALOG}.{SCHEMA}")

# COMMAND ----------

# MAGIC %md
# MAGIC ## 1. Create the two functions from their `.sql` files
# MAGIC
# MAGIC We read the checked-in `functions/*.sql` files and execute each
# MAGIC `CREATE OR REPLACE FUNCTION` statement. The files are catalog/schema
# MAGIC qualified to `supply_chain.route_optimizer_accelerator`; if your widgets
# MAGIC point elsewhere, we rewrite that prefix to `${catalog}.${schema}` before
# MAGIC running so the demo works in any target schema.
# MAGIC
# MAGIC Each `.sql` file contains exactly one statement (the leading `--` comment
# MAGIC banner is part of that statement's whitespace), so we send the whole file
# MAGIC to `spark.sql` after stripping the trailing `;`.

# COMMAND ----------

import os

# Resolve the repo's functions/ directory relative to this notebook. In a
# Databricks Git folder the notebook runs from notebooks/, so functions/ is a
# sibling. Fall back to a couple of common locations if needed.
_NB_DIR = os.path.dirname(
    dbutils.notebook.entry_point.getDbutils().notebook().getContext()
    .notebookPath().get()
)
_CANDIDATES = [
    os.path.join(os.getcwd(), "..", "functions"),
    "/Workspace" + os.path.join(_NB_DIR, "..", "functions"),
    os.path.join(os.getcwd(), "functions"),
]
FUNCTIONS_DIR = next((p for p in _CANDIDATES if os.path.isdir(p)), _CANDIDATES[0])
print(f"Reading SQL from: {FUNCTIONS_DIR}")

DEFAULT_PREFIX = "supply_chain.route_optimizer_accelerator"
TARGET_PREFIX = f"{CATALOG}.{SCHEMA}"


def apply_sql_file(filename: str) -> None:
    """Read one .sql file, retarget the catalog/schema prefix, and execute it."""
    path = os.path.join(FUNCTIONS_DIR, filename)
    with open(path, "r") as fh:
        stmt = fh.read()
    if TARGET_PREFIX != DEFAULT_PREFIX:
        stmt = stmt.replace(DEFAULT_PREFIX, TARGET_PREFIX)
    stmt = stmt.strip().rstrip(";")
    spark.sql(stmt)
    print(f"Applied {filename}")


# Order matters: the scalar UDF must exist before the TVF that calls it.
apply_sql_file("solve_routes_udf.sql")
apply_sql_file("optimize_routes_tvf.sql")

# COMMAND ----------

# MAGIC %md
# MAGIC ### (Alternative) inline the CREATE FUNCTION statements
# MAGIC
# MAGIC If you are not running inside a Git folder where the `.sql` files are
# MAGIC reachable on disk, copy the bodies of `functions/solve_routes_udf.sql` and
# MAGIC `functions/optimize_routes_tvf.sql` into two `%sql` cells here instead. The
# MAGIC file-reading approach above keeps this notebook as the single source of the
# MAGIC SQL.

# COMMAND ----------

# MAGIC %md
# MAGIC ## 2. Confirm the functions registered

# COMMAND ----------

display(spark.sql(f"SHOW USER FUNCTIONS IN {CATALOG}.{SCHEMA} LIKE '*routes*'"))

# COMMAND ----------

# MAGIC %md
# MAGIC ## 3. Direct SQL call — the same call Genie will make
# MAGIC
# MAGIC `optimize_routes(depot_id, horizon_minutes, vehicle_count, vehicle_capacity, ...)`
# MAGIC returns one row per planned stop, ordered by `(vehicle_id, stop_sequence)`.
# MAGIC Unserved/dropped parcels come back with `vehicle_id = -1`,
# MAGIC `stop_sequence = -1`, `is_dropped = true`.
# MAGIC
# MAGIC Here: depot **1**, full **480**-minute (8h) shift, **4** vans, **100** units each.

# COMMAND ----------

routes = spark.sql(f"""
    SELECT *
    FROM {CATALOG}.{SCHEMA}.optimize_routes(1, 480, 4, 100)
    ORDER BY vehicle_id, stop_sequence
""")
display(routes)

# COMMAND ----------

# MAGIC %md
# MAGIC ### Named-argument call (what Genie typically generates)
# MAGIC
# MAGIC Every parameter has a default, so Genie can pass only what the user named.
# MAGIC This asks for depot 1 with 6 vans, leaving the rest at their defaults.

# COMMAND ----------

display(spark.sql(f"""
    SELECT *
    FROM {CATALOG}.{SCHEMA}.optimize_routes(
        depot_id => 1,
        vehicle_count => 6
    )
    ORDER BY vehicle_id, stop_sequence
"""))

# COMMAND ----------

# MAGIC %md
# MAGIC ## 4. Plan summary — a useful aggregate for the demo
# MAGIC
# MAGIC Per-vehicle stop counts, final load, and finish time; plus how many
# MAGIC parcels (if any) the solver had to drop.

# COMMAND ----------

display(spark.sql(f"""
    WITH plan AS (
        SELECT * FROM {CATALOG}.{SCHEMA}.optimize_routes(1, 480, 4, 100)
    )
    SELECT
        vehicle_id,
        COUNT(*)                         AS stops,
        MAX(load_after)                  AS final_load,
        MAX(arrival_minute)              AS last_arrival_minute
    FROM plan
    WHERE NOT is_dropped
    GROUP BY vehicle_id
    ORDER BY vehicle_id
"""))

# COMMAND ----------

display(spark.sql(f"""
    SELECT COUNT(*) AS dropped_parcels
    FROM {CATALOG}.{SCHEMA}.optimize_routes(1, 480, 4, 100)
    WHERE is_dropped
"""))

# COMMAND ----------

# MAGIC %md
# MAGIC ## 5. Register `optimize_routes` as a Genie Space tool
# MAGIC
# MAGIC A Genie Space can call a Unity Catalog function directly as a **tool**. The
# MAGIC `optimize_routes` TVF is purpose-built for this: it takes simple scalar
# MAGIC arguments an analyst would speak (depot, horizon, fleet size) and returns a
# MAGIC tidy table.
# MAGIC
# MAGIC ### Setup steps
# MAGIC
# MAGIC 1. **Grants.** The Genie Space runs as the analyst (or a service principal).
# MAGIC    Grant them, on `${catalog}.${schema}`:
# MAGIC    - `USE CATALOG` on `supply_chain`,
# MAGIC    - `USE SCHEMA` on `route_optimizer_accelerator`,
# MAGIC    - `SELECT` on `depot`, `customers`, `parcels`,
# MAGIC    - `EXECUTE` on **both** `optimize_routes` **and** `solve_routes_json`
# MAGIC      (the TVF invokes the UDF, so the caller needs EXECUTE on both).
# MAGIC
# MAGIC    ```sql
# MAGIC    GRANT USE CATALOG ON CATALOG supply_chain TO `analysts`;
# MAGIC    GRANT USE SCHEMA  ON SCHEMA  supply_chain.route_optimizer_accelerator TO `analysts`;
# MAGIC    GRANT SELECT ON TABLE supply_chain.route_optimizer_accelerator.depot     TO `analysts`;
# MAGIC    GRANT SELECT ON TABLE supply_chain.route_optimizer_accelerator.customers TO `analysts`;
# MAGIC    GRANT SELECT ON TABLE supply_chain.route_optimizer_accelerator.parcels   TO `analysts`;
# MAGIC    GRANT EXECUTE ON FUNCTION supply_chain.route_optimizer_accelerator.solve_routes_json TO `analysts`;
# MAGIC    GRANT EXECUTE ON FUNCTION supply_chain.route_optimizer_accelerator.optimize_routes   TO `analysts`;
# MAGIC    ```
# MAGIC
# MAGIC 2. **Create / open the Genie Space.** In the Databricks UI go to
# MAGIC    **Genie → New Space** (or open an existing one). Add the
# MAGIC    `route_optimizer_accelerator` schema tables (`depot`, `customers`,
# MAGIC    `parcels`) as data so Genie can answer descriptive questions too.
# MAGIC
# MAGIC 3. **Add the function as a tool.** In the Space's
# MAGIC    **Settings → Instructions / Tools** (Functions) section, add the UC
# MAGIC    function `supply_chain.route_optimizer_accelerator.optimize_routes`.
# MAGIC    Genie reads the function's signature, `COMMENT`, and parameter defaults
# MAGIC    to decide when and how to call it. (This is the same "register a UC
# MAGIC    function as a callable tool" mechanism the Designer
# MAGIC    [k-means UDTF tutorial](https://docs.databricks.com/aws/en/designer/tutorial-kmeans-clustering-udtf)
# MAGIC    and the Genie function-tool docs describe.)
# MAGIC
# MAGIC 4. **Add tool instructions** (next cell) so Genie maps phrases like
# MAGIC    "depot 1", "today", and "4 vans" onto the right arguments.

# COMMAND ----------

# MAGIC %md
# MAGIC ### Tool instructions (paste into the Genie tool's description / Space instructions)
# MAGIC
# MAGIC > **Tool: `optimize_routes` — plan optimized parcel-delivery routes.**
# MAGIC >
# MAGIC > Use this tool whenever the user asks to *plan, optimize, build, or
# MAGIC > re-plan delivery routes* for a depot, or asks "what's the best route /
# MAGIC > schedule for today's parcels". It runs a capacitated vehicle-routing
# MAGIC > solver (CVRPTW, OR-Tools) over the urgent parcels for one depot and
# MAGIC > returns one row per stop.
# MAGIC >
# MAGIC > **Arguments** (all optional; sensible defaults shown):
# MAGIC > - `depot_id` INT (default 1) — which depot to route from. Map "depot 1",
# MAGIC >   "the Houston hub", etc. The demo data has a single depot, id 1.
# MAGIC > - `horizon_minutes` INT (default 480) — only parcels due within this many
# MAGIC >   minutes of shift start are routed. "today" / "this shift" = 480;
# MAGIC >   "this morning" ≈ 240; "by noon" ≈ 300.
# MAGIC > - `vehicle_count` INT (default 5) — number of vans/trucks. Map "4 vans",
# MAGIC >   "six trucks", "my fleet of 3".
# MAGIC > - `vehicle_capacity` INT (default 100) — units each vehicle can carry.
# MAGIC > - `max_route_minutes` INT (default 480), `avg_speed_kph` DOUBLE
# MAGIC >   (default 50), `service_minutes` INT (default 10),
# MAGIC >   `solver_seconds` INT (default 10), `drop_penalty` INT (default 1000000)
# MAGIC >   — advanced solver knobs; leave at defaults unless the user is explicit.
# MAGIC >
# MAGIC > **Returns** one row per planned stop: `vehicle_id`, `stop_sequence`,
# MAGIC > `stop_id`, `lat`, `lon`, `arrival_minute` (minutes after shift start),
# MAGIC > `load_after` (cumulative units on the van after this stop), `is_dropped`.
# MAGIC > Rows with `is_dropped = true` (and `vehicle_id = -1`) are parcels that
# MAGIC > could **not** be served within the constraints — surface these as
# MAGIC > "unserved / needs another vehicle or a relaxed deadline".
# MAGIC >
# MAGIC > **Presentation tips:** order by `vehicle_id, stop_sequence`; describe each
# MAGIC > vehicle's route as an ordered list of stops with arrival times; call out
# MAGIC > the number of dropped parcels; convert `arrival_minute` to a clock time if
# MAGIC > the user gave a shift start.

# COMMAND ----------

# MAGIC %md
# MAGIC ### Sample natural-language utterances
# MAGIC
# MAGIC Try these in the Genie Space once the tool is registered. Each should drive
# MAGIC a call to `optimize_routes(...)` with the mapped arguments.
# MAGIC
# MAGIC 1. *"Plan delivery routes for depot 1 today with 4 vans."*
# MAGIC    → `optimize_routes(depot_id => 1, horizon_minutes => 480, vehicle_count => 4)`
# MAGIC 2. *"Optimize today's parcel deliveries for depot 1 using 6 trucks."*
# MAGIC    → `optimize_routes(depot_id => 1, vehicle_count => 6)`
# MAGIC 3. *"What's the best route plan for the Houston depot this morning?"*
# MAGIC    → `optimize_routes(depot_id => 1, horizon_minutes => 240)`
# MAGIC 4. *"Build routes for depot 1 with 5 vans that each hold 120 units."*
# MAGIC    → `optimize_routes(depot_id => 1, vehicle_count => 5, vehicle_capacity => 120)`
# MAGIC 5. *"Can 3 vans cover all of today's urgent parcels from depot 1?"*
# MAGIC    → `optimize_routes(depot_id => 1, vehicle_count => 3)` — then report dropped count.
# MAGIC 6. *"Show me the delivery schedule for depot 1 with arrival times."*
# MAGIC    → `optimize_routes(depot_id => 1)` — present `arrival_minute` per stop.
# MAGIC 7. *"Which parcels can't we deliver today from depot 1 with only 2 vans?"*
# MAGIC    → `optimize_routes(depot_id => 1, vehicle_count => 2)` — filter `is_dropped = true`.
# MAGIC 8. *"Re-plan depot 1's routes assuming everything must be done by noon."*
# MAGIC    → `optimize_routes(depot_id => 1, horizon_minutes => 300)`
# MAGIC 9. *"Optimize routes for depot 1 and give the solver 30 seconds to think."*
# MAGIC    → `optimize_routes(depot_id => 1, solver_seconds => 30)`
# MAGIC 10. *"Plan depot 1's deliveries assuming the vans average 40 km/h in traffic."*
# MAGIC     → `optimize_routes(depot_id => 1, avg_speed_kph => 40)`

# COMMAND ----------

# MAGIC %md
# MAGIC ## 6. Recap
# MAGIC
# MAGIC - `solve_routes_json` (scalar UDF, `ENVIRONMENT: ortools==9.14.6206`) carries
# MAGIC   the dependency a UDTF could not, and runs the CVRPTW solver.
# MAGIC - `optimize_routes` (SQL TVF) is the **Genie tool** — it shapes the Delta
# MAGIC   tables into solver input, calls the UDF, and explodes the JSON to a table.
# MAGIC - Register `optimize_routes` as a Genie function tool, grant `EXECUTE` on
# MAGIC   both functions, paste the tool instructions, and analysts can plan routes
# MAGIC   in natural language.
