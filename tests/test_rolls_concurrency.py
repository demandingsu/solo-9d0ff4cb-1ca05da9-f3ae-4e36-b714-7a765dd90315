"""卷序监测并发测试：写入串行化，最终状态与从头回算一致。"""

from __future__ import annotations

import random
import sys
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.main import create_app  # noqa: E402

TARGET, TOLERANCE, THRESHOLD = 100, 5, 10


@pytest.fixture()
def client(tmp_path):
    app = create_app(str(tmp_path / "rolls_concurrent.db"))
    with TestClient(app) as test_client:
        yield test_client


def expected_states(measured: dict[int, int]):
    cumulative = 0
    states: dict[int, tuple[int, bool]] = {}
    for roll in sorted(measured):
        cumulative = max(0, cumulative + measured[roll] - TARGET - TOLERANCE)
        alarmed = cumulative >= THRESHOLD
        if alarmed:
            cumulative = 0
        states[roll] = (cumulative, alarmed)
    return states


def setup(client):
    inspection_id = client.post("/inspections", json={"total_pieces": 10}).json()["id"]
    series_id = client.post(
        "/roll-series",
        json={
            "target_thickness": TARGET,
            "tolerance": TOLERANCE,
            "alarm_threshold": THRESHOLD,
        },
    ).json()["id"]
    return series_id, inspection_id


def test_concurrent_corrections_leave_consistent_state(client):
    series_id, inspection_id = setup(client)
    measured = {roll: 100 for roll in range(1, 21)}
    for roll in measured:
        assert (
            client.put(
                f"/roll-series/{series_id}/readings/{roll}",
                json={"measured": 100, "inspection_id": inspection_id},
            ).status_code
            == 200
        )

    rng = random.Random(99)
    changes = {roll: rng.randint(0, 140) for roll in range(1, 21)}

    def submit(roll: int):
        response = client.put(
            f"/roll-series/{series_id}/readings/{roll}",
            json={"measured": changes[roll], "inspection_id": inspection_id},
        )
        assert response.status_code == 200, response.text

    with ThreadPoolExecutor(max_workers=8) as pool:
        list(pool.map(submit, changes))

    readings = client.get(f"/roll-series/{series_id}/readings").json()["readings"]
    assert [r["roll_index"] for r in readings] == list(range(1, 21))
    stored = {
        r["roll_index"]: (r["cumulative_after"], r["alarmed"]) for r in readings
    }
    expect = expected_states(changes)
    assert stored == expect

    alarms = client.get(f"/roll-series/{series_id}/alarms").json()["alarms"]
    assert [a["roll_index"] for a in alarms] == [
        roll for roll in sorted(expect) if expect[roll][1]
    ]
    assert all(a["inspection_id"] == inspection_id for a in alarms)


def test_concurrent_backfill_of_new_rolls_all_accepted(client):
    series_id, inspection_id = setup(client)

    def submit(roll: int):
        return client.put(
            f"/roll-series/{series_id}/readings/{roll}",
            json={"measured": 100 + roll, "inspection_id": inspection_id},
        ).status_code

    with ThreadPoolExecutor(max_workers=8) as pool:
        statuses = list(pool.map(submit, range(1, 21)))

    assert statuses == [200] * 20
    readings = client.get(f"/roll-series/{series_id}/readings").json()["readings"]
    measured = {r["roll_index"]: r["measured"] for r in readings}
    assert measured == {roll: 100 + roll for roll in range(1, 21)}
    stored = {
        r["roll_index"]: (r["cumulative_after"], r["alarmed"]) for r in readings
    }
    assert stored == expected_states(measured)
