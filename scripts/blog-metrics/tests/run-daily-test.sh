#!/bin/bash
# scripts/blog-metrics/run-daily.sh の自動テスト。
# ssh・notify.sh をスタブに差し替え、git の「本番リモート」はローカルのbare repoで代替する
# （fetch/push/commit等のgit操作自体は本物を使い、ネットワークだけ排除する）。
# aggregate.sh/build_daily.py/validate_metrics.py は本物を使い、metrics-export.sh 相当の
# 出力だけをスタブ化する（ssh先のコマンド文字列をローカルでそのままbash実行することで
# 実質「sshを経由しない」ことにする）。
# 実行: bash scripts/blog-metrics/tests/run-daily-test.sh
set -u
HERE=$(cd "$(dirname "$0")" && pwd)
BLOG_METRICS_SRC=$(cd "$HERE/.." && pwd)
REPO_ROOT=$(cd "$BLOG_METRICS_SRC/../.." && pwd)
RUN_DAILY="$BLOG_METRICS_SRC/run-daily.sh"
FAILS=0; PASSES=0
ok()   { PASSES=$((PASSES+1)); }
fail() { FAILS=$((FAILS+1)); echo "FAIL: $*"; }
assert_eq() { # $1=label $2=actual $3=expected
  if [ "$2" = "$3" ]; then ok; else fail "$1: expected [$3] got [$2]"; fi
}
assert_contains() { # $1=label $2=haystack $3=needle
  if printf '%s' "$2" | /usr/bin/grep -qF -- "$3"; then ok; else
    fail "$1: expected to contain [$3]"; echo "  got: $2"; fi
}

# 個々のシナリオ間で状態を持ち越さない共通セットアップ。STUB_DAILY_JSON/STUB_META_JSON/
# BLOG_METRICS_TODAY はシナリオごとに export で上書きして使う。
setup() {
  T=$(mktemp -d)
  mkdir -p "$T/bin"

  # --- ssh スタブ: 第2引数(リモートで実行する文字列)をそのままローカルでbash実行する
  # （EXPORT_CMDにローカルの絶対パスを渡すので、パス書き換え不要でそのまま動く）---
  cat > "$T/bin/ssh" <<'STUB'
#!/bin/bash
shift
bash -c "$1"
STUB
  chmod +x "$T/bin/ssh"

  # --- metrics-export.sh スタブ ---
  cat > "$T/bin/stub-export.sh" <<'STUB'
#!/bin/bash
set -euo pipefail
CMD=""
while [ $# -gt 0 ]; do
  case "$1" in
    --db) shift 2 ;;
    daily) CMD="daily"; shift ;;
    meta) CMD="meta"; shift ;;
    profile) CMD="profile"; DATE="$2"; shift 2 ;;
    *) shift ;;
  esac
done
case "$CMD" in
  daily) printf '%s\n' "${STUB_DAILY_JSON}" ;;
  meta) printf '%s\n' "${STUB_META_JSON}" ;;
  profile)
    echo "bucket_at,solar_w,buy_w,sell_w,nichicon_pv_w,nichicon_battery_w,nichicon_soc,eco_ac_in_w,eco_ac_out_w,power_n,nichicon_n,ecoflow_n"
    echo "${DATE} 12:00,1000,0,200,500,100,55,0,0,10,10,10"
    ;;
esac
STUB
  chmod +x "$T/bin/stub-export.sh"

  # --- notify.sh スタブ（呼び出しを記録するだけ） ---
  mkdir -p "$T/notify"
  cat > "$T/bin/fake-notify.sh" <<STUBEOF
#!/bin/bash
echo "\$1|\$2" >> "$T/notify/calls.log"
STUBEOF
  chmod +x "$T/bin/fake-notify.sh"

  export PATH="$T/bin:$PATH"
  export BLOG_METRICS_NOTIFY_CMD="$T/bin/fake-notify.sh"
  export STUB_DAILY_JSON='[{"date":"2026-09-01","solar_kwh":20.0,"buy_kwh":0.0,"sell_kwh":5.0,"nichicon_charge_kwh":1.5,"ecoflow_charge_kwh":null,"status":"COMPLETE"}]'
  export STUB_META_JSON='{"power_history_since":"2026-09-01","nichicon_data_since":"2026-09-01","ecoflow_data_since":null}'
  export BLOG_METRICS_TODAY="2026-09-01"

  # --- bundle（/opt/blog-metrics 相当） ---
  BUNDLE="$T/blog-metrics"
  mkdir -p "$BUNDLE/inputs"
  cp "$BLOG_METRICS_SRC/aggregate.sh" "$BUNDLE/aggregate.sh"
  cp "$BLOG_METRICS_SRC/build_daily.py" "$BUNDLE/build_daily.py"
  cp "$BLOG_METRICS_SRC/validate_metrics.py" "$BUNDLE/validate_metrics.py"
  cp "$BLOG_METRICS_SRC/layer_model.py" "$BUNDLE/layer_model.py"
  cp "$BLOG_METRICS_SRC/bill_model.py" "$BUNDLE/bill_model.py"
  cp "$BLOG_METRICS_SRC/delta_model.py" "$BUNDLE/delta_model.py"
  cp "$BLOG_METRICS_SRC/tariff.json" "$BUNDLE/inputs/tariff.json"
  cp "$REPO_ROOT/data/metrics/official_buy.json" "$BUNDLE/inputs/official_buy.json"
  cp "$REPO_ROOT/data/metrics/official_sell.json" "$BUNDLE/inputs/official_sell.json"
  echo "test-bundle-rev" > "$BUNDLE/BUNDLE_REV"

  # --- 「本番」origin (bare) を用意し、初回データで seed commit を積む ---
  ORIGIN="$T/origin.git"
  git init -q --bare --initial-branch=main "$ORIGIN"
  SEED=$(mktemp -d)
  git clone -q "$ORIGIN" "$SEED"
  mkdir -p "$SEED/data/metrics"
  SEED_OUT=$(mktemp -d)
  bash "$BUNDLE/aggregate.sh" --local /dev/null --export-cmd "$T/bin/stub-export.sh" \
      --profile-since "2026-09-01" --official-dir "$BUNDLE/inputs" --tariff "$BUNDLE/inputs/tariff.json" \
      --out "$SEED_OUT" >/dev/null 2>&1
  cp "$SEED_OUT/daily.json" "$SEED_OUT/monthly.json" "$SEED_OUT/meta.json" "$SEED_OUT/bills.json" "$SEED_OUT/layers.json" "$SEED/data/metrics/"
  echo "# solar-metrics-data (test fixture)" > "$SEED/README.md"
  python3 - "$SEED" "$SEED_OUT" "$BUNDLE/inputs" <<'PY'
import json, hashlib, sys
from pathlib import Path
from datetime import datetime
seed, seed_out, inputs = Path(sys.argv[1]), Path(sys.argv[2]), Path(sys.argv[3])
def sha(p):
    return hashlib.sha256(p.read_bytes()).hexdigest() if p.exists() else None
daily = json.loads((seed_out / "daily.json").read_text(encoding="utf-8"))
monthly = json.loads((seed_out / "monthly.json").read_text(encoding="utf-8"))
pipeline = {
    "schema_version": 1,
    "generated_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
    "source": "homelab",
    "bundle_rev": "seed",
    "profile_window_days": 1,
    "inputs": {
        "tariff_sha256": sha(inputs / "tariff.json"),
        "official_buy_sha256": sha(inputs / "official_buy.json"),
        "official_sell_sha256": sha(inputs / "official_sell.json"),
    },
    "row_counts": {"daily": len(daily), "monthly": len(monthly)},
    "db_query_seconds": 0.1,
}
(seed / "pipeline.json").write_text(json.dumps(pipeline, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
PY
  ( cd "$SEED" && git add -A && git -c user.name=seed -c user.email=seed@example.com commit -q -m seed && git push -q origin main )
  rm -rf "$SEED" "$SEED_OUT"

  CLONE="$T/clone"
  git clone -q "$ORIGIN" "$CLONE"

  BROKEN_CLONE="$T/broken-clone"
  mkdir -p "$BROKEN_CLONE"  # .git無し = sync_clone が即失敗する（失敗シナリオ用）

  STATE_DIR="$T/state"
  LOG_FILE="$T/run.log"
  LOCK_FILE="$T/lock/blog-metrics.lock"
  mkdir -p "$T/lock"
}

teardown() { rm -rf "$T"; }

run_daily_success_args() {
  bash "$RUN_DAILY" \
    --blog-metrics-dir "$BUNDLE" \
    --clone-dir "$CLONE" \
    --lock-file "$LOCK_FILE" \
    --state-dir "$STATE_DIR" \
    --log-file "$LOG_FILE" \
    --ssh-host "fakehost" \
    --export-cmd "$T/bin/stub-export.sh" \
    --profile-since-days 1
}

run_daily_failure_args() {
  bash "$RUN_DAILY" \
    --blog-metrics-dir "$BUNDLE" \
    --clone-dir "$BROKEN_CLONE" \
    --lock-file "$LOCK_FILE" \
    --state-dir "$STATE_DIR" \
    --log-file "$LOG_FILE" \
    --ssh-host "fakehost" \
    --export-cmd "$T/bin/stub-export.sh" \
    --profile-since-days 1
}

origin_log_count() { git --git-dir="$ORIGIN" log --oneline main 2>/dev/null | wc -l | tr -d ' '; }
notify_calls() { [ -f "$T/notify/calls.log" ] && cat "$T/notify/calls.log" || true; }

echo "# 1. 変更あり: 1 commit 1 push（origin のコミット数が1増える）"
setup
BEFORE=$(origin_log_count)
run_daily_success_args >/dev/null 2>&1
RC=$?
assert_eq "1 exit" "$RC" "0"
AFTER=$(origin_log_count)
assert_eq "1 commit count +1" "$AFTER" "$((BEFORE + 1))"
[ -f "$STATE_DIR/last_hashes.json" ] && ok || fail "1 last_hashes.json missing"
assert_eq "1 no notify" "$(notify_calls)" ""
teardown

echo "# 2. 変更なし（同一データを再実行）: commit されない"
setup
run_daily_success_args >/dev/null 2>&1   # 1回目でcommitを作る
BEFORE=$(origin_log_count)
run_daily_success_args >/dev/null 2>&1   # 2回目: 同一STUBデータ
RC=$?
assert_eq "2 exit" "$RC" "0"
AFTER=$(origin_log_count)
assert_eq "2 commit count unchanged" "$AFTER" "$BEFORE"
WT_STATUS=$(git -C "$CLONE" status --porcelain)
assert_eq "2 working tree clean" "$WT_STATUS" ""
teardown

echo "# 3. generated_at だけの差分: 実際に generated_at が変わることを確認した上でcommitされないこと"
setup
GEN1_DIR="$T/gen1"; GEN2_DIR="$T/gen2"
DAILY_FILE="$T/stub_daily.json"; META_FILE="$T/stub_meta.json"
printf '%s' "$STUB_DAILY_JSON" > "$DAILY_FILE"
printf '%s' "$STUB_META_JSON" > "$META_FILE"
python3 "$BUNDLE/build_daily.py" --daily-export "$DAILY_FILE" --meta-export "$META_FILE" \
    --tariff "$BUNDLE/inputs/tariff.json" --out-dir "$GEN1_DIR" >/dev/null 2>&1
sleep 1
python3 "$BUNDLE/build_daily.py" --daily-export "$DAILY_FILE" --meta-export "$META_FILE" \
    --tariff "$BUNDLE/inputs/tariff.json" --out-dir "$GEN2_DIR" >/dev/null 2>&1
GEN1=$(python3 -c "import json;print(json.load(open('$GEN1_DIR/meta.json'))['generated_at'])")
GEN2=$(python3 -c "import json;print(json.load(open('$GEN2_DIR/meta.json'))['generated_at'])")
if [ "$GEN1" != "$GEN2" ]; then ok; else fail "3 setup: generated_at が2回の実行で変化しなかった（テスト前提が崩れている）"; fi
DAILY1=$(python3 -c "import json;print(json.load(open('$GEN1_DIR/daily.json')))")
DAILY2=$(python3 -c "import json;print(json.load(open('$GEN2_DIR/daily.json')))")
assert_eq "3 daily.json は generated_at と無関係に同一" "$DAILY1" "$DAILY2"
# 上記の「generated_atだけ違うデータ」を実際にrun-daily.shに2回流し込んでもcommitが増えないこと
run_daily_success_args >/dev/null 2>&1
BEFORE=$(origin_log_count)
sleep 1
run_daily_success_args >/dev/null 2>&1
AFTER=$(origin_log_count)
assert_eq "3 commit count unchanged despite generated_at drift" "$AFTER" "$BEFORE"
teardown

echo "# 4. 失敗3回（同一暦日）: 通知なし・fail_streak_days=1"
setup
run_daily_failure_args >/dev/null 2>&1
run_daily_failure_args >/dev/null 2>&1
run_daily_failure_args >/dev/null 2>&1
STREAK=$(python3 -c "import json;print(json.load(open('$STATE_DIR/fail_state.json'))['fail_streak_days'])" 2>/dev/null)
assert_eq "4 fail_streak_days" "$STREAK" "1"
assert_eq "4 no notify" "$(notify_calls)" ""
teardown

echo "# 5. 3暦日連続失敗: 通知1通（障害）"
setup
BLOG_METRICS_TODAY="2026-09-01" run_daily_failure_args >/dev/null 2>&1
BLOG_METRICS_TODAY="2026-09-02" run_daily_failure_args >/dev/null 2>&1
BLOG_METRICS_TODAY="2026-09-03" run_daily_failure_args >/dev/null 2>&1
CALLS=$(notify_calls)
CALL_COUNT=$(printf '%s\n' "$CALLS" | /usr/bin/grep -c . || true)
assert_eq "5 notify count" "$CALL_COUNT" "1"
assert_contains "5 notify subject" "$CALLS" "障害"

echo "# 6. 4日目も失敗継続: 追加通知なし"
BLOG_METRICS_TODAY="2026-09-04" run_daily_failure_args >/dev/null 2>&1
CALLS=$(notify_calls)
CALL_COUNT=$(printf '%s\n' "$CALLS" | /usr/bin/grep -c . || true)
assert_eq "6 notify count still 1" "$CALL_COUNT" "1"

echo "# 7. 5日目に成功: 復旧通知1通"
BLOG_METRICS_TODAY="2026-09-05" run_daily_success_args >/dev/null 2>&1
CALLS=$(notify_calls)
CALL_COUNT=$(printf '%s\n' "$CALLS" | /usr/bin/grep -c . || true)
assert_eq "7 notify count 2" "$CALL_COUNT" "2"
assert_contains "7 notify subject" "$CALLS" "復旧"
STREAK=$(python3 -c "import json;print(json.load(open('$STATE_DIR/fail_state.json'))['fail_streak_days'])" 2>/dev/null)
assert_eq "7 fail_streak reset" "$STREAK" "0"
teardown

echo "# 8. 未pushコミットがあるとき: reset より先に push される（履歴を失わない）"
setup
run_daily_success_args >/dev/null 2>&1  # 通常の1回目
# 前回実行が push だけ失敗して残した、という状況を模する（origin未反映のローカルcommit）
git -C "$CLONE" -c user.name=stray -c user.email=stray@example.com commit -q --allow-empty -m "stray-unpushed-commit"
STRAY_SHA=$(git -C "$CLONE" rev-parse HEAD)
# 既存日(2026-09-01)の値を書き換えるとG9(履歴不変性、実時刻基準でtoday-20日超なら保護対象)に
# 抵触しうるため、新しい日を1件追加する形で「変更あり」を作る（G9は新規日には適用されない）。
export STUB_DAILY_JSON='[{"date":"2026-09-01","solar_kwh":20.0,"buy_kwh":0.0,"sell_kwh":5.0,"nichicon_charge_kwh":1.5,"ecoflow_charge_kwh":null,"status":"COMPLETE"},{"date":"2026-09-02","solar_kwh":18.0,"buy_kwh":0.5,"sell_kwh":4.0,"nichicon_charge_kwh":1.0,"ecoflow_charge_kwh":null,"status":"COMPLETE"}]'
run_daily_success_args >/dev/null 2>&1
RC=$?
assert_eq "8 exit" "$RC" "0"
if git --git-dir="$ORIGIN" cat-file -e "$STRAY_SHA" 2>/dev/null; then ok; else fail "8 stray commit was not pushed to origin (history lost)"; fi
teardown

echo "# 9. --dry-run: 何も実行されない"
setup
BEFORE=$(origin_log_count)
OUTPUT=$(bash "$RUN_DAILY" --dry-run \
    --blog-metrics-dir "$BUNDLE" --clone-dir "$CLONE" --lock-file "$LOCK_FILE" \
    --state-dir "$STATE_DIR" --log-file "$LOG_FILE" --ssh-host fakehost \
    --export-cmd "$T/bin/stub-export.sh" --profile-since-days 1 2>&1)
RC=$?
assert_eq "9 exit" "$RC" "0"
AFTER=$(origin_log_count)
assert_eq "9 no commit" "$AFTER" "$BEFORE"
[ -f "$STATE_DIR/last_hashes.json" ] && fail "9 state should not be created" || ok
[ -f "$T/notify/calls.log" ] && fail "9 notify should not be called" || ok
teardown

echo "# 10. flock 競合: 他インスタンス実行中なら exit 0 で何もしない"
setup
mkdir -p "$(dirname "$LOCK_FILE")"
(
  exec 8>"$LOCK_FILE"
  flock 8
  sleep 3
) &
HOLDER_PID=$!
sleep 0.5
BEFORE=$(origin_log_count)
OUTPUT=$(run_daily_success_args 2>&1)
RC=$?
assert_eq "10 exit" "$RC" "0"
AFTER=$(origin_log_count)
assert_eq "10 no commit while locked" "$AFTER" "$BEFORE"
wait "$HOLDER_PID" 2>/dev/null
teardown

echo "# 11. aggregate.sh が layers.json を欠いたまま exit 0 した場合: 非0・commitなし・fail_streak=1"
setup
BROKEN_BUNDLE="$T/broken-bundle"
mkdir -p "$BROKEN_BUNDLE/inputs"
cp "$BUNDLE/build_daily.py" "$BROKEN_BUNDLE/build_daily.py"
cp "$BUNDLE/validate_metrics.py" "$BROKEN_BUNDLE/validate_metrics.py"
cp "$BUNDLE/inputs/tariff.json" "$BROKEN_BUNDLE/inputs/tariff.json"
cp "$BUNDLE/inputs/official_buy.json" "$BROKEN_BUNDLE/inputs/official_buy.json"
cp "$BUNDLE/inputs/official_sell.json" "$BROKEN_BUNDLE/inputs/official_sell.json"
echo "broken-bundle-rev" > "$BROKEN_BUNDLE/BUNDLE_REV"
cat > "$BROKEN_BUNDLE/aggregate.sh" <<'STUB'
#!/bin/bash
# QA指摘F4の再発防止テスト用スタブ: layers.json を意図的に書かずexit 0する
# (本物のaggregate.shはprofile 0行時に非0終了するよう修正済みだが、run-daily.sh側にも
# 独立した5ファイル健全性チェックがあることをこのスタブで確認する)。
set -euo pipefail
OUT=""
while [ $# -gt 0 ]; do
  case "$1" in
    --out) OUT="$2"; shift 2 ;;
    *) shift ;;
  esac
done
echo '[]' > "$OUT/daily.json"
echo '[]' > "$OUT/monthly.json"
echo '{}' > "$OUT/meta.json"
echo '{}' > "$OUT/bills.json"
exit 0
STUB
chmod +x "$BROKEN_BUNDLE/aggregate.sh"

BEFORE=$(origin_log_count)
bash "$RUN_DAILY" \
    --blog-metrics-dir "$BROKEN_BUNDLE" --clone-dir "$CLONE" --lock-file "$LOCK_FILE" \
    --state-dir "$STATE_DIR" --log-file "$LOG_FILE" --ssh-host fakehost \
    --export-cmd "$T/bin/stub-export.sh" --profile-since-days 1 >/dev/null 2>&1
RC=$?
assert_eq "11 exit" "$RC" "1"
AFTER=$(origin_log_count)
assert_eq "11 no commit" "$AFTER" "$BEFORE"
STREAK=$(python3 -c "import json;print(json.load(open('$STATE_DIR/fail_state.json'))['fail_streak_days'])" 2>/dev/null)
assert_eq "11 fail_streak_days" "$STREAK" "1"
# QA指摘#4: run_once の5ファイル健全性チェック自体が無効化されて素通りしていないことを、
# ログにファイル名が名指しされていることで確認する（ガード無効化の回帰防止）。
assert_contains "11 log names the missing file" "$(cat "$LOG_FILE" 2>/dev/null)" "aggregate.sh の出力に layers.json がありません"
teardown

echo "# 12. clone に data/metrics/ が無い場合(初期セットアップ未完了): 非0で失敗し自動作成しない"
setup
INCOMPLETE_ORIGIN="$T/incomplete-origin.git"
git init -q --bare --initial-branch=main "$INCOMPLETE_ORIGIN"
INCOMPLETE_SEED=$(mktemp -d)
git clone -q "$INCOMPLETE_ORIGIN" "$INCOMPLETE_SEED"
echo "# solar-metrics-data (incomplete)" > "$INCOMPLETE_SEED/README.md"
( cd "$INCOMPLETE_SEED" && git add -A && git -c user.name=seed -c user.email=seed@example.com commit -q -m seed && git push -q origin main )
rm -rf "$INCOMPLETE_SEED"
INCOMPLETE_CLONE="$T/incomplete-clone"
git clone -q "$INCOMPLETE_ORIGIN" "$INCOMPLETE_CLONE"

BEFORE=$(git --git-dir="$INCOMPLETE_ORIGIN" log --oneline main 2>/dev/null | wc -l | tr -d ' ')
bash "$RUN_DAILY" \
    --blog-metrics-dir "$BUNDLE" --clone-dir "$INCOMPLETE_CLONE" --lock-file "$LOCK_FILE" \
    --state-dir "$STATE_DIR" --log-file "$LOG_FILE" --ssh-host fakehost \
    --export-cmd "$T/bin/stub-export.sh" --profile-since-days 1 >/dev/null 2>&1
RC=$?
assert_eq "12 exit" "$RC" "1"
AFTER=$(git --git-dir="$INCOMPLETE_ORIGIN" log --oneline main 2>/dev/null | wc -l | tr -d ' ')
assert_eq "12 no commit" "$AFTER" "$BEFORE"
[ -d "$INCOMPLETE_CLONE/data/metrics" ] && fail "12 data/metrics should not be auto-created" || ok
teardown

echo "# 13. G9(履歴不変性)違反: pushされずローカルcommitもロールバックされる"
setup
export STUB_DAILY_JSON='[{"date":"2026-08-01","solar_kwh":20.0,"buy_kwh":0.0,"sell_kwh":5.0,"nichicon_charge_kwh":null,"ecoflow_charge_kwh":null,"status":"COMPLETE"}]'
export STUB_META_JSON='{"power_history_since":"2026-08-01","nichicon_data_since":null,"ecoflow_data_since":null}'
export BLOG_METRICS_TODAY="2026-08-01"
run_daily_success_args >/dev/null 2>&1   # 2026-08-01 を確定コミット
BEFORE=$(origin_log_count)
BEFORE_HEAD=$(git -C "$CLONE" rev-parse HEAD)

# 確定済み日(2026-08-01)の値を書き換え、todayをそこから20日超先にしてG9に抵触させる
export STUB_DAILY_JSON='[{"date":"2026-08-01","solar_kwh":25.0,"buy_kwh":0.0,"sell_kwh":5.0,"nichicon_charge_kwh":null,"ecoflow_charge_kwh":null,"status":"COMPLETE"}]'
export BLOG_METRICS_TODAY="2026-09-25"
bash "$RUN_DAILY" \
    --blog-metrics-dir "$BUNDLE" --clone-dir "$CLONE" --lock-file "$LOCK_FILE" \
    --state-dir "$STATE_DIR" --log-file "$LOG_FILE" --ssh-host fakehost \
    --export-cmd "$T/bin/stub-export.sh" --profile-since-days 1 >/dev/null 2>&1
RC=$?
assert_eq "13 exit" "$RC" "1"
AFTER=$(origin_log_count)
assert_eq "13 no push (origin unchanged)" "$AFTER" "$BEFORE"
AFTER_HEAD=$(git -C "$CLONE" rev-parse HEAD)
assert_eq "13 local commit rolled back" "$AFTER_HEAD" "$BEFORE_HEAD"
assert_contains "13 log mentions validate failure" "$(cat "$LOG_FILE" 2>/dev/null)" "validate_metrics.py が失敗したため"
teardown

echo "# 14. 3日連続失敗→通知1通、6日ギャップ→新インシデントとして再度3日連続失敗→2通目の通知(QA指摘#2)"
setup
BLOG_METRICS_TODAY="2026-09-01" run_daily_failure_args >/dev/null 2>&1
BLOG_METRICS_TODAY="2026-09-02" run_daily_failure_args >/dev/null 2>&1
BLOG_METRICS_TODAY="2026-09-03" run_daily_failure_args >/dev/null 2>&1
CALLS=$(notify_calls)
CALL_COUNT=$(printf '%s\n' "$CALLS" | /usr/bin/grep -c . || true)
assert_eq "14 first streak notify count" "$CALL_COUNT" "1"
# 6日ギャップ（4〜8日目は実行されない想定）。9日目は新しいインシデントとして1から数え直す。
BLOG_METRICS_TODAY="2026-09-09" run_daily_failure_args >/dev/null 2>&1
STREAK=$(python3 -c "import json;print(json.load(open('$STATE_DIR/fail_state.json'))['fail_streak_days'])" 2>/dev/null)
assert_eq "14 streak resets to 1 after gap" "$STREAK" "1"
CALLS=$(notify_calls)
CALL_COUNT=$(printf '%s\n' "$CALLS" | /usr/bin/grep -c . || true)
assert_eq "14 no extra notify right after gap restart" "$CALL_COUNT" "1"
BLOG_METRICS_TODAY="2026-09-10" run_daily_failure_args >/dev/null 2>&1
BLOG_METRICS_TODAY="2026-09-11" run_daily_failure_args >/dev/null 2>&1
CALLS=$(notify_calls)
CALL_COUNT=$(printf '%s\n' "$CALLS" | /usr/bin/grep -c . || true)
assert_eq "14 second streak notify count" "$CALL_COUNT" "2"
assert_contains "14 second notify subject" "$CALLS" "障害"
teardown

echo "passed=$PASSES failed=$FAILS"
[ "$FAILS" = 0 ]
