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
import os
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
# golden day 合成フィクスチャ（QA #1 BLOCKER 対応）
# ---------------------------------------------------------------------------
# 12個のアンカー時刻の値は 2026-09-01 の実測値（docs/design/20260905_layer-model-ddr.md
# §0「検証の要点」に記載の値と同一。00:00と12:20はDDR本文にも明記されている）。
# 分 -> フィールド値の辞書。1440(=翌日00:00)は周期境界として0分の値を再利用する。
_GOLDEN_ANCHOR_VALUES = {
    0: dict(solar_w=0.0, buy_w=0.0, sell_w=0.0, nichicon_battery_w=-446.0, nichicon_pv_w=0.0,
            nichicon_soc=56.0, eco_ac_in_w=0.0, eco_ac_out_w=346.8),
    180: dict(solar_w=0.0, buy_w=0.0, sell_w=0.0, nichicon_battery_w=-447.0, nichicon_pv_w=0.0,
              nichicon_soc=39.0, eco_ac_in_w=0.0, eco_ac_out_w=268.8),
    360: dict(solar_w=1206.9, buy_w=0.0, sell_w=372.4, nichicon_battery_w=59.0, nichicon_pv_w=78.0,
              nichicon_soc=23.0, eco_ac_in_w=257.4, eco_ac_out_w=229.6),
    445: dict(solar_w=2579.3, buy_w=0.0, sell_w=734.5, nichicon_battery_w=623.0, nichicon_pv_w=619.0,
              nichicon_soc=27.0, eco_ac_in_w=883.8, eco_ac_out_w=483.8),
    540: dict(solar_w=4000.0, buy_w=0.0, sell_w=753.6, nichicon_battery_w=1404.0, nichicon_pv_w=1398.0,
              nichicon_soc=39.0, eco_ac_in_w=1942.4, eco_ac_out_w=169.8),
    600: dict(solar_w=4000.0, buy_w=0.0, sell_w=620.7, nichicon_battery_w=2358.0, nichicon_pv_w=2352.0,
              nichicon_soc=58.0, eco_ac_in_w=1895.2, eco_ac_out_w=143.0),
    660: dict(solar_w=2325.0, buy_w=0.0, sell_w=475.0, nichicon_battery_w=2082.0, nichicon_pv_w=2071.0,
              nichicon_soc=79.0, eco_ac_in_w=868.6, eco_ac_out_w=168.8),
    730: dict(solar_w=4000.0, buy_w=0.0, sell_w=6831.0, nichicon_battery_w=0.0, nichicon_pv_w=3905.0,
              nichicon_soc=100.0, eco_ac_in_w=217.2, eco_ac_out_w=158.0),
    740: dict(solar_w=2907.1, buy_w=0.0, sell_w=4450.0, nichicon_battery_w=0.0, nichicon_pv_w=2736.0,
              nichicon_soc=100.0, eco_ac_in_w=174.7, eco_ac_out_w=141.3),
    900: dict(solar_w=800.0, buy_w=10.3, sell_w=189.7, nichicon_battery_w=0.0, nichicon_pv_w=588.0,
              nichicon_soc=100.0, eco_ac_in_w=256.0, eco_ac_out_w=233.4),
    1080: dict(solar_w=0.0, buy_w=6.7, sell_w=0.0, nichicon_battery_w=-893.0, nichicon_pv_w=0.0,
               nichicon_soc=88.0, eco_ac_in_w=0.0, eco_ac_out_w=221.6),
    1260: dict(solar_w=0.0, buy_w=0.0, sell_w=0.0, nichicon_battery_w=-1250.0, nichicon_pv_w=0.0,
               nichicon_soc=58.0, eco_ac_in_w=0.0, eco_ac_out_w=407.8),
}
_GOLDEN_ANCHOR_VALUES[1440] = _GOLDEN_ANCHOR_VALUES[0]
_GOLDEN_FIELDS = ("solar_w", "buy_w", "sell_w", "nichicon_battery_w", "nichicon_pv_w",
                   "nichicon_soc", "eco_ac_in_w", "eco_ac_out_w")
GOLDEN_SNAPSHOTS_HHMM = {
    "00:00": 0, "03:00": 180, "06:00": 360, "07:25": 445, "09:00": 540, "10:00": 600,
    "11:00": 660, "12:10": 730, "12:20": 740, "15:00": 900, "18:00": 1080, "21:00": 1260,
}


def build_synthetic_golden_day_buckets(day: str = "2026-09-01") -> list:
    """QA #1 (BLOCKER): 実データをコミットできないため、12個の実測アンカー点（上記）を
    線形補間して滑らかな288バケット/日の合成プロファイルを作る。アンカー時刻ちょうどの値は
    実測値と文字どおり一致し、それ以外の276バケットは補間による合成値（時間帯粒度の実データを
    含まない）。"""
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
        # golden day 00:00 実測値: 蓄電池放電446W・DELTA出力347W・太陽光ゼロ → 復元負荷793W
        b = make_bucket(solar_w=0.0, buy_w=0.0, sell_w=0.0, nichicon_pv_w=0.0, nichicon_battery_w=-446.0,
                         eco_ac_in_w=0.0, eco_ac_out_w=346.8)
        self.assertAlmostEqual(b.load_true_w(), 792.8, places=1)

    def test_sell_greater_than_solar_uses_west_roof_pv(self):
        # golden day 12:20 実測値（DDR §0検証の要点）。西屋根PVが主パワコンと別系統である
        # ことの根拠になった時刻。復元負荷は1159.7W(±1)。
        b = make_bucket(solar_w=2907.1, buy_w=0.0, sell_w=4450.0, nichicon_pv_w=2736.0, nichicon_battery_w=0.0,
                         eco_ac_in_w=174.7, eco_ac_out_w=141.3)
        self.assertAlmostEqual(b.load_true_w(), 1159.7, delta=1.0)

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
        rows = [{
            "bucket": "2026-09-01 00:00", "solar_w": "0.0", "buy_w": "0.0", "sell_w": "0.0",
            "nichicon_battery_w": "-446.0", "nichicon_pv_w": "0.0", "nichicon_soc": "56.0",
            "eco_ac_in_w": "0.0", "eco_ac_out_w": "346.8", "eco_usb_out_w": "0.0",
            "n_p": "5", "n_n": "1", "n_e": "20",
        }]
        buckets = lm.parse_profile_rows(rows)
        self.assertEqual(len(buckets), 1)
        self.assertEqual(buckets[0].bucket_at, "2026-09-01 00:00")
        self.assertAlmostEqual(buckets[0].load_true_w(), 792.8, places=1)

    def test_missing_bucket_column_raises(self):
        with self.assertRaises(KeyError):
            lm.parse_profile_rows([{"solar_w": "0.0"}])

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

    def test_twelve_snapshots_match_real_measured_values(self):
        # アンカー時刻は実測値と文字どおり一致する（QA #1 の指示どおり残す12点）。
        expected = {
            "00:00": 792.8, "03:00": 715.8, "06:00": 825.7, "07:25": 1440.8,
            "09:00": 1467.8, "10:00": 1621.1, "11:00": 1139.2, "12:10": 1014.8,
            "12:20": 1159.7, "15:00": 1186.0, "18:00": 1121.3, "21:00": 1657.8,
        }
        by_time = {b.bucket_at[-5:]: b for b in self.buckets}
        for t, expected_w in expected.items():
            with self.subTest(time=t):
                self.assertAlmostEqual(by_time[t].load_true_w(), expected_w, delta=0.5)

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

    def test_twelve_snapshots_match_expected_load(self):
        expected = {
            "00:00": 792.8, "03:00": 715.8, "06:00": 825.7, "07:25": 1440.8,
            "09:00": 1467.8, "10:00": 1621.1, "11:00": 1139.2, "12:10": 1014.8,
            "12:20": 1159.7, "15:00": 1186.0, "18:00": 1121.3, "21:00": 1657.8,
        }
        by_time = {b.bucket_at[-5:]: b for b in self.buckets}
        for t, expected_w in expected.items():
            with self.subTest(time=t):
                self.assertAlmostEqual(by_time[t].load_true_w(), expected_w, delta=0.5)

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
        self.assertIn("5分プロファイル欠測", record["layers"]["L0"]["unavailable_reason"])

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


# ---------------------------------------------------------------------------
# E. 品質ゲート（DDR §5-E）
# ---------------------------------------------------------------------------
class QualityGateTest(unittest.TestCase):
    def test_day_with_missing_bucket_is_unusable(self):
        buckets = [make_bucket(f"2026-09-01 {h:02d}:00") for h in range(23)]  # 288に足りない
        usable, reason = lm.day_is_usable(buckets, date(2026, 9, 1))
        self.assertFalse(usable)
        self.assertIn("バケット数不足", reason)

    def test_day_with_null_field_bucket_is_unusable(self):
        buckets = [make_bucket(f"2026-09-01 bucket{i}", eco_ac_in_w=None) for i in range(lm.DAY_BUCKETS)]
        usable, reason = lm.day_is_usable(buckets, date(2026, 9, 1))
        self.assertFalse(usable)

    def test_no_profile_data_is_unusable(self):
        usable, reason = lm.day_is_usable(None, date(2026, 9, 1))
        self.assertFalse(usable)
        self.assertEqual(reason, "5分プロファイルデータなし")

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
        self.assertIn("5分プロファイル欠測", record["layers"]["L0"]["unavailable_reason"])
        self.assertIn("1/31", record["layers"]["L0"]["unavailable_reason"])
        self.assertIn("2026-08-31", record["layers"]["L0"]["unavailable_reason"])

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
