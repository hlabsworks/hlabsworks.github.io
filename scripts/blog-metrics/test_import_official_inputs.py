#!/usr/bin/env python3
"""import_official_inputs.py の unittest。

実行方法:
  cd scripts/blog-metrics && python3 -m unittest test_import_official_inputs -v
"""
from __future__ import annotations

import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import import_official_inputs  # noqa: E402
import validate_metrics  # noqa: E402


def make_base_tariff(**overrides) -> dict:
    """test_bill_model.make_tariff() 相当（本ファイル単独で完結させるため複製）。"""
    tariff = {
        "retailer": "テスト電力",
        "plan": "テストプランS",
        "area": "テスト",
        "basic_fee_yen_per_month": 0,
        "energy_tiers_yen_per_kwh": [
            {"up_to_kwh": 400, "yen_per_kwh": 27.00},
            {"up_to_kwh": None, "yen_per_kwh": 26.00},
        ],
        "renewable_levy_yen_per_kwh": {"2026-05..2027-04": 4.18},
        "capacity_contribution_yen_per_month": {},
        "fuel_cost_adjustment_yen_per_kwh": {},
        "sell_price_yen_per_kwh": {"fit": 16.0, "post_fit_assumed_for_readers": 8.0},
        "meter_read_day": 2,
    }
    tariff.update(overrides)
    return tariff


# usage_kwh=50.0, fuel_rate=5.0, capacity_yen=200, levy_rate=4.18(base の年度レンジと同一) での
# compute_bill total_yen を手計算した値。energy=50*27.00=1350 fuel=50*5.0=250
# levy=floor(50*4.18)=209 capacity=200 → total=floor(1350+250+209+200)=2009
CORRECT_BILLED_YEN = 2009


def make_bill_breakdown_month(**overrides) -> dict:
    month = {
        "billing_ym": "2026-09",
        "period": "2026年8月2日 ～ 2026年9月1日",
        "usage_kwh": 50.0,
        "billed_yen": CORRECT_BILLED_YEN,
        "fuel_rate": 5.0,
        "capacity_yen": 200,
        "levy_rate": 4.18,
    }
    month.update(overrides)
    return month


def make_purchase_monthly_month(**overrides) -> dict:
    month = {
        "settlement_month": "2026-09",
        "period_from": "2026-08-02",
        "period_to": "2026-09-01",
        "purchase_yen": 1600,
        "purchase_kwh_from_yen": 100.0,
        "kwh_from_meter_diff": 100.0,
    }
    month.update(overrides)
    return month


class BuildInputsTest(unittest.TestCase):
    def test_matching_month_is_included_in_official_buy_and_tariff_months(self):
        base = make_base_tariff()
        bill_breakdown = {"months": [make_bill_breakdown_month()]}
        purchase_monthly = {"months": [make_purchase_monthly_month()]}

        result = import_official_inputs.build_inputs(base, bill_breakdown, purchase_monthly)

        self.assertIsNotNone(result["official_buy"])
        self.assertEqual([m["settlement_month"] for m in result["official_buy"]["months"]], ["2026-09"])
        self.assertEqual(result["tariff_months"]["fuel_cost_adjustment_yen_per_kwh"]["2026-09"], 5.0)
        self.assertEqual(result["tariff_months"]["capacity_contribution_yen_per_month"]["2026-09"], 200)
        self.assertEqual(result["tariff_months"]["renewable_levy_yen_per_kwh_observed"]["2026-09..2026-09"], 4.18)
        self.assertEqual(result["excluded_months"], {})
        self.assertIsNotNone(result["official_sell"])

    def test_mismatched_billed_yen_is_excluded_with_reconcile_mismatch(self):
        base = make_base_tariff()
        bill_breakdown = {"months": [make_bill_breakdown_month(billed_yen=CORRECT_BILLED_YEN + 500)]}
        purchase_monthly = {"months": []}

        result = import_official_inputs.build_inputs(base, bill_breakdown, purchase_monthly)

        self.assertIsNone(result["official_buy"])
        self.assertEqual(result["excluded_months"], {"2026-09": "reconcile_mismatch"})
        self.assertNotIn("2026-09", result["tariff_months"]["fuel_cost_adjustment_yen_per_kwh"])

    def test_non_integer_capacity_yen_is_excluded_with_parse_incomplete(self):
        base = make_base_tariff()
        bill_breakdown = {"months": [make_bill_breakdown_month(capacity_yen=200.5)]}
        purchase_monthly = {"months": []}

        result = import_official_inputs.build_inputs(base, bill_breakdown, purchase_monthly)

        self.assertIsNone(result["official_buy"])
        self.assertEqual(result["excluded_months"], {"2026-09": "parse_incomplete"})

    def test_month_already_confirmed_in_base_with_conflicting_value_is_excluded(self):
        base = make_base_tariff(
            fuel_cost_adjustment_yen_per_kwh={"2026-09": 999.0},
            capacity_contribution_yen_per_month={"2026-09": 1},
        )
        bill_breakdown = {"months": [make_bill_breakdown_month()]}
        purchase_monthly = {"months": []}

        result = import_official_inputs.build_inputs(base, bill_breakdown, purchase_monthly)

        self.assertIsNone(result["official_buy"])
        self.assertEqual(result["excluded_months"], {"2026-09": "levy_conflict"})

    def test_sell_month_without_matching_buy_is_marked_missing_official_buy(self):
        base = make_base_tariff()
        bill_breakdown = {"months": []}
        purchase_monthly = {"months": [make_purchase_monthly_month()]}

        result = import_official_inputs.build_inputs(base, bill_breakdown, purchase_monthly)

        self.assertEqual(result["excluded_months"], {"2026-09": "missing_official_buy"})
        self.assertIsNotNone(result["official_sell"])


class OutputSchemaTest(unittest.TestCase):
    """import_official_inputs.py の出力が validate_metrics.py の G6(秘匿情報deny)・
    G18(inputsスキーマ)を通ることを確認する（handoff→clone/inputsへ反映される前に
    stage_inputsがかける検査と同じもの）。"""

    def test_output_passes_g6_and_g18(self):
        base = make_base_tariff()
        bill_breakdown = {"months": [make_bill_breakdown_month()]}
        purchase_monthly = {"months": [make_purchase_monthly_month()]}
        result = import_official_inputs.build_inputs(base, bill_breakdown, purchase_monthly)

        generated_at = "2026-09-26 07:00:00"
        official_buy = dict(result["official_buy"], generated_at=generated_at)
        official_sell = dict(result["official_sell"], generated_at=generated_at)
        tariff_months = dict(result["tariff_months"], generated_at=generated_at)

        for relpath, data in (
            ("inputs/official_buy.json", official_buy),
            ("inputs/official_sell.json", official_sell),
            ("inputs/tariff_months.json", tariff_months),
        ):
            text = json.dumps(data, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
            validate_metrics.gate6_secret_deny(text, relpath)

        validate_metrics.gate18_input_files_schema(
            {
                "inputs/official_buy.json": official_buy,
                "inputs/official_sell.json": official_sell,
                "inputs/tariff_months.json": tariff_months,
            },
            meter_read_day=base["meter_read_day"],
            sell_fit=base["sell_price_yen_per_kwh"]["fit"],
        )
        # G19: 実際に突合0円である月だけ採用しているので、素通りするはず
        validate_metrics.gate19_official_buy_reconcile(base, official_buy, tariff_months)


class CliIdempotencyTest(unittest.TestCase):
    def _run_cli(self, tmp: Path, bill_breakdown_path: Path, purchase_monthly_path: Path, base_tariff_path: Path, out_dir: Path) -> dict:
        script = str(Path(__file__).resolve().parent / "import_official_inputs.py")
        proc = subprocess.run(
            [
                sys.executable, script,
                "--bill-breakdown", str(bill_breakdown_path),
                "--purchase-monthly", str(purchase_monthly_path),
                "--base-tariff", str(base_tariff_path),
                "--out-dir", str(out_dir),
            ],
            capture_output=True, text=True,
        )
        self.assertEqual(proc.returncode, 0, proc.stderr)
        return json.loads(proc.stdout)

    def test_second_run_does_not_rewrite_unchanged_output(self):
        with tempfile.TemporaryDirectory() as tmp_str:
            tmp = Path(tmp_str)
            base_tariff_path = tmp / "tariff.json"
            base_tariff_path.write_text(json.dumps(make_base_tariff()), encoding="utf-8")
            bill_breakdown_path = tmp / "bill_breakdown.json"
            bill_breakdown_path.write_text(json.dumps({"months": [make_bill_breakdown_month()]}), encoding="utf-8")
            purchase_monthly_path = tmp / "purchase_monthly.json"
            purchase_monthly_path.write_text(json.dumps({"months": [make_purchase_monthly_month()]}), encoding="utf-8")
            out_dir = tmp / "out"

            first = self._run_cli(tmp, bill_breakdown_path, purchase_monthly_path, base_tariff_path, out_dir)
            self.assertEqual(len(first["written"]), 3)

            mtimes_before = {p.name: p.stat().st_mtime_ns for p in out_dir.glob("*.json")}

            second = self._run_cli(tmp, bill_breakdown_path, purchase_monthly_path, base_tariff_path, out_dir)
            self.assertEqual(second["written"], [])

            mtimes_after = {p.name: p.stat().st_mtime_ns for p in out_dir.glob("*.json")}
            self.assertEqual(mtimes_before, mtimes_after)

    def test_missing_bill_breakdown_exits_nonzero(self):
        with tempfile.TemporaryDirectory() as tmp_str:
            tmp = Path(tmp_str)
            base_tariff_path = tmp / "tariff.json"
            base_tariff_path.write_text(json.dumps(make_base_tariff()), encoding="utf-8")
            purchase_monthly_path = tmp / "purchase_monthly.json"
            purchase_monthly_path.write_text(json.dumps({"months": []}), encoding="utf-8")
            out_dir = tmp / "out"

            script = str(Path(__file__).resolve().parent / "import_official_inputs.py")
            proc = subprocess.run(
                [
                    sys.executable, script,
                    "--bill-breakdown", str(tmp / "nonexistent.json"),
                    "--purchase-monthly", str(purchase_monthly_path),
                    "--base-tariff", str(base_tariff_path),
                    "--out-dir", str(out_dir),
                ],
                capture_output=True, text=True,
            )
            self.assertNotEqual(proc.returncode, 0)
            self.assertFalse(out_dir.exists())


if __name__ == "__main__":
    unittest.main()
