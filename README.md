# Route Optimizer Accelerator

Drag-and-drop mathematical solvers in Lakeflow Designer, demonstrated with parcel-delivery route optimization on Google OR-Tools running on CPU.

This accelerator shows how an analyst can drag a custom **Route Optimizer** operator onto a Lakeflow Designer canvas, connect a table of parcel stops, configure routing parameters in the side panel, and produce optimized delivery routes. It uses a plain CVRPTW solver (capacitated vehicle routing with time windows): each stop has location, demand, ready time, and due-by constraints; each van has capacity and a maximum route duration.

The demo intentionally uses **Google OR-Tools on CPU**, not NVIDIA cuOpt on GPU. OR-Tools keeps the accelerator runnable on commodity Databricks compute with no GPU requirement. The solver construction and deterministic synthetic-data pattern are adapted from the [`gas-tank-delivery`](../gas-tank-delivery) repository, rethemed from fuel drops to parcel deliveries.

## What Ships

- `src/solver_core.py` is the shared pure-Python solver core. It contains no Spark or Databricks imports, builds a haversine travel-time matrix, and solves CVRPTW with OR-Tools routing dimensions for capacity and time windows.
- `notebooks/00_setup_synthetic_data.py` creates deterministic synthetic Delta tables for depots, customers, parcels, and vehicles in Unity Catalog.
- `notebooks/01_compute_urgent_parcels.py` filters today's parcel data into `urgent_parcels_today`, the stable input table used by both routing surfaces.
- `operators/route_optimizer.yaml` defines the Lakeflow Designer `python-run-function` operator with inline solver code and an `ortools==9.14.6206` environment dependency.
- `functions/solve_routes_udf.sql` and `functions/optimize_routes_tvf.sql` expose the same route optimization through a Unity Catalog scalar UDF plus SQL table-valued function for Genie.
- `databricks.yml` and `resources/setup.job.yml` package the setup notebooks as a Databricks Asset Bundle job.

Some operator, function, and demo notebook files are delivered by companion PRs. This README describes the merged accelerator layout.

## Prerequisites

- A Databricks workspace with Lakeflow Designer and Databricks Asset Bundles support. This accelerator targets `https://fevm-supply-chain-demo.cloud.databricks.com`.
- Unity Catalog catalog `supply_chain` and schema `route_optimizer_accelerator`. The schema is expected to already exist.
- Permissions for users who will run UC-backed functions or Designer operators: `USE SCHEMA` on `supply_chain.route_optimizer_accelerator` and `EXECUTE` on the relevant functions. For `python-run-function` operators, users also need read access to the operator YAML and registration file in the workspace.
- A serverless SQL warehouse named `Serverless Starter Warehouse` for SQL and Genie demos.
- Databricks Runtime support for Unity Catalog Python functions with custom environments. OR-Tools is declared per surface as `ortools==9.14.6206`: in the Designer operator `environment` block and in the scalar UDF `ENVIRONMENT` clause.
- Databricks CLI authentication configured for bundle commands, for example through the workspace profile used by the demo environment.

## Architecture

The accelerator has one solver and two Databricks surfaces.

1. **Shared solver core** — `src/solver_core.py` implements `haversine_matrix(...)` and `solve_cvrptw(...)`. Node 0 is the depot, parcel stops are independent CVRPTW stops, arc cost is travel minutes plus service time, and dropped stops are returned with `is_dropped = true`.
2. **Lakeflow Designer operator** — `operators/route_optimizer.yaml` is a `python-run-function` user-defined operator. It reads an input DataFrame, uses column picker configuration from the Designer side panel, converts the stops to Pandas, runs the inline solver, and returns a `routes` DataFrame.
3. **Genie SQL tool** — `functions/solve_routes_udf.sql` registers a Unity Catalog Python scalar UDF that returns route rows as JSON using the same solver logic. `functions/optimize_routes_tvf.sql` wraps that UDF in a SQL TVF that selects urgent parcels, explodes the JSON route plan, and can be registered as a Genie Space tool.

Unity Catalog Python UDTFs are not used for OR-Tools because they cannot install this custom dependency. The dependency is declared where it is supported: the Designer operator environment and the scalar UDF `ENVIRONMENT` clause.

Useful Databricks references:

- [What is Lakeflow Designer?](https://docs.databricks.com/aws/en/designer/what-is-lakeflow-designer)
- [User-defined operators in Lakeflow Designer](https://docs.databricks.com/aws/en/designer/user-operators)
- [User-defined operator YAML reference](https://docs.databricks.com/aws/en/designer/operators-yaml-ref)
- [Tutorial: K-means clustering UDTF operator](https://docs.databricks.com/aws/en/designer/tutorial-kmeans-clustering-udtf)

## Run Order

1. **Deploy the bundle.** From the repository root, run:

   ```bash
   databricks bundle validate
   databricks bundle deploy -t dev
   ```

   The bundle defaults to catalog `supply_chain`, schema `route_optimizer_accelerator`, and warehouse name `Serverless Starter Warehouse`. Override bundle variables if needed:

   ```bash
   databricks bundle deploy -t dev --var="catalog=supply_chain" --var="schema=route_optimizer_accelerator"
   ```

2. **Run the setup notebooks.** Run the DAB job, or run the notebooks manually in order:

   ```bash
   databricks bundle run -t dev setup_pipeline
   ```

   The job executes `notebooks/00_setup_synthetic_data.py` first, then `notebooks/01_compute_urgent_parcels.py`. The result is a Unity Catalog table named `supply_chain.route_optimizer_accelerator.urgent_parcels_today`.

3. **Register and run the Designer operator.** Run `notebooks/02_register_operator.py` after the operator files from the companion PR are merged. Then follow `notebooks/03_run_in_designer.md` to open Lakeflow Designer, drag the Route Optimizer onto the canvas, connect `urgent_parcels_today`, choose the ID/lat/lon/demand/due columns, and run the CPU OR-Tools solver.

4. **Run the Genie TVF demo.** Run `notebooks/04_genie_tvf_demo.py` after the function files from the companion PR are merged. The demo creates or uses the scalar UDF and SQL TVF, validates a direct SQL call to `optimize_routes(...)`, and shows how to register the TVF as a Genie Space tool.

## Bundle Contents

The bundle defines one setup job in `resources/setup.job.yml`:

- `setup_synthetic_data` runs `notebooks/00_setup_synthetic_data.py` with `catalog` and `schema` base parameters.
- `compute_urgent_parcels` runs `notebooks/01_compute_urgent_parcels.py` after the setup task with the same parameters.

The bundle deliberately does not create the Unity Catalog schema or SQL warehouse. The demo environment already has `supply_chain.route_optimizer_accelerator` and the serverless warehouse available.

## Notes

- This accelerator runs on CPU only. There is no GPU dependency and no cuOpt integration.
- `ortools==9.14.6206` is pinned per execution surface rather than installed globally.
- The routing model uses haversine travel minutes for portability. Road-network distances such as OSRM can be added later as an optional upgrade without changing the Databricks surfaces.
- MLflow model serving and PyFunc packaging are intentionally out of scope.
