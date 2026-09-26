#!/usr/bin/env python3
"""build_daily.py の unittest。

実行方法:
  cd scripts/blog-metrics && python3 -m unittest test_build_daily -v
"""
import copy
import sys
import unittest
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import build_daily  # noqa: E402


def make_tariff(**overrides) -> dict:
    """テスト用の最小 tariff.json 相当の dict を作る（test_bill_model.py と同じ最小構成）。"""
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


def make_daily_record(date: str, solar: float, buy: float, sell: float, nichicon=None, ecoflow=None, status=None) -> dict:
    record = {
        "date": date,
        "solar_kwh": solar,
        "buy_kwh": buy,
        "sell_kwh": sell,
        "nichicon_charge_kwh": nichicon,
        "ecoflow_charge_kwh": ecoflow,
    }
    if status is not None:
        record["status"] = status
    return record


class DailyBuySellPriceTest(unittest.TestCase):
    def test_date_in_confirmed_billing_month_uses_its_own_rate(self):
        # meter_read_day=2 なので、day>=2の 2026-07-15 は翌月分の請求月 2026-08 に属する
        # (billing_month_for_date の仕様。2026-07-02〜2026-08-01 使用分 = 2026-08請求)。
        # 27.00(tier1) + (-3.50)(fuel_adj 2026-08) + 4.18(levy 2026-05..2027-04) = 27.68
        buy_price, sell_price = build_daily.daily_buy_sell_price(make_tariff(), "2026-07-15")
        self.assertAlmostEqual(buy_price, 27.68, places=2)
        self.assertEqual(sell_price, 16.0)

    def test_date_before_meter_read_day_belongs_to_same_month(self):
        # 2026-08-01(day=1<meter_read_day=2)は請求月2026-08に属する（2026-07-02〜2026-08-01が
        # 2026-08請求分のため）。
        buy_price, _ = build_daily.daily_buy_sell_price(make_tariff(), "2026-08-01")
        self.assertAlmostEqual(buy_price, 27.68, places=2)

    def test_date_without_confirmed_month_falls_back_to_latest_confirmed(self):
        # 2026-09-10(day=10>=2)の請求月は2026-10。fuel_cost_adjustmentに2026-10は無いため、
        # 直近確定月(2026-08)の値が暫定適用される。
        buy_price, _ = build_daily.daily_buy_sell_price(make_tariff(), "2026-09-10")
        self.assertAlmostEqual(buy_price, 27.68, places=2)


class BuildDailyRowsTest(unittest.TestCase):
    def test_saving_yen_formula(self):
        rows = build_daily.build_daily_rows(
            [make_daily_record("2026-08-05", solar=10.0, buy=0.0, sell=2.0)],
            tariff=make_tariff(),
        )
        # max(10-2,0)*27.68 + 2*16.0 = 221.44 + 32 = 253.44 -> round = 253
        self.assertEqual(rows[0]["saving_yen"], 253)

    def test_null_solar_or_sell_yields_null_saving_yen(self):
        # QA指摘#3: metrics-export.sh の daily は solar_kwh/buy_kwh/sell_kwh のいずれか1つでも
        # 非NULLなら行を返す(WHERE ... OR ...)ため、buy_kwhだけ確定でsolar_kwh/sell_kwhが
        # NULLのPARTIAL日がありうる。以前は solar - sell で TypeError になっていた。
        rows = build_daily.build_daily_rows(
            [
                make_daily_record("2026-08-05", solar=None, buy=1.0, sell=None, status="PARTIAL"),
                make_daily_record("2026-08-06", solar=5.0, buy=1.0, sell=None, status="PARTIAL"),
                make_daily_record("2026-08-07", solar=None, buy=1.0, sell=2.0, status="PARTIAL"),
            ],
            tariff=make_tariff(),
        )
        self.assertEqual(len(rows), 3)
        for row in rows:
            self.assertIsNone(row["saving_yen"])

    def test_self_consumption_shift_null_when_both_missing(self):
        rows = build_daily.build_daily_rows(
            [make_daily_record("2026-08-05", solar=1.0, buy=0.0, sell=0.0)],
            tariff=make_tariff(),
        )
        self.assertIsNone(rows[0]["self_consumption_shift_kwh"])

    def test_self_consumption_shift_coalesces_missing_side_to_zero(self):
        rows = build_daily.build_daily_rows(
            [make_daily_record("2026-08-05", solar=1.0, buy=0.0, sell=0.0, nichicon=2.5, ecoflow=None)],
            tariff=make_tariff(),
        )
        self.assertEqual(rows[0]["self_consumption_shift_kwh"], 2.5)

    def test_self_consumption_shift_sums_both_sides(self):
        rows = build_daily.build_daily_rows(
            [make_daily_record("2026-08-05", solar=1.0, buy=0.0, sell=0.0, nichicon=1.0, ecoflow=2.111)],
            tariff=make_tariff(),
        )
        self.assertEqual(rows[0]["self_consumption_shift_kwh"], 3.111)

    def test_status_missing_is_excluded(self):
        rows = build_daily.build_daily_rows(
            [make_daily_record("2026-08-05", solar=1.0, buy=0.0, sell=0.0, status="MISSING")],
            tariff=make_tariff(),
        )
        self.assertEqual(rows, [])

    def test_status_partial_is_included(self):
        rows = build_daily.build_daily_rows(
            [make_daily_record("2026-08-05", solar=1.0, buy=0.0, sell=0.0, status="PARTIAL")],
            tariff=make_tariff(),
        )
        self.assertEqual(len(rows), 1)

    def test_status_final_is_included(self):
        # SolarChargeController側 metrics-export.sh の実インターフェイス
        # (status IN ('COMPLETE','FINAL','PARTIAL')) に合わせ、FINAL も採用する。
        rows = build_daily.build_daily_rows(
            [make_daily_record("2026-08-05", solar=1.0, buy=0.0, sell=0.0, status="FINAL")],
            tariff=make_tariff(),
        )
        self.assertEqual(len(rows), 1)

    def test_status_complete_is_included(self):
        rows = build_daily.build_daily_rows(
            [make_daily_record("2026-08-05", solar=1.0, buy=0.0, sell=0.0, status="COMPLETE")],
            tariff=make_tariff(),
        )
        self.assertEqual(len(rows), 1)

    def test_missing_status_key_is_included(self):
        rows = build_daily.build_daily_rows(
            [make_daily_record("2026-08-05", solar=1.0, buy=0.0, sell=0.0)],
            tariff=make_tariff(),
        )
        self.assertEqual(len(rows), 1)

    def test_deprecated_keys_are_absent(self):
        rows = build_daily.build_daily_rows(
            [make_daily_record("2026-08-05", solar=1.0, buy=0.0, sell=0.0, nichicon=1.0, ecoflow=1.0)],
            tariff=make_tariff(),
        )
        self.assertEqual(
            set(rows[0].keys()),
            {
                "date",
                "solar_kwh",
                "buy_kwh",
                "sell_kwh",
                "nichicon_charge_kwh",
                "ecoflow_charge_kwh",
                "self_consumption_shift_kwh",
                "saving_yen",
            },
        )

    def test_rows_are_sorted_by_date(self):
        rows = build_daily.build_daily_rows(
            [
                make_daily_record("2026-08-06", solar=1.0, buy=0.0, sell=0.0),
                make_daily_record("2026-08-05", solar=1.0, buy=0.0, sell=0.0),
            ],
            tariff=make_tariff(),
        )
        self.assertEqual([r["date"] for r in rows], ["2026-08-05", "2026-08-06"])


class TariffHistoryImmutabilityTest(unittest.TestCase):
    """QA指摘対応: tariff.jsonに新しい月の単価を追加しても、既に確定単価を持つ過去日の
    saving_yenは変わらない（旧実装は『tariff.json中の最新月』を全履歴に一律適用していたため、
    月を追加するたびpush済みの過去日の値が動きvalidate_metrics.pyのG9がpushを拒否していた）。"""

    def test_adding_a_newer_month_does_not_change_older_confirmed_day(self):
        # 2026-07-15(day>=2)の請求月は2026-08で、tariff_v1で既に確定済み。
        # 2026-09という無関係な新月を追加しても、この日の請求月(2026-08)の確定値は変わらない。
        tariff_v1 = make_tariff()
        record = make_daily_record("2026-07-15", solar=10.0, buy=0.0, sell=2.0)

        rows_v1 = build_daily.build_daily_rows([record], tariff=tariff_v1)

        tariff_v2 = copy.deepcopy(tariff_v1)
        tariff_v2["fuel_cost_adjustment_yen_per_kwh"]["2026-09"] = 99.0  # 大きく異なる新月を追加
        rows_v2 = build_daily.build_daily_rows([record], tariff=tariff_v2)

        self.assertEqual(rows_v1[0]["saving_yen"], rows_v2[0]["saving_yen"])

    def test_adding_a_newer_month_changes_meta_current_price_but_not_daily(self):
        tariff_v1 = make_tariff()
        record = make_daily_record("2026-07-15", solar=10.0, buy=0.0, sell=2.0)
        rows_v1 = build_daily.build_daily_rows([record], tariff=tariff_v1)

        tariff_v2 = copy.deepcopy(tariff_v1)
        tariff_v2["fuel_cost_adjustment_yen_per_kwh"]["2026-09"] = 99.0
        rows_v2 = build_daily.build_daily_rows([record], tariff=tariff_v2)

        # 過去日(2026-07-15、請求月2026-08)のsaving_yenは不変
        self.assertEqual(rows_v1[0]["saving_yen"], rows_v2[0]["saving_yen"])
        # しかしmeta.json(today=2026-09-10基準)が表示する「現在の単価」は新月の値に更新される
        meta_v1 = build_daily.build_meta({}, tariff_v1, today=date(2026, 9, 10))
        meta_v2 = build_daily.build_meta({}, tariff_v2, today=date(2026, 9, 10))
        self.assertNotEqual(meta_v1["buy_price_yen_per_kwh"], meta_v2["buy_price_yen_per_kwh"])


class BuildMonthlyRowsTest(unittest.TestCase):
    def test_monthly_equals_sum_of_daily_for_each_key(self):
        tariff = make_tariff()
        daily_rows = build_daily.build_daily_rows(
            [
                make_daily_record("2026-08-05", solar=10.0, buy=1.0, sell=2.0, nichicon=1.0, ecoflow=2.0),
                make_daily_record("2026-08-06", solar=5.0, buy=0.5, sell=1.0, nichicon=3.0, ecoflow=None),
            ],
            tariff=tariff,
        )
        monthly_rows = build_daily.build_monthly_rows(daily_rows)
        self.assertEqual(len(monthly_rows), 1)
        month = monthly_rows[0]
        self.assertEqual(month["month"], "2026-08")
        self.assertAlmostEqual(month["solar_kwh"], 15.0, places=3)
        self.assertAlmostEqual(month["buy_kwh"], 1.5, places=3)
        self.assertAlmostEqual(month["sell_kwh"], 3.0, places=3)
        self.assertAlmostEqual(month["nichicon_charge_kwh"], 4.0, places=3)
        # SQLのSUMと同じくnullは無視して合計する（欠測日を0扱いにしない）
        self.assertAlmostEqual(month["ecoflow_charge_kwh"], 2.0, places=3)
        self.assertEqual(month["saving_yen"], daily_rows[0]["saving_yen"] + daily_rows[1]["saving_yen"])

    def test_monthly_saving_yen_ignores_null_days(self):
        # QA指摘#3のフォローアップ: saving_yenがNULLの日を含む月でも、NULLでない日だけ
        # 合算する（SQLのSUMと同じ挙動。全日NULLなら月もNULL、他のMONTHLY_SUM_KEYSと同じ扱い）。
        daily_rows = build_daily.build_daily_rows(
            [
                make_daily_record("2026-08-05", solar=10.0, buy=1.0, sell=2.0),
                make_daily_record("2026-08-06", solar=None, buy=1.0, sell=None, status="PARTIAL"),
            ],
            tariff=make_tariff(),
        )
        monthly_rows = build_daily.build_monthly_rows(daily_rows)
        self.assertEqual(monthly_rows[0]["saving_yen"], daily_rows[0]["saving_yen"])

    def test_monthly_saving_yen_is_null_when_entire_month_null(self):
        daily_rows = build_daily.build_daily_rows(
            [make_daily_record("2026-08-05", solar=None, buy=1.0, sell=None, status="PARTIAL")],
            tariff=make_tariff(),
        )
        monthly_rows = build_daily.build_monthly_rows(daily_rows)
        self.assertIsNone(monthly_rows[0]["saving_yen"])

    def test_monthly_column_is_null_when_entire_month_missing(self):
        daily_rows = build_daily.build_daily_rows(
            [make_daily_record("2026-08-05", solar=1.0, buy=0.0, sell=0.0, nichicon=None, ecoflow=None)],
            tariff=make_tariff(),
        )
        monthly_rows = build_daily.build_monthly_rows(daily_rows)
        self.assertIsNone(monthly_rows[0]["nichicon_charge_kwh"])
        self.assertIsNone(monthly_rows[0]["ecoflow_charge_kwh"])
        self.assertIsNone(monthly_rows[0]["self_consumption_shift_kwh"])

    def test_two_months_produce_two_rows(self):
        daily_rows = build_daily.build_daily_rows(
            [
                make_daily_record("2026-07-31", solar=1.0, buy=0.0, sell=0.0),
                make_daily_record("2026-08-05", solar=2.0, buy=0.0, sell=0.0),
            ],
            tariff=make_tariff(),
        )
        monthly_rows = build_daily.build_monthly_rows(daily_rows)
        self.assertEqual([m["month"] for m in monthly_rows], ["2026-07", "2026-08"])

    def test_empty_daily_rows_produce_empty_monthly_rows(self):
        self.assertEqual(build_daily.build_monthly_rows([]), [])


class BuildMetaTest(unittest.TestCase):
    def test_passes_through_export_meta_fields(self):
        meta = build_daily.build_meta(
            {
                "power_history_since": "2026-03-07",
                "nichicon_data_since": "2026-07-27",
                "ecoflow_data_since": "2026-09-06",
            },
            make_tariff(),
            today=date(2026, 8, 5),
        )
        self.assertEqual(meta["power_history_since"], "2026-03-07")
        self.assertEqual(meta["nichicon_data_since"], "2026-07-27")
        self.assertEqual(meta["ecoflow_data_since"], "2026-09-06")
        self.assertAlmostEqual(meta["buy_price_yen_per_kwh"], 27.68, places=2)
        self.assertEqual(meta["sell_price_yen_per_kwh"], 16.0)
        self.assertEqual(meta["buy_sell_price_effective_month"], "2026-08")
        self.assertIn("契約中の新電力プラン", meta["buy_sell_price_source"])
        # 読者向け文言に社内ファイル名を出さない（オーナー決定2026-09-23）
        self.assertNotIn("tariff.json", meta["buy_sell_price_source"])
        self.assertIsNone(meta["publish_since"])
        # generated_at は 'YYYY-MM-DD HH:MM:SS' 形式で書けること（フォーマット崩れの検知）
        self.assertRegex(meta["generated_at"], r"^\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}$")

    def test_today_without_confirmed_month_uses_provisional_source_month(self):
        meta = build_daily.build_meta({}, make_tariff(), today=date(2026, 9, 10))
        # 2026-09は未確定のため、直近確定月(2026-08)の値が暫定適用され、
        # effective_monthはその出典月(2026-08)を指す。
        self.assertEqual(meta["buy_sell_price_effective_month"], "2026-08")


class EmptyInputTest(unittest.TestCase):
    def test_empty_daily_export_produces_empty_daily_and_monthly(self):
        daily_rows = build_daily.build_daily_rows([], tariff=make_tariff())
        self.assertEqual(daily_rows, [])
        self.assertEqual(build_daily.build_monthly_rows(daily_rows), [])


class PublishSinceFilterTest(unittest.TestCase):
    """オーナー決定2026-09-23: 全チャネルが揃う日付(publish_since)より前の断片的な日次・月次は
    公開JSONに含めない。build_daily.py はmain()の--publish-sinceでdaily_rowsを絞り込む
    （build_daily_rows自体は変えず、mainで呼ぶフィルタをここでは同じロジックで検証する）。"""

    def _filtered_rows(self, records, publish_since):
        rows = build_daily.build_daily_rows(records, tariff=make_tariff())
        if publish_since:
            rows = [r for r in rows if r["date"] >= publish_since]
        return rows

    def test_rows_before_publish_since_are_excluded(self):
        records = [
            make_daily_record("2026-08-27", solar=1.0, buy=0.0, sell=0.0),
            make_daily_record("2026-08-28", solar=1.0, buy=0.0, sell=0.0),
            make_daily_record("2026-08-29", solar=1.0, buy=0.0, sell=0.0),
        ]
        rows = self._filtered_rows(records, "2026-08-29")
        self.assertEqual([r["date"] for r in rows], ["2026-08-29"])

    def test_none_publish_since_keeps_all_rows(self):
        records = [
            make_daily_record("2026-08-27", solar=1.0, buy=0.0, sell=0.0),
            make_daily_record("2026-08-29", solar=1.0, buy=0.0, sell=0.0),
        ]
        rows = self._filtered_rows(records, None)
        self.assertEqual([r["date"] for r in rows], ["2026-08-27", "2026-08-29"])

    def test_all_rows_before_publish_since_yields_empty_list(self):
        # 失敗系: publish_sinceがすべての行より後の場合、クラッシュせず空配列になる。
        records = [make_daily_record("2026-08-05", solar=1.0, buy=0.0, sell=0.0)]
        rows = self._filtered_rows(records, "2099-01-01")
        self.assertEqual(rows, [])

    def test_meta_records_publish_since_value(self):
        meta = build_daily.build_meta({}, make_tariff(), today=date(2026, 9, 23), publish_since="2026-08-29")
        self.assertEqual(meta["publish_since"], "2026-08-29")


if __name__ == "__main__":
    unittest.main()
