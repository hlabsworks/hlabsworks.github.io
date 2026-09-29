#!/bin/bash
# GA4 / Google AdSense / Cloudflare Web Analytics のタグが、
#   (a) ID未設定（空文字の上書き設定）なら本番ビルドでも一切出力されない
#   (b) ID設定済みなら本番ビルドでのみ出力される
#   (c) ID設定済みでも development ビルドでは出力されない
# ことを確認する回帰テスト。ネットワークアクセスはしない（hugo build の出力を静的に検査するだけ）。
#
# 作業ツリーには一切書き込まない。site-structure-test.shと同じ「一時ディレクトリへコピーしてから
# ビルドする」方式にそろえる。
# 実行: bash scripts/tests/site-head-test.sh
set -u
HERE=$(cd "$(dirname "$0")" && pwd)
REPO_ROOT=$(cd "$HERE/../.." && pwd)
FAILS=0; PASSES=0
ok()   { PASSES=$((PASSES+1)); }
fail() { FAILS=$((FAILS+1)); echo "FAIL: $*"; }

assert_none_present() { # $1=destdir $2=label
  local label="$2"
  local hits
  hits=$(/usr/bin/grep -ril "adsbygoogle\|cloudflareinsights\|gtag(" "$1" 2>/dev/null || true)
  if [ -z "$hits" ]; then ok; else fail "$label: 出力されないはずのタグが見つかった: $hits"; fi
}

assert_all_present() { # $1=file $2=label
  local file="$1" label="$2"
  local content
  content=$(cat "$file" 2>/dev/null || true)
  if printf '%s' "$content" | /usr/bin/grep -qF "adsbygoogle"; then ok; else fail "$label: adsbygoogle が見つからない"; fi
  if printf '%s' "$content" | /usr/bin/grep -qF "cloudflareinsights"; then ok; else fail "$label: cloudflareinsights が見つからない"; fi
  if printf '%s' "$content" | /usr/bin/grep -qF "gtag("; then ok; else fail "$label: gtag( が見つからない"; fi
}

assert_no_ads_but_ga() { # $1=file $2=label
  # 事業トップ・/about/・/apps/・/contact/・/privacy-policy/ 等(noAds:true)は
  # adsOnBusinessPages=falseのときAdSenseを出さないが、GA4は全ページ共通なので出す。
  local file="$1" label="$2"
  local content
  content=$(cat "$file" 2>/dev/null || true)
  if printf '%s' "$content" | /usr/bin/grep -qF "adsbygoogle"; then fail "$label: noAdsページなのにadsbygoogleが出力されている"; else ok; fi
  if printf '%s' "$content" | /usr/bin/grep -qF "gtag("; then ok; else fail "$label: gtag( が見つからない"; fi
}

T=$(mktemp -d)
trap 'rm -rf "$T"' EXIT

# site-structure-test.shと同じ除外方式のコピーでビルドする。
SITE="$T/site"
mkdir -p "$SITE"
rsync -a \
  --exclude '.git' --exclude 'public' --exclude 'resources' \
  --exclude '.hugo_build.lock' --exclude '__pycache__' --exclude '*.pyc' \
  --exclude '.DS_Store' --exclude '*.swp' --exclude '.claude' \
  --exclude 'content/labs/solar/monthly-report' \
  "$REPO_ROOT/" "$SITE/"
mkdir -p "$SITE/content/labs/solar/monthly-report"

cd "$SITE" || exit 1

OVERRIDE="$T/override.toml"
cat > "$OVERRIDE" <<'EOF'
[services.googleAnalytics]
  ID = "G-TEST"

[params.adsense]
  client = "ca-pub-0000000000000000"

[params.cloudflareAnalytics]
  token = "test-token"
EOF

echo "# 1. 既定設定(ID未設定) + 本番ビルド: 3タグとも出力されない"
DEST1="$T/default-production"
BLANK="$T/blank.toml"
cat > "$BLANK" <<'EOF'
[services]
  [services.googleAnalytics]
  ID = ""
[params]
  [params.adsense]
  client = ""
  [params.cloudflareAnalytics]
  token = ""
EOF
hugo --quiet -e production --config "hugo.toml,$BLANK" --destination "$DEST1" >/dev/null 2>"$T/hugo1.log"
RC=$?
if [ "$RC" = 0 ]; then ok; else fail "1 hugo build failed: $(cat "$T/hugo1.log")"; fi
assert_none_present "$DEST1" "1"

echo "# 2. override設定(ID設定済み) + 本番ビルド + 既定のadsOnBusinessPages(true): 全ページで3タグとも出力される"
# AdSense審査対応でadsOnBusinessPagesの既定値をtrueにしているため、
# hugo.tomlを上書きしない既定状態では事業ページ(noAds:true)にも広告が出る。
DEST2="$T/override-production"
hugo --quiet -e production --config "hugo.toml,$OVERRIDE" --destination "$DEST2" >/dev/null 2>"$T/hugo2.log"
RC=$?
if [ "$RC" = 0 ]; then ok; else fail "2 hugo build failed: $(cat "$T/hugo2.log")"; fi
assert_all_present "$DEST2/labs/solar/index.html" "2-labs"
assert_all_present "$DEST2/labs/solar/2026/hello/index.html" "2-labs-post"
assert_all_present "$DEST2/labs/tags/index.html" "2-labs-tags"
for rel in index.html about/index.html apps/index.html contact/index.html privacy-policy/index.html; do
  assert_all_present "$DEST2/$rel" "2-${rel}(adsOnBusinessPages既定true)"
done

echo "# 2b. override設定 + adsOnBusinessPages=false: 事業ページはAdSenseを出さないがGA4は出す。Labs配下は変わらず出る"
# noAdsの検査対象は/apps/だけでなく事業ページ全部(index.html, about.md, contact.md,
# privacy-policy.md)に適用し、about.mdのnoAds相当を外す変異でも検出できるようにする。
# Labs側に広告が出ることのassertも追加する。
ADS_OFF_BUSINESS_OVERRIDE="$T/ads-off-business.toml"
cat > "$ADS_OFF_BUSINESS_OVERRIDE" <<'EOF'
[params.business]
  adsOnBusinessPages = false
EOF
DEST2B="$T/override-production-ads-off-business"
hugo --quiet -e production --config "hugo.toml,$OVERRIDE,$ADS_OFF_BUSINESS_OVERRIDE" --destination "$DEST2B" >/dev/null 2>"$T/hugo2b.log"
RC=$?
if [ "$RC" = 0 ]; then ok; else fail "2b hugo build failed: $(cat "$T/hugo2b.log")"; fi
for rel in index.html about/index.html apps/index.html contact/index.html privacy-policy/index.html; do
  assert_no_ads_but_ga "$DEST2B/$rel" "2b-${rel}"
done
assert_all_present "$DEST2B/labs/solar/index.html" "2b-labs(adsOnBusinessPagesの影響を受けない)"

echo "# 3. override設定(ID設定済み) + development ビルド: 3タグとも出力されない"
DEST3="$T/override-development"
hugo --quiet -e development --config "hugo.toml,$OVERRIDE" --destination "$DEST3" >/dev/null 2>"$T/hugo3.log"
RC=$?
if [ "$RC" = 0 ]; then ok; else fail "3 hugo build failed: $(cat "$T/hugo3.log")"; fi
assert_none_present "$DEST3" "3"

echo "passed=$PASSES failed=$FAILS"
[ "$FAILS" = 0 ]
