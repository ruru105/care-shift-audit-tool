"""異常を検出できることと、正常例を壊さないことを確認する。"""
from copy import deepcopy
from datetime import date
from pathlib import Path
import random
import pytest
from care_shift_audit.engine import audit, legal_break_minutes, leave_findings, overtime_findings, distribution_status
from care_shift_audit.io import load_folder
from care_shift_audit.output import export_report
from cases import CASES, make_case

ROOT=Path(__file__).resolve().parents[1]


@pytest.fixture(scope='module')
def baseline():
    return load_folder(ROOT/'data/baseline')


def test_normal_has_zero_critical_and_no_shortage(baseline):
    result=audit(baseline)
    assert result['counts'].get('RED',0)==0
    assert len(result['coverage'])==7*48*4
    assert all(row['deficit']==0 for row in result['coverage'])
    assert result['status']=='YELLOW'
    assert result['counts']['YELLOW']==5


@pytest.mark.parametrize('index',range(len(CASES)),ids=[c[0] for c in CASES])
def test_intentional_ng(baseline,index):
    result=audit(make_case(baseline,index))
    assert CASES[index][1] in {x['code'] for x in result['issues']}
    assert result['status']=='RED'


def test_legacy_faults_survive_import():
    result=audit(load_folder(ROOT/'data/legacy_import'))
    assert {'EARLY_SHORT','NIGHT_FORBIDDEN','REQUEST_CONFLICT','POST_NIGHT_CONFLICT','COVERAGE_SHORT'} <= {x['code'] for x in result['issues']}


@pytest.mark.parametrize('minutes,expected',[(360,0),(361,45),(480,45),(481,60)])
def test_law_break_boundaries(minutes,expected):
    assert legal_break_minutes(minutes)==expected


@pytest.mark.parametrize('granted,taken,half,hourly,day,expected',[
    (9,0,0,0,'2026-10-01',None),(10,5,0,0,'2026-10-01',None),
    (10,4,2,0,'2026-10-01',None),(10,4,0,8,'2026-10-01','LEAVE_FIVE'),
    (10,4,0,0,'2026-09-01','LEAVE_PENDING')])
def test_paid_leave_conditions(granted,taken,half,hourly,day,expected):
    row=dict(grant_date='2025-10-01',granted_days=granted,taken_full_days=taken,taken_half_days=half,taken_hourly_hours=hourly)
    codes={x[1] for x in leave_findings(row,date.fromisoformat(day))}
    assert (expected in codes) if expected else not codes


@pytest.mark.parametrize('ot,holiday,special,annual,history,expected',[
    (45,0,False,360,[0,0,0,0,0,45],'OT_APPROACH'),
    (46,0,False,360,[0,0,0,0,0,46],'OT_MONTH'),
    (80,20,True,200,[0,0,0,0,0,100],'OT_100'),
    (80,0,True,721,[0,0,0,0,0,80],'OT_YEAR'),
    (80,0,True,200,[0,0,0,0,82,80],'OT_AVERAGE'),
    (10,0,False,20,[],'OT_HISTORY_MISSING')])
def test_overtime_bounds(ot,holiday,special,annual,history,expected):
    row=dict(overtime_hours=ot,holiday_hours=holiday,annual_overtime_hours=annual,combined_history=history)
    assert expected in {x[1] for x in overtime_findings(row,special)}


def test_cannot_publish_yellow_or_keep_old_formal(baseline,tmp_path):
    old=tmp_path/'staff_formal.csv';old.write_text('old approval')
    with pytest.raises(ValueError,match='正式配布'):
        export_report(baseline,tmp_path,formal=True)
    assert not old.exists()
    assert (tmp_path/'staff_draft.csv').exists()


def test_priority():
    assert distribution_status([])=='GREEN'
    assert distribution_status([{'severity':'YELLOW'}])=='YELLOW'
    assert distribution_status([{'severity':'RED'},{'severity':'YELLOW'}])=='RED'


def test_duplicates_do_not_inflate_coverage(baseline):
    data=deepcopy(baseline);s=next(s for s in data['schedule'] if s['code']=='早' and s['date']=='2026-10-06')
    data['schedule'].append(deepcopy(s))
    result=audit(data)
    assert 'DUPLICATE_SHIFT' in {x['code'] for x in result['issues']}
    assert result['coverage']==audit(baseline)['coverage']


def test_unknown_staff_fails_closed(baseline):
    data=deepcopy(baseline);data['schedule'][0]['staff_id']='UNKNOWN'
    assert audit(data)['status']=='RED'


def test_off_grid_time_is_rejected(baseline):
    data=deepcopy(baseline);data['activities'][0]['start']='2026-10-04T16:45'
    assert 'INPUT_INVALID' in {x['code'] for x in audit(data)['issues']}


def test_input_order_does_not_change_counts(baseline):
    data=deepcopy(baseline);rng=random.Random(20260913)
    for key in ['staff','schedule','activities']:rng.shuffle(data[key])
    a,b=audit(data),audit(baseline)
    assert a['counts']==b['counts']
    assert a['coverage']==b['coverage']


def test_transfer_is_counted_once(baseline):
    r=audit(baseline)
    rows=[x for x in r['coverage'] if x['time']=='2026-10-06T22:00']
    ids=[sid for row in rows for sid in row['staff_ids'].split('・') if sid]
    assert len(ids)==len(set(ids))
    assert next(x for x in rows if x['floor']=='統括')['active']>=1


def test_short_shift_end_not_included(baseline):
    s=next(s for s in baseline['schedule'] if s['code']=='短A' and s['date']=='2026-10-06')
    rows=[x for x in audit(baseline)['coverage'] if x['time']=='2026-10-07T01:00']
    assert all(s['staff_id'] not in row['staff_ids'].split('・') for row in rows)


def test_gender_not_used(baseline):
    data=deepcopy(baseline)
    for person in data['staff']:person['gender']='任意の値'
    assert audit(data)['coverage']==audit(baseline)['coverage']


def test_weekday_and_time_restriction(baseline):
    data=deepcopy(baseline)
    s=next(s for s in data['schedule'] if s['code']=='早' and s['date']=='2026-10-06')
    p=next(p for p in data['staff'] if p['staff_id']==s['staff_id'])
    p.update(weekdays='0',available_start='09:00',available_end='12:00')
    codes={x['code'] for x in audit(data)['issues']}
    assert {'WEEKDAY_RESTRICTION','TIME_RESTRICTION'}<=codes


def test_no_break_is_not_zero_work(baseline):
    data=deepcopy(baseline);data['activities']=[]
    result=audit(data)
    assert 'LEGAL_BREAK_SHORT' in {x['code'] for x in result['issues']}


def test_settings_not_hardcoded(baseline):
    data=deepcopy(baseline);data['config']['day_min']=20
    assert 'COVERAGE_SHORT' in {x['code'] for x in audit(data)['issues']}


def test_missing_input_is_yellow(baseline):
    data=deepcopy(baseline);data['schedule']=[r for r in data['schedule'] if not (r['date']=='2026-10-06' and r['code']=='休')]
    assert 'SCHEDULE_MISSING' in {x['code'] for x in audit(data)['issues']}
