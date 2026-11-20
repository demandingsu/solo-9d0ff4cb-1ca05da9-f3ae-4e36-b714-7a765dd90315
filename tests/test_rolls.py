"""卷序监测计算测试：手工用例、乱序/更正与从头回算对拍、增量回放提前收敛。"""

from __future__ import annotations

import random
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app import rolls  # noqa: E402

TARGET, TOLERANCE, THRESHOLD = 100, 5, 10


def readings(*measured: int, inspection: int = 1):
    return [
        (index + 1, value, inspection + index)
        for index, value in enumerate(measured)
    ]


def alarm_list(states: dict[int, rolls.RollState], measured: dict[int, int]):
    return [
        (roll, measured[roll], roll)
        for roll in sorted(states)
        if states[roll].alarmed
    ]


def full_scan_from_dict(
    measured: dict[int, int],
    target: int = TARGET,
    tolerance: int = TOLERANCE,
    threshold: int = THRESHOLD,
) -> rolls.ScanResult:
    return rolls.full_scan(
        target, tolerance, threshold, [(r, m, r) for r, m in measured.items()]
    )


def test_classify_only_counts_excess_over_target_plus_tolerance():
    assert rolls.classify(TARGET, TOLERANCE, THRESHOLD, 0, 100) == (0, False)
    # 恰好等于目标 + 容差：不累计
    assert rolls.classify(TARGET, TOLERANCE, THRESHOLD, 0, 105) == (0, False)
    assert rolls.classify(TARGET, TOLERANCE, THRESHOLD, 0, 108) == (3, False)


def test_thin_rolls_never_go_negative():
    scan = rolls.full_scan(TARGET, TOLERANCE, THRESHOLD, readings(80, 90, 112))
    assert scan.cumulative == 7
    assert scan.states[1] == rolls.RollState(0, False)
    assert scan.states[2] == rolls.RollState(0, False)


def test_alarm_resets_cumulative_and_can_repeat():
    scan = rolls.full_scan(
        TARGET, TOLERANCE, THRESHOLD, readings(108, 104, 103, 110, 120)
    )
    # 3 -> 3 -> 3 -> 8 -> 报警清零
    assert [tuple(a) for a in scan.alarms] == [(5, 120, 5)]
    assert scan.states[5] == rolls.RollState(0, True)


def test_alarm_when_cumulative_reaches_threshold_exactly():
    scan = rolls.full_scan(TARGET, TOLERANCE, THRESHOLD, readings(115, 115))
    assert [tuple(a) for a in scan.alarms] == [(1, 115, 1), (2, 115, 2)]


def test_full_scan_sorts_unordered_input_and_rejects_duplicates():
    scan = rolls.full_scan(
        TARGET,
        TOLERANCE,
        THRESHOLD,
        [(3, 130, 3), (1, 106, 1), (2, 100, 2)],
    )
    assert scan.cumulative == 0
    assert [tuple(a) for a in scan.alarms] == [(3, 130, 3)]

    try:
        rolls.full_scan(TARGET, TOLERANCE, THRESHOLD, [(1, 100, 1), (1, 106, 2)])
    except ValueError as exc:
        assert "重复" in str(exc)
    else:  # pragma: no cover
        raise AssertionError("重复卷序应被拒绝")


def test_config_validation():
    for target, tolerance, threshold in [(-1, 0, 10), (100, -1, 10), (100, 0, 0)]:
        try:
            rolls.full_scan(target, tolerance, threshold, [])
        except ValueError:
            pass
        else:  # pragma: no cover
            raise AssertionError("非法配置应被拒绝")


def test_incremental_matches_full_scan_after_any_single_correction():
    rng = random.Random(20261120)
    for _ in range(300):
        target = rng.randint(50, 150)
        tolerance = rng.randint(0, 10)
        threshold = rng.randint(1, 30)
        count = rng.randint(1, 12)
        measured = {
            roll: rng.randint(max(0, target - 20), target + 30)
            for roll in range(1, count + 1)
        }
        scan = full_scan_from_dict(measured, target, tolerance, threshold)
        old_states = dict(scan.states)

        changed_roll = rng.randint(1, count)
        changed = dict(measured)
        changed[changed_roll] = rng.randint(0, target + 40)
        start = old_states[changed_roll - 1].cumulative_after if changed_roll > 1 else 0

        plan = rolls.plan_incremental(
            target,
            tolerance,
            threshold,
            changed_roll,
            start,
            [(r, changed[r]) for r in sorted(changed) if r >= changed_roll],
            old_states,
        )
        new_states = {**old_states, **plan.updates}
        expect = full_scan_from_dict(changed, target, tolerance, threshold)

        assert new_states == expect.states
        assert alarm_list(new_states, changed) == [tuple(a) for a in expect.alarms]


def test_incremental_matches_full_scan_after_inserting_missing_roll():
    rng = random.Random(7)
    for _ in range(200):
        target = rng.randint(50, 150)
        tolerance = rng.randint(0, 10)
        threshold = rng.randint(1, 30)
        count = rng.randint(2, 10)
        measured = {
            roll: rng.randint(0, target + 40) for roll in range(1, count + 1)
        }
        scan = full_scan_from_dict(measured, target, tolerance, threshold)
        old_states = dict(scan.states)

        # 删掉一卷，再以新值补录到同一卷序（乱序补录）
        roll = rng.randint(1, count)
        without = {r: m for r, m in measured.items() if r != roll}
        start = old_states[roll - 1].cumulative_after if roll > 1 else 0
        late_value = rng.randint(0, target + 40)

        plan = rolls.plan_incremental(
            target,
            tolerance,
            threshold,
            roll,
            start,
            [(roll, late_value)]
            + [(r, without[r]) for r in sorted(without) if r > roll],
            {r: state for r, state in old_states.items() if r != roll},
        )
        new_states = {
            **{r: s for r, s in old_states.items() if r != roll},
            **plan.updates,
        }
        changed = {**without, roll: late_value}
        expect = full_scan_from_dict(changed, target, tolerance, threshold)

        assert new_states == expect.states
        assert alarm_list(new_states, changed) == [tuple(a) for a in expect.alarms]


def test_replay_stops_once_cumulative_matches_prior_state():
    measured = {roll: 100 for roll in range(1, 101)}
    measured[50] = 120
    scan = full_scan_from_dict(measured)
    assert scan.states[50].alarmed is True

    # 把第 10 卷改小：累计值仍为 0，与改动前一致 → 只消费这一卷。
    old_states = dict(scan.states)
    changed = dict(measured)
    changed[10] = 80
    consumed: list[int] = []

    def lazy():
        for roll in sorted(changed):
            if roll < 10:
                continue
            consumed.append(roll)
            yield roll, changed[roll]

    plan = rolls.plan_incremental(
        TARGET,
        TOLERANCE,
        THRESHOLD,
        10,
        old_states[9].cumulative_after,
        lazy(),
        old_states,
    )
    assert consumed == [10]
    assert plan.converged_at == 10
    assert plan.replayed_roll_count == 1
    assert plan.updates == {10: rolls.RollState(0, False)}


def test_replay_stop_keeps_following_alarms():
    measured = {1: 100, 2: 106, 3: 106, 4: 130, 5: 130}
    scan = full_scan_from_dict(measured)
    assert [a.roll_index for a in scan.alarms] == [4, 5]
    old_states = dict(scan.states)

    changed = dict(measured)
    changed[1] = 80  # 累计值仍为 0，收敛于第 1 卷，后续报警应沿用
    plan = rolls.plan_incremental(
        TARGET,
        TOLERANCE,
        THRESHOLD,
        1,
        0,
        ((r, changed[r]) for r in sorted(changed)),
        old_states,
    )
    assert plan.converged_at == 1
    assert plan.replayed_roll_count == 1
    new_states = {**old_states, **plan.updates}
    assert alarm_list(new_states, changed) == [(4, 130, 4), (5, 130, 5)]


def test_late_reading_moves_alarm_up():
    # 先录 1..6，第 6 卷报警
    measured = {1: 103, 2: 105, 3: 106, 4: 104, 5: 110, 6: 110}
    scan = full_scan_from_dict(measured)
    assert [a.roll_index for a in scan.alarms] == [6]

    # 补录第 5 卷为一个很厚的读数：报警提前到第 5 卷
    old_states = dict(scan.states)
    late = dict(measured)
    late[5] = 130
    plan = rolls.plan_incremental(
        TARGET,
        TOLERANCE,
        THRESHOLD,
        5,
        old_states[4].cumulative_after,
        [(r, late[r]) for r in sorted(late) if r >= 5],
        old_states,
    )
    new_states = {**old_states, **plan.updates}
    expect = full_scan_from_dict(late)
    assert new_states == expect.states
    assert alarm_list(new_states, late) == [(5, 130, 5)]
