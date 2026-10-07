"""入力やExcelから独立した監査。時刻は日本の施設の現地時刻、終了は含まない。"""
from collections import Counter, defaultdict
from datetime import date, datetime, timedelta
import calendar
import hashlib
import json

STEP = timedelta(minutes=30)

# 以下は既定値。config.jsonでsupervisor_role等を指定すれば、施設ごとの呼び方・コードに変更できる。
DEFAULT_SUPERVISOR_ROLE = "統括"
DEFAULT_SUPERVISOR_CODE = "統"
DEFAULT_OFF_CODE = "休"
DEFAULT_POST_NIGHT_CODE = "明"
DEFAULT_PAID_LEAVE_CODE = "有"
DEFAULT_EARLY_CODE = "早"
DEFAULT_LATE_CODE = "遅"
DEFAULT_WEEK_START_WEEKDAY = 0  # 0=月曜(Python標準のweekday()に合わせる)


# 時間の基本処理。30分以外の端数を切り捨てて安全扱いしない。
def stamp(value):
    result = datetime.fromisoformat(str(value))
    if result.tzinfo or result.second or result.microsecond or result.minute % 30:
        raise ValueError("日時はタイムゾーンなし・30分境界で入力してください")
    return result


def ticks(start, end):
    while start < end:
        yield start
        start += STEP


def dates(start, end):
    while start <= end:
        yield start
        start += timedelta(days=1)


def fingerprint(data):
    return hashlib.sha256(json.dumps(data, ensure_ascii=False, sort_keys=True).encode()).hexdigest()


def legal_break_minutes(work_minutes):
    """労基法34条の境界。施設の60分・180分とは別。"""
    return 60 if work_minutes > 480 else 45 if work_minutes > 360 else 0


def longest_run(points):
    best = run = 0
    previous = None
    for point in sorted(points):
        run = run + 30 if previous is not None and point - previous == STEP else 30
        best = max(best, run)
        previous = point
    return best


# 法定時間外・休日労働の「確認済み集計値」を検査。シフト時間の単純差を使わない。
def overtime_findings(row, special=False):
    issues = []
    ot, holiday = float(row["overtime_hours"]), float(row["holiday_hours"])
    annual = float(row["annual_overtime_hours"])
    if min(ot, holiday, annual) < 0 or annual < ot:
        return [("RED", "LEDGER_INVALID", "残業集計値の大小・符号が矛盾")]
    if ot + holiday >= 100:
        issues.append(("RED", "OT_100", "時間外＋休日労働が月100時間以上"))
    if annual > (720 if special else 360):
        issues.append(("RED", "OT_YEAR", "年間時間外労働が上限超過"))
    if not special and ot > 45:
        issues.append(("RED", "OT_MONTH", "特別条項なしで月45時間超過"))
    if special and int(row.get("months_over_45", 0)) > 6:
        issues.append(("RED", "OT_SIX", "月45時間超過が年6か月を超える"))
    # 当月を含む連続した直近月の時間外＋休日労働を古い順に渡す。
    history = row.get("combined_history", [])
    if isinstance(history, str):
        history = json.loads(history or "[]")
    if len(history) != 6:
        issues.append(("YELLOW", "OT_HISTORY_MISSING", "当月を含む6か月の実績不足"))
    else:
        history = [float(x) for x in history]
        if min(history) < 0 or abs(history[-1] - ot - holiday) > 1e-8:
            issues.append(("RED", "LEDGER_INVALID", "直近月履歴と当月集計が不一致"))
        for n in range(2, 7):
            if sum(history[-n:]) / n > 80:
                issues.append(("RED", "OT_AVERAGE", f"{n}か月平均が80時間超過"))
    if not issues and ot >= 40:
        issues.append(("YELLOW", "OT_APPROACH", "月45時間に接近。協定の限度時間も確認"))
    return issues


def leave_findings(row, as_of):
    """通常の単一基準日方式。前倒し・比例期間などは別途確認。予定取得は含めない。"""
    if row.get("scheme", "standard") != "standard":
        return [("YELLOW", "LEAVE_SCHEME", "前倒し等の年休付与方式は個別確認")]
    grant = date.fromisoformat(row["grant_date"])
    try:
        deadline = grant.replace(year=grant.year + 1) - timedelta(days=1)
    except ValueError:  # 2月29日を基準日とする1年間は翌年2月28日まで。
        deadline = date(grant.year + 1, 2, 28)
    if float(row["granted_days"]) < 10:
        return []
    # 時間単位年休は5日義務の算入対象外。半日は0.5日。
    taken = float(row["taken_full_days"]) + float(row["taken_half_days"]) * 0.5
    if taken >= 5:
        return []
    if as_of >= deadline:
        return [("RED", "LEAVE_FIVE", f"年休5日取得の期限到来・不足 {5-taken:g}日")]
    return [("YELLOW", "LEAVE_PENDING", f"取得済み {taken:g}日。期限 {deadline} までの取得を確認")]


# 配布判定の優先順位。未確認を緑に読み替えない。
def distribution_status(issues):
    levels = {x["severity"] for x in issues}
    return "RED" if "RED" in levels else "YELLOW" if "YELLOW" in levels else "GREEN"


def is_whole_number(value, minimum=0, maximum=None):
    """整数か(True/Falseは整数として扱わない)。minimum以上、maximumがあればその以下。"""
    if isinstance(value, bool) or not isinstance(value, int):
        return False
    return value >= minimum and (maximum is None or value <= maximum)


def is_plain_number(value, minimum=0):
    """数値(整数または小数)か。True/False・非数・無限大は不可。minimum以上。"""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return False
    return value == value and abs(value) != float("inf") and value >= minimum


def check_facility_settings(config, has_leave_rows):
    """施設設定の型・範囲を確認する。不正なら、配布不可として扱えるよう理由つきのValueErrorにする。"""
    for key in ["day_min", "night_min", "early_min", "late_min", "free_supervisor_min"]:
        if not is_whole_number(config[key]):
            raise ValueError(f"人数設定 {key} が不正(0以上の整数で指定してください)")
    # 目標人数(early_target・late_target・day_target・night_target)は任意。指定するなら0以上の整数で、最低人数以上にする。
    for target_key, minimum_key in (("early_target", "early_min"), ("late_target", "late_min"),
                                    ("day_target", "day_min"), ("night_target", "night_min")):
        if target_key in config:
            if not is_whole_number(config[target_key]):
                raise ValueError(f"人数設定 {target_key} が不正(0以上の整数で指定してください)")
            if config[target_key] < config[minimum_key]:
                raise ValueError(f"人数設定 {target_key} が不正(最低人数 {minimum_key} 以上にしてください)")
    for key in ["night_continuous_minutes", "night_supervisors", "annual_holidays"]:
        if key not in config or not is_whole_number(config[key]):
            raise ValueError(f"施設設定 {key} が不正(0以上の整数で指定してください)")
    if "interval_hours" not in config or not is_plain_number(config["interval_hours"]):
        raise ValueError("施設設定 interval_hours が不正(0以上の数値で指定してください)")
    if "week_start_weekday" in config and not is_whole_number(config["week_start_weekday"], 0, 6):
        raise ValueError("施設設定 week_start_weekday が不正(0=月曜〜6=日曜の整数で指定してください)")
    if "special_clause" in config and not isinstance(config["special_clause"], bool):
        raise ValueError("施設設定 special_clause が不正(true か false で指定してください)")
    if "as_of" in config or has_leave_rows:
        try:
            date.fromisoformat(str(config["as_of"]))
        except (KeyError, ValueError):
            raise ValueError("施設設定 as_of が不正(YYYY-MM-DD の日付で指定してください)")


def audit(data):
    config = data["config"]
    issues, coverage, workload = [], [], []
    # 施設ごとに呼び方・コードを変えられるようにする(未指定なら既定値のまま動く)。
    SUPERVISOR = config.get("supervisor_role", DEFAULT_SUPERVISOR_ROLE)
    SUPERVISOR_CODE = config.get("supervisor_shift_code", DEFAULT_SUPERVISOR_CODE)
    OFF_CODE = config.get("off_code", DEFAULT_OFF_CODE)
    POST_NIGHT_CODE = config.get("post_night_code", DEFAULT_POST_NIGHT_CODE)
    PAID_LEAVE_CODE = config.get("paid_leave_code", DEFAULT_PAID_LEAVE_CODE)
    EARLY_CODE = config.get("early_shift_code", DEFAULT_EARLY_CODE)
    LATE_CODE = config.get("late_shift_code", DEFAULT_LATE_CODE)
    OFF_CODES = {OFF_CODE, PAID_LEAVE_CODE, POST_NIGHT_CODE}
    WEEK_START = DEFAULT_WEEK_START_WEEKDAY  # 範囲の確認は、下の入力検査で行う

    def week_of(day):
        return day - timedelta(days=(day.weekday() - WEEK_START) % 7)

    def flag(code, message, staff_id="", when="", floor="", severity="RED", category="施設ルール"):
        issues.append(dict(severity=severity, code=code, category=category,
                           staff_id=staff_id, when=str(when), floor=floor, message=message))

    def result():
        # issues・workloadは集合の走査順に依存していたため、並べ替えて再現性を確保する(件数・判定は変えない)。
        return dict(status=distribution_status(issues),
                    issues=sorted(issues, key=lambda x: (x["staff_id"], x["when"], x["floor"], x["code"])),
                    coverage=coverage,
                    workload=sorted(workload, key=lambda w: (w["staff_id"], w["period"])),
                    input_sha256=fingerprint(data),
                    counts=dict(Counter(x["severity"] for x in issues)))

    # 必須マスタ・入力の検査。入力が壊れた場合も配布不可として返す。
    try:
        start, end = stamp(config["start"]), stamp(config["end"])
        if end <= start or (end-start).days > 62:
            raise ValueError("監査期間は正の長さで最大62日")
        floors = config["floors"]
        if not floors or len(set(floors)) != len(floors) or SUPERVISOR in floors:
            raise ValueError("フロア名が空または重複")
        shifts = config["shifts"]
        for code, spec in shifts.items():
            a = stamp("2000-01-01T" + spec["start"])
            b = stamp("2000-01-01T" + spec["end"]) + timedelta(days=int(spec["next_day"]))
            if b <= a or b-a > timedelta(hours=24) or float(spec["break_minutes"]) < 0:
                raise ValueError(f"勤務設定 {code} の時間が不正")
        check_facility_settings(config, bool(data.get("leave_ledger")))
        # 早番・遅番の勤務コード(既定は「早」「遅」)。人数の下限が1以上なら、勤務区分にあるコードでなければならない。
        if EARLY_CODE == LATE_CODE:
            raise ValueError("施設設定 early_shift_code と late_shift_code が同じ勤務コードです")
        for key, label, code_value, minimum_key in (
            ("early_shift_code", "早番", EARLY_CODE, "early_min"),
            ("late_shift_code", "遅番", LATE_CODE, "late_min"),
        ):
            if not isinstance(code_value, str):
                raise ValueError(f"施設設定 {key} が不正(勤務コードは文字で指定してください)")
            if config[minimum_key] > 0 and code_value not in shifts:
                raise ValueError(
                    f"施設設定 {key} が不正({label}の勤務コード「{code_value}」が勤務区分にありません。"
                    f"コードを変えた場合は {key} で指定してください)"
                )
        WEEK_START = config.get("week_start_weekday", DEFAULT_WEEK_START_WEEKDAY)
        day_start = stamp("2000-01-01T"+config["day_start"]).time()
        day_end = stamp("2000-01-01T"+config["day_end"]).time()
        if day_start >= day_end:
            raise ValueError("日中時間帯は同日内で指定")
        staff = {}
        for row in data["staff"]:
            sid = row["staff_id"]
            if not sid or sid in staff or row["floor"] not in floors:
                raise ValueError("職員ID重複・空欄・所属不正")
            if row["overtime"] not in ["可", "不可", "要相談"]:
                raise ValueError("残業可否の値が不正")
            if row["night_allowed"] not in ["可", "不可"] or row["supervisor"] not in ["可", "不可"]:
                raise ValueError("勤務可否の値が不正")
            if float(row["weekly_hours"]) < 0:
                raise ValueError("契約時間が不正")
            staff[sid] = row
        if not staff:
            raise ValueError("職員が未入力")
    except (KeyError, ValueError, TypeError) as exc:
        flag("INPUT_INVALID", str(exc), category="入力整合")
        return result()

    assignments, occupied, schedule = [], defaultdict(list), {}
    for row in data["schedule"]:
        try:
            sid, day, code = row["staff_id"], date.fromisoformat(row["date"]), row["code"]
            if sid not in staff or code not in set(shifts) | OFF_CODES:
                raise ValueError("未知の職員・勤務コード")
            key = (sid, day)
            if key in schedule:
                flag("DUPLICATE_SHIFT", "同一職員・日付に複数の入力", sid, day, category="入力整合")
                continue
            schedule[key] = code
            if code in OFF_CODES:
                continue
            person, spec = staff[sid], shifts[code]
            begin = stamp(f"{day}T{spec['start']}")
            finish = stamp(f"{day}T{spec['end']}") + timedelta(days=int(spec["next_day"]))
            place = row.get("floor") or (SUPERVISOR if code == SUPERVISOR_CODE else person["floor"])
            if place not in floors + [SUPERVISOR]:
                raise ValueError("配置先が不正")
            a = dict(staff_id=sid, date=day, code=code, start=begin, end=finish,
                     floor=place, breaks=set(), activities={}, points=set(ticks(begin, finish)))
            assignments.append(a)
            for point in a["points"]:
                occupied[sid, point].append(a)
            if code not in person["allowed_codes"].split("・"):
                flag("CODE_RESTRICTION", f"勤務可能コード外: {code}", sid, day)
            if spec.get("night") and person["night_allowed"] != "可":
                flag("NIGHT_FORBIDDEN", "長時間夜勤不可者への夜勤", sid, day)
            if place == SUPERVISOR and person["supervisor"] != "可":
                flag("SUPERVISOR_FORBIDDEN", "統括対応不可", sid, day)
            allowed_days = person.get("weekdays", "0・1・2・3・4・5・6").split("・")
            for part_day in {t.date() for t in a["points"]}:
                if str(part_day.weekday()) not in allowed_days:
                    flag("WEEKDAY_RESTRICTION", "曜日制限に抵触（実際の勤務日）", sid, part_day)
            low, high = person.get("available_start", ""), person.get("available_end", "")
            if low and high:
                lo, hi = datetime.strptime(low, "%H:%M").time(), datetime.strptime(high, "%H:%M").time()
                if any(not (lo <= t.time() < hi if lo < hi else t.time() >= lo or t.time() < hi) for t in a["points"]):
                    flag("TIME_RESTRICTION", "勤務可能時間の外に割当", sid, day)
        except (ValueError, KeyError, TypeError) as exc:
            flag("INPUT_INVALID", str(exc), row.get("staff_id", ""), row.get("date", ""), category="入力整合")

    indexed = {(a["staff_id"], a["date"]): a for a in assignments}
    for row in data["activities"]:
        try:
            sid, day = row["staff_id"], date.fromisoformat(row["shift_date"])
            a = indexed.get((sid, day))
            begin, finish = stamp(row["start"]), stamp(row["end"])
            kind = row["kind"]
            if kind not in {"BREAK", "BATH", "OTHER", "TRANSFER", "BREAK_INTERRUPTED"}:
                raise ValueError("未知の離脱コード")
            if a is None or begin < a["start"] or finish > a["end"] or finish <= begin:
                flag("ACTIVITY_OUTSIDE_SHIFT", "非勤務・短時間勤務の時間外に担当あり", sid, begin, category="入力整合")
                continue
            if kind == "TRANSFER" and row.get("target") not in floors + [SUPERVISOR]:
                raise ValueError("応援先が不正")
            if kind == "TRANSFER" and row.get("target") == SUPERVISOR and staff[sid]["supervisor"] != "可":
                flag("SUPERVISOR_FORBIDDEN", "統括対応不可の職員を統括に配置", sid, begin)
            if kind == "TRANSFER" and row.get("target") != a["floor"] and staff[sid].get("transfer_allowed", "可") != "可":
                flag("TRANSFER_FORBIDDEN", "他フロア応援不可", sid, begin)
            if kind == "BREAK" and (begin == a["start"] or finish == a["end"]):
                flag("BREAK_AT_EDGE", "休憩は勤務の途中に確保", sid, begin, category="法令")
                continue
            for point in ticks(begin, finish):
                a["activities"].setdefault(point, []).append((kind, row.get("target", "")))
            if kind == "BREAK_INTERRUPTED":
                flag("BREAK_INTERRUPTED", "休憩中断。中断分は労働時間。代替休憩と経緯を確認", sid, begin,
                     severity="YELLOW", category="法令・運用確認")
        except (ValueError, KeyError, TypeError) as exc:
            flag("INPUT_INVALID", str(exc), row.get("staff_id", ""), category="入力整合")

    # 人を単位に重複を排除。活動の重複部分は休憩にも配置にも認定しない。
    work_points = defaultdict(set)
    for a in assignments:
        sid = a["staff_id"]
        for point in a["points"]:
            activity = a["activities"].get(point, [])
            if len(occupied[sid, point]) > 1:
                flag("SHIFT_OVERLAP", "勤務時間が重複。実配置から除外", sid, point, category="入力整合")
            if len(activity) > 1:
                flag("ACTIVITY_OVERLAP", "休憩・担当が重複。実配置と休憩から除外", sid, point, category="入力整合")
            if len(activity) == 1 and activity[0][0] == "BREAK" and len(occupied[sid, point]) == 1:
                a["breaks"].add(point)
            else:
                work_points[sid].add(point)
        total_break = len(a["breaks"]) * 30
        work_minutes = len(a["points"] - a["breaks"]) * 30
        if total_break < legal_break_minutes(work_minutes):
            flag("LEGAL_BREAK_SHORT", f"法定休憩不足: {total_break}分", sid, a["date"], category="法令")
        spec = shifts[a["code"]]
        if total_break < float(spec["break_minutes"]):
            flag("FACILITY_BREAK_SHORT", f"施設休憩不足: {total_break}/{spec['break_minutes']}分", sid, a["date"])
        if spec.get("night") and longest_run(a["breaks"]) < config["night_continuous_minutes"]:
            flag("NIGHT_BREAK_CONTINUITY", "夜勤の連続休憩が不足", sid, a["date"])

    # 日付と希望・明け表示。前夜の勤務も、希望休当日の勤務として判定。
    for row in data["requests"]:
        try:
            sid, day = row["staff_id"], date.fromisoformat(row["date"])
            if sid not in staff or row["code"] not in {OFF_CODE, PAID_LEAVE_CODE}:
                raise ValueError("希望休の職員・種別が不正")
            if schedule.get((sid, day)) != row["code"] or any(t.date() == day for t in work_points[sid]):
                flag("REQUEST_CONFLICT", f"{row['code']}希望日に割当または未入力", sid, day)
        except (ValueError, KeyError) as exc:
            flag("INPUT_INVALID", str(exc), category="入力整合")

    for sid, person in staff.items():
        own = sorted([a for a in assignments if a["staff_id"] == sid], key=lambda a:a["start"])
        for prior, following in zip(own, own[1:]):
            gap = (following["start"] - prior["end"]).total_seconds() / 3600
            if 0 <= gap < config["interval_hours"]:
                flag("INTERVAL_SHORT", f"勤務間隔 {gap:g}時間 < 施設基準{config['interval_hours']}時間", sid, following["start"])
        for day in dates(start.date(), (end-STEP).date()):
            code = schedule.get((sid, day))
            if code is None:
                flag("SCHEDULE_MISSING", "勤務・休・明の記録が未入力", sid, day, severity="YELLOW", category="入力不足")
                continue
            previous_code = schedule.get((sid, day-timedelta(days=1)))
            was_night = previous_code in shifts and shifts[previous_code].get("next_day")
            if was_night and code != POST_NIGHT_CODE:
                flag("POST_NIGHT_CONFLICT", "前日夜勤の終了日は明とする施設ルール", sid, day)
            if code == POST_NIGHT_CODE and not was_night:
                flag("ORPHAN_POST_NIGHT", "明に対応する前日夜勤がない", sid, day,
                     severity="YELLOW" if previous_code is None else "RED", category="入力整合")
            if code in {OFF_CODE, PAID_LEAVE_CODE} and any(t.date() == day for t in work_points[sid]):
                flag("OFF_DAY_WORK", "公休・有休の日に実勤務時間がある", sid, day)
        # 暦週を分割集計。週の開始曜日はconfigのweek_start_weekday(既定は月曜)。
        weekly = defaultdict(int)
        monthly = defaultdict(int)
        for point in work_points[sid]:
            week = week_of(point.date())
            weekly[week] += 30
            monthly[point.strftime("%Y-%m")] += 30
        for week, minutes in weekly.items():
            hours = minutes / 60
            workload.append(dict(staff_id=sid, period=str(week), work_hours=hours,
                                 contract_hours=float(person["weekly_hours"])))
            if hours > float(person["weekly_hours"]):
                flag("CONTRACT_HOURS", f"暦週{week}の既知実働{hours:g}hが契約{person['weekly_hours']}h超過。法定残業とは別", sid, week,
                     severity="YELLOW", category="契約確認")
            full_week = list(dates(week,week+timedelta(days=6)))
            if all((sid,d) in schedule for d in full_week):
                rests = [d for d in full_week if schedule[sid,d] == OFF_CODE and not any(t.date()==d for t in work_points[sid])]
                if not rests:
                    flag("WEEKLY_HOLIDAY", "週1日の公休なし（週休方式の仮想施設。4週4休・休日労働の例外は未採用）", sid, week)
        for month, minutes in monthly.items():
            year, num = map(int, month.split("-"))
            cap = 40 * calendar.monthrange(year,num)[1] / 7
            if minutes / 60 > cap:
                flag("MONTH_PLAN_FRAME", f"既知の月実働{minutes/60:g}hが40×暦日数÷7={cap:.3f}hを超過。変形制の予定枠・時間外を確認", sid, month,
                     category="法令適用確認")
        for month in {a['date'].strftime('%Y-%m') for a in own}:
            nights = sum(bool(shifts[a['code']].get('night')) for a in own if a['date'].strftime('%Y-%m')==month)
            if nights > int(person.get('night_month_limit', 5)):
                flag("NIGHT_MONTH_LIMIT", "施設の月間夜勤上限超過", sid, month)

    # 勤務コードによる最低人数は、実配置とは独立して検査。
    for day in dates(start.date(), (end-STEP).date()):
        if datetime.combine(day, day_start) >= end:
            continue
        for floor in floors:
            for code, limit, target_key in [(EARLY_CODE,config["early_min"],"early_target"),(LATE_CODE,config["late_min"],"late_target")]:
                count = sum(a["date"]==day and a["code"]==code and a["floor"]==floor for a in assignments)
                if count < limit:
                    flag("EARLY_SHORT" if code==EARLY_CODE else "LATE_SHORT", f"{code}番 {count}/{limit}人", when=day, floor=floor)
                elif target_key in config and count < config[target_key]:
                    # 最低人数は満たすが、目標人数には届かない。最低人数の不足(重大NG)とは別の、要確認の警告。
                    flag("EARLY_BELOW_TARGET" if code==EARLY_CODE else "LATE_BELOW_TARGET",
                         f"{code}番 {count}人(最低{limit}人は満たしているが、目標{config[target_key]}人に未達)",
                         when=day, floor=floor, severity="YELLOW", category="目標人数")
        night_rows = [a for a in assignments if a["date"]==day and shifts[a["code"]].get("night")]
        if sum(a["floor"]==SUPERVISOR for a in night_rows) < config["night_supervisors"]:
            flag("NIGHT_SUPERVISOR_SHIFT", "夜勤統括の開始人数不足", when=day, floor=SUPERVISOR)
        for floor in floors:
            if sum(a["floor"]==floor for a in night_rows)<config["night_min"]:
                flag("NIGHT_SHIFT_SHORT", "夜勤のフロア担当開始人数不足", when=day, floor=floor)

    # 30分ごとに実際の担当を一意に決定。余剰統括を自動で複数フロアに配らない。
    occupied_by_time = defaultdict(list)
    for (sid, point), group in occupied.items():
        occupied_by_time[point].append((sid, group))
    below_target = {}   # (期間の開始日, フロア, 日中/夜間) -> (最小の実働人数, 最初の時刻)
    short_periods = set()   # 最低人数割れ(重大NG)が出ている期間。目標人数の警告は重ねて出さない
    for point in ticks(start, end):
        day_time = day_start <= point.time() < day_end
        buckets = {f:dict(planned=set(), breaks=set(), bath=set(), other=set(), out=set(), incoming=set(), active=set()) for f in floors+[SUPERVISOR]}
        for sid, group in occupied_by_time[point]:
            a = group[0]
            b = buckets[a["floor"]]
            b["planned"].add(sid)
            activity = a["activities"].get(point, [])
            if len(group)>1 or len(activity)>1:
                b["other"].add(sid)
            elif activity:
                kind, target = activity[0]
                if kind == "TRANSFER":
                    b["out"].add(sid)
                    buckets[target]["incoming"].add(sid)
                    buckets[target]["active"].add(sid)
                elif kind == "BREAK":
                    b["breaks"].add(sid)
                elif kind == "BATH":
                    b["bath"].add(sid)
                else:
                    b["other"].add(sid)
            else:
                b["active"].add(sid)
        for floor, b in buckets.items():
            minimum = (0 if day_time else config["free_supervisor_min"]) if floor==SUPERVISOR else (config["day_min"] if day_time else config["night_min"])
            deficit = max(0, minimum-len(b["active"]))
            row = dict(time=point.isoformat(timespec="minutes"), floor=floor,
                       **{k:len(v) for k,v in b.items()}, minimum=minimum, deficit=deficit,
                       judgment="不足" if deficit else "充足", staff_ids="・".join(sorted(b["active"])))
            coverage.append(row)
            if deficit:
                flag("COVERAGE_SHORT", f"実働{row['active']}人・最低{minimum}人・不足{deficit}人", when=point, floor=floor)
            if floor != SUPERVISOR:
                # 夜間は日付をまたぐため、開始日側(日中開始時刻より前は前日)でまとめる。
                period_day = point.date() if (day_time or point.time() >= day_end) else point.date() - timedelta(days=1)
                period = (period_day, floor, "day" if day_time else "night")
                if deficit:
                    short_periods.add(period)
                else:
                    target = config.get("day_target" if day_time else "night_target")
                    if target is not None and len(b["active"]) < target:
                        old = below_target.get(period)
                        if old is None or len(b["active"]) < old[0]:
                            below_target[period] = (len(b["active"]), point if old is None else old[1])

    # 日中・夜勤の目標人数。最低人数は満たすが目標に届かない期間を、1期間(日・フロア・日中/夜間)につき1件の要確認として出す。
    for (period_day, floor, kind), (lowest, first_point) in sorted(below_target.items()):
        if (period_day, floor, kind) in short_periods:
            continue
        key, label = ("day_target", "日中") if kind == "day" else ("night_target", "夜勤")
        flag("DAY_BELOW_TARGET" if kind == "day" else "NIGHT_BELOW_TARGET",
             f"{label}の実働が最少{lowest}人(最低人数は満たしているが、目標{config[key]}人に未達。最初の時刻 {first_point:%H:%M})",
             when=period_day, floor=floor, severity="YELLOW", category="目標人数")

    # 残業は候補審査のみ。配置人数・勤務時間へ加算しない。
    for row in data.get("overtime_candidates", []):
        sid = row["staff_id"]
        if sid not in staff:
            flag("INPUT_INVALID", "残業候補の職員IDが不明", sid, category="入力整合")
            continue
        if staff[sid]["overtime"]=="不可":
            flag("OT_FORBIDDEN", "残業不可の職員を候補指定", sid)
        elif staff[sid]["overtime"]=="要相談":
            flag("OT_CONSULT", "本人への相談が必要", sid, severity="YELLOW")
        else:
            flag("OT_CANDIDATE_ONLY", "残業は未確定候補。36協定・累計・次勤務を確認し勤務表へ別途反映", sid, severity="YELLOW")
    for row in data.get("overtime_ledger", []):
        try:
            for severity, code, message in overtime_findings(row, config.get("special_clause") is True):
                flag(code, message, row["staff_id"], row.get("month", ""), severity=severity, category="法令（集計入力）")
        except (ValueError, KeyError, TypeError):
            flag("INPUT_INVALID", "残業集計の形式不正", category="入力整合")
    for row in data.get("leave_ledger", []):
        try:
            for severity, code, message in leave_findings(row, date.fromisoformat(config["as_of"])):
                flag(code, message, row["staff_id"], severity=severity, category="法令（集計入力）")
        except (ValueError, KeyError, TypeError):
            flag("INPUT_INVALID", "年休集計の形式不正", category="入力整合")

    # このV1の自動判定範囲外。設定フラグ1つで消せない固定の要確認事項。
    for code, message in [
        ("MONTH_BOUNDARY_REVIEW", "前後月を含む完全な勤務・休暇履歴の監査が未完了"),
        ("YEAR_HOLIDAY_REVIEW", f"年間公休{config['annual_holidays']}日は施設目標。年間実績は未監査"),
        ("LEAVE_HISTORY_REVIEW", "全員の年休付与・残数・取得履歴の完全性を未確認"),
        ("WORK_SYSTEM_REVIEW", "変形労働時間制の手続・事前特定・日週月の時間外計算と36協定は個別審査が必要"),
        ("FACILITY_LEGAL_STAFFING", "施設種別・資格・利用者数に応じた法定配置基準は未実装"),
    ]:
        flag(code, message, severity="YELLOW", category="未確認・V1制約")
    return result()
