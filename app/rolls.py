"""卷序厚度偏差累计与报警计算。

模型
----
一条监测序列在创建时固定目标厚度 ``target``、容差 ``tolerance`` 和报警线
``threshold``（均为整数微米）。每卷按**生产卷序** ``roll_index``（从 1 起）
录入一个测厚值 ``measured``，并关联已有的质检登记号。

按卷序逐卷处理::

    amount  = measured - target - tolerance
    current = max(0, current + amount)
    if current >= threshold:
        记该卷为报警卷，current = 0

即只有超出“目标 + 容差”的部分才计入累计；累计值不会低于零；累计值达到或
超过报警线时该卷报警，记录报警并清零，继续处理后续卷。

增量回算
--------
每卷的结果只取决于处理它之前的累计值。持久化时把每卷处理后的累计值与报警
标记和读数一起保存，因此：

* 补录或更正第 r 卷时，从 ``roll_index < r`` 的最近一卷直接取出起点累计值，
  **不需要从头扫描先前各卷**；
* 从第 r 卷起按卷序向后回放，处理完某个**原有卷**后若累计值与改动前相同，
  说明其后各卷的输入与起点都与改动前一致，立即停止回放，沿用后续报警。

回算代价只与被影响的后缀长度有关：常见情况下改一卷只重算该卷本身。全量
从头回放仅用于独立校验与测试（:func:`full_scan`）。
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from typing import NamedTuple

ZERO = 0


class AlarmRoll(NamedTuple):
    """报警卷：生产卷序、测厚值、关联的质检登记号。"""

    roll_index: int
    measured: int
    inspection_id: int


class RollState(NamedTuple):
    """一卷处理后的持久化状态。"""

    cumulative_after: int
    alarmed: bool


class ScanResult(NamedTuple):
    """整条序列从头回放的结果。"""

    alarms: tuple[AlarmRoll, ...]
    states: dict[int, RollState]
    cumulative: int


class PlanResult(NamedTuple):
    """增量回放计划。

    ``updates`` 只含需要写回状态的卷（被影响的后缀）；``converged_at`` 为
    累计值与原状态对齐的卷序，``None`` 表示一路回放到序列末尾；
    ``replayed_roll_count`` 为本次实际回放的卷数。
    """

    updates: dict[int, RollState]
    converged_at: int | None
    replayed_roll_count: int


def classify(
    target: int,
    tolerance: int,
    threshold: int,
    cumulative: int,
    measured: int,
) -> tuple[int, bool]:
    """处理一卷，返回 ``(新的累计值, 是否报警)``；报警卷的累计值已清零。"""
    current = cumulative + measured - target - tolerance
    if current < ZERO:
        current = ZERO
    if current >= threshold:
        return ZERO, True
    return current, False


def validate_config(target: int, tolerance: int, threshold: int) -> None:
    if target < 0:
        raise ValueError("目标厚度不能为负")
    if tolerance < 0:
        raise ValueError("容差不能为负")
    if threshold <= 0:
        raise ValueError("报警线必须为正数")


def full_scan(
    target: int,
    tolerance: int,
    threshold: int,
    readings: Iterable[tuple[int, int, int]],
) -> ScanResult:
    """从零开始完整回放整条序列（独立校验与测试用）。

    ``readings`` 为 ``(roll_index, measured, inspection_id)``；若未按卷序
    给出会先排序。同一卷序重复出现视为调用方错误。
    """
    validate_config(target, tolerance, threshold)
    ordered = sorted(readings, key=lambda item: item[0])
    for previous, current in zip(ordered, ordered[1:], strict=False):
        if previous[0] == current[0]:
            raise ValueError(f"卷序 {current[0]} 重复")

    alarms: list[AlarmRoll] = []
    states: dict[int, RollState] = {}
    cumulative = ZERO
    for roll_index, measured, inspection_id in ordered:
        cumulative, alarmed = classify(
            target, tolerance, threshold, cumulative, measured
        )
        states[roll_index] = RollState(cumulative, alarmed)
        if alarmed:
            alarms.append(AlarmRoll(roll_index, measured, inspection_id))
    return ScanResult(tuple(alarms), states, cumulative)


def plan_incremental(
    target: int,
    tolerance: int,
    threshold: int,
    changed_roll_index: int,
    start_cumulative: int,
    new_readings: Iterable[tuple[int, int]],
    old_states: Mapping[int, RollState],
) -> PlanResult:
    """规划补录、更正或删除一卷后的回放。

    参数
    ----
    changed_roll_index
        本次插入或更正的卷序（从该卷起回放）。删除一卷时传被删除卷序，此时
        ``new_readings`` 从其后第一个仍存在的卷开始。
    start_cumulative
        改动前 ``roll_index < changed_roll_index`` 的最近一卷处理后的累计值；
        之前没有卷时为 0。调用方直接按键取出，无需扫描先前各卷。
    new_readings
        改动后从改动点起的 ``(roll_index, measured)``，按卷序升序且可能很大，
        因此接受任意可迭代对象：一旦回放收敛即停止消费，其余各行不再读取。
    old_states
        改动前各原有卷的状态，按需按键查询（可为实现 ``Mapping`` 的惰性视图）。
        被删除的卷不应出现在其中。

    返回需要写回的状态。回放到某个原有卷时若累计值与其原状态相同，即
    停止，余下各卷沿用原报警。
    """
    validate_config(target, tolerance, threshold)
    if start_cumulative < ZERO:
        raise ValueError("累计起点不能为负")

    updates: dict[int, RollState] = {}
    cumulative = start_cumulative
    replayed = 0
    iterator = iter(new_readings)
    try:
        for roll_index, measured in iterator:
            if roll_index < changed_roll_index:
                raise ValueError("回放不能早于被改动的卷")
            cumulative, alarmed = classify(
                target, tolerance, threshold, cumulative, measured
            )
            replayed += 1
            state = RollState(cumulative, alarmed)
            updates[roll_index] = state
            previous = old_states.get(roll_index)
            if previous is not None and previous.cumulative_after == cumulative:
                # 处理完这个原有卷后累计值与改动前相同：其后各卷的输入与
                # 起点都与改动前一致，跳过余下各卷，沿用后续报警。
                return PlanResult(updates, roll_index, replayed)
        return PlanResult(updates, None, replayed)
    finally:
        # 提前收敛时不再消费余下输入（惰性查询/游标随之关闭）。
        close = getattr(iterator, "close", None)
        if close is not None:
            close()


