#!/bin/bash
# GA4 / Google AdSense / Cloudflare Web Analytics のタグが、
#   (a) ID未設定なら本番ビルドでも一切出力されない
#   (b) ID設定済みなら本番ビルドでのみ出力される
#   (c) ID設定済みでも development ビルドでは出力されない
# ことを確認する回帰テスト。ネットワークアクセスはしない（hugo build の出力を静的に検査するだけ）。
# 実行: bash scripts/tests/site-head-test.sh
set -u
HERE=$(cd "$(dirname "$0")" && pwd)
REPO_ROOT=$(cd "$HERE/../.." && pwd)
FAILS=0; PASSES=0
ok()   { PASSES=$((PASSES+1)); }
fail() { FAILS=$((FAILS+1)); echo "FAIL: $*"; }

TAGS='adsbygoogle
cloudflareinsights
gtag('

assert_none_present() { # $1=label $2=destdir
  local label="$2"
  local hits
  hits=$(/usr/bin/grep -ril "adsbygoogle\|cloudflareinsights\|gtag(" "$1" 2>/dev/null || true)
  if [ -z "$hits" ]; then ok; else fail "$label: 出力されないはずのタグが見つかった: $hits"; fi
}

assert_all_present() { # $1=destdir $2=label
  local dest="$1" label="$2"
  local content
  content=$(cat "$dest/index.html" 2>/dev/null || true)
  if printf '%s' "$content" | /usr/bin/grep -qF "adsbygoogle"; then ok; else fail "$label: adsbygoogle が見つからない"; fi
  if printf '%s' "$content" | /usr/bin/grep -qF "cloudflareinsights"; then ok; else fail "$label: cloudflareinsights が見つからない"; fi
  if printf '%s' "$content" | /usr/bin/grep -qF "gtag("; then ok; else fail "$label: gtag( が見つからない"; fi
}

T=$(mktemp -d)
trap 'rm -rf "$T"' EXIT

OVERRIDE="$T/override.toml"
cat > "$OVERRIDE" <<'EOF'
[services.googleAnalytics]
  ID = "G-TEST"

[params.adsense]
  client = "ca-pub-0000000000000000"

[params.cloudflareAnalytics]
  token = "test-token"
EOF

cd "$REPO_ROOT" || exit 1

echo "# 1. 既定設定(ID未設定) + 本番ビルド: 3タグとも出力されない"
DEST1="$T/default-production"
hugo --quiet -e production --destination "$DEST1" >/dev/null 2>"$T/hugo1.log"
RC=$?
if [ "$RC" = 0 ]; then ok; else fail "1 hugo build failed: $(cat "$T/hugo1.log")"; fi
assert_none_present "$DEST1" "1"

echo "# 2. override設定(ID設定済み) + 本番ビルド: 3タグとも出力される"
DEST2="$T/override-production"
hugo --quiet -e production --config "hugo.toml,$OVERRIDE" --destination "$DEST2" >/dev/null 2>"$T/hugo2.log"
RC=$?
if [ "$RC" = 0 ]; then ok; else fail "2 hugo build failed: $(cat "$T/hugo2.log")"; fi
assert_all_present "$DEST2" "2"

echo "# 3. override設定(ID設定済み) + development ビルド: 3タグとも出力されない"
DEST3="$T/override-development"
hugo --quiet -e development --config "hugo.toml,$OVERRIDE" --destination "$DEST3" >/dev/null 2>"$T/hugo3.log"
RC=$?
if [ "$RC" = 0 ]; then ok; else fail "3 hugo build failed: $(cat "$T/hugo3.log")"; fi
assert_none_present "$DEST3" "3"

echo "passed=$PASSES failed=$FAILS"
[ "$FAILS" = 0 ]
