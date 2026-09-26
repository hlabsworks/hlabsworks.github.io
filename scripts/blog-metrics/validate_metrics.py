#!/usr/bin/env python3
"""validate_metrics.py — 別リポジトリ hlabsworks/solar-metrics-data（homelab が毎日 push する
公開メトリクスデータ）から取り込む内容を、Hugo ビルドに反映する前に検査するゲート集
(G1〜G19)。

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
  G13 pipeline.json（inputs/*.json不在時は警告のみ、存在時はincomingと直接fatal照合）
  G14 posts/*.jsonのスキーマ・型   G15 posts/*.jsonの再計算一致・確定判定
  G16 posts/*.jsonの履歴整合       G17 render_monthly_posts.pyの自己検査（別プロセスで実行）
  G18 inputs/*.jsonのスキーマ・型（月次確定の自動化、DDR実装手順S1）
  G19 inputs/official_buy.jsonの請求突合（同上、確定済み月はreconcile_bill==0が必須）

posts/YYYY-MM.json（月締めレポートの数値スナップショット、設計判断2026-09-23/26
「速報＋改訂」方式）は monthly_report.py の is_closable()/build_snapshot_body() を
import して使う（layer_model.py と同じ流儀で sys.path.insert する）。
"""
from __future__ import annotations

import argparse
import hashlib
import json
import math
from collections import Counter
import re
import statistics
import subprocess
import sys
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import bill_model  # noqa: E402  billing_period()を二重実装しない(G14のusage_period検査用)
import import_official_buy  # noqa: E402  SOURCE_NOTEをG18で共有する(二重実装しない)
import import_official_sell  # noqa: E402  SOURCE_NOTEをG18で共有する(二重実装しない)
import monthly_report  # noqa: E402

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

# posts/YYYY-MM.json は0〜MAX_POST_FILES件（必須ファイルではない）。既存7ファイルとの
# 完全一致要求(ALLOWED_FILES)は変えず、posts/だけ別枠で数・命名パターンを検査する。
# QA指摘2026-09-26 item8: \d はUnicodeの数字(全角等)にもマッチするため、ASCII数字限定の
# re.ASCII を付ける。$ は文字列末尾の改行の直前にもマッチしうるため \Z にする。
POST_FILE_RE = re.compile(r"^posts/(\d{4}-\d{2})\.json\Z", re.ASCII)
MAX_POST_FILES = 240

# inputs/official_buy.json・inputs/official_sell.json・inputs/tariff_months.json（月次確定の
# 自動化、DDR §「月次確定の自動化」・実装手順S1）。0〜3件、固定ファイル名のみで任意
# （必須ファイルではない。handoffが無い運用ではこれまでどおり存在しない）。
OPTIONAL_INPUT_FILES = {"inputs/official_buy.json", "inputs/official_sell.json", "inputs/tariff_months.json"}

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
    "note", "params", "period_end_actual", "preliminary_months", "profile_rows_since",
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
    "tariff_months_sha256", "row_counts", "daily", "monthly", "db_query_seconds",
}

# --- G18: inputs/*.json のスキーマ(キー・型・範囲) -----------------------------------------
TARIFF_MONTHS_KEYS = {
    "schema_version", "generated_at", "fuel_cost_adjustment_yen_per_kwh",
    "capacity_contribution_yen_per_month", "renewable_levy_yen_per_kwh_observed",
    "excluded_months",
}
OFFICIAL_BUY_TOP_KEYS = {"months", "generated_at", "source_note"}
OFFICIAL_BUY_MONTH_KEYS = {"settlement_month", "period_from", "period_to", "official_buy_kwh", "billed_yen"}
OFFICIAL_SELL_TOP_KEYS = {"months", "generated_at", "source_note"}
OFFICIAL_SELL_MONTH_KEYS = {"settlement_month", "period_from", "period_to", "official_sell_kwh", "sell_revenue_yen"}
EXCLUDED_MONTH_REASONS = {"reconcile_mismatch", "levy_conflict", "parse_incomplete", "missing_official_buy", "tariff_conflict"}
_LEVY_RANGE_RE = re.compile(r"^(\d{4}-\d{2})\.\.(\d{4}-\d{2})\Z", re.ASCII)
MAX_INPUT_MONTHLY_ROWS = 240

# posts/YYYY-MM.json（月締めレポートのスナップショットv1）のキー allowlist（DDR §B・追補B'）。
POST_KEYS = {
    "schema_version", "billing_month", "report_month", "usage_period", "start", "end", "days",
    "first_published", "revision", "revised", "estimation", "days_usable", "days_total",
    "layers", "L0", "L1", "L2", "L3", "net_cost_fit_yen", "net_cost_post_fit_yen", "buy_kwh", "sell_kwh",
    "l2_band", "net_cost_fit_yen_min", "net_cost_fit_yen_max",
    "energy", "solar_kwh", "sell_kwh_sensor", "nichicon_charge_kwh", "ecoflow_charge_kwh",
    "weather", "sunny_days", "cloudy_days", "overcast_days", "unknown_days",
    "comparison", "prev_solar_kwh", "yoy_solar_kwh",
    "stage", "tariff_basis", "l3_source", "buy", "sell",
    "transitioned_from_preliminary",
}
# posts/*.jsonの文字列値は日付・月の正規表現か、以下の列挙値のどれかに限る（G14）。
POST_STRING_ENUM_VALUES = {"full", "scaled", "preliminary", "final", "provisional", "confirmed", "sensor", "billed", "official_meter"}
MAX_POST_BYTES = 16 * 1024

# --- G5: 文字列フォーマット allowlist ---------------------------------------
# QA指摘2026-09-26 item8: ASCII数字限定(re.ASCII)＋\Z（$は末尾改行の直前にもマッチしうる）。
_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}\Z", re.ASCII)
_MONTH_RE = re.compile(r"^\d{4}-\d{2}\Z", re.ASCII)
_DATETIME_RE = re.compile(r"^\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}\Z", re.ASCII)
DATE_VALUE_KEYS = {
    "date", "start", "end", "period_end_actual",
    "profile_since", "profile_rows_since",
    "power_history_since", "nichicon_data_since", "ecoflow_data_since",
    "publish_since", "calibrated_on",
    "first_published", "revised",
}
MONTH_VALUE_KEYS = {
    "month", "billing_month", "buy_sell_price_effective_month", "tariff_source_month",
    "report_month",
}

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
    # 区切り文字入りの供給地点特定番号様の数字列（例: '1234-5678-9012-3456'）。
    # 一般送配電事業者の供給地点特定番号は22桁を4桁区切りにした表記が使われることが
    # あるため、13桁未満の数字列(上のパターンでは検出できない)も区切り文字入りの
    # 塊が3個以上あれば検出する（月次確定の自動化、DDR実装手順S1、G6追加分）。
    # QA指摘L3: 区切り文字はASCIIハイフンだけでなく全角ハイフンマイナス(－)・
    # Unicodeハイフン(‐)・半角スペースも見る（表計算ソフトのコピペ・PDF抽出で
    # ハイフンが全角化したり空白区切りになることがあるため）。
    re.compile(r"\b\d{2,4}(?:[-－‐ ]\d{4}){3,}\b"),
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
    """1つのゲート違反。gate: 'G1'〜'G19' / message: 違反箇所を含む説明。"""

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


G11_MIN_DAYS_FOR_MONTHLY_RATIO = 20  # 前月比±10倍チェックを適用する最小日数（両月とも）

PREVIOUS_COMMIT_MAX_DEPTH = 10  # hugo.yml の _incoming checkout fetch-depth と揃える


def _previous_commit_json(incoming: Path, relpath: str) -> list | dict | None:
    """incoming の祖先コミット（HEAD~1, HEAD~2, …）のうち relpath が JSON として読める
    最新のものを返す。祖先に relpath が無い（最初のコミット・git リポジトリでない等）なら None。

    HEAD~1 だけを見ると、検証で拒否された壊れた push を revert した直後の CI が
    JSONDecodeError で落ちて復旧できない（N2 負テスト 2026-09-26 で発見）。読めない祖先は
    飛ばして次を見る。PREVIOUS_COMMIT_MAX_DEPTH 以内に読める祖先が無ければ比較対象なしとして
    None を返し stderr に警告する（G9 を迂回するには連続 MAX_DEPTH 回の壊れた push が要り、
    かつ書込鍵の保持者は既にゲート範囲内の数値を自由に書ける立場のため、脅威モデル上許容）。"""
    if not (incoming / ".git").exists():
        return None
    saw_unparseable = False
    for depth in range(1, PREVIOUS_COMMIT_MAX_DEPTH + 1):
        result = subprocess.run(
            ["git", "-C", str(incoming), "show", f"HEAD~{depth}:{relpath}"],
            capture_output=True, text=True,
        )
        if result.returncode != 0:
            break
        try:
            return json.loads(result.stdout)
        except json.JSONDecodeError:
            saw_unparseable = True
            continue
    if saw_unparseable:
        print(f"::warning::{relpath}: 直近{PREVIOUS_COMMIT_MAX_DEPTH}コミット内に JSON として読める前回値が無いため、前コミット比較(G8/G9/G11)をスキップします", file=sys.stderr)
    return None


def gate1_file_allowlist(incoming: Path) -> tuple[list[str], list[str]]:
    """G1: incoming の追跡ファイルが、既存7ファイル(ALLOWED_FILES、完全一致)＋
    posts/YYYY-MM.json（0〜MAX_POST_FILES件、命名パターン一致のみ要求）＋
    inputs/{official_buy,official_sell,tariff_months}.json（0〜3件、OPTIONAL_INPUT_FILES、
    月次確定の自動化。必須ファイルではない）で構成されること。
    戻り値: (見つかった posts/*.json の相対パスのリスト, 見つかった inputs/*.json の
    相対パスのリスト)。"""
    tracked = set(_list_tracked_files(incoming))
    post_files = sorted(p for p in tracked if p.startswith("posts/"))
    input_files = sorted(p for p in tracked if p in OPTIONAL_INPUT_FILES)
    non_post_tracked = tracked - set(post_files)
    invalid_post_files = sorted(p for p in post_files if not POST_FILE_RE.match(p))
    unexpected = sorted(non_post_tracked - ALLOWED_FILES - set(input_files)) + invalid_post_files
    missing = sorted(ALLOWED_FILES - non_post_tracked)
    if unexpected:
        raise ValidationFailure("G1", f"許可されていないファイルが含まれています: {unexpected}")
    if missing:
        raise ValidationFailure("G1", f"必須ファイルが不足しています: {missing}")
    if len(post_files) > MAX_POST_FILES:
        raise ValidationFailure("G1", f"posts/ のファイル数が上限({MAX_POST_FILES})を超えています ({len(post_files)})")
    return [p for p in post_files if p not in invalid_post_files], input_files


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

    prev_daily = _previous_commit_json(incoming, "data/metrics/daily.json")
    if prev_daily is None:
        return
    if not prev_daily:
        return
    prev_last_date = prev_daily[-1]["date"]
    if dates and dates[-1] < prev_last_date:
        raise ValidationFailure("G8", f"data/metrics/daily.json: 最終日が前コミットより過去に後退しています ({dates[-1]} < {prev_last_date})")
    if not allow_history_change and len(dates) < len(prev_daily):
        raise ValidationFailure("G8", f"data/metrics/daily.json: 行数が前コミットより減少しています ({len(dates)} < {len(prev_daily)})")


def gate9_history_immutability(
    daily: list[dict], incoming: Path, allow_history_change: bool,
    *, meter_read_day: int | None = None, newly_confirmed_months: set[str] | None = None,
) -> None:
    """G9: today-20日以前の daily 行は前コミットと値まで一致すること。
    --allow-history-change でスキップ可能。

    例外（月次確定の自動化、DDR実装手順S1「G9 の例外」、allow-history-once/手動dispatchを
    不要にする）: 行の日付が属する請求月(billing_month_for_date)が newly_confirmed_months
    （今回のtariff_months.json取り込みで新たに確定した請求月の集合）に含まれる場合は、
    前コミットとの差分キーが {"saving_yen"} の部分集合のときだけ許可する（tariff確定で
    暫定単価から実額に置き換わり saving_yen だけが動くケースを想定。他のキーが動く場合は
    従来どおり拒否する）。meter_read_day が None（未確定・inputs/tariff_months.json が
    incomingに無い等）なら例外は適用せず従来どおり厳密一致を要求する。"""
    if allow_history_change:
        return
    prev_daily = _previous_commit_json(incoming, "data/metrics/daily.json")
    if prev_daily is None:
        return
    prev_by_date = {row["date"]: row for row in prev_daily}
    today_jst = datetime.now(JST).date()
    cutoff = today_jst - timedelta(days=20)
    newly_confirmed_months = newly_confirmed_months or set()
    for row in daily:
        d = date.fromisoformat(row["date"])
        if d > cutoff:
            continue
        prev_row = prev_by_date.get(row["date"])
        if prev_row is None:
            continue
        if row == prev_row:
            continue
        if meter_read_day is not None and bill_model.billing_month_for_date(d, meter_read_day) in newly_confirmed_months:
            changed_keys = {k for k in set(row) | set(prev_row) if row.get(k) != prev_row.get(k)}
            if changed_keys <= {"saving_yen"}:
                continue
            raise ValidationFailure(
                "G9",
                f"data/metrics/daily.json: 確定済み日({row['date']})の値が前コミットと異なります"
                f"（新たに確定した請求月ですが saving_yen 以外も変化しています: {sorted(changed_keys)}）",
            )
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
    prev_daily = _previous_commit_json(incoming, "data/metrics/daily.json")
    if prev_daily is not None:
        prev_dates = {row["date"] for row in prev_daily}
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

    # 前月比は両月に十分な日数（daily.json の行数 ≥ G11_MIN_DAYS_FOR_MONTHLY_RATIO）がある
    # 場合だけ見る。publish_since 直後の月（例: 2026-08 は 8/29〜の3日分）は数値が小さく、
    # 翌月との比率が容易に10倍を超えて誤検知するため（初回 homelab 実行 2026-09-26 で発生）。
    days_per_month = Counter(row["date"][:7] for row in daily)
    if len(monthly) >= 2:
        last, prev = monthly[-1], monthly[-2]
        last_saving, prev_saving = last.get("saving_yen"), prev.get("saving_yen")
        enough_days = (days_per_month.get(last["month"], 0) >= G11_MIN_DAYS_FOR_MONTHLY_RATIO
                       and days_per_month.get(prev["month"], 0) >= G11_MIN_DAYS_FOR_MONTHLY_RATIO)
        if enough_days and last_saving is not None and prev_saving not in (None, 0):
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


_POST_REQUIRED_TOP_KEYS = {
    "schema_version", "billing_month", "report_month", "usage_period", "first_published",
    "revision", "revised", "estimation", "days_usable", "days_total", "layers", "l2_band",
    "energy", "weather", "comparison", "stage", "tariff_basis", "l3_source",
    "transitioned_from_preliminary",
}
_POST_LAYER_KEYS = {"net_cost_fit_yen", "net_cost_post_fit_yen", "buy_kwh", "sell_kwh"}
_POST_ENERGY_KEYS = {"solar_kwh", "sell_kwh_sensor", "nichicon_charge_kwh", "ecoflow_charge_kwh"}
_POST_WEATHER_KEYS = {"sunny_days", "cloudy_days", "overcast_days", "unknown_days"}
MAX_POST_YEN_ABS = 200_000
POST_KWH_RANGE = (0, 6200)


def _check_yen_value(value, relpath: str, label: str) -> None:
    if type(value) is not int:
        raise ValidationFailure("G14", f"{relpath}: {label} は int である必要があります: {value!r}")
    if abs(value) > MAX_POST_YEN_ABS:
        raise ValidationFailure("G14", f"{relpath}: {label} が物理レンジ外です: {value} (許容 ±{MAX_POST_YEN_ABS})")


def _check_kwh_value(value, relpath: str, label: str, *, nullable: bool) -> None:
    if value is None:
        if nullable:
            return
        raise ValidationFailure("G14", f"{relpath}: {label} が null です（必須）")
    if type(value) not in (int, float) or not math.isfinite(value):
        raise ValidationFailure("G14", f"{relpath}: {label} は有限の数値である必要があります: {value!r}")
    low, high = POST_KWH_RANGE
    if not (low <= value <= high):
        raise ValidationFailure("G14", f"{relpath}: {label} が物理レンジ外です: {value} (許容 {low}〜{high})")


def _require_dict(value, relpath: str, label: str) -> dict:
    if not isinstance(value, dict):
        raise ValidationFailure("G14", f"{relpath}: {label} は object である必要があります: {value!r}")
    return value


def _parse_iso_date(value, relpath: str, label: str) -> date:
    if not isinstance(value, str):
        raise ValidationFailure("G14", f"{relpath}: {label} は文字列である必要があります: {value!r}")
    try:
        return date.fromisoformat(value)
    except ValueError as exc:
        raise ValidationFailure("G14", f"{relpath}: {label} が実在する日付ではありません: {value!r} ({exc})") from exc


def gate14_post_schema(post: object, relpath: str, today_jst: date, meter_read_day: int) -> None:
    """G14: posts/YYYY-MM.json のスキーマ・型（DDR §D・追補D'、QA指摘2026-09-26で強化）。
    postがdict以外（Tracebackにしない）・必須キー・型（円は`type(v) is int`、kWhは
    int/floatかつmath.isfinite。json.loadsはNaNを通すため必須）・文字列は日付/月の
    正規表現(ASCII数字限定)か列挙値のみ・usage_periodがbill_model.billing_period()と
    完全一致・billing_monthがFIRST_REPORT_BILLING_MONTH以降・各種レンジを検査する。"""
    post = _require_dict(post, relpath, "post")

    missing = _POST_REQUIRED_TOP_KEYS - set(post)
    if missing:
        raise ValidationFailure("G14", f"{relpath}: 必須キーが不足しています: {sorted(missing)}")

    # QA再指摘2026-09-26 R1: type(x) is intでbool混入を排除する(True/Falseは共に1ではない)。
    if type(post.get("schema_version")) is not int or post.get("schema_version") != 1:
        raise ValidationFailure("G14", f"{relpath}: schema_version が1ではありません: {post.get('schema_version')!r}")

    # 文字列値は日付/月の正規表現(ASCII数字限定)か列挙値のどれかに限る
    # （自由文を型のレベルで禁止する。§B）。
    def _visit(key, value, rel):
        if _DATE_RE.match(value) or _MONTH_RE.match(value):
            return
        if value in POST_STRING_ENUM_VALUES:
            return
        raise ValidationFailure("G14", f"{rel}: {key} の値が日付/月/許可された列挙値のいずれでもありません: {value!r}")

    _walk_string_values(post, relpath, None, _visit)

    billing_month = post["billing_month"]
    if not isinstance(billing_month, str):
        raise ValidationFailure("G14", f"{relpath}: billing_month は文字列である必要があります: {billing_month!r}")
    if billing_month < monthly_report.FIRST_REPORT_BILLING_MONTH:
        raise ValidationFailure(
            "G14",
            f"{relpath}: billing_month({billing_month}) が FIRST_REPORT_BILLING_MONTH"
            f"({monthly_report.FIRST_REPORT_BILLING_MONTH}) より前です",
        )
    filename_month = Path(relpath).stem
    if filename_month != billing_month:
        raise ValidationFailure("G14", f"{relpath}: ファイル名の月({filename_month})とbilling_month({billing_month})が一致しません")

    usage_period = _require_dict(post["usage_period"], relpath, "usage_period")
    for key in ("start", "end", "days"):
        if key not in usage_period:
            raise ValidationFailure("G14", f"{relpath}: usage_period.{key} がありません")
    start_date = _parse_iso_date(usage_period["start"], relpath, "usage_period.start")
    end_date = _parse_iso_date(usage_period["end"], relpath, "usage_period.end")
    days = usage_period["days"]
    # QA再指摘2026-09-26 R1: type(x) is intでbool混入を排除する。
    if type(days) is not int:
        raise ValidationFailure("G14", f"{relpath}: usage_period.days は int である必要があります: {days!r}")
    if not (28 <= days <= 31):
        raise ValidationFailure("G14", f"{relpath}: usage_period.days が範囲外です: {days} (許容 28〜31)")
    if days != (end_date - start_date).days + 1:
        raise ValidationFailure("G14", f"{relpath}: usage_period.days が start/end と一致しません: {days}")
    # QA指摘2026-09-26 item3: usage_periodはbill_model.billing_period(billing_month,
    # meter_read_day)と完全一致すること（billing_monthを騙って別期間のusage_periodを
    # 埋め込むことを防ぐ）。
    try:
        expected_start, expected_end = bill_model.billing_period(billing_month, meter_read_day)
    except (KeyError, ValueError) as exc:
        raise ValidationFailure("G14", f"{relpath}: billing_month({billing_month})からusage_periodを計算できません ({exc})") from exc
    if start_date != expected_start or end_date != expected_end:
        raise ValidationFailure(
            "G14",
            f"{relpath}: usage_period({start_date}..{end_date}) が billing_period"
            f"({expected_start}..{expected_end}) と一致しません",
        )
    if post["report_month"] != usage_period["start"][:7]:
        raise ValidationFailure("G14", f"{relpath}: report_month が usage_period.start と一致しません: {post['report_month']!r}")

    days_usable, days_total = post["days_usable"], post["days_total"]
    if type(days_usable) is not int or type(days_total) is not int:
        raise ValidationFailure("G14", f"{relpath}: days_usable/days_total は int である必要があります")
    if days_total != days:
        raise ValidationFailure("G14", f"{relpath}: days_total が usage_period.days と一致しません: {days_total} != {days}")
    if not (0 <= days_usable <= days_total):
        raise ValidationFailure("G14", f"{relpath}: days_usable が範囲外です: {days_usable} (許容 0〜{days_total})")

    revision = post["revision"]
    if type(revision) is not int:
        raise ValidationFailure("G14", f"{relpath}: revision は int である必要があります: {revision!r}")
    if not (1 <= revision <= 12):
        raise ValidationFailure("G14", f"{relpath}: revision が範囲外です: {revision} (許容 1〜12)")
    revised = post["revised"]
    if revised is not None and not isinstance(revised, str):
        raise ValidationFailure("G14", f"{relpath}: revised は文字列かnullである必要があります: {revised!r}")
    if (revised is None) != (revision == 1):
        raise ValidationFailure("G14", f"{relpath}: revised is None は revision==1 と対応する必要があります (revision={revision}, revised={revised!r})")

    stage = post["stage"]
    if stage not in ("preliminary", "final"):
        raise ValidationFailure("G14", f"{relpath}: stage が不正です: {stage!r}")
    # QA指摘2026-09-26 item4: 速報(stage=preliminary)は初回公開後は凍結するため、
    # revision==1かつrevised is Noneを強制する。
    if stage == "preliminary" and (revision != 1 or revised is not None):
        raise ValidationFailure(
            "G14",
            f"{relpath}: 速報(stage=preliminary)は revision==1 かつ revised is None である必要があります"
            f" (revision={revision}, revised={revised!r})",
        )

    first_published = _parse_iso_date(post["first_published"], relpath, "first_published")
    if not (end_date + timedelta(days=1) <= first_published <= today_jst):
        raise ValidationFailure("G14", f"{relpath}: first_published が範囲外です: {first_published} (許容 {end_date + timedelta(days=1)}〜{today_jst})")
    if revised is not None:
        revised_date = _parse_iso_date(revised, relpath, "revised")
        if not (first_published <= revised_date <= today_jst):
            raise ValidationFailure("G14", f"{relpath}: revised が範囲外です: {revised_date} (許容 {first_published}〜{today_jst})")

    layers = _require_dict(post["layers"], relpath, "layers")
    for key in ("L0", "L1", "L2", "L3"):
        if key not in layers:
            raise ValidationFailure("G14", f"{relpath}: layers.{key} がありません")
        layer = _require_dict(layers[key], relpath, f"layers.{key}")
        missing_layer_keys = _POST_LAYER_KEYS - set(layer)
        if missing_layer_keys:
            raise ValidationFailure("G14", f"{relpath}: layers.{key} に必須キーが不足しています: {sorted(missing_layer_keys)}")
        _check_yen_value(layer["net_cost_fit_yen"], relpath, f"layers.{key}.net_cost_fit_yen")
        _check_yen_value(layer["net_cost_post_fit_yen"], relpath, f"layers.{key}.net_cost_post_fit_yen")
        _check_kwh_value(layer["buy_kwh"], relpath, f"layers.{key}.buy_kwh", nullable=False)
        _check_kwh_value(layer["sell_kwh"], relpath, f"layers.{key}.sell_kwh", nullable=False)

    l2_band = _require_dict(post["l2_band"], relpath, "l2_band")
    for key in ("net_cost_fit_yen_min", "net_cost_fit_yen_max"):
        if key not in l2_band:
            raise ValidationFailure("G14", f"{relpath}: l2_band.{key} がありません")
        _check_yen_value(l2_band[key], relpath, f"l2_band.{key}")
    if l2_band["net_cost_fit_yen_min"] > l2_band["net_cost_fit_yen_max"]:
        raise ValidationFailure("G14", f"{relpath}: l2_band.net_cost_fit_yen_min が net_cost_fit_yen_max を超えています")

    energy = _require_dict(post["energy"], relpath, "energy")
    missing_energy_keys = _POST_ENERGY_KEYS - set(energy)
    if missing_energy_keys:
        raise ValidationFailure("G14", f"{relpath}: energy に必須キーが不足しています: {sorted(missing_energy_keys)}")
    for key in _POST_ENERGY_KEYS:
        _check_kwh_value(energy[key], relpath, f"energy.{key}", nullable=True)

    weather = _require_dict(post["weather"], relpath, "weather")
    missing_weather_keys = _POST_WEATHER_KEYS - set(weather)
    if missing_weather_keys:
        raise ValidationFailure("G14", f"{relpath}: weather に必須キーが不足しています: {sorted(missing_weather_keys)}")
    for key in _POST_WEATHER_KEYS:
        value = weather[key]
        if type(value) is not int or value < 0:
            raise ValidationFailure("G14", f"{relpath}: weather.{key} は0以上のintである必要があります: {value!r}")
    if sum(weather[key] for key in _POST_WEATHER_KEYS) != days:
        raise ValidationFailure("G14", f"{relpath}: weatherの日数合計がusage_period.daysと一致しません")

    comparison = _require_dict(post["comparison"], relpath, "comparison")
    for key in ("prev_solar_kwh", "yoy_solar_kwh"):
        if key not in comparison:
            raise ValidationFailure("G14", f"{relpath}: comparison.{key} がありません")
        _check_kwh_value(comparison[key], relpath, f"comparison.{key}", nullable=True)

    estimation = post["estimation"]
    if estimation not in ("full", "scaled"):
        raise ValidationFailure("G14", f"{relpath}: estimation が不正です: {estimation!r}")

    tariff_basis = post["tariff_basis"]
    if tariff_basis not in ("provisional", "confirmed"):
        raise ValidationFailure("G14", f"{relpath}: tariff_basis が不正です: {tariff_basis!r}")
    # QA再指摘2026-09-26 R4: 不変条件は「stage=="final" ⇒ tariff_basis=="confirmed"」の
    # 片方向のみ（追補D'を緩和）。速報でも単価は既に確定済みという状態はありうる
    # （buy_source/sell_sourceだけが未確定な月）。
    if stage == "final" and tariff_basis != "confirmed":
        raise ValidationFailure("G14", f"{relpath}: stage=final なのに tariff_basis が confirmed ではありません: {tariff_basis!r}")

    l3_source = _require_dict(post["l3_source"], relpath, "l3_source")
    for key in ("buy", "sell"):
        if key not in l3_source:
            raise ValidationFailure("G14", f"{relpath}: l3_source.{key} がありません")
    if l3_source["buy"] not in ("sensor", "billed"):
        raise ValidationFailure("G14", f"{relpath}: l3_source.buy が不正です: {l3_source['buy']!r}")
    if l3_source["sell"] not in ("sensor", "official_meter"):
        raise ValidationFailure("G14", f"{relpath}: l3_source.sell が不正です: {l3_source['sell']!r}")

    transitioned_from_preliminary = post["transitioned_from_preliminary"]
    if type(transitioned_from_preliminary) is not bool:
        raise ValidationFailure("G14", f"{relpath}: transitioned_from_preliminary は bool である必要があります: {transitioned_from_preliminary!r}")
    if stage == "preliminary" and transitioned_from_preliminary:
        raise ValidationFailure("G14", f"{relpath}: 速報(stage=preliminary)で transitioned_from_preliminary が true になっています")


def gate15_post_recompute(
    post: dict, relpath: str, layers_json: dict, daily_json: list[dict], incoming: Path, meter_read_day: int,
) -> None:
    """G15: 確定段階(stage=final)の本体は monthly_report.expected_final_body() で
    「今のlayers/dailyデータと前回コミットの本体」から期待される本体を計算し、完全一致する
    ことを要求する（run()と同じ関数を共有するため、新規公開・速報からの遷移・既存確定版の
    改版のどのケースでも同じロジックで検証できる。QA再指摘2026-09-26 N3: 以前はlayer由来
    項目だけしか比較しておらず、energy/weather/comparisonの捏造がすり抜けていた）。
    確定条件(is_closable)を満たさない「凍結された確定版」（420日窓の外に出た等）は、
    前回コミットの本体と完全一致する場合だけ許可する。
    速報段階は、前回と本体が同一なら凍結として許可、そうでなければ
    layers.preliminary_months[]からの再計算と完全一致することを要求する。"""
    billing_month = post["billing_month"]
    stage = post["stage"]
    daily_by_date = {row["date"]: row for row in daily_json}
    body = monthly_report.body_without_meta(post)
    prev = _previous_commit_json(incoming, relpath)
    prev_stage = prev.get("stage") if isinstance(prev, dict) else None
    prev_body = monthly_report.body_without_meta(prev) if isinstance(prev, dict) else None

    if stage == "final":
        month_rec = next((m for m in layers_json.get("months", []) if m["billing_month"] == billing_month), None)
        if monthly_report.is_closable(month_rec):
            expected = monthly_report.expected_final_body(billing_month, layers_json, daily_by_date, meter_read_day, prev_stage, prev_body)
            if expected is None or expected != body:
                raise ValidationFailure("G15", f"{relpath}: layers.months[]からの再計算(expected_final_body)と本体が一致しません")
            return
        # 確定条件を満たさない「凍結された確定版」: 前回コミットの本体と一致する場合だけ許可する
        # （420日窓の外に出てmonths[]から消えた確定月が恒久的にreject対象になるのを防ぐ）。
        if prev_stage == "final" and prev_body == body:
            return
        raise ValidationFailure(
            "G15",
            f"{relpath}: stage=final ですが確定条件(is_closable)を満たさず、前回コミットの確定版とも一致しません",
        )

    if stage == "preliminary":
        if prev_body is not None and prev_body == body:
            return  # 既に公開済みの速報（凍結）
        recomputed = monthly_report.build_snapshot_body(billing_month, layers_json, daily_by_date, "preliminary", meter_read_day)
        if recomputed != body:
            raise ValidationFailure("G15", f"{relpath}: layers.preliminary_months[]からの再計算と本体が一致しません")
        return

    raise ValidationFailure("G15", f"{relpath}: stage が不正です: {stage!r}")


def _previous_commit_depth_for_posts(incoming: Path) -> int | None:
    """G16(posts/一覧の削除検知)が比較すべき祖先コミットの深さを、data/metrics/daily.json
    （G8/G9/G11と同じ判断基準）が読める最も浅い祖先に揃える（QA指摘2026-09-26 item9:
    以前はHEAD~1固定だったため、拒否pushをrevertした直後（HEAD~1が壊れたコミット）に
    誤って「全post削除」と判定していた）。"""
    if not (incoming / ".git").exists():
        return None
    for depth in range(1, PREVIOUS_COMMIT_MAX_DEPTH + 1):
        result = subprocess.run(
            ["git", "-C", str(incoming), "show", f"HEAD~{depth}:data/metrics/daily.json"],
            capture_output=True, text=True,
        )
        if result.returncode != 0:
            return None
        try:
            json.loads(result.stdout)
            return depth
        except json.JSONDecodeError:
            continue
    return None


def _previous_commit_post_paths(incoming: Path) -> list[str] | None:
    """_previous_commit_depth_for_posts()が返す深さの祖先コミット時点のposts/配下ファイル
    一覧（相対パス）。読めなければNone。"""
    depth = _previous_commit_depth_for_posts(incoming)
    if depth is None:
        return None
    result = subprocess.run(
        ["git", "-C", str(incoming), "ls-tree", "-r", "--name-only", f"HEAD~{depth}", "--", "posts"],
        capture_output=True, text=True,
    )
    if result.returncode != 0:
        return None
    return [line for line in result.stdout.splitlines() if line]


def gate16_post_history(posts_by_relpath: dict[str, dict], incoming: Path, allow_history_change: bool) -> None:
    """G16: 前回コミット（_previous_commit_depth_for_postsと同じ深さ）との履歴整合
    （--allow-history-changeでスキップ可）。
    ・前回にあったpostが削除されていないこと
    ・first_publishedが変わっていないこと
    ・本体が変わったらrevisionが+1であること。本体が同じならrevision/revisedも同じこと
    ・追補(2026-09-26)D': stageがfinal→preliminaryに後退したらreject。
      preliminary→finalはrevision==前回+1が必須。"""
    if allow_history_change:
        return
    prev_paths = _previous_commit_post_paths(incoming)
    if prev_paths is None:
        return
    if not set(prev_paths) <= set(posts_by_relpath):
        removed = sorted(set(prev_paths) - set(posts_by_relpath))
        raise ValidationFailure("G16", f"前回コミットにあったpostsが削除されています（--allow-history-changeが必要）: {removed}")

    for relpath in prev_paths:
        prev = _previous_commit_json(incoming, relpath)
        if not isinstance(prev, dict):
            continue
        cur = posts_by_relpath[relpath]
        if cur["first_published"] != prev["first_published"]:
            raise ValidationFailure("G16", f"{relpath}: first_published が前コミットと異なります（--allow-history-changeが必要）: {prev['first_published']} -> {cur['first_published']}")
        if prev.get("stage") == "final" and cur.get("stage") == "preliminary":
            raise ValidationFailure("G16", f"{relpath}: stageがfinal→preliminaryに後退しています（--allow-history-changeが必要）")
        prev_body = monthly_report.body_without_meta(prev)
        cur_body = monthly_report.body_without_meta(cur)
        if prev_body == cur_body:
            if cur["revision"] != prev["revision"] or cur["revised"] != prev["revised"]:
                raise ValidationFailure("G16", f"{relpath}: 本体が同一なのにrevision/revisedが変わっています（--allow-history-changeが必要）")
            continue
        if cur["revision"] != prev["revision"] + 1:
            raise ValidationFailure("G16", f"{relpath}: 本体が変わったのにrevisionが+1になっていません（--allow-history-changeが必要）: {prev['revision']} -> {cur['revision']}")


def _require_exact_keys(data: object, relpath: str, keys: set[str]) -> None:
    if not isinstance(data, dict):
        raise ValidationFailure("G18", f"{relpath}: トップレベルは object である必要があります")
    unexpected = set(data) - keys
    if unexpected:
        raise ValidationFailure("G18", f"{relpath}: 許可されていないキーです: {sorted(unexpected)}")
    missing = keys - set(data)
    if missing:
        raise ValidationFailure("G18", f"{relpath}: 必須キーが不足しています: {sorted(missing)}")


def _check_input_int_range(value, low, high, relpath, label) -> None:
    # G14の_check_yen_valueと同じ流儀(type(...) is int、boolを弾く)に揃える。
    if type(value) is not int:
        raise ValidationFailure("G18", f"{relpath}: {label} は整数である必要があります: {value!r}")
    if not (low <= value <= high):
        raise ValidationFailure("G18", f"{relpath}: {label} が範囲外です: {value} (許容 {low}〜{high})")


def _check_input_whole_yen_range(value, low, high, relpath, label) -> None:
    """円額の範囲検査。official_buy.json/official_sell.json の billed_yen/sell_revenue_yen は
    既存データ（import_official_buy.py/import_official_sell.py が私有の抽出元から
    そのまま転記する値）がJSON上float(例: 2563.0)で保存されていることがあるため、整数値と
    等しいfloatも許容する（tariff_months.jsonのcapacity_contribution_yen_per_monthのように
    新規生成されるフィールドは_check_input_int_rangeで厳密にintのみ要求し続ける）。"""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValidationFailure("G18", f"{relpath}: {label} は整数である必要があります: {value!r}")
    if isinstance(value, float) and (not math.isfinite(value) or not value.is_integer()):
        raise ValidationFailure("G18", f"{relpath}: {label} は整数である必要があります: {value!r}")
    ivalue = int(value)
    if not (low <= ivalue <= high):
        raise ValidationFailure("G18", f"{relpath}: {label} が範囲外です: {value} (許容 {low}〜{high})")


def _check_input_finite_range(value, low, high, relpath, label) -> None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValidationFailure("G18", f"{relpath}: {label} は数値である必要があります: {value!r}")
    if not math.isfinite(value):
        raise ValidationFailure("G18", f"{relpath}: {label} が有限値ではありません: {value!r}")
    if not (low <= value <= high):
        raise ValidationFailure("G18", f"{relpath}: {label} が範囲外です: {value} (許容 {low}〜{high})")


def _check_input_month_sequence(months: list[dict], relpath: str) -> None:
    if len(months) > MAX_INPUT_MONTHLY_ROWS:
        raise ValidationFailure("G18", f"{relpath}: months が上限({MAX_INPUT_MONTHLY_ROWS})を超えています ({len(months)})")
    prev = None
    for month in months:
        if not isinstance(month, dict):
            raise ValidationFailure("G18", f"{relpath}: months の要素は object である必要があります: {month!r}")
        sm = month.get("settlement_month")
        if not isinstance(sm, str) or not _MONTH_RE.match(sm):
            raise ValidationFailure("G18", f"{relpath}: settlement_month の書式が不正です: {sm!r}")
        if prev is not None and sm <= prev:
            raise ValidationFailure("G18", f"{relpath}: settlement_month が昇順・重複なしではありません: {prev!r} -> {sm!r}")
        prev = sm


def _check_input_period(month: dict, meter_read_day: int, relpath: str) -> None:
    sm = month["settlement_month"]
    period_from, period_to = month.get("period_from"), month.get("period_to")
    if not isinstance(period_from, str) or not _DATE_RE.match(period_from):
        raise ValidationFailure("G18", f"{relpath}: {sm} period_from の書式が不正です: {period_from!r}")
    if not isinstance(period_to, str) or not _DATE_RE.match(period_to):
        raise ValidationFailure("G18", f"{relpath}: {sm} period_to の書式が不正です: {period_to!r}")
    expected_from, expected_to = bill_model.billing_period(sm, meter_read_day)
    if (period_from, period_to) != (expected_from.isoformat(), expected_to.isoformat()):
        raise ValidationFailure(
            "G18", f"{relpath}: {sm} の period が billing_period と一致しません: ({period_from}, {period_to}) != ({expected_from}, {expected_to})"
        )


def _gate18_official_buy(data: object, relpath: str, meter_read_day: int) -> None:
    _require_exact_keys(data, relpath, OFFICIAL_BUY_TOP_KEYS)
    generated_at = data.get("generated_at")
    if not isinstance(generated_at, str) or not _DATETIME_RE.match(generated_at):
        raise ValidationFailure("G18", f"{relpath}: generated_at の書式が不正です: {generated_at!r}")
    if data.get("source_note") != import_official_buy.SOURCE_NOTE:
        raise ValidationFailure("G18", f"{relpath}: source_note が既知の文言と一致しません")
    months = data.get("months")
    if not isinstance(months, list):
        raise ValidationFailure("G18", f"{relpath}: months は配列である必要があります")
    _check_input_month_sequence(months, relpath)
    for month in months:
        _require_exact_keys(month, relpath, OFFICIAL_BUY_MONTH_KEYS)
        _check_input_period(month, meter_read_day, relpath)
        sm = month["settlement_month"]
        _check_input_finite_range(month.get("official_buy_kwh"), 0, 6200, relpath, f"{sm} official_buy_kwh")
        _check_input_whole_yen_range(month.get("billed_yen"), 0, 300000, relpath, f"{sm} billed_yen")


def _gate18_official_sell(data: object, relpath: str, meter_read_day: int, sell_fit: float) -> None:
    _require_exact_keys(data, relpath, OFFICIAL_SELL_TOP_KEYS)
    generated_at = data.get("generated_at")
    if not isinstance(generated_at, str) or not _DATETIME_RE.match(generated_at):
        raise ValidationFailure("G18", f"{relpath}: generated_at の書式が不正です: {generated_at!r}")
    if data.get("source_note") != import_official_sell.SOURCE_NOTE:
        raise ValidationFailure("G18", f"{relpath}: source_note が既知の文言と一致しません")
    months = data.get("months")
    if not isinstance(months, list):
        raise ValidationFailure("G18", f"{relpath}: months は配列である必要があります")
    _check_input_month_sequence(months, relpath)
    for month in months:
        _require_exact_keys(month, relpath, OFFICIAL_SELL_MONTH_KEYS)
        _check_input_period(month, meter_read_day, relpath)
        sm = month["settlement_month"]
        kwh = month.get("official_sell_kwh")
        _check_input_finite_range(kwh, 0, 6200, relpath, f"{sm} official_sell_kwh")
        yen = month.get("sell_revenue_yen")
        _check_input_whole_yen_range(yen, 0, 300000, relpath, f"{sm} sell_revenue_yen")
        if abs(yen / sell_fit - kwh) > 1.0:
            raise ValidationFailure("G18", f"{relpath}: {sm} sell_revenue_yen/fit と official_sell_kwh の差が許容(1.0kWh)を超えています")


def _gate18_tariff_months(data: object, relpath: str) -> None:
    _require_exact_keys(data, relpath, TARIFF_MONTHS_KEYS)
    if data.get("schema_version") != 1:
        raise ValidationFailure("G18", f"{relpath}: schema_version が1ではありません: {data.get('schema_version')!r}")
    generated_at = data.get("generated_at")
    if not isinstance(generated_at, str) or not _DATETIME_RE.match(generated_at):
        raise ValidationFailure("G18", f"{relpath}: generated_at の書式が不正です: {generated_at!r}")

    fuel = data.get("fuel_cost_adjustment_yen_per_kwh")
    capacity = data.get("capacity_contribution_yen_per_month")
    if not isinstance(fuel, dict) or not isinstance(capacity, dict):
        raise ValidationFailure("G18", f"{relpath}: fuel_cost_adjustment_yen_per_kwh / capacity_contribution_yen_per_month は object である必要があります")
    if set(fuel) != set(capacity):
        raise ValidationFailure("G18", f"{relpath}: fuel_cost_adjustment_yen_per_kwh と capacity_contribution_yen_per_month のキー集合が一致しません")
    if len(fuel) > MAX_INPUT_MONTHLY_ROWS:
        raise ValidationFailure("G18", f"{relpath}: 月数が上限({MAX_INPUT_MONTHLY_ROWS})を超えています ({len(fuel)})")
    if list(fuel) != sorted(fuel):
        raise ValidationFailure("G18", f"{relpath}: fuel_cost_adjustment_yen_per_kwh の月が昇順ではありません")
    for m in fuel:
        if not isinstance(m, str) or not _MONTH_RE.match(m):
            raise ValidationFailure("G18", f"{relpath}: fuel_cost_adjustment_yen_per_kwh のキーの書式が不正です: {m!r}")
        _check_input_finite_range(fuel[m], -20, 30, relpath, f"fuel_cost_adjustment_yen_per_kwh[{m}]")
    for m in capacity:
        _check_input_int_range(capacity[m], 0, 5000, relpath, f"capacity_contribution_yen_per_month[{m}]")

    levy = data.get("renewable_levy_yen_per_kwh_observed")
    if not isinstance(levy, dict):
        raise ValidationFailure("G18", f"{relpath}: renewable_levy_yen_per_kwh_observed は object である必要があります")
    for rng, value in levy.items():
        m = _LEVY_RANGE_RE.match(rng) if isinstance(rng, str) else None
        if not m or m.group(1) != m.group(2):
            raise ValidationFailure("G18", f"{relpath}: renewable_levy_yen_per_kwh_observed のキーは単月レンジ(YYYY-MM..YYYY-MM)である必要があります: {rng!r}")
        _check_input_finite_range(value, 0, 10, relpath, f"renewable_levy_yen_per_kwh_observed[{rng}]")

    excluded = data.get("excluded_months")
    if not isinstance(excluded, dict):
        raise ValidationFailure("G18", f"{relpath}: excluded_months は object である必要があります")
    for m, reason in excluded.items():
        if not isinstance(m, str) or not _MONTH_RE.match(m):
            raise ValidationFailure("G18", f"{relpath}: excluded_months のキーの書式が不正です: {m!r}")
        # QA指摘L1: reasonがlist/dict等の非文字列だと `in EXCLUDED_MONTH_REASONS`（set）が
        # unhashableでTypeErrorになり、ValidationFailureではなく未処理例外として漏れる。
        if not isinstance(reason, str) or reason not in EXCLUDED_MONTH_REASONS:
            raise ValidationFailure("G18", f"{relpath}: excluded_months[{m}] の値が許可された列挙値ではありません: {reason!r}")


def gate18_input_files_schema(parsed_inputs: dict[str, object], meter_read_day: int, sell_fit: float) -> None:
    """G18: inputs/{official_buy,official_sell,tariff_months}.json（存在するものだけ）の
    キー・型・範囲・書式を厳密に検査する（月次確定の自動化、DDR実装手順S1）。"""
    if "inputs/official_buy.json" in parsed_inputs:
        _gate18_official_buy(parsed_inputs["inputs/official_buy.json"], "inputs/official_buy.json", meter_read_day)
    if "inputs/official_sell.json" in parsed_inputs:
        _gate18_official_sell(parsed_inputs["inputs/official_sell.json"], "inputs/official_sell.json", meter_read_day, sell_fit)
    if "inputs/tariff_months.json" in parsed_inputs:
        _gate18_tariff_months(parsed_inputs["inputs/tariff_months.json"], "inputs/tariff_months.json")


def gate19_official_buy_reconcile(tariff_base: dict, official_buy: dict | None, tariff_months: dict | None) -> None:
    """G19: merge_tariff(base tariff, incoming の tariff_months) で確定している請求月のうち
    inputs/official_buy.json にある月は、すべて reconcile_bill()==0（請求総額と一致）で
    あることを要求する（fatal）。

    QA指摘F1: overlay(tariff_months)が base に対して新たに確定させた月
    （base単独では未確定だった月）は、inputs/official_buy.json に対応する月が
    存在し reconcile_bill()==0 であることを**必須**にする（無ければ fatal。
    official_buy 自体が無いのに overlay が新規確定月を追加している場合も fatal）。
    base が単独で既に確定していた月（overlayが関与しない）は、official_buy に
    たまたま存在する場合だけ突合を要求する（従来の一般則）。"""
    base_confirmed = bill_model.confirmed_tariff_months(tariff_base)
    try:
        effective = bill_model.merge_tariff(tariff_base, tariff_months)
    except ValueError as exc:
        raise ValidationFailure("G19", f"inputs/tariff_months.json が base tariff と競合しています: {exc}") from exc
    confirmed = bill_model.confirmed_tariff_months(effective)
    overlay_added_months = confirmed - base_confirmed

    official_buy_by_month = {
        month.get("settlement_month"): month for month in (official_buy.get("months", []) if official_buy is not None else [])
    }

    for sm in sorted(overlay_added_months):
        month = official_buy_by_month.get(sm)
        if month is None:
            raise ValidationFailure(
                "G19",
                f"inputs/tariff_months.json: {sm} は新たに確定した請求月ですが、"
                "inputs/official_buy.json に対応する月がありません（新規確定月は請求突合が必須です）",
            )
        diff = bill_model.reconcile_bill(effective, sm, month["official_buy_kwh"], month["billed_yen"])
        if diff != 0:
            raise ValidationFailure("G19", f"inputs/official_buy.json: {sm} の請求突合が一致しません（diff={diff}円）")

    if official_buy is not None:
        for month in official_buy.get("months", []):
            sm = month.get("settlement_month")
            if sm not in confirmed or sm in overlay_added_months:
                continue  # overlay_added_monthsは上のループで既に検査済み
            diff = bill_model.reconcile_bill(effective, sm, month["official_buy_kwh"], month["billed_yen"])
            if diff != 0:
                raise ValidationFailure("G19", f"inputs/official_buy.json: {sm} の請求突合が一致しません（diff={diff}円）")


def check_inputs_dir(inputs_dir: Path, tariff_path: Path) -> None:
    """--check-inputs-dir 用: inputs_dir 内の official_buy.json/official_sell.json/
    tariff_months.json（存在するものだけ）に G2/G3/G6/G7/G18/G19 をかける。
    run-daily.sh の stage_inputs が、handoff を clone/inputs へ反映する前に呼ぶ
    （不合格なら clone/inputs を変更せず既存を維持させるため）。"""
    names = ("official_buy.json", "official_sell.json", "tariff_months.json")
    present = [n for n in names if (inputs_dir / n).exists()]

    texts: dict[str, str] = {}
    parsed: dict[str, object] = {}
    total_bytes = 0
    for name in present:
        relpath = f"inputs/{name}"
        path = inputs_dir / name
        raw_bytes = path.stat().st_size
        total_bytes += raw_bytes
        if raw_bytes > MAX_FILE_BYTES:
            raise ValidationFailure("G3", f"{relpath}: サイズが上限({MAX_FILE_BYTES}bytes)を超えています ({raw_bytes}bytes)")
        text = gate2_encoding(path, relpath)
        texts[relpath] = text
        try:
            parsed[relpath] = json.loads(text)
        except json.JSONDecodeError as exc:
            raise ValidationFailure("G2", f"{relpath}: JSON として不正です ({exc})") from exc
    if total_bytes > MAX_TOTAL_BYTES:
        raise ValidationFailure("G3", f"inputs 合計サイズが上限({MAX_TOTAL_BYTES}bytes)を超えています ({total_bytes}bytes)")

    for relpath, text in texts.items():
        gate6_secret_deny(text, relpath)
    for relpath, data in parsed.items():
        gate7_no_time_of_day(data, relpath)

    tariff_base = json.loads(tariff_path.read_text(encoding="utf-8"))
    meter_read_day = tariff_base["meter_read_day"]
    sell_fit = tariff_base["sell_price_yen_per_kwh"]["fit"]

    gate18_input_files_schema(parsed, meter_read_day, sell_fit)
    gate19_official_buy_reconcile(tariff_base, parsed.get("inputs/official_buy.json"), parsed.get("inputs/tariff_months.json"))


def default_input_paths(repo: Path) -> dict[str, Path]:
    """--repo（Hugoリポジトリのフルチェックアウト）を前提にした、G13入力ハッシュ照合先の
    既定パス。homelab の /opt/blog-metrics/ のようなフラットなbundleレイアウトでは
    validate()/main() の override 引数で個別に差し替える。"""
    return {
        "tariff_sha256": repo / "scripts" / "blog-metrics" / "tariff.json",
        "official_buy_sha256": repo / "data" / "metrics" / "official_buy.json",
        "official_sell_sha256": repo / "data" / "metrics" / "official_sell.json",
        "tariff_months_sha256": repo / "data" / "metrics" / "tariff_months.json",
    }


# G13でincoming(data repo)側のinputs/*.jsonと直接照合するキー（一致しなければfatal）。
# tariff_sha256はここに含めない(base tariff.jsonはmainにしか存在しないため従来どおり警告のみ)。
_G13_INCOMING_INPUT_RELPATHS = {
    "official_buy_sha256": "inputs/official_buy.json",
    "official_sell_sha256": "inputs/official_sell.json",
    "tariff_months_sha256": "inputs/tariff_months.json",
}


def gate13_pipeline(pipeline: dict, incoming: Path, input_paths: dict[str, Path]) -> list[str]:
    """G13: pipeline.json のスキーマ・鮮度・入力ハッシュ。鮮度・main実ファイルとの
    ハッシュ不一致は警告のみ（GitHub Actions の ::warning:: 形式で返す。fatalにしない）。

    ただし official_buy_sha256・official_sell_sha256・tariff_months_sha256 は、incoming
    （data repo自身、今回pushしようとしているコミット）に対応する inputs/*.json が
    存在するならそちらと直接照合し、不一致は fatal にする（月次確定の自動化、
    DDR実装手順S1「G13 は incoming 内で fatal 照合」）。incoming に inputs/ が無い
    移行期間は、従来どおり input_paths（main側の実ファイル）との照合で警告のみに留める。"""
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
        incoming_relpath = _G13_INCOMING_INPUT_RELPATHS.get(key)
        if incoming_relpath is not None:
            incoming_path = incoming / incoming_relpath
            if incoming_path.exists():
                # QA指摘L2: incomingに対応ファイルが実在するのにpipeline.jsonがshaキーを
                # 持たない（=検証されずに素通りする穴）ことをfatalにする。
                if not expected:
                    raise ValidationFailure(
                        "G13", f"pipeline.json.inputs.{key} がありませんが incoming/{incoming_relpath} が存在します"
                    )
                actual = hashlib.sha256(incoming_path.read_bytes()).hexdigest()
                if actual != expected:
                    raise ValidationFailure("G13", f"pipeline.json.inputs.{key} が incoming/{incoming_relpath} と一致しません")
                continue
            # incoming に対応ファイルが無い移行期間 → 従来どおり main と照合して警告のみ
        if not expected:
            continue
        if not real_path.exists():
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
    today: date | None = None,
) -> list[str]:
    """全ゲートを実行する。戻り値は警告メッセージのリスト（fatalではない）。
    fatal な違反があれば ValidationFailure を送出する。

    input_paths: G13入力ハッシュ照合先の override（省略時は --repo を Hugoリポジトリの
    フルチェックアウトとみなした既定パスを使う。homelab の run-daily.sh はフラットな
    bundle レイアウト(/opt/blog-metrics/inputs/*)向けに明示的に渡す）。
    today: G14(first_published<=today)の基準日のoverride（省略時は実行日のJST日付。
    monthly_report.run()と同じ流儀でテストから注入できるようにする）。"""
    post_files, input_files = gate1_file_allowlist(incoming)
    all_files = sorted(ALLOWED_FILES) + post_files + input_files

    texts: dict[str, str] = {}
    total_bytes = 0
    for relpath in all_files:
        path = incoming / relpath
        raw_bytes = path.stat().st_size
        total_bytes += raw_bytes
        texts[relpath] = gate2_encoding(path, relpath)

    parsed: dict[str, object] = {}
    for relpath in sorted(DATA_FILES | {"pipeline.json"}) + post_files + input_files:
        try:
            parsed[relpath] = json.loads(texts[relpath])
        except json.JSONDecodeError as exc:
            raise ValidationFailure("G2", f"{relpath}: JSON として不正です ({exc})") from exc

    for relpath in sorted(ALLOWED_FILES):
        gate3_size_and_rows(incoming, relpath, incoming.joinpath(relpath).stat().st_size, total_bytes, parsed.get(relpath))
    for relpath in post_files:
        raw_bytes = incoming.joinpath(relpath).stat().st_size
        if raw_bytes > MAX_POST_BYTES:
            raise ValidationFailure("G3", f"{relpath}: サイズが上限({MAX_POST_BYTES}bytes)を超えています ({raw_bytes}bytes)")
    for relpath in input_files:
        raw_bytes = incoming.joinpath(relpath).stat().st_size
        if raw_bytes > MAX_FILE_BYTES:
            raise ValidationFailure("G3", f"{relpath}: サイズが上限({MAX_FILE_BYTES}bytes)を超えています ({raw_bytes}bytes)")

    # G4(再帰キーallowlist)・G5(文字列フォーマット) は inputs/ には適用しない
    # （専用の厳密スキーマ G18 で検査するため。DDR実装手順S1）。
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
    for relpath in post_files:
        _walk_keys(parsed[relpath], relpath, POST_KEYS)

    for relpath in sorted(DATA_FILES | {"pipeline.json"}) + post_files:
        gate5_string_format(parsed[relpath], relpath)
        gate7_no_time_of_day(parsed[relpath], relpath)
    for relpath in input_files:
        gate7_no_time_of_day(parsed[relpath], relpath)

    for relpath in all_files:
        gate6_secret_deny(texts[relpath], relpath)

    daily = parsed["data/metrics/daily.json"]
    monthly = parsed["data/metrics/monthly.json"]
    meta = parsed["data/metrics/meta.json"]
    layers_json = parsed["data/metrics/layers.json"]

    resolved_input_paths = input_paths if input_paths is not None else default_input_paths(repo)

    # QA再指摘2026-09-26 R2 を踏襲: tariff.jsonの読み込み(meter_read_day取得)はpostsか
    # inputs/のどちらかが1件以上あるときだけ行う（posts/もinputs/も無い日にファイル
    # アクセス・I/Oを増やさない）。
    tariff_base: dict | None = None
    meter_read_day: int | None = None
    newly_confirmed_months: set[str] = set()
    if post_files or input_files:
        tariff_path = resolved_input_paths["tariff_sha256"]
        tariff_base = json.loads(tariff_path.read_text(encoding="utf-8"))
        meter_read_day = tariff_base["meter_read_day"]

    if "inputs/tariff_months.json" in input_files:
        overlay = parsed["inputs/tariff_months.json"]
        prev_overlay = _previous_commit_json(incoming, "inputs/tariff_months.json")
        try:
            cur_confirmed = bill_model.confirmed_tariff_months(bill_model.merge_tariff(tariff_base, overlay))
        except ValueError:
            cur_confirmed = set()
        # QA指摘F2: prev_overlayが無い（初めてtariff_months.jsonが現れた回、または
        # 前コミットに読めるtariff_months.jsonが無い）場合、prev_confirmedを空集合にすると
        # base単独で以前から確定済みだった月まで「今回新たに確定した」扱いになり、G9の
        # 例外(saving_yenのみ許容)が過剰に適用されてしまう。overlay無し(=base単独)の
        # confirmed_tariff_monthsを基準にする。
        try:
            prev_confirmed = bill_model.confirmed_tariff_months(bill_model.merge_tariff(tariff_base, prev_overlay))
        except ValueError:
            prev_confirmed = set()
        newly_confirmed_months = cur_confirmed - prev_confirmed

    gate8_date_health(daily, incoming, allow_history_change)
    gate9_history_immutability(
        daily, incoming, allow_history_change,
        meter_read_day=meter_read_day, newly_confirmed_months=newly_confirmed_months,
    )
    gate10_physical_range(daily, monthly)
    gate11_anomaly(daily, monthly, incoming)
    gate12_consistency(daily, monthly, meta)

    posts_by_relpath = {relpath: parsed[relpath] for relpath in post_files}
    if posts_by_relpath:
        today_jst = today if today is not None else datetime.now(JST).date()
    for relpath, post in posts_by_relpath.items():
        gate14_post_schema(post, relpath, today_jst, meter_read_day)
    for relpath, post in posts_by_relpath.items():
        gate15_post_recompute(post, relpath, layers_json, daily, incoming, meter_read_day)
    gate16_post_history(posts_by_relpath, incoming, allow_history_change)

    if input_files:
        sell_fit = tariff_base["sell_price_yen_per_kwh"]["fit"]
        gate18_input_files_schema({relpath: parsed[relpath] for relpath in input_files}, meter_read_day, sell_fit)
        gate19_official_buy_reconcile(
            tariff_base, parsed.get("inputs/official_buy.json"), parsed.get("inputs/tariff_months.json"),
        )

    return gate13_pipeline(parsed["pipeline.json"], incoming, resolved_input_paths)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--incoming", type=Path, default=None, help="別リポジトリ solar-metrics-data のチェックアウト先ディレクトリ（--check-inputs-dir 未指定時は必須）")
    parser.add_argument("--repo", type=Path, default=None, help="このリポジトリ(main)のルートディレクトリ（G13入力ハッシュ照合の既定パス算出に使う。--check-inputs-dir 未指定時は必須）")
    parser.add_argument("--allow-history-change", action="store_true", help="G9(履歴不変性)をスキップする（workflow_dispatch の入力用）")
    parser.add_argument("--tariff-path", type=Path, default=None, help="G13でtariff_sha256と照合する実ファイルのパス（既定: --repo基準）。--check-inputs-dir指定時はG18/G19のbase tariffとして必須")
    parser.add_argument("--official-buy-path", type=Path, default=None, help="G13でofficial_buy_sha256と照合する実ファイルのパス（既定: --repo基準）")
    parser.add_argument("--official-sell-path", type=Path, default=None, help="G13でofficial_sell_sha256と照合する実ファイルのパス（既定: --repo基準）")
    parser.add_argument(
        "--check-inputs-dir", type=Path, default=None,
        help="このディレクトリ内の official_buy.json/official_sell.json/tariff_months.json（存在するものだけ）に"
        "G2/G3/G6/G7/G18/G19だけをかけて終了する（--tariff-pathが必須。--incoming/--repoは不要。"
        "run-daily.sh の stage_inputs が handoff を clone/inputs へ反映する前の検査に使う）",
    )
    args = parser.parse_args()

    if args.check_inputs_dir is not None:
        if args.tariff_path is None:
            print("validate_metrics.py: --check-inputs-dir には --tariff-path が必須です", file=sys.stderr)
            sys.exit(1)
        try:
            check_inputs_dir(args.check_inputs_dir, args.tariff_path)
        except ValidationFailure as exc:
            print(f"validate_metrics.py: {exc.gate}: {exc.message}", file=sys.stderr)
            sys.exit(1)
        print("validate_metrics.py: OK（inputs 検査通過）")
        return

    if args.incoming is None or args.repo is None:
        print("validate_metrics.py: --incoming と --repo が必要です（--check-inputs-dir 指定時を除く）", file=sys.stderr)
        sys.exit(1)

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
