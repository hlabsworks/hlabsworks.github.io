#!/usr/bin/env python3
"""validate_metrics.py の unittest。

実行方法:
  cd scripts/blog-metrics && python3 -m unittest test_validate_metrics -v

現行5ファイルのうち meta/bills/layers は scripts/blog-metrics/testdata/ の静的フィクスチャ
（{meta,bills,layers}_fixture.json）を、daily/monthly は新スキーマの合成フィクスチャを使い、
全ゲートを通過することを回帰の土台にし、各ゲートの失敗経路を1つずつ確認する。

QA指摘#8: 以前は data/metrics/{meta,bills,layers}.json（リポジトリ実ファイル）を直接
読んでいたが、(a) .github/workflows/hugo.yml の Sync incoming ステップの実行順序に
テストが依存してしまう（F1と同種の問題）、(b) data/metrics/layers.json が「全月
unavailable」の退化スナップショットだったため確定月available(full/scaled)の正常系キーが
G4のallowlistから漏れていた（QA指摘#1）、という2つの問題があった。testdata/ 配下の
フィクスチャは layer_model.py/bill_model.py を合成プロファイル（実測値・時間帯粒度を
含まない）で実走させた出力を静的に保存したもので、生成手順は
testdata/README.md 参照。
"""
from __future__ import annotations

import copy
import hashlib
import json
import shutil
import subprocess
import sys
import tempfile
import unittest
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import monthly_report  # noqa: E402
import validate_metrics  # noqa: E402

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
TESTDATA_DIR = Path(__file__).resolve().parent / "testdata"
JST = timezone(timedelta(hours=9))

# QA指摘#8: meta/bills/layers はリポジトリ実ファイルではなく静的フィクスチャから読む
# （生成手順は testdata/README.md 参照。CIのステップ順序や data/metrics/ の現在の内容に
# 依存しないようにするため）。
_FIXTURE_FILES = {
    "data/metrics/meta.json": "meta_fixture.json",
    "data/metrics/bills.json": "bills_fixture.json",
    "data/metrics/layers.json": "layers_fixture.json",
}


def _load_real(relpath: str) -> dict:
    fixture_name = _FIXTURE_FILES.get(relpath)
    if fixture_name is None:
        raise KeyError(f"_load_real: 静的フィクスチャが無い相対パスです: {relpath}")
    return json.loads((TESTDATA_DIR / fixture_name).read_text(encoding="utf-8"))


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def make_daily_fixture() -> list[dict]:
    return [
        {
            "date": "2026-09-20",
            "solar_kwh": 20.0,
            "buy_kwh": 0.0,
            "sell_kwh": 5.0,
            "nichicon_charge_kwh": None,
            "ecoflow_charge_kwh": None,
            "self_consumption_shift_kwh": None,
            "saving_yen": 500,
        },
        {
            "date": "2026-09-21",
            "solar_kwh": 18.0,
            "buy_kwh": 0.5,
            "sell_kwh": 4.0,
            "nichicon_charge_kwh": None,
            "ecoflow_charge_kwh": None,
            "self_consumption_shift_kwh": None,
            "saving_yen": 450,
        },
        {
            "date": "2026-09-22",
            "solar_kwh": 22.0,
            "buy_kwh": 0.0,
            "sell_kwh": 6.0,
            "nichicon_charge_kwh": None,
            "ecoflow_charge_kwh": None,
            "self_consumption_shift_kwh": None,
            "saving_yen": 550,
        },
    ]


def make_monthly_fixture(daily: list[dict]) -> list[dict]:
    total_solar = round(sum(r["solar_kwh"] for r in daily), 3)
    total_buy = round(sum(r["buy_kwh"] for r in daily), 3)
    total_sell = round(sum(r["sell_kwh"] for r in daily), 3)
    total_saving = sum(r["saving_yen"] for r in daily)
    return [
        {
            "month": "2026-09",
            "solar_kwh": total_solar,
            "buy_kwh": total_buy,
            "sell_kwh": total_sell,
            "nichicon_charge_kwh": None,
            "ecoflow_charge_kwh": None,
            "self_consumption_shift_kwh": None,
            "saving_yen": total_saving,
        }
    ]


def make_pipeline_fixture(generated_at: str | None = None) -> dict:
    if generated_at is None:
        generated_at = datetime.now(JST).strftime("%Y-%m-%d %H:%M:%S")
    return {
        "schema_version": 1,
        "generated_at": generated_at,
        "source": "homelab",
        "bundle_rev": "abc1234",
        "profile_window_days": 420,
        "inputs": {
            "tariff_sha256": _sha256(REPO_ROOT / "scripts" / "blog-metrics" / "tariff.json"),
            "official_buy_sha256": _sha256(REPO_ROOT / "data" / "metrics" / "official_buy.json"),
            "official_sell_sha256": _sha256(REPO_ROOT / "data" / "metrics" / "official_sell.json"),
        },
        "row_counts": {"daily": 3, "monthly": 1},
        "db_query_seconds": 4.2,
    }


def write_incoming(
    base: Path, *, daily=None, monthly=None, meta=None, bills=None, layers=None, pipeline=None, readme=None,
    posts: dict[str, dict] | None = None, inputs: dict[str, dict] | None = None,
) -> Path:
    """有効な incoming ディレクトリを作る（値渡しがあれば差し替え）。
    posts: {"2026-10.json": {...}} のように相対ファイル名をキーにした辞書（QA指摘に
    倣い任意）。inputs: {"official_buy.json": {...}} のように OPTIONAL_INPUT_FILES の
    ファイル名(拡張子込み)をキーにした辞書（月次確定の自動化、DDR実装手順S1、任意）。"""
    (base / "data" / "metrics").mkdir(parents=True, exist_ok=True)
    daily = make_daily_fixture() if daily is None else daily
    monthly = make_monthly_fixture(daily) if monthly is None else monthly
    meta = _load_real("data/metrics/meta.json") if meta is None else meta
    bills = _load_real("data/metrics/bills.json") if bills is None else bills
    layers = _load_real("data/metrics/layers.json") if layers is None else layers
    pipeline = make_pipeline_fixture() if pipeline is None else pipeline
    readme = "# solar-metrics-data\n\nhomelab が毎日生成する公開メトリクス。\n" if readme is None else readme

    (base / "data" / "metrics" / "daily.json").write_text(json.dumps(daily, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    (base / "data" / "metrics" / "monthly.json").write_text(json.dumps(monthly, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    (base / "data" / "metrics" / "meta.json").write_text(json.dumps(meta, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    (base / "data" / "metrics" / "bills.json").write_text(json.dumps(bills, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    (base / "data" / "metrics" / "layers.json").write_text(json.dumps(layers, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    (base / "pipeline.json").write_text(json.dumps(pipeline, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    (base / "README.md").write_text(readme, encoding="utf-8")
    if posts:
        (base / "posts").mkdir(parents=True, exist_ok=True)
        for name, content in posts.items():
            (base / "posts" / name).write_text(json.dumps(content, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    if inputs:
        (base / "inputs").mkdir(parents=True, exist_ok=True)
        for name, content in inputs.items():
            (base / "inputs" / name).write_text(json.dumps(content, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return base


# --- posts/YYYY-MM.json 用フィクスチャ(G14〜G16テスト用) ---------------------------------------
POST_BILLING_MONTH = "2026-10"  # monthly_report.FIRST_REPORT_BILLING_MONTH と同じ（実データ）
POST_METER_READ_DAY = 2  # 実際のtariff.jsonと同じ値
POST_USAGE_PERIOD = {"start": "2026-09-02", "end": "2026-10-01", "days": 30}  # billing_period("2026-10", 2)と一致させる


def _post_layer(buy_kwh: float, sell_kwh: float, net_fit: int, net_post_fit: int, extra: dict | None = None) -> dict:
    d = {"available": True, "buy_kwh": buy_kwh, "sell_kwh": sell_kwh, "net_cost_fit_yen": net_fit, "net_cost_post_fit_yen": net_post_fit}
    if extra:
        d.update(extra)
    return d


def make_post_month_record(*, l3_buy_source="billed", l3_sell_source="official_meter") -> dict:
    """monthly_report.build_snapshot_body に渡す layers.months[]/preliminary_months[] の
    1エントリ相当（DDR §A・§B）。値は本テストのために考案した合成値。"""
    common_extra = {"estimation": "full", "days_usable": 30, "days_total": 30}
    return {
        "billing_month": POST_BILLING_MONTH,
        "usage_period": dict(POST_USAGE_PERIOD),
        "layers": {
            "L0": _post_layer(500.0, 0.0, 10000, 9500, common_extra),
            "L1": _post_layer(300.0, 100.0, 6000, 5800, common_extra),
            "L2": _post_layer(150.0, 200.0, 3500, 4000, common_extra),
            "L3": _post_layer(160.0, 190.0, 3200, 3600, {"buy_source": l3_buy_source, "sell_source": l3_sell_source}),
        },
        "uncertainty": {"L2": {"net_cost_fit_yen_min": 3000, "net_cost_fit_yen_max": 4000}},
    }


def make_valid_post_fixture(
    *, stage: str = "final", first_published: str = "2026-10-24", revision: int = 1, revised: str | None = None,
    daily_by_date: dict | None = None, transitioned_from_preliminary: bool = False,
) -> tuple[dict, dict]:
    """gate15_post_recompute と完全一致するpostを、monthly_report.build_snapshot_body()を
    直接使って組み立てる（手計算による数値の食い違いを避けるため）。
    戻り値: (post, layers_json)。"""
    month_rec = make_post_month_record(l3_buy_source="billed" if stage == "final" else "sensor")
    if stage == "final":
        layers_json = {"months": [month_rec], "preliminary_months": []}
    else:
        layers_json = {"months": [], "preliminary_months": [month_rec]}
    daily_by_date = daily_by_date if daily_by_date is not None else {r["date"]: r for r in make_daily_fixture()}
    body = monthly_report.build_snapshot_body(POST_BILLING_MONTH, layers_json, daily_by_date, stage, POST_METER_READ_DAY)
    post = {
        **body, "first_published": first_published, "revision": revision, "revised": revised,
        "transitioned_from_preliminary": transitioned_from_preliminary,
    }
    return post, layers_json


class BaselineTest(unittest.TestCase):
    """現行の bills.json/layers.json/meta.json（実ファイル）+ 新スキーマ daily/monthly が
    全ゲートを通過すること（回帰の土台）。"""

    def test_current_files_pass_all_gates_without_warnings(self):
        with tempfile.TemporaryDirectory() as tmp:
            incoming = write_incoming(Path(tmp))
            warnings = validate_metrics.validate(incoming, REPO_ROOT, allow_history_change=False)
            self.assertEqual(warnings, [])

    def test_tariff_json_is_not_read_when_there_are_no_posts(self):
        # QA再指摘2026-09-26 R2: postsが0件ならtariff.jsonを読まない。存在しないパスを
        # tariff_sha256に渡すと、postsが1件でもあればjson.loads()がFileNotFoundErrorで
        # 例外になるはずだが、0件ならそのコードパス自体を通らないため例外にならない
        # （G13はreal_path.exists()==Falseなら静かにスキップする既存仕様）。
        with tempfile.TemporaryDirectory() as tmp:
            incoming = write_incoming(Path(tmp))
            bogus_input_paths = validate_metrics.default_input_paths(REPO_ROOT)
            bogus_input_paths["tariff_sha256"] = Path(tmp) / "does-not-exist.json"
            warnings = validate_metrics.validate(incoming, REPO_ROOT, allow_history_change=False, input_paths=bogus_input_paths)
            self.assertEqual(warnings, [])

    def test_old_shaped_daily_json_is_rejected_by_gate4(self):
        """旧スキーマ（consumption_kwh/surplus_kwh を含む）を混ぜると G4 で弾かれることを
        確認する（廃止キーの回帰防止）。QA指摘F1: 以前は data/metrics/daily.json の実ファイルを
        読んでいたが、hugo.yml の Sync incoming ステップが新スキーマで上書きするため、CI の
        実行順序次第で前提（実ファイルが旧スキーマのままであること）が崩れる。ワークフローの
        実行順序やリポジトリの実データ更新に依存しない静的フィクスチャ(testdata/)を使う。"""
        old_daily = json.loads((Path(__file__).resolve().parent / "testdata" / "daily_legacy.json").read_text(encoding="utf-8"))
        with tempfile.TemporaryDirectory() as tmp:
            incoming = write_incoming(Path(tmp), daily=old_daily)
            with self.assertRaises(validate_metrics.ValidationFailure) as ctx:
                validate_metrics.validate(incoming, REPO_ROOT, allow_history_change=False)
            self.assertEqual(ctx.exception.gate, "G4")

    def test_layers_json_with_available_month_passes_gate4(self):
        """QA指摘#1(最優先): data/metrics/layers.json が「全月unavailable」の退化
        スナップショットだったため、確定月available(full/scaled)の正常系キー10個
        (billing_months/days_total/days_usable/estimation/includes_scaled_months/
        net_cost_fit_yen_max/net_cost_fit_yen_min/note/saving_yen_fit/scale_factor)が
        G4のallowlist(LAYERS_KEYS)から漏れていた。layers_fixture.json/bills_fixture.json
        は layer_model.py/bill_model.py を合成プロファイルで実走させた出力（確定月
        full・in_progress・daily・excluded_months・cumulative を含む）であることをここで
        固定し、validate() が警告なしで通ることを確認する（BaselineTestと役割は重なるが、
        「退化スナップショットではない」という前提自体を明示的に検証する目的で分離する）。"""
        layers = _load_real("data/metrics/layers.json")
        bills = _load_real("data/metrics/bills.json")

        # 前提: 退化スナップショット(全月unavailable)ではなく、少なくとも1つの確定月で
        # L0-L3すべてavailable:trueであること(cumulativeが算出される条件と同じ)。
        available_months = [
            m for m in layers["months"]
            if all(m["layers"][layer]["available"] for layer in ("L0", "L1", "L2", "L3"))
        ]
        self.assertGreater(len(available_months), 0, "layers_fixture.json に available な確定月が1つも無い（退化スナップショットのままの疑い）")
        self.assertTrue(layers["cumulative"]["available"])
        self.assertGreater(len(layers["daily"]), 0)
        self.assertGreater(len(bills["months"]), 0)

        # DDR §7: L1S（太陽光＋SolarChargeController試算）が退化したままG4を素通りしていない
        # こと（build_self_consistent_golden_periodで生成した確定月はreplayゲートに合格する
        # はずなので、L1Sもavailable:trueで、検証用のl1s_replay/params.l1s_modelを持つ）。
        self.assertTrue(available_months[0]["layers"]["L1S"]["available"])
        self.assertIsNotNone(available_months[0].get("l1s_replay"))
        self.assertIn("l1s_model", layers["params"])

        with tempfile.TemporaryDirectory() as tmp:
            incoming = write_incoming(Path(tmp), bills=bills, layers=layers)
            warnings = validate_metrics.validate(incoming, REPO_ROOT, allow_history_change=False)
            self.assertEqual(warnings, [])


class Gate1Test(unittest.TestCase):
    def test_missing_required_file_fails(self):
        with tempfile.TemporaryDirectory() as tmp:
            incoming = write_incoming(Path(tmp))
            (incoming / "README.md").unlink()
            with self.assertRaises(validate_metrics.ValidationFailure) as ctx:
                validate_metrics.validate(incoming, REPO_ROOT, allow_history_change=False)
            self.assertEqual(ctx.exception.gate, "G1")

    def test_unexpected_extra_file_fails(self):
        with tempfile.TemporaryDirectory() as tmp:
            incoming = write_incoming(Path(tmp))
            (incoming / "extra.txt").write_text("not allowed\n", encoding="utf-8")
            with self.assertRaises(validate_metrics.ValidationFailure) as ctx:
                validate_metrics.validate(incoming, REPO_ROOT, allow_history_change=False)
            self.assertEqual(ctx.exception.gate, "G1")


class Gate2Test(unittest.TestCase):
    def test_bom_is_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            incoming = write_incoming(Path(tmp))
            path = incoming / "data" / "metrics" / "daily.json"
            path.write_bytes(b"\xef\xbb\xbf" + path.read_bytes())
            with self.assertRaises(validate_metrics.ValidationFailure) as ctx:
                validate_metrics.validate(incoming, REPO_ROOT, allow_history_change=False)
            self.assertEqual(ctx.exception.gate, "G2")

    def test_control_character_is_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            incoming = write_incoming(Path(tmp))
            path = incoming / "README.md"
            path.write_text("hello\x07world\n", encoding="utf-8")
            with self.assertRaises(validate_metrics.ValidationFailure) as ctx:
                validate_metrics.validate(incoming, REPO_ROOT, allow_history_change=False)
            self.assertEqual(ctx.exception.gate, "G2")

    def test_missing_trailing_newline_is_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            incoming = write_incoming(Path(tmp))
            path = incoming / "README.md"
            path.write_text("no trailing newline", encoding="utf-8")
            with self.assertRaises(validate_metrics.ValidationFailure) as ctx:
                validate_metrics.validate(incoming, REPO_ROOT, allow_history_change=False)
            self.assertEqual(ctx.exception.gate, "G2")

    def test_invalid_json_is_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            incoming = write_incoming(Path(tmp))
            path = incoming / "data" / "metrics" / "daily.json"
            path.write_text("{not valid json\n", encoding="utf-8")
            with self.assertRaises(validate_metrics.ValidationFailure) as ctx:
                validate_metrics.validate(incoming, REPO_ROOT, allow_history_change=False)
            self.assertEqual(ctx.exception.gate, "G2")


class Gate3Test(unittest.TestCase):
    def test_oversized_file_is_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            incoming = write_incoming(Path(tmp))
            path = incoming / "README.md"
            path.write_text(("x" * (validate_metrics.MAX_FILE_BYTES + 1)) + "\n", encoding="utf-8")
            with self.assertRaises(validate_metrics.ValidationFailure) as ctx:
                validate_metrics.validate(incoming, REPO_ROOT, allow_history_change=False)
            self.assertEqual(ctx.exception.gate, "G3")

    def test_daily_row_count_over_limit_is_rejected(self):
        daily = []
        base_row = make_daily_fixture()[0]
        for i in range(validate_metrics.MAX_DAILY_ROWS + 1):
            row = dict(base_row)
            row["date"] = (datetime(2020, 1, 1) + timedelta(days=i)).strftime("%Y-%m-%d")
            daily.append(row)
        with tempfile.TemporaryDirectory() as tmp:
            incoming = write_incoming(Path(tmp), daily=daily, monthly=[])
            with self.assertRaises(validate_metrics.ValidationFailure) as ctx:
                validate_metrics.validate(incoming, REPO_ROOT, allow_history_change=False)
            self.assertEqual(ctx.exception.gate, "G3")


class Gate4Test(unittest.TestCase):
    def test_unknown_daily_row_key_is_rejected(self):
        daily = make_daily_fixture()
        daily[0]["device_serial"] = "XYZ123"
        with tempfile.TemporaryDirectory() as tmp:
            incoming = write_incoming(Path(tmp), daily=daily)
            with self.assertRaises(validate_metrics.ValidationFailure) as ctx:
                validate_metrics.validate(incoming, REPO_ROOT, allow_history_change=False)
            self.assertEqual(ctx.exception.gate, "G4")

    def test_unknown_bills_key_is_rejected(self):
        bills = _load_real("data/metrics/bills.json")
        bills["unexpected_field"] = "x"
        with tempfile.TemporaryDirectory() as tmp:
            incoming = write_incoming(Path(tmp), bills=bills)
            with self.assertRaises(validate_metrics.ValidationFailure) as ctx:
                validate_metrics.validate(incoming, REPO_ROOT, allow_history_change=False)
            self.assertEqual(ctx.exception.gate, "G4")

    def test_unknown_pipeline_key_is_rejected(self):
        # QA指摘F12: pipeline.json にもキーallowlist(PIPELINE_KEYS)を適用する。
        pipeline = make_pipeline_fixture()
        pipeline["unexpected_field"] = "x"
        with tempfile.TemporaryDirectory() as tmp:
            incoming = write_incoming(Path(tmp), pipeline=pipeline)
            with self.assertRaises(validate_metrics.ValidationFailure) as ctx:
                validate_metrics.validate(incoming, REPO_ROOT, allow_history_change=False)
            self.assertEqual(ctx.exception.gate, "G4")


class Gate5Test(unittest.TestCase):
    def test_bad_date_format_is_rejected(self):
        daily = make_daily_fixture()
        daily[0]["date"] = "2026/09/20"
        with tempfile.TemporaryDirectory() as tmp:
            incoming = write_incoming(Path(tmp), daily=daily)
            with self.assertRaises(validate_metrics.ValidationFailure) as ctx:
                validate_metrics.validate(incoming, REPO_ROOT, allow_history_change=False)
            self.assertEqual(ctx.exception.gate, "G5")

    def test_bad_generated_at_format_is_rejected(self):
        meta = _load_real("data/metrics/meta.json")
        meta["generated_at"] = "2026-09-20T23:05:48"
        with tempfile.TemporaryDirectory() as tmp:
            incoming = write_incoming(Path(tmp), meta=meta)
            with self.assertRaises(validate_metrics.ValidationFailure) as ctx:
                validate_metrics.validate(incoming, REPO_ROOT, allow_history_change=False)
            self.assertEqual(ctx.exception.gate, "G5")


class Gate6Test(unittest.TestCase):
    def test_secret_like_string_is_rejected(self):
        meta = _load_real("data/metrics/meta.json")
        meta["buy_sell_price_source"] = "api_key=sk-abcdefg1234567890"
        with tempfile.TemporaryDirectory() as tmp:
            incoming = write_incoming(Path(tmp), meta=meta)
            with self.assertRaises(validate_metrics.ValidationFailure) as ctx:
                validate_metrics.validate(incoming, REPO_ROOT, allow_history_change=False)
            self.assertEqual(ctx.exception.gate, "G6")

    def test_private_ip_is_rejected(self):
        # QA指摘F7: 公開repoに実運用環境のサブネット表記や実ユーザー名を一切残さない運用のため、
        # 10/8側のRFC1918プライベートレンジを使う（ゲートの検知対象がRFC1918全般であることの
        # 確認が目的で、特定のプレフィクスへの一致は不要）。
        meta = _load_real("data/metrics/meta.json")
        meta["buy_sell_price_source"] = "collected from 10.0.0.55"
        with tempfile.TemporaryDirectory() as tmp:
            incoming = write_incoming(Path(tmp), meta=meta)
            with self.assertRaises(validate_metrics.ValidationFailure) as ctx:
                validate_metrics.validate(incoming, REPO_ROOT, allow_history_change=False)
            self.assertEqual(ctx.exception.gate, "G6")

    def test_test_net_ip_is_also_rejected(self):
        # QA指摘F7: RFC1918の私設IPだけでなく、IANA予約のドキュメント用範囲(TEST-NET-1等)も
        # 「IPアドレスらしき文字列」として検知する（実IPの代わりに使うべき値を、実運用データに
        # うっかり残した事故に気づけるようにするため）。
        meta = _load_real("data/metrics/meta.json")
        meta["buy_sell_price_source"] = "example host 192.0.2.14"
        with tempfile.TemporaryDirectory() as tmp:
            incoming = write_incoming(Path(tmp), meta=meta)
            with self.assertRaises(validate_metrics.ValidationFailure) as ctx:
                validate_metrics.validate(incoming, REPO_ROOT, allow_history_change=False)
            self.assertEqual(ctx.exception.gate, "G6")

    def test_mac_address_is_rejected(self):
        meta = _load_real("data/metrics/meta.json")
        meta["buy_sell_price_source"] = "device mac aa:bb:cc:dd:ee:ff observed"
        with tempfile.TemporaryDirectory() as tmp:
            incoming = write_incoming(Path(tmp), meta=meta)
            with self.assertRaises(validate_metrics.ValidationFailure) as ctx:
                validate_metrics.validate(incoming, REPO_ROOT, allow_history_change=False)
            self.assertEqual(ctx.exception.gate, "G6")

    def test_13_or_more_digit_number_is_rejected(self):
        meta = _load_real("data/metrics/meta.json")
        meta["buy_sell_price_source"] = "contract no 1234567890123 on file"
        with tempfile.TemporaryDirectory() as tmp:
            incoming = write_incoming(Path(tmp), meta=meta)
            with self.assertRaises(validate_metrics.ValidationFailure) as ctx:
                validate_metrics.validate(incoming, REPO_ROOT, allow_history_change=False)
            self.assertEqual(ctx.exception.gate, "G6")

    def test_serial_number_like_string_is_rejected(self):
        meta = _load_real("data/metrics/meta.json")
        meta["buy_sell_price_source"] = "device serial ABCDEFGHIJ12 attached"
        with tempfile.TemporaryDirectory() as tmp:
            incoming = write_incoming(Path(tmp), meta=meta)
            with self.assertRaises(validate_metrics.ValidationFailure) as ctx:
                validate_metrics.validate(incoming, REPO_ROOT, allow_history_change=False)
            self.assertEqual(ctx.exception.gate, "G6")

    def test_email_address_is_rejected(self):
        meta = _load_real("data/metrics/meta.json")
        meta["buy_sell_price_source"] = "contact owner@example.com for details"
        with tempfile.TemporaryDirectory() as tmp:
            incoming = write_incoming(Path(tmp), meta=meta)
            with self.assertRaises(validate_metrics.ValidationFailure) as ctx:
                validate_metrics.validate(incoming, REPO_ROOT, allow_history_change=False)
            self.assertEqual(ctx.exception.gate, "G6")

    def test_address_like_string_is_rejected(self):
        meta = _load_real("data/metrics/meta.json")
        meta["buy_sell_price_source"] = "愛知県名古屋市中区1丁目"
        with tempfile.TemporaryDirectory() as tmp:
            incoming = write_incoming(Path(tmp), meta=meta)
            with self.assertRaises(validate_metrics.ValidationFailure) as ctx:
                validate_metrics.validate(incoming, REPO_ROOT, allow_history_change=False)
            self.assertEqual(ctx.exception.gate, "G6")

    def test_common_japanese_words_do_not_false_positive(self):
        # QA指摘F11: 「都市ガス」「京都市」のような一般語が住所様パターンに誤検知しないこと。
        for phrase in ("都市ガスの契約時に確認", "京都市内のイベントで発表", "市場価格を参照"):
            meta = _load_real("data/metrics/meta.json")
            meta["buy_sell_price_source"] = phrase
            with tempfile.TemporaryDirectory() as tmp:
                incoming = write_incoming(Path(tmp), meta=meta)
                try:
                    validate_metrics.validate(incoming, REPO_ROOT, allow_history_change=False)
                except validate_metrics.ValidationFailure as exc:
                    self.fail(f"{phrase!r} が誤って {exc.gate} で拒否された: {exc.message}")

    def test_retailer_or_grid_operator_name_is_rejected(self):
        # オーナー決定2026-09-23: 居住地域の推定材料になる小売電気事業者名・送配電会社名・
        # プラン名は公開データに一切含めない。混入を検知するdenyパターンの回帰テスト。
        for phrase in ("東京電力パワーグリッドの実績", "TEPCOの公式メーター", "tepcoのAPI",
                       "Japan電力の請求明細", "japaden.jpを参照", "くらしプランSの単価",
                       "関西電力送配電のメーター", "九州電力の従量電灯B", "楽天でんきの請求",
                       "オクトパスエナジーのプラン", "はぴeタイムRの単価"):
            meta = _load_real("data/metrics/meta.json")
            meta["buy_sell_price_source"] = phrase
            with tempfile.TemporaryDirectory() as tmp:
                incoming = write_incoming(Path(tmp), meta=meta)
                with self.assertRaises(validate_metrics.ValidationFailure) as ctx:
                    validate_metrics.validate(incoming, REPO_ROOT, allow_history_change=False)
                self.assertEqual(ctx.exception.gate, "G6", f"{phrase!r} で期待したG6拒否にならなかった")


class Gate7Test(unittest.TestCase):
    def test_time_of_day_in_non_generated_at_field_is_rejected(self):
        meta = _load_real("data/metrics/meta.json")
        meta["buy_sell_price_source"] = "単価は毎日23:05に更新"
        with tempfile.TemporaryDirectory() as tmp:
            incoming = write_incoming(Path(tmp), meta=meta)
            with self.assertRaises(validate_metrics.ValidationFailure) as ctx:
                validate_metrics.validate(incoming, REPO_ROOT, allow_history_change=False)
            self.assertEqual(ctx.exception.gate, "G7")

    def test_generated_at_itself_is_exempt(self):
        # generated_at 自体は HH:MM:SS を含むが例外的に許可される（baseline が通ることで確認）
        with tempfile.TemporaryDirectory() as tmp:
            incoming = write_incoming(Path(tmp))
            validate_metrics.validate(incoming, REPO_ROOT, allow_history_change=False)

    def test_time_of_day_in_key_name_is_rejected(self):
        # 値だけでなくキー名自体に時間帯粒度らしき文字列が含まれる場合も検知する
        # （QA指摘F10: G4のキーallowlistが先に弾いてしまいG7のキー名分岐がvalidate()経由の
        # テストでは到達できず「ミューテーションで生存」していたため、gate7_no_time_of_day を
        # 直接呼んでこの分岐だけを単体テストする）。
        data = {"23:05": "value"}
        with self.assertRaises(validate_metrics.ValidationFailure) as ctx:
            validate_metrics.gate7_no_time_of_day(data, "data/metrics/meta.json")
        self.assertEqual(ctx.exception.gate, "G7")


class Gate8Test(unittest.TestCase):
    def test_non_ascending_dates_are_rejected(self):
        daily = make_daily_fixture()
        daily[0], daily[1] = daily[1], daily[0]
        with tempfile.TemporaryDirectory() as tmp:
            incoming = write_incoming(Path(tmp), daily=daily)
            with self.assertRaises(validate_metrics.ValidationFailure) as ctx:
                validate_metrics.validate(incoming, REPO_ROOT, allow_history_change=False)
            self.assertEqual(ctx.exception.gate, "G8")

    def test_duplicate_dates_are_rejected(self):
        daily = make_daily_fixture()
        daily[1]["date"] = daily[0]["date"]
        daily.sort(key=lambda r: r["date"])
        with tempfile.TemporaryDirectory() as tmp:
            incoming = write_incoming(Path(tmp), daily=daily)
            with self.assertRaises(validate_metrics.ValidationFailure) as ctx:
                validate_metrics.validate(incoming, REPO_ROOT, allow_history_change=False)
            self.assertEqual(ctx.exception.gate, "G8")

    def test_future_date_is_rejected(self):
        daily = make_daily_fixture()
        future = (datetime.now(JST) + timedelta(days=3)).strftime("%Y-%m-%d")
        daily.append(dict(daily[-1], date=future))
        with tempfile.TemporaryDirectory() as tmp:
            incoming = write_incoming(Path(tmp), daily=daily)
            with self.assertRaises(validate_metrics.ValidationFailure) as ctx:
                validate_metrics.validate(incoming, REPO_ROOT, allow_history_change=False)
            self.assertEqual(ctx.exception.gate, "G8")

    def test_last_date_regression_against_previous_commit_is_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            incoming = _git_repo_with_two_commits(
                Path(tmp),
                old_daily=make_daily_fixture(),
                new_daily=make_daily_fixture()[:1],  # 最終日が過去に後退
            )
            with self.assertRaises(validate_metrics.ValidationFailure) as ctx:
                validate_metrics.validate(incoming, REPO_ROOT, allow_history_change=False)
            self.assertEqual(ctx.exception.gate, "G8")

    def test_broken_previous_commit_falls_back_to_older_parseable_commit(self):
        # N2 負テスト(2026-09-26)で発見: 壊れた JSON を push→revert した直後、HEAD~1 が
        # JSON として読めず validate_metrics.py が Traceback で落ちた。revert 後の正常データは
        # 読める最古の祖先(HEAD~2)と比較して通過しなければならない。
        daily = make_daily_fixture()
        with tempfile.TemporaryDirectory() as tmp:
            incoming = _git_repo_with_broken_middle_commit(Path(tmp), old_daily=daily, new_daily=daily)
            validate_metrics.validate(incoming, REPO_ROOT, allow_history_change=False)

    def test_broken_previous_commit_does_not_bypass_history_immutability(self):
        # 壊れたコミットを挟んでも、確定済み日の改変は HEAD~2 との比較で G9 拒否されること。
        old_daily = make_daily_fixture()
        for i, row in enumerate(old_daily):  # G9 の対象になる確定済み日(today-20日以前)にずらす
            row["date"] = f"2026-07-{i + 1:02d}"
        new_daily = copy.deepcopy(old_daily)
        new_daily[0]["solar_kwh"] = (new_daily[0]["solar_kwh"] or 0) + 1.0
        with tempfile.TemporaryDirectory() as tmp:
            incoming = _git_repo_with_broken_middle_commit(Path(tmp), old_daily=old_daily, new_daily=new_daily)
            with self.assertRaises(validate_metrics.ValidationFailure) as ctx:
                validate_metrics.validate(incoming, REPO_ROOT, allow_history_change=False)
            self.assertEqual(ctx.exception.gate, "G9")

    def test_row_count_decrease_against_previous_commit_is_rejected(self):
        # 最終日は維持したまま先頭の行だけ減らす（publish_since導入初回の移行と同型: 末尾は
        # 後退しないが行数は減る）。
        old_daily = make_daily_fixture()
        new_daily = old_daily[1:]  # 先頭日を1件落とす。最終日は同じ。
        with tempfile.TemporaryDirectory() as tmp:
            incoming = _git_repo_with_two_commits(Path(tmp), old_daily=old_daily, new_daily=new_daily)
            with self.assertRaises(validate_metrics.ValidationFailure) as ctx:
                validate_metrics.validate(incoming, REPO_ROOT, allow_history_change=False)
            self.assertEqual(ctx.exception.gate, "G8")

    def test_row_count_decrease_is_allowed_with_history_change_flag(self):
        # オーナー決定2026-09-23: publish_since導入の初回移行など、意図的な行数減少は
        # --allow-history-change（G9と同じフラグ）で1回だけ許可する。
        old_daily = make_daily_fixture()
        new_daily = old_daily[1:]
        with tempfile.TemporaryDirectory() as tmp:
            incoming = _git_repo_with_two_commits(Path(tmp), old_daily=old_daily, new_daily=new_daily)
            validate_metrics.validate(incoming, REPO_ROOT, allow_history_change=True)


class Gate9Test(unittest.TestCase):
    def test_changed_historic_value_is_rejected_without_flag(self):
        old_daily = _old_daily_fixture()
        new_daily = copy.deepcopy(old_daily)
        new_daily[0]["solar_kwh"] = 999.0  # 確定済み日の値を書き換え
        with tempfile.TemporaryDirectory() as tmp:
            incoming = _git_repo_with_two_commits(Path(tmp), old_daily=old_daily, new_daily=new_daily)
            with self.assertRaises(validate_metrics.ValidationFailure) as ctx:
                validate_metrics.validate(incoming, REPO_ROOT, allow_history_change=False)
            self.assertEqual(ctx.exception.gate, "G9")

    def test_changed_historic_value_is_allowed_with_flag(self):
        old_daily = _old_daily_fixture()
        new_daily = copy.deepcopy(old_daily)
        new_daily[0]["solar_kwh"] = 5.0
        with tempfile.TemporaryDirectory() as tmp:
            incoming = _git_repo_with_two_commits(Path(tmp), old_daily=old_daily, new_daily=new_daily)
            validate_metrics.validate(incoming, REPO_ROOT, allow_history_change=True)


class Gate10Test(unittest.TestCase):
    def test_solar_kwh_out_of_range_is_rejected(self):
        daily = make_daily_fixture()
        daily[0]["solar_kwh"] = 500.0
        with tempfile.TemporaryDirectory() as tmp:
            incoming = write_incoming(Path(tmp), daily=daily)
            with self.assertRaises(validate_metrics.ValidationFailure) as ctx:
                validate_metrics.validate(incoming, REPO_ROOT, allow_history_change=False)
            self.assertEqual(ctx.exception.gate, "G10")


def _daily_rows_for_months(days_by_month: dict[str, int]) -> list[dict]:
    """月ごとに指定日数の daily 行（1日から連続）を作る。G11 の前月比チェックの日数条件用。"""
    base = make_daily_fixture()[0]
    rows = []
    for month, n in sorted(days_by_month.items()):
        for i in range(n):
            rows.append(dict(base, date=f"{month}-{i + 1:02d}"))
    return rows


class Gate11Test(unittest.TestCase):
    _MONTHLY_SWING = [
        {"month": "2026-08", "solar_kwh": 1.0, "buy_kwh": 0.0, "sell_kwh": 0.0, "nichicon_charge_kwh": None, "ecoflow_charge_kwh": None, "self_consumption_shift_kwh": None, "saving_yen": 100},
        {"month": "2026-09", "solar_kwh": 1.0, "buy_kwh": 0.0, "sell_kwh": 0.0, "nichicon_charge_kwh": None, "ecoflow_charge_kwh": None, "self_consumption_shift_kwh": None, "saving_yen": 5000},
    ]

    def test_monthly_saving_yen_swing_is_rejected(self):
        daily = _daily_rows_for_months({"2026-08": 25, "2026-09": 25})
        with tempfile.TemporaryDirectory() as tmp:
            incoming = write_incoming(Path(tmp), daily=daily, monthly=self._MONTHLY_SWING)
            with self.assertRaises(validate_metrics.ValidationFailure) as ctx:
                validate_metrics.gate11_anomaly(daily, self._MONTHLY_SWING, incoming)
            self.assertEqual(ctx.exception.gate, "G11")

    def test_monthly_swing_is_ignored_when_previous_month_is_partial(self):
        # 初回 homelab 実行(2026-09-26)の誤検知: publish_since=2026-08-29 で 8 月が 3 日分しか
        # 無く、9 月との比率が 10 倍を超えた。日数が足りない月は前月比の対象にしない。
        daily = _daily_rows_for_months({"2026-08": 3, "2026-09": 25})
        with tempfile.TemporaryDirectory() as tmp:
            incoming = write_incoming(Path(tmp), daily=daily, monthly=self._MONTHLY_SWING)
            validate_metrics.gate11_anomaly(daily, self._MONTHLY_SWING, incoming)

    def test_monthly_swing_is_ignored_when_latest_month_is_short(self):
        daily = _daily_rows_for_months({"2026-08": 25, "2026-09": 5})
        with tempfile.TemporaryDirectory() as tmp:
            incoming = write_incoming(Path(tmp), daily=daily, monthly=self._MONTHLY_SWING)
            validate_metrics.gate11_anomaly(daily, self._MONTHLY_SWING, incoming)

    def test_new_day_solar_spike_against_30day_median_is_rejected(self):
        old_daily = [
            dict(make_daily_fixture()[0], date=(datetime(2026, 8, 1) + timedelta(days=i)).strftime("%Y-%m-%d"), solar_kwh=10.0)
            for i in range(35)
        ]
        new_daily = old_daily + [dict(old_daily[-1], date="2026-09-05", solar_kwh=100.0)]
        with tempfile.TemporaryDirectory() as tmp:
            incoming = _git_repo_with_two_commits(Path(tmp), old_daily=old_daily, new_daily=new_daily)
            with self.assertRaises(validate_metrics.ValidationFailure) as ctx:
                validate_metrics.validate(incoming, REPO_ROOT, allow_history_change=False)
            self.assertEqual(ctx.exception.gate, "G11")


class Gate12Test(unittest.TestCase):
    def test_monthly_sum_mismatch_is_rejected(self):
        daily = make_daily_fixture()
        monthly = make_monthly_fixture(daily)
        monthly[0]["solar_kwh"] += 50.0
        with tempfile.TemporaryDirectory() as tmp:
            incoming = write_incoming(Path(tmp), daily=daily, monthly=monthly)
            with self.assertRaises(validate_metrics.ValidationFailure) as ctx:
                validate_metrics.validate(incoming, REPO_ROOT, allow_history_change=False)
            self.assertEqual(ctx.exception.gate, "G12")

    def test_power_history_since_after_daily_min_date_is_rejected(self):
        meta = _load_real("data/metrics/meta.json")
        meta["power_history_since"] = "2099-01-01"
        with tempfile.TemporaryDirectory() as tmp:
            incoming = write_incoming(Path(tmp), meta=meta)
            with self.assertRaises(validate_metrics.ValidationFailure) as ctx:
                validate_metrics.validate(incoming, REPO_ROOT, allow_history_change=False)
            self.assertEqual(ctx.exception.gate, "G12")


class Gate13Test(unittest.TestCase):
    def test_wrong_schema_version_is_rejected(self):
        pipeline = make_pipeline_fixture()
        pipeline["schema_version"] = 2
        with tempfile.TemporaryDirectory() as tmp:
            incoming = write_incoming(Path(tmp), pipeline=pipeline)
            with self.assertRaises(validate_metrics.ValidationFailure) as ctx:
                validate_metrics.validate(incoming, REPO_ROOT, allow_history_change=False)
            self.assertEqual(ctx.exception.gate, "G13")

    def test_stale_generated_at_is_warning_only(self):
        # 閾値は8日（run-daily.shが変更なし日にpipeline.jsonをcommitしない方針のため、
        # 1日3回実行の間隔より十分長く取っている。QA指摘F16）。
        stale = (datetime.now(JST) - timedelta(days=9)).strftime("%Y-%m-%d %H:%M:%S")
        pipeline = make_pipeline_fixture(generated_at=stale)
        with tempfile.TemporaryDirectory() as tmp:
            incoming = write_incoming(Path(tmp), pipeline=pipeline)
            warnings = validate_metrics.validate(incoming, REPO_ROOT, allow_history_change=False)
            self.assertTrue(any("generated_at" in w for w in warnings))

    def test_generated_at_within_8_days_is_not_stale(self):
        recent = (datetime.now(JST) - timedelta(days=7)).strftime("%Y-%m-%d %H:%M:%S")
        pipeline = make_pipeline_fixture(generated_at=recent)
        with tempfile.TemporaryDirectory() as tmp:
            incoming = write_incoming(Path(tmp), pipeline=pipeline)
            warnings = validate_metrics.validate(incoming, REPO_ROOT, allow_history_change=False)
            self.assertFalse(any("generated_at" in w for w in warnings))

    def test_hash_mismatch_is_warning_only(self):
        pipeline = make_pipeline_fixture()
        pipeline["inputs"]["tariff_sha256"] = "0" * 64
        with tempfile.TemporaryDirectory() as tmp:
            incoming = write_incoming(Path(tmp), pipeline=pipeline)
            warnings = validate_metrics.validate(incoming, REPO_ROOT, allow_history_change=False)
            self.assertTrue(any("tariff_sha256" in w for w in warnings))

    def test_input_paths_override_is_used_for_hash_check(self):
        """homelab の /opt/blog-metrics/ のようなフラットレイアウト向けに、G13の照合先を
        --repo基準の既定から明示的に差し替えられること。"""
        with tempfile.TemporaryDirectory() as tmp:
            bundle_tariff = Path(tmp) / "bundle_tariff.json"
            bundle_tariff.write_text((REPO_ROOT / "scripts" / "blog-metrics" / "tariff.json").read_text(encoding="utf-8"), encoding="utf-8")
            pipeline = make_pipeline_fixture()
            pipeline["inputs"]["tariff_sha256"] = _sha256(bundle_tariff)
            incoming_dir = Path(tmp) / "incoming"
            incoming_dir.mkdir()
            incoming = write_incoming(incoming_dir, pipeline=pipeline)
            override_paths = validate_metrics.default_input_paths(REPO_ROOT)
            override_paths["tariff_sha256"] = bundle_tariff
            warnings = validate_metrics.validate(incoming, REPO_ROOT, allow_history_change=False, input_paths=override_paths)
            self.assertFalse(any("tariff_sha256" in w for w in warnings))


def _old_daily_fixture() -> list[dict]:
    """G9テスト用: today-20日より確実に過去の日付だけで構成する。"""
    base = (datetime.now(JST) - timedelta(days=40)).date()
    rows = []
    for i in range(3):
        row = dict(make_daily_fixture()[0])
        row["date"] = (base + timedelta(days=i)).strftime("%Y-%m-%d")
        rows.append(row)
    return rows


def _git_repo_with_broken_middle_commit(base: Path, *, old_daily: list[dict], new_daily: list[dict]) -> Path:
    """コミット1=old_daily、コミット2=壊れたJSON（検証で拒否された push を模す）、
    コミット3=new_daily（revert 後）。HEAD~1 が読めない場合に HEAD~2 と比較することを
    確認する G8/G9/G11 のテストで使う（N2 負テストで発見した回帰）。"""
    write_incoming(base, daily=old_daily, monthly=make_monthly_fixture(old_daily))
    subprocess.run(["git", "init", "-q"], cwd=base, check=True)
    subprocess.run(["git", "config", "user.email", "test@example.com"], cwd=base, check=True)
    subprocess.run(["git", "config", "user.name", "test"], cwd=base, check=True)
    subprocess.run(["git", "add", "-A"], cwd=base, check=True)
    subprocess.run(["git", "commit", "-q", "-m", "commit1"], cwd=base, check=True)
    (base / "data" / "metrics" / "daily.json").write_text("[{ this is not json\n", encoding="utf-8")
    subprocess.run(["git", "add", "-A"], cwd=base, check=True)
    subprocess.run(["git", "commit", "-q", "-m", "broken"], cwd=base, check=True)
    write_incoming(base, daily=new_daily, monthly=make_monthly_fixture(new_daily))
    subprocess.run(["git", "add", "-A"], cwd=base, check=True)
    subprocess.run(["git", "commit", "-q", "-m", "commit3"], cwd=base, check=True)
    return base


def _git_repo_with_two_commits(base: Path, *, old_daily: list[dict], new_daily: list[dict]) -> Path:
    """incoming を git リポジトリにし、old_daily でコミット1、new_daily でコミット2を作る。
    HEAD~1 差分に依存する G8/G9/G11 のテストで使う。"""
    write_incoming(base, daily=old_daily, monthly=make_monthly_fixture(old_daily))
    subprocess.run(["git", "init", "-q"], cwd=base, check=True)
    subprocess.run(["git", "config", "user.email", "test@example.com"], cwd=base, check=True)
    subprocess.run(["git", "config", "user.name", "test"], cwd=base, check=True)
    subprocess.run(["git", "add", "-A"], cwd=base, check=True)
    subprocess.run(["git", "commit", "-q", "-m", "commit1"], cwd=base, check=True)

    write_incoming(base, daily=new_daily, monthly=make_monthly_fixture(new_daily))
    subprocess.run(["git", "add", "-A"], cwd=base, check=True)
    subprocess.run(["git", "commit", "-q", "-m", "commit2"], cwd=base, check=True)
    return base


def _git_repo_with_two_post_versions(base: Path, *, old_post: dict, new_post: dict, name: str = "2026-10.json") -> Path:
    """incoming を git リポジトリにし、old_post でコミット1、new_post でコミット2を作る
    （G16のHEAD~1比較テスト用。_git_repo_with_two_commitsのposts版）。old_post/new_postが
    同一(凍結の境界テスト)でも空コミットとして成立するよう --allow-empty を使う。"""
    write_incoming(base, posts={name: old_post})
    subprocess.run(["git", "init", "-q"], cwd=base, check=True)
    subprocess.run(["git", "config", "user.email", "test@example.com"], cwd=base, check=True)
    subprocess.run(["git", "config", "user.name", "test"], cwd=base, check=True)
    subprocess.run(["git", "add", "-A"], cwd=base, check=True)
    subprocess.run(["git", "commit", "-q", "-m", "commit1"], cwd=base, check=True)

    write_incoming(base, posts={name: new_post})
    subprocess.run(["git", "add", "-A"], cwd=base, check=True)
    subprocess.run(["git", "commit", "-q", "--allow-empty", "-m", "commit2"], cwd=base, check=True)
    return base


def _git_repo_with_post_deleted(base: Path, *, post: dict, name: str = "2026-10.json") -> Path:
    """incoming を git リポジトリにし、コミット1でpostsを含め、コミット2でposts/name を
    削除する（G16の削除検知テスト用）。"""
    write_incoming(base, posts={name: post})
    subprocess.run(["git", "init", "-q"], cwd=base, check=True)
    subprocess.run(["git", "config", "user.email", "test@example.com"], cwd=base, check=True)
    subprocess.run(["git", "config", "user.name", "test"], cwd=base, check=True)
    subprocess.run(["git", "add", "-A"], cwd=base, check=True)
    subprocess.run(["git", "commit", "-q", "-m", "commit1"], cwd=base, check=True)

    subprocess.run(["git", "rm", "-q", f"posts/{name}"], cwd=base, check=True)
    subprocess.run(["git", "commit", "-q", "-m", "commit2"], cwd=base, check=True)
    return base


def _git_repo_with_broken_middle_commit_and_posts(
    base: Path, *, old_post: dict, new_post: dict, name: str = "2026-10.json",
) -> Path:
    """コミット1=old_post、コミット2=data/metrics/daily.jsonが壊れたコミット（拒否push を
    模す。postsはold_postのまま）、コミット3=new_post（revert後）。G16が
    _previous_commit_depth_for_posts経由でHEAD~1を飛ばしHEAD~2(commit1)と比較することを
    確認する（QA指摘2026-09-26 item9。以前はposts一覧の比較がHEAD~1固定だったため、
    拒否pushをrevertした直後にHEAD~1が壊れていると誤って「全post削除」と判定していた）。"""
    write_incoming(base, posts={name: old_post})
    subprocess.run(["git", "init", "-q"], cwd=base, check=True)
    subprocess.run(["git", "config", "user.email", "test@example.com"], cwd=base, check=True)
    subprocess.run(["git", "config", "user.name", "test"], cwd=base, check=True)
    subprocess.run(["git", "add", "-A"], cwd=base, check=True)
    subprocess.run(["git", "commit", "-q", "-m", "commit1"], cwd=base, check=True)

    (base / "data" / "metrics" / "daily.json").write_text("[{ this is not json\n", encoding="utf-8")
    subprocess.run(["git", "add", "-A"], cwd=base, check=True)
    subprocess.run(["git", "commit", "-q", "-m", "broken"], cwd=base, check=True)

    write_incoming(base, posts={name: new_post})
    subprocess.run(["git", "add", "-A"], cwd=base, check=True)
    subprocess.run(["git", "commit", "-q", "-m", "commit3"], cwd=base, check=True)
    return base


class Gate1PostsTest(unittest.TestCase):
    def test_stray_files_under_posts_are_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            incoming = write_incoming(Path(tmp))
            (incoming / "posts").mkdir()
            (incoming / "posts" / "evil.md").write_text("# not json\n", encoding="utf-8")
            (incoming / "posts" / "2026-8.json").write_text("{}\n", encoding="utf-8")
            with self.assertRaises(validate_metrics.ValidationFailure) as ctx:
                validate_metrics.validate(incoming, REPO_ROOT, allow_history_change=False)
            self.assertEqual(ctx.exception.gate, "G1")

    def test_too_many_post_files_are_rejected(self):
        post, _ = make_valid_post_fixture()
        posts = {f"2020-{m:02d}.json": post for m in range(1, validate_metrics.MAX_POST_FILES + 2)}
        with tempfile.TemporaryDirectory() as tmp:
            incoming = write_incoming(Path(tmp), posts=posts)
            with self.assertRaises(validate_metrics.ValidationFailure) as ctx:
                validate_metrics.validate(incoming, REPO_ROOT, allow_history_change=False)
            self.assertEqual(ctx.exception.gate, "G1")


class Gate4PostsTest(unittest.TestCase):
    def test_unknown_post_key_is_rejected(self):
        post, _ = make_valid_post_fixture()
        post["unexpected_field"] = "x"
        with tempfile.TemporaryDirectory() as tmp:
            incoming = write_incoming(Path(tmp), posts={"2026-10.json": post})
            with self.assertRaises(validate_metrics.ValidationFailure) as ctx:
                validate_metrics.validate(incoming, REPO_ROOT, allow_history_change=False)
            self.assertEqual(ctx.exception.gate, "G4")


class Gate6PostsTest(unittest.TestCase):
    def test_utility_company_name_in_post_is_rejected(self):
        post, _ = make_valid_post_fixture()
        post["estimation"] = "東京電力"  # G4のキー自体は許可されているが値に事業者名を混ぜる
        with tempfile.TemporaryDirectory() as tmp:
            incoming = write_incoming(Path(tmp), posts={"2026-10.json": post})
            with self.assertRaises(validate_metrics.ValidationFailure) as ctx:
                validate_metrics.validate(incoming, REPO_ROOT, allow_history_change=False)
            self.assertEqual(ctx.exception.gate, "G6")


class Gate14PostSchemaTest(unittest.TestCase):
    """G14はwall-clockに依存する(first_published<=today_jst等)ため、today_jstを明示的に
    与えられるgate14_post_schema()を直接呼ぶ（validate()経由だと実行日に依存してしまう）。"""

    def _today(self) -> date:
        return date.fromisoformat(POST_USAGE_PERIOD["end"]) + timedelta(days=23)  # first_published(10/24)以降

    def test_valid_post_passes(self):
        post, _ = make_valid_post_fixture()
        validate_metrics.gate14_post_schema(post, "posts/2026-10.json", self._today(), POST_METER_READ_DAY)  # 例外なし

    def test_free_text_string_value_is_rejected(self):
        post, _ = make_valid_post_fixture()
        post["estimation"] = "<b>x</b>"
        with self.assertRaises(validate_metrics.ValidationFailure) as ctx:
            validate_metrics.gate14_post_schema(post, "posts/2026-10.json", self._today(), POST_METER_READ_DAY)
        self.assertEqual(ctx.exception.gate, "G14")

    def test_bool_typed_yen_is_rejected(self):
        post, _ = make_valid_post_fixture()
        post["layers"]["L3"]["net_cost_fit_yen"] = True
        with self.assertRaises(validate_metrics.ValidationFailure) as ctx:
            validate_metrics.gate14_post_schema(post, "posts/2026-10.json", self._today(), POST_METER_READ_DAY)
        self.assertEqual(ctx.exception.gate, "G14")

    def test_nan_kwh_is_rejected(self):
        post, _ = make_valid_post_fixture()
        post["layers"]["L3"]["buy_kwh"] = float("nan")
        with self.assertRaises(validate_metrics.ValidationFailure) as ctx:
            validate_metrics.gate14_post_schema(post, "posts/2026-10.json", self._today(), POST_METER_READ_DAY)
        self.assertEqual(ctx.exception.gate, "G14")

    def test_out_of_range_yen_is_rejected(self):
        post, _ = make_valid_post_fixture()
        post["layers"]["L3"]["net_cost_fit_yen"] = 999_999
        with self.assertRaises(validate_metrics.ValidationFailure) as ctx:
            validate_metrics.gate14_post_schema(post, "posts/2026-10.json", self._today(), POST_METER_READ_DAY)
        self.assertEqual(ctx.exception.gate, "G14")

    def test_stage_tariff_basis_mismatch_is_rejected(self):
        post, _ = make_valid_post_fixture()
        post["tariff_basis"] = "provisional"  # stage=="final"のまま
        with self.assertRaises(validate_metrics.ValidationFailure) as ctx:
            validate_metrics.gate14_post_schema(post, "posts/2026-10.json", self._today(), POST_METER_READ_DAY)
        self.assertEqual(ctx.exception.gate, "G14")

    def test_non_dict_post_is_rejected_not_crashed(self):
        # QA指摘2026-09-26 item8: postがdict以外ならValidationFailure(Tracebackにしない)。
        with self.assertRaises(validate_metrics.ValidationFailure) as ctx:
            validate_metrics.gate14_post_schema(["not", "a", "dict"], "posts/2026-10.json", self._today(), POST_METER_READ_DAY)
        self.assertEqual(ctx.exception.gate, "G14")
        with self.assertRaises(validate_metrics.ValidationFailure) as ctx:
            validate_metrics.gate14_post_schema("also not a dict", "posts/2026-10.json", self._today(), POST_METER_READ_DAY)
        self.assertEqual(ctx.exception.gate, "G14")

    def test_l2_band_min_greater_than_max_is_rejected(self):
        post, _ = make_valid_post_fixture()
        post["l2_band"] = {"net_cost_fit_yen_min": 5000, "net_cost_fit_yen_max": 3000}
        with self.assertRaises(validate_metrics.ValidationFailure) as ctx:
            validate_metrics.gate14_post_schema(post, "posts/2026-10.json", self._today(), POST_METER_READ_DAY)
        self.assertEqual(ctx.exception.gate, "G14")

    def test_usage_period_mismatched_with_billing_period_is_rejected(self):
        # QA指摘2026-09-26 item3: usage_periodがbill_model.billing_period(billing_month,
        # meter_read_day)と一致しないpostを拒否する。
        post, _ = make_valid_post_fixture()
        post["usage_period"] = {"start": "2026-09-01", "end": "2026-09-30", "days": 30}
        with self.assertRaises(validate_metrics.ValidationFailure) as ctx:
            validate_metrics.gate14_post_schema(post, "posts/2026-10.json", self._today(), POST_METER_READ_DAY)
        self.assertEqual(ctx.exception.gate, "G14")

    def test_billing_month_before_first_report_month_is_rejected(self):
        # QA指摘2026-09-26 item3: billing_month>=FIRST_REPORT_BILLING_MONTHをvalidator側でも
        # 強制する（速報・確定のどちらでも）。
        post, _ = make_valid_post_fixture()
        post["billing_month"] = "2020-01"
        post["report_month"] = "2019-12"
        post["usage_period"] = {"start": "2019-12-02", "end": "2020-01-01", "days": 31}
        with self.assertRaises(validate_metrics.ValidationFailure) as ctx:
            validate_metrics.gate14_post_schema(post, "posts/2020-01.json", self._today(), POST_METER_READ_DAY)
        self.assertEqual(ctx.exception.gate, "G14")

    def test_fullwidth_digit_date_bypass_attempt_is_rejected(self):
        # QA指摘2026-09-26 item8: \dはUnicodeの全角数字にもマッチするため、re.ASCIIが無いと
        # 全角数字による偽装日付が正規表現を通ってしまう可能性がある。
        post, _ = make_valid_post_fixture()
        post["first_published"] = "２０２６-10-24"  # 全角数字
        with self.assertRaises(validate_metrics.ValidationFailure) as ctx:
            validate_metrics.gate14_post_schema(post, "posts/2026-10.json", self._today(), POST_METER_READ_DAY)
        self.assertEqual(ctx.exception.gate, "G14")

    def test_preliminary_with_revision_not_one_is_rejected(self):
        # QA指摘2026-09-26 item4: 速報はrevision==1かつrevised is Noneを強制する。
        post, _ = make_valid_post_fixture(stage="preliminary")
        post["revision"] = 2
        post["revised"] = "2026-10-05"
        with self.assertRaises(validate_metrics.ValidationFailure) as ctx:
            validate_metrics.gate14_post_schema(post, "posts/2026-10.json", self._today(), POST_METER_READ_DAY)
        self.assertEqual(ctx.exception.gate, "G14")

    def test_transitioned_from_preliminary_must_be_bool(self):
        post, _ = make_valid_post_fixture()
        post["transitioned_from_preliminary"] = "true"
        with self.assertRaises(validate_metrics.ValidationFailure) as ctx:
            validate_metrics.gate14_post_schema(post, "posts/2026-10.json", self._today(), POST_METER_READ_DAY)
        self.assertEqual(ctx.exception.gate, "G14")

    def test_bool_typed_schema_version_is_rejected(self):
        # QA再指摘2026-09-26 R1: type(x) is intでbool混入を排除する(True==1だが型はboolであるべき)。
        post, _ = make_valid_post_fixture()
        post["schema_version"] = True
        with self.assertRaises(validate_metrics.ValidationFailure) as ctx:
            validate_metrics.gate14_post_schema(post, "posts/2026-10.json", self._today(), POST_METER_READ_DAY)
        self.assertEqual(ctx.exception.gate, "G14")

    def test_bool_typed_usage_period_days_is_rejected(self):
        post, _ = make_valid_post_fixture()
        post["usage_period"]["days"] = True
        with self.assertRaises(validate_metrics.ValidationFailure) as ctx:
            validate_metrics.gate14_post_schema(post, "posts/2026-10.json", self._today(), POST_METER_READ_DAY)
        self.assertEqual(ctx.exception.gate, "G14")


class Gate15PostRecomputeTest(unittest.TestCase):
    def test_valid_final_post_passes(self):
        post, layers_json = make_valid_post_fixture()
        daily_by_date = {r["date"]: r for r in make_daily_fixture()}
        validate_metrics.gate15_post_recompute(post, "posts/2026-10.json", layers_json, list(daily_by_date.values()), Path("/nonexistent"), POST_METER_READ_DAY)

    def test_tampered_confirmed_month_value_is_rejected(self):
        post, layers_json = make_valid_post_fixture()
        post["layers"]["L3"]["net_cost_fit_yen"] += 1  # 確定月のL3を+1円改ざん
        daily_by_date = {r["date"]: r for r in make_daily_fixture()}
        with self.assertRaises(validate_metrics.ValidationFailure) as ctx:
            validate_metrics.gate15_post_recompute(post, "posts/2026-10.json", layers_json, list(daily_by_date.values()), Path("/nonexistent"), POST_METER_READ_DAY)
        self.assertEqual(ctx.exception.gate, "G15")

    def test_final_stage_for_non_closable_month_is_rejected(self):
        # buy_source=="sensor"(請求書未着)のままstage=="final"を名乗る新規post。
        post, layers_json = make_valid_post_fixture(stage="preliminary")
        post["stage"] = "final"
        post["tariff_basis"] = "confirmed"
        daily_by_date = {r["date"]: r for r in make_daily_fixture()}
        with self.assertRaises(validate_metrics.ValidationFailure) as ctx:
            validate_metrics.gate15_post_recompute(post, "posts/2026-10.json", layers_json, list(daily_by_date.values()), Path("/nonexistent"), POST_METER_READ_DAY)
        self.assertEqual(ctx.exception.gate, "G15")

    # --- QA再指摘2026-09-26 N3: energy/weather/comparisonの捏造がすり抜けていた -----------------

    def test_new_final_post_with_fabricated_energy_is_rejected(self):
        # 前回のpostが無い(新規公開)場合、energyを実際の発電量とかけ離れた値に書き換えても
        # 本体全体が再計算(expected_final_body)と一致しないためrejectされる。
        post, layers_json = make_valid_post_fixture()
        post["energy"]["solar_kwh"] = 5999.0  # 捏造
        daily_by_date = {r["date"]: r for r in make_daily_fixture()}
        with self.assertRaises(validate_metrics.ValidationFailure) as ctx:
            validate_metrics.gate15_post_recompute(post, "posts/2026-10.json", layers_json, list(daily_by_date.values()), Path("/nonexistent"), POST_METER_READ_DAY)
        self.assertEqual(ctx.exception.gate, "G15")

    def test_new_preliminary_post_with_fabricated_weather_is_rejected(self):
        post, layers_json = make_valid_post_fixture(stage="preliminary")
        post["weather"] = {"sunny_days": 30, "cloudy_days": 0, "overcast_days": 0, "unknown_days": 0}  # 捏造
        daily_by_date = {r["date"]: r for r in make_daily_fixture()}
        with self.assertRaises(validate_metrics.ValidationFailure) as ctx:
            validate_metrics.gate15_post_recompute(post, "posts/2026-10.json", layers_json, list(daily_by_date.values()), Path("/nonexistent"), POST_METER_READ_DAY)
        self.assertEqual(ctx.exception.gate, "G15")

    def test_frozen_confirmed_post_with_unchanged_body_passes(self):
        # 既存の確定版(layer由来が変わっていない)は、energy/weather/comparisonを含め
        # 本体全体が前回と同一であれば通る（凍結）。
        post, layers_json = make_valid_post_fixture()
        daily_by_date = {r["date"]: r for r in make_daily_fixture()}
        with tempfile.TemporaryDirectory() as tmp:
            incoming = _git_repo_with_two_post_versions(Path(tmp), old_post=post, new_post=post)
            validate_metrics.gate15_post_recompute(post, "posts/2026-10.json", layers_json, list(daily_by_date.values()), incoming, POST_METER_READ_DAY)

    def test_frozen_confirmed_post_with_fabricated_energy_is_rejected(self):
        # layer由来は前回と同じ(＝改版すべきでない)のに、energyだけ前回と異なる値に
        # 書き換えると拒否される（QA再指摘2026-09-26 N3の核心: 以前はここが素通りしていた）。
        old_post, layers_json = make_valid_post_fixture()
        new_post = copy.deepcopy(old_post)
        new_post["energy"]["solar_kwh"] = 5999.0  # 捏造（layers/l2_band/l3_source等は不変のまま）
        daily_by_date = {r["date"]: r for r in make_daily_fixture()}
        with tempfile.TemporaryDirectory() as tmp:
            incoming = _git_repo_with_two_post_versions(Path(tmp), old_post=old_post, new_post=new_post)
            with self.assertRaises(validate_metrics.ValidationFailure) as ctx:
                validate_metrics.gate15_post_recompute(new_post, "posts/2026-10.json", layers_json, list(daily_by_date.values()), incoming, POST_METER_READ_DAY)
            self.assertEqual(ctx.exception.gate, "G15")

    def test_transition_from_preliminary_with_fabricated_energy_is_rejected(self):
        # QA再指摘2026-09-26 L5: 速報から確定への遷移時にenergyを捏造すると拒否される
        # ことを、実際に速報のpostがgit履歴上に存在する状態で確認する(N3の核心ケース)。
        old_post, _ = make_valid_post_fixture(stage="preliminary")
        new_post, layers_json = make_valid_post_fixture(
            stage="final", transitioned_from_preliminary=True, revision=2, revised="2026-11-01",
        )
        new_post["first_published"] = old_post["first_published"]
        new_post["energy"]["solar_kwh"] = 5999.0  # 捏造
        daily_by_date = {r["date"]: r for r in make_daily_fixture()}
        with tempfile.TemporaryDirectory() as tmp:
            incoming = _git_repo_with_two_post_versions(Path(tmp), old_post=old_post, new_post=new_post)
            with self.assertRaises(validate_metrics.ValidationFailure) as ctx:
                validate_metrics.gate15_post_recompute(
                    new_post, "posts/2026-10.json", layers_json, list(daily_by_date.values()), incoming, POST_METER_READ_DAY,
                )
            self.assertEqual(ctx.exception.gate, "G15")

    def test_revised_confirmed_post_inherits_previous_energy(self):
        # layer由来が変わった正当な改版では、energy/weather/comparisonは前回の値を
        # 引き継いだものだけが通る（新しく計算し直した値は通らない）。
        old_post, layers_json = make_valid_post_fixture()
        layers_json["months"][0]["layers"]["L3"]["net_cost_fit_yen"] += 100  # layer由来を変える
        expected = monthly_report.expected_final_body(
            POST_BILLING_MONTH, layers_json, {r["date"]: r for r in make_daily_fixture()},
            POST_METER_READ_DAY, "final", monthly_report.body_without_meta(old_post),
        )
        new_post = {**expected, "first_published": old_post["first_published"], "revision": 2, "revised": "2026-11-01", "transitioned_from_preliminary": False}
        daily_by_date = {r["date"]: r for r in make_daily_fixture()}
        with tempfile.TemporaryDirectory() as tmp:
            incoming = _git_repo_with_two_post_versions(Path(tmp), old_post=old_post, new_post=new_post)
            validate_metrics.gate15_post_recompute(new_post, "posts/2026-10.json", layers_json, list(daily_by_date.values()), incoming, POST_METER_READ_DAY)
            self.assertEqual(new_post["energy"], old_post["energy"])  # 引き継がれていること


class Gate16PostHistoryTest(unittest.TestCase):
    def test_revision_not_bumped_when_body_changed_is_rejected(self):
        old_post, _ = make_valid_post_fixture()
        new_post = copy.deepcopy(old_post)
        new_post["layers"]["L3"]["net_cost_fit_yen"] += 100  # 本体を変えたのにrevisionはそのまま
        with tempfile.TemporaryDirectory() as tmp:
            incoming = _git_repo_with_two_post_versions(Path(tmp), old_post=old_post, new_post=new_post)
            with self.assertRaises(validate_metrics.ValidationFailure) as ctx:
                validate_metrics.gate16_post_history({"posts/2026-10.json": new_post}, incoming, allow_history_change=False)
            self.assertEqual(ctx.exception.gate, "G16")

    def test_first_published_change_is_rejected(self):
        old_post, _ = make_valid_post_fixture()
        new_post = copy.deepcopy(old_post)
        new_post["first_published"] = "2026-10-25"
        with tempfile.TemporaryDirectory() as tmp:
            incoming = _git_repo_with_two_post_versions(Path(tmp), old_post=old_post, new_post=new_post)
            with self.assertRaises(validate_metrics.ValidationFailure) as ctx:
                validate_metrics.gate16_post_history({"posts/2026-10.json": new_post}, incoming, allow_history_change=False)
            self.assertEqual(ctx.exception.gate, "G16")

    def test_deleted_post_is_rejected(self):
        old_post, _ = make_valid_post_fixture()
        with tempfile.TemporaryDirectory() as tmp:
            incoming = _git_repo_with_post_deleted(Path(tmp), post=old_post)
            with self.assertRaises(validate_metrics.ValidationFailure) as ctx:
                validate_metrics.gate16_post_history({}, incoming, allow_history_change=False)
            self.assertEqual(ctx.exception.gate, "G16")

    def test_allow_history_change_bypasses_all_checks(self):
        old_post, _ = make_valid_post_fixture()
        new_post = copy.deepcopy(old_post)
        new_post["layers"]["L3"]["net_cost_fit_yen"] += 100
        with tempfile.TemporaryDirectory() as tmp:
            incoming = _git_repo_with_two_post_versions(Path(tmp), old_post=old_post, new_post=new_post)
            validate_metrics.gate16_post_history({"posts/2026-10.json": new_post}, incoming, allow_history_change=True)

    def test_stage_regression_from_final_to_preliminary_is_rejected(self):
        old_post, _ = make_valid_post_fixture(stage="final")
        new_post, _ = make_valid_post_fixture(stage="preliminary")
        new_post["first_published"] = old_post["first_published"]
        with tempfile.TemporaryDirectory() as tmp:
            incoming = _git_repo_with_two_post_versions(Path(tmp), old_post=old_post, new_post=new_post)
            with self.assertRaises(validate_metrics.ValidationFailure) as ctx:
                validate_metrics.gate16_post_history({"posts/2026-10.json": new_post}, incoming, allow_history_change=False)
            self.assertEqual(ctx.exception.gate, "G16")

    def test_frozen_old_month_identical_to_head_minus_1_passes(self):
        # G15/G16の境界: 確定しない古い月でも、HEAD~1と本体が同一なら通る（凍結）。
        post, _ = make_valid_post_fixture(stage="preliminary")
        with tempfile.TemporaryDirectory() as tmp:
            incoming = _git_repo_with_two_post_versions(Path(tmp), old_post=post, new_post=post)
            validate_metrics.gate16_post_history({"posts/2026-10.json": post}, incoming, allow_history_change=False)
            daily_by_date = {r["date"]: r for r in make_daily_fixture()}
            layers_json = {"months": [], "preliminary_months": []}  # もはやpreliminary_monthsにも無い(窓の外)
            validate_metrics.gate15_post_recompute(post, "posts/2026-10.json", layers_json, list(daily_by_date.values()), incoming, POST_METER_READ_DAY)

    # --- QA指摘2026-09-26 item9: 祖先の深さを_previous_commit_jsonと揃える ---------------------

    def test_reverted_broken_push_falls_back_to_older_ancestor_for_unchanged_post(self):
        # HEAD~1(拒否pushをrevertした直後)のdaily.jsonが壊れていても、HEAD~2まで遡って
        # posts一覧・本体を正しく比較できる（本体・revisionともに変わっていないので通る）。
        post, _ = make_valid_post_fixture()
        with tempfile.TemporaryDirectory() as tmp:
            incoming = _git_repo_with_broken_middle_commit_and_posts(Path(tmp), old_post=post, new_post=post)
            validate_metrics.gate16_post_history({"posts/2026-10.json": post}, incoming, allow_history_change=False)

    def test_reverted_broken_push_still_detects_revision_violation_via_older_ancestor(self):
        # 同じ状況で、本体を変えたのにrevisionを上げていない場合はHEAD~2との比較で
        # 正しくrejectされる（HEAD~1が壊れているからといって履歴整合チェックが素通りしない）。
        old_post, _ = make_valid_post_fixture()
        new_post = copy.deepcopy(old_post)
        new_post["layers"]["L3"]["net_cost_fit_yen"] += 100  # revisionはそのまま
        with tempfile.TemporaryDirectory() as tmp:
            incoming = _git_repo_with_broken_middle_commit_and_posts(Path(tmp), old_post=old_post, new_post=new_post)
            with self.assertRaises(validate_metrics.ValidationFailure) as ctx:
                validate_metrics.gate16_post_history({"posts/2026-10.json": new_post}, incoming, allow_history_change=False)
            self.assertEqual(ctx.exception.gate, "G16")

    def test_reverted_broken_push_deletion_via_older_ancestor_is_rejected(self):
        # HEAD~1が壊れている状態でpostsが削除されていた場合も、HEAD~2との比較で検出できる。
        post, _ = make_valid_post_fixture()
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            write_incoming(base, posts={"2026-10.json": post})
            subprocess.run(["git", "init", "-q"], cwd=base, check=True)
            subprocess.run(["git", "config", "user.email", "test@example.com"], cwd=base, check=True)
            subprocess.run(["git", "config", "user.name", "test"], cwd=base, check=True)
            subprocess.run(["git", "add", "-A"], cwd=base, check=True)
            subprocess.run(["git", "commit", "-q", "-m", "commit1"], cwd=base, check=True)
            (base / "data" / "metrics" / "daily.json").write_text("[{ this is not json\n", encoding="utf-8")
            subprocess.run(["git", "add", "-A"], cwd=base, check=True)
            subprocess.run(["git", "commit", "-q", "-m", "broken"], cwd=base, check=True)
            subprocess.run(["git", "rm", "-q", "posts/2026-10.json"], cwd=base, check=True)
            subprocess.run(["git", "commit", "-q", "-m", "commit3"], cwd=base, check=True)
            with self.assertRaises(validate_metrics.ValidationFailure) as ctx:
                validate_metrics.gate16_post_history({}, base, allow_history_change=False)
            self.assertEqual(ctx.exception.gate, "G16")


class EndToEndWithPostsTest(unittest.TestCase):
    """QAが指摘した不足テスト: posts/*.json込みでvalidate()を通しで実行する
    （G1〜G16全ゲート）。validate()のtodayオーバーライドを使い、実行日に依存せず
    billing_month=FIRST_REPORT_BILLING_MONTH("2026-10")の確定postを検証できるようにする。"""

    def test_validate_passes_end_to_end_with_a_valid_final_post(self):
        post, layers_json = make_valid_post_fixture()
        with tempfile.TemporaryDirectory() as tmp:
            incoming = write_incoming(Path(tmp), layers=layers_json, posts={"2026-10.json": post})
            warnings = validate_metrics.validate(
                incoming, REPO_ROOT, allow_history_change=False, today=date(2026, 10, 24),
            )
            self.assertEqual(warnings, [])

    def test_validate_rejects_tampered_post_end_to_end(self):
        post, layers_json = make_valid_post_fixture()
        post["layers"]["L3"]["net_cost_fit_yen"] += 1  # 確定月の改ざん
        with tempfile.TemporaryDirectory() as tmp:
            incoming = write_incoming(Path(tmp), layers=layers_json, posts={"2026-10.json": post})
            with self.assertRaises(validate_metrics.ValidationFailure) as ctx:
                validate_metrics.validate(incoming, REPO_ROOT, allow_history_change=False, today=date(2026, 10, 24))
            self.assertEqual(ctx.exception.gate, "G15")

    def test_validate_passes_end_to_end_with_a_valid_preliminary_post(self):
        post, layers_json = make_valid_post_fixture(stage="preliminary", first_published="2026-10-03")
        with tempfile.TemporaryDirectory() as tmp:
            incoming = write_incoming(Path(tmp), layers=layers_json, posts={"2026-10.json": post})
            warnings = validate_metrics.validate(
                incoming, REPO_ROOT, allow_history_change=False, today=date(2026, 10, 3),
            )
            self.assertEqual(warnings, [])


# --- inputs/*.json（月次確定の自動化、DDR実装手順S1）用フィクスチャ ------------------------
import import_official_buy  # noqa: E402
import import_official_sell  # noqa: E402

REAL_TARIFF = json.loads((REPO_ROOT / "scripts" / "blog-metrics" / "tariff.json").read_text(encoding="utf-8"))
REAL_METER_READ_DAY = REAL_TARIFF["meter_read_day"]
REAL_SELL_FIT = REAL_TARIFF["sell_price_yen_per_kwh"]["fit"]


def make_official_buy_fixture(*, generated_at=None, months=None) -> dict:
    if generated_at is None:
        generated_at = datetime.now(JST).strftime("%Y-%m-%d %H:%M:%S")
    if months is None:
        # 実tariff.jsonで確定済みの2026-08、ReconciledMonthsTestと同じ数値(diff=0)。
        months = [
            {
                "settlement_month": "2026-08",
                "period_from": "2026-07-02",
                "period_to": "2026-08-01",
                "official_buy_kwh": 83.0,
                "billed_yen": 3661,
            }
        ]
    return {"months": months, "generated_at": generated_at, "source_note": import_official_buy.SOURCE_NOTE}


def make_official_sell_fixture(*, generated_at=None, months=None) -> dict:
    if generated_at is None:
        generated_at = datetime.now(JST).strftime("%Y-%m-%d %H:%M:%S")
    if months is None:
        months = [
            {
                "settlement_month": "2026-08",
                "period_from": "2026-07-02",
                "period_to": "2026-08-01",
                "official_sell_kwh": 100.0,
                "sell_revenue_yen": 1600,  # = 16.0(fit) * 100.0
            }
        ]
    return {"months": months, "generated_at": generated_at, "source_note": import_official_sell.SOURCE_NOTE}


def make_tariff_months_fixture(*, generated_at=None, fuel=None, capacity=None, levy=None, excluded=None) -> dict:
    if generated_at is None:
        generated_at = datetime.now(JST).strftime("%Y-%m-%d %H:%M:%S")
    return {
        "schema_version": 1,
        "generated_at": generated_at,
        "fuel_cost_adjustment_yen_per_kwh": {} if fuel is None else fuel,
        "capacity_contribution_yen_per_month": {} if capacity is None else capacity,
        "renewable_levy_yen_per_kwh_observed": {} if levy is None else levy,
        "excluded_months": {} if excluded is None else excluded,
    }


class Gate1InputsTest(unittest.TestCase):
    def test_optional_input_files_are_accepted(self):
        with tempfile.TemporaryDirectory() as tmp:
            incoming = write_incoming(
                Path(tmp),
                inputs={
                    "official_buy.json": make_official_buy_fixture(),
                    "official_sell.json": make_official_sell_fixture(),
                    "tariff_months.json": make_tariff_months_fixture(),
                },
            )
            # G13はincomingにinputs/*.jsonがあれば実ファイルと直接照合(fatal)するため、
            # pipeline.json側のハッシュも実際に書いたファイルに合わせておく。
            pipeline = make_pipeline_fixture()
            pipeline["inputs"]["official_buy_sha256"] = _sha256(incoming / "inputs" / "official_buy.json")
            pipeline["inputs"]["official_sell_sha256"] = _sha256(incoming / "inputs" / "official_sell.json")
            pipeline["inputs"]["tariff_months_sha256"] = _sha256(incoming / "inputs" / "tariff_months.json")
            (incoming / "pipeline.json").write_text(json.dumps(pipeline, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

            warnings = validate_metrics.validate(incoming, REPO_ROOT, allow_history_change=False)
            self.assertEqual(warnings, [])

    def test_unrecognized_file_under_inputs_is_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            incoming = write_incoming(Path(tmp))
            (incoming / "inputs").mkdir()
            (incoming / "inputs" / "evil.json").write_text("{}\n", encoding="utf-8")
            with self.assertRaises(validate_metrics.ValidationFailure) as ctx:
                validate_metrics.validate(incoming, REPO_ROOT, allow_history_change=False)
            self.assertEqual(ctx.exception.gate, "G1")


class Gate6SpidTest(unittest.TestCase):
    def test_hyphenated_supply_point_number_is_rejected(self):
        with self.assertRaises(validate_metrics.ValidationFailure) as ctx:
            validate_metrics.gate6_secret_deny("spid: 1234-5678-9012-3456", "inputs/tariff_months.json")
        self.assertEqual(ctx.exception.gate, "G6")

    def test_short_hyphenated_number_below_three_groups_is_not_flagged(self):
        # 2グループまでは供給地点番号らしさが弱いため誤検知しない（既存の日付表記等との
        # 衝突を避ける）。
        validate_metrics.gate6_secret_deny("1234-5678", "inputs/tariff_months.json")

    def test_fullwidth_hyphen_separated_supply_point_number_is_rejected(self):
        # QA指摘L3: 全角ハイフンマイナス(－)区切りも検出する。
        with self.assertRaises(validate_metrics.ValidationFailure) as ctx:
            validate_metrics.gate6_secret_deny("spid: 1234－5678－9012－3456", "inputs/tariff_months.json")
        self.assertEqual(ctx.exception.gate, "G6")

    def test_unicode_hyphen_separated_supply_point_number_is_rejected(self):
        # QA指摘L3: Unicodeハイフン(U+2010)区切りも検出する。
        with self.assertRaises(validate_metrics.ValidationFailure) as ctx:
            validate_metrics.gate6_secret_deny("spid: 1234‐5678‐9012‐3456", "inputs/tariff_months.json")
        self.assertEqual(ctx.exception.gate, "G6")

    def test_space_separated_supply_point_number_is_rejected(self):
        # QA指摘L3: 半角スペース区切りも検出する。
        with self.assertRaises(validate_metrics.ValidationFailure) as ctx:
            validate_metrics.gate6_secret_deny("spid: 1234 5678 9012 3456", "inputs/tariff_months.json")
        self.assertEqual(ctx.exception.gate, "G6")

    def test_real_data_metrics_and_testdata_have_zero_false_positives(self):
        # QA指摘L3: 区切り文字を広げたことで既存の公開済みデータに誤検知が出ないこと。
        paths = list((REPO_ROOT / "data" / "metrics").glob("*.json")) + list(TESTDATA_DIR.glob("*.json"))
        self.assertGreater(len(paths), 0)
        for path in paths:
            text = path.read_text(encoding="utf-8")
            with self.subTest(path=str(path)):
                validate_metrics.gate6_secret_deny(text, str(path))  # raiseしなければ合格


class Gate18OfficialBuyTest(unittest.TestCase):
    def test_valid_fixture_passes(self):
        validate_metrics.gate18_input_files_schema(
            {"inputs/official_buy.json": make_official_buy_fixture()}, REAL_METER_READ_DAY, REAL_SELL_FIT,
        )

    def test_unexpected_top_level_key_is_rejected(self):
        data = dict(make_official_buy_fixture(), extra="x")
        with self.assertRaises(validate_metrics.ValidationFailure) as ctx:
            validate_metrics.gate18_input_files_schema({"inputs/official_buy.json": data}, REAL_METER_READ_DAY, REAL_SELL_FIT)
        self.assertEqual(ctx.exception.gate, "G18")

    def test_wrong_source_note_is_rejected(self):
        data = dict(make_official_buy_fixture(), source_note="不明な文言")
        with self.assertRaises(validate_metrics.ValidationFailure) as ctx:
            validate_metrics.gate18_input_files_schema({"inputs/official_buy.json": data}, REAL_METER_READ_DAY, REAL_SELL_FIT)
        self.assertEqual(ctx.exception.gate, "G18")

    def test_period_mismatched_with_billing_period_is_rejected(self):
        months = [dict(make_official_buy_fixture()["months"][0], period_from="2000-01-01")]
        data = make_official_buy_fixture(months=months)
        with self.assertRaises(validate_metrics.ValidationFailure) as ctx:
            validate_metrics.gate18_input_files_schema({"inputs/official_buy.json": data}, REAL_METER_READ_DAY, REAL_SELL_FIT)
        self.assertEqual(ctx.exception.gate, "G18")

    def test_billed_yen_as_fractional_float_is_rejected(self):
        months = [dict(make_official_buy_fixture()["months"][0], billed_yen=3661.5)]
        data = make_official_buy_fixture(months=months)
        with self.assertRaises(validate_metrics.ValidationFailure) as ctx:
            validate_metrics.gate18_input_files_schema({"inputs/official_buy.json": data}, REAL_METER_READ_DAY, REAL_SELL_FIT)
        self.assertEqual(ctx.exception.gate, "G18")

    def test_billed_yen_as_integer_valued_float_is_accepted(self):
        # 既存の data/metrics/official_buy.json は billed_yen が JSON 上 float(例: 2563.0)で
        # 保存されている（import_official_buy.py が私有の抽出元の値をそのまま転記するため）。
        # 移行措置(stage_inputsのbundleコピー)がこの既存データをそのまま使う前提を壊さない
        # よう、整数値と等しいfloatは許容する。
        months = [dict(make_official_buy_fixture()["months"][0], billed_yen=3661.0)]
        data = make_official_buy_fixture(months=months)
        validate_metrics.gate18_input_files_schema({"inputs/official_buy.json": data}, REAL_METER_READ_DAY, REAL_SELL_FIT)

    def test_official_buy_kwh_out_of_range_is_rejected(self):
        months = [dict(make_official_buy_fixture()["months"][0], official_buy_kwh=9999.0)]
        data = make_official_buy_fixture(months=months)
        with self.assertRaises(validate_metrics.ValidationFailure) as ctx:
            validate_metrics.gate18_input_files_schema({"inputs/official_buy.json": data}, REAL_METER_READ_DAY, REAL_SELL_FIT)
        self.assertEqual(ctx.exception.gate, "G18")

    def test_months_out_of_order_is_rejected(self):
        m1 = dict(make_official_buy_fixture()["months"][0], settlement_month="2026-08")
        m2 = dict(m1, settlement_month="2026-07", period_from="2026-06-02", period_to="2026-07-01")
        data = make_official_buy_fixture(months=[m1, m2])
        with self.assertRaises(validate_metrics.ValidationFailure) as ctx:
            validate_metrics.gate18_input_files_schema({"inputs/official_buy.json": data}, REAL_METER_READ_DAY, REAL_SELL_FIT)
        self.assertEqual(ctx.exception.gate, "G18")

    def test_non_dict_month_element_is_rejected(self):
        # QA指摘L1: months の要素が非dict（文字列等）だと month.get(...) がAttributeErrorに
        # なり、ValidationFailureとして扱われず未処理例外が漏れる。
        data = make_official_buy_fixture(months=["not-a-dict"])
        with self.assertRaises(validate_metrics.ValidationFailure) as ctx:
            validate_metrics.gate18_input_files_schema({"inputs/official_buy.json": data}, REAL_METER_READ_DAY, REAL_SELL_FIT)
        self.assertEqual(ctx.exception.gate, "G18")


class Gate18OfficialSellTest(unittest.TestCase):
    def test_valid_fixture_passes(self):
        validate_metrics.gate18_input_files_schema(
            {"inputs/official_sell.json": make_official_sell_fixture()}, REAL_METER_READ_DAY, REAL_SELL_FIT,
        )

    def test_revenue_fit_mismatch_beyond_tolerance_is_rejected(self):
        months = [dict(make_official_sell_fixture()["months"][0], sell_revenue_yen=1600 + 100)]
        data = make_official_sell_fixture(months=months)
        with self.assertRaises(validate_metrics.ValidationFailure) as ctx:
            validate_metrics.gate18_input_files_schema({"inputs/official_sell.json": data}, REAL_METER_READ_DAY, REAL_SELL_FIT)
        self.assertEqual(ctx.exception.gate, "G18")


class Gate18TariffMonthsTest(unittest.TestCase):
    def test_valid_fixture_passes(self):
        data = make_tariff_months_fixture(
            fuel={"2026-09": 9.12}, capacity={"2026-09": 213}, levy={"2026-09..2026-09": 4.18},
        )
        validate_metrics.gate18_input_files_schema({"inputs/tariff_months.json": data}, REAL_METER_READ_DAY, REAL_SELL_FIT)

    def test_fuel_and_capacity_key_mismatch_is_rejected(self):
        data = make_tariff_months_fixture(fuel={"2026-09": 9.12}, capacity={"2026-10": 213})
        with self.assertRaises(validate_metrics.ValidationFailure) as ctx:
            validate_metrics.gate18_input_files_schema({"inputs/tariff_months.json": data}, REAL_METER_READ_DAY, REAL_SELL_FIT)
        self.assertEqual(ctx.exception.gate, "G18")

    def test_capacity_as_float_is_rejected(self):
        data = make_tariff_months_fixture(fuel={"2026-09": 9.12}, capacity={"2026-09": 213.5})
        with self.assertRaises(validate_metrics.ValidationFailure) as ctx:
            validate_metrics.gate18_input_files_schema({"inputs/tariff_months.json": data}, REAL_METER_READ_DAY, REAL_SELL_FIT)
        self.assertEqual(ctx.exception.gate, "G18")

    def test_fuel_rate_out_of_range_is_rejected(self):
        data = make_tariff_months_fixture(fuel={"2026-09": 999.0}, capacity={"2026-09": 213})
        with self.assertRaises(validate_metrics.ValidationFailure) as ctx:
            validate_metrics.gate18_input_files_schema({"inputs/tariff_months.json": data}, REAL_METER_READ_DAY, REAL_SELL_FIT)
        self.assertEqual(ctx.exception.gate, "G18")

    def test_levy_range_not_single_month_is_rejected(self):
        data = make_tariff_months_fixture(levy={"2026-09..2026-10": 4.18})
        with self.assertRaises(validate_metrics.ValidationFailure) as ctx:
            validate_metrics.gate18_input_files_schema({"inputs/tariff_months.json": data}, REAL_METER_READ_DAY, REAL_SELL_FIT)
        self.assertEqual(ctx.exception.gate, "G18")

    def test_unknown_excluded_reason_is_rejected(self):
        data = make_tariff_months_fixture(excluded={"2026-09": "not_a_real_reason"})
        with self.assertRaises(validate_metrics.ValidationFailure) as ctx:
            validate_metrics.gate18_input_files_schema({"inputs/tariff_months.json": data}, REAL_METER_READ_DAY, REAL_SELL_FIT)
        self.assertEqual(ctx.exception.gate, "G18")

    def test_excluded_reason_as_list_is_rejected(self):
        # QA指摘L1: reasonがlist(非文字列)だと `in EXCLUDED_MONTH_REASONS`（set）が
        # unhashableでTypeErrorになり、ValidationFailureとして扱われず未処理例外が漏れる。
        data = make_tariff_months_fixture(excluded={"2026-09": ["reconcile_mismatch"]})
        with self.assertRaises(validate_metrics.ValidationFailure) as ctx:
            validate_metrics.gate18_input_files_schema({"inputs/tariff_months.json": data}, REAL_METER_READ_DAY, REAL_SELL_FIT)
        self.assertEqual(ctx.exception.gate, "G18")

    def test_tariff_conflict_is_an_allowed_excluded_reason(self):
        # QA指摘L4: import_official_inputs.py の fuel/capacity 競合を表す理由コード
        # "tariff_conflict" が validate_metrics.py 側の enum にも含まれること。
        data = make_tariff_months_fixture(excluded={"2026-09": "tariff_conflict"})
        validate_metrics.gate18_input_files_schema({"inputs/tariff_months.json": data}, REAL_METER_READ_DAY, REAL_SELL_FIT)

    def test_wrong_schema_version_is_rejected(self):
        data = dict(make_tariff_months_fixture(), schema_version=2)
        with self.assertRaises(validate_metrics.ValidationFailure) as ctx:
            validate_metrics.gate18_input_files_schema({"inputs/tariff_months.json": data}, REAL_METER_READ_DAY, REAL_SELL_FIT)
        self.assertEqual(ctx.exception.gate, "G18")


class Gate19ReconcileTest(unittest.TestCase):
    def test_matching_month_passes(self):
        official_buy = make_official_buy_fixture()
        validate_metrics.gate19_official_buy_reconcile(REAL_TARIFF, official_buy, None)

    def test_one_yen_tampering_is_rejected(self):
        months = [dict(make_official_buy_fixture()["months"][0], billed_yen=3661 + 1)]
        official_buy = make_official_buy_fixture(months=months)
        with self.assertRaises(validate_metrics.ValidationFailure) as ctx:
            validate_metrics.gate19_official_buy_reconcile(REAL_TARIFF, official_buy, None)
        self.assertEqual(ctx.exception.gate, "G19")

    def test_unconfirmed_month_is_skipped_without_tariff_months(self):
        # 2026-09は実tariff.jsonでは未確定(fuel/capacityが無い)ため、official_buyに含めても
        # confirmed_tariff_monthsに入らずG19の対象外になる。
        months = [
            {
                "settlement_month": "2026-09", "period_from": "2026-08-02", "period_to": "2026-09-01",
                "official_buy_kwh": 1.0, "billed_yen": 999999,
            }
        ]
        official_buy = make_official_buy_fixture(months=months)
        validate_metrics.gate19_official_buy_reconcile(REAL_TARIFF, official_buy, None)

    def test_newly_confirmed_month_via_tariff_months_is_checked(self):
        tariff_months = make_tariff_months_fixture(
            fuel={"2026-09": 5.0}, capacity={"2026-09": 200}, levy={"2026-09..2026-09": 4.18},
        )
        # compute_bill(merge(REAL_TARIFF, tariff_months), 50.0, "2026-09").total_yen = 2009
        # (test_import_official_inputs.pyのCORRECT_BILLED_YENと同じ手計算)
        months = [
            {
                "settlement_month": "2026-09", "period_from": "2026-08-02", "period_to": "2026-09-01",
                "official_buy_kwh": 50.0, "billed_yen": 2009 + 1,
            }
        ]
        official_buy = make_official_buy_fixture(months=months)
        with self.assertRaises(validate_metrics.ValidationFailure) as ctx:
            validate_metrics.gate19_official_buy_reconcile(REAL_TARIFF, official_buy, tariff_months)
        self.assertEqual(ctx.exception.gate, "G19")

    def test_g19_rejects_tariff_month_without_reconciled_official_buy(self):
        """QA指摘F1: overlay(tariff_months)がbaseに対して新たに確定させた月は、
        official_buy.jsonに対応する月が存在しreconcile_bill==0であることを必須にする。
        (a) official_buy自体が無い、(b) official_buyはあるが対象月が無い、の両方を拒否する。"""
        tariff_months = make_tariff_months_fixture(
            fuel={"2026-09": 5.0}, capacity={"2026-09": 200}, levy={"2026-09..2026-09": 4.18},
        )
        # (a) official_buy が None
        with self.assertRaises(validate_metrics.ValidationFailure) as ctx:
            validate_metrics.gate19_official_buy_reconcile(REAL_TARIFF, None, tariff_months)
        self.assertEqual(ctx.exception.gate, "G19")

        # (b) official_buy はあるが 2026-09 が含まれない（既存の2026-08分のみ）
        official_buy_without_target = make_official_buy_fixture()
        with self.assertRaises(validate_metrics.ValidationFailure) as ctx:
            validate_metrics.gate19_official_buy_reconcile(REAL_TARIFF, official_buy_without_target, tariff_months)
        self.assertEqual(ctx.exception.gate, "G19")

    def test_g19_base_confirmed_month_without_official_buy_entry_is_not_required(self):
        """overlay(tariff_months)が新規に確定させたのではなく、base単独で以前から確定
        済みの月(2026-08)は、official_buy.jsonに対応する月が無くても reject されない
        （F1はoverlay-added月にのみ厳密要求を課す）。"""
        official_buy_without_2026_08 = {"months": [], "generated_at": make_official_buy_fixture()["generated_at"], "source_note": import_official_buy.SOURCE_NOTE}
        validate_metrics.gate19_official_buy_reconcile(REAL_TARIFF, official_buy_without_2026_08, None)


class Gate13IncomingInputsHashTest(unittest.TestCase):
    def test_mismatch_against_incoming_inputs_is_fatal(self):
        official_buy = make_official_buy_fixture()
        with tempfile.TemporaryDirectory() as tmp:
            incoming = write_incoming(Path(tmp), inputs={"official_buy.json": official_buy})
            pipeline = make_pipeline_fixture()
            pipeline["inputs"]["official_buy_sha256"] = "0" * 64  # incoming の実ファイルと不一致
            (incoming / "pipeline.json").write_text(json.dumps(pipeline, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
            with self.assertRaises(validate_metrics.ValidationFailure) as ctx:
                validate_metrics.validate(incoming, REPO_ROOT, allow_history_change=False)
            self.assertEqual(ctx.exception.gate, "G13")

    def test_match_against_incoming_inputs_passes_without_warning(self):
        official_buy = make_official_buy_fixture()
        with tempfile.TemporaryDirectory() as tmp:
            incoming = write_incoming(Path(tmp), inputs={"official_buy.json": official_buy})
            expected_sha = hashlib.sha256((incoming / "inputs" / "official_buy.json").read_bytes()).hexdigest()
            pipeline = make_pipeline_fixture()
            pipeline["inputs"]["official_buy_sha256"] = expected_sha
            (incoming / "pipeline.json").write_text(json.dumps(pipeline, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
            warnings = validate_metrics.validate(incoming, REPO_ROOT, allow_history_change=False)
            self.assertFalse(any("official_buy_sha256" in w for w in warnings))

    def test_migration_period_without_incoming_inputs_falls_back_to_warning(self):
        # incoming に inputs/official_buy.json が無ければ、従来どおり main 実ファイルとの
        # 照合で警告のみ（fatalにしない）。
        with tempfile.TemporaryDirectory() as tmp:
            incoming = write_incoming(Path(tmp))
            pipeline = make_pipeline_fixture()
            pipeline["inputs"]["official_buy_sha256"] = "0" * 64
            (incoming / "pipeline.json").write_text(json.dumps(pipeline, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
            warnings = validate_metrics.validate(incoming, REPO_ROOT, allow_history_change=False)
            self.assertTrue(any("official_buy_sha256" in w for w in warnings))

    def test_missing_sha_key_while_incoming_file_exists_is_fatal(self):
        # QA指摘L2: incomingにinputs/official_buy.jsonが実在するのに、pipeline.jsonに
        # 対応するshaキーが無ければ（検証を素通りする穴になるため）fatalにする。
        official_buy = make_official_buy_fixture()
        with tempfile.TemporaryDirectory() as tmp:
            incoming = write_incoming(Path(tmp), inputs={"official_buy.json": official_buy})
            pipeline = make_pipeline_fixture()
            del pipeline["inputs"]["official_buy_sha256"]
            (incoming / "pipeline.json").write_text(json.dumps(pipeline, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
            with self.assertRaises(validate_metrics.ValidationFailure) as ctx:
                validate_metrics.validate(incoming, REPO_ROOT, allow_history_change=False)
            self.assertEqual(ctx.exception.gate, "G13")


class Gate9TariffConfirmationExceptionTest(unittest.TestCase):
    """G9の例外（DDR実装手順S1「G9 の例外」）: 新たに確定した請求月に属する日は、
    変化したキーが {"saving_yen"} の部分集合のときだけ許可する。"""

    def test_saving_yen_only_change_in_newly_confirmed_month_is_allowed(self):
        # billing_month_for_date("2026-07-15", meter_read_day=2) == "2026-08"
        old_row = dict(make_daily_fixture()[0], date="2026-07-15", saving_yen=100)
        new_row = dict(old_row, saving_yen=999)
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            incoming = _git_repo_with_two_commits(base, old_daily=[old_row], new_daily=[new_row])
            validate_metrics.gate9_history_immutability(
                [new_row], incoming, allow_history_change=False,
                meter_read_day=REAL_METER_READ_DAY, newly_confirmed_months={"2026-08"},
            )  # 例外が無ければ ValidationFailure になるはず(raiseしなければ合格)

    def test_change_of_other_field_in_newly_confirmed_month_is_still_rejected(self):
        old_row = dict(make_daily_fixture()[0], date="2026-07-15", saving_yen=100, solar_kwh=10.0)
        new_row = dict(old_row, saving_yen=999, solar_kwh=20.0)
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            incoming = _git_repo_with_two_commits(base, old_daily=[old_row], new_daily=[new_row])
            with self.assertRaises(validate_metrics.ValidationFailure) as ctx:
                validate_metrics.gate9_history_immutability(
                    [new_row], incoming, allow_history_change=False,
                    meter_read_day=REAL_METER_READ_DAY, newly_confirmed_months={"2026-08"},
                )
            self.assertEqual(ctx.exception.gate, "G9")

    def test_change_outside_newly_confirmed_months_is_rejected(self):
        old_row = dict(make_daily_fixture()[0], date="2026-08-03", saving_yen=100)
        new_row = dict(old_row, saving_yen=999)
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            incoming = _git_repo_with_two_commits(base, old_daily=[old_row], new_daily=[new_row])
            with self.assertRaises(validate_metrics.ValidationFailure) as ctx:
                validate_metrics.gate9_history_immutability(
                    [new_row], incoming, allow_history_change=False,
                    meter_read_day=REAL_METER_READ_DAY, newly_confirmed_months=set(),
                )
            self.assertEqual(ctx.exception.gate, "G9")

    def test_end_to_end_newly_confirmed_month_via_tariff_months_allows_saving_yen_change(self):
        """validate()を通した統合テスト: 前回コミットにtariff_months.json(未確定)が無く、
        今回のコミットで2026-09（実tariff.jsonでは未確定、overlayが新規に確定させる月）を
        新たに確定させた場合、確定済み日(cutoff以前)のsaving_yenだけの変化がG9で許可される
        ことを確認する。QA指摘F1によりoverlay-added月はofficial_buy.jsonでの請求突合が
        必須になったため、対応するinputs/official_buy.jsonも合わせて用意する。"""
        old_daily_rows = _old_daily_fixture()
        # billing_month_for_date("2026-08-10", meter_read_day=2) == "2026-09"
        confirmed_day = dict(old_daily_rows[0], date="2026-08-10", saving_yen=100)
        old_daily_rows = [confirmed_day] + old_daily_rows[1:]
        new_daily_rows = [dict(confirmed_day, saving_yen=999)] + old_daily_rows[1:]

        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            write_incoming(base, daily=old_daily_rows, monthly=make_monthly_fixture(old_daily_rows))
            subprocess.run(["git", "init", "-q"], cwd=base, check=True)
            subprocess.run(["git", "config", "user.email", "test@example.com"], cwd=base, check=True)
            subprocess.run(["git", "config", "user.name", "test"], cwd=base, check=True)
            subprocess.run(["git", "add", "-A"], cwd=base, check=True)
            subprocess.run(["git", "commit", "-q", "-m", "commit1(no tariff_months)"], cwd=base, check=True)

            target_month = "2026-09"
            self.assertNotIn(target_month, validate_metrics.bill_model.confirmed_tariff_months(REAL_TARIFF))
            tariff_months = make_tariff_months_fixture(
                fuel={target_month: 5.0}, capacity={target_month: 200},
            )
            effective = validate_metrics.bill_model.merge_tariff(REAL_TARIFF, tariff_months)
            usage_kwh = 50.0
            billed_yen = validate_metrics.bill_model.compute_bill(effective, usage_kwh, target_month).total_yen
            start, end = validate_metrics.bill_model.billing_period(target_month, REAL_METER_READ_DAY)
            official_buy = make_official_buy_fixture(months=[{
                "settlement_month": target_month, "period_from": start.isoformat(), "period_to": end.isoformat(),
                "official_buy_kwh": usage_kwh, "billed_yen": billed_yen,
            }])
            write_incoming(
                base, daily=new_daily_rows, monthly=make_monthly_fixture(new_daily_rows),
                inputs={"tariff_months.json": tariff_months, "official_buy.json": official_buy},
            )
            # G13(L2)はinputs/official_buy.json・inputs/tariff_months.jsonが実在するなら
            # pipeline.json側のハッシュも要求するため、実際に書いたファイルに合わせる。
            pipeline = make_pipeline_fixture()
            pipeline["inputs"]["official_buy_sha256"] = _sha256(base / "inputs" / "official_buy.json")
            pipeline["inputs"]["tariff_months_sha256"] = _sha256(base / "inputs" / "tariff_months.json")
            (base / "pipeline.json").write_text(json.dumps(pipeline, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
            subprocess.run(["git", "add", "-A"], cwd=base, check=True)
            subprocess.run(["git", "commit", "-q", "-m", "commit2(newly confirmed 2026-09)"], cwd=base, check=True)

            warnings = validate_metrics.validate(base, REPO_ROOT, allow_history_change=False)
            self.assertEqual(warnings, [])

    def test_g9_first_seed_does_not_treat_base_months_as_newly_confirmed(self):
        """QA指摘F2の反例: tariff_months.jsonが今回のコミットで初めて現れても（前コミットに
        tariff_months.json自体が無い＝prev_overlay is None）、base単独で以前から確定済み
        だった月(2026-08)まで「新たに確定した」扱いにしてはならない。ここでは意図的に
        saving_yenだけを変える（もし2026-08が誤って「新たに確定した」扱いになれば、修正前の
        バグではG9の例外に該当し許可されてしまっていたはずの変更）。overlay(tariff_months)
        は2026-08と無関係な月(2026-10)だけを追加する。"""
        old_daily_rows = _old_daily_fixture()
        # billing_month_for_date("2026-07-15", meter_read_day=2) == "2026-08"（base単独で確定済み）
        confirmed_day = dict(old_daily_rows[0], date="2026-07-15", saving_yen=100)
        old_daily_rows = [confirmed_day] + old_daily_rows[1:]
        new_daily_rows = [dict(confirmed_day, saving_yen=999)] + old_daily_rows[1:]

        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            write_incoming(base, daily=old_daily_rows, monthly=make_monthly_fixture(old_daily_rows))
            subprocess.run(["git", "init", "-q"], cwd=base, check=True)
            subprocess.run(["git", "config", "user.email", "test@example.com"], cwd=base, check=True)
            subprocess.run(["git", "config", "user.name", "test"], cwd=base, check=True)
            subprocess.run(["git", "add", "-A"], cwd=base, check=True)
            subprocess.run(["git", "commit", "-q", "-m", "commit1(no tariff_months)"], cwd=base, check=True)

            # tariff_monthsは2026-08と無関係な将来月(2026-10)だけを含む
            tariff_months = make_tariff_months_fixture(fuel={"2026-10": 5.0}, capacity={"2026-10": 200})
            write_incoming(
                base, daily=new_daily_rows, monthly=make_monthly_fixture(new_daily_rows),
                inputs={"tariff_months.json": tariff_months},
            )
            subprocess.run(["git", "add", "-A"], cwd=base, check=True)
            subprocess.run(["git", "commit", "-q", "-m", "commit2(tariff_months first appears)"], cwd=base, check=True)

            with self.assertRaises(validate_metrics.ValidationFailure) as ctx:
                validate_metrics.validate(base, REPO_ROOT, allow_history_change=False)
            self.assertEqual(ctx.exception.gate, "G9")


    def test_g9_conflicting_previous_tariff_months_does_not_widen_exception_to_base_months(self):
        """再 QA 指摘 R1: 前コミットの tariff_months が base と矛盾（2026-08 の燃料費が 1 円違う）し、
        今回それを除去したコミット（run-daily の tariff_conflict 自動除去と同じ形）でも、
        base 単独で確定済みの 2026-08 に属する日の saving_yen 書き換えは G9 で拒否されること。"""
        old_daily_rows = _old_daily_fixture()
        confirmed_day = dict(old_daily_rows[0], date="2026-07-15", saving_yen=100)
        old_daily_rows = [confirmed_day] + old_daily_rows[1:]
        new_daily_rows = [dict(confirmed_day, saving_yen=999)] + old_daily_rows[1:]

        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            conflicting = make_tariff_months_fixture(fuel={"2026-08": 11.38}, capacity={"2026-08": 213})
            write_incoming(
                base, daily=old_daily_rows, monthly=make_monthly_fixture(old_daily_rows),
                inputs={"tariff_months.json": conflicting},
            )
            subprocess.run(["git", "init", "-q"], cwd=base, check=True)
            subprocess.run(["git", "config", "user.email", "test@example.com"], cwd=base, check=True)
            subprocess.run(["git", "config", "user.name", "test"], cwd=base, check=True)
            subprocess.run(["git", "add", "-A"], cwd=base, check=True)
            subprocess.run(["git", "commit", "-q", "-m", "commit1(conflicting tariff_months)"], cwd=base, check=True)

            cleaned = make_tariff_months_fixture(excluded={"2026-08": "tariff_conflict"})
            write_incoming(
                base, daily=new_daily_rows, monthly=make_monthly_fixture(new_daily_rows),
                inputs={"tariff_months.json": cleaned},
            )
            subprocess.run(["git", "add", "-A"], cwd=base, check=True)
            subprocess.run(["git", "commit", "-q", "-m", "commit2(conflict removed)"], cwd=base, check=True)

            with self.assertRaises(validate_metrics.ValidationFailure) as ctx:
                validate_metrics.validate(base, REPO_ROOT, allow_history_change=False)
            self.assertEqual(ctx.exception.gate, "G9")


class CheckInputsDirTest(unittest.TestCase):
    def test_valid_inputs_dir_passes(self):
        with tempfile.TemporaryDirectory() as tmp:
            inputs_dir = Path(tmp) / "inputs"
            inputs_dir.mkdir()
            (inputs_dir / "official_buy.json").write_text(
                json.dumps(make_official_buy_fixture(), ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8",
            )
            tariff_path = Path(tmp) / "tariff.json"
            tariff_path.write_text(json.dumps(REAL_TARIFF), encoding="utf-8")
            validate_metrics.check_inputs_dir(inputs_dir, tariff_path)  # raiseしなければ合格

    def test_invalid_inputs_dir_raises(self):
        with tempfile.TemporaryDirectory() as tmp:
            inputs_dir = Path(tmp) / "inputs"
            inputs_dir.mkdir()
            months = [dict(make_official_buy_fixture()["months"][0], billed_yen=3661 + 1)]
            (inputs_dir / "official_buy.json").write_text(
                json.dumps(make_official_buy_fixture(months=months), ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8",
            )
            tariff_path = Path(tmp) / "tariff.json"
            tariff_path.write_text(json.dumps(REAL_TARIFF), encoding="utf-8")
            with self.assertRaises(validate_metrics.ValidationFailure) as ctx:
                validate_metrics.check_inputs_dir(inputs_dir, tariff_path)
            self.assertEqual(ctx.exception.gate, "G19")

    def test_cli_check_inputs_dir_exits_nonzero_on_failure(self):
        with tempfile.TemporaryDirectory() as tmp:
            inputs_dir = Path(tmp) / "inputs"
            inputs_dir.mkdir()
            months = [dict(make_official_buy_fixture()["months"][0], billed_yen=3661 + 1)]
            (inputs_dir / "official_buy.json").write_text(
                json.dumps(make_official_buy_fixture(months=months), ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8",
            )
            tariff_path = Path(tmp) / "tariff.json"
            tariff_path.write_text(json.dumps(REAL_TARIFF), encoding="utf-8")

            script = str(Path(__file__).resolve().parent / "validate_metrics.py")
            proc = subprocess.run(
                [sys.executable, script, "--check-inputs-dir", str(inputs_dir), "--tariff-path", str(tariff_path)],
                capture_output=True, text=True,
            )
            self.assertNotEqual(proc.returncode, 0)
            self.assertIn("G19", proc.stderr)

    def test_cli_check_inputs_dir_exits_zero_on_success(self):
        with tempfile.TemporaryDirectory() as tmp:
            inputs_dir = Path(tmp) / "inputs"
            inputs_dir.mkdir()
            (inputs_dir / "official_buy.json").write_text(
                json.dumps(make_official_buy_fixture(), ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8",
            )
            tariff_path = Path(tmp) / "tariff.json"
            tariff_path.write_text(json.dumps(REAL_TARIFF), encoding="utf-8")

            script = str(Path(__file__).resolve().parent / "validate_metrics.py")
            proc = subprocess.run(
                [sys.executable, script, "--check-inputs-dir", str(inputs_dir), "--tariff-path", str(tariff_path)],
                capture_output=True, text=True,
            )
            self.assertEqual(proc.returncode, 0, proc.stderr)


if __name__ == "__main__":
    unittest.main()
