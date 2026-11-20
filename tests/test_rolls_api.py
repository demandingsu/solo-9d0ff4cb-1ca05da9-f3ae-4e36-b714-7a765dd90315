"""卷序监测 API 测试：建序列、乱序补录、更正、报警回算、失败不改原结果。"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.main import create_app  # noqa: E402

TARGET, TOLERANCE, THRESHOLD = 100, 5, 10


@pytest.fixture()
def client(tmp_path):
    app = create_app(str(tmp_path / "rolls.db"))
    with TestClient(app) as test_client:
        yield test_client


def make_inspection(client, total_pieces=10, note=None):
    response = client.post(
        "/inspections", json={"total_pieces": total_pieces, "note": note}
    )
    assert response.status_code == 201, response.text
    return response.json()["id"]


def make_series(client, target=TARGET, tolerance=TOLERANCE, threshold=THRESHOLD, note=None):
    response = client.post(
        "/roll-series",
        json={
            "target_thickness": target,
            "tolerance": tolerance,
            "alarm_threshold": threshold,
            "note": note,
        },
    )
    assert response.status_code == 201, response.text
    return response.json()


def put_reading(client, series_id, roll_index, measured, inspection_id):
    return client.put(
        f"/roll-series/{series_id}/readings/{roll_index}",
        json={"measured": measured, "inspection_id": inspection_id},
    )


def alarm_indexes(response):
    assert response.status_code == 200, response.text
    return [alarm["roll_index"] for alarm in response.json()["alarms"]]


def expected_alarms(measured, target=TARGET, tolerance=TOLERANCE, threshold=THRESHOLD):
    """独立于实现的从头计算，作为对拍基准。"""
    cumulative = 0
    alarms = []
    for roll in sorted(measured):
        cumulative = max(0, cumulative + measured[roll] - target - tolerance)
        if cumulative >= threshold:
            alarms.append(roll)
            cumulative = 0
    return alarms


def test_create_series_and_validation(client):
    created = make_series(client, note="1 号机")
    assert created["target_thickness"] == TARGET
    assert created["tolerance"] == TOLERANCE
    assert created["alarm_threshold"] == THRESHOLD
    assert created["reading_count"] == 0
    assert created["alarm_count"] == 0
    assert [s["id"] for s in client.get("/roll-series").json()] == [created["id"]]

    assert client.post("/roll-series", json={"target_thickness": -1, "tolerance": 5, "alarm_threshold": 10}).status_code == 422
    assert client.post("/roll-series", json={"target_thickness": 100, "tolerance": -1, "alarm_threshold": 10}).status_code == 422
    assert client.post("/roll-series", json={"target_thickness": 100, "tolerance": 5, "alarm_threshold": 0}).status_code == 422
    # 报警线为 0 会让每卷都报警，拒绝
    assert client.post("/roll-series", json={"target_thickness": 100, "tolerance": 5, "alarm_threshold": -3}).status_code == 422


def test_reading_validation(client):
    inspection_id = make_inspection(client)
    series = make_series(client)
    sid = series["id"]

    assert put_reading(client, sid, 0, 100, inspection_id).status_code == 422
    assert put_reading(client, sid, 1, -1, inspection_id).status_code == 422
    assert put_reading(client, sid, 1, 100, 0).status_code == 422
    # 关联的质检登记号不存在：整次拒绝
    missing = put_reading(client, sid, 1, 100, 999)
    assert missing.status_code == 422
    assert "不存在" in missing.json()["detail"]
    assert client.get(f"/roll-series/{sid}/readings").json()["readings"] == []

    assert put_reading(client, 999, 1, 100, inspection_id).status_code == 404
    assert client.get("/roll-series/999").status_code == 404
    assert client.get("/roll-series/999/alarms").status_code == 404


def test_readings_record_cumulative_and_alarm_together(client):
    inspection_id = make_inspection(client)
    sid = make_series(client)["id"]

    sequence = [(106, 1), (107, 3), (108, 6), (100, 1), (120, 0), (130, 0)]
    inserted: dict[int, int] = {}
    for roll, (measured, cumulative) in enumerate(sequence, start=1):
        inserted[roll] = measured
        response = put_reading(client, sid, roll, measured, inspection_id)
        assert response.status_code == 200, response.text
        body = response.json()
        assert body["reading"]["cumulative_after"] == cumulative
        assert body["reading"]["alarmed"] == (roll in (5, 6))
        assert body["alarms"] == [
            {"roll_index": index, "measured": inserted[index],
             "inspection_id": inspection_id}
            for index in expected_alarms(inserted)
        ]

    readings = client.get(f"/roll-series/{sid}/readings").json()["readings"]
    assert [r["roll_index"] for r in readings] == [1, 2, 3, 4, 5, 6]
    assert [r["cumulative_after"] for r in readings] == [1, 3, 6, 1, 0, 0]

    detail = client.get(f"/roll-series/{sid}").json()
    assert detail["reading_count"] == 6
    assert detail["alarm_count"] == 2
    assert [(a["roll_index"], a["inspection_id"]) for a in detail["alarms"]] == [
        (5, inspection_id),
        (6, inspection_id),
    ]


def test_out_of_order_backfill_matches_full_recalculation(client):
    inspection_id = make_inspection(client)
    sid = make_series(client)["id"]

    # 先录 1、2、4、6，再补录 3、5
    measured = {1: 103, 2: 105, 4: 106, 6: 110}
    for roll, value in measured.items():
        assert put_reading(client, sid, roll, value, inspection_id).status_code == 200

    measured[3] = 108
    assert put_reading(client, sid, 3, 108, inspection_id).status_code == 200
    assert alarm_indexes(client.get(f"/roll-series/{sid}/alarms")) == expected_alarms(measured)

    measured[5] = 120
    response = put_reading(client, sid, 5, 120, inspection_id)
    assert response.status_code == 200
    assert alarm_indexes(client.get(f"/roll-series/{sid}/alarms")) == expected_alarms(measured)
    assert expected_alarms(measured) == [5]

    # 报警卷对应的质检登记号正确
    alarms = client.get(f"/roll-series/{sid}/alarms").json()["alarms"]
    assert alarms == [
        {"roll_index": 5, "measured": 120, "inspection_id": inspection_id}
    ]


def test_late_reading_moves_alarm_position(client):
    inspection_id = make_inspection(client)
    sid = make_series(client)["id"]

    # 1..5 卷都只轻微超厚，累计到第 4 卷触线
    initial = {1: 106, 2: 107, 3: 108, 4: 109, 5: 110}
    for roll, value in initial.items():
        assert put_reading(client, sid, roll, value, inspection_id).status_code == 200
    # 超出部分 1+2+3+4=10，第 4 卷报警
    assert alarm_indexes(client.get(f"/roll-series/{sid}/alarms")) == [4]

    # 迟到的第 4 卷更薄：累计值不足以报警，报警消失
    late = dict(initial)
    late[4] = 100
    response = put_reading(client, sid, 4, 100, inspection_id)
    assert response.status_code == 200
    assert alarm_indexes(client.get(f"/roll-series/{sid}/alarms")) == expected_alarms(late)
    assert expected_alarms(late) == []

    # 迟到的第 3 卷更厚：报警提前到第 3 卷
    late[3] = 130
    response = put_reading(client, sid, 3, 130, inspection_id)
    assert response.status_code == 200
    assert alarm_indexes(client.get(f"/roll-series/{sid}/alarms")) == expected_alarms(late)
    assert expected_alarms(late) == [3]

    detail = client.get(f"/roll-series/{sid}").json()
    assert [(a["roll_index"], a["measured"]) for a in detail["alarms"]] == [(3, 130)]
    tail = {r["roll_index"]: r["cumulative_after"] for r in detail["readings"]}
    assert tail[4] == 0 and tail[5] == 5


def test_patch_reading_recalculates_and_preserves_other_fields(client):
    first = make_inspection(client, note="首批")
    second = make_inspection(client, note="复检")
    sid = make_series(client)["id"]
    for roll, value in {1: 103, 2: 104, 3: 106, 4: 110}.items():
        assert put_reading(client, sid, roll, value, first).status_code == 200

    # 只改关联登记号：累计值不变
    before = client.get(f"/roll-series/{sid}/readings").json()["readings"]
    response = client.patch(
        f"/roll-series/{sid}/readings/3", json={"inspection_id": second}
    )
    assert response.status_code == 200
    after = client.get(f"/roll-series/{sid}/readings").json()["readings"]
    assert [r["cumulative_after"] for r in before] == [
        r["cumulative_after"] for r in after
    ]
    assert after[2]["inspection_id"] == second
    assert after[2]["measured"] == 106

    # 只改读数：累计值随之变化
    response = client.patch(f"/roll-series/{sid}/readings/1", json={"measured": 80})
    assert response.status_code == 200
    assert response.json()["reading"]["cumulative_after"] == 0
    readings = client.get(f"/roll-series/{sid}/readings").json()["readings"]
    assert [r["cumulative_after"] for r in readings] == [0, 0, 1, 6]

    # 不存在的卷、空请求体、非法关联
    assert client.patch(f"/roll-series/{sid}/readings/99", json={"measured": 100}).status_code == 404
    assert client.patch(f"/roll-series/{sid}/readings/1", json={}).status_code == 422
    assert client.patch(f"/roll-series/{sid}/readings/1", json={"inspection_id": None}).status_code == 422
    assert client.patch(f"/roll-series/{sid}/readings/1", json={"inspection_id": 999}).status_code == 422
    # 失败的更正不改变原结果
    assert client.get(f"/roll-series/{sid}/readings").json()["readings"][2]["inspection_id"] == second


def test_failed_write_keeps_original_results_queryable(client):
    inspection_id = make_inspection(client)
    sid = make_series(client)["id"]
    for roll, value in {1: 104, 2: 108, 3: 120}.items():
        assert put_reading(client, sid, roll, value, inspection_id).status_code == 200

    before_readings = client.get(f"/roll-series/{sid}/readings").json()
    before_alarms = client.get(f"/roll-series/{sid}/alarms").json()

    failed = put_reading(client, sid, 2, 150, 999)  # 关联登记号不存在
    assert failed.status_code == 422
    assert client.get(f"/roll-series/{sid}/readings").json() == before_readings
    assert client.get(f"/roll-series/{sid}/alarms").json() == before_alarms

    # 失败不留下半写状态：累计值仍与原有读数一致
    readings = client.get(f"/roll-series/{sid}/readings").json()["readings"]
    expected = expected_alarms({1: 104, 2: 108, 3: 120})
    assert [r["cumulative_after"] for r in readings] == [0, 3, 0]
    assert [r for r in readings if r["alarmed"]][0]["roll_index"] == 3
    assert expected == [3]


def test_delete_reading_recalculates_suffix(client):
    inspection_id = make_inspection(client)
    sid = make_series(client)["id"]
    measured = {1: 106, 2: 107, 3: 108, 4: 109, 5: 110}
    for roll, value in measured.items():
        assert put_reading(client, sid, roll, value, inspection_id).status_code == 200
    assert alarm_indexes(client.get(f"/roll-series/{sid}/alarms")) == [4]

    response = client.delete(f"/roll-series/{sid}/readings/3")
    assert response.status_code == 200
    remaining = {r: m for r, m in measured.items() if r != 3}
    assert [a["roll_index"] for a in response.json()["alarms"]] == expected_alarms(remaining)
    assert alarm_indexes(client.get(f"/roll-series/{sid}/alarms")) == expected_alarms(remaining)

    assert client.delete(f"/roll-series/{sid}/readings/3").status_code == 404
    assert client.delete("/roll-series/999/readings/1").status_code == 404


def test_alarm_uses_associated_inspection_id(client):
    first = make_inspection(client, note="1 号质检")
    second = make_inspection(client, note="2 号质检")
    sid = make_series(client)["id"]

    assert put_reading(client, sid, 1, 120, first).status_code == 200
    assert put_reading(client, sid, 2, 120, second).status_code == 200

    alarms = client.get(f"/roll-series/{sid}/alarms").json()["alarms"]
    assert alarms == [
        {"roll_index": 1, "measured": 120, "inspection_id": first},
        {"roll_index": 2, "measured": 120, "inspection_id": second},
    ]
    # 报警卷关联的登记号可回到原登记查询
    detail = client.get(f"/inspections/{alarms[1]['inspection_id']}").json()
    assert detail["note"] == "2 号质检"


def test_series_isolated_from_each_other(client):
    inspection_id = make_inspection(client)
    first = make_series(client)["id"]
    second = make_series(client, target=50, tolerance=0, threshold=5)["id"]

    assert put_reading(client, first, 1, 120, inspection_id).status_code == 200
    assert put_reading(client, second, 1, 100, inspection_id).status_code == 200
    assert alarm_indexes(client.get(f"/roll-series/{first}/alarms")) == [1]
    assert alarm_indexes(client.get(f"/roll-series/{second}/alarms")) == [1]
    assert client.get(f"/roll-series/{second}/readings").json()["readings"][0][
        "measured"
    ] == 100


def test_docs_lists_roll_series_endpoints(client):
    assert client.get("/docs").status_code == 200
    paths = client.get("/openapi.json").json()["paths"]
    assert "/roll-series" in paths
    assert "/roll-series/{series_id}/readings/{roll_index}" in paths
    assert "/roll-series/{series_id}/alarms" in paths
