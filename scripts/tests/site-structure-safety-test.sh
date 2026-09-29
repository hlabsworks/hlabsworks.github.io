#!/bin/bash
# site-structure-test.sh / site-head-test.sh 自体の回帰テスト。
# 旧実装は content/labs/solar/monthly-report/ を trap で無条件に rm -rf しており、実行前から
# 存在した運営者のファイルや他プロセスの生成物を消しうるバグがあった。各スクリプトを
# 「スクラッチにコピーしたリポジトリ」に対して実行し、
#   (1) 実行前から content/labs/solar/monthly-report/ に存在するファイルが、実行後も内容含めて
#       変化しないこと
#   (2) 実行前後で（コピー先の）リポジトリのファイル一覧・ディレクトリ一覧に増減が無く、
#       既存ファイルの中身も書き換わっていないこと
#       （resources/・public/ 等の生成物が作業ツリー相当の場所に残らないこと）
# を確認する。ネットワークアクセスはしない。
# 実行: bash scripts/tests/site-structure-safety-test.sh
set -u
HERE=$(cd "$(dirname "$0")" && pwd)
REPO_ROOT=$(cd "$HERE/../.." && pwd)
FAILS=0; PASSES=0
ok()   { PASSES=$((PASSES+1)); }
fail() { FAILS=$((FAILS+1)); echo "FAIL: $*"; }

T=$(mktemp -d)
trap 'rm -rf "$T"' EXIT

check_script_does_not_touch_worktree() { # $1=対象スクリプトの相対パス(scripts/tests/以下)
  local script_relpath="$1"
  local case_t case_copy keep before_listing before_sha after_listing after_sha diff sha_diff rc

  case_t=$(mktemp -d)
  # 対象スクリプト自身が「作業ツリーに書き込まない」ことを確認したいので、実リポジトリ
  # ではなく、まるごとコピーした「作業ツリー相当」の場所に対して実行する（.git は不要）。
  case_copy="$case_t/repo-copy"
  mkdir -p "$case_copy"
  rsync -a \
    --exclude '.git' --exclude 'public/' --exclude 'resources/' \
    --exclude '.hugo_build.lock' --exclude '__pycache__' --exclude '*.pyc' \
    --exclude '.DS_Store' --exclude '*.swp' \
    "$REPO_ROOT/" "$case_copy/"

  # 実行前から存在する運営者のファイル・他プロセスの生成物を模したフィクスチャ。
  mkdir -p "$case_copy/content/labs/solar/monthly-report"
  keep="$case_copy/content/labs/solar/monthly-report/keep.md"
  printf -- '---\ntitle: "keep"\ndate: 2026-01-01\n---\n\n運営者の既存ファイル（テストでは削除しない）。\n' > "$keep"
  local keep_before
  keep_before=$(cat "$keep")
  # 空ディレクトリの新規作成の見逃しを検出するためのフィクスチャ。
  mkdir -p "$case_copy/content/labs/solar/monthly-report/empty-dir-kept-by-operator"

  before_listing="$case_t/before.txt"
  find "$case_copy" | LC_ALL=C sort > "$before_listing"
  before_sha="$case_t/before.sha256"
  find "$case_copy" -type f -print0 | LC_ALL=C sort -z | xargs -0 shasum -a 256 > "$before_sha"

  bash "$case_copy/$script_relpath" >"$case_t/run.log" 2>&1
  rc=$?
  if [ "$rc" = 0 ]; then ok; else fail "${script_relpath}（コピー上で実行）が失敗した: $(tail -20 "$case_t/run.log")"; fi

  if [ -f "$keep" ]; then ok; else fail "$script_relpath 実行後、content/labs/solar/monthly-report/keep.md が消えている"; fi
  if [ -f "$keep" ] && [ "$(cat "$keep")" = "$keep_before" ]; then
    ok
  else
    fail "$script_relpath 実行後、content/labs/solar/monthly-report/keep.md の内容が変化した"
  fi

  after_listing="$case_t/after.txt"
  find "$case_copy" | LC_ALL=C sort > "$after_listing"
  diff=$(diff "$before_listing" "$after_listing" || true)
  if [ -z "$diff" ]; then
    ok
  else
    fail "$script_relpath の実行前後でファイル・ディレクトリ一覧が変化した（作業ツリー相当への書き込みの疑い）: $diff"
  fi

  after_sha="$case_t/after.sha256"
  find "$case_copy" -type f -print0 | LC_ALL=C sort -z | xargs -0 shasum -a 256 > "$after_sha"
  sha_diff=$(diff "$before_sha" "$after_sha" || true)
  if [ -z "$sha_diff" ]; then
    ok
  else
    fail "$script_relpath の実行前後で既存ファイルの内容が変化した（keep.md以外の書き換えの疑い）: $sha_diff"
  fi

  rm -rf "$case_t"
}

# site-structure-test.shのdraft検査はビルド後のindex.htmlの有無とdraftフラグだけを見るのに対し、
# no-draft-leak-test.shは出力全ファイルを「下書きメモ」「TODO」の文字列で横断検査する（目的が異なる
# ため両方が必要）。
for script in scripts/tests/site-structure-test.sh scripts/tests/site-head-test.sh scripts/tests/no-draft-leak-test.sh; do
  echo "# $script"
  check_script_does_not_touch_worktree "$script"
done

echo "passed=$PASSES failed=$FAILS"
[ "$FAILS" = 0 ]
