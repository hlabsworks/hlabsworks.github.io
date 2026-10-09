#!/usr/bin/env bash
# site-health-check.sh — 公開サイト（hlabsworks.com）の健全性を homelab から確認し、
# システム的な障害だけを LINE（notify.sh、ACTION）で知らせる。
#
# 背景（2026-10-09〜10）: サイト CI の単体テストが日付依存で落ち、定時ビルドが 2 日間失敗して
# 実績ダッシュボードが更新されないまま気づけなかった。CI 失敗は LINE を使わない設計だったため。
# オーナー指示 2026-10-10: LINE 通知が激減したので、システム的なクリティカル問題は LINE で知らせる。
#
# 確認すること:
#   1. GitHub Actions の hugo.yml 最新完了ランが success でなければ通知（同じランで再送しない。
#      次の success で復旧を 1 通）。公開リポジトリなので認証なしの REST API で読める。
#   2. 公開中のダッシュボードページ（--site-page、既定 /labs/solar/metrics/）に埋め込まれた
#      "generated_at" の最新値が、homelab のデータ repo clone の meta.json の generated_at より
#      --max-lag-hours（既定 30）以上古ければ通知（同じ遅れで再送しない。追いついたら復旧を
#      1 通）。ビルド成功なのに Pages 配信が古い場合もこれで拾う。Hugo は data/ 配下の JSON を
#      そのまま配信しないため、ページに埋め込まれた値を読む。
#   ネットワーク到達不能（curl 失敗）はログに残すだけで通知しない（homelab 側の一時的な断で
#   LINE 枠を消費しない）。
#
# 環境変数（テスト用）:
#   BLOG_METRICS_NOTIFY_CMD  通知コマンド（既定 /usr/local/bin/notify.sh、"$1"=件名 "$2"=本文）
#   BLOG_METRICS_CURL        curl の代替（既定 curl）。テストでは固定応答を返すスタブに差し替える
#   BLOG_METRICS_NOW         現在時刻 'YYYY-MM-DD HH:MM:SS'（JST、テスト専用）
#
# 終了コード: 0（確認できた／通知した）、2（引数エラー）。到達不能は 0 で終える（ログのみ）。
set -euo pipefail

STATE_DIR="${HOME}/.local/state/blog-metrics"
LOG_FILE="/var/log/blog-metrics/site-health.log"
SITE_URL="https://hlabsworks.com"
SITE_PAGE="/labs/solar/metrics/"
REPO="hlabsworks/hlabsworks.github.io"
WORKFLOW="hugo.yml"
DATA_CLONE="${HOME}/solar-metrics-data"
MAX_LAG_HOURS=30

usage() {
    sed -n '2,24p' "$0" | sed 's/^# \{0,1\}//'
    echo "使い方: $0 [--state-dir DIR] [--log-file F] [--site-url URL] [--site-page PATH] [--repo OWNER/NAME] [--workflow FILE] [--data-clone DIR] [--max-lag-hours N]"
}

while [[ $# -gt 0 ]]; do
    case "$1" in
        --state-dir) STATE_DIR="$2"; shift 2 ;;
        --log-file) LOG_FILE="$2"; shift 2 ;;
        --site-url) SITE_URL="${2%/}"; shift 2 ;;
        --site-page) SITE_PAGE="$2"; shift 2 ;;
        --repo) REPO="$2"; shift 2 ;;
        --workflow) WORKFLOW="$2"; shift 2 ;;
        --data-clone) DATA_CLONE="$2"; shift 2 ;;
        --max-lag-hours) MAX_LAG_HOURS="$2"; shift 2 ;;
        -h|--help) usage; exit 0 ;;
        *) echo "site-health-check.sh: 不明な引数: $1" >&2; usage >&2; exit 2 ;;
    esac
done

STATE_FILE="${STATE_DIR}/site_health.json"
CURL="${BLOG_METRICS_CURL:-curl}"
mkdir -p "${STATE_DIR}" "$(dirname "${LOG_FILE}")" 2>/dev/null || true

log() {
    local line="$(date '+%Y-%m-%d %H:%M:%S') $*"
    echo "${line}"
    echo "${line}" >> "${LOG_FILE}" 2>/dev/null || true
}

notify() {
    local cmd="${BLOG_METRICS_NOTIFY_CMD:-/usr/local/bin/notify.sh}"
    if "${cmd}" "$1" "$2"; then
        log "notify: $1"
    else
        log "notify failed (rc=$?): $1"
    fi
}

fetch() {
    # $1=URL。本文を stdout に。失敗時は非0（呼び出し側がログのみで続行する）。
    "${CURL}" -fsSL --max-time 30 -H 'Accept: application/vnd.github+json' -H 'User-Agent: blog-metrics-site-health' "$1"
}

TMP="$(mktemp -d)"
trap 'rm -rf "${TMP}"' EXIT

RUNS_OK=true
if ! fetch "https://api.github.com/repos/${REPO}/actions/workflows/${WORKFLOW}/runs?status=completed&per_page=5" > "${TMP}/runs.json"; then
    log "GitHub Actions の実行一覧を取得できませんでした（到達不能。通知しません）"
    RUNS_OK=false
fi

SITE_OK=true
if ! fetch "${SITE_URL}${SITE_PAGE}" > "${TMP}/site_page.html"; then
    log "公開サイトのダッシュボードページを取得できませんでした（到達不能。通知しません）"
    SITE_OK=false
fi

LOCAL_META="${DATA_CLONE}/data/metrics/meta.json"
if [[ ! -f "${LOCAL_META}" ]]; then
    log "データ repo clone の meta.json がありません: ${LOCAL_META}（鮮度確認をスキップ）"
    SITE_OK=false
fi

# 判定本体は python3（JSON の扱いと時刻計算のため）。stdout に「通知の件名\t本文」を 0 行以上出す。
python3 - "${STATE_FILE}" "${TMP}/runs.json" "${RUNS_OK}" "${TMP}/site_page.html" "${LOCAL_META}" "${SITE_OK}" \
    "${MAX_LAG_HOURS}" "${REPO}" "${SITE_URL}" "${BLOG_METRICS_NOW:-}" > "${TMP}/actions.tsv" <<'PY'
import json, re, sys
from datetime import datetime, timedelta
from pathlib import Path

(state_path, runs_path, runs_ok, site_page_path, local_meta_path, site_ok,
 max_lag_hours, repo, site_url, now_override) = sys.argv[1:11]
runs_ok = runs_ok == "true"
site_ok = site_ok == "true"
max_lag = timedelta(hours=float(max_lag_hours))
now = datetime.strptime(now_override, "%Y-%m-%d %H:%M:%S") if now_override else datetime.now()

try:
    state = json.loads(Path(state_path).read_text(encoding="utf-8"))
except (FileNotFoundError, json.JSONDecodeError):
    state = {}
state.setdefault("build", {"alerted_run_id": None})
state.setdefault("freshness", {"alerted_local_generated_at": None})
out = []

def parse_ts(value):
    try:
        return datetime.strptime(str(value), "%Y-%m-%d %H:%M:%S")
    except (TypeError, ValueError):
        return None

# 1. ビルド結果
if runs_ok:
    try:
        runs = json.loads(Path(runs_path).read_text(encoding="utf-8")).get("workflow_runs") or []
    except (json.JSONDecodeError, AttributeError):
        runs = []
    latest = runs[0] if runs else None
    b = state["build"]
    if latest is None:
        print("site-health: ワークフローの完了ランが見つかりません（判定スキップ）", file=sys.stderr)
    elif latest.get("conclusion") != "success":
        if b.get("alerted_run_id") != latest.get("id"):
            b["alerted_run_id"] = latest.get("id")
            b["alerted_conclusion"] = latest.get("conclusion")
            out.append(("サイトのビルド失敗",
                        f"hlabsworks.com の自動ビルド（{latest.get('event')}）が {latest.get('conclusion')} で終わりました。"
                        f"実績ダッシュボードと予約記事が更新されません。{latest.get('html_url')}"))
        else:
            print(f"site-health: ビルド失敗は通知済み（run {latest.get('id')}）", file=sys.stderr)
    else:
        if b.get("alerted_run_id") is not None:
            out.append(("サイトのビルド復旧", f"hlabsworks.com の自動ビルドが成功に戻りました。{latest.get('html_url')}"))
        b["alerted_run_id"] = None
        b.pop("alerted_conclusion", None)

# 2. 公開データの鮮度
if site_ok:
    # ページには複数のデータ（layers/meta 等）の generated_at が埋め込まれる。同じ run で数秒差なので最新値を採る
    found = re.findall(r'"generated_at"\s*:\s*"(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2})"',
                       Path(site_page_path).read_text(encoding="utf-8", errors="replace"))
    site_gen = max((parse_ts(v) for v in found), default=None)
    try:
        local_gen = parse_ts(json.loads(Path(local_meta_path).read_text(encoding="utf-8")).get("generated_at"))
    except (json.JSONDecodeError, AttributeError):
        local_gen = None
    f = state["freshness"]
    if site_gen is None or local_gen is None:
        print("site-health: generated_at を読めません（鮮度判定スキップ）", file=sys.stderr)
    elif local_gen - site_gen > max_lag:
        key = local_gen.strftime("%Y-%m-%d %H:%M:%S")
        if f.get("alerted_local_generated_at") != key:
            f["alerted_local_generated_at"] = key
            out.append(("サイトのデータが古い",
                        f"公開中の実績ダッシュボードのデータ（{site_gen:%m/%d %H:%M} 生成）が、homelab の最新（{local_gen:%m/%d %H:%M} 生成）より"
                        f"{(local_gen - site_gen).total_seconds() / 3600:.0f} 時間古いままです。ビルドか配信が止まっている可能性があります。"))
        else:
            print("site-health: データ遅延は通知済み", file=sys.stderr)
    else:
        if f.get("alerted_local_generated_at") is not None:
            out.append(("サイトのデータ復旧", f"公開中の実績ダッシュボードが最新（{site_gen:%m/%d %H:%M} 生成）に追いつきました。"))
        f["alerted_local_generated_at"] = None

Path(state_path).write_text(json.dumps(state, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
for subject, body in out:
    print(f"{subject}\t{body}")
PY

while IFS=$'\t' read -r subject body; do
    [[ -z "${subject}" ]] && continue
    notify "${subject}" "${body}"
done < "${TMP}/actions.tsv"
log "site-health-check: 完了（build_check=${RUNS_OK} freshness_check=${SITE_OK}）"
