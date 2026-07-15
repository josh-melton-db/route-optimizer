# Databricks notebook source
# MAGIC %md
# MAGIC # Verify endpoint-backed TVF on a serverless SQL warehouse
# MAGIC
# MAGIC Runs `optimize_routes_via_endpoint` via the SQL Statement Execution API
# MAGIC against the configured warehouse and compares row counts with the control
# MAGIC Python-UDF TVF `optimize_routes`.

# COMMAND ----------

dbutils.widgets.text("catalog", "supplychain", "Catalog")
dbutils.widgets.text("schema", "route_optimizer_accelerator", "Schema")
dbutils.widgets.text("warehouse_id", "", "Serverless SQL warehouse ID")
dbutils.widgets.text("vehicle_count", "10", "Vehicle count for fixture")

CATALOG = dbutils.widgets.get("catalog")
SCHEMA = dbutils.widgets.get("schema")
WAREHOUSE_ID = dbutils.widgets.get("warehouse_id").strip()
VEHICLE_COUNT = int(dbutils.widgets.get("vehicle_count") or "10")
PREFIX = f"{CATALOG}.{SCHEMA}"

if not WAREHOUSE_ID:
    raise ValueError("warehouse_id is required for ai_query verification")

# COMMAND ----------

import time

from databricks.sdk import WorkspaceClient

w = WorkspaceClient()


def run_sql(statement: str, timeout_s: int = 180):
    resp = w.statement_execution.execute_statement(
        warehouse_id=WAREHOUSE_ID,
        statement=statement,
        wait_timeout="50s",
    )
    deadline = time.time() + timeout_s
    while resp.status and resp.status.state.value in ("PENDING", "RUNNING"):
        if time.time() > deadline:
            raise TimeoutError(statement)
        time.sleep(3)
        resp = w.statement_execution.get_statement(resp.statement_id)
    if resp.status.state.value != "SUCCEEDED":
        err = resp.status.error if resp.status else None
        raise RuntimeError(f"SQL failed: {err}\n{statement}")
    return resp


# Fail fast if warehouse is not serverless / ai_query-capable.
warehouse = w.warehouses.get(WAREHOUSE_ID)
print(
    {
        "warehouse_id": WAREHOUSE_ID,
        "name": warehouse.name,
        "enable_serverless_compute": getattr(warehouse, "enable_serverless_compute", None),
        "warehouse_type": str(getattr(warehouse, "warehouse_type", None)),
    }
)

# COMMAND ----------

control = run_sql(
    f"""
    SELECT
      COUNT(*) AS total_rows,
      SUM(CASE WHEN is_dropped THEN 1 ELSE 0 END) AS dropped_rows,
      SUM(CASE WHEN NOT is_dropped THEN 1 ELSE 0 END) AS served_rows
    FROM {PREFIX}.optimize_routes(depot_id => 1, vehicle_count => {VEHICLE_COUNT})
    """
)
endpoint = run_sql(
    f"""
    SELECT
      COUNT(*) AS total_rows,
      SUM(CASE WHEN is_dropped THEN 1 ELSE 0 END) AS dropped_rows,
      SUM(CASE WHEN NOT is_dropped THEN 1 ELSE 0 END) AS served_rows
    FROM {PREFIX}.optimize_routes_via_endpoint(depot_id => 1, vehicle_count => {VEHICLE_COUNT})
    """
)

control_row = control.result.data_array[0]
endpoint_row = endpoint.result.data_array[0]
summary = {
    "control_total": int(control_row[0]),
    "control_dropped": int(control_row[1] or 0),
    "control_served": int(control_row[2] or 0),
    "endpoint_total": int(endpoint_row[0]),
    "endpoint_dropped": int(endpoint_row[1] or 0),
    "endpoint_served": int(endpoint_row[2] or 0),
}
print(summary)

assert summary["endpoint_total"] == summary["control_total"], summary
assert summary["endpoint_total"] == 40, summary  # demo has 40 parcels
assert summary["endpoint_served"] + summary["endpoint_dropped"] == summary["endpoint_total"], summary

sample = run_sql(
    f"""
    SELECT vehicle_id, stop_sequence, stop_id, arrival_minute, is_dropped
    FROM {PREFIX}.optimize_routes_via_endpoint(depot_id => 1, vehicle_count => {VEHICLE_COUNT})
    ORDER BY vehicle_id, stop_sequence
    LIMIT 10
    """
)
print("sample rows:")
for row in sample.result.data_array:
    print(row)

print("Endpoint-backed TVF verification PASSED")
