#!/usr/bin/env python3
"""import_official_sell.py — energy-archive（プライベートリポジトリ）の
tepco-fit/purchase_monthly.json から、公開しても問題ない集計値のみを抽出して
data/metrics/official_sell.json を生成する。

入力元の purchase_monthly.json には供給地点番号・設備ID・検針日・指示数（積算計器の
生の指示数）等の契約識別情報が含まれるため、それらは一切読み込んだ値をそのまま
出力しない。抽出するのは以下の集計値のみ:
  - settlement_month（精算月 'YYYY-MM'）
  - period_from / period_to（算定期間、毎月2日〜翌月1日）
  - official_sell_kwh（公式メーターの指示数差分 kwh_from_meter_diff。null の月は
    購入金額から逆算した purchase_kwh_from_yen で代替する）
  - sell_revenue_yen（東京電力パワーグリッドから実際に支払われた売電収入額。
    全月で purchase_yen == 16円 × purchase_kwh_from_yen が厳密一致することを確認済み
    — VERIFIED 2026-09-05、tepco-fit/purchase_monthly.json の12ヶ月分で検算）。

入力元ファイルが見つからない場合は既存の data/metrics/official_sell.json を
一切変更せず、エラー終了する（値を捏造しない）。

使い方:
  python3 import_official_sell.py [--source PATH] [--out PATH]
"""
from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
REPO_ROOT = SCRIPT_DIR.parent.parent

DEFAULT_SOURCE_PATH = Path.home() / "Develop" / "energy-archive" / "tepco-fit" / "purchase_monthly.json"
DEFAULT_OUT_PATH = REPO_ROOT / "data" / "metrics" / "official_sell.json"

REQUIRED_MONTH_FIELDS = ("settlement_month", "period_from", "period_to", "purchase_yen")


def extract_month(month: dict) -> dict:
    """purchase_monthly.json の1ヶ月分レコードから、公開可能な集計値のみを抽出する。"""
    for field in REQUIRED_MONTH_FIELDS:
        if field not in month:
            raise KeyError(f"purchase_monthly.json の月次レコードに必須フィールド '{field}' がありません: {month}")

    official_sell_kwh = month.get("kwh_from_meter_diff")
    if official_sell_kwh is None:
        if "purchase_kwh_from_yen" not in month:
            raise KeyError(
                f"kwh_from_meter_diff が null かつ purchase_kwh_from_yen も無い月があります: {month}"
            )
        official_sell_kwh = month["purchase_kwh_from_yen"]

    return {
        "settlement_month": month["settlement_month"],
        "period_from": month["period_from"],
        "period_to": month["period_to"],
        "official_sell_kwh": official_sell_kwh,
        "sell_revenue_yen": month["purchase_yen"],
    }


def extract_months(source: dict) -> list[dict]:
    """purchase_monthly.json 全体（dict）から公開可能な月次レコードの一覧を作る。"""
    months = source.get("months")
    if months is None:
        raise KeyError("purchase_monthly.json に 'months' 配列がありません")
    return [extract_month(m) for m in months]


def build_official_sell(source: dict) -> dict:
    return {
        "months": extract_months(source),
        "generated_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "source_note": (
            "東京電力パワーグリッド 購入実績お知らせサービスの公式メーター指示数差分・"
            "支払実績。供給地点番号・設備ID・検針日・指示数の生値は含まない。"
        ),
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--source", type=Path, default=DEFAULT_SOURCE_PATH, help="purchase_monthly.json のパス（既定: energy-archive 配下）")
    parser.add_argument("--out", type=Path, default=DEFAULT_OUT_PATH, help="出力先 official_sell.json のパス")
    args = parser.parse_args()

    if not args.source.exists():
        print(f"import_official_sell.py: 入力ファイルが見つかりません: {args.source}", file=sys.stderr)
        sys.exit(1)

    source = json.loads(args.source.read_text(encoding="utf-8"))
    result = build_official_sell(source)

    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(f"wrote: {args.out} ({len(result['months'])} months)")


if __name__ == "__main__":
    main()
