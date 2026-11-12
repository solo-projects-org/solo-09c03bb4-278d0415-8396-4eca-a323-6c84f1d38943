# Modbus TCP 电表试采与补采接口

单台 Modbus TCP 电表的试采与循环历史区补采服务：

- **试采**：厂务工程师提交测点的保持寄存器起始地址（从 0 算）与字数（1 或 2），
  并给出设备的可读区间（含两端）与单次读取上限；接口自动规划读段、按方案读表，
  返回读段与无符号计数（双字按高字在前拼接）。
- **补采**：网关恢复通信后，从电表自带的循环历史区补回漏采记录。接口以首次读到
  的表头序号为截止，按整槽合并读取（不跨槽区末尾），返回尚存的未归档记录、
  此前已被覆盖的未归档序号段和供下轮补采的截止序号；期间新写入的记录留待下轮。

## 快速开始（Docker Compose）

```bash
docker compose up --build
```

- API：<http://localhost:8000>，Swagger UI：<http://localhost:8000/docs>
- 模拟电表：`localhost:1502`（容器内 `meter:502`），保持寄存器 0..99 为试采
  演示区，200..249 为循环历史区

### 试采

在 Swagger 中打开 `POST /read` → Try it out → 直接 Execute 即可对模拟表试采
（请求体默认指向容器内的模拟表）。也可在宿主机用 curl：

```bash
curl -X POST http://localhost:8000/read \
  -H 'Content-Type: application/json' \
  -d '{
    "points": [
      {"address": 0, "words": 2},
      {"address": 2, "words": 2},
      {"address": 10, "words": 1},
      {"address": 12, "words": 1}
    ],
    "range_start": 0,
    "range_end": 99,
    "max_read": 8
  }'
```

### 补采（跨槽区末尾的一批）

在 Swagger 中打开 `POST /backfill` → Try it out → **直接 Execute**（默认请求体
即指向模拟表历史区：表头 200、首槽 202、槽数 8、`last_archived` 0）。模拟表
启动时已有 20 条记录并每秒追加 1 条，而槽只有 8 个，因此首批必然从槽区中部
起读、跨槽区末尾回绕，响应 `segments` 中可见分段（槽 5..7 一段、槽 0.. 接续
一段之类），`lost_ranges` 给出已被覆盖的序号段。

> 模拟表每秒写一条，若 Execute 恰逢最新序号 ≡ 7 (mod 8) 的那一秒，整环恰好
> 对齐槽区起点、该批不回绕，稍候一秒再试即可。

随后把上轮响应的 `cutoff_seq` 填入 `last_archived` 再 Execute：补回上一批
之后新写入的记录（上一批"期间新写入的记录"即在此轮出现），`lost_ranges`
为空。也可用 curl：

```bash
curl -X POST http://localhost:8000/backfill \
  -H 'Content-Type: application/json' \
  -d '{"header_address": 200, "slot_address": 202, "slot_count": 8,
       "max_read": 32, "last_archived": 0}'
```

## 接口说明

### `POST /read`

请求体：

| 字段 | 说明 |
| --- | --- |
| `points` | 测点列表，`address` 为保持寄存器起始地址（从 0 算），`words` 为字数（1 或 2） |
| `range_start` / `range_end` | 设备可读区间，含两端 |
| `max_read` | 单次读取寄存器字数上限 |
| `host` / `port` / `slave_id` / `timeout` | 设备连接参数，默认指向 Compose 中的模拟表 |

读段规划规则：

1. 每次读取一段连续寄存器，读段整体落在可读区间内，字数不超过 `max_read`；
2. 每个测点必须被某一次读取完整覆盖（不允许跨段拼接）；
3. 先使请求次数最少；次数相同时使读取寄存器总数最少
   （按地址排序后动态规划求解，读段紧贴测点，不多读无用寄存器）。

响应（200）：

- `request_count` / `total_registers`：实际请求次数与读取寄存器总数；
- `segments`：各读段的起止地址（含）、字数、覆盖的测点下标、原始寄存器值；
- `points`：按请求下标返回的测点值，`value` 为无符号计数，
  双字按高字在前（big-endian）拼接。

### `POST /backfill`

请求体：

| 字段 | 说明 |
| --- | --- |
| `header_address` | 表头地址（2 字：最新序号，32 位无符号高字在前，0 表示空），默认 200 |
| `slot_address` | 首槽地址（每槽 6 字：序号、UTC 采样秒、累计计数，均 32 位高字在前），默认 202 |
| `slot_count` | 槽数；槽号从 0 算，槽号 = 序号 % 槽数，默认 8 |
| `max_read` | 单次读取寄存器字数上限（再与 Modbus 协议上限 125 取小），默认 32 |
| `last_archived` | 上次归档序号（0 表示尚未归档），默认 0 |
| `host` / `port` / `slave_id` / `timeout` | 设备连接参数，默认指向 Compose 中的模拟表 |

设备历史区模型：

- 表头存最新序号，0 表示空；序号从 1 递增、不回绕；
- 设备按整槽更新，每槽依次存序号、UTC 采样秒、累计计数（各 2 字，高字在前）；
- 槽区只保留最近 `slot_count` 条，更老的记录被新记录覆盖。

补采流程：

1. 先读表头，**以首次读到的表头序号为本批截止**；期间新写入的记录留待下轮；
2. 未归档区间为 `(last_archived, 截止序号]`：其中仍存于槽内的记录按整槽
   合并读取（相邻整槽并入一次请求，字数不超过设备与协议上限，跨槽区末尾
   时分段），更早的序号已被覆盖，并入 `lost_ranges`；
3. 逐槽校验槽内序号与预期一致。

响应（200），按序为：

- `records`：尚存的未归档记录（`seq` / `slot` / `sampled_at` / `count`），
  按序号升序；
- `lost_ranges`：此前已被覆盖的未归档序号段（`start` / `end`，含两端）；
- `cutoff_seq`：本批截止序号，供下轮补采作为 `last_archived`；
- 另有 `record_count` / `request_count` / `segments`（槽区读段明细）便于核对。

### 错误

- `422`：参数校验失败（含表头与槽区地址重叠、单次上限不足一个整槽 6 字）、
  无可行方案，或上次归档序号大于设备最新序号（设备可能被复位），
  `detail` 为中文原因；
- `502`：连接失败、Modbus 读取失败，或槽内序号与预期不符（采集期间历史区
  被新记录覆盖）。**整批报错，不返回部分记录或新截止序号**，重试即可。

## 模拟电表数据

`simulator/meter_sim.py`：保持寄存器分两段，地址 100..199 为无效空洞。

试采演示区 0..99（可读区间 `[0, 99]`）：

| 地址 | 值 | 说明 |
| --- | --- | --- |
| 0..1 | 0x0001, 0x86A0 | 32 位无符号 100000（高字在前） |
| 2..3 | 0x0000, 0x0064 | 32 位无符号 100 |
| 10..13 | 1234, 5678, 65535, 42 | 单字演示值 |
| 其余 | `(addr * 37 + 11) & 0xFFFF` | 确定性填充，便于人工核对 |

循环历史区 200..249（模拟真实电表，每秒追加一条）：

| 地址 | 说明 |
| --- | --- |
| 200..201 | 表头：最新序号（32 位高字在前） |
| 202..249 | 8 槽 × 6 字：序号、UTC 采样秒、累计计数（均 32 位高字在前） |

启动时已有序号 1..20（采样时刻按 1 秒间隔回推），之后每秒追加一条；
累计计数为 500000 + 序号 × 1000。历史区通过 SimDevice 的 action 回调
在每次访问时按墙钟惰性重算，无需后台任务。

## 本地开发（无 Docker）

```bash
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
METER_PORT=1502 .venv/bin/python -m simulator.meter_sim &
METER_HOST=127.0.0.1 METER_PORT=1502 .venv/bin/uvicorn app.main:app --port 8000
.venv/bin/pytest            # 44 个用例：规划算法 + 补采规划 + API 集成
```

环境变量：`METER_HOST` / `METER_PORT` / `METER_SLAVE_ID` / `METER_TIMEOUT`
为 API 请求体连接参数的默认值（Compose 中已注入）。

## 目录结构

```
app/main.py        FastAPI 接口：/read 试采、/backfill 补采
app/planner.py     试采读段规划（动态规划：次数最少 -> 寄存器总数最少）
app/backfill.py    补采规划与槽解析（整槽合并、跨槽区末尾分段、序号校验）
simulator/         pymodbus 内存模拟电表（试采演示区 + 动态循环历史区）
tests/             规划/补采单元测试 + API 集成测试（内存模拟表，无需 Docker）
docker-compose.yml api + meter 两个服务
```
