#!/usr/bin/env python3
"""generate_layer_bill_fixtures.py — testdata/{meta,bills,layers}_fixture.json を再生成する。

layer_model.py/bill_model.py を「合成プロファイル」（実測値・時間帯粒度を一切含まない、
test_layer_model.py の GoldenDaySyntheticTest と同じ12アンカー点の線形補間パターン）で
実走させ、その出力をそのまま testdata/ に保存する。手で書いた辞書ではなく実際にスクリプトを
実行して生成することで、layer_model.py/bill_model.py の出力スキーマが将来変わっても
（QA指摘#1のような）キーallowlistの手動収穫漏れが起きにくくなる。

対象の請求月は 2026-08（使用期間 2026-07-02〜2026-08-01、tariff.json で確定単価済み）を
全日usable(coverage=100%、"full")にして、L0〜L3すべてavailableな確定月を1つ作る
（cumulative や uncertainty 等、正常系でしか出ないキーも含めるため）。

加えて 2026-09（使用期間 2026-08-02〜2026-09-01、tariff.json では単価が未確定）も全日usableに
し、layers.preliminary_months[] に1件入れる（設計判断2026-09-23/26「速報＋改訂」方式）。
G4のキーallowlist収穫元と、ダッシュボードの「速報」バッジ描画確認の両方に使う。

使い方:
  cd scripts/blog-metrics && python3 testdata/generate_layer_bill_fixtures.py

実行後、testdata/{meta,bills,layers}_fixture.json の差分を確認してからコミットすること。
"""
from __future__ import annotations

import csv
import io
import json
import subprocess
import sys
from datetime import date, datetime, timedelta
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent.parent  # scripts/blog-metrics
TESTDATA_DIR = SCRIPT_DIR / "testdata"
sys.path.insert(0, str(SCRIPT_DIR))

import build_daily  # noqa: E402  buy_sell_price_sourceの文言を二重実装しないため再利用する
import layer_model as lm  # noqa: E402  DEFAULT_DELTA_FLEET_PARAMS を再利用する
import test_layer_model as tlm  # noqa: E402  合成ゴールデンデイ生成関数を再利用する

L1S_ANCHOR_SOC_PCT = 50.0  # 較正前の初期SOC（テスト用固定値、実測値ではない）

BILLING_MONTH = "2026-08"
START = date(2026, 7, 2)
END = date(2026, 8, 1)
# 2026-09（tariff.json未確定）: preliminary_months[]を1件作るための第2期間。
PRELIMINARY_BILLING_MONTH = "2026-09"
PRELIMINARY_START = date(2026, 8, 2)
PRELIMINARY_END = date(2026, 9, 1)
TODAY_FOR_GENERATION = "2026-09-15"  # in_progress判定・速報の確定判定に使う基準日（テスト用に固定）

_PROFILE_FIELDS = (
    "bucket_at", "solar_w", "buy_w", "sell_w", "nichicon_pv_w",
    "nichicon_battery_w", "nichicon_soc", "eco_ac_in_w", "eco_ac_out_w",
)


def build_profile_and_ecoflow_daily() -> tuple[str, list[dict]]:
    """START〜ENDの全日を、合成ゴールデンデイパターン（実測値を含まない）をベースに、
    layer_model.DEFAULT_DELTA_FLEET_PARAMS でreplayモードのDELTA群シミュレーションを通して
    eco_ac_in_w/buy_w/sell_wを上書きしたCSVを作る（DDR §5.8）。合成golden dayのままだと
    L1S replayゲートが不合格になりL1Sがunavailableのままとなって、G4のallowlistがL1S
    availableなキーを一度も収穫できない（QA指摘#1と同型の見落とし経路）。

    PRELIMINARY_START〜PRELIMINARY_ENDは素の合成golden day(build_synthetic_golden_day_buckets)
    をそのまま使う（L1S replay一致は要求しない。preliminary_months[]の例はL0〜L3が
    availableであれば十分で、L1Sは他の確定月同様unavailableのままでよい）。

    戻り値は (profile CSV文字列, ecoflow_daily.json相当の[{date, soc_start_pct}, ...])。"""
    buckets, soc_by_date = tlm.build_self_consistent_golden_period(
        START.isoformat(), END.isoformat(), lm.DEFAULT_DELTA_FLEET_PARAMS, L1S_ANCHOR_SOC_PCT
    )
    d = PRELIMINARY_START
    while d <= PRELIMINARY_END:
        buckets = buckets + tlm.build_synthetic_golden_day_buckets(day=d.isoformat())
        d += timedelta(days=1)
    buf = io.StringIO()
    writer = csv.DictWriter(buf, fieldnames=_PROFILE_FIELDS)
    writer.writeheader()
    for bucket in buckets:
        writer.writerow({f: getattr(bucket, f) for f in _PROFILE_FIELDS})
    ecoflow_daily_rows = [{"date": d, "soc_start_pct": pct} for d, pct in sorted(soc_by_date.items())]
    return buf.getvalue(), ecoflow_daily_rows


def build_daily_json(tmp_dir: Path) -> Path:
    """L3(実測)源となる daily.json。値は集計値のみのプレースホルダ（実測値ではない）。
    START〜ENDとPRELIMINARY_START〜PRELIMINARY_ENDの両期間を含める。"""
    rows = []
    for period_start, period_end in ((START, END), (PRELIMINARY_START, PRELIMINARY_END)):
        d = period_start
        while d <= period_end:
            rows.append({
                "date": d.isoformat(),
                "solar_kwh": 20.0,
                "buy_kwh": 1.0,
                "sell_kwh": 5.0,
                "nichicon_charge_kwh": 3.0,
                "ecoflow_charge_kwh": 1.0,
                "self_consumption_shift_kwh": 4.0,
                "saving_yen": 300,
            })
            d += timedelta(days=1)
    path = tmp_dir / "daily.json"
    path.write_text(json.dumps(rows, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return path


def main() -> None:
    import tempfile

    with tempfile.TemporaryDirectory() as tmp:
        tmp_dir = Path(tmp)
        profile_csv, ecoflow_daily_rows = build_profile_and_ecoflow_daily()
        daily_path = build_daily_json(tmp_dir)
        tariff_path = SCRIPT_DIR / "tariff.json"
        ecoflow_daily_path = tmp_dir / "ecoflow_daily.json"
        ecoflow_daily_path.write_text(
            json.dumps(ecoflow_daily_rows, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
        )

        layers_out = tmp_dir / "layers.json"
        daily_load_out = tmp_dir / "daily_load.json"
        subprocess.run(
            [sys.executable, str(SCRIPT_DIR / "layer_model.py"),
             "--tariff", str(tariff_path),
             "--daily", str(daily_path),
             "--out", str(layers_out),
             "--daily-load-out", str(daily_load_out),
             "--today", TODAY_FOR_GENERATION,
             "--profile-source", "synthetic_test_fixture",
             "--ecoflow-daily", str(ecoflow_daily_path)],
            input=profile_csv, text=True, check=True,
        )

        bills_out = tmp_dir / "bills.json"
        subprocess.run(
            [sys.executable, str(SCRIPT_DIR / "bill_model.py"),
             "--tariff", str(tariff_path),
             "--daily", str(daily_path),
             "--daily-load", str(daily_load_out),
             "--out", str(bills_out)],
            check=True,
        )

        layers = json.loads(layers_out.read_text(encoding="utf-8"))
        bills = json.loads(bills_out.read_text(encoding="utf-8"))

    meta = {
        "generated_at": "2026-09-15 12:00:00",
        "buy_price_yen_per_kwh": 41.56,
        "sell_price_yen_per_kwh": 16.0,
        "buy_sell_price_effective_month": BILLING_MONTH,
        "buy_sell_price_source": build_daily.BUY_SELL_PRICE_SOURCE,
        "ecoflow_data_since": START.isoformat(),
        "nichicon_data_since": START.isoformat(),
        "power_history_since": START.isoformat(),
        "publish_since": START.isoformat(),
    }

    (TESTDATA_DIR / "layers_fixture.json").write_text(json.dumps(layers, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    (TESTDATA_DIR / "bills_fixture.json").write_text(json.dumps(bills, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    (TESTDATA_DIR / "meta_fixture.json").write_text(json.dumps(meta, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    full_months = [m for m in layers["months"] if all(m["layers"][l]["available"] for l in ("L0", "L1", "L2", "L3"))]
    print(
        f"wrote testdata/layers_fixture.json ({len(layers['months'])} months, {len(full_months)} available, "
        f"{len(layers.get('preliminary_months', []))} preliminary)"
    )
    print(f"wrote testdata/bills_fixture.json ({len(bills['months'])} months)")
    print("wrote testdata/meta_fixture.json")


if __name__ == "__main__":
    main()
