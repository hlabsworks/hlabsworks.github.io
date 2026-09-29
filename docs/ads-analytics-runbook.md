# アクセス解析（GA4 / Cloudflare Web Analytics）・Google AdSense 導入手順

hlabsworks.com に GA4・Cloudflare Web Analytics・Google AdSense を「ID を設定すれば有効になる」
形で組み込んである（`hugo.toml` の `services.googleAnalytics.ID` /
`params.cloudflareAnalytics.token` / `params.adsense.client`）。いずれも値が空文字の間は
本番ビルドでも一切タグを出力しない。本ドキュメントは各サービスの ID・token を取得したあとの
設定手順のみをまとめる。公開リポジトリのため、実際の測定 ID・token・pub ID はこのファイルには
書かない（取得後に各自 `hugo.toml` に直接記入する。これらは公開 HTML に載る値であり秘匿情報では
ないため、GitHub Actions Secret 化は不要）。

実装箇所:
- GA4: `hugo.toml` の `[services.googleAnalytics]` `ID`（Hugo 内蔵の
  `google_analytics.html` テンプレートが本番ビルドでのみ出力する。テーマの
  `themes/PaperMod/layouts/_partials/head.html` が `hugo.IsProduction` 判定の内側で呼ぶ）
- Cloudflare Web Analytics / AdSense: `hugo.toml` の `[params.cloudflareAnalytics]` `token` /
  `[params.adsense]` `client`。`layouts/_partials/extend_head.html`（このリポジトリ側の
  上書き partial）が同じ本番判定（`hugo.IsProduction | or (eq site.Params.env "production")`）
  の内側で出力する
- 回帰テスト: `scripts/tests/site-head-test.sh`（ID未設定/設定済み × production/development の
  3パターンを `hugo` の実ビルド出力で検証。CI では `.github/workflows/hugo.yml` の
  「Run blog-metrics unit tests」の直後に実行される）。URL移設(Phase 1、2026-09-27。テーマ別
  パス化R1'、2026-09-28)後は`scripts/tests/site-structure-test.sh`
  （`docs/site-structure-runbook.md` §6）がその直後に続けて実行され、GA4/AdSense/Cloudflare
  のタグ出力とは別に、`/labs/<テーマ>/` 配下への移設・旧URLのリダイレクト・内部リンクの
  整合性を検証する。CIが生成する月次レポートページ（`content/labs/solar/monthly-report/`）も
  フィクスチャ経由で同スクリプト内で生成・検査する。

## 1. GA4（Google アナリティクス）

1. Google アナリティクスでプロパティを新規作成する
2. データストリーム（種類: ウェブ、URL: `https://hlabsworks.com`）を追加し、発行された
   測定 ID（`G-XXXXXXXXXX` 形式）を確認する
3. `hugo.toml` の `[services.googleAnalytics]` `ID` にその測定 ID を設定する
4. main に push し、`.github/workflows/hugo.yml` のビルド・デプロイ完了を待つ
5. 本番 HTML に反映されたことを確認する:
   ```sh
   curl -s https://hlabsworks.com/ | grep gtag
   ```
   `gtag('config', 'G-XXXXXXXXXX')` が出力されていれば反映済み
6. GA4 の「レポート → ユーザー属性」「テクノロジー（ブラウザ・OS）」で、想定読者の
   属性・利用環境を確認できる。初日はデータが少なくグラフが安定しないため、判断は
   数日分のデータが溜まってから行う

## 2. Cloudflare Web Analytics

**現状（2026-09-26 確認）**: hlabsworks.com は Cloudflare のプロキシ経由で配信されており、
Cloudflare ダッシュボードの Web Analytics で既に「Enable, excluding visitor data in the EU」
（ビーコンをエッジで自動挿入、EU からの訪問者は除外）が有効になっている。**このため
`params.cloudflareAnalytics.token` は空のままにする**（token を設定すると自動挿入分と
二重にビーコンが入り、計測が重複する）。以下の手順は、将来プロキシを外す・自動挿入を
「Enable with JS Snippet installation」に切り替える場合にだけ使う。


1. Cloudflare ダッシュボード → 左メニュー「Analytics & Logs」→「Web Analytics」
2. 「サイトを追加」で `hlabsworks.com` を追加する
3. セットアップ方式の選択肢が出るが、コードで管理する方針のため **JS スニペット方式** を選ぶ
   （プロキシ済みサイト向けの「自動セットアップ（HTML への自動挿入）」は使わない。挿入箇所が
   `hugo.toml` の値から追えなくなるため）
4. 発行された JS スニペット中の `token` の値を `hugo.toml` の
   `[params.cloudflareAnalytics]` `token` に設定する
5. main に push し、デプロイ後に本番 HTML に `cloudflareinsights` を含むタグが
   出力されていることを確認する（GA4 の §1-5 と同様に `curl | grep` で確認できる）

## 3. Google AdSense

### 3-1. 申請前の前提

AdSense の審査は独自コンテンツの量・質を見る。2026-09-26 時点で記事は3本＋固定ページのみのため、
今後の記事・月次レポート等の投稿が数本たまり、コンテンツが一定量に育ってから申請することを
推奨する（時期はオーナー判断）。

### 3-2. 申請〜有効化

1. AdSense に申請し、サイト（`https://hlabsworks.com`）を登録する
2. サイト所有権確認: AdSense が提示する確認用 `<script>` タグは、
   `hugo.toml` の `[params.adsense]` `client` に発行された `ca-pub-XXXXXXXXXXXXXXXX` を
   設定すれば、`extend_head.html` が出力する自動広告タグで同じ確認要件を満たせる
   （確認用に別のタグを追加する必要はない）。
   **前提**: この確認方法はトップページ（`/`）等の事業ページで広告タグが出力されていること
   （§3-3の `adsOnBusinessPages` が `true`）が前提。`false` にしている間は事業ページに
   広告タグが出ないため、サイト所有権確認にトップページを使う場合は事前に `true` であることを
   確認すること（`/labs/` 配下の記事ページでは `adsOnBusinessPages` の値に関わらず常に出力
   されるため、そちらを確認先にしてもよい）
3. `static/ads.txt` を新規作成し、以下の1行を書く（`XXXXXXXXXXXXXXXX` は発行された pub ID に
   置き換える）:
   ```
   google.com, pub-XXXXXXXXXXXXXXXX, DIRECT, f08c47fec0942fa0
   ```
4. main に push し、`https://hlabsworks.com/ads.txt` が公開されていることを確認する
5. 承認されたら AdSense 管理画面の「広告 → 概要」で「自動広告」を ON にする
6. AdSense 管理画面の「プライバシーとメッセージ」で EEA/UK 向けの同意メッセージ
   （Consent Management Platform）を有効化する（EU 圏の利用者に対する同意取得ポリシー対応。
   `client` を設定した時点でタグは出力されるが、同意メッセージの表示自体は AdSense 側の
   この設定に依存する）
7. 審査中・広告非表示の間も `client` を空文字のままにしておけば、他の挙動
   （ページ表示・GA4・Cloudflare Web Analytics）に影響しない

### 3-3. 事業ページへの広告表示について（noAds / adsOnBusinessPages）

事業トップ（`/`）・事業者情報（`/about/`）・お問い合わせ（`/contact/`）・プライバシーポリシー・
`/apps/` 配下は front matter の `noAds: true`（`content/apps/_index.md` は cascade で配下にも
継承）を持つ（`layouts/_partials/extend_head.html`）。ただし `noAds: true` だけを見て
「事業ページには広告が出ない」と早合点しないこと。`hugo.toml` の
`[params.business] adsOnBusinessPages` が `true` の間（**現状の既定値**）は、AdSense の審査・
サイト所有権確認のためにトップページ等のコードが確認される場合があるのを踏まえ、`noAds`
ページでも広告タグを出力する。つまり `noAds: true` は
「`adsOnBusinessPages` が `false` になったときに広告を止める」ためのフラグであり、単独では
広告出力の有無を決めない。

- **現状（2026-09-28確認）**: hlabsworks.com は AdSense 未承認（審査リクエスト前または審査待ち）。
  `adsOnBusinessPages` の既定値は `true`（審査対応のため事業ページにも広告コードを出す）
- **AdSense 承認後**: `hugo.toml` の `adsOnBusinessPages` を `false` に変更し、main に push する
  （事業ページ（`/`, `/about/`, `/contact/`, `/privacy-policy/`, `/apps/` 配下）には広告を
  出さない運用に戻す。Labs（`/labs/` 配下）は `noAds` を付けていないため、この設定に関わらず
  常に広告が出る）
- 回帰テスト: `scripts/tests/site-head-test.sh` が `adsOnBusinessPages` true/false 両方の
  ケースを検証する
- `content/privacy-policy.md` の広告配信についての記述は、事業ページでの表示有無を断定しない
  表現にしてある（フラグの値によって内容が事実と食い違わないようにするため）

## 4. ロールバック

いずれのサービスも、対応する値を空文字に戻して main に push すれば、次のビルドから
タグの出力が停止する（`static/ads.txt` は残るが実害はない。気になる場合は削除する）:

- GA4 停止: `services.googleAnalytics.ID = ""`
- Cloudflare Web Analytics 停止: `params.cloudflareAnalytics.token = ""`
- AdSense 停止: `params.adsense.client = ""`
