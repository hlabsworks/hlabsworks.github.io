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
            "L3": {"net_cost_fit_yen": 3200, "net_cost_post_fit_yen": 3600, "buy_kwh": 160.0, "sell_kwh": 195.0},
        },
        "l2_band": {"net_cost_fit_yen_min": 3000, "net_cost_fit_yen_max": 4000},
        "energy": {"solar_kwh": 400.0, "sell_kwh_sensor": 190.0, "nichicon_charge_kwh": 80.0, "ecoflow_charge_kwh": 40.0},
        "weather": {"sunny_days": 8, "cloudy_days": 10, "overcast_days": 10, "unknown_days": 2},
        "comparison": {"prev_solar_kwh": 450.0, "yoy_solar_kwh": 380.0},
        "stage": "final",
        "tariff_basis": "confirmed",
        "l3_source": {"buy": "billed", "sell": "official_meter"},
        "transitioned_from_preliminary": False,
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
        self.assertIn("| 売電量（検針値） | 195.0kWh |", md)

    def test_l3_above_band_and_cloudy_gives_s4_and_standby_note_only(self):
        snap = base_snapshot()
        snap["layers"]["L3"]["net_cost_fit_yen"] = 5200  # > band_max(4000)
        snap["weather"] = {"sunny_days": 5, "cloudy_days": 15, "overcast_days": 8, "unknown_days": 2}
        md = rmp.render_markdown(snap)
        self.assertIn("ポータブル電源の充放電・変換ロスと待機電力が", md)
        self.assertIn("停電時の備えにもなります", md)
        self.assertNotIn("この集計だけでは原因を特定できません", md)  # S5(晴れ多め)の文言は出ない
        self.assertNotIn("余剰の多い日に、ポータブル電源へ電気を振り分けた分が効いています", md)

    def test_l3_above_band_and_sunny_gives_s5(self):
        snap = base_snapshot()
        snap["layers"]["L3"]["net_cost_fit_yen"] = 5200  # > band_max(4000)
        snap["weather"] = {"sunny_days": 20, "cloudy_days": 3, "overcast_days": 3, "unknown_days": 2}
        md = rmp.render_markdown(snap)
        self.assertIn("晴れの日が多かったにもかかわらず", md)
        self.assertIn("この集計だけでは原因を特定できません", md)
        self.assertIn("停電時の備えにもなります", md)
        self.assertNotIn("ポータブル電源の充放電・変換ロスと待機電力が", md)

    def test_l3_above_band_and_unknown_weather_gives_s6(self):
        snap = base_snapshot()
        snap["layers"]["L3"]["net_cost_fit_yen"] = 5200  # > band_max(4000)
        snap["weather"] = {"sunny_days": 1, "cloudy_days": 1, "overcast_days": 1, "unknown_days": 27}
        md = rmp.render_markdown(snap)
        self.assertIn("今月は家庭用蓄電池だけの試算より", md)
        self.assertNotIn("晴れの日が多かったにもかかわらず", md)
        self.assertNotIn("曇りや雨の日が多く余剰が少なかったため", md)
        self.assertIn("停電時の備えにもなります", md)

    def test_l3_below_band_and_unknown_weather_gives_s6(self):
        snap = base_snapshot()
        snap["layers"]["L3"]["net_cost_fit_yen"] = 2000  # < band_min(3000)
        snap["weather"] = {"sunny_days": 1, "cloudy_days": 1, "overcast_days": 1, "unknown_days": 27}
        md = rmp.render_markdown(snap)
        self.assertIn("家庭用蓄電池だけの試算より", md)
        self.assertNotIn("日照が少ない月でも", md)
        self.assertNotIn("余剰の多い日に、ポータブル電源へ電気を振り分けた分が効いています", md)
        self.assertNotIn("停電時の備えにもなります", md)

    def test_l3_below_band_and_cloudy_gives_s2(self):
        snap = base_snapshot()
        snap["layers"]["L3"]["net_cost_fit_yen"] = 2000  # < band_min(3000)
        snap["weather"] = {"sunny_days": 5, "cloudy_days": 15, "overcast_days": 8, "unknown_days": 2}
        md = rmp.render_markdown(snap)
        self.assertIn("日照が少ない月でも", md)
        self.assertIn("家庭用蓄電池だけの試算より", md)
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

    def test_revision_2_after_preliminary_transition_shows_transition_sentence(self):
        snap = base_snapshot(revision=2, revised="2026-11-05", transitioned_from_preliminary=True)
        md = rmp.render_markdown(snap)
        self.assertIn("lastmod: 2026-11-05T00:00:00+09:00", md)
        self.assertIn("2026年11月5日に請求書と検針値の数値で確定版に更新しました（第2版）", md)

    def test_revision_2_correction_without_preliminary_shows_generic_sentence(self):
        # QA指摘2026-09-26 item10: 速報を経ていない確定後の訂正は中立な文言にする。
        snap = base_snapshot(revision=2, revised="2026-11-05", transitioned_from_preliminary=False)
        md = rmp.render_markdown(snap)
        self.assertIn("2026年11月5日に数値を更新しました（第2版）", md)
        self.assertNotIn("請求書と検針値の数値で確定版に更新しました", md)

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
        self.assertNotIn("自家消費率", md)  # 行を出さないので注記も出ない(QA指摘item11)

    def test_rendering_is_deterministic(self):
        snap = base_snapshot()
        md1 = rmp.render_markdown(copy.deepcopy(snap))
        md2 = rmp.render_markdown(copy.deepcopy(snap))
        self.assertEqual(md1, md2)

    def test_preliminary_stage_title_and_l3_label(self):
        snap = base_snapshot(stage="preliminary", tariff_basis="provisional")
        snap["l3_source"] = {"buy": "sensor", "sell": "official_meter"}
        md = rmp.render_markdown(snap)
        self.assertIn("（速報）", md)
        self.assertIn("＋SolarChargeController（実測・センサー計測値と検針値）", md)
        self.assertIn("この記事は速報です", md)

    def test_l3_source_labels_come_from_l3_source_not_stage(self):
        # QA指摘2026-09-26 item2: 売電だけ検針値が先に届く月がある。
        snap = base_snapshot()
        snap["l3_source"] = {"buy": "sensor", "sell": "official_meter"}
        md = rmp.render_markdown(snap)
        self.assertIn("| 買電量（計測値） | 160.0kWh |", md)
        self.assertIn("| 売電量（検針値） | 195.0kWh |", md)
        self.assertIn("＋SolarChargeController（実測・センサー計測値と検針値）", md)

    def test_l3_source_both_sensor_uses_single_label(self):
        snap = base_snapshot()
        snap["l3_source"] = {"buy": "sensor", "sell": "sensor"}
        md = rmp.render_markdown(snap)
        self.assertIn("＋SolarChargeController（実測・センサー計測値）", md)
        self.assertNotIn("センサー計測値とセンサー計測値", md)

    def test_headline_equal_amounts_uses_neutral_wording(self):
        # QA指摘2026-09-26 item11: L3==0は「上回りました」ではなく「同額でした」。
        snap = base_snapshot()
        snap["layers"]["L0"]["net_cost_fit_yen"] = 0
        snap["layers"]["L3"]["net_cost_fit_yen"] = 0
        md = rmp.render_markdown(snap)
        self.assertIn("電気代と売電収入が同額でした", md)
        self.assertNotIn("上回りました", md)

    def test_headline_negative_l3_uses_exceeded_wording(self):
        snap = base_snapshot()
        snap["layers"]["L3"]["net_cost_fit_yen"] = -500
        md = rmp.render_markdown(snap)
        self.assertIn("売電収入が電気代を上回りました", md)

    def test_weather_sentence_only_appears_in_weather_section(self):
        # QA指摘2026-09-26 item11: 天候の文はまとめ節から削除し、天候と発電節のみにする。
        md = rmp.render_markdown(base_snapshot())
        summary_section = md.split("## まとめ")[1].split("## 電気代の4層比較")[0]
        weather_section = md.split("## 天候と発電")[1].split("## CO2排出削減量")[0]
        self.assertNotIn("曇りや雨の日が多い月でした", summary_section)
        self.assertIn("曇りや雨の日が多い月でした", weather_section)

    def test_co2_section_positive_scc_shows_kg(self):
        md = rmp.render_markdown(base_snapshot())  # L2.buy(150) > L3.buy(160)は負のケース、まず正のケースを作る
        snap = base_snapshot()
        snap["layers"]["L2"]["buy_kwh"] = 200.0
        snap["layers"]["L3"]["buy_kwh"] = 160.0
        md = rmp.render_markdown(snap)
        self.assertIn("そのうちSolarChargeControllerの効果分は約", md)

    def test_co2_section_negative_scc_shows_kwh_increase(self):
        # QA指摘2026-09-26 item11: 負のときは「わずかに増えています」ではなくkWh数値を示す。
        snap = base_snapshot()
        snap["layers"]["L2"]["buy_kwh"] = 150.0
        snap["layers"]["L3"]["buy_kwh"] = 160.0
        md = rmp.render_markdown(snap)
        self.assertIn("ポータブル電源の分だけ買電が10.0kWh増えています", md)
        self.assertNotIn("わずかに増えています", md)

    def test_co2_section_zero_scc_shows_no_sentence(self):
        snap = base_snapshot()
        snap["layers"]["L2"]["buy_kwh"] = 160.0
        snap["layers"]["L3"]["buy_kwh"] = 160.0
        md = rmp.render_markdown(snap)
        co2_section = md.split("## CO2排出削減量")[1].split("## この数字について")[0]
        self.assertNotIn("そのうちSolarChargeControllerの効果分", co2_section)
        self.assertNotIn("増えています", co2_section)

    def test_co2_section_missing_factor_shows_unavailable_sentence(self):
        snap = base_snapshot(billing_month="2026-09", report_month="2026-08")
        snap["usage_period"] = {"start": "2026-08-02", "end": "2026-09-01", "days": 31}
        md = rmp.render_markdown(snap)
        self.assertIn("この請求月に適用できるCO2排出係数が未設定のため、推定できません", md)

    def test_no_l0_l1_l2_l3_code_names_in_this_number_section(self):
        # QA指摘2026-09-26 item5: 「この数字について」節にコード名(L0〜L3)を出さない。
        md = rmp.render_markdown(base_snapshot())
        this_number_section = md.split("## この数字について")[1]
        for code in ("L0", "L1", "L2", "L3"):
            self.assertNotIn(code, this_number_section)
        self.assertNotIn("分子・分母", md)
        self.assertNotIn("5分ごと", this_number_section)

    def test_dashboard_terminology_is_used(self):
        # QA指摘2026-09-26 item11: ダッシュボードの表記(試算・売電16円・卒FIT8円で計算)に揃える。
        md = rmp.render_markdown(base_snapshot())
        self.assertIn("実質電気代（売電16円）", md)
        self.assertIn("実質電気代（卒FIT 8円で計算）", md)
        self.assertIn("太陽光・蓄電池なし（試算）", md)


class CheckRenderedTest(unittest.TestCase):
    def test_script_tag_is_rejected(self):
        md = rmp.render_markdown(base_snapshot())
        injected = md + "\n<script>alert(1)</script>\n"
        self.assertTrue(rmp.check_rendered(injected))

    def test_image_markdown_is_rejected(self):
        md = rmp.render_markdown(base_snapshot())
        injected = md + "\n![alt](https://evil.example.com/x.png)\n"
        problems = rmp.check_rendered(injected)
        self.assertTrue(any("![" in p for p in problems))

    def test_time_of_day_pattern_is_rejected(self):
        md = rmp.render_markdown(base_snapshot())
        injected = md + "\n07:30に計測しました。\n"
        problems = rmp.check_rendered(injected)
        self.assertTrue(any("時間帯粒度" in p for p in problems))

    def test_missing_front_matter_key_is_rejected(self):
        md = rmp.render_markdown(base_snapshot())
        injected = md.replace('draft: false\n', '')
        problems = rmp.check_rendered(injected)
        self.assertTrue(problems)

    def test_disallowed_external_link_is_rejected(self):
        md = rmp.render_markdown(base_snapshot())
        injected = md.replace("[実績ダッシュボード](/metrics/)", "[実績ダッシュボード](https://evil.example.com/)")
        problems = rmp.check_rendered(injected)
        self.assertTrue(any("evil.example.com" in p for p in problems))

    def test_utility_company_name_is_rejected(self):
        md = rmp.render_markdown(base_snapshot())
        injected = md + "\n東京電力の管内です。\n"
        self.assertTrue(rmp.check_rendered(injected))

    def test_valid_markdown_passes_with_no_problems(self):
        md = rmp.render_markdown(base_snapshot())
        self.assertEqual(rmp.check_rendered(md), [])


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
            self.assertFalse((out_dir / "2026-09.md").exists())

    def test_billing_month_before_first_report_month_is_not_rendered(self):
        with tempfile.TemporaryDirectory() as tmp:
            posts_dir = Path(tmp) / "posts"
            posts_dir.mkdir()
            snap = base_snapshot(billing_month="2026-09", report_month="2026-08")
            snap["usage_period"] = {"start": "2026-08-02", "end": "2026-09-01", "days": 31}
            (posts_dir / "2026-09.json").write_text(json.dumps(snap, ensure_ascii=False), encoding="utf-8")
            out_dir = Path(tmp) / "out"
            written = rmp.run(posts_dir, out_dir)
            self.assertEqual(written, 0)

    def test_normal_post_is_rendered_to_expected_filename(self):
        # QA指摘2026-09-26 item12: 出力先はcontent/posts/monthly-report/、ファイル名YYYY-MM.md
        # （URLは/posts/monthly-report/2026-09/になる）。
        with tempfile.TemporaryDirectory() as tmp:
            posts_dir = Path(tmp) / "posts"
            posts_dir.mkdir()
            (posts_dir / "2026-10.json").write_text(json.dumps(base_snapshot(), ensure_ascii=False), encoding="utf-8")
            out_dir = Path(tmp) / "out"
            written = rmp.run(posts_dir, out_dir)
            self.assertEqual(written, 1)
            self.assertTrue((out_dir / "2026-09.md").exists())

    def test_duplicate_report_month_raises(self):
        # QA指摘2026-09-26 item3: 異なるbilling_monthが同じreport_monthを主張したら止める。
        with tempfile.TemporaryDirectory() as tmp:
            posts_dir = Path(tmp) / "posts"
            posts_dir.mkdir()
            snap_a = base_snapshot()
            snap_b = base_snapshot(billing_month="2026-11")
            snap_b["usage_period"] = {"start": "2026-10-02", "end": "2026-11-01", "days": 31}
            # report_monthを意図的にsnap_aと衝突させる(本来はbill_model.billing_periodと
            # 一致しないため実際には作れないが、レンダラ側の多重防御を確認する)。
            snap_b["report_month"] = snap_a["report_month"]
            (posts_dir / "2026-10.json").write_text(json.dumps(snap_a, ensure_ascii=False), encoding="utf-8")
            (posts_dir / "2026-11.json").write_text(json.dumps(snap_b, ensure_ascii=False), encoding="utf-8")
            out_dir = Path(tmp) / "out"
            with self.assertRaises(SystemExit):
                rmp.run(posts_dir, out_dir)


if __name__ == "__main__":
    unittest.main()
