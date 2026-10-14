"""不足枠の修正案(検討材料)を作る。

このモジュールは配置を自動で決定・実行しません。監査(engine.audit)で見つかった
不足枠(COVERAGE_SHORT)について、
    ・休憩のタイミングを30分ずらす
    ・入浴介助のタイミングを30分ずらす
といった、ごく限られた種類の変更だけを試し、変更後のデータを同じ監査エンジンで
再実行して「その不足が消え、かつ新しい重大NGが発生しない」ことを確認できたものだけを、
1つの不足枠につき最大2件、候補として返します。

唯一の正解として配置を決めるものではなく、現場で検討するための一例です。
「どの職員を動かすべきか」「この配置は誤りだ」という判断は行いません。
最終判断・実際の配置変更は現場で行ってください。
"""
from datetime import timedelta

from .engine import audit, stamp

# 休憩の方が動かしやすいと考えられるため先に試す。入浴介助は後。
CANDIDATE_KINDS = (("BREAK", "breaks"), ("BATH", "bath"))
# 後ろ倒し、前倒しの順で試す。±30分のみ(V1はこの範囲に限定)。
TRY_DELTAS = (30, -30)
KIND_LABEL = {"BREAK": "休憩", "BATH": "入浴介助"}


def _red_signature(issues):
    """RED(重大NG)issueの比較用シグネチャ集合。メッセージ中の人数等の表記ゆれを無視し、
    「どの検出コード・職員・日時・フロアで発生したか」だけを見る。"""
    return {(i["code"], i["staff_id"], i["when"], i["floor"]) for i in issues if i["severity"] == "RED"}


def _shift_one_activity(data, sid, shift_date_str, kind, point, delta_minutes):
    """dataを複製し、指定した職員・日付・種別のうち point を含む活動の開始・終了を
    delta_minutes分だけ動かす。対象の活動が見つからなければ None を返す。

    同じ職員・日付・種別の活動が複数ある場合(例: 休憩が2回に分かれている)は、
    point を含むものだけを動かし、他はそのまま残す。
    """
    new_activities = []
    changed = False
    for row in data["activities"]:
        if (not changed and row.get("staff_id") == sid and row.get("shift_date") == shift_date_str
                and row.get("kind") == kind):
            try:
                begin, finish = stamp(row["start"]), stamp(row["end"])
            except (ValueError, KeyError):
                new_activities.append(row)
                continue
            if begin <= point < finish:
                new_row = dict(row)
                new_row["start"] = (begin + timedelta(minutes=delta_minutes)).isoformat(timespec="minutes")
                new_row["end"] = (finish + timedelta(minutes=delta_minutes)).isoformat(timespec="minutes")
                new_activities.append(new_row)
                changed = True
                continue
        new_activities.append(row)
    if not changed:
        return None
    new_data = dict(data)
    new_data["activities"] = new_activities
    return new_data


def find_candidates_for_deficit(data, coverage_row, base_red, target_sig, max_candidates=2):
    """1件の不足枠について、検証済みの候補を最大max_candidates件探す。

    coverage_row: engine.audit()の結果のcoverageのうち、この不足枠(日時・フロア)に対応する行
                  (detailに休憩中・入浴介助中の職員IDが入っている)。
    base_red: 変更前のRED issueシグネチャ集合(_red_signature)。
    target_sig: 解消したい不足そのもののシグネチャ((code, staff_id, when, floor))。
    """
    point = stamp(coverage_row["time"])
    shift_date_str = coverage_row["time"][:10]
    detail = coverage_row.get("detail", {})

    candidates = []
    tried_people = set()
    for kind, detail_key in CANDIDATE_KINDS:
        for sid in detail.get(detail_key, []):
            if sid in tried_people or len(candidates) >= max_candidates:
                continue
            tried_people.add(sid)
            for delta in TRY_DELTAS:
                new_data = _shift_one_activity(data, sid, shift_date_str, kind, point, delta)
                if new_data is None:
                    continue
                try:
                    new_result = audit(new_data)
                except (ValueError, KeyError, TypeError):
                    continue
                new_red = _red_signature(new_result["issues"])
                if target_sig in new_red:
                    continue  # まだこの不足が残っている
                if new_red - base_red:
                    continue  # 別の場所に新しい重大NGが発生する
                direction = "後ろ" if delta > 0 else "前"
                candidates.append(dict(
                    staff_id=sid, kind=kind, delta_minutes=delta,
                    description=f"{sid}さんの{KIND_LABEL[kind]}を30分{direction}にずらす",
                ))
                break
            if len(candidates) >= max_candidates:
                break
    return candidates


def suggest_fixes(data, max_per_deficit=2, max_deficits=30):
    """監査結果のCOVERAGE_SHORT(不足枠)ごとに、検証済みの修正候補(検討材料)を集める。

    候補探索は不足枠1件につき監査エンジンを複数回(最大4回程度)再実行して検証するため、
    不足枠が多い入力では時間がかかる。max_deficits件を超える不足枠がある場合、
    超過分は候補探索を行わず(件数の多さそのものが、休憩等の微調整では解消しない
    構造的な人員不足を示しているため)、truncated=Trueとして返す。

    戻り値: dict(items, total_deficits, truncated)
        items: list of dict(when, floor, message, candidates)。
               candidatesが空のリストの不足枠は、このV1の候補探索(休憩・入浴介助の
               ±30分シフトのみ)では解消策が見つからなかったことを意味する。
               その場合も「解決不可能」とは判定せず、現場での検討材料として
               候補なしのまま一覧に残す。
        total_deficits: 検出された不足枠(COVERAGE_SHORT)の総数。
        truncated: max_deficitsを超えたため、候補探索を打ち切ったかどうか。
    """
    base_result = audit(data)
    coverage_by_point_floor = {(row["time"], row["floor"]): row for row in base_result["coverage"]}
    base_red = _red_signature(base_result["issues"])
    deficits = [i for i in base_result["issues"] if i["code"] == "COVERAGE_SHORT"]

    items = []
    for issue in deficits[:max_deficits]:
        point_iso = stamp(issue["when"]).isoformat(timespec="minutes")
        coverage_row = coverage_by_point_floor.get((point_iso, issue["floor"]))
        target_sig = ("COVERAGE_SHORT", issue["staff_id"], issue["when"], issue["floor"])
        candidates = (find_candidates_for_deficit(data, coverage_row, base_red, target_sig, max_per_deficit)
                      if coverage_row else [])
        items.append(dict(when=issue["when"], floor=issue["floor"], message=issue["message"],
                           candidates=candidates))
    return dict(items=items, total_deficits=len(deficits), truncated=len(deficits) > max_deficits)
