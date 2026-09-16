"""Test cho các hàm/hằng số liên quan đến upload file lớn trong sharepoint.py."""

import pytest

from .sharepoint import SIMPLE_UPLOAD_LIMIT, _CHUNK_SIZE, _plan_chunks


def test_plan_chunks_covers_the_whole_file_exactly_once():
    cases = [
        # (size, chunk, want_parts)
        (10, 4, 3),  # 0-3, 4-7, 8-9
        (8, 4, 2),  # bội số chẵn
        (3, 4, 1),  # nhỏ hơn một chunk
        (1, 1, 1),
    ]
    for size, chunk, want_parts in cases:
        parts = _plan_chunks(size, chunk)
        assert len(parts) == want_parts, (
            f"_plan_chunks({size},{chunk}) tạo ra {len(parts)} phần, "
            f"muốn {want_parts}"
        )

        # Các range phải liên tục, bao gồm cả hai đầu, và phủ đúng
        # [0, size-1].
        covered = 0
        for i, p in enumerate(parts):
            assert p.end >= p.start, (
                f"phần {i} có end < start ({p.end} < {p.start})"
            )
            if i == 0:
                assert p.start == 0, f"phần đầu tiên bắt đầu tại {p.start}, muốn 0"
            if i > 0:
                assert p.start == parts[i - 1].end + 1, (
                    f"có khoảng trống hoặc chồng lấp giữa phần {i - 1} và {i}"
                )
            covered += p.end - p.start + 1

        assert covered == size, f"các phần phủ {covered} byte, muốn {size}"

        last = parts[-1]
        assert last.end == size - 1, f"phần cuối kết thúc tại {last.end}, muốn {size - 1}"


def test_plan_chunks_edge_cases():
    assert _plan_chunks(0, 100) == [], "file rỗng phải không tạo ra chunk nào"
    assert _plan_chunks(-5, 100) == [], "size âm phải không tạo ra chunk nào"

    # chunk size không dương phải rơi về giá trị mặc định thay vì lặp vô hạn.
    got = _plan_chunks(10, 0)
    assert len(got) == 1, (
        f"chunk size bằng 0 phải rơi về mặc định, nhưng tạo ra {len(got)} phần"
    )


def test_chunk_size_matches_graph_requirement():
    # Microsoft Graph yêu cầu mọi chunk (trừ chunk cuối) phải là bội số của
    # 320 KiB; vi phạm điều này khiến upload session thất bại giữa chừng.
    unit = 320 * 1024
    assert _CHUNK_SIZE % unit == 0, f"_CHUNK_SIZE {_CHUNK_SIZE} không phải bội số của 320 KiB"
    assert _CHUNK_SIZE <= SIMPLE_UPLOAD_LIMIT * 2, (
        f"_CHUNK_SIZE {_CHUNK_SIZE} lớn bất thường"
    )


def test_simple_upload_limit_is_4mb():
    assert SIMPLE_UPLOAD_LIMIT == 4 * 1024 * 1024, (
        f"SIMPLE_UPLOAD_LIMIT = {SIMPLE_UPLOAD_LIMIT}, muốn 4 MiB "
        "(giới hạn PUT đơn giản của Graph)"
    )