#!/usr/bin/env python3
"""bill_model.py — 公開単価(tariff.json)を使い、請求期間（毎月2日〜翌月1日）ベースで
Japan電力 くらしプランS の推定請求額を再現し、太陽光なし(L0)反実仮想との差分から
節約額（FIT実態 / 卒FIT換算）を算出する。

入力:
  - scripts/blog-metrics/tariff.json （公開単価データ、手動更新）
  - data/metrics/daily.json          （aggregate.sh の生成物。暦日ごとの実測 kWh）
出力:
  - data/metrics/bills.json

設計方針:
  - 標準ライブラリのみを使用する（外部依存を追加しない）。
  - 請求期間（毎月2日〜翌月1日）の全日分のデータが daily.json に揃っている月のみ
    bills.json の "months" に含める。データ欠測・tariff.json 側の単価未収載の月は
    "excluded_months" に理由付きで列挙し、値を捏造しない。
  - 容量拠出金は契約電力(kW)が未確定（tariff.json の contract_capacity_amp が null）の間は
    0円として計上し、"capacity_unknown": true を明示する。契約電力が確定したら
    tariff.json を更新するだけで自動的に計上されるようにする。
  - 円換算は各内訳項目ごとに四捨五入(ROUND_HALF_UP)し、合計はその内訳の総和とする
    （実際の請求書の端数処理方式と厳密には一致しない可能性がある。ASSUMED）。
  - L0（太陽光なし反実仮想）は「消費電力(consumption_kwh)の全量を買電した」とみなす。
    L1/L2/L3（太陽光のみ／蓄電池／ポータブル電源の層別）は SolarChargeController 側の
    日次ロールアップが未整備のため準備中。現状の "bill_actual" は実測（太陽光+蓄電池+
    ポータブル電源の結果）であり、L1 単体の値ではない。
"""
from __future__ import annotations

import argparse
import json
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from decimal import ROUND_HALF_UP, Decimal
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
REPO_ROOT = SCRIPT_DIR.parent.parent

DEFAULT_TARIFF_PATH = SCRIPT_DIR / "tariff.json"
DEFAULT_DAILY_PATH = REPO_ROOT / "data" / "metrics" / "daily.json"
DEFAULT_OUT_PATH = REPO_ROOT / "data" / "metrics" / "bills.json"


def _round_yen(value: float) -> int:
    """円単位に四捨五入する（ROUND_HALF_UP）。"""
    return int(Decimal(str(value)).quantize(Decimal("1"), rounding=ROUND_HALF_UP))


def _parse_ym(ym: str) -> int:
    """'YYYY-MM' を年*12+月 の通し月数に変換する（範囲比較用）。"""
    y, m = ym.split("-")
    return int(y) * 12 + int(m)


def _add_month(year: int, month: int, delta: int) -> tuple[int, int]:
    idx = year * 12 + (month - 1) + delta
    return idx // 12, idx % 12 + 1


def billing_period(billing_month: str, meter_read_day: int) -> tuple[date, date]:
    """billing_month('YYYY-MM': 使用分の月) の請求期間 [start, end]（両端含む）を返す。

    tariff.json の meter_read_day（既定2）に基づき、使用期間は
    「前月の meter_read_day 日 〜 当月の (meter_read_day - 1) 日」。
    例: billing_month="2026-08", meter_read_day=2 → 2026-07-02 〜 2026-08-01。
    """
    if meter_read_day < 2:
        raise ValueError(f"meter_read_day は2以上を想定（実際: {meter_read_day}）")
    year, month = (int(x) for x in billing_month.split("-"))
    end = date(year, month, meter_read_day - 1)
    prev_year, prev_month = _add_month(year, month, -1)
    start = date(prev_year, prev_month, meter_read_day)
    return start, end


def renewable_levy_rate(tariff: dict, billing_month: str) -> float:
    """再エネ賦課金単価(円/kWh)を、billing_month を含む年度レンジから引く。"""
    target = _parse_ym(billing_month)
    for rng, rate in tariff["renewable_levy_yen_per_kwh"].items():
        if ".." not in rng:
            continue
        lo, hi = rng.split("..")
        if _parse_ym(lo) <= target <= _parse_ym(hi):
            return rate
    raise KeyError(f"renewable_levy_yen_per_kwh に {billing_month} を含む期間が見つかりません")


def tiered_energy_charge(tiers: list[dict], kwh: float) -> float:
    """段階制の電力量料金を計算する。tiers は up_to_kwh 昇順（最後の要素は null=上限なし）を想定。"""
    total = 0.0
    remaining = kwh
    prev_cap = 0.0
    for tier in tiers:
        cap = tier["up_to_kwh"]
        rate = tier["yen_per_kwh"]
        if cap is None:
            total += remaining * rate
            remaining = 0.0
            break
        width = cap - prev_cap
        portion = min(remaining, width) if remaining > 0 else 0.0
        if portion > 0:
            total += portion * rate
            remaining -= portion
        prev_cap = cap
        if remaining <= 0:
            break
    return total


@dataclass
class BillBreakdown:
    buy_kwh: float
    basic_fee_yen: int
    energy_charge_yen: int
    fuel_adjustment_yen: int
    renewable_levy_yen: int
    capacity_contribution_yen: int
    capacity_unknown: bool
    total_yen: int

    def to_dict(self) -> dict:
        return {
            "buy_kwh": round(self.buy_kwh, 3),
            "basic_fee_yen": self.basic_fee_yen,
            "energy_charge_yen": self.energy_charge_yen,
            "fuel_adjustment_yen": self.fuel_adjustment_yen,
            "renewable_levy_yen": self.renewable_levy_yen,
            "capacity_contribution_yen": self.capacity_contribution_yen,
            "capacity_unknown": self.capacity_unknown,
            "total_yen": self.total_yen,
        }


def compute_bill(tariff: dict, buy_kwh: float, billing_month: str) -> BillBreakdown:
    """buy_kwh(当該請求期間の買電量kWh)から tariff.json の公開単価で請求額を再現する。"""
    if buy_kwh < 0:
        raise ValueError(f"buy_kwh は非負を想定（実際: {buy_kwh}）")

    basic_fee = tariff["basic_fee_yen_per_month"]
    energy_charge = tiered_energy_charge(tariff["energy_tiers_yen_per_kwh"], buy_kwh)

    fuel_adj_table = tariff["fuel_cost_adjustment_yen_per_kwh"]
    if billing_month not in fuel_adj_table:
        raise KeyError(f"fuel_cost_adjustment_yen_per_kwh に {billing_month} がありません")
    fuel_adjustment = buy_kwh * fuel_adj_table[billing_month]["applied"]

    levy_rate = renewable_levy_rate(tariff, billing_month)
    renewable_levy = buy_kwh * levy_rate

    capacity_table = tariff["capacity_contribution_yen_per_kw_month"]
    contract_amp = capacity_table.get("contract_capacity_amp")
    capacity_unknown = contract_amp is None
    if capacity_unknown:
        capacity_contribution = 0.0
    else:
        rate = capacity_table.get(billing_month)
        if rate is None:
            raise KeyError(f"capacity_contribution_yen_per_kw_month に {billing_month} がありません")
        contract_kw = contract_amp * 100 / 1000  # 低圧: 契約電力(kW) = 契約容量(A) × 100V / 1000
        capacity_contribution = rate * contract_kw

    basic_fee_yen = _round_yen(basic_fee)
    energy_charge_yen = _round_yen(energy_charge)
    fuel_adjustment_yen = _round_yen(fuel_adjustment)
    renewable_levy_yen = _round_yen(renewable_levy)
    capacity_contribution_yen = _round_yen(capacity_contribution)
    total_yen = (
        basic_fee_yen
        + energy_charge_yen
        + fuel_adjustment_yen
        + renewable_levy_yen
        + capacity_contribution_yen
    )
    return BillBreakdown(
        buy_kwh=buy_kwh,
        basic_fee_yen=basic_fee_yen,
        energy_charge_yen=energy_charge_yen,
        fuel_adjustment_yen=fuel_adjustment_yen,
        renewable_levy_yen=renewable_levy_yen,
        capacity_contribution_yen=capacity_contribution_yen,
        capacity_unknown=capacity_unknown,
        total_yen=total_yen,
    )


def load_daily(path: Path) -> dict[str, dict]:
    rows = json.loads(path.read_text(encoding="utf-8"))
    return {r["date"]: r for r in rows}


def _daterange(start: date, end: date):
    d = start
    while d <= end:
        yield d
        d += timedelta(days=1)


def sum_period(daily_by_date: dict[str, dict], start: date, end: date, field_name: str) -> tuple[float, int, int]:
    """[start, end] の指定フィールドを合計する。戻り値: (合計, 揃っている日数, 欠測日数)。"""
    total = 0.0
    present = 0
    missing = 0
    for d in _daterange(start, end):
        row = daily_by_date.get(d.isoformat())
        if row is None or row.get(field_name) is None:
            missing += 1
            continue
        total += row[field_name]
        present += 1
    return total, present, missing


def list_candidate_billing_months(min_date: date, max_date: date) -> list[str]:
    """daily.json の日付範囲から、判定対象になりうる請求月候補を列挙する。"""
    months = []
    y, m = min_date.year, min_date.month
    end_y, end_m = max_date.year, max_date.month
    while (y, m) <= (end_y, end_m):
        months.append(f"{y:04d}-{m:02d}")
        y, m = _add_month(y, m, 1)
    months.append(f"{y:04d}-{m:02d}")  # 請求期間の終端が max_date の翌月にまたがるケースを含める
    return months


def build_month_record(tariff: dict, daily_by_date: dict, billing_month: str) -> dict:
    meter_read_day = tariff["meter_read_day"]
    start, end = billing_period(billing_month, meter_read_day)
    total_days = (end - start).days + 1

    buy_kwh, _, buy_missing = sum_period(daily_by_date, start, end, "buy_kwh")
    if buy_missing > 0:
        return {
            "billing_month": billing_month,
            "excluded": True,
            "reason": f"usage period {start.isoformat()}..{end.isoformat()} に daily.json 欠測 {buy_missing}/{total_days} 日",
        }

    if billing_month not in tariff["fuel_cost_adjustment_yen_per_kwh"]:
        return {
            "billing_month": billing_month,
            "excluded": True,
            "reason": f"tariff.json の fuel_cost_adjustment_yen_per_kwh に {billing_month} が未収載",
        }

    consumption_kwh, _, cons_missing = sum_period(daily_by_date, start, end, "consumption_kwh")
    if cons_missing > 0:
        return {
            "billing_month": billing_month,
            "excluded": True,
            "reason": f"usage period に consumption_kwh 欠測 {cons_missing}/{total_days} 日",
        }

    solar_kwh, _, _ = sum_period(daily_by_date, start, end, "solar_kwh")
    sell_kwh, _, _ = sum_period(daily_by_date, start, end, "sell_kwh")

    bill_actual = compute_bill(tariff, buy_kwh, billing_month)
    bill_l0 = compute_bill(tariff, consumption_kwh, billing_month)

    sell_fit = tariff["sell_price_yen_per_kwh"]["fit"]
    sell_post_fit = tariff["sell_price_yen_per_kwh"]["post_fit_assumed_for_readers"]
    sell_revenue_fit_yen = _round_yen(sell_kwh * sell_fit)
    sell_revenue_post_fit_yen = _round_yen(sell_kwh * sell_post_fit)

    saving_yen_fit = (bill_l0.total_yen - bill_actual.total_yen) + sell_revenue_fit_yen
    saving_yen_post_fit = (bill_l0.total_yen - bill_actual.total_yen) + sell_revenue_post_fit_yen

    return {
        "billing_month": billing_month,
        "excluded": False,
        "usage_period": {"start": start.isoformat(), "end": end.isoformat(), "days": total_days},
        "solar_kwh": round(solar_kwh, 3),
        "consumption_kwh": round(consumption_kwh, 3),
        "sell_kwh": round(sell_kwh, 3),
        "bill_actual": bill_actual.to_dict(),
        "bill_l0_no_solar": bill_l0.to_dict(),
        "sell_revenue_fit_yen": sell_revenue_fit_yen,
        "sell_revenue_post_fit_yen": sell_revenue_post_fit_yen,
        "saving_yen_fit": saving_yen_fit,
        "saving_yen_post_fit": saving_yen_post_fit,
    }


def build_bills(tariff: dict, daily_by_date: dict) -> dict:
    dates = sorted(date.fromisoformat(d) for d in daily_by_date)
    if not dates:
        return {"months": [], "excluded_months": []}

    months = []
    excluded = []
    for billing_month in list_candidate_billing_months(dates[0], dates[-1]):
        record = build_month_record(tariff, daily_by_date, billing_month)
        if record["excluded"]:
            excluded.append({"billing_month": record["billing_month"], "reason": record["reason"]})
        else:
            months.append(record)
    return {"months": months, "excluded_months": excluded}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--tariff", type=Path, default=DEFAULT_TARIFF_PATH, help="tariff.json のパス")
    parser.add_argument("--daily", type=Path, default=DEFAULT_DAILY_PATH, help="daily.json のパス")
    parser.add_argument("--out", type=Path, default=DEFAULT_OUT_PATH, help="出力先 bills.json のパス")
    args = parser.parse_args()

    tariff = json.loads(args.tariff.read_text(encoding="utf-8"))
    daily_by_date = load_daily(args.daily)

    result = build_bills(tariff, daily_by_date)
    result["generated_at"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    result["tariff_source"] = {
        "retailer": tariff["retailer"],
        "plan": tariff["plan"],
        "area": tariff["area"],
    }

    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(f"wrote: {args.out} ({len(result['months'])} months, {len(result['excluded_months'])} excluded)")


if __name__ == "__main__":
    main()
