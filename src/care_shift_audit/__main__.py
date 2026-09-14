"""使い方: python -m care_shift_audit data/baseline --out reports/baseline"""
import argparse
import sys
from pathlib import Path
from .io import load_excel, load_folder
from .output import export_report, LABELS


def main():
    parser = argparse.ArgumentParser(description='勤務表の実配置・休憩・勤務制限を監査')
    parser.add_argument('input',type=Path,help='CSVフォルダまたはV1管理者Excel')
    parser.add_argument('--out',type=Path,default=Path('reports/current'))
    parser.add_argument('--formal',action='store_true',help='正式配布を要求（未確認があれば停止）')
    args = parser.parse_args()
    # 読込失敗時にも古い正式出力を最新版として残さない。
    previous_formal=args.out/'staff_formal.csv'
    if previous_formal.exists():
        previous_formal.unlink()
    try:
        data = load_excel(args.input) if args.input.suffix.lower()=='.xlsx' else load_folder(args.input)
        result = export_report(data,args.out,args.formal)
    except (ValueError, KeyError, TypeError, OSError) as exc:
        print(f'処理停止: {exc}',file=sys.stderr)
        return 2
    print(f"{LABELS[result['status']]} | 重大NG {result['counts'].get('RED',0)}件 | 要確認 {result['counts'].get('YELLOW',0)}件")
    print(f'結果: {args.out / "report.html"}')
    return 0


if __name__=='__main__':
    sys.exit(main())
