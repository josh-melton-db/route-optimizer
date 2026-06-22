-- ============================================================================
-- Route Optimizer Accelerator — Surface 2 (Genie), step 2 of 2
-- SQL Table-Valued Function: optimize_routes(...) RETURNS TABLE(...)
-- ----------------------------------------------------------------------------
-- THIS TVF IS THE GENIE TOOL (see docs/DESIGN.md §6). It is a thin SQL wrapper
-- around the Python scalar UDF solve_routes_json (functions/solve_routes_udf.sql)
-- — the UDF carries the ortools dependency and does the optimization; this TVF
-- adapts the Delta tables into the UDF's input and explodes its JSON result
-- back into rows so Genie (and direct SQL callers) get a clean table.
--
-- Data flow:
--   parcels ⋈ customers  --(filter by depot_id + horizon)-->  stops ARRAY<STRUCT>
--   depot                --(lat/lon for this depot)-------->  depot_lat/lon
--   solve_routes_json(stops, depot_lat, depot_lon, params)  -->  plan JSON string
--   from_json(plan, ARRAY<STRUCT<...>>) + explode            -->  one row per stop
--
-- The RETURNS TABLE column list below is byte-for-byte the JSON keys produced by
-- solve_cvrptw: vehicle_id, stop_sequence, stop_id, lat, lon, arrival_minute,
-- load_after, is_dropped. The from_json schema string uses the same names/types.
--
-- Parameters are qualified with the function name (optimize_routes.<param>)
-- everywhere to avoid clashes with table column names. All parameters have
-- DEFAULTs so Genie can call with as few or as many named args as the user gave
-- (Databricks rule: once one param has a default, all trailing params must too —
-- here every param has one).
--
-- Grants required for callers: USE CATALOG supply_chain, USE SCHEMA
-- route_optimizer_accelerator, EXECUTE ON FUNCTION optimize_routes (and EXECUTE
-- on solve_routes_json, which optimize_routes invokes).
-- ============================================================================

CREATE OR REPLACE FUNCTION supply_chain.route_optimizer_accelerator.optimize_routes(
    -- Which depot's parcels to route, and the within-shift due-by cutoff.
    depot_id          INT     DEFAULT 1,
    horizon_minutes   INT     DEFAULT 480,
    -- Fleet shape (uniform capacity across the fleet for v1).
    vehicle_count     INT     DEFAULT 5,
    vehicle_capacity  INT     DEFAULT 100,
    -- Solver knobs — sensible demo defaults; rarely overridden by an analyst.
    max_route_minutes INT     DEFAULT 480,
    avg_speed_kph     DOUBLE  DEFAULT 50.0,
    service_minutes   INT     DEFAULT 10,
    solver_seconds    INT     DEFAULT 10,
    drop_penalty      INT     DEFAULT 1000000
)
RETURNS TABLE(
    vehicle_id     INT,
    stop_sequence  INT,
    stop_id        STRING,
    lat            DOUBLE,
    lon            DOUBLE,
    arrival_minute INT,
    load_after     INT,
    is_dropped     BOOLEAN
)
COMMENT 'Plan optimized parcel-delivery routes (CVRPTW via OR-Tools) for a depot. Selects today''s urgent parcels for the depot due within horizon_minutes, solves with a fleet of vehicle_count vans of vehicle_capacity units each, and returns one row per stop (vehicle_id, stop_sequence, arrival_minute, load_after); unserved/dropped stops come back with vehicle_id = -1 and is_dropped = true.'
RETURN
    WITH stops AS (
        -- Today's urgent parcels for this depot, shaped exactly as the UDF's
        -- ARRAY<STRUCT<...>> input. collect_list over zero matching rows yields
        -- an empty array (one row out), so the chain below stays well-defined.
        SELECT collect_list(
                   named_struct(
                       'stop_id',      CAST(p.parcel_id AS STRING),
                       'lat',          CAST(c.lat AS DOUBLE),
                       'lon',          CAST(c.lon AS DOUBLE),
                       'demand',       CAST(p.demand_units AS INT),
                       'ready_minute', CAST(p.ready_minute AS INT),
                       'due_minute',   CAST(p.due_minute AS INT)
                   )
               ) AS stop_array
        FROM supply_chain.route_optimizer_accelerator.parcels    AS p
        JOIN supply_chain.route_optimizer_accelerator.customers  AS c
          USING (customer_id)
        WHERE p.depot_id = optimize_routes.depot_id
          AND p.created_date <= CURRENT_DATE()
          AND p.due_minute <= optimize_routes.horizon_minutes
    ),
    depot_loc AS (
        -- Depot coordinates become node 0 of the travel matrix inside the UDF.
        SELECT CAST(d.lat AS DOUBLE) AS depot_lat,
               CAST(d.lon AS DOUBLE) AS depot_lon
        FROM supply_chain.route_optimizer_accelerator.depot AS d
        WHERE d.depot_id = optimize_routes.depot_id
    ),
    solved AS (
        -- One call to the ortools UDF; returns the whole route plan as JSON.
        SELECT supply_chain.route_optimizer_accelerator.solve_routes_json(
                   s.stop_array,
                   dl.depot_lat,
                   dl.depot_lon,
                   optimize_routes.vehicle_count,
                   optimize_routes.vehicle_capacity,
                   optimize_routes.max_route_minutes,
                   optimize_routes.avg_speed_kph,
                   optimize_routes.service_minutes,
                   optimize_routes.solver_seconds,
                   optimize_routes.drop_penalty
               ) AS plan_json
        FROM stops AS s
        CROSS JOIN depot_loc AS dl
    )
    -- Explode the JSON array of route rows into one output row per stop. The
    -- schema string matches solve_cvrptw's keys and the RETURNS TABLE types.
    SELECT
        r.vehicle_id     AS vehicle_id,
        r.stop_sequence  AS stop_sequence,
        r.stop_id        AS stop_id,
        r.lat            AS lat,
        r.lon            AS lon,
        r.arrival_minute AS arrival_minute,
        r.load_after     AS load_after,
        r.is_dropped     AS is_dropped
    FROM solved
    LATERAL VIEW explode(
        from_json(
            solved.plan_json,
            'ARRAY<STRUCT<vehicle_id: INT, stop_sequence: INT, stop_id: STRING, lat: DOUBLE, lon: DOUBLE, arrival_minute: INT, load_after: INT, is_dropped: BOOLEAN>>'
        )
    ) exploded AS r;
