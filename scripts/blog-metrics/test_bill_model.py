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
        "capacity_contribution_yen_per_kw_month": {
            "2026-08": 159,
            "contract_capacity_amp": None,
        },
        "fuel_cost_adjustment_yen_per_kwh": {
            "2026-07": {"applied": 8.69, "base": 8.69, "relief": 0.00},
            "2026-08": {"applied": -3.50, "base": 1.00, "relief": -4.50},
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
    def test_capacity_unknown_contributes_zero_and_is_flagged(self):
        tariff = make_tariff()
        bill = bill_model.compute_bill(tariff, buy_kwh=80.362, billing_month="2026-08")
        self.assertTrue(bill.capacity_unknown)
        self.assertEqual(bill.capacity_contribution_yen, 0)

    def test_negative_fuel_adjustment_reduces_total(self):
        tariff = make_tariff()
        # 2026-08 は applied=-3.50（政府軽減措置込みでマイナス）。買電量に比例して総額を押し下げる。
        bill = bill_model.compute_bill(tariff, buy_kwh=100.0, billing_month="2026-08")
        self.assertEqual(bill.fuel_adjustment_yen, bill_model._round_yen(100.0 * -3.50))
        self.assertLess(bill.fuel_adjustment_yen, 0)

    def test_capacity_contribution_applied_when_contract_amp_known(self):
        tariff = make_tariff()
        tariff["capacity_contribution_yen_per_kw_month"]["contract_capacity_amp"] = 40  # 40A = 4kW
        bill = bill_model.compute_bill(tariff, buy_kwh=80.362, billing_month="2026-08")
        self.assertFalse(bill.capacity_unknown)
        self.assertEqual(bill.capacity_contribution_yen, bill_model._round_yen(159 * 4.0))

    def test_missing_fuel_adjustment_month_raises(self):
        tariff = make_tariff()
        with self.assertRaises(KeyError):
            bill_model.compute_bill(tariff, buy_kwh=10.0, billing_month="2099-01")

    def test_negative_buy_kwh_rejected(self):
        tariff = make_tariff()
        with self.assertRaises(ValueError):
            bill_model.compute_bill(tariff, buy_kwh=-1.0, billing_month="2026-08")


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
