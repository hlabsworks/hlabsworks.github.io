#!/usr/bin/env python3
"""import_official_inputs.py — 自動取得側（homelab、private）が作った
bill_breakdown.json（import_official_buy.py と同型の入力）・purchase_monthly.json
（import_official_sell.py と同型の入力）から、公開可能な集計値だけを抽出し、
請求総額との突合（compute_bill(merge(base, 候補), usage_kwh, M).total_yen == billed_yen）
が一致した請求月だけを inputs/{official_buy,official_sell,tariff_months}.json として
出力する。

設計（DDR §「月次確定の自動化」・実装手順S1）:
  - 料金体系の骨格（--base-tariff、main の tariff.json）は変更しない。base で既に確定して
    いる月（fuel_cost_adjustment_yen_per_kwh と capacity_contribution_yen_per_month の
    両方が base にある月）は tariff_months.json に二重登録しない（QA指摘F3(a)、
    DDR「両方には登録しない」）。base で未確定の月だけ、bill_breakdown.json の各月
    レコードから読んだ燃料費等調整単価(fuel_rate)・容量拠出金(capacity_yen)・
    賦課金(levy_rate)を tariff_months.json の候補として bill_model.merge_tariff() に通す。
    base と矛盾する（値が違う）月は候補から外す（捏造・上書きをしない）。
  - capacity_yen が整数値でない（小数部を持つ、bool、文字列等）月は候補から除外する
    （parse_incomplete）。実データがfloat(例: 213.0)で来ても整数値と等しければintとして
    受け付ける（QA指摘、S2との契約の食い違い対応）。
  - base で既に確定している月は base 自身で、そうでない月は候補を merge した実効tariffで、
    それぞれ compute_bill().total_yen が bill_breakdown.json の billed_yen と一致した月だけ
    official_buy.json に採用する（tariff_months.json への採用は base 未確定の月のみ）。
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
# QA指摘L4: fuel/capacityの競合(base既存値と観測値が食い違う)はlevy_conflictとは別の
# 理由コードにする（levy_conflictは賦課金レンジの競合専用に絞る）。
EXCLUDED_REASON_TARIFF_CONFLICT = "tariff_conflict"


def _levy_key(billing_month: str) -> str:
    return f"{billing_month}..{billing_month}"


def _build_candidate_overlay(month: dict) -> dict | None:
    """bill_breakdown.json の1ヶ月分レコードから tariff_months.json の候補
    (fuel_cost_adjustment_yen_per_kwh/capacity_contribution_yen_per_month/
    renewable_levy_yen_per_kwh_observed、いずれも当該月1件のみ)を作る。
    billing_ym・fuel_rate・capacity_yen・levy_rate のいずれかが欠けている、または
    capacity_yen が整数値でない場合は None を返す（値を捏造しない）。

    QA指摘（S2のQAで判明、リポジトリ間の契約の食い違い）: 実データの bill_breakdown.json は
    capacity_yen が整数値の float（例: 213.0、私有の抽出元がそのまま転記するため）で
    渡ってくる。整数値と等しい float（bool・NaN・Inf・小数部を持つ値は除く）は int に
    変換して受け付ける（tariff_months.json の capacity_contribution_yen_per_month は
    G18(validate_metrics.py) が厳密 int を要求するスキーマのため、ここで正規化する）。"""
    billing_ym = month.get("billing_ym")
    fuel_rate = month.get("fuel_rate")
    capacity_yen = month.get("capacity_yen")
    levy_rate = month.get("levy_rate")
    if billing_ym is None or fuel_rate is None or capacity_yen is None or levy_rate is None:
        return None
    if isinstance(capacity_yen, bool):
        return None
    if isinstance(capacity_yen, int):
        capacity_yen_int = capacity_yen
    elif isinstance(capacity_yen, float) and capacity_yen.is_integer():
        capacity_yen_int = int(capacity_yen)
    else:
        return None
    return {
        "fuel_cost_adjustment_yen_per_kwh": {billing_ym: fuel_rate},
        "capacity_contribution_yen_per_month": {billing_ym: capacity_yen_int},
        "renewable_levy_yen_per_kwh_observed": {_levy_key(billing_ym): levy_rate},
    }


def build_inputs(base_tariff: dict, bill_breakdown: dict, purchase_monthly: dict) -> dict:
    """戻り値: {"official_buy": dict|None, "official_sell": dict|None,
    "tariff_months": dict, "excluded_months": {billing_month: reason}}。
    official_buy/official_sellはmonths内容(いずれも0件ならNone、generated_at/source_noteは
    呼び出し側で付与する)、tariff_monthsはschema_version以外の各フィールド。

    QA指摘F3(a)（DDR「両方には登録しない」）: base で既に確定している月（fuel と capacity が
    両方 base にある月）は tariff_months.json に登録しない。official_buy.json は従来どおり
    全月（reconcileできた月）が対象。"""
    fuel_table: dict[str, float] = {}
    capacity_table: dict[str, int] = {}
    levy_table: dict[str, float] = {}
    excluded: dict[str, str] = {}
    official_buy_months: list[dict] = []
    base_confirmed = bill_model.confirmed_tariff_months(base_tariff)

    for month in bill_breakdown.get("months", []):
        try:
            extracted = import_official_buy.extract_month(month)
        except (KeyError, ValueError):
            billing_ym = month.get("billing_ym")
            if billing_ym:
                excluded[billing_ym] = EXCLUDED_REASON_PARSE_INCOMPLETE
            continue
        billing_month = extracted["settlement_month"]

        if billing_month in base_confirmed:
            # base単独で既に確定している月は、tariff_monthsに二重登録せずbase自身で
            # 直接突合する（official_buyは従来どおり対象）。
            diff = bill_model.reconcile_bill(base_tariff, billing_month, extracted["official_buy_kwh"], extracted["billed_yen"])
            if diff != 0:
                excluded[billing_month] = EXCLUDED_REASON_MISMATCH
                continue
            official_buy_months.append(extracted)
            continue

        overlay = _build_candidate_overlay(month)
        if overlay is None:
            excluded[billing_month] = EXCLUDED_REASON_PARSE_INCOMPLETE
            continue

        try:
            candidate = bill_model.merge_tariff(base_tariff, overlay)
        except ValueError as exc:
            # QA指摘L4: fuel/capacityの競合とlevyの競合で理由コードを分ける。
            excluded[billing_month] = (
                EXCLUDED_REASON_LEVY_CONFLICT if "renewable_levy_yen_per_kwh" in str(exc) else EXCLUDED_REASON_TARIFF_CONFLICT
            )
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
