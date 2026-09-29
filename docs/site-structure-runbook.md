# サイト再構成（Labsのテーマ別パス移設）runbook

hlabsworks.com は、ブログ関連コンテンツを「Labs」に改称のうえ `/labs/` 配下（テーマごとに
`/labs/<テーマ>/`）へ移設し、ルート（`/`）を事業サイトにする再構成を行う
（テーマ別パス化(R1')への変更を含む）。Phase 1（URL 移設、本
ドキュメントの主対象）と Phase 2（事業トップ新設）は別ブランチで実装し、Phase 2 は
Phase 1 に積んだ状態で main へマージする（2026-10-03 以降、両 Phase をまとめて公開する予定。
2 段階公開はしない）。`/blog/` は一度も本番公開していないため、`/blog/` からの alias は張らない。

## 0. 公開手順

Phase 1・Phase 2 をまとめて公開する際の手順を、実行順に1か所にまとめる。

1. **main へマージ**: 2026-10-03 以降に、Phase 2 ブランチ（Phase 1 を積んだ状態）を main へ
   マージする。未完成の予約記事2本を非公開に戻す hotfix（`fix/unpublish-draft-posts`、
   `content/labs/solar/2026/soc-4percent-deep-discharge.md`・`voltage-only-sell-detection.md`
   を `draft: true` に、`scripts/tests/no-draft-leak-test.sh` を追加）は、Phase 1 ブランチに
   先に取り込み済み。main に直接マージする必要はなく、この手順1のマージで一緒に main へ入る。
   `content/privacy-policy.md` の改定日（§4参照）は、実際の公開日が2026-10-03からずれた
   場合はこの手順の前に直す。
2. **CI の確認**: `.github/workflows/hugo.yml` のビルド・デプロイが緑で完了したことを
   GitHub Actions の実行結果で確認する。
3. **Cloudflare Single Redirects の作成・有効化**: 本ドキュメント §3 の5本
   （`redirect-posts`/`redirect-metrics`/`redirect-scc`/`redirect-tags`/`redirect-page`）を
   作成・有効化する。**§3 に記載のとおり、`curl` で新URLが `200` を返すことを確認した直後に
   行うこと**（デプロイ前に有効化すると新URLがまだ存在せず 404 になる）。
   CI が生成する月次レポート（`/labs/solar/monthly-report/<YYYY-MM>/`）は Hugo の
   `aliases` を持たないため、デプロイ完了からこの手順で `redirect-posts`
   （`/posts/monthly-report/*` → `/labs/solar/monthly-report/${1}`、旧URLは
   `/posts/monthly-report/<YYYY-MM>/`）を有効化するまでの間、旧URLでアクセスすると 404 に
   なる。本番反映は 10-03 以降で、最初の月次レポートも 10-03 以降に生成されるため通常は
   影響しないが、公開直前に本番の `/sitemap.xml` を確認し、`/posts/monthly-report/` 配下の
   URL が既にインデックスされていないかを確認すること。
4. **Pi（homelab）の再デプロイ**: `scripts/blog-metrics/layer_model.py` は
   `scripts/blog-metrics/deploy-homelab.sh:88` でバンドルされ、`aggregate.sh:198-203` が
   Pi 上で実行し、`run-daily.sh:430` が `layers.json` を solar-metrics-data へ push する。
   CI は `_incoming/data/metrics/*.json` で `data/metrics/` を上書きするため、Pi 側を
   再デプロイするまで本番の `/labs/solar/metrics/` の HTML ソース
   （`<script id=metrics-layers-data>` 内の JSON）に旧文面が残る（画面には表示されない。
   `validate_metrics.py` は文面を検査しないため、再デプロイの前後どちらでも毎朝の更新は
   止まらない）。main のマージ後の状態をチェックアウトした上で、まず
   `scripts/blog-metrics/deploy-homelab.sh --service-user <実行ユーザー名> --dry-run` を
   実行して差分を確認し、問題なければ `--dry-run` を外して再実行する
   （systemd unit は変更していないため `--install-units` は不要。`tariff.json` の値も
   変更していないため、CI の `workflow_dispatch` を `allow_history_change` 付きで
   実行し直す必要もない）。
5. **翌朝の確認**: `/labs/solar/metrics/`（実績ダッシュボード）のページソースに埋め込まれた
   JSON（`layers.json` 由来）の文面は、手順4の Pi 再デプロイが完了し、Pi が次回
   `layers.json` を push した後の翌朝の自動ビルドで初めて更新される。月次レポート
   （`/labs/solar/monthly-report/<YYYY-MM>/`）の文面は `render_monthly_posts.py`（この
   リポジトリ・main側のコード）が生成するため、Pi の再デプロイ完了を待たず、手順1の
   マージ後の次回ビルド（手動 `workflow_dispatch` でも可）で更新される。両者は更新の
   タイミングが異なることに注意し、それぞれの反映後に旧文面が残っていないか確認する。
6. **Search Console への再送信**: Google Search Console でサイトマップ（`/sitemap.xml`）を
   再送信し、新URLのインデックスを促す。

## 1. URL マップ（R1'、テーマ別パス化、Phase 2マージ後の最終形）

太陽光テーマ以外にもテーマが増える前提で、Labs 配下はテーマごとのパスにする。太陽光テーマの
パス名は `solar`。

| URL | 内容 | 備考 |
|---|---|---|
| `/` | 事業トップ（`layouts/index.html`、Phase 2） | 屋号・事業内容・Labs欄・アプリ一覧・事業者情報・お問い合わせを掲載 |
| `/about/` | 事業者情報（`content/about.md`、Phase 2） | 旧「このサイトについて」はLabs全体の方針・運営者情報部分を`/labs/about/`へ、太陽光固有の設備紹介・想定読者を`/labs/solar/`へ移した |
| `/contact/`, `/privacy-policy/` | 据え置き | |
| `/labs/` | Labs トップ（Labsの紹介＋テーマ一覧、`content/labs/_index.md`） | 新設。テーマ一覧には現在のテーマ(solar)のみ掲載し、予告・準備中のテーマは載せない |
| `/labs/about/` | Labs について（Labs全体の方針・運営者情報・免責、`content/labs/about.md`） | 旧`content/blog-about.md`から改称。太陽光固有の設備紹介・想定読者は`/labs/solar/`へ移した |
| `/labs/tags/`, `/labs/tags/<tag>/` | タグ別一覧（テーマ横断） | 旧 `/tags/<tag>/`（`hugo.toml` の `[permalinks.taxonomy]`/`[permalinks.term]`） |
| `/labs/solar/` | 太陽光テーマのトップ（概要・設備紹介・記事一覧） | 旧 `/posts/`（`content/labs/solar/_index.md`） |
| `/labs/solar/2026/<slug>/` | 記事 | 旧 `/posts/2026/<slug>/` |
| `/labs/solar/metrics/` | 実績ダッシュボード | 旧 `/metrics/`。`content/labs/solar/metrics/_index.md` に `type: metrics` を付け、`layouts/metrics/list.html` のレイアウト引き当てを保つ（`url:` オーバーライドは廃止し、ディレクトリ構成そのままの素のpermalinkにした。実ビルドで確認済み。§7参照） |
| `/labs/solar/metrics/methodology/` | このデータについて | 旧 `/metrics/methodology/`。`type: labs-page`（下記§7参照） |
| `/labs/solar/solar-charge-controller/` | SolarChargeController とは | 旧 `/solar-charge-controller/`。`type: labs-page` |
| `/labs/solar/monthly-report/<YYYY-MM>/` | 月次レポート（CI 生成、リポジトリ履歴には残らない） | 旧 `/posts/monthly-report/<YYYY-MM>/` |
| `/ads.txt`, `/robots.txt`, `/sitemap.xml` | ルートのまま | |

## 2. 旧 URL 対応表（サイト内リダイレクト、Hugo `aliases`）

以下は Hugo の `aliases` front matter によって旧 URL 側に静的なリダイレクトページ
（`<meta http-equiv="refresh">` による転送）が生成される。`scripts/tests/site-structure-test.sh`
がビルドのたびにこの対応を検査する。

| 旧URL | 新URL | 備考 |
|---|---|---|
| `/posts/` | `/labs/solar/` | Hugo alias |
| `/posts/2026/hello/` | `/labs/solar/2026/hello/` | Hugo alias |
| `/posts/2026/cloudy-september-portable-batteries/` | `/labs/solar/2026/cloudy-september-portable-batteries/` | Hugo alias |
| `/posts/2026/soc-4percent-deep-discharge/` | `/labs/solar/2026/soc-4percent-deep-discharge/` | Hugo alias（`draft: true` のため記事公開まではaliasは生成されない。§2末尾の注記参照） |
| `/posts/2026/voltage-only-sell-detection/` | `/labs/solar/2026/voltage-only-sell-detection/` | Hugo alias（`draft: true` のため記事公開まではaliasは生成されない。§2末尾の注記参照） |
| `/posts/2026/portable-battery-usable-capacity/` | `/labs/solar/2026/portable-battery-usable-capacity/` | Hugo alias |
| `/metrics/` | `/labs/solar/metrics/` | Hugo alias |
| `/metrics/methodology/` | `/labs/solar/metrics/methodology/` | Hugo alias |
| `/solar-charge-controller/` | `/labs/solar/solar-charge-controller/` | Hugo alias |
| `/posts/index.xml` | `/labs/solar/index.xml` | Cloudflareのみ（Hugo aliasなし。RSSフィード） |
| `/metrics/index.xml` | `/labs/solar/metrics/index.xml` | Cloudflareのみ（Hugo aliasなし。RSSフィード） |
| `/posts/page/1/` | `/labs/solar/page/1/` | Cloudflareのみ（Hugo aliasなし。ページネーション） |
| `/tags/`（タグ一覧） | `/labs/tags/` | Cloudflareのみ（Hugo aliasなし） |
| `/page/1/`（旧ホームのページネーション1ページ目） | `/`（事業トップ） | Hugo alias（`content/_index.md` の `aliases`）。事業トップ化で `/page/1/` が出力されなくなったための救済（m-1） |

（上記のうち「Cloudflareのみ」の4行は Hugo の `aliases` を張って
いない（記事本体ではなく一覧・フィード・ページネーションのURLのため）。次節 §3 の
`redirect-posts`/`redirect-metrics`/`redirect-tags` ルールが `wildcard` で丸ごと拾うため、
個別のルール追加は不要。`/tags/<tag>/`（個別タグ）も同様に個別の `aliases` は張っていない
（タグは記事の増減で増え続けるため）。`/about/` は Phase 1 では移動しないため、リダイレクト
は張らない）。

`soc-4percent-deep-discharge`・`voltage-only-sell-detection` の2記事は本ドキュメント作成
時点で `draft: true`（未完成のため非公開）であり、Hugo の `aliases` は draft のページでは
生成されない。上表の該当行は記事公開（`draft: false` への変更）時に初めて有効になる。
現在はこれらの記事の旧URL（`/posts/2026/soc-4percent-deep-discharge/` 等）も一度も本番公開
されていないため、公開前の時点でリダイレクトが無いことによる実害は無い。

## 3. Cloudflare Single Redirects（外部からの旧URLアクセス救済）

Hugo の `aliases` はサイト内で静的ページとして生成されるため、クローラ・旧リンク経由の
アクセスはそのページ経由で 200 応答 + JS を使わないメタリフレッシュで転送される。ただし
恒久的な移転であることを検索エンジンに正しく伝えるため、Cloudflare の **Single Redirects**
（ダッシュボード → 該当ゾーン → Rules → Redirect Rules）で 301 を追加する。

**作成・有効化のタイミング**: 以下のルールは、Phase 1 の本番反映を
`curl -s https://hlabsworks.com/labs/solar/ -o /dev/null -w '%{http_code}'` が `200` を
返すことで確認した**直後**に作成・有効化する。デプロイ前に作成すると、新URL
（`/labs/solar/...`）がまだ存在しない状態で旧URLからの転送先が 404 になり、全記事が
閲覧できなくなる。

設定手順（5本）:

1. Cloudflare ダッシュボード → 対象ゾーン（hlabsworks.com）→ **Rules → Redirect Rules** →
   **Create rule**
2. マッチ条件・転送先は、Cloudflare の **Wildcard**（ワイルドカード）ルールを主案とする
   （`regex_replace()` を使う Dynamic 式は Cloudflare の Business
   プラン以上限定の可能性があるため主案から外す。`wildcard` によるマッチと `${1}` の
   キャプチャ置換自体は Free プランでも利用できる。
   参考: https://developers.cloudflare.com/rules/url-forwarding/single-redirects/
   （未取得・設定時に要確認。本ドキュメント作成時点でこのURL・UI手順・
   プラン制限を実機確認していない〔ASSUMED。学習知識ベース〕ため、実際に取得していない
   取得日を記載しない）。**設定時に実際の Cloudflare ダッシュボードの画面で UI・式構文・
   プラン制限を確認すること**）。パターンは URL 全体に先頭から一致する（Cloudflare の
   Wildcard match は前方一致ではなく、`*` を含むパターン全体を URL 全体に対して照合する）。
   `*/page/*` のような前方に `*` を置く contains 的なパターンは使わない（`/labs/solar/`
   配下の `/page/2/` 等、意図しない URL まで拾ってしまう）。以下の5本を、
   この順序（`/posts/*` は `/labs/solar/${1}`、他はそれぞれの配下に閉じているため干渉しない）
   で作成する:

   | ルール名 | マッチ条件（Wildcard pattern） | 転送先（Wildcard target URL） | ステータス |
   |---|---|---|---|
   | redirect-posts | `https://hlabsworks.com/posts/*` | `https://hlabsworks.com/labs/solar/${1}` | 301, Preserve query string ON |
   | redirect-metrics | `https://hlabsworks.com/metrics/*` | `https://hlabsworks.com/labs/solar/metrics/${1}` | 301, Preserve query string ON |
   | redirect-scc | `https://hlabsworks.com/solar-charge-controller/*` | `https://hlabsworks.com/labs/solar/solar-charge-controller/${1}` | 301, Preserve query string ON |
   | redirect-tags | `https://hlabsworks.com/tags/*` | `https://hlabsworks.com/labs/tags/${1}` | 301, Preserve query string ON |
   | redirect-page | `https://hlabsworks.com/page/*` | `https://hlabsworks.com/` | 301, Preserve query string ON |

   （`redirect-page`（m-1）: 事業トップ化で `/page/2/` 以降（かつてのブログ一覧の2ページ目
   以降）が出力されなくなったための救済。`/page/1/` 自体は Hugo alias で `/` へ転送される
   （§2）が、Cloudflare 側でもまとめて拾っておく）

   （上表は Cloudflare の「Wildcard match」テンプレート（URL全体に対して `*` を1箇所含む
   パターンを指定し、転送先で `${1}` を使う形式）を想定している。ダッシュボードの実際の
   フィールド名・入力形式（URL全体かパスのみか等）は設定時に画面で確認すること。
   同等のことを Custom filter expression（例: `http.request.uri.path wildcard
   "/posts/*"`）で行う運用でもよいが、いずれの場合も `regex_replace()` は使わない）
3. **`/about/` は対象外**（Phase 1 では `/about/` を移動しないため、リダイレクトは張らない）
4. 5本とも作成後、"Enable" にする

### 確認用 curl（本番反映後）

```sh
curl -sI https://hlabsworks.com/posts/2026/hello/ | grep -i '^location\|^HTTP'
curl -sI https://hlabsworks.com/posts/index.xml | grep -i '^location\|^HTTP'
curl -sI https://hlabsworks.com/metrics/ | grep -i '^location\|^HTTP'
curl -sI https://hlabsworks.com/solar-charge-controller/ | grep -i '^location\|^HTTP'
curl -sI https://hlabsworks.com/tags/お知らせ/ | grep -i '^location\|^HTTP'
curl -sI https://hlabsworks.com/page/2/ | grep -i '^location\|^HTTP'
curl -sI https://hlabsworks.com/labs/solar/page/1/ | grep -i '^location\|^HTTP'
curl -sI https://hlabsworks.com/labs/tags/お知らせ/page/1/ | grep -i '^location\|^HTTP'
```

先頭5行はいずれも `HTTP/2 301` と `location: https://hlabsworks.com/labs/...` が返ることを
確認する。クエリ文字列付き（例: `?utm_source=x`）でも保持されることを確認する（
RSS の `<guid>` は記事の絶対URLを使っているため、URL移設で値が変わる。購読リーダーに
よっては同じ記事でも guid の変化を「新着」とみなし再配信する場合がある）。
`/page/2/` は `redirect-page` により `HTTP/2 301` で `location: https://hlabsworks.com/`
を返すことを確認する。`/labs/solar/page/1/`・`/labs/tags/お知らせ/page/1/` は
`redirect-page` のパターン（`https://hlabsworks.com/page/*`、URL全体に先頭から一致）に
マッチしないため 301 されず、通常のページ（`HTTP/2 200`、Hugo のページネーション）が
返ることを確認する。`*/page/*` のような前方一致・contains 的なパターンで設定してしまうと
この2つが誤って `/` に転送されてしまうため、この確認は §3 冒頭の設定ミス検出を兼ねる。

## 4. アプリ URL 規約（Phase 2 で `/apps/` を新設済み。個別アプリは今後追加）

`/apps/`（アプリ一覧）は Phase 2 で新設済み。個別アプリを追加する際は以下の規約に従う
（アプリ自体は本ドキュメント作成時点では未公開のため、個別URLは将来のための予約）:

- アプリ一覧: `/apps/`
- 個別アプリ: `/apps/<slug>/`
- 個別アプリのプライバシーポリシー: `/apps/<slug>/privacy/`
- 個別アプリのサポート: `/apps/<slug>/support/`

コンテンツファイルの置き方: 個別アプリ・privacy・support は
`content/apps/` 直下にフラットな `.md` ファイルとして置き、`url:` front matter で個別URLを
指定する（`content/labs/solar/metrics/methodology.md` 等、既存の他ページと同じパターン）。
ネストしたディレクトリ（`content/apps/<slug>/privacy.md` 等）は使わない。事業トップ
（`layouts/index.html`）のアプリ一覧は `site.RegularPages` の `Section` だけでなく、
front matter の `appEntry: true` が明示されたページだけを対象にする（`Section` だけで
絞ると、privacy/support ページもアプリ本体として一覧に混入してしまうため）。新しいアプリを
追加する際は、アプリ本体のページ（`content/apps/<slug>.md`）にだけ `appEntry: true` を
設定すること。

`content/privacy-policy.md`（サイト共通のプライバシーポリシー）は Phase 2 で、AdSense・
アフィリエイトなど Labs（`/labs/` 配下）に関する記述を主とし、アプリ固有の事項は
`/apps/<slug>/privacy/` に委任する形に改定済み（Phase 2 実装方針）。

`content/privacy-policy.md` の改定日（2026-10-03）は、Phase 2 を開業日である 2026-10-03
以降に本番反映する前提で設定している。Phase 2 のマージ・本番反映を
前倒しする場合は、改定日を実際の公開日に直すこと。

## 5. ロールバック手順

URL 移設は Hugo の `aliases`（サイト内リダイレクト）と `hugo.toml` の `permalinks`/`menu`/
`mainSections` の変更のみで実現しており、DB マイグレーションのような不可逆な変更は無い。
Phase 1・Phase 2 をまとめて1回のマージで main へ反映するため、切り戻しも1回の revert で
行う。問題が起きた場合は以下の順で行う（サイト側の revert を先に本番反映すると、その後
Cloudflare 側の無効化が完了するまでの間、Cloudflare が `/posts/X` 等を `/labs/solar/X` へ
301 し続けるが、revert 後のサイトは旧URL構成に戻っているため `/labs/solar/X` が存在せず
404 になる。これを避けるため Cloudflare 側を先に無効化する）:

1. **Cloudflare Single Redirects を先に無効化**: 本ドキュメント §3 で作成した5本の
   ルールを Disable する（削除はしなくてもよい）。これにより外部からの `/posts/*` 等への
   アクセスは、この時点ではまだ新URL構成であるサイト側の Hugo `aliases`
   （`http-equiv="refresh"`）に委ねられる。
2. **サイト側（このリポジトリ）**: §0 でマージしたマージコミットを revert する commit を
   1つ作り、main に push する。`git revert` 1回で、`content/labs/solar` を
   `content/posts` へ戻す（逆方向の）ファイル移動、`hugo.toml` の
   permalinks/menu/mainSections、事業トップ（`layouts/index.html`）・事業者情報
   （`content/about.md`）の新設は、いずれも自動的に元に戻る（手動でファイルを移動し直したり
   設定を書き直したりする必要はない）。次回ビルドから移設前のURL構成（ルートが記事一覧、
   `/about/` が旧「このサイトについて」）に戻る。`content/labs/solar/metrics/`・
   `content/labs/solar/solar-charge-controller.md` も自動的に
   `content/metrics/`・`content/solar-charge-controller.md` へ戻り、front matter の
   `type:`/`aliases:` も戻る。
3. **確認**: revert 後は curl で直接確認する:
   ```sh
   curl -s https://hlabsworks.com/posts/2026/hello/ -o /dev/null -w '%{http_code}\n'
   curl -s https://hlabsworks.com/ -o /dev/null -w '%{http_code}\n'
   ```
   revert 後はいずれも `200` を返し、`/posts/2026/hello/` は（新URLへのリダイレクトではなく）
   旧URL構成の記事ページそのものが表示されることを確認する。
   - revert 後は、`/labs/...`（新URL）・`/`（事業トップ）・`/about/`（事業者情報）への
     外部からのリンクや検索エンジンのインデックスは、旧URL構成のページに置き換わるか
     404 になる。移設後に張られた新URLへのリンク・ブックマーク・RSS購読には影響が出る。

注意:
- ブラウザ・中間キャッシュは 301 を長期間キャッシュする。切り戻し後もキャッシュ済みの
  クライアントは古い 301 応答（新URLへの転送）に従い続けることがある。切り替え直後の
  一定期間は 301 ではなく 302（一時的リダイレクト、キャッシュされにくい）で運用する
  選択肢もある（Cloudflare のルール設定でステータスコードを変更する）。
- 手順1と2の間は、Cloudflare 側の 301 が無効化されているため `/posts/*` 等への外部
  アクセスはサイト側の Hugo `aliases` による `http-equiv="refresh"` 転送でのみ救済される
  （この時点ではまだ新URL構成のため 404 にはならない）。手順1と2の実施間隔はできるだけ
  短くする。

## 6. 回帰テスト

- `scripts/tests/site-structure-test.sh`: 新URL側の必須ファイル生成・旧URLの
  `http-equiv="refresh"` リダイレクト・全ページの内部リンク実在・2テーマ構成での一覧分離
  （フィクスチャで2つ目のテーマを注入し、Labsトップの一覧に2件出ること、各テーマの記事
  一覧に他テーマの記事が混ざらないことを検証する）に加えて、記事の `tags` 未設定検出・
  `/index.xml`（事業トップ）への月次レポートのリンク混入・`og:locale`・`/categories/`
  無効化・`github.com/hlabsworks` へのリンク不在・電話番号らしき文字列の検出（`js/vendor/`
  配下の外部ライブラリは対象外）等を確認する（`.github/workflows/hugo.yml`
  の「Run site head tests」直後に実行）。CI が `render_monthly_posts.py` で生成する
  `content/labs/solar/monthly-report/` 配下のページも、同スクリプト内でフィクスチャ
  （`test_render_monthly_posts.py` の `base_snapshot()`）を使って一時的に生成し、リンク
  検査の対象にする。ビルド・生成は一時ディレクトリにコピーした
  リポジトリ上でのみ行い、作業ツリー（`content/labs/solar/monthly-report/` を含む）には
  一切書き込まない（旧実装は実行前から存在した月次レポート出力ディレクトリを trap で
  無条件に削除しており、運営者のファイルを消しうるバグがあった）。リポジトリのコピーは
  固定のディレクトリ名一覧ではなく除外指定方式にする（将来 `i18n/`・`archetypes/`・
  `config/` を追加してもテストと本番ビルドの対象が食い違わないようにする）。内部リンク検査・
  旧URLリダイレクト検査のPythonが異常終了した場合は明示的にfailする（従来はexit codeを
  見ておらず、Pythonの例外で検査が空振りしても「合格」してしまっていた）。bash 3.2（macOS標準）と
  bash 5.x のどちらでも構文エラーにならないよう、ヒアドキュメントはコマンド置換の中に
  直接書かず、一度ファイルに出力してから読む。
- `scripts/tests/site-head-test.sh`: GA4/AdSense/Cloudflare Web Analyticsのタグ出力を
  検証する回帰テスト。site-structure-test.shと同じ、一時ディレクトリへコピーしてから
  ビルドする方式にそろえてある（旧実装はREPO_ROOTで直接ビルドしており
  `.hugo_build.lock`を作業ツリーに書き、既存の`content/*/monthly-report/`もビルドに
  取り込んでいた）。
- `scripts/tests/no-draft-leak-test.sh`: 未完成記事（本文冒頭に「下書きメモ」+ TODO が残る
  記事）が `draft` フラグの設定ミスで本番ビルドに公開されてしまうことを検出する回帰テスト。
  `--buildFuture` 付きの本番ビルドの出力全ページを対象に「下書きメモ」「TODO」の文字列を
  横断検査する。site-structure-test.sh の draft 検査（ビルド後の `index.html` の有無と
  `draft` フラグだけを見る）とは目的が異なるため両方が必要（前者はフラグの設定漏れ、
  後者は本文が実際に未完成のまま公開されていないかを見る）。
- `scripts/tests/site-structure-safety-test.sh`: 上記3つのスクリプト（site-structure-test.sh・
  site-head-test.sh・no-draft-leak-test.sh）自体が作業ツリーに書き込まないことを検証する
  回帰テスト。スクラッチに
  コピーしたリポジトリに事前配置したファイル・空ディレクトリが
  実行後も変化しないこと、ファイル・ディレクトリ一覧が増減しないこと、既存ファイルの
  内容（shasum）が変化しないことを確認する（従来は`find -type f`の
  一覧比較のみで、空ディレクトリの作成やkeep.md以外の既存ファイルの書き換えを見逃していた）。
  `.github/workflows/hugo.yml` の「Run site structure tests」直後に実行する。
  `scripts/blog-metrics/test_render_monthly_posts.py` の `PipelineWiringTest` には、
  `hugo.yml` がこの4つのテストスクリプトを全て実行していることを検証するテストがあり、
  いずれか1つでも `hugo.yml` のステップから外れると検出する。
- `scripts/blog-metrics/test_render_monthly_posts.py` の `PipelineWiringTest`:
  `render_monthly_posts.py` の `METHODOLOGY_URL`/`SOLAR_CHARGE_CONTROLLER_URL` が
  `content/labs/solar/metrics/_index.md`・`content/labs/solar/solar-charge-controller.md`
  の実際の配置（front matterに`url:`オーバーライドを持たないため、ディレクトリ構成から
  導ける素のpermalink）と一致していること、CI の `--out` と `.gitignore` が
  `content/labs/solar/monthly-report/` を指していることを検証する。

## 7. content/metrics・content/solar-charge-controller.md の配置方式の比較

R1'着手時に、以下2方式を実ビルドで比較した（`layouts/metrics/list.html`のレイアウト引き当てと
パンくずの両方を基準に判断）。

- **方式A（front matterのurl:オーバーライドを維持）**: `content/metrics/`・
  `content/solar-charge-controller.md`をディレクトリ移動せず、`url: /labs/solar/metrics/`
  等のオーバーライドだけを書き換える。レイアウト引き当ては動作したが、コンテンツの物理配置と
  URLが一致しなくなり、「Labsは`content/labs/`配下」という一貫性が崩れる。
- **方式B（`content/labs/solar/`配下へ移動し、`type:`で明示）**: `content/metrics/`を
  `content/labs/solar/metrics/`へ、`content/solar-charge-controller.md`を
  `content/labs/solar/solar-charge-controller.md`へ移動し、`url:`オーバーライドをやめて
  素のpermalink（ディレクトリ構成そのまま）にする。実績ダッシュボード
  （`content/labs/solar/metrics/_index.md`）には`type: metrics`を付け、Hugoの
  レイアウト検索が`type:`で`layouts/metrics/list.html`を引き当てることを実ビルドで確認済み。

**方式B を採用**（本ドキュメントの§1の記載どおり）。理由: (1) より素直な構成（URL移設の
指示どおり、コンテンツの物理配置がURLと一致する）、(2) `type:`によるレイアウト引き当ては
`content/`内の物理パスに依存しないため、将来テーマが増えてもレイアウトを再定義する必要が
ない、(3) `url:`オーバーライドが1つ減ることで、URLとファイルパスの食い違いによる将来の
保守ミスを防げる。

パンくずについて: `layouts/metrics/list.html`は元々PaperModの`breadcrumbs.html`パーシャルを
呼んでいなかった（既存の問題）。R1'であわせて`{{- partial "breadcrumbs.html" . }}`を追加し、
実績ダッシュボードでも「ホーム > Labs > 太陽光×蓄電池×ポータブル電源 > 実績ダッシュボード」の
パンくずが出ることを実ビルドで確認した。`content/labs/solar/metrics/methodology.md`・
`content/labs/solar/solar-charge-controller.md`はPaperMod既定の`single.html`を使うため、
元からパンくずが出る。

Labsトップ（`/labs/`）と各テーマのトップ（`/labs/<テーマ>/`）は、いずれもHugoの
セクション一覧ページ（Kind: section）だが、同じ`Section`（"labs"）を持つため、テーマの
物理パスに基づくネストしたレイアウト検索（例: `layouts/labs/solar/list.html`）に頼ると
Hugoのバージョン間の挙動差のリスクがある。そのため、Labsトップには明示的に
`type: labshome`を付けて`layouts/labshome/list.html`を専用に引き当て、各テーマのトップは
`type:`を指定せず既定の`layouts/labs/list.html`（テーマ非依存の共通テンプレート、
`.RegularPages`から`Params.tags`を持つページ＝実際の記事・月次レポートだけに絞って一覧化）
を使う設計にした。新テーマを追加する際は`content/labs/<テーマ名>/_index.md`を作成するだけで、
Labsトップの一覧（`.Sections`を列挙）とテーマトップの記事一覧の両方が自動的に機能する。
