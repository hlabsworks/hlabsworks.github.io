#!/usr/bin/env python3
"""bill_model.py — 請求実績単価(tariff.json)を使い、請求期間（毎月2日〜翌月1日）ベースで
Japan電力 くらしプランS の推定請求額を再現し、太陽光なし(L0)反実仮想との差分から
節約額（FIT実態 / 卒FIT換算）を算出する。

入力:
  - scripts/blog-metrics/tariff.json      （請求明細PDFベースの実績単価データ、手動更新）
  - data/metrics/daily.json               （aggregate.sh の生成物。暦日ごとの実測 kWh）
  - data/metrics/official_sell.json（任意）（import_official_sell.py の生成物。東京電力
    パワーグリッドの公式メーター売電実績。無い場合はセンサー(power_history)由来の
    sell_kwh にフォールバックする）
出力:
  - data/metrics/bills.json

設計方針:
  - 標準ライブラリのみを使用する（外部依存を追加しない）。
  - 請求期間（毎月2日〜翌月1日）の全日分のデータが daily.json に揃っている月のみ
    bills.json の "months" に含める。データ欠測・tariff.json 側の単価未収載の月は
    "excluded_months" に理由付きで列挙し、値を捏造しない。
  - 売電収入（FIT実態）は official_sell.json に対応する精算月があればそれを優先採用する
    （東京電力パワーグリッドの実支払額のため、センサー推定より正確）。算定期間が
    daily.json 側の請求期間と一致しない場合は信用せずセンサー値にフォールバックする。
    採用した売電量の出典は各月の "sell_source" ("tepco_official"|"sensor") に記録し、
    センサー計測との差分は "sell_diff_pct" に併記する（センサー値は "sell_kwh" として
    参考値のまま残す）。
  - 容量拠出金は tariff.json の capacity_contribution_yen_per_month に billing_month の
    実額（請求PDFから確定した円額そのもの）が必要。無い月（請求PDF未取得の将来月）は
    燃料費等調整額と同様に値を捏造せず excluded_months に理由付きで回す。
  - 円換算は、請求明細PDF13か月分（energy-archive/japaden/derived/bill_breakdown.json,
    2026-09-05抽出）との突合で確定した実際の端数処理方式（VERIFIED）に従う:
      1. 電力量料金（段階制）と容量拠出金は元々整数円のため丸め不要。
      2. 燃料費等調整額は単価(円/kWh, 小数2桁)×kWhの厳密値をそのまま使う（請求書自体が
         この行を丸めずセント単位のまま表示するため）。
      3. 再エネ発電促進賦課金は単価×kWhを円未満切り捨て（math.floor）する（請求書がこの
         行だけ切り捨てた整数円を表示するため）。
      4. 合計(total_yen)は 1〜3 の内訳の総和を円未満切り捨て(math.floor)する。
    この方式で請求PDF13か月分全てbilled_yenと完全一致することを確認済み
    （test_bill_model.py の ReconciledMonthsTest 参照）。
    内訳の表示用フィールド(energy_charge_yen等)は可読性のため各々を円未満切り捨てた値で、
    その総和は端数の関係でtotal_yenと最大数円ずれうる（ASSUMED、表示専用）。
  - L0（太陽光なし反実仮想）は「消費電力(consumption_kwh)の全量を買電した」とみなす。
    L1/L2/L3（太陽光のみ／蓄電池／ポータブル電源の層別）は SolarChargeController 側の
    日次ロールアップが未整備のため準備中。現状の "bill_actual" は実測（太陽光+蓄電池+
    ポータブル電源の結果）であり、L1 単体の値ではない。
"""
from __future__ import annotations

import argparse
import json
import math
import sys
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from decimal import ROUND_HALF_UP, Decimal
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
REPO_ROOT = SCRIPT_DIR.parent.parent

DEFAULT_TARIFF_PATH = SCRIPT_DIR / "tariff.json"
DEFAULT_DAILY_PATH = REPO_ROOT / "data" / "metrics" / "daily.json"
DEFAULT_OFFICIAL_SELL_PATH = REPO_ROOT / "data" / "metrics" / "official_sell.json"
DEFAULT_OUT_PATH = REPO_ROOT / "data" / "metrics" / "bills.json"


def _round_yen(value: float) -> int:
    """円単位に四捨五入する（ROUND_HALF_UP）。売電収入（TEPCO購入実績・センサー推定）の
    丸めに使う。買電側の請求額再現は _floor_yen を使う（端数処理方式が異なるため。
    請求書側の端数処理はVERIFIED、売電側は要検証のためASSUMEDのまま四捨五入を維持）。"""
    return int(Decimal(str(value)).quantize(Decimal("1"), rounding=ROUND_HALF_UP))


def _floor_yen(value: float) -> int:
    """円単位に切り捨てる（請求書の端数処理方式。VERIFIED、bill_breakdown.json 突合済み）。"""
    return math.floor(value)


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
    total_yen: int

    def to_dict(self) -> dict:
        return {
            "buy_kwh": round(self.buy_kwh, 3),
            "basic_fee_yen": self.basic_fee_yen,
            "energy_charge_yen": self.energy_charge_yen,
            "fuel_adjustment_yen": self.fuel_adjustment_yen,
            "renewable_levy_yen": self.renewable_levy_yen,
            "capacity_contribution_yen": self.capacity_contribution_yen,
            "total_yen": self.total_yen,
        }


def compute_bill(tariff: dict, buy_kwh: float, billing_month: str) -> BillBreakdown:
    """buy_kwh(当該請求期間の買電量kWh)から tariff.json の実請求単価で請求額を再現する。

    端数処理は請求PDF13か月分との突合で確定した方式（VERIFIED、モジュールdocstring参照）:
    燃料費等調整額は厳密値のまま、再エネ賦課金は円未満切り捨て、合計はその総和を円未満切り捨て。
    """
    if buy_kwh < 0:
        raise ValueError(f"buy_kwh は非負を想定（実際: {buy_kwh}）")

    basic_fee = tariff["basic_fee_yen_per_month"]
    energy_charge = tiered_energy_charge(tariff["energy_tiers_yen_per_kwh"], buy_kwh)

    fuel_adj_table = tariff["fuel_cost_adjustment_yen_per_kwh"]
    if billing_month not in fuel_adj_table:
        raise KeyError(f"fuel_cost_adjustment_yen_per_kwh に {billing_month} がありません")
    fuel_adjustment = buy_kwh * fuel_adj_table[billing_month]

    levy_rate = renewable_levy_rate(tariff, billing_month)
    renewable_levy = math.floor(buy_kwh * levy_rate)

    capacity_table = tariff["capacity_contribution_yen_per_month"]
    if billing_month not in capacity_table:
        raise KeyError(f"capacity_contribution_yen_per_month に {billing_month} がありません")
    capacity_contribution = capacity_table[billing_month]

    total_yen = math.floor(basic_fee + energy_charge + fuel_adjustment + renewable_levy + capacity_contribution)

    return BillBreakdown(
        buy_kwh=buy_kwh,
        basic_fee_yen=_floor_yen(basic_fee),
        energy_charge_yen=_floor_yen(energy_charge),
        fuel_adjustment_yen=_floor_yen(fuel_adjustment),
        renewable_levy_yen=renewable_levy,
        capacity_contribution_yen=_floor_yen(capacity_contribution),
        total_yen=total_yen,
    )


def load_daily(path: Path) -> dict[str, dict]:
    rows = json.loads(path.read_text(encoding="utf-8"))
    return {r["date"]: r for r in rows}


def load_official_sell(path: Path) -> dict[str, dict]:
    """import_official_sell.py の生成物(official_sell.json)を読み、settlement_month
    (= billing_month と同じ命名規則) をキーにした辞書にする。

    ファイルが存在しない場合は空の辞書を返す（official_sell.json は任意の補助データ
    であり、無ければ従来どおりセンサー値にフォールバックする設計のため）。
    """
    if not path.exists():
        return {}
    data = json.loads(path.read_text(encoding="utf-8"))
    return {m["settlement_month"]: m for m in data.get("months", [])}


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


def resolve_official_sell(official_by_month: dict, billing_month: str, start: date, end: date) -> dict | None:
    """billing_month に対応する official_sell.json のレコードを返す。

    settlement_month が一致しても算定期間(period_from/period_to)が daily.json 側の
    請求期間(start/end)とズレている場合は、値を信用せずセンサー値にフォールバックする
    （財務に関わる値のため、キー一致だけで採用しない）。
    """
    official = official_by_month.get(billing_month)
    if official is None:
        return None
    if official["period_from"] != start.isoformat() or official["period_to"] != end.isoformat():
        print(
            f"bill_model.py: official_sell.json の期間が一致しないため {billing_month} はセンサー値にフォールバックします "
            f"(official: {official['period_from']}..{official['period_to']}, usage: {start.isoformat()}..{end.isoformat()})",
            file=sys.stderr,
        )
        return None
    return official


def build_month_record(
    tariff: dict, daily_by_date: dict, billing_month: str, official_by_month: dict | None = None
) -> dict:
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

    if billing_month not in tariff["capacity_contribution_yen_per_month"]:
        return {
            "billing_month": billing_month,
            "excluded": True,
            "reason": f"tariff.json の capacity_contribution_yen_per_month に {billing_month} が未収載",
        }

    consumption_kwh, _, cons_missing = sum_period(daily_by_date, start, end, "consumption_kwh")
    if cons_missing > 0:
        return {
            "billing_month": billing_month,
            "excluded": True,
            "reason": f"usage period に consumption_kwh 欠測 {cons_missing}/{total_days} 日",
        }

    solar_kwh, _, _ = sum_period(daily_by_date, start, end, "solar_kwh")
    sell_kwh_sensor, _, _ = sum_period(daily_by_date, start, end, "sell_kwh")

    bill_actual = compute_bill(tariff, buy_kwh, billing_month)
    bill_l0 = compute_bill(tariff, consumption_kwh, billing_month)

    sell_fit = tariff["sell_price_yen_per_kwh"]["fit"]
    sell_post_fit = tariff["sell_price_yen_per_kwh"]["post_fit_assumed_for_readers"]

    official = resolve_official_sell(official_by_month or {}, billing_month, start, end)
    if official is not None:
        sell_kwh_official = official["official_sell_kwh"]
        sell_source = "tepco_official"
        # 東京電力パワーグリッドが実際に支払った金額（購入実績お知らせサービス）をそのまま採用する。
        # センサー推定(sell_kwh_sensor × FIT単価)より正確なため、これを「FIT実態」の売電収入とする。
        sell_revenue_fit_yen = official["sell_revenue_yen"]
        sell_revenue_post_fit_yen = _round_yen(sell_kwh_official * sell_post_fit)
        sell_diff_pct = (
            round(((sell_kwh_sensor - sell_kwh_official) / sell_kwh_official) * 100, 1)
            if sell_kwh_official
            else None
        )
    else:
        sell_kwh_official = None
        sell_source = "sensor"
        sell_revenue_fit_yen = _round_yen(sell_kwh_sensor * sell_fit)
        sell_revenue_post_fit_yen = _round_yen(sell_kwh_sensor * sell_post_fit)
        sell_diff_pct = None

    saving_yen_fit = (bill_l0.total_yen - bill_actual.total_yen) + sell_revenue_fit_yen
    saving_yen_post_fit = (bill_l0.total_yen - bill_actual.total_yen) + sell_revenue_post_fit_yen

    return {
        "billing_month": billing_month,
        "excluded": False,
        "usage_period": {"start": start.isoformat(), "end": end.isoformat(), "days": total_days},
        "solar_kwh": round(solar_kwh, 3),
        "consumption_kwh": round(consumption_kwh, 3),
        "sell_kwh": round(sell_kwh_sensor, 3),
        "sell_kwh_official": round(sell_kwh_official, 3) if sell_kwh_official is not None else None,
        "sell_diff_pct": sell_diff_pct,
        "sell_source": sell_source,
        "bill_actual": bill_actual.to_dict(),
        "bill_l0_no_solar": bill_l0.to_dict(),
        "sell_revenue_fit_yen": sell_revenue_fit_yen,
        "sell_revenue_post_fit_yen": sell_revenue_post_fit_yen,
        "saving_yen_fit": saving_yen_fit,
        "saving_yen_post_fit": saving_yen_post_fit,
    }


BILLS_ROUNDING_NOTE = (
    "内訳(energy_charge_yen等)は表示用に円未満切り捨て。合計(total_yen)は内訳の単純和ではなく、"
    "燃料費等調整額の厳密値（丸め前）を含めた計算式全体を円未満切り捨てするため、"
    "内訳の単純和とtotal_yenが端数の関係で最大数円ずれることがある（表示専用の丸め。詳細は bill_model.py の"
    "compute_bill() docstring 参照）。"
)


def build_bills(tariff: dict, daily_by_date: dict, official_by_month: dict | None = None) -> dict:
    dates = sorted(date.fromisoformat(d) for d in daily_by_date)
    if not dates:
        return {"months": [], "excluded_months": [], "_note": BILLS_ROUNDING_NOTE}

    months = []
    excluded = []
    for billing_month in list_candidate_billing_months(dates[0], dates[-1]):
        record = build_month_record(tariff, daily_by_date, billing_month, official_by_month)
        if record["excluded"]:
            excluded.append({"billing_month": record["billing_month"], "reason": record["reason"]})
        else:
            months.append(record)
    return {"months": months, "excluded_months": excluded, "_note": BILLS_ROUNDING_NOTE}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--tariff", type=Path, default=DEFAULT_TARIFF_PATH, help="tariff.json のパス")
    parser.add_argument("--daily", type=Path, default=DEFAULT_DAILY_PATH, help="daily.json のパス")
    parser.add_argument(
        "--official-sell", type=Path, default=DEFAULT_OFFICIAL_SELL_PATH,
        help="import_official_sell.py の生成物(official_sell.json)のパス（無ければセンサー値にフォールバック）",
    )
    parser.add_argument("--out", type=Path, default=DEFAULT_OUT_PATH, help="出力先 bills.json のパス")
    args = parser.parse_args()

    tariff = json.loads(args.tariff.read_text(encoding="utf-8"))
    daily_by_date = load_daily(args.daily)
    official_by_month = load_official_sell(args.official_sell)

    result = build_bills(tariff, daily_by_date, official_by_month)
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
