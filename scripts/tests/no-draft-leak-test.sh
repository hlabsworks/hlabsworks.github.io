#!/bin/bash
# 未完成記事（本文冒頭に「下書きメモ」＋ TODO が残る記事）が、front matter の draft 設定ミスで
# 本番ビルドに公開されてしまうことを検出する回帰テスト。
# --buildFuture を付けて将来日付の記事も対象にビルドし、出力 HTML のどのページにも
# 「下書きメモ」「TODO」の文字列が含まれないことを確認する。
# 作業ツリーを汚さないよう、リポジトリを mktemp のディレクトリへコピーしてそこでビルドする。
# 実行: bash scripts/tests/no-draft-leak-test.sh
set -u
HERE=$(cd "$(dirname "$0")" && pwd)
REPO_ROOT=$(cd "$HERE/../.." && pwd)
FAILS=0; PASSES=0
ok()   { PASSES=$((PASSES+1)); }
fail() { FAILS=$((FAILS+1)); echo "FAIL: $*"; }

T=$(mktemp -d)
trap 'rm -rf "$T"' EXIT

COPY="$T/repo"
mkdir -p "$COPY"
# .git は不要（かつ submodule 実体の複製を避けるため）、テーマと content 等のみコピーする。
/usr/bin/tar -C "$REPO_ROOT" --exclude=.git -cf - . | /usr/bin/tar -C "$COPY" -xf -

DEST="$T/build-buildfuture"
cd "$COPY" || exit 1
hugo --gc --minify --buildFuture -e production --destination "$DEST" >/dev/null 2>"$T/hugo.log"
RC=$?
if [ "$RC" = 0 ]; then ok; else fail "hugo build failed: $(cat "$T/hugo.log")"; fi

echo "# 1. --buildFuture 本番ビルドの出力に「下書きメモ」を含むページが無い"
hits=$(/usr/bin/grep -rl "下書きメモ" "$DEST" 2>/dev/null || true)
if [ -z "$hits" ]; then ok; else fail "「下書きメモ」が残っているページが見つかった: $hits"; fi

echo "# 2. --buildFuture 本番ビルドの出力に「TODO」を含むページが無い"
hits=$(/usr/bin/grep -rl "TODO" "$DEST" 2>/dev/null || true)
if [ -z "$hits" ]; then ok; else fail "「TODO」が残っているページが見つかった: $hits"; fi

echo "passed=$PASSES failed=$FAILS"
[ "$FAILS" = 0 ]
