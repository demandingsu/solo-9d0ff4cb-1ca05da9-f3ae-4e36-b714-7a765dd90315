"""增量回算的数据库访问测试：确认不会从头扫描、收敛后不再读取余下各卷。"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app import db as app_db  # noqa: E402
from app.main import create_app  # noqa: E402

TARGET, TOLERANCE, THRESHOLD = 100, 5, 10


@pytest.fixture()
def client(tmp_path):
    app = create_app(str(tmp_path / "incremental.db"))
    with TestClient(app) as test_client:
        yield test_client


def seed(client, count, measured=100):
    inspection_id = client.post(
        "/inspections", json={"total_pieces": 10}
    ).json()["id"]
    series_id = client.post(
        "/roll-series",
        json={
            "target_thickness": TARGET,
            "tolerance": TOLERANCE,
            "alarm_threshold": THRESHOLD,
        },
    ).json()["id"]
    for roll in range(1, count + 1):
        response = client.put(
            f"/roll-series/{series_id}/readings/{roll}",
            json={"measured": measured, "inspection_id": inspection_id},
        )
        assert response.status_code == 200, response.text
    return series_id, inspection_id


def test_correction_does_not_read_earlier_rolls(client, monkeypatch):
    series_id, inspection_id = seed(client, 50)
    earlier_state_reads: list[int] = []
    original_get_state = app_db.get_roll_state

    def tracking_get_state(connection, sid, roll_index):
        return original_get_state(connection, sid, roll_index)

    original_before = app_db.get_state_before

    def tracking_before(connection, sid, roll_index):
        earlier_state_reads.append(roll_index)
        return original_before(connection, sid, roll_index)

    monkeypatch.setattr(app_db, "get_state_before", tracking_before)
    monkeypatch.setattr(app_db, "get_roll_state", tracking_get_state)

    # 第 40 卷改成 100：累计值本来也是 0（每卷都不累计），立即收敛。
    response = client.put(
        f"/roll-series/{series_id}/readings/40",
        json={"measured": 100, "inspection_id": inspection_id},
    )
    assert response.status_code == 200
    # 只按键查了第 40 卷之前最近的一卷，没有遍历 1..39。
    assert earlier_state_reads == [40]
    assert response.json()["reading"]["cumulative_after"] == 0
    assert response.json()["alarms"] == []


def test_replay_stops_reading_rows_after_convergence(client, monkeypatch):
    series_id, inspection_id = seed(client, 100)
    pulled: list[int] = []
    original_iter = app_db.iter_roll_readings_from

    def tracking_iter(connection, sid, from_roll_index):
        for roll_index, measured in original_iter(connection, sid, from_roll_index):
            pulled.append(roll_index)
            yield roll_index, measured

    monkeypatch.setattr(app_db, "iter_roll_readings_from", tracking_iter)

    # 第 3 卷改成更薄：累计值仍为 0，与改动前一致，回放立即收敛。
    response = client.put(
        f"/roll-series/{series_id}/readings/3",
        json={"measured": 60, "inspection_id": inspection_id},
    )
    assert response.status_code == 200
    # 收敛于被改动卷本身，其后 4..100 卷的读数一行都没有被读取。
    assert pulled == []
    assert response.json()["alarms"] == []


def test_replay_reads_only_until_state_matches(client, monkeypatch):
    series_id, inspection_id = seed(client, 100)
    pulled: list[int] = []
    original_iter = app_db.iter_roll_readings_from

    def tracking_iter(connection, sid, from_roll_index):
        for roll_index, measured in original_iter(connection, sid, from_roll_index):
            pulled.append(roll_index)
            yield roll_index, measured

    monkeypatch.setattr(app_db, "iter_roll_readings_from", tracking_iter)

    # 第 10 卷改成 120：报警使累计值归零，与改动前第 10 卷的累计值（0）相同，
    # 因此回放到第 10 卷即停止，余下 11..100 卷不再读取。
    response = client.put(
        f"/roll-series/{series_id}/readings/10",
        json={"measured": 120, "inspection_id": inspection_id},
    )
    assert response.status_code == 200
    assert [a["roll_index"] for a in response.json()["alarms"]] == [10]
    # 收敛于第 10 卷，11..100 卷的读数一行都没有被读取。
    assert pulled == []


def test_state_survives_restart(tmp_path):
    path = str(tmp_path / "restart.db")
    app = create_app(path)
    with TestClient(app) as client:
        series_id, inspection_id = seed(client, 5, measured=120)

    # 重新打开同一数据库：累计值与报警都来自持久化状态。
    with TestClient(create_app(path)) as client:
        readings = client.get(f"/roll-series/{series_id}/readings").json()["readings"]
        assert [r["cumulative_after"] for r in readings] == [0] * 5
        assert all(r["alarmed"] for r in readings)
        alarms = client.get(f"/roll-series/{series_id}/alarms").json()["alarms"]
        assert [a["roll_index"] for a in alarms] == [1, 2, 3, 4, 5]
        assert all(a["inspection_id"] == inspection_id for a in alarms)


def test_existing_database_is_migrated_in_place(tmp_path):
    """沿用命名卷里的旧库：新增表在启动时自动补建，原登记数据不受影响。"""
    import sqlite3

    path = str(tmp_path / "legacy.db")
    connection = sqlite3.connect(path)
    connection.executescript(
        """
        CREATE TABLE inspections (
            id            INTEGER PRIMARY KEY AUTOINCREMENT,
            total_pieces  INTEGER NOT NULL CHECK (total_pieces >= 0),
            note          TEXT,
            created_at    TEXT NOT NULL
        );
        CREATE TABLE count_reports (
            id             INTEGER PRIMARY KEY AUTOINCREMENT,
            inspection_id  INTEGER NOT NULL REFERENCES inspections(id) ON DELETE CASCADE,
            created_at     TEXT NOT NULL
        );
        CREATE TABLE segments (
            id             INTEGER PRIMARY KEY AUTOINCREMENT,
            report_id      INTEGER NOT NULL REFERENCES count_reports(id) ON DELETE CASCADE,
            inspection_id  INTEGER NOT NULL REFERENCES inspections(id) ON DELETE CASCADE,
            lo             INTEGER NOT NULL CHECK (lo >= 0),
            hi             INTEGER NOT NULL CHECK (hi > lo),
            count          INTEGER NOT NULL CHECK (count >= 0)
        );
        INSERT INTO inspections (total_pieces, note, created_at)
        VALUES (100, '旧库登记', '2026-01-01T00:00:00+00:00');
        """
    )
    connection.commit()
    connection.close()

    with TestClient(create_app(path)) as client:
        assert client.get("/inspections/1").json()["note"] == "旧库登记"
        series_id = client.post(
            "/roll-series",
            json={
                "target_thickness": TARGET,
                "tolerance": TOLERANCE,
                "alarm_threshold": THRESHOLD,
            },
        ).json()["id"]
        response = client.put(
            f"/roll-series/{series_id}/readings/1",
            json={"measured": 120, "inspection_id": 1},
        )
        assert response.status_code == 200
        assert response.json()["alarms"] == [
            {"roll_index": 1, "measured": 120, "inspection_id": 1}
        ]


def test_failure_mid_suffix_write_rolls_back_everything(client, monkeypatch):
    """后缀写到一半出错：整次回滚，读数和报警都回到原样。"""
    series_id, inspection_id = seed(client, 6, measured=100)
    for roll, measured in {1: 109, 2: 109, 3: 109, 4: 109, 5: 109, 6: 109}.items():
        assert (
            client.put(
                f"/roll-series/{series_id}/readings/{roll}",
                json={"measured": measured, "inspection_id": inspection_id},
            ).status_code
            == 200
        )
    before_readings = client.get(f"/roll-series/{series_id}/readings").json()
    before_alarms = client.get(f"/roll-series/{series_id}/alarms").json()
    assert [a["roll_index"] for a in before_alarms["alarms"]] == [3, 6]

    original_update = app_db.update_roll_state
    calls = {"count": 0}

    def failing_update(*args, **kwargs):
        calls["count"] += 1
        if calls["count"] == 1:
            raise RuntimeError("模拟磁盘写入失败")
        return original_update(*args, **kwargs)

    monkeypatch.setattr(app_db, "update_roll_state", failing_update)

    # 第 1 卷改成 120：报警前移，需要改写后缀；第一次写后缀即失败。
    with pytest.raises(RuntimeError):
        client.put(
            f"/roll-series/{series_id}/readings/1",
            json={"measured": 120, "inspection_id": inspection_id},
        )

    monkeypatch.setattr(app_db, "update_roll_state", original_update)
    assert client.get(f"/roll-series/{series_id}/readings").json() == before_readings
    assert client.get(f"/roll-series/{series_id}/alarms").json() == before_alarms
