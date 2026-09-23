#!/usr/bin/env python3
"""calibrate_delta_model.py — delta_model.py のパラメータ（η・idle_w・tracking_margin_w・
capacity_factor）をオフラインで較正する。

設計根拠: 設計書（非公開） 20260923_l1s-scc-no-home-battery-ddr.md §3.3。

このスクリプトは公開リポジトリのコードだが、**入力は非公開データ**（5分プロファイルCSV・
SN を u1〜u4 の番号に置き換えたエクストラバッテリー込み日次サマリJSON）であり、リポジトリには
何も書き込まない。出力は標準出力への定数表・診断のみ。オーナーが確認し、layer_model.py の
較正済みデフォルト値へ出典コメント付きで手動で貼る運用（§3.3・§8 手順5）。

手順（§3.3）:
  1. load_share を台ごとの放電量比（Σdischarge_kwh）から求める。
  2. η（5候補）× idle_w（13候補）× tracking_margin_w（9候補）× capacity_factor（4候補）の
     2340通りをreplayで全探索する。目的関数は「日次AC入力の相対二乗誤差の平均 + 買電・売電
     合計の相対誤差 + 台ごとの日次SOC最小/最大のRMSE（/100で0〜1スケールに正規化）」の合計。
  3. 最良の組を「中央値」とする。L1S_REPLAY_TOLERANCE（layer_model.py と同じ定数）に合格した
     組の中でL1Sの電気代（期間合計のbuy/sellをtariff.jsonの直近確定単価で換算）が最小・最大に
     なる組を「楽観」「悲観」とする。
  4. 補助診断: 「接続中かつ買電中の時間」(central paramsのreplay: ac_in>50W かつ buy>0 の
     バケット数×dt_h)を実測(ac_in>50W かつ buy_w>0)と比較する（目安 ±25%）。

追加提案（Gemini 3.1 Pro、second-opinion。付記: §11参照）: 5分データを「パススルーのみ／
放電のみ／充電のみ」の3状態に分け、待機電力とη_c/η_dを最小二乗で独立推定し、格子探索の
初期値・妥当性確認に使う。ただしプロファイルはDELTA群の**合算値**（台ごとの内訳が無い）ため、
「アクティブ台数」は既知ではない。ここでは「その状態の間、4台全てが接続されている」という
単純化の下で1台あたりの値に換算する（診断値として明記し、格子探索の結果を上書きしない）。
"""
from __future__ import annotations

import argparse
import csv
import io
import json
import sys
from dataclasses import dataclass, replace as dataclass_replace
from datetime import date
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPT_DIR))
import bill_model  # noqa: E402
import delta_model as dm  # noqa: E402
import layer_model as lm  # noqa: E402

# --- 格子探索の候補（DDR §3.3の候補数と一致させる: 5×13×9×4 = 2340通り） -------------------
ETA_CANDIDATES: tuple[float, ...] = (0.86, 0.88, 0.90, 0.92, 0.94)
IDLE_CANDIDATES: tuple[float, ...] = tuple(float(w) for w in range(0, 65, 5))  # 0,5,...,60 (13)
MARGIN_CANDIDATES: tuple[float, ...] = tuple(float(w) for w in range(0, 450, 50))  # 0,50,...,400 (9)
CAPACITY_FACTOR_CANDIDATES: tuple[float, ...] = (0.85, 0.90, 0.95, 1.00)

UNIT_ORDER: tuple[str, ...] = ("u1", "u2", "u3", "u4")


def _canonical_unit_name(row: dict) -> str:
    """scratchの ecoflow_unit_daily.json は同容量2048Whの2台の並びが実際と逆順
    （u3=DELTA3_PLUS, u4=DELTA2_MAX_S）になっていることがある（コーディネーター訂正
    2026-09-24）。unit番号ではなく device_type + capacity_wh で
    delta_model.DEFAULT_DELTA_FLEET_PARAMS と同じ u1〜u4（容量降順、同容量内はDELTA2系を
    先に置く）へ正規化する。"""
    capacity = row["capacity_wh"]
    device_type = row["device_type"]
    if capacity == 6144:
        return "u1"
    if capacity == 4096:
        return "u2"
    if capacity == 2048 and device_type != "DELTA3_PLUS":
        return "u3"
    if capacity == 2048 and device_type == "DELTA3_PLUS":
        return "u4"
    raise ValueError(f"calibrate_delta_model.py: 未知の容量/機種の組み合わせです: capacity_wh={capacity} device_type={device_type!r}")


@dataclass
class UnitDailyRecord:
    date: str
    soc_start_pct: float
    soc_min_pct: float
    soc_max_pct: float
    discharge_kwh: float
    ac_input_kwh: float


def load_ecoflow_unit_daily(path: Path) -> dict[str, list[UnitDailyRecord]]:
    """DDR §3.3の入力2。SNをu1〜u4に置き換えた日次サマリJSON。unit名は
    _canonical_unit_name() で正規化する（ファイルの unit フィールドは信用しない）。"""
    rows = json.loads(path.read_text(encoding="utf-8"))
    by_unit: dict[str, list[UnitDailyRecord]] = {u: [] for u in UNIT_ORDER}
    for row in rows:
        unit = _canonical_unit_name(row)
        by_unit[unit].append(
            UnitDailyRecord(
                date=row["date"],
                soc_start_pct=float(row["soc_start_percent"]),
                soc_min_pct=float(row["soc_min_percent"]),
                soc_max_pct=float(row["soc_max_percent"]),
                discharge_kwh=float(row["discharge_kwh"]),
                ac_input_kwh=float(row["ac_input_kwh"]),
            )
        )
    for u in by_unit:
        by_unit[u].sort(key=lambda r: r.date)
    return by_unit


def compute_load_share(by_unit: dict[str, list[UnitDailyRecord]]) -> dict[str, float]:
    """DDR §3.3手順1: 台ごとの放電量比（Σdischarge_kwh）から load_share を求める。"""
    totals = {u: sum(r.discharge_kwh for r in recs) for u, recs in by_unit.items()}
    grand_total = sum(totals.values())
    if grand_total <= 0:
        raise ValueError("calibrate_delta_model.py: 全台の discharge_kwh 合計が0以下です（load_share を求められません）")
    return {u: totals[u] / grand_total for u in UNIT_ORDER}


def compute_capacity_weighted_soc_anchor(by_unit: dict[str, list[UnitDailyRecord]], nominal_wh: dict[str, float]) -> dict[str, float]:
    """本番の metrics-export.sh ecoflow_daily クエリと同じ式（公称容量で加重した合計SOC）で
    日次アンカーを作る。較正はこの「本番と同じ入力形」で行うことで、実運用時の replay
    ゲート挙動と較正結果がずれないようにする。"""
    by_date: dict[str, list[tuple[float, float]]] = {}
    for u, recs in by_unit.items():
        for r in recs:
            by_date.setdefault(r.date, []).append((r.soc_start_pct, nominal_wh[u]))
    result = {}
    for d, pairs in by_date.items():
        total_cap = sum(cap for _pct, cap in pairs)
        if total_cap <= 0:
            continue
        result[d] = sum(pct * cap for pct, cap in pairs) / total_cap
    return result


def load_profile_buckets(path: Path, since: str | None = None) -> dict[str, list]:
    """5分プロファイルCSVを読み、resolve_day_buckets で usable な日だけを返す
    （real-world データは初期の欠測チャネルを含むため、layer_model.py と同じゲートを通す）。"""
    text = path.read_text(encoding="utf-8")
    rows = list(csv.DictReader(io.StringIO(text)))
    buckets = lm.parse_profile_rows(rows)
    by_date = lm.group_by_date(buckets)
    usable: dict[str, list] = {}
    for d_str, day_buckets in by_date.items():
        if since is not None and d_str < since:
            continue
        resolution = lm.resolve_day_buckets(day_buckets, date.fromisoformat(d_str))
        if resolution.buckets is not None:
            usable[d_str] = resolution.buckets
    return usable


@dataclass
class ReplayCollection:
    sim_ac_in_kwh: float
    sim_buy_kwh: float
    sim_sell_kwh: float
    meas_ac_in_kwh: float
    meas_buy_kwh: float
    meas_sell_kwh: float
    days: int
    # (unit_name, date) -> (sim_soc_min_pct, sim_soc_max_pct)
    per_unit_day_soc: dict[tuple[str, str], tuple[float, float]]
    connected_and_buying_hours: float
    missing_anchor: bool
    daily_sim_ac_in_kwh: dict[str, float]


def replay_and_collect(
    ordered_buckets: list, params: dm.DeltaFleetParams, soc_anchor_by_date: dict[str, float]
) -> ReplayCollection:
    """_simulate_delta_series（replayモード）と同じ恒等式・アンカー規則を使いつつ、
    較正の目的関数に必要な台ごとの日次SOC最小/最大と「接続中かつ買電中の時間」も同時に
    集計する（layer_model.DeltaSeriesResult はこの粒度を公開しないため、較正専用に
    ここで独自に走らせる。DDR §3.3診断項目・目的関数のSOC RMSE項に使う）。"""
    dt_h = lm.BUCKET_MINUTES / 60.0
    states: list[dm.UnitState] | None = None
    prev_bucket_at: str | None = None
    sim_ac_in_wh = sim_buy_wh = sim_sell_wh = 0.0
    meas_ac_in_wh = meas_buy_wh = meas_sell_wh = 0.0
    connected_and_buying_h = 0.0
    per_unit_day_soc: dict[tuple[str, str], tuple[float, float]] = {}
    daily_sim_ac_in_wh: dict[str, float] = {}
    days_seen: set[str] = set()

    for b in ordered_buckets:
        need_anchor = states is None or (
            prev_bucket_at is not None and b.bucket_at != lm._next_bucket_at(prev_bucket_at, lm.BUCKET_MINUTES)
        )
        if need_anchor:
            anchor = soc_anchor_by_date.get(b.date_str)
            if anchor is None:
                return ReplayCollection(0, 0, 0, 0, 0, 0, 0, {}, 0.0, True, {})
            states = dm.init_states(params, anchor)

        load_w = b.load_true_w()
        delta_load_w = max(0.0, b.eco_ac_out_w or 0.0)
        house_load_w = load_w - delta_load_w
        pv_w = b.solar_w + b.nichicon_pv_w - b.nichicon_battery_w
        surplus_w = pv_w - house_load_w

        step = dm.fleet_step(states, params, surplus_w, delta_load_w, dt_h)
        grid_w = house_load_w + step.ac_in_w + step.unserved_w - pv_w
        buy_w = max(0.0, grid_w)
        sell_w = max(0.0, -grid_w)

        sim_ac_in_wh += step.ac_in_w * dt_h
        sim_buy_wh += buy_w * dt_h
        sim_sell_wh += sell_w * dt_h
        meas_ac_in_wh += (b.eco_ac_in_w or 0.0) * dt_h
        meas_buy_wh += (b.buy_w or 0.0) * dt_h
        meas_sell_wh += (b.sell_w or 0.0) * dt_h
        daily_sim_ac_in_wh[b.date_str] = daily_sim_ac_in_wh.get(b.date_str, 0.0) + step.ac_in_w * dt_h
        # 診断用: 「接続中(ac_in>50W)かつ買電中(buy>0)」の延べ時間は、シミュレーション側の
        # 値(step.ac_in_w/buy_w)で数える（バグ修正2026-09-24: 以前はここで誤って実測フィールド
        # b.eco_ac_in_w/b.buy_w を使っていたため、run_calibration側の実測診断値と常に一致する
        # トートロジーになっていた。実測側は run_calibration が ordered_buckets から独立に集計する）。
        if step.ac_in_w > 50.0 and buy_w > 0.0:
            connected_and_buying_h += dt_h

        for i, u in enumerate(params.units):
            key = (u.name, b.date_str)
            soc = states[i].soc_pct
            if key not in per_unit_day_soc:
                per_unit_day_soc[key] = (soc, soc)
            else:
                lo, hi = per_unit_day_soc[key]
                per_unit_day_soc[key] = (min(lo, soc), max(hi, soc))

        days_seen.add(b.date_str)
        prev_bucket_at = b.bucket_at

    return ReplayCollection(
        sim_ac_in_kwh=sim_ac_in_wh / 1000.0,
        sim_buy_kwh=sim_buy_wh / 1000.0,
        sim_sell_kwh=sim_sell_wh / 1000.0,
        meas_ac_in_kwh=meas_ac_in_wh / 1000.0,
        meas_buy_kwh=meas_buy_wh / 1000.0,
        meas_sell_kwh=meas_sell_wh / 1000.0,
        days=len(days_seen),
        per_unit_day_soc=per_unit_day_soc,
        connected_and_buying_hours=connected_and_buying_h,
        missing_anchor=False,
        daily_sim_ac_in_kwh={d: wh / 1000.0 for d, wh in daily_sim_ac_in_wh.items()},
    )


def _daily_ac_in_by_date(ordered_buckets: list) -> dict[str, float]:
    """日次の実測AC入力kWh（目的関数の「日次AC入力の相対二乗誤差」に使う）。"""
    dt_h = lm.BUCKET_MINUTES / 60.0
    out: dict[str, float] = {}
    for b in ordered_buckets:
        out[b.date_str] = out.get(b.date_str, 0.0) + (b.eco_ac_in_w or 0.0) * dt_h / 1000.0
    return out


def objective(
    ordered_buckets: list,
    soc_anchor_by_date: dict[str, float],
    meas_ac_in_by_date: dict[str, float],
    unit_daily: dict[str, list[UnitDailyRecord]],
    params: dm.DeltaFleetParams,
) -> tuple[float, ReplayCollection] | None:
    """DDR §3.3手順2の目的関数。戻り値は (score, ReplayCollection) または None（missing_anchor）。
    小さいほど良い。"""
    coll = replay_and_collect(ordered_buckets, params, soc_anchor_by_date)
    if coll.missing_anchor:
        return None

    rel_sq_errs = []
    for d, meas in meas_ac_in_by_date.items():
        sim = coll.daily_sim_ac_in_kwh.get(d, 0.0)
        denom = max(meas, 0.1)
        rel_sq_errs.append(((sim - meas) / denom) ** 2)
    ac_in_term = sum(rel_sq_errs) / len(rel_sq_errs) if rel_sq_errs else 0.0

    buy_term = abs(coll.sim_buy_kwh - coll.meas_buy_kwh) / max(coll.meas_buy_kwh, 1.0)
    sell_term = abs(coll.sim_sell_kwh - coll.meas_sell_kwh) / max(coll.meas_sell_kwh, 1.0)

    sq_diffs = []
    for u, recs in unit_daily.items():
        for r in recs:
            key = (u, r.date)
            if key not in coll.per_unit_day_soc:
                continue
            sim_min, sim_max = coll.per_unit_day_soc[key]
            sq_diffs.append((sim_min - r.soc_min_pct) ** 2)
            sq_diffs.append((sim_max - r.soc_max_pct) ** 2)
    soc_rmse = (sum(sq_diffs) / len(sq_diffs)) ** 0.5 if sq_diffs else 0.0

    score = ac_in_term + buy_term + sell_term + soc_rmse / 100.0
    return score, coll


def build_params(
    units_spec: tuple[tuple[str, str, float], ...],
    load_share: dict[str, float],
    eta: float,
    idle_w: float,
    margin_w: float,
    capacity_factor: float,
) -> dm.DeltaFleetParams:
    units = tuple(
        dm.DeltaUnitSpec(
            name=name, kind=kind, capacity_wh=nominal_wh * capacity_factor,
            load_share=load_share[name], max_charge_w=lm._DELTA_MODEL_MAX_CHARGE_W,
        )
        for name, kind, nominal_wh in units_spec
    )
    return dm.DeltaFleetParams(units=units, charge_efficiency=eta, discharge_efficiency=eta, idle_w=idle_w, tracking_margin_w=margin_w)


def run_calibration(profile_path: Path, ecoflow_unit_daily_path: Path, since: str | None = None) -> dict:
    """較正の全体フロー。CLIからも unittest（合成データ）からも呼べるようにする。
    戻り値は標準出力用の結果一式（辞書。ファイルには書かない）。"""
    unit_daily = load_ecoflow_unit_daily(ecoflow_unit_daily_path)
    load_share = compute_load_share(unit_daily)
    nominal_wh = {u: lm.DELTA_UNIT_NOMINAL_WH[u] for u in UNIT_ORDER}
    soc_anchor_by_date = compute_capacity_weighted_soc_anchor(unit_daily, nominal_wh)

    usable_by_date = load_profile_buckets(profile_path, since=since)
    # ecoflow側にアンカーがある日だけを使う（両方揃わないと較正できない）。
    usable_dates = sorted(d for d in usable_by_date if d in soc_anchor_by_date)
    if not usable_dates:
        raise ValueError("calibrate_delta_model.py: profileとecoflow_unit_dailyの両方でusableな日がありません")
    ordered_buckets: list = []
    for d in usable_dates:
        ordered_buckets.extend(usable_by_date[d])
    ordered_buckets.sort(key=lambda b: b.bucket_at)

    meas_ac_in_by_date = _daily_ac_in_by_date(ordered_buckets)

    units_spec = tuple(
        (name, "delta3" if name == "u4" else "delta2", nominal_wh[name]) for name in UNIT_ORDER
    )

    best_score = float("inf")
    best_params: dm.DeltaFleetParams | None = None
    best_coll: ReplayCollection | None = None
    gate_passing: list[tuple[dm.DeltaFleetParams, ReplayCollection]] = []
    evaluated = 0

    for eta in ETA_CANDIDATES:
        for idle_w in IDLE_CANDIDATES:
            for margin_w in MARGIN_CANDIDATES:
                for capacity_factor in CAPACITY_FACTOR_CANDIDATES:
                    params = build_params(units_spec, load_share, eta, idle_w, margin_w, capacity_factor)
                    result = objective(ordered_buckets, soc_anchor_by_date, meas_ac_in_by_date, unit_daily, params)
                    evaluated += 1
                    if result is None:
                        continue
                    score, coll = result
                    if score < best_score:
                        best_score = score
                        best_params = params
                        best_coll = coll
                    l1s_replay, gate_ok = lm._l1s_replay_check(
                        lm.DeltaSeriesResult(
                            daily_kwh={}, max_export_w=0.0, boundary_delta_kwh=0.0,
                            sim_ac_in_kwh=coll.sim_ac_in_kwh, sim_buy_kwh=coll.sim_buy_kwh, sim_sell_kwh=coll.sim_sell_kwh,
                            meas_ac_in_kwh=coll.meas_ac_in_kwh, meas_buy_kwh=coll.meas_buy_kwh, meas_sell_kwh=coll.meas_sell_kwh,
                            unserved_kwh=0.0, days=coll.days, missing_anchor=False,
                        )
                    )
                    if gate_ok:
                        gate_passing.append((params, coll))

    if best_params is None or best_coll is None:
        raise ValueError("calibrate_delta_model.py: 有効な組み合わせが1つもありませんでした（アンカー不足の疑い）")

    # 楽観/悲観: ゲート合格した組の中でL1S電気代（期間合計をtariff.jsonの直近確定単価で近似）が
    # 最小・最大になる組。tariff.json は既存のものを再利用する（二重実装しない）。
    tariff = json.loads((SCRIPT_DIR / "tariff.json").read_text(encoding="utf-8"))
    billing_months = sorted(
        {bill_model.billing_month_for_date(date.fromisoformat(d), tariff["meter_read_day"]) for d in usable_dates}
    )
    pricing_month = billing_months[-1] if billing_months else None

    def cost_of(coll: ReplayCollection) -> float | None:
        if pricing_month is None:
            return None
        try:
            buy_price, sell_fit, _sell_post, _prov, _src = bill_model.per_kwh_prices(tariff, pricing_month)
        except KeyError:
            return None
        return coll.sim_buy_kwh * buy_price - coll.sim_sell_kwh * sell_fit

    optimistic = pessimistic = None
    if gate_passing:
        costed = [(cost_of(coll), params, coll) for params, coll in gate_passing]
        costed = [c for c in costed if c[0] is not None]
        if costed:
            optimistic = min(costed, key=lambda c: c[0])
            pessimistic = max(costed, key=lambda c: c[0])

    # 診断: 接続中かつ買電中の時間（central params）を実測と比較する。
    diagnostic_hours_sim = best_coll.connected_and_buying_hours
    dt_h = lm.BUCKET_MINUTES / 60.0
    diagnostic_hours_meas = sum(
        dt_h for b in ordered_buckets if (b.eco_ac_in_w or 0.0) > 50.0 and (b.buy_w or 0.0) > 0.0
    )

    return {
        "usable_days": len(usable_dates),
        "usable_date_range": (usable_dates[0], usable_dates[-1]),
        "evaluated_combos": evaluated,
        "load_share": load_share,
        "central": {
            "score": best_score,
            "charge_efficiency": best_params.charge_efficiency,
            "discharge_efficiency": best_params.discharge_efficiency,
            "idle_w": best_params.idle_w,
            "tracking_margin_w": best_params.tracking_margin_w,
            "capacity_factor": best_params.units[0].capacity_wh / nominal_wh["u1"],
            "sim_ac_in_kwh": best_coll.sim_ac_in_kwh, "meas_ac_in_kwh": best_coll.meas_ac_in_kwh,
            "sim_buy_kwh": best_coll.sim_buy_kwh, "meas_buy_kwh": best_coll.meas_buy_kwh,
            "sim_sell_kwh": best_coll.sim_sell_kwh, "meas_sell_kwh": best_coll.meas_sell_kwh,
        },
        "optimistic": None if optimistic is None else {
            "cost_yen": optimistic[0], "charge_efficiency": optimistic[1].charge_efficiency,
            "idle_w": optimistic[1].idle_w, "tracking_margin_w": optimistic[1].tracking_margin_w,
            "capacity_factor": optimistic[1].units[0].capacity_wh / nominal_wh["u1"],
        },
        "pessimistic": None if pessimistic is None else {
            "cost_yen": pessimistic[0], "charge_efficiency": pessimistic[1].charge_efficiency,
            "idle_w": pessimistic[1].idle_w, "tracking_margin_w": pessimistic[1].tracking_margin_w,
            "capacity_factor": pessimistic[1].units[0].capacity_wh / nominal_wh["u1"],
        },
        "gate_passing_count": len(gate_passing),
        "diagnostic_connected_and_buying_hours": {
            "sim": diagnostic_hours_sim, "meas": diagnostic_hours_meas,
            "within_25pct": abs(diagnostic_hours_sim - diagnostic_hours_meas) <= 0.25 * max(diagnostic_hours_meas, 1.0),
        },
    }


# ---------------------------------------------------------------------------
# Gemini提案の3状態最小二乗（診断・初期値の妥当性確認専用。格子探索の結果を上書きしない）
# ---------------------------------------------------------------------------
def three_state_diagnostic(ordered_buckets: list, unit_count: int = 4) -> dict:
    """パススルーのみ／放電のみ／充電のみの3状態にバケットを分け、1台あたりの待機電力と
    充放電効率を単純化した仮定（その状態の間、unit_count台すべてが接続されている）の下で
    最小二乗的に推定する。プロファイルはDELTA群の合算値なので「アクティブ台数」は真の意味では
    不明であり、ここでの1台あたり換算は診断値に過ぎない（DDR §3.3付記、Gemini 3.1 Pro提案）。
    """
    passthrough_diffs = []  # ac_in - ac_out（パススルーのみ状態）
    charge_pairs = []  # (ac_in - ac_out, 推定bms_in相当) は求められないため、chargeのAC投入量のみ収集
    discharge_pairs = []  # ac_out（放電のみ状態、ac_in==0）
    for b in ordered_buckets:
        ac_in = b.eco_ac_in_w or 0.0
        ac_out = b.eco_ac_out_w or 0.0
        if ac_in <= 1.0 and ac_out > 20.0:
            discharge_pairs.append(ac_out)
        elif ac_in > 20.0 and ac_out > 20.0 and ac_in <= ac_out * 1.15:
            passthrough_diffs.append(ac_in - ac_out)
        elif ac_in > ac_out * 1.3 and ac_in > 100.0:
            charge_pairs.append(ac_in - ac_out)

    idle_total_passthrough_w = (sum(passthrough_diffs) / len(passthrough_diffs)) if passthrough_diffs else None
    idle_per_unit_w = (idle_total_passthrough_w / unit_count) if idle_total_passthrough_w is not None else None
    avg_charge_headroom_w = (sum(charge_pairs) / len(charge_pairs)) if charge_pairs else None
    avg_discharge_out_w = (sum(discharge_pairs) / len(discharge_pairs)) if discharge_pairs else None

    return {
        "passthrough_buckets": len(passthrough_diffs),
        "charge_buckets": len(charge_pairs),
        "discharge_buckets": len(discharge_pairs),
        "idle_total_w_during_passthrough": idle_total_passthrough_w,
        "idle_w_per_unit_assuming_all_connected": idle_per_unit_w,
        "avg_charge_headroom_w": avg_charge_headroom_w,
        "avg_discharge_out_w": avg_discharge_out_w,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--profile", type=Path, required=True, help="5分プロファイルCSV（非公開データ）のパス")
    parser.add_argument("--ecoflow-unit-daily", type=Path, required=True, help="SN→u1..u4置換済み日次サマリJSON（非公開データ）のパス")
    parser.add_argument("--since", type=str, default=None, help="この日付以降のプロファイルのみ使う(YYYY-MM-DD)")
    args = parser.parse_args()

    result = run_calibration(args.profile, args.ecoflow_unit_daily, since=args.since)

    usable_by_date = load_profile_buckets(args.profile, since=args.since)
    diag_buckets: list = []
    for d in sorted(usable_by_date):
        diag_buckets.extend(usable_by_date[d])
    diag = three_state_diagnostic(diag_buckets)

    print(json.dumps(result, ensure_ascii=False, indent=2))
    print("--- 3状態診断（初期値・妥当性確認専用、格子探索を上書きしない） ---")
    print(json.dumps(diag, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
