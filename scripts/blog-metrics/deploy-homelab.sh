#!/usr/bin/env bash
#
# deploy-homelab.sh — Mac からblog-metricsの実行スクリプト一式（aggregate.sh/build_daily.py/
# validate_metrics.py/layer_model.py/bill_model.py/run-daily.sh + tariff.json/official_*.json
# + systemd unit）を homelab の /opt/blog-metrics/ へ rsync するだけの配備スクリプト。
#
# 本スクリプト自身は blog-metrics.timer の有効化・起動は行わない（オーナーが判断の上、
# 案内されたコマンドを手動実行する）。本スクリプト自体もこのタスクでは実行しない
# （オーナーが実行する）。
#
# 使い方:
#   scripts/blog-metrics/deploy-homelab.sh --service-user NAME [options]
#
# Options:
#   --host HOST          デプロイ先SSHホスト（既定: homelab。~/.ssh/config のHostエイリアス
#                         推奨。空文字を指定するとsshを使わずローカルパスとして--destに
#                         そのままrsyncする — 動作確認用）
#   --dest PATH           配備先ディレクトリ（既定: /opt/blog-metrics。絶対パスかつ "/" 自体は
#                         不可 — rsync --delete の対象になるため誤入力を軽く検証する）
#   --service-user NAME   blog-metrics.service の User= に書き込む実行ユーザー名（必須。
#                         公開リポジトリの blog-metrics.service にはプレースホルダしか
#                         置いていないため、配備のたびにここで指定する）
#   --install-units       rsync 後、リモートで systemd unit を sudo install + daemon-reload
#                         する（確認プロンプトあり。指定しない場合はrsyncのみでunit配置は
#                         オーナーが別途行う）
#   --dry-run             rsync --dry-run のみ行う（--install-units があっても無視する）
#   -h, --help            このヘルプを表示
#
# 例:
#   scripts/blog-metrics/deploy-homelab.sh --service-user <homelabの実行ユーザー名> --dry-run
#   scripts/blog-metrics/deploy-homelab.sh --service-user <homelabの実行ユーザー名> --install-units
#
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/../.." && pwd)"

HOST="homelab"
DEST="/opt/blog-metrics"
SERVICE_USER=""
INSTALL_UNITS=false
DRY_RUN=false

usage() {
    grep -E '^#( |$)' "${BASH_SOURCE[0]}" | sed -E 's/^# ?//'
    exit "${1:-0}"
}

while [[ $# -gt 0 ]]; do
    case "$1" in
        --host) HOST="$2"; shift 2 ;;
        --dest) DEST="$2"; shift 2 ;;
        --service-user) SERVICE_USER="$2"; shift 2 ;;
        --install-units) INSTALL_UNITS=true; shift ;;
        --dry-run) DRY_RUN=true; shift ;;
        -h|--help) usage 0 ;;
        *) echo "unknown option: $1" >&2; usage 1 ;;
    esac
done

if [[ -z "${SERVICE_USER}" ]]; then
    echo "deploy-homelab.sh: --service-user は必須です（blog-metrics.service の User= に書き込む実行ユーザー名）" >&2
    exit 1
fi

# F9: rsync --delete の対象になるため、--dest の typo で危険なパスを渡さないよう軽く検証する。
case "${DEST}" in
    /*) ;;
    *) echo "deploy-homelab.sh: --dest は絶対パスで指定してください: ${DEST}" >&2; exit 1 ;;
esac
if [[ "${DEST}" == "/" || "${#DEST}" -lt 3 ]]; then
    echo "deploy-homelab.sh: --dest が危険なパスです（rsync --delete の対象になります）: ${DEST}" >&2
    exit 1
fi

# --- bundle をローカルの一時ディレクトリに組み立てる ---
BUNDLE=$(mktemp -d)
trap 'rm -rf "${BUNDLE}"' EXIT

mkdir -p "${BUNDLE}/inputs" "${BUNDLE}/systemd"
cp "${SCRIPT_DIR}/aggregate.sh" "${BUNDLE}/aggregate.sh"
cp "${SCRIPT_DIR}/build_daily.py" "${BUNDLE}/build_daily.py"
cp "${SCRIPT_DIR}/validate_metrics.py" "${BUNDLE}/validate_metrics.py"
cp "${SCRIPT_DIR}/layer_model.py" "${BUNDLE}/layer_model.py"
cp "${SCRIPT_DIR}/bill_model.py" "${BUNDLE}/bill_model.py"
cp "${SCRIPT_DIR}/run-daily.sh" "${BUNDLE}/run-daily.sh"
NEW_TARIFF="${BUNDLE}/inputs/tariff.json"
cp "${SCRIPT_DIR}/tariff.json" "${NEW_TARIFF}"

# official_buy.json/official_sell.json は private repo (energy-archive) 由来で、
# Mac 上で import_official_buy.py/import_official_sell.py をオーナーが手動実行した
# 最新版をそのまま同梱する（本スクリプトはそれらを再生成しない）。
for name in official_buy official_sell; do
    src="${REPO_ROOT}/data/metrics/${name}.json"
    if [[ -f "${src}" ]]; then
        cp "${src}" "${BUNDLE}/inputs/${name}.json"
    else
        echo "deploy-homelab.sh: ${src} が無いため同梱をスキップします" >&2
    fi
done

# blog-metrics.service のプレースホルダ(__SERVICE_USER__)を実ユーザー名に置換してから同梱する
# （公開リポジトリの元ファイルには実ユーザー名を書かないため。QA指摘F7）。
sed "s/__SERVICE_USER__/${SERVICE_USER}/" "${SCRIPT_DIR}/systemd/blog-metrics.service" > "${BUNDLE}/systemd/blog-metrics.service"
cp "${SCRIPT_DIR}/systemd/blog-metrics.timer" "${BUNDLE}/systemd/blog-metrics.timer"

git -C "${REPO_ROOT}" rev-parse --short HEAD > "${BUNDLE}/BUNDLE_REV" 2>/dev/null || echo "unknown" > "${BUNDLE}/BUNDLE_REV"

chmod +x "${BUNDLE}"/*.sh

echo "bundle contents ($(cat "${BUNDLE}/BUNDLE_REV")):"
find "${BUNDLE}" -type f | sed "s#${BUNDLE}/##" | sort | sed 's/^/  /'

# --- F5b: tariff.json が変更される配備なら、次回 run-daily.sh 実行時に1回だけ
# validate_metrics.py の G9(履歴不変性)をスキップさせるフラグを置く準備をする。
# tariff.jsonに新しい請求月の単価が追加されると、暫定単価を使っていた過去日の値が
# (正しく)動くことがあり、そのままだとG9がpushを拒否してパイプラインが止まるため。
# rsync前の「配備先に現在ある」tariff.jsonのハッシュを先に取得しておく。
remote_sha256() {
    # $1 = 絶対パス。ファイルが無い/取得失敗時は空文字を返す。初回配備時は配備先に何も
    # 無いのが正常系のため、$1が無くてもこの関数自体は必ず exit 0 で返す
    # （failしてset -eでスクリプト全体が落ちないようにする）。
    if [[ -z "${HOST}" ]]; then
        if [[ -f "$1" ]]; then
            { shasum -a 256 "$1" 2>/dev/null || sha256sum "$1" 2>/dev/null; } | awk '{print $1}'
        fi
    else
        ssh "${HOST}" "sha256sum '$1' 2>/dev/null | awk '{print \$1}'" 2>/dev/null || true
    fi
    return 0
}
OLD_TARIFF_SHA=$(remote_sha256 "${DEST}/inputs/tariff.json")
NEW_TARIFF_SHA=$({ shasum -a 256 "${NEW_TARIFF}" 2>/dev/null || sha256sum "${NEW_TARIFF}" 2>/dev/null; } | awk '{print $1}')

RSYNC_OPTS=(-avz --delete)
if [[ "${DRY_RUN}" == true ]]; then
    RSYNC_OPTS+=(--dry-run)
fi

echo ""
if [[ -z "${HOST}" ]]; then
    echo "rsync -> ${DEST}/ （ローカルパス。--host が空のため ssh は使いません）"
    mkdir -p "${DEST}"
    rsync "${RSYNC_OPTS[@]}" "${BUNDLE}/" "${DEST}/"
else
    echo "rsync -> ${HOST}:${DEST}/"
    rsync "${RSYNC_OPTS[@]}" "${BUNDLE}/" "${HOST}:${DEST}/"
fi

if [[ "${DRY_RUN}" == true ]]; then
    echo "deploy-homelab.sh: --dry-run のため systemd unit のインストール・allow-history-onceフラグ設定は行いません"
    exit 0
fi

# state dir は systemd StateDirectory=blog-metrics が作る /var/lib/blog-metrics に統一する
# （blog-metrics.service の ExecStart --state-dir と揃える。QA指摘#5/#6）。
STATE_DIR="/var/lib/blog-metrics"
if [[ -n "${OLD_TARIFF_SHA}" && "${OLD_TARIFF_SHA}" != "${NEW_TARIFF_SHA}" ]]; then
    echo ""
    echo "tariff.json の変更を検出したため、次回 run-daily.sh 実行時のみ G9(履歴不変性)を"
    echo "スキップする allow-history-once フラグを設定します"
    if [[ -z "${HOST}" ]]; then
        # ローカル動作確認用(--hostが空)は /var/lib への書き込み権限が無いことが多いため、
        # 失敗しても警告のみでスクリプト全体は継続する（本番はHOST指定の下のsudo経路を使う）。
        mkdir -p "${STATE_DIR}" 2>/dev/null && touch "${STATE_DIR}/allow-history-once" 2>/dev/null \
            || echo "deploy-homelab.sh: ${STATE_DIR} に書き込めないためallow-history-onceフラグ設定をスキップしました（ローカル動作確認用のため許容）" >&2
    else
        ssh "${HOST}" "sudo install -d -o '${SERVICE_USER}' '${STATE_DIR}' && sudo -u '${SERVICE_USER}' touch '${STATE_DIR}/allow-history-once'"
    fi
fi

if [[ "${INSTALL_UNITS}" != true ]]; then
    echo ""
    echo "deploy-homelab.sh: 完了（rsyncのみ。--install-units を付けていないため systemd unit の"
    echo "  インストールは行っていません。手動で行うか、次回 --install-units 付きで再実行してください）"
    exit 0
fi

echo ""
echo "systemd unit をインストールします（sudo install + daemon-reload のみ。有効化・起動は行いません）"
if [[ -z "${HOST}" ]]; then
    echo "deploy-homelab.sh: --host が空のため systemd unit のインストールはスキップします（ローカル動作確認用）" >&2
else
    echo "対象ホスト: ${HOST} / 配備先: ${DEST}"
    read -r -p "リモートで sudo install + systemctl daemon-reload を実行します。よろしいですか？ [y/N] " CONFIRM
    case "${CONFIRM}" in
        y|Y|yes|YES) ;;
        *) echo "deploy-homelab.sh: 中断しました（rsyncは完了済みです）"; exit 0 ;;
    esac
    ssh "${HOST}" "sudo install -m 644 '${DEST}/systemd/blog-metrics.service' /etc/systemd/system/blog-metrics.service && \
        sudo install -m 644 '${DEST}/systemd/blog-metrics.timer' /etc/systemd/system/blog-metrics.timer && \
        sudo systemctl daemon-reload"
fi

echo ""
echo "deploy-homelab.sh: 完了。有効化する場合はオーナーが以下を実行してください:"
echo "  ssh ${HOST:-<homelab>} 'sudo systemctl enable --now blog-metrics.timer'"
