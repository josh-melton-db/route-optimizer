"""Unit tests for the MLflow PyFunc adapter around solve_cvrptw."""

from __future__ import annotations

import json
import os
import sys

import pandas as pd

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from solver_endpoint_model import (  # noqa: E402
    RouteSolverModel,
    make_input_row,
)
from test_solver_core import (  # noqa: E402
    _assert_plan_invariants,
    _base_params,
    _stops,
)


def test_predict_returns_valid_plan_json():
    stops = _stops()
    params = _base_params()
    request = make_input_row(stops, **params)
    frame = pd.DataFrame([request])

    out = RouteSolverModel().predict(None, frame)
    assert list(out.columns) == ["plan_json"]
    assert len(out) == 1

    plan = json.loads(out.iloc[0]["plan_json"])
    served, dropped = _assert_plan_invariants(stops, params, plan)
    assert len(dropped) == 0
    assert len(served) == len(stops)


def test_predict_empty_stops():
    request = make_input_row([], depot_lat=29.76, depot_lon=-95.37, vehicle_count=2)
    out = RouteSolverModel().predict(None, pd.DataFrame([request]))
    assert json.loads(out.iloc[0]["plan_json"]) == []


def test_predict_batch_two_rows():
    stops = _stops()
    params = _base_params(vehicle_capacity=60)
    rows = [
        make_input_row(stops, **params),
        make_input_row(stops[:2], **_base_params(vehicle_count=1)),
    ]
    out = RouteSolverModel().predict(None, pd.DataFrame(rows))
    assert len(out) == 2
    for plan_json in out["plan_json"]:
        plan = json.loads(plan_json)
        assert isinstance(plan, list)
        assert all("stop_id" in row for row in plan)
