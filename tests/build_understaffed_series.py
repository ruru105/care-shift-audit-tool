"""実在の72人だけを使い、増員せずに手薄な状態をそのまま見せる6か月分(2026年10月〜2027年3月)の例を作る開発スクリプト。
自動シフト作成機能ではない。data/baseline等の既存データには一切触れない。

方針(2026-09-26 陽司さんとの合意事項):
- 職員は原本の72人だけを使う。人が足りない枠が出ても新しい職員を自動追加しない。
- 埋まらない枠はそのまま空けておく。ツールの重大NG・要確認の表示自体が、管理者への判断材料になる
  (増員するか休日出勤で埋めるか)という位置づけで、ツール側で提案文などは作らない。
- 年休は全員「付与10日・基準日2026-04-01」で決め打ちする(短時間勤務者も含めて簡略化。実際の労基法では
  契約時間に応じて減る場合があるが、今回は簡略化のためそれを採用しない)。
- 事前の年休計画表提出のような仕組みは作らない。単純に、まだ5日取得できていない人の残り日数を
  6か月に均等に振り分けるだけの簡易ロジックとする。
"""
from collections import defaultdict
from copy import deepcopy
from datetime import date, timedelta
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'src'))
from care_shift_audit.io import write_csv
from scenario_builder import generate

FIELDS = {
    'staff': ['staff_id', 'name', 'floor', 'night_allowed', 'supervisor', 'allowed_codes', 'weekly_hours',
              'night_month_limit', 'overtime', 'weekdays', 'available_start', 'available_end', 'transfer_allowed'],
    'schedule': ['staff_id', 'date', 'code', 'floor'],
    'activities': ['staff_id', 'shift_date', 'start', 'end', 'kind', 'target'],
    'requests': ['staff_id', 'date', 'code'],
    'overtime_candidates': ['staff_id', 'date', 'planned_hours'],
    'overtime_ledger': ['staff_id', 'month', 'overtime_hours', 'holiday_hours', 'annual_overtime_hours', 'months_over_45', 'combined_history'],
    'leave_ledger': ['staff_id', 'grant_date', 'granted_days', 'taken_full_days', 'taken_half_days', 'taken_hourly_hours', 'scheme'],
}

GRANT_DATE = date(2026, 4, 1)
GRANTED_DAYS = 10
MONTHS = [(2026, 10), (2026, 11), (2026, 12), (2027, 1), (2027, 2), (2027, 3)]


def month_range(y, m):
    start = date(y, m, 1)
    end = date(y + 1, 1, 1) if m == 12 else date(y, m + 1, 1)
    return start, end


def base_config():
    config = dict(start='', end='', as_of='', floors=['1F', '2F', '3F'], day_start='07:00', day_end='19:00',
                  day_min=4, night_min=2, early_min=4, late_min=4, early_target=5, late_target=5,
                  free_supervisor_min=1, night_supervisors=2, night_continuous_minutes=120, interval_hours=11,
                  annual_holidays=120, special_clause=False, work_system='monthly_variable_unconfirmed', shifts={})
    for code, begin, end_, next_day, pause, night in [
        ('早', '07:00', '16:00', 0, 60, False), ('日', '08:30', '17:30', 0, 60, False),
        ('遅', '10:00', '19:00', 0, 60, False), ('午', '07:00', '12:00', 0, 0, False),
        ('後', '14:00', '19:00', 0, 0, False), ('短A', '22:00', '01:00', 1, 0, False),
        ('短B', '01:00', '04:00', 0, 0, False), ('夜', '16:00', '09:00', 1, 180, True),
        ('統', '16:00', '09:00', 1, 180, True)]:
        config['shifts'][code] = dict(start=begin, end=end_, next_day=next_day, break_minutes=pause, night=night)
    return config


def load_staff(source):
    raw = json.loads(Path(source).read_text())
    staff = []
    for r in raw['職員マスター'][5:77]:
        allowed = r[13] + ('・統' if r[12] == '可' else '')
        staff.append(dict(staff_id=r[0], name='職員' + r[0], floor=r[1], night_allowed=r[5],
                           supervisor=r[12], allowed_codes=allowed, weekly_hours=r[14], night_month_limit=r[7],
                           overtime='要相談', weekdays='0・1・2・3・4・5・6', available_start='', available_end='',
                           transfer_allowed=r[6]))
    return staff


def save(data, name):
    folder = ROOT / 'data' / name
    folder.mkdir(parents=True, exist_ok=True)
    (folder / 'config.json').write_text(json.dumps(data['config'], ensure_ascii=False, indent=2), encoding='utf-8')
    for key, fields in FIELDS.items():
        write_csv(folder / f'{key}.csv', data[key], fields)


def build(source):
    config = base_config()
    staff = load_staff(source)
    people = {p['staff_id']: p for p in staff}

    # 有給：全員付与10日・基準日2026-04-01で固定。9月30日までの取得済み日数はID順に0〜5日で分散させる(架空の前提)。
    taken_before = {p['staff_id']: i % 6 for i, p in enumerate(staff)}
    need = {sid: max(0, 5 - t) for sid, t in taken_before.items()}

    window_start = date(2026, 9, 30)
    window_end = date(2027, 4, 1)
    all_days = [window_start + timedelta(days=i) for i in range((window_end - window_start).days + 1)]

    assigned = {}
    leave_dates = defaultdict(list)
    requests = []

    # まだ5日取得できていない人の残り日数を、6か月にできるだけ均等に振り分ける。
    # 事前の年休計画表提出のような仕組みは作らない、単純な均等割りに留める。
    for idx, p in enumerate(staff):
        sid = p['staff_id']
        n = need[sid]
        if n == 0:
            continue
        month_slots = [MONTHS[(k * 6) // n if n <= 6 else k % 6] for k in range(n)]
        for k, (y, m) in enumerate(month_slots):
            mstart, mend = month_range(y, m)
            days_in_month = (mend - mstart).days
            offset = 2 + ((idx * 7 + k * 11) % max(1, days_in_month - 4))
            d = mstart + timedelta(days=offset)
            while (sid, d) in assigned:
                d += timedelta(days=1)
                if d >= mend:
                    d = mend - timedelta(days=1)
                    break
            assigned[sid, d] = '有'
            leave_dates[sid].append(d)
            requests.append(dict(staff_id=sid, date=str(d), code='有'))

    # 手薄をそのまま見せるため、増員はしない(allow_autofill=False)。組み立てロジック自体は
    # data/baseline等と同じscenario_builder.generateを共有する。
    result = generate(staff, all_days, config, allow_autofill=False, preassigned=assigned)
    schedule = result['schedule']
    activities = result['activities']

    for (y, m) in MONTHS:
        mstart, mend = month_range(y, m)
        folder = f'understaffed_{y}_{m:02d}'
        slice_start = mstart - timedelta(days=1)
        slice_end = mend + timedelta(days=1)
        data = dict(config=deepcopy(config))
        data['config']['start'] = f'{mstart}T00:00'
        data['config']['end'] = f'{mend}T00:00'
        data['config']['as_of'] = str(min(mend - timedelta(days=1), date(2027, 3, 31)))
        data['staff'] = staff
        data['schedule'] = [r for r in schedule if slice_start <= date.fromisoformat(r['date']) < slice_end]
        data['activities'] = [a for a in activities if slice_start <= date.fromisoformat(a['shift_date']) < slice_end]
        data['requests'] = [r for r in requests if slice_start <= date.fromisoformat(r['date']) < slice_end]
        data['overtime_candidates'] = []
        data['overtime_ledger'] = []
        data['leave_ledger'] = []
        for sid in people:
            extra = sum(1 for d in leave_dates[sid] if d < mend)
            taken = taken_before[sid] + extra
            data['leave_ledger'].append(dict(staff_id=sid, grant_date=str(GRANT_DATE), granted_days=GRANTED_DAYS,
                                              taken_full_days=taken, taken_half_days=0, taken_hourly_hours=0, scheme='standard'))
        save(data, folder)
    return schedule


if __name__ == '__main__':
    build(sys.argv[1])
