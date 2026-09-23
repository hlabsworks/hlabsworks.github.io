#!/usr/bin/env python3
"""calibrate_delta_model.py の unittest。

実行方法:
  cd scripts/blog-metrics && python3 -m unittest test_calibrate_delta_model -v

設計書（非公開） §7「その他」: 合成データで既知のパラメータ（idle 25、margin 200）が
格子探索で取り戻せることを確認する。テスト用に候補グリッドを縮小して実行時間を抑える
（本番の2340通りは実データ較正でのみ実行する。本テストは仕組みの正しさの検証が目的）。
"""
import csv
import json
import sys
import tempfile
import unittest
from dataclasses import replace as dataclass_replace
from datetime import date, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import calibrate_delta_model as cal  # noqa: E402
import delta_model as dm  # noqa: E402
import layer_model as lm  # noqa: E402
import test_layer_model as tlm  # noqa: E402

_PROFILE_FIELDS = (
    "bucket_at", "solar_w", "buy_w", "sell_w", "nichicon_pv_w",
    "nichicon_battery_w", "nichicon_soc", "eco_ac_in_w", "eco_ac_out_w",
)


def _make_true_params(capacity_factor: float, eta: float, idle_w: float, margin_w: float) -> dm.DeltaFleetParams:
    units = tuple(
        dm.DeltaUnitSpec(
            name=name, kind="delta3" if name == "u4" else "delta2",
            capacity_wh=lm.DELTA_UNIT_NOMINAL_WH[name] * capacity_factor,
            load_share=lm._DELTA_MODEL_LOAD_SHARE[name], max_charge_w=1400.0,
        )
        for name in cal.UNIT_ORDER
    )
    return dm.DeltaFleetParams(units=units, charge_efficiency=eta, discharge_efficiency=eta, idle_w=idle_w, tracking_margin_w=margin_w)


def _generate_synthetic_calibration_inputs(
    true_params: dm.DeltaFleetParams, dates: list[str], soc_start_pct: float
) -> tuple[list, list[dict]]:
    """既知パラメータでreplay自己整合な合成データを作る（test_layer_model.
    build_self_consistent_golden_periodと同じ考え方だが、ecoflow_unit_daily.json相当の
    台ごと日次サマリ(soc_min/max・discharge_kwh)も同時に記録する必要があるため、専用に
    ここで実装する）。discharge_kwhの代わりに「按分負荷の積算量」を使う。load_shareは
    デルタ負荷への按分比そのものなので、この積算量の比は真のload_shareに厳密に一致する
    （compute_load_shareの検証に十分）。"""
    states = dm.init_states(true_params, soc_start_pct)
    dt_h = lm.BUCKET_MINUTES / 60.0
    buckets_out: list = []
    unit_daily_rows: list[dict] = []
    served_wh: dict[str, dict[str, float]] = {u.name: {} for u in true_params.units}

    for d in dates:
        day_buckets = tlm.build_synthetic_golden_day_buckets(day=d)
        soc_start_by_unit = {u.name: states[i].soc_pct for i, u in enumerate(true_params.units)}
        soc_min = dict(soc_start_by_unit)
        soc_max = dict(soc_start_by_unit)
        for b in day_buckets:
            load_w = b.load_true_w()
            delta_load_w = max(0.0, b.eco_ac_out_w)
            house_load_w = load_w - delta_load_w
            pv_w = b.solar_w + b.nichicon_pv_w - b.nichicon_battery_w
            surplus_w = pv_w - house_load_w
            step = dm.fleet_step(states, true_params, surplus_w, delta_load_w, dt_h)
            if abs(step.unserved_w) > 1e-6:
                raise AssertionError(f"合成データ生成中にunservedが発生しました: {b.bucket_at} unserved_w={step.unserved_w}")
            grid_w = house_load_w + step.ac_in_w + step.unserved_w - pv_w
            buckets_out.append(
                dataclass_replace(b, eco_ac_in_w=step.ac_in_w, buy_w=max(0.0, grid_w), sell_w=max(0.0, -grid_w))
            )
            for i, u in enumerate(true_params.units):
                l_i = delta_load_w * u.load_share
                served_wh[u.name][d] = served_wh[u.name].get(d, 0.0) + l_i * dt_h
                soc_min[u.name] = min(soc_min[u.name], states[i].soc_pct)
                soc_max[u.name] = max(soc_max[u.name], states[i].soc_pct)
        for i, u in enumerate(true_params.units):
            nominal = lm.DELTA_UNIT_NOMINAL_WH[u.name]
            device_type = "DELTA3_PLUS" if u.kind == "delta3" else ("DELTA2_MAX" if nominal == 6144 else "DELTA2_MAX_S")
            unit_daily_rows.append({
                "date": d, "device_type": device_type, "capacity_wh": nominal,
                "soc_start_percent": round(soc_start_by_unit[u.name], 1),
                "soc_end_percent": round(states[i].soc_pct, 1),
                "soc_min_percent": round(soc_min[u.name], 1),
                "soc_max_percent": round(soc_max[u.name], 1),
                "charge_kwh": 0.0,
                "discharge_kwh": round(served_wh[u.name][d] / 1000.0, 4),
                "ac_input_kwh": 0.0,
                "unit": u.name,
            })
    return buckets_out, unit_daily_rows


class GridSearchRecoversKnownParamsTest(unittest.TestCase):
    def setUp(self):
        # テスト用に候補グリッドを縮小する（本番2340通りは実データでのみ実行）。
        self._orig = (cal.ETA_CANDIDATES, cal.IDLE_CANDIDATES, cal.MARGIN_CANDIDATES, cal.CAPACITY_FACTOR_CANDIDATES)
        cal.ETA_CANDIDATES = (0.88, 0.90, 0.92)
        cal.IDLE_CANDIDATES = (20.0, 25.0, 30.0)
        cal.MARGIN_CANDIDATES = (150.0, 200.0, 250.0)
        cal.CAPACITY_FACTOR_CANDIDATES = (0.90, 0.95, 1.00)

    def tearDown(self):
        cal.ETA_CANDIDATES, cal.IDLE_CANDIDATES, cal.MARGIN_CANDIDATES, cal.CAPACITY_FACTOR_CANDIDATES = self._orig

    def test_grid_search_recovers_idle_and_margin(self):
        true_eta, true_idle, true_margin, true_capacity_factor = 0.90, 25.0, 200.0, 0.95
        true_params = _make_true_params(true_capacity_factor, true_eta, true_idle, true_margin)
        dates = [(date(2026, 9, 1) + timedelta(days=i)).isoformat() for i in range(4)]
        # SOC 30%開始（低め）にすることで緊急/充電の遷移が起き、marginの識別力が上がる
        # （SOC 50%開始・短期間だと margin=200/250 の目的関数値がほぼ同点になり得た。実験で確認済み）。
        buckets, unit_daily_rows = _generate_synthetic_calibration_inputs(true_params, dates, soc_start_pct=30.0)

        with tempfile.TemporaryDirectory() as tmp:
            profile_path = Path(tmp) / "profile.csv"
            with open(profile_path, "w", newline="", encoding="utf-8") as f:
                writer = csv.DictWriter(f, fieldnames=_PROFILE_FIELDS)
                writer.writeheader()
                for b in buckets:
                    writer.writerow({field: getattr(b, field) for field in _PROFILE_FIELDS})
            ecoflow_path = Path(tmp) / "ecoflow_unit_daily.json"
            ecoflow_path.write_text(json.dumps(unit_daily_rows, ensure_ascii=False), encoding="utf-8")

            result = cal.run_calibration(profile_path, ecoflow_path)

        central = result["central"]
        self.assertAlmostEqual(central["charge_efficiency"], true_eta, places=6)
        self.assertAlmostEqual(central["idle_w"], true_idle, places=6)
        self.assertAlmostEqual(central["tracking_margin_w"], true_margin, places=6)
        self.assertAlmostEqual(central["capacity_factor"], true_capacity_factor, places=6)
        self.assertLess(central["score"], 0.05)

        # load_share も按分負荷の積算量から正しく復元される（誤差1%未満）。
        for name in cal.UNIT_ORDER:
            expected = lm._DELTA_MODEL_LOAD_SHARE[name]
            self.assertAlmostEqual(result["load_share"][name], expected, delta=0.01)


class CanonicalUnitNameTest(unittest.TestCase):
    """コーディネーター訂正(2026-09-24): scratchのunit番号は信用せずdevice_type+capacity_whで
    正規化する。同容量2048Whの2台がu3=DELTA3_PLUS/u4=DELTA2_MAX_S(実際とは逆順)でも
    正しくu3=MAX_S側/u4=DELTA3側に振り直せること。"""

    def test_reversed_2048wh_pair_is_normalized_by_device_type(self):
        rows = [
            {"capacity_wh": 6144, "device_type": "DELTA2_MAX"},
            {"capacity_wh": 4096, "device_type": "DELTA2_MAX_S"},
            {"capacity_wh": 2048, "device_type": "DELTA3_PLUS"},  # ファイル上は"u3"表記でも実体はDELTA3
            {"capacity_wh": 2048, "device_type": "DELTA2_MAX_S"},  # ファイル上は"u4"表記でも実体はMAX_S
        ]
        names = [cal._canonical_unit_name(r) for r in rows]
        self.assertEqual(names, ["u1", "u2", "u4", "u3"])

    def test_unknown_capacity_raises(self):
        with self.assertRaises(ValueError):
            cal._canonical_unit_name({"capacity_wh": 9999, "device_type": "DELTA2_MAX"})


if __name__ == "__main__":
    unittest.main()
