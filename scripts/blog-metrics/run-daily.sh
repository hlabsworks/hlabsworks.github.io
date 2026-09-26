#!/usr/bin/env bash
#
# run-daily.sh — homelab (Pi) の systemd timer から1日3回呼ばれるオーケストレータ。
# aggregate.sh でメトリクスを再生成し、変更があれば別リポジトリ solar-metrics-data
# （~/solar-metrics-data）に commit・push する。validate_metrics.py で自己検証してから
# push するため、壊れたデータが公開リポジトリに渡ることはない。
#
# 使い方:
#   scripts/blog-metrics/run-daily.sh [options]
#
# Options:
#   --dry-run                何も実行せず終了する（flock・aggregate.sh・git・通知いずれも呼ばない）
#   --blog-metrics-dir DIR   bundle のルート（既定: /opt/blog-metrics。aggregate.sh/
#                            build_daily.py/validate_metrics.py/inputs/ を含む）
#   --clone-dir DIR          solar-metrics-data のローカルclone（既定: ~/solar-metrics-data）
#   --lock-file PATH         flock対象ファイル（既定: /run/blog-metrics/run.lock。
#                            systemd unit の RuntimeDirectory=blog-metrics が /run/blog-metrics/
#                            を作成する前提。サービス実行ユーザーで書けない場合は変更する）
#   --state-dir DIR          最終ハッシュ・連続失敗カウンタの保存先
#                            （既定: ~/.local/state/blog-metrics）
#   --log-file PATH          詳細ログの出力先（既定: /var/log/blog-metrics/run.log。
#                            1MiB超で末尾のみ残して切り詰める）
#   --ssh-host HOST          aggregate.sh に渡すSSH接続先（既定: solarchgctl-metrics。
#                            ~/.ssh/config のHostエイリアスを使う）
#   --export-cmd PATH        aggregate.sh に渡す metrics-export.sh のパス
#                            （既定: /usr/local/bin/metrics-export.sh、接続先Pi上のパス）
#   --profile-since-days N   5分プロファイル取得の遡り日数（既定: 420。テストで
#                            420日分のSSH往復を避けるための上書き用）
#   --auto-inputs-dir DIR    月次確定の自動化（DDR実装手順S1）。energy-fetch（別プロセス、
#                            本スクリプトの対象外）が置く official_buy.json/official_sell.json/
#                            tariff_months.json のhandoffディレクトリ（既定:
#                            /var/lib/energy-fetch/handoff）。存在しない・中身が無ければ
#                            何もしない（handoffが無ければ挙動は従来と同一）。
#   -h, --help                このヘルプを表示
#
# 環境変数:
#   BLOG_METRICS_NOTIFY_CMD  通知コマンド（既定: /usr/local/bin/notify.sh。"$1"=件名 "$2"=本文
#                            で呼ぶ notify.sh 互換インターフェイス）
#   BLOG_METRICS_TODAY       暦日判定に使う日付('YYYY-MM-DD'、テスト専用。省略時は実行日）
#
# 設計方針:
#   - commit author は metrics-bot <metrics-bot@users.noreply.github.com> 固定。
#   - 履歴は append のみ（force push しない）。
#   - git操作順序: fetch -> (ローカルが進んでいれば先にpush) -> reset --hard origin/main
#     -> stage_inputs(handoffの検査・反映) -> 実効tariff生成 -> 新データ配置 ->
#     pipeline.json更新 -> (変更があれば)commit -> validate_metrics.py
#     -> 失敗ならcommitを取り消し / 成功ならpush(3回リトライ)。
#   - データに実質的な変更が無い日（generated_at 以外が前回と同一）はcommitしない。
#   - 失敗は「連続する暦日」でカウントし（間が空いたら1からカウントし直す）、3暦日連続で
#     全失敗した場合のみ通知を1通送る（4日目以降は連続していても再送しない）。回復時に1通送る。
#   - ${STATE_DIR}/allow-history-once フラグ（deploy-homelab.sh が tariff.json の変更を
#     検知したときだけ置く）を検出したら、そのcommit試行1回に限り validate_metrics.py に
#     --allow-history-change を付与し、フラグを消費(削除)する。
#   - 月次確定の自動化（DDR実装手順S1）: 料金体系の骨格は ${INPUTS_DIR}/tariff.json（bundle
#     同梱、従来どおり手動更新）のまま。毎月観測する値（燃料費等調整単価・容量拠出金・賦課金
#     観測値）と official_buy.json/official_sell.json は clone/inputs/ に置き、
#     stage_inputs() が --auto-inputs-dir の handoff を検査した上で clone/inputs/ へ反映する
#     （不合格・handoff無しなら clone/inputs/ の既存値を維持）。実効tariffは
#     bill_model.merge_tariff(base, clone/inputs/tariff_months.json) で都度作り、
#     aggregate.sh/monthly_report.py にはこの実効tariffと --official-dir clone/inputs を渡す。
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

DRY_RUN=false
BLOG_METRICS_DIR="/opt/blog-metrics"
CLONE_DIR="${HOME}/solar-metrics-data"
LOCK_FILE="/run/blog-metrics/run.lock"
STATE_DIR="${HOME}/.local/state/blog-metrics"
LOG_FILE="/var/log/blog-metrics/run.log"
SSH_HOST="solarchgctl-metrics"
EXPORT_CMD="/usr/local/bin/metrics-export.sh"
PROFILE_SINCE_DAYS=420
AUTO_INPUTS_DIR="/var/lib/energy-fetch/handoff"

usage() {
    grep -E '^#( |$)' "${BASH_SOURCE[0]}" | sed -E 's/^# ?//'
    exit "${1:-0}"
}

while [[ $# -gt 0 ]]; do
    case "$1" in
        --dry-run) DRY_RUN=true; shift ;;
        --blog-metrics-dir) BLOG_METRICS_DIR="$2"; shift 2 ;;
        --clone-dir) CLONE_DIR="$2"; shift 2 ;;
        --lock-file) LOCK_FILE="$2"; shift 2 ;;
        --state-dir) STATE_DIR="$2"; shift 2 ;;
        --log-file) LOG_FILE="$2"; shift 2 ;;
        --ssh-host) SSH_HOST="$2"; shift 2 ;;
        --export-cmd) EXPORT_CMD="$2"; shift 2 ;;
        --profile-since-days) PROFILE_SINCE_DAYS="$2"; shift 2 ;;
        --auto-inputs-dir) AUTO_INPUTS_DIR="$2"; shift 2 ;;
        -h|--help) usage 0 ;;
        *) echo "unknown option: $1" >&2; usage 1 ;;
    esac
done

INPUTS_DIR="${BLOG_METRICS_DIR}/inputs"
AGGREGATE_SH="${BLOG_METRICS_DIR}/aggregate.sh"
VALIDATE_PY="${BLOG_METRICS_DIR}/validate_metrics.py"
MONTHLY_REPORT_PY="${BLOG_METRICS_DIR}/monthly_report.py"
LAST_HASHES_FILE="${STATE_DIR}/last_hashes.json"
FAIL_STATE_FILE="${STATE_DIR}/fail_state.json"
ALLOW_HISTORY_ONCE_FLAG="${STATE_DIR}/allow-history-once"

if [[ "${DRY_RUN}" == true ]]; then
    echo "run-daily.sh: --dry-run のため何も実行しません"
    exit 0
fi

# ログ/ロック/状態ディレクトリを先に作っておく（systemd の LogsDirectory=/RuntimeDirectory=/
# StateDirectory=blog-metrics が無い環境や手動実行でも、後続の "2>>${LOG_FILE}" のような
# 未ガードredirectがディレクトリ不在で失敗しないようにする）。
mkdir -p "$(dirname "${LOG_FILE}")" 2>/dev/null || true
mkdir -p "$(dirname "${LOCK_FILE}")" 2>/dev/null || true
mkdir -p "${STATE_DIR}" 2>/dev/null || true

today_str() {
    if [[ -n "${BLOG_METRICS_TODAY:-}" ]]; then
        printf '%s' "${BLOG_METRICS_TODAY}"
    else
        date +%F
    fi
}

log() {
    mkdir -p "$(dirname "${LOG_FILE}")" 2>/dev/null || true
    { echo "$(date '+%F %T') $*" >> "${LOG_FILE}"; } 2>/dev/null || true
}

rotate_log_if_needed() {
    [[ -f "${LOG_FILE}" ]] || return 0
    local size
    size=$(wc -c < "${LOG_FILE}" 2>/dev/null || echo 0)
    if [[ "${size}" -gt $((1024 * 1024)) ]]; then
        local tmp
        tmp=$(mktemp)
        tail -n 2000 "${LOG_FILE}" > "${tmp}" && mv "${tmp}" "${LOG_FILE}"
    fi
}

notify() {
    # $1=件名 $2=本文
    local cmd="${BLOG_METRICS_NOTIFY_CMD:-/usr/local/bin/notify.sh}"
    bash "${cmd}" "$1" "$2" >>"${LOG_FILE}" 2>&1 || log "通知コマンドの実行に失敗しました: ${cmd}"
}

compute_hashes() {
    # $1 = data/metrics ディレクトリ、$2 = posts ディレクトリ（省略可）、
    # $3 = inputs ディレクトリ（省略可、月次確定の自動化・DDR実装手順S1）。generated_at を
    # 除いた内容のsha256をJSONで返す（generated_atだけが変わった日を「変更なし」として
    # 扱うための比較用ハッシュ）。posts/*.json は設計判断2026-09-23/26「速報＋改訂」方式で
    # 追加。キーは posts/<拡張子抜きファイル名>（例: posts/2026-10）でソート順。
    # inputs/official_buy.json・official_sell.json・tariff_months.json（存在するものだけ）も
    # 同様に inputs/<拡張子抜きファイル名> のキーで含める。
    python3 - "$1" "${2:-}" "${3:-}" <<'PY'
import sys, json, hashlib
from pathlib import Path

def strip(obj):
    if isinstance(obj, dict):
        return {k: strip(v) for k, v in obj.items() if k != "generated_at"}
    if isinstance(obj, list):
        return [strip(v) for v in obj]
    return obj

def digest(data) -> str:
    stripped = strip(data)
    return hashlib.sha256(json.dumps(stripped, sort_keys=True, ensure_ascii=False).encode("utf-8")).hexdigest()

d = Path(sys.argv[1])
result = {}
for name in ("daily", "monthly", "meta", "bills", "layers"):
    data = json.loads((d / f"{name}.json").read_text(encoding="utf-8"))
    result[name] = digest(data)

posts_dir = Path(sys.argv[2]) if len(sys.argv) > 2 and sys.argv[2] else None
if posts_dir and posts_dir.is_dir():
    for post_path in sorted(posts_dir.glob("*.json")):
        result[f"posts/{post_path.stem}"] = digest(json.loads(post_path.read_text(encoding="utf-8")))

inputs_dir = Path(sys.argv[3]) if len(sys.argv) > 3 and sys.argv[3] else None
if inputs_dir and inputs_dir.is_dir():
    for name in ("official_buy", "official_sell", "tariff_months"):
        p = inputs_dir / f"{name}.json"
        if p.exists():
            result[f"inputs/{name}"] = digest(json.loads(p.read_text(encoding="utf-8")))

print(json.dumps(result, sort_keys=True))
PY
}

write_pipeline_json() {
    # $1=aggregate.sh出力ディレクトリ $2=db_query_seconds
    # tariff_sha256 は bundle の base tariff.json(${INPUTS_DIR})、official_buy_sha256/
    # official_sell_sha256/tariff_months_sha256 は clone/inputs（月次確定の自動化、
    # DDR実装手順S1「inputsのshaはclone/inputsから計算」）と照合する。
    python3 - "$1" "${CLONE_DIR}" "$(cat "${BLOG_METRICS_DIR}/BUNDLE_REV" 2>/dev/null || echo unknown)" \
        "${PROFILE_SINCE_DAYS}" "$2" "${INPUTS_DIR}" "${CLONE_DIR}/inputs" <<'PY'
import sys, json, hashlib
from datetime import datetime
from pathlib import Path

out_dir, clone_dir, bundle_rev, profile_window_days, db_query_seconds, bundle_inputs_dir, clone_inputs_dir = sys.argv[1:8]

def sha256_or_none(p: Path):
    return hashlib.sha256(p.read_bytes()).hexdigest() if p.exists() else None

daily = json.loads((Path(out_dir) / "daily.json").read_text(encoding="utf-8"))
monthly = json.loads((Path(out_dir) / "monthly.json").read_text(encoding="utf-8"))

pipeline = {
    "schema_version": 1,
    "generated_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
    "source": "homelab",
    "bundle_rev": bundle_rev.strip(),
    "profile_window_days": int(profile_window_days),
    "inputs": {
        "tariff_sha256": sha256_or_none(Path(bundle_inputs_dir) / "tariff.json"),
        "official_buy_sha256": sha256_or_none(Path(clone_inputs_dir) / "official_buy.json"),
        "official_sell_sha256": sha256_or_none(Path(clone_inputs_dir) / "official_sell.json"),
        "tariff_months_sha256": sha256_or_none(Path(clone_inputs_dir) / "tariff_months.json"),
    },
    "row_counts": {"daily": len(daily), "monthly": len(monthly)},
    "db_query_seconds": float(db_query_seconds),
}
Path(clone_dir, "pipeline.json").write_text(json.dumps(pipeline, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
PY
}

stage_inputs() {
    # $1 = --auto-inputs-dir。sync_clone後・aggregate前に呼ぶ。handoff（official_buy.json/
    # official_sell.json/tariff_months.json、存在するものだけ）があれば
    # validate_metrics.py --check-inputs-dir で検査し、合格分だけ clone/inputs へ反映する
    # （tmp+rename）。不合格なら clone/inputs の既存値を維持し、グローバル変数
    # STAGE_INPUTS_FAILED=true を設定する（run_onceの最後で1を返す判断に使う。データの
    # commit・push自体は既存値のまま続行する）。handoffが無ければ移行措置として、
    # clone/inputsにofficial_buy.json/official_sell.jsonが無い場合だけ bundle の
    # 値を初期値としてコピーする（従来の手動運用からの移行期間。tariff_months.jsonに
    # 対応する手動運用は無いためコピーしない）。
    local auto_dir="$1"
    STAGE_INPUTS_FAILED=false
    mkdir -p "${CLONE_DIR}/inputs" 2>>"${LOG_FILE}" || true

    local names=(official_buy official_sell tariff_months)
    local any_present=false
    if [[ -d "${auto_dir}" ]]; then
        local name
        for name in "${names[@]}"; do
            [[ -f "${auto_dir}/${name}.json" ]] && any_present=true
        done
    fi

    if [[ "${any_present}" == true ]]; then
        local check_dir
        check_dir=$(mktemp -d)
        local name
        for name in "${names[@]}"; do
            [[ -f "${auto_dir}/${name}.json" ]] && cp "${auto_dir}/${name}.json" "${check_dir}/${name}.json"
        done
        if python3 "${VALIDATE_PY}" --check-inputs-dir "${check_dir}" --tariff-path "${INPUTS_DIR}/tariff.json" >>"${LOG_FILE}" 2>&1; then
            for name in "${names[@]}"; do
                if [[ -f "${check_dir}/${name}.json" ]]; then
                    cp "${check_dir}/${name}.json" "${CLONE_DIR}/inputs/${name}.json.tmp" \
                        && mv "${CLONE_DIR}/inputs/${name}.json.tmp" "${CLONE_DIR}/inputs/${name}.json"
                fi
            done
            log "stage_inputs: handoff(${auto_dir})の検査に合格したため clone/inputs へ反映しました"
        else
            log "stage_inputs: handoff(${auto_dir})の検査に失敗したため clone/inputs を維持します"
            STAGE_INPUTS_FAILED=true
        fi
        rm -rf "${check_dir}"
    else
        local name
        for name in official_buy official_sell; do
            if [[ ! -f "${CLONE_DIR}/inputs/${name}.json" && -f "${INPUTS_DIR}/${name}.json" ]]; then
                cp "${INPUTS_DIR}/${name}.json" "${CLONE_DIR}/inputs/${name}.json"
                log "stage_inputs: 移行措置として bundle の ${name}.json を clone/inputs へ初期コピーしました"
            fi
        done
    fi
}

build_effective_tariff() {
    # $1 = 出力先パス。base=${INPUTS_DIR}/tariff.json（bundle同梱、料金体系の骨格）に
    # overlay=${CLONE_DIR}/inputs/tariff_months.json（無ければNone）を
    # bill_model.merge_tariff() で重ね合わせる。overlayがbaseと矛盾(tariff_conflict)する
    # 場合はbaseのみを書き出し非0を返す（呼び出し元はbaseのまま処理を続行しつつ
    # run_onceの最後で1を返す判断に使う）。
    local out_path="$1"
    python3 - "${BLOG_METRICS_DIR}" "${INPUTS_DIR}/tariff.json" "${CLONE_DIR}/inputs/tariff_months.json" "${out_path}" <<'PY'
import sys, json
from pathlib import Path

blog_metrics_dir, base_path, overlay_path, out_path = sys.argv[1:5]
sys.path.insert(0, blog_metrics_dir)
import bill_model  # noqa: E402  merge_tariffを二重実装しない

base = json.loads(Path(base_path).read_text(encoding="utf-8"))
overlay_file = Path(overlay_path)
overlay = json.loads(overlay_file.read_text(encoding="utf-8")) if overlay_file.exists() else None

conflict = False
try:
    effective = bill_model.merge_tariff(base, overlay)
except ValueError as exc:
    print(f"build_effective_tariff: {exc}", file=sys.stderr)
    effective = base
    conflict = True

Path(out_path).write_text(json.dumps(effective, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
sys.exit(1 if conflict else 0)
PY
}

push_with_retry() {
    # 呼び出し元(sync_clone/commit_and_maybe_push)経由で errexit が効かない文脈のため、
    # ここでも各コマンドの戻り値を明示的に確認する（QA指摘F3対応）。
    local attempt
    for attempt in 1 2 3; do
        if git -C "${CLONE_DIR}" push origin main -q 2>>"${LOG_FILE}"; then
            local new_hashes
            if new_hashes=$(compute_hashes "${CLONE_DIR}/data/metrics" "${CLONE_DIR}/posts" "${CLONE_DIR}/inputs" 2>>"${LOG_FILE}") && [[ -n "${new_hashes}" ]]; then
                mkdir -p "${STATE_DIR}" 2>>"${LOG_FILE}" || true
                printf '%s\n' "${new_hashes}" > "${LAST_HASHES_FILE}"
            else
                log "push後のcompute_hashesに失敗しました（last_hashes.jsonは更新されません）"
            fi
            return 0
        fi
        log "git push 失敗（試行${attempt}/3）"
        git -C "${CLONE_DIR}" fetch origin main -q 2>>"${LOG_FILE}" || true
        sleep "${BLOG_METRICS_RETRY_SLEEP:-5}"
    done
    return 1
}

sync_clone() {
    # 注意: 本関数は `run_once` から `if run_once; then` 経由で呼ばれるため、この関数内では
    # bash の errexit(set -e) が事実上無効（if/while/&&/||/! の対象コマンドではerrexitが
    # 効かない既知の挙動）。各コマンドの成否を明示的に確認すること（QA指摘F3対応）。
    if [[ ! -d "${CLONE_DIR}/.git" ]]; then
        log "clone が見つかりません: ${CLONE_DIR}（初回セットアップが必要です）"
        return 1
    fi
    if ! git -C "${CLONE_DIR}" fetch origin main -q 2>>"${LOG_FILE}"; then
        log "git fetch に失敗しました"
        return 1
    fi
    local ahead
    if ! ahead=$(git -C "${CLONE_DIR}" rev-list --count origin/main..HEAD 2>>"${LOG_FILE}"); then
        log "git rev-list に失敗しました"
        return 1
    fi
    if [[ "${ahead}" -gt 0 ]]; then
        log "ローカルに未pushのコミットが${ahead}件あるため先にpushします"
        if ! push_with_retry; then
            log "未pushコミットのpushに失敗したため今回の実行を中断します"
            return 1
        fi
    fi
    if ! git -C "${CLONE_DIR}" reset --hard origin/main -q 2>>"${LOG_FILE}"; then
        log "git reset --hard origin/main に失敗しました"
        return 1
    fi
    return 0
}

commit_and_maybe_push() {
    # sync_clone 同様、`if ! commit_and_maybe_push` 経由の呼び出しで errexit が効かない
    # ため、各コマンドの戻り値を明示的に確認する（QA指摘F3対応）。cp/compute_hashes が
    # クラッシュした場合にハッシュが空文字同士で一致し「変更なしでexit 0」に化ける事故を防ぐ。
    local out_dir="$1" db_query_seconds="$2" effective_tariff_path="$3"
    local monthly_report_failed=false

    # data/metrics/ が無い clone は初回セットアップ未完了（README.md欠落チェックと同じ思想）
    # として扱い、自動作成せずに失敗させる。自動作成すると、seed commit が無い壊れた clone
    # に対しても気づかずcommitを作ってしまう（QA指摘F3の根本原因と同種の「静かな回復」）。
    if [[ ! -d "${CLONE_DIR}/data/metrics" ]]; then
        log "clone に data/metrics/ がありません（初回セットアップ未完了）: ${CLONE_DIR}/data/metrics"
        return 1
    fi

    if ! cp "${out_dir}/daily.json" "${out_dir}/monthly.json" "${out_dir}/meta.json" \
            "${out_dir}/bills.json" "${out_dir}/layers.json" "${CLONE_DIR}/data/metrics/" 2>>"${LOG_FILE}"; then
        log "aggregate.sh の出力を ${CLONE_DIR}/data/metrics/ へコピーできませんでした"
        return 1
    fi

    # 設計判断(2026-09-23/26「速報＋改訂」方式): 確定・速報段階に入った月のposts/YYYY-MM.json
    # を作成・改版する。失敗してもデータのcommit・pushは止めない（run-daily.shの主目的は
    # メトリクスデータの公開であり、月次レポートはその上に乗る追加機能のため）。posts/だけ
    # 元に戻し、この日は失敗として扱い run_once の最後で1を返す（既存の「3暦日連続失敗で
    # LINE1通」の仕組みに乗せる。通知の追加は行わない）。
    mkdir -p "${CLONE_DIR}/posts" 2>>"${LOG_FILE}" || true
    if ! python3 "${MONTHLY_REPORT_PY}" \
            --data-dir "${CLONE_DIR}/data/metrics" \
            --posts-dir "${CLONE_DIR}/posts" \
            --tariff "${effective_tariff_path}" \
            --today "$(today_str)" \
            >>"${LOG_FILE}" 2>&1; then
        log "monthly_report.py が失敗しました（postsだけを元に戻します）"
        git -C "${CLONE_DIR}" checkout -q -- posts 2>>"${LOG_FILE}" || true
        git -C "${CLONE_DIR}" clean -fdq posts 2>>"${LOG_FILE}" || true
        monthly_report_failed=true
    fi

    local new_hashes old_hashes
    if ! new_hashes=$(compute_hashes "${CLONE_DIR}/data/metrics" "${CLONE_DIR}/posts" "${CLONE_DIR}/inputs" 2>>"${LOG_FILE}") || [[ -z "${new_hashes}" ]]; then
        # compute_hashes(python3)の例外はLOG_FILEにリダイレクト済みでstdout(journal)には出さない。
        log "compute_hashes に失敗しました（コピー後のファイルが壊れている可能性があります）"
        return 1
    fi
    old_hashes=""
    [[ -f "${LAST_HASHES_FILE}" ]] && old_hashes=$(cat "${LAST_HASHES_FILE}")

    if [[ "${new_hashes}" == "${old_hashes}" ]]; then
        log "データに実質的な変更が無いためcommitしません"
        # git checkout/cleanは複数pathspecのうち1つでも「知られていないパス」だと全体が
        # 失敗する(gitの既知の挙動)。posts/inputsはコミット履歴に一度も現れないことが
        # あるため data/metrics とは別コマンドにする（QA再発防止: 2026-09-26発見の回帰）。
        git -C "${CLONE_DIR}" checkout -q -- data/metrics 2>>"${LOG_FILE}" || true
        git -C "${CLONE_DIR}" clean -fdq data/metrics 2>>"${LOG_FILE}" || true
        git -C "${CLONE_DIR}" checkout -q -- posts 2>>"${LOG_FILE}" || true
        git -C "${CLONE_DIR}" clean -fdq posts 2>>"${LOG_FILE}" || true
        git -C "${CLONE_DIR}" checkout -q -- inputs 2>>"${LOG_FILE}" || true
        git -C "${CLONE_DIR}" clean -fdq inputs 2>>"${LOG_FILE}" || true
        [[ "${monthly_report_failed}" == true ]] && return 1
        return 0
    fi

    if ! write_pipeline_json "${out_dir}" "${db_query_seconds}" 2>>"${LOG_FILE}"; then
        log "pipeline.json の生成に失敗しました"
        return 1
    fi

    if ! git -C "${CLONE_DIR}" add -A 2>>"${LOG_FILE}"; then
        log "git add に失敗しました"
        return 1
    fi
    if ! git -C "${CLONE_DIR}" -c user.name="metrics-bot" -c user.email="metrics-bot@users.noreply.github.com" \
            commit -q -m "metrics: $(today_str) 分を更新" 2>>"${LOG_FILE}"; then
        log "git commit に失敗しました"
        return 1
    fi

    # F5b QA指摘: tariff.jsonに新しい請求月の単価を追加すると、その月が確定するまで
    # 暫定単価を使っていた過去日の値が(正しく)動くことがあり、validate_metrics.py の
    # G9(履歴不変性)がpushを拒否してパイプラインが止まる。deploy-homelab.sh が
    # tariff.json の変更を検知したときだけ置く一時フラグを検出したら、今回1回に限り
    # --allow-history-change を付与し、フラグを消費(削除)する。
    local validate_extra_args=()
    if [[ -f "${ALLOW_HISTORY_ONCE_FLAG}" ]]; then
        validate_extra_args+=(--allow-history-change)
        rm -f "${ALLOW_HISTORY_ONCE_FLAG}"
        log "allow-history-once フラグを検出したため今回のみ --allow-history-change を適用します"
    fi

    # --tariff-path は bundle の base tariff.json のまま（G13の警告のみ照合用、G18/G19は
    # 別途resolve_input_pathsが読む）。--official-buy-path/--official-sell-path は
    # clone/inputs（月次確定の自動化、DDR実装手順S1）を指す。incomingにinputs/*.jsonが
    # あればgate13_pipelineはそちらと直接fatal照合するため、通常はこの2引数は移行期間の
    # フォールバック（incomingにまだinputs/が無い場合の警告用）としてのみ働く。
    if ! python3 "${VALIDATE_PY}" \
            --incoming "${CLONE_DIR}" \
            --repo "${BLOG_METRICS_DIR}" \
            --tariff-path "${INPUTS_DIR}/tariff.json" \
            --official-buy-path "${CLONE_DIR}/inputs/official_buy.json" \
            --official-sell-path "${CLONE_DIR}/inputs/official_sell.json" \
            "${validate_extra_args[@]}" \
            >>"${LOG_FILE}" 2>&1; then
        log "validate_metrics.py が失敗したためcommitを取り消します"
        git -C "${CLONE_DIR}" reset --hard HEAD~1 -q 2>>"${LOG_FILE}" || true
        return 1
    fi

    if ! push_with_retry; then
        log "git push に失敗しました（コミットはローカルに残し、次回実行時に先にpushします）"
        return 1
    fi
    log "commit・pushが完了しました"
    [[ "${monthly_report_failed}" == true ]] && return 1
    return 0
}

update_fail_state() {
    # $1 = "success" | "failure" 。通知が必要なら "alert"/"recover" を、不要なら空文字を返す。
    # fail_streak_days は「連続する暦日」で数える。間が(1日でも)空いたら1からカウントし直す
    # （QA指摘F17: 例えば電源断でrun-daily.sh自体が数日動かず、久しぶりに失敗した1回だけで
    # 過去の古いstreakに積み増されて誤って通知が飛ぶことを防ぐ）。
    mkdir -p "${STATE_DIR}"
    python3 - "${FAIL_STATE_FILE}" "$1" "$(today_str)" <<'PY'
import json, sys
from datetime import date, timedelta
from pathlib import Path

path, outcome, today_str = sys.argv[1], sys.argv[2], sys.argv[3]
today = date.fromisoformat(today_str)
try:
    state = json.loads(Path(path).read_text(encoding="utf-8"))
except (FileNotFoundError, json.JSONDecodeError):
    state = {"fail_streak_days": 0, "last_fail_date": None, "alerted": False}

action = ""
if outcome == "failure":
    last_fail_str = state.get("last_fail_date")
    last_fail = date.fromisoformat(last_fail_str) if last_fail_str else None
    if last_fail == today:
        pass  # 同じ暦日内の再実行は既にカウント済み（複数回失敗しても1日分）
    elif last_fail is not None and last_fail == today - timedelta(days=1):
        state["fail_streak_days"] = state.get("fail_streak_days", 0) + 1
        state["last_fail_date"] = today_str
    else:
        # 前回失敗の記録が無い、または間が空いている(連続していない) → 新しい連続失敗として1から
        # 数え直す。alertedも一緒にリセットしないと、間に成功を挟まず(例: 電源断で
        # run-daily.sh自体が数日動かない)新しいインシデントが始まった場合、古いインシデントの
        # alerted=Trueが残ったままになり、新インシデントが3日連続に達しても再通知されない
        # (QA指摘#2)。
        state["fail_streak_days"] = 1
        state["last_fail_date"] = today_str
        state["alerted"] = False
    if state["fail_streak_days"] >= 3 and not state.get("alerted"):
        state["alerted"] = True
        action = "alert"
else:
    if state.get("fail_streak_days", 0) > 0 and state.get("alerted"):
        action = "recover"
    state = {"fail_streak_days": 0, "last_fail_date": None, "alerted": False}

Path(path).write_text(json.dumps(state, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
print(action)
PY
}

run_once() {
    sync_clone || return 1

    if [[ ! -f "${CLONE_DIR}/README.md" ]]; then
        log "clone に README.md がありません（初回セットアップ未完了）: ${CLONE_DIR}"
        return 1
    fi

    # 月次確定の自動化（DDR実装手順S1）: handoff(--auto-inputs-dir)を検査してclone/inputsへ
    # 反映し、実効tariff(base + tariff_months.json)を作る。aggregate/monthly_report より
    # 前に済ませる必要がある。
    stage_inputs "${AUTO_INPUTS_DIR}"
    local stage_inputs_failed="${STAGE_INPUTS_FAILED}"

    local effective_tariff_path
    effective_tariff_path=$(mktemp)
    local effective_tariff_failed=false
    if ! build_effective_tariff "${effective_tariff_path}"; then
        effective_tariff_failed=true
    fi

    local out_dir
    out_dir=$(mktemp -d)

    local start_ts elapsed
    start_ts=${SECONDS}
    if ! bash "${AGGREGATE_SH}" \
            --export-cmd "${EXPORT_CMD}" \
            --ssh-host "${SSH_HOST}" \
            --official-dir "${CLONE_DIR}/inputs" \
            --tariff "${effective_tariff_path}" \
            --profile-since "$(python3 -c "from datetime import date, timedelta; print((date.today() - timedelta(days=${PROFILE_SINCE_DAYS})).isoformat())")" \
            --out "${out_dir}" >>"${LOG_FILE}" 2>&1; then
        log "aggregate.sh が失敗しました"
        rm -rf "${out_dir}"
        rm -f "${effective_tariff_path}"
        return 1
    fi
    elapsed=$((SECONDS - start_ts))

    # aggregate.sh が exit 0 でも、期待する5ファイルが全て揃っていなければ不完全な結果として
    # 扱い、commitしない（QA指摘F4: profile 0行時にlayers.jsonが無いまま前回のstaleな
    # layers.jsonが誤ってpushされる事故の再発防止。aggregate.sh自身のexit code依存にしない
    # 二重の防御）。
    local name
    for name in daily monthly meta bills layers; do
        if [[ ! -s "${out_dir}/${name}.json" ]]; then
            log "aggregate.sh の出力に ${name}.json がありません（不完全な結果のためcommitしません）"
            rm -rf "${out_dir}"
            rm -f "${effective_tariff_path}"
            return 1
        fi
    done

    if ! commit_and_maybe_push "${out_dir}" "${elapsed}" "${effective_tariff_path}"; then
        rm -rf "${out_dir}"
        rm -f "${effective_tariff_path}"
        return 1
    fi
    rm -rf "${out_dir}"
    rm -f "${effective_tariff_path}"

    # stage_inputs/build_effective_tariffが不合格だった回は、データのcommit・push自体は
    # （既存のclone/inputs値のまま）成功させつつ、run全体としては失敗扱いにする
    # （既存の「3暦日連続失敗でLINE1通」の仕組みに乗せる。monthly_report_failedと同じ流儀）。
    if [[ "${stage_inputs_failed}" == true || "${effective_tariff_failed}" == true ]]; then
        return 1
    fi
    return 0
}

main() {
    mkdir -p "$(dirname "${LOCK_FILE}")" 2>/dev/null || true
    exec 9>"${LOCK_FILE}" || { echo "run-daily.sh: ロックファイルを開けません: ${LOCK_FILE}" >&2; exit 1; }
    if ! flock -n 9; then
        echo "run-daily.sh: 他のインスタンスが実行中のためスキップします"
        exit 0
    fi

    rotate_log_if_needed
    log "run-daily.sh 開始"

    local outcome action
    if run_once; then
        outcome="success"
        log "run-daily.sh 成功"
    else
        outcome="failure"
        log "run-daily.sh 失敗"
    fi

    action=$(update_fail_state "${outcome}")
    case "${action}" in
        alert) notify "blog-metrics 障害" "blog-metrics の自動更新が3日連続で失敗しています。$(today_str) 時点。/var/log/blog-metrics/run.log を確認してください。" ;;
        recover) notify "blog-metrics 復旧" "blog-metrics の自動更新が復旧しました（$(today_str)）。" ;;
    esac

    if [[ "${outcome}" == "success" ]]; then
        echo "run-daily.sh: OK ($(today_str))"
        exit 0
    else
        echo "run-daily.sh: FAILED ($(today_str)) — see ${LOG_FILE}"
        exit 1
    fi
}

main
