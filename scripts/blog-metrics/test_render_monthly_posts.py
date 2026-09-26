#!/usr/bin/env python3
"""render_monthly_posts.py の unittest。

実行方法:
  cd scripts/blog-metrics && python3 -m unittest test_render_monthly_posts -v
"""
from __future__ import annotations

import copy
import json
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import render_monthly_posts as rmp  # noqa: E402


def base_snapshot(**overrides) -> dict:
    snapshot = {
        "schema_version": 1,
        "billing_month": "2026-10",
        "report_month": "2026-09",
        "usage_period": {"start": "2026-09-02", "end": "2026-10-01", "days": 30},
        "first_published": "2026-10-24",
        "revision": 1,
        "revised": None,
        "estimation": "full",
        "days_usable": 30,
        "days_total": 30,
        "layers": {
            "L0": {"net_cost_fit_yen": 10000, "net_cost_post_fit_yen": 9500, "buy_kwh": 500.0, "sell_kwh": 0.0},
            "L1": {"net_cost_fit_yen": 6000, "net_cost_post_fit_yen": 5800, "buy_kwh": 300.0, "sell_kwh": 100.0},
            "L2": {"net_cost_fit_yen": 3500, "net_cost_post_fit_yen": 4000, "buy_kwh": 150.0, "sell_kwh": 200.0},
            "L3": {
                "net_cost_fit_yen": 3200, "net_cost_post_fit_yen": 3600, "buy_kwh": 160.0, "sell_kwh": 195.0,
                "buy_source": "billed", "sell_source": "official_meter",
            },
        },
        "l2_band": {"net_cost_fit_yen_min": 3000, "net_cost_fit_yen_max": 4000},
        "energy": {"solar_kwh": 400.0, "sell_kwh_sensor": 190.0, "nichicon_charge_kwh": 80.0, "ecoflow_charge_kwh": 40.0},
        "weather": {"sunny_days": 8, "cloudy_days": 10, "overcast_days": 10, "unknown_days": 2},
        "comparison": {"prev_solar_kwh": 450.0, "yoy_solar_kwh": 380.0},
        "stage": "final",
        "tariff_basis": "confirmed",
        "l3_source": {"buy": "billed", "sell": "official_meter"},
    }
    snapshot.update(overrides)
    return snapshot


class RenderMarkdownTest(unittest.TestCase):
    def test_normal_month_has_expected_front_matter_and_table_values(self):
        md = rmp.render_markdown(base_snapshot())
        problems = rmp.check_rendered(md)
        self.assertEqual(problems, [])
        self.assertIn('title: "2026年9月の発電と電気代レポート"', md)
        self.assertIn("draft: false", md)
        self.assertIn("| 発電量 | 400.0kWh |", md)
        self.assertIn("| 買電量（請求書） | 160.0kWh |", md)

    def test_l3_above_band_and_cloudy_gives_s4_and_standby_note_only(self):
        snap = base_snapshot()
        snap["layers"]["L3"]["net_cost_fit_yen"] = 5200  # > band_max(4000)
        snap["weather"] = {"sunny_days": 5, "cloudy_days": 15, "overcast_days": 8, "unknown_days": 2}
        md = rmp.render_markdown(snap)
        self.assertIn("ポータブル電源の充放電・変換ロスと待機電力が", md)
        self.assertIn("停電時の備えにもなります", md)
        self.assertNotIn("この集計だけでは原因を特定できません", md)  # S5(晴れ多め)の文言は出ない
        self.assertNotIn("余剰の多い日に、ポータブル電源へ電気を振り分けた分が効いています", md)

    def test_l3_within_band_gives_s3(self):
        snap = base_snapshot()
        snap["layers"]["L3"]["net_cost_fit_yen"] = 3500  # band内(3000〜4000)
        md = rmp.render_markdown(snap)
        self.assertIn("今月は差があるとは言えません", md)
        self.assertNotIn("停電時の備えにもなります", md)

    def test_l3_below_band_and_sunny_gives_s1(self):
        snap = base_snapshot()
        snap["layers"]["L3"]["net_cost_fit_yen"] = 2000  # < band_min(3000)
        snap["weather"] = {"sunny_days": 20, "cloudy_days": 5, "overcast_days": 3, "unknown_days": 2}
        md = rmp.render_markdown(snap)
        self.assertIn("余剰の多い日に、ポータブル電源へ電気を振り分けた分が効いています", md)
        self.assertNotIn("日照が少ない月でも", md)

    def test_revision_2_shows_update_sentence_and_lastmod(self):
        snap = base_snapshot(revision=2, revised="2026-11-05")
        md = rmp.render_markdown(snap)
        self.assertIn("lastmod: 2026-11-05T00:00:00+09:00", md)
        self.assertIn("2026-11-05に請求書と検針値の数値で確定版に更新しました（第2版）", md)

    def test_null_comparison_and_energy_omit_sentences_without_none_or_nan(self):
        snap = base_snapshot()
        snap["comparison"] = {"prev_solar_kwh": None, "yoy_solar_kwh": None}
        snap["energy"] = {"solar_kwh": None, "sell_kwh_sensor": None, "nichicon_charge_kwh": None, "ecoflow_charge_kwh": None}
        md = rmp.render_markdown(snap)
        self.assertNotIn("None", md)
        self.assertNotIn("nan", md)
        self.assertNotIn("| 発電量 |", md)
        self.assertNotIn("前月", md)
        self.assertNotIn("前年同月", md)

    def test_rendering_is_deterministic(self):
        snap = base_snapshot()
        md1 = rmp.render_markdown(copy.deepcopy(snap))
        md2 = rmp.render_markdown(copy.deepcopy(snap))
        self.assertEqual(md1, md2)

    def test_preliminary_stage_title_and_l3_label(self):
        snap = base_snapshot(stage="preliminary", tariff_basis="provisional")
        snap["layers"]["L3"]["buy_source"] = "sensor"
        md = rmp.render_markdown(snap)
        self.assertIn("（速報）", md)
        self.assertIn("＋SolarChargeController（実測・センサー値）", md)
        self.assertIn("この記事は速報です", md)


class CheckRenderedTest(unittest.TestCase):
    def test_script_tag_is_rejected(self):
        md = rmp.render_markdown(base_snapshot())
        injected = md + "\n<script>alert(1)</script>\n"
        self.assertTrue(rmp.check_rendered(injected))

    def test_disallowed_external_link_is_rejected(self):
        md = rmp.render_markdown(base_snapshot())
        injected = md.replace("[実績ダッシュボード](/metrics/)", "[実績ダッシュボード](https://evil.example.com/)")
        problems = rmp.check_rendered(injected)
        self.assertTrue(any("evil.example.com" in p for p in problems))

    def test_utility_company_name_is_rejected(self):
        md = rmp.render_markdown(base_snapshot())
        injected = md + "\n東京電力の管内です。\n"
        self.assertTrue(rmp.check_rendered(injected))


class RunTest(unittest.TestCase):
    def test_missing_posts_dir_writes_nothing_and_returns_zero(self):
        with tempfile.TemporaryDirectory() as tmp:
            posts_dir = Path(tmp) / "posts"  # 作らない
            out_dir = Path(tmp) / "out"
            written = rmp.run(posts_dir, out_dir)
            self.assertEqual(written, 0)

    def test_suppressed_billing_month_is_not_rendered(self):
        with tempfile.TemporaryDirectory() as tmp:
            posts_dir = Path(tmp) / "posts"
            posts_dir.mkdir()
            (posts_dir / "2026-10.json").write_text(json.dumps(base_snapshot(), ensure_ascii=False), encoding="utf-8")
            out_dir = Path(tmp) / "out"
            original = rmp.SUPPRESSED_BILLING_MONTHS
            rmp.SUPPRESSED_BILLING_MONTHS = frozenset({"2026-10"})
            try:
                written = rmp.run(posts_dir, out_dir)
            finally:
                rmp.SUPPRESSED_BILLING_MONTHS = original
            self.assertEqual(written, 0)
            self.assertFalse((out_dir / "monthly-report-2026-09.md").exists())

    def test_normal_post_is_rendered_to_expected_filename(self):
        with tempfile.TemporaryDirectory() as tmp:
            posts_dir = Path(tmp) / "posts"
            posts_dir.mkdir()
            (posts_dir / "2026-10.json").write_text(json.dumps(base_snapshot(), ensure_ascii=False), encoding="utf-8")
            out_dir = Path(tmp) / "out"
            written = rmp.run(posts_dir, out_dir)
            self.assertEqual(written, 1)
            self.assertTrue((out_dir / "monthly-report-2026-09.md").exists())


if __name__ == "__main__":
    unittest.main()
