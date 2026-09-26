# blog-metrics 自動更新パイプライン運用手順

homelab（`~/.ssh/config` の Host エイリアス `homelab`）が毎日 solar-metrics-data リポジトリへ
公開メトリクスを push し、このサイトの `.github/workflows/hugo.yml` が `validate_metrics.py` で
検証してから取り込む。コードの詳細は `scripts/blog-metrics/` 配下・各スクリプトの docstring を
参照。本ドキュメントは配備・GitHub側設定・障害対応の手順のみをまとめる。

実ホスト名・IP・ユーザー名は本ドキュメントには書かない（公開リポジトリのため）。作業時は
各自の環境の値に読み替えること。以降 `<homelab>` は homelab の SSH 接続先（`~/.ssh/config` の
Host エイリアス推奨）、`<controller>` は制御機(solarchgctl)の SSH 接続先を指す。

## 1. 初回配備手順（この順序で行う）

**重要**: `~/solar-metrics-data` の clone は `--service-user`（blog-metrics.service の
`User=`）と同じユーザーの `$HOME` に作る必要がある。run-daily.sh は systemd 実行時
`--clone-dir` を明示指定しない限り既定で `$HOME/solar-metrics-data`（サービス実行ユーザーの
ホーム）を見るため、別ユーザーで ssh ログインして clone すると見つからずに失敗する。
以下の手順3〜5は必ず `--service-user` に指定する予定のユーザーで ssh ログインして行うこと。

1. **データ用リポジトリを作成する**: GitHub で `hlabsworks/solar-metrics-data` を
   **private** で新規作成する（`README.md` を1つコミットしておく）。作成直後に
   Settings → General → Default branch が `main` であることを確認する。
2. **GitHub Deploy key（write、homelab の push 用）を登録する**（§2-2）。
3. **GitHub Deploy key（read-only、CI の `_incoming` checkout 用）を登録し、秘密鍵を
   このリポジトリの Actions Secret に設定する**（§2-3。solar-metrics-data が private に
   なったため、`hugo.yml` の `_incoming` checkout は既定の `GITHUB_TOKEN` では読めず、
   この鍵が必須になった）。
4. **homelab の `~/.ssh/config` を設定する**（§2-1, §2-2 の両方の Host エイリアス）。
5. **制御機(solarchgctl)の authorized_keys を設定する**（§2-1）。
6. **homelab に `/opt/blog-metrics` と `~/solar-metrics-data` を用意する**（`--service-user`
   予定のユーザーで ssh ログインして実行。上記の注意参照）:
   ```sh
   ssh <homelab> 'sudo mkdir -p /opt/blog-metrics && sudo chown $(whoami): /opt/blog-metrics'
   ssh <homelab> 'git clone git@<solar-metrics-data用Hostエイリアス>:hlabsworks/solar-metrics-data.git ~/solar-metrics-data'
   ```
   `/var/log/blog-metrics`・`/run/blog-metrics`・`/var/lib/blog-metrics` は
   `blog-metrics.service` の `LogsDirectory=`/`RuntimeDirectory=`/`StateDirectory=` が
   systemd 起動時に自動作成するため、手動で作る必要はない
   （`--state-dir` は `blog-metrics.service` の `ExecStart` で明示的に
   `/var/lib/blog-metrics` を指定しており、`deploy-homelab.sh` が置く
   `allow-history-once` フラグ（§6）もこのディレクトリに統一している）。
7. **Mac からバンドルを配備する**:
   ```sh
   scripts/blog-metrics/deploy-homelab.sh --service-user <homelabの実行ユーザー名>
   ```
   （rsyncで `/opt/blog-metrics/` へ配置するのみ。`--install-units` を付けない限り
   systemd unit のインストールは行わない。詳細はスクリプト先頭のコメント参照）。
8. **`run-daily.sh --dry-run` で配線を確認する（オーナーが実行）**:
   ```sh
   ssh <homelab> '/opt/blog-metrics/run-daily.sh --dry-run'
   ```
   何も実行されず即終了することだけを確認する段階。
9. **負のテスト N1（deploy key のスコープ確認）を実施する**（§4。main マージ前に行う
   理由: N1 は solar-metrics-data 側の鍵設定だけで完結し、このリポジトリの状態に
   依存しないため、早い段階で鍵の設定ミスに気づける）。
10. **solar-metrics-data に初回データを投入する**（Mac から 1 回だけ。`hugo.yml` は
    `_incoming/data/metrics/*.json` と `pipeline.json` が無いと G1 で失敗し Pages が
    更新されないため、homelab の timer を有効化する前に手動で最初のデータを置く）:
    このリポジトリで確認済みの `data/metrics/{daily,monthly,meta,bills,layers}.json` を
    データ用リポジトリの `data/metrics/` にコピーし、`pipeline.json`（`source: "mac-seed"`、
    `bundle_rev` はこのリポジトリの HEAD、`inputs.*_sha256` は `scripts/blog-metrics/tariff.json`
    と `data/metrics/official_*.json` の sha256）を書いて commit・push する。
    `official_buy.json` / `official_sell.json` は main 側の入力なので置かない（G1 で拒否される）。
    push 前に `validate_metrics.py --incoming <データ用clone> --repo .` で全ゲート通過を確認する。
11. **このリポジトリの feat ブランチを main にマージする**
    （`.github/workflows/hugo.yml` の `_incoming` checkout・`validate_metrics.py` 検証
    ステップは main にマージされて初めて有効になる）。
12. **負のテスト N2（壊れたJSONでPagesが更新されないこと）を実施する**（§4。main
    マージ後でないと `hugo.yml` 側の検証ステップ自体が存在せず確認できないため、
    ここで行う。timer 有効化前に確認しておく）。
13. **systemd unit をインストールし、timer を有効化する（オーナーが実行）**:
    ```sh
    scripts/blog-metrics/deploy-homelab.sh --service-user <homelabの実行ユーザー名> --install-units
    ssh <homelab> 'sudo systemctl enable --now blog-metrics.timer'
    ```
    実施記録: 2026-09-26 に有効化。初回実行で timer の `OnCalendar` 構文誤り（bad unit file
    setting）と G11 の部分月誤検知（publish_since 直後の 8 月が 3 日分）を発見し修正済み。
    修正後の初回実行は成功し、データ repo への push → `workflow_dispatch` → Pages 更新まで確認。
14. **初回実行を手動で確認する**:
    ```sh
    ssh <homelab> 'sudo systemctl start blog-metrics.service && sleep 5 && sudo systemctl status blog-metrics.service --no-pager'
    ```
    `/var/log/blog-metrics/run.log` に詳細ログが残る。

**月次確定の自動化（任意、DDR実装手順S1）を導入する場合の配備順序（重要・QA指摘F4）**:
S1 で `run-daily.sh`/`validate_metrics.py`（G1 の `inputs/*.json` 許可等）が同時に変わるため、
**必ず「① このリポジトリの feat ブランチを main にマージする → ② `deploy-homelab.sh` で
homelab に配備する」の順序を守ること**。逆順（先に homelab へ配備）で実行すると、S1 対応済みの
新しい `run-daily.sh` が最初の実行だけで `~/solar-metrics-data/inputs/official_buy.json`・
`official_sell.json` を（`--auto-inputs-dir` のhandoffが未設置でも）bundle の値から
`clone/inputs/` へ**必ず初期コピーし、データ用リポジトリに新しく `inputs/` が追加された状態で
push してしまう**。このとき GitHub Actions が checkout する main がまだ S1 未マージ（旧
`validate_metrics.py`）だと、G1（旧: 許可ファイルの完全一致要求）が `inputs/*.json` を
「許可されていないファイル」として拒否し、Pages のデプロイが止まる。
「別プロセス（`/var/lib/energy-fetch/handoff/`、private、本リポジトリの対象外）が未設置でも
`run-daily.sh` の挙動は変わらない」という表現は誤り（旧版の記載を訂正）: handoff
（`--auto-inputs-dir` の既定パス）が無くても、上記の移行措置的な初期コピー自体は S1 導入後の
最初の実行で必ず発生する。「変わらない」のは、S1 が main にマージ済みの状態で ① を終えてから
② を行った場合に限る（その場合は新しい G1 がこの `inputs/` 追加を最初から許可しているため
問題にならない）。詳細は §6 末尾参照。

### 1-a. 通知（notify.sh）の前提

`/usr/local/bin/notify.sh` は `/etc/solar-notify.env`（root 所有）を読む。blog-metrics.service は
一般ユーザーで動くため、そのままでは読めず LINE 通知が黙って送られない。homelab では
グループ `solar-notify` を作り、env を `root:solar-notify 0640` にして実行ユーザーをグループに
追加してある（2026-09-26）。実行ユーザーを変える場合は同じ手当てをすること。
また `/opt/blog-metrics` は他ユーザー（自動取得の実行ユーザー）からも読める 0755 にしておく
（`deploy-homelab.sh` が配備のたびに戻す）。

## 2. 鍵の作成と authorized_keys

### 2-1. homelab -> solarchgctl（metrics-export.sh 実行用）

homelab 上で専用鍵を作る（他用途と共用しない）:

```sh
ssh-keygen -t ed25519 -f ~/.ssh/id_ed25519_metrics-export -C "homelab-blog-metrics" -N ""
```

`~/.ssh/config`（homelab側）に Host エイリアスを登録する（`aggregate.sh --ssh-host` の既定値
`solarchgctl-metrics` と一致させる）:

```
Host solarchgctl-metrics
    HostName <controllerの実ホスト名/IP>
    User <controllerのSSHユーザー名>
    IdentityFile ~/.ssh/id_ed25519_metrics-export
    IdentitiesOnly yes
```

solarchgctl 側の `~/.ssh/authorized_keys` に、**この鍵で `metrics-export.sh --from-ssh`
以外を実行できず、かつ homelab 以外からの接続を拒否するよう** `from=` と forced-command
付きで追記する（鍵が漏洩しても集計値取得以外に使えず、他ホストから使い回すこともできない
ようにするため。Security Engineer視点で必須。`--from-ssh` を付けると
metrics-export.sh 側で `--db` 引数上書き等が禁止される多重防御が働く。
`scripts/metrics-export.sh` 冒頭コメント参照）:

```
restrict,from="<homelabのIP/CIDR、例: 203.0.113.14/32>",command="/usr/local/bin/metrics-export.sh --from-ssh" ssh-ed25519 AAAA...（homelabの公開鍵） homelab-blog-metrics
```

### 2-2. homelab -> GitHub（solar-metrics-data への push 用）

homelab 上でもう1本、別用途の鍵を作る:

```sh
ssh-keygen -t ed25519 -f ~/.ssh/id_ed25519_solar-metrics-data -C "homelab-solar-metrics-data" -N ""
```

GitHub の `hlabsworks/solar-metrics-data` → Settings → Deploy keys → Add deploy key で
公開鍵を登録し、**"Allow write access" を有効にする**（これがこの鍵の権限を
`solar-metrics-data` 1リポジトリのみに限定する仕組み — GitHubの個人アカウント鍵として
登録すると全リポジトリに触れてしまうため、必ず repository の Deploy key として登録する）。

`~/.ssh/config`（homelab側）:

```
Host github.com-solar-metrics-data
    HostName github.com
    User git
    IdentityFile ~/.ssh/id_ed25519_solar-metrics-data
    IdentitiesOnly yes
```

`~/solar-metrics-data` の remote は `git@github.com-solar-metrics-data:hlabsworks/solar-metrics-data.git`
を指すように設定する。

### 2-3. GitHub Actions -> solar-metrics-data（CI の `_incoming` checkout 用、read-only）

solar-metrics-data を private にしたため、`.github/workflows/hugo.yml` の
`_incoming` checkout（`Checkout solar-metrics-data (incoming)` ステップ）は既定の
`GITHUB_TOKEN` では読めない（別リポジトリのため権限が及ばない）。**write権限を持たない
専用鍵**を作り、Actions Secret として登録する:

```sh
ssh-keygen -t ed25519 -f /tmp/id_ed25519_metrics-data-read -C "hlabsworks.github.io-ci-read" -N ""
```

GitHub の `hlabsworks/solar-metrics-data` → Settings → Deploy keys → Add deploy key で
`/tmp/id_ed25519_metrics-data-read.pub` を登録する。**"Allow write access" は有効に
しない**（デフォルトのまま read-only。§2-2 の鍵と違い、CI はデータを書き換える必要が
無いため）。

このリポジトリ (hlabsworks.github.io) → Settings → Secrets and variables → Actions →
New repository secret で、秘密鍵 `/tmp/id_ed25519_metrics-data-read` の中身を
`METRICS_DATA_READ_KEY` という名前で登録する。登録後、鍵ファイルは削除する:

```sh
rm -f /tmp/id_ed25519_metrics-data-read /tmp/id_ed25519_metrics-data-read.pub
```

`hugo.yml` は `actions/checkout@v4` の `ssh-key: ${{ secrets.METRICS_DATA_READ_KEY }}`
でこの鍵を使う。`persist-credentials: false`（後続ステップに認証情報を残さない）・
`fetch-depth: 10`（`validate_metrics.py` の G8/G9/G11 が `git show HEAD~N:...` で、JSON として
読める最新の祖先コミットと比較するために必要。`PREVIOUS_COMMIT_MAX_DEPTH` と揃える。
壊れた push を revert した直後に HEAD~1 が読めず CI が落ちた N2 の教訓）は維持している。`ssh-key` はチェックアウト自体の認証方式であり、
`persist-credentials: false` はチェックアウト後の資格情報の残し方の設定なので両者は
独立して両立する。`validate_metrics.py` はチェックアウト済みのローカル `.git` に対して
`git show`/`git ls-files` を実行するだけ（ネットワークアクセスなし）のため、
`ssh-key` 経由のチェックアウトでも同様に動作する（VERIFIED、ローカルで
`actions/checkout` 相当の `git clone` 後に同じコマンドを実行して確認）。

## 3. GitHub 側設定

- **solar-metrics-data リポジトリ**: private で作成し（§1 手順1）、Settings → Actions →
  Disable actions にする（データ専用リポジトリであり、Actions を使う正当な理由が無い。
  private であっても、将来コラボレーターが増えた場合やソースが汚染された場合の保険として、
  repo-write権限を持つ Actions ワークフローが動く余地自体を無くしておく）。
- **このリポジトリ (hlabsworks.github.io)**:
  - Settings → Environments → `github-pages` → Deployment branches and tags を
    `main` のみに限定する（誤って別ブランチからのdeployを防ぐ）。
  - Settings → Actions → General → Workflow permissions を
    "Read repository contents permission" に設定する（`hugo.yml` の
    `permissions: contents: read`・両 `actions/checkout` の `persist-credentials: false`
    と多重に絞る）。
  - Settings → Secrets and variables → Actions に `METRICS_DATA_READ_KEY`
    （§2-3）が登録されていることを確認する。

## 4. 負のテスト

§1 の手順9（main マージ前）・手順11（main マージ後、timer 有効化前）でそれぞれ1回実施する。

- **N1: データ repo 以外へ push 不可の確認**（§1 手順9、main マージ前でも実施可能）—
  §2-2 の鍵で、`solar-metrics-data` 以外のリポジトリ（例: このリポジトリ自体）へ push を
  試み、`Permission denied` になることを確認する:
  `GIT_SSH_COMMAND="ssh -i ~/.ssh/id_ed25519_solar-metrics-data" git push git@github.com:hlabsworks/hlabsworks.github.io.git HEAD:refs/heads/_deploy-key-scope-check`
  （成功してしまった場合は鍵の登録範囲が誤っている — 個人アカウント鍵として登録されていないか確認する）。
- **N2: 壊れたJSONでPagesが更新されない**（§1 手順12、main マージ後のみ実施可能。
  `hugo.yml` の `Validate incoming metrics data` ステップが main に無いと確認できないため）—
  `solar-metrics-data` に手動で壊れたJSON
  （例: `data/metrics/daily.json` の末尾カンマを1つ増やす）を push し、このリポジトリの
  Actions（`Deploy Hugo site to Pages`）の `Validate incoming metrics data` ステップが
  失敗し、`Build with Hugo`・`deploy` ジョブが実行されないことを確認する。確認後は
  `solar-metrics-data` 側で当該コミットを revert し、`workflow_dispatch` で再実行して
  復旧（全ゲート通過・deploy 成功）まで確認する。
  実施記録: 2026-09-26 に実施。壊れた push は G2 で拒否され deploy はスキップ、サイトは
  旧ビルドのまま（期待どおり）。revert 直後の再実行が HEAD~1 の壊れた JSON を読んで
  Traceback で落ちる回帰を発見し、読める祖先まで遡る修正（PR #2）を入れて復旧を確認した。

## 5. ロールバック

- **timer を止める**: `ssh <homelab> 'sudo systemctl disable --now blog-metrics.timer'`
- **公開済みの悪い値を戻す**: `solar-metrics-data` 側で該当コミットを
  `git revert <sha>` して push する（`validate_metrics.py` の G9 に引っかかる場合は
  `workflow_dispatch` の `allow_history_change` を有効にして手動実行する）。
  パイプラインのコード自体に問題がある場合は、このリポジトリ側の該当コミットを revert する。
- **鍵を失効させる**: 漏洩・誤用が疑われる場合、solarchgctl の
  `~/.ssh/authorized_keys` から該当行（§2-1）を1行削除する。GitHub側は
  Settings → Deploy keys から該当鍵を削除する（§2-2）。
- **月次レポート記事を止める**（設計判断2026-09-23/26「速報＋改訂」方式）:
  `scripts/blog-metrics/render_monthly_posts.py` の `SUPPRESSED_BILLING_MONTHS` に
  対象の請求月("YYYY-MM")を追加して main にコミットするだけでよい（homelab側の再配備は
  不要。次回のCIビルドから該当月の記事が生成されなくなる）。
- **月次レポートの改版を取り消す**: data repo（`solar-metrics-data`）側で該当の
  `posts/YYYY-MM.json` の変更を `git revert` して push する（`validate_metrics.py` の
  G16 に引っかかる場合は `--allow-history-change`／`workflow_dispatch` の
  `allow_history_change` を有効にして手動実行する）。
- **月次確定の自動化（DDR実装手順S1）で反映された `inputs/*.json` を戻す**: data repo側で
  `inputs/official_buy.json`/`inputs/official_sell.json`/`inputs/tariff_months.json` の
  変更を `git revert` して push する（G19の突合が壊れる場合は `--allow-history-change` が
  必要になることがある）。handoff側（`/var/lib/energy-fetch/handoff/`、private）の生成物が
  誤っている場合は、そちらの停止・修正が根本対応になる（本リポジトリの対象外）。

## 6. 月次作業

請求 PDF・売電実績を取り込んだら、Mac上で
`import_official_buy.py` / `import_official_sell.py` を実行して
`data/metrics/official_buy.json` / `official_sell.json` を更新し、
`scripts/blog-metrics/deploy-homelab.sh --service-user <homelabの実行ユーザー名>` を
再実行して homelab の `/opt/blog-metrics/inputs/` に反映する（aggregate.sh はこれらを
再生成せず、bundle に同梱されたものをそのまま使うため、再配備しない限り古いまま）。

**この月次作業が月締めレポート記事公開のトリガになる**（設計判断2026-09-23/26「速報＋改訂」
方式）: 請求書・検針値を取り込んで単価を確定すると、`monthly_report.py` の確定条件
(`is_closable`: L0〜L3が available、L3の買電が請求書実額(`buy_source=="billed"`)、
売電が検針値(`sell_source=="official_meter"`)、L2の不確かさ帯がある、請求月が
`FIRST_REPORT_BILLING_MONTH`以降)を満たした月から、次回の `run-daily.sh` 実行で
`posts/YYYY-MM.json` が確定版(`stage: final`)として作成・改版される。確定前に請求期間が
終了済みの月は、暫定単価による速報(`stage: preliminary`)が先に一度だけ公開される場合がある
（速報は初回公開後、確定するまで数値を更新しない）。記事の文章はすべて main 側の
`render_monthly_posts.py` が生成するため、homelab を再配備しても記事の文言は変わらない。

`monthly_report.py` を変更したら、main へのマージ直後に
`scripts/blog-metrics/deploy-homelab.sh` を実行して homelab の bundle に反映すること
（`run-daily.sh` は bundle 内の `monthly_report.py` を呼ぶため、bundle と main がずれると
`validate_metrics.py` の G15 が失敗し、サイト全体のデプロイが止まる）。

`tariff.json` に新しい請求月の単価を追加してから配備する場合も同じコマンドでよい。
`deploy-homelab.sh` が配備前後で `tariff.json` のハッシュを比較し、変更を検出したときだけ
homelab 側に `/var/lib/blog-metrics/allow-history-once` フラグを
`sudo -u <homelabの実行ユーザー名>` で置く（`blog-metrics.service` の
`StateDirectory=blog-metrics` が作る `/var/lib/blog-metrics` と同じ場所。run-daily.sh
自身も systemd 経由では `--state-dir /var/lib/blog-metrics` で起動するため一致する）。
これにより次回の `run-daily.sh` 実行では、それまで暫定単価を使っていた過去日の値が
確定値に変わっても `validate_metrics.py` の G9(履歴不変性)に拒否されず1回だけ通過する
（フラグは使用後に自動的に消費・削除される）。

`publish_since`（公開範囲フィルタ、オーナー決定2026-09-23）を初めて導入してデプロイする回だけは、
`daily.json` の行数が意図的に減るため G8(行数減少禁止)にも拒否される。`${STATE_DIR}/allow-history-once`
フラグ（上記と同じ仕組み。`sudo -u <homelabの実行ユーザー名> touch /var/lib/blog-metrics/allow-history-once`）
を該当デプロイの直前に1回だけ手動で置き、`--allow-history-change` を1回だけ適用すること
（以後は `publish_since` が動かないため再発しない一度限りの移行措置）。

### 月次確定の自動化（DDR実装手順S1、任意）

料金体系の骨格（段階単価・賦課金の年度レンジ・売電単価・`meter_read_day`）は引き続き
`scripts/blog-metrics/tariff.json`（本リポジトリ側、手動更新）が正になる。毎月観測する値
（燃料費等調整単価・容量拠出金・賦課金の観測値）と `official_buy.json`/`official_sell.json`
は `~/solar-metrics-data` の `inputs/` に置かれ、`run-daily.sh` の `stage_inputs` が
反映する。反映経路は2つある:

1. **自動（別プロセス、private、本リポジトリの対象外）**: `--auto-inputs-dir`
   （既定 `/var/lib/energy-fetch/handoff`）に `official_buy.json`/`official_sell.json`/
   `tariff_months.json` が置かれていれば、`run-daily.sh` が毎回 `validate_metrics.py
   --check-inputs-dir`（G2/G3/G6/G7/G18/G19）で検査し、合格したファイルだけ
   `~/solar-metrics-data/inputs/` へ反映する。不合格なら既存の `inputs/` を維持したまま
   データのcommit・pushは続行し、run全体は失敗扱い（`run.log` に理由が残り、3暦日連続で
   LINE通知の対象になる）。G19（突合）は、`tariff_months.json` が `tariff.json` に対して
   **新たに確定させた**請求月について、`official_buy.json` に対応する月があり
   `reconcile_bill()==0` であることを必須にする（無ければ fatal）。`tariff.json` 側で
   既に確定済みの月にはこの必須要件は適用されない。
2. **手動（従来どおり、移行措置）**: 上記1のファイルが1つも無い状態が続く限り、Mac上で
   `import_official_buy.py`/`import_official_sell.py` を実行して
   `data/metrics/official_buy.json`/`official_sell.json` を更新し、
   `scripts/blog-metrics/deploy-homelab.sh --service-user <homelabの実行ユーザー名>` で
   `/opt/blog-metrics/inputs/` に反映する。`run-daily.sh` は `~/solar-metrics-data/inputs/`
   に `official_buy.json`/`official_sell.json` がまだ無ければ、この bundle 同梱版を初期値
   として1回だけコピーする（`tariff_months.json` に対応する手動運用は無いため、これは
   `tariff.json` 自体の手動更新で代替する）。

`tariff.json`（骨格）に新しい請求月の単価を直接追記する運用（`deploy-homelab.sh` による
`allow-history-once` フラグ、上記参照）は変わらない。**「両方には登録しない」が原則**:
`import_official_inputs.py`（自動取り込み側）は、`tariff.json` で既に確定している請求月
（fuel と capacity の両方がある月）を `inputs/tariff_months.json` に二重登録しない
（`official_buy.json` へは従来どおり登録する）。

それでも `tariff.json` と `inputs/tariff_months.json` の両方に同じ請求月の値があり、かつ
値が食い違う場合（例: `tariff.json` を手動更新した後、古い `inputs/tariff_months.json`
がまだ残っている等）は、`run-daily.sh` の `build_effective_tariff` が
`bill_model.tariff_conflicts()` で衝突箇所を特定し、**衝突した月だけを
`~/solar-metrics-data/inputs/tariff_months.json` から自動的に取り除いて push する**
（`excluded_months[月] = "tariff_conflict"` として記録。他の月・`official_buy.json`・
`official_sell.json` は変更しない）。除去後の内容で実効tariffを作り直してデータの
commit・pushは通常どおり続行しつつ、その回の `run-daily.sh` はrun全体としては失敗扱いに
なる（`run.log` に除去した月が記録され、3暦日連続で LINE通知の対象になる。
`allow-history-once` フラグの手動操作は不要）。

**S1 導入時の配備順序は §1 末尾の注意を必ず参照すること**（main へのマージ → 
`deploy-homelab.sh` の順を守らないと、初回実行の移行措置的な `inputs/` 追加が旧
`validate_metrics.py` の G1 に拒否されて Pages のデプロイが止まる）。
