# Databricks notebook source
# MAGIC %md
# MAGIC # Register the Route Optimizer Lakeflow Designer operator
# MAGIC
# MAGIC This notebook prepares the Route Optimizer user-defined operator for Lakeflow Designer.
# MAGIC The operator itself is defined in `operators/route_optimizer.yaml` and uses the
# MAGIC `python-run-function` surface so it can install `ortools==9.14.6206` from its
# MAGIC `environment` block.
# MAGIC
# MAGIC ## Prerequisites
# MAGIC
# MAGIC - Databricks Runtime that supports Lakeflow Designer user-defined operators.
# MAGIC - The repo folder is synced to a Databricks workspace Git folder with `operators/` present.
# MAGIC - Unity Catalog catalog and schema exist.
# MAGIC - Users who run the Designer operator need workspace read access to the synced
# MAGIC   operator YAML files. This PR B operator is file-based, not a Unity Catalog function.
# MAGIC - The source data table `urgent_parcels_today` exists from notebooks `00` and `01`.
# MAGIC - The notebook smoke test cluster can install `ortools==9.14.6206`; the Designer
# MAGIC   operator installs this dependency itself from `operators/route_optimizer.yaml`.
# MAGIC - If you also deploy the Genie Unity Catalog functions from PR C, grant those users
# MAGIC   `USE CATALOG`, `USE SCHEMA`, and `EXECUTE` on the UC function objects separately.

# COMMAND ----------

dbutils.widgets.text("catalog", "supply_chain")
dbutils.widgets.text("schema", "route_optimizer_accelerator")
dbutils.widgets.text("depot_lat", "37.7749")
dbutils.widgets.text("depot_lon", "-122.4194")

catalog = dbutils.widgets.get("catalog")
schema = dbutils.widgets.get("schema")
qualified_schema = f"`{catalog}`.`{schema}`"
print(f"Using schema: {qualified_schema}")

# COMMAND ----------

spark.sql(f"USE CATALOG `{catalog}`")
spark.sql(f"USE SCHEMA `{schema}`")

# COMMAND ----------

# MAGIC %md
# MAGIC ## Register for Lakeflow Designer
# MAGIC
# MAGIC In the Databricks workspace Git folder, keep these files together:
# MAGIC
# MAGIC - `operators/.user_defined_operators.yaml`
# MAGIC - `operators/route_optimizer.yaml`
# MAGIC
# MAGIC Lakeflow Designer discovers user-defined operators from the registration manifest.
# MAGIC The manifest registers the inline `python-run-function` operator by file path:
# MAGIC
# MAGIC ```yaml
# MAGIC operators:
# MAGIC   - operators/route_optimizer.yaml
# MAGIC ```
# MAGIC
# MAGIC No separate SQL `CREATE FUNCTION` is required for this PR B design because the
# MAGIC operator is a `python-run-function` with inline code and its own environment.
# MAGIC The `catalog` and `schema` widgets below only select the smoke-test data table.
# MAGIC Grant workspace read access on the synced operator files to Lakeflow Designer users
# MAGIC or groups. `EXECUTE` grants apply to PR C's UC functions, not this file-based operator.

# COMMAND ----------

# MAGIC %sql
# MAGIC -- Example data/Genie grants for an administrator to adapt.
# MAGIC -- The PR B Designer operator itself is file-based; grant workspace READ/CAN READ
# MAGIC -- access on the synced operators/*.yaml files through workspace permissions.
# MAGIC -- GRANT USE CATALOG ON CATALOG supply_chain TO `account users`;
# MAGIC -- GRANT USE SCHEMA ON SCHEMA supply_chain.route_optimizer_accelerator TO `account users`;
# MAGIC -- For PR C UC functions only:
# MAGIC -- GRANT EXECUTE ON SCHEMA supply_chain.route_optimizer_accelerator TO `account users`;

# COMMAND ----------

# MAGIC %md
# MAGIC ## Smoke test the solver contract with `urgent_parcels_today`
# MAGIC
# MAGIC The local operator embeds the same solver logic inline. This smoke test exercises the
# MAGIC Python solver core against the table that will be wired to the Designer `stops` port.
# MAGIC It does not run Lakeflow Designer from the notebook.

# COMMAND ----------

# MAGIC %pip install ortools==9.14.6206

# COMMAND ----------

from src.solver_core import solve_cvrptw

empty_params = {
    "depot_lat": float(dbutils.widgets.get("depot_lat")),
    "depot_lon": float(dbutils.widgets.get("depot_lon")),
    "vehicle_count": 5,
    "vehicle_capacity": 100,
    "max_route_minutes": 480,
    "avg_speed_kph": 50.0,
    "service_minutes": 10,
    "solver_seconds": 10,
    "drop_penalty": 1000000,
}
assert solve_cvrptw([], empty_params) == []

source_table = f"{qualified_schema}.`urgent_parcels_today`"
urgent_pdf = spark.table(source_table).limit(50).toPandas()

candidate_columns = set(urgent_pdf.columns)
ready_column = "ready_minute" if "ready_minute" in candidate_columns else None

stops = []
for row in urgent_pdf.to_dict("records"):
    stop = {
        "stop_id": str(row["parcel_id"] if "parcel_id" in candidate_columns else row["stop_id"]),
        "lat": float(row["lat"]),
        "lon": float(row["lon"]),
        "demand": int(round(float(row["demand"]))),
        "due_minute": int(round(float(row["due_minute"]))),
        "ready_minute": int(round(float(row[ready_column]))) if ready_column else 0,
    }
    stops.append(stop)

params = {
    "depot_lat": float(dbutils.widgets.get("depot_lat")),
    "depot_lon": float(dbutils.widgets.get("depot_lon")),
    "vehicle_count": 5,
    "vehicle_capacity": 100,
    "max_route_minutes": 480,
    "avg_speed_kph": 50.0,
    "service_minutes": 10,
    "solver_seconds": 10,
    "drop_penalty": 1000000,
}

routes = solve_cvrptw(stops, params)
routes_schema = "vehicle_id INT, stop_sequence INT, stop_id STRING, lat DOUBLE, lon DOUBLE, arrival_minute INT, load_after INT, is_dropped BOOLEAN"
routes_columns = ["vehicle_id", "stop_sequence", "stop_id", "lat", "lon", "arrival_minute", "load_after", "is_dropped"]
route_tuples = [tuple(row[column] for column in routes_columns) for row in routes]
routes_df = spark.createDataFrame(route_tuples, schema=routes_schema)
display(routes_df.orderBy("is_dropped", "vehicle_id", "stop_sequence"))

# COMMAND ----------

# MAGIC %md
# MAGIC Confirm the output columns match the Designer operator `routes` port:
# MAGIC
# MAGIC `vehicle_id INT`, `stop_sequence INT`, `stop_id STRING`, `lat DOUBLE`, `lon DOUBLE`,
# MAGIC `arrival_minute INT`, `load_after INT`, `is_dropped BOOLEAN`.
