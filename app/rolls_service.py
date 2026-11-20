"""卷序监测应用服务：录入/更正/删除读数、增量回算、查询报警卷。

对调用方可见的行为：

* 读数与报警一起生效——读数、回放后的累计值和报警标记在**同一个写事务**
  里提交，异常时整体回滚，原有结果保持不变、仍可查询；
* 乱序补录或更正一卷后，报警卷号与“从头计算”完全一致；但计算不从头扫描：
  起点累计值按键查前一卷即可得到，回放只消费被影响的后缀，处理完某个原有
  卷后累计值若与改动前相同，立即停止——之后各卷的读数不再读取、状态不再
  查询，直接沿用其后的既有报警。
"""

from __future__ import annotations

from collections.abc import Iterator, Mapping
from sqlite3 import Connection

from . import db, rolls
from .rolls import RollState

Config = tuple[int, int, int]  # (target, tolerance, alarm_threshold)


class _StoredStates(Mapping[int, RollState]):
    """按需按键查询已存状态的只读视图。

    回放处理到某一卷时才查询该卷的原状态；一旦收敛便不再查询，因此
    “跳过余下各卷”在数据库访问上同样成立。
    """

    def __init__(self, connection: Connection, series_id: int) -> None:
        self._connection = connection
        self._series_id = series_id

    def get(self, roll_index, default=None):
        stored = db.get_roll_state(self._connection, self._series_id, roll_index)
        if stored is None:
            return default
        cumulative, alarmed = stored
        return RollState(cumulative, alarmed)

    def __getitem__(self, roll_index: int) -> RollState:
        state = self.get(roll_index)
        if state is None:
            raise KeyError(roll_index)
        return state

    def __iter__(self) -> Iterator[int]:
        raise TypeError("已存状态视图只支持按键查询")

    def __len__(self) -> int:
        raise TypeError("已存状态视图只支持按键查询")


def get_alarms(connection: Connection, series_id: int) -> list[rolls.AlarmRoll]:
    """按卷序取出全部报警卷（含关联质检登记号）。"""
    rows = connection.execute(
        """
        SELECT roll_index, measured, inspection_id
        FROM roll_readings
        WHERE series_id = ? AND alarmed = 1
        ORDER BY roll_index
        """,
        (series_id,),
    ).fetchall()
    return [
        rolls.AlarmRoll(
            int(row["roll_index"]), int(row["measured"]), int(row["inspection_id"])
        )
        for row in rows
    ]


def reading_payload(row) -> dict:
    return {
        "roll_index": int(row["roll_index"]),
        "measured": int(row["measured"]),
        "inspection_id": int(row["inspection_id"]),
        "cumulative_after": int(row["cumulative_after"]),
        "alarmed": bool(row["alarmed"]),
    }


def _suffix_readings(connection: Connection, series_id: int, from_roll_index: int):
    """惰性生成后缀各卷的 ``(roll_index, measured)``，供回放按需消费。"""
    yield from db.iter_roll_readings_from(connection, series_id, from_roll_index)


def _write_suffix(
    connection: Connection,
    series_id: int,
    plan: rolls.PlanResult,
    skip_roll_index: int,
) -> None:
    """把回放结果写回，只更新状态确有变化的卷（被改动卷由调用方写入）。"""
    timestamp = db.now()
    for roll_index, state in plan.updates.items():
        if roll_index == skip_roll_index:
            continue
        db.update_roll_state(
            connection,
            series_id,
            roll_index,
            state.cumulative_after,
            state.alarmed,
            timestamp,
        )


def apply_reading(
    connection: Connection,
    series_id: int,
    roll_index: int,
    measured: int,
    inspection_id: int,
    config: Config,
) -> tuple[dict, list[rolls.AlarmRoll]]:
    """新增或更正一卷，并在事务内回算受影响的后缀。

    调用方负责事务边界：成功时提交，异常时回滚。返回
    ``(该卷写入后的状态, 当前全部报警卷)``。
    """
    start_cumulative = db.get_state_before(connection, series_id, roll_index)

    def replay_input():
        yield roll_index, measured
        yield from _suffix_readings(connection, series_id, roll_index + 1)

    plan = rolls.plan_incremental(
        config[0],
        config[1],
        config[2],
        roll_index,
        start_cumulative,
        replay_input(),
        _StoredStates(connection, series_id),
    )

    # 先写入被改动卷的完整读数（含关联登记号），再刷新后缀状态。
    state = plan.updates[roll_index]
    db.upsert_roll_reading(
        connection,
        series_id,
        roll_index,
        measured,
        inspection_id,
        state.cumulative_after,
        state.alarmed,
    )
    _write_suffix(connection, series_id, plan, roll_index)

    row = db.get_roll_reading(connection, series_id, roll_index)
    return reading_payload(row), get_alarms(connection, series_id)


def delete_reading(
    connection: Connection,
    series_id: int,
    roll_index: int,
    config: Config,
) -> list[rolls.AlarmRoll]:
    """删除一卷并回算其后缀，返回最新报警卷列表。"""
    start_cumulative = db.get_state_before(connection, series_id, roll_index)

    plan = rolls.plan_incremental(
        config[0],
        config[1],
        config[2],
        roll_index,
        start_cumulative,
        _suffix_readings(connection, series_id, roll_index + 1),
        _StoredStates(connection, series_id),
    )
    db.delete_roll_reading(connection, series_id, roll_index)
    _write_suffix(connection, series_id, plan, roll_index)
    return get_alarms(connection, series_id)
