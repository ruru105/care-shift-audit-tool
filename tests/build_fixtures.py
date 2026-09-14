"""公開用の架空テスト資料を再現する開発スクリプト。自動シフト作成機能ではない。"""
from collections import defaultdict
from copy import deepcopy
from datetime import date, datetime, timedelta
import csv
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/'src'))
from care_shift_audit.io import write_csv

FIELDS = {
 'staff':['staff_id','name','floor','night_allowed','supervisor','allowed_codes','weekly_hours','night_month_limit','overtime','weekdays','available_start','available_end','transfer_allowed'],
 'schedule':['staff_id','date','code','floor'],
 'activities':['staff_id','shift_date','start','end','kind','target'],
 'requests':['staff_id','date','code'],
 'overtime_candidates':['staff_id','date','planned_hours'],
 'overtime_ledger':['staff_id','month','overtime_hours','holiday_hours','annual_overtime_hours','months_over_45','combined_history'],
 'leave_ledger':['staff_id','grant_date','granted_days','taken_full_days','taken_half_days','taken_hourly_hours','scheme'],
}


def save(data, name):
    folder=ROOT/'data'/name
    folder.mkdir(parents=True,exist_ok=True)
    (folder/'config.json').write_text(json.dumps(data['config'],ensure_ascii=False,indent=2),encoding='utf-8')
    for key, fields in FIELDS.items():
        write_csv(folder/f'{key}.csv',data[key],fields)


def build(source):
    raw=json.loads(Path(source).read_text())
    config=dict(start='2026-10-05T00:00',end='2026-10-12T00:00',as_of='2026-09-13',
                floors=['1F','2F','3F'],day_start='07:00',day_end='19:00',day_min=4,night_min=2,
                early_min=4,late_min=4,early_target=5,late_target=5,free_supervisor_min=1,
                night_supervisors=2,night_continuous_minutes=120,interval_hours=11,
                annual_holidays=120,special_clause=False,work_system='monthly_variable_unconfirmed',shifts={})
    for code,begin,end,next_day,pause,night in [
        ('早','07:00','16:00',0,60,False),('日','08:30','17:30',0,60,False),
        ('遅','10:00','19:00',0,60,False),('午','07:00','12:00',0,0,False),
        ('後','14:00','19:00',0,0,False),('短A','22:00','01:00',1,0,False),
        ('短B','01:00','04:00',0,0,False),('夜','16:00','09:00',1,180,True),
        ('統','16:00','09:00',1,180,True)]:
        config['shifts'][code]=dict(start=begin,end=end,next_day=next_day,break_minutes=pause,night=night)
    data={key:[] for key in FIELDS};data['config']=config
    for r in raw['職員マスター'][5:77]:
        allowed=r[13]+('・統' if r[12]=='可' else '')
        data['staff'].append(dict(staff_id=r[0],name='職員'+r[0],floor=r[1],night_allowed=r[5],
              supervisor=r[12],allowed_codes=allowed,weekly_hours=r[14],night_month_limit=r[7],
              overtime='要相談',weekdays='0・1・2・3・4・5・6',available_start='',available_end='',transfer_allowed=r[6]))
    source_codes={}
    for r in raw['週間仮シフト'][6:78]:
        for i,day in enumerate(raw['週間仮シフト'][4][2:11],2):
            source_codes[r[0],day[:10]]=r[i]
            data['schedule'].append(dict(staff_id=r[0],date=day[:10],code=r[i],floor='統括' if r[i]=='統' else r[1]))
    data['requests']=[dict(staff_id='1F-01',date='2026-10-09',code='休')]
    # 原本の夜間担当を、時刻を持った独立データへ移す。
    for block in range(7):
        header=raw['週間夜間配置'][6+block*22]
        day=header[2][:10]
        for r in raw['週間夜間配置'][7+block*22:17+block*22]:
            for i,t in enumerate(header[2:22],2):
                role=r[i]
                if role=='非勤務':continue
                start=datetime.fromisoformat(t)
                # 短Bは暦日の01:00開始。夜勤開始日に結びつけない。
                shift_day=str(start.date()) if source_codes.get((r[0],str(start.date())))=='短B' else day
                kind='BREAK' if role=='仮眠' else 'TRANSFER'
                data['activities'].append(dict(staff_id=r[0],shift_date=shift_day,start=start.isoformat(timespec='minutes'),
                    end=(start+timedelta(minutes=30)).isoformat(timespec='minutes'),kind=kind,target='' if kind=='BREAK' else role))
    save(data,'legacy_import')

    # 正常基準例：原本のID・可否・契約を残し、足りない条件に限って架空職員を追加。
    base=deepcopy(data);base['schedule']=[];base['activities']=[]
    assigned={}; hours=defaultdict(float); workdays=defaultdict(set); next_id=defaultdict(lambda:25)
    people={p['staff_id']:p for p in base['staff']}
    changes=[]
    def choose(day,code,floor,preferred=None):
        spec=config['shifts'][code];duration=(17 if spec['night'] else 9 if code in ['早','遅','日'] else 5 if code in ['午','後'] else 3)-spec['break_minutes']/60
        wk=day-timedelta(days=day.weekday())
        begin=datetime.fromisoformat(f'{day}T{spec["start"]}')
        finish=datetime.fromisoformat(f'{day}T{spec["end"]}')+timedelta(days=spec['next_day'])
        charges=defaultdict(float)
        point=begin
        while point<finish:
            charges[point.date()-timedelta(days=point.weekday())]+=.5
            point+=timedelta(minutes=30)
        def eligible(p):
            sid=p['staff_id'];home=p['floor']
            if code not in p['allowed_codes'].split('・') or (floor!='統括' and home!=floor and p['transfer_allowed']!='可'):return False
            if floor=='統括' and p['transfer_allowed']!='可':return False
            if (sid,day) in assigned:return False
            if spec['next_day'] and (sid,day+timedelta(days=1)) in assigned:return False
            if sid=='1F-01' and (day==date(2026,10,9) or spec['night'] and day==date(2026,10,8)):return False
            if any(hours[sid,w]+charge>float(p['weekly_hours']) for w,charge in charges.items()):return False
            occupied_days=workdays[sid,wk]|{day}
            if spec['night'] and (day+timedelta(days=1)).weekday()!=0:occupied_days.add(day+timedelta(days=1))
            if len(occupied_days)>5:return False
            if spec['night'] and sum(s['staff_id']==sid and config['shifts'][s['code']].get('night') for s in base['schedule'])>=int(p['night_month_limit']):return False
            for s in base['schedule']:
                if s['staff_id']!=sid:continue
                other=config['shifts'][s['code']]
                a=datetime.fromisoformat(s['date']+'T'+other['start'])
                b=datetime.fromisoformat(s['date']+'T'+other['end'])+timedelta(days=other['next_day'])
                if not (begin>=b+timedelta(hours=11) or finish+timedelta(hours=11)<=a):return False
            return True
        options=[p for p in base['staff'] if eligible(p)]
        options.sort(key=lambda p:(p['staff_id']!=preferred, p['floor']!=floor, hours[p['staff_id'],wk],p['staff_id']))
        if not options:
            home=floor if floor!='統括' else '1F'
            sid=f'{home}-{next_id[home]:02d}';next_id[home]+=1
            p=dict(staff_id=sid,name='職員'+sid,floor=home,night_allowed='可',supervisor='可',
                allowed_codes='早・日・遅・午・後・短A・短B・夜・統',weekly_hours=40,night_month_limit=5,
                overtime='可' if next_id[home]%2 else '不可',weekdays='0・1・2・3・4・5・6',available_start='',available_end='',transfer_allowed='可')
            base['staff'].append(p);people[sid]=p;options=[p]
        sid=options[0]['staff_id']
        s=dict(staff_id=sid,date=str(day),code=code,floor=floor)
        base['schedule'].append(s);assigned[sid,day]=code
        for w,charge in charges.items():hours[sid,w]+=charge
        workdays[sid,wk].add(day)
        if spec['next_day']:
            assigned[sid,day+timedelta(days=1)]='明'
            nextday=day+timedelta(days=1);workdays[sid,nextday-timedelta(days=nextday.weekday())].add(nextday)
        changes.append(dict(date=str(day),code=code,floor=floor,original=preferred or '',assigned=sid))
        return s
    def activity(s,begin,end,kind,target=''):
        base['activities'].append(dict(staff_id=s['staff_id'],shift_date=s['date'],start=begin.isoformat(timespec='minutes'),end=end.isoformat(timespec='minutes'),kind=kind,target=target))
    all_days=[date(2026,10,4)+timedelta(days=i) for i in range(9)]
    # 夜勤から先に置き、明け・契約・休息を保持する。
    for day in all_days:
        night=[]
        for floor in config['floors']+['統括']:
            code='統' if floor=='統括' else '夜'
            preferred=[r['staff_id'] for r in data['schedule'] if r['date']==str(day) and r['code']==code and (floor=='統括' or r['floor']==floor)]
            for i in range(2):night.append(choose(day,code,floor,preferred[i] if i<len(preferred) else None))
        a=choose(day,'短A','1F')
        b=choose(day+timedelta(days=1),'短B','2F')
        # 食事は遅番が在席する16:30〜18:30。連続休憩は従来の夜間交代形を再現。
        nap_offsets=[5,6,7,8,9,10,11,13]
        naps={}
        for i,s in enumerate(night):
            origin=datetime.combine(day,datetime.min.time())
            meal=origin+timedelta(hours=16.5+(i//4))
            nap=origin+timedelta(hours=16+nap_offsets[i]);naps[s['staff_id']]=(nap,nap+timedelta(hours=2))
            activity(s,meal,meal+timedelta(hours=1),'BREAK')
            activity(s,nap,nap+timedelta(hours=2),'BREAK')
        point=datetime.combine(day,datetime.min.time())+timedelta(hours=21)
        while point<datetime.combine(day+timedelta(days=1),datetime.min.time())+timedelta(hours=7):
            free=[s for s in night[6:] if not naps[s['staff_id']][0]<=point<naps[s['staff_id']][1]]
            available=[]
            for s in [a,b]:
                spec=config['shifts'][s['code']];lo=datetime.fromisoformat(s['date']+'T'+spec['start']);hi=datetime.fromisoformat(s['date']+'T'+spec['end'])+timedelta(days=spec['next_day'])
                if lo<=point<hi:available.append(s)
            # フロアを担当する短時間職員の既定位置をいったんOTHERとして外し、応援へ明示。
            used=[]
            for s in night[:6]:
                if naps[s['staff_id']][0]<=point<naps[s['staff_id']][1]:
                    cover=available.pop(0) if available else free.pop(0)
                    activity(cover,point,point+timedelta(minutes=30),'TRANSFER',s['floor']);used.append(cover)
            point+=timedelta(minutes=30)
    # 日中は原本の早・遅の目標5名を引継ぎ。必要なIDだけ入れ替える。
    for day in all_days:
        for floor in config['floors']:
            for code in ['早','遅']:
                pref=[r['staff_id'] for r in data['schedule'] if r['date']==str(day) and r['code']==code and r['floor']==floor]
                for i in range(5):
                    s=choose(day,code,floor,pref[i] if i<len(pref) else None)
                    begin=datetime.combine(day,datetime.min.time())+timedelta(hours=11 if code=='早' else 13)
                    activity(s,begin,begin+timedelta(hours=1),'BREAK')
                    if i==0:
                        bath=datetime.combine(day,datetime.min.time())+timedelta(hours=10 if code=='早' else 15)
                        activity(s,bath,bath+timedelta(hours=1),'BATH')
        # 日勤と短時間の日中支援の利用例（曜日別）。
        if day.weekday() in [0,2,4]:
            s=choose(day,'日','1F');begin=datetime.combine(day,datetime.min.time())+timedelta(hours=12)
            activity(s,begin,begin+timedelta(hours=1),'BREAK')
        if day.weekday() in [1,3]:choose(day,'午','2F')
        if day.weekday() in [5,6]:choose(day,'後','3F')
    for p in base['staff']:
        for day in all_days:
            key=p['staff_id'],day
            if key not in assigned:assigned[key]='休'
            if assigned[key] in ['休','明']:
                base['schedule'].append(dict(staff_id=p['staff_id'],date=str(day),code=assigned[key],floor=p['floor']))
    # 翌朝のみの短Bが持つ10/13勤務を残すが、監査本体は10/5〜11。
    base['schedule'].sort(key=lambda r:(r['staff_id'],r['date']))
    save(base,'baseline')
    write_csv(ROOT/'docs'/'assignment_changes.csv',changes,['date','code','floor','original','assigned'])
    return base


if __name__=='__main__':
    baseline=build(sys.argv[1])
    print('staff',len(baseline['staff']),'schedule',len(baseline['schedule']),'activities',len(baseline['activities']))
