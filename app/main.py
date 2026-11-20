"""FastAPI 应用：纸样厚度计数核对 + 卷序厚度偏差监测。

* ``POST /inspections``                    登记一批纸样的总片数
* ``GET  /inspections``                    列出全部登记
* ``GET  /inspections/{id}``               查看登记及已接收的分次补报
* ``PATCH /inspections/{id}``              更正总片数/备注（与已有计数冲突则拒收）
* ``POST /inspections/{id}/reports``       分次提交厚度区间计数（整份原子接收或拒收）
* ``GET  /inspections/{id}/reports``       查看补报历史
* ``GET  /inspections/{id}/bounds``        给定合格区间，查询合格片数最少/最多
* ``POST /roll-series``                    建立卷序监测序列（目标、容差、报警线）
* ``GET  /roll-series``                    列出全部监测序列
* ``GET  /roll-series/{id}``               查看序列：逐卷读数与报警卷号
* ``PUT  /roll-series/{id}/readings/{roll}``  录入/更正某卷读数（乱序补录可）
* ``DELETE /roll-series/{id}/readings/{roll}`` 删除某卷读数
* ``GET  /roll-series/{id}/readings``      查看逐卷读数与累计值
* ``GET  /roll-series/{id}/alarms``        查看报警卷号及关联质检登记号
* ``GET  /health``                         健康检查

所有区间均为整数微米，含起点不含终点，即 ``[lo, hi)``，且 ``hi > lo``。
"""

from __future__ import annotations

import os
from contextlib import asynccontextmanager

from fastapi import Depends, FastAPI, HTTPException, Query, status
from sqlite3 import Connection

from . import db, rolls_service
from .schemas import (
    AlarmRollOut,
    BoundsOut,
    CountReportIn,
    CountReportOut,
    InspectionCreate,
    InspectionDetail,
    InspectionSummary,
    InspectionUpdate,
    RollReadingIn,
    RollReadingOut,
    RollReadingUpdate,
    RollSeriesAlarmsOut,
    RollSeriesCreate,
    RollSeriesDetail,
    RollSeriesReadResult,
    RollSeriesReadingsOut,
    RollSeriesSummary,
)
from .solver import InconsistentCounts, check_consistent, compute_bounds

DEFAULT_DB_PATH = os.environ.get("DB_PATH", "./data/inspection.db")


def create_app(db_path: str | None = None) -> FastAPI:
    path = db_path or DEFAULT_DB_PATH

    @asynccontextmanager
    async def lifespan(_: FastAPI):
        db.init(path)
        yield

    api = FastAPI(
        title="纸样厚度计数核对与卷序监测",
        description=(
            "登记总片数，分次补报互可重叠的厚度区间计数，按全部计数的交集"
            "计算合格片数上下界，不逐片展开；并按生产卷序累计厚度偏差，"
            "越过报警线时记下报警卷与关联质检登记号。"
        ),
        version="1.1.0",
        lifespan=lifespan,
    )

    def get_conn():
        connection = db.connect(path)
        try:
            yield connection
        finally:
            connection.close()

    def _require_inspection(connection: Connection, inspection_id: int):
        row = db.get_inspection(connection, inspection_id)
        if row is None:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"登记 {inspection_id} 不存在",
            )
        return row

    def _summary(row) -> InspectionSummary:
        return InspectionSummary(
            id=row["id"],
            total_pieces=row["total_pieces"],
            note=row["note"],
            created_at=row["created_at"],
            report_count=row["report_count"],
            segment_count=row["segment_count"],
        )

    @api.get("/health", tags=["运维"], summary="健康检查")
    def health() -> dict:
        return {"status": "ok"}

    @api.post(
        "/inspections",
        response_model=InspectionSummary,
        status_code=status.HTTP_201_CREATED,
        tags=["登记"],
        summary="登记一批纸样的总片数",
    )
    def create_inspection(
        payload: InspectionCreate, connection: Connection = Depends(get_conn)
    ) -> InspectionSummary:
        inspection_id = db.create_inspection(
            connection, payload.total_pieces, payload.note
        )
        row = _require_inspection(connection, inspection_id)
        return _summary(row)

    @api.get(
        "/inspections",
        response_model=list[InspectionSummary],
        tags=["登记"],
        summary="列出全部登记",
    )
    def list_inspections(
        connection: Connection = Depends(get_conn),
    ) -> list[InspectionSummary]:
        return [_summary(row) for row in db.list_inspections(connection)]

    @api.get(
        "/inspections/{inspection_id}",
        response_model=InspectionDetail,
        tags=["登记"],
        summary="查看登记及已接收的补报",
    )
    def get_inspection(
        inspection_id: int, connection: Connection = Depends(get_conn)
    ) -> InspectionDetail:
        row = _require_inspection(connection, inspection_id)
        summary = _summary(row)
        reports = [CountReportOut(**r) for r in db.get_reports(connection, inspection_id)]
        return InspectionDetail(**summary.model_dump(), reports=reports)

    @api.patch(
        "/inspections/{inspection_id}",
        response_model=InspectionSummary,
        tags=["登记"],
        summary="更正总片数或备注",
        description=(
            "若新的总片数与已接收的计数不相容，整次更正被拒收，原登记保持不变。"
        ),
    )
    def update_inspection(
        inspection_id: int,
        payload: InspectionUpdate,
        connection: Connection = Depends(get_conn),
    ) -> InspectionSummary:
        connection.execute("BEGIN IMMEDIATE")
        try:
            row = _require_inspection(connection, inspection_id)
            new_total = (
                payload.total_pieces
                if "total_pieces" in payload.model_fields_set
                else int(row["total_pieces"])
            )
            new_note = payload.note if "note" in payload.model_fields_set else row["note"]
            if "total_pieces" in payload.model_fields_set:
                constraints = db.load_constraints(connection, inspection_id)
                try:
                    check_consistent(new_total, constraints)
                except InconsistentCounts:
                    raise HTTPException(
                        status_code=status.HTTP_409_CONFLICT,
                        detail=(
                            f"新总片数 {new_total} 与已接收的计数不相容，"
                            "本次更正已拒收，原登记保持不变"
                        ),
                    )
            db.update_inspection(connection, inspection_id, new_total, new_note)
            connection.execute("COMMIT")
        except HTTPException:
            connection.execute("ROLLBACK")
            raise
        except BaseException:
            connection.execute("ROLLBACK")
            raise
        row = _require_inspection(connection, inspection_id)
        return _summary(row)

    @api.post(
        "/inspections/{inspection_id}/reports",
        response_model=CountReportOut,
        status_code=status.HTTP_201_CREATED,
        tags=["补报"],
        summary="提交一批厚度区间计数",
        description=(
            "区间可以重叠。整份报告要么全部接收，要么在与已有计数"
            "（含本报告内其他区间）不相容时整份拒收；拒收不改变已有结果。"
        ),
    )
    def submit_report(
        inspection_id: int,
        payload: CountReportIn,
        connection: Connection = Depends(get_conn),
    ) -> CountReportOut:
        segments = [(s.lo, s.hi, s.count) for s in payload.segments]

        connection.execute("BEGIN IMMEDIATE")
        try:
            row = _require_inspection(connection, inspection_id)
            existing = db.load_constraints(connection, inspection_id)
            try:
                check_consistent(int(row["total_pieces"]), existing + segments)
            except InconsistentCounts as exc:
                raise HTTPException(
                    status_code=status.HTTP_409_CONFLICT,
                    detail=(
                        "补报与已有计数（或本报告内部）不相容，整份拒收；"
                        "已有结果保持不变"
                    ),
                ) from exc
            report_id = db.insert_report(connection, inspection_id, segments)
            connection.execute("COMMIT")
        except HTTPException:
            connection.execute("ROLLBACK")
            raise
        except BaseException:
            connection.execute("ROLLBACK")
            raise

        reports = db.get_reports(connection, inspection_id)
        for report in reports:
            if report["id"] == report_id:
                return CountReportOut(**report)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="补报写入后未能读回",
        )

    @api.get(
        "/inspections/{inspection_id}/reports",
        response_model=list[CountReportOut],
        tags=["补报"],
        summary="查看补报历史",
    )
    def list_reports(
        inspection_id: int, connection: Connection = Depends(get_conn)
    ) -> list[CountReportOut]:
        _require_inspection(connection, inspection_id)
        return [CountReportOut(**r) for r in db.get_reports(connection, inspection_id)]

    @api.get(
        "/inspections/{inspection_id}/bounds",
        response_model=BoundsOut,
        tags=["查询"],
        summary="查询合格片数的最少/最多可达到值",
        description=(
            "合格区间为 [qualified_lo, qualified_hi)，整数微米，含起点不含终点。"
            "返回值是同时满足全部已接收计数时，合格片数可同时达到的最小值和最大值；"
            "不逐片展开，按端点划分的整数规划精确求解。"
        ),
    )
    def get_bounds(
        inspection_id: int,
        qualified_lo: int = Query(..., ge=0, description="合格区间起点，包含"),
        qualified_hi: int = Query(..., description="合格区间终点，不包含，须大于起点"),
        connection: Connection = Depends(get_conn),
    ) -> BoundsOut:
        if qualified_hi <= qualified_lo:
            raise HTTPException(
                status_code=422,
                detail="qualified_hi 必须大于 qualified_lo（区间含起点、不含终点）",
            )
        row = _require_inspection(connection, inspection_id)
        constraints = db.load_constraints(connection, inspection_id)
        try:
            min_count, max_count = compute_bounds(
                int(row["total_pieces"]), constraints, qualified_lo, qualified_hi
            )
        except InconsistentCounts as exc:
            # 正常写入路径会拒绝不相容数据；此处防御数据库被外部改写。
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail="已存计数互相矛盾，无法计算；请检查数据",
            ) from exc
        return BoundsOut(
            inspection_id=inspection_id,
            total_pieces=int(row["total_pieces"]),
            qualified_lo=qualified_lo,
            qualified_hi=qualified_hi,
            min_count=min_count,
            max_count=max_count,
        )

    # ------------------------------------------------------------------
    # 卷序监测
    # ------------------------------------------------------------------

    def _require_series(connection: Connection, series_id: int):
        row = db.get_roll_series(connection, series_id)
        if row is None:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"监测序列 {series_id} 不存在",
            )
        return row

    def _config(row) -> tuple[int, int, int]:
        return (
            int(row["target_thickness"]),
            int(row["tolerance"]),
            int(row["alarm_threshold"]),
        )

    def _series_summary(row) -> RollSeriesSummary:
        return RollSeriesSummary(
            id=row["id"],
            target_thickness=row["target_thickness"],
            tolerance=row["tolerance"],
            alarm_threshold=row["alarm_threshold"],
            note=row["note"],
            created_at=row["created_at"],
            reading_count=row["reading_count"],
            alarm_count=row["alarm_count"],
        )

    def _alarm_out(alarm) -> AlarmRollOut:
        return AlarmRollOut(
            roll_index=alarm.roll_index,
            measured=alarm.measured,
            inspection_id=alarm.inspection_id,
        )

    def _require_inspection_id(connection: Connection, inspection_id: int) -> None:
        if not db.inspection_exists(connection, inspection_id):
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
                detail=f"关联的质检登记号 {inspection_id} 不存在",
            )

    @api.post(
        "/roll-series",
        response_model=RollSeriesSummary,
        status_code=status.HTTP_201_CREATED,
        tags=["卷序监测"],
        summary="建立卷序监测序列（目标厚度、容差、报警线）",
    )
    def create_roll_series(
        payload: RollSeriesCreate, connection: Connection = Depends(get_conn)
    ) -> RollSeriesSummary:
        connection.execute("BEGIN IMMEDIATE")
        try:
            series_id = db.create_roll_series(
                connection,
                payload.target_thickness,
                payload.tolerance,
                payload.alarm_threshold,
                payload.note,
            )
            connection.execute("COMMIT")
        except BaseException:
            connection.execute("ROLLBACK")
            raise
        return _series_summary(_require_series(connection, series_id))

    @api.get(
        "/roll-series",
        response_model=list[RollSeriesSummary],
        tags=["卷序监测"],
        summary="列出全部监测序列",
    )
    def list_roll_series(
        connection: Connection = Depends(get_conn),
    ) -> list[RollSeriesSummary]:
        return [_series_summary(row) for row in db.list_roll_series(connection)]

    @api.get(
        "/roll-series/{series_id}",
        response_model=RollSeriesDetail,
        tags=["卷序监测"],
        summary="查看监测序列：逐卷读数与报警卷",
    )
    def get_roll_series(
        series_id: int, connection: Connection = Depends(get_conn)
    ) -> RollSeriesDetail:
        row = _require_series(connection, series_id)
        summary = _series_summary(row)
        readings = [
            RollReadingOut(**rolls_service.reading_payload(r))
            for r in db.list_roll_readings(connection, series_id)
        ]
        alarms = [_alarm_out(a) for a in rolls_service.get_alarms(connection, series_id)]
        return RollSeriesDetail(
            **summary.model_dump(), readings=readings, alarms=alarms
        )

    @api.put(
        "/roll-series/{series_id}/readings/{roll_index}",
        response_model=RollSeriesReadResult,
        tags=["卷序监测"],
        summary="录入或更正某卷读数（可乱序补录）",
        description=(
            "同一路径重复提交即更正该卷。写入后自动回算：只从该卷起回放，"
            "不从头扫描先前各卷；处理完某个原有卷后累计值若与改动前相同，"
            "即跳过余下各卷并沿用后续报警。读数与报警在同一事务内生效；"
            "关联的质检登记号不存在时整次拒绝，原有结果保持不变。"
        ),
    )
    def put_roll_reading(
        series_id: int,
        roll_index: int,
        payload: RollReadingIn,
        connection: Connection = Depends(get_conn),
    ) -> RollSeriesReadResult:
        if roll_index < 1:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
                detail="卷序从 1 起",
            )
        connection.execute("BEGIN IMMEDIATE")
        try:
            series = _require_series(connection, series_id)
            _require_inspection_id(connection, payload.inspection_id)
            reading, alarms = rolls_service.apply_reading(
                connection,
                series_id,
                roll_index,
                payload.measured,
                payload.inspection_id,
                _config(series),
            )
            connection.execute("COMMIT")
        except BaseException:
            connection.execute("ROLLBACK")
            raise
        return RollSeriesReadResult(
            series_id=series_id,
            reading=RollReadingOut(**reading),
            alarms=[_alarm_out(a) for a in alarms],
        )

    @api.patch(
        "/roll-series/{series_id}/readings/{roll_index}",
        response_model=RollSeriesReadResult,
        tags=["卷序监测"],
        summary="更正某卷读数或关联登记号",
    )
    def patch_roll_reading(
        series_id: int,
        roll_index: int,
        payload: RollReadingUpdate,
        connection: Connection = Depends(get_conn),
    ) -> RollSeriesReadResult:
        connection.execute("BEGIN IMMEDIATE")
        try:
            series = _require_series(connection, series_id)
            existing = db.get_roll_reading(connection, series_id, roll_index)
            if existing is None:
                raise HTTPException(
                    status_code=status.HTTP_404_NOT_FOUND,
                    detail=f"监测序列 {series_id} 中没有第 {roll_index} 卷读数",
                )
            measured = (
                int(payload.measured)
                if "measured" in payload.model_fields_set
                else int(existing["measured"])
            )
            inspection_id = (
                int(payload.inspection_id)
                if "inspection_id" in payload.model_fields_set
                else int(existing["inspection_id"])
            )
            _require_inspection_id(connection, inspection_id)
            reading, alarms = rolls_service.apply_reading(
                connection,
                series_id,
                roll_index,
                measured,
                inspection_id,
                _config(series),
            )
            connection.execute("COMMIT")
        except BaseException:
            connection.execute("ROLLBACK")
            raise
        return RollSeriesReadResult(
            series_id=series_id,
            reading=RollReadingOut(**reading),
            alarms=[_alarm_out(a) for a in alarms],
        )

    @api.delete(
        "/roll-series/{series_id}/readings/{roll_index}",
        response_model=RollSeriesAlarmsOut,
        tags=["卷序监测"],
        summary="删除某卷读数",
        description=(
            "删除后从该卷位置起回算后续各卷；读数与报警在同一事务内生效。"
        ),
    )
    def delete_roll_reading(
        series_id: int,
        roll_index: int,
        connection: Connection = Depends(get_conn),
    ) -> RollSeriesAlarmsOut:
        connection.execute("BEGIN IMMEDIATE")
        try:
            series = _require_series(connection, series_id)
            existing = db.get_roll_reading(connection, series_id, roll_index)
            if existing is None:
                raise HTTPException(
                    status_code=status.HTTP_404_NOT_FOUND,
                    detail=f"监测序列 {series_id} 中没有第 {roll_index} 卷读数",
                )
            alarms = rolls_service.delete_reading(
                connection, series_id, roll_index, _config(series)
            )
            connection.execute("COMMIT")
        except BaseException:
            connection.execute("ROLLBACK")
            raise
        return RollSeriesAlarmsOut(
            series_id=series_id, alarms=[_alarm_out(a) for a in alarms]
        )

    @api.get(
        "/roll-series/{series_id}/readings",
        response_model=RollSeriesReadingsOut,
        tags=["卷序监测"],
        summary="查看逐卷读数、累计值与报警标记",
    )
    def list_roll_readings(
        series_id: int, connection: Connection = Depends(get_conn)
    ) -> RollSeriesReadingsOut:
        _require_series(connection, series_id)
        return RollSeriesReadingsOut(
            series_id=series_id,
            readings=[
                RollReadingOut(**rolls_service.reading_payload(r))
                for r in db.list_roll_readings(connection, series_id)
            ],
        )

    @api.get(
        "/roll-series/{series_id}/alarms",
        response_model=RollSeriesAlarmsOut,
        tags=["卷序监测"],
        summary="查看报警卷号及对应的质检登记号",
    )
    def list_roll_alarms(
        series_id: int, connection: Connection = Depends(get_conn)
    ) -> RollSeriesAlarmsOut:
        _require_series(connection, series_id)
        return RollSeriesAlarmsOut(
            series_id=series_id,
            alarms=[_alarm_out(a) for a in rolls_service.get_alarms(connection, series_id)],
        )

    return api


app = create_app()
