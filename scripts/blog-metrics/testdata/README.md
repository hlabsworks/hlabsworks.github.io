# testdata

このディレクトリには時間帯粒度（5分単位）の実データは置かない。

- 経緯: 当初 `profile_20260901.csv`（2026-09-01の1日分・実データ288行）を golden day
  テスト用フィクスチャとして同梱していたが、QAレビューで「時間帯粒度の実データは
  公開repoに置けない」（設計書 `docs/design/20260905_layer-model-ddr.md` §6 却下案4、
  オーナー決定）との指摘を受け削除した（2026-09-06）。
- golden day テスト（`test_layer_model.py` の `GoldenDaySyntheticTest`）は、実データの
  代わりに `test_layer_model.py` 内で **合成**した滑らかなパラメトリック曲線のプロファイルを
  使う。12個のスナップショット時刻（00:00, 03:00, 06:00, 07:25, 09:00, 10:00, 11:00,
  12:10, 12:20, 15:00, 18:00, 21:00）の値は 2026-09-01 の実測値（DDR §0検証の要点、
  `docs/design/20260905_layer-model-ddr.md`）をそのまま採用し、残り276バケットはこれらの
  実測アンカー点を線形補間して合成する。時間帯粒度データを含まず、恒等式・蓄電池モデルの
  正しさを検証する目的に限定する。
- 実データでの golden day 検証（`GoldenDayRealDataOptInTest`）は、
  `~/Develop/energy-archive/solarchgctl/profile_5min/2026-08-27_to_now.csv`（private repo
  `hlabsworks/energy-archive`）がローカルに存在する場合のみ実行される opt-in テストにした
  （`unittest.skipUnless(os.path.exists(...))`）。このファイル自体は本repoにはコミットしない。
