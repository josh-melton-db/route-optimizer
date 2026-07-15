# Databricks notebook source
# MAGIC %md
# MAGIC # Register endpoint-backed UC functions (ai_query path)
# MAGIC
# MAGIC Creates:
# MAGIC 1. `solve_routes_endpoint_json` — SQL scalar wrapping `ai_query` against the solver endpoint
# MAGIC 2. `optimize_routes_via_endpoint` — SQL TVF Genie tool that calls (1)
# MAGIC
# MAGIC Prerequisites: synthetic data + Model Serving endpoint READY.

# COMMAND ----------

dbutils.widgets.text("catalog", "supplychain", "Catalog")
dbutils.widgets.text("schema", "route_optimizer_accelerator", "Schema")
dbutils.widgets.text("solver_endpoint_name", "route-optimizer-solver-dev", "Endpoint name")

CATALOG = dbutils.widgets.get("catalog")
SCHEMA = dbutils.widgets.get("schema")
ENDPOINT = dbutils.widgets.get("solver_endpoint_name").strip() or "route-optimizer-solver-dev"
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
    stmt = stmt.replace("__SOLVER_ENDPOINT__", ENDPOINT)
    spark.sql(stmt.strip().rstrip(";"))
    print(f"Applied {filename} (endpoint={ENDPOINT})")


apply_sql_file("solve_routes_endpoint_udf.sql")
apply_sql_file("optimize_routes_endpoint_tvf.sql")

# COMMAND ----------

display(spark.sql(f"SHOW USER FUNCTIONS IN {CATALOG}.{SCHEMA} LIKE '*routes*'"))

# COMMAND ----------

# MAGIC %md
# MAGIC ## Smoke test on the current Spark session
# MAGIC Prefer the dedicated SQL-warehouse verification notebook for `ai_query`
# MAGIC (serverless warehouse). This cell is a best-effort check.

# COMMAND ----------

try:
    display(
        spark.sql(f"""
            SELECT vehicle_id, stop_sequence, stop_id, arrival_minute, is_dropped
            FROM {TARGET_PREFIX}.optimize_routes_via_endpoint(depot_id => 1, vehicle_count => 10)
            ORDER BY vehicle_id, stop_sequence
            LIMIT 20
        """)
    )
except Exception as exc:
    print(f"Spark-session smoke skipped/failed (expected if ai_query needs serverless SQL warehouse): {exc}")
