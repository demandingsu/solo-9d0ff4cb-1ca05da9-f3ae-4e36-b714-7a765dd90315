"""SQLite 持久化层。"""

from __future__ import annotations

import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterator, Sequence

SCHEMA = """
CREATE TABLE IF NOT EXISTS inspections (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    total_pieces  INTEGER NOT NULL CHECK (total_pieces >= 0),
    note          TEXT,
    created_at    TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS count_reports (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    inspection_id  INTEGER NOT NULL REFERENCES inspections(id) ON DELETE CASCADE,
    created_at     TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS segments (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    report_id      INTEGER NOT NULL REFERENCES count_reports(id) ON DELETE CASCADE,
    inspection_id  INTEGER NOT NULL REFERENCES inspections(id) ON DELETE CASCADE,
    lo             INTEGER NOT NULL CHECK (lo >= 0),
    hi             INTEGER NOT NULL CHECK (hi > lo),
    count          INTEGER NOT NULL CHECK (count >= 0)
);

CREATE INDEX IF NOT EXISTS idx_reports_inspection
    ON count_reports(inspection_id);
CREATE INDEX IF NOT EXISTS idx_segments_inspection
    ON segments(inspection_id);
CREATE INDEX IF NOT EXISTS idx_segments_report
    ON segments(report_id);

CREATE TABLE IF NOT EXISTS roll_series (
    id                INTEGER PRIMARY KEY AUTOINCREMENT,
    target_thickness  INTEGER NOT NULL CHECK (target_thickness >= 0),
    tolerance         INTEGER NOT NULL CHECK (tolerance >= 0),
    alarm_threshold   INTEGER NOT NULL CHECK (alarm_threshold > 0),
    note              TEXT,
    created_at        TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS roll_readings (
    series_id         INTEGER NOT NULL
                          REFERENCES roll_series(id) ON DELETE CASCADE,
    roll_index        INTEGER NOT NULL CHECK (roll_index >= 1),
    measured          INTEGER NOT NULL CHECK (measured >= 0),
    inspection_id     INTEGER NOT NULL REFERENCES inspections(id),
    cumulative_after  INTEGER NOT NULL CHECK (cumulative_after >= 0),
    alarmed           INTEGER NOT NULL CHECK (alarmed IN (0, 1)),
    updated_at        TEXT NOT NULL,
    PRIMARY KEY (series_id, roll_index)
);

CREATE INDEX IF NOT EXISTS idx_roll_readings_inspection
    ON roll_readings(inspection_id);
CREATE INDEX IF NOT EXISTS idx_roll_readings_alarmed
    ON roll_readings(series_id, alarmed);
"""


def connect(path: str) -> sqlite3.Connection:
    """打开一个请求级连接。

    ``isolation_level=None`` 让我们显式使用 ``BEGIN IMMEDIATE`` 控制写事务，
    从而把“读取现有计数 + 校验 + 写入”放在同一个串行化事务里。

    ``check_same_thread=False``：每条连接只服务一个请求，但依赖清理与
    端点可能落在不同的线程池线程上；连接不会跨请求共享。
    """
    connection = sqlite3.connect(
        path, timeout=30.0, isolation_level=None, check_same_thread=False
    )
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys = ON")
    return connection


def init(path: str) -> None:
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    connection = connect(path)
    try:
        connection.execute("PRAGMA journal_mode = WAL")
        connection.executescript(SCHEMA)
    finally:
        connection.close()


def now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def create_inspection(
    connection: sqlite3.Connection, total_pieces: int, note: str | None
) -> int:
    cursor = connection.execute(
        "INSERT INTO inspections (total_pieces, note, created_at) VALUES (?, ?, ?)",
        (total_pieces, note, now()),
    )
    return int(cursor.lastrowid)


def get_inspection(connection: sqlite3.Connection, inspection_id: int) -> sqlite3.Row | None:
    return connection.execute(
        """
        SELECT i.id, i.total_pieces, i.note, i.created_at,
               (SELECT COUNT(*) FROM count_reports r WHERE r.inspection_id = i.id)
                   AS report_count,
               (SELECT COUNT(*) FROM segments s WHERE s.inspection_id = i.id)
                   AS segment_count
        FROM inspections i
        WHERE i.id = ?
        """,
        (inspection_id,),
    ).fetchone()


def list_inspections(connection: sqlite3.Connection) -> list[sqlite3.Row]:
    return connection.execute(
        """
        SELECT i.id, i.total_pieces, i.note, i.created_at,
               (SELECT COUNT(*) FROM count_reports r WHERE r.inspection_id = i.id)
                   AS report_count,
               (SELECT COUNT(*) FROM segments s WHERE s.inspection_id = i.id)
                   AS segment_count
        FROM inspections i
        ORDER BY i.id
        """
    ).fetchall()


def update_inspection(
    connection: sqlite3.Connection,
    inspection_id: int,
    total_pieces: int,
    note: str | None,
) -> None:
    connection.execute(
        "UPDATE inspections SET total_pieces = ?, note = ? WHERE id = ?",
        (total_pieces, note, inspection_id),
    )


def load_constraints(
    connection: sqlite3.Connection, inspection_id: int
) -> list[tuple[int, int, int]]:
    rows = connection.execute(
        "SELECT lo, hi, count FROM segments WHERE inspection_id = ? ORDER BY id",
        (inspection_id,),
    ).fetchall()
    return [(int(row["lo"]), int(row["hi"]), int(row["count"])) for row in rows]


def insert_report(
    connection: sqlite3.Connection,
    inspection_id: int,
    segments: Sequence[tuple[int, int, int]],
) -> int:
    cursor = connection.execute(
        "INSERT INTO count_reports (inspection_id, created_at) VALUES (?, ?)",
        (inspection_id, now()),
    )
    report_id = int(cursor.lastrowid)
    connection.executemany(
        """
        INSERT INTO segments (report_id, inspection_id, lo, hi, count)
        VALUES (?, ?, ?, ?, ?)
        """,
        [(report_id, inspection_id, lo, hi, count) for lo, hi, count in segments],
    )
    return report_id


def get_reports(connection: sqlite3.Connection, inspection_id: int) -> list[dict]:
    reports = connection.execute(
        "SELECT id, inspection_id, created_at FROM count_reports "
        "WHERE inspection_id = ? ORDER BY id",
        (inspection_id,),
    ).fetchall()
    segment_rows = connection.execute(
        "SELECT id, report_id, lo, hi, count FROM segments "
        "WHERE inspection_id = ? ORDER BY id",
        (inspection_id,),
    ).fetchall()

    segments_by_report: dict[int, list[dict]] = {int(r["id"]): [] for r in reports}
    for row in segment_rows:
        segments_by_report[int(row["report_id"])].append(
            {
                "id": int(row["id"]),
                "lo": int(row["lo"]),
                "hi": int(row["hi"]),
                "count": int(row["count"]),
            }
        )

    return [
        {
            "id": int(report["id"]),
            "inspection_id": int(report["inspection_id"]),
            "created_at": report["created_at"],
            "segments": segments_by_report[int(report["id"])],
        }
        for report in reports
    ]


# ---------------------------------------------------------------------------
# 卷序监测
# ---------------------------------------------------------------------------


def create_roll_series(
    connection: sqlite3.Connection,
    target_thickness: int,
    tolerance: int,
    alarm_threshold: int,
    note: str | None,
) -> int:
    cursor = connection.execute(
        """
        INSERT INTO roll_series
            (target_thickness, tolerance, alarm_threshold, note, created_at)
        VALUES (?, ?, ?, ?, ?)
        """,
        (target_thickness, tolerance, alarm_threshold, note, now()),
    )
    return int(cursor.lastrowid)


def get_roll_series(connection: sqlite3.Connection, series_id: int) -> sqlite3.Row | None:
    return connection.execute(
        """
        SELECT s.id, s.target_thickness, s.tolerance, s.alarm_threshold, s.note,
               s.created_at,
               (SELECT COUNT(*) FROM roll_readings r WHERE r.series_id = s.id)
                   AS reading_count,
               (SELECT COUNT(*) FROM roll_readings r
                    WHERE r.series_id = s.id AND r.alarmed = 1)
                   AS alarm_count
        FROM roll_series s
        WHERE s.id = ?
        """,
        (series_id,),
    ).fetchone()


def list_roll_series(connection: sqlite3.Connection) -> list[sqlite3.Row]:
    return connection.execute(
        """
        SELECT s.id, s.target_thickness, s.tolerance, s.alarm_threshold, s.note,
               s.created_at,
               (SELECT COUNT(*) FROM roll_readings r WHERE r.series_id = s.id)
                   AS reading_count,
               (SELECT COUNT(*) FROM roll_readings r
                    WHERE r.series_id = s.id AND r.alarmed = 1)
                   AS alarm_count
        FROM roll_series s
        ORDER BY s.id
        """
    ).fetchall()


def get_roll_reading(
    connection: sqlite3.Connection, series_id: int, roll_index: int
) -> sqlite3.Row | None:
    return connection.execute(
        """
        SELECT series_id, roll_index, measured, inspection_id,
               cumulative_after, alarmed, updated_at
        FROM roll_readings
        WHERE series_id = ? AND roll_index = ?
        """,
        (series_id, roll_index),
    ).fetchone()


def get_roll_state(
    connection: sqlite3.Connection, series_id: int, roll_index: int
) -> tuple[int, bool] | None:
    """按键取一卷的已存状态；不存在时返回 ``None``。"""
    row = connection.execute(
        """
        SELECT cumulative_after, alarmed
        FROM roll_readings
        WHERE series_id = ? AND roll_index = ?
        """,
        (series_id, roll_index),
    ).fetchone()
    if row is None:
        return None
    return int(row["cumulative_after"]), bool(row["alarmed"])


def get_state_before(
    connection: sqlite3.Connection, series_id: int, roll_index: int
) -> int:
    """取 ``roll_index`` 之前最近一卷的累计值；没有更早的卷时为 0。

    单行按键查询，不扫描先前各卷。
    """
    row = connection.execute(
        """
        SELECT cumulative_after
        FROM roll_readings
        WHERE series_id = ? AND roll_index < ?
        ORDER BY roll_index DESC
        LIMIT 1
        """,
        (series_id, roll_index),
    ).fetchone()
    return int(row["cumulative_after"]) if row is not None else 0


def iter_roll_readings_from(
    connection: sqlite3.Connection, series_id: int, from_roll_index: int
) -> Iterator[tuple[int, int]]:
    """惰性逐行取出后缀各卷的 ``(roll_index, measured)``。

    回放一旦收敛，调用方即停止迭代并关闭生成器，余下各卷既不会从数据库
    拉取，也不会参与计算。
    """
    cursor = connection.execute(
        """
        SELECT roll_index, measured
        FROM roll_readings
        WHERE series_id = ? AND roll_index >= ?
        ORDER BY roll_index
        """,
        (series_id, from_roll_index),
    )
    try:
        for row in cursor:
            yield int(row["roll_index"]), int(row["measured"])
    finally:
        cursor.close()


def upsert_roll_reading(
    connection: sqlite3.Connection,
    series_id: int,
    roll_index: int,
    measured: int,
    inspection_id: int,
    cumulative_after: int,
    alarmed: bool,
) -> None:    connection.execute(
        """
        INSERT INTO roll_readings
            (series_id, roll_index, measured, inspection_id,
             cumulative_after, alarmed, updated_at)
        VALUES (?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT (series_id, roll_index) DO UPDATE SET
            measured = excluded.measured,
            inspection_id = excluded.inspection_id,
            cumulative_after = excluded.cumulative_after,
            alarmed = excluded.alarmed,
            updated_at = excluded.updated_at
        """,
        (
            series_id,
            roll_index,
            measured,
            inspection_id,
            cumulative_after,
            1 if alarmed else 0,
            now(),
        ),
    )


def delete_roll_reading(
    connection: sqlite3.Connection, series_id: int, roll_index: int
) -> None:
    connection.execute(
        "DELETE FROM roll_readings WHERE series_id = ? AND roll_index = ?",
        (series_id, roll_index),
    )


def update_roll_state(
    connection: sqlite3.Connection,
    series_id: int,
    roll_index: int,
    cumulative_after: int,
    alarmed: bool,
    updated_at: str,
) -> None:
    """只更新一卷的累计值与报警标记，读数与关联登记号保持不变。"""
    connection.execute(
        """
        UPDATE roll_readings
        SET cumulative_after = ?, alarmed = ?, updated_at = ?
        WHERE series_id = ? AND roll_index = ?
        """,
        (
            cumulative_after,
            1 if alarmed else 0,
            updated_at,
            series_id,
            roll_index,
        ),
    )


def list_roll_readings(
    connection: sqlite3.Connection, series_id: int
) -> list[sqlite3.Row]:
    return connection.execute(
        """
        SELECT r.series_id, r.roll_index, r.measured, r.inspection_id,
               r.cumulative_after, r.alarmed, r.updated_at, i.note AS inspection_note
        FROM roll_readings r
        JOIN inspections i ON i.id = r.inspection_id
        WHERE r.series_id = ?
        ORDER BY r.roll_index
        """,
        (series_id,),
    ).fetchall()


def inspection_exists(connection: sqlite3.Connection, inspection_id: int) -> bool:
    row = connection.execute(
        "SELECT 1 FROM inspections WHERE id = ?", (inspection_id,)
    ).fetchone()
    return row is not None
