"""異常を検出できることと、正常例を壊さないことを確認する。"""
from copy import deepcopy
from datetime import date
from pathlib import Path
import random
import pytest
from care_shift_audit.engine import audit, legal_break_minutes, leave_findings, overtime_findings, distribution_status
from care_shift_audit.io import load_folder
from care_shift_audit.output import export_report, staff_rows
from care_shift_audit.__main__ import main as cli_main
from cases import CASES, make_case

ROOT=Path(__file__).resolve().parents[1]


@pytest.fixture(scope='module')
def baseline():
    return load_folder(ROOT/'data/baseline')


@pytest.fixture(scope='module')
def one_month():
    return load_folder(ROOT/'data/one_month_example')


def test_normal_has_zero_critical_and_no_shortage(baseline):
    result=audit(baseline)
    assert result['counts'].get('RED',0)==0
    assert len(result['coverage'])==7*48*4
    assert all(row['deficit']==0 for row in result['coverage'])
    assert result['status']=='YELLOW'
    assert result['counts']['YELLOW']==5


def test_one_month_example_has_zero_critical_and_no_shortage(one_month):
    """7日間だけでなく、1か月分の期間でも同じ監査ロジックが崩れないことを確認する。"""
    result=audit(one_month)
    assert result['counts'].get('RED',0)==0
    assert len(result['coverage'])==31*48*4
    assert all(row['deficit']==0 for row in result['coverage'])
    assert result['status']=='YELLOW'
    assert result['counts']['YELLOW']==5


def test_one_month_example_actually_spans_calendar_month(one_month):
    """監査期間がconfigどおり2026年10月1か月分になっていることを日付そのもので確認する。"""
    days={row['date'] for row in one_month['schedule']
          if one_month['config']['start']<=row['date']+'T00:00'<one_month['config']['end']}
    assert days=={f'2026-10-{d:02d}' for d in range(1,32)}


UNDERSTAFFED_EXPECTED = {
    '2026_10': dict(RED=1764, YELLOW=53, slots=5952),
    '2026_11': dict(RED=3270, YELLOW=53, slots=5760),
    '2026_12': dict(RED=3303, YELLOW=53, slots=5952),
    '2027_01': dict(RED=3366, YELLOW=41, slots=5952),
    '2027_02': dict(RED=3018, YELLOW=5, slots=5376),
    '2027_03': dict(RED=3292, YELLOW=5, slots=5952),
}


@pytest.mark.parametrize('month', sorted(UNDERSTAFFED_EXPECTED))
def test_understaffed_series_shows_real_shortage_without_added_staff(month):
    """72人だけ・増員なしで6か月を組むと、月ごとに実際にどれだけ手薄かを確認する(2026-09-26 に決めた方針)。
    数値は既知の生成結果を固定したもの。tests/build_understaffed_series.pyを変えたら意図的な変化か確認すること。"""
    data=load_folder(ROOT/f'data/understaffed_{month}')
    assert len(data['staff'])==72
    result=audit(data)
    expected=UNDERSTAFFED_EXPECTED[month]
    assert result['status']=='RED'
    assert result['counts'].get('RED',0)==expected['RED']
    assert result['counts'].get('YELLOW',0)==expected['YELLOW']
    assert len(result['coverage'])==expected['slots']
    assert sum(1 for row in result['coverage'] if row['deficit']>0)>0


def test_supervisor_and_off_codes_are_configurable(baseline):
    """「統括」「統」「休」「明」「有」はconfigで名前・コードを変えられる(施設ごとの呼び方に対応)。
    名前だけ変えても、正常基準の判定結果(件数)がまったく変わらないことを確認する。"""
    data=deepcopy(baseline)
    data['config']['supervisor_role']='フリー'
    data['config']['supervisor_shift_code']='F'
    data['config']['off_code']='OFF'
    data['config']['post_night_code']='AKE'
    data['config']['paid_leave_code']='PTO'
    remap={'統':'F','休':'OFF','明':'AKE','有':'PTO'}
    data['config']['shifts']={remap.get(k,k):v for k,v in data['config']['shifts'].items()}
    for row in data['schedule']:
        row['code']=remap.get(row['code'],row['code'])
        if row['floor']=='統括':row['floor']='フリー'
    for row in data['requests']:
        row['code']=remap.get(row['code'],row['code'])
    for row in data['activities']:
        if row.get('target')=='統括':row['target']='フリー'
    for row in data['staff']:
        row['allowed_codes']='・'.join(remap.get(c,c) for c in row['allowed_codes'].split('・'))
    before=audit(baseline)
    after=audit(data)
    assert after['counts']==before['counts']
    assert len(after['coverage'])==len(before['coverage'])
    assert {r['floor'] for r in after['coverage']}=={'1F','2F','3F','フリー'}


def test_week_start_weekday_is_configurable(baseline):
    """week_start_weekdayを変えると、週の集計区切りが実際にその曜日から始まるようになることを確認する。"""
    data=deepcopy(baseline)
    monday_periods={row['period'] for row in audit(data)['workload']}
    data['config']['week_start_weekday']=6  # 日曜始まり
    sunday_periods={row['period'] for row in audit(data)['workload']}
    assert monday_periods!=sunday_periods
    assert all(date.fromisoformat(p).weekday()==0 for p in monday_periods)
    assert all(date.fromisoformat(p).weekday()==6 for p in sunday_periods)


def test_understaffed_series_leave_catches_up_by_february(month=None):
    """まだ5日取得できていない人の数が、2月には0人になる(今回の均等割りロジックの結果)ことを確認する。"""
    counts=[]
    for month in sorted(UNDERSTAFFED_EXPECTED):
        data=load_folder(ROOT/f'data/understaffed_{month}')
        as_of=date.fromisoformat(data['config']['as_of'])
        pending=sum(1 for row in data['leave_ledger']
                    if leave_findings(row, as_of) and leave_findings(row, as_of)[0][1] in ('LEAVE_PENDING', 'LEAVE_FIVE'))
        counts.append(pending)
    assert counts[0] > 0  # 10月時点ではまだ足りない人がいる
    assert counts[-2:] == [0, 0]  # 2月・3月には全員追いついている
    assert all(c1 >= c2 for c1, c2 in zip(counts, counts[1:]))  # 単調に減っていく


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


def test_workload_rows_sorted_for_reproducibility(baseline):
    """workloadの並び順が集合の走査順に依存しないことを確認する(2026-09-27発見・修正)。"""
    result=audit(baseline)
    rows=result['workload']
    assert len(rows)>0
    assert rows==sorted(rows,key=lambda w:(w['staff_id'],w['period']))


def test_issues_rows_sorted_for_reproducibility():
    """issuesの並び順も集合の走査順に依存しないことを確認する(2026-09-27発見・修正)。データ量の多い旧移行データで発生を確認済み。"""
    data=load_folder(ROOT/'data/legacy_import')
    result=audit(data)
    rows=result['issues']
    assert len(rows)>0
    assert rows==sorted(rows,key=lambda x:(x['staff_id'],x['when'],x['floor'],x['code']))


def test_csv_headers_are_japanese(baseline,tmp_path):
    """職員に渡す前提のCSVは、見出しが日本語であることを確認する(2026-09-27追加)。内部のキー名(英語)自体は変えていない。"""
    export_report(baseline,tmp_path)
    import csv
    with (tmp_path/'coverage.csv').open(encoding='utf-8-sig') as f:
        header=next(csv.reader(f))
    assert header==['日時','フロア','予定人数','休憩中','入浴介助中','その他離脱','応援で外出中','応援受入','実働人数','最低人数','不足人数','判定','職員ID']
    with (tmp_path/'issues.csv').open(encoding='utf-8-sig') as f:
        header=next(csv.reader(f))
    assert header==['重大度','検出コード','分類','職員ID','日時','フロア','内容']
    with (tmp_path/'workload.csv').open(encoding='utf-8-sig') as f:
        header=next(csv.reader(f))
    assert header==['職員ID','期間','実働時間','契約時間']
    with (tmp_path/'staff_draft.csv').open(encoding='utf-8-sig') as f:
        header=next(csv.reader(f))
    assert header==['日付','曜日','職員ID','氏名','勤務コード','時間','状態']


# ---- 入力エラーがあっても、レポートが途中で止まらないこと ----

def _with_unknown_staff_in_period(baseline):
    """監査期間内の勤務入力1行の職員IDを、マスタにないIDへ変えたデータを返す。"""
    data = deepcopy(baseline)
    start, end = data['config']['start'][:10], data['config']['end'][:10]
    for row in data['schedule']:
        if start <= row['date'] < end:
            row['staff_id'] = 'UNKNOWN'
            return data
    raise AssertionError('期間内の勤務入力がありません')


def test_unknown_staff_id_still_produces_report_with_red_status(baseline, tmp_path):
    data = _with_unknown_staff_in_period(baseline)

    result = export_report(data, tmp_path)

    assert result['status'] == 'RED'
    assert any(issue['code'] == 'INPUT_INVALID' for issue in result['issues'])
    html_text = (tmp_path/'report.html').read_text(encoding='utf-8')
    assert '配布不可' in html_text
    assert '除外した行: 1件' in html_text
    assert 'UNKNOWN' in html_text  # 理由の表に、問題の職員IDが出る


def test_unknown_staff_row_is_excluded_from_staff_draft(baseline, tmp_path):
    data = _with_unknown_staff_in_period(baseline)

    rows = staff_rows(data, 'RED')
    normal_rows = staff_rows(baseline, 'YELLOW')

    assert len(rows) == len(normal_rows) - 1
    assert all(row['staff_id'] != 'UNKNOWN' for row in rows)


def test_unknown_staff_never_produces_formal_csv(baseline, tmp_path):
    data = _with_unknown_staff_in_period(baseline)

    with pytest.raises(ValueError):
        export_report(data, tmp_path, formal=True)

    assert not (tmp_path/'staff_formal.csv').exists()


def test_stopped_run_replaces_stale_report_with_stop_reason(tmp_path, capsys, monkeypatch):
    out = tmp_path/'out'
    out.mkdir()
    (out/'report.html').write_text('<html>前回の正常なレポート</html>', encoding='utf-8')
    missing_input = tmp_path/'no_such_folder'
    monkeypatch.setattr('sys.argv', ['care_shift_audit', str(missing_input), '--out', str(out)])

    code = cli_main()

    assert code == 2
    assert '処理停止' in capsys.readouterr().err
    html_text = (out/'report.html').read_text(encoding='utf-8')
    assert '前回の正常なレポート' not in html_text
    assert '処理停止' in html_text
    assert '配布に使わないでください' in html_text


def test_stop_reason_is_html_escaped(tmp_path):
    from care_shift_audit.output import write_stop_report
    write_stop_report(tmp_path, '<script>alert(1)</script>')
    html_text = (tmp_path/'report.html').read_text(encoding='utf-8')
    assert '<script>alert' not in html_text
    assert '&lt;script&gt;' in html_text


BAD_FACILITY_SETTINGS=[
    ('interval_hours','bad'),
    ('interval_hours',None),
    ('interval_hours',-5),
    ('interval_hours',True),
    ('night_continuous_minutes','x'),
    ('night_continuous_minutes',-1),
    ('night_supervisors','two'),
    ('night_supervisors',-1),
    ('annual_holidays','x'),
    ('as_of','bad-date'),
    ('week_start_weekday','x'),
    ('week_start_weekday',9),
    ('day_min',True),
    ('special_clause','true'),
]


@pytest.mark.parametrize('key,value',BAD_FACILITY_SETTINGS)
def test_bad_facility_setting_is_input_invalid_not_exception(baseline,key,value):
    """施設設定の型・範囲が不正でも例外で止まらず、配布不可(RED)として理由つきで返す。"""
    data=deepcopy(baseline);data['config'][key]=value
    result=audit(data)
    invalid=[x for x in result['issues'] if x['code']=='INPUT_INVALID']
    assert result['status']=='RED'
    assert invalid and key in invalid[0]['message']


def test_missing_required_facility_setting_is_input_invalid(baseline):
    data=deepcopy(baseline);del data['config']['interval_hours']
    result=audit(data)
    assert result['status']=='RED'
    assert any('interval_hours' in x['message'] for x in result['issues'] if x['code']=='INPUT_INVALID')


def test_bad_facility_setting_still_produces_report(baseline,tmp_path):
    data=deepcopy(baseline);data['config']['interval_hours']='bad'
    result=export_report(data,tmp_path)
    assert result['status']=='RED'
    assert '配布不可' in (tmp_path/'report.html').read_text(encoding='utf-8')


def test_valid_facility_settings_are_still_accepted(baseline):
    """境界の正常値(0・小数・日曜始まり・特別条項あり)は、不正扱いにならないこと。"""
    data=deepcopy(baseline)
    data['config'].update(interval_hours=11.5,night_supervisors=0,week_start_weekday=6,special_clause=True)
    assert not any(x['code']=='INPUT_INVALID' for x in audit(data)['issues'])
    data['config'].update(interval_hours=0,week_start_weekday=0,special_clause=False)
    assert not any(x['code']=='INPUT_INVALID' for x in audit(data)['issues'])
