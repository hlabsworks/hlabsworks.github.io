# testdata

**5分粒度・時間帯別の実測値は公開repoに一切置かない。実データ検証は
`~/Develop/energy-archive`（private repo）参照の opt-in テストのみで行う。**

- 経緯: 当初 `profile_20260901.csv`（2026-09-01の1日分・実データ288行）を golden day
  テスト用フィクスチャとして同梱していたが、QAレビューで「時間帯粒度の実データは
  公開repoに置けない」（設計書（非公開） §6 却下案4、
  オーナー決定）との指摘を受け削除した（2026-09-06）。
- golden day テスト（`test_layer_model.py` の `GoldenDaySyntheticTest`）は、実データの
  代わりに `test_layer_model.py` 内で**合成**した滑らかなパラメトリック曲線のプロファイルを
  使う。12個のアンカー時刻（00:00, 03:00, 06:00, 07:25, 09:00, 10:00, 11:00, 12:10, 12:20,
  15:00, 18:00, 21:00）の値は本テストのために考案した合成値であり、実測値は一切含まない
  （2026-09-06の再QAで、当初使っていた実測値ベースのアンカーを合成値に置き換えた）。
  各アンカー時刻の期待復元負荷は恒等式（DDR §1）から独立に算出し（`expected_load_at_anchor()`、
  `Bucket.load_true_w()` を呼ばず素の四則演算で再実装）、ハードコードされた実測magnitudeに
  一切依存しない。残り276バケットはこれらの合成アンカー点を線形補間して合成する。
- 実データでの golden day 検証（`GoldenDayRealDataOptInTest`）は、
  `~/Develop/energy-archive/solarchgctl/profile_5min/2026-08-27_to_now.csv`（private repo
  `hlabsworks/energy-archive`）がローカルに存在する場合のみ実行される opt-in テストにした
  （`unittest.skipUnless(...)`）。このファイル自体は本repoにはコミットしない。このテストも
  実測の瞬時値(W)を本ファイルに書かず、恒等式の独立再実装との一致のみを確認する
  （具体的な実測magnitudeをソースコードに残さないため）。日積算kWh（時間帯粒度を持たない
  暦日合計値）のみ、既知の値との比較に用いる。
- `daily_legacy.json` は `test_validate_metrics.py` の
  `test_old_shaped_daily_json_is_rejected_by_gate4`（廃止済みの `consumption_kwh`・
  `surplus_kwh` キーを含む旧スキーマを validate_metrics.py の G4 が拒否することの回帰テスト）
  専用の静的フィクスチャ。以前は `data/metrics/daily.json`（実リポジトリの現物）をそのまま
  読んでいたが、`.github/workflows/hugo.yml` の `Sync incoming metrics data` ステップが
  homelab 由来の新スキーマで上書きするため、CI 実行順序次第でテストの前提（現物が旧
  スキーマのままであること）が崩れていた（QA指摘F1）。本フィクスチャは値そのものは
  `data/metrics/daily.json` の初期2行と同一（公開済みの集計値のみで秘匿情報は含まない）だが、
  ワークフローの実行順序やリポジトリの実データ更新に依存しない独立ファイルとして固定する。
- `{meta,bills,layers}_fixture.json` は `test_validate_metrics.py` の `_load_real()`
  （`data/metrics/{meta,bills,layers}.json` の代わりに読む静的フィクスチャ、QA指摘#8）。
  `generate_layer_bill_fixtures.py` が、`test_layer_model.py` の
  `build_synthetic_golden_day_buckets()`（実測値を含まない合成ゴールデンデイパターン）を
  2026-07-02〜2026-08-01（請求月2026-08、tariff.json で確定単価済み）の全31日に敷き詰めて
  100%usable(coverage=1.0、"full")な確定月を1つ作り、`layer_model.py`/`bill_model.py` を
  実際に実行してその出力をそのまま保存したもの。手で書いた辞書ではなく実走出力を使うのは、
  `data/metrics/layers.json`（実リポジトリの現物）が「全月unavailable」の退化スナップショット
  だったため、確定月available(full/scaled)でしか出ない正常系キー10個
  （`billing_months`/`days_total`/`days_usable`/`estimation`/`includes_scaled_months`/
  `net_cost_fit_yen_max`/`net_cost_fit_yen_min`/`note`/`saving_yen_fit`/`scale_factor`）が
  `validate_metrics.py` の `LAYERS_KEYS` allowlist から漏れていたため（QA指摘#1）。
  再生成: `cd scripts/blog-metrics && python3 testdata/generate_layer_bill_fixtures.py`
  （決定的に同じ内容が出力される。差分が出た場合は layer_model.py/bill_model.py の出力
  スキーマが変わった証拠なので、`validate_metrics.py` の allowlist も追随させること）。
- 設計判断（2026-09-23/26、「速報＋改訂」方式）: `generate_layer_bill_fixtures.py` は
  2026-08-02〜2026-09-01（請求月2026-09、tariff.json では単価未確定）にも合成golden dayを
  敷き詰め、`layers_fixture.json` の `preliminary_months[]` に1件（`tariff_provisional: true`、
  `tariff_source_month: "2026-08"`）を作る。`test_monthly_report.py`/
  `test_render_monthly_posts.py`/ダッシュボードの「速報」バッジ描画確認・
  `validate_metrics.py` の G4(POST_KEYS収穫元とは別枠、LAYERS_KEYSにpreliminary_monthsを
  含めるための実データ確認)に使う。この期間は本リポジトリ実物の
  `data/metrics/official_sell.json`（2026-09分の検針値。既に届いている）を使うため
  `sell_source: "official_meter"` になるが、`data/metrics/official_buy.json` に2026-09分は
  まだ無いため `buy_source: "sensor"`（請求書未着）のまま。買電が未確定・売電だけ確定という
  現実の状態をそのまま表しており、確定条件(is_closable、buy_source=="billed"必須)を満たさず
  速報のままになる。
- 月次確定の自動化（DDR実装手順S1）用の `inputs/official_buy.json`/`official_sell.json`/
  `tariff_months.json` フィクスチャは、静的ファイルとして本ディレクトリには置かず
  `test_validate_metrics.py`（`make_official_buy_fixture()`等）・
  `test_import_official_inputs.py`・`tests/run-daily-test.sh`（`setup_handoff_dir()`）の
  各テストファイル内で合成する。値はすべて `bill_model.compute_bill()`/`merge_tariff()` を
  実際に呼んで手計算せずに算出し、実際の請求額・供給地点番号・顧客番号等の私有データは
  一切含まない（既存の合成golden dayフィクスチャと同じ方針）。`run-daily-test.sh` の
  handoff系シナリオ（21〜23）は `stage_inputs`（--auto-inputs-dir）の正常・不正・未設置の
  3経路を検証する。
