"""补采读段规划与槽解析单元测试（不需要设备与网络）。"""

from __future__ import annotations

import pytest

from app.backfill import (
    SLOT_WORDS,
    BackfillPlanError,
    decode_u32,
    expected_seq,
    lost_range,
    parse_slot,
    plan_history_reads,
    slot_intervals,
    surviving_start,
)


def seg_tuples(segments):
    return [(s.start, s.end, tuple(s.slots)) for s in segments]


class TestExpectedSeq:
    def test_slot_holding_cutoff(self):
        # 截止序号 10、槽数 4：槽 2 存 10，槽 3 存 7，槽 0 存 8，槽 1 存 9
        assert expected_seq(2, 10, 4) == 10
        assert expected_seq(3, 10, 4) == 7
        assert expected_seq(0, 10, 4) == 8
        assert expected_seq(1, 10, 4) == 9

    def test_slot_never_written_when_seq_below_one(self):
        # 最新序号 3、槽数 8：槽 5 对应序号 -3，表示从未写入
        assert expected_seq(5, 3, 8) < 1
        assert expected_seq(3, 3, 8) == 3

    def test_single_slot_ring(self):
        assert expected_seq(0, 99, 1) == 99


class TestSurvivingAndLost:
    def test_all_survive_when_archive_is_recent(self):
        # 上次归档 8、最新 10、槽数 4：9、10 均在槽内，无丢失
        assert surviving_start(8, 10, 4) == 9
        assert lost_range(8, 10, 4) is None

    def test_oldest_unarchived_overwritten(self):
        # 上次归档 0、最新 10、槽数 4：槽内仅存 7..10，1..6 被覆盖
        assert surviving_start(0, 10, 4) == 7
        assert lost_range(0, 10, 4) == (1, 6)

    def test_partial_loss_single_seq(self):
        # 上次归档 5、最新 10、槽数 4：仅 6 被覆盖
        assert surviving_start(5, 10, 4) == 7
        assert lost_range(5, 10, 4) == (6, 6)

    def test_nothing_lost_when_ring_not_full_yet(self):
        # 最新 3、槽数 8：1..3 全在，无丢失
        assert surviving_start(0, 3, 8) == 1
        assert lost_range(0, 3, 8) is None


class TestSlotIntervals:
    def test_no_wrap(self):
        assert slot_intervals(3, 6, 8) == [(3, 6)]

    def test_wrap_when_range_crosses_slot_count_multiple(self):
        # 7..10（槽数 8）：槽 7，再回绕到槽 0..2
        assert slot_intervals(7, 10, 8) == [(7, 7), (0, 2)]

    def test_wrap_across_region_end(self):
        # 7..10（槽数 4）：槽 3，再回绕到槽 0..2
        assert slot_intervals(7, 10, 4) == [(3, 3), (0, 2)]

    def test_full_ring_single_interval_when_first_is_zero(self):
        assert slot_intervals(8, 15, 8) == [(0, 7)]

    def test_full_ring_wrapped(self):
        # 整环 8 条、起点槽 3：两段拼满全部槽
        assert slot_intervals(11, 18, 8) == [(3, 7), (0, 2)]

    def test_single_record(self):
        assert slot_intervals(5, 5, 8) == [(5, 5)]


class TestPlanHistoryReads:
    def test_merges_adjacent_slots_within_limit(self):
        # 首槽 202、8 槽、上限 32 字（5 槽/段）；补 1..8（槽 1..7、0）
        segs = plan_history_reads(
            slot_address=202, slot_count=8, max_read=32, start_seq=1, cutoff_seq=8
        )
        # 区间 [1..7] + [0..0]；[1..7] 拆为 1..5、6..7
        assert seg_tuples(segs) == [
            (208, 237, (1, 2, 3, 4, 5)),
            (238, 249, (6, 7)),
            (202, 207, (0,)),
        ]
        assert all(s.count % SLOT_WORDS == 0 for s in segs)
        assert all(s.count <= 32 for s in segs)

    def test_splits_at_region_end_when_wrapped(self):
        # 槽数 4、补 7..10：槽 3 与槽 0..2 地址不连续，必须分段
        segs = plan_history_reads(
            slot_address=202, slot_count=4, max_read=12, start_seq=7, cutoff_seq=10
        )
        assert seg_tuples(segs) == [
            (220, 225, (3,)),
            (202, 213, (0, 1)),
            (214, 219, (2,)),
        ]

    def test_modbus_protocol_cap_125(self):
        # 设备上限 200 字，但协议上限 125 -> 每段最多 20 槽（120 字）
        segs = plan_history_reads(
            slot_address=0, slot_count=64, max_read=200, start_seq=1, cutoff_seq=63
        )
        assert [len(s.slots) for s in segs] == [20, 20, 20, 3]
        assert all(s.count <= 125 for s in segs)

    def test_max_read_below_one_slot_rejected(self):
        with pytest.raises(BackfillPlanError, match="不足一个整槽"):
            plan_history_reads(
                slot_address=202, slot_count=8, max_read=5, start_seq=1, cutoff_seq=8
            )

    def test_nothing_to_read_returns_empty(self):
        assert (
            plan_history_reads(
                slot_address=202, slot_count=8, max_read=32, start_seq=9, cutoff_seq=8
            )
            == []
        )

    def test_segments_cover_exactly_the_surviving_slots(self):
        # 槽数 8、最新 25、上次归档 0 -> 补 18..25，槽 2..7 与 0..1
        segs = plan_history_reads(
            slot_address=202, slot_count=8, max_read=24, start_seq=18, cutoff_seq=25
        )
        slots = [slot for s in segs for slot in s.slots]
        assert slots == [2, 3, 4, 5, 6, 7, 0, 1]
        # 上限 24 字 -> 每段 4 槽
        assert seg_tuples(segs) == [
            (214, 237, (2, 3, 4, 5)),
            (238, 249, (6, 7)),
            (202, 213, (0, 1)),
        ]


class TestSlotCodec:
    def test_decode_u32_high_word_first(self):
        assert decode_u32(0x0001, 0x86A0) == 100000
        assert decode_u32(0xFFFF, 0xFFFF) == 4294967295
        assert decode_u32(0, 0) == 0

    def test_parse_slot(self):
        words = [0x0000, 0x0019, 0x6553, 0xF100, 0x0007, 0xA120]
        seq, sampled_at, count = parse_slot(words)
        assert seq == 25
        assert sampled_at == 0x6553F100
        assert count == 500_000

    def test_parse_slot_rejects_wrong_length(self):
        with pytest.raises(BackfillPlanError):
            parse_slot([0, 1, 2])
