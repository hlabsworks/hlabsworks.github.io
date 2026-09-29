#!/usr/bin/env python3
"""monthly_report.py — 請求月が確定する（または請求期間終了後に速報段階に入る）たびに、
posts/YYYY-MM.json（数値だけのスナップショット。自由文は一切含まない）を作成・改版する。

設計判断（2026-09-23/26 オーナー決定「速報＋改訂」方式）: homelab は Markdown を作らない。
本スクリプトが作るのは posts/YYYY-MM.json という数値スナップショットだけで、記事の文章は
main 側の render_monthly_posts.py（信頼済みコード）がビルド時に生成する。これにより
homelab が侵害されても自由文が公開側に渡らない。

homelab の run-daily.sh と CI 側の validate_metrics.py の両方が本モジュールの
is_closable()/build_snapshot_body() を import して使うため、同じ確定判定・同じ再計算ロジックを
共有する（1箇所直せば両方に効く）。stdlib + bill_model/layer_model のみで書き、Python
3.12/3.13 両方で動く構文に限る。

使い方（homelab の run-daily.sh から呼ばれる）:
  python3 monthly_report.py --data-dir CLONE/data/metrics --posts-dir CLONE/posts \
      --tariff /opt/blog-metrics/inputs/tariff.json --today 2026-09-26
"""
from __future__ import annotations

import argparse
import json
import sys
from datetime import date, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import bill_model  # noqa: E402  billing_period()を二重実装しない
import layer_model  # noqa: E402  PRELIMINARY_DELAY_DAYS の正本を1箇所にする(QA指摘2026-09-26)

# 追補(2026-09-26「速報＋改訂」方式) A': 最初の自動記事は請求月2026-10（使用期間2026-09-02〜
# 2026-10-01）。過去月を後から埋めても記事が一度に大量公開されないようにする防波堤。
FIRST_REPORT_BILLING_MONTH = "2026-10"

# QA指摘2026-09-26: layer_model.build_layers() の preliminary_months[] 候補判定と同じ値を
# 使う（正本は layer_model.py 側の1箇所）。
PRELIMINARY_DELAY_DAYS = layer_model.PRELIMINARY_DELAY_DAYS

# QA指摘2026-09-26: 確定後の改版がrevision上限を超えたら書き込まずログを出して非ゼロ終了する
# （通常運用では起こらないはずの異常系。無限に改版し続ける不具合の暴走を止める安全弁）。
MAX_REVISION = 12

_META_KEYS = ("first_published", "revision", "revised", "transitioned_from_preliminary")
# 改版の要否は「layer由来」の項目だけで判定する(QA指摘2026-09-26): energy/weather/comparison
# はdaily.jsonの420日窓が動くだけで(意味のあるデータ変化なしに)値が変わることがあるため、
# 確定後の改版判定・inherit対象から除く。
_LAYER_DERIVED_KEYS = ("layers", "l2_band", "l3_source", "stage", "estimation", "days_usable", "days_total")


class RevisionLimitExceededError(RuntimeError):
    """確定後の改版でrevisionがMAX_REVISIONを超える場合に送出する（安全弁）。"""


def body_without_meta(snapshot: dict) -> dict:
    """スナップショットから first_published/revision/revised を除いた「本体」を返す
    （G15の再計算一致比較・冪等性の差分検出はこの本体だけで行う）。"""
    return {k: v for k, v in snapshot.items() if k not in _META_KEYS}


def layer_derived_subset(snapshot: dict) -> dict:
    return {k: snapshot[k] for k in _LAYER_DERIVED_KEYS}


def is_closable(month_rec: dict | None, *, first_report_billing_month: str = FIRST_REPORT_BILLING_MONTH) -> bool:
    """DDR §A: month_rec（layers.json の months[] の1エントリ）が「確定」とみなせるか。
    以下をすべて満たす月だけを確定とみなす。
      1. L0/L1/L2/L3 すべて available
      2. L3.buy_source == "billed"（請求書の実使用量を取り込み済み）
      3. L3.sell_source == "official_meter"（検針値の売電実績を取り込み済み）
      4. uncertainty.L2.net_cost_fit_yen_min/max がある
      5. billing_month >= first_report_billing_month（防波堤）
    確定しない理由はここでは判定しない（呼び出し側がログに出す）。"""
    if month_rec is None:
        return False
    if month_rec["billing_month"] < first_report_billing_month:
        return False
    layers = month_rec.get("layers", {})
    for key in ("L0", "L1", "L2", "L3"):
        if not layers.get(key, {}).get("available"):
            return False
    l3 = layers["L3"]
    if l3.get("buy_source") != "billed":
        return False
    if l3.get("sell_source") != "official_meter":
        return False
    l2_band = (month_rec.get("uncertainty") or {}).get("L2") or {}
    if "net_cost_fit_yen_min" not in l2_band or "net_cost_fit_yen_max" not in l2_band:
        return False
    return True


# --- 日照分類（外部API・緯度を使わない。§C「日照分類」） -----------------------------------
WEATHER_WINDOW_DAYS = 15
WEATHER_MIN_SAMPLES = 10
WEATHER_SUNNY_RATIO = 0.75
WEATHER_CLOUDY_RATIO = 0.40


def _day_of_year_non_leap(d: date) -> int:
    """月日だけを年をまたいで比較するための基準（閏年2/29は2/28扱いにする）。"""
    month, day = d.month, d.day
    if month == 2 and day == 29:
        day = 28
    return date(2001, month, day).timetuple().tm_yday


def _season_distance_days(a: date, b: date) -> int:
    """月日だけを見た循環距離（年をまたいで近い側を採る）。"""
    diff = abs(_day_of_year_non_leap(a) - _day_of_year_non_leap(b))
    return min(diff, 365 - diff)


def classify_weather(daily_by_date: dict[str, dict], start: date, end: date) -> dict:
    """§C「日照分類」。usage_period内の各日を sunny/cloudy/overcast/unknown に分類し、
    日数だけを返す（参照値R(d)そのものは公開しない）。

    R(d) = daily_by_date のうち x<=end（因果的な集合。窓の終端をusage_period.endに固定する
    のは、翌年のデータが増えても過去月の分類が変わらないようにするため）かつ季節日(月日)が
    dの±WEATHER_WINDOW_DAYS日以内、かつsolar_kwhが非nullの日のsolar_kwhの最大値。
    標本がWEATHER_MIN_SAMPLES日未満、または当日の値がnullならunknown。
    r = solar/R: r>=0.75が晴れ相当、0.40<=r<0.75がくもり相当、r<0.40が雨・厚い雲相当。"""
    counts = {"sunny_days": 0, "cloudy_days": 0, "overcast_days": 0, "unknown_days": 0}
    candidates: list[tuple[date, float]] = []
    for x_str, row in daily_by_date.items():
        solar = row.get("solar_kwh")
        if solar is None:
            continue
        x = date.fromisoformat(x_str)
        if x <= end:
            candidates.append((x, solar))

    d = start
    while d <= end:
        row = daily_by_date.get(d.isoformat())
        solar = row.get("solar_kwh") if row else None
        matching = [v for x, v in candidates if _season_distance_days(d, x) <= WEATHER_WINDOW_DAYS]
        if solar is None or len(matching) < WEATHER_MIN_SAMPLES:
            counts["unknown_days"] += 1
        else:
            ref_max = max(matching)
            if ref_max <= 0:
                counts["unknown_days"] += 1
            else:
                r = solar / ref_max
                if r >= WEATHER_SUNNY_RATIO:
                    counts["sunny_days"] += 1
                elif r >= WEATHER_CLOUDY_RATIO:
                    counts["cloudy_days"] += 1
                else:
                    counts["overcast_days"] += 1
        d += timedelta(days=1)
    return counts


# --- energy/comparison 集計（§B） -----------------------------------------------------------
_ENERGY_FIELD_MAP = {
    "solar_kwh": "solar_kwh",
    "sell_kwh_sensor": "sell_kwh",
    "nichicon_charge_kwh": "nichicon_charge_kwh",
    "ecoflow_charge_kwh": "ecoflow_charge_kwh",
}


def _sum_field_if_complete(daily_by_date: dict[str, dict], field: str, start: date, end: date) -> float | None:
    total = 0.0
    d = start
    while d <= end:
        row = daily_by_date.get(d.isoformat())
        value = row.get(field) if row else None
        if value is None:
            return None
        total += value
        d += timedelta(days=1)
    return round(total, 1)


def _sum_energy(daily_by_date: dict[str, dict], start: date, end: date) -> dict:
    return {out_key: _sum_field_if_complete(daily_by_date, src_key, start, end) for out_key, src_key in _ENERGY_FIELD_MAP.items()}


def _shift_billing_month(billing_month: str, delta_months: int) -> str:
    year, month = int(billing_month[:4]), int(billing_month[5:7])
    total = year * 12 + (month - 1) + delta_months
    new_year, new_month0 = divmod(total, 12)
    return f"{new_year:04d}-{new_month0 + 1:02d}"


def _comparison(daily_by_date: dict, billing_month: str, meter_read_day: int) -> dict:
    """QA指摘2026-09-26: 比較対象の期間は layers.json の months[]/preliminary_months[] を
    探すのではなく bill_model.billing_period() で直接求める（420日窓の外に出て一覧から
    消えた月でも、前月比・前年同月比の期間自体は変わらないため）。"""
    prev_start, prev_end = bill_model.billing_period(_shift_billing_month(billing_month, -1), meter_read_day)
    yoy_start, yoy_end = bill_model.billing_period(_shift_billing_month(billing_month, -12), meter_read_day)
    return {
        "prev_solar_kwh": _sum_field_if_complete(daily_by_date, "solar_kwh", prev_start, prev_end),
        "yoy_solar_kwh": _sum_field_if_complete(daily_by_date, "solar_kwh", yoy_start, yoy_end),
    }


def build_snapshot_body(billing_month: str, layers: dict, daily_by_date: dict, stage: str, meter_read_day: int) -> dict | None:
    """§B スナップショットv1の本体（first_published/revision/revisedを除く）を組み立てる。
    stage=="final" は layers["months"]、stage=="preliminary" は layers["preliminary_months"]
    からbilling_monthのレコードを探す。L0〜L3のいずれかがunavailable、L2の不確かさ帯が無い、
    final で L3 が buy_source=="billed"/sell_source=="official_meter" でない、または
    billing_month が FIRST_REPORT_BILLING_MONTH より前の場合は None を返す（作れない月）。"""
    if billing_month < FIRST_REPORT_BILLING_MONTH:
        return None

    source_key = "months" if stage == "final" else "preliminary_months"
    record = next((m for m in layers.get(source_key, []) if m["billing_month"] == billing_month), None)
    if record is None:
        return None

    layer_map = record.get("layers", {})
    for key in ("L0", "L1", "L2", "L3"):
        if not layer_map.get(key, {}).get("available"):
            return None
    l3 = layer_map["L3"]
    if stage == "final" and (l3.get("buy_source") != "billed" or l3.get("sell_source") != "official_meter"):
        return None

    uncertainty = record.get("uncertainty") or {}
    l2_band_src = uncertainty.get("L2") or {}
    if "net_cost_fit_yen_min" not in l2_band_src or "net_cost_fit_yen_max" not in l2_band_src:
        return None

    usage_period = record["usage_period"]
    expected_start, expected_end = bill_model.billing_period(billing_month, meter_read_day)
    if usage_period["start"] != expected_start.isoformat() or usage_period["end"] != expected_end.isoformat():
        return None  # billing_month と usage_period が矛盾するレコードは作らない
    start, end = expected_start, expected_end
    days = usage_period["days"]

    l0 = layer_map["L0"]

    return {
        "schema_version": 1,
        "billing_month": billing_month,
        "report_month": start.isoformat()[:7],
        "usage_period": {"start": start.isoformat(), "end": end.isoformat(), "days": days},
        "estimation": l0.get("estimation", "full"),
        "days_usable": l0.get("days_usable", days),
        "days_total": l0.get("days_total", days),
        "layers": {
            key: {
                "net_cost_fit_yen": layer_map[key]["net_cost_fit_yen"],
                "net_cost_post_fit_yen": layer_map[key]["net_cost_post_fit_yen"],
                "buy_kwh": round(layer_map[key]["buy_kwh"], 1),
                "sell_kwh": round(layer_map[key]["sell_kwh"], 1),
            }
            for key in ("L0", "L1", "L2", "L3")
        },
        "l2_band": {
            "net_cost_fit_yen_min": l2_band_src["net_cost_fit_yen_min"],
            "net_cost_fit_yen_max": l2_band_src["net_cost_fit_yen_max"],
        },
        "energy": _sum_energy(daily_by_date, start, end),
        "weather": classify_weather(daily_by_date, start, end),
        "comparison": _comparison(daily_by_date, billing_month, meter_read_day),
        "stage": stage,
        # QA再指摘2026-09-26 R4: tariff_basisはstageで決め打ちせず、レコード自身の
        # tariff_provisional（layer_model.build_month_layers(allow_provisional_tariff=True)
        # が付ける）から決める。months[]のレコード(allow_provisional_tariff=False)は
        # このキーを持たないため常にFalse=="confirmed"になる。不変条件は「stage=="final"
        # ならtariff_basis=="confirmed"」の片方向のみ（速報でも単価は既に確定済みという
        # 状態はありうるため。追補D'を緩和。validate_metrics.py G14参照）。
        "tariff_basis": "provisional" if record.get("tariff_provisional") else "confirmed",
        "l3_source": {"buy": l3.get("buy_source"), "sell": l3.get("sell_source")},
    }


def expected_final_body(
    billing_month: str, layers: dict, daily_by_date: dict, meter_read_day: int,
    prev_stage: str | None, prev_body: dict | None,
) -> dict | None:
    """確定版として書き込む／検証すべき本体を返す。billing_monthが確定条件(is_closable)を
    満たす前提で呼ぶ（呼び出し側で確認済みであること）。run()とvalidate_metrics.pyのG15の
    両方がこの関数を共有し、「本体はどうあるべきか」のロジックを1箇所にする
    （QA指摘2026-09-26 N3: 以前はG15がenergy/weather/comparisonを再計算と比較しておらず、
    新規確定・速報からの遷移でも捏造値が通っていた）。

    prev_stage/prev_body: 前回のpost（無ければ両方None）。
    - 前回が無い、または前回がfinalでない（速報からの遷移含む）: 全項目を新規に計算する。
    - 前回がfinalで、layer由来の項目(layers/l2_band/l3_source/stage/estimation/days_*)が
      変わっていなければ、前回の本体をそのまま返す（凍結。改版しない）。
    - 前回がfinalで、layer由来の項目が変わっていれば、layer由来だけ再計算し、
      energy/weather/comparisonは前回の値を引き継ぐ（420日窓が動くだけの無意味な改版を
      避けるため）。
    """
    recomputed = build_snapshot_body(billing_month, layers, daily_by_date, "final", meter_read_day)
    if recomputed is None:
        return None
    if prev_stage != "final" or prev_body is None:
        return recomputed
    if layer_derived_subset(recomputed) == layer_derived_subset(prev_body):
        return prev_body
    result = dict(recomputed)
    result["energy"] = prev_body["energy"]
    result["weather"] = prev_body["weather"]
    result["comparison"] = prev_body["comparison"]
    return result


def _write_snapshot(path: Path, snapshot: dict) -> None:
    path.write_text(json.dumps(snapshot, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def _load_existing(path: Path) -> dict | None:
    if not path.exists():
        return None
    return json.loads(path.read_text(encoding="utf-8"))


def run(
    data_dir: Path,
    posts_dir: Path,
    today: date,
    *,
    meter_read_day: int,
    first_report_billing_month: str = FIRST_REPORT_BILLING_MONTH,
) -> int:
    """確定した月・速報段階に入った月ごとに posts/YYYY-MM.json を作成・改版する。
    戻り値: 新規に書き込んだファイル数（改版・凍結は含まない。ログ用）。
    確定後の改版でrevisionがMAX_REVISIONを超える場合は RevisionLimitExceededError を送出する
    （呼び出し側のCLIはこれを非ゼロ終了に変換する。QA指摘2026-09-26の安全弁）。"""
    layers = json.loads((data_dir / "layers.json").read_text(encoding="utf-8"))
    daily_rows = json.loads((data_dir / "daily.json").read_text(encoding="utf-8"))
    daily_by_date = {row["date"]: row for row in daily_rows}

    months_by_key = {m["billing_month"]: m for m in layers.get("months", [])}
    preliminary_by_key = {m["billing_month"]: m for m in layers.get("preliminary_months", [])}

    posts_dir.mkdir(parents=True, exist_ok=True)
    written = 0

    for billing_month in sorted(set(months_by_key) | set(preliminary_by_key)):
        if billing_month < first_report_billing_month:
            continue
        month_rec = months_by_key.get(billing_month)
        closable = is_closable(month_rec, first_report_billing_month=first_report_billing_month)
        path = posts_dir / f"{billing_month}.json"
        existing = _load_existing(path)

        if existing is not None and existing.get("stage") == "final":
            # 確定後は凍結が既定。420日窓の外に出てmonths[]から消えても一切触らない。
            # 改版の要否・本体はexpected_final_body()に委ねる（run()とG15で同じロジックを
            # 共有する。QA指摘2026-09-26 N3）。
            if not closable:
                continue
            expected = expected_final_body(
                billing_month, layers, daily_by_date, meter_read_day, "final", body_without_meta(existing),
            )
            if expected is None or expected == body_without_meta(existing):
                continue  # 計算不能、または本体に変化なし(凍結)
            if existing["revision"] >= MAX_REVISION:
                raise RevisionLimitExceededError(
                    f"{billing_month}: revisionが上限({MAX_REVISION})に達したため書き込みません"
                )
            existing["revision"] = existing["revision"] + 1
            existing["revised"] = today.isoformat()
            # この改版は速報からの遷移ではなく確定後の訂正（QA指摘2026-09-26 item10:
            # render_monthly_posts.py が改版文の表現を選ぶための目印）。
            existing["transitioned_from_preliminary"] = False
            existing.update(expected)
            _write_snapshot(path, existing)
            continue

        if existing is not None and existing.get("stage") == "preliminary":
            # 速報は初回公開後は凍結する（revision==1を維持）。確定したときだけ確定版へ遷移する
            # （遷移時は新規公開に近いため、energy/weather/comparisonも新しく計算し直す。
            # prev_stage="preliminary"を渡すのでexpected_final_bodyは必ず新規計算になる）。
            if not closable:
                continue
            expected = expected_final_body(billing_month, layers, daily_by_date, meter_read_day, "preliminary", None)
            if expected is None:
                continue
            if existing["revision"] >= MAX_REVISION:
                raise RevisionLimitExceededError(
                    f"{billing_month}: revisionが上限({MAX_REVISION})に達したため書き込みません"
                )
            snapshot = {
                **expected,
                "first_published": existing["first_published"],
                "revision": existing["revision"] + 1,
                "revised": today.isoformat(),
                # 速報からの確定遷移の目印（QA指摘2026-09-26 item10）。
                "transitioned_from_preliminary": True,
            }
            _write_snapshot(path, snapshot)
            continue

        # 新規（この請求月のposts/*.jsonがまだ無い）
        if closable:
            expected = expected_final_body(billing_month, layers, daily_by_date, meter_read_day, None, None)
            if expected is None:
                continue
            snapshot = {
                **expected, "first_published": today.isoformat(), "revision": 1, "revised": None,
                "transitioned_from_preliminary": False,
            }
            _write_snapshot(path, snapshot)
            written += 1
            continue

        prelim_rec = preliminary_by_key.get(billing_month)
        if prelim_rec is None:
            continue
        period_end = date.fromisoformat(prelim_rec["usage_period"]["end"])
        if today < period_end + timedelta(days=PRELIMINARY_DELAY_DAYS):
            continue
        body = build_snapshot_body(billing_month, layers, daily_by_date, "preliminary", meter_read_day)
        if body is None:
            continue
        snapshot = {
            **body, "first_published": today.isoformat(), "revision": 1, "revised": None,
            "transitioned_from_preliminary": False,
        }
        _write_snapshot(path, snapshot)
        written += 1

    return written


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--data-dir", type=Path, required=True, help="layers.json/daily.json のあるディレクトリ")
    parser.add_argument("--posts-dir", type=Path, required=True, help="posts/YYYY-MM.json の出力先")
    parser.add_argument("--tariff", type=Path, default=bill_model.DEFAULT_TARIFF_PATH, help="tariff.json のパス（meter_read_dayの取得用）")
    parser.add_argument("--today", type=str, default=None, help="基準日('YYYY-MM-DD'、省略時は実行日）")
    parser.add_argument(
        "--first-report-month", type=str, default=FIRST_REPORT_BILLING_MONTH,
        help=f"この請求月より前は対象にしない（既定: {FIRST_REPORT_BILLING_MONTH}）",
    )
    args = parser.parse_args()

    tariff = json.loads(args.tariff.read_text(encoding="utf-8"))
    meter_read_day = tariff["meter_read_day"]
    today = date.fromisoformat(args.today) if args.today else date.today()
    try:
        written = run(
            args.data_dir, args.posts_dir, today,
            meter_read_day=meter_read_day, first_report_billing_month=args.first_report_month,
        )
    except RevisionLimitExceededError as exc:
        print(f"monthly_report.py: {exc}", file=sys.stderr)
        sys.exit(1)
    print(f"monthly_report.py: wrote {written} new post(s) under {args.posts_dir}")


if __name__ == "__main__":
    main()
