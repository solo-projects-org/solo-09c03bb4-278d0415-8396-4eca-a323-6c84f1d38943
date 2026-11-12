"""FastAPI：单台 Modbus TCP 电表试采与循环历史区补采接口。"""

from __future__ import annotations

import os

from fastapi import FastAPI, HTTPException
from pydantic import BaseModel, Field, model_validator
from pymodbus.client import AsyncModbusTcpClient
from pymodbus.exceptions import ModbusException

from .backfill import (
    HEADER_WORDS,
    SLOT_WORDS,
    BackfillPlanError,
    expected_seq,
    lost_range,
    parse_slot,
    plan_history_reads,
    surviving_start,
)
from .planner import PlanError, Segment, plan_reads

DEFAULT_HOST = os.getenv("METER_HOST", "meter")
DEFAULT_PORT = int(os.getenv("METER_PORT", "502"))
DEFAULT_SLAVE = int(os.getenv("METER_SLAVE_ID", "1"))
DEFAULT_TIMEOUT = float(os.getenv("METER_TIMEOUT", "3"))


class PointIn(BaseModel):
    address: int = Field(..., ge=0, description="保持寄存器起始地址（从 0 算）")
    words: int = Field(..., ge=1, le=2, description="字数：1（单字）或 2（双字，高字在前）")


class ReadRequest(BaseModel):
    points: list[PointIn] = Field(..., min_length=1, description="待试采测点，至少 1 个")
    range_start: int = Field(..., ge=0, description="设备可读区间起始地址（含两端）")
    range_end: int = Field(..., ge=0, description="设备可读区间结束地址（含两端）")
    max_read: int = Field(..., ge=1, description="单次读取寄存器字数上限")
    host: str = Field(default=DEFAULT_HOST, description="Modbus TCP 设备地址")
    port: int = Field(default=DEFAULT_PORT, ge=1, le=65535, description="Modbus TCP 端口")
    slave_id: int = Field(default=DEFAULT_SLAVE, ge=1, le=247, description="从站 / 单元号")
    timeout: float = Field(default=DEFAULT_TIMEOUT, gt=0, le=30, description="连接与读取超时（秒）")

    @model_validator(mode="after")
    def _check_range(self) -> "ReadRequest":
        if self.range_end < self.range_start:
            raise ValueError("range_end 不能小于 range_start")
        return self


class PointOut(BaseModel):
    index: int = Field(..., description="测点在请求 points 中的下标（从 0 算）")
    address: int
    words: int
    value: int = Field(..., description="无符号计数；双字时按高字在前（big-endian）拼接")


class SegmentOut(BaseModel):
    start: int = Field(..., description="读段起始地址（含）")
    end: int = Field(..., description="读段结束地址（含）")
    count: int = Field(..., description="本次读取的寄存器字数")
    point_indexes: list[int] = Field(..., description="该读段覆盖的测点下标")
    registers: list[int] = Field(..., description="读回的原始 16 位寄存器值（按地址升序）")


class ReadResponse(BaseModel):
    request_count: int = Field(..., description="实际 Modbus 请求次数（已最少化）")
    total_registers: int = Field(..., description="读取寄存器总数（次数相同时已最少化）")
    segments: list[SegmentOut]
    points: list[PointOut] = Field(..., description="按请求下标返回的测点评值")


class BackfillRequest(BaseModel):
    header_address: int = Field(
        default=200,
        ge=0,
        description="表头地址（2 字：最新序号，32 位无符号高字在前，0 表示空）",
    )
    slot_address: int = Field(
        default=202,
        ge=0,
        description="首槽地址（每槽 6 字：序号、UTC 采样秒、累计计数，均 32 位高字在前）",
    )
    slot_count: int = Field(
        default=8, ge=1, description="槽数；槽号从 0 算，槽号 = 序号 % 槽数"
    )
    max_read: int = Field(
        default=32,
        ge=1,
        description="单次读取寄存器字数上限（再与 Modbus 协议上限 125 取小）",
    )
    last_archived: int = Field(
        default=0,
        ge=0,
        le=4294967295,
        description="上次归档序号（0 表示尚未归档任何记录）",
    )
    host: str = Field(default=DEFAULT_HOST, description="Modbus TCP 设备地址")
    port: int = Field(default=DEFAULT_PORT, ge=1, le=65535, description="Modbus TCP 端口")
    slave_id: int = Field(default=DEFAULT_SLAVE, ge=1, le=247, description="从站 / 单元号")
    timeout: float = Field(default=DEFAULT_TIMEOUT, gt=0, le=30, description="连接与读取超时（秒）")

    @model_validator(mode="after")
    def _check_layout(self) -> "BackfillRequest":
        if self.max_read < SLOT_WORDS:
            raise ValueError(
                f"单次读取字数上限必须 >= {SLOT_WORDS}（一个整槽 {SLOT_WORDS} 字）"
            )
        header_end = self.header_address + HEADER_WORDS - 1
        slots_end = self.slot_address + self.slot_count * SLOT_WORDS - 1
        if self.header_address <= slots_end and self.slot_address <= header_end:
            raise ValueError("表头与槽区地址重叠")
        return self


class BackfillRecord(BaseModel):
    seq: int = Field(..., description="记录序号（从 1 递增，不回绕）")
    slot: int = Field(..., description="槽号（从 0 算）")
    sampled_at: int = Field(..., description="UTC 采样秒")
    count: int = Field(..., description="累计计数")


class LostRange(BaseModel):
    start: int = Field(..., description="被覆盖的未归档序号段起点（含）")
    end: int = Field(..., description="被覆盖的未归档序号段终点（含）")


class BackfillSegment(BaseModel):
    start: int = Field(..., description="读段起始地址（含）")
    end: int = Field(..., description="读段结束地址（含）")
    count: int = Field(..., description="本次读取的寄存器字数")
    slots: list[int] = Field(..., description="该读段覆盖的槽号")


class BackfillResponse(BaseModel):
    records: list[BackfillRecord] = Field(
        ..., description="尚存的未归档记录，按序号升序"
    )
    lost_ranges: list[LostRange] = Field(
        ..., description="此前已被覆盖的未归档序号段（含两端）"
    )
    cutoff_seq: int = Field(
        ...,
        description="本批截止序号（首次读到的表头值），供下轮补采作为上次归档序号",
    )
    record_count: int = Field(..., description="本批补回的记录条数")
    request_count: int = Field(..., description="实际 Modbus 请求次数（含表头读取）")
    segments: list[BackfillSegment] = Field(
        ..., description="槽区读段（相邻整槽合并，不跨槽区末尾）"
    )


app = FastAPI(
    title="Modbus TCP 电表试采与补采接口",
    version="1.1.0",
    description=(
        "试采：提交测点保持寄存器起始地址（从 0 算）与字数（1 或 2），"
        "并给出设备可读区间（含两端）与单次读取上限。"
        "接口先做读段规划（请求次数最少，次数相同则读取寄存器总数最少），"
        "再逐段读取，返回读段与无符号计数（双字高字在前）。\n\n"
        "补采：网关恢复通信后，从电表循环历史区补回漏采记录。"
        "以首次读到的表头序号为截止，按整槽合并读取（不跨槽区末尾），"
        "返回尚存的未归档记录、已被覆盖的未归档序号段和供下轮的截止序号；"
        "期间新写入的记录留待下轮。\n\n"
        "无可行方案或读取失败时直接报错，不返回部分读数。"
    ),
)


def decode_unsigned(words: list[int]) -> int:
    """无符号拼接；双字高字在前（big-endian / 高字在低地址）。"""
    value = 0
    for word in words:
        value = (value << 16) | (word & 0xFFFF)
    return value


@app.get("/", include_in_schema=False)
def root() -> dict[str, str]:
    return {"service": "modbus-meter-sampling", "docs": "/docs"}


@app.post(
    "/read",
    response_model=ReadResponse,
    summary="规划读段并对单台 Modbus TCP 电表执行试采",
    responses={
        422: {"description": "请求参数校验失败 / 无可行方案"},
        502: {"description": "连接或 Modbus 读取失败（不会返回部分读数）"},
    },
)
async def read_meter(req: ReadRequest) -> ReadResponse:
    try:
        segments: list[Segment] = plan_reads(
            points=[(p.address, p.words) for p in req.points],
            range_start=req.range_start,
            range_end=req.range_end,
            max_read=req.max_read,
        )
    except PlanError as exc:
        # 规划阶段失败：尚未与设备通信
        raise HTTPException(status_code=422, detail=str(exc)) from exc

    client = AsyncModbusTcpClient(
        host=req.host,
        port=req.port,
        timeout=req.timeout,
    )
    try:
        await client.connect()
        if not client.connected:
            raise HTTPException(
                status_code=502,
                detail=f"连接 Modbus 设备失败：{req.host}:{req.port}",
            )

        seg_out: list[SegmentOut] = []
        # point_index -> 该测点读回的原始寄存器值
        point_words: dict[int, list[int]] = {}

        for seg in segments:
            try:
                rr = await client.read_holding_registers(
                    address=seg.start,
                    count=seg.count,
                    device_id=req.slave_id,
                )
            except (ModbusException, OSError, TimeoutError) as exc:
                raise HTTPException(
                    status_code=502,
                    detail=f"读取地址 {seg.start}..{seg.end} 失败：{exc}",
                ) from exc
            if rr.isError():
                raise HTTPException(
                    status_code=502,
                    detail=f"读取地址 {seg.start}..{seg.end} 失败：{rr}",
                )

            registers = list(rr.registers)
            seg_out.append(
                SegmentOut(
                    start=seg.start,
                    end=seg.end,
                    count=seg.count,
                    point_indexes=list(seg.point_indexes),
                    registers=registers,
                )
            )
            for idx in seg.point_indexes:
                p = req.points[idx]
                offset = p.address - seg.start
                point_words[idx] = registers[offset:offset + p.words]
    finally:
        client.close()

    points_out = [
        PointOut(
            index=i,
            address=p.address,
            words=p.words,
            value=decode_unsigned(point_words[i]),
        )
        for i, p in enumerate(req.points)
    ]
    return ReadResponse(
        request_count=len(segments),
        total_registers=sum(s.count for s in segments),
        segments=seg_out,
        points=points_out,
    )


async def _read_registers(
    client: AsyncModbusTcpClient, req: BackfillRequest, address: int, count: int
) -> list[int]:
    """读取一段保持寄存器；失败抛 502（整批作废，不返回部分数据）。"""
    try:
        rr = await client.read_holding_registers(
            address=address,
            count=count,
            device_id=req.slave_id,
        )
    except (ModbusException, OSError, TimeoutError) as exc:
        raise HTTPException(
            status_code=502,
            detail=f"读取地址 {address}..{address + count - 1} 失败：{exc}",
        ) from exc
    if rr.isError():
        raise HTTPException(
            status_code=502,
            detail=f"读取地址 {address}..{address + count - 1} 失败：{rr}",
        )
    return list(rr.registers)


@app.post(
    "/backfill",
    response_model=BackfillResponse,
    summary="从电表循环历史区补采一批未归档记录",
    responses={
        422: {"description": "请求参数校验失败 / 与设备状态矛盾 / 无可行方案"},
        502: {"description": "连接、读取失败或槽内序号与预期不符（整批作废，不返回部分记录）"},
    },
)
async def backfill_meter(req: BackfillRequest) -> BackfillResponse:
    client = AsyncModbusTcpClient(
        host=req.host,
        port=req.port,
        timeout=req.timeout,
    )
    try:
        await client.connect()
        if not client.connected:
            raise HTTPException(
                status_code=502,
                detail=f"连接 Modbus 设备失败：{req.host}:{req.port}",
            )

        # 首次读到的表头即本批截止；期间新写入的记录留待下轮
        header_regs = await _read_registers(client, req, req.header_address, HEADER_WORDS)
        cutoff_seq = decode_unsigned(header_regs)

        if req.last_archived > cutoff_seq:
            raise HTTPException(
                status_code=422,
                detail=(
                    f"上次归档序号 {req.last_archived} 大于设备最新序号 "
                    f"{cutoff_seq}，设备可能被复位"
                ),
            )
        if cutoff_seq == 0 or req.last_archived == cutoff_seq:
            # 设备为空，或无新记录
            return BackfillResponse(
                records=[],
                lost_ranges=[],
                cutoff_seq=cutoff_seq,
                record_count=0,
                request_count=1,
                segments=[],
            )

        start_seq = surviving_start(req.last_archived, cutoff_seq, req.slot_count)
        lost = lost_range(req.last_archived, cutoff_seq, req.slot_count)
        try:
            segments = plan_history_reads(
                slot_address=req.slot_address,
                slot_count=req.slot_count,
                max_read=req.max_read,
                start_seq=start_seq,
                cutoff_seq=cutoff_seq,
            )
        except BackfillPlanError as exc:
            # 规划阶段失败：尚未读取槽区
            raise HTTPException(status_code=422, detail=str(exc)) from exc

        records: list[BackfillRecord] = []
        seg_out: list[BackfillSegment] = []
        for seg in segments:
            registers = await _read_registers(client, req, seg.start, seg.count)
            seg_out.append(
                BackfillSegment(
                    start=seg.start,
                    end=seg.end,
                    count=seg.count,
                    slots=list(seg.slots),
                )
            )
            for i, slot in enumerate(seg.slots):
                seq, sampled_at, count = parse_slot(
                    registers[i * SLOT_WORDS:(i + 1) * SLOT_WORDS]
                )
                want = expected_seq(slot, cutoff_seq, req.slot_count)
                if seq != want:
                    raise HTTPException(
                        status_code=502,
                        detail=(
                            f"槽 {slot} 内序号 {seq} 与预期 {want} 不符"
                            "（采集期间历史区被新记录覆盖），整批作废，不返回部分记录"
                        ),
                    )
                records.append(
                    BackfillRecord(seq=seq, slot=slot, sampled_at=sampled_at, count=count)
                )

        records.sort(key=lambda r: r.seq)
        return BackfillResponse(
            records=records,
            lost_ranges=[LostRange(start=lost[0], end=lost[1])] if lost else [],
            cutoff_seq=cutoff_seq,
            record_count=len(records),
            request_count=1 + len(segments),
            segments=seg_out,
        )
    finally:
        client.close()
