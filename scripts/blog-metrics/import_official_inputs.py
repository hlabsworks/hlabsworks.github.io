#!/usr/bin/env python3
"""import_official_inputs.py — 自動取得側（homelab、private）が作った
bill_breakdown.json（import_official_buy.py と同型の入力）・purchase_monthly.json
（import_official_sell.py と同型の入力）から、公開可能な集計値だけを抽出し、
請求総額との突合（compute_bill(merge(base, 候補), usage_kwh, M).total_yen == billed_yen）
が一致した請求月だけを inputs/{official_buy,official_sell,tariff_months}.json として
出力する。

設計（DDR §「月次確定の自動化」・実装手順S1）:
  - 料金体系の骨格（--base-tariff、main の tariff.json）は変更しない。月別に観測した
    燃料費等調整単価(fuel_rate)・容量拠出金(capacity_yen)・賦課金(levy_rate)は
    bill_breakdown.json の各月レコードから読み、tariff_months.json の候補として
    bill_model.merge_tariff() に通す。base と矛盾する（値が違う）月・base に既に
    確定している月と競合する月は候補から外す（捏造・上書きをしない）。
  - capacity_yen が整数でない月は候補から除外する（parse_incomplete）。
  - 候補で merge した実効tariffの compute_bill().total_yen が bill_breakdown.json の
    billed_yen と一致した月だけ tariff_months.json・official_buy.json に採用する。
    不一致は excluded_months に理由コードで記録し、official_buy.json からも除く
    （値を捏造しない、既存 import_official_buy.py と同じ方針）。
  - official_sell.json は import_official_sell.build_official_sell() をそのまま使う
    （買電側のような突合対象が無いため、売電側は購入実績をそのまま採用する）。
    purchase_monthly.json にあって bill_breakdown.json 側で採用されなかった月は
    missing_official_buy として excluded_months に記録する（買電側の請求突合が
    無いまま売電側だけ確定させないための印。tariff_months.json自体には影響しない）。
  - 出力が変わらなければ（generated_at を除く）ファイルを書き換えない
    （run-daily.sh の compute_hashes が「実質変更なし」を検知できるようにするため）。

使い方:
  python3 import_official_inputs.py --bill-breakdown PATH --purchase-monthly PATH \\
      --base-tariff PATH --out-dir DIR
"""
from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPT_DIR))
import bill_model  # noqa: E402  merge_tariff/reconcile_billを二重実装しない
import import_official_buy  # noqa: E402  extract_month/SOURCE_NOTEを再利用する
import import_official_sell  # noqa: E402  build_official_sell/SOURCE_NOTEを再利用する

# excluded_months の理由コード（validate_metrics.py の G18 と共有する列挙値）。
EXCLUDED_REASON_MISMATCH = "reconcile_mismatch"
EXCLUDED_REASON_LEVY_CONFLICT = "levy_conflict"
EXCLUDED_REASON_PARSE_INCOMPLETE = "parse_incomplete"
EXCLUDED_REASON_MISSING_OFFICIAL_BUY = "missing_official_buy"


def _levy_key(billing_month: str) -> str:
    return f"{billing_month}..{billing_month}"


def _build_candidate_overlay(month: dict) -> dict | None:
    """bill_breakdown.json の1ヶ月分レコードから tariff_months.json の候補
    (fuel_cost_adjustment_yen_per_kwh/capacity_contribution_yen_per_month/
    renewable_levy_yen_per_kwh_observed、いずれも当該月1件のみ)を作る。
    billing_ym・fuel_rate・capacity_yen・levy_rate のいずれかが欠けている、または
    capacity_yen が整数でない場合は None を返す（値を捏造しない）。"""
    billing_ym = month.get("billing_ym")
    fuel_rate = month.get("fuel_rate")
    capacity_yen = month.get("capacity_yen")
    levy_rate = month.get("levy_rate")
    if billing_ym is None or fuel_rate is None or capacity_yen is None or levy_rate is None:
        return None
    if isinstance(capacity_yen, bool) or not isinstance(capacity_yen, int):
        return None
    return {
        "fuel_cost_adjustment_yen_per_kwh": {billing_ym: fuel_rate},
        "capacity_contribution_yen_per_month": {billing_ym: capacity_yen},
        "renewable_levy_yen_per_kwh_observed": {_levy_key(billing_ym): levy_rate},
    }


def build_inputs(base_tariff: dict, bill_breakdown: dict, purchase_monthly: dict) -> dict:
    """戻り値: {"official_buy": dict|None, "official_sell": dict|None,
    "tariff_months": dict, "excluded_months": {billing_month: reason}}。
    official_buy/official_sellはmonths内容(いずれも0件ならNone、generated_at/source_noteは
    呼び出し側で付与する)、tariff_monthsはschema_version以外の各フィールド。"""
    fuel_table: dict[str, float] = {}
    capacity_table: dict[str, int] = {}
    levy_table: dict[str, float] = {}
    excluded: dict[str, str] = {}
    official_buy_months: list[dict] = []

    for month in bill_breakdown.get("months", []):
        try:
            extracted = import_official_buy.extract_month(month)
        except (KeyError, ValueError):
            billing_ym = month.get("billing_ym")
            if billing_ym:
                excluded[billing_ym] = EXCLUDED_REASON_PARSE_INCOMPLETE
            continue
        billing_month = extracted["settlement_month"]

        overlay = _build_candidate_overlay(month)
        if overlay is None:
            excluded[billing_month] = EXCLUDED_REASON_PARSE_INCOMPLETE
            continue

        try:
            candidate = bill_model.merge_tariff(base_tariff, overlay)
        except ValueError:
            excluded[billing_month] = EXCLUDED_REASON_LEVY_CONFLICT
            continue

        diff = bill_model.reconcile_bill(candidate, billing_month, extracted["official_buy_kwh"], extracted["billed_yen"])
        if diff != 0:
            excluded[billing_month] = EXCLUDED_REASON_MISMATCH
            continue

        official_buy_months.append(extracted)
        fuel_table[billing_month] = overlay["fuel_cost_adjustment_yen_per_kwh"][billing_month]
        capacity_table[billing_month] = overlay["capacity_contribution_yen_per_month"][billing_month]
        levy_table[_levy_key(billing_month)] = overlay["renewable_levy_yen_per_kwh_observed"][_levy_key(billing_month)]

    official_buy_months.sort(key=lambda m: m["settlement_month"])
    buy_months = {m["settlement_month"] for m in official_buy_months}

    official_sell = import_official_sell.build_official_sell(purchase_monthly)
    for month in official_sell.get("months", []):
        sm = month["settlement_month"]
        if sm not in buy_months and sm not in excluded:
            excluded[sm] = EXCLUDED_REASON_MISSING_OFFICIAL_BUY

    tariff_months = {
        "schema_version": 1,
        "fuel_cost_adjustment_yen_per_kwh": dict(sorted(fuel_table.items())),
        "capacity_contribution_yen_per_month": dict(sorted(capacity_table.items())),
        "renewable_levy_yen_per_kwh_observed": dict(sorted(levy_table.items())),
        "excluded_months": dict(sorted(excluded.items())),
    }

    official_buy = None
    if official_buy_months:
        official_buy = {"months": official_buy_months, "source_note": import_official_buy.SOURCE_NOTE}

    official_sell_out = None
    if official_sell.get("months"):
        official_sell_out = {"months": official_sell["months"], "source_note": import_official_sell.SOURCE_NOTE}

    return {
        "official_buy": official_buy,
        "official_sell": official_sell_out,
        "tariff_months": tariff_months,
        "excluded_months": tariff_months["excluded_months"],
    }


def _strip_generated_at(data: dict) -> dict:
    return {k: v for k, v in data.items() if k != "generated_at"}


def _write_if_changed(path: Path, data: dict, generated_at: str, written: list[str]) -> None:
    """既存ファイルの内容が(generated_at を除いて)同じなら書き込まない。
    run-daily.sh の compute_hashes が generated_at を除いたsha256で「実質変更なし」を
    検知する仕組みと対になる（同じ判断を出力側でも先取りし、無用なコミットを増やさない）。"""
    out = dict(data)
    out["generated_at"] = generated_at
    if path.exists():
        try:
            existing = json.loads(path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            existing = None
        if existing is not None and _strip_generated_at(existing) == _strip_generated_at(out):
            return
    path.write_text(json.dumps(out, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    written.append(str(path))


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--bill-breakdown", type=Path, required=True, help="bill_breakdown.json のパス（import_official_buy.py と同型）")
    parser.add_argument("--purchase-monthly", type=Path, required=True, help="purchase_monthly.json のパス（import_official_sell.py と同型）")
    parser.add_argument("--base-tariff", type=Path, required=True, help="main の tariff.json のパス（料金体系の骨格、変更しない）")
    parser.add_argument("--out-dir", type=Path, required=True, help="official_buy.json/official_sell.json/tariff_months.json の出力先ディレクトリ")
    args = parser.parse_args()

    for path, label in (
        (args.bill_breakdown, "--bill-breakdown"),
        (args.purchase_monthly, "--purchase-monthly"),
        (args.base_tariff, "--base-tariff"),
    ):
        if not path.exists():
            print(f"import_official_inputs.py: {label} が見つかりません: {path}", file=sys.stderr)
            sys.exit(1)

    base_tariff = json.loads(args.base_tariff.read_text(encoding="utf-8"))
    bill_breakdown = json.loads(args.bill_breakdown.read_text(encoding="utf-8"))
    purchase_monthly = json.loads(args.purchase_monthly.read_text(encoding="utf-8"))

    result = build_inputs(base_tariff, bill_breakdown, purchase_monthly)
    generated_at = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    args.out_dir.mkdir(parents=True, exist_ok=True)
    written: list[str] = []
    _write_if_changed(args.out_dir / "tariff_months.json", result["tariff_months"], generated_at, written)
    if result["official_buy"] is not None:
        _write_if_changed(args.out_dir / "official_buy.json", result["official_buy"], generated_at, written)
    if result["official_sell"] is not None:
        _write_if_changed(args.out_dir / "official_sell.json", result["official_sell"], generated_at, written)

    summary = {"excluded": result["excluded_months"], "written": written}
    print(json.dumps(summary, ensure_ascii=False, sort_keys=True))


if __name__ == "__main__":
    main()
