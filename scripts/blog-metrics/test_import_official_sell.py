#!/usr/bin/env python3
"""import_official_sell.py の unittest。

実行方法:
  cd scripts/blog-metrics && python3 -m unittest test_import_official_sell -v
"""
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import import_official_sell  # noqa: E402


def make_source_month(**overrides) -> dict:
    month = {
        "settlement_month": "2026-06",
        "period_from": "2026-05-02",
        "period_to": "2026-06-01",
        "meter_read_day": "06-04",
        "purchase_yen": 16160,
        "purchase_kwh_from_yen": 1010.0,
        "meter_reading_kwh": 14627.9,
        "kwh_from_meter_diff": 1010.1,
    }
    month.update(overrides)
    return month


class ExtractMonthTest(unittest.TestCase):
    def test_prefers_kwh_from_meter_diff_when_present(self):
        month = make_source_month()
        extracted = import_official_sell.extract_month(month)
        self.assertEqual(extracted["official_sell_kwh"], 1010.1)

    def test_falls_back_to_purchase_kwh_from_yen_when_meter_diff_null(self):
        month = make_source_month(kwh_from_meter_diff=None)
        extracted = import_official_sell.extract_month(month)
        self.assertEqual(extracted["official_sell_kwh"], 1010.0)

    def test_extracted_fields_exclude_contract_identifiers(self):
        month = make_source_month()
        extracted = import_official_sell.extract_month(month)
        self.assertEqual(
            set(extracted.keys()),
            {"settlement_month", "period_from", "period_to", "official_sell_kwh", "sell_revenue_yen"},
        )
        self.assertNotIn("meter_read_day", extracted)
        self.assertNotIn("meter_reading_kwh", extracted)

    def test_sell_revenue_yen_is_actual_purchase_yen(self):
        month = make_source_month()
        extracted = import_official_sell.extract_month(month)
        self.assertEqual(extracted["sell_revenue_yen"], 16160)

    def test_missing_required_field_raises(self):
        month = make_source_month()
        del month["purchase_yen"]
        with self.assertRaises(KeyError):
            import_official_sell.extract_month(month)

    def test_missing_meter_diff_and_purchase_kwh_raises(self):
        month = make_source_month(kwh_from_meter_diff=None)
        del month["purchase_kwh_from_yen"]
        with self.assertRaises(KeyError):
            import_official_sell.extract_month(month)


class ExtractMonthsTest(unittest.TestCase):
    def test_extracts_all_months_in_order(self):
        source = {"months": [make_source_month(settlement_month="2026-05"), make_source_month(settlement_month="2026-06")]}
        months = import_official_sell.extract_months(source)
        self.assertEqual([m["settlement_month"] for m in months], ["2026-05", "2026-06"])

    def test_missing_months_key_raises(self):
        with self.assertRaises(KeyError):
            import_official_sell.extract_months({})


class BuildOfficialSellTest(unittest.TestCase):
    def test_includes_generated_at_and_months(self):
        source = {"months": [make_source_month()]}
        result = import_official_sell.build_official_sell(source)
        self.assertIn("generated_at", result)
        self.assertIn("source_note", result)
        self.assertEqual(len(result["months"]), 1)


class MainMissingSourceTest(unittest.TestCase):
    def test_main_exits_nonzero_when_source_missing(self):
        import subprocess
        import tempfile

        script = str(Path(__file__).resolve().parent / "import_official_sell.py")
        with tempfile.TemporaryDirectory() as tmp:
            out_path = Path(tmp) / "official_sell.json"
            proc = subprocess.run(
                [sys.executable, script, "--source", str(Path(tmp) / "nonexistent.json"), "--out", str(out_path)],
                capture_output=True,
                text=True,
            )
            self.assertNotEqual(proc.returncode, 0)
            self.assertFalse(out_path.exists())


if __name__ == "__main__":
    unittest.main()
