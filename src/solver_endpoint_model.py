"""MLflow PyFunc wrapper around the shared CVRPTW solver.

Accepts one route-request row per input record (JSON-safe scalars) and returns
one ``plan_json`` string per row — the same JSON array schema that the Genie
TVF already explodes via ``from_json``.
"""

from __future__ import annotations

import json
from typing import Any

import pandas as pd

from solver_core import solve_cvrptw

try:
    import mlflow.pyfunc as _mlflow_pyfunc

    _PythonModel = _mlflow_pyfunc.PythonModel
except Exception:  # pragma: no cover - local envs without full mlflow

    class _PythonModel:  # type: ignore[no-redef]
        """Minimal stand-in so unit tests can import without MLflow."""

        pass

INPUT_COLUMNS = [
    "stops_json",
    "depot_lat",
    "depot_lon",
    "vehicle_count",
    "vehicle_capacity",
    "max_route_minutes",
    "avg_speed_kph",
    "service_minutes",
    "solver_seconds",
    "drop_penalty",
]

OUTPUT_COLUMNS = ["plan_json"]


def make_input_row(
    stops: list[dict[str, Any]],
    depot_lat: float,
    depot_lon: float,
    vehicle_count: int = 5,
    vehicle_capacity: int = 100,
    max_route_minutes: int = 480,
    avg_speed_kph: float = 50.0,
    service_minutes: int = 10,
    solver_seconds: int = 10,
    drop_penalty: int = 1_000_000,
) -> dict[str, Any]:
    """Build one dataframe_records-compatible request row."""
    return {
        "stops_json": json.dumps(stops),
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


def _parse_stops(value: object) -> list[dict[str, Any]]:
    if value is None or (isinstance(value, float) and pd.isna(value)):
        return []
    if isinstance(value, list):
        stops = value
    elif isinstance(value, str):
        if not value:
            return []
        decoded = json.loads(value)
        if not isinstance(decoded, list):
            raise TypeError("stops_json must decode to a JSON array")
        stops = decoded
    else:
        raise TypeError(f"stops_json must be str or list, got {type(value).__name__}")

    normalized: list[dict[str, Any]] = []
    for stop in stops:
        if not isinstance(stop, dict):
            raise TypeError("each stop must be a JSON object")
        ready = stop.get("ready_minute", 0)
        normalized.append(
            {
                "stop_id": str(stop["stop_id"]),
                "lat": float(stop["lat"]),
                "lon": float(stop["lon"]),
                "demand": int(stop["demand"]),
                "ready_minute": int(ready if ready is not None else 0),
                "due_minute": int(stop["due_minute"]),
            }
        )
    return normalized


def solve_row(row: dict[str, Any] | pd.Series) -> str:
    """Solve one request row and return the route-plan JSON string."""
    stops = _parse_stops(row.get("stops_json") if hasattr(row, "get") else row["stops_json"])
    params = {
        "depot_lat": float(row["depot_lat"]),
        "depot_lon": float(row["depot_lon"]),
        "vehicle_count": int(row["vehicle_count"]),
        "vehicle_capacity": int(row["vehicle_capacity"]),
        "max_route_minutes": int(row["max_route_minutes"]),
        "avg_speed_kph": float(row["avg_speed_kph"]),
        "service_minutes": int(row["service_minutes"]),
        "solver_seconds": int(row["solver_seconds"]),
        "drop_penalty": int(row["drop_penalty"]),
    }
    return json.dumps(solve_cvrptw(stops, params))


class RouteSolverModel(_PythonModel):
    """Served CVRPTW solver: one plan_json string per request row."""

    def predict(self, context: Any, model_input: pd.DataFrame) -> pd.DataFrame:
        if isinstance(model_input, dict):
            records = (
                model_input.get("inputs")
                or model_input.get("dataframe_records")
                or [model_input]
            )
            frame = pd.DataFrame(records)
        elif isinstance(model_input, pd.DataFrame):
            frame = model_input
        else:
            frame = pd.DataFrame(model_input)

        missing = [col for col in INPUT_COLUMNS if col not in frame.columns]
        if missing:
            raise ValueError(f"missing required columns: {missing}")

        return pd.DataFrame(
            {"plan_json": [solve_row(row) for _, row in frame.iterrows()]}
        )
