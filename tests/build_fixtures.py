"""公開用の架空テスト資料を再現する開発スクリプト。自動シフト作成機能ではない。
勤務・休憩・担当の組み立てロジック自体はscenario_builder.pyに共通化してあり、
data/one_month_example・data/understaffed_*でも同じ土台を使う。"""
from copy import deepcopy
from datetime import date, datetime, timedelta
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/'src'))
from care_shift_audit.io import write_csv
from scenario_builder import generate

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

    base=build_scenario(data,config,'baseline',[date(2026,10,4)+timedelta(days=i) for i in range(9)],
                         config['start'],config['end'],ROOT/'docs'/'assignment_changes.csv')

    # 1か月例：同じ72人・同じ設定を使い、監査期間だけを2026年10月の1か月分に広げる。
    one_month_days=[date(2026,9,30)+timedelta(days=i) for i in range(33)]
    build_scenario(data,config,'one_month_example',one_month_days,
                    '2026-10-01T00:00','2026-11-01T00:00',
                    ROOT/'data'/'one_month_example'/'assignment_changes.csv')
    return base


def build_scenario(data,config,folder_name,all_days,start,end,changes_path):
    # 正常基準例：原本のID・可否・契約を残し、足りない条件に限って架空職員を追加。
    # 希望休(1F-01・2026-10-09)は、あらかじめassignedを埋めることで実現する(特別扱いのコードは書かない)。
    base=deepcopy(data);base['schedule']=[];base['activities']=[]
    base['config']=deepcopy(config);base['config']['start']=start;base['config']['end']=end
    preassigned={(r['staff_id'],date.fromisoformat(r['date'])):r['code'] for r in data['requests']}
    result=generate(base['staff'],all_days,config,allow_autofill=True,
                     preassigned=preassigned,preferred_source=data['schedule'])
    base['schedule']=result['schedule']
    base['activities']=result['activities']
    save(base,folder_name)
    changes_path.parent.mkdir(parents=True,exist_ok=True)
    write_csv(changes_path,result['changes'],['date','code','floor','original','assigned'])
    return base


if __name__=='__main__':
    baseline=build(sys.argv[1])
    print('staff',len(baseline['staff']),'schedule',len(baseline['schedule']),'activities',len(baseline['activities']))
