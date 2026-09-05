#!/usr/bin/env bash
#
# aggregate.sh — SolarChargeController の本番 SQLite DB から
# 日次/月次の「集計値のみ」を抽出し、Hugo の data ディレクトリ
# (data/metrics/daily.json, data/metrics/monthly.json) に書き出す。
#
# 設計方針（重要・変更時は維持すること）:
#   - DB へは READ-ONLY の SELECT のみを発行する（`sqlite3 -readonly` を必須で使用）。
#     UPDATE/DELETE/INSERT やスキーマ変更は一切行わない。
#   - 出力は日次・月次の集計値のみ。時間帯別カーブや生ログ、デバイスS/N等は含めない。
#   - 本番 Pi 上には一切ファイルを書かない。SELECT結果は SSH 経由で stdout に返させ、
#     ローカル側でファイルに書き出す（Pi 上に一時ファイルすら作らない）。
#   - 消費電力量(kWh)は「10秒固定間隔」を仮定せず、行間の実時間差(dt)で積分する。
#     欠測ギャップはノイズにならないよう dt に上限(cap)を掛けて除外する。
#
# 使い方:
#   scripts/blog-metrics/aggregate.sh [options]
#
# Options:
#   --db PATH        SQLite DB パス（既定: 本番パス）
#   --host HOST      SSH 接続先（既定: ht1003@solarchgctl.local）。空文字でローカル直結。
#   --local          --host を無視し、ローカルの sqlite3 で直接 --db を開く（動作確認用）
#   --out DIR        出力先ディレクトリ（既定: <repo>/data/metrics）
#   -h, --help       このヘルプを表示
#
# 例:
#   # 本番 Pi に対して実行（既定）
#   scripts/blog-metrics/aggregate.sh
#
#   # ローカルにコピーしたテスト用 DB に対して実行
#   scripts/blog-metrics/aggregate.sh --local --db /tmp/test.db
#
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/../.." && pwd)"

DB_PATH="/opt/solar-charge-controller/db/solar-charge-controller.db"
SSH_HOST="ht1003@solarchgctl.local"
LOCAL_MODE=false
OUT_DIR="${REPO_ROOT}/data/metrics"

# 電気料金の単価。実際の契約プランに合わせて要調整（ここでは依頼時の指定値をそのまま採用）。
# 節約額試算 = 自家消費分(=太陽光発電-売電) × 買電単価 + 売電分 × 売電単価
BUY_PRICE_YEN_PER_KWH=30
SELL_PRICE_YEN_PER_KWH=10

usage() {
    grep -E '^#( |$)' "${BASH_SOURCE[0]}" | sed -E 's/^# ?//'
    exit "${1:-0}"
}

while [[ $# -gt 0 ]]; do
    case "$1" in
        --db) DB_PATH="$2"; shift 2 ;;
        --host) SSH_HOST="$2"; shift 2 ;;
        --local) LOCAL_MODE=true; shift ;;
        --out) OUT_DIR="$2"; shift 2 ;;
        -h|--help) usage 0 ;;
        *) echo "unknown option: $1" >&2; usage 1 ;;
    esac
done

run_sql() {
    # 標準入力の SQL を READ-ONLY で実行し、結果を stdout に流す。
    # -list -noheader: 1 行 1 レコード、区切り文字なしのプレーン出力（JSON文字列を1行で受け取る用途）。
    if [[ "${LOCAL_MODE}" == true || -z "${SSH_HOST}" ]]; then
        sqlite3 -readonly -noheader -list "${DB_PATH}"
    else
        ssh "${SSH_HOST}" "sudo sqlite3 -readonly -noheader -list '${DB_PATH}'"
    fi
}

mkdir -p "${OUT_DIR}"

# 3本の集計を1つの sqlite3 セッション(=1回のSSH接続)にまとめ、
# power_history(90万行規模)へのウィンドウ関数スキャンを1パスに抑える。
# 出力は3行: 1行目=daily.json, 2行目=monthly.json, 3行目=meta.json
SQL=$(cat <<SQL
-- power_history: 実時間差(dt)で積分し kWh 化。欠測ギャップは 120 秒で cap（10秒間隔想定の12倍）。
CREATE TEMP TABLE ph_daily AS
WITH ph AS (
  SELECT date(recorded_at) AS d, solar_w, buy_w, sell_w, surplus_w,
    (julianday(recorded_at) - julianday(LAG(recorded_at) OVER (ORDER BY recorded_at))) * 86400 AS dt
  FROM power_history
)
SELECT d,
  MAX(ROUND(SUM(solar_w * MIN(dt,120)) / 3600000.0, 3), 0) AS solar_kwh,
  MAX(ROUND(SUM(buy_w * MIN(dt,120)) / 3600000.0, 3), 0) AS buy_kwh,
  MAX(ROUND(SUM(sell_w * MIN(dt,120)) / 3600000.0, 3), 0) AS sell_kwh,
  ROUND(SUM(MAX(surplus_w,0) * MIN(dt,120)) / 3600000.0, 3) AS surplus_kwh
FROM ph WHERE dt IS NOT NULL GROUP BY d;
-- 注: 2026-03-07/08（計測開始直後2日間）はセンサー較正過渡で buy_w が瞬間的に負値を
-- 記録しており、そのまま積分すると buy_kwh が負になる。物理的にあり得ないため 0 に floor する。

-- nichicon_battery_history: 既に時間帯別 kWh が入っているので日付ごとに SUM するだけ。
CREATE TEMP TABLE nb_daily AS
SELECT date AS d, ROUND(SUM(charge_kwh), 3) AS nichicon_charge_kwh
FROM nichicon_battery_history
GROUP BY date;

-- ecoflow_power_history: battery_power_w は 正=充電/放電=負
-- （出典: solar-charge-controller/src/main/java/jp/ht1003/scctrl/webui/servlet/EcoFlowPowerHistoryApiServlet.java
--   「chargeW/dischargeW は battery_power_w（正=充電/負=放電）から分解して返す」、確認日 2026-09-05）。
-- 60秒間隔サンプル・保持14日ローリング（EcoFlowPowerHistoryService.RETENTION_DAYS=14）のため、
-- 直近14日分のみ値が入り、それより古い日付は自動的に NULL になる。
CREATE TEMP TABLE ef_daily AS
WITH ef AS (
  SELECT recorded_at, battery_power_w,
    (julianday(recorded_at) - julianday(LAG(recorded_at) OVER (PARTITION BY serial_number ORDER BY recorded_at))) * 86400 AS dt
  FROM ecoflow_power_history
)
SELECT date(recorded_at) AS d,
  ROUND(SUM(MAX(battery_power_w,0) * MIN(dt,180)) / 3600000.0, 3) AS ecoflow_charge_kwh
FROM ef WHERE dt IS NOT NULL GROUP BY date(recorded_at);

CREATE TEMP TABLE daily_all AS
SELECT
  p.d AS date,
  p.solar_kwh, p.buy_kwh, p.sell_kwh, p.surplus_kwh,
  n.nichicon_charge_kwh, e.ecoflow_charge_kwh,
  CASE WHEN n.nichicon_charge_kwh IS NULL AND e.ecoflow_charge_kwh IS NULL THEN NULL
       ELSE ROUND(COALESCE(n.nichicon_charge_kwh,0) + COALESCE(e.ecoflow_charge_kwh,0), 3)
  END AS self_consumption_shift_kwh,
  CAST(ROUND(MAX(p.solar_kwh - p.sell_kwh, 0) * ${BUY_PRICE_YEN_PER_KWH} + p.sell_kwh * ${SELL_PRICE_YEN_PER_KWH}) AS INTEGER) AS saving_yen
FROM ph_daily p
LEFT JOIN nb_daily n ON n.d = p.d
LEFT JOIN ef_daily e ON e.d = p.d;

SELECT json_group_array(json_object(
  'date', date,
  'solar_kwh', solar_kwh,
  'buy_kwh', buy_kwh,
  'sell_kwh', sell_kwh,
  'surplus_kwh', surplus_kwh,
  'nichicon_charge_kwh', nichicon_charge_kwh,
  'ecoflow_charge_kwh', ecoflow_charge_kwh,
  'self_consumption_shift_kwh', self_consumption_shift_kwh,
  'saving_yen', saving_yen
))
FROM (SELECT * FROM daily_all ORDER BY date);

SELECT json_group_array(json_object(
  'month', month,
  'solar_kwh', solar_kwh,
  'buy_kwh', buy_kwh,
  'sell_kwh', sell_kwh,
  'surplus_kwh', surplus_kwh,
  'nichicon_charge_kwh', nichicon_charge_kwh,
  'ecoflow_charge_kwh', ecoflow_charge_kwh,
  'self_consumption_shift_kwh', self_consumption_shift_kwh,
  'saving_yen', saving_yen
))
FROM (
  SELECT strftime('%Y-%m', date) AS month,
    ROUND(SUM(solar_kwh), 3) AS solar_kwh,
    ROUND(SUM(buy_kwh), 3) AS buy_kwh,
    ROUND(SUM(sell_kwh), 3) AS sell_kwh,
    ROUND(SUM(surplus_kwh), 3) AS surplus_kwh,
    ROUND(SUM(nichicon_charge_kwh), 3) AS nichicon_charge_kwh,
    ROUND(SUM(ecoflow_charge_kwh), 3) AS ecoflow_charge_kwh,
    ROUND(SUM(self_consumption_shift_kwh), 3) AS self_consumption_shift_kwh,
    CAST(ROUND(SUM(saving_yen)) AS INTEGER) AS saving_yen
  FROM daily_all
  GROUP BY strftime('%Y-%m', date)
  ORDER BY month
);

SELECT json_object(
  'generated_at', datetime('now','localtime'),
  'buy_price_yen_per_kwh', ${BUY_PRICE_YEN_PER_KWH},
  'sell_price_yen_per_kwh', ${SELL_PRICE_YEN_PER_KWH},
  'ecoflow_data_since', (SELECT MIN(date(recorded_at)) FROM ecoflow_power_history),
  'nichicon_data_since', (SELECT MIN(date) FROM nichicon_battery_history),
  'power_history_since', (SELECT MIN(date(recorded_at)) FROM power_history)
);
SQL
)

RESULT=$(printf '%s\n' "${SQL}" | run_sql)

DAILY_JSON=$(printf '%s\n' "${RESULT}" | sed -n '1p')
MONTHLY_JSON=$(printf '%s\n' "${RESULT}" | sed -n '2p')
META_JSON=$(printf '%s\n' "${RESULT}" | sed -n '3p')

if [[ -z "${DAILY_JSON}" || -z "${MONTHLY_JSON}" || -z "${META_JSON}" ]]; then
    echo "aggregate.sh: 集計結果が空です。SSH接続・DBパス・権限を確認してください。" >&2
    exit 1
fi

printf '%s\n' "${DAILY_JSON}" | python3 -m json.tool --indent 2 --sort-keys=false > "${OUT_DIR}/daily.json" 2>/dev/null \
    || printf '%s' "${DAILY_JSON}" > "${OUT_DIR}/daily.json"
printf '%s\n' "${MONTHLY_JSON}" | python3 -m json.tool --indent 2 --sort-keys=false > "${OUT_DIR}/monthly.json" 2>/dev/null \
    || printf '%s' "${MONTHLY_JSON}" > "${OUT_DIR}/monthly.json"
printf '%s\n' "${META_JSON}" | python3 -m json.tool --indent 2 --sort-keys=false > "${OUT_DIR}/meta.json" 2>/dev/null \
    || printf '%s' "${META_JSON}" > "${OUT_DIR}/meta.json"

echo "wrote:"
echo "  ${OUT_DIR}/daily.json   ($(printf '%s' "${DAILY_JSON}" | python3 -c 'import json,sys; print(len(json.load(sys.stdin)))' 2>/dev/null || echo '?') rows)"
echo "  ${OUT_DIR}/monthly.json ($(printf '%s' "${MONTHLY_JSON}" | python3 -c 'import json,sys; print(len(json.load(sys.stdin)))' 2>/dev/null || echo '?') rows)"
echo "  ${OUT_DIR}/meta.json"
