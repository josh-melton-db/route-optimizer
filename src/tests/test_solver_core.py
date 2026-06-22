"""Unit tests for the shared CVRPTW solver core.

Runnable with ``python -m pytest src/tests/test_solver_core.py`` (or directly,
``python src/tests/test_solver_core.py``). Imports ONLY ortools + numpy +
stdlib, matching the pure-Python contract of ``src/solver_core.py``.

These assert STRUCTURAL invariants, not exact routes (OR-Tools is deterministic
enough for a demo, but the contract guarantees structure, not a specific tour):

* every non-dropped stop appears exactly once;
* per-vehicle cumulative load never exceeds ``vehicle_capacity``;
* each ``arrival_minute`` lies within that stop's ``[ready_minute, due_minute]``;
* a deliberately unreachable / over-tight stop comes back flagged dropped.
"""

import os
import sys

import numpy as np

# Make ``import solver_core`` work whether tests run from the repo root or from
# within src/tests (pytest rootdir varies).
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from solver_core import haversine_matrix, solve_cvrptw  # noqa: E402

# --- Tiny fixture: depot + 6 parcels around a metro, 2 vehicles. ------------

DEPOT_LAT, DEPOT_LON = 29.7604, -95.3698  # Houston metro, mirrors the template.

# stop_id, lat, lon, demand, due_minute  (ready defaults to 0)
_PARCELS = [
    ("P-001", 29.7700, -95.3600, 10, 480),
    ("P-002", 29.7500, -95.3800, 20, 480),
    ("P-003", 29.7800, -95.3500, 15, 480),
    ("P-004", 29.7400, -95.3900, 25, 480),
    ("P-005", 29.7650, -95.3650, 30, 480),
    ("P-006", 29.7550, -95.3750, 12, 480),
]


def _stops():
    return [
        {"stop_id": sid, "lat": lat, "lon": lon, "demand": dem, "due_minute": due}
        for (sid, lat, lon, dem, due) in _PARCELS
    ]


def _base_params(**overrides):
    params = {
        "depot_lat": DEPOT_LAT,
        "depot_lon": DEPOT_LON,
        "vehicle_count": 2,
        "vehicle_capacity": 100,
        "max_route_minutes": 480,
        "avg_speed_kph": 50.0,
        "service_minutes": 10,
        "solver_seconds": 3,
        "drop_penalty": 1_000_000,
    }
    params.update(overrides)
    return params


# --- haversine_matrix -------------------------------------------------------


def test_haversine_matrix_shape_symmetry_and_diagonal():
    lats = [DEPOT_LAT] + [p[1] for p in _PARCELS]
    lons = [DEPOT_LON] + [p[2] for p in _PARCELS]
    m = haversine_matrix(lats, lons, avg_speed_kph=50.0)

    n = len(lats)
    assert len(m) == n
    assert all(len(row) == n for row in m)
    # Diagonal is zero.
    for i in range(n):
        assert m[i][i] == 0
    # Symmetric, whole non-negative minutes.
    for i in range(n):
        for j in range(n):
            assert m[i][j] == m[j][i]
            assert isinstance(m[i][j], int)
            assert m[i][j] >= 0


def test_haversine_matrix_rejects_bad_speed():
    try:
        haversine_matrix([0.0, 1.0], [0.0, 1.0], avg_speed_kph=0.0)
    except ValueError:
        pass
    else:
        raise AssertionError("expected ValueError for non-positive speed")


# --- solve_cvrptw structural invariants -------------------------------------


def _assert_plan_invariants(stops, params, plan):
    by_id = {s["stop_id"]: s for s in stops}
    served = [r for r in plan if not r["is_dropped"]]
    dropped = [r for r in plan if r["is_dropped"]]

    # Every stop is accounted for exactly once (served xor dropped).
    seen_ids = [r["stop_id"] for r in plan]
    assert sorted(seen_ids) == sorted(by_id.keys())
    assert len(seen_ids) == len(set(seen_ids)), "a stop appears more than once"

    # Served stops carry a real vehicle and sequence; dropped use the sentinels.
    for r in served:
        assert r["vehicle_id"] >= 0
        assert r["stop_sequence"] >= 1
        assert r["is_dropped"] is False
    for r in dropped:
        assert r["vehicle_id"] == -1
        assert r["stop_sequence"] == -1
        assert r["is_dropped"] is True

    # Arrival within each stop's [ready, due] window.
    for r in served:
        s = by_id[r["stop_id"]]
        ready = int(s.get("ready_minute", 0))
        due = int(s["due_minute"])
        assert ready <= r["arrival_minute"] <= due, (
            f"{r['stop_id']} arrival {r['arrival_minute']} outside "
            f"[{ready}, {due}]"
        )

    # Per-vehicle: sequence is contiguous from 1, load monotonic and capped.
    cap = int(params["vehicle_capacity"])
    vehicles = {}
    for r in served:
        vehicles.setdefault(r["vehicle_id"], []).append(r)
    for vid, rows in vehicles.items():
        rows.sort(key=lambda r: r["stop_sequence"])
        assert [r["stop_sequence"] for r in rows] == list(range(1, len(rows) + 1))
        prev_load = 0
        for r in rows:
            assert r["load_after"] <= cap, (
                f"vehicle {vid} load {r['load_after']} exceeds capacity {cap}"
            )
            assert r["load_after"] >= prev_load, "load must be non-decreasing"
            prev_load = r["load_after"]

    return served, dropped


def test_solve_basic_all_served():
    stops, params = _stops(), _base_params()
    plan = solve_cvrptw(stops, params)
    served, dropped = _assert_plan_invariants(stops, params, plan)
    # Total demand is 112; two vehicles at capacity 100 can serve all 6.
    assert len(dropped) == 0
    assert len(served) == len(stops)


def test_capacity_forces_split_across_vehicles():
    # Tight capacity so a single vehicle cannot carry everything: total demand
    # 112 with two vehicles of capacity 60 each must split the work.
    stops = _stops()
    params = _base_params(vehicle_capacity=60)
    plan = solve_cvrptw(stops, params)
    served, dropped = _assert_plan_invariants(stops, params, plan)
    assert len(dropped) == 0
    used_vehicles = {r["vehicle_id"] for r in served}
    assert len(used_vehicles) >= 2, "capacity should force use of both vehicles"


def test_ready_minute_respected():
    # Give one parcel a late ready time; its arrival must not precede it.
    stops = _stops()
    stops[0]["ready_minute"] = 120
    params = _base_params()
    plan = solve_cvrptw(stops, params)
    served, _ = _assert_plan_invariants(stops, params, plan)
    target = [r for r in served if r["stop_id"] == "P-001"]
    assert target, "P-001 should still be served"
    assert target[0]["arrival_minute"] >= 120


def test_unreachable_stop_is_dropped():
    # Add a parcel on the far side of the planet with an impossibly tight
    # window: it cannot be served, so it must come back dropped while the
    # reachable parcels are still served.
    stops = _stops()
    stops.append(
        {
            "stop_id": "P-FAR",
            "lat": -33.8688,  # Sydney — thousands of km from Houston
            "lon": 151.2093,
            "demand": 5,
            "due_minute": 30,  # far too tight to ever reach
        }
    )
    params = _base_params()
    plan = solve_cvrptw(stops, params)
    served, dropped = _assert_plan_invariants(stops, params, plan)
    dropped_ids = {r["stop_id"] for r in dropped}
    assert "P-FAR" in dropped_ids, "the unreachable parcel must be dropped"
    # The nearby parcels remain servable.
    assert len(served) >= len(_PARCELS) - 1


def test_empty_stops_returns_empty_plan():
    assert solve_cvrptw([], _base_params()) == []


if __name__ == "__main__":
    # Allow running without pytest installed.
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
            print(f"PASS {name}")
    print("All solver-core tests passed.")
