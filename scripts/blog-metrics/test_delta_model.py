#!/usr/bin/env python3
"""delta_model.py の unittest。

実行方法:
  cd scripts/blog-metrics && python3 -m unittest test_delta_model -v

設計書（非公開） §7「test_delta_model.py」の13項目に対応する（項目番号をコメントで示す）。
本番の較正値ではなく、仕組みを検証するためのテスト用パラメータ（η=0.9、idle 20W、
1台2000Wh 等）を使う。
"""
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import delta_model as dm  # noqa: E402


def make_unit(name="u1", kind="delta2", capacity_wh=2000.0, load_share=1.0, max_charge_w=1400.0) -> dm.DeltaUnitSpec:
    return dm.DeltaUnitSpec(name=name, kind=kind, capacity_wh=capacity_wh, load_share=load_share, max_charge_w=max_charge_w)


def make_params(units, **overrides) -> dm.DeltaFleetParams:
    base = dict(
        units=tuple(units),
        charge_efficiency=0.9,
        discharge_efficiency=0.9,
        idle_w=20.0,
    )
    base.update(overrides)
    return dm.DeltaFleetParams(**base)


class FullLatchTest(unittest.TestCase):
    """1. 満充電ラッチ: SOC 95 で c=0、余剰は売電に残る。91.9 で解除、92.0 では解除されない。"""

    def test_full_sets_at_95_and_blocks_charging(self):
        params = make_params([make_unit()])
        states = [dm.UnitState(soc_pct=95.0)]
        result = dm.fleet_step(states, params, surplus_w=2000.0, delta_load_w=0.0, dt_h=5 / 60)
        self.assertTrue(states[0].full)
        # 満充電のため新規接続(speedup)されず、ac_inは0（余剰はfleetに吸われず売電側に残る）。
        self.assertFalse(states[0].plugged)
        self.assertEqual(result.ac_in_w, 0.0)

    def test_full_clears_below_92(self):
        params = make_params([make_unit()])
        states = [dm.UnitState(soc_pct=91.9, full=True)]
        dm.fleet_step(states, params, surplus_w=2000.0, delta_load_w=0.0, dt_h=5 / 60)
        self.assertFalse(states[0].full)

    def test_full_does_not_clear_at_92(self):
        params = make_params([make_unit()])
        states = [dm.UnitState(soc_pct=92.0, full=True)]
        dm.fleet_step(states, params, surplus_w=2000.0, delta_load_w=0.0, dt_h=5 / 60)
        self.assertTrue(states[0].full)


class Delta3MinChargeTest(unittest.TestCase):
    """2. DELTA3は接続中SOC<100なら100W。満充電ラッチ中でもSOC100に達するまで100W。"""

    def test_delta3_min_charge_while_full_latched(self):
        unit = make_unit(kind="delta3", capacity_wh=2000.0)
        params = make_params([unit])
        # soc=99: full latch(>=95)は立つが、100未満なのでcmin=100Wは続く。
        states = [dm.UnitState(soc_pct=99.0, plugged=True)]
        result = dm.fleet_step(states, params, surplus_w=2000.0, delta_load_w=0.0, dt_h=5 / 60)
        self.assertTrue(states[0].full)
        self.assertAlmostEqual(result.ac_in_w, 20.0 + 100.0, places=6)  # idle + cmin(100)、extraは満充電で0

    def test_delta3_no_min_charge_at_100(self):
        unit = make_unit(kind="delta3", capacity_wh=2000.0)
        params = make_params([unit])
        states = [dm.UnitState(soc_pct=100.0, plugged=True)]
        result = dm.fleet_step(states, params, surplus_w=2000.0, delta_load_w=0.0, dt_h=5 / 60)
        self.assertAlmostEqual(result.ac_in_w, 20.0, places=6)  # idleのみ


class UnservedTest(unittest.TestCase):
    """3. 空: 未接続でSOCがわずかな台に負荷 → unserved_w > 0、SOC=0。負荷は消えず系統側に回る。"""

    def test_insufficient_soc_produces_unserved_and_drains_to_zero(self):
        unit = make_unit(capacity_wh=100.0, load_share=1.0)
        params = make_params(
            [unit], idle_w=20.0, discharge_efficiency=0.9,
            emergency_on_pct=-1.0,  # emergencyで強制接続されないよう無効化（未接続のまま検証するため）
        )
        states = [dm.UnitState(soc_pct=50.0, plugged=False)]  # available_wh=50Wh
        result = dm.fleet_step(states, params, surplus_w=0.0, delta_load_w=1000.0, dt_h=1.0)
        self.assertFalse(states[0].plugged)
        self.assertGreater(result.unserved_w, 0.0)
        self.assertAlmostEqual(states[0].soc_pct, 0.0, places=6)
        # need_w=(1000+20)/0.9=1133.33..., available_wh=50 → f=50/1133.33...
        f = 50.0 / ((1000.0 + 20.0) / 0.9)
        self.assertAlmostEqual(result.unserved_w, (1.0 - f) * 1000.0, places=3)


class ChargeEfficiencyTest(unittest.TestCase):
    """4. 充電効率: 1000Wを1h → BMS +900Wh（dt 12回に分けて積算）。"""

    def test_charge_energy_accumulates_over_12_buckets(self):
        unit = make_unit(capacity_wh=1_000_000.0, max_charge_w=2000.0)  # SOC上限に当たらない大容量
        params = make_params([unit], charge_efficiency=0.9, idle_w=0.0, tracking_margin_w=0.0)
        states = dm.init_states(params, soc_pct=0.0)
        total_bms_in_wh = 0.0
        dt_h = 5 / 60
        for _ in range(12):
            result = dm.fleet_step(states, params, surplus_w=1000.0, delta_load_w=0.0, dt_h=dt_h)
            total_bms_in_wh += result.bms_in_w * dt_h
        self.assertAlmostEqual(total_bms_in_wh, 900.0, places=3)


class DischargeEfficiencyTest(unittest.TestCase):
    """5. 放電効率と待機: 負荷430W+idle20Wを1h → BMS -500Wh。"""

    def test_discharge_energy_accumulates_over_12_buckets(self):
        unit = make_unit(capacity_wh=1_000_000.0, load_share=1.0)
        params = make_params([unit], discharge_efficiency=0.9, idle_w=20.0, emergency_on_pct=-1.0)
        states = dm.init_states(params, soc_pct=100.0)
        total_bms_out_wh = 0.0
        dt_h = 5 / 60
        for _ in range(12):
            result = dm.fleet_step(states, params, surplus_w=0.0, delta_load_w=430.0, dt_h=dt_h)
            self.assertFalse(states[0].plugged)  # surplus=0 < speedup閾値のため未接続のまま
            total_bms_out_wh += result.bms_out_w * dt_h
        self.assertAlmostEqual(total_bms_out_wh, 500.0, places=3)


class PassthroughTest(unittest.TestCase):
    """6. パススルー: 接続中・充電0 → ac_in = L+idle、BMSは-3Wh/h。"""

    def test_passthrough_drain_when_full_and_connected(self):
        unit = make_unit(capacity_wh=2000.0, load_share=1.0)
        params = make_params([unit], idle_w=20.0, passthrough_drain_w=3.0)
        states = [dm.UnitState(soc_pct=100.0, plugged=True, full=True)]
        result = dm.fleet_step(states, params, surplus_w=1000.0, delta_load_w=300.0, dt_h=1.0)
        self.assertAlmostEqual(result.ac_in_w, 300.0 + 20.0, places=6)
        self.assertAlmostEqual(result.bms_out_w, 3.0, places=6)
        self.assertAlmostEqual(result.bms_in_w, 0.0, places=6)


class EmergencyModeTest(unittest.TestCase):
    """7. 緊急モード: 10.0で入り、surplus<0でも接続したまま（DELTA2はc=0、DELTA3は100W）。
    19.9では抜けず、20.0で抜ける。"""

    def test_enters_at_10_and_forces_connection(self):
        unit = make_unit(kind="delta2", load_share=1.0)
        params = make_params([unit], idle_w=20.0)
        states = [dm.UnitState(soc_pct=10.0)]
        result = dm.fleet_step(states, params, surplus_w=-1000.0, delta_load_w=0.0, dt_h=5 / 60)
        self.assertTrue(states[0].emergency)
        self.assertTrue(states[0].plugged)
        self.assertAlmostEqual(result.ac_in_w, 20.0, places=6)  # c=0（soc10はlifeline閾値7以下ではない）

    def test_delta3_emergency_min_charge_100w(self):
        unit = make_unit(kind="delta3", load_share=1.0)
        params = make_params([unit], idle_w=20.0)
        states = [dm.UnitState(soc_pct=10.0)]
        result = dm.fleet_step(states, params, surplus_w=-1000.0, delta_load_w=0.0, dt_h=5 / 60)
        self.assertAlmostEqual(result.ac_in_w, 20.0 + 100.0, places=6)

    def test_stays_emergency_at_19_9(self):
        unit = make_unit(kind="delta2")
        params = make_params([unit])
        states = [dm.UnitState(soc_pct=19.9, emergency=True, plugged=True)]
        dm.fleet_step(states, params, surplus_w=-1000.0, delta_load_w=0.0, dt_h=5 / 60)
        self.assertTrue(states[0].emergency)

    def test_exits_emergency_at_20_0(self):
        unit = make_unit(kind="delta2")
        params = make_params([unit])
        states = [dm.UnitState(soc_pct=20.0, emergency=True, plugged=True)]
        dm.fleet_step(states, params, surplus_w=-1000.0, delta_load_w=0.0, dt_h=5 / 60)
        self.assertFalse(states[0].emergency)


class LifelineTest(unittest.TestCase):
    """8. 延命充電: DELTA2でemergency中に7.0 → 100W、9.0で停止。"""

    def test_lifeline_starts_at_7(self):
        unit = make_unit(kind="delta2", load_share=1.0)
        params = make_params([unit], idle_w=20.0)
        states = [dm.UnitState(soc_pct=7.0)]
        result = dm.fleet_step(states, params, surplus_w=-1000.0, delta_load_w=0.0, dt_h=5 / 60)
        self.assertTrue(states[0].lifeline)
        self.assertAlmostEqual(result.ac_in_w, 20.0 + 100.0, places=6)

    def test_lifeline_stops_at_9(self):
        unit = make_unit(kind="delta2", load_share=1.0)
        params = make_params([unit], idle_w=20.0)
        states = [dm.UnitState(soc_pct=9.0, emergency=True, lifeline=True, plugged=True)]
        result = dm.fleet_step(states, params, surplus_w=-1000.0, delta_load_w=0.0, dt_h=5 / 60)
        self.assertFalse(states[0].lifeline)
        self.assertAlmostEqual(result.ac_in_w, 20.0, places=6)


class SpeedupThresholdTest(unittest.TestCase):
    """9. スピードアップ閾値: 未接続でsurplus=399 → 未接続のまま、400 → 接続。"""

    def test_below_threshold_stays_unplugged(self):
        unit = make_unit(load_share=1.0)
        params = make_params([unit], idle_w=0.0, speedup_threshold_w=400.0)
        states = [dm.UnitState(soc_pct=50.0)]
        dm.fleet_step(states, params, surplus_w=399.0, delta_load_w=0.0, dt_h=5 / 60)
        self.assertFalse(states[0].plugged)

    def test_at_threshold_connects(self):
        unit = make_unit(load_share=1.0)
        params = make_params([unit], idle_w=0.0, speedup_threshold_w=400.0)
        states = [dm.UnitState(soc_pct=50.0)]
        dm.fleet_step(states, params, surplus_w=400.0, delta_load_w=0.0, dt_h=5 / 60)
        self.assertTrue(states[0].plugged)


class BuyCutoffTest(unittest.TestCase):
    """10. 買電中の遮断: emergencyでない接続中の台がavail<0で、baseの大きい順に切断される
    （emergencyの台は除外される）。"""

    def test_disconnects_largest_base_first_and_skips_emergency(self):
        unit_a = make_unit(name="a", kind="delta2", load_share=1.0)  # L=1000（下でdelta_loadから計算）
        unit_b = make_unit(name="b", kind="delta2", load_share=0.2)
        unit_c = make_unit(name="c", kind="delta3", load_share=0.0)  # emergency強制、cmin=100だけ
        params = make_params([unit_a, unit_b, unit_c], idle_w=0.0)
        # delta_load_w=1000 → L_a=1000, L_b=200, L_c=0（load_share比率で按分。合計1.2だが
        # このテストではload_shareの合計1.0制約を検証対象にしないため無視して良い）
        states = [
            dm.UnitState(soc_pct=50.0, plugged=True),  # a
            dm.UnitState(soc_pct=50.0, plugged=True),  # b
            dm.UnitState(soc_pct=5.0, plugged=False),  # c: emergencyになる
        ]
        result = dm.fleet_step(states, params, surplus_w=500.0, delta_load_w=1000.0, dt_h=5 / 60)
        self.assertFalse(states[0].plugged)  # a（base=1000、最大）が切断される
        self.assertTrue(states[1].plugged)  # b（base=200）は残る
        self.assertTrue(states[2].plugged)  # c（emergency）は切断対象から除外
        self.assertTrue(states[2].emergency)
        self.assertEqual(result.plugged_units, 2)


class PlugLimitTest(unittest.TestCase):
    """11. プラグ上限: c_i ≤ 1450 − L_i − idle。base > 1450ならemergencyでも切断。"""

    def test_overload_evacuates_even_emergency_unit(self):
        unit = make_unit(kind="delta2", load_share=1.0)
        params = make_params([unit], idle_w=0.0, plug_input_limit_w=1450.0)
        states = [dm.UnitState(soc_pct=5.0, plugged=False)]  # emergencyになるがbase=1500>1450
        dm.fleet_step(states, params, surplus_w=2000.0, delta_load_w=1500.0, dt_h=5 / 60)
        self.assertTrue(states[0].emergency)
        self.assertFalse(states[0].plugged)  # 退避がemergencyより優先

    def test_charge_capped_by_plug_input_limit(self):
        unit = make_unit(kind="delta2", load_share=1.0, capacity_wh=1_000_000.0, max_charge_w=2000.0)
        params = make_params([unit], idle_w=0.0, plug_input_limit_w=1450.0, tracking_margin_w=0.0)
        states = [dm.UnitState(soc_pct=50.0, plugged=True)]
        result = dm.fleet_step(states, params, surplus_w=5000.0, delta_load_w=1000.0, dt_h=5 / 60)
        self.assertLessEqual(result.ac_in_w, 1450.0 + 1e-6)
        self.assertAlmostEqual(result.ac_in_w, 1450.0, places=6)  # base(1000)+cap2(450)


class AllocationOrderTest(unittest.TestCase):
    """12. 配分順: poolはSOCの低い台から配られる。"""

    def test_lower_soc_unit_gets_priority(self):
        unit_a = make_unit(name="a", capacity_wh=2000.0, load_share=0.0, max_charge_w=500.0)
        unit_b = make_unit(name="b", capacity_wh=2000.0, load_share=0.0, max_charge_w=500.0)
        params = make_params([unit_a, unit_b], idle_w=0.0, tracking_margin_w=0.0)
        states = [
            dm.UnitState(soc_pct=80.0, plugged=True),  # a: SOC高い
            dm.UnitState(soc_pct=30.0, plugged=True),  # b: SOC低い → 優先
        ]
        dm.fleet_step(states, params, surplus_w=600.0, delta_load_w=0.0, dt_h=5 / 60)
        delta_a = states[0].soc_pct - 80.0
        delta_b = states[1].soc_pct - 30.0
        self.assertGreater(delta_b, delta_a)  # SOCの低いbがより多く充電された


class EnergyConservationTest(unittest.TestCase):
    """13. エネルギー保存（全バケット）: Σac_in = ΣL_i(接続中) + idle×接続台数 + Σc_i。"""

    def test_ac_in_matches_load_idle_and_charge_sum(self):
        unit_a = make_unit(name="a", kind="delta2", load_share=0.6, capacity_wh=3000.0, max_charge_w=1400.0)
        unit_b = make_unit(name="b", kind="delta3", load_share=0.4, capacity_wh=2000.0, max_charge_w=1400.0)
        params = make_params([unit_a, unit_b], idle_w=25.0, charge_efficiency=0.9, discharge_efficiency=0.9)
        states = [dm.UnitState(soc_pct=40.0, plugged=True), dm.UnitState(soc_pct=60.0, plugged=True)]
        delta_load_w = 500.0
        surplus_w = 1200.0
        dt_h = 5 / 60
        result = dm.fleet_step(states, params, surplus_w=surplus_w, delta_load_w=delta_load_w, dt_h=dt_h)

        load = [delta_load_w * unit_a.load_share, delta_load_w * unit_b.load_share]
        connected_load_idle = sum(load[i] + params.idle_w for i in range(2) if states[i].plugged)
        # Σc_i は bms_in_w（＝Σc_i×η_c、接続中の台のみ寄与）から逆算する。
        sum_c = result.bms_in_w / params.charge_efficiency if params.charge_efficiency > 0 else 0.0
        self.assertAlmostEqual(result.ac_in_w, connected_load_idle + sum_c, places=6)


if __name__ == "__main__":
    unittest.main()
