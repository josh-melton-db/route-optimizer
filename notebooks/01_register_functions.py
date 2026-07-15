# Databricks notebook source
# MAGIC %md
# MAGIC # Register route-optimization UC functions for Genie
# MAGIC
# MAGIC Creates two Unity Catalog functions in `${catalog}.${schema}`:
# MAGIC
# MAGIC 1. **`solve_routes_json`** — Python scalar UDF (`ENVIRONMENT: ortools`) that runs the CVRPTW solver and returns a JSON route plan.
# MAGIC 2. **`optimize_routes`** — SQL table-valued function (TVF) that selects parcels for a depot, calls the UDF, and explodes the result. **Register this TVF as the Genie Space tool.**
# MAGIC
# MAGIC Prerequisites: run `00_setup_synthetic_data` first.

# COMMAND ----------

dbutils.widgets.text("catalog", "supplychain", "Catalog")
dbutils.widgets.text("schema", "route_optimizer_accelerator", "Schema")

CATALOG = dbutils.widgets.get("catalog")
SCHEMA = dbutils.widgets.get("schema")
TARGET_PREFIX = f"{CATALOG}.{SCHEMA}"

spark.sql(f"USE CATALOG {CATALOG}")
spark.sql(f"USE SCHEMA {SCHEMA}")

# COMMAND ----------

import os

_NB_DIR = os.path.dirname(
    dbutils.notebook.entry_point.getDbutils().notebook().getContext()
    .notebookPath().get()
)
FUNCTIONS_DIR = next(
    (
        p
        for p in (
            os.path.join(os.getcwd(), "functions"),
            "/Workspace" + os.path.join(_NB_DIR, "..", "functions"),
            os.path.join(os.getcwd(), "..", "functions"),
        )
        if os.path.isdir(p)
    ),
    None,
)
if FUNCTIONS_DIR is None:
    raise FileNotFoundError("Could not locate functions/ directory")

DEFAULT_PREFIX = "supply_chain.route_optimizer_accelerator"


def apply_sql_file(filename: str) -> None:
    path = os.path.join(FUNCTIONS_DIR, filename)
    stmt = open(path, "r").read()
    if TARGET_PREFIX != DEFAULT_PREFIX:
        stmt = stmt.replace(DEFAULT_PREFIX, TARGET_PREFIX)
    spark.sql(stmt.strip().rstrip(";"))
    print(f"Applied {filename}")


apply_sql_file("solve_routes_udf.sql")
apply_sql_file("optimize_routes_tvf.sql")

# COMMAND ----------

display(spark.sql(f"SHOW USER FUNCTIONS IN {CATALOG}.{SCHEMA} LIKE '*routes*'"))

# COMMAND ----------

display(
    spark.sql(f"""
        SELECT vehicle_id, stop_sequence, stop_id, arrival_minute, is_dropped
        FROM {TARGET_PREFIX}.optimize_routes(depot_id => 1, vehicle_count => 10)
        ORDER BY vehicle_id, stop_sequence
        LIMIT 20
    """)
)
