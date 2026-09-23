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
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

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


def write_incoming(base: Path, *, daily=None, monthly=None, meta=None, bills=None, layers=None, pipeline=None, readme=None) -> Path:
    """有効な incoming ディレクトリを作る（値渡しがあれば差し替え）。"""
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
    return base


class BaselineTest(unittest.TestCase):
    """現行の bills.json/layers.json/meta.json（実ファイル）+ 新スキーマ daily/monthly が
    全ゲートを通過すること（回帰の土台）。"""

    def test_current_files_pass_all_gates_without_warnings(self):
        with tempfile.TemporaryDirectory() as tmp:
            incoming = write_incoming(Path(tmp))
            warnings = validate_metrics.validate(incoming, REPO_ROOT, allow_history_change=False)
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
                       "Japan電力の請求明細", "japaden.jpを参照", "くらしプランSの単価"):
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


class Gate11Test(unittest.TestCase):
    def test_monthly_saving_yen_swing_is_rejected(self):
        monthly = [
            {"month": "2026-08", "solar_kwh": 1.0, "buy_kwh": 0.0, "sell_kwh": 0.0, "nichicon_charge_kwh": None, "ecoflow_charge_kwh": None, "self_consumption_shift_kwh": None, "saving_yen": 100},
            {"month": "2026-09", "solar_kwh": 1.0, "buy_kwh": 0.0, "sell_kwh": 0.0, "nichicon_charge_kwh": None, "ecoflow_charge_kwh": None, "self_consumption_shift_kwh": None, "saving_yen": 5000},
        ]
        with tempfile.TemporaryDirectory() as tmp:
            incoming = write_incoming(Path(tmp), monthly=monthly)
            with self.assertRaises(validate_metrics.ValidationFailure) as ctx:
                validate_metrics.validate(incoming, REPO_ROOT, allow_history_change=False)
            self.assertEqual(ctx.exception.gate, "G11")

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


if __name__ == "__main__":
    unittest.main()
