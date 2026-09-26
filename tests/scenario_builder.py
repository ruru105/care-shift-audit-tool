"""複数の例(data/baseline・data/one_month_example・data/understaffed_*)で共通して使う、
勤務・休憩・担当の組み立てロジック。自動シフト作成機能ではなく、テスト・デモ用データを
再現する開発補助。施設ルール(休憩・夜勤・統括・週休等)はここに1か所だけ書く。

allow_autofill=True  : 割り当てられる人がいない枠には、新しい架空職員を自動追加して埋める
                        (data/baseline・data/one_month_example の作り方)。
allow_autofill=False : 割り当てられる人がいない枠はそのまま空けておく。増員しない
                        (data/understaffed_* の作り方。手薄をそのまま見せる)。
"""
from collections import defaultdict
from datetime import datetime, timedelta


def generate(staff, all_days, config, allow_autofill=True, preassigned=None, preferred_source=None):
    """staff:職員リスト(このリストへ直接追加が起きる。呼び出し側はコピーを渡すこと)
    all_days:生成対象の日付リスト。preassigned:あらかじめ確定させておく{(staff_id,date):code}。
    preferred_source:元の週間シフト等、選ぶ際に優先したい候補を探すための勤務入力(任意)。
    戻り値:dict(schedule, activities, changes)"""
    assigned = dict(preassigned or {})
    schedule = []
    activities = []
    hours = defaultdict(float)
    workdays = defaultdict(set)
    next_id = defaultdict(lambda: 25)
    changes = []

    def choose(day, code, floor, preferred=None):
        spec = config['shifts'][code]
        wk = day - timedelta(days=day.weekday())
        begin = datetime.fromisoformat(f'{day}T{spec["start"]}')
        finish = datetime.fromisoformat(f'{day}T{spec["end"]}') + timedelta(days=spec['next_day'])
        charges = defaultdict(float)
        point = begin
        while point < finish:
            charges[point.date() - timedelta(days=point.weekday())] += .5
            point += timedelta(minutes=30)

        def eligible(p):
            sid = p['staff_id']
            home = p['floor']
            if code not in p['allowed_codes'].split('・') or (floor != '統括' and home != floor and p['transfer_allowed'] != '可'):
                return False
            if floor == '統括' and p['transfer_allowed'] != '可':
                return False
            if (sid, day) in assigned:
                return False
            if spec['next_day'] and (sid, day + timedelta(days=1)) in assigned:
                return False
            if any(hours[sid, w] + charge > float(p['weekly_hours']) for w, charge in charges.items()):
                return False
            occupied_days = workdays[sid, wk] | {day}
            if spec['night'] and (day + timedelta(days=1)).weekday() != 0:
                occupied_days.add(day + timedelta(days=1))
            if len(occupied_days) > 5:
                return False
            if spec['night'] and sum(s['staff_id'] == sid and config['shifts'][s['code']].get('night') for s in schedule) >= int(p['night_month_limit']):
                return False
            for s in schedule:
                if s['staff_id'] != sid:
                    continue
                other = config['shifts'][s['code']]
                a = datetime.fromisoformat(s['date'] + 'T' + other['start'])
                b = datetime.fromisoformat(s['date'] + 'T' + other['end']) + timedelta(days=other['next_day'])
                if not (begin >= b + timedelta(hours=11) or finish + timedelta(hours=11) <= a):
                    return False
            return True

        options = [p for p in staff if eligible(p)]
        options.sort(key=lambda p: (p['staff_id'] != preferred, p['floor'] != floor, hours[p['staff_id'], wk], p['staff_id']))
        if not options:
            if not allow_autofill:
                return None  # 増員せず、空けたままにする(手薄をそのまま見せる)
            home = floor if floor != '統括' else '1F'
            sid = f'{home}-{next_id[home]:02d}'
            next_id[home] += 1
            p = dict(staff_id=sid, name='職員' + sid, floor=home, night_allowed='可', supervisor='可',
                     allowed_codes='早・日・遅・午・後・短A・短B・夜・統', weekly_hours=40, night_month_limit=5,
                     overtime='可' if next_id[home] % 2 else '不可', weekdays='0・1・2・3・4・5・6',
                     available_start='', available_end='', transfer_allowed='可')
            staff.append(p)
            options = [p]
        sid = options[0]['staff_id']
        s = dict(staff_id=sid, date=str(day), code=code, floor=floor)
        schedule.append(s)
        assigned[sid, day] = code
        for w, charge in charges.items():
            hours[sid, w] += charge
        workdays[sid, wk].add(day)
        if spec['next_day']:
            assigned[sid, day + timedelta(days=1)] = '明'
            nextday = day + timedelta(days=1)
            workdays[sid, nextday - timedelta(days=nextday.weekday())].add(nextday)
        if allow_autofill:
            changes.append(dict(date=str(day), code=code, floor=floor, original=preferred or '', assigned=sid))
        return s

    def activity(s, begin, end, kind, target=''):
        if s is None:
            return
        activities.append(dict(staff_id=s['staff_id'], shift_date=s['date'], start=begin.isoformat(timespec='minutes'),
                                end=end.isoformat(timespec='minutes'), kind=kind, target=target))

    def preferred_ids(day, code, floor):
        if preferred_source is None:
            return []
        return [r['staff_id'] for r in preferred_source if r['date'] == str(day) and r['code'] == code
                and (floor == '統括' or r['floor'] == floor)]

    # 夜勤から先に置き、明け・契約・休息を保持する。
    for day in all_days:
        night = []
        for floor in config['floors'] + ['統括']:
            code = '統' if floor == '統括' else '夜'
            preferred = preferred_ids(day, code, floor)
            for i in range(2):
                night.append(choose(day, code, floor, preferred[i] if i < len(preferred) else None))
        a = choose(day, '短A', '1F')
        b = choose(day + timedelta(days=1), '短B', '2F')
        present_night = [s for s in night if s]
        # 食事は遅番が在席する16:30〜18:30。連続休憩は従来の夜間交代形を再現。
        nap_offsets = [5, 6, 7, 8, 9, 10, 11, 13]
        naps = {}
        for i, s in enumerate(present_night):
            origin = datetime.combine(day, datetime.min.time())
            meal = origin + timedelta(hours=16.5 + (i // 4))
            nap = origin + timedelta(hours=16 + nap_offsets[i % len(nap_offsets)])
            naps[s['staff_id']] = (nap, nap + timedelta(hours=2))
            activity(s, meal, meal + timedelta(hours=1), 'BREAK')
            activity(s, nap, nap + timedelta(hours=2), 'BREAK')
        point = datetime.combine(day, datetime.min.time()) + timedelta(hours=21)
        while point < datetime.combine(day + timedelta(days=1), datetime.min.time()) + timedelta(hours=7):
            free = [s for s in present_night[6:] if not naps[s['staff_id']][0] <= point < naps[s['staff_id']][1]]
            available = []
            for s in [a, b]:
                if s is None:
                    continue
                spec = config['shifts'][s['code']]
                lo = datetime.fromisoformat(s['date'] + 'T' + spec['start'])
                hi = datetime.fromisoformat(s['date'] + 'T' + spec['end']) + timedelta(days=spec['next_day'])
                if lo <= point < hi:
                    available.append(s)
            # フロアを担当する短時間職員の既定位置をいったんOTHERとして外し、応援へ明示。
            for s in present_night[:6]:
                if naps[s['staff_id']][0] <= point < naps[s['staff_id']][1]:
                    cover = available.pop(0) if available else (free.pop(0) if free else None)
                    activity(cover, point, point + timedelta(minutes=30), 'TRANSFER', s['floor'])
            point += timedelta(minutes=30)
    # 日中は原本の早・遅の目標5名を引継ぎ。必要なIDだけ入れ替える。
    for day in all_days:
        for floor in config['floors']:
            for code in ['早', '遅']:
                pref = preferred_ids(day, code, floor)
                for i in range(5):
                    s = choose(day, code, floor, pref[i] if i < len(pref) else None)
                    if s is None:
                        continue
                    begin = datetime.combine(day, datetime.min.time()) + timedelta(hours=11 if code == '早' else 13)
                    activity(s, begin, begin + timedelta(hours=1), 'BREAK')
                    if i == 0:
                        bath = datetime.combine(day, datetime.min.time()) + timedelta(hours=10 if code == '早' else 15)
                        activity(s, bath, bath + timedelta(hours=1), 'BATH')
        # 日勤と短時間の日中支援の利用例（曜日別）。
        if day.weekday() in [0, 2, 4]:
            s = choose(day, '日', '1F')
            if s:
                begin = datetime.combine(day, datetime.min.time()) + timedelta(hours=12)
                activity(s, begin, begin + timedelta(hours=1), 'BREAK')
        if day.weekday() in [1, 3]:
            choose(day, '午', '2F')
        if day.weekday() in [5, 6]:
            choose(day, '後', '3F')
    # 残りの日は休(または、あらかじめ確定させた分はその内容)とする。
    for p in staff:
        for day in all_days:
            key = p['staff_id'], day
            if key not in assigned:
                assigned[key] = '休'
            if assigned[key] in ['休', '明'] or (preassigned and key in preassigned):
                schedule.append(dict(staff_id=p['staff_id'], date=str(day), code=assigned[key], floor=p['floor']))

    schedule.sort(key=lambda r: (r['staff_id'], r['date']))
    return dict(schedule=schedule, activities=activities, changes=changes)
