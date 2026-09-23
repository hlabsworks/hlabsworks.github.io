#!/usr/bin/env python3
"""delta_model.py — SCC（SolarChargeController）配下の DELTA 4 台を規則ベースで
シミュレートする、入出力を持たない純粋な計算モジュール（stdlib のみ）。

設計根拠: 設計書（非公開） 20260923_l1s-scc-no-home-battery-ddr.md §2・§3。
本ファイルのコメントで参照する「§」はこの DDR の節番号。

layer_model.py（L1S の系列シミュレーション）と calibrate_delta_model.py（オフライン較正）の
両方から import される。numpy/scipy 等の新規依存は使わない（旧 DDR の却下案と同じ理由。
格子探索で当てはめが足りるため）。

fleet_step() は §3.1 の 11 ステップをこの順序で固定して実装する。台ごとの状態（SOC・
ラッチ）は呼び出し側が UnitState のリストとして保持し、fleet_step が in-place で更新する。
"""
from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(frozen=True)
class DeltaUnitSpec:
    """DELTA 1台分の仕様（DDR §2.2）。S/N は持たない（台ごとの匿名化、DDR §5.6）。"""

    name: str  # "u1".."u4"
    kind: str  # "delta2" | "delta3"
    capacity_wh: float  # SOC100%あたりの実効Wh（= 公称 × capacity_factor）
    load_share: float  # delta_load_w の按分比（全台の合計 1.0）
    max_charge_w: float  # SCC max_charging_speed_w


@dataclass(frozen=True)
class DeltaFleetParams:
    """DELTA 群のパラメータ（DDR §2.2・§2.3）。既定値は初期推定であり、較正で置き換える
    （calibrate_delta_model.py の出力を出典コメント付きで貼る運用）。"""

    units: tuple[DeltaUnitSpec, ...]
    charge_efficiency: float  # AC入力→BMS
    discharge_efficiency: float  # BMS→AC出力
    idle_w: float  # 1台あたり常時の待機消費（USB込み、AC換算）
    passthrough_drain_w: float = 3.0  # 接続中にBMSから減る分（domain rules「−3W前後」）
    tracking_margin_w: float = 200.0  # SCCが吸いきれず残る売電の平均（較正で決める）
    speedup_threshold_w: float = 400.0
    full_on_pct: float = 95.0
    full_off_pct: float = 92.0
    emergency_on_pct: float = 10.0
    emergency_off_pct: float = 20.0
    lifeline_on_pct: float = 7.0
    lifeline_off_pct: float = 9.0
    lifeline_w: float = 100.0
    delta3_min_charge_w: float = 100.0
    plug_input_limit_w: float = 1450.0
    safe_on_load_w_delta2: float = 1350.0
    safe_on_load_w_delta3: float = 1250.0


@dataclass
class UnitState:
    soc_pct: float
    plugged: bool = False
    full: bool = False
    emergency: bool = False
    lifeline: bool = False


@dataclass
class FleetStepResult:
    ac_in_w: float
    unserved_w: float
    plugged_units: int
    bms_in_w: float
    bms_out_w: float


def init_states(params: DeltaFleetParams, soc_pct: float) -> list[UnitState]:
    """全台を同じ SOC で初期化する（DDR §2.3「全台に同じ値を入れる」）。ラッチは
    fleet_step の1回目の呼び出しで soc_pct から再計算されるため、ここでは False のまま返す。"""
    return [UnitState(soc_pct=soc_pct) for _ in params.units]


def _safe_on_load_w(kind: str, params: DeltaFleetParams) -> float:
    return params.safe_on_load_w_delta2 if kind == "delta2" else params.safe_on_load_w_delta3


def fleet_step(
    states: list[UnitState], params: DeltaFleetParams, surplus_w: float, delta_load_w: float, dt_h: float
) -> FleetStepResult:
    """§3.1 の1バケット分の計算。states を in-place で更新する。"""
    units = params.units
    n = len(units)

    # 1. 各台の按分負荷
    load = [delta_load_w * units[i].load_share for i in range(n)]

    # 2. バケット開始時のSOCでラッチを更新（ヒステリシス）。
    for i in range(n):
        st = states[i]
        if st.soc_pct >= params.full_on_pct:
            st.full = True
        elif st.soc_pct < params.full_off_pct:
            st.full = False
        if st.soc_pct <= params.emergency_on_pct:
            st.emergency = True
        elif st.soc_pct >= params.emergency_off_pct:
            st.emergency = False
        if units[i].kind == "delta2":
            if st.emergency and st.soc_pct <= params.lifeline_on_pct:
                st.lifeline = True
            elif (not st.emergency) or st.soc_pct >= params.lifeline_off_pct:
                st.lifeline = False
        else:
            st.lifeline = False

    # 3. emergency の台は強制接続。
    for i in range(n):
        if states[i].emergency:
            states[i].plugged = True

    # 4. 接続中の最低充電cmin_iとbase_i。
    cmin = [0.0] * n
    for i in range(n):
        if units[i].kind == "delta3":
            cmin[i] = params.delta3_min_charge_w if states[i].soc_pct < 100.0 else 0.0
        else:
            cmin[i] = params.lifeline_w if states[i].lifeline else 0.0
    base = [load[i] + params.idle_w + cmin[i] for i in range(n)]

    # 5. 過負荷退避（emergencyより優先）。
    for i in range(n):
        if states[i].plugged and base[i] > params.plug_input_limit_w:
            states[i].plugged = False

    # 6. 残り余剰。
    avail = surplus_w - sum(base[i] for i in range(n) if states[i].plugged)

    # 7. 買電中の遮断: emergencyでない接続中の台をbaseの大きい順に切断。
    if avail < 0:
        candidates = sorted(
            (i for i in range(n) if states[i].plugged and not states[i].emergency), key=lambda i: -base[i]
        )
        for i in candidates:
            if avail >= 0:
                break
            states[i].plugged = False
            avail += base[i]

    # 8. スピードアップで接続: SOCが最も低い1台ずつ。
    while avail >= params.speedup_threshold_w:
        candidates = [
            i
            for i in range(n)
            if not states[i].plugged
            and not states[i].full
            and (load[i] + params.idle_w) < _safe_on_load_w(units[i].kind, params)
            and base[i] <= avail
        ]
        if not candidates:
            break
        i = min(candidates, key=lambda i: states[i].soc_pct)
        states[i].plugged = True
        avail -= base[i]

    # 9. 充電の配分: SOCの低い順に、接続中で満充電でない台にpoolを配る。
    pool = max(0.0, avail - params.tracking_margin_w)
    extra = [0.0] * n  # x_i（cminに上乗せする分）
    eligible = sorted(
        (i for i in range(n) if states[i].plugged and not states[i].full), key=lambda i: states[i].soc_pct
    )
    for i in eligible:
        if pool <= 0.0:
            break
        u = units[i]
        cap1 = max(0.0, u.max_charge_w - cmin[i])
        cap2 = max(0.0, params.plug_input_limit_w - base[i])
        headroom_wh = max(0.0, (100.0 - states[i].soc_pct) / 100.0 * u.capacity_wh)
        cmin_energy_wh = cmin[i] * params.charge_efficiency * dt_h
        remaining_headroom_wh = max(0.0, headroom_wh - cmin_energy_wh)
        cap3 = (
            remaining_headroom_wh / dt_h / params.charge_efficiency
            if dt_h > 0.0 and params.charge_efficiency > 0.0
            else 0.0
        )
        cap = min(cap1, cap2, cap3)
        x_i = max(0.0, min(cap, pool))
        extra[i] = x_i
        pool -= x_i

    charge_w = [cmin[i] + extra[i] for i in range(n)]

    # 10. エネルギーの更新。
    ac_in_total = 0.0
    unserved_total = 0.0
    bms_in_total = 0.0
    bms_out_total = 0.0
    for i in range(n):
        st = states[i]
        if st.plugged:
            ac_in_i = load[i] + params.idle_w + charge_w[i]
            bms_in_w = charge_w[i] * params.charge_efficiency
            bms_out_w = params.passthrough_drain_w
            ac_in_total += ac_in_i
        else:
            need_w = (load[i] + params.idle_w) / params.discharge_efficiency if params.discharge_efficiency > 0.0 else 0.0
            need_wh = need_w * dt_h
            available_wh = max(0.0, st.soc_pct / 100.0 * units[i].capacity_wh)
            if need_wh <= 0.0:
                f = 1.0
            else:
                f = min(1.0, available_wh / need_wh)
            unserved_total += (1.0 - f) * load[i]
            bms_in_w = 0.0
            bms_out_w = f * need_w
        bms_in_total += bms_in_w
        bms_out_total += bms_out_w
        delta_soc = (bms_in_w - bms_out_w) * dt_h / units[i].capacity_wh * 100.0
        st.soc_pct = max(0.0, min(100.0, st.soc_pct + delta_soc))

    plugged_units = sum(1 for st in states if st.plugged)

    # load_shareの合計が1.0未満（台数不足・units=()を含む）の場合、按分しきれない
    # delta_load_wの残りはどの台にも割り当てられていない実負荷であり、消してはならない
    # （layer_model.pyの「units=()ならL1Sの buy/sell はL1と完全一致する」という退化不変条件は、
    # 割り当てられなかった分を丸ごとunservedとして返すことで成り立つ）。
    unassigned_load_w = max(0.0, delta_load_w - sum(load))
    unserved_total += unassigned_load_w

    # 11. 返り値。
    return FleetStepResult(
        ac_in_w=ac_in_total,
        unserved_w=unserved_total,
        plugged_units=plugged_units,
        bms_in_w=bms_in_total,
        bms_out_w=bms_out_total,
    )
