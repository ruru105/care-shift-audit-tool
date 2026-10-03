"""監査レポートと職員用CSV。監査エンジンを必ず再実行して配布を制御する。"""
from datetime import date
import html
import json
from pathlib import Path
from .engine import audit
from .io import write_csv

LABELS = {"RED":"配布不可", "YELLOW":"要確認", "GREEN":"配布可能"}

# CSVの見出し行を日本語表記にするための対応表。内部で使うキー名(英語)はここでは変えない。
ISSUES_LABELS = {"severity":"重大度", "code":"検出コード", "category":"分類", "staff_id":"職員ID",
                  "when":"日時", "floor":"フロア", "message":"内容"}
COVERAGE_LABELS = {"time":"日時", "floor":"フロア", "planned":"予定人数", "breaks":"休憩中",
                    "bath":"入浴介助中", "other":"その他離脱", "out":"応援で外出中", "incoming":"応援受入",
                    "active":"実働人数", "minimum":"最低人数", "deficit":"不足人数", "judgment":"判定",
                    "staff_ids":"職員ID"}
WORKLOAD_LABELS = {"staff_id":"職員ID", "period":"期間", "work_hours":"実働時間", "contract_hours":"契約時間"}
STAFF_DRAFT_LABELS = {"date":"日付", "weekday":"曜日", "staff_id":"職員ID", "name":"氏名",
                       "code":"勤務コード", "time":"時間", "status":"状態"}


def staff_rows(data, status):
    """職員用の勤務表(日付・職員・勤務コード)の行を作る。

    職員IDがマスタにない行・日付を読めない行は、職員用CSVから除外する(その行の問題は、
    監査の入力不正として、issues.csvとレポートに出る)。
    """
    rows, _ = staff_rows_with_excluded(data, status)
    return rows


def staff_rows_with_excluded(data, status):
    """staff_rowsと同じ行に加えて、入力不正のため除外した行の数を返す。"""
    people = {p['staff_id']:p for p in data['staff']}
    rows = []
    excluded = 0
    for row in data['schedule']:
        try:
            day = date.fromisoformat(row['date'])
        except (KeyError, ValueError, TypeError):
            excluded += 1
            continue
        if not data['config']['start'][:10] <= row['date'] < data['config']['end'][:10]:
            continue
        person = people.get(row.get('staff_id'))
        if person is None:
            excluded += 1
            continue
        spec = data['config']['shifts'].get(row['code'])
        rows.append(dict(date=row['date'], weekday='月火水木金土日'[day.weekday()],
                         staff_id=row['staff_id'], name=person['name'],
                         code=row['code'], time=(spec['start']+'～'+('翌' if spec['next_day'] else '')+spec['end']) if spec else '',
                         status='正式' if status=='GREEN' else '確認中・未確定'))
    return rows, excluded


def write_stop_report(folder, reason):
    """処理が途中で止まったとき、今回の停止理由だけを書いたreport.htmlを置く。

    前回までの正常なreport.htmlが、最新の結果のように残らないようにするため。
    """
    folder = Path(folder)
    folder.mkdir(parents=True,exist_ok=True)
    page = f'''<!doctype html><html lang="ja"><meta charset="utf-8"><title>勤務表監査 処理停止</title>
    <style>body{{font-family:system-ui,sans-serif;margin:40px;color:#23354b}}.status{{font-size:28px;color:#b42318}}</style>
    <h1>Care Shift Audit Tool V1</h1><p class="status">処理停止</p>
    <p>入力を確認できなかったため、今回は監査結果を作成していません。</p>
    <p>理由: {html.escape(str(reason))}</p>
    <p>このフォルダにあるほかのファイルは、前回までの結果が残っている可能性があります。配布に使わないでください。</p></html>'''
    (folder/'report.html').write_text(page,encoding='utf-8')


def export_report(data, folder, formal=False):
    folder = Path(folder)
    folder.mkdir(parents=True,exist_ok=True)
    result = audit(data)
    # 同じフォルダに古い正式版が残らないようにする。
    formal_path = folder/'staff_formal.csv'
    if result['status'] != 'GREEN' and formal_path.exists():
        formal_path.unlink()
    (folder/'audit.json').write_text(json.dumps(result,ensure_ascii=False,indent=2),encoding='utf-8')
    write_csv(folder/'issues.csv',result['issues'],['severity','code','category','staff_id','when','floor','message'],labels=ISSUES_LABELS)
    write_csv(folder/'coverage.csv',result['coverage'],['time','floor','planned','breaks','bath','other','out','incoming','active','minimum','deficit','judgment','staff_ids'],labels=COVERAGE_LABELS)
    write_csv(folder/'workload.csv',result['workload'],['staff_id','period','work_hours','contract_hours'],labels=WORKLOAD_LABELS)
    rows, excluded_rows = staff_rows_with_excluded(data,result['status'])
    write_csv(folder/'staff_draft.csv',rows,['date','weekday','staff_id','name','code','time','status'],labels=STAFF_DRAFT_LABELS)
    esc = lambda value: html.escape(str(value))
    table = ''.join('<tr>'+''.join('<td>'+esc(row[k])+'</td>' for k in ['severity','when','floor','staff_id','message'])+'</tr>' for row in result['issues'])
    color = {'RED':'#b42318','YELLOW':'#946200','GREEN':'#18704a'}[result['status']]
    excluded_note = f'<p>入力不正のため、職員用の勤務表から除外した行: {excluded_rows}件(内容は下の表を確認してください)</p>' if excluded_rows else ''
    page = f'''<!doctype html><html lang="ja"><meta charset="utf-8"><title>勤務表監査結果</title>
    <style>body{{font-family:system-ui,sans-serif;margin:40px;color:#23354b}}h1{{font-size:26px}}
    .status{{font-size:28px;color:{color}}}table{{border-collapse:collapse;width:100%}}td,th{{padding:9px;text-align:left;border-bottom:1px solid #ddd}}th{{background:#eaf0f5}}</style>
    <h1>Care Shift Audit Tool V1</h1><p class="status">{LABELS[result['status']]}</p>
    <p>重大NG {result['counts'].get('RED',0)}件 ／ 要確認 {result['counts'].get('YELLOW',0)}件</p>
    <p>入力変更後は再監査してください。これは保存時点の結果です。架空データ。</p>
    {excluded_note}
    <table><tr><th>区分</th><th>日時</th><th>場所</th><th>職員</th><th>理由</th></tr>{table}</table>
    <p>入力識別子: {result['input_sha256']}</p></html>'''
    (folder/'report.html').write_text(page,encoding='utf-8')
    if formal:
        if result['status']!='GREEN':
            raise ValueError('正式配布を停止しました。重大NGまたは未確認事項が残っています。')
        write_csv(formal_path,rows,['date','weekday','staff_id','name','code','time','status'],labels=STAFF_DRAFT_LABELS)
    return result
