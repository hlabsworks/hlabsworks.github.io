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


class ReasonHelperTest(unittest.TestCase):
    """読者向けQAレビュー対応（2026-09-06）: reason() が3点セット
    {reason_code, reason_label, reason_detail} を返すことを確認する。"""

    def test_reason_returns_three_keys(self):
        r = bill_model.reason("profile_missing", "5分プロファイル未取得", "内部ファイルXが無い")
        self.assertEqual(
            r, {"reason_code": "profile_missing", "reason_label": "5分プロファイル未取得", "reason_detail": "内部ファイルXが無い"}
        )

    def test_reason_codes_are_distinct_constants(self):
        codes = {
            bill_model.REASON_CODE_PROFILE_MISSING,
            bill_model.REASON_CODE_PERIOD_INCOMPLETE,
            bill_model.REASON_CODE_DAILY_MISSING,
            bill_model.REASON_CODE_TARIFF_MISSING,
        }
        self.assertEqual(len(codes), 4)


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


class BillingMonthForDateTest(unittest.TestCase):
    """オーナー承認機能（2026-09-06、月途中集計）: billing_period の逆写像。"""

    def test_date_on_or_after_meter_read_day_belongs_to_next_month(self):
        self.assertEqual(bill_model.billing_month_for_date(date(2026, 7, 15), meter_read_day=2), "2026-08")
        self.assertEqual(bill_model.billing_month_for_date(date(2026, 7, 2), meter_read_day=2), "2026-08")

    def test_date_before_meter_read_day_belongs_to_current_month(self):
        self.assertEqual(bill_model.billing_month_for_date(date(2026, 7, 1), meter_read_day=2), "2026-07")

    def test_round_trips_with_billing_period(self):
        for d in (date(2026, 9, 2), date(2026, 9, 5), date(2026, 10, 1)):
            billing_month = bill_model.billing_month_for_date(d, meter_read_day=2)
            start, end = bill_model.billing_period(billing_month, meter_read_day=2)
            self.assertTrue(start <= d <= end)


class PerKwhPricesTest(unittest.TestCase):
    """オーナー承認機能（2026-09-06、日次per_kwh_only換算）。"""

    def test_confirmed_month_uses_its_own_fuel_rate(self):
        tariff = make_tariff()
        buy_price, sell_fit, sell_post_fit, provisional, source_month = bill_model.per_kwh_prices(tariff, "2026-08")
        expected = (
            tariff["energy_tiers_yen_per_kwh"][0]["yen_per_kwh"]
            + tariff["fuel_cost_adjustment_yen_per_kwh"]["2026-08"]
            + bill_model.renewable_levy_rate(tariff, "2026-08")
        )
        self.assertAlmostEqual(buy_price, expected, places=6)
        self.assertFalse(provisional)
        self.assertIsNone(source_month)
        self.assertEqual(sell_fit, tariff["sell_price_yen_per_kwh"]["fit"])
        self.assertEqual(sell_post_fit, tariff["sell_price_yen_per_kwh"]["post_fit_assumed_for_readers"])

    def test_unconfirmed_month_falls_back_to_latest_confirmed_fuel_rate(self):
        tariff = make_tariff()  # fuel_cost_adjustment は 2026-07/2026-08 のみ確定
        buy_price, _, _, provisional, source_month = bill_model.per_kwh_prices(tariff, "2026-09")
        self.assertTrue(provisional)
        self.assertEqual(source_month, "2026-08")
        expected = tariff["energy_tiers_yen_per_kwh"][0]["yen_per_kwh"] + tariff["fuel_cost_adjustment_yen_per_kwh"]["2026-08"] + 4.18
        self.assertAlmostEqual(buy_price, expected, places=6)

    def test_no_confirmed_month_at_all_raises(self):
        tariff = make_tariff(fuel_cost_adjustment_yen_per_kwh={})
        with self.assertRaises(KeyError):
            bill_model.per_kwh_prices(tariff, "2026-09")


class ResolveEffectiveTariffTest(unittest.TestCase):
    """オーナー承認機能（2026-09-06、月途中集計）: 暫定単価の適用。"""

    def test_confirmed_month_returns_tariff_unchanged(self):
        tariff = make_tariff()
        effective, provisional, source_month = bill_model.resolve_effective_tariff(tariff, "2026-08")
        self.assertIs(effective, tariff)
        self.assertFalse(provisional)
        self.assertIsNone(source_month)

    def test_unconfirmed_month_patches_fuel_and_capacity_from_latest_confirmed(self):
        tariff = make_tariff()  # fuel+capacity両方が確定しているのは2026-08のみ
        effective, provisional, source_month = bill_model.resolve_effective_tariff(tariff, "2026-10")
        self.assertTrue(provisional)
        self.assertEqual(source_month, "2026-08")
        self.assertEqual(effective["fuel_cost_adjustment_yen_per_kwh"]["2026-10"], tariff["fuel_cost_adjustment_yen_per_kwh"]["2026-08"])
        self.assertEqual(effective["capacity_contribution_yen_per_month"]["2026-10"], tariff["capacity_contribution_yen_per_month"]["2026-08"])
        # 元のtariffは変更しない（副作用を持たせない）。
        self.assertNotIn("2026-10", tariff["fuel_cost_adjustment_yen_per_kwh"])

    def test_unconfirmed_month_with_no_fallback_returns_original_tariff(self):
        tariff = make_tariff(fuel_cost_adjustment_yen_per_kwh={}, capacity_contribution_yen_per_month={})
        effective, provisional, source_month = bill_model.resolve_effective_tariff(tariff, "2026-09")
        self.assertIs(effective, tariff)
        self.assertFalse(provisional)
        self.assertIsNone(source_month)

    def test_compute_bill_with_patched_tariff_succeeds_for_unconfirmed_month(self):
        # 確定表示(compute_bill単体)には従来どおり暫定を渡さないが、明示的に暫定tariffを
        # 作ってcompute_billに渡せば正常に計算できることを確認する（in_progress用の経路）。
        tariff = make_tariff()
        effective, provisional, source_month = bill_model.resolve_effective_tariff(tariff, "2026-10")
        self.assertTrue(provisional)
        bill = bill_model.compute_bill(effective, buy_kwh=10.0, billing_month="2026-10")
        self.assertGreater(bill.total_yen, 0)
        # 確定月(build_month_record が呼ぶ compute_bill)は暫定を使わないため、元のtariffで
        # 同じ月を計算すると引き続きKeyErrorになる（捏造禁止方針が確定表示には残ることの確認）。
        with self.assertRaises(KeyError):
            bill_model.compute_bill(tariff, buy_kwh=10.0, billing_month="2026-10")


class ComputeBillTest(unittest.TestCase):
    def test_capacity_uses_actual_billed_value_when_month_present(self):
        tariff = make_tariff()
        bill = bill_model.compute_bill(tariff, buy_kwh=80.362, billing_month="2026-08")
        self.assertEqual(bill.capacity_contribution_yen, 213)

    def test_capacity_missing_month_raises(self):
        # 請求PDF未取得の月（capacity_contribution_yen_per_monthに billing_month が無い）は
        # 値を捏造せずKeyErrorにする（燃料費等調整額と同じ失敗パス）。
        tariff = make_tariff()
        tariff["fuel_cost_adjustment_yen_per_kwh"]["2026-09"] = -3.50  # fuel未収載だと先にKeyErrorになるため補う
        with self.assertRaises(KeyError):
            bill_model.compute_bill(tariff, buy_kwh=80.362, billing_month="2026-09")

    def test_billing_month_outside_levy_ranges_raises(self):
        # renewable_levy_yen_per_kwh のどの範囲にも入らない請求月（2030-01）はKeyErrorになる
        # （値を捏造しない設計を守るための失敗パス）。
        tariff = make_tariff()
        tariff["fuel_cost_adjustment_yen_per_kwh"]["2030-01"] = 1.0
        tariff["capacity_contribution_yen_per_month"]["2030-01"] = 213
        with self.assertRaises(KeyError):
            bill_model.compute_bill(tariff, buy_kwh=10.0, billing_month="2030-01")

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
        # （実請求額と一致。private の energy-archive の bill_breakdown.json 2026-08 参照）。
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
    """private の energy-archive の bill_breakdown.json（請求明細PDF13か月分、2026-09-05抽出）
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

    def test_all_13_months_match_billed_yen_exactly(self):
        tariff = self._load_real_tariff()
        for billing_month, usage_kwh, billed_yen in self.RECONCILED_MONTHS:
            with self.subTest(billing_month=billing_month):
                bill = bill_model.compute_bill(tariff, buy_kwh=usage_kwh, billing_month=billing_month)
                self.assertEqual(
                    bill.total_yen, billed_yen,
                    f"{billing_month}: computed={bill.total_yen} billed={billed_yen}",
                )

    def test_reconcile_bill_is_zero_for_all_13_months(self):
        """reconcile_bill() が compute_bill().total_yen - billed_yen と同じ差額を返すこと
        （本物の tariff.json・13か月分で diff=0 を確認する。突合ロジックの二重実装を避ける
        ため compute_bill を呼ぶだけの薄い関数だが、実データでの契約を固定する）。"""
        tariff = self._load_real_tariff()
        for billing_month, usage_kwh, billed_yen in self.RECONCILED_MONTHS:
            with self.subTest(billing_month=billing_month):
                diff = bill_model.reconcile_bill(tariff, billing_month, usage_kwh, billed_yen)
                self.assertEqual(diff, 0)

    def test_reconcile_bill_returns_nonzero_diff_on_mismatch(self):
        tariff = self._load_real_tariff()
        diff = bill_model.reconcile_bill(tariff, "2026-08", 83.0, 3661 + 500)
        self.assertEqual(diff, -500)


class MergeTariffTest(unittest.TestCase):
    def test_overlay_none_returns_copy_of_base(self):
        base = make_tariff()
        merged = bill_model.merge_tariff(base, None)
        self.assertEqual(merged, base)
        self.assertIsNot(merged, base)

    def test_overlay_adds_month_missing_from_base(self):
        base = make_tariff()
        overlay = {
            "fuel_cost_adjustment_yen_per_kwh": {"2026-09": 9.12},
            "capacity_contribution_yen_per_month": {"2026-09": 213},
        }
        merged = bill_model.merge_tariff(base, overlay)
        self.assertEqual(merged["fuel_cost_adjustment_yen_per_kwh"]["2026-09"], 9.12)
        self.assertEqual(merged["capacity_contribution_yen_per_month"]["2026-09"], 213)
        # base 側の既存月は変わらない
        self.assertEqual(merged["fuel_cost_adjustment_yen_per_kwh"]["2026-08"], -3.50)
        # base 自体は変更されない
        self.assertNotIn("2026-09", base["fuel_cost_adjustment_yen_per_kwh"])

    def test_overlay_matching_existing_month_is_accepted(self):
        base = make_tariff()
        overlay = {"fuel_cost_adjustment_yen_per_kwh": {"2026-08": -3.50}}
        merged = bill_model.merge_tariff(base, overlay)
        self.assertEqual(merged["fuel_cost_adjustment_yen_per_kwh"]["2026-08"], -3.50)

    def test_overlay_conflicting_month_raises_value_error(self):
        base = make_tariff()
        overlay = {"fuel_cost_adjustment_yen_per_kwh": {"2026-08": 999.0}}
        with self.assertRaises(ValueError) as ctx:
            bill_model.merge_tariff(base, overlay)
        self.assertIn("tariff_conflict", str(ctx.exception))

    def test_overlay_capacity_conflict_raises_value_error(self):
        base = make_tariff()
        overlay = {"capacity_contribution_yen_per_month": {"2026-08": 1}}
        with self.assertRaises(ValueError):
            bill_model.merge_tariff(base, overlay)

    def test_overlay_levy_adds_uncovered_month(self):
        base = make_tariff()
        overlay = {"renewable_levy_yen_per_kwh_observed": {"2027-05..2027-05": 4.5}}
        merged = bill_model.merge_tariff(base, overlay)
        self.assertEqual(merged["renewable_levy_yen_per_kwh"]["2027-05..2027-05"], 4.5)
        # base の既存レンジはそのまま残る
        self.assertEqual(merged["renewable_levy_yen_per_kwh"]["2026-05..2027-04"], 4.18)

    def test_overlay_levy_matching_covered_month_is_accepted(self):
        base = make_tariff()
        overlay = {"renewable_levy_yen_per_kwh_observed": {"2026-08..2026-08": 4.18}}
        merged = bill_model.merge_tariff(base, overlay)
        # 既存の年度レンジのみが残り、単月レンジは追加されない
        self.assertNotIn("2026-08..2026-08", merged["renewable_levy_yen_per_kwh"])

    def test_overlay_levy_conflicting_covered_month_raises_value_error(self):
        base = make_tariff()
        overlay = {"renewable_levy_yen_per_kwh_observed": {"2026-08..2026-08": 1.0}}
        with self.assertRaises(ValueError) as ctx:
            bill_model.merge_tariff(base, overlay)
        self.assertIn("tariff_conflict", str(ctx.exception))


class ConfirmedTariffMonthsTest(unittest.TestCase):
    def test_returns_months_with_both_fuel_and_capacity(self):
        tariff = make_tariff()
        # make_tariff(): fuel has 2026-07, 2026-08; capacity has only 2026-08
        self.assertEqual(bill_model.confirmed_tariff_months(tariff), {"2026-08"})

    def test_empty_when_no_overlap(self):
        tariff = make_tariff(
            fuel_cost_adjustment_yen_per_kwh={"2026-07": 8.69},
            capacity_contribution_yen_per_month={"2026-08": 213},
        )
        self.assertEqual(bill_model.confirmed_tariff_months(tariff), set())

    def test_merge_then_confirmed_reflects_newly_added_month(self):
        base = make_tariff()
        overlay = {
            "fuel_cost_adjustment_yen_per_kwh": {"2026-09": 9.12},
            "capacity_contribution_yen_per_month": {"2026-09": 213},
        }
        merged = bill_model.merge_tariff(base, overlay)
        self.assertEqual(bill_model.confirmed_tariff_months(merged), {"2026-08", "2026-09"})


class TariffConflictsTest(unittest.TestCase):
    def test_overlay_none_returns_empty_list(self):
        self.assertEqual(bill_model.tariff_conflicts(make_tariff(), None), [])

    def test_no_conflicts_returns_empty_list(self):
        base = make_tariff()
        overlay = {"fuel_cost_adjustment_yen_per_kwh": {"2026-09": 9.12}}
        self.assertEqual(bill_model.tariff_conflicts(base, overlay), [])

    def test_fuel_conflict_is_reported(self):
        base = make_tariff()
        overlay = {"fuel_cost_adjustment_yen_per_kwh": {"2026-08": 999.0}}
        self.assertEqual(
            bill_model.tariff_conflicts(base, overlay),
            [("fuel_cost_adjustment_yen_per_kwh", "2026-08")],
        )

    def test_capacity_conflict_is_reported(self):
        base = make_tariff()
        overlay = {"capacity_contribution_yen_per_month": {"2026-08": 1}}
        self.assertEqual(
            bill_model.tariff_conflicts(base, overlay),
            [("capacity_contribution_yen_per_month", "2026-08")],
        )

    def test_levy_conflict_is_reported(self):
        base = make_tariff()
        overlay = {"renewable_levy_yen_per_kwh_observed": {"2026-08..2026-08": 1.0}}
        self.assertEqual(
            bill_model.tariff_conflicts(base, overlay),
            [("renewable_levy_yen_per_kwh_observed", "2026-08..2026-08")],
        )

    def test_multiple_conflicts_are_all_reported(self):
        base = make_tariff()
        overlay = {
            "fuel_cost_adjustment_yen_per_kwh": {"2026-08": 999.0},
            "capacity_contribution_yen_per_month": {"2026-08": 1},
            "renewable_levy_yen_per_kwh_observed": {"2026-08..2026-08": 1.0},
        }
        conflicts = bill_model.tariff_conflicts(base, overlay)
        self.assertEqual(len(conflicts), 3)
        self.assertIn(("fuel_cost_adjustment_yen_per_kwh", "2026-08"), conflicts)
        self.assertIn(("capacity_contribution_yen_per_month", "2026-08"), conflicts)
        self.assertIn(("renewable_levy_yen_per_kwh_observed", "2026-08..2026-08"), conflicts)

    def test_matching_values_are_not_conflicts(self):
        base = make_tariff()
        overlay = {
            "fuel_cost_adjustment_yen_per_kwh": {"2026-08": -3.50},
            "capacity_contribution_yen_per_month": {"2026-08": 213},
            "renewable_levy_yen_per_kwh_observed": {"2026-08..2026-08": 4.18},
        }
        self.assertEqual(bill_model.tariff_conflicts(base, overlay), [])


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
        self.assertEqual(record["reason_code"], bill_model.REASON_CODE_DAILY_MISSING)
        self.assertIn("欠測", record["reason_label"])
        self.assertIn("欠測", record["reason_detail"])
        # reason_detail には内部ファイル名(daily.json)が残るが、reason_labelには出さない
        # （読者向けQAレビュー対応。表示側はreason_labelのみ使う）。
        self.assertNotIn("daily.json", record["reason_label"])

    def test_month_without_fuel_rate_is_excluded(self):
        # fuel_cost_adjustment_yen_per_kwh に billing_month が未収載（請求PDF未取得）の場合、
        # 日次データが全日揃っていても excluded_months に回す（値を捏造しない設計）。
        tariff = make_tariff()
        start, end = bill_model.billing_period("2026-09", meter_read_day=2)
        daily = {}
        d = start
        while d <= end:
            daily[d.isoformat()] = self._daily_row(d.isoformat())
            from datetime import timedelta

            d += timedelta(days=1)
        record = bill_model.build_month_record(tariff, daily, "2026-09")
        self.assertTrue(record["excluded"])
        self.assertEqual(record["reason_code"], bill_model.REASON_CODE_TARIFF_MISSING)
        self.assertIn("未確定", record["reason_label"])
        self.assertIn("未収載", record["reason_detail"])
        self.assertNotIn("fuel_cost_adjustment_yen_per_kwh", record["reason_label"])

    def test_month_without_capacity_contribution_is_excluded_with_distinct_label(self):
        # 容量拠出金未収載は燃料費調整単価未収載と同じreason_codeだが、読者向けlabelは
        # 別文言にする（「燃料費調整単価」と「容量拠出金」を混同させない）。
        tariff = make_tariff()
        tariff["fuel_cost_adjustment_yen_per_kwh"]["2026-10"] = 1.0  # fuelは収載済みにしておく
        start, end = bill_model.billing_period("2026-10", meter_read_day=2)
        daily = {}
        d = start
        while d <= end:
            daily[d.isoformat()] = self._daily_row(d.isoformat())
            from datetime import timedelta

            d += timedelta(days=1)
        record = bill_model.build_month_record(tariff, daily, "2026-10")
        self.assertTrue(record["excluded"])
        self.assertEqual(record["reason_code"], bill_model.REASON_CODE_TARIFF_MISSING)
        self.assertIn("容量拠出金", record["reason_label"])
        self.assertNotIn("capacity_contribution_yen_per_month", record["reason_label"])

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
        self.assertEqual(record["sell_source"], "official_meter")
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
        record = bill_model.build_month_record(tariff, daily, "2026-08", official_sell_by_month={})
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

    # --- L0（復元負荷ベース、旧consumption_kwhベースの廃止） ---

    def test_l0_is_null_when_daily_load_absent(self):
        # daily_load.json が無ければ捏造せず null（旧仕様の consumption_kwh フォールバックは廃止）。
        # 読者向けQAレビュー対応: l0_unavailable_reason は {reason_code, reason_label,
        # reason_detail} を持ち、reason_labelには内部ファイル名を出さない。
        tariff = make_tariff()
        daily = self._full_month_daily("2026-08")
        record = bill_model.build_month_record(tariff, daily, "2026-08")
        self.assertIsNone(record["bill_l0_no_solar"])
        l0_reason = record["l0_unavailable_reason"]
        self.assertEqual(l0_reason["reason_code"], bill_model.REASON_CODE_PROFILE_MISSING)
        self.assertEqual(l0_reason["reason_label"], "5分プロファイル未取得")
        self.assertIn("daily_load.json", l0_reason["reason_detail"])
        self.assertNotIn("daily_load.json", l0_reason["reason_label"])
        self.assertIsNone(record["saving_yen_fit"])
        self.assertIsNone(record["saving_yen_post_fit"])

    def test_l0_is_null_when_daily_load_has_missing_day(self):
        tariff = make_tariff()
        daily = self._full_month_daily("2026-08")
        start, end = bill_model.billing_period("2026-08", meter_read_day=2)
        daily_load = {}
        d = start
        while d <= end:
            if d.isoformat() != "2026-07-20":  # 1日だけ欠測させる
                daily_load[d.isoformat()] = {"date": d.isoformat(), "load_kwh": 20.0}
            from datetime import timedelta

            d += timedelta(days=1)
        record = bill_model.build_month_record(tariff, daily, "2026-08", daily_load_by_date=daily_load)
        self.assertIsNone(record["bill_l0_no_solar"])
        l0_reason = record["l0_unavailable_reason"]
        self.assertEqual(l0_reason["reason_code"], bill_model.REASON_CODE_PERIOD_INCOMPLETE)
        # QA再レビュー #14: period_incomplete(5分プロファイル由来)はdaily_missing
        # (daily.jsonセンサー日次欠測)の「計測データ欠測」と区別し「5分プロファイル欠落」にする。
        self.assertIn("5分プロファイル欠落", l0_reason["reason_label"])
        self.assertIn("欠測", l0_reason["reason_detail"])
        self.assertNotIn("load_kwh", l0_reason["reason_label"])
        self.assertNotIn("usage period", l0_reason["reason_label"])

    def test_l0_computed_from_restored_load_when_complete(self):
        tariff = make_tariff()
        daily = self._full_month_daily("2026-08")
        start, end = bill_model.billing_period("2026-08", meter_read_day=2)
        total_days = (end - start).days + 1
        daily_load = {}
        d = start
        while d <= end:
            daily_load[d.isoformat()] = {"date": d.isoformat(), "load_kwh": 20.0}
            from datetime import timedelta

            d += timedelta(days=1)
        record = bill_model.build_month_record(tariff, daily, "2026-08", daily_load_by_date=daily_load)
        expected_bill = bill_model.compute_bill(tariff, 20.0 * total_days, "2026-08")
        self.assertEqual(record["bill_l0_no_solar"], expected_bill.to_dict())
        self.assertIsNone(record["l0_unavailable_reason"])
        self.assertIsNotNone(record["saving_yen_fit"])

    # --- buy_source（請求実績の優先採用） ---

    def test_buy_source_sensor_by_default(self):
        tariff = make_tariff()
        daily = self._full_month_daily("2026-08")
        record = bill_model.build_month_record(tariff, daily, "2026-08")
        self.assertEqual(record["buy_source"], "sensor")
        self.assertIsNone(record["buy_diff_pct"])
        self.assertEqual(record["bill_actual"]["buy_kwh"], record["buy_kwh_sensor"])

    def test_buy_source_billed_when_official_buy_matches_period(self):
        tariff = make_tariff()
        daily = self._full_month_daily("2026-08")  # buy_kwh=1.0/日 → sensor合計31.0
        start, end = bill_model.billing_period("2026-08", meter_read_day=2)
        official_buy_by_month = {
            "2026-08": {
                "settlement_month": "2026-08",
                "period_from": start.isoformat(),
                "period_to": end.isoformat(),
                "official_buy_kwh": 83.0,
                "billed_yen": 3661,
            }
        }
        record = bill_model.build_month_record(
            tariff, daily, "2026-08", official_buy_by_month=official_buy_by_month
        )
        self.assertEqual(record["buy_source"], "billed")
        self.assertEqual(record["bill_actual"]["buy_kwh"], 83.0)
        self.assertEqual(record["buy_kwh_sensor"], 31.0)
        self.assertIsNotNone(record["buy_diff_pct"])

    def test_buy_source_falls_back_to_sensor_on_period_mismatch(self):
        tariff = make_tariff()
        daily = self._full_month_daily("2026-08")
        official_buy_by_month = {
            "2026-08": {
                "settlement_month": "2026-08",
                "period_from": "2099-01-01",
                "period_to": "2099-01-31",
                "official_buy_kwh": 83.0,
                "billed_yen": 3661,
            }
        }
        record = bill_model.build_month_record(
            tariff, daily, "2026-08", official_buy_by_month=official_buy_by_month
        )
        self.assertEqual(record["buy_source"], "sensor")
        self.assertEqual(record["bill_actual"]["buy_kwh"], record["buy_kwh_sensor"])


class BuildBillsTest(unittest.TestCase):
    def test_empty_daily_returns_empty_result(self):
        tariff = make_tariff()
        result = bill_model.build_bills(tariff, {})
        self.assertEqual(result["months"], [])
        self.assertEqual(result["excluded_months"], [])
        self.assertIn("_note", result)

    def test_excluded_months_expose_reason_triple_not_internal_names(self):
        # 読者向けQAレビュー対応: excluded_months は reason_code/reason_label/reason_detail
        # の3点セットを持ち、reason_label には内部ファイル名・キー名を出さない
        # （旧仕様の単一"reason"文字列は廃止）。
        tariff = make_tariff()
        daily = {"2026-08-15": {"date": "2026-08-15", "buy_kwh": 1.0, "solar_kwh": 10.0, "sell_kwh": 5.0}}
        result = bill_model.build_bills(tariff, daily)
        self.assertGreater(len(result["excluded_months"]), 0)
        for m in result["excluded_months"]:
            self.assertIn("reason_code", m)
            self.assertIn("reason_label", m)
            self.assertIn("reason_detail", m)
            self.assertNotIn("reason", m)
            for internal_name in ("daily.json", "tariff.json", "fuel_cost_adjustment_yen_per_kwh", "capacity_contribution_yen_per_month"):
                self.assertNotIn(internal_name, m["reason_label"])


class BuildBillsPublishSinceTest(unittest.TestCase):
    """オーナー決定2026-09-23: 全チャネルが揃う日付(publish_since)より前が請求期間開始の
    請求月を months/excluded_months から除外する。"""

    def _daily_row(self, d: str, buy=1.0, solar=10.0, sell=5.0) -> dict:
        return {"date": d, "buy_kwh": buy, "solar_kwh": solar, "sell_kwh": sell}

    def _full_two_months_daily(self) -> dict:
        # 2026-08(2026-07-02〜2026-08-01)・2026-09(2026-08-02〜2026-09-01)を両方全日揃える。
        from datetime import timedelta

        tariff_start, _ = bill_model.billing_period("2026-08", meter_read_day=2)
        _, tariff_end = bill_model.billing_period("2026-09", meter_read_day=2)
        daily = {}
        d = tariff_start
        while d <= tariff_end:
            daily[d.isoformat()] = self._daily_row(d.isoformat())
            d += timedelta(days=1)
        return daily

    def test_month_starting_before_publish_since_is_excluded(self):
        tariff = make_tariff(
            capacity_contribution_yen_per_month={"2026-08": 213, "2026-09": 213},
            fuel_cost_adjustment_yen_per_kwh={"2026-07": 8.69, "2026-08": -3.50, "2026-09": -3.50},
        )
        daily = self._full_two_months_daily()
        # 2026-09の請求期間開始日(2026-08-02)をpublish_sinceにすると、2026-08は期間開始が
        # 前なので除外され、2026-09だけが残る。
        result = bill_model.build_bills(tariff, daily, publish_since="2026-08-02")
        billing_months = {m["billing_month"] for m in result["months"]}
        excluded_months = {m["billing_month"] for m in result["excluded_months"]}
        self.assertNotIn("2026-08", billing_months | excluded_months)
        self.assertIn("2026-09", billing_months)

    def test_none_publish_since_keeps_all_months(self):
        # 回帰防止: publish_since省略時は従来どおりフィルタなし。
        tariff = make_tariff(
            capacity_contribution_yen_per_month={"2026-08": 213, "2026-09": 213},
            fuel_cost_adjustment_yen_per_kwh={"2026-07": 8.69, "2026-08": -3.50, "2026-09": -3.50},
        )
        daily = self._full_two_months_daily()
        result = bill_model.build_bills(tariff, daily)
        billing_months = {m["billing_month"] for m in result["months"]}
        self.assertIn("2026-08", billing_months)
        self.assertIn("2026-09", billing_months)


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


class LoadOfficialBuyTest(unittest.TestCase):
    def test_missing_file_returns_empty_dict(self):
        result = bill_model.load_official_buy(Path("/nonexistent/official_buy.json"))
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
                    "official_buy_kwh": 83.0,
                    "billed_yen": 3661,
                }
            ]
        }
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "official_buy.json"
            path.write_text(json.dumps(payload), encoding="utf-8")
            result = bill_model.load_official_buy(path)
        self.assertIn("2026-08", result)
        self.assertEqual(result["2026-08"]["official_buy_kwh"], 83.0)


class LoadDailyLoadTest(unittest.TestCase):
    def test_missing_file_returns_empty_dict(self):
        result = bill_model.load_daily_load(Path("/nonexistent/daily_load.json"))
        self.assertEqual(result, {})

    def test_loads_and_indexes_by_date(self):
        import json
        import tempfile

        payload = {"days": [{"date": "2026-09-01", "load_kwh": 26.76}]}
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "daily_load.json"
            path.write_text(json.dumps(payload), encoding="utf-8")
            result = bill_model.load_daily_load(path)
        self.assertIn("2026-09-01", result)
        self.assertEqual(result["2026-09-01"]["load_kwh"], 26.76)


if __name__ == "__main__":
    unittest.main()


class PerKwhPricesRoundingTest(unittest.TestCase):
    def test_buy_price_is_rounded_to_2_decimals(self):
        """27 + 9.16 + 4.18 は float では 40.339999999999996 になる。公開 JSON に丸め誤差の桁を
        出さない（G6 の 13 桁検査に引っかかり公開が止まった実例 2026-09-27）。"""
        tariff = {
            "energy_tiers_yen_per_kwh": [{"yen_per_kwh": 27.0}],
            "fuel_cost_adjustment_yen_per_kwh": {"2026-09": 9.16},
            "capacity_contribution_yen_per_month": {"2026-09": 477},
            "renewable_levy_yen_per_kwh": {"2026-05..2027-04": 4.18},
            "sell_price_yen_per_kwh": {"fit": 16.0, "post_fit_assumed_for_readers": 8.0},
        }
        buy, _fit, _post, provisional, _src = bill_model.per_kwh_prices(tariff, "2026-09")
        self.assertEqual(buy, 40.34)
        self.assertFalse(provisional)
