"""API 请求/响应模型。"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field, model_validator


class SegmentIn(BaseModel):
    lo: int = Field(..., ge=0, description="区间起点，包含，整数微米")
    hi: int = Field(..., description="区间终点，不包含，整数微米，必须大于 lo")
    count: int = Field(..., ge=0, description="该厚度区间内的片数")

    @model_validator(mode="after")
    def _validate_range(self) -> "SegmentIn":
        if self.hi <= self.lo:
            raise ValueError("hi 必须大于 lo（区间含起点、不含终点）")
        return self


class CountReportIn(BaseModel):
    segments: list[SegmentIn] = Field(
        ...,
        min_length=1,
        description="本次补报的全部厚度区间计数；同一报告内或与已有报告可以重叠。"
        "任一区间与已有计数不相容时，整份报告拒收。",
    )


class InspectionCreate(BaseModel):
    total_pieces: int = Field(..., ge=0, description="纸样总片数")
    note: str | None = Field(None, max_length=500, description="备注，可选")


class InspectionUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    total_pieces: int | None = Field(None, ge=0, description="新的总片数，可选")
    note: str | None = Field(None, max_length=500, description="新的备注；传 null 可清空")

    @model_validator(mode="after")
    def _validate_payload(self) -> "InspectionUpdate":
        if not self.model_fields_set:
            raise ValueError("至少提供 total_pieces 或 note 中的一项")
        return self


class SegmentOut(BaseModel):
    id: int
    lo: int
    hi: int
    count: int


class CountReportOut(BaseModel):
    id: int
    inspection_id: int
    created_at: str
    segments: list[SegmentOut]


class InspectionSummary(BaseModel):
    id: int
    total_pieces: int
    note: str | None
    created_at: str
    report_count: int = Field(..., description="已接收的补报批次数")
    segment_count: int = Field(..., description="已接收的厚度区间条数")


class InspectionDetail(InspectionSummary):
    reports: list[CountReportOut]


class BoundsOut(BaseModel):
    inspection_id: int
    total_pieces: int
    qualified_lo: int
    qualified_hi: int
    min_count: int = Field(..., description="同时满足全部计数时，合格片数可达到的最小值")
    max_count: int = Field(..., description="同时满足全部计数时，合格片数可达到的最大值")


# ---------------------------------------------------------------------------
# 卷序监测
# ---------------------------------------------------------------------------


class RollSeriesCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    target_thickness: int = Field(
        ..., ge=0, description="目标厚度，整数微米"
    )
    tolerance: int = Field(
        ..., ge=0, description="容差，整数微米；超出目标加容差的部分才计入累计"
    )
    alarm_threshold: int = Field(
        ..., gt=0, description="报警线，整数微米；累计值达到或超过即报警并清零"
    )
    note: str | None = Field(None, max_length=500, description="备注，可选")


class RollReadingIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    measured: int = Field(..., ge=0, description="实测厚度，整数微米")
    inspection_id: int = Field(
        ..., ge=1, description="关联的已有质检登记号（inspections.id）"
    )


class RollReadingUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    measured: int | None = Field(None, ge=0, description="新的实测厚度，整数微米")
    inspection_id: int | None = Field(
        None, ge=1, description="新的关联质检登记号；传 null 或省略表示不改"
    )

    @model_validator(mode="after")
    def _validate_payload(self) -> "RollReadingUpdate":
        if not self.model_fields_set:
            raise ValueError("至少提供 measured 或 inspection_id 中的一项")
        if "measured" in self.model_fields_set and self.measured is None:
            raise ValueError("measured 不能为 null")
        if "inspection_id" in self.model_fields_set and self.inspection_id is None:
            raise ValueError("inspection_id 不能为 null；不改动请省略该字段")
        return self


class RollReadingOut(BaseModel):
    roll_index: int
    measured: int
    inspection_id: int
    cumulative_after: int = Field(
        ..., description="处理完该卷后的累计值；报警卷此处已清零"
    )
    alarmed: bool = Field(..., description="该卷是否达到报警线")


class AlarmRollOut(BaseModel):
    roll_index: int = Field(..., description="报警卷的生产卷序")
    measured: int = Field(..., description="该卷实测厚度，整数微米")
    inspection_id: int = Field(..., description="该卷关联的质检登记号")


class RollSeriesSummary(BaseModel):
    id: int
    target_thickness: int
    tolerance: int
    alarm_threshold: int
    note: str | None
    created_at: str
    reading_count: int = Field(..., description="已录入的卷数")
    alarm_count: int = Field(..., description="报警卷数")


class RollSeriesDetail(RollSeriesSummary):
    readings: list[RollReadingOut]
    alarms: list[AlarmRollOut]


class RollSeriesReadResult(BaseModel):
    """单卷写入的返回：写入后的该卷状态，以及当前全部报警卷。"""

    series_id: int
    reading: RollReadingOut
    alarms: list[AlarmRollOut]


class RollSeriesReadingsOut(BaseModel):
    series_id: int
    readings: list[RollReadingOut]


class RollSeriesAlarmsOut(BaseModel):
    series_id: int
    alarms: list[AlarmRollOut]
