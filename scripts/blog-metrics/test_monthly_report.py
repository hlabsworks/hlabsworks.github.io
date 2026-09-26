#!/usr/bin/env python3
"""monthly_report.py の unittest。

実行方法:
  cd scripts/blog-metrics && python3 -m unittest test_monthly_report -v

layer_model.py/bill_model.py を実走させず、is_closable()/build_snapshot_body()/run() が
期待するレコード形（layers.json の months[]/preliminary_months[] の1エントリと同じ形）を
直接組み立てる合成フィクスチャで検証する（値は本テストのために考案した合成値で実測値ではない）。
"""
from __future__ import annotations

import json
import sys
import tempfile
import unittest
from datetime import date, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import monthly_report as mr  # noqa: E402

FIRST_MONTH = "2026-10"  # このテストファイル内で使う基準（本番既定値と同じ）
METER_READ_DAY = 2  # 実際のtariff.jsonと同じ値。usage_period(start,end)はこれに基づく


def _layer(available: bool, buy_kwh: float = 0.0, sell_kwh: float = 0.0, net_fit: int = 0, net_post_fit: int = 0, extra: dict | None = None) -> dict:
    if not available:
        return {"available": False}
    d = {
        "available": True, "buy_kwh": buy_kwh, "sell_kwh": sell_kwh,
        "net_cost_fit_yen": net_fit, "net_cost_post_fit_yen": net_post_fit,
    }
    if extra:
        d.update(extra)
    return d


def make_month_record(
    billing_month: str,
    start: str,
    end: str,
    *,
    all_available: bool = True,
    l3_buy_source: str = "billed",
    l3_sell_source: str = "official_meter",
    estimation: str = "full",
    days_usable: int | None = None,
    l0_net_fit: int = 10000,
    l1_net_fit: int = 7000,
    l2_net_fit: int = 4000,
    l3_net_fit: int = 3000,
    l2_band: tuple[int, int] = (3500, 4500),
    with_uncertainty: bool = True,
) -> dict:
    days = (date.fromisoformat(end) - date.fromisoformat(start)).days + 1
    days_usable = days if days_usable is None else days_usable
    common_extra = {"estimation": estimation, "days_usable": days_usable, "days_total": days}
    record = {
        "billing_month": billing_month,
        "usage_period": {"start": start, "end": end, "days": days},
        "layers": {
            "L0": _layer(all_available, 100.0, 10.0, l0_net_fit, l0_net_fit - 200, common_extra),
            "L1": _layer(all_available, 80.0, 20.0, l1_net_fit, l1_net_fit - 200, common_extra),
            "L2": _layer(all_available, 60.0, 30.0, l2_net_fit, l2_net_fit - 200, common_extra),
            "L3": _layer(
                all_available, 50.0, 35.0, l3_net_fit, l3_net_fit - 200,
                {"buy_source": l3_buy_source, "sell_source": l3_sell_source},
            ),
        },
    }
    if with_uncertainty:
        record["uncertainty"] = {"L2": {"net_cost_fit_yen_min": l2_band[0], "net_cost_fit_yen_max": l2_band[1]}}
    else:
        record["uncertainty"] = {}
    return record


def make_daily_row(d: date, *, solar=20.0, sell=5.0, nichicon=3.0, ecoflow=1.0) -> dict:
    return {
        "date": d.isoformat(), "solar_kwh": solar, "buy_kwh": 1.0, "sell_kwh": sell,
        "nichicon_charge_kwh": nichicon, "ecoflow_charge_kwh": ecoflow,
        "self_consumption_shift_kwh": 4.0, "saving_yen": 300,
    }


def make_daily_rows(start: date, end: date, **kwargs) -> list[dict]:
    rows = []
    d = start
    while d <= end:
        rows.append(make_daily_row(d, **kwargs))
        d += timedelta(days=1)
    return rows


def write_data_dir(base: Path, layers: dict, daily_rows: list[dict]) -> Path:
    base.mkdir(parents=True, exist_ok=True)
    (base / "layers.json").write_text(json.dumps(layers, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    (base / "daily.json").write_text(json.dumps(daily_rows, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return base


class IsClosableTest(unittest.TestCase):
    def test_none_is_not_closable(self):
        self.assertFalse(mr.is_closable(None, first_report_billing_month=FIRST_MONTH))

    def test_full_confirmed_month_is_closable(self):
        rec = make_month_record(FIRST_MONTH, "2026-09-02", "2026-10-01")
        self.assertTrue(mr.is_closable(rec, first_report_billing_month=FIRST_MONTH))

    def test_sensor_buy_source_is_not_closable(self):
        rec = make_month_record(FIRST_MONTH, "2026-09-02", "2026-10-01", l3_buy_source="sensor")
        self.assertFalse(mr.is_closable(rec, first_report_billing_month=FIRST_MONTH))

    def test_before_first_report_month_is_not_closable(self):
        rec = make_month_record("2026-09", "2026-08-02", "2026-09-01")
        self.assertFalse(mr.is_closable(rec, first_report_billing_month=FIRST_MONTH))

    def test_unavailable_layer_is_not_closable(self):
        rec = make_month_record(FIRST_MONTH, "2026-09-02", "2026-10-01", all_available=False)
        self.assertFalse(mr.is_closable(rec, first_report_billing_month=FIRST_MONTH))

    def test_missing_uncertainty_band_is_not_closable(self):
        rec = make_month_record(FIRST_MONTH, "2026-09-02", "2026-10-01", with_uncertainty=False)
        self.assertFalse(mr.is_closable(rec, first_report_billing_month=FIRST_MONTH))


class ClassifyWeatherTest(unittest.TestCase):
    def test_causal_window_ignores_future_daily_rows(self):
        # DDR §C「窓の終端をusage_period.endに固定する」: usage_period.endより後のdaily行を
        # 足しても過去月の分類は変わらない。
        start, end = date(2026, 9, 2), date(2026, 10, 1)
        base_rows = {r["date"]: r for r in make_daily_rows(start - timedelta(days=60), end, solar=20.0)}
        before = mr.classify_weather(base_rows, start, end)
        future_rows = dict(base_rows)
        for r in make_daily_rows(end + timedelta(days=1), end + timedelta(days=60), solar=1.0):
            future_rows[r["date"]] = r
        after = mr.classify_weather(future_rows, start, end)
        self.assertEqual(before, after)

    def test_insufficient_samples_is_unknown(self):
        start, end = date(2026, 9, 2), date(2026, 9, 5)
        rows = {r["date"]: r for r in make_daily_rows(start, end, solar=20.0)}
        result = mr.classify_weather(rows, start, end)
        self.assertEqual(result["unknown_days"], (end - start).days + 1)

    def test_weather_counts_sum_to_period_days(self):
        start, end = date(2026, 9, 2), date(2026, 10, 1)
        rows = {r["date"]: r for r in make_daily_rows(start - timedelta(days=40), end, solar=20.0)}
        result = mr.classify_weather(rows, start, end)
        self.assertEqual(sum(result.values()), (end - start).days + 1)


class BuildSnapshotBodyTest(unittest.TestCase):
    def _layers_and_daily(self, billing_month="2026-10", start="2026-09-02", end="2026-10-01", **month_kwargs):
        rec = make_month_record(billing_month, start, end, **month_kwargs)
        layers = {"months": [rec], "preliminary_months": []}
        daily_rows = make_daily_rows(date.fromisoformat(start), date.fromisoformat(end))
        daily_by_date = {r["date"]: r for r in daily_rows}
        return layers, daily_by_date

    def test_final_snapshot_matches_expected_schema(self):
        layers, daily_by_date = self._layers_and_daily()
        body = mr.build_snapshot_body("2026-10", layers, daily_by_date, "final", METER_READ_DAY)
        self.assertIsNotNone(body)
        self.assertEqual(body["billing_month"], "2026-10")
        self.assertEqual(body["report_month"], "2026-09")
        self.assertEqual(body["stage"], "final")
        self.assertEqual(body["tariff_basis"], "confirmed")
        self.assertEqual(body["l3_source"], {"buy": "billed", "sell": "official_meter"})
        self.assertEqual(body["layers"]["L3"]["net_cost_fit_yen"], 3000)
        self.assertEqual(body["l2_band"], {"net_cost_fit_yen_min": 3500, "net_cost_fit_yen_max": 4500})
        self.assertIsNotNone(body["energy"]["solar_kwh"])
        self.assertEqual(sum(body["weather"].values()), body["usage_period"]["days"])

    def test_unavailable_layer_returns_none(self):
        layers, daily_by_date = self._layers_and_daily(all_available=False)
        self.assertIsNone(mr.build_snapshot_body("2026-10", layers, daily_by_date, "final", METER_READ_DAY))

    def test_final_stage_rejects_sensor_sourced_month(self):
        layers, daily_by_date = self._layers_and_daily(l3_buy_source="sensor")
        self.assertIsNone(mr.build_snapshot_body("2026-10", layers, daily_by_date, "final", METER_READ_DAY))

    def test_preliminary_stage_accepts_sensor_sourced_month(self):
        rec = make_month_record("2026-10", "2026-09-02", "2026-10-01", l3_buy_source="sensor")
        layers = {"months": [], "preliminary_months": [rec]}
        daily_rows = make_daily_rows(date(2026, 9, 2), date(2026, 10, 1))
        daily_by_date = {r["date"]: r for r in daily_rows}
        body = mr.build_snapshot_body("2026-10", layers, daily_by_date, "preliminary", METER_READ_DAY)
        self.assertIsNotNone(body)
        self.assertEqual(body["stage"], "preliminary")
        self.assertEqual(body["tariff_basis"], "provisional")

    def test_scaled_month_reports_partial_days_usable(self):
        layers, daily_by_date = self._layers_and_daily(estimation="scaled", days_usable=25)
        body = mr.build_snapshot_body("2026-10", layers, daily_by_date, "final", METER_READ_DAY)
        self.assertEqual(body["estimation"], "scaled")
        self.assertEqual(body["days_usable"], 25)
        self.assertLess(body["days_usable"], body["days_total"])

    def test_incomplete_energy_period_is_null(self):
        layers, daily_by_date = self._layers_and_daily()
        del daily_by_date["2026-09-15"]  # 1日欠測させるとenergy.*は全部null
        body = mr.build_snapshot_body("2026-10", layers, daily_by_date, "final", METER_READ_DAY)
        for key in body["energy"]:
            self.assertIsNone(body["energy"][key])

    def test_missing_billing_month_returns_none(self):
        layers, daily_by_date = self._layers_and_daily()
        self.assertIsNone(mr.build_snapshot_body("2099-01", layers, daily_by_date, "final", METER_READ_DAY))

    def test_billing_month_before_first_report_month_returns_none(self):
        # QA指摘2026-09-26 item3: build_snapshot_body自身もFIRST_REPORT_BILLING_MONTHの
        # 防波堤を強制する（呼び出し側がrun()を経由しない直接呼び出しでも安全なように）。
        layers, daily_by_date = self._layers_and_daily(billing_month="2026-09", start="2026-08-02", end="2026-09-01")
        self.assertIsNone(mr.build_snapshot_body("2026-09", layers, daily_by_date, "final", METER_READ_DAY))

    def test_usage_period_mismatch_with_billing_period_returns_none(self):
        # QA指摘2026-09-26 item3: usage_periodがbill_model.billing_period(billing_month,
        # meter_read_day)と一致しないレコードは作らない（billing_monthを騙って別期間の
        # usage_periodを埋め込む攻撃を防ぐ）。
        rec = make_month_record("2026-10", "2026-09-01", "2026-09-30")  # 本来は09-02〜10-01
        layers = {"months": [rec], "preliminary_months": []}
        daily_by_date = {r["date"]: r for r in make_daily_rows(date(2026, 9, 1), date(2026, 9, 30))}
        self.assertIsNone(mr.build_snapshot_body("2026-10", layers, daily_by_date, "final", METER_READ_DAY))


class RunTest(unittest.TestCase):
    """monthly_report.run() の冪等性・改版・凍結（§A・追補A'）。"""

    def _write(self, tmp: Path, layers: dict, daily_rows: list[dict]) -> tuple[Path, Path]:
        data_dir = write_data_dir(Path(tmp) / "data", layers, daily_rows)
        posts_dir = Path(tmp) / "posts"
        return data_dir, posts_dir

    def test_new_confirmed_month_is_written_once(self):
        rec = make_month_record("2026-10", "2026-09-02", "2026-10-01")
        layers = {"months": [rec], "preliminary_months": []}
        daily_rows = make_daily_rows(date(2026, 9, 2), date(2026, 10, 1))
        with tempfile.TemporaryDirectory() as tmp:
            data_dir, posts_dir = self._write(tmp, layers, daily_rows)
            written = mr.run(data_dir, posts_dir, date(2026, 10, 24), first_report_billing_month=FIRST_MONTH, meter_read_day=METER_READ_DAY)
            self.assertEqual(written, 1)
            snapshot = json.loads((posts_dir / "2026-10.json").read_text(encoding="utf-8"))
            self.assertEqual(snapshot["first_published"], "2026-10-24")
            self.assertEqual(snapshot["revision"], 1)
            self.assertIsNone(snapshot["revised"])
            self.assertEqual(snapshot["stage"], "final")

    def test_second_run_with_same_data_writes_nothing(self):
        rec = make_month_record("2026-10", "2026-09-02", "2026-10-01")
        layers = {"months": [rec], "preliminary_months": []}
        daily_rows = make_daily_rows(date(2026, 9, 2), date(2026, 10, 1))
        with tempfile.TemporaryDirectory() as tmp:
            data_dir, posts_dir = self._write(tmp, layers, daily_rows)
            mr.run(data_dir, posts_dir, date(2026, 10, 24), first_report_billing_month=FIRST_MONTH, meter_read_day=METER_READ_DAY)
            before = (posts_dir / "2026-10.json").read_bytes()
            written = mr.run(data_dir, posts_dir, date(2026, 10, 25), first_report_billing_month=FIRST_MONTH, meter_read_day=METER_READ_DAY)
            after = (posts_dir / "2026-10.json").read_bytes()
            self.assertEqual(written, 0)
            self.assertEqual(before, after)

    def test_revision_bump_when_body_changes(self):
        rec = make_month_record("2026-10", "2026-09-02", "2026-10-01", l3_net_fit=3000)
        layers = {"months": [rec], "preliminary_months": []}
        daily_rows = make_daily_rows(date(2026, 9, 2), date(2026, 10, 1))
        with tempfile.TemporaryDirectory() as tmp:
            data_dir, posts_dir = self._write(tmp, layers, daily_rows)
            mr.run(data_dir, posts_dir, date(2026, 10, 24), first_report_billing_month=FIRST_MONTH, meter_read_day=METER_READ_DAY)

            corrected = make_month_record("2026-10", "2026-09-02", "2026-10-01", l3_net_fit=3111)
            layers["months"] = [corrected]
            (data_dir / "layers.json").write_text(json.dumps(layers, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

            mr.run(data_dir, posts_dir, date(2026, 11, 1), first_report_billing_month=FIRST_MONTH, meter_read_day=METER_READ_DAY)
            snapshot = json.loads((posts_dir / "2026-10.json").read_text(encoding="utf-8"))
            self.assertEqual(snapshot["revision"], 2)
            self.assertEqual(snapshot["revised"], "2026-11-01")
            self.assertEqual(snapshot["first_published"], "2026-10-24")
            self.assertEqual(snapshot["layers"]["L3"]["net_cost_fit_yen"], 3111)

    def test_windowed_out_month_is_left_untouched(self):
        rec = make_month_record("2026-10", "2026-09-02", "2026-10-01")
        layers = {"months": [rec], "preliminary_months": []}
        daily_rows = make_daily_rows(date(2026, 9, 2), date(2026, 10, 1))
        with tempfile.TemporaryDirectory() as tmp:
            data_dir, posts_dir = self._write(tmp, layers, daily_rows)
            mr.run(data_dir, posts_dir, date(2026, 10, 24), first_report_billing_month=FIRST_MONTH, meter_read_day=METER_READ_DAY)
            before = (posts_dir / "2026-10.json").read_bytes()

            # 420日窓の外に出てmonths[]から消えた状態を模す。
            layers["months"] = []
            (data_dir / "layers.json").write_text(json.dumps(layers, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
            written = mr.run(data_dir, posts_dir, date(2028, 1, 1), first_report_billing_month=FIRST_MONTH, meter_read_day=METER_READ_DAY)
            after = (posts_dir / "2026-10.json").read_bytes()
            self.assertEqual(written, 0)
            self.assertEqual(before, after)

    def test_month_before_first_report_month_is_skipped(self):
        rec = make_month_record("2026-09", "2026-08-02", "2026-09-01")
        layers = {"months": [rec], "preliminary_months": []}
        daily_rows = make_daily_rows(date(2026, 8, 2), date(2026, 9, 1))
        with tempfile.TemporaryDirectory() as tmp:
            data_dir, posts_dir = self._write(tmp, layers, daily_rows)
            written = mr.run(data_dir, posts_dir, date(2026, 10, 1), first_report_billing_month=FIRST_MONTH, meter_read_day=METER_READ_DAY)
            self.assertEqual(written, 0)
            self.assertFalse((posts_dir / "2026-09.json").exists())

    # --- 追補(2026-09-26)A': 速報＋改訂 -------------------------------------------------

    def test_preliminary_generated_after_delay_when_not_yet_closable(self):
        rec = make_month_record("2026-10", "2026-09-02", "2026-10-01", l3_buy_source="sensor")
        layers = {"months": [], "preliminary_months": [rec]}
        daily_rows = make_daily_rows(date(2026, 9, 2), date(2026, 10, 1))
        with tempfile.TemporaryDirectory() as tmp:
            data_dir, posts_dir = self._write(tmp, layers, daily_rows)
            # 使用期間終了(2026-10-01)からPRELIMINARY_DELAY_DAYS(2)後。
            written = mr.run(data_dir, posts_dir, date(2026, 10, 3), first_report_billing_month=FIRST_MONTH, meter_read_day=METER_READ_DAY)
            self.assertEqual(written, 1)
            snapshot = json.loads((posts_dir / "2026-10.json").read_text(encoding="utf-8"))
            self.assertEqual(snapshot["stage"], "preliminary")
            self.assertEqual(snapshot["revision"], 1)
            self.assertEqual(snapshot["first_published"], "2026-10-03")

    def test_preliminary_not_generated_before_delay(self):
        rec = make_month_record("2026-10", "2026-09-02", "2026-10-01", l3_buy_source="sensor")
        layers = {"months": [], "preliminary_months": [rec]}
        daily_rows = make_daily_rows(date(2026, 9, 2), date(2026, 10, 1))
        with tempfile.TemporaryDirectory() as tmp:
            data_dir, posts_dir = self._write(tmp, layers, daily_rows)
            written = mr.run(data_dir, posts_dir, date(2026, 10, 2), first_report_billing_month=FIRST_MONTH, meter_read_day=METER_READ_DAY)
            self.assertEqual(written, 0)
            self.assertFalse((posts_dir / "2026-10.json").exists())

    def test_preliminary_is_frozen_even_if_source_data_changes(self):
        rec = make_month_record("2026-10", "2026-09-02", "2026-10-01", l3_buy_source="sensor", l3_net_fit=5000)
        layers = {"months": [], "preliminary_months": [rec]}
        daily_rows = make_daily_rows(date(2026, 9, 2), date(2026, 10, 1))
        with tempfile.TemporaryDirectory() as tmp:
            data_dir, posts_dir = self._write(tmp, layers, daily_rows)
            mr.run(data_dir, posts_dir, date(2026, 10, 3), first_report_billing_month=FIRST_MONTH, meter_read_day=METER_READ_DAY)
            before = (posts_dir / "2026-10.json").read_bytes()

            changed = make_month_record("2026-10", "2026-09-02", "2026-10-01", l3_buy_source="sensor", l3_net_fit=9999)
            layers["preliminary_months"] = [changed]
            (data_dir / "layers.json").write_text(json.dumps(layers, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
            written = mr.run(data_dir, posts_dir, date(2026, 10, 10), first_report_billing_month=FIRST_MONTH, meter_read_day=METER_READ_DAY)
            after = (posts_dir / "2026-10.json").read_bytes()
            self.assertEqual(written, 0)
            self.assertEqual(before, after)

    def test_preliminary_transitions_to_final_with_revision_bump(self):
        prelim = make_month_record("2026-10", "2026-09-02", "2026-10-01", l3_buy_source="sensor")
        layers = {"months": [], "preliminary_months": [prelim]}
        daily_rows = make_daily_rows(date(2026, 9, 2), date(2026, 10, 1))
        with tempfile.TemporaryDirectory() as tmp:
            data_dir, posts_dir = self._write(tmp, layers, daily_rows)
            mr.run(data_dir, posts_dir, date(2026, 10, 3), first_report_billing_month=FIRST_MONTH, meter_read_day=METER_READ_DAY)

            final = make_month_record("2026-10", "2026-09-02", "2026-10-01")  # billed/official_meterに確定
            layers = {"months": [final], "preliminary_months": []}
            (data_dir / "layers.json").write_text(json.dumps(layers, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
            mr.run(data_dir, posts_dir, date(2026, 10, 25), first_report_billing_month=FIRST_MONTH, meter_read_day=METER_READ_DAY)

            snapshot = json.loads((posts_dir / "2026-10.json").read_text(encoding="utf-8"))
            self.assertEqual(snapshot["stage"], "final")
            self.assertEqual(snapshot["revision"], 2)
            self.assertEqual(snapshot["first_published"], "2026-10-03")
            self.assertEqual(snapshot["revised"], "2026-10-25")

    def test_final_without_preceding_preliminary_has_revision_one(self):
        rec = make_month_record("2026-10", "2026-09-02", "2026-10-01")
        layers = {"months": [rec], "preliminary_months": []}
        daily_rows = make_daily_rows(date(2026, 9, 2), date(2026, 10, 1))
        with tempfile.TemporaryDirectory() as tmp:
            data_dir, posts_dir = self._write(tmp, layers, daily_rows)
            mr.run(data_dir, posts_dir, date(2026, 10, 24), first_report_billing_month=FIRST_MONTH, meter_read_day=METER_READ_DAY)
            snapshot = json.loads((posts_dir / "2026-10.json").read_text(encoding="utf-8"))
            self.assertEqual(snapshot["revision"], 1)
            self.assertEqual(snapshot["stage"], "final")

    # --- QA指摘2026-09-26 item6: 確定後の改版はlayer由来の項目だけで判定する -------------------

    def test_revision_unchanged_when_only_energy_weather_comparison_would_differ(self):
        # daily.jsonの420日窓が動く等でenergy/weather/comparisonの再計算結果が変わっても、
        # layers/l2_band/l3_source/stage/estimation/days_*が変わらなければ改版しない。
        rec = make_month_record("2026-10", "2026-09-02", "2026-10-01")
        layers = {"months": [rec], "preliminary_months": []}
        daily_rows = make_daily_rows(date(2026, 9, 2), date(2026, 10, 1))
        with tempfile.TemporaryDirectory() as tmp:
            data_dir, posts_dir = self._write(tmp, layers, daily_rows)
            mr.run(data_dir, posts_dir, date(2026, 10, 24), first_report_billing_month=FIRST_MONTH, meter_read_day=METER_READ_DAY)
            before = (posts_dir / "2026-10.json").read_bytes()

            # solar_kwhだけ変える(layersの数値には影響しない、energyだけ変わる想定の変更)。
            changed_daily = make_daily_rows(date(2026, 9, 2), date(2026, 10, 1), solar=999.0)
            (data_dir / "daily.json").write_text(json.dumps(changed_daily, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
            written = mr.run(data_dir, posts_dir, date(2026, 11, 1), first_report_billing_month=FIRST_MONTH, meter_read_day=METER_READ_DAY)
            after = (posts_dir / "2026-10.json").read_bytes()
            self.assertEqual(written, 0)
            self.assertEqual(before, after)

    def test_revision_bump_inherits_old_energy_weather_comparison(self):
        rec = make_month_record("2026-10", "2026-09-02", "2026-10-01", l3_net_fit=3000)
        layers = {"months": [rec], "preliminary_months": []}
        daily_rows = make_daily_rows(date(2026, 9, 2), date(2026, 10, 1), solar=20.0)
        with tempfile.TemporaryDirectory() as tmp:
            data_dir, posts_dir = self._write(tmp, layers, daily_rows)
            mr.run(data_dir, posts_dir, date(2026, 10, 24), first_report_billing_month=FIRST_MONTH, meter_read_day=METER_READ_DAY)
            original = json.loads((posts_dir / "2026-10.json").read_text(encoding="utf-8"))

            # layers(net_cost_fit_yen)とdaily(solar_kwh)を両方変える。
            corrected = make_month_record("2026-10", "2026-09-02", "2026-10-01", l3_net_fit=3111)
            layers["months"] = [corrected]
            (data_dir / "layers.json").write_text(json.dumps(layers, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
            changed_daily = make_daily_rows(date(2026, 9, 2), date(2026, 10, 1), solar=999.0)
            (data_dir / "daily.json").write_text(json.dumps(changed_daily, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

            mr.run(data_dir, posts_dir, date(2026, 11, 1), first_report_billing_month=FIRST_MONTH, meter_read_day=METER_READ_DAY)
            snapshot = json.loads((posts_dir / "2026-10.json").read_text(encoding="utf-8"))
            self.assertEqual(snapshot["revision"], 2)
            self.assertEqual(snapshot["layers"]["L3"]["net_cost_fit_yen"], 3111)  # layer由来は更新
            self.assertEqual(snapshot["energy"], original["energy"])  # energyは旧値を引き継ぐ
            self.assertEqual(snapshot["weather"], original["weather"])
            self.assertEqual(snapshot["comparison"], original["comparison"])

    def test_revision_limit_exceeded_raises(self):
        rec = make_month_record("2026-10", "2026-09-02", "2026-10-01", l3_net_fit=3000)
        layers = {"months": [rec], "preliminary_months": []}
        daily_rows = make_daily_rows(date(2026, 9, 2), date(2026, 10, 1))
        with tempfile.TemporaryDirectory() as tmp:
            data_dir, posts_dir = self._write(tmp, layers, daily_rows)
            mr.run(data_dir, posts_dir, date(2026, 10, 24), first_report_billing_month=FIRST_MONTH, meter_read_day=METER_READ_DAY)
            existing = json.loads((posts_dir / "2026-10.json").read_text(encoding="utf-8"))
            existing["revision"] = mr.MAX_REVISION  # 上限まで既に改版済みの状態を模す
            (posts_dir / "2026-10.json").write_text(json.dumps(existing, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

            corrected = make_month_record("2026-10", "2026-09-02", "2026-10-01", l3_net_fit=4444)
            layers["months"] = [corrected]
            (data_dir / "layers.json").write_text(json.dumps(layers, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
            with self.assertRaises(mr.RevisionLimitExceededError):
                mr.run(data_dir, posts_dir, date(2026, 11, 1), first_report_billing_month=FIRST_MONTH, meter_read_day=METER_READ_DAY)


if __name__ == "__main__":
    unittest.main()
