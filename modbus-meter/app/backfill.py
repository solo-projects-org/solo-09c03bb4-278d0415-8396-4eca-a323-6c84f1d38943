"""循环历史区补采：读段规划与槽解析（纯逻辑，不依赖设备）。

设备历史区布局（保持寄存器，32 位无符号数均为高字在前）：

- 表头 2 字：最新序号，0 表示空；
- 槽区 ``slot_count`` 个槽，每槽 6 字：序号、UTC 采样秒、累计计数；
- 序号从 1 递增、不回绕；槽号从 0 算，槽号 = 序号 % 槽数。

补采原则：以首次读到的表头序号为截止，只补 ``(上次归档序号, 截止序号]``
中仍存于槽内的记录；更早的未归档序号已被覆盖，按段上报。槽区按整槽
合并读取（相邻整槽并入一次请求，字数不超过设备与 Modbus 协议上限），
跨槽区末尾时分段；槽内序号必须与预期一致，否则整批作废。
"""

from __future__ import annotations

from dataclasses import dataclass

HEADER_WORDS = 2  # 表头字数（最新序号，32 位）
SLOT_WORDS = 6  # 每槽字数（序号 / UTC 采样秒 / 累计计数，各 32 位）
MODBUS_MAX_WORDS = 125  # Modbus 协议单次读取保持寄存器的字数上限


class BackfillPlanError(ValueError):
    """补采读段无可行方案。"""


@dataclass(frozen=True)
class SlotSegment:
    """一次 Modbus 读取对应的连续整槽段（不跨槽区末尾）。"""

    start: int  # 读段起始寄存器地址（含）
    end: int  # 读段结束寄存器地址（含）
    slots: tuple[int, ...]  # 覆盖的槽号（升序）

    @property
    def count(self) -> int:
        return self.end - self.start + 1


def decode_u32(high: int, low: int) -> int:
    """高字在前的 32 位无符号拼接。"""
    return ((high & 0xFFFF) << 16) | (low & 0xFFFF)


def parse_slot(words: list[int]) -> tuple[int, int, int]:
    """解析一个整槽的 6 个字，返回 ``(序号, UTC 采样秒, 累计计数)``。"""
    if len(words) != SLOT_WORDS:
        raise BackfillPlanError(f"槽数据应为 {SLOT_WORDS} 字，实际 {len(words)} 字")
    return (
        decode_u32(words[0], words[1]),
        decode_u32(words[2], words[3]),
        decode_u32(words[4], words[5]),
    )


def expected_seq(slot: int, cutoff_seq: int, slot_count: int) -> int:
    """槽 ``slot`` 在截止序号 ``cutoff_seq`` 时按序应存的序号。

    即不超过 ``cutoff_seq`` 且对 ``slot_count`` 同余于 ``slot`` 的最大序号；
    结果小于 1 表示该槽从未被写入。
    """
    return cutoff_seq - (cutoff_seq - slot) % slot_count


def surviving_start(last_archived: int, cutoff_seq: int, slot_count: int) -> int:
    """尚存的未归档记录的最小序号（设备只保留最近 ``slot_count`` 条）。"""
    return max(last_archived + 1, cutoff_seq - slot_count + 1, 1)


def lost_range(
    last_archived: int, cutoff_seq: int, slot_count: int
) -> tuple[int, int] | None:
    """此前已被覆盖的未归档序号段（含两端）；无丢失返回 ``None``。"""
    end = cutoff_seq - slot_count  # 已被覆盖的最大序号
    if end < last_archived + 1:
        return None
    return (last_archived + 1, end)


def slot_intervals(
    start_seq: int, cutoff_seq: int, slot_count: int
) -> list[tuple[int, int]]:
    """覆盖 ``[start_seq, cutoff_seq]`` 的槽号区间（含两端），按槽区地址升序。

    序号连续且不超过 ``slot_count`` 条，因此结果为 1 段；跨槽区末尾时为 2 段
    （前段到槽区末尾为止，后段从槽 0 开始）。
    """
    first = start_seq % slot_count
    last = cutoff_seq % slot_count
    if first <= last:
        return [(first, last)]
    return [(first, slot_count - 1), (0, last)]


def plan_history_reads(
    slot_address: int,
    slot_count: int,
    max_read: int,
    start_seq: int,
    cutoff_seq: int,
) -> list[SlotSegment]:
    """规划槽区读段：相邻整槽合并，不跨槽区末尾。

    每段字数 = 整槽数 × 6，不超过 ``min(max_read, 125)``（设备上限与
    Modbus 协议上限取小）。``start_seq > cutoff_seq`` 时无槽可读，返回空表。

    :raises BackfillPlanError: 单次读取上限不足一个整槽时抛出
    """
    if slot_address < 0 or slot_count < 1:
        raise BackfillPlanError(f"槽区参数非法：首槽地址 {slot_address}，槽数 {slot_count}")
    if start_seq > cutoff_seq:
        return []
    per_segment = min(max_read, MODBUS_MAX_WORDS) // SLOT_WORDS
    if per_segment < 1:
        raise BackfillPlanError(
            f"单次读取上限 {max_read} 字不足一个整槽（{SLOT_WORDS} 字），无法补采"
        )

    segments: list[SlotSegment] = []
    for first, last in slot_intervals(start_seq, cutoff_seq, slot_count):
        slot = first
        while slot <= last:
            end_slot = min(slot + per_segment - 1, last)
            segments.append(
                SlotSegment(
                    start=slot_address + slot * SLOT_WORDS,
                    end=slot_address + (end_slot + 1) * SLOT_WORDS - 1,
                    slots=tuple(range(slot, end_slot + 1)),
                )
            )
            slot = end_slot + 1
    return segments
