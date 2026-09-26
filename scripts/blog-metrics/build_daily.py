#!/usr/bin/env python3
"""build_daily.py — SolarChargeController の metrics-export.sh（daily/meta サブコマンド）の
出力(JSON) と tariff.json から、Hugo の data ディレクトリ
(data/metrics/daily.json, monthly.json, meta.json) を生成する。

aggregate.sh（homelab で毎日実行）のオーケストレーションの一部で、旧実装が持っていた
power_history 等への直接 SQL 集計を置き換える。日次のkWh積分は metrics-export.sh 側
（SolarChargeController リポジトリ）が担い、本スクリプトは集計値の整形・単価付与のみを行う。

入力:
  - --daily-export PATH  metrics-export.sh <id> daily の出力(JSON配列)をそのまま保存した
    ファイル。各レコードのキー: date, solar_kwh, buy_kwh, sell_kwh, nichicon_charge_kwh,
    ecoflow_charge_kwh, status（status の意味は SolarChargeController 側の仕様に従う。
    本スクリプトは status="MISSING" の日のみ除外し、"COMPLETE"/"FINAL"/"PARTIAL"/未指定は
    採用する — 値を持たない日を勝手に切り捨てない）
  - --meta-export PATH   metrics-export.sh <id> meta の出力(JSONオブジェクト)をそのまま
    保存したファイル。キー: power_history_since, nichicon_data_since, ecoflow_data_since
  - --tariff PATH        tariff.json（電気料金の単価）
  - --today YYYY-MM-DD   meta.json の「現在の単価」表示に使う基準日（省略時は実行日。テスト用）
  - --publish-since YYYY-MM-DD  この日付以降の行のみ daily.json/monthly.json に含める
    （オーナー決定2026-09-23: 全チャネルが揃う日付より前の断片的な日次・月次は公開しない。
    省略時（None）はフィルタなし・全履歴をそのまま出力する。aggregate.sh は
    layer_model.py が自動算出した params.profile_since をこの値として渡す運用にする
    （build_daily.py 自身は5分プロファイルを扱わないため profile_since を自己算出できない）。

出力:
  - <out-dir>/daily.json
  - <out-dir>/monthly.json（daily.json を暦月で合算したもの）
  - <out-dir>/meta.json（publish_since を含む。--publish-since 省略時は null）

設計方針:
  - 標準ライブラリ + bill_model.py（同ディレクトリ、layer_model.py とも共用）のみを使用する。
  - consumption_kwh・surplus_kwh は出力しない（2026-09 の設計変更で廃止。旧仕様の
    consumption_kwh ベースの反実仮想は逆潮流時に実態の67%程度しか説明できないと判明し、
    L0 は layer_model.py の daily_load.json ベースに移行済み。layer_model.py/bill_model.py
    は daily.json からこの2キーを読んでいないことを確認済み）。
  - self_consumption_shift_kwh は nichicon_charge_kwh・ecoflow_charge_kwh が両方 null の
    日のみ null とし、それ以外は片方が欠測でも 0 とみなして合算する（SQL の
    COALESCE(n,0)+COALESCE(e,0) と同じ挙動）。
  - saving_yen（節約額試算・簡易値）= round(max(solar_kwh − sell_kwh, 0) × 買電単価
    + sell_kwh × 売電単価)。単価は「その日が属する請求月」の単価を
    bill_model.per_kwh_prices()（layer_model.py の日次per_kwh_only表示とも共用、二重実装
    しない）で求める。tariff.json に新しい月の単価が追加されても、既に確定単価を持つ
    過去日の単価・saving_yenは変わらない（QA指摘対応: 旧実装は「tariff.json中の最新月」を
    全履歴に一律適用していたため、月を追加するたびpush済みの過去日の値が動き、
    validate_metrics.py の G9(履歴不変性)がpushを拒否してパイプラインが止まっていた）。
    請求月の単価がまだ確定していない日は latest_confirmed_fuel_month() 由来の暫定単価を
    使う（bill_model.per_kwh_prices() の provisional フラグ。この暫定単価は該当月が確定
    次第、値が動きうる — run-daily.sh の allow-history-once フラグで対応する運用）。
  - 厳密な請求額再現は bill_model.py の bills.json を参照。
  - 月次は日次の各カラムを SQL の SUM(...) と同じ意味で合算する（null を無視して合計し、
    その月の全日が null のカラムのみ null にする）。saving_yen は日次値の単純合計。
  - meta.json の buy_price_yen_per_kwh 等は「現在(--today)が属する請求月」の単価を表示する
    （ダッシュボード注記の「現在の単価」表示用。過去日ごとのsaving_yenの単価とは独立）。
"""
from __future__ import annotations

import argparse
import json
from datetime import date, datetime
from pathlib import Path

import bill_model

SCRIPT_DIR = Path(__file__).resolve().parent
REPO_ROOT = SCRIPT_DIR.parent.parent

DEFAULT_TARIFF_PATH = SCRIPT_DIR / "tariff.json"
DEFAULT_OUT_DIR = REPO_ROOT / "data" / "metrics"

# metrics-export.sh daily の status がこの集合に含まれる日は出力から除外する
# （値を捏造しない。それ以外の status は採用する — PARTIAL も採用対象）。
EXCLUDED_STATUSES = {"MISSING"}

MONTHLY_SUM_KEYS = (
    "solar_kwh",
    "buy_kwh",
    "sell_kwh",
    "nichicon_charge_kwh",
    "ecoflow_charge_kwh",
    "self_consumption_shift_kwh",
)

BUY_SELL_PRICE_SOURCE = (
    "契約中の新電力プランの単価表（従量第1段階+燃料費調整+再エネ賦課金の簡易合算。"
    "容量拠出金は未含・厳密な請求額再現は下部の請求額再現表を参照。日ごとにその日が属する請求月の"
    "単価を適用する）"
)


def daily_buy_sell_price(tariff: dict, d: str) -> tuple[float, float]:
    """暦日 d(ISO文字列) が属する請求月の単価(買電単価, FIT売電単価)を返す。

    bill_model.per_kwh_prices()（layer_model.py の日次per_kwh_only表示とも共用）を
    そのまま再利用し、単価導出ロジックを二重実装しない。
    """
    meter_read_day = tariff["meter_read_day"]
    billing_month = bill_model.billing_month_for_date(date.fromisoformat(d), meter_read_day)
    buy_price, sell_fit, _sell_post_fit, _provisional, _source_month = bill_model.per_kwh_prices(tariff, billing_month)
    return buy_price, sell_fit


def build_daily_rows(daily_export: list[dict], tariff: dict) -> list[dict]:
    """metrics-export.sh daily の生レコード配列から、公開用 daily.json の行配列を作る。"""
    rows = []
    for record in daily_export:
        if record.get("status") in EXCLUDED_STATUSES:
            continue

        nichicon = record.get("nichicon_charge_kwh")
        ecoflow = record.get("ecoflow_charge_kwh")
        if nichicon is None and ecoflow is None:
            self_consumption_shift = None
        else:
            self_consumption_shift = round((nichicon or 0) + (ecoflow or 0), 3)

        solar = record["solar_kwh"]
        sell = record["sell_kwh"]
        # metrics-export.sh の daily は solar_kwh/buy_kwh/sell_kwh のいずれか1つでも非NULLなら
        # 行を返す(WHERE ... OR ...)ため、solar_kwh・sell_kwhの片方または両方がNULLの日がある
        # （例: buy_kwhだけ確定しているPARTIAL日）。両方揃わないとsaving_yenを計算できないため
        # 捏造せずNULLにする（self_consumption_shift_kwhと同じ「欠測はNULL」方針、QA指摘#3）。
        if solar is None or sell is None:
            saving_yen = None
        else:
            buy_price, sell_price = daily_buy_sell_price(tariff, record["date"])
            saving_yen = round(max(solar - sell, 0) * buy_price + sell * sell_price)

        rows.append(
            {
                "date": record["date"],
                "solar_kwh": solar,
                "buy_kwh": record["buy_kwh"],
                "sell_kwh": sell,
                "nichicon_charge_kwh": nichicon,
                "ecoflow_charge_kwh": ecoflow,
                "self_consumption_shift_kwh": self_consumption_shift,
                "saving_yen": saving_yen,
            }
        )

    rows.sort(key=lambda r: r["date"])
    return rows


def build_monthly_rows(daily_rows: list[dict]) -> list[dict]:
    """daily.json の行配列を暦月（YYYY-MM）で合算する。"""
    grouped: dict[str, dict] = {}
    for row in daily_rows:
        month = row["date"][:7]
        acc = grouped.setdefault(
            month,
            {
                "sums": {k: 0.0 for k in MONTHLY_SUM_KEYS},
                "has_value": {k: False for k in MONTHLY_SUM_KEYS},
                "saving_yen_sum": 0,
                "saving_yen_has_value": False,
            },
        )
        for key in MONTHLY_SUM_KEYS:
            value = row.get(key)
            if value is not None:
                acc["sums"][key] += value
                acc["has_value"][key] = True
        # saving_yen もQA指摘#3でNULLになりうるため、他のMONTHLY_SUM_KEYSと同じくSQLのSUMと
        # 同じ意味(NULLを無視して合計、全日NULLならNULL)で扱う。
        saving_yen = row.get("saving_yen")
        if saving_yen is not None:
            acc["saving_yen_sum"] += saving_yen
            acc["saving_yen_has_value"] = True

    monthly_rows = []
    for month in sorted(grouped):
        acc = grouped[month]
        entry: dict = {"month": month}
        for key in MONTHLY_SUM_KEYS:
            entry[key] = round(acc["sums"][key], 3) if acc["has_value"][key] else None
        entry["saving_yen"] = acc["saving_yen_sum"] if acc["saving_yen_has_value"] else None
        monthly_rows.append(entry)
    return monthly_rows


def build_meta(meta_export: dict, tariff: dict, today: date | None = None, publish_since: str | None = None) -> dict:
    """meta.json を作る。単価表示は today（省略時は実行日）が属する請求月のものを使う
    （ダッシュボードの「現在の単価」注記用。各日の saving_yen の単価とは独立した値）。

    publish_since: --publish-since でdaily.json/monthly.jsonに適用したフィルタの下限日
    （省略時null）をそのまま記録する。読者が「表示されている集計値はどの日以降か」を
    確認できるようにするため（オーナー決定2026-09-23）。"""
    if today is None:
        today = date.today()
    meter_read_day = tariff["meter_read_day"]
    billing_month = bill_model.billing_month_for_date(today, meter_read_day)
    buy_price, sell_fit, _sell_post_fit, provisional, source_month = bill_model.per_kwh_prices(tariff, billing_month)
    effective_month = source_month if provisional else billing_month

    return {
        "generated_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "buy_price_yen_per_kwh": buy_price,
        "sell_price_yen_per_kwh": sell_fit,
        "buy_sell_price_effective_month": effective_month,
        "buy_sell_price_source": BUY_SELL_PRICE_SOURCE,
        "ecoflow_data_since": meta_export.get("ecoflow_data_since"),
        "nichicon_data_since": meta_export.get("nichicon_data_since"),
        "power_history_since": meta_export.get("power_history_since"),
        "publish_since": publish_since,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--daily-export", type=Path, required=True, help="metrics-export.sh daily の出力(JSON)を保存したファイルのパス")
    parser.add_argument("--meta-export", type=Path, required=True, help="metrics-export.sh meta の出力(JSON)を保存したファイルのパス")
    parser.add_argument("--tariff", type=Path, default=DEFAULT_TARIFF_PATH, help="tariff.json のパス")
    parser.add_argument("--out-dir", type=Path, default=DEFAULT_OUT_DIR, help="daily.json/monthly.json/meta.json の出力先ディレクトリ")
    parser.add_argument("--today", type=str, default=None, help="meta.json の単価表示の基準日('YYYY-MM-DD'、省略時は実行日）。テスト用。")
    parser.add_argument(
        "--publish-since", type=str, default=None,
        help="この日付('YYYY-MM-DD')以降の行のみ daily.json/monthly.json に含める（省略時はフィルタなし）",
    )
    args = parser.parse_args()

    daily_export = json.loads(args.daily_export.read_text(encoding="utf-8"))
    meta_export = json.loads(args.meta_export.read_text(encoding="utf-8"))
    tariff = json.loads(args.tariff.read_text(encoding="utf-8"))
    today = date.fromisoformat(args.today) if args.today else None

    daily_rows = build_daily_rows(daily_export, tariff)
    if args.publish_since:
        daily_rows = [r for r in daily_rows if r["date"] >= args.publish_since]
    monthly_rows = build_monthly_rows(daily_rows)
    meta = build_meta(meta_export, tariff, today=today, publish_since=args.publish_since)

    args.out_dir.mkdir(parents=True, exist_ok=True)
    (args.out_dir / "daily.json").write_text(json.dumps(daily_rows, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    (args.out_dir / "monthly.json").write_text(json.dumps(monthly_rows, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    (args.out_dir / "meta.json").write_text(json.dumps(meta, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    print(f"wrote: {args.out_dir / 'daily.json'} ({len(daily_rows)} rows)")
    print(f"wrote: {args.out_dir / 'monthly.json'} ({len(monthly_rows)} rows)")
    print(f"wrote: {args.out_dir / 'meta.json'}")


if __name__ == "__main__":
    main()
