#!/usr/bin/env python3
"""bill_model.py の unittest。

実行方法:
  cd scripts/blog-metrics && python3 -m unittest test_bill_model -v
"""
import sys
import unittest
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import bill_model  # noqa: E402


def make_tariff(**overrides) -> dict:
    """テスト用の最小 tariff.json 相当の dict を作る。"""
    tariff = {
        "retailer": "テスト電力",
        "plan": "テストプランS",
        "area": "テスト",
        "basic_fee_yen_per_month": 0,
        "energy_tiers_yen_per_kwh": [
            {"up_to_kwh": 400, "yen_per_kwh": 27.00},
            {"up_to_kwh": None, "yen_per_kwh": 26.00},
        ],
        "renewable_levy_yen_per_kwh": {
            "2025-05..2026-04": 3.98,
            "2026-05..2027-04": 4.18,
        },
        "capacity_contribution_yen_per_month": {
            "2026-08": 213,
            "default_for_unbilled_months": 213,
        },
        "fuel_cost_adjustment_yen_per_kwh": {
            "2026-07": 8.69,
            "2026-08": -3.50,
        },
        "sell_price_yen_per_kwh": {"fit": 16.0, "post_fit_assumed_for_readers": 8.0},
        "meter_read_day": 2,
    }
    tariff.update(overrides)
    return tariff


class TieredEnergyChargeTest(unittest.TestCase):
    def test_below_first_tier_uses_single_rate(self):
        tiers = [{"up_to_kwh": 400, "yen_per_kwh": 27.0}, {"up_to_kwh": None, "yen_per_kwh": 26.0}]
        self.assertAlmostEqual(bill_model.tiered_energy_charge(tiers, 80.362), 80.362 * 27.0, places=6)

    def test_above_first_tier_splits_across_tiers(self):
        tiers = [{"up_to_kwh": 400, "yen_per_kwh": 27.0}, {"up_to_kwh": None, "yen_per_kwh": 26.0}]
        # 500kWh = 400kWh(27円) + 100kWh(26円)
        expected = 400 * 27.0 + 100 * 26.0
        self.assertAlmostEqual(bill_model.tiered_energy_charge(tiers, 500), expected, places=6)

    def test_zero_kwh_is_zero_charge(self):
        tiers = [{"up_to_kwh": 400, "yen_per_kwh": 27.0}, {"up_to_kwh": None, "yen_per_kwh": 26.0}]
        self.assertEqual(bill_model.tiered_energy_charge(tiers, 0), 0.0)


class BillingPeriodTest(unittest.TestCase):
    def test_billing_month_covers_2nd_to_1st(self):
        start, end = bill_model.billing_period("2026-08", meter_read_day=2)
        self.assertEqual(start, date(2026, 7, 2))
        self.assertEqual(end, date(2026, 8, 1))

    def test_billing_month_crosses_year_boundary(self):
        start, end = bill_model.billing_period("2026-01", meter_read_day=2)
        self.assertEqual(start, date(2025, 12, 2))
        self.assertEqual(end, date(2026, 1, 1))

    def test_meter_read_day_below_2_is_rejected(self):
        with self.assertRaises(ValueError):
            bill_model.billing_period("2026-08", meter_read_day=1)


class ComputeBillTest(unittest.TestCase):
    def test_capacity_uses_actual_billed_value_when_month_present(self):
        tariff = make_tariff()
        bill = bill_model.compute_bill(tariff, buy_kwh=80.362, billing_month="2026-08")
        self.assertFalse(bill.capacity_estimated)
        self.assertEqual(bill.capacity_contribution_yen, 213)

    def test_capacity_falls_back_to_default_for_unbilled_month(self):
        # 請求PDF未取得の月（capacity_contribution_yen_per_monthに billing_month が無い）は
        # default_for_unbilled_months を使い、capacity_estimated=True を明示する。
        tariff = make_tariff()
        tariff["fuel_cost_adjustment_yen_per_kwh"]["2026-09"] = -3.50  # fuel未収載だと先にKeyErrorになるため補う
        bill = bill_model.compute_bill(tariff, buy_kwh=80.362, billing_month="2026-09")
        self.assertTrue(bill.capacity_estimated)
        self.assertEqual(bill.capacity_contribution_yen, 213)

    def test_missing_default_for_unbilled_month_raises(self):
        # default_for_unbilled_months 自体が無い状態で未収載月を渡すとKeyErrorになる
        # （値を捏造しない設計を守るための失敗パス）。
        tariff = make_tariff()
        del tariff["capacity_contribution_yen_per_month"]["default_for_unbilled_months"]
        tariff["fuel_cost_adjustment_yen_per_kwh"]["2026-09"] = -3.50
        with self.assertRaises(KeyError):
            bill_model.compute_bill(tariff, buy_kwh=80.362, billing_month="2026-09")

    def test_negative_fuel_adjustment_reduces_total(self):
        tariff = make_tariff()
        # 2026-08 は -3.50円/kWh（政府軽減措置込みでマイナス）。買電量に比例して総額を押し下げる。
        bill = bill_model.compute_bill(tariff, buy_kwh=100.0, billing_month="2026-08")
        self.assertEqual(bill.fuel_adjustment_yen, bill_model._floor_yen(100.0 * -3.50))
        self.assertLess(bill.fuel_adjustment_yen, 0)

    def test_renewable_levy_is_floored_not_rounded(self):
        # 3.98円/kWh × 65kWh = 258.7円 → 円未満切り捨てで258円（四捨五入なら259円になり誤り）。
        # 請求書の実際の端数処理方式（bill_breakdown.json突合でVERIFIED）。
        tariff = make_tariff()
        tariff["fuel_cost_adjustment_yen_per_kwh"]["2026-04"] = -0.27
        tariff["capacity_contribution_yen_per_month"]["2026-04"] = 0
        bill = bill_model.compute_bill(tariff, buy_kwh=65.0, billing_month="2026-04")
        self.assertEqual(bill.renewable_levy_yen, 258)

    def test_total_yen_is_floored_sum_of_exact_components(self):
        # 2026-08実績(83kWh, fuel_rate=10.38円/kWh)の再現: energy=2241 + fuel=861.54(厳密値)
        # + levy=floor(346.94)=346 + capacity=213 → 3661.54 を円未満切り捨てして3661円
        # （実請求額と一致。energy-archive/japaden/derived/bill_breakdown.json 2026-08 参照）。
        tariff = make_tariff()
        tariff["fuel_cost_adjustment_yen_per_kwh"]["2026-08"] = 10.38
        bill = bill_model.compute_bill(tariff, buy_kwh=83.0, billing_month="2026-08")
        self.assertEqual(bill.total_yen, 3661)

    def test_missing_fuel_adjustment_month_raises(self):
        tariff = make_tariff()
        with self.assertRaises(KeyError):
            bill_model.compute_bill(tariff, buy_kwh=10.0, billing_month="2099-01")

    def test_negative_buy_kwh_rejected(self):
        tariff = make_tariff()
        with self.assertRaises(ValueError):
            bill_model.compute_bill(tariff, buy_kwh=-1.0, billing_month="2026-08")


class ReconciledMonthsTest(unittest.TestCase):
    """energy-archive/japaden/derived/bill_breakdown.json（請求明細PDF13か月分、2026-09-05抽出）
    の usage_kwh を入力に compute_bill を実行し、実請求額 billed_yen と ±1円以内で一致することを
    固定する。値は tariff.json の _verification.reconciled_months と同一のfixture（本ファイルに
    埋め込み、energy-archive への実行時依存は作らない）。
    """

    # (billing_month, usage_kwh, billed_yen)
    RECONCILED_MONTHS = [
        ("2025-08", 73.0, 2563), ("2025-09", 242.0, 8362), ("2025-10", 160.0, 5495),
        ("2025-11", 86.0, 3219), ("2025-12", 140.0, 5136), ("2026-01", 428.0, 14409),
        ("2026-02", 511.0, 14285), ("2026-03", 245.0, 7314), ("2026-04", 70.0, 2149),
        ("2026-05", 9.0, 473), ("2026-06", 3.0, 461), ("2026-07", 34.0, 1770),
        ("2026-08", 83.0, 3661),
    ]

    def _load_real_tariff(self) -> dict:
        import json as _json
        return _json.loads((Path(__file__).resolve().parent / "tariff.json").read_text(encoding="utf-8"))

    def test_all_13_months_match_billed_yen_within_1_yen(self):
        tariff = self._load_real_tariff()
        for billing_month, usage_kwh, billed_yen in self.RECONCILED_MONTHS:
            with self.subTest(billing_month=billing_month):
                bill = bill_model.compute_bill(tariff, buy_kwh=usage_kwh, billing_month=billing_month)
                self.assertLessEqual(
                    abs(bill.total_yen - billed_yen), 1,
                    f"{billing_month}: computed={bill.total_yen} billed={billed_yen}",
                )


class BuildMonthRecordTest(unittest.TestCase):
    def _daily_row(self, d: str, buy=1.0, solar=10.0, sell=5.0, consumption=6.0) -> dict:
        return {"date": d, "buy_kwh": buy, "solar_kwh": solar, "sell_kwh": sell, "consumption_kwh": consumption}

    def test_complete_period_is_not_excluded(self):
        tariff = make_tariff()
        start, end = bill_model.billing_period("2026-08", meter_read_day=2)
        daily = {}
        d = start
        while d <= end:
            daily[d.isoformat()] = self._daily_row(d.isoformat())
            from datetime import timedelta

            d += timedelta(days=1)
        record = bill_model.build_month_record(tariff, daily, "2026-08")
        self.assertFalse(record["excluded"])
        self.assertIn("saving_yen_fit", record)
        self.assertIn("saving_yen_post_fit", record)

    def test_missing_day_excludes_with_reason(self):
        tariff = make_tariff()
        start, end = bill_model.billing_period("2026-08", meter_read_day=2)
        daily = {}
        d = start
        while d <= end:
            if d.isoformat() != "2026-07-15":  # 1日だけ欠測させる
                daily[d.isoformat()] = self._daily_row(d.isoformat())
            from datetime import timedelta

            d += timedelta(days=1)
        record = bill_model.build_month_record(tariff, daily, "2026-08")
        self.assertTrue(record["excluded"])
        self.assertIn("欠測", record["reason"])

    def _full_month_daily(self, billing_month: str) -> dict:
        start, end = bill_model.billing_period(billing_month, meter_read_day=2)
        daily = {}
        d = start
        while d <= end:
            daily[d.isoformat()] = self._daily_row(d.isoformat(), sell=13.0)
            from datetime import timedelta

            d += timedelta(days=1)
        return daily

    def test_official_sell_used_when_present_and_period_matches(self):
        tariff = make_tariff()
        daily = self._full_month_daily("2026-08")
        start, end = bill_model.billing_period("2026-08", meter_read_day=2)
        total_days = (end - start).days + 1
        sensor_sell_kwh = 13.0 * total_days  # 403.0 kWh
        official_by_month = {
            "2026-08": {
                "settlement_month": "2026-08",
                "period_from": start.isoformat(),
                "period_to": end.isoformat(),
                "official_sell_kwh": 400.0,
                "sell_revenue_yen": 6400,
            }
        }
        record = bill_model.build_month_record(tariff, daily, "2026-08", official_by_month)
        self.assertEqual(record["sell_source"], "tepco_official")
        self.assertEqual(record["sell_kwh_official"], 400.0)
        self.assertEqual(record["sell_kwh"], round(sensor_sell_kwh, 3))
        self.assertEqual(record["sell_revenue_fit_yen"], 6400)
        # diff% = (センサー - 公式) / 公式 * 100
        expected_diff_pct = round(((sensor_sell_kwh - 400.0) / 400.0) * 100, 1)
        self.assertEqual(record["sell_diff_pct"], expected_diff_pct)
        self.assertGreater(record["sell_diff_pct"], 0)  # センサーが公式より多く見積もっている

    def test_falls_back_to_sensor_when_official_sell_absent(self):
        tariff = make_tariff()
        daily = self._full_month_daily("2026-08")
        record = bill_model.build_month_record(tariff, daily, "2026-08", official_by_month={})
        self.assertEqual(record["sell_source"], "sensor")
        self.assertIsNone(record["sell_kwh_official"])
        self.assertIsNone(record["sell_diff_pct"])
        self.assertEqual(record["sell_revenue_fit_yen"], bill_model._round_yen(record["sell_kwh"] * 16.0))

    def test_falls_back_to_sensor_when_official_sell_period_mismatch(self):
        tariff = make_tariff()
        daily = self._full_month_daily("2026-08")
        official_by_month = {
            "2026-08": {
                "settlement_month": "2026-08",
                "period_from": "2099-01-01",  # daily.json 側の請求期間とわざとズラす
                "period_to": "2099-01-31",
                "official_sell_kwh": 400.0,
                "sell_revenue_yen": 6400,
            }
        }
        record = bill_model.build_month_record(tariff, daily, "2026-08", official_by_month)
        self.assertEqual(record["sell_source"], "sensor")
        self.assertIsNone(record["sell_kwh_official"])


class LoadOfficialSellTest(unittest.TestCase):
    def test_missing_file_returns_empty_dict(self):
        result = bill_model.load_official_sell(Path("/nonexistent/official_sell.json"))
        self.assertEqual(result, {})

    def test_loads_and_indexes_by_settlement_month(self):
        import json
        import tempfile

        payload = {
            "months": [
                {
                    "settlement_month": "2026-08",
                    "period_from": "2026-07-02",
                    "period_to": "2026-08-01",
                    "official_sell_kwh": 408.1,
                    "sell_revenue_yen": 6528,
                }
            ]
        }
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "official_sell.json"
            path.write_text(json.dumps(payload), encoding="utf-8")
            result = bill_model.load_official_sell(path)
        self.assertIn("2026-08", result)
        self.assertEqual(result["2026-08"]["official_sell_kwh"], 408.1)


if __name__ == "__main__":
    unittest.main()
