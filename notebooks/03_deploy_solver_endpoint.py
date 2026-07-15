# Databricks notebook source
# MAGIC %md
# MAGIC # Deploy the route solver Model Serving endpoint
# MAGIC
# MAGIC Idempotently creates/updates a CPU Model Serving endpoint that serves the
# MAGIC UC-registered route solver PyFunc. Genie/SQL call this endpoint via
# MAGIC `ai_query` from a SQL UC function.

# COMMAND ----------

dbutils.widgets.text("catalog", "supplychain", "Catalog")
dbutils.widgets.text("schema", "route_optimizer_accelerator", "Schema")
dbutils.widgets.text("solver_model_name", "", "Registered model (blank = catalog.schema.route_solver)")
dbutils.widgets.text("solver_model_alias", "champion", "Model alias")
dbutils.widgets.text("solver_endpoint_name", "route-optimizer-solver-dev", "Endpoint name")

CATALOG = dbutils.widgets.get("catalog")
SCHEMA = dbutils.widgets.get("schema")
SOLVER_MODEL_NAME = (
    dbutils.widgets.get("solver_model_name").strip()
    or f"{CATALOG}.{SCHEMA}.route_solver"
)
SOLVER_MODEL_ALIAS = dbutils.widgets.get("solver_model_alias").strip() or "champion"
ENDPOINT_NAME = (
    dbutils.widgets.get("solver_endpoint_name").strip() or "route-optimizer-solver-dev"
)

# COMMAND ----------

from __future__ import annotations

import time
from datetime import timedelta

from databricks.sdk import WorkspaceClient
from databricks.sdk.errors import NotFound, ResourceDoesNotExist
from databricks.sdk.service.serving import (
    EndpointCoreConfigInput,
    Route,
    ServedEntityInput,
    ServingModelWorkloadType,
    TrafficConfig,
)
from mlflow.tracking import MlflowClient

mlflow_client = MlflowClient(registry_uri="databricks-uc")
try:
    model_version = mlflow_client.get_model_version_by_alias(
        SOLVER_MODEL_NAME, SOLVER_MODEL_ALIAS
    )
    registered_version = str(model_version.version)
except AttributeError:
    versions = mlflow_client.search_model_versions(f"name = '{SOLVER_MODEL_NAME}'")
    matching = [
        v
        for v in versions
        if SOLVER_MODEL_ALIAS in (getattr(v, "aliases", None) or [])
    ]
    if not matching:
        raise ValueError(
            f"No {SOLVER_MODEL_NAME}@{SOLVER_MODEL_ALIAS} model version found."
        )
    registered_version = str(max(int(v.version) for v in matching))

served_entity_name = (
    f"{ENDPOINT_NAME}-v{registered_version}".replace(".", "-").replace("_", "-")
)
served_entity = ServedEntityInput(
    name=served_entity_name,
    entity_name=SOLVER_MODEL_NAME,
    entity_version=registered_version,
    scale_to_zero_enabled=True,
    workload_size="Small",
    workload_type=ServingModelWorkloadType.CPU,
)
traffic = TrafficConfig(
    routes=[Route(served_entity_name=served_entity_name, traffic_percentage=100)]
)

workspace = WorkspaceClient()
try:
    workspace.serving_endpoints.get(ENDPOINT_NAME)
    exists = True
except (NotFound, ResourceDoesNotExist):
    exists = False

if not exists:
    workspace.serving_endpoints.create_and_wait(
        name=ENDPOINT_NAME,
        config=EndpointCoreConfigInput(
            name=ENDPOINT_NAME,
            served_entities=[served_entity],
            traffic_config=traffic,
        ),
        timeout=timedelta(minutes=45),
    )
else:
    ep = workspace.serving_endpoints.get(ENDPOINT_NAME)
    # If a prior attempt left the endpoint in UPDATE_FAILED, recreate it.
    ready = str(getattr(getattr(ep, "state", None), "ready", "") or "")
    update = str(getattr(getattr(ep, "state", None), "config_update", "") or "")
    if "NOT_READY" in ready.upper() and "FAILED" in update.upper():
        print(f"Deleting failed endpoint {ENDPOINT_NAME} before recreate")
        workspace.serving_endpoints.delete(ENDPOINT_NAME)
        # wait briefly for delete to settle
        for _ in range(30):
            try:
                workspace.serving_endpoints.get(ENDPOINT_NAME)
                time.sleep(5)
            except (NotFound, ResourceDoesNotExist):
                break
        workspace.serving_endpoints.create_and_wait(
            name=ENDPOINT_NAME,
            config=EndpointCoreConfigInput(
                name=ENDPOINT_NAME,
                served_entities=[served_entity],
                traffic_config=traffic,
            ),
            timeout=timedelta(minutes=45),
        )
    else:
        workspace.serving_endpoints.wait_get_serving_endpoint_not_updating(
            ENDPOINT_NAME, timeout=timedelta(minutes=45)
        )
        for attempt in range(1, 6):
            try:
                workspace.serving_endpoints.update_config_and_wait(
                    name=ENDPOINT_NAME,
                    served_entities=[served_entity],
                    traffic_config=traffic,
                    timeout=timedelta(minutes=45),
                )
                break
            except Exception as exc:
                if "currently being updated" not in str(exc) or attempt == 5:
                    raise
                workspace.serving_endpoints.wait_get_serving_endpoint_not_updating(
                    ENDPOINT_NAME, timeout=timedelta(minutes=45)
                )
                time.sleep(15)

# Wait until READY / available for query.
deadline = time.time() + 20 * 60
while time.time() < deadline:
    ep = workspace.serving_endpoints.get(ENDPOINT_NAME)
    state = getattr(getattr(ep, "state", None), "ready", None) or getattr(
        getattr(ep, "state", None), "config_update", None
    )
    ready_val = str(state)
    print(f"endpoint state={ready_val}")
    if "READY" in ready_val.upper() or ready_val in ("None", ""):
        # Some SDK versions expose ready as an enum; also check config_update.
        config_update = str(
            getattr(getattr(ep, "state", None), "config_update", "") or ""
        )
        if "IN_PROGRESS" not in config_update.upper():
            break
    time.sleep(15)
else:
    raise TimeoutError(f"Endpoint {ENDPOINT_NAME} did not become ready in time")

# Smoke-query the endpoint with a tiny fixture.
import json

smoke = {
    "dataframe_records": [
        {
            "stops_json": json.dumps(
                [
                    {
                        "stop_id": "1",
                        "lat": 29.77,
                        "lon": -95.36,
                        "demand": 10,
                        "ready_minute": 0,
                        "due_minute": 480,
                    }
                ]
            ),
            "depot_lat": 29.7604,
            "depot_lon": -95.3698,
            "vehicle_count": 1,
            "vehicle_capacity": 100,
            "max_route_minutes": 480,
            "avg_speed_kph": 50.0,
            "service_minutes": 10,
            "solver_seconds": 3,
            "drop_penalty": 1000000,
        }
    ]
}
resp = workspace.serving_endpoints.query(name=ENDPOINT_NAME, dataframe_records=smoke["dataframe_records"])
print({"endpoint": ENDPOINT_NAME, "smoke_response": resp.as_dict() if hasattr(resp, "as_dict") else str(resp)})

print(
    f"Serving endpoint {ENDPOINT_NAME} now serves {SOLVER_MODEL_NAME} "
    f"version {registered_version} from @{SOLVER_MODEL_ALIAS}."
)

dbutils.jobs.taskValues.set("solver_endpoint_name", ENDPOINT_NAME)
dbutils.jobs.taskValues.set("solver_model_version", registered_version)
