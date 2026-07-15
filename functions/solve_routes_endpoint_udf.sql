-- SQL scalar UC function: solve_routes_endpoint_json(...)
-- Calls the Model Serving endpoint via ai_query (OR-Tools lives on the endpoint).
-- Placeholder __SOLVER_ENDPOINT__ is replaced at registration time with a SQL literal.

CREATE OR REPLACE FUNCTION supply_chain.route_optimizer_accelerator.solve_routes_endpoint_json(
    stops ARRAY<STRUCT<
        stop_id STRING,
        lat DOUBLE,
        lon DOUBLE,
        demand INT,
        ready_minute INT,
        due_minute INT
    >>,
    depot_lat DOUBLE,
    depot_lon DOUBLE,
    vehicle_count INT,
    vehicle_capacity INT,
    max_route_minutes INT,
    avg_speed_kph DOUBLE,
    service_minutes INT,
    solver_seconds INT,
    drop_penalty INT
)
RETURNS STRING
COMMENT 'Invoke the route-solver Model Serving endpoint via ai_query and return the route plan as JSON.'
RETURN
    ai_query(
        '__SOLVER_ENDPOINT__',
        named_struct(
            'stops_json', to_json(stops),
            'depot_lat', depot_lat,
            'depot_lon', depot_lon,
            'vehicle_count', vehicle_count,
            'vehicle_capacity', vehicle_capacity,
            'max_route_minutes', max_route_minutes,
            'avg_speed_kph', avg_speed_kph,
            'service_minutes', service_minutes,
            'solver_seconds', solver_seconds,
            'drop_penalty', drop_penalty
        ),
        returnType => 'STRING'
    );
