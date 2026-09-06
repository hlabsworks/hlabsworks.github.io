#!/usr/bin/env python3
"""layer_model.py — 5分プロファイル（SolarChargeController の energy_profile_5min 相当）から
系統連系点のエネルギー収支で真の家庭負荷 load_true を復元し、太陽光・蓄電池の有無で
4層（L0/L1/L2/L3）の請求期間ベース比較を行う。

設計根拠: docs/design/20260905_layer-model-ddr.md（SolarChargeController リポジトリ）。
本ファイルのコメントで参照する「§」はこの DDR の節番号。

前提の訂正（DDR §0、必読）: パワコンの consumption_w は独立計測ではなく
max(0, solar_w + buy_w − sell_w) の導出値であり、逆潮流時に0へ張り付くため
真の家庭負荷を表さない（実測7日平均で67%しか説明しない）。本モデルは consumption_w /
consumption_kwh を一切使わず、下記の恒等式（DDR §1）で真の負荷を復元する:

  load_true(t) = solar_w(t) + nichicon_pv_w(t) − nichicon_battery_w(t)
               + buy_w(t) − sell_w(t) + eco_ac_out_w(t) − eco_ac_in_w(t)

日次ゲート（DDR §3・§0既知のノイズ、オーナー承認機能・2026-09-06）: nichicon_realtime_history
は300秒瞬時値のためジッタで5分バケットを1個落とすのは常態。resolve_day_buckets()が、
1日あたりチャネルごとの欠落が INTERPOLATION_MAX_GAP(3)個以下なら前後の実測値で線形補間して
埋める（先頭/末尾の欠落は最近傍値）。4個以上の欠落・bucket_at重複・プロファイル自体が無い
場合は当該日を不採用のまま（period_incomplete/profile_missing）にし、捏造しない。補間した
バケット数は各レコードの interpolated_buckets に記録する。

層の定義（オーナー決定 2026-09-05）:
  L0 = 太陽光・蓄電池・本システムなし（推定、buy_L0 = load_true 全量買電）
  L1 = 太陽光のみ 9.4kW 全量（推定、DDR §2.3）
  L2 = 太陽光＋ニチコン ESS-H2L1（推定、DDR §2.4）
  L3 = 全部導入（実測。bill_model.build_month_record の bill_actual と同一）

入力:
  - 5分プロファイル CSV（stdin または --profile）。列名は energy_profile_5min のカラム名
    (bucket_at, solar_w, buy_w, sell_w, nichicon_pv_w, nichicon_battery_w, nichicon_soc,
    eco_ac_in_w, eco_ac_out_w, power_n, nichicon_n, ecoflow_n) を正とし、退避済み
    energy-archive/solarchgctl/profile_5min/*.csv の列名（bucket, n_p, n_n, n_e 等）も
    別名として受け付ける（COLUMN_ALIASES 参照）。eco_usb_out_w は負荷復元に使わない
    （オーナー決定）ため読み捨てる。
  - scripts/blog-metrics/tariff.json、data/metrics/daily.json、
    data/metrics/official_sell.json、data/metrics/official_buy.json
    （すべて bill_model.py と共用。料金計算ロジックは import して二重実装しない）
出力:
  - data/metrics/layers.json（公開・時間帯粒度やS/N等の在宅推定可能情報は含めない）
  - scripts/blog-metrics/.cache/daily_load.json（暦日ごとの復元負荷 kWh。bill_model.py が
    bill_l0_no_solar の算出に使う中間ファイル。Hugo は読まないため data/metrics/ には置かず
    .gitignore 済みの .cache/ に置く。時間帯粒度は含まない。QA #3/#12 対応: L0 の算出元は
    本ファイルの day_is_usable()/build_daily_load() 一箇所のみとし、bill_model.py 側では
    再計算しない）

5分プロファイルは SSH → stdin のメモリ経由のみで扱い、ファイルには書かない
（DDR §4。--profile はローカル開発・テスト用のみ）。
"""
from __future__ import annotations

import argparse
import csv
import io
import json
import sys
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import bill_model  # noqa: E402

SCRIPT_DIR = Path(__file__).resolve().parent
REPO_ROOT = SCRIPT_DIR.parent.parent

DEFAULT_TARIFF_PATH = SCRIPT_DIR / "tariff.json"
DEFAULT_DAILY_PATH = REPO_ROOT / "data" / "metrics" / "daily.json"
DEFAULT_OFFICIAL_SELL_PATH = REPO_ROOT / "data" / "metrics" / "official_sell.json"
DEFAULT_OFFICIAL_BUY_PATH = REPO_ROOT / "data" / "metrics" / "official_buy.json"
DEFAULT_OUT_PATH = REPO_ROOT / "data" / "metrics" / "layers.json"
DEFAULT_DAILY_LOAD_OUT_PATH = SCRIPT_DIR / ".cache" / "daily_load.json"

BUCKET_MINUTES = 5
DAY_BUCKETS = 24 * 60 // BUCKET_MINUTES  # 288
# QA #2: 0.95カバレッジゲートは廃止。L0〜L2は請求期間の全日が usable (day_is_usable) で
# ない限り unavailable にする（1日でも欠測なら按分・満月換算しない。部分月をあたかも
# 満月であるかのように compute_bill すると L0 が系統的に過小評価される。実例: 1日欠測で
# 3.2%過小 = ¥33,173 vs ¥34,258）。
MAX_EXPORT_WARN_W = 9400  # DDR §2.6: PCS合計9.9kW、9400W超で無音丸めせずstderr警告

# 蓄電池パラメータ（出典 https://www.nichicon.co.jp/products/ess/essh2l1.html 取得 2026-09-05、
# 実測較正2026-09-01で上書き。DDR §2.4）。
BATTERY_CHARGE_KWH_PER_100SOC = 10.4  # 実測: SOC 23→100%充電積分 7.99kWh
BATTERY_DISCHARGE_KWH_PER_100SOC = 8.92  # 実測: 放電9.01kWhでSOC降下101%
BATTERY_MAX_DISCHARGE_KW = 5.9  # 定格出力

PV_CAPACITY_KW = 9.4  # L1 全量（FIT設備認定が9.4kW一体、オーナー決定）

# オーナー承認機能・2026-09-06（データ再生成 #5）: 退避済みCSV(archive_csv)は単純平均集計で
# power_history由来チャンネルに約+4.1%の既知バイアスがある（DDR §5-C）。Pi側実装
# (energy_profile_5min, V1.00.059) 投入後は --profile-source で明示的に上書きする。
DEFAULT_PROFILE_SOURCE_LABEL = "archive_csv_simple_avg（暫定）"

# 5分プロファイルの列名エイリアス。energy_profile_5min（Pi側・実装済み、V1.00.059。
# EnergyProfile5MinAggregatorがdt加重(ゼロ次ホールド)で集計、DDR §5-C追記参照）のカラム名を
# 正とし、退避済みCSV（energy-archive/solarchgctl/profile_5min/*.csv、実装前の簡易集計で
# +4.1%バイアスあり、DDR §5-C参照）の列名も受け付ける。
COLUMN_ALIASES: dict[str, str] = {
    "bucket_at": "bucket_at", "bucket": "bucket_at",
    "solar_w": "solar_w",
    "buy_w": "buy_w",
    "sell_w": "sell_w",
    "nichicon_pv_w": "nichicon_pv_w",
    "nichicon_battery_w": "nichicon_battery_w",
    "nichicon_soc": "nichicon_soc",
    "eco_ac_in_w": "eco_ac_in_w",
    "eco_ac_out_w": "eco_ac_out_w",
    "power_n": "power_n", "n_p": "power_n",
    "nichicon_n": "nichicon_n", "n_n": "nichicon_n",
    "ecoflow_n": "ecoflow_n", "n_e": "ecoflow_n",
    # eco_usb_out_w: CSV側にのみ存在。恒等式(DDR §1)に無く、負荷復元には使わない。
}

# 恒等式(DDR §1)の復元に必須のフィールド。ひとつでも欠測ならそのバケットは None。
REQUIRED_LOAD_FIELDS = (
    "solar_w", "buy_w", "sell_w", "nichicon_pv_w", "nichicon_battery_w", "eco_ac_in_w", "eco_ac_out_w",
)


def _round_yen(value: float) -> int:
    """円単位に四捨五入する（ROUND_HALF_UP）。bill_model._round_yen と同じ方式を再利用する
    （売電/買電収入の丸め処理を二重実装しない）。"""
    return bill_model._round_yen(value)


def _to_float(value: str | None, column: str = "", bucket_at: str = "") -> float | None:
    """数値セルをfloatに変換する。変換できない場合は列名とbucket_atを含む例外にする
    （追加テストa: どのバケットのどの列が壊れているか原因追跡できるようにする）。"""
    if value is None or value == "":
        return None
    try:
        return float(value)
    except ValueError as exc:
        raise ValueError(
            f"profile行の数値変換に失敗しました: bucket_at={bucket_at!r} column={column!r} value={value!r}"
        ) from exc


@dataclass
class Bucket:
    bucket_at: str  # 'YYYY-MM-DD HH:MM' localtime
    solar_w: float | None
    buy_w: float | None
    sell_w: float | None
    nichicon_pv_w: float | None
    nichicon_battery_w: float | None
    nichicon_soc: float | None
    eco_ac_in_w: float | None
    eco_ac_out_w: float | None

    @property
    def date_str(self) -> str:
        return self.bucket_at[:10]

    def load_true_w(self) -> float | None:
        """DDR §1 の恒等式。必須フィールドが1つでも欠測なら None（捏造しない）。"""
        for field in REQUIRED_LOAD_FIELDS:
            if getattr(self, field) is None:
                return None
        return (
            self.solar_w + self.nichicon_pv_w - self.nichicon_battery_w
            + self.buy_w - self.sell_w
            + self.eco_ac_out_w - self.eco_ac_in_w
        )


def parse_profile_rows(rows) -> list[Bucket]:
    """csv.DictReader が返す行(dict)のイテラブルを Bucket のリストに変換する。"""
    buckets: list[Bucket] = []
    for row in rows:
        canon: dict[str, str] = {}
        for key, value in row.items():
            canon_key = COLUMN_ALIASES.get(key)
            if canon_key is None:
                continue
            canon[canon_key] = value
        if "bucket_at" not in canon or not canon["bucket_at"]:
            raise KeyError(f"profile行に bucket_at/bucket 列がありません: {row}")
        bucket_at = canon["bucket_at"]
        buckets.append(
            Bucket(
                bucket_at=bucket_at,
                solar_w=_to_float(canon.get("solar_w"), "solar_w", bucket_at),
                buy_w=_to_float(canon.get("buy_w"), "buy_w", bucket_at),
                sell_w=_to_float(canon.get("sell_w"), "sell_w", bucket_at),
                nichicon_pv_w=_to_float(canon.get("nichicon_pv_w"), "nichicon_pv_w", bucket_at),
                nichicon_battery_w=_to_float(canon.get("nichicon_battery_w"), "nichicon_battery_w", bucket_at),
                nichicon_soc=_to_float(canon.get("nichicon_soc"), "nichicon_soc", bucket_at),
                eco_ac_in_w=_to_float(canon.get("eco_ac_in_w"), "eco_ac_in_w", bucket_at),
                eco_ac_out_w=_to_float(canon.get("eco_ac_out_w"), "eco_ac_out_w", bucket_at),
            )
        )
    return buckets


def group_by_date(buckets: list[Bucket]) -> dict[str, list[Bucket]]:
    by_date: dict[str, list[Bucket]] = {}
    for b in buckets:
        by_date.setdefault(b.date_str, []).append(b)
    for d in by_date:
        by_date[d].sort(key=lambda b: b.bucket_at)
    return by_date


def expected_bucket_count(d: date) -> int:
    """その暦日に期待されるバケット数。DAY_BUCKETSは常に288（ローカルタイムのDST無し前提。
    日本にDSTは無いため常に一定）。"""
    return DAY_BUCKETS


# DDR §0既知のノイズ対策（オーナー承認機能・2026-09-06）: nichicon_realtime_history は
# 300秒瞬時値のため、ジッタで5分バケットを1個落とすのは常態（DDRの言う「隣接サンプル
# 線形補間」対策そのもの）。1日あたりチャネルごとの欠落が INTERPOLATION_MAX_GAP 個以下
# なら前後の有効値で線形補間して埋める（先頭/末尾の欠落は最近傍値）。それを超える場合は
# 従来どおり当日を不採用にし、捏造しない。
INTERPOLATION_MAX_GAP = 3


def _linear_interpolate(values: list[float | None]) -> list[float]:
    """Noneの穴を前後の既知値で線形補間して埋める（先頭/末尾の穴は最近傍値で埋める）。
    全欠測の場合は防御的に0.0で埋める（呼び出し側で事前にゲートしているため通常は
    到達しない）。"""
    n = len(values)
    result: list[float] = list(values)  # type: ignore[assignment]
    known = [i for i, v in enumerate(values) if v is not None]
    if not known:
        return [0.0] * n
    for i in range(0, known[0]):
        result[i] = values[known[0]]
    for i in range(known[-1] + 1, n):
        result[i] = values[known[-1]]
    for a, b in zip(known, known[1:]):
        if b - a <= 1:
            continue
        va, vb = values[a], values[b]
        for i in range(a + 1, b):
            result[i] = va + (vb - va) * (i - a) / (b - a)
    return result


def resolve_day_buckets(buckets: list[Bucket] | None, d: date) -> tuple[list[Bucket] | None, str | None, int]:
    """暦日dの5分プロファイルを解決する（オーナー承認機能・2026-09-06、DDR §0既知のノイズ
    対策）。バケット行の欠落（288未満）と各チャネルの欠測(None)を数え、
    REQUIRED_LOAD_FIELDS のいずれのチャネルも欠落が INTERPOLATION_MAX_GAP(3)個以下なら、
    前後の有効値で線形補間して288バケット全てを埋める（先頭/末尾の欠落は最近傍値）。
    いずれかのチャネルで欠落が4個以上、bucket_atが重複、またはプロファイル自体が無い
    場合は None を返す（部分的な積算による過小評価を避け、捏造しない）。

    戻り値: (解決済み288バケット|None, 却下理由の詳細文字列|None, 補間したバケット数の合計)。
    欠落ゼロの日は元のバケット列をそのまま返す（回帰: 既存動作と完全に同じ結果になる）。
    """
    expected = expected_bucket_count(d)
    if not buckets:
        return None, "5分プロファイルデータなし", 0

    # bucket_at が重複していると、行数は288でも実際には異なる時刻が欠落している
    # （重複分で頭数が水増しされる）。補間の対象外とし従来どおり不採用にする。
    distinct_times = {b.bucket_at for b in buckets}
    if len(distinct_times) != len(buckets):
        duplicated = len(buckets) - len(distinct_times)
        return None, f"bucket_at重複 {duplicated}件（実際の時刻種別 {len(distinct_times)}/{expected}）", 0

    by_time = {b.bucket_at: b for b in buckets}
    slots: list[str] = []
    cursor = f"{d.isoformat()} 00:00"
    for _ in range(expected):
        slots.append(cursor)
        cursor = _next_bucket_at(cursor, BUCKET_MINUTES)

    channels = REQUIRED_LOAD_FIELDS + ("nichicon_soc",)
    raw: dict[str, list[float | None]] = {ch: [] for ch in channels}
    for slot in slots:
        b = by_time.get(slot)
        for ch in channels:
            raw[ch].append(getattr(b, ch) if b is not None else None)

    gap_counts = {ch: sum(1 for v in raw[ch] if v is None) for ch in REQUIRED_LOAD_FIELDS}
    soc_gap = sum(1 for v in raw["nichicon_soc"] if v is None)
    max_gap = max(gap_counts.values())
    if max_gap == 0 and soc_gap == 0:
        return list(buckets), None, 0  # 完全一致（回帰: 従来どおり元のリストをそのまま返す）
    if max_gap > INTERPOLATION_MAX_GAP:
        return None, f"バケット欠落 最大{max_gap}件/{expected}（許容{INTERPOLATION_MAX_GAP}件を超過）", 0

    interpolated_total = 0
    filled: dict[str, list[float]] = {}
    for ch in channels:
        n_missing = sum(1 for v in raw[ch] if v is None)
        if n_missing == 0:
            filled[ch] = raw[ch]  # type: ignore[assignment]
            continue
        if ch in gap_counts:  # REQUIRED_LOAD_FIELDS分のみ集計対象にカウントする
            interpolated_total += n_missing
        filled[ch] = _linear_interpolate(raw[ch])

    resolved = [
        Bucket(
            bucket_at=slot,
            solar_w=filled["solar_w"][i], buy_w=filled["buy_w"][i], sell_w=filled["sell_w"][i],
            nichicon_pv_w=filled["nichicon_pv_w"][i], nichicon_battery_w=filled["nichicon_battery_w"][i],
            nichicon_soc=filled["nichicon_soc"][i],
            eco_ac_in_w=filled["eco_ac_in_w"][i], eco_ac_out_w=filled["eco_ac_out_w"][i],
        )
        for i, slot in enumerate(slots)
    ]
    return resolved, None, interpolated_total


def day_is_usable(buckets: list[Bucket] | None, d: date) -> tuple[bool, str | None]:
    """暦日dの5分プロファイルが（補間を含めて）usableかどうかを判定する。実際の
    （補間済み）バケット列が必要な呼び出し元は resolve_day_buckets を直接使うこと
    （ロジックの二重実装を避けるため、本関数はそちらに委譲する）。"""
    resolved, reason_detail, _ = resolve_day_buckets(buckets, d)
    return resolved is not None, reason_detail


def _dt_hours(bucket_minutes: int) -> float:
    return bucket_minutes / 60.0


def resample_buckets(buckets: list[Bucket], bucket_minutes: int) -> list[tuple[float, dict]]:
    """5分バケットを bucket_minutes 単位に平均で再集計する（DDR §5-B「バケット内分割」、
    不確かさ帯計算用）。戻り値は (dt_hours, {field: avg_w}) のリスト。
    5分のときはそのまま1件ずつ返す。"""
    if bucket_minutes <= BUCKET_MINUTES:
        return [
            (
                _dt_hours(BUCKET_MINUTES),
                {
                    "solar_w": b.solar_w, "nichicon_pv_w": b.nichicon_pv_w,
                    "nichicon_battery_w": b.nichicon_battery_w, "buy_w": b.buy_w, "sell_w": b.sell_w,
                    "eco_ac_out_w": b.eco_ac_out_w, "eco_ac_in_w": b.eco_ac_in_w,
                    "nichicon_soc": b.nichicon_soc,
                },
            )
            for b in buckets
        ]
    group_size = bucket_minutes // BUCKET_MINUTES
    fields = (
        "solar_w", "nichicon_pv_w", "nichicon_battery_w", "buy_w", "sell_w",
        "eco_ac_out_w", "eco_ac_in_w", "nichicon_soc",
    )
    out: list[tuple[float, dict]] = []
    for i in range(0, len(buckets), group_size):
        chunk = buckets[i : i + group_size]
        avg = {f: sum(getattr(b, f) for b in chunk) / len(chunk) for f in fields}
        out.append((_dt_hours(BUCKET_MINUTES) * len(chunk), avg))
    return out


@dataclass
class LayerTotals:
    buy_kwh: float
    sell_kwh: float
    max_export_w: float


def simulate_l0(resampled: list[tuple[float, dict]]) -> LayerTotals:
    buy_wh = 0.0
    for dt_h, row in resampled:
        solar = row["solar_w"]
        npv = row["nichicon_pv_w"]
        nb = row["nichicon_battery_w"]
        buy = row["buy_w"]
        sell = row["sell_w"]
        eco_out = row["eco_ac_out_w"]
        eco_in = row["eco_ac_in_w"]
        load = solar + npv - nb + buy - sell + eco_out - eco_in
        buy_wh += load * dt_h
    return LayerTotals(buy_kwh=buy_wh / 1000.0, sell_kwh=0.0, max_export_w=0.0)


def simulate_l1(resampled: list[tuple[float, dict]], pv_ac_efficiency: float) -> LayerTotals:
    """DDR §2.3。太陽光9.4kW全量のみ、蓄電池なし。"""
    buy_wh = 0.0
    sell_wh = 0.0
    max_export = 0.0
    for dt_h, row in resampled:
        solar = row["solar_w"]
        npv = row["nichicon_pv_w"]
        nb = row["nichicon_battery_w"]
        buy = row["buy_w"]
        sell = row["sell_w"]
        eco_out = row["eco_ac_out_w"]
        eco_in = row["eco_ac_in_w"]
        load = solar + npv - nb + buy - sell + eco_out - eco_in
        pv_ac = solar + npv * pv_ac_efficiency
        buy_w = max(0.0, load - pv_ac)
        sell_w = max(0.0, pv_ac - load)
        buy_wh += buy_w * dt_h
        sell_wh += sell_w * dt_h
        max_export = max(max_export, sell_w)
    return LayerTotals(buy_kwh=buy_wh / 1000.0, sell_kwh=sell_wh / 1000.0, max_export_w=max_export)


@dataclass
class L2StepResult:
    """simulate_l2 の1バケット分の内訳（テスト用に公開。QA #6/#7）。

    エネルギー保存則（pv_ac_efficiency=1.0 のとき厳密に成立）:
      pv_total_w − charge_taken_w + buy_w + discharge_w − load_w − sell_w == 0
    """

    load_w: float
    pv_total_w: float  # solar_w + nichicon_pv_w（西屋根PV含む総発電）
    charge_taken_w: float  # 実際に蓄電池へ入った電力（QA #6: 満充電で消えない、QA #7: npv超えない）
    discharge_w: float
    buy_w: float
    sell_w: float
    soc_pct: float  # このバケット処理後のSOC


def _simulate_l2_step(row: dict, dt_h: float, soc_pct: float, pv_ac_efficiency: float) -> L2StepResult:
    solar = row["solar_w"]
    npv = row["nichicon_pv_w"]
    nb = row["nichicon_battery_w"]
    buy = row["buy_w"]
    sell = row["sell_w"]
    eco_out = row["eco_ac_out_w"]
    eco_in = row["eco_ac_in_w"]
    load_w = solar + npv - nb + buy - sell + eco_out - eco_in
    pv_total_w = solar + npv

    # QA #7: 蓄電池が吸えるのは自分のDC結合PV(npv)だけ（AC余剰は吸わない、DDR §0 #3）。
    # 測定値nbがnpvを超える場合（センサー誤差・丸め等）でも、それを超える充電は創出しない。
    requested_charge_w = max(0.0, min(nb, npv))
    requested_charge_wh = requested_charge_w * dt_h

    # QA #6: SOC残headroomを超える分は受け入れない。受け入れられなかった分は消さず、
    # 通常のPV出力(pv_house_w)として家計負荷・売電側に回す（accepted分のみでSOC/家計を更新）。
    headroom_wh = max(0.0, (100.0 - soc_pct) / 100.0 * BATTERY_CHARGE_KWH_PER_100SOC * 1000.0)
    accepted_wh = min(requested_charge_wh, headroom_wh)
    soc_pct = min(100.0, soc_pct + accepted_wh / (BATTERY_CHARGE_KWH_PER_100SOC * 1000.0) * 100.0)
    charge_taken_w = accepted_wh / dt_h if dt_h > 0 else 0.0

    pv_house_w = solar + max(0.0, npv - charge_taken_w) * pv_ac_efficiency
    residual_w = load_w - pv_house_w

    if residual_w > 0:
        max_energy_from_soc_wh = soc_pct / 100.0 * BATTERY_DISCHARGE_KWH_PER_100SOC * 1000.0
        max_energy_from_power_wh = BATTERY_MAX_DISCHARGE_KW * 1000.0 * dt_h
        e_out_wh = max(0.0, min(residual_w * dt_h, max_energy_from_soc_wh, max_energy_from_power_wh))
        soc_pct = max(0.0, soc_pct - e_out_wh / (BATTERY_DISCHARGE_KWH_PER_100SOC * 1000.0) * 100.0)
        discharge_w = e_out_wh / dt_h if dt_h > 0 else 0.0
        buy_w = residual_w - discharge_w
        sell_w = 0.0
    else:
        discharge_w = 0.0
        buy_w = 0.0
        sell_w = -residual_w

    return L2StepResult(
        load_w=load_w, pv_total_w=pv_total_w, charge_taken_w=charge_taken_w,
        discharge_w=discharge_w, buy_w=buy_w, sell_w=sell_w, soc_pct=soc_pct,
    )


def simulate_l2(
    resampled: list[tuple[float, dict]], pv_ac_efficiency: float, soc_start_pct: float
) -> tuple[LayerTotals, float]:
    """DDR §2.4。太陽光＋ニチコンESS-H2L1、DELTAなし。戻り値: (集計, soc_end_pct)。

    充電電力は実測 nichicon_battery_w と西屋根PV(nichicon_pv_w)の小さい方を採用する
    （DELTAの有無で変わらない、AC余剰は吸わない、QA #7）。放電は残存負荷(load - pv_house)を
    蓄電池残量・定格出力の範囲で賄う。満充電で受け入れられなかった充電分はエネルギーとして
    消さず pv_house 側に回す（QA #6）。1バケット分の内訳は _simulate_l2_step 参照。
    """
    soc_pct = max(0.0, min(100.0, soc_start_pct))
    buy_wh = 0.0
    sell_wh = 0.0
    max_export = 0.0
    for dt_h, row in resampled:
        step = _simulate_l2_step(row, dt_h, soc_pct, pv_ac_efficiency)
        buy_wh += step.buy_w * dt_h
        sell_wh += step.sell_w * dt_h
        max_export = max(max_export, step.sell_w)
        soc_pct = step.soc_pct

    return LayerTotals(buy_kwh=buy_wh / 1000.0, sell_kwh=sell_wh / 1000.0, max_export_w=max_export), soc_pct


def _next_bucket_at(bucket_at: str, minutes: int) -> str:
    dt = datetime.strptime(bucket_at, "%Y-%m-%d %H:%M") + timedelta(minutes=minutes)
    return dt.strftime("%Y-%m-%d %H:%M")


def simulate_l2_series(
    ordered_buckets: list[Bucket], pv_ac_efficiency: float, initial_soc_pct: float
) -> dict[str, tuple[float, float]]:
    """時系列順（複数日をまたぐ、単一請求期間内を想定）のバケット列にL2を連続シミュレートし、
    日付ごとの(buy_kwh, sell_kwh)を返す（オーナー承認機能・2026-09-06: 日次4層系列）。

    バケット列の5分間隔の連続性が途切れる箇所（欠測日等のギャップ）では実測nichicon_socに
    再アンカーし、不明な期間の充放電を捏造しない（ドリフトを蓄積させない）。DDR §2.4の
    「期間開始時刻の実測socで初期化」をギャップ発生のたびにも適用したもの。
    """
    daily_wh: dict[str, list[float]] = {}
    soc_pct = max(0.0, min(100.0, initial_soc_pct))
    prev_bucket_at: str | None = None
    dt_h = _dt_hours(BUCKET_MINUTES)
    for b in ordered_buckets:
        if prev_bucket_at is not None and b.bucket_at != _next_bucket_at(prev_bucket_at, BUCKET_MINUTES):
            if b.nichicon_soc is not None:
                soc_pct = max(0.0, min(100.0, b.nichicon_soc))
        row = {
            "solar_w": b.solar_w, "nichicon_pv_w": b.nichicon_pv_w, "nichicon_battery_w": b.nichicon_battery_w,
            "buy_w": b.buy_w, "sell_w": b.sell_w, "eco_ac_out_w": b.eco_ac_out_w, "eco_ac_in_w": b.eco_ac_in_w,
        }
        step = _simulate_l2_step(row, dt_h, soc_pct, pv_ac_efficiency)
        entry = daily_wh.setdefault(b.date_str, [0.0, 0.0])
        entry[0] += step.buy_w * dt_h
        entry[1] += step.sell_w * dt_h
        soc_pct = step.soc_pct
        prev_bucket_at = b.bucket_at
    return {d: (v[0] / 1000.0, v[1] / 1000.0) for d, v in daily_wh.items()}


def daily_layer_dict(
    available: bool,
    buy_kwh: float | None = None,
    sell_kwh: float | None = None,
    buy_price_yen_per_kwh: float | None = None,
    sell_price_fit: float | None = None,
    sell_price_post_fit: float | None = None,
    extra: dict | None = None,
) -> dict:
    """日次レイヤー内訳（オーナー承認機能・2026-09-06）。月次の compute_bill（段階制・容量
    拠出金込み）とは異なり、per_kwh_only（買電単価×kWh − 売電単価×kWh）の概算にする
    （容量拠出金・段階は暦日に按分できないため）。available:false は金額キーを持たない
    （既存の unavailable_layer/available_layer と同じ規約）。"""
    if not available:
        return {"available": False}
    net_fit = _round_yen(buy_kwh * buy_price_yen_per_kwh - sell_kwh * sell_price_fit)
    net_post_fit = _round_yen(buy_kwh * buy_price_yen_per_kwh - sell_kwh * sell_price_post_fit)
    d = {
        "available": True,
        "buy_kwh": round(buy_kwh, 3),
        "sell_kwh": round(sell_kwh, 3),
        "net_cost_fit_yen": net_fit,
        "net_cost_post_fit_yen": net_post_fit,
    }
    if extra:
        d.update(extra)
    return d


def eco_balance_warning(buckets: list[Bucket]) -> str | None:
    """DDR §5-E。EcoFlow収支（in >= out がほぼ常に成立するはず）の異常を検出する。"""
    in_wh = sum((b.eco_ac_in_w or 0.0) * _dt_hours(BUCKET_MINUTES) for b in buckets)
    out_wh = sum((b.eco_ac_out_w or 0.0) * _dt_hours(BUCKET_MINUTES) for b in buckets)
    if out_wh > in_wh * 1.02:
        return f"EcoFlow収支異常: out={out_wh / 1000:.2f}kWh > in={in_wh / 1000:.2f}kWh"
    return None


def unavailable_layer(kind: str, reason: dict | None, extra: dict | None = None) -> dict:
    """QA #7: 「available:false の層は金額キーを持たない」形を1箇所に固定する。
    L0〜L3すべての unavailable 構築箇所はこの関数を経由する。

    reason は bill_model.reason() と同型の {reason_code, reason_label, reason_detail}
    （読者向けQAレビュー対応、2026-09-06）。reason_detail は内部ファイル名・キー名を含み
    うるため、UI側は reason_label のみを表示し reason_detail は title 属性等に限定する。
    """
    d = {"kind": kind, "available": False, "unavailable_reason": reason}
    if extra:
        d.update(extra)
    return d


def available_layer(
    kind: str,
    buy_kwh: float,
    sell_kwh: float,
    bill_dict: dict,
    net_cost_fit_yen: float,
    net_cost_post_fit_yen: float,
    extra: dict | None = None,
) -> dict:
    """QA #7: 「available:true の層」の形を1箇所に固定する。L0〜L2(compute_bill経由)と
    L3(bill_model.build_month_record経由)の両方がこの関数を通る。"""
    d = {
        "kind": kind,
        "available": True,
        "buy_kwh": round(buy_kwh, 3),
        "sell_kwh": round(sell_kwh, 3),
        "bill": bill_dict,
        "net_cost_fit_yen": net_cost_fit_yen,
        "net_cost_post_fit_yen": net_cost_post_fit_yen,
    }
    if extra:
        d.update(extra)
    return d


def layer_dict(
    kind: str,
    available: bool,
    buy_kwh: float | None,
    sell_kwh: float | None,
    tariff: dict,
    billing_month: str,
    sell_price_yen_per_kwh: float,
    sell_post_fit_price: float,
    unavailable_reason: dict | None = None,
    extra: dict | None = None,
) -> dict:
    """L0/L1/L2 用の薄いラッパー: compute_bill を呼んで available_layer/unavailable_layer を
    組み立てる（料金計算ロジックを二重実装しない）。"""
    if not available:
        return unavailable_layer(kind, unavailable_reason, extra)
    bill = bill_model.compute_bill(tariff, buy_kwh, billing_month)
    sell_revenue_fit = _round_yen(sell_kwh * sell_price_yen_per_kwh)
    sell_revenue_post_fit = _round_yen(sell_kwh * sell_post_fit_price)
    return available_layer(
        kind, buy_kwh, sell_kwh, bill.to_dict(),
        bill.total_yen - sell_revenue_fit, bill.total_yen - sell_revenue_post_fit, extra,
    )


def _format_missing_ranges(missing_days: list[str]) -> str:
    """欠測日のリストを連続区間に圧縮して読みやすくする（QA #4）。
    例: ['2026-08-02',...,'2026-08-26'] → '2026-08-02〜2026-08-26'。"""
    if not missing_days:
        return ""
    dates = [date.fromisoformat(d) for d in sorted(missing_days)]
    ranges: list[tuple[date, date]] = []
    start = prev = dates[0]
    for d in dates[1:]:
        if (d - prev).days == 1:
            prev = d
            continue
        ranges.append((start, prev))
        start = prev = d
    ranges.append((start, prev))
    parts = [s.isoformat() if s == e else f"{s.isoformat()}〜{e.isoformat()}" for s, e in ranges]
    return "、".join(parts)


def _missing_days_detail(missing_days: list[str], total_days: int) -> str:
    return f"5分プロファイル欠測 {len(missing_days)}/{total_days}日（{_format_missing_ranges(missing_days)}）"


def _missing_days_reason(missing_days: list[str], total_days: int) -> dict:
    """QA読者向け対応（2026-09-06）: 全日欠測（プロファイル自体が無い）と一部欠測を
    reason_code で区別する（profile_missing vs period_incomplete）。"""
    detail = _missing_days_detail(missing_days, total_days)
    if len(missing_days) >= total_days:
        return bill_model.reason(bill_model.REASON_CODE_PROFILE_MISSING, "5分プロファイル未取得", detail)
    return bill_model.reason(
        bill_model.REASON_CODE_PERIOD_INCOMPLETE, f"計測データ欠測（{len(missing_days)}日）", detail
    )


def build_month_layers(
    tariff: dict,
    billing_month: str,
    daily_by_date: dict,
    official_sell_by_month: dict,
    official_buy_by_month: dict,
    profile_by_date: dict[str, list[Bucket]],
) -> dict:
    """1請求月分の4層レコードを組み立てる。層ごとに available/unavailable_reason を持つため、
    呼び出し側(build_layers)は「1つも available が無い月」だけを excluded_months に回す
    （QA #4: 除外理由を層ごとに具体化するため、本関数は常にレコードを返す）。"""
    meter_read_day = tariff["meter_read_day"]
    start, end = bill_model.billing_period(billing_month, meter_read_day)
    total_days = (end - start).days + 1

    sell_fit = tariff["sell_price_yen_per_kwh"]["fit"]
    sell_post_fit = tariff["sell_price_yen_per_kwh"]["post_fit_assumed_for_readers"]

    # --- L3（実測）: bill_model の共通ロジックをそのまま再利用（二重実装しない） ---
    l3_record = None
    try:
        l3_record = bill_model.build_month_record(
            tariff, daily_by_date, billing_month, official_sell_by_month, official_buy_by_month, daily_load_by_date=None
        )
    except KeyError as exc:
        l3_record = {
            "billing_month": billing_month,
            "excluded": True,
            **bill_model.reason(bill_model.REASON_CODE_TARIFF_MISSING, "料金表の設定が不足しています", str(exc)),
        }

    if l3_record["excluded"]:
        l3_layer = unavailable_layer(
            "measured",
            bill_model.reason(l3_record["reason_code"], l3_record["reason_label"], l3_record["reason_detail"]),
        )
    else:
        bill_actual = l3_record["bill_actual"]
        l3_layer = available_layer(
            "measured",
            bill_actual["buy_kwh"],
            l3_record["sell_kwh_official"] if l3_record["sell_kwh_official"] is not None else l3_record["sell_kwh"],
            bill_actual,
            bill_actual["total_yen"] - l3_record["sell_revenue_fit_yen"],
            bill_actual["total_yen"] - l3_record["sell_revenue_post_fit_yen"],
            extra={"buy_source": l3_record["buy_source"], "sell_source": l3_record["sell_source"]},
        )

    # --- L0/L1/L2（推定）: 5分プロファイルが請求期間の全日そろっているか確認（QA #2: 部分月は
    # 満月換算しない。coverage < 1.0 なら1日でも欠測があるため unavailable にする） ---
    days_in_period = [start + timedelta(days=i) for i in range(total_days)]
    missing_days: list[str] = []
    all_buckets: list[Bucket] = []
    month_interpolated_buckets = 0
    for d in days_in_period:
        d_str = d.isoformat()
        day_buckets = profile_by_date.get(d_str)
        resolved_buckets, _, n_interpolated = resolve_day_buckets(day_buckets, d)
        if resolved_buckets is None:
            missing_days.append(d_str)
        else:
            all_buckets.extend(resolved_buckets)
            month_interpolated_buckets += n_interpolated

    coverage = 1.0 - (len(missing_days) / total_days) if total_days else 0.0

    if missing_days:
        reason = _missing_days_reason(missing_days, total_days)
        l0_layer = unavailable_layer("estimated", reason)
        l1_layer = unavailable_layer("estimated", reason)
        l2_layer = unavailable_layer("estimated", reason)
        uncertainty: dict = {}
        boundary_storage_kwh = None
        max_export_w = None
        eco_warning = None
    else:
        all_buckets.sort(key=lambda b: b.bucket_at)
        resampled5 = resample_buckets(all_buckets, BUCKET_MINUTES)

        l0_totals = simulate_l0(resampled5)
        l1_totals = simulate_l1(resampled5, pv_ac_efficiency=1.00)
        soc_start_pct = all_buckets[0].nichicon_soc if all_buckets[0].nichicon_soc is not None else 0.0
        l2_totals, soc_end_pct = simulate_l2(resampled5, pv_ac_efficiency=1.00, soc_start_pct=soc_start_pct)

        l0_layer = layer_dict("estimated", True, l0_totals.buy_kwh, l0_totals.sell_kwh, tariff, billing_month, sell_fit, sell_post_fit)
        l1_layer = layer_dict("estimated", True, l1_totals.buy_kwh, l1_totals.sell_kwh, tariff, billing_month, sell_fit, sell_post_fit)
        l2_layer = layer_dict(
            "estimated", True, l2_totals.buy_kwh, l2_totals.sell_kwh, tariff, billing_month, sell_fit, sell_post_fit,
            extra={"soc_start_pct": round(soc_start_pct, 1), "soc_end_pct": round(soc_end_pct, 1)},
        )

        max_export_w = max(l1_totals.max_export_w, l2_totals.max_export_w)
        if max_export_w > MAX_EXPORT_WARN_W:
            print(
                f"layer_model.py: {billing_month} の最大逆潮流推定 {max_export_w:.0f}W が {MAX_EXPORT_WARN_W}W を超えています",
                file=sys.stderr,
            )
        eco_warning = eco_balance_warning(all_buckets)
        if eco_warning:
            print(f"layer_model.py: {billing_month}: {eco_warning}", file=sys.stderr)

        boundary_storage_kwh = {
            "nichicon_soc_start_pct": round(soc_start_pct, 1),
            "nichicon_soc_end_pct": round(soc_end_pct, 1),
        }

        # 不確かさ帯: バケット5/15/30分 × pv_ac_efficiency 1.00/0.95 の組み合わせでL1/L2を再計算
        # （DDR §0既知のノイズ: max(0,・)は凸なのでバケットを粗くするほどJensenバイアスで
        # buy/sellが両方かさ上げされる）。
        l1_costs = []
        l2_costs = []
        for bucket_minutes in (5, 15, 30):
            resampled = resample_buckets(all_buckets, bucket_minutes)
            for eff in (1.00, 0.95):
                l1_t = simulate_l1(resampled, pv_ac_efficiency=eff)
                l1_bill = bill_model.compute_bill(tariff, l1_t.buy_kwh, billing_month)
                l1_costs.append(l1_bill.total_yen - _round_yen(l1_t.sell_kwh * sell_fit))
                l2_t, _ = simulate_l2(resampled, pv_ac_efficiency=eff, soc_start_pct=soc_start_pct)
                l2_bill = bill_model.compute_bill(tariff, l2_t.buy_kwh, billing_month)
                l2_costs.append(l2_bill.total_yen - _round_yen(l2_t.sell_kwh * sell_fit))
        uncertainty = {
            "L1": {"net_cost_fit_yen_min": min(l1_costs), "net_cost_fit_yen_max": max(l1_costs)},
            "L2": {"net_cost_fit_yen_min": min(l2_costs), "net_cost_fit_yen_max": max(l2_costs)},
            "note": "バケット5/15/30分 × pv_ac_efficiency 1.00/0.95 の組み合わせでのnet_cost_fit_yenの範囲（DDR §0既知のノイズ参照）",
        }

    layers = {"L0": l0_layer, "L1": l1_layer, "L2": l2_layer, "L3": l3_layer}

    return {
        "billing_month": billing_month,
        "usage_period": {"start": start.isoformat(), "end": end.isoformat(), "days": total_days},
        "coverage": round(coverage, 3),
        "layers": layers,
        "boundary_storage_kwh": boundary_storage_kwh,
        "max_export_w": round(max_export_w, 1) if max_export_w is not None else None,
        "uncertainty": uncertainty,
        "interpolated_buckets": month_interpolated_buckets,
    }


def build_daily_load(profile_by_date: dict[str, list[Bucket]]) -> list[dict]:
    """暦日ごとの復元負荷 kWh（bill_model.py の bill_l0_no_solar 用。時間帯粒度は含まない）。
    resolve_day_buckets が解決できない日（欠落が許容量を超える）は load_kwh=None にする
    （捏造しない）。1日3バケットまでの欠落は線形補間して埋める（オーナー承認機能・2026-09-06）。"""
    days = []
    for d_str in sorted(profile_by_date):
        buckets = profile_by_date[d_str]
        d = date.fromisoformat(d_str)
        resolved_buckets, _, _ = resolve_day_buckets(buckets, d)
        if resolved_buckets is None:
            days.append({"date": d_str, "load_kwh": None})
            continue
        load_wh = sum(b.load_true_w() * _dt_hours(BUCKET_MINUTES) for b in resolved_buckets)
        days.append({"date": d_str, "load_kwh": round(load_wh / 1000.0, 3)})
    return days


def build_daily_layers(tariff: dict, daily_by_date: dict, profile_by_date: dict[str, list[Bucket]]) -> list[dict]:
    """usableな各日（1日3バケットまでの欠落は resolve_day_buckets が線形補間して埋める。
    オーナー承認機能・2026-09-06、DDR §0既知のノイズ対策）についてL0/L1/L2(推定)と
    L3(センサー実測)の日次buy/sell kWhと円換算(per_kwh_only)を算出する（請求期間が全日
    揃うまで待たず、今ある分(08-28〜)を見せる）。暦日単位の集計のみで時間帯粒度は含まない。
    層ごとに独立して available/unavailable を判定する（1層でも欠ければ他層も隠す、では
    「今ある分を見せたい」という目的に反するため）。
    """
    meter_read_day = tariff["meter_read_day"]
    if not profile_by_date:
        return []

    # 1日3バケットまでの欠落は線形補間して埋める（オーナー承認機能・2026-09-06）。
    resolved_by_date: dict[str, list[Bucket]] = {}
    interpolated_by_date: dict[str, int] = {}
    for d_str, buckets in profile_by_date.items():
        resolved, _, n_interp = resolve_day_buckets(buckets, date.fromisoformat(d_str))
        if resolved is not None:
            resolved_by_date[d_str] = resolved
            interpolated_by_date[d_str] = n_interp

    usable_dates = sorted(resolved_by_date)
    if not usable_dates:
        return []

    # L0/L1は状態を持たないため日ごとに独立計算する。
    l0_l1_by_date: dict[str, tuple[LayerTotals, LayerTotals]] = {}
    for d_str in usable_dates:
        buckets = sorted(resolved_by_date[d_str], key=lambda b: b.bucket_at)
        resampled = resample_buckets(buckets, BUCKET_MINUTES)
        l0_l1_by_date[d_str] = (simulate_l0(resampled), simulate_l1(resampled, pv_ac_efficiency=1.0))

    # L2は請求期間ごとにグルーピングして連続シミュレートする（日をまたいで引き継ぐ）。
    first_date = date.fromisoformat(usable_dates[0])
    last_date = date.fromisoformat(usable_dates[-1])
    l2_by_date: dict[str, tuple[float, float]] = {}
    for billing_month in bill_model.list_candidate_billing_months(first_date, last_date):
        p_start, p_end = bill_model.billing_period(billing_month, meter_read_day)
        period_dates = [d for d in usable_dates if p_start.isoformat() <= d <= p_end.isoformat()]
        if not period_dates:
            continue
        ordered_buckets: list[Bucket] = []
        for d_str in period_dates:
            ordered_buckets.extend(resolved_by_date[d_str])
        ordered_buckets.sort(key=lambda b: b.bucket_at)
        initial_soc = ordered_buckets[0].nichicon_soc if ordered_buckets[0].nichicon_soc is not None else 0.0
        l2_by_date.update(simulate_l2_series(ordered_buckets, pv_ac_efficiency=1.0, initial_soc_pct=initial_soc))

    days = []
    for d_str in usable_dates:
        d = date.fromisoformat(d_str)
        billing_month = bill_model.billing_month_for_date(d, meter_read_day)
        try:
            buy_price, sell_fit, sell_post_fit, provisional, source_month = bill_model.per_kwh_prices(
                tariff, billing_month
            )
        except KeyError:
            # 暫定単価の出典すら無い（まだ1件も確定請求月が無い）場合はその日をスキップする。
            continue

        l0_totals, l1_totals = l0_l1_by_date[d_str]
        l2_buy_sell = l2_by_date.get(d_str)

        daily_row = daily_by_date.get(d_str) or {}
        l3_buy_kwh = daily_row.get("buy_kwh")
        l3_sell_kwh = daily_row.get("sell_kwh")
        l3_available = l3_buy_kwh is not None and l3_sell_kwh is not None

        days.append({
            "date": d_str,
            "billing_month": billing_month,
            "layers": {
                "L0": daily_layer_dict(True, l0_totals.buy_kwh, l0_totals.sell_kwh, buy_price, sell_fit, sell_post_fit),
                "L1": daily_layer_dict(True, l1_totals.buy_kwh, l1_totals.sell_kwh, buy_price, sell_fit, sell_post_fit),
                "L2": daily_layer_dict(
                    l2_buy_sell is not None,
                    l2_buy_sell[0] if l2_buy_sell else None,
                    l2_buy_sell[1] if l2_buy_sell else None,
                    buy_price, sell_fit, sell_post_fit,
                ),
                "L3": daily_layer_dict(
                    l3_available, l3_buy_kwh, l3_sell_kwh, buy_price, sell_fit, sell_post_fit,
                    extra={"source": "sensor"} if l3_available else None,
                ),
            },
            "tariff_basis": "per_kwh_only",
            "tariff_provisional": provisional,
            "tariff_source_month": source_month,
            "interpolated_buckets": interpolated_by_date.get(d_str, 0),
        })
    return days


def build_in_progress(
    tariff: dict,
    daily_by_date: dict,
    profile_by_date: dict[str, list[Bucket]],
    today: date,
) -> dict | None:
    """現在進行中の請求期間（todayを含む期間）の月途中集計（オーナー承認機能・2026-09-06）。
    確定月（build_month_layers）と異なり、期間の全日が揃うのを待たず開始日〜最後に usable な
    日までのデータで compute_bill する（段階制・容量拠出金込み、通常どおり）。単価が未確定
    なら直近確定月の単価を暫定適用し tariff_provisional / tariff_source_month で明示する
    （確定表示にのみ捏造禁止方針を適用し、月途中集計は明示ラベル付きで暫定単価を許容 —
    オーナー承認済み）。何のデータも無ければ None を返す。
    """
    meter_read_day = tariff["meter_read_day"]
    billing_month = bill_model.billing_month_for_date(today, meter_read_day)
    start, end = bill_model.billing_period(billing_month, meter_read_day)
    period_days = (end - start).days + 1
    effective_end = min(end, today)

    sell_fit = tariff["sell_price_yen_per_kwh"]["fit"]
    sell_post_fit = tariff["sell_price_yen_per_kwh"]["post_fit_assumed_for_readers"]
    effective_tariff, provisional, source_month = bill_model.resolve_effective_tariff(tariff, billing_month)

    # --- L3: センサー(daily.json)、開始日〜effective_endの範囲でusableな日のみ合算 ---
    l3_buy_kwh, l3_present, _ = bill_model.sum_period(daily_by_date, start, effective_end, "buy_kwh")
    l3_sell_kwh, _, _ = bill_model.sum_period(daily_by_date, start, effective_end, "sell_kwh")
    if l3_present == 0:
        l3_layer = unavailable_layer(
            "measured",
            bill_model.reason(bill_model.REASON_CODE_DAILY_MISSING, "計測データなし", "usage period内にdaily.jsonのデータが1日もありません"),
        )
    else:
        try:
            l3_bill = bill_model.compute_bill(effective_tariff, l3_buy_kwh, billing_month)
            l3_layer = available_layer(
                "measured", l3_buy_kwh, l3_sell_kwh, l3_bill.to_dict(),
                l3_bill.total_yen - _round_yen(l3_sell_kwh * sell_fit),
                l3_bill.total_yen - _round_yen(l3_sell_kwh * sell_post_fit),
                extra={"buy_source": "sensor", "sell_source": "sensor"},
            )
        except KeyError as exc:
            l3_layer = unavailable_layer(
                "measured", bill_model.reason(bill_model.REASON_CODE_TARIFF_MISSING, "料金表の設定が不足しています", str(exc))
            )

    # --- L0/L1/L2: usableな日のみ範囲内で合算（連続シミュレーションはL2のみ）。
    # 1日3バケットまでの欠落は線形補間して埋める（オーナー承認機能・2026-09-06）。---
    resolved_period_profile: dict[str, list[Bucket]] = {}
    period_interpolated_buckets = 0
    for d_str in sorted(profile_by_date):
        if not (start.isoformat() <= d_str <= effective_end.isoformat()):
            continue
        resolved, _, n_interp = resolve_day_buckets(profile_by_date[d_str], date.fromisoformat(d_str))
        if resolved is not None:
            resolved_period_profile[d_str] = resolved
            period_interpolated_buckets += n_interp
    period_profile_dates = sorted(resolved_period_profile)
    boundary_storage_kwh = None
    if not period_profile_dates:
        profile_reason = bill_model.reason(
            bill_model.REASON_CODE_PROFILE_MISSING, "5分プロファイル未取得",
            "usage period内にusableな5分プロファイルの日がありません",
        )
        l0_layer = unavailable_layer("estimated", profile_reason)
        l1_layer = unavailable_layer("estimated", profile_reason)
        l2_layer = unavailable_layer("estimated", profile_reason)
    else:
        ordered_buckets: list[Bucket] = []
        for d_str in period_profile_dates:
            ordered_buckets.extend(resolved_period_profile[d_str])
        ordered_buckets.sort(key=lambda b: b.bucket_at)
        resampled = resample_buckets(ordered_buckets, BUCKET_MINUTES)
        l0_totals = simulate_l0(resampled)
        l1_totals = simulate_l1(resampled, pv_ac_efficiency=1.0)
        soc_start_pct = ordered_buckets[0].nichicon_soc if ordered_buckets[0].nichicon_soc is not None else 0.0
        l2_totals, soc_end_pct = simulate_l2(resampled, pv_ac_efficiency=1.0, soc_start_pct=soc_start_pct)
        try:
            l0_layer = layer_dict(
                "estimated", True, l0_totals.buy_kwh, l0_totals.sell_kwh, effective_tariff, billing_month, sell_fit, sell_post_fit
            )
            l1_layer = layer_dict(
                "estimated", True, l1_totals.buy_kwh, l1_totals.sell_kwh, effective_tariff, billing_month, sell_fit, sell_post_fit
            )
            l2_layer = layer_dict(
                "estimated", True, l2_totals.buy_kwh, l2_totals.sell_kwh, effective_tariff, billing_month, sell_fit, sell_post_fit,
                extra={"soc_start_pct": round(soc_start_pct, 1), "soc_end_pct": round(soc_end_pct, 1)},
            )
            boundary_storage_kwh = {
                "nichicon_soc_start_pct": round(soc_start_pct, 1), "nichicon_soc_end_pct": round(soc_end_pct, 1),
            }
        except KeyError as exc:
            tariff_reason = bill_model.reason(bill_model.REASON_CODE_TARIFF_MISSING, "料金表の設定が不足しています", str(exc))
            l0_layer = unavailable_layer("estimated", tariff_reason)
            l1_layer = unavailable_layer("estimated", tariff_reason)
            l2_layer = unavailable_layer("estimated", tariff_reason)

    covered_dates: set[str] = set()
    for d in bill_model._daterange(start, effective_end):
        d_str = d.isoformat()
        if (daily_by_date.get(d_str) or {}).get("buy_kwh") is not None:
            covered_dates.add(d_str)
        if d_str in resolved_period_profile:
            covered_dates.add(d_str)
    if not covered_dates:
        return None

    return {
        "billing_month": billing_month,
        "status": "in_progress",
        "usage_period": {"start": start.isoformat(), "end": end.isoformat(), "days": period_days},
        "days_covered": len(covered_dates),
        "period_end_actual": max(covered_dates),
        "layers": {"L0": l0_layer, "L1": l1_layer, "L2": l2_layer, "L3": l3_layer},
        "boundary_storage_kwh": boundary_storage_kwh,
        "tariff_provisional": provisional,
        "tariff_source_month": source_month,
        "interpolated_buckets": period_interpolated_buckets,
    }


def build_layers(
    tariff: dict,
    daily_by_date: dict,
    official_sell_by_month: dict,
    official_buy_by_month: dict,
    profile_by_date: dict[str, list[Bucket]],
    today: date | None = None,
    profile_source: str = DEFAULT_PROFILE_SOURCE_LABEL,
) -> dict:
    candidate_months: set[str] = set()
    if daily_by_date:
        dates = sorted(date.fromisoformat(d) for d in daily_by_date)
        candidate_months.update(bill_model.list_candidate_billing_months(dates[0], dates[-1]))
    if profile_by_date:
        pdates = sorted(date.fromisoformat(d) for d in profile_by_date)
        candidate_months.update(bill_model.list_candidate_billing_months(pdates[0], pdates[-1]))

    months = []
    excluded = []
    for billing_month in sorted(candidate_months):
        record = build_month_layers(
            tariff, billing_month, daily_by_date, official_sell_by_month, official_buy_by_month, profile_by_date
        )
        layers = record["layers"]
        if any(layer.get("available") for layer in layers.values()):
            months.append(record)
        else:
            # QA #4: 全層が unavailable の月は層ごとの理由を残す（一律の文言にしない）。
            # 各理由は {reason_code, reason_label, reason_detail} 形式（読者向けQAレビュー対応）。
            excluded.append({
                "billing_month": billing_month,
                "layer_reasons": {key: layer.get("unavailable_reason") for key, layer in layers.items()},
            })

    cumulative = _build_cumulative(months)

    # QA #7: ダッシュボードのハードコード日付をやめ、入力プロファイルの最古バケット日を
    # params.profile_since として出力する（無ければ null。日付のみで時間帯粒度は含まない）。
    profile_since = min(profile_by_date) if profile_by_date else None

    daily_layers = build_daily_layers(tariff, daily_by_date, profile_by_date)
    in_progress = build_in_progress(tariff, daily_by_date, profile_by_date, today or date.today())

    return {
        "params": {
            "battery_charge_kwh_per_100soc": BATTERY_CHARGE_KWH_PER_100SOC,
            "battery_discharge_kwh_per_100soc": BATTERY_DISCHARGE_KWH_PER_100SOC,
            "battery_max_discharge_kw": BATTERY_MAX_DISCHARGE_KW,
            "pv_capacity_kw": PV_CAPACITY_KW,
            "pv_ac_efficiency": 1.00,
            "sell_price_yen_per_kwh_fit": tariff["sell_price_yen_per_kwh"]["fit"],
            "sell_price_yen_per_kwh_post_fit": tariff["sell_price_yen_per_kwh"]["post_fit_assumed_for_readers"],
            "profile_since": profile_since,
            "profile_source": profile_source,
            "_source": "docs/design/20260905_layer-model-ddr.md §2.4（蓄電池パラメータ出典・実測較正済み）",
        },
        "months": months,
        "excluded_months": excluded,
        "cumulative": cumulative,
        "daily": daily_layers,
        "in_progress": in_progress,
        "_note": (
            "5分プロファイルは退避済みCSV（energy-archive/solarchgctl/profile_5min/、単純平均集計）を"
            "暫定的に使用しており、power_history由来チャンネル(solar_w/sell_w)に約+4.1%の既知バイアスが"
            "ある（docs/design/20260905_layer-model-ddr.md §5-C参照）。Pi側 EnergyProfile5MinAggregator"
            "（dt加重、V1.00.059実装済み）の本番投入・データ蓄積後にこの入力を置き換え、再検証する。"
        ),
    }


def _build_cumulative(months: list[dict]) -> dict:
    """「導入で年間いくら浮いているか」= 全4層が available な月の累計（オーナー決定）。
    L1/L2 も併記し「太陽光のみ」「＋蓄電池」の限界価値を可視化する（QA #9c）。"""
    full_months = [m for m in months if all(m["layers"][layer]["available"] for layer in ("L0", "L1", "L2", "L3"))]
    if not full_months:
        return {"months_included": 0, "available": False}
    total_l0 = sum(m["layers"]["L0"]["net_cost_fit_yen"] for m in full_months)
    total_l1 = sum(m["layers"]["L1"]["net_cost_fit_yen"] for m in full_months)
    total_l2 = sum(m["layers"]["L2"]["net_cost_fit_yen"] for m in full_months)
    total_l3 = sum(m["layers"]["L3"]["net_cost_fit_yen"] for m in full_months)
    return {
        "months_included": len(full_months),
        "available": True,
        "billing_months": [m["billing_month"] for m in full_months],
        "net_cost_fit_yen": {"L0": total_l0, "L1": total_l1, "L2": total_l2, "L3": total_l3},
        "saving_yen_fit": total_l0 - total_l3,
    }


def _read_profile_rows(profile_path: Path | None) -> list[dict]:
    if profile_path is not None:
        text = profile_path.read_text(encoding="utf-8")
    else:
        if sys.stdin.isatty():
            return []
        text = sys.stdin.read()
    if not text.strip():
        return []
    return list(csv.DictReader(io.StringIO(text)))


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--tariff", type=Path, default=DEFAULT_TARIFF_PATH, help="tariff.json のパス")
    parser.add_argument("--daily", type=Path, default=DEFAULT_DAILY_PATH, help="daily.json のパス")
    parser.add_argument("--official-sell", type=Path, default=DEFAULT_OFFICIAL_SELL_PATH, help="official_sell.json のパス")
    parser.add_argument("--official-buy", type=Path, default=DEFAULT_OFFICIAL_BUY_PATH, help="official_buy.json のパス")
    parser.add_argument(
        "--profile", type=Path, default=None,
        help="5分プロファイルCSVのパス（省略時はstdinから読む。本番運用ではPiからのSSH stdinパイプのみを使い、"
        "ファイルには書かない。--profileはローカル開発・テスト専用）",
    )
    parser.add_argument("--out", type=Path, default=DEFAULT_OUT_PATH, help="出力先 layers.json のパス")
    parser.add_argument(
        "--daily-load-out", type=Path, default=DEFAULT_DAILY_LOAD_OUT_PATH,
        help="出力先 daily_load.json のパス（bill_model.py の bill_l0_no_solar 用）",
    )
    parser.add_argument(
        "--today", type=str, default=None,
        help="in_progress（月途中集計）の基準日('YYYY-MM-DD'、省略時は実行日）。テスト用。",
    )
    parser.add_argument(
        "--profile-source", type=str, default=DEFAULT_PROFILE_SOURCE_LABEL,
        help="params.profile_source に出力する説明文字列（Pi側 energy_profile_5min 投入後は明示的に変更する）",
    )
    args = parser.parse_args()

    tariff = json.loads(args.tariff.read_text(encoding="utf-8"))
    daily_by_date = bill_model.load_daily(args.daily) if args.daily.exists() else {}
    official_sell_by_month = bill_model.load_official_sell(args.official_sell)
    official_buy_by_month = bill_model.load_official_buy(args.official_buy)
    today = date.fromisoformat(args.today) if args.today else date.today()

    rows = _read_profile_rows(args.profile)
    buckets = parse_profile_rows(rows)
    profile_by_date = group_by_date(buckets)

    if not profile_by_date:
        print("layer_model.py: 5分プロファイルが空のため L0/L1/L2 はすべて unavailable になります", file=sys.stderr)

    result = build_layers(
        tariff, daily_by_date, official_sell_by_month, official_buy_by_month, profile_by_date,
        today=today, profile_source=args.profile_source,
    )
    result["generated_at"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(
        f"wrote: {args.out} ({len(result['months'])} months, {len(result['excluded_months'])} excluded, "
        f"{len(result['daily'])} daily rows, in_progress={'yes' if result['in_progress'] else 'no'})"
    )

    daily_load = {"days": build_daily_load(profile_by_date), "generated_at": result["generated_at"]}
    args.daily_load_out.parent.mkdir(parents=True, exist_ok=True)
    args.daily_load_out.write_text(json.dumps(daily_load, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(f"wrote: {args.daily_load_out} ({len(daily_load['days'])} days)")


if __name__ == "__main__":
    main()
