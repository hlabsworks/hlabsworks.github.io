#!/usr/bin/env python3
"""import_official_buy.py の unittest。

実行方法:
  cd scripts/blog-metrics && python3 -m unittest test_import_official_buy -v
"""
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import import_official_buy  # noqa: E402


def make_source_month(**overrides) -> dict:
    month = {
        "billing_ym": "2026-08",
        "period": "2026年7月2日 ～ 2026年8月1日",
        "usage_kwh": 83.0,
        "billed_yen": 3661.0,
        "energy_rate": 27.0,
        "energy_yen": 2241.0,
        "fuel_rate": 10.38,
        "fuel_yen": 861.54,
        "levy_rate": 4.18,
        "levy_yen": 346.0,
        "capacity_yen": 213.0,
        "prev_meter": 25259.1,
        "meter_diff": 83.3,
    }
    month.update(overrides)
    return month


class ParsePeriodTest(unittest.TestCase):
    def test_parses_standard_period(self):
        period_from, period_to = import_official_buy._parse_period("2026年7月2日 ～ 2026年8月1日")
        self.assertEqual(period_from, "2026-07-02")
        self.assertEqual(period_to, "2026-08-01")

    def test_parses_year_boundary_period(self):
        period_from, period_to = import_official_buy._parse_period("2025年12月2日 ～ 2026年1月1日")
        self.assertEqual(period_from, "2025-12-02")
        self.assertEqual(period_to, "2026-01-01")

    def test_unparsable_period_raises(self):
        with self.assertRaises(ValueError):
            import_official_buy._parse_period("2026/07/02-2026/08/01")


class ExtractMonthTest(unittest.TestCase):
    def test_uses_usage_kwh_not_meter_diff(self):
        # usage_kwh(請求書の実請求額基準)を採用する。meter_diff(検針差分の生値)は使わない
        # — compute_bill(usage_kwh) が billed_yen と完全一致することを確認済みのため。
        month = make_source_month()
        extracted = import_official_buy.extract_month(month)
        self.assertEqual(extracted["official_buy_kwh"], 83.0)
        self.assertNotEqual(extracted["official_buy_kwh"], month["meter_diff"])

    def test_extracted_fields_exclude_contract_identifiers(self):
        month = make_source_month()
        extracted = import_official_buy.extract_month(month)
        self.assertEqual(
            set(extracted.keys()),
            {"settlement_month", "period_from", "period_to", "official_buy_kwh", "billed_yen"},
        )
        self.assertNotIn("prev_meter", extracted)
        self.assertNotIn("meter_diff", extracted)
        self.assertNotIn("energy_rate", extracted)

    def test_period_converted_to_iso(self):
        month = make_source_month()
        extracted = import_official_buy.extract_month(month)
        self.assertEqual(extracted["period_from"], "2026-07-02")
        self.assertEqual(extracted["period_to"], "2026-08-01")

    def test_missing_required_field_raises(self):
        month = make_source_month()
        del month["usage_kwh"]
        with self.assertRaises(KeyError):
            import_official_buy.extract_month(month)


class ExtractMonthsTest(unittest.TestCase):
    def test_extracts_all_months_in_order(self):
        source = {"months": [make_source_month(billing_ym="2026-07"), make_source_month(billing_ym="2026-08")]}
        months = import_official_buy.extract_months(source)
        self.assertEqual([m["settlement_month"] for m in months], ["2026-07", "2026-08"])

    def test_missing_months_key_raises(self):
        with self.assertRaises(KeyError):
            import_official_buy.extract_months({})


class BuildOfficialBuyTest(unittest.TestCase):
    def test_includes_generated_at_and_months(self):
        source = {"months": [make_source_month()]}
        result = import_official_buy.build_official_buy(source)
        self.assertIn("generated_at", result)
        self.assertIn("source_note", result)
        self.assertEqual(len(result["months"]), 1)


class MainMissingSourceTest(unittest.TestCase):
    def test_main_exits_nonzero_when_source_missing(self):
        import subprocess
        import tempfile

        script = str(Path(__file__).resolve().parent / "import_official_buy.py")
        with tempfile.TemporaryDirectory() as tmp:
            out_path = Path(tmp) / "official_buy.json"
            proc = subprocess.run(
                [sys.executable, script, "--source", str(Path(tmp) / "nonexistent.json"), "--out", str(out_path)],
                capture_output=True,
                text=True,
            )
            self.assertNotEqual(proc.returncode, 0)
            self.assertFalse(out_path.exists())


if __name__ == "__main__":
    unittest.main()
