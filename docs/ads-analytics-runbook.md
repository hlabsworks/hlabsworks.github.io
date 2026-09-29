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
  「Run blog-metrics unit tests」の直後に実行される）

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
   （確認用に別のタグを追加する必要はない）
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

## 4. ロールバック

いずれのサービスも、対応する値を空文字に戻して main に push すれば、次のビルドから
タグの出力が停止する（`static/ads.txt` は残るが実害はない。気になる場合は削除する）:

- GA4 停止: `services.googleAnalytics.ID = ""`
- Cloudflare Web Analytics 停止: `params.cloudflareAnalytics.token = ""`
- AdSense 停止: `params.adsense.client = ""`
