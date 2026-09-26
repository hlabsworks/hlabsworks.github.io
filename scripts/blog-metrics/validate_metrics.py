#!/usr/bin/env python3
"""validate_metrics.py — 別リポジトリ hlabsworks/solar-metrics-data（homelab が毎日 push する
公開メトリクスデータ）から取り込む内容を、Hugo ビルドに反映する前に検査するゲート集
(G1〜G13)。

必ず main ブランチのこのファイル（信頼されたコード）で実行し、_incoming 側からは JSON/
Markdown のテキストしか読まない。ゲートを1つでも満たさない場合は非ゼロ終了し、
.github/workflows/hugo.yml のビルド・デプロイを止める。

使い方:
  python3 scripts/blog-metrics/validate_metrics.py --incoming _incoming --repo .
  python3 scripts/blog-metrics/validate_metrics.py --incoming _incoming --repo . --allow-history-change

ゲート一覧（詳細は各 gate_* 関数のdocstring）:
  G1  ファイルallowlist       G2  UTF-8/BOM/制御文字/改行     G3  サイズ・行数上限
  G4  キーallowlist(再帰)     G5  文字列フォーマット           G6  秘匿情報deny
  G7  時間帯粒度の禁止        G8  日付健全性                   G9  履歴不変性
  G10 物理レンジ              G11 前回比異常                   G12 整合性チェック
  G13 pipeline.json（警告のみ）
"""
from __future__ import annotations

import argparse
import hashlib
import json
import re
import statistics
import subprocess
import sys
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

JST = timezone(timedelta(hours=9))

# --- G1: ファイル allowlist -------------------------------------------------
DATA_FILES = {
    "data/metrics/daily.json",
    "data/metrics/monthly.json",
    "data/metrics/meta.json",
    "data/metrics/bills.json",
    "data/metrics/layers.json",
}
ALLOWED_FILES = DATA_FILES | {"README.md", "pipeline.json"}

# --- G3: サイズ・行数上限 ---------------------------------------------------
MAX_FILE_BYTES = 1 * 1024 * 1024
MAX_TOTAL_BYTES = 4 * 1024 * 1024
MAX_DAILY_ROWS = 4000
MAX_MONTHLY_ROWS = 240
MAX_LAYERS_MONTHS_ROWS = 240
MAX_LAYERS_DAILY_ROWS = 4000

# --- G4: キー allowlist（再帰・ハードコード） -------------------------------
# QA指摘#1: 以前は「現行ファイルから収穫」していたが、data/metrics/layers.json が
# 全月unavailableな退化スナップショットだったため、確定月available(full/scaled)の
# 正常系キー10個(billing_months/days_total/days_usable/estimation/
# includes_scaled_months/net_cost_fit_yen_max/net_cost_fit_yen_min/note/
# saving_yen_fit/scale_factor)が欠落していた。now layer_model.py/bill_model.py を
# 合成プロファイルで実走させた出力(scripts/blog-metrics/testdata/{layers,bills}_fixture.json、
# 生成手順は同ファイルのコメント参照)から収穫し、収穫元自体をテストで固定する
# （test_validate_metrics.py の test_layers_json_with_available_month_passes_gate4）。
DAILY_ROW_KEYS = {
    "date", "solar_kwh", "buy_kwh", "sell_kwh",
    "nichicon_charge_kwh", "ecoflow_charge_kwh",
    "self_consumption_shift_kwh", "saving_yen",
}
MONTHLY_ROW_KEYS = {
    "month", "solar_kwh", "buy_kwh", "sell_kwh",
    "nichicon_charge_kwh", "ecoflow_charge_kwh",
    "self_consumption_shift_kwh", "saving_yen",
}
META_KEYS = {
    "generated_at", "buy_price_yen_per_kwh", "sell_price_yen_per_kwh",
    "buy_sell_price_effective_month", "buy_sell_price_source",
    "ecoflow_data_since", "nichicon_data_since", "power_history_since",
    "publish_since",
}
BILLS_KEYS = {
    "_note", "area", "basic_fee_yen", "bill_actual", "bill_l0_no_solar",
    "billing_month", "buy_diff_pct", "buy_kwh", "buy_kwh_sensor", "buy_source",
    "capacity_contribution_yen", "days", "end", "energy_charge_yen", "excluded",
    "excluded_months", "fuel_adjustment_yen", "generated_at", "l0_unavailable_reason",
    "months", "plan", "reason_code", "reason_detail", "reason_label",
    "renewable_levy_yen", "retailer", "saving_yen_fit", "saving_yen_post_fit",
    "sell_diff_pct", "sell_kwh", "sell_kwh_official", "sell_revenue_fit_yen",
    "sell_revenue_post_fit_yen", "sell_source", "solar_kwh", "start",
    "tariff_source", "total_yen", "usage_period",
}
LAYERS_KEYS = {
    "L0", "L1", "L1S", "L2", "L3", "_note", "_source", "available", "basic_fee_yen",
    "battery_charge_kwh_per_100soc", "battery_discharge_kwh_per_100soc",
    "battery_max_discharge_kw", "bill", "billing_month", "billing_months",
    "boundary_storage_kwh", "buy_kwh", "buy_source", "capacity_contribution_yen",
    "coverage", "cumulative", "daily", "date", "days", "days_covered",
    "days_elapsed", "days_total", "days_usable", "end", "energy_charge_yen",
    "estimation", "excluded_dates", "excluded_months", "fuel_adjustment_yen",
    "generated_at", "in_progress", "includes_scaled_months", "interpolated_buckets",
    "interpolated_slots", "kind", "layer_reasons", "layers", "max_export_w",
    "month_usable_fraction_threshold", "months", "months_included",
    "net_cost_fit_yen", "net_cost_fit_yen_max", "net_cost_fit_yen_min",
    "net_cost_post_fit_yen", "nichicon_soc_end_pct", "nichicon_soc_start_pct",
    "note", "params", "period_end_actual", "profile_rows_since",
    "profile_since", "profile_source", "pv_ac_efficiency", "pv_capacity_kw",
    "reason_code", "reason_detail", "reason_label", "renewable_levy_yen",
    "saving_yen_fit", "scale_factor", "sell_kwh", "sell_price_yen_per_kwh_fit",
    "sell_price_yen_per_kwh_post_fit", "sell_source", "soc_end_pct",
    "soc_start_pct", "source", "start", "status", "tariff_basis",
    "tariff_provisional", "tariff_source_month", "total_yen",
    "unavailable_reason", "uncertainty", "usage_period",
    # L1S（太陽光＋SolarChargeController、家庭用蓄電池なし試算。DDR §5.7）
    "l1s_model", "l1s_replay", "ac_in_error_pct", "buy_error_pct", "sell_error_pct",
    "unit_count", "capacity_kwh_nominal", "capacity_kwh_effective", "charge_efficiency",
    "discharge_efficiency", "idle_w_per_unit", "tracking_margin_w", "speedup_threshold_w",
    "full_soc_pct", "emergency_soc_pct", "emergency_exit_soc_pct",
    "replay_tolerance_ac_in_pct", "replay_tolerance_buy_pct", "replay_tolerance_sell_pct",
    "calibrated_on",
}
PIPELINE_KEYS = {
    "schema_version", "generated_at", "source", "bundle_rev", "profile_window_days",
    "inputs", "tariff_sha256", "official_buy_sha256", "official_sell_sha256",
    "row_counts", "daily", "monthly", "db_query_seconds",
}

# --- G5: 文字列フォーマット allowlist ---------------------------------------
_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
_MONTH_RE = re.compile(r"^\d{4}-\d{2}$")
_DATETIME_RE = re.compile(r"^\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}$")
DATE_VALUE_KEYS = {
    "date", "start", "end", "period_end_actual",
    "profile_since", "profile_rows_since",
    "power_history_since", "nichicon_data_since", "ecoflow_data_since",
    "publish_since", "calibrated_on",
}
MONTH_VALUE_KEYS = {"month", "billing_month", "buy_sell_price_effective_month", "tariff_source_month"}

# --- G6: 秘匿情報 deny（生テキストに正規表現） ------------------------------
# sha256ダイジェスト（pipeline.json.inputs.*_sha256、64桁16進）は一方向ハッシュで
# 秘匿情報ではないが、ランダムな16進文字列は高確率で13桁以上の数字の連続を含み
# \d{13,} 誤検知するため、deny判定前に無害な固定文字列へ置換する。
_HEX64_RE = re.compile(r"\b[0-9a-fA-F]{64}\b")

_DENY_PATTERNS = [
    re.compile(r"(?i)(bearer |authorization|password|passwd|secret|token|api[_-]?key|BEGIN [A-Z ]*PRIVATE KEY)"),
    re.compile(r"\b[0-9A-Fa-f]{2}(:[0-9A-Fa-f]{2}){5}\b"),  # MAC アドレス
    # 私設IP(RFC1918)に加え、IANA予約のドキュメント用範囲(RFC5737 TEST-NET-1/2/3)も対象にする。
    # TEST-NET自体は公開されても実害の無いアドレスだが、ドキュメント/テストで実IPの代わりに
    # 使うべき値であり、これも検出できることで「実IPをうっかりコミットに残す」事故への
    # 防御になる（QA指摘F7: テストfixtureの実IP風文字列の置き換え先として使う）。
    re.compile(
        r"\b(?:10\.\d{1,3}\.\d{1,3}\.\d{1,3}"
        r"|192\.168\.\d{1,3}\.\d{1,3}"
        r"|172\.(?:1[6-9]|2\d|3[0-1])\.\d{1,3}\.\d{1,3}"
        r"|192\.0\.2\.\d{1,3}"
        r"|198\.51\.100\.\d{1,3}"
        r"|203\.0\.113\.\d{1,3})\b"
    ),
    re.compile(r"\b\d{13,}\b"),  # 13桁以上の数字（電話番号・契約番号等）
    re.compile(r"\b[A-Z][A-Z0-9]{9,}\b"),  # S/N 様の大文字英数字列
    re.compile(r"[\w.+-]+@[\w-]+\.[\w.-]+"),  # メールアドレス
    # 住所様。「都市ガス」「京都市」等の一般語誤検知を避けるため、都道府県文字と市区町村文字の
    # 間に1文字以上(区切り文字・句読点以外)を要求し、市区町村字の直後に「ガ」「場」が続く場合
    # (「都市ガス」「市場」等)は除外する（QA指摘F11）。
    re.compile(r"\d+丁目|\d+番地|[都道府県][^\s、。]{1,8}[市区町村](?![ガ場])"),
    # 小売電気事業者名・送配電会社名・料金プラン名（居住地域の推定材料になるため、オーナー決定
    # 2026-09-23で公開側は「電力会社」「契約中の新電力」等の一般名詞に統一した）。
    # 全国の一般送配電事業者・主要小売事業者・代表的プラン名を網羅的に列挙する。特定社名だけを
    # 挙げるとこの deny リスト自体が契約先の手掛かりになるため、意図的に広く取る。
    re.compile(
        r"(?i)(北海道電力|東北電力|東京電力|中部電力|北陸電力|関西電力|中国電力|四国電力|九州電力|沖縄電力"
        r"|TEPCO|KEPCO|CHUDEN|HEPCO|TOHOKU-EPCO|RIKUDEN|ENERGIA|YONDEN|KYUDEN|OKIDEN"
        r"|パワーグリッド|ネットワーク株式会社|送配電"
        r"|Japan電力|japaden|楽天でんき|ENEOSでんき|Looop|ハチドリ|オクトパス|CDエナジー|東京ガス|大阪ガス"
        r"|auでんき|ソフトバンクでんき|ドコモでんき|エルピオ|ミツウロコ|シン・エナジー|ナンワエナジー|HTBエナジー|idemitsu"
        r"|くらしプラン|従量電灯|スマートライフ|おうちプラン|なっトクプラン|ポイントプラン|スマートでんき|ベーシックプラン"
        r"|とくとくプラン|プレミアムプラン|eスマート|はぴeタイム|でんき\S{0,4}プラン)"
    ),
]

# --- G7: 時間帯粒度の禁止 ----------------------------------------------------
_TIME_OF_DAY_RE = re.compile(r"\d{1,2}:\d{2}")


class ValidationFailure(Exception):
    """1つのゲート違反。gate: 'G1'〜'G13' / message: 違反箇所を含む説明。"""

    def __init__(self, gate: str, message: str) -> None:
        self.gate = gate
        self.message = message
        super().__init__(f"{gate}: {message}")


def _list_tracked_files(incoming: Path) -> list[str]:
    """incoming ディレクトリの追跡ファイル一覧を相対パスで返す。
    git リポジトリなら `git ls-files`（未追跡ファイルを無視）、
    そうでなければ .git を除くファイルツリーを列挙する（単体テスト用）。"""
    if (incoming / ".git").exists():
        result = subprocess.run(
            ["git", "-C", str(incoming), "ls-files"],
            capture_output=True, text=True, check=True,
        )
        return [line for line in result.stdout.splitlines() if line]
    files = []
    for path in incoming.rglob("*"):
        if path.is_file() and ".git" not in path.relative_to(incoming).parts:
            files.append(str(path.relative_to(incoming)))
    return sorted(files)


def _previous_commit_text(incoming: Path, relpath: str) -> str | None:
    """incoming の HEAD~1 時点の relpath の内容を返す。取得できなければ None
    （最初のコミット・git リポジトリでない等）。"""
    if not (incoming / ".git").exists():
        return None
    result = subprocess.run(
        ["git", "-C", str(incoming), "show", f"HEAD~1:{relpath}"],
        capture_output=True, text=True,
    )
    if result.returncode != 0:
        return None
    return result.stdout


def gate1_file_allowlist(incoming: Path) -> None:
    """G1: incoming の追跡ファイルが ALLOWED_FILES と完全一致すること。"""
    tracked = set(_list_tracked_files(incoming))
    unexpected = sorted(tracked - ALLOWED_FILES)
    missing = sorted(ALLOWED_FILES - tracked)
    if unexpected:
        raise ValidationFailure("G1", f"許可されていないファイルが含まれています: {unexpected}")
    if missing:
        raise ValidationFailure("G1", f"必須ファイルが不足しています: {missing}")


def gate2_encoding(path: Path, relpath: str) -> str:
    """G2: UTF-8・BOMなし・制御文字なし・末尾改行。デコード後のテキストを返す。"""
    raw = path.read_bytes()
    if raw.startswith(b"\xef\xbb\xbf"):
        raise ValidationFailure("G2", f"{relpath}: BOM が含まれています")
    try:
        text = raw.decode("utf-8", errors="strict")
    except UnicodeDecodeError as exc:
        raise ValidationFailure("G2", f"{relpath}: UTF-8 として不正です ({exc})") from exc
    for i, ch in enumerate(text):
        if ord(ch) < 0x20 and ch != "\n":
            raise ValidationFailure("G2", f"{relpath}: 制御文字 (0x{ord(ch):02x}) が含まれています (index={i})")
    if not text.endswith("\n"):
        raise ValidationFailure("G2", f"{relpath}: 末尾改行がありません")
    return text


def gate3_size_and_rows(incoming: Path, relpath: str, raw_bytes: int, total_bytes: int, data: object) -> None:
    """G3: 個別ファイル ≤1MiB・合計 ≤4MiB・行数上限。"""
    if raw_bytes > MAX_FILE_BYTES:
        raise ValidationFailure("G3", f"{relpath}: サイズが上限({MAX_FILE_BYTES}bytes)を超えています ({raw_bytes}bytes)")
    if total_bytes > MAX_TOTAL_BYTES:
        raise ValidationFailure("G3", f"incoming 合計サイズが上限({MAX_TOTAL_BYTES}bytes)を超えています ({total_bytes}bytes)")
    if relpath == "data/metrics/daily.json" and len(data) > MAX_DAILY_ROWS:
        raise ValidationFailure("G3", f"{relpath}: 行数が上限({MAX_DAILY_ROWS})を超えています ({len(data)})")
    if relpath == "data/metrics/monthly.json" and len(data) > MAX_MONTHLY_ROWS:
        raise ValidationFailure("G3", f"{relpath}: 行数が上限({MAX_MONTHLY_ROWS})を超えています ({len(data)})")
    if relpath == "data/metrics/layers.json":
        months = data.get("months", []) if isinstance(data, dict) else []
        daily = data.get("daily", []) if isinstance(data, dict) else []
        if len(months) > MAX_LAYERS_MONTHS_ROWS:
            raise ValidationFailure("G3", f"{relpath}: months 行数が上限({MAX_LAYERS_MONTHS_ROWS})を超えています ({len(months)})")
        if len(daily) > MAX_LAYERS_DAILY_ROWS:
            raise ValidationFailure("G3", f"{relpath}: daily 行数が上限({MAX_LAYERS_DAILY_ROWS})を超えています ({len(daily)})")


def _walk_keys(obj: object, relpath: str, allowlist: set[str]) -> None:
    """G4: obj 内に現れる全 dict キーが allowlist に含まれることを再帰的に確認する。"""
    if isinstance(obj, dict):
        for key, value in obj.items():
            if key not in allowlist:
                raise ValidationFailure("G4", f"{relpath}: 許可されていないキーです: {key!r}")
            _walk_keys(value, relpath, allowlist)
    elif isinstance(obj, list):
        for item in obj:
            _walk_keys(item, relpath, allowlist)


def _walk_string_values(obj: object, relpath: str, key: str | None, visit) -> None:
    """JSON ツリーを再帰し、(key, str値) の組ごとに visit(key, value, relpath) を呼ぶ。"""
    if isinstance(obj, dict):
        for k, v in obj.items():
            _walk_string_values(v, relpath, k, visit)
    elif isinstance(obj, list):
        for item in obj:
            _walk_string_values(item, relpath, key, visit)
    elif isinstance(obj, str):
        visit(key, obj, relpath)


def gate5_string_format(data: object, relpath: str) -> None:
    """G5: 日付/月/generated_at は固定フォーマットのみ許可する。"""

    def visit(key, value, rel):
        if key == "generated_at":
            if not _DATETIME_RE.match(value):
                raise ValidationFailure("G5", f"{rel}: generated_at の書式が不正です: {value!r}")
        elif key in DATE_VALUE_KEYS:
            if not _DATE_RE.match(value):
                raise ValidationFailure("G5", f"{rel}: {key} の書式が不正です（日付が必要）: {value!r}")
        elif key in MONTH_VALUE_KEYS:
            if not _MONTH_RE.match(value):
                raise ValidationFailure("G5", f"{rel}: {key} の書式が不正です（月が必要）: {value!r}")

    _walk_string_values(data, relpath, None, visit)


def gate6_secret_deny(text: str, relpath: str) -> None:
    """G6: 秘匿情報・個人情報・端末識別情報らしき文字列を生テキストに対して検査する。"""
    sanitized = _HEX64_RE.sub("sha256digest", text)
    for pattern in _DENY_PATTERNS:
        m = pattern.search(sanitized)
        if m:
            raise ValidationFailure("G6", f"{relpath}: 秘匿/個人情報の疑いがある文字列を検出しました: {m.group(0)!r}")


def gate7_no_time_of_day(data: object, relpath: str) -> None:
    """G7: generated_at 以外の全文字列値・キー名に時刻粒度(H:MM)が含まれないこと。"""

    def visit(key, value, rel):
        if key == "generated_at":
            return
        if _TIME_OF_DAY_RE.search(value):
            raise ValidationFailure("G7", f"{rel}: {key} の値に時間帯粒度らしき文字列が含まれています: {value!r}")

    _walk_string_values(data, relpath, None, visit)

    def walk_keys(obj):
        if isinstance(obj, dict):
            for k, v in obj.items():
                if _TIME_OF_DAY_RE.search(k):
                    raise ValidationFailure("G7", f"{relpath}: キー名に時間帯粒度らしき文字列が含まれています: {k!r}")
                walk_keys(v)
        elif isinstance(obj, list):
            for item in obj:
                walk_keys(item)

    walk_keys(data)


def gate8_date_health(daily: list[dict], incoming: Path, allow_history_change: bool = False) -> None:
    """G8: daily.date が昇順・重複なし・未来日なし（JST）。前コミットより最終日が
    過去／行数が減少していないこと（前コミットが無ければスキップ）。

    行数減少チェックのみ --allow-history-change でスキップできる（最終日の後退は
    allow_history_change の有無に関わらず常に拒否する。過去日の切り捨てだけを許可する
    運用のため）。オーナー決定2026-09-23: publish_since 導入で公開範囲の下限日を初めて
    設定する回だけ daily.json の行数が意図的に減る。以後は publish_since が動かないため
    再発しない一度限りの移行措置。"""
    dates = [row["date"] for row in daily]
    if dates != sorted(dates):
        raise ValidationFailure("G8", "data/metrics/daily.json: date が昇順ではありません")
    if len(dates) != len(set(dates)):
        raise ValidationFailure("G8", "data/metrics/daily.json: date が重複しています")
    today_jst = datetime.now(JST).date()
    for d in dates:
        if date.fromisoformat(d) > today_jst:
            raise ValidationFailure("G8", f"data/metrics/daily.json: 未来日が含まれています: {d}")

    prev_text = _previous_commit_text(incoming, "data/metrics/daily.json")
    if prev_text is None:
        return
    prev_daily = json.loads(prev_text)
    if not prev_daily:
        return
    prev_last_date = prev_daily[-1]["date"]
    if dates and dates[-1] < prev_last_date:
        raise ValidationFailure("G8", f"data/metrics/daily.json: 最終日が前コミットより過去に後退しています ({dates[-1]} < {prev_last_date})")
    if not allow_history_change and len(dates) < len(prev_daily):
        raise ValidationFailure("G8", f"data/metrics/daily.json: 行数が前コミットより減少しています ({len(dates)} < {len(prev_daily)})")


def gate9_history_immutability(daily: list[dict], incoming: Path, allow_history_change: bool) -> None:
    """G9: today-20日以前の daily 行は前コミットと値まで一致すること。
    --allow-history-change でスキップ可能。"""
    if allow_history_change:
        return
    prev_text = _previous_commit_text(incoming, "data/metrics/daily.json")
    if prev_text is None:
        return
    prev_by_date = {row["date"]: row for row in json.loads(prev_text)}
    today_jst = datetime.now(JST).date()
    cutoff = today_jst - timedelta(days=20)
    for row in daily:
        d = date.fromisoformat(row["date"])
        if d > cutoff:
            continue
        prev_row = prev_by_date.get(row["date"])
        if prev_row is None:
            continue
        if row != prev_row:
            raise ValidationFailure("G9", f"data/metrics/daily.json: 確定済み日({row['date']})の値が前コミットと異なります（--allow-history-change が必要）")


def _check_range(value, low, high, relpath, label) -> None:
    if value is None:
        return
    if not (low <= value <= high):
        raise ValidationFailure("G10", f"{relpath}: {label} が物理レンジ外です: {value} (許容 {low}〜{high})")


def gate10_physical_range(daily: list[dict], monthly: list[dict]) -> None:
    """G10: kWh/円の値が物理的にありえない範囲でないこと。月次は日次の上限×31。"""
    for row in daily:
        _check_range(row.get("solar_kwh"), 0, 100, "data/metrics/daily.json", f"{row['date']} solar_kwh")
        _check_range(row.get("buy_kwh"), 0, 200, "data/metrics/daily.json", f"{row['date']} buy_kwh")
        _check_range(row.get("sell_kwh"), 0, 100, "data/metrics/daily.json", f"{row['date']} sell_kwh")
        _check_range(row.get("nichicon_charge_kwh"), 0, 30, "data/metrics/daily.json", f"{row['date']} nichicon_charge_kwh")
        _check_range(row.get("ecoflow_charge_kwh"), 0, 40, "data/metrics/daily.json", f"{row['date']} ecoflow_charge_kwh")
        saving = row.get("saving_yen")
        if saving is not None and abs(saving) > 20000:
            raise ValidationFailure("G10", f"data/metrics/daily.json: {row['date']} saving_yen が物理レンジ外です: {saving}")
    for row in monthly:
        _check_range(row.get("solar_kwh"), 0, 100 * 31, "data/metrics/monthly.json", f"{row['month']} solar_kwh")
        _check_range(row.get("buy_kwh"), 0, 200 * 31, "data/metrics/monthly.json", f"{row['month']} buy_kwh")
        _check_range(row.get("sell_kwh"), 0, 100 * 31, "data/metrics/monthly.json", f"{row['month']} sell_kwh")
        _check_range(row.get("nichicon_charge_kwh"), 0, 30 * 31, "data/metrics/monthly.json", f"{row['month']} nichicon_charge_kwh")
        _check_range(row.get("ecoflow_charge_kwh"), 0, 40 * 31, "data/metrics/monthly.json", f"{row['month']} ecoflow_charge_kwh")
        saving = row.get("saving_yen")
        if saving is not None and abs(saving) > 20000 * 31:
            raise ValidationFailure("G10", f"data/metrics/monthly.json: {row['month']} saving_yen が物理レンジ外です: {saving}")


def gate11_anomaly(daily: list[dict], monthly: list[dict], incoming: Path) -> None:
    """G11: 新規日の solar_kwh が直近30日中央値の3倍超、monthly 最終月 saving_yen が
    前月比±10倍超で fail。"""
    prev_text = _previous_commit_text(incoming, "data/metrics/daily.json")
    if prev_text is not None:
        prev_dates = {row["date"] for row in json.loads(prev_text)}
        new_rows = [row for row in daily if row["date"] not in prev_dates]
        by_date = {row["date"]: row for row in daily}
        sorted_dates = sorted(by_date)
        for new_row in new_rows:
            idx = sorted_dates.index(new_row["date"])
            window_dates = sorted_dates[max(0, idx - 30):idx]
            window_values = [by_date[d]["solar_kwh"] for d in window_dates if by_date[d].get("solar_kwh") is not None]
            if len(window_values) < 5:
                continue
            median = statistics.median(window_values)
            solar = new_row.get("solar_kwh")
            if median > 0 and solar is not None and solar > median * 3:
                raise ValidationFailure("G11", f"data/metrics/daily.json: {new_row['date']} の solar_kwh が直近30日中央値の3倍を超えています ({solar} > {median}*3)")

    if len(monthly) >= 2:
        last, prev = monthly[-1], monthly[-2]
        last_saving, prev_saving = last.get("saving_yen"), prev.get("saving_yen")
        if last_saving is not None and prev_saving not in (None, 0):
            ratio = last_saving / prev_saving
            if ratio > 10 or ratio < (1 / 10):
                raise ValidationFailure("G11", f"data/metrics/monthly.json: {last['month']} の saving_yen が前月比10倍を超えて変化しています ({prev_saving} -> {last_saving})")


def gate12_consistency(daily: list[dict], monthly: list[dict], meta: dict) -> None:
    """G12: monthly の各月 solar_kwh が daily の月内合計と一致すること（1e-6）。
    meta.power_history_since は daily 最小日以下、nichicon/ecoflow_data_since は
    それぞれのカラムが非null になる最小日以下であること
    （※ nichicon/ecoflow は power_history より後に計測を始めているため、単純に
    「daily 全体の最小日」と比較すると必ず矛盾する。実データで検証済みの、
    列ごとの最小 non-null 日と比較する解釈を採用— ASSUMED、詳細は runbook 参照）。"""
    daily_solar_by_month: dict[str, float] = {}
    for row in daily:
        month = row["date"][:7]
        if row.get("solar_kwh") is not None:
            daily_solar_by_month[month] = daily_solar_by_month.get(month, 0.0) + row["solar_kwh"]
    for row in monthly:
        expected = daily_solar_by_month.get(row["month"])
        actual = row.get("solar_kwh")
        if expected is None or actual is None:
            continue
        if abs(round(expected, 3) - actual) > 1e-6:
            raise ValidationFailure("G12", f"data/metrics/monthly.json: {row['month']} solar_kwh が daily の合計と一致しません (monthly={actual}, daily和={round(expected, 3)})")

    if not daily:
        return
    min_date = min(row["date"] for row in daily)
    if meta.get("power_history_since") and meta["power_history_since"] > min_date:
        raise ValidationFailure("G12", f"data/metrics/meta.json: power_history_since({meta['power_history_since']}) が daily 最小日({min_date})より後です")

    for meta_key, column in (("nichicon_data_since", "nichicon_charge_kwh"), ("ecoflow_data_since", "ecoflow_charge_kwh")):
        since = meta.get(meta_key)
        if not since:
            continue
        column_dates = [row["date"] for row in daily if row.get(column) is not None]
        if not column_dates:
            continue
        if since > min(column_dates):
            raise ValidationFailure("G12", f"data/metrics/meta.json: {meta_key}({since}) が daily 側の最小計測日({min(column_dates)})より後です")


def default_input_paths(repo: Path) -> dict[str, Path]:
    """--repo（Hugoリポジトリのフルチェックアウト）を前提にした、G13入力ハッシュ照合先の
    既定パス。homelab の /opt/blog-metrics/ のようなフラットなbundleレイアウトでは
    validate()/main() の override 引数で個別に差し替える。"""
    return {
        "tariff_sha256": repo / "scripts" / "blog-metrics" / "tariff.json",
        "official_buy_sha256": repo / "data" / "metrics" / "official_buy.json",
        "official_sell_sha256": repo / "data" / "metrics" / "official_sell.json",
    }


def gate13_pipeline(pipeline: dict, input_paths: dict[str, Path]) -> list[str]:
    """G13: pipeline.json のスキーマ・鮮度・入力ハッシュ。鮮度・ハッシュ不一致は
    警告のみ（GitHub Actions の ::warning:: 形式で返す。fatalにしない）。"""
    warnings: list[str] = []
    if pipeline.get("schema_version") != 1:
        raise ValidationFailure("G13", f"pipeline.json: schema_version が 1 ではありません: {pipeline.get('schema_version')!r}")

    generated_at = pipeline.get("generated_at")
    if generated_at:
        try:
            generated_dt = datetime.strptime(generated_at, "%Y-%m-%d %H:%M:%S").replace(tzinfo=JST)
            age = datetime.now(JST) - generated_dt
            # run-daily.sh はデータに実質的な変更が無い日はcommitしない方針(pipeline.jsonも
            # 更新しない)のため、鮮度閾値は1日3回実行の間隔より十分長い8日にしている
            # （QA指摘F16。閾値を短くする代わりに「変更なし日もpipeline.jsonだけ更新して
            # commitする」案は、実質的な変更が無い日にnoise commitを作ることになるため採らない）。
            if age > timedelta(days=8):
                warnings.append(f"::warning::G13: pipeline.json の generated_at が8日以上前です ({generated_at})")
        except ValueError:
            warnings.append(f"::warning::G13: pipeline.json の generated_at の書式を解釈できません ({generated_at!r})")

    inputs = pipeline.get("inputs", {})
    for key, real_path in input_paths.items():
        expected = inputs.get(key)
        if not expected or not real_path.exists():
            continue
        actual = hashlib.sha256(real_path.read_bytes()).hexdigest()
        if actual != expected:
            warnings.append(f"::warning::G13: pipeline.json.inputs.{key} が main の実ファイルと一致しません ({real_path})")

    return warnings


def validate(
    incoming: Path,
    repo: Path,
    allow_history_change: bool,
    input_paths: dict[str, Path] | None = None,
) -> list[str]:
    """全ゲートを実行する。戻り値は警告メッセージのリスト（fatalではない）。
    fatal な違反があれば ValidationFailure を送出する。

    input_paths: G13入力ハッシュ照合先の override（省略時は --repo を Hugoリポジトリの
    フルチェックアウトとみなした既定パスを使う。homelab の run-daily.sh はフラットな
    bundle レイアウト(/opt/blog-metrics/inputs/*)向けに明示的に渡す）。"""
    gate1_file_allowlist(incoming)

    texts: dict[str, str] = {}
    total_bytes = 0
    for relpath in sorted(ALLOWED_FILES):
        path = incoming / relpath
        raw_bytes = path.stat().st_size
        total_bytes += raw_bytes
        texts[relpath] = gate2_encoding(path, relpath)

    parsed: dict[str, object] = {}
    for relpath in sorted(DATA_FILES | {"pipeline.json"}):
        try:
            parsed[relpath] = json.loads(texts[relpath])
        except json.JSONDecodeError as exc:
            raise ValidationFailure("G2", f"{relpath}: JSON として不正です ({exc})") from exc

    for relpath in sorted(ALLOWED_FILES):
        gate3_size_and_rows(incoming, relpath, incoming.joinpath(relpath).stat().st_size, total_bytes, parsed.get(relpath))

    key_allowlists = {
        "data/metrics/meta.json": META_KEYS,
        "data/metrics/bills.json": BILLS_KEYS,
        "data/metrics/layers.json": LAYERS_KEYS,
        "pipeline.json": PIPELINE_KEYS,
    }
    for relpath, allowlist in key_allowlists.items():
        _walk_keys(parsed[relpath], relpath, allowlist)
    for row in parsed["data/metrics/daily.json"]:
        _walk_keys(row, "data/metrics/daily.json", DAILY_ROW_KEYS)
    for row in parsed["data/metrics/monthly.json"]:
        _walk_keys(row, "data/metrics/monthly.json", MONTHLY_ROW_KEYS)

    for relpath in sorted(DATA_FILES | {"pipeline.json"}):
        gate5_string_format(parsed[relpath], relpath)
        gate7_no_time_of_day(parsed[relpath], relpath)

    for relpath in sorted(ALLOWED_FILES):
        gate6_secret_deny(texts[relpath], relpath)

    daily = parsed["data/metrics/daily.json"]
    monthly = parsed["data/metrics/monthly.json"]
    meta = parsed["data/metrics/meta.json"]

    gate8_date_health(daily, incoming, allow_history_change)
    gate9_history_immutability(daily, incoming, allow_history_change)
    gate10_physical_range(daily, monthly)
    gate11_anomaly(daily, monthly, incoming)
    gate12_consistency(daily, monthly, meta)

    resolved_input_paths = input_paths if input_paths is not None else default_input_paths(repo)
    return gate13_pipeline(parsed["pipeline.json"], resolved_input_paths)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--incoming", type=Path, required=True, help="別リポジトリ solar-metrics-data のチェックアウト先ディレクトリ")
    parser.add_argument("--repo", type=Path, required=True, help="このリポジトリ(main)のルートディレクトリ（G13入力ハッシュ照合の既定パス算出に使う）")
    parser.add_argument("--allow-history-change", action="store_true", help="G9(履歴不変性)をスキップする（workflow_dispatch の入力用）")
    parser.add_argument("--tariff-path", type=Path, default=None, help="G13でtariff_sha256と照合する実ファイルのパス（既定: --repo基準）")
    parser.add_argument("--official-buy-path", type=Path, default=None, help="G13でofficial_buy_sha256と照合する実ファイルのパス（既定: --repo基準）")
    parser.add_argument("--official-sell-path", type=Path, default=None, help="G13でofficial_sell_sha256と照合する実ファイルのパス（既定: --repo基準）")
    args = parser.parse_args()

    input_paths = default_input_paths(args.repo)
    if args.tariff_path is not None:
        input_paths["tariff_sha256"] = args.tariff_path
    if args.official_buy_path is not None:
        input_paths["official_buy_sha256"] = args.official_buy_path
    if args.official_sell_path is not None:
        input_paths["official_sell_sha256"] = args.official_sell_path

    try:
        warnings = validate(args.incoming, args.repo, args.allow_history_change, input_paths=input_paths)
    except ValidationFailure as exc:
        print(f"validate_metrics.py: {exc.gate}: {exc.message}", file=sys.stderr)
        sys.exit(1)

    for warning in warnings:
        print(warning)
    print("validate_metrics.py: OK（全ゲート通過）")


if __name__ == "__main__":
    main()
