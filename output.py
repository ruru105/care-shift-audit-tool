"""監査レポートと職員用CSV。監査エンジンを必ず再実行して配布を制御する。"""
from datetime import date
import html
import json
from pathlib import Path
from .engine import audit
from .io import write_csv

LABELS = {"RED":"配布不可", "YELLOW":"要確認", "GREEN":"配布可能"}


def staff_rows(data, status):
    people = {p['staff_id']:p for p in data['staff']}
    rows = []
    for row in data['schedule']:
        day = date.fromisoformat(row['date'])
        if not data['config']['start'][:10] <= row['date'] < data['config']['end'][:10]:
            continue
        spec = data['config']['shifts'].get(row['code'])
        rows.append(dict(date=row['date'], weekday='月火水木金土日'[day.weekday()],
                         staff_id=row['staff_id'], name=people[row['staff_id']]['name'],
                         code=row['code'], time=(spec['start']+'～'+('翌' if spec['next_day'] else '')+spec['end']) if spec else '',
                         status='正式' if status=='GREEN' else '確認中・未確定'))
    return rows


def export_report(data, folder, formal=False):
    folder = Path(folder)
    folder.mkdir(parents=True,exist_ok=True)
    result = audit(data)
    # 同じフォルダに古い正式版が残らないようにする。
    formal_path = folder/'staff_formal.csv'
    if result['status'] != 'GREEN' and formal_path.exists():
        formal_path.unlink()
    (folder/'audit.json').write_text(json.dumps(result,ensure_ascii=False,indent=2),encoding='utf-8')
    write_csv(folder/'issues.csv',result['issues'],['severity','code','category','staff_id','when','floor','message'])
    write_csv(folder/'coverage.csv',result['coverage'],['time','floor','planned','breaks','bath','other','out','incoming','active','minimum','deficit','judgment','staff_ids'])
    write_csv(folder/'workload.csv',result['workload'],['staff_id','period','work_hours','contract_hours'])
    rows = staff_rows(data,result['status'])
    write_csv(folder/'staff_draft.csv',rows,['date','weekday','staff_id','name','code','time','status'])
    esc = lambda value: html.escape(str(value))
    table = ''.join('<tr>'+''.join('<td>'+esc(row[k])+'</td>' for k in ['severity','when','floor','staff_id','message'])+'</tr>' for row in result['issues'])
    color = {'RED':'#b42318','YELLOW':'#946200','GREEN':'#18704a'}[result['status']]
    page = f'''<!doctype html><html lang="ja"><meta charset="utf-8"><title>勤務表監査結果</title>
    <style>body{{font-family:system-ui,sans-serif;margin:40px;color:#23354b}}h1{{font-size:26px}}
    .status{{font-size:28px;color:{color}}}table{{border-collapse:collapse;width:100%}}td,th{{padding:9px;text-align:left;border-bottom:1px solid #ddd}}th{{background:#eaf0f5}}</style>
    <h1>Care Shift Audit Tool V1</h1><p class="status">{LABELS[result['status']]}</p>
    <p>重大NG {result['counts'].get('RED',0)}件 ／ 要確認 {result['counts'].get('YELLOW',0)}件</p>
    <p>入力変更後は再監査してください。これは保存時点の結果です。架空データ。</p>
    <table><tr><th>区分</th><th>日時</th><th>場所</th><th>職員</th><th>理由</th></tr>{table}</table>
    <p>入力識別子: {result['input_sha256']}</p></html>'''
    (folder/'report.html').write_text(page,encoding='utf-8')
    if formal:
        if result['status']!='GREEN':
            raise ValueError('正式配布を停止しました。重大NGまたは未確認事項が残っています。')
        write_csv(formal_path,rows,['date','weekday','staff_id','name','code','time','status'])
    return result
