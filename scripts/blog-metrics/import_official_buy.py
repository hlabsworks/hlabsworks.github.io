#!/usr/bin/env python3
"""import_official_buy.py — energy-archive（プライベートリポジトリ）の
japaden/derived/bill_breakdown.json から、公開しても問題ない集計値のみを抽出して
data/metrics/official_buy.json を生成する（import_official_sell.py と同型）。

入力元の bill_breakdown.json には供給地点特定番号（トップレベルの "_source" フィールド）・
検針前指示数(prev_meter)等の契約識別情報が含まれるため、それらは一切読み込んだ値を
そのまま出力しない。抽出するのは各月レコード(months[])のうち以下の集計値のみ:
  - settlement_month（請求月 'YYYY-MM'。bill_breakdown.json の billing_ym と同じ）
  - period_from / period_to（使用期間、毎月2日〜翌月1日。bill_breakdown.json の
    period（日本語表記 '2025年7月2日 ～ 2025年8月1日'）をISO日付に変換する）
  - official_buy_kwh（請求書に実際に適用された使用量 usage_kwh。段階制電力量料金の
    元になった値そのもの。meter_diff（検針差分の生値）ではなく usage_kwh を採用する
    のは、compute_bill(usage_kwh) が billed_yen と完全一致することを
    ReconciledMonthsTest で確認済みのため — 買電量の請求実績としてこれが最も正確）
  - billed_yen（請求明細PDFの請求総額。bill_model.py 側の compute_bill() の
    突合・表示用。供給地点番号等は含まない）

入力元ファイルが見つからない場合は既存の data/metrics/official_buy.json を
一切変更せず、エラー終了する（値を捏造しない）。

使い方:
  python3 import_official_buy.py [--source PATH] [--out PATH]
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from datetime import datetime
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
REPO_ROOT = SCRIPT_DIR.parent.parent

DEFAULT_SOURCE_PATH = Path.home() / "Develop" / "energy-archive" / "japaden" / "derived" / "bill_breakdown.json"
DEFAULT_OUT_PATH = REPO_ROOT / "data" / "metrics" / "official_buy.json"

REQUIRED_MONTH_FIELDS = ("billing_ym", "period", "usage_kwh", "billed_yen")

# '2025年7月2日 ～ 2025年8月1日' 形式を ISO 日付範囲に変換する。
_PERIOD_RE = re.compile(
    r"(?P<fy>\d{4})年(?P<fm>\d{1,2})月(?P<fd>\d{1,2})日\s*[～~\-]\s*"
    r"(?P<ty>\d{4})年(?P<tm>\d{1,2})月(?P<td>\d{1,2})日"
)


def _parse_period(period: str) -> tuple[str, str]:
    m = _PERIOD_RE.match(period.strip())
    if not m:
        raise ValueError(f"period の形式を解釈できません: {period!r}")
    period_from = f"{int(m['fy']):04d}-{int(m['fm']):02d}-{int(m['fd']):02d}"
    period_to = f"{int(m['ty']):04d}-{int(m['tm']):02d}-{int(m['td']):02d}"
    return period_from, period_to


def extract_month(month: dict) -> dict:
    """bill_breakdown.json の1ヶ月分レコードから、公開可能な集計値のみを抽出する。"""
    for field in REQUIRED_MONTH_FIELDS:
        if field not in month:
            raise KeyError(f"bill_breakdown.json の月次レコードに必須フィールド '{field}' がありません: {month}")

    period_from, period_to = _parse_period(month["period"])

    return {
        "settlement_month": month["billing_ym"],
        "period_from": period_from,
        "period_to": period_to,
        "official_buy_kwh": month["usage_kwh"],
        "billed_yen": month["billed_yen"],
    }


def extract_months(source: dict) -> list[dict]:
    """bill_breakdown.json 全体（dict）から公開可能な月次レコードの一覧を作る。"""
    months = source.get("months")
    if months is None:
        raise KeyError("bill_breakdown.json に 'months' 配列がありません")
    return [extract_month(m) for m in months]


def build_official_buy(source: dict) -> dict:
    return {
        "months": extract_months(source),
        "generated_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "source_note": (
            "Japan電力 請求明細PDF（pdftotext抽出）の実使用量(usage_kwh)・請求総額。"
            "供給地点特定番号・検針前指示数等の契約識別情報は含まない。"
        ),
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--source", type=Path, default=DEFAULT_SOURCE_PATH, help="bill_breakdown.json のパス（既定: energy-archive 配下）")
    parser.add_argument("--out", type=Path, default=DEFAULT_OUT_PATH, help="出力先 official_buy.json のパス")
    args = parser.parse_args()

    if not args.source.exists():
        print(f"import_official_buy.py: 入力ファイルが見つかりません: {args.source}", file=sys.stderr)
        sys.exit(1)

    source = json.loads(args.source.read_text(encoding="utf-8"))
    result = build_official_buy(source)

    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(f"wrote: {args.out} ({len(result['months'])} months)")


if __name__ == "__main__":
    main()
