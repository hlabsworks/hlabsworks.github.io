#!/usr/bin/env python3
"""layer_model.py の unittest。

実行方法:
  cd scripts/blog-metrics && python3 -m unittest test_layer_model -v

テスト分類は docs/design/20260905_layer-model-ddr.md §5 に対応する
（A: 恒等式 / B: 蓄電池 / C: golden day / D: 請求変換 / E: 品質ゲート）。

QA #1 (BLOCKER) 対応: 時間帯粒度の実データ(*.csv)は本repoにコミットしない
（オーナー決定、DDR §6 却下案4）。golden day テストは合成データ(GoldenDaySyntheticTest)を
既定とし、実データでの検証(GoldenDayRealDataOptInTest)は
~/Develop/energy-archive/solarchgctl/profile_5min/2026-08-27_to_now.csv がローカルに
存在する場合のみ実行される opt-in テストにする。詳細は testdata/README.md 参照。
"""
import contextlib
import io
import json
import os
import re
import sys
import unittest
from datetime import date, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import bill_model  # noqa: E402
import layer_model as lm  # noqa: E402

ARCHIVE_CSV_PATH = Path.home() / "Develop" / "energy-archive" / "solarchgctl" / "profile_5min" / "2026-08-27_to_now.csv"


def make_bucket(bucket_at="2026-01-01 00:00", **overrides) -> lm.Bucket:
    base = dict(
        bucket_at=bucket_at,
        solar_w=0.0,
        buy_w=0.0,
        sell_w=0.0,
        nichicon_pv_w=0.0,
        nichicon_battery_w=0.0,
        nichicon_soc=50.0,
        eco_ac_in_w=0.0,
        eco_ac_out_w=0.0,
    )
    base.update(overrides)
    return lm.Bucket(**base)


def make_tariff(**overrides) -> dict:
    tariff = {
        "retailer": "テスト電力",
        "plan": "テストプランS",
        "area": "テスト",
        "basic_fee_yen_per_month": 0,
        "energy_tiers_yen_per_kwh": [
            {"up_to_kwh": 400, "yen_per_kwh": 27.00},
            {"up_to_kwh": None, "yen_per_kwh": 26.00},
        ],
        "renewable_levy_yen_per_kwh": {"2025-05..2027-04": 3.98},
        "capacity_contribution_yen_per_month": {"2026-09": 213},
        "fuel_cost_adjustment_yen_per_kwh": {"2026-09": -3.50},
        "sell_price_yen_per_kwh": {"fit": 16.0, "post_fit_assumed_for_readers": 8.0},
        "meter_read_day": 2,
    }
    tariff.update(overrides)
    return tariff


def _full_month_daily(billing_month: str, buy=1.0, solar=10.0, sell=5.0) -> dict:
    start, end = bill_model.billing_period(billing_month, meter_read_day=2)
    daily = {}
    d = start
    while d <= end:
        daily[d.isoformat()] = {"date": d.isoformat(), "buy_kwh": buy, "solar_kwh": solar, "sell_kwh": sell}
        d += timedelta(days=1)
    return daily


# ---------------------------------------------------------------------------
# golden day 合成フィクスチャ（QA #1 BLOCKER / #6 オーナー規則厳守対応）
# ---------------------------------------------------------------------------
# 12個のアンカー時刻の値は本テストのために考案した合成値であり、2026-09-01等の実測値は
# 一切含まない（オーナー決定「時間帯粒度の実測値は公開repoに一切置かない」を字義通り厳守）。
# 深夜の放電・朝の充電立ち上がり・正午のSOC100%到達による全量売電・夕方以降の放電という
# 定性的な形（DDR §0で観測されたレジームの一般的な形状）だけを模した、切りの良い数値。
# 分 -> フィールド値の辞書。1440(=翌日00:00)は周期境界として0分の値を再利用する。
_GOLDEN_ANCHOR_VALUES = {
    0: dict(solar_w=0.0, buy_w=0.0, sell_w=0.0, nichicon_battery_w=-500.0, nichicon_pv_w=0.0,
            nichicon_soc=50.0, eco_ac_in_w=0.0, eco_ac_out_w=300.0),
    180: dict(solar_w=0.0, buy_w=0.0, sell_w=0.0, nichicon_battery_w=-500.0, nichicon_pv_w=0.0,
              nichicon_soc=35.0, eco_ac_in_w=0.0, eco_ac_out_w=250.0),
    360: dict(solar_w=1000.0, buy_w=0.0, sell_w=300.0, nichicon_battery_w=100.0, nichicon_pv_w=100.0,
              nichicon_soc=20.0, eco_ac_in_w=200.0, eco_ac_out_w=150.0),
    445: dict(solar_w=2000.0, buy_w=0.0, sell_w=600.0, nichicon_battery_w=500.0, nichicon_pv_w=500.0,
              nichicon_soc=25.0, eco_ac_in_w=700.0, eco_ac_out_w=400.0),
    540: dict(solar_w=3500.0, buy_w=0.0, sell_w=700.0, nichicon_battery_w=1200.0, nichicon_pv_w=1200.0,
              nichicon_soc=35.0, eco_ac_in_w=1500.0, eco_ac_out_w=200.0),
    600: dict(solar_w=3500.0, buy_w=0.0, sell_w=600.0, nichicon_battery_w=2000.0, nichicon_pv_w=2000.0,
              nichicon_soc=55.0, eco_ac_in_w=1500.0, eco_ac_out_w=150.0),
    660: dict(solar_w=2000.0, buy_w=0.0, sell_w=400.0, nichicon_battery_w=1800.0, nichicon_pv_w=1800.0,
              nichicon_soc=75.0, eco_ac_in_w=700.0, eco_ac_out_w=150.0),
    730: dict(solar_w=3500.0, buy_w=0.0, sell_w=6000.0, nichicon_battery_w=0.0, nichicon_pv_w=3500.0,
              nichicon_soc=100.0, eco_ac_in_w=200.0, eco_ac_out_w=150.0),
    740: dict(solar_w=2500.0, buy_w=0.0, sell_w=4000.0, nichicon_battery_w=0.0, nichicon_pv_w=2500.0,
              nichicon_soc=100.0, eco_ac_in_w=150.0, eco_ac_out_w=120.0),
    900: dict(solar_w=700.0, buy_w=10.0, sell_w=150.0, nichicon_battery_w=0.0, nichicon_pv_w=500.0,
              nichicon_soc=100.0, eco_ac_in_w=200.0, eco_ac_out_w=200.0),
    1080: dict(solar_w=0.0, buy_w=5.0, sell_w=0.0, nichicon_battery_w=-800.0, nichicon_pv_w=0.0,
               nichicon_soc=80.0, eco_ac_in_w=0.0, eco_ac_out_w=200.0),
    1260: dict(solar_w=0.0, buy_w=0.0, sell_w=0.0, nichicon_battery_w=-1000.0, nichicon_pv_w=0.0,
               nichicon_soc=55.0, eco_ac_in_w=0.0, eco_ac_out_w=350.0),
}
_GOLDEN_ANCHOR_VALUES[1440] = _GOLDEN_ANCHOR_VALUES[0]
_GOLDEN_FIELDS = ("solar_w", "buy_w", "sell_w", "nichicon_battery_w", "nichicon_pv_w",
                   "nichicon_soc", "eco_ac_in_w", "eco_ac_out_w")
GOLDEN_SNAPSHOTS_HHMM = {
    "00:00": 0, "03:00": 180, "06:00": 360, "07:25": 445, "09:00": 540, "10:00": 600,
    "11:00": 660, "12:10": 730, "12:20": 740, "15:00": 900, "18:00": 1080, "21:00": 1260,
}


def expected_load_at_anchor(minute: int) -> float:
    """アンカー時刻の期待復元負荷を恒等式（DDR §1）から独立に算出する（QA #6:
    「恒等式は線形なので、合成アンカーでも期待値をコード内で式から導けば同じ拘束力を持つ」。
    lm.Bucket.load_true_w() を呼ばず、素の四則演算で再実装することで検出力を保つ）。"""
    v = _GOLDEN_ANCHOR_VALUES[minute]
    return v["solar_w"] + v["nichicon_pv_w"] - v["nichicon_battery_w"] + v["buy_w"] - v["sell_w"] \
        + v["eco_ac_out_w"] - v["eco_ac_in_w"]


def build_synthetic_golden_day_buckets(day: str = "2026-09-01") -> list:
    """QA #1 (BLOCKER) / #6: 実データを一切使わず、12個の合成アンカー点（上記）を線形補間して
    滑らかな288バケット/日のプロファイルを作る。アンカー時刻ちょうどの値も合成値であり、
    実測値は含まない。"""
    anchors = sorted(_GOLDEN_ANCHOR_VALUES)
    buckets = []
    for slot in range(lm.DAY_BUCKETS):
        t = slot * lm.BUCKET_MINUTES
        lo = max(a for a in anchors if a <= t)
        hi = min(a for a in anchors if a >= t)
        frac = 0.0 if lo == hi else (t - lo) / (hi - lo)
        lo_vals = _GOLDEN_ANCHOR_VALUES[lo]
        hi_vals = _GOLDEN_ANCHOR_VALUES[hi]
        values = {f: lo_vals[f] + (hi_vals[f] - lo_vals[f]) * frac for f in _GOLDEN_FIELDS}
        hh, mm = divmod(t, 60)
        buckets.append(lm.Bucket(bucket_at=f"{day} {hh:02d}:{mm:02d}", **values))
    return buckets


# ---------------------------------------------------------------------------
# A. 恒等式（DDR §1、§5-A）
# ---------------------------------------------------------------------------
class LoadIdentityTest(unittest.TestCase):
    def test_night_discharge_only(self):
        # 夜間: 蓄電池放電のみ・太陽光ゼロの合成ケース（数値はテスト用の合成値、実測値ではない）。
        # 蓄電池500W放電・DELTA出力300W → 復元負荷 = 0+0-(-500)+0-0+300-0 = 800W
        b = make_bucket(solar_w=0.0, buy_w=0.0, sell_w=0.0, nichicon_pv_w=0.0, nichicon_battery_w=-500.0,
                         eco_ac_in_w=0.0, eco_ac_out_w=300.0)
        self.assertAlmostEqual(b.load_true_w(), 800.0, places=6)

    def test_sell_greater_than_solar_uses_west_roof_pv(self):
        # 西屋根PVが主パワコンと別系統であることを示す合成ケース（sell > solar でも恒等式が
        # 破綻しないことを検証。数値は合成値、実測値ではない）。
        # solar=2500, npv=2500, sell=4000, eco_in=150, eco_out=120
        # → 復元負荷 = 2500+2500-0+0-4000+120-150 = 970W
        b = make_bucket(solar_w=2500.0, buy_w=0.0, sell_w=4000.0, nichicon_pv_w=2500.0, nichicon_battery_w=0.0,
                         eco_ac_in_w=150.0, eco_ac_out_w=120.0)
        self.assertAlmostEqual(b.load_true_w(), 970.0, places=6)

    def test_round_trip_composition_charge_and_delta(self):
        # 主PV1000W、西屋根PV500Wのうち300Wを充電、DELTA充電200W、系統買電50W、売電0W
        # → 復元負荷 = 1000 + 500 - 300 + 50 - 0 + 0 - 200 = 1050W
        b = make_bucket(solar_w=1000.0, buy_w=50.0, sell_w=0.0, nichicon_pv_w=500.0, nichicon_battery_w=300.0,
                         eco_ac_in_w=200.0, eco_ac_out_w=0.0)
        self.assertAlmostEqual(b.load_true_w(), 1050.0, places=6)

    def test_pass_through_no_battery_no_delta(self):
        # 蓄電池・DELTAが無い場合は単純に solar + buy - sell に一致する。
        b = make_bucket(solar_w=800.0, buy_w=120.0, sell_w=0.0)
        self.assertAlmostEqual(b.load_true_w(), 920.0, places=6)

    def test_missing_required_field_propagates_none(self):
        # DDR §3「欠測バケットはNone、捏造しない」。eco_ac_in_w欠測ならload_true_wはNone。
        b = make_bucket(eco_ac_in_w=None)
        self.assertIsNone(b.load_true_w())

    def test_nichicon_soc_missing_does_not_block_load_calc(self):
        # nichicon_soc は恒等式に現れないため、欠測でも load_true_w は計算できる。
        b = make_bucket(solar_w=100.0, nichicon_soc=None)
        self.assertIsNotNone(b.load_true_w())


class ColumnAliasTest(unittest.TestCase):
    def test_archived_csv_column_names_are_accepted(self):
        # energy-archive の退避済みCSV列名（bucket, n_p, n_n, n_e）をエイリアス経由で読める。
        # 数値は合成値（実測値ではない）: 蓄電池放電400W・DELTA出力250W・太陽光ゼロ
        # → 復元負荷 = 0+0-(-400)+0-0+250-0 = 650W
        rows = [{
            "bucket": "2026-09-01 00:00", "solar_w": "0.0", "buy_w": "0.0", "sell_w": "0.0",
            "nichicon_battery_w": "-400.0", "nichicon_pv_w": "0.0", "nichicon_soc": "50.0",
            "eco_ac_in_w": "0.0", "eco_ac_out_w": "250.0", "eco_usb_out_w": "0.0",
            "n_p": "5", "n_n": "1", "n_e": "20",
        }]
        buckets = lm.parse_profile_rows(rows)
        self.assertEqual(len(buckets), 1)
        self.assertEqual(buckets[0].bucket_at, "2026-09-01 00:00")
        self.assertAlmostEqual(buckets[0].load_true_w(), 650.0, places=6)

    def test_missing_bucket_column_raises(self):
        with self.assertRaises(KeyError):
            lm.parse_profile_rows([{"solar_w": "0.0"}])

    def test_malformed_numeric_cell_raises_with_column_context(self):
        # 追加テストa: 数値化できないセルは、どのバケット・どの列が壊れているか
        # 追跡できる例外メッセージ（bucket_atと列名を含む）を出す。
        rows = [{
            "bucket": "2026-09-01 00:05", "solar_w": "N/A", "buy_w": "0.0", "sell_w": "0.0",
            "nichicon_battery_w": "0.0", "nichicon_pv_w": "0.0", "nichicon_soc": "50.0",
            "eco_ac_in_w": "0.0", "eco_ac_out_w": "0.0",
        }]
        with self.assertRaises(ValueError) as ctx:
            lm.parse_profile_rows(rows)
        message = str(ctx.exception)
        self.assertIn("solar_w", message)
        self.assertIn("2026-09-01 00:05", message)

    def test_eco_usb_out_w_is_not_used_for_load(self):
        # usb_output_w は負荷復元に含めない（オーナー決定）。CSVにあっても無視される。
        rows = [{
            "bucket": "2026-09-01 00:00", "solar_w": "0.0", "buy_w": "0.0", "sell_w": "0.0",
            "nichicon_battery_w": "0.0", "nichicon_pv_w": "0.0", "nichicon_soc": "50.0",
            "eco_ac_in_w": "0.0", "eco_ac_out_w": "0.0", "eco_usb_out_w": "999.0",
        }]
        buckets = lm.parse_profile_rows(rows)
        self.assertEqual(buckets[0].load_true_w(), 0.0)


# ---------------------------------------------------------------------------
# B. 蓄電池（DDR §2.4、§5-B）
# ---------------------------------------------------------------------------
class BatterySimulationTest(unittest.TestCase):
    def _dt_h(self):
        return lm.BUCKET_MINUTES / 60.0

    def test_soc_never_exceeds_100_percent(self):
        # 充電が続いてもSOCは100%でクリップされる。
        buckets = [
            (self._dt_h(), {
                "solar_w": 0.0, "nichicon_pv_w": 3000.0, "nichicon_battery_w": 3000.0,
                "buy_w": 0.0, "sell_w": 0.0, "eco_ac_out_w": 0.0, "eco_ac_in_w": 0.0,
            })
            for _ in range(20)
        ]
        _, soc_end = lm.simulate_l2(buckets, pv_ac_efficiency=1.0, soc_start_pct=90.0)
        self.assertLessEqual(soc_end, 100.0)
        self.assertAlmostEqual(soc_end, 100.0, places=1)

    def test_full_soc_produces_zero_effective_charge_headroom(self):
        # SOCが既に100%なら、それ以上充電エネルギーを積み増さない（100%クリップの帰結）。
        row = {
            "solar_w": 0.0, "nichicon_pv_w": 1000.0, "nichicon_battery_w": 1000.0,
            "buy_w": 0.0, "sell_w": 0.0, "eco_ac_out_w": 0.0, "eco_ac_in_w": 0.0,
        }
        _, soc_end = lm.simulate_l2([(self._dt_h(), row)], pv_ac_efficiency=1.0, soc_start_pct=100.0)
        self.assertEqual(soc_end, 100.0)

    def test_full_soc_surplus_flows_to_sell_not_vanished(self):
        # QA #6・missing-test#4: SOC99%で受け入れきれなかった充電分はエネルギーとして消えず
        # 通常のPV出力として売電に回る。soc=99%, npv=nb=5000W, load=0, dt=1h → sell≈4.896kWh
        # (headroom=0.1kWh=104Wh、accepted=104Wh、残り4896Whがpv_house→sell)。
        row = {
            "solar_w": 0.0, "nichicon_pv_w": 5000.0, "nichicon_battery_w": 5000.0,
            "buy_w": 0.0, "sell_w": 0.0, "eco_ac_out_w": 0.0, "eco_ac_in_w": 0.0,
        }
        totals, soc_end = lm.simulate_l2([(1.0, row)], pv_ac_efficiency=1.0, soc_start_pct=99.0)
        self.assertAlmostEqual(totals.sell_kwh, 4.896, places=3)
        self.assertAlmostEqual(soc_end, 100.0, places=6)
        self.assertEqual(totals.buy_kwh, 0.0)

    def test_charge_never_exceeds_west_roof_pv(self):
        # QA #7・missing-test#2: 蓄電池はAC結合の余剰(nb)がnpvを超えても、npv分しか吸えない。
        # nb=5000W, npv=2000W → charge_taken は 2000W に制限される（headroom十分と仮定）。
        row = {
            "solar_w": 0.0, "nichicon_pv_w": 5000.0, "nichicon_battery_w": 2000.0,
            "buy_w": 0.0, "sell_w": 0.0, "eco_ac_out_w": 0.0, "eco_ac_in_w": 0.0,
        }
        step = lm._simulate_l2_step(row, dt_h=1.0, soc_pct=0.0, pv_ac_efficiency=1.0)
        self.assertAlmostEqual(step.charge_taken_w, 2000.0, places=6)

        row2 = dict(row)
        row2["nichicon_pv_w"] = 2000.0
        row2["nichicon_battery_w"] = 5000.0  # nbがnpvを超える（センサー誤差等を想定）
        step2 = lm._simulate_l2_step(row2, dt_h=1.0, soc_pct=0.0, pv_ac_efficiency=1.0)
        self.assertAlmostEqual(step2.charge_taken_w, 2000.0, places=6)
        self.assertLessEqual(step2.charge_taken_w, row2["nichicon_pv_w"])

    def test_discharge_capped_by_rated_power(self):
        # 残存負荷が定格出力(5.9kW)を超える場合、放電はP_dis_maxで頭打ちになり不足分は買電。
        row = {
            "solar_w": 0.0, "nichicon_pv_w": 0.0, "nichicon_battery_w": 0.0,
            "buy_w": 0.0, "sell_w": 0.0, "eco_ac_out_w": 8000.0, "eco_ac_in_w": 0.0,
        }
        totals, soc_end = lm.simulate_l2([(self._dt_h(), row)], pv_ac_efficiency=1.0, soc_start_pct=100.0)
        dt_h = self._dt_h()
        expected_e_out_wh = lm.BATTERY_MAX_DISCHARGE_KW * 1000.0 * dt_h
        expected_buy_kwh = (8000.0 * dt_h - expected_e_out_wh) / 1000.0
        self.assertAlmostEqual(totals.buy_kwh, expected_buy_kwh, places=6)
        expected_soc_drop = expected_e_out_wh / (lm.BATTERY_DISCHARGE_KWH_PER_100SOC * 1000.0) * 100.0
        self.assertAlmostEqual(soc_end, 100.0 - expected_soc_drop, places=3)

    def test_discharge_capped_by_soc_when_battery_nearly_empty(self):
        # QA missing-test#5: SOC制限は出力上限(5.9kW)と独立に効く。
        # soc=1%, eco_out=2000W, dt=1h → 放電可能エネルギー = 1%*8.92kWh = 89.2Wh = 0.0892kWh
        # （出力上限5.9kWよりずっと小さいのでSOC側が支配する）。
        row = {
            "solar_w": 0.0, "nichicon_pv_w": 0.0, "nichicon_battery_w": 0.0,
            "buy_w": 0.0, "sell_w": 0.0, "eco_ac_out_w": 2000.0, "eco_ac_in_w": 0.0,
        }
        totals, soc_end = lm.simulate_l2([(1.0, row)], pv_ac_efficiency=1.0, soc_start_pct=1.0)
        self.assertEqual(soc_end, 0.0)
        self.assertAlmostEqual(totals.buy_kwh, (2000.0 - 89.2) / 1000.0, places=4)
        # 放電量そのもの(discharge)を _simulate_l2_step で直接確認。
        step = lm._simulate_l2_step(row, dt_h=1.0, soc_pct=1.0, pv_ac_efficiency=1.0)
        self.assertAlmostEqual(step.discharge_w / 1000.0, 0.0892, places=4)

    def test_battery_does_not_charge_from_ac_surplus(self):
        # 系統への逆潮流(AC余剰)があっても、蓄電池充電は測定値nichicon_battery_wそのまま。
        # AC余剰から追加で充電量を作らない（DDR §0 #3, #5-B）。
        row = {
            "solar_w": 5000.0, "nichicon_pv_w": 0.0, "nichicon_battery_w": 0.0,
            "buy_w": 0.0, "sell_w": 4500.0, "eco_ac_out_w": 0.0, "eco_ac_in_w": 0.0,
        }
        totals, soc_end = lm.simulate_l2([(self._dt_h(), row)], pv_ac_efficiency=1.0, soc_start_pct=50.0)
        self.assertEqual(soc_end, 50.0)  # 充電も放電も発生しない
        self.assertGreater(totals.sell_kwh, 0.0)

    def test_period_boundary_uses_actual_soc_not_carried_state(self):
        # 期間開始時のSOCは実測値で初期化する（前期間の推定終了SOCを引き継がない）。
        row = {
            "solar_w": 0.0, "nichicon_pv_w": 0.0, "nichicon_battery_w": 0.0,
            "buy_w": 0.0, "sell_w": 0.0, "eco_ac_out_w": 0.0, "eco_ac_in_w": 0.0,
        }
        _, soc_end_a = lm.simulate_l2([(self._dt_h(), row)], pv_ac_efficiency=1.0, soc_start_pct=10.0)
        _, soc_end_b = lm.simulate_l2([(self._dt_h(), row)], pv_ac_efficiency=1.0, soc_start_pct=90.0)
        self.assertEqual(soc_end_a, 10.0)
        self.assertEqual(soc_end_b, 90.0)
        self.assertNotEqual(soc_end_a, soc_end_b)

    def test_round_trip_efficiency_uses_asymmetric_factors(self):
        # 10.4kWh充電分は100%SOC相当、放電時は8.92kWhで100%SOC相当（往復85.8%）。
        charge_row = {
            "solar_w": 0.0, "nichicon_pv_w": 10400.0, "nichicon_battery_w": 10400.0,
            "buy_w": 0.0, "sell_w": 0.0, "eco_ac_out_w": 0.0, "eco_ac_in_w": 0.0,
        }
        _, soc_after_charge = lm.simulate_l2([(1.0, charge_row)], pv_ac_efficiency=1.0, soc_start_pct=0.0)
        self.assertAlmostEqual(soc_after_charge, 100.0, places=6)

        # 放電1000W×8.92hで8920Wh（定格5.9kW未満に抑え、P_dis_maxで頭打ちにならないようにする）。
        discharge_row = {
            "solar_w": 0.0, "nichicon_pv_w": 0.0, "nichicon_battery_w": 0.0,
            "buy_w": 0.0, "sell_w": 0.0, "eco_ac_out_w": 1000.0, "eco_ac_in_w": 0.0,
        }
        totals, soc_after_discharge = lm.simulate_l2([(8.92, discharge_row)], pv_ac_efficiency=1.0, soc_start_pct=100.0)
        self.assertAlmostEqual(soc_after_discharge, 0.0, places=3)
        self.assertAlmostEqual(totals.buy_kwh, 0.0, places=3)

    def test_bucket_resampling_changes_l1_totals_due_to_jensen_bias(self):
        # max(0,・)は凸関数のため、バケットを粗くすると buy/sell が両方かさ上げされうる
        # （DDR §0既知のノイズ）。急峻な負荷/PVの入れ替わり（雲の切れ目相当）を3周期分
        # 用意し、5分バケットの合計と15分再集計の合計が一致しないことを確認する
        # （golden dayの滑らかな補間データでは差が小さすぎて検出できないため専用データを使う）。
        buckets = []
        for cycle in range(3):
            base_min = cycle * 15
            # load優勢(買電)の5分 → PV優勢(売電)の5分 → 中間の5分、を3回繰り返す
            buckets.append(make_bucket(f"2026-09-01 c{cycle}-0", solar_w=0.0, buy_w=6000.0))
            buckets.append(make_bucket(f"2026-09-01 c{cycle}-1", solar_w=6000.0, sell_w=6000.0))
            buckets.append(make_bucket(f"2026-09-01 c{cycle}-2", solar_w=0.0, buy_w=0.0))
        resampled_5 = lm.resample_buckets(buckets, 5)
        resampled_15 = lm.resample_buckets(buckets, 15)
        self.assertEqual(len(resampled_5), 9)
        self.assertEqual(len(resampled_15), 3)
        l1_5 = lm.simulate_l1(resampled_5, pv_ac_efficiency=1.0)
        l1_15 = lm.simulate_l1(resampled_15, pv_ac_efficiency=1.0)
        self.assertNotAlmostEqual(l1_5.buy_kwh, l1_15.buy_kwh, places=2)
        # 粗いバケットほど買電・売電が減る方向（山谷が均されるため）。
        self.assertLess(l1_15.buy_kwh, l1_5.buy_kwh)


class EnergyConservationTest(unittest.TestCase):
    """QA missing-test#3: golden day（合成）でのエネルギー保存則の検証。
    pv_ac_efficiency=1.0 のとき、各バケットで
      pv_total_w − charge_taken_w + buy_w + discharge_w − load_w − sell_w == 0
    が厳密に成り立つはず（変換損失を計上していないため）。"""

    def test_conservation_holds_for_every_bucket_of_synthetic_golden_day(self):
        buckets = build_synthetic_golden_day_buckets()
        resampled = lm.resample_buckets(buckets, 5)
        soc_pct = buckets[0].nichicon_soc
        for dt_h, row in resampled:
            step = lm._simulate_l2_step(row, dt_h, soc_pct, pv_ac_efficiency=1.0)
            balance = step.pv_total_w - step.charge_taken_w + step.buy_w + step.discharge_w - step.load_w - step.sell_w
            self.assertAlmostEqual(balance, 0.0, places=6)
            soc_pct = step.soc_pct

    def test_conservation_holds_with_full_charge_clip_scenario(self):
        # SOCがすでに高く、充電分の多くが受け入れられないケースでも保存則は成り立つ。
        row = {"solar_w": 100.0, "nichicon_pv_w": 5000.0, "nichicon_battery_w": 5000.0,
               "buy_w": 0.0, "sell_w": 0.0, "eco_ac_out_w": 0.0, "eco_ac_in_w": 0.0}
        step = lm._simulate_l2_step(row, dt_h=1.0, soc_pct=99.5, pv_ac_efficiency=1.0)
        balance = step.pv_total_w - step.charge_taken_w + step.buy_w + step.discharge_w - step.load_w - step.sell_w
        self.assertAlmostEqual(balance, 0.0, places=6)


class SimulateL2SeriesTest(unittest.TestCase):
    """オーナー承認機能（2026-09-06、日次4層系列）: L2 SOCの日跨ぎ引き継ぎ・ギャップ再アンカー。"""

    def _bucket(self, bucket_at, **overrides):
        return make_bucket(bucket_at, **overrides)

    def test_soc_carries_over_contiguous_day_boundary(self):
        # day1 23:55 と day2 00:00 は5分連続 → ギャップなしでsocが引き継がれる
        # （day2側の実測soc=50.0には再アンカーされず、day1から引き継いだ推定値(≈1.0%、
        # SOC制約が支配的になる低残量)が使われる。既存の discharge_capped_by_soc の
        # シナリオと同じ考え方で、SOC次第で放電可能量＝買電量が変わることを利用する）。
        day1_last = self._bucket("2026-09-01 23:55", nichicon_soc=1.0)  # 負荷ゼロ→socは変化しない
        day2_first = self._bucket("2026-09-02 00:00", eco_ac_out_w=8000.0, nichicon_soc=50.0)
        result = lm.simulate_l2_series([day1_last, day2_first], pv_ac_efficiency=1.0, initial_soc_pct=1.0)
        self.assertIn("2026-09-01", result)
        self.assertIn("2026-09-02", result)

        dt_h = 5 / 60
        zero_row = {"solar_w": 0.0, "nichicon_pv_w": 0.0, "nichicon_battery_w": 0.0, "buy_w": 0.0, "sell_w": 0.0,
                    "eco_ac_out_w": 0.0, "eco_ac_in_w": 0.0}
        draw_row = {"solar_w": 0.0, "nichicon_pv_w": 0.0, "nichicon_battery_w": 0.0, "buy_w": 0.0, "sell_w": 0.0,
                    "eco_ac_out_w": 8000.0, "eco_ac_in_w": 0.0}
        step1 = lm._simulate_l2_step(zero_row, dt_h, 1.0, 1.0)
        step2_carried = lm._simulate_l2_step(draw_row, dt_h, step1.soc_pct, 1.0)
        step2_reanchored = lm._simulate_l2_step(draw_row, dt_h, 50.0, 1.0)
        day2_buy_kwh = result["2026-09-02"][0]
        self.assertAlmostEqual(day2_buy_kwh, step2_carried.buy_w * dt_h / 1000.0, places=6)
        self.assertNotAlmostEqual(day2_buy_kwh, step2_reanchored.buy_w * dt_h / 1000.0, places=2)

    def test_gap_reinitializes_to_actual_soc(self):
        # day1とday3の間（day2）が丸ごと欠測しているケース。day3の最初のバケットでは
        # 実測nichicon_soc(50.0)に再アンカーし、day1から引き継いだ推定値(carriedのまま
        # なら1.0%)は使わない（SOC次第で放電可能量＝買電量が変わる負荷を使って区別する）。
        day1 = self._bucket("2026-09-01 23:55", nichicon_soc=1.0)  # 負荷ゼロ→socは変化しない
        day3 = self._bucket("2026-09-03 00:00", eco_ac_out_w=8000.0, nichicon_soc=50.0)
        result_gap = lm.simulate_l2_series([day1, day3], pv_ac_efficiency=1.0, initial_soc_pct=1.0)

        dt_h = 5 / 60
        draw_row = {"solar_w": 0.0, "nichicon_pv_w": 0.0, "nichicon_battery_w": 0.0, "buy_w": 0.0, "sell_w": 0.0,
                    "eco_ac_out_w": 8000.0, "eco_ac_in_w": 0.0}
        expected_reanchored = lm._simulate_l2_step(draw_row, dt_h, 50.0, 1.0)
        expected_if_carried = lm._simulate_l2_step(draw_row, dt_h, 1.0, 1.0)  # 起きてはいけない挙動
        day3_buy_kwh = result_gap["2026-09-03"][0]
        self.assertAlmostEqual(day3_buy_kwh, expected_reanchored.buy_w * dt_h / 1000.0, places=6)
        self.assertNotAlmostEqual(day3_buy_kwh, expected_if_carried.buy_w * dt_h / 1000.0, places=2)

    def test_daily_totals_sum_correctly_across_two_days(self):
        buckets = [
            self._bucket("2026-09-01 00:00", eco_ac_out_w=1000.0, nichicon_soc=50.0),
            self._bucket("2026-09-01 00:05", eco_ac_out_w=1000.0),
            self._bucket("2026-09-02 00:00", eco_ac_out_w=1000.0),
        ]
        result = lm.simulate_l2_series(buckets, pv_ac_efficiency=1.0, initial_soc_pct=50.0)
        self.assertEqual(set(result.keys()), {"2026-09-01", "2026-09-02"})
        # soc50%なら1000Wの放電要求は蓄電池だけで賄えるため買電は発生しない
        # （日付ごとの内訳がきちんと分かれて記録されていることの確認）。
        for buy_kwh, sell_kwh in result.values():
            self.assertGreaterEqual(buy_kwh, 0.0)
            self.assertEqual(sell_kwh, 0.0)


# ---------------------------------------------------------------------------
# C. golden day（DDR §5-C）
# ---------------------------------------------------------------------------
class GoldenDaySyntheticTest(unittest.TestCase):
    """合成フィクスチャによる構造検証（QA #1 BLOCKER 対応で実データCSVを置換）。
    具体的な日積算値は合成データに依存するため固定せず、恒等式・順序関係・保存則等の
    構造的性質のみを検証する。"""

    @classmethod
    def setUpClass(cls):
        cls.buckets = build_synthetic_golden_day_buckets()
        cls.resampled = lm.resample_buckets(cls.buckets, 5)

    def test_full_day_has_288_buckets(self):
        self.assertEqual(len(self.buckets), 288)

    def test_twelve_snapshots_match_formula_derived_expected_load(self):
        # QA #6: アンカー時刻の期待値は実測値ではなく、恒等式(DDR §1)から独立に算出する
        # （expected_load_at_anchor は Bucket.load_true_w() を呼ばず素の四則演算で再実装）。
        by_time = {b.bucket_at[-5:]: b for b in self.buckets}
        for hhmm, minute in GOLDEN_SNAPSHOTS_HHMM.items():
            with self.subTest(time=hhmm):
                self.assertAlmostEqual(
                    by_time[hhmm].load_true_w(), expected_load_at_anchor(minute), places=6
                )

    def test_l0_ge_l1_ge_l2_buy_order(self):
        l0 = lm.simulate_l0(self.resampled)
        l1 = lm.simulate_l1(self.resampled, pv_ac_efficiency=1.0)
        soc_start = self.buckets[0].nichicon_soc
        l2, _ = lm.simulate_l2(self.resampled, pv_ac_efficiency=1.0, soc_start_pct=soc_start)
        self.assertGreaterEqual(l0.buy_kwh, l1.buy_kwh)
        self.assertGreaterEqual(l1.buy_kwh, l2.buy_kwh)

    def test_l0_buy_matches_restored_load(self):
        l0 = lm.simulate_l0(self.resampled)
        load_wh = sum(b.load_true_w() * (lm.BUCKET_MINUTES / 60.0) for b in self.buckets)
        self.assertAlmostEqual(l0.buy_kwh, load_wh / 1000.0, places=6)
        self.assertEqual(l0.sell_kwh, 0.0)

    def test_max_export_within_pcs_capacity(self):
        l1 = lm.simulate_l1(self.resampled, pv_ac_efficiency=1.0)
        self.assertLess(l1.max_export_w, lm.MAX_EXPORT_WARN_W)

    def test_day_is_usable(self):
        usable, reason = lm.day_is_usable(self.buckets, date(2026, 9, 1))
        self.assertTrue(usable)
        self.assertIsNone(reason)


@unittest.skipUnless(
    ARCHIVE_CSV_PATH.exists(),
    f"実データ opt-in テスト: {ARCHIVE_CSV_PATH} が無いためスキップ（private repo energy-archive 未取得）",
)
class GoldenDayRealDataOptInTest(unittest.TestCase):
    """実データ(2026-09-01)での golden day 検証（QA #1・#5対応でopt-in化）。

    QA #5 根本原因メモ: DDR §5-C は日積算load 26.18±0.5kWh を挙げるが、本テストが使う
    退避済みCSV（および architect が同一クエリ・丸め無しで抽出した series_5min CSV）は
    どちらも load_true≈26.76kWhを与える。チャネル別に daily_energy_summary（Java側の
    実dt重み付き積分、信頼できる基準）と突き合わせた結果、solar_w/sell_w（power_history由来）
    でのみ有意な差（約+4.1%、solar: 28.85 vs 27.72kWh、sell: 19.35 vs 18.58kWh）があり、
    nichicon_pv_w/nichicon_battery_w（nichicon_realtime_history由来）はほぼ一致した
    （npv: 18.61 vs 18.62kWh、charge: 7.99 vs 8.00kWh）。したがって丸め誤差ではなく、
    5分プロファイルを作った集計方法（power_history由来チャンネルの単純平均とみられる）が
    aggregate.sh/daily_energy_summary の dt重み付き台形積分と異なることに起因する
    上流データ品質の問題（REASONED、SQLソース非公開のため未VERIFIED）。本モデルの恒等式・
    シミュレーションロジック自体は精度違いの2種のCSV（丸めあり/丸めなし）で同一の結果
    （26.7596 vs 26.7594kWh）を返すため正しいと確認済み。詳細は
    docs/design/20260905_layer-model-ddr.md §5-C 追記を参照。
    """

    @classmethod
    def setUpClass(cls):
        import csv

        with ARCHIVE_CSV_PATH.open(encoding="utf-8") as f:
            rows = [r for r in csv.DictReader(f) if r["bucket"].startswith("2026-09-01")]
        cls.buckets = lm.parse_profile_rows(rows)
        cls.resampled = lm.resample_buckets(cls.buckets, 5)

    def test_full_day_has_288_buckets(self):
        self.assertEqual(len(self.buckets), 288)

    def test_daily_load_total_kwh_matches_known_biased_archive_value(self):
        # 既知の上流バイアス込みの値（VERIFIED、上記docstring参照）。DDRの26.18は
        # dt重み付き積分（Java daily_energy_summary）と整合する値であり、本CSVの単純平均
        # 集計とは異なる（根本原因はテストクラスdocstring参照）。
        load_wh = sum(b.load_true_w() * (lm.BUCKET_MINUTES / 60.0) for b in self.buckets)
        self.assertAlmostEqual(load_wh / 1000.0, 26.76, delta=0.05)

    def test_daily_nichicon_charge_kwh(self):
        # nichicon側は上流バイアスの影響を受けない（docstring参照）。DDR §5-C: 7.99±0.05kWh。
        charge_wh = sum(max(0.0, b.nichicon_battery_w) * (lm.BUCKET_MINUTES / 60.0) for b in self.buckets)
        self.assertAlmostEqual(charge_wh / 1000.0, 7.99, delta=0.05)

    def test_every_bucket_matches_independent_formula_reimplementation(self):
        # QA #6: 実測の具体的な瞬時値(W)を本ファイルに書かない。real dataでの検証は
        # Bucket.load_true_w() を呼ばず恒等式(DDR §1)を素の四則演算で独立に再実装し、
        # 実CSV全288バケットで一致することを確認する（実測magnitudeをハードコードしない）。
        for b in self.buckets:
            independent = (
                b.solar_w + b.nichicon_pv_w - b.nichicon_battery_w
                + b.buy_w - b.sell_w + b.eco_ac_out_w - b.eco_ac_in_w
            )
            with self.subTest(bucket_at=b.bucket_at):
                self.assertAlmostEqual(b.load_true_w(), independent, places=6)

    def test_l0_ge_l1_ge_l2_buy_order(self):
        l0 = lm.simulate_l0(self.resampled)
        l1 = lm.simulate_l1(self.resampled, pv_ac_efficiency=1.0)
        soc_start = self.buckets[0].nichicon_soc
        l2, _ = lm.simulate_l2(self.resampled, pv_ac_efficiency=1.0, soc_start_pct=soc_start)
        self.assertGreaterEqual(l0.buy_kwh, l1.buy_kwh)
        self.assertGreaterEqual(l1.buy_kwh, l2.buy_kwh)


# ---------------------------------------------------------------------------
# D. 請求変換（DDR §5-D）
# ---------------------------------------------------------------------------
class LayerDictTest(unittest.TestCase):
    def test_unavailable_layer_has_no_money_keys(self):
        d = lm.layer_dict("estimated", False, None, None, make_tariff(), "2026-09", 16.0, 8.0, unavailable_reason="test")
        self.assertFalse(d["available"])
        self.assertEqual(d["unavailable_reason"], "test")
        self.assertNotIn("buy_kwh", d)
        self.assertNotIn("bill", d)

    def test_unavailable_layer_via_unavailable_layer_helper_has_no_money_keys(self):
        # QA #7: 全構築箇所（L0〜L3）が unavailable_layer()/available_layer() を経由する
        # ことを直接確認する。
        d = lm.unavailable_layer("measured", "test reason")
        self.assertFalse(d["available"])
        self.assertNotIn("buy_kwh", d)
        self.assertNotIn("sell_kwh", d)
        self.assertNotIn("bill", d)
        self.assertNotIn("net_cost_fit_yen", d)

    def test_available_layer_uses_compute_bill(self):
        tariff = make_tariff()
        d = lm.layer_dict("estimated", True, 100.0, 20.0, tariff, "2026-09", 16.0, 8.0)
        expected_bill = bill_model.compute_bill(tariff, 100.0, "2026-09")
        self.assertEqual(d["bill"], expected_bill.to_dict())
        self.assertEqual(d["net_cost_fit_yen"], expected_bill.total_yen - lm._round_yen(20.0 * 16.0))
        self.assertEqual(d["net_cost_post_fit_yen"], expected_bill.total_yen - lm._round_yen(20.0 * 8.0))

    def test_compute_bill_monkeypatch_changes_all_layers(self):
        # bill_model.compute_bill をmonkeypatchすると layer_dict の全出力が追随する
        # （料金計算を二重実装していないことの確認）。
        tariff = make_tariff()
        original = bill_model.compute_bill
        try:
            bill_model.compute_bill = lambda t, kwh, m: original(t, kwh, m)  # sanity: same behavior
            d1 = lm.layer_dict("estimated", True, 100.0, 20.0, tariff, "2026-09", 16.0, 8.0)

            def fake_compute_bill(t, kwh, m):
                b = original(t, kwh, m)
                b.total_yen += 10000
                return b

            bill_model.compute_bill = fake_compute_bill
            d2 = lm.layer_dict("estimated", True, 100.0, 20.0, tariff, "2026-09", 16.0, 8.0)
            self.assertEqual(d2["bill"]["total_yen"] - d1["bill"]["total_yen"], 10000)
        finally:
            bill_model.compute_bill = original

    def test_400kwh_tier_boundary_reflected_in_layer(self):
        tariff = make_tariff()
        d = lm.layer_dict("estimated", True, 450.0, 0.0, tariff, "2026-09", 16.0, 8.0)
        expected = bill_model.tiered_energy_charge(tariff["energy_tiers_yen_per_kwh"], 450.0)
        self.assertEqual(d["bill"]["energy_charge_yen"], bill_model._floor_yen(expected))


class BuildMonthLayersTest(unittest.TestCase):
    def test_l3_available_without_profile_data(self):
        tariff = make_tariff()
        daily = _full_month_daily("2026-09")
        record = lm.build_month_layers(tariff, "2026-09", daily, {}, {}, profile_by_date={})
        self.assertIsNotNone(record)
        self.assertTrue(record["layers"]["L3"]["available"])
        self.assertFalse(record["layers"]["L0"]["available"])
        self.assertFalse(record["layers"]["L1"]["available"])
        self.assertFalse(record["layers"]["L2"]["available"])
        # QA #2: 部分月の一律コード生成理由ではなく、欠測日/期間の具体的な文言が出ること。
        # 読者向けQAレビュー対応: reason_code/reason_label/reason_detailの3点セットを持つ。
        l0_reason = record["layers"]["L0"]["unavailable_reason"]
        self.assertEqual(l0_reason["reason_code"], "profile_missing")
        self.assertEqual(l0_reason["reason_label"], "5分プロファイル未取得")
        self.assertIn("5分プロファイル欠測", l0_reason["reason_detail"])

    def test_l3_unavailable_via_unexpected_keyerror_gets_tariff_missing_reason(self):
        # bill_model.build_month_record が明示チェックしていない設定不備（renewable_levy の
        # 対象期間が無い等）はKeyErrorとして伝播する。build_month_layers はこれを
        # tariff_missing の reason_code/reason_label/reason_detail に変換して捕捉する
        # （読者向けQAレビュー対応: 内部の例外メッセージをそのまま出さない）。
        tariff = make_tariff(renewable_levy_yen_per_kwh={"2099-01..2099-02": 3.98})
        daily = _full_month_daily("2026-09")
        record = lm.build_month_layers(tariff, "2026-09", daily, {}, {}, profile_by_date={})
        l3 = record["layers"]["L3"]
        self.assertFalse(l3["available"])
        l3_reason = l3["unavailable_reason"]
        self.assertEqual(l3_reason["reason_code"], "tariff_missing")
        self.assertEqual(l3_reason["reason_label"], "料金表の設定が不足しています")
        self.assertIn("renewable_levy_yen_per_kwh", l3_reason["reason_detail"])
        self.assertNotIn("renewable_levy_yen_per_kwh", l3_reason["reason_label"])

    def test_l3_only_month_still_exposes_disclosure_keys(self):
        # 追加テストc: L3のみavailableな月でも、ダッシュボードの開示表
        # (renderLayerDisclosureTable) が必要とするキー(buy_source/sell_source/coverage)は
        # recordに揃っている（QA再レビュー #1: 開示表がL3のみの月でも読者に届く前提の検証）。
        tariff = make_tariff()
        daily = _full_month_daily("2026-09")
        record = lm.build_month_layers(tariff, "2026-09", daily, {}, {}, profile_by_date={})
        self.assertTrue(record["layers"]["L3"]["available"])
        self.assertIn("buy_source", record["layers"]["L3"])
        self.assertIn("sell_source", record["layers"]["L3"])
        self.assertIn("coverage", record)
        self.assertEqual(record["coverage"], 0.0)

    def test_buy_source_billed_when_official_buy_matches_period(self):
        tariff = make_tariff()
        daily = _full_month_daily("2026-09")
        start, end = bill_model.billing_period("2026-09", meter_read_day=2)
        official_buy_by_month = {
            "2026-09": {
                "settlement_month": "2026-09", "period_from": start.isoformat(), "period_to": end.isoformat(),
                "official_buy_kwh": 999.0, "billed_yen": 12345,
            }
        }
        record = lm.build_month_layers(tariff, "2026-09", daily, {}, official_buy_by_month, profile_by_date={})
        self.assertEqual(record["layers"]["L3"]["buy_source"], "billed")
        self.assertEqual(record["layers"]["L3"]["buy_kwh"], 999.0)

    def test_l3_sell_prefers_official_value(self):
        # QA missing-test#10: L3の売電は公式値(official_sell.json)を優先する。
        tariff = make_tariff()
        daily = _full_month_daily("2026-09", sell=5.0)
        start, end = bill_model.billing_period("2026-09", meter_read_day=2)
        official_sell_by_month = {
            "2026-09": {
                "settlement_month": "2026-09", "period_from": start.isoformat(), "period_to": end.isoformat(),
                "official_sell_kwh": 555.5, "sell_revenue_yen": 8888,
            }
        }
        record = lm.build_month_layers(tariff, "2026-09", daily, official_sell_by_month, {}, profile_by_date={})
        self.assertEqual(record["layers"]["L3"]["sell_source"], "tepco_official")
        self.assertEqual(record["layers"]["L3"]["sell_kwh"], 555.5)

    def test_all_layers_unavailable_still_returns_record_with_reasons(self):
        # QA #4: build_month_layers は None を返さず、層ごとの理由を持つ record を返す。
        tariff = make_tariff()
        record = lm.build_month_layers(tariff, "2026-09", {}, {}, {}, profile_by_date={})
        self.assertIsNotNone(record)
        for key in ("L0", "L1", "L2", "L3"):
            self.assertFalse(record["layers"][key]["available"])
            self.assertIsNotNone(record["layers"][key]["unavailable_reason"])

    def test_l3_unavailable_layer_has_no_money_keys(self):
        # QA #7: L3側もunavailable_layer()経由で構築され金額キーを持たない。
        tariff = make_tariff()
        record = lm.build_month_layers(tariff, "2026-09", {}, {}, {}, profile_by_date={})
        l3 = record["layers"]["L3"]
        self.assertNotIn("buy_kwh", l3)
        self.assertNotIn("bill", l3)

    def test_soc_start_uses_first_measured_bucket_of_period(self):
        # QA missing-test#6: build_month_layersはL2初期SOCを期間内最初の実測nichicon_socで
        # 初期化する。
        tariff = make_tariff()
        daily = _full_month_daily("2026-09")
        start, end = bill_model.billing_period("2026-09", meter_read_day=2)
        profile_by_date = {}
        d = start
        while d <= end:
            day_buckets = build_synthetic_golden_day_buckets(day=d.isoformat())
            if d == start:
                for b in day_buckets:
                    b.nichicon_soc = 42.0 if b.bucket_at.endswith("00:00") else b.nichicon_soc
            profile_by_date[d.isoformat()] = day_buckets
            d += timedelta(days=1)
        record = lm.build_month_layers(tariff, "2026-09", daily, {}, {}, profile_by_date)
        self.assertTrue(record["layers"]["L2"]["available"])
        self.assertEqual(record["layers"]["L2"]["soc_start_pct"], 42.0)

    def test_month_record_reports_interpolated_slots(self):
        # QA再レビュー(2回目) #1: build_month_layers が interpolated_slots を出力していない
        # ため、JS側(metrics-dashboard.js)が読む m.interpolated_slots が確定月では恒久的に
        # 0になっていた。daily/in_progressと同形でmonthレコードにも追加する。
        tariff = make_tariff()
        daily = _full_month_daily("2026-09")
        start, end = bill_model.billing_period("2026-09", meter_read_day=2)
        profile_by_date = {}
        d = start
        while d <= end:
            day_buckets = build_synthetic_golden_day_buckets(day=d.isoformat())
            if d.isoformat() == "2026-08-15":
                day_buckets.pop(100)  # 1バケット行が丸ごと欠落 → 1スロット、チャネル値8個分
            profile_by_date[d.isoformat()] = day_buckets
            d += timedelta(days=1)
        record = lm.build_month_layers(tariff, "2026-09", daily, {}, {}, profile_by_date)
        self.assertTrue(record["layers"]["L0"]["available"])
        self.assertEqual(record["interpolated_slots"], 1)
        self.assertEqual(record["interpolated_buckets"], len(lm.REQUIRED_LOAD_FIELDS) + 1)


class BuildLayersTest(unittest.TestCase):
    def test_empty_inputs_return_empty_result(self):
        tariff = make_tariff()
        result = lm.build_layers(tariff, {}, {}, {}, {})
        self.assertEqual(result["months"], [])
        self.assertEqual(result["excluded_months"], [])
        self.assertIn("params", result)
        self.assertFalse(result["cumulative"]["available"])

    def test_excluded_month_has_per_layer_reasons(self):
        # QA #4: excluded_months[].layer_reasons に L0〜L3 それぞれの理由が入る。
        # daily_by_date は候補月を発生させるために1日分だけ与え（他は欠測のままにして
        # L3もexcludedにする）、profile_by_date は空のままにしてL0〜L2もunavailableにする。
        tariff = make_tariff()
        daily = {"2026-08-15": {"date": "2026-08-15", "buy_kwh": 1.0, "solar_kwh": 10.0, "sell_kwh": 5.0}}
        result = lm.build_layers(tariff, daily, {}, {}, {})
        excluded_2026_09 = next((m for m in result["excluded_months"] if m["billing_month"] == "2026-09"), None)
        self.assertIsNotNone(excluded_2026_09)
        self.assertIn("layer_reasons", excluded_2026_09)
        for key in ("L0", "L1", "L2", "L3"):
            self.assertIn(key, excluded_2026_09["layer_reasons"])
            self.assertIsNotNone(excluded_2026_09["layer_reasons"][key])


class BuildCumulativeTest(unittest.TestCase):
    """QA missing-test#8: 累計は「全4層available」の月のみを対象にする。"""

    def _month(self, billing_month: str, available: dict) -> dict:
        layers = {}
        for key in ("L0", "L1", "L2", "L3"):
            if available.get(key, True):
                layers[key] = {"available": True, "net_cost_fit_yen": {"L0": 10000, "L1": 7000, "L2": 4000, "L3": 3000}[key]}
            else:
                layers[key] = {"available": False, "unavailable_reason": "test"}
        return {"billing_month": billing_month, "layers": layers}

    def test_partial_month_excluded_from_cumulative(self):
        months = [self._month("2026-08", available={"L0": True, "L1": True, "L2": False, "L3": True})]
        cumulative = lm._build_cumulative(months)
        self.assertFalse(cumulative["available"])
        self.assertEqual(cumulative["months_included"], 0)

    def test_full_month_included_in_cumulative_with_all_layer_totals(self):
        months = [self._month("2026-08", available={})]
        cumulative = lm._build_cumulative(months)
        self.assertTrue(cumulative["available"])
        self.assertEqual(cumulative["months_included"], 1)
        self.assertEqual(cumulative["net_cost_fit_yen"], {"L0": 10000, "L1": 7000, "L2": 4000, "L3": 3000})
        self.assertEqual(cumulative["saving_yen_fit"], 10000 - 3000)

    def test_mixed_months_only_full_ones_counted(self):
        months = [
            self._month("2026-08", available={}),
            self._month("2026-09", available={"L1": False}),
        ]
        cumulative = lm._build_cumulative(months)
        self.assertEqual(cumulative["months_included"], 1)
        self.assertEqual(cumulative["billing_months"], ["2026-08"])


class BuildDailyLayersTest(unittest.TestCase):
    """オーナー承認機能（2026-09-06、日次4層系列）: 請求期間が全日揃うまで待たず、
    usableな日ごとにL0〜L3を出す。"""

    def test_per_kwh_only_matches_manual_formula(self):
        tariff = make_tariff()  # 2026-09 が確定
        buckets = build_synthetic_golden_day_buckets(day="2026-09-01")  # 2026-09に属する日
        profile_by_date = {"2026-09-01": buckets}
        days = lm.build_daily_layers(tariff, {}, profile_by_date)
        self.assertEqual(len(days), 1)
        entry = days[0]
        self.assertEqual(entry["date"], "2026-09-01")
        self.assertEqual(entry["billing_month"], "2026-09")
        self.assertEqual(entry["tariff_basis"], "per_kwh_only")
        self.assertFalse(entry["tariff_provisional"])
        self.assertIsNone(entry["tariff_source_month"])
        self.assertEqual(entry["interpolated_buckets"], 0)

        buy_price, sell_fit, sell_post_fit, _, _ = bill_model.per_kwh_prices(tariff, "2026-09")
        resampled = lm.resample_buckets(buckets, lm.BUCKET_MINUTES)
        l0_totals = lm.simulate_l0(resampled)
        expected_net_fit = lm._round_yen(l0_totals.buy_kwh * buy_price - l0_totals.sell_kwh * sell_fit)
        expected_net_post_fit = lm._round_yen(l0_totals.buy_kwh * buy_price - l0_totals.sell_kwh * sell_post_fit)
        l0 = entry["layers"]["L0"]
        self.assertTrue(l0["available"])
        self.assertEqual(l0["net_cost_fit_yen"], expected_net_fit)
        self.assertEqual(l0["net_cost_post_fit_yen"], expected_net_post_fit)
        self.assertAlmostEqual(l0["buy_kwh"], round(l0_totals.buy_kwh, 3), places=3)
        # per_kwh_only には容量拠出金・段階制の内訳(bill)が無い。
        self.assertNotIn("bill", l0)

    def test_provisional_tariff_flagged_for_unconfirmed_month(self):
        tariff = make_tariff()  # 2026-09のみ確定、2026-10は未確定
        buckets = build_synthetic_golden_day_buckets(day="2026-09-15")  # billing_month=2026-10
        profile_by_date = {"2026-09-15": buckets}
        days = lm.build_daily_layers(tariff, {}, profile_by_date)
        self.assertEqual(len(days), 1)
        entry = days[0]
        self.assertEqual(entry["billing_month"], "2026-10")
        self.assertTrue(entry["tariff_provisional"])
        self.assertEqual(entry["tariff_source_month"], "2026-09")

    def test_l3_independently_unavailable_when_daily_json_missing_that_date(self):
        tariff = make_tariff()
        buckets = build_synthetic_golden_day_buckets(day="2026-09-01")
        profile_by_date = {"2026-09-01": buckets}
        days = lm.build_daily_layers(tariff, {}, profile_by_date)  # daily_by_date は空
        entry = days[0]
        self.assertTrue(entry["layers"]["L0"]["available"])
        self.assertTrue(entry["layers"]["L1"]["available"])
        self.assertTrue(entry["layers"]["L2"]["available"])
        self.assertFalse(entry["layers"]["L3"]["available"])
        self.assertNotIn("buy_kwh", entry["layers"]["L3"])

    def test_l3_available_and_sourced_from_sensor_when_daily_json_present(self):
        tariff = make_tariff()
        buckets = build_synthetic_golden_day_buckets(day="2026-09-01")
        profile_by_date = {"2026-09-01": buckets}
        daily_by_date = {"2026-09-01": {"date": "2026-09-01", "buy_kwh": 5.0, "sell_kwh": 2.0, "solar_kwh": 10.0}}
        days = lm.build_daily_layers(tariff, daily_by_date, profile_by_date)
        l3 = days[0]["layers"]["L3"]
        self.assertTrue(l3["available"])
        self.assertEqual(l3["source"], "sensor")
        self.assertEqual(l3["buy_kwh"], 5.0)
        self.assertEqual(l3["sell_kwh"], 2.0)

    def test_no_time_of_day_granularity_in_output(self):
        tariff = make_tariff()
        buckets = build_synthetic_golden_day_buckets(day="2026-09-01")
        profile_by_date = {"2026-09-01": buckets}
        days = lm.build_daily_layers(tariff, {}, profile_by_date)
        dumped = json.dumps(days, ensure_ascii=False)
        self.assertNotIn("bucket_at", dumped)
        # 日付(YYYY-MM-DD)のみで時刻(HH:MM)が一切含まれないことを確認する。
        self.assertIsNone(re.search(r"\d{2}:\d{2}", dumped))

    def test_empty_profile_returns_empty_list(self):
        tariff = make_tariff()
        self.assertEqual(lm.build_daily_layers(tariff, {}, {}), [])

    def test_day_with_small_gap_is_included_via_interpolation(self):
        # オーナー承認機能（2026-09-06）: 1バケット欠落の日も補間されてdaily[]に含まれる。
        tariff = make_tariff()
        buckets = build_synthetic_golden_day_buckets(day="2026-09-01")
        buckets.pop(200)
        days = lm.build_daily_layers(tariff, {}, {"2026-09-01": buckets})
        self.assertEqual(len(days), 1)
        entry = days[0]
        self.assertTrue(entry["layers"]["L0"]["available"])
        # QA再レビュー #4/#6: nichicon_socも補間対象のため REQUIRED_LOAD_FIELDS(7)+soc(1)=8。
        # interpolated_slots は欠けているスロットが1個だけなので1。
        self.assertEqual(entry["interpolated_buckets"], len(lm.REQUIRED_LOAD_FIELDS) + 1)
        self.assertEqual(entry["interpolated_slots"], 1)

    def test_day_skipped_when_no_confirmed_tariff_source(self):
        # QA再レビュー #13: per_kwh_prices の KeyError（暫定単価の出典すら無い）で日を
        # 黙って捨てず、bill_model.reason(...) を伴う警告をstderrに出してからスキップする。
        tariff = make_tariff(fuel_cost_adjustment_yen_per_kwh={}, capacity_contribution_yen_per_month={})
        buckets = build_synthetic_golden_day_buckets(day="2026-09-01")
        stderr = io.StringIO()
        with contextlib.redirect_stderr(stderr):
            days = lm.build_daily_layers(tariff, {}, {"2026-09-01": buckets})
        self.assertEqual(days, [])  # 単価が全く確定していないため日次系列は空になる
        output = stderr.getvalue()
        self.assertIn("2026-09-01", output)
        self.assertIn("料金表の設定が不足しています", output)


class BuildInProgressTest(unittest.TestCase):
    """オーナー承認機能（2026-09-06、月途中集計）。"""

    def test_returns_none_when_no_data_at_all(self):
        tariff = make_tariff()
        result = lm.build_in_progress(tariff, {}, {}, today=date(2026, 9, 20))
        self.assertIsNone(result)

    def test_cuts_at_last_usable_day_and_counts_covered_days(self):
        tariff = make_tariff()
        # today=2026-09-20 の請求期間は 2026-09-02〜2026-10-01（billing_month=2026-10）。
        # usableなプロファイル日を09-02,09-03の2日だけ用意し、09-04以降は無い状態にする。
        profile_by_date = {
            "2026-09-02": build_synthetic_golden_day_buckets(day="2026-09-02"),
            "2026-09-03": build_synthetic_golden_day_buckets(day="2026-09-03"),
        }
        daily_by_date = {
            "2026-09-02": {"date": "2026-09-02", "buy_kwh": 1.0, "sell_kwh": 0.5},
            "2026-09-03": {"date": "2026-09-03", "buy_kwh": 1.0, "sell_kwh": 0.5},
        }
        result = lm.build_in_progress(tariff, daily_by_date, profile_by_date, today=date(2026, 9, 20))
        self.assertIsNotNone(result)
        self.assertEqual(result["status"], "in_progress")
        self.assertEqual(result["billing_month"], "2026-10")
        self.assertEqual(result["usage_period"], {"start": "2026-09-02", "end": "2026-10-01", "days": 30})
        self.assertEqual(result["days_covered"], 2)
        self.assertEqual(result["period_end_actual"], "2026-09-03")
        for key in ("L0", "L1", "L2", "L3"):
            self.assertTrue(result["layers"][key]["available"])
        self.assertEqual(result["interpolated_buckets"], 0)
        # compute_bill経由なので段階制・容量拠出金込みの内訳(bill)を持つ（per_kwh_onlyでない）。
        self.assertIn("bill", result["layers"]["L0"])

    def test_in_progress_excludes_mid_window_profile_gap(self):
        # QA再レビュー(2回目) #2: effective_endの決定は「期間内で最後にusableなprofile日」
        # (max)だと、窓の途中(start+1日)にprofile欠測日が挟まっていても飛び越えてしまい、
        # L3(daily.json全日合算可能)とL0〜L2(usable日のみ)の日集合がずれる。startから連続
        # してusableな最終日で打ち切ることを確認する。
        tariff = make_tariff()
        profile_by_date = {
            "2026-09-02": build_synthetic_golden_day_buckets(day="2026-09-02"),
            # 09-03はprofile自体が無い(窓の途中の欠測)
            "2026-09-04": build_synthetic_golden_day_buckets(day="2026-09-04"),
        }
        daily_by_date = {
            "2026-09-02": {"date": "2026-09-02", "buy_kwh": 1.0, "sell_kwh": 0.5},
            "2026-09-03": {"date": "2026-09-03", "buy_kwh": 2.0, "sell_kwh": 0.5},
            "2026-09-04": {"date": "2026-09-04", "buy_kwh": 3.0, "sell_kwh": 0.5},
        }
        result = lm.build_in_progress(tariff, daily_by_date, profile_by_date, today=date(2026, 9, 20))
        self.assertIsNotNone(result)
        self.assertEqual(result["period_end_actual"], "2026-09-02")
        self.assertEqual(result["days_covered"], 1)
        self.assertEqual(result["layers"]["L3"]["buy_kwh"], 1.0)

    def test_uses_provisional_tariff_when_billing_month_unconfirmed(self):
        tariff = make_tariff()  # 2026-09のみ確定
        profile_by_date = {"2026-09-02": build_synthetic_golden_day_buckets(day="2026-09-02")}
        daily_by_date = {"2026-09-02": {"date": "2026-09-02", "buy_kwh": 1.0, "sell_kwh": 0.5}}
        result = lm.build_in_progress(tariff, daily_by_date, profile_by_date, today=date(2026, 9, 20))
        self.assertTrue(result["tariff_provisional"])
        self.assertEqual(result["tariff_source_month"], "2026-09")
        # 実際にeffective_tariffで計算されたbillになっていることを確認する。
        effective_tariff, provisional, source_month = bill_model.resolve_effective_tariff(tariff, "2026-10")
        expected_bill = bill_model.compute_bill(effective_tariff, result["layers"]["L3"]["buy_kwh"], "2026-10")
        self.assertEqual(result["layers"]["L3"]["bill"]["total_yen"], expected_bill.total_yen)

    def test_confirmed_month_ignores_available_provisional_source(self):
        # QA再レビュー #3: 旧テストは燃料費調整・容量拠出金を全月分消していたため、
        # build_month_layers が誤って resolve_effective_tariff 経由の暫定単価を適用する
        # ように変異(mutation)しても、フォールバック元の確定月自体が存在せず結局
        # KeyErrorになるので「たまたま」パスしてしまう無効なテストだった。
        # 2026-08は確定のまま残し2026-09だけ未確定にすることで、直近確定月(2026-08)への
        # 暫定フォールバックが実在する状態を作り、build_month_layers（確定月表示）が
        # それに絶対に頼ってはいけない、という不変条件を検出できるようにする。
        tariff = make_tariff(
            fuel_cost_adjustment_yen_per_kwh={"2026-08": -3.50},
            capacity_contribution_yen_per_month={"2026-08": 213},
        )
        daily = _full_month_daily("2026-09")
        record = lm.build_month_layers(tariff, "2026-09", daily, {}, {}, profile_by_date={})
        for key in ("L0", "L1", "L2", "L3"):
            self.assertFalse(record["layers"][key]["available"], f"{key} should be unavailable")
        l3 = record["layers"]["L3"]
        self.assertEqual(l3["unavailable_reason"]["reason_code"], "tariff_missing")

    def test_in_progress_excludes_partial_today(self):
        # QA再レビュー #2: today(当日)はaggregate.shが部分行を出力するため、profileと
        # daily.json双方にtoday分のデータが揃っていても常に除外する。
        today = date(2026, 9, 20)
        tariff = make_tariff()
        profile_by_date = {
            "2026-09-02": build_synthetic_golden_day_buckets(day="2026-09-02"),
            today.isoformat(): build_synthetic_golden_day_buckets(day=today.isoformat()),
        }
        daily_by_date = {
            "2026-09-02": {"date": "2026-09-02", "buy_kwh": 1.0, "sell_kwh": 0.5},
            today.isoformat(): {"date": today.isoformat(), "buy_kwh": 1.0, "sell_kwh": 0.5},
        }
        result = lm.build_in_progress(tariff, daily_by_date, profile_by_date, today=today)
        self.assertIsNotNone(result)
        self.assertEqual(result["days_covered"], 1)
        self.assertEqual(result["period_end_actual"], "2026-09-02")
        self.assertEqual(result["layers"]["L3"]["buy_kwh"], 1.0)

    def test_in_progress_layers_cover_identical_day_span(self):
        # QA再レビュー #1: L0〜L2はusableなprofile日のみ、L3はdaily.jsonの全日を独立に
        # 合算していたため窓がずれていた（実データでL0=3日、L3=4日）。修正後は同一の
        # effective_endに基づく同一の日集合をL0〜L2/L3双方が使うことを確認する。
        tariff = make_tariff()
        profile_by_date = {
            "2026-09-02": build_synthetic_golden_day_buckets(day="2026-09-02"),
            "2026-09-03": build_synthetic_golden_day_buckets(day="2026-09-03"),
            # 09-04以降はprofile欠測（daily.jsonだけ先行して存在する状態を意図的に作る）
        }
        daily_by_date = {
            "2026-09-02": {"date": "2026-09-02", "buy_kwh": 1.0, "sell_kwh": 0.5},
            "2026-09-03": {"date": "2026-09-03", "buy_kwh": 1.0, "sell_kwh": 0.5},
            "2026-09-04": {"date": "2026-09-04", "buy_kwh": 1.0, "sell_kwh": 0.5},
            "2026-09-05": {"date": "2026-09-05", "buy_kwh": 1.0, "sell_kwh": 0.5},
        }
        result = lm.build_in_progress(tariff, daily_by_date, profile_by_date, today=date(2026, 9, 20))
        self.assertIsNotNone(result)
        self.assertEqual(result["period_end_actual"], "2026-09-03")
        self.assertEqual(result["days_covered"], 2)
        # 修正前ならL3は09-02〜09-05の4日分(buy_kwh=4.0)を合算してしまっていたが、
        # 修正後はL0〜L2と同じ09-02〜09-03の2日分(buy_kwh=2.0)だけを合算する。
        self.assertEqual(result["layers"]["L3"]["buy_kwh"], 2.0)
        self.assertTrue(result["layers"]["L0"]["available"])

    def test_status_field_is_in_progress_not_confirmed(self):
        tariff = make_tariff()
        profile_by_date = {"2026-09-02": build_synthetic_golden_day_buckets(day="2026-09-02")}
        daily_by_date = {"2026-09-02": {"date": "2026-09-02", "buy_kwh": 1.0, "sell_kwh": 0.5}}
        result = lm.build_in_progress(tariff, daily_by_date, profile_by_date, today=date(2026, 9, 20))
        self.assertEqual(result["status"], "in_progress")
        self.assertNotIn("excluded", result)


# ---------------------------------------------------------------------------
# E. 品質ゲート（DDR §5-E）
# ---------------------------------------------------------------------------
class ResolveDayBucketsTest(unittest.TestCase):
    """オーナー承認機能（2026-09-06、QA再レビュー同日）: 1日3バケットまでの欠落は線形補間して
    usable にする（DDR §0既知のノイズ対策。nichicon_realtime_historyは300秒瞬時値でジッタが
    常態）。resolve_day_buckets() は DayResolution(buckets, reason_detail,
    interpolated_values, interpolated_slots) を返す。"""

    def _golden_day(self):
        return build_synthetic_golden_day_buckets(day="2026-09-01")

    def test_single_middle_gap_is_interpolated_and_usable(self):
        buckets = self._golden_day()
        removed = buckets.pop(150)  # 中間の1バケットを丸ごと欠落させる
        r = lm.resolve_day_buckets(buckets, date(2026, 9, 1))
        self.assertIsNotNone(r.buckets)
        self.assertIsNone(r.reason_detail)
        self.assertEqual(len(r.buckets), lm.DAY_BUCKETS)
        self.assertGreater(r.interpolated_values, 0)
        self.assertGreater(r.interpolated_slots, 0)
        usable, usable_reason = lm.day_is_usable(buckets, date(2026, 9, 1))
        self.assertTrue(usable)
        self.assertIsNone(usable_reason)
        # 補間されたバケットも他のバケットと同様に恒等式を計算できる(Noneが残っていない)。
        interpolated_bucket = next(b for b in r.buckets if b.bucket_at == removed.bucket_at)
        self.assertIsNotNone(interpolated_bucket.load_true_w())

    def test_three_gaps_in_one_channel_is_still_usable(self):
        # QA再レビュー #9: 境界値テスト。INTERPOLATION_MAX_GAP(3)ちょうどの欠落は補間される。
        # INTERPOLATION_MAX_GAPを2に変更すると本テストは失敗する（境界を検出するための設計）。
        buckets = self._golden_day()
        for i in (100, 101, 102):
            buckets[i] = make_bucket(buckets[i].bucket_at, eco_ac_in_w=None)
        r = lm.resolve_day_buckets(buckets, date(2026, 9, 1))
        self.assertIsNotNone(r.buckets)
        self.assertEqual(r.interpolated_values, 3)
        self.assertEqual(r.interpolated_slots, 3)
        self.assertLessEqual(3, lm.INTERPOLATION_MAX_GAP)  # 境界値であることの前提を明記

    def test_four_gaps_in_one_channel_remains_unusable(self):
        buckets = self._golden_day()
        for i in (100, 101, 102, 103):
            buckets[i] = make_bucket(buckets[i].bucket_at, eco_ac_in_w=None)
        r = lm.resolve_day_buckets(buckets, date(2026, 9, 1))
        self.assertIsNone(r.buckets)
        self.assertIn("バケット欠落", r.reason_detail)
        self.assertEqual(r.interpolated_values, 0)
        self.assertEqual(r.interpolated_slots, 0)

    def test_leading_gap_uses_nearest_neighbor(self):
        buckets = self._golden_day()
        buckets[0] = make_bucket(buckets[0].bucket_at, nichicon_battery_w=None)
        r = lm.resolve_day_buckets(buckets, date(2026, 9, 1))
        self.assertIsNotNone(r.buckets)
        self.assertEqual(r.interpolated_values, 1)
        self.assertEqual(r.interpolated_slots, 1)
        first = sorted(r.buckets, key=lambda b: b.bucket_at)[0]
        second = sorted(r.buckets, key=lambda b: b.bucket_at)[1]
        # 先頭の欠落は最近傍(2番目のバケット)の値で埋める。
        self.assertEqual(first.nichicon_battery_w, second.nichicon_battery_w)

    def test_trailing_gap_uses_nearest_neighbor(self):
        buckets = self._golden_day()
        buckets[-1] = make_bucket(buckets[-1].bucket_at, sell_w=None)
        r = lm.resolve_day_buckets(buckets, date(2026, 9, 1))
        self.assertIsNotNone(r.buckets)
        ordered = sorted(r.buckets, key=lambda b: b.bucket_at)
        self.assertEqual(ordered[-1].sell_w, ordered[-2].sell_w)

    def test_energy_conservation_holds_after_interpolation(self):
        # 補間後もL2のエネルギー保存則(pv_total - charge_taken + buy + discharge - load - sell == 0)
        # が各バケットで成り立つことを確認する。
        buckets = self._golden_day()
        buckets.pop(200)
        r = lm.resolve_day_buckets(buckets, date(2026, 9, 1))
        resampled = lm.resample_buckets(sorted(r.buckets, key=lambda b: b.bucket_at), lm.BUCKET_MINUTES)
        soc_pct = r.buckets[0].nichicon_soc if r.buckets[0].nichicon_soc is not None else 50.0
        for dt_h, row in resampled:
            step = lm._simulate_l2_step(row, dt_h, soc_pct, pv_ac_efficiency=1.0)
            balance = step.pv_total_w - step.charge_taken_w + step.buy_w + step.discharge_w - step.load_w - step.sell_w
            self.assertAlmostEqual(balance, 0.0, places=6)
            soc_pct = step.soc_pct

    def test_interpolated_buckets_count_matches_missing_channels(self):
        buckets = self._golden_day()
        buckets.pop(50)  # 1バケット行が丸ごと欠落 → REQUIRED_LOAD_FIELDS(7)+nichicon_soc(1)=8個
        r = lm.resolve_day_buckets(buckets, date(2026, 9, 1))
        self.assertEqual(r.interpolated_values, len(lm.REQUIRED_LOAD_FIELDS) + 1)
        self.assertEqual(r.interpolated_slots, 1)  # 欠けているのは1スロットだけ

    def test_zero_gap_day_returns_identical_result_to_before(self):
        # 回帰: 欠落0の日は元のバケット列がそのまま返り、補間件数は0。
        buckets = self._golden_day()
        r = lm.resolve_day_buckets(buckets, date(2026, 9, 1))
        self.assertIsNone(r.reason_detail)
        self.assertEqual(r.interpolated_values, 0)
        self.assertEqual(r.interpolated_slots, 0)
        self.assertEqual(len(r.buckets), len(buckets))
        self.assertEqual(
            [b.load_true_w() for b in sorted(r.buckets, key=lambda b: b.bucket_at)],
            [b.load_true_w() for b in sorted(buckets, key=lambda b: b.bucket_at)],
        )
        usable, usable_reason = lm.day_is_usable(buckets, date(2026, 9, 1))
        self.assertTrue(usable)
        self.assertIsNone(usable_reason)

    def test_soc_gap_beyond_limit_makes_day_unusable(self):
        # QA再レビュー #4: nichicon_socの欠落もmax_gapに含める。socだけ4個欠落していても不採用。
        buckets = self._golden_day()
        for i in (10, 11, 12, 13):
            buckets[i] = make_bucket(buckets[i].bucket_at, nichicon_soc=None)
        r = lm.resolve_day_buckets(buckets, date(2026, 9, 1))
        self.assertIsNone(r.buckets)
        self.assertIn("バケット欠落", r.reason_detail)

    def test_soc_interpolation_is_counted_in_interpolated_buckets(self):
        # QA再レビュー #4: nichicon_soc単独の欠落(1個、許容内)も補間数に算入される。
        buckets = self._golden_day()
        buckets[10] = make_bucket(buckets[10].bucket_at, nichicon_soc=None)
        r = lm.resolve_day_buckets(buckets, date(2026, 9, 1))
        self.assertIsNotNone(r.buckets)
        self.assertEqual(r.interpolated_values, 1)
        self.assertEqual(r.interpolated_slots, 1)
        resolved_bucket = sorted(r.buckets, key=lambda b: b.bucket_at)[10]
        self.assertIsNotNone(resolved_bucket.nichicon_soc)

    def test_soc_three_gaps_is_still_usable(self):
        # QA再レビュー(2回目) 追加テスト: nichicon_soc単独でも境界値(3個)までは補間対象。
        buckets = self._golden_day()
        for i in (10, 11, 12):
            buckets[i] = make_bucket(buckets[i].bucket_at, nichicon_soc=None)
        r = lm.resolve_day_buckets(buckets, date(2026, 9, 1))
        self.assertIsNotNone(r.buckets)
        self.assertEqual(r.interpolated_values, 3)
        self.assertEqual(r.interpolated_slots, 3)

    def test_extra_off_slot_row_makes_day_unusable(self):
        # QA再レビュー #5: 想定外時刻（生成スロット集合外）の行が混入していると不採用にする
        # （行数超過を防ぐため、代わりに正規の1バケットを削って総数は288に保つ）。
        buckets = self._golden_day()[:-1]  # 287件にしておく
        buckets.append(make_bucket("2026-09-01 24:00"))  # 想定外の時刻（生成スロット集合外）
        self.assertEqual(len(buckets), lm.DAY_BUCKETS)
        r = lm.resolve_day_buckets(buckets, date(2026, 9, 1))
        self.assertIsNone(r.buckets)
        self.assertIn("想定外の時刻", r.reason_detail)

    def test_row_count_over_288_makes_day_unusable(self):
        # QA再レビュー #5: 288を超える行数（重複ではなく単純な水増し）も不採用にする。
        buckets = self._golden_day()
        buckets.append(make_bucket("2026-09-02 00:00"))  # 翌日の正規スロットだが総数が289になる
        r = lm.resolve_day_buckets(buckets, date(2026, 9, 1))
        self.assertIsNone(r.buckets)
        self.assertIn("バケット行数超過", r.reason_detail)


class MissingDaysReasonTest(unittest.TestCase):
    """読者向けQAレビュー対応（2026-09-06）: _missing_days_reason が全日欠測と一部欠測を
    reason_code で区別し、reason_label に内部識別子（日付範囲・件数比）を出さないことを確認する。"""

    def test_all_days_missing_is_profile_missing(self):
        r = lm._missing_days_reason(["2026-08-02", "2026-08-03"], total_days=2)
        self.assertEqual(r["reason_code"], "profile_missing")
        self.assertEqual(r["reason_label"], "5分プロファイル未取得")
        self.assertIn("5分プロファイル欠測", r["reason_detail"])

    def test_partial_days_missing_is_period_incomplete(self):
        r = lm._missing_days_reason(["2026-08-02"], total_days=31)
        self.assertEqual(r["reason_code"], "period_incomplete")
        # QA再レビュー #14: period_incomplete(5分プロファイル欠落)はbill_model.pyの
        # daily_missing(センサー日次欠測=「計測データ欠測」)と区別するラベルにする。
        self.assertEqual(r["reason_label"], "5分プロファイル欠落（1日）")
        self.assertIn("2026-08-02", r["reason_detail"])
        self.assertNotIn("2026-08-02", r["reason_label"])


class QualityGateTest(unittest.TestCase):
    def test_day_with_missing_bucket_is_unusable(self):
        # 23個しかない(265個欠落、補間許容の3個を大幅に超える)ため不採用になる。
        buckets = [make_bucket(f"2026-09-01 {h:02d}:00") for h in range(23)]  # 288に足りない
        usable, reason = lm.day_is_usable(buckets, date(2026, 9, 1))
        self.assertFalse(usable)
        self.assertIn("バケット欠落", reason)

    def test_day_with_many_null_field_buckets_is_unusable(self):
        # オーナー承認機能（2026-09-06）: 欠落4個は補間の許容(3個)を超えるため不採用のまま。
        buckets = build_synthetic_golden_day_buckets(day="2026-09-01")
        for i in (10, 11, 12, 13):
            buckets[i] = make_bucket(buckets[i].bucket_at, eco_ac_in_w=None)
        usable, reason = lm.day_is_usable(buckets, date(2026, 9, 1))
        self.assertFalse(usable)
        self.assertIn("バケット欠落", reason)
        self.assertIn("4件", reason)

    def test_no_profile_data_is_unusable(self):
        usable, reason = lm.day_is_usable(None, date(2026, 9, 1))
        self.assertFalse(usable)
        self.assertEqual(reason, "5分プロファイルデータなし")

    def test_duplicate_bucket_at_is_not_counted_as_complete_day(self):
        # 追加テストb: 288行あっても bucket_at が重複していれば、実際には異なる時刻が
        # 欠落している（重複分で頭数が水増しされている）ため usable=False にする。
        buckets = [make_bucket(f"2026-09-01 {(h * 5) // 60:02d}:{(h * 5) % 60:02d}") for h in range(lm.DAY_BUCKETS - 1)]
        buckets.append(make_bucket("2026-09-01 00:00"))  # 00:00 を重複させ、本来あるべき最終時刻を欠落させる
        self.assertEqual(len(buckets), lm.DAY_BUCKETS)
        usable, reason = lm.day_is_usable(buckets, date(2026, 9, 1))
        self.assertFalse(usable)
        self.assertIn("bucket_at重複", reason)

    def test_partial_month_marks_layer_unavailable(self):
        # QA #2: coverage 1.0 未満（1日でも欠測）ならL0〜L2はunavailable。0.95ゲートは廃止。
        tariff = make_tariff()
        start, end = bill_model.billing_period("2026-09", meter_read_day=2)
        daily = {}
        d = start
        while d <= end:
            daily[d.isoformat()] = {"date": d.isoformat(), "buy_kwh": 1.0, "solar_kwh": 10.0, "sell_kwh": 5.0}
            d += timedelta(days=1)

        profile_by_date = {}
        d = start
        while d <= end:
            if d.isoformat() != "2026-08-31":  # 1日だけ欠測させる（coverage=29/30≈0.967>0.95でも不可）
                profile_by_date[d.isoformat()] = build_synthetic_golden_day_buckets(day=d.isoformat())
            d += timedelta(days=1)
        record = lm.build_month_layers(tariff, "2026-09", daily, {}, {}, profile_by_date)
        self.assertIsNotNone(record)
        self.assertTrue(record["layers"]["L3"]["available"])
        self.assertFalse(record["layers"]["L0"]["available"])
        l0_reason = record["layers"]["L0"]["unavailable_reason"]
        # 1日だけ欠測(31日中)なので profile_missing ではなく period_incomplete。
        self.assertEqual(l0_reason["reason_code"], "period_incomplete")
        self.assertEqual(l0_reason["reason_label"], "5分プロファイル欠落（1日）")
        self.assertIn("5分プロファイル欠測", l0_reason["reason_detail"])
        self.assertIn("1/31", l0_reason["reason_detail"])
        self.assertIn("2026-08-31", l0_reason["reason_detail"])
        # reason_labelには内部識別子（日付・件数比の生文言）を出さない。
        self.assertNotIn("2026-08-31", l0_reason["reason_label"])

    def test_full_month_available_when_all_days_usable(self):
        # coverage==1.0（全日usable）ならL0〜L2はavailableになる。
        tariff = make_tariff()
        start, end = bill_model.billing_period("2026-09", meter_read_day=2)
        daily = {}
        profile_by_date = {}
        d = start
        while d <= end:
            daily[d.isoformat()] = {"date": d.isoformat(), "buy_kwh": 1.0, "solar_kwh": 10.0, "sell_kwh": 5.0}
            profile_by_date[d.isoformat()] = build_synthetic_golden_day_buckets(day=d.isoformat())
            d += timedelta(days=1)
        record = lm.build_month_layers(tariff, "2026-09", daily, {}, {}, profile_by_date)
        self.assertTrue(record["layers"]["L0"]["available"])
        self.assertTrue(record["layers"]["L1"]["available"])
        self.assertTrue(record["layers"]["L2"]["available"])
        self.assertEqual(record["coverage"], 1.0)
        self.assertEqual(record["interpolated_buckets"], 0)

    def test_month_with_small_gap_is_still_available_via_interpolation(self):
        # オーナー承認機能（2026-09-06）: 1日3バケットまでの欠落は線形補間されるため、
        # coverage==1.0のまま available になり、interpolated_bucketsに補間数が記録される。
        tariff = make_tariff()
        start, end = bill_model.billing_period("2026-09", meter_read_day=2)
        daily = {}
        profile_by_date = {}
        d = start
        while d <= end:
            day_buckets = build_synthetic_golden_day_buckets(day=d.isoformat())
            if d.isoformat() == "2026-08-15":
                day_buckets.pop(100)  # 1バケット欠落
            daily[d.isoformat()] = {"date": d.isoformat(), "buy_kwh": 1.0, "solar_kwh": 10.0, "sell_kwh": 5.0}
            profile_by_date[d.isoformat()] = day_buckets
            d += timedelta(days=1)
        record = lm.build_month_layers(tariff, "2026-09", daily, {}, {}, profile_by_date)
        self.assertTrue(record["layers"]["L0"]["available"])
        self.assertEqual(record["coverage"], 1.0)
        # QA再レビュー #4/#6: nichicon_socも補間対象のため REQUIRED_LOAD_FIELDS(7)+soc(1)=8。
        self.assertEqual(record["interpolated_buckets"], len(lm.REQUIRED_LOAD_FIELDS) + 1)

    def test_uncertainty_band_differs_across_pv_ac_efficiency(self):
        # QA missing-test#9: 不確かさ帯がpv_ac_efficiency 1.00/0.95で異なる（min!=max）。
        tariff = make_tariff()
        start, end = bill_model.billing_period("2026-09", meter_read_day=2)
        daily = {}
        profile_by_date = {}
        d = start
        while d <= end:
            daily[d.isoformat()] = {"date": d.isoformat(), "buy_kwh": 1.0, "solar_kwh": 10.0, "sell_kwh": 5.0}
            profile_by_date[d.isoformat()] = build_synthetic_golden_day_buckets(day=d.isoformat())
            d += timedelta(days=1)
        record = lm.build_month_layers(tariff, "2026-09", daily, {}, {}, profile_by_date)
        uncertainty = record["uncertainty"]
        self.assertNotEqual(uncertainty["L1"]["net_cost_fit_yen_min"], uncertainty["L1"]["net_cost_fit_yen_max"])
        self.assertNotEqual(uncertainty["L2"]["net_cost_fit_yen_min"], uncertainty["L2"]["net_cost_fit_yen_max"])

    def test_ecoflow_balance_warning_detected_when_out_exceeds_in(self):
        buckets = [
            make_bucket(f"2026-09-01 b{i}", eco_ac_in_w=0.0, eco_ac_out_w=500.0) for i in range(10)
        ]
        warning = lm.eco_balance_warning(buckets)
        self.assertIsNotNone(warning)
        self.assertIn("EcoFlow収支異常", warning)

    def test_ecoflow_balance_warning_absent_when_in_covers_out(self):
        buckets = [
            make_bucket(f"2026-09-01 b{i}", eco_ac_in_w=600.0, eco_ac_out_w=500.0) for i in range(10)
        ]
        warning = lm.eco_balance_warning(buckets)
        self.assertIsNone(warning)

    def test_max_export_warning_printed_to_stderr(self):
        tariff = make_tariff()
        start, end = bill_model.billing_period("2026-09", meter_read_day=2)
        # 意図的に9400Wを超える売電が発生するL1想定バケットを1日分作る（実測を超える極端値）。
        # load = solar+npv-nb+buy-sell+eco_out-eco_in = 4000+6000-9500 = 500W
        # pv_ac(L1, eff=1.0) = solar+npv = 10000W → sell_L1 = 10000-500 = 9500W > 9400W
        row = dict(solar_w=4000.0, buy_w=0.0, sell_w=9500.0, nichicon_pv_w=6000.0, nichicon_battery_w=0.0,
                   nichicon_soc=50.0, eco_ac_in_w=0.0, eco_ac_out_w=0.0)
        profile_by_date = {}
        d = start
        while d <= end:
            profile_by_date[d.isoformat()] = [
                make_bucket(f"{d.isoformat()} {h // 60:02d}:{h % 60:02d}", **row) for h in range(0, 24 * 60, 5)
            ]
            d += timedelta(days=1)
        stderr = io.StringIO()
        with contextlib.redirect_stderr(stderr):
            record = lm.build_month_layers(tariff, "2026-09", {}, {}, {}, profile_by_date)
        self.assertIsNotNone(record)
        self.assertIn("最大逆潮流推定", stderr.getvalue())


if __name__ == "__main__":
    unittest.main()
