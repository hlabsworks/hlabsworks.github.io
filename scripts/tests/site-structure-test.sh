#!/bin/bash
# Labsのテーマ別パス移設（/labs/<テーマ>/）の回帰テスト。
#   (a) 新URL側の必須ファイルが生成されること
#   (b) 旧URL側に http-equiv="refresh" のリダイレクトページが残り、新URLを指していること
#   (c) 全ページの内部リンク(<a href>)が、移設後の出力に実在する場所を指していること
#   (d) 2つ目のテーマを注入しても、Labsトップの一覧・各テーマの記事一覧が正しく分離されること
# 本番の `hugo --gc --minify` 相当（--minify）でビルドし、minify後のHTML（属性値の引用符省略等）
# でも各検査が機能することを確認する。
# ネットワークアクセスはしない（hugo build の出力を静的に検査するだけ）。
#
# 作業ツリーには一切書き込まない: リポジトリを一時ディレクトリに
# コピーし、月次レポートの生成・hugo buildはすべてそのコピー上で行う。旧実装は
# `content/labs/solar/monthly-report/` を trap で無条件に rm -rf していたため、実行前から
# 存在した運営者のファイルや他プロセスの生成物を消しうるバグがあった。
#
# bash 3.2（macOS標準）でも bash 5.x でも構文エラーなく動くこと:
# `$(... <<'PYEOF' ... PYEOF)` のようにヒアドキュメントをコマンド置換の中に直接書くと、
# ヒアドキュメント本文に含まれる `'` の解釈をbash 3.2が誤り、構文エラー(rc=2)になる。
# ヒアドキュメントは必ず単独の文としてファイルへ出力し、その後 `$(cat file)` で読む。
# 実行: bash scripts/tests/site-structure-test.sh
set -u
MIN_CHECKED=100    # (c)内部リンク検査の「検査漏れ」下限件数(<a href>の総数)
MIN_PAGES=30       # 言語設定検査の「検査漏れ」下限件数(index.htmlの総数)
# REPO_ROOT配下のPythonスクリプトをimport/実行する際に __pycache__/*.pyc を
# 作業ツリーへ書き込ませない（バイトコードキャッシュも「一切書き込まない」の対象）。
export PYTHONDONTWRITEBYTECODE=1
HERE=$(cd "$(dirname "$0")" && pwd)
REPO_ROOT=$(cd "$HERE/../.." && pwd)
FAILS=0; PASSES=0
ok()   { PASSES=$((PASSES+1)); }
fail() { FAILS=$((FAILS+1)); echo "FAIL: $*"; }

T=$(mktemp -d)
trap 'rm -rf "$T"' EXIT

# hugoのビルドに必要な範囲を一時ディレクトリにコピーする。
# コピー対象を固定のディレクトリ名一覧にすると、将来 i18n/・
# archetypes/・config/ 等を追加したときにテストと本番ビルドの対象が食い違う
# （言語設定で追加したi18n/を拾えない、等）。生成物・VCS・作業ツリー限定ファイルだけを除外して
# リポジトリルート全体をコピーする方式にする。
SITE="$T/site"
mkdir -p "$SITE"
rsync -a \
  --exclude '.git' --exclude 'public' --exclude 'resources' \
  --exclude '.hugo_build.lock' --exclude '__pycache__' --exclude '*.pyc' \
  --exclude '.DS_Store' --exclude '*.swp' --exclude '.claude' \
  --exclude 'content/labs/solar/monthly-report' --exclude '_incoming' \
  "$REPO_ROOT/" "$SITE/"
# CIが生成する月次レポート(content/labs/solar/monthly-report/)は実リポジトリではなく
# コピー側に生成する。コピー側のディレクトリなので削除してもよい。
MONTHLY_REPORT_DIR="$SITE/content/labs/solar/monthly-report"
mkdir -p "$MONTHLY_REPORT_DIR"

cd "$SITE" || exit 1

echo "# 記事のtags: 記事本文(content/labs/*/YYYY/*.md)が全て空でないtagsを持つこと"
# post_nav_links.html/layouts/labs/list.html/layouts/labshome/rss.xmlはいずれも
# 「tagsを持つ=記事」という判定で記事一覧・前後リンク・RSS・最新記事一覧を組み立てている。
# tags未設定/空配列の記事は、front matterのミスだけでこれらから静かに消えるため、
# 記事ファイル側でtagsが空でないことを機械的に確認する。
ARTICLE_FILES=$(find content/labs -mindepth 3 -maxdepth 3 -path '*/[0-9][0-9][0-9][0-9]/*.md')
while IFS= read -r f; do
  [ -z "$f" ] && continue
  if /usr/bin/grep -qE '^tags: *\[[^]]*"' "$f"; then ok; else fail "記事のtags: ${f} が空でないtagsを持たない"; fi
done <<< "$ARTICLE_FILES"

# site-head-test.sh と同じ理由（本番判定に影響する値を無効化してノイズを消す）でoverrideを使う。
# GA4だけはテスト用IDを設定する: layouts/index.html等へのレイアウト
# 差し替えでも<head>(google_analytics.html)が欠落していないことを(d)で確認するため。
# gtagのscriptタグは<a href>を持たないため、(c)の内部リンク検査には影響しない。
OVERRIDE="$T/override.toml"
cat > "$OVERRIDE" <<'EOF'
[services.googleAnalytics]
  ID = "G-TEST"

[params.adsense]
  client = ""

[params.cloudflareAnalytics]
  token = ""
EOF

DEST="$T/public"

echo "# (0) CI相当の月次レポート生成"
# .github/workflows/hugo.yml の「Render monthly report posts」を模す。フィクスチャは
# test_render_monthly_posts.py の base_snapshot() をそのまま使い、期待値を二重管理しない。
POSTS_DIR="$T/incoming-posts"
mkdir -p "$POSTS_DIR"
python3 - "$REPO_ROOT" <<'PYEOF' > "$T/fixture.json"
import sys, json
sys.path.insert(0, sys.argv[1] + "/scripts/blog-metrics")
import test_render_monthly_posts as t
print(json.dumps(t.base_snapshot(), ensure_ascii=False))
PYEOF
FIXTURE_JSON=$(cat "$T/fixture.json")
FIXTURE_BILLING_MONTH=$(printf '%s' "$FIXTURE_JSON" | python3 -c "import json,sys; print(json.load(sys.stdin)['billing_month'])")
FIXTURE_REPORT_MONTH=$(printf '%s' "$FIXTURE_JSON" | python3 -c "import json,sys; print(json.load(sys.stdin)['report_month'])")
printf '%s' "$FIXTURE_JSON" > "$POSTS_DIR/${FIXTURE_BILLING_MONTH}.json"
python3 "$REPO_ROOT/scripts/blog-metrics/render_monthly_posts.py" --posts-dir "$POSTS_DIR" --out "$MONTHLY_REPORT_DIR" >"$T/render.log" 2>&1
RC=$?
if [ "$RC" = 0 ]; then ok; else fail "render_monthly_posts.py failed: $(cat "$T/render.log")"; fi
MONTHLY_REPORT_URL="labs/solar/monthly-report/${FIXTURE_REPORT_MONTH}/index.html"

hugo --gc --minify --quiet -e production --config "hugo.toml,$OVERRIDE" --buildFuture --destination "$DEST" >/dev/null 2>"$T/hugo.log"
RC=$?
if [ "$RC" = 0 ]; then ok; else fail "hugo build failed: $(cat "$T/hugo.log")"; fi

echo "# (a) 新URL側の必須ファイル"
REQUIRED_FILES='index.html
labs/index.html
labs/solar/index.html
labs/solar/index.xml
labs/solar/2026/hello/index.html
labs/solar/metrics/index.html
labs/solar/metrics/methodology/index.html
labs/solar/solar-charge-controller/index.html
labs/tags/index.html
labs/tags/お知らせ/index.html
labs/tags/月次実績/index.html
ads.txt
robots.txt
sitemap.xml'
while IFS= read -r rel; do
  if [ -s "$DEST/$rel" ]; then ok; else fail "必須ファイルが無い、または空: $rel"; fi
done <<< "$REQUIRED_FILES"

# categoriesタクソノミーを無効化しているため、Hugo既定の空の/categories/ページが
# 生成されないこと(hugo.tomlの[taxonomies]がtagのみになっていることの回帰テスト)。
if [ ! -e "$DEST/categories" ]; then ok; else fail "categoriesタクソノミーが無効化されていない(/categories/が生成されている)"; fi

if [ -s "$DEST/$MONTHLY_REPORT_URL" ]; then ok; else fail "CI生成の月次レポートが無い、または空: $MONTHLY_REPORT_URL"; fi

echo "# RSSへの月次レポート混入: CI生成の月次レポートが /labs/solar/index.xml と /labs/index.xml の両方にlinkとして入ること"
MONTHLY_REPORT_LINK="https://hlabsworks.com/labs/solar/monthly-report/${FIXTURE_REPORT_MONTH}/"
SOLAR_RSS=$(cat "$DEST/labs/solar/index.xml" 2>/dev/null || true)
if printf '%s' "$SOLAR_RSS" | /usr/bin/grep -qF "<link>${MONTHLY_REPORT_LINK}</link>"; then ok; else fail "RSS: /labs/solar/index.xml に月次レポート(${MONTHLY_REPORT_LINK})のlinkが無い"; fi
LABS_RSS=$(cat "$DEST/labs/index.xml" 2>/dev/null || true)
if printf '%s' "$LABS_RSS" | /usr/bin/grep -qF "<link>${MONTHLY_REPORT_LINK}</link>"; then ok; else fail "RSS: /labs/index.xml に月次レポート(${MONTHLY_REPORT_LINK})のlinkが無い"; fi

# [permalinks] が効いていないと tags/ が旧URL構成(ルート直下)のまま残る。
if [ ! -e "$DEST/tags" ]; then ok; else fail "旧URL構成の tags/ ディレクトリが生成されている（[permalinks] が効いていない疑い）: $DEST/tags"; fi

TAGS_INDEX=$(cat "$DEST/labs/tags/index.html" 2>/dev/null || true)
if printf '%s' "$TAGS_INDEX" | /usr/bin/grep -qE '<h1[^>]*>タグ</h1>'; then ok; else fail "labs/tags/index.html の見出しが「タグ」になっていない(content/tags/_index.mdのtitle参照)"; fi

if /usr/bin/grep -q '^Sitemap:' "$DEST/robots.txt" 2>/dev/null; then ok; else fail "robots.txt に Sitemap 行が無い"; fi
if /usr/bin/grep -qx 'Disallow: /' "$DEST/robots.txt" 2>/dev/null; then fail "robots.txt が Disallow: / になっている（本番ビルドなのに全面禁止）"; else ok; fi

SITEMAP=$(cat "$DEST/sitemap.xml" 2>/dev/null || true)
if printf '%s' "$SITEMAP" | /usr/bin/grep -qF 'https://hlabsworks.com/labs/solar/'; then ok; else fail "sitemap.xml に /labs/solar/ が含まれない"; fi
# 旧URLのルート(/posts/,/tags/,/metrics/,/solar-charge-controller/)、および
# R1'でテーマなしになった/labs/metrics/等の中間URLがsitemapに残っていないことをまとめて確認する。
for old_root in posts tags metrics solar-charge-controller blog labs/metrics labs/monthly-report labs/2026 labs/solar-charge-controller; do
  if printf '%s' "$SITEMAP" | /usr/bin/grep -qF "https://hlabsworks.com/${old_root}/"; then
    fail "sitemap.xml に旧/中間URL https://hlabsworks.com/${old_root}/ がまだ含まれている"
  else
    ok
  fi
done

echo "# 旧/中間URLのパス: 出力ディレクトリそのものに旧/中間URLのパスが存在しないこと(文字列一致だけだとaliasで生成されたファイルを見逃す)"
# sitemap.xmlの文字列不一致だけでは、hugoのaliasで
# `/blog/index.html` のような旧URL側のリダイレクトスタブが実在ファイルとして生成されて
# いても検出できない。出力ディレクトリを直接検索し、0件であることを確認する。
for old_path in blog labs/metrics labs/monthly-report labs/2026; do
  hits=$(find "$DEST" -path "*/${old_path}/*" -o -path "*/${old_path}" 2>/dev/null)
  if [ -z "$hits" ]; then ok; else fail "出力に旧/中間URLのパス ${old_path} が実在する: $(printf '%s' "$hits" | head -3)"; fi
done

echo "# 旧名称/旧URL: 出力(HTML/XML)に「ブログ」という語や /blog/ を含むURLが残っていないこと"
# 「ブログ」への改称後、記事本文中に文脈上必要な「ブログ」が残っていれば
# 報告する方針だが、現状の全記事・固定ページの文面は「Labs」「記事」に統一済みのため、
# 出力全体でゼロ件であることをassertする（0件でなくなった場合は運営者判断が必要）。
BLOG_WORD_OUT=$(find "$DEST" \( -name '*.html' -o -name '*.xml' \) -print)
BLOG_WORD_HITS=0
BLOG_URL_HITS=0
while IFS= read -r page; do
  [ -z "$page" ] && continue
  text=$(cat "$page" 2>/dev/null || true)
  if printf '%s' "$text" | /usr/bin/grep -qF "ブログ"; then
    BLOG_WORD_HITS=$((BLOG_WORD_HITS+1))
    fail "旧名称/旧URL: ${page#"$DEST"/} に「ブログ」という語が含まれている"
  fi
  if printf '%s' "$text" | /usr/bin/grep -qE '(https://hlabsworks\.com)?/blog/'; then
    BLOG_URL_HITS=$((BLOG_URL_HITS+1))
    fail "旧名称/旧URL: ${page#"$DEST"/} に /blog/ を含むURLが含まれている"
  fi
done <<< "$BLOG_WORD_OUT"
if [ "$BLOG_WORD_HITS" = 0 ]; then ok; fi
if [ "$BLOG_URL_HITS" = 0 ]; then ok; fi

echo "# 住居表現: 出力全体に住居・個人を示す禁止語が含まれないこと"
# 数量を表す「〜のうち」(例:「カタログ値14.3kWhのうち実際に」)は
# 住居と無関係なので、「うちで/うちには/うちでは/うちも/うちは」を禁止語に追加しても
# 誤検出しないことをフィクスチャで確認する（下のNOT-MATCHフィクスチャ参照）。
V4_OUT=$(find "$DEST" -name 'index.html' -print)
while IFS= read -r page; do
  [ -z "$page" ] && continue
  html=$(cat "$page" 2>/dev/null || true)
  rel="${page#"$DEST"/}"
  for word in "個人事業" "自宅" "我が家" "わが家" "うちの" "書き手の家" "うちで" "うちには" "うちでは" "うちも" "うちは"; do
    if printf '%s' "$html" | /usr/bin/grep -qF "$word"; then
      fail "住居表現: ${rel} に「${word}」が含まれている"
    else
      ok
    fi
  done
done <<< "$V4_OUT"
# フィクスチャ(mutation対象): 数量の「〜のうち」を誤検出しないこと。
V4_NOT_MATCH_SAMPLE="ポータブル電源4台、カタログ値14.3kWhのうち実際に使えるのは何kWhなのか"
V4_FALSE_POSITIVE=0
for word in "うちで" "うちには" "うちでは" "うちも" "うちは"; do
  if printf '%s' "$V4_NOT_MATCH_SAMPLE" | /usr/bin/grep -qF "$word"; then
    V4_FALSE_POSITIVE=1
    fail "住居表現: 数量の「のうち」フィクスチャ(${V4_NOT_MATCH_SAMPLE})が禁止語「${word}」に誤反応した"
  fi
done
if [ "$V4_FALSE_POSITIVE" = 0 ]; then ok; fi

echo "# 未完成記事: 未完成の予約記事2本(draft: true)が出力に含まれないこと"
DRAFT_PATHS='labs/solar/2026/soc-4percent-deep-discharge
labs/solar/2026/voltage-only-sell-detection'
while IFS= read -r p; do
  [ -z "$p" ] && continue
  if [ ! -e "$DEST/$p" ]; then ok; else fail "draft: true の記事が出力されている: $p"; fi
done <<< "$DRAFT_PATHS"
# 「下書きメモ」「TODO」を含む未完成記事が出力に1件も無いこと(draft検査の取りこぼし対策)。
DRAFT_MARKER_HITS=$(find "$DEST" -name 'index.html' -exec /usr/bin/grep -lF -e "下書きメモ" -e "TODO" {} + 2>/dev/null)
if [ -z "$DRAFT_MARKER_HITS" ]; then ok; else fail "出力に「下書きメモ」または「TODO」を含むページがある: $DRAFT_MARKER_HITS"; fi

echo "# 言語設定: 全HTMLページの<html lang>がjaであること"
# defaultContentLanguage/[languages.ja]を外すと<html lang="en">
# に戻る（PaperModのbaseof.htmlは.Site.Language.Langを出す。languageCodeパラメータだけでは
# 決まらない）。全index.htmlを対象に走査し、1件でもja以外があればfailする。
python3 - "$DEST" <<'PYEOF' > "$T/langcheck.out" 2>"$T/langcheck.err"
import re
import sys
import pathlib

dest = pathlib.Path(sys.argv[1])
# --minify後は属性値に空白が無ければ引用符が省略される(<html lang=ja>)ため両対応する。
lang_re = re.compile(r'<html\b[^>]*\blang=(?:"([^"]*)"|\'([^\']*)\'|([^\s>]+))', re.IGNORECASE)
checked = 0
bad = []
for page in sorted(dest.rglob("index.html")):
    text = page.read_text(encoding="utf-8", errors="replace")
    m = lang_re.search(text)
    checked += 1
    if not m:
        bad.append(f"{page.relative_to(dest)} に <html lang=...> が見つからない")
    else:
        lang = m.group(1) if m.group(1) is not None else (m.group(2) if m.group(2) is not None else m.group(3))
        if lang != "ja":
            bad.append(f"{page.relative_to(dest)} の lang が ja ではない: {lang!r}")
for b in bad:
    print(f"FAIL:{b}")
print(f"SUMMARY:{checked}:{len(bad)}")
PYEOF
LANG_CHECK_RC=$?
if [ "$LANG_CHECK_RC" = 0 ]; then ok; else fail "lang検査のPythonが異常終了した(rc=$LANG_CHECK_RC): $(cat "$T/langcheck.err")"; fi
SAW_LANG_SUMMARY=0
while IFS= read -r line; do
  case "$line" in
    FAIL:*) fail "${line#FAIL:}" ;;
    SUMMARY:*)
      SAW_LANG_SUMMARY=1
      rest="${line#SUMMARY:}"
      lang_checked_n="${rest%%:*}"; lang_bad_n="${rest#*:}"
      echo "  lang検査対象: ${lang_checked_n} 件、ja以外: ${lang_bad_n} 件"
      if [ "$lang_checked_n" -lt "$MIN_PAGES" ]; then
        fail "lang検査対象が${MIN_PAGES}件未満(${lang_checked_n}件)。検査漏れの疑い"
      elif [ "$lang_bad_n" = "0" ]; then
        ok
      fi
      ;;
  esac
done < "$T/langcheck.out"
if [ "$SAW_LANG_SUMMARY" = 1 ]; then ok; else fail "lang検査のSUMMARY行が出力されなかった（検査が実行されなかった疑い）"; fi

echo "# og:locale: トップページのog:localeがja_JPであること"
INDEX_HTML=$(cat "$DEST/index.html" 2>/dev/null || true)
if printf '%s' "$INDEX_HTML" | /usr/bin/grep -qE '<meta property="?og:locale"? content="?ja_JP"?'; then ok; else fail "og:locale: index.html のog:localeがja_JPでない"; fi

echo "# (b) 旧URLのリダイレクト（http-equiv=refresh で新URLへ）"
# 「旧パス 新パス」の対応表。読み込みは while read で1行ずつ。
# soc-4percent-deep-discharge/voltage-only-sell-detectionは
# 本文に下書きメモ・TODOが残る未完成記事のため draft: true にした(本番未公開)。
# 一度も公開されていない旧URLのalias検査対象からは外す(下の draft 検査で別途確認する)。
OLD_NEW_MAP='posts/ /labs/solar/
posts/2026/hello/ /labs/solar/2026/hello/
posts/2026/cloudy-september-portable-batteries/ /labs/solar/2026/cloudy-september-portable-batteries/
posts/2026/portable-battery-usable-capacity/ /labs/solar/2026/portable-battery-usable-capacity/
metrics/ /labs/solar/metrics/
metrics/methodology/ /labs/solar/metrics/methodology/
solar-charge-controller/ /labs/solar/solar-charge-controller/'
# alias（旧URLのリダイレクトスタブ）自体は実在ファイルなので、(c)の内部リンク
# 検査でリンク先として"実在する"扱いになってしまう。旧URLの集合を作って(c)側で弾く。
ALIAS_PATHS=""
while IFS=' ' read -r old new; do
  [ -z "$old" ] && continue
  page="$DEST/$old/index.html"
  content=$(cat "$page" 2>/dev/null || true)
  if [ -f "$page" ]; then ok; else fail "旧URLの index.html が無い: $old"; continue; fi
  # minify後は値に空白を含まない属性の引用符が省略される(http-equiv=refresh)ため両対応する。
  if printf '%s' "$content" | /usr/bin/grep -qE 'http-equiv="?refresh"?'; then ok; else fail "旧URL $old のページに http-equiv=refresh が無い"; fi
  # 部分一致(grep -F)だと、newが別ページのURLの接頭辞になっている場合に誤って
  # 合格してしまう。content="0; url=X" のXを抽出し、新URLと完全一致で比較する。
  printf '%s' "$content" > "$T/redirect-page.html"
  python3 - "$T/redirect-page.html" <<'PYEOF' > "$T/redirect-target.txt"
import sys, re
text = open(sys.argv[1], encoding="utf-8").read()
m = re.search(r'content="0; url=([^"]+)"', text)
print(m.group(1) if m else "")
PYEOF
  actual=$(cat "$T/redirect-target.txt")
  expected="https://hlabsworks.com${new}"
  old_url="https://hlabsworks.com/${old}"
  if [ "$actual" = "$expected" ]; then ok; else fail "旧URL $old のリダイレクト先が新URL $new と完全一致しない（実際: ${actual:-なし}）"; fi
  if [ "$actual" != "$old_url" ]; then ok; else fail "旧URL $old のリダイレクト先が自分自身になっている（自己ループ）"; fi
  ALIAS_PATHS="${ALIAS_PATHS}/${old}
"
done <<< "$OLD_NEW_MAP"

echo "# (c) 内部リンク検査: 全ページの <a href> が出力に実在する場所を指しているか"
# HTML解析はbashの正規表現より堅牢な標準ライブラリ(re)に任せる。
# alias（旧URLの http-equiv=refresh リダイレクトページ）自体は <a> を持たないので検査対象から除外する。
# Pythonが例外で異常終了しても、このシェルはヒアドキュメント経由の値を素通りさせて
# しまい、SUMMARY行が出ないまま「検査なしで合格」する恐れがある。exit codeを確認し、
# SUMMARY行を実際に見たかどうかのフラグを立てて、見ていなければ明示的にfailさせる。
ALIAS_PATHS="$ALIAS_PATHS" python3 - "$DEST" <<'PYEOF' > "$T/linkcheck.out" 2>"$T/linkcheck.err"
import os
import re
import sys
import pathlib
import urllib.parse

dest = pathlib.Path(sys.argv[1])
domain = "https://hlabsworks.com"
alias_paths = set(p for p in os.environ.get("ALIAS_PATHS", "").splitlines() if p)
# 引用符つきhrefしか拾えないと、minify後(属性値に空白が無ければ引用符が省略される)は
# 検査対象が0件になり、リンク切れがあっても素通りしてしまう。引用符あり/なし両方を拾う。
a_href_re = re.compile(
    r'<a\b[^>]*?\bhref=(?:"([^"]*)"|\'([^\']*)\'|([^\s>]+))', re.IGNORECASE | re.DOTALL
)
refresh_re = re.compile(r'http-equiv="?refresh"?', re.IGNORECASE)

checked = 0
broken = []
for page in sorted(dest.rglob("index.html")):
    text = page.read_text(encoding="utf-8", errors="replace")
    if refresh_re.search(text):
        continue  # 旧URLのリダイレクトページ
    for m in a_href_re.finditer(text):
        href = m.group(1) if m.group(1) is not None else (m.group(2) if m.group(2) is not None else m.group(3))
        if href.startswith(domain + "/"):
            target = href[len(domain):]
        elif href.startswith(("http://", "https://", "mailto:", "tel:", "#")):
            continue
        elif href.startswith("/"):
            target = href
        else:
            continue  # 相対パス等、内部絶対リンクではないものは対象外
        target = target.split("#", 1)[0].split("?", 1)[0]
        if target == "":
            target = "/"
        target = urllib.parse.unquote(target)  # タグ名(日本語)がURLエンコードされているため、実ディレクトリ名に戻す
        checked += 1
        if target in alias_paths:
            # alias先(旧URL)自体は実在するが、本文からのリンク先としては不正
            # （新URLへ直接リンクすべき）。
            broken.append(f"{page.relative_to(dest)} が {href} (-> 旧URL {target}、リダイレクトスタブ) を指している。新URLへ直接リンクすべき")
            continue
        if target.endswith("/"):
            dest_file = dest / target.lstrip("/") / "index.html"
        else:
            dest_file = dest / target.lstrip("/")
        if not dest_file.is_file():
            broken.append(f"{page.relative_to(dest)} が {href} (-> {dest_file}) を指しているが実在しない")

for b in broken:
    print(f"FAIL:{b}")
print(f"SUMMARY:{checked}:{len(broken)}")
PYEOF
LINKCHECK_RC=$?
if [ "$LINKCHECK_RC" = 0 ]; then ok; else fail "内部リンク検査のPythonが異常終了した(rc=$LINKCHECK_RC): $(cat "$T/linkcheck.err")"; fi
SAW_SUMMARY=0
while IFS= read -r line; do
  case "$line" in
    FAIL:*) fail "内部リンク切れ: ${line#FAIL:}" ;;
    SUMMARY:*)
      SAW_SUMMARY=1
      rest="${line#SUMMARY:}"
      checked_n="${rest%%:*}"; broken_n="${rest#*:}"
      echo "  内部リンク検査対象: ${checked_n} 件、切れていたリンク: ${broken_n} 件"
      # href抽出が想定通り機能していないと checked_n が0近くまで落ち、
      # broken_nも0のまま「合格」してしまう。下限を設けて検査漏れそのものを検出する。
      if [ "$checked_n" -lt "$MIN_CHECKED" ]; then
        fail "内部リンク検査対象が${MIN_CHECKED}件未満(${checked_n}件)。<a href>の抽出漏れの疑い"
      elif [ "$broken_n" = "0" ]; then
        ok
      fi
      ;;
  esac
done < "$T/linkcheck.out"
# SUMMARY行を一度も見ていなければ、Pythonが例外で終了した等の理由で検査自体が
# 行われなかった疑いがある。rc=0でもSUMMARYが無ければ検査漏れとしてfailさせる。
if [ "$SAW_SUMMARY" = 1 ]; then ok; else fail "内部リンク検査のSUMMARY行が出力されなかった（検査が実行されなかった疑い）"; fi

echo "# (d) テーマ別パス化: 2つ目のテーマを注入しても一覧が正しく分離されること"
# フィクスチャで2つ目のテーマ(fixture-theme)をLabs直下に注入する。
mkdir -p "$SITE/content/labs/fixture-theme/2026"
cat > "$SITE/content/labs/fixture-theme/_index.md" <<'EOF'
---
title: "フィクスチャテーマ"
description: "R1'回帰テスト用の2つ目のテーマ(フィクスチャ)"
summary: "R1'回帰テスト用の2つ目のテーマ(フィクスチャ)"
---
フィクスチャテーマの概要文。
EOF
# 最新記事一覧: 事業トップの最新記事一覧(home-latest-posts)は新しい順に3件を拾うため、フィクスチャの
# 日付が既存記事より古いと最新3件に入らず、「両テーマの記事が出ること」の検査が機能しない。
# --buildFutureでこのブロックはビルドしているため、既存記事より確実に新しい未来日付にする。
cat > "$SITE/content/labs/fixture-theme/2026/fixture-article.md" <<'EOF'
---
title: "フィクスチャテーマの記事"
date: 2099-01-01T00:00:00+09:00
tags: ["フィクスチャ"]
---
フィクスチャテーマの記事本文。
EOF
DEST_MULTI="$T/public-multi-theme"
hugo --quiet -e production --config "hugo.toml,$OVERRIDE" --buildFuture --destination "$DEST_MULTI" >/dev/null 2>"$T/hugo-multi.log"
RC=$?
if [ "$RC" = 0 ]; then ok; else fail "(d) 2テーマ構成のhugo buildが失敗: $(cat "$T/hugo-multi.log")"; fi

LABS_INDEX=$(cat "$DEST_MULTI/labs/index.html" 2>/dev/null || true)
if printf '%s' "$LABS_INDEX" | /usr/bin/grep -qF 'href="/labs/solar/"'; then ok; else fail "(d) Labsトップに太陽光テーマへのリンクが無い"; fi
if printf '%s' "$LABS_INDEX" | /usr/bin/grep -qF 'href="/labs/fixture-theme/"'; then ok; else fail "(d) Labsトップにフィクスチャテーマへのリンクが無い（2テーマ目がテーマ一覧に出ていない）"; fi

SOLAR_INDEX=$(cat "$DEST_MULTI/labs/solar/index.html" 2>/dev/null || true)
if printf '%s' "$SOLAR_INDEX" | /usr/bin/grep -qF "フィクスチャテーマの記事"; then
  fail "(d) 太陽光テーマの記事一覧に他テーマ(フィクスチャテーマ)の記事が混ざっている"
else
  ok
fi
FIXTURE_THEME_INDEX=$(cat "$DEST_MULTI/labs/fixture-theme/index.html" 2>/dev/null || true)
if printf '%s' "$FIXTURE_THEME_INDEX" | /usr/bin/grep -qF "フィクスチャテーマの記事"; then ok; else fail "(d) フィクスチャテーマの記事一覧に自テーマの記事が出ていない"; fi
if printf '%s' "$FIXTURE_THEME_INDEX" | /usr/bin/grep -qE 'href="?/labs/solar/2026/[a-z-]+/"?'; then
  fail "(d) フィクスチャテーマの記事一覧に太陽光テーマの記事が混ざっている"
else
  ok
fi

echo "# 最新記事一覧: 事業トップの最新記事一覧に両テーマの記事が出ること"
INDEX_MULTI=$(cat "$DEST_MULTI/index.html" 2>/dev/null || true)
if printf '%s' "$INDEX_MULTI" | /usr/bin/grep -qF "/labs/fixture-theme/2026/fixture-article/"; then ok; else fail "最新記事一覧: 事業トップの最新記事一覧にフィクスチャテーマの記事が無い(他テーマの記事が混ざっていない)"; fi
if printf '%s' "$INDEX_MULTI" | /usr/bin/grep -qF "/labs/solar/2026/"; then ok; else fail "最新記事一覧: 事業トップの最新記事一覧に太陽光テーマの記事が無い"; fi

echo "# (d) 事業トップ(index.html)の表示内容"
INDEX_HTML=$(cat "$DEST/index.html" 2>/dev/null || true)
if printf '%s' "$INDEX_HTML" | /usr/bin/grep -qF "H Labs Works"; then ok; else fail "index.html に「H Labs Works」が無い"; fi
if printf '%s' "$INDEX_HTML" | /usr/bin/grep -qF "エイチラボワークス"; then ok; else fail "index.html に「エイチラボワークス」が無い"; fi
if printf '%s' "$INDEX_HTML" | /usr/bin/grep -qF "スマートフォン・タブレット・パソコン向けアプリケーションの開発・運営"; then ok; else fail "index.html に事業内容(アプリ開発・運営)の行が無い"; fi
if printf '%s' "$INDEX_HTML" | /usr/bin/grep -qF "3Dプリンター等を利用したオリジナル雑貨・小物の企画・製造・販売"; then ok; else fail "index.html に事業内容(3Dプリンター雑貨)の行が無い"; fi
# テーマが増えると成り立たなくなる固定文言(「現在のテーマ（太陽光）の実績は…」)を削除し、
# 事業トップからのリンクはテーマ一覧(/labs/<テーマ>/)への導線だけにした。個別テーマの
# ダッシュボードへのリンクは各テーマトップ自身(例: /labs/solar/)に掲載する。
for href in '/about/' '/apps/' '/labs/' '/labs/solar/' '/contact/' '/privacy-policy/'; do
  if printf '%s' "$INDEX_HTML" | /usr/bin/grep -qE "href=\"?${href//\//\\/}\"?"; then
    ok
  else
    fail "index.html に ${href} へのリンクが無い"
  fi
done
# 事業トップはPaperMod既定の記事一覧(post-entry)を使い回さず独自マークアップにしている
# （最新3件の見せ方をブログ記事一覧と区別するため）。`class="post-entry"`（引用符つき）
# しか探していないと、--minify後の `class=post-entry`（値に空白が無いため引用符が省略される）
# を検出できず、常に合格してしまう。class属性値の単語境界でpost-entryを探す。
if printf '%s' "$INDEX_HTML" | /usr/bin/grep -qE 'class="?[^">]*\bpost-entry\b'; then fail "index.html にPaperMod既定の post-entry クラスの記事一覧が漏れている"; else ok; fi
# レイアウト差し替え(layouts/index.html)でも<head>(GA4等)が欠落していないことを確認する。
if printf '%s' "$INDEX_HTML" | /usr/bin/grep -qF "gtag("; then ok; else fail "index.html にGA4タグ(gtag()が無い（レイアウト差し替えでheadが欠落した疑い）"; fi

echo "# home RSS: /index.xmlに事業ページが無く、Labsの記事が入ること"
HOME_RSS=$(cat "$DEST/index.xml" 2>/dev/null || true)
if printf '%s' "$HOME_RSS" | /usr/bin/grep -qF "<link>https://hlabsworks.com/labs/solar/2026/hello/</link>"; then ok; else fail "home RSS: /index.xml にLabsの記事(hello)が無い"; fi
for bad_link in "https://hlabsworks.com/about/" "https://hlabsworks.com/contact/" "https://hlabsworks.com/privacy-policy/" "https://hlabsworks.com/labs/about/"; do
  if printf '%s' "$HOME_RSS" | /usr/bin/grep -qF "<link>${bad_link}</link>"; then
    fail "home RSS: /index.xml に事業ページ(${bad_link})が混入している"
  else
    ok
  fi
done

echo "# (e) 事業サイトのページが揃っていること"
for rel in about/index.html contact/index.html privacy-policy/index.html apps/index.html labs/about/index.html; do
  if [ -s "$DEST/$rel" ]; then ok; else fail "必須ページが無い、または空: $rel"; fi
done
if [ -s "$DEST/privacy-policy/index.html" ] && /usr/bin/grep -qF "H Labs Works" "$DEST/privacy-policy/index.html"; then
  ok
else
  fail "privacy-policy/index.html に「H Labs Works」が無い"
fi

echo "# 旧URLリダイレクト: 旧URL /page/1/ がcontent/_index.mdのaliasで事業トップへリダイレクトされること"
PAGE1_HTML=$(cat "$DEST/page/1/index.html" 2>/dev/null || true)
if printf '%s' "$PAGE1_HTML" | /usr/bin/grep -qE 'http-equiv="?refresh"?'; then ok; else fail "旧URLリダイレクト: page/1/index.html に http-equiv=refresh が無い"; fi
if printf '%s' "$PAGE1_HTML" | /usr/bin/grep -qF 'content="0; url=https://hlabsworks.com/"'; then ok; else fail "旧URLリダイレクト: page/1/ のリダイレクト先が事業トップ(/)でない"; fi

echo "# メニュー: メニューが4項目であること、全ページのフッターに /privacy-policy/ へのリンクがあること"
MENU_ITEM_COUNT=$(python3 -c "
import re
html = open('$DEST/index.html', encoding='utf-8').read()
# --minify後は属性値の引用符が省略される(id=menu)ため両対応する。
m = re.search(r'<ul id=\"?menu\"?[^>]*>(.*?)</ul>', html, re.S)
body = m.group(1) if m else ''
print(len(re.findall(r'<li>', body)))
")
if [ "$MENU_ITEM_COUNT" = "4" ]; then ok; else fail "メニュー: メニューが4項目ではない(実際: ${MENU_ITEM_COUNT}項目)"; fi

echo "# フッター: 全ページの<footer class=footer>の中にプライバシーポリシー・事業者情報へのリンクがあること"
# params.footer.text(hugo.toml)経由でPaperMod本来の<footer>の中に描画されることを確認する。
# 対象を固定8ページではなく、alias(http-equiv=refreshのリダイレクトページ)を除く全index.htmlと
# 404.htmlにする。minify後は属性値の引用符が省略される(class=footer)ため両対応する。
FOOTER_CHECK_OUT=$(python3 -c "
import re
import pathlib

dest = pathlib.Path('$DEST')
refresh_re = re.compile(r'http-equiv=\"?refresh\"?', re.IGNORECASE)
footer_re = re.compile(r'<footer\b[^>]*\bclass=\"?footer\"?[^>]*>(.*?)</footer>', re.IGNORECASE | re.DOTALL)
checked = 0
bad = []
pages = sorted(dest.rglob('index.html'))
p404 = dest / '404.html'
if p404.is_file():
    pages.append(p404)
for page in pages:
    text = page.read_text(encoding='utf-8', errors='replace')
    if refresh_re.search(text):
        continue
    checked += 1
    m = footer_re.search(text)
    if not m:
        bad.append(f'{page.relative_to(dest)}: <footer class=footer>が見つからない')
        continue
    body = m.group(1)
    if not re.search(r'href=\"?/privacy-policy/\"?', body):
        bad.append(f'{page.relative_to(dest)}: footer内に/privacy-policy/へのリンクが無い')
    if not re.search(r'href=\"?/about/\"?', body):
        bad.append(f'{page.relative_to(dest)}: footer内に/about/へのリンクが無い')
for b in bad:
    print(f'FAIL:{b}')
print(f'SUMMARY:{checked}:{len(bad)}')
")
SAW_FOOTER_SUMMARY=0
while IFS= read -r line; do
  case "$line" in
    FAIL:*) fail "フッター: ${line#FAIL:}" ;;
    SUMMARY:*)
      SAW_FOOTER_SUMMARY=1
      rest="${line#SUMMARY:}"
      footer_checked_n="${rest%%:*}"; footer_bad_n="${rest#*:}"
      echo "  footer検査対象: ${footer_checked_n} 件、不備: ${footer_bad_n} 件"
      if [ "$footer_checked_n" -lt 10 ]; then
        fail "フッター: footer検査対象が10件未満(${footer_checked_n}件)。検査漏れの疑い"
      elif [ "$footer_bad_n" = "0" ]; then
        ok
      fi
      ;;
  esac
done <<< "$FOOTER_CHECK_OUT"
if [ "$SAW_FOOTER_SUMMARY" = 1 ]; then ok; else fail "フッター: footer検査のSUMMARY行が出力されなかった"; fi

echo "# 事業者情報の表: 事業者情報の表(dl.business-info)のレイアウト崩れ対策CSSが出力に含まれること"
# PaperModの .md-content dl が display:flex のため、dl.business-info をblock化して打ち消す
# 上書きルール(assets/css/extended/business.css)が、連結・minify後のCSSにも残っていることを
# 確認する。見た目そのものはシェルテストでは検証できないため、
# 依頼元がブラウザで最終確認する（本タスクの報告に確認先URLとチェックポイントを記載）。
CSS_TEXT=$(find "$DEST" -name '*.css' -exec cat {} + 2>/dev/null || true)
CSS_OK=$(printf '%s' "$CSS_TEXT" | python3 -c "
import re, sys
css = sys.stdin.read()
print('1' if re.search(r'dl\.business-info[^{}]*\{[^{}]*display\s*:\s*block', css) else '0')
")
if [ "$CSS_OK" = "1" ]; then ok; else fail "事業者情報の表: 出力CSSに dl.business-info の display:block 上書きルールが見つからない"; fi

echo "# スマホ幅レイアウト: スマホ幅(480px以下)のメディアクエリ内にdtの高さ崩れ対策が含まれること"
# メディアクエリ内の `.business-info-row dt { flex-basis: auto }`
# (詳細度0,1,1)が、本文内で効く `.md-content .business-info-row dt { flex: 0 0 6em }`
# (詳細度0,2,1)に負けて縦積み時のdtの高さが6em(108px)になっていた。メディアクエリ内にも
# 同じ詳細度のセレクタがあることを確認する。
M1_CSS_OK=$(printf '%s' "$CSS_TEXT" | python3 -c "
import re, sys
css = sys.stdin.read()
m = re.search(r'@media[^{]*max-width:\s*480px[^{]*\{(.*)', css, re.S)
if not m:
    print('0')
else:
    # メディアクエリの中身をブレースの対応を数えて取り出す（ネストしたルールを含むため単純な
    # 最初の}までの切り出しでは足りない）。
    body = m.group(1)
    depth = 1
    end = 0
    for i, ch in enumerate(body):
        if ch == '{':
            depth += 1
        elif ch == '}':
            depth -= 1
            if depth == 0:
                end = i
                break
    inner = body[:end]
    print('1' if re.search(r'\.md-content\s+\.business-info-row\s+dt', inner) else '0')
")
if [ "$M1_CSS_OK" = "1" ]; then ok; else fail "スマホ幅レイアウト: @media (max-width:480px) 内に .md-content .business-info-row dt を含むセレクタが見つからない"; fi

echo "# 読了時間: 事業ページ(about/contact/privacy-policy/apps)に読了時間が表示されないこと"
# 'min'（英語表記）しか探していないと、言語設定後の実際の出力表記「1 分」を見逃す。
# 分/minどちらでも検出する。
for rel in about/index.html contact/index.html privacy-policy/index.html apps/index.html; do
  html=$(cat "$DEST/$rel" 2>/dev/null || true)
  if printf '%s' "$html" | /usr/bin/grep -qE '>[0-9]+[[:space:]]*(分|min)<'; then
    fail "読了時間: $rel に読了時間が表示されている(ShowReadingTime: falseのはず)"
  else
    ok
  fi
done

echo "# 個人情報の混入防止（対象はビルド出力全体）"
# テストコードには実際の番地・電話番号・氏名を書かない。hugo.tomlの設定値との一致・
# owner/phone行の不在だけを機械的に確認する。about本文の<dt>行だけでなく、
# ビルド出力(HTML/XML/JSON/txt)全体を走査し、index.html等への混入も検出できるようにする。
# 型注釈 `-> str | None` はPython 3.9(macOS標準/usr/bin/python3)ではTypeErrorになり、
# この検査全体が実行されずにスキップされる(のにシェル側はexit codeもSUMMARY行の有無も
# 見ていなかったため「検査なしで合格」していた)。`from __future__ import annotations` で
# 型注釈を実行時評価しないようにし(Python 3.9でも動作する)、シェル側は
# exit codeとSUMMARY行を見たかどうかのフラグの両方で検査漏れを検出する。
# ヒアドキュメントはコマンド置換の外で単独の文として実行し、ファイル経由で読む。
python3 - "$DEST" "$SITE/hugo.toml" <<'PYEOF' > "$T/privacycheck.out" 2>"$T/privacycheck.err"
from __future__ import annotations

import re
import sys
import pathlib

dest = pathlib.Path(sys.argv[1])
toml_text = pathlib.Path(sys.argv[2]).read_text(encoding="utf-8")

problems = []


def toml_value(key: str) -> str | None:
    m = re.search(rf'^\s*{re.escape(key)}\s*=\s*"([^"]*)"', toml_text, re.MULTILINE)
    return m.group(1) if m else None


expected_address = toml_value("address")
if expected_address is None:
    problems.append("hugo.toml に params.business.address が見つからない")

for key, label in (("owner", "owner"), ("phone", "phone")):
    v = toml_value(key)
    if v is None:
        problems.append(f"hugo.toml に params.business.{key} が見つからない")
    elif v != "":
        problems.append(f"hugo.toml の params.business.{key} が非空(運営者判断により空欄運用のはず): {v!r}")

about_path = dest / "about/index.html"
if not about_path.is_file():
    problems.append("about/index.html が無いため個人情報チェックを実施できない")
else:
    about_html = about_path.read_text(encoding="utf-8", errors="replace")
    m = re.search(r"<dt>所在地</dt>\s*<dd>\s*([^<]*?)\s*</dd>", about_html)
    if not m:
        problems.append("about/index.html に所在地の行が見つからない")
    elif expected_address is not None:
        shown = m.group(1).strip()
        if shown != expected_address:
            problems.append(f"所在地の表示値がhugo.tomlのaddressと一致しない(表示:{shown!r})")
        if re.search(r"[0-9０-９]", shown):
            problems.append(f"所在地の表示値に数字が含まれている(番地の混入疑い): {shown!r}")

# about本文の<dt>行だけでなく、ビルド出力全体(HTML/XML/JSON/txt/js)を走査する。index.htmlに
# 代表者・電話番号を挿入しても、about本文に番地を追記しても、自前のJS(metrics-dashboard.js等)に
# 電話番号を書いても検出できるようにする。
# 電話番号らしき文字列の判定は、検査本体とその回帰テスト(電話番号パターンフィクスチャ検査)の
# 両方から同じ定義を使うため scripts/tests/phone_patterns.py に切り出してある。
sys.path.insert(0, "scripts/tests")
from phone_patterns import is_phone_like  # noqa: E402

TEL_LINK_RE = re.compile(r'href=["\']?tel:', re.IGNORECASE)
scan_exts = {".html", ".xml", ".json", ".txt", ".js"}
# vendor配下(js/vendor/、assets/js/vendor/chart.umd.min.jsのビルド出力)はMaven webjarからの
# 未編集の外部ライブラリで、数字の密度が高く電話番号パターンに偶然一致しうるため対象外にする。
scan_exclude_dirnames = {"vendor"}
address_follow_re = re.compile(re.escape(expected_address) + r'[^<"\s]') if expected_address else None

for path in sorted(dest.rglob("*")):
    if not path.is_file() or path.suffix.lower() not in scan_exts:
        continue
    if scan_exclude_dirnames & set(path.relative_to(dest).parts[:-1]):
        continue
    text = path.read_text(encoding="utf-8", errors="replace")
    rel = path.relative_to(dest)
    if is_phone_like(text):
        problems.append(f"{rel} に電話番号らしき文字列が含まれている(phoneは非掲載のはず)")
    if TEL_LINK_RE.search(text):
        problems.append(f"{rel} に tel: リンクが含まれている(phoneは非掲載のはず)")
    if "代表者" in text:
        problems.append(f"{rel} に「代表者」の文字列が含まれている(ownerは非掲載のはず)")
    if "<dt>電話</dt>" in text:
        problems.append(f"{rel} に <dt>電話</dt> の行が出力されている(phoneは非掲載のはず)")
    if address_follow_re and address_follow_re.search(text):
        problems.append(f"{rel} の所在地の直後に文字が続いている(番地等の混入疑い)")

for p in problems:
    print(f"FAIL:{p}")
print(f"SUMMARY:{len(problems)}")
PYEOF
PRIVACY_CHECK_RC=$?
if [ "$PRIVACY_CHECK_RC" = 0 ]; then ok; else fail "個人情報検査のPythonが異常終了した(rc=$PRIVACY_CHECK_RC): $(cat "$T/privacycheck.err")"; fi
SAW_PRIVACY_SUMMARY=0
while IFS= read -r line; do
  case "$line" in
    FAIL:*) fail "${line#FAIL:}" ;;
    SUMMARY:*)
      SAW_PRIVACY_SUMMARY=1
      n="${line#SUMMARY:}"
      if [ "$n" = "0" ]; then ok; fi
      ;;
  esac
done < "$T/privacycheck.out"
if [ "$SAW_PRIVACY_SUMMARY" = 1 ]; then ok; else fail "個人情報検査のSUMMARY行が出力されなかった（検査が実行されなかった疑い）"; fi

echo "# 電話番号パターン: 電話番号パターンの見逃し例・誤検出例（ダミー値のみ、フィクスチャで検証）"
# 見逃し例(ハイフンなし/空白区切り/+81表記/国番号の後の(0)/市外局番を括弧で囲む/ドット区切り/
# 全角数字)を検出し、誤検出例(日付+連番、data-属性風の文字列、日付(月-日-年)、フリーダイヤル)
# を誤検出しないことを、個人情報検査本体と同じ scripts/tests/phone_patterns.py の
# is_phone_like() を直接呼び出して確認する（ダミー値のみ使用、実在の電話番号は書かない。
# 検査ロジックをモジュールに切り出し、検査本体・回帰テストの両方がロジックの複製ではなく
# 同じ実装を呼ぶようにした）。
python3 - <<'PYEOF' > "$T/phoneregex.out" 2>"$T/phoneregex.err"
import sys

sys.path.insert(0, "scripts/tests")
from phone_patterns import is_phone_like

matches = is_phone_like

# ダミーの電話番号らしき文字列（見逃してはいけない）
must_detect = [
    "電話: 09900000000",            # ハイフンなし
    "電話: 099 000 0000",           # 空白区切り
    "電話: +81-99-000-0000",        # 国際表記
    "電話: 03-0000-0000",           # 標準的なハイフン区切り
    "電話: +81 99 000 0000",        # 国番号の後が空白区切り
    "電話: +81(0)99-000-0000",      # 国番号の後に(0)
    "電話: (03) 1234-5678",         # 市外局番を括弧で囲む
    "電話: 099.000.0000",           # ドット区切り
    "電話: ０９９－０００－００００",  # 全角数字・全角ハイフン
    "電話: (099)000-0000",          # 市外局番の括弧の直後に区切り無し
    "電話: 099ー000ー0000",          # 長音符区切り
    "電話: 099−000−0000",  # U+2212(数学的マイナス記号)区切り
    "電話: 099–000–0000",  # enダッシュ区切り
]
# 誤検出してはいけないダミー文字列
must_not_detect = [
    "記事ID 20260927-001-0001",   # 日付+連番
    'data-2026-09-001をご覧ください',  # data-属性風
    "05-10-2026 に更新",           # 日付(月-日-年)
    "0120-000-000 のフリーダイヤル",  # フリーダイヤル
    "静岡県沼津市の日照時間は長い",  # 通常の文(数字を含まない)
    "ポータブル電源4台、カタログ値14.3kWhのうち実際に使えるのは何kWhなのか",  # 数量の「のうち」
    "12.0123456789",               # 小数部(桁数の多い小数)
    "0.00 0.00 0.000",             # 小数の羅列(区切りが電話番号のパターンに偶然一致する)
]

problems = []
for s in must_detect:
    if not matches(s):
        problems.append(f"見逃し: {s!r} を検出できなかった")
for s in must_not_detect:
    if matches(s):
        problems.append(f"誤検出: {s!r} を電話番号として誤検出した")

for p in problems:
    print(f"FAIL:{p}")
print(f"SUMMARY:{len(problems)}")
PYEOF
PHONE_REGEX_RC=$?
if [ "$PHONE_REGEX_RC" = 0 ]; then ok; else fail "電話番号パターン: フィクスチャ検査のPythonが異常終了した(rc=$PHONE_REGEX_RC): $(cat "$T/phoneregex.err")"; fi
SAW_PHONE_SUMMARY=0
while IFS= read -r line; do
  case "$line" in
    FAIL:*) fail "${line#FAIL:}" ;;
    SUMMARY:*)
      SAW_PHONE_SUMMARY=1
      n="${line#SUMMARY:}"
      if [ "$n" = "0" ]; then ok; fi
      ;;
  esac
done < "$T/phoneregex.out"
if [ "$SAW_PHONE_SUMMARY" = 1 ]; then ok; else fail "電話番号パターン: フィクスチャ検査のSUMMARY行が出力されなかった"; fi

echo "# (f) 所在地未設定/設定時の表示切り替え（home_address_fallback、事業トップも対象）"
ADDR_BLANK_OVERRIDE="$T/addr-blank.toml"
cat > "$ADDR_BLANK_OVERRIDE" <<'EOF'
[params.business]
  address = ""
EOF
DEST_ADDR_BLANK="$T/public-addr-blank"
hugo --quiet -e production --config "hugo.toml,$OVERRIDE,$ADDR_BLANK_OVERRIDE" --destination "$DEST_ADDR_BLANK" >/dev/null 2>"$T/hugo-addr-blank.log"
RC=$?
if [ "$RC" = 0 ]; then ok; else fail "(f) address=空 のhugo buildが失敗: $(cat "$T/hugo-addr-blank.log")"; fi
ABOUT_BLANK=$(cat "$DEST_ADDR_BLANK/about/index.html" 2>/dev/null || true)
if printf '%s' "$ABOUT_BLANK" | /usr/bin/grep -qF "ご請求いただければ遅滞なく開示いたします"; then ok; else fail "(f) address=空 のとき開示文が出ない"; fi
# 所在地フォールバックのロジックは business_address.html に共通化してあるため、
# layouts/index.html(事業トップ)側もaboutと同様に検査する。
INDEX_BLANK=$(cat "$DEST_ADDR_BLANK/index.html" 2>/dev/null || true)
if printf '%s' "$INDEX_BLANK" | /usr/bin/grep -qF "ご請求いただければ遅滞なく開示いたします"; then ok; else fail "(f) address=空 のとき事業トップ(index.html)に開示文が出ない"; fi

ADDR_SET_OVERRIDE="$T/addr-set.toml"
cat > "$ADDR_SET_OVERRIDE" <<'EOF'
[params.business]
  address = "テスト県テスト市"
EOF
DEST_ADDR_SET="$T/public-addr-set"
hugo --quiet -e production --config "hugo.toml,$OVERRIDE,$ADDR_SET_OVERRIDE" --destination "$DEST_ADDR_SET" >/dev/null 2>"$T/hugo-addr-set.log"
RC=$?
if [ "$RC" = 0 ]; then ok; else fail "(f) address=テスト県テスト市 のhugo buildが失敗: $(cat "$T/hugo-addr-set.log")"; fi
ABOUT_SET=$(cat "$DEST_ADDR_SET/about/index.html" 2>/dev/null || true)
if printf '%s' "$ABOUT_SET" | /usr/bin/grep -qF "テスト県テスト市"; then ok; else fail "(f) address=テスト県テスト市 のとき設定値が出ない"; fi
if printf '%s' "$ABOUT_SET" | /usr/bin/grep -qF "ご請求いただければ遅滞なく開示いたします"; then fail "(f) address設定済みなのに開示文が出ている"; else ok; fi
INDEX_SET=$(cat "$DEST_ADDR_SET/index.html" 2>/dev/null || true)
if printf '%s' "$INDEX_SET" | /usr/bin/grep -qF "テスト県テスト市"; then ok; else fail "(f) address=テスト県テスト市 のとき事業トップ(index.html)に設定値が出ない"; fi

echo "# 未来日付記事: no_future_posts_on_home_production（--buildFutureなしの本番ビルドで未来日付の記事が事業トップに出ない）"
# 実在の記事の日付は時間経過で「未来」でなくなるため、日付をnowから動的に計算したフィクスチャ
# 記事を追加コピー先に注入して検証する（実行時点の日付に依存しない回帰テスト）。
FUTURE_DATE=$(python3 -c "import datetime; print((datetime.date.today()+datetime.timedelta(days=730)).isoformat())")
FIXTURE_SLUG="n9-future-fixture-post"
mkdir -p "$SITE/content/labs/solar/2099-n9-fixture"
cat > "$SITE/content/labs/solar/2099-n9-fixture/${FIXTURE_SLUG}.md" <<EOF
---
title: "n9フィクスチャ: 未来日付テスト記事"
date: ${FUTURE_DATE}T00:00:00+09:00
tags: ["テスト"]
---

no_future_posts_on_home_production の回帰テスト用フィクスチャ記事。
EOF
DEST_NOFUTURE="$T/public-nofuture"
hugo --quiet -e production --config "hugo.toml,$OVERRIDE" --destination "$DEST_NOFUTURE" >/dev/null 2>"$T/hugo-nofuture.log"
RC=$?
if [ "$RC" = 0 ]; then ok; else fail "未来日付記事: --buildFutureなしのhugo buildが失敗: $(cat "$T/hugo-nofuture.log")"; fi
INDEX_NOFUTURE=$(cat "$DEST_NOFUTURE/index.html" 2>/dev/null || true)
if printf '%s' "$INDEX_NOFUTURE" | /usr/bin/grep -qF "$FIXTURE_SLUG"; then
  fail "未来日付記事: --buildFutureなしの本番ビルドなのに、未来日付の記事(${FIXTURE_SLUG})が事業トップに出ている"
else
  ok
fi
# フィクスチャ自体が正しく「未来の記事」として振る舞うこと（buildFutureありでは出る）の
# サニティチェック。falseで壊れていたら上のfail検出が無意味になるため。
DEST_WITHFUTURE="$T/public-withfuture"
hugo --quiet -e production --config "hugo.toml,$OVERRIDE" --buildFuture --destination "$DEST_WITHFUTURE" >/dev/null 2>"$T/hugo-withfuture.log"
RC=$?
if [ "$RC" = 0 ]; then ok; else fail "未来日付記事: --buildFutureありのhugo buildが失敗: $(cat "$T/hugo-withfuture.log")"; fi
INDEX_WITHFUTURE=$(cat "$DEST_WITHFUTURE/index.html" 2>/dev/null || true)
if printf '%s' "$INDEX_WITHFUTURE" | /usr/bin/grep -qF "$FIXTURE_SLUG"; then ok; else fail "未来日付記事: フィクスチャ記事が --buildFuture ありでも出ない(フィクスチャ自体が壊れている疑い)"; fi

echo "# アプリ一覧(事業トップ): /apps/<slug>/privacy/ 等の子ページが事業トップのアプリ一覧に混入しないこと"
# アプリ本体・privacyページは、docs/site-structure-runbook.md §4の規約どおり
# content/apps/ 直下のフラットな.mdファイル(url:で個別URLを指定)として追加される想定。
# アプリ本体だけに appEntry: true を付け、privacyページには付けない構成をフィクスチャで再現する。
mkdir -p "$SITE/content/apps"
cat > "$SITE/content/apps/apps-fixture-app.md" <<'EOF'
---
title: "appsフィクスチャアプリ"
appEntry: true
---
no_apps_subpages_leak_to_homeの回帰テスト用フィクスチャ。
EOF
cat > "$SITE/content/apps/apps-fixture-app-privacy.md" <<'EOF'
---
title: "appsフィクスチャアプリのプライバシーポリシー"
url: /apps/apps-fixture-app/privacy/
---
アプリ一覧の回帰テスト用フィクスチャ。
EOF
DEST_APPS_FIXTURE="$T/public-apps-fixture"
hugo --quiet -e production --config "hugo.toml,$OVERRIDE" --destination "$DEST_APPS_FIXTURE" >/dev/null 2>"$T/hugo-apps-fixture.log"
RC=$?
if [ "$RC" = 0 ]; then ok; else fail "アプリ一覧(事業トップ): フィクスチャ込みのhugo buildが失敗: $(cat "$T/hugo-apps-fixture.log")"; fi
INDEX_APPS_FIXTURE=$(cat "$DEST_APPS_FIXTURE/index.html" 2>/dev/null || true)
if printf '%s' "$INDEX_APPS_FIXTURE" | /usr/bin/grep -qF "appsフィクスチャアプリ"; then ok; else fail "アプリ一覧(事業トップ): アプリ本体(appsフィクスチャアプリ)が事業トップのアプリ一覧に出ていない"; fi
if printf '%s' "$INDEX_APPS_FIXTURE" | /usr/bin/grep -qF "appsフィクスチャアプリのプライバシーポリシー"; then
  fail "アプリ一覧(事業トップ): アプリの子ページ(プライバシーポリシー)が事業トップのアプリ一覧に混入している"
else
  ok
fi

echo "# アプリ一覧(/apps/): /apps/ 一覧ページ自体にも appEntry のページだけが並ぶこと"
# 同じフィクスチャで、事業トップ(index.html)だけでなく /apps/ 一覧ページ自体
# (layouts/apps/list.html)も検査する。
APPS_PAGE_FIXTURE=$(cat "$DEST_APPS_FIXTURE/apps/index.html" 2>/dev/null || true)
if printf '%s' "$APPS_PAGE_FIXTURE" | /usr/bin/grep -qF "appsフィクスチャアプリ"; then ok; else fail "アプリ一覧(/apps/): /apps/ 一覧にアプリ本体(appsフィクスチャアプリ)が出ていない"; fi
if printf '%s' "$APPS_PAGE_FIXTURE" | /usr/bin/grep -qF "appsフィクスチャアプリのプライバシーポリシー"; then
  fail "アプリ一覧(/apps/): /apps/ 一覧にアプリの子ページ(プライバシーポリシー)が混入している"
else
  ok
fi

echo "# 前後リンク: 記事の前後リンク(paginav)が同一テーマ内に閉じ、記事以外のページ(Labsについて等)や他テーマを含まないこと"
# PaperMod既定の前後リンクは.Type(ルートセクション名)だけで絞るため、
# テーマ別パス化後は content/labs/ 配下の全ページ(Labsについて等の非記事ページ・他テーマの記事)が
# 同じ.Typeとして混ざる。layouts/_partials/post_nav_links.html のオーバーライドで
# .CurrentSection配下かつtagsを持つページだけに絞ったことを、2テーマ構成のフィクスチャで確認する。
HELLO_MULTI=$(cat "$DEST_MULTI/labs/solar/2026/hello/index.html" 2>/dev/null || true)
if printf '%s' "$HELLO_MULTI" | /usr/bin/grep -qE 'class="?paginav"?'; then
  if printf '%s' "$HELLO_MULTI" | /usr/bin/grep -qF "/labs/about/"; then
    fail "前後リンク: 記事(hello)の前後リンクに記事以外のページ(/labs/about/)が混ざっている"
  else
    ok
  fi
  if printf '%s' "$HELLO_MULTI" | /usr/bin/grep -qF "/labs/fixture-theme/"; then
    fail "前後リンク: 記事(hello)の前後リンクに他テーマ(フィクスチャテーマ)が混ざっている"
  else
    ok
  fi
else
  fail "前後リンク: 記事(hello)にpaginavが出力されていない(前後リンク検査ができない)"
fi

echo "# RSS: RSSに記事以外のページが混入しないこと。/labs/index.xmlは全テーマの記事、/labs/solar/index.xmlはそのテーマの記事(月次レポート含む)だけが入ること"
LABS_RSS_MULTI=$(cat "$DEST_MULTI/labs/index.xml" 2>/dev/null || true)
if printf '%s' "$LABS_RSS_MULTI" | /usr/bin/grep -qF "<link>https://hlabsworks.com/labs/solar/2026/hello/</link>"; then ok; else fail "RSS: /labs/index.xml に太陽光テーマの記事が無い"; fi
if printf '%s' "$LABS_RSS_MULTI" | /usr/bin/grep -qF "<link>https://hlabsworks.com/labs/fixture-theme/2026/fixture-article/</link>"; then ok; else fail "RSS: /labs/index.xml にフィクスチャテーマの記事が無い(全テーマの記事が入っていない)"; fi
if printf '%s' "$LABS_RSS_MULTI" | /usr/bin/grep -qF "/labs/about/"; then fail "RSS: /labs/index.xml に記事以外のページ(/labs/about/)が混入している"; else ok; fi

SOLAR_RSS_MULTI=$(cat "$DEST_MULTI/labs/solar/index.xml" 2>/dev/null || true)
if printf '%s' "$SOLAR_RSS_MULTI" | /usr/bin/grep -qF "/labs/solar/solar-charge-controller/"; then
  fail "RSS: /labs/solar/index.xml に記事以外のページ(SolarChargeControllerとは)が混入している"
else
  ok
fi
if printf '%s' "$SOLAR_RSS_MULTI" | /usr/bin/grep -qF "/labs/fixture-theme/"; then
  fail "RSS: /labs/solar/index.xml に他テーマ(フィクスチャテーマ)の記事が混入している"
else
  ok
fi

echo "# 個人情報の混入防止(GitHubリンク): 出力にgithub.com/hlabsworksへのリンクが無いこと"
# 事業サイトからリポジトリのコミット履歴(過去の作成者情報)に到達できる経路を無くすため、
# content/・layouts/にgithub.com/hlabsworksへのリンクを置かない(content/contact.md参照)。
GITHUB_LINK_HITS=$(find "$DEST" \( -name '*.html' -o -name '*.xml' \) -exec /usr/bin/grep -lF "github.com/hlabsworks" {} + 2>/dev/null)
if [ -z "$GITHUB_LINK_HITS" ]; then ok; else fail "個人情報の混入防止(GitHubリンク): github.com/hlabsworksへのリンクを含むページがある: $GITHUB_LINK_HITS"; fi

echo "passed=$PASSES failed=$FAILS"
[ "$FAILS" = 0 ]
