# Run the Route Optimizer in Lakeflow Designer

Use this walkthrough after the repository has been synced to a Databricks workspace Git folder and the `operators/` manifest has been made available to Lakeflow Designer.

References:

- Lakeflow Designer overview: https://docs.databricks.com/aws/en/designer/what-is-lakeflow-designer
- User-defined operators YAML reference: https://docs.databricks.com/aws/en/designer/operators-yaml-ref

## Prerequisites

- `notebooks/00_setup_synthetic_data.py` and `notebooks/01_compute_urgent_parcels.py` have created `supply_chain.route_optimizer_accelerator.urgent_parcels_today`.
- The Designer user has `USE CATALOG` on `supply_chain`, `USE SCHEMA` on `supply_chain.route_optimizer_accelerator`, and `EXECUTE` on the registered operator/function surface.
- The operator files are present in the workspace Git folder:
  - `operators/.user_defined_operators.yaml`
  - `operators/route_optimizer.yaml`

## Build the flow

1. Open Lakeflow Designer in the Databricks workspace.
2. Create or open a pipeline/flow for the route optimizer demo.
3. Add the source table `supply_chain.route_optimizer_accelerator.urgent_parcels_today` to the canvas.
4. Drag the **Route Optimizer** user-defined operator onto the canvas.
5. Wire the table output into the operator input port named `stops`.
6. Open the operator side panel and set the column pickers:
   - `id_column`: parcel/stop identifier column, such as `parcel_id` or `stop_id`
   - `lat_column`: `lat`
   - `lon_column`: `lon`
   - `demand_column`: `demand`
   - `due_minutes_column`: `due_minute`
   - `ready_minutes_column`: `ready_minute`, if present; otherwise leave unset
7. Set the depot and fleet parameters:
   - `depot_lat`: depot latitude
   - `depot_lon`: depot longitude
   - `vehicle_count`: default `5`
   - `vehicle_capacity`: default `100`
   - `max_route_minutes`: default `480`
   - `avg_speed_kph`: default `50`
   - `service_minutes`: default `10`
   - `solver_seconds`: default `10`
   - `drop_penalty`: default `1000000`
8. Run the flow.

## Read the output

The operator emits a `routes` table with one row per served stop and one row per dropped stop:

| Column | Meaning |
| --- | --- |
| `vehicle_id` | Assigned vehicle ID; `-1` means the stop was dropped. |
| `stop_sequence` | Visit order within the vehicle route; `-1` for dropped stops. |
| `stop_id` | Original parcel/stop identifier. |
| `lat`, `lon` | Stop coordinates. |
| `arrival_minute` | Planned arrival minute from route start; `-1` for dropped stops. |
| `load_after` | Vehicle load after serving the stop; `-1` for dropped stops. |
| `is_dropped` | `true` when OR-Tools skipped the stop via the drop penalty. |

Sort non-dropped rows by `vehicle_id`, then `stop_sequence` to inspect each optimized van route. Dropped rows indicate infeasible or too-expensive stops; relax time windows, add vehicles, raise capacity, or increase `solver_seconds` if too many stops are dropped.
