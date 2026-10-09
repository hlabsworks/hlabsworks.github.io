#!/usr/bin/env bash
# scripts/blog-metrics/site-health-check.sh の自動テスト。curl と notify.sh をスタブに差し替える。
# 実行: bash scripts/blog-metrics/tests/site-health-check-test.sh
set -u
HERE="$(cd "$(dirname "$0")" && pwd)"
SCRIPT="${HERE}/../site-health-check.sh"
PASS=0; FAIL=0
ok() { PASS=$((PASS+1)); }
fail() { FAIL=$((FAIL+1)); echo "FAIL: $*"; }
assert_eq() { if [ "$2" = "$3" ]; then ok; else fail "$1: expected [$3] got [$2]"; fi; }
assert_contains() { case "$2" in *"$3"*) ok ;; *) fail "$1: [$2] に [$3] が含まれない" ;; esac; }

setup() {
    TMP="$(mktemp -d)"; STATE="${TMP}/state"; FIX="${TMP}/fix"; CLONE="${TMP}/clone/data/metrics"
    mkdir -p "${STATE}" "${FIX}" "${CLONE}"
    NOTIFY_LOG="${TMP}/notify.log"; : > "${NOTIFY_LOG}"
    cat > "${TMP}/notify.sh" <<N
#!/usr/bin/env bash
printf '%s\t%s\n' "\$1" "\$2" >> "${NOTIFY_LOG}"
N
    chmod +x "${TMP}/notify.sh"
    # curl スタブ: URL に応じてフィクスチャを返す。環境変数 STUB_FAIL_RUNS / STUB_FAIL_SITE で失敗を模す
    cat > "${TMP}/curl" <<C
#!/usr/bin/env bash
url="\${@: -1}"
case "\$url" in
  *api.github.com*) [ "\${STUB_FAIL_RUNS:-}" = "1" ] && exit 22; cat "${FIX}/runs.json" ;;
  *meta.json*)      [ "\${STUB_FAIL_SITE:-}" = "1" ] && exit 22; cat "${FIX}/site_meta.json" ;;
  *) exit 22 ;;
esac
C
    chmod +x "${TMP}/curl"
    export BLOG_METRICS_NOTIFY_CMD="${TMP}/notify.sh" BLOG_METRICS_CURL="${TMP}/curl" BLOG_METRICS_NOW="2026-10-10 10:30:00"
    unset STUB_FAIL_RUNS STUB_FAIL_SITE
}
teardown() { rm -rf "${TMP}"; }
runs_json() { # $1=id $2=conclusion
    printf '{"workflow_runs":[{"id":%s,"event":"schedule","conclusion":"%s","html_url":"https://github.com/x/y/actions/runs/%s"}]}' "$1" "$2" "$1" > "${FIX}/runs.json"
}
site_meta() { printf '{"generated_at":"%s"}' "$1" > "${FIX}/site_meta.json"; }
local_meta() { printf '{"generated_at":"%s"}' "$1" > "${CLONE}/meta.json"; }
run_check() { bash "${SCRIPT}" --state-dir "${STATE}" --log-file "${TMP}/health.log" --data-clone "${TMP}/clone" "$@" >/dev/null 2>&1; }
notify_count() { /usr/bin/grep -c . "${NOTIFY_LOG}" || true; }

echo "# 1. 正常（success・鮮度OK）: 通知なし"
setup; runs_json 1 success; site_meta "2026-10-10 07:31:00"; local_meta "2026-10-10 07:31:00"
run_check; assert_eq "1 count" "$(notify_count)" "0"; teardown

echo "# 2. ビルド失敗: 通知1通、同じランの再実行では再送なし、次の success で復旧1通"
setup; runs_json 10 failure; site_meta "2026-10-09 07:31:00"; local_meta "2026-10-09 07:31:00"
run_check; assert_eq "2 first" "$(notify_count)" "1"; assert_contains "2 subject" "$(cat "${NOTIFY_LOG}")" "ビルド失敗"
assert_contains "2 url" "$(cat "${NOTIFY_LOG}")" "actions/runs/10"
run_check; assert_eq "2 no resend" "$(notify_count)" "1"
runs_json 11 failure; run_check; assert_eq "2 new failed run alerts again" "$(notify_count)" "2"
runs_json 12 success; run_check; assert_eq "2 recover" "$(notify_count)" "3"; assert_contains "2 recover subject" "$(cat "${NOTIFY_LOG}")" "ビルド復旧"
run_check; assert_eq "2 no resend after recover" "$(notify_count)" "3"; teardown

echo "# 3. データ遅延（30時間超）: 通知1通、同じ遅れでは再送なし、追いついたら復旧1通"
setup; runs_json 1 success; site_meta "2026-10-08 07:31:00"; local_meta "2026-10-10 07:31:00"
run_check; assert_eq "3 alert" "$(notify_count)" "1"; assert_contains "3 subject" "$(cat "${NOTIFY_LOG}")" "データが古い"
run_check; assert_eq "3 no resend" "$(notify_count)" "1"
local_meta "2026-10-11 07:31:00"; run_check; assert_eq "3 local advanced -> alert again" "$(notify_count)" "2"
site_meta "2026-10-11 07:31:00"; run_check; assert_eq "3 recover" "$(notify_count)" "3"; assert_contains "3 recover subject" "$(cat "${NOTIFY_LOG}")" "データ復旧"; teardown

echo "# 4. 遅延が閾値以内（26時間）: 通知なし"
setup; runs_json 1 success; site_meta "2026-10-09 05:31:00"; local_meta "2026-10-10 07:31:00"
run_check; assert_eq "4 count" "$(notify_count)" "0"; teardown

echo "# 5. 到達不能（curl 失敗）: 通知なし・ログに記録・exit 0"
setup; runs_json 10 failure; site_meta "2026-10-01 07:31:00"; local_meta "2026-10-10 07:31:00"
STUB_FAIL_RUNS=1 STUB_FAIL_SITE=1 bash "${SCRIPT}" --state-dir "${STATE}" --log-file "${TMP}/health.log" --data-clone "${TMP}/clone" >/dev/null 2>&1
assert_eq "5 exit" "$?" "0"; assert_eq "5 count" "$(notify_count)" "0"
assert_contains "5 log" "$(cat "${TMP}/health.log")" "到達不能"; teardown

echo "# 6. 不正JSON: 通知なし・exit 0"
setup; echo "{ not json" > "${FIX}/runs.json"; echo "{ not json" > "${FIX}/site_meta.json"; local_meta "2026-10-10 07:31:00"
run_check; assert_eq "6 exit" "$?" "0"; assert_eq "6 count" "$(notify_count)" "0"; teardown

echo "# 7. 不明な引数: exit 2"
setup; bash "${SCRIPT}" --bogus >/dev/null 2>&1; assert_eq "7 exit" "$?" "2"; teardown

echo "passed=${PASS} failed=${FAIL}"
[ "${FAIL}" -eq 0 ]
