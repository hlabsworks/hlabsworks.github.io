#!/usr/bin/env bash
#
# aggregate.sh — SolarChargeController の metrics-export.sh（本番 Pi 側の CLI）から
# 日次/月次/5分プロファイルの「集計値のみ」を取得し、build_daily.py(1st pass) ->
# layer_model.py -> build_daily.py(2nd pass、publish_sinceで絞る) -> bill_model.py の順に
# オーケストレーションして Hugo の data ディレクトリ
# (data/metrics/{daily,monthly,meta,bills,layers}.json) を更新する。
#
# 設計方針（重要・変更時は維持すること）:
#   - 本スクリプト自身は SQL を発行しない。日次/月次のkWh積分・DBアクセスは
#     metrics-export.sh（SolarChargeController リポジトリ）側の責務。本スクリプトは
#     その出力(JSON/CSV)を受け取って整形するオーケストレータに徹する。
#   - 本番 Pi・SolarChargeController の DB へは metrics-export.sh 経由でのみアクセスする
#     （ssh 先で直接 sqlite3 は呼ばない）。
#   - 5分プロファイル（energy_profile_5min、時間帯粒度）はシェル変数(メモリ)経由でのみ
#     扱い、ファイルには一切書かない（--local でのローカル検証時も同様）。
#   - daily/meta の生エクスポート(JSON)は一時ディレクトリ(mktemp -d、trapで削除)に
#     書いてbuild_daily.pyに渡す。これらは暦日・暦月粒度の集計値のみで時間帯粒度を
#     含まないため、一時ファイルとして扱って問題ない。
#   - bill_model.py は必ず layer_model.py の後に実行する（layer_model.py が書く
#     .cache/daily_load.json をbill_model.pyのL0算出が使うため）。
#   - build_daily.py は2回実行する（オーナー決定2026-09-23、publish_sinceによる公開範囲
#     フィルタの導入）。1st passは全履歴で daily.json を作り、layer_model.py が
#     L2のSOC連続性・確定月判定に全履歴を使えるようにする。layer_model.py が
#     自動算出したparams.profile_sinceを読み取り、2nd passでdaily.json/monthly.json/
#     meta.jsonをpublish_sinceで絞って上書きする（最終的に公開するのは2nd passの出力）。
#
# 使い方:
#   scripts/blog-metrics/aggregate.sh [options]
#
# Options:
#   --export-cmd PATH   metrics-export.sh のパス（既定: /usr/local/bin/metrics-export.sh。
#                        ssh先ではこのパスをそのまま実行する。--local時はこのパスを
#                        `bash PATH --db DB <id>` として直接実行する）
#   --ssh-host HOST     SSH 接続先（既定: solarchgctl-metrics。~/.ssh/config のHostエイリアス。
#                        --local指定時は無視）。
#   --local DB_PATH     ssh を使わず、ローカルの --export-cmd を `--db DB_PATH` 付きで
#                        直接実行する（動作確認用。fixture DB 等に対して使う）
#   --profile-since DATE 5分プロファイル取得の開始日（既定: 今日から420日前）
#   --official-dir DIR  official_buy.json/official_sell.json の取得元ディレクトリ
#                        （既定: <repo>/data/metrics。homelab では /opt/blog-metrics/inputs
#                        を指定する運用）
#   --tariff PATH       tariff.json のパス（既定: scripts/blog-metrics/tariff.json）
#   --out DIR           出力先ディレクトリ（既定: <repo>/data/metrics）
#   -h, --help          このヘルプを表示
#
# 例:
#   # 本番 Pi に対して実行（homelab の run-daily.sh から呼ばれる想定）
#   scripts/blog-metrics/aggregate.sh
#
#   # Mac からも同じスクリプトで再生成できる（official-dir は既定のままでよい。
#   # ~/.ssh/config に Host エイリアスが無い環境では <user>@solarchgctl.local のように直接指定）
#   scripts/blog-metrics/aggregate.sh --ssh-host <user>@solarchgctl.local --export-cmd /usr/local/bin/metrics-export.sh
#
#   # ローカル fixture DB での動作確認
#   scripts/blog-metrics/aggregate.sh --local /tmp/fixture.db --export-cmd ../SolarChargeController/scripts/metrics-export.sh
#
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/../.." && pwd)"

EXPORT_CMD="/usr/local/bin/metrics-export.sh"
# 既定は ~/.ssh/config の Host エイリアス（実ユーザー名/IPを本リポジトリに書かないため。
# 各環境の ~/.ssh/config で `Host solarchgctl-metrics` に実ホスト・ユーザー・鍵を設定する）。
SSH_HOST="solarchgctl-metrics"
LOCAL_MODE=false
LOCAL_DB=""
PROFILE_SINCE=""
OFFICIAL_DIR="${REPO_ROOT}/data/metrics"
TARIFF_JSON="${SCRIPT_DIR}/tariff.json"
OUT_DIR="${REPO_ROOT}/data/metrics"
# layer_model.py -> bill_model.py 間の中間ファイル（L0算出用）。Hugoは読まないため
# data/metrics/ には置かない（.gitignore済み）。
CACHE_DIR="${SCRIPT_DIR}/.cache"

usage() {
    grep -E '^#( |$)' "${BASH_SOURCE[0]}" | sed -E 's/^# ?//'
    exit "${1:-0}"
}

while [[ $# -gt 0 ]]; do
    case "$1" in
        --export-cmd) EXPORT_CMD="$2"; shift 2 ;;
        --ssh-host) SSH_HOST="$2"; shift 2 ;;
        --local) LOCAL_MODE=true; LOCAL_DB="$2"; shift 2 ;;
        --profile-since) PROFILE_SINCE="$2"; shift 2 ;;
        --official-dir) OFFICIAL_DIR="$2"; shift 2 ;;
        --tariff) TARIFF_JSON="$2"; shift 2 ;;
        --out) OUT_DIR="$2"; shift 2 ;;
        -h|--help) usage 0 ;;
        *) echo "unknown option: $1" >&2; usage 1 ;;
    esac
done

if [[ -z "${PROFILE_SINCE}" ]]; then
    PROFILE_SINCE=$(python3 -c 'from datetime import date, timedelta; print((date.today() - timedelta(days=420)).isoformat())')
fi

mkdir -p "${OUT_DIR}" "${CACHE_DIR}"

TMP_DIR=$(mktemp -d)
trap 'rm -rf "${TMP_DIR}"' EXIT

# --- metrics-export.sh 呼び出し（daily/meta はファイル経由、profileはメモリ経由のみ） ---
run_export() {
    # $@ = metrics-export.sh に渡すサブコマンド（例: daily / meta / "profile 2026-09-01"）
    if [[ "${LOCAL_MODE}" == true ]]; then
        bash "${EXPORT_CMD}" --db "${LOCAL_DB}" "$@"
    else
        ssh "${SSH_HOST}" "${EXPORT_CMD} $*"
    fi
}

# metrics-export.sh の profile クエリは "bucket_at >= 指定日" の1本のクエリで、指定日以降の
# 全バケットを一度に返す（1日ずつ区切る仕様ではない）。そのため呼び出しは PROFILE_SINCE を
# 指定した1回だけでよい。
fetch_profile_csv() {
    local since="$1"
    run_export profile "${since}"
}

DAILY_EXPORT_FILE="${TMP_DIR}/daily_export.json"
META_EXPORT_FILE="${TMP_DIR}/meta_export.json"

run_export daily > "${DAILY_EXPORT_FILE}"
run_export meta > "${META_EXPORT_FILE}"

if [[ ! -s "${DAILY_EXPORT_FILE}" || ! -s "${META_EXPORT_FILE}" ]]; then
    echo "aggregate.sh: metrics-export.sh の daily/meta 出力が空です。接続・--export-cmd・--ssh-host/--local を確認してください。" >&2
    exit 1
fi

# --- 5分プロファイル（メモリ(シェル変数)上のみで扱い、ファイルには書かない）---
# 0行の場合、以前はlayers.json生成をスキップしてexit 0していたが、それだと前回実行分の
# stale なlayers.jsonがそのまま(意図せず)pushされてしまう(QA指摘)。0行はDB接続不調等の
# 異常を示すため、理由をstderrに出して非0終了し、run-daily.sh側に「今回の出力は不完全」と
# 伝える（run-daily.sh は5ファイル(daily/monthly/meta/bills/layers)が揃わなければ
# commit・pushしない）。
PROFILE_CSV=$(fetch_profile_csv "${PROFILE_SINCE}")
PROFILE_ROW_COUNT=0
if [[ -n "${PROFILE_CSV}" ]]; then
    PROFILE_ROW_COUNT=$(($(printf '%s\n' "${PROFILE_CSV}" | wc -l) - 1))
fi
if [[ "${PROFILE_ROW_COUNT}" -le 0 ]]; then
    echo "aggregate.sh: 5分プロファイルが0行でした（--profile-since=${PROFILE_SINCE}）。metrics-export.sh の疎通・energy_profile_5min テーブルの有無を確認してください。layers.json は生成しません。" >&2
    exit 1
fi

# --- build_daily.py 1st pass（daily.json/monthly.json/meta.json を生成。layer_model.py が
# L2のSOC連続性・確定月判定に全履歴を必要とするため、この時点ではpublish_sinceで絞らない） ---
python3 "${SCRIPT_DIR}/build_daily.py" \
    --daily-export "${DAILY_EXPORT_FILE}" \
    --meta-export "${META_EXPORT_FILE}" \
    --tariff "${TARIFF_JSON}" \
    --out-dir "${OUT_DIR}"

# --- official_sell.json / official_buy.json（--official-dir にあれば取り込む。
# 生成自体は import_official_sell.py/import_official_buy.py をオーナーが別途 Mac 上で
# 手動実行し、deploy-homelab.sh が inputs/ に同梱する運用に変更した。aggregate.sh は
# ここでは単にコピーするだけで、energy-archive 等の private ソースには触れない） ---
copy_official() {
    local name="$1"
    local src="${OFFICIAL_DIR}/${name}.json"
    local dst="${OUT_DIR}/${name}.json"
    if [[ ! -f "${src}" ]]; then
        echo "aggregate.sh: ${src} が無いため ${name}.json 更新をスキップします" >&2
        return
    fi
    local src_real dst_real
    src_real="$(cd "$(dirname "${src}")" && pwd -P)/$(basename "${src}")"
    dst_real=""
    if [[ -f "${dst}" ]]; then
        dst_real="$(cd "$(dirname "${dst}")" && pwd -P)/$(basename "${dst}")"
    fi
    if [[ "${src_real}" == "${dst_real}" ]]; then
        return
    fi
    cp "${src}" "${dst}"
    echo "copied: ${src} -> ${dst}"
}
copy_official official_buy
copy_official official_sell

# --- layer_model.py（layers.json を生成。PROFILE_CSV は上で0行でないことを確認済み） ---
printf '%s\n' "${PROFILE_CSV}" | python3 "${SCRIPT_DIR}/layer_model.py" \
    --tariff "${TARIFF_JSON}" \
    --daily "${OUT_DIR}/daily.json" \
    --official-sell "${OUT_DIR}/official_sell.json" \
    --official-buy "${OUT_DIR}/official_buy.json" \
    --out "${OUT_DIR}/layers.json" \
    --daily-load-out "${CACHE_DIR}/daily_load.json" \
    --profile-source "pi_energy_profile_5min（dt加重ゼロ次ホールド、SolarChargeController V1.00.059〜）"

# --- publish_since の解決（オーナー決定2026-09-23: 全チャネルが揃う日付より前の断片的な
# データを公開しない）。layer_model.py が自動算出しlayers.jsonに書いたparams.profile_sinceを
# そのまま「公開開始日」として採用し、build_daily.py 2nd pass / bill_model.py に明示的に渡す
# （build_daily.py は5分プロファイルを扱わないため profile_since を自己算出できない。
# bill_model.py も同様のため、layer_model.py が求めた値をここで一度だけ解決して共有する） ---
PUBLISH_SINCE=$(python3 -c "
import json
with open('${OUT_DIR}/layers.json', encoding='utf-8') as f:
    print(json.load(f)['params']['profile_since'] or '')
")

# --- build_daily.py 2nd pass（daily.json/monthly.json/meta.json をpublish_sinceで絞って
# 上書きする。公開JSONの最終版はこちら） ---
if [[ -n "${PUBLISH_SINCE}" ]]; then
    python3 "${SCRIPT_DIR}/build_daily.py" \
        --daily-export "${DAILY_EXPORT_FILE}" \
        --meta-export "${META_EXPORT_FILE}" \
        --tariff "${TARIFF_JSON}" \
        --out-dir "${OUT_DIR}" \
        --publish-since "${PUBLISH_SINCE}"
else
    echo "aggregate.sh: layers.json の params.profile_since が未確定のため、daily.json/monthly.json/meta.json は公開範囲フィルタなしのまま出力します" >&2
fi

# --- bill_model.py（必ず layer_model.py の後に実行する順序を守る。daily.json は2nd pass後の
# publish_since適用済みファイルを読むため、明示的な --publish-since は無くても候補月は自然に
# 絞られるが、過去月がexcluded_monthsに残留しないよう明示的にも渡す） ---
python3 "${SCRIPT_DIR}/bill_model.py" \
    --tariff "${TARIFF_JSON}" \
    --daily "${OUT_DIR}/daily.json" \
    --official-sell "${OUT_DIR}/official_sell.json" \
    --official-buy "${OUT_DIR}/official_buy.json" \
    --daily-load "${CACHE_DIR}/daily_load.json" \
    --out "${OUT_DIR}/bills.json" \
    ${PUBLISH_SINCE:+--publish-since "${PUBLISH_SINCE}"}

echo "aggregate.sh: done (profile rows: ${PROFILE_ROW_COUNT}, publish_since: ${PUBLISH_SINCE:-未確定})"
