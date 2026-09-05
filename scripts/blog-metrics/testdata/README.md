# testdata

**5分粒度・時間帯別の実測値は公開repoに一切置かない。実データ検証は
`~/Develop/energy-archive`（private repo）参照の opt-in テストのみで行う。**

- 経緯: 当初 `profile_20260901.csv`（2026-09-01の1日分・実データ288行）を golden day
  テスト用フィクスチャとして同梱していたが、QAレビューで「時間帯粒度の実データは
  公開repoに置けない」（設計書 `docs/design/20260905_layer-model-ddr.md` §6 却下案4、
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
