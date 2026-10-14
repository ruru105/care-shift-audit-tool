"""suggest.py(修正案の検討材料)の確認。候補は必ず監査エンジンで再検証されたものだけが
返ること、正常基準では候補探索自体が発生しないこと、入力を書き換えないことを確認する。"""
from copy import deepcopy
from pathlib import Path

import pytest

from care_shift_audit.engine import audit
from care_shift_audit.io import load_folder
from care_shift_audit.suggest import suggest_fixes

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture(scope="module")
def baseline():
    return load_folder(ROOT / "data/baseline")


@pytest.fixture(scope="module")
def intentional_ng():
    return load_folder(ROOT / "data/intentional_ng")


def test_no_deficit_means_no_candidates_to_search(baseline):
    result = suggest_fixes(baseline)
    assert result["total_deficits"] == 0
    assert result["items"] == []
    assert result["truncated"] is False


def test_finds_the_two_known_deficits(intentional_ng):
    result = suggest_fixes(intentional_ng)
    assert result["total_deficits"] == 2
    assert len(result["items"]) == 2
    for item in result["items"]:
        assert item["when"].startswith("2026-10-06 09:")
        assert item["floor"] == "1F"


def test_every_candidate_is_independently_verified(intentional_ng):
    """提示された候補を実際に適用すると、本当にその不足が消え、新しい重大NGも
    発生しないことを、suggest.pyの内部ロジックを経由せず直接確認する。"""
    from care_shift_audit.suggest import _shift_one_activity, _red_signature
    from care_shift_audit.engine import stamp

    base_result = audit(intentional_ng)
    base_red = _red_signature(base_result["issues"])
    result = suggest_fixes(intentional_ng)

    found_any = False
    for item in result["items"]:
        for cand in item["candidates"]:
            found_any = True
            point = stamp(item["when"])
            shift_date_str = item["when"][:10]
            new_data = _shift_one_activity(
                intentional_ng, cand["staff_id"], shift_date_str, cand["kind"], point, cand["delta_minutes"])
            assert new_data is not None
            new_result = audit(new_data)
            new_red = _red_signature(new_result["issues"])
            target_sig = ("COVERAGE_SHORT", "", item["when"], item["floor"])
            assert target_sig not in new_red, "候補を適用しても不足が消えていない"
            assert not (new_red - base_red), "候補を適用すると新しい重大NGが発生する"
    assert found_any, "intentional_ngには検証済みの候補が少なくとも1件見つかるはず"


def test_does_not_mutate_the_input_data(intentional_ng):
    before = deepcopy(intentional_ng)
    suggest_fixes(intentional_ng)
    assert intentional_ng == before


def test_max_deficits_caps_the_search_and_reports_truncation():
    """不足枠が非常に多い入力では、全件ではなくmax_deficits件までに絞り、
    truncatedで「全件は探索していない」ことを明示する。"""
    data = load_folder(ROOT / "data/understaffed_2026_10")
    result = suggest_fixes(data, max_deficits=5)
    assert result["total_deficits"] > 5
    assert len(result["items"]) == 5
    assert result["truncated"] is True


def test_candidates_capped_at_max_per_deficit(intentional_ng):
    result = suggest_fixes(intentional_ng, max_per_deficit=1)
    for item in result["items"]:
        assert len(item["candidates"]) <= 1
