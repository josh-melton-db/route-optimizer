-- SQL TVF: optimize_routes_via_endpoint(...) — Genie function tool (endpoint path).
-- Same adapter as optimize_routes, but calls solve_routes_endpoint_json (ai_query).

CREATE OR REPLACE FUNCTION supply_chain.route_optimizer_accelerator.optimize_routes_via_endpoint(
    depot_id          INT     DEFAULT 1,
    horizon_minutes   INT     DEFAULT 480,
    vehicle_count     INT     DEFAULT 5,
    vehicle_capacity  INT     DEFAULT 100,
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
COMMENT 'Plan optimized parcel-delivery routes via Model Serving (ai_query). Selects urgent parcels for a depot and returns one row per stop; dropped stops have vehicle_id = -1 and is_dropped = true.'
RETURN
    WITH stops AS (
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
        WHERE p.depot_id = optimize_routes_via_endpoint.depot_id
          AND p.created_date <= CURRENT_DATE()
          AND p.due_minute <= optimize_routes_via_endpoint.horizon_minutes
    ),
    depot_loc AS (
        SELECT CAST(d.lat AS DOUBLE) AS depot_lat,
               CAST(d.lon AS DOUBLE) AS depot_lon
        FROM supply_chain.route_optimizer_accelerator.depot AS d
        WHERE d.depot_id = optimize_routes_via_endpoint.depot_id
    ),
    solved AS (
        SELECT supply_chain.route_optimizer_accelerator.solve_routes_endpoint_json(
                   s.stop_array,
                   dl.depot_lat,
                   dl.depot_lon,
                   optimize_routes_via_endpoint.vehicle_count,
                   optimize_routes_via_endpoint.vehicle_capacity,
                   optimize_routes_via_endpoint.max_route_minutes,
                   optimize_routes_via_endpoint.avg_speed_kph,
                   optimize_routes_via_endpoint.service_minutes,
                   optimize_routes_via_endpoint.solver_seconds,
                   optimize_routes_via_endpoint.drop_penalty
               ) AS plan_json
        FROM stops AS s
        CROSS JOIN depot_loc AS dl
    )
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
