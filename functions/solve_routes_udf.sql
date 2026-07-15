-- UC Python scalar UDF: solve_routes_json(...) RETURNS STRING (route plan JSON).
-- OR-Tools is declared via ENVIRONMENT; a SQL TVF wraps this for Genie.

CREATE OR REPLACE FUNCTION supply_chain.route_optimizer_accelerator.solve_routes_json(
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
LANGUAGE PYTHON
ENVIRONMENT (
    dependencies = '["ortools==9.14.6206"]',
    environment_version = 'None'
)
AS $$
import json

import numpy as np
from ortools.constraint_solver import pywrapcp, routing_enums_pb2

# Mean Earth radius (meters), matching the gas-tank-delivery template.
_EARTH_RADIUS_M = 6371008.8


def haversine_matrix(
    lats: list[float],
    lons: list[float],
    avg_speed_kph: float,
) -> list[list[int]]:
    """Symmetric travel-time matrix in whole minutes; diagonal = 0.

    Node order is the caller's order (node 0 must be the depot). Great-circle
    distance between every pair of nodes is converted to minutes using a single
    average-speed constant, then rounded to the nearest whole minute.

    Args:
        lats: latitudes in degrees, one per node, depot first.
        lons: longitudes in degrees, one per node, depot first.
        avg_speed_kph: average travel speed used for the distance->minutes
            conversion. Must be > 0.

    Returns:
        ``n x n`` list-of-lists of non-negative integer travel minutes,
        symmetric, with a zero diagonal.
    """
    if avg_speed_kph <= 0:
        raise ValueError("avg_speed_kph must be positive")
    if len(lats) != len(lons):
        raise ValueError("lats and lons must have the same length")

    lat_r = np.radians(np.asarray(lats, dtype=float))
    lon_r = np.radians(np.asarray(lons, dtype=float))

    dlat = lat_r[:, None] - lat_r[None, :]
    dlon = lon_r[:, None] - lon_r[None, :]
    a = (
        np.sin(dlat / 2.0) ** 2
        + np.cos(lat_r)[:, None] * np.cos(lat_r)[None, :] * np.sin(dlon / 2.0) ** 2
    )
    meters = _EARTH_RADIUS_M * 2.0 * np.arcsin(np.minimum(1.0, np.sqrt(a)))

    speed_mps = avg_speed_kph * 1000.0 / 3600.0
    travel_min = np.rint(meters / speed_mps / 60.0).astype(np.int64)
    np.fill_diagonal(travel_min, 0)

    return travel_min.tolist()


def solve_cvrptw(stops: list[dict], params: dict) -> list[dict]:
    """Solve a CVRPTW and return a flat route plan.

    Args:
        stops: rows with keys ``stop_id``, ``lat``, ``lon``, ``demand``,
            ``due_minute`` (and optional ``ready_minute``, default 0). The depot
            is NOT a member of this list — its coordinates come from ``params``.
        params: keys ``depot_lat``, ``depot_lon`` (float); ``vehicle_count``,
            ``vehicle_capacity``, ``max_route_minutes``, ``service_minutes``,
            ``solver_seconds``, ``drop_penalty`` (int); ``avg_speed_kph``
            (float). ``vehicle_capacity`` is uniform across the fleet for v1.

    Returns:
        Route-plan rows, one per VISITED stop, ordered by
        ``(vehicle_id, stop_sequence)``, each with keys ``vehicle_id`` (int),
        ``stop_sequence`` (int), ``stop_id``, ``lat`` (float), ``lon`` (float),
        ``arrival_minute`` (int), ``load_after`` (int), ``is_dropped`` (bool).
        Stops dropped via disjunction are appended with ``vehicle_id = -1``,
        ``stop_sequence = -1``, ``is_dropped = True`` so the caller can report
        unserved parcels.
    """
    depot_lat = float(params["depot_lat"])
    depot_lon = float(params["depot_lon"])
    vehicle_count = int(params["vehicle_count"])
    vehicle_capacity = int(params["vehicle_capacity"])
    max_route_minutes = int(params["max_route_minutes"])
    avg_speed_kph = float(params["avg_speed_kph"])
    service_minutes = int(params["service_minutes"])
    solver_seconds = int(params["solver_seconds"])
    drop_penalty = int(params["drop_penalty"])

    n_stops = len(stops)

    # No stops to route -> empty plan.
    if n_stops == 0:
        return []

    # Node 0 = depot, then one node per stop in caller order.
    lats = [depot_lat] + [float(s["lat"]) for s in stops]
    lons = [depot_lon] + [float(s["lon"]) for s in stops]
    travel_min = haversine_matrix(lats, lons, avg_speed_kph)

    demands = [0] + [int(round(float(s["demand"]))) for s in stops]
    ready = [0] + [int(s.get("ready_minute", 0)) for s in stops]
    due = [0] + [int(s["due_minute"]) for s in stops]

    n_nodes = n_stops + 1
    capacities = [vehicle_capacity] * vehicle_count

    manager = pywrapcp.RoutingIndexManager(n_nodes, vehicle_count, 0)
    routing = pywrapcp.RoutingModel(manager)

    # Arc cost = travel minutes + per-stop service time (charged on departure
    # from a real stop; nothing is charged for leaving the depot).
    def time_cb(from_index, to_index):
        a_node = manager.IndexToNode(from_index)
        b_node = manager.IndexToNode(to_index)
        service = service_minutes if a_node != 0 else 0
        return int(travel_min[a_node][b_node] + service)

    time_idx = routing.RegisterTransitCallback(time_cb)
    routing.SetArcCostEvaluatorOfAllVehicles(time_idx)

    # Capacity dimension caps demand units per vehicle.
    def demand_cb(from_index):
        return int(demands[manager.IndexToNode(from_index)])

    demand_idx = routing.RegisterUnaryTransitCallback(demand_cb)
    routing.AddDimensionWithVehicleCapacity(
        demand_idx,
        0,  # null capacity slack
        capacities,  # per-vehicle capacity
        True,  # start cumul at zero
        "Capacity",
    )

    # Time dimension with per-stop [ready, due] windows.
    routing.AddDimension(
        time_idx,
        max_route_minutes,  # waiting slack allowed at each stop
        max_route_minutes,  # max cumulative time per vehicle (shift length)
        False,  # don't force start cumul to zero (set explicitly below)
        "Time",
    )
    time_dim = routing.GetDimensionOrDie("Time")
    for stop_i in range(n_stops):
        index = manager.NodeToIndex(stop_i + 1)
        time_dim.CumulVar(index).SetRange(ready[stop_i + 1], due[stop_i + 1])
    for v in range(vehicle_count):
        time_dim.CumulVar(routing.Start(v)).SetRange(0, 0)

    # Each stop is optional via a disjunction with a large drop penalty: the
    # solver only drops a stop when serving it is infeasible or costs more than
    # the penalty. Better to drop a stop than to return no solution at all.
    for node in range(1, n_nodes):
        routing.AddDisjunction([manager.NodeToIndex(node)], drop_penalty)

    search = pywrapcp.DefaultRoutingSearchParameters()
    search.first_solution_strategy = (
        routing_enums_pb2.FirstSolutionStrategy.PARALLEL_CHEAPEST_INSERTION
    )
    search.local_search_metaheuristic = (
        routing_enums_pb2.LocalSearchMetaheuristic.GUIDED_LOCAL_SEARCH
    )
    search.time_limit.seconds = solver_seconds

    solution = routing.SolveWithParameters(search)
    if solution is None:
        raise RuntimeError(
            "OR-Tools found no feasible solution — relax windows, raise "
            "vehicle_count/capacity, or increase solver_seconds."
        )

    capacity_dim = routing.GetDimensionOrDie("Capacity")

    rows: list[dict] = []
    visited_nodes: set[int] = set()
    for v in range(vehicle_count):
        index = routing.Start(v)
        seq = 0
        while not routing.IsEnd(index):
            node = manager.IndexToNode(index)
            if node != 0:
                seq += 1
                stop = stops[node - 1]
                visited_nodes.add(node)
                # The Capacity CumulVar holds the load BEFORE this stop's demand
                # (the unary transit is charged at the from-node), so add this
                # node's own demand to report the load AFTER serving the stop.
                load_before = int(solution.Value(capacity_dim.CumulVar(index)))
                rows.append(
                    {
                        "vehicle_id": v,
                        "stop_sequence": seq,
                        "stop_id": stop["stop_id"],
                        "lat": float(stop["lat"]),
                        "lon": float(stop["lon"]),
                        "arrival_minute": int(
                            solution.Value(time_dim.CumulVar(index))
                        ),
                        "load_after": load_before + demands[node],
                        "is_dropped": False,
                    }
                )
            index = solution.Value(routing.NextVar(index))

    # Any stop whose node never appeared on a route was dropped.
    for node in range(1, n_nodes):
        if node not in visited_nodes:
            stop = stops[node - 1]
            rows.append(
                {
                    "vehicle_id": -1,
                    "stop_sequence": -1,
                    "stop_id": stop["stop_id"],
                    "lat": float(stop["lat"]),
                    "lon": float(stop["lon"]),
                    "arrival_minute": -1,
                    "load_after": -1,
                    "is_dropped": True,
                }
            )

    return rows


# ---------------------------------------------------------------------------
# Data adapter (the ONLY surface-specific code; the math above is verbatim).
# UC Python UDFs deliver an ARRAY argument as a Python list and each STRUCT
# element as a dict-like / Row / positional value depending on runtime. Normalize
# every element to the plain dict shape solve_cvrptw expects, then call it.
# ---------------------------------------------------------------------------
def _struct_to_dict(s):
    if isinstance(s, dict):
        return s
    as_dict = getattr(s, "asDict", None)
    if callable(as_dict):
        return as_dict()
    # Positional fallback, in the declared field order of the STRUCT.
    return {
        "stop_id": s[0],
        "lat": s[1],
        "lon": s[2],
        "demand": s[3],
        "ready_minute": s[4],
        "due_minute": s[5],
    }


stop_dicts = []
for s in (stops or []):
    d = _struct_to_dict(s)
    ready_minute = d.get("ready_minute", 0)
    stop_dicts.append(
        {
            "stop_id": str(d["stop_id"]),
            "lat": float(d["lat"]),
            "lon": float(d["lon"]),
            "demand": int(d["demand"]),
            "ready_minute": int(ready_minute if ready_minute is not None else 0),
            "due_minute": int(d["due_minute"]),
        }
    )

params = {
    "depot_lat": float(depot_lat),
    "depot_lon": float(depot_lon),
    "vehicle_count": int(vehicle_count),
    "vehicle_capacity": int(vehicle_capacity),
    "max_route_minutes": int(max_route_minutes),
    "avg_speed_kph": float(avg_speed_kph),
    "service_minutes": int(service_minutes),
    "solver_seconds": int(solver_seconds),
    "drop_penalty": int(drop_penalty),
}

plan_rows = solve_cvrptw(stop_dicts, params)
return json.dumps(plan_rows)
$$;
