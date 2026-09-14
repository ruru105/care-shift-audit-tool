"""正常基準から1種類ずつ異常を作る。期待する検出コードは手で指定。"""
from copy import deepcopy
from datetime import date, timedelta

CASES = [
 ('早番不足','EARLY_SHORT'),('遅番不足','LATE_SHORT'),('休憩重複','ACTIVITY_OVERLAP'),
 ('夜間人数不足','COVERAGE_SHORT'),('夜勤休憩重複','COVERAGE_SHORT'),
 ('夜勤不可','NIGHT_FORBIDDEN'),('希望休違反','REQUEST_CONFLICT'),
 ('夜勤明け矛盾','POST_NIGHT_CONFLICT'),('入浴で人数割れ','COVERAGE_SHORT'),
 ('残業不可','OT_FORBIDDEN'),('残業上限','OT_MONTH'),('休日不足','WEEKLY_HOLIDAY'),
 ('月間時間枠','MONTH_PLAN_FRAME'),('短時間外の配置','ACTIVITY_OUTSIDE_SHIFT'),
 ('休憩中断','BREAK_INTERRUPTED'),('勤務間隔不足','INTERVAL_SHORT'),
]


def make_case(baseline, index):
    data=deepcopy(baseline)
    shifts=data['schedule']
    def find(code, day='2026-10-06',floor='1F'):
        return next(s for s in shifts if s['date']==day and s['code']==code and s['floor']==floor)
    def change(s,code):
        data['activities']=[a for a in data['activities'] if (a['staff_id'],a['shift_date'])!=(s['staff_id'],s['date'])]
        s['code']=code
    def add(s,begin,end,kind,target=''):
        data['activities'].append(dict(staff_id=s['staff_id'],shift_date=s['date'],start=begin,end=end,kind=kind,target=target))
    if index in [0,1]:
        code='早' if index==0 else '遅'
        rows=[s for s in shifts if s['date']=='2026-10-06' and s['code']==code and s['floor']=='1F'][:2]
        for s in rows:change(s,'休')
    elif index==2:
        s=find('早');add(s,'2026-10-06T11:00','2026-10-06T11:30','BATH')
    elif index==3:
        s=find('夜');add(s,'2026-10-06T20:00','2026-10-06T20:30','OTHER')
    elif index==4:
        rows=[s for s in shifts if s['date']=='2026-10-06' and s['code']=='夜' and s['floor']=='1F']
        for s in rows:add(s,'2026-10-06T20:00','2026-10-06T21:00','BREAK')
    elif index==5:
        s=find('夜');next(p for p in data['staff'] if p['staff_id']==s['staff_id'])['night_allowed']='不可'
    elif index==6:
        s=find('早');data['requests'].append(dict(staff_id=s['staff_id'],date=s['date'],code='休'))
    elif index==7:
        s=find('夜');r=next(r for r in shifts if r['staff_id']==s['staff_id'] and r['date']=='2026-10-07');change(r,'早')
    elif index==8:
        for s in [s for s in shifts if s['date']=='2026-10-06' and s['code']=='早' and s['floor']=='1F'][:2]:
            add(s,'2026-10-06T09:00','2026-10-06T10:00','BATH')
    elif index==9:
        s=find('早');next(p for p in data['staff'] if p['staff_id']==s['staff_id'])['overtime']='不可'
        data['overtime_candidates'].append(dict(staff_id=s['staff_id'],date=s['date'],planned_hours='1'))
    elif index==10:
        s=find('早');data['overtime_ledger'].append(dict(staff_id=s['staff_id'],month='2026-09',overtime_hours='46',holiday_hours='0',annual_overtime_hours='46',months_over_45='1',combined_history='[0,0,0,0,0,46]'))
    elif index in [11,12]:
        sid=find('早')['staff_id']
        data['schedule']=[s for s in shifts if s['staff_id']!=sid]
        data['activities']=[a for a in data['activities'] if a['staff_id']!=sid]
        start=date(2026,10,5) if index==11 else date(2026,10,1)
        for i in range(7 if index==11 else 31):
            day=str(start+timedelta(days=i));s=dict(staff_id=sid,date=day,code='早',floor='1F');data['schedule'].append(s)
            add(s,day+'T11:00',day+'T12:00','BREAK')
    elif index==13:
        s=find('短A');add(s,'2026-10-06T21:30','2026-10-06T22:00','TRANSFER','2F')
    elif index==14:
        s=find('夜');a=next(a for a in data['activities'] if a['staff_id']==s['staff_id'] and a['shift_date']==s['date'] and a['kind']=='BREAK' and a['start'][11:]=='21:00');a['kind']='BREAK_INTERRUPTED'
    elif index==15:
        s=find('遅');sid=s['staff_id']
        following=next(r for r in shifts if r['staff_id']==sid and r['date']=='2026-10-07');change(following,'早')
        data['config']['shifts']['早']['start']='05:00'
    return data
