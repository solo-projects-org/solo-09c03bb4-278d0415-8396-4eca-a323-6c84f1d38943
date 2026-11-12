"""API 端到端集成测试。

在同一事件循环内启动 pymodbus 内存模拟表（随机本地端口），
通过 httpx ASGI 传输调用 FastAPI，不依赖 Docker 或固定端口。
"""

from __future__ import annotations

import asyncio
import contextlib
import socket

import httpx
import pytest
from pymodbus.server import StartAsyncTcpServer
from pymodbus.simulator import DataType, SimData, SimDevice

from app.main import app
from simulator.meter_sim import device


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


@contextlib.asynccontextmanager
async def run_meter(dev):
    """在随机端口启动给定设备的模拟表，退出时关闭。"""
    port = _free_port()
    task = asyncio.create_task(
        StartAsyncTcpServer(context=dev, address=("127.0.0.1", port))
    )
    for _ in range(50):
        try:
            _, writer = await asyncio.wait_for(
                asyncio.open_connection("127.0.0.1", port), timeout=0.2
            )
            writer.close()
            break
        except OSError:
            await asyncio.sleep(0.1)
    else:
        task.cancel()
        pytest.fail("模拟 Modbus 表启动超时")
    try:
        yield port
    finally:
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task


@pytest.fixture
async def meter_port() -> int:
    async with run_meter(device) as port:
        yield port


def make_history_device(
    *,
    latest: int,
    slot_count: int = 4,
    header_addr: int = 200,
    slots_addr: int = 202,
    base_ts: int = 1_700_000_000,
    base_count: int = 1000,
    corrupt_slot: int | None = None,
    corrupt_seq: int = 0,
) -> SimDevice:
    """构造带静态循环历史区的模拟表（槽内序号按 latest 满环推算）。"""
    values = [0] * (slots_addr + slot_count * 6)
    values[header_addr] = (latest >> 16) & 0xFFFF
    values[header_addr + 1] = latest & 0xFFFF
    for slot in range(slot_count):
        seq = latest - (latest - slot) % slot_count
        if seq < 1:
            continue  # 该槽从未写入
        if slot == corrupt_slot:
            seq = corrupt_seq
        sampled_at = base_ts + seq * 10
        count = base_count + seq
        base = slots_addr + slot * 6
        values[base:base + 6] = [
            (seq >> 16) & 0xFFFF, seq & 0xFFFF,
            (sampled_at >> 16) & 0xFFFF, sampled_at & 0xFFFF,
            (count >> 16) & 0xFFFF, count & 0xFFFF,
        ]
    return SimDevice(
        id=1,
        simdata=[SimData(address=0, count=1, values=values, datatype=DataType.REGISTERS)],
    )


def backfill_payload(port: int, **overrides) -> dict:
    payload = {
        "header_address": 200,
        "slot_address": 202,
        "slot_count": 4,
        "max_read": 12,
        "last_archived": 0,
        "host": "127.0.0.1",
        "port": port,
    }
    payload.update(overrides)
    return payload


async def _client() -> httpx.AsyncClient:
    return httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    )


async def test_read_meter_happy_path(meter_port: int) -> None:
    payload = {
        "points": [
            {"address": 0, "words": 2},
            {"address": 2, "words": 2},
            {"address": 10, "words": 1},
        ],
        "range_start": 0,
        "range_end": 99,
        "max_read": 8,
        "host": "127.0.0.1",
        "port": meter_port,
    }
    async with await _client() as client:
        resp = await client.post("/read", json=payload)
    assert resp.status_code == 200
    data = resp.json()
    # 0..3 一段、10 一段 -> 2 次；读 4 + 1 = 5 个寄存器
    assert data["request_count"] == 2
    assert data["total_registers"] == 5
    values = {p["index"]: p["value"] for p in data["points"]}
    assert values == {0: 100000, 1: 100, 2: 1234}
    assert data["segments"][0]["registers"] == [1, 34464, 0, 100]


async def test_infeasible_plan_returns_422_without_device_call(meter_port: int) -> None:
    payload = {
        "points": [{"address": 200, "words": 1}],
        "range_start": 0,
        "range_end": 99,
        "max_read": 8,
        "host": "127.0.0.1",
        "port": meter_port,
    }
    async with await _client() as client:
        resp = await client.post("/read", json=payload)
    assert resp.status_code == 422
    assert "无可行方案" in resp.json()["detail"]


async def test_connection_failure_returns_502_no_partial_reads() -> None:
    dead_port = _free_port()  # 无人监听
    payload = {
        "points": [
            {"address": 0, "words": 1},
            {"address": 20, "words": 1},
        ],
        "range_start": 0,
        "range_end": 99,
        "max_read": 8,
        "host": "127.0.0.1",
        "port": dead_port,
        "timeout": 0.5,
    }
    async with await _client() as client:
        resp = await client.post("/read", json=payload)
    assert resp.status_code == 502
    body = resp.json()
    assert "连接" in body["detail"]
    assert "segments" not in body  # 不返回部分读数


async def test_backfill_wraparound_batch() -> None:
    """跨槽区末尾的一批：槽数 4、最新 10、上次归档 0 -> 补 7..10，丢 1..6。"""
    dev = make_history_device(latest=10)
    async with run_meter(dev) as port:
        async with await _client() as client:
            resp = await client.post("/backfill", json=backfill_payload(port))
    assert resp.status_code == 200
    data = resp.json()
    assert data["cutoff_seq"] == 10
    assert data["lost_ranges"] == [{"start": 1, "end": 6}]
    assert [r["seq"] for r in data["records"]] == [7, 8, 9, 10]
    assert [r["slot"] for r in data["records"]] == [3, 0, 1, 2]
    assert data["records"][0]["sampled_at"] == 1_700_000_000 + 70
    assert data["records"][0]["count"] == 1007
    assert data["record_count"] == 4
    # 表头 1 次 + 槽区 3 段（槽 3 | 槽 0..1 | 槽 2，跨槽区末尾分段）
    assert data["request_count"] == 4
    assert [(s["start"], s["end"], s["slots"]) for s in data["segments"]] == [
        (220, 225, [3]),
        (202, 213, [0, 1]),
        (214, 219, [2]),
    ]


async def test_backfill_partial_loss_single_seq() -> None:
    """上次归档 5、最新 10、槽数 4 -> 仅序号 6 被覆盖。"""
    dev = make_history_device(latest=10)
    async with run_meter(dev) as port:
        async with await _client() as client:
            resp = await client.post(
                "/backfill", json=backfill_payload(port, last_archived=5)
            )
    assert resp.status_code == 200
    data = resp.json()
    assert data["lost_ranges"] == [{"start": 6, "end": 6}]
    assert [r["seq"] for r in data["records"]] == [7, 8, 9, 10]
    assert data["cutoff_seq"] == 10


async def test_backfill_nothing_new_reads_header_only() -> None:
    dev = make_history_device(latest=10)
    async with run_meter(dev) as port:
        async with await _client() as client:
            resp = await client.post(
                "/backfill", json=backfill_payload(port, last_archived=10)
            )
    assert resp.status_code == 200
    data = resp.json()
    assert data["records"] == []
    assert data["lost_ranges"] == []
    assert data["cutoff_seq"] == 10
    assert data["request_count"] == 1  # 只读表头
    assert data["segments"] == []


async def test_backfill_empty_device() -> None:
    dev = make_history_device(latest=0)
    async with run_meter(dev) as port:
        async with await _client() as client:
            resp = await client.post("/backfill", json=backfill_payload(port))
    assert resp.status_code == 200
    data = resp.json()
    assert data["records"] == []
    assert data["lost_ranges"] == []
    assert data["cutoff_seq"] == 0
    assert data["request_count"] == 1


async def test_backfill_slot_seq_mismatch_returns_502_no_partial() -> None:
    """槽内序号与预期不符：整批报错，不返回部分记录或新截止序号。"""
    dev = make_history_device(latest=10, corrupt_slot=0, corrupt_seq=999)
    async with run_meter(dev) as port:
        async with await _client() as client:
            resp = await client.post("/backfill", json=backfill_payload(port))
    assert resp.status_code == 502
    body = resp.json()
    assert "不符" in body["detail"]
    assert "records" not in body
    assert "cutoff_seq" not in body


async def test_backfill_archived_ahead_of_device_returns_422() -> None:
    dev = make_history_device(latest=10)
    async with run_meter(dev) as port:
        async with await _client() as client:
            resp = await client.post(
                "/backfill", json=backfill_payload(port, last_archived=11)
            )
    assert resp.status_code == 422
    assert "复位" in resp.json()["detail"]


async def test_backfill_max_read_below_one_slot_returns_422() -> None:
    dev = make_history_device(latest=10)
    async with run_meter(dev) as port:
        async with await _client() as client:
            resp = await client.post(
                "/backfill", json=backfill_payload(port, max_read=5)
            )
    assert resp.status_code == 422
    assert "整槽" in resp.text


async def test_backfill_header_slot_overlap_returns_422() -> None:
    dev = make_history_device(latest=10)
    async with run_meter(dev) as port:
        async with await _client() as client:
            resp = await client.post(
                "/backfill", json=backfill_payload(port, header_address=204)
            )
    assert resp.status_code == 422
    assert "重叠" in resp.text


async def test_backfill_connection_failure_returns_502() -> None:
    dead_port = _free_port()  # 无人监听
    async with await _client() as client:
        resp = await client.post(
            "/backfill", json=backfill_payload(dead_port, timeout=0.5)
        )
    assert resp.status_code == 502
    body = resp.json()
    assert "连接" in body["detail"]
    assert "records" not in body  # 不返回部分记录
