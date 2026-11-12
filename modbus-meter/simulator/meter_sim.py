"""本地模拟 Modbus TCP 电表（pymodbus 3.16 simulator API）。

保持寄存器布局：

- 地址 0..99：试采演示区。地址 0..1、2..3 预置 32 位无符号计数
  （高字在前），地址 10..13 预置单字演示值，其余按确定性公式填充；
- 地址 200..201：循环历史区表头——最新序号（32 位无符号，高字在前，
  0 表示空）；
- 地址 202..249：8 个历史槽，每槽 6 字——序号、UTC 采样秒、累计计数
  （均 32 位无符号，高字在前）。

历史区模拟真实电表的循环历史：启动时已有 20 条记录（序号 1..20，
采样时刻按 1 秒间隔回推），之后每 1 秒追加一条；序号从 1 递增、
不回绕，槽号 = 序号 % 槽数。通过 SimDevice 的 action 回调在每次
访问时按墙钟惰性重算，无需后台任务；两次读取之间的新写入对补采
接口即表现为"期间新写入的记录"。
"""

from __future__ import annotations

import asyncio
import logging
import os
import time

from pymodbus.server import StartAsyncTcpServer
from pymodbus.simulator import DataType, SimData, SimDevice

SIZE = 100
values = [(addr * 37 + 11) & 0xFFFF for addr in range(SIZE)]

# 预置演示值（高字在前）
values[0], values[1] = 0x0001, 0x86A0  # 32 位 = 100000
values[2], values[3] = 0x0000, 0x0064  # 32 位 = 100
values[10:14] = [1234, 5678, 0xFFFF, 42]

# 循环历史区布局
HEADER_ADDR = 200  # 表头：最新序号（2 字，高字在前）
SLOTS_ADDR = 202  # 首槽地址
SLOT_COUNT = 8  # 槽数
SLOT_WORDS = 6  # 每槽字数：序号 / UTC 采样秒 / 累计计数（各 2 字）
HISTORY_WORDS = 2 + SLOT_COUNT * SLOT_WORDS  # 表头 + 槽区共 50 字
PREFILL = 20  # 启动时已存在的记录条数（序号 1..20）
INTERVAL_S = 1.0  # 追加一条记录的间隔（秒）
BOOT_TIME = time.time()


def _u32_words(value: int) -> list[int]:
    """32 位无符号拆为高字在前的两个字。"""
    return [(value >> 16) & 0xFFFF, value & 0xFFFF]


def _latest_seq(now: float) -> int:
    """``now`` 时刻设备应有的最新序号。"""
    return PREFILL + max(0, int((now - BOOT_TIME) // INTERVAL_S))


def render_history(now: float | None = None) -> list[int]:
    """生成 ``now`` 时刻历史区的 50 个字（表头 2 字 + 8 槽 × 6 字）。"""
    now = time.time() if now is None else now
    latest = _latest_seq(now)
    words = _u32_words(latest)
    for slot in range(SLOT_COUNT):
        # 该槽按序应存的序号：不超过 latest 且与 slot 同余（模槽数）的最大者
        seq = latest - (latest - slot) % SLOT_COUNT
        if seq < 1:  # 该槽从未写入
            words += [0] * SLOT_WORDS
            continue
        sampled_at = int(BOOT_TIME + (seq - PREFILL) * INTERVAL_S)
        count = 500_000 + seq * 1000  # 单调递增的累计计数
        words += _u32_words(seq) + _u32_words(sampled_at) + _u32_words(count)
    return words


async def _history_action(
    func_code: int,
    start_address: int,
    address: int,
    count: int,
    current_registers: list[int],
    set_values: list[int] | list[bool] | None,
) -> None:
    """每次访问前按墙钟重算历史区（内联修改，影响本次及后续响应）。"""
    base = HEADER_ADDR - start_address
    current_registers[base:base + HISTORY_WORDS] = render_history()
    return None


# 0 基地址：协议地址 0 对应 values[0]；100..199 为无效空洞（真实电表亦有地址间隙）
device = SimDevice(
    id=1,
    simdata=[
        SimData(address=0, count=1, values=values, datatype=DataType.REGISTERS),
        SimData(
            address=HEADER_ADDR,
            count=1,
            values=render_history(),
            datatype=DataType.REGISTERS,
        ),
    ],
    action=_history_action,
)


async def main() -> None:
    logging.basicConfig(level=logging.INFO)
    port = int(os.getenv("METER_PORT", "502"))
    await StartAsyncTcpServer(context=device, address=("0.0.0.0", port))


if __name__ == "__main__":
    asyncio.run(main())
