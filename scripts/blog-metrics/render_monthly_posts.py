#!/usr/bin/env python3
"""render_monthly_posts.py — data repo（solar-metrics-data、private、homelab運用）から
渡された posts/YYYY-MM.json（数値だけのスナップショット）を読み、月次レポート記事の
Markdown を生成する。CIのビルド時（main側の信頼済みコード）でのみ実行する。

設計判断（2026-09-23/26、「速報＋改訂」方式）: homelab はMarkdownを作らない。文言・分岐は
すべてこのファイル（main）にあるため、homelabが侵害されても任意の文言は公開されない
（posts/*.json の値は validate_metrics.py のG14で日付・月・列挙値以外の文字列を全て拒否する）。

使い方（.github/workflows/hugo.yml から呼ばれる）:
  python3 render_monthly_posts.py --posts-dir _incoming/posts --out content/posts/monthly-report
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import monthly_report  # noqa: E402  FIRST_REPORT_BILLING_MONTHの防波堤を強制するため(QA指摘2026-09-26 item3)
import validate_metrics  # noqa: E402  G6 deny パターン・時間帯粒度チェックを再利用する（二重実装しない）

POST_FILE_RE = re.compile(r"^(\d{4}-\d{2})\.json\Z", re.ASCII)

# main をコミットするだけで特定の請求月の記事を非公開にできる（DDR §C 停止スイッチ）。
SUPPRESSED_BILLING_MONTHS: frozenset[str] = frozenset()

METHODOLOGY_URL = "/metrics/"
SOLAR_CHARGE_CONTROLLER_URL = "/solar-charge-controller/"  # 追補(2026-09-26) C'

# 環境省・経済産業省「電気事業者別排出係数(特定排出者の温室効果ガス排出量算定用)-R6年度実績-」
# （令和8年提出用rev4、R8.1.9公表、R8.8.3一部追加・更新）。全国平均係数 0.000423 t-CO2/kWh
# （= 0.423 kg-CO2/kWh、PDF p.18「全国平均係数(t-CO2/kWh) 0.000423」。取得日2026-09-23。
# REASONED。同頁の「代替値0.000416」は不明事業者からの受電分の報告用であり「全国平均」を
# 名乗る値ではないため採用しない）。
CO2_FACTOR_URL = "https://policies.env.go.jp/earth/ghg-santeikohyo/files/calc/r08_denki_coefficient_rev4.pdf"
CO2_FACTORS = [
    {
        "from_billing_month": "2026-10",
        "t_per_kwh": 0.000423,
        "label": "令和6年度実績 全国平均係数",
        "url": CO2_FACTOR_URL,
        "fetched": "2026-09-23",
    },
]

ALLOWED_LINK_TARGETS = {METHODOLOGY_URL, SOLAR_CHARGE_CONTROLLER_URL, CO2_FACTOR_URL} | {
    f["url"] for f in CO2_FACTORS
}

# QA指摘2026-09-26 item11: ダッシュボード(metrics-dashboard.js の LAYER_NAMES)の表記に揃える
# （「推定」ではなく「試算」）。
_FOUR_LAYER_LABELS_L0_L2 = {
    "L0": "太陽光・蓄電池なし（試算）",
    "L1": "太陽光のみ（試算）",
    "L2": "太陽光＋家庭用蓄電池（試算）",
}

_LINK_RE = re.compile(r"\]\(([^)]*)\)")
_FRONT_MATTER_KEY_RE = re.compile(r"^(\w+):")
_EXPECTED_FRONT_MATTER_KEYS = {"title", "date", "lastmod", "draft", "tags", "summary"}


class DuplicateReportMonthError(SystemExit):
    """2つの異なるbilling_monthが同じreport_month(=出力ファイル名)を主張した場合に送出する
    （QA指摘2026-09-26 item3。billing_month<->report_monthはbill_model.billing_period経由で
    本来1対1のはずだが、validate_metrics.pyのG14を経由しない呼び出しに備えた多重防御）。"""


def _co2_factor_for(billing_month: str) -> dict | None:
    """billing_month以下で最新のCO2係数エントリを返す（新年度の値が出たらエントリを追加し、
    過去記事は旧い係数のままにする。DDR §C）。"""
    candidates = [f for f in CO2_FACTORS if f["from_billing_month"] <= billing_month]
    if not candidates:
        return None
    return max(candidates, key=lambda f: f["from_billing_month"])


def _yen(value: int | float) -> str:
    return f"{round(value):,}円"


def _kwh(value: float) -> str:
    return f"{value:.1f}kWh"


def _jp_date(iso_str: str) -> str:
    """'2026-10-20' -> '2026年10月20日'（QA指摘2026-09-26 item10の日付書式）。"""
    y, m, d = (int(x) for x in iso_str.split("-"))
    return f"{y}年{m}月{d}日"


def _pct_change(new: float | None, old: float | None) -> float | None:
    if new is None or old is None or old == 0:
        return None
    return (new - old) / old * 100


def _comparison_sentence(label: str, pct: float | None) -> str | None:
    if pct is None:
        return None
    if abs(pct) < 10:
        return f"{label}とほぼ同じ発電量でした。"
    direction = "多い" if pct > 0 else "少ない"
    return f"{label}より{round(abs(pct))}%{direction}発電量でした。"


def _buy_row_label(l3_source: dict) -> str:
    return "買電量（請求書）" if l3_source["buy"] == "billed" else "買電量（計測値）"


def _sell_row_label(l3_source: dict) -> str:
    return "売電量（検針値）" if l3_source["sell"] == "official_meter" else "売電量（計測値）"


# --- W（天候）パターン ----------------------------------------------------------------------
def _weather_class(weather: dict) -> tuple[str, float | None]:
    days = sum(weather.values())
    unknown = weather["unknown_days"]
    if days == 0 or unknown > days / 2:
        return "W0", None
    sunny_share = weather["sunny_days"] / (days - unknown)
    if sunny_share >= 0.6:
        return "W1", sunny_share
    if sunny_share >= 0.3:
        return "W2", sunny_share
    return "W3", sunny_share


_WEATHER_SENTENCES = {
    "W0": "判定できる日が少ないため、天候の傾向は示しません。",
    "W1": "晴れの日が多い月でした。",
    "W2": "晴れと曇りが入り混じった月でした。",
    "W3": "曇りや雨の日が多い月でした。",
}


# --- H（まとめ）パターン ---------------------------------------------------------------------
def _headline_sentence(l0_fit: int, l3_fit: int) -> str:
    saving = l0_fit - l3_fit
    if l3_fit == 0:
        # QA指摘2026-09-26 item11: ちょうど同額のときは「上回りました」は不自然。
        return f"電気代と売電収入が同額でした。太陽光・蓄電池なしの試算（{_yen(l0_fit)}）と比べて{_yen(saving)}の差です。"
    if l3_fit < 0:
        return f"売電収入が電気代を上回りました。太陽光・蓄電池なしの試算（{_yen(l0_fit)}）と比べて{_yen(saving)}の差です。"
    return f"実質の電気代は{_yen(l3_fit)}でした。太陽光・蓄電池なしの試算（{_yen(l0_fit)}）と比べて{_yen(saving)}の差です。"


# --- S（SolarChargeControllerの効果）パターン -------------------------------------------------
_BATTERY_ONLY_ESTIMATE = "家庭用蓄電池だけの試算"


def _scc_sentence(
    l3_fit: int, l2_fit: int, band_min: int, band_max: int, sunny_share: float | None,
) -> tuple[str, bool]:
    """S1〜S6の文を組み立てる。戻り値: (本文, B文(停電時の備え)を付けるか)。
    符号の約束: scc = L2 − L3。正ならSCCが得をした月。晴れ多め/少なめの閾値は0.5
    （W1〜W3判定(0.6/0.3)とは別の、S判定専用の閾値。sunny_shareがNone(天候不明、W0)ならS6）。"""
    scc = l2_fit - l3_fit
    band = f"（{_yen(band_min)}〜{_yen(band_max)}）"
    weather_known = sunny_share is not None
    sunny_majority = weather_known and sunny_share >= 0.5

    if l3_fit < band_min:
        text = f"{_BATTERY_ONLY_ESTIMATE}より{_yen(scc)}安くなりました。試算の誤差幅{band}を超える差です。"
        if not weather_known:
            return text, False  # S6（below band）
        if sunny_majority:
            return text + "余剰の多い日に、ポータブル電源へ電気を振り分けた分が効いています。", False  # S1
        return "日照が少ない月でも、" + text, False  # S2
    if l3_fit > band_max:
        loss = _yen(scc * -1)
        if not weather_known:
            return f"今月は{_BATTERY_ONLY_ESTIMATE}より{loss}高くなりました。", True  # S6（above band）
        if sunny_majority:
            return f"晴れの日が多かったにもかかわらず、{_BATTERY_ONLY_ESTIMATE}より{loss}高くなりました。この集計だけでは原因を特定できません。", True  # S5
        return (
            f"今月は{_BATTERY_ONLY_ESTIMATE}より{loss}高くなりました。曇りや雨の日が多く余剰が少なかったため、"
            "ポータブル電源の充放電・変換ロスと待機電力が、振り分けで得られる効果を上回ったと考えられます。"
        ), True  # S4
    return (
        f"実測（{_yen(l3_fit)}）は、{_BATTERY_ONLY_ESTIMATE}の誤差幅{band}の中に収まりました。"
        "今月は差があるとは言えません。",
        False,
    )  # S3（天候不明でも同じ文、S6のband内側）


_STANDBY_NOTE = "電気代の面では不利でしたが、ポータブル電源に電気を蓄えておくことは停電時の備えにもなります（この価値は上の金額に含まれていません）。"


def _preliminary_intro_sentence(tariff_basis: str, l3_source: dict) -> str:
    """QA再指摘2026-09-26 R4: 速報の導入文をtariff_basis/l3_sourceから組み立てる。単価が
    既に確定していれば「前月の単価で仮計算」とは書かない。買電・売電のうち既に確定している
    方は「届くのを待っている」対象に含めない（stage固定の決め打ちをやめる）。
    QA再指摘2026-09-26 F1: tariff_basis==provisionalかつl3_sourceが両方確定(買電=billed・
    売電=official_meter)のとき、missingが空になり「電気料金は前月の単価で仮計算し、」で
    文が途切れていた（続く「実際の…」の文が無い）。missingが空のときは単価の確定待ちだけを
    閉じた文にする。"""
    sentence = "この記事は速報です。"
    missing = []
    if l3_source["buy"] != "billed":
        missing.append(("買電", "請求書"))
    if l3_source["sell"] != "official_meter":
        missing.append(("売電", "検針値"))
    if tariff_basis == "provisional" and not missing:
        return sentence + "電気料金は前月の単価で仮計算しています。単価が確定したら確定版に更新します。"
    if tariff_basis == "provisional":
        sentence += "電気料金は前月の単価で仮計算し、"
    if missing:
        amounts = "・".join(label for label, _ in missing)
        sentence += f"実際の{amounts}量はセンサーの計測値から求めています。"
        sources = "と".join(src for _, src in missing)
        sentence += f"{sources}が届いたら確定版に更新します。"
    return sentence


# --- 前面（front matter）---------------------------------------------------------------------
def _front_matter(snapshot: dict, summary: str) -> str:
    year, month = snapshot["report_month"].split("-")
    stage = snapshot["stage"]
    title = f"{int(year)}年{int(month)}月の発電と電気代レポート"
    if stage == "preliminary":
        title += "（速報）"
    first_published = snapshot["first_published"]
    lastmod = snapshot["revised"] or first_published
    tags = '["月次レポート", "実測データ", "SolarChargeController"]'
    lines = [
        "---",
        f'title: "{title}"',
        f"date: {first_published}T00:00:00+09:00",
        f"lastmod: {lastmod}T00:00:00+09:00",
        "draft: false",
        f"tags: {tags}",
        f'summary: "{summary}"',
        "---",
        "",
    ]
    return "\n".join(lines)


def render_markdown(snapshot: dict) -> str:
    """posts/YYYY-MM.jsonのスナップショット1件からMarkdown本文を組み立てる（DDR §C）。"""
    stage = snapshot["stage"]
    usage_period = snapshot["usage_period"]
    start_y, start_m, start_d = (int(x) for x in usage_period["start"].split("-"))
    _end_y, end_m, end_d = (int(x) for x in usage_period["end"].split("-"))
    layers = snapshot["layers"]
    l0, l1, l2, l3 = layers["L0"], layers["L1"], layers["L2"], layers["L3"]
    l3_source = snapshot["l3_source"]
    band = snapshot["l2_band"]
    energy = snapshot["energy"]
    weather = snapshot["weather"]
    comparison = snapshot["comparison"]
    revision = snapshot["revision"]

    weather_tag, sunny_share = _weather_class(weather)
    headline = _headline_sentence(l0["net_cost_fit_yen"], l3["net_cost_fit_yen"])
    # QA再指摘2026-09-26 スタイル: 「…の差です。（速報）」ではなく「…の差です（速報）。」の順にする。
    summary = headline.rstrip("。") + "（速報）。" if stage == "preliminary" else headline

    parts = [_front_matter(snapshot, summary)]

    # 1. 導入
    intro = (
        f"電気の請求期間に合わせた{start_y}年{start_m}月{start_d}日〜{end_m}月{end_d}日"
        f"（{usage_period['days']}日間）の実測データから自動生成したレポートです。"
        f"数値は[実績ダッシュボード]({METHODOLOGY_URL})と同じデータに基づきます。"
    )
    if stage == "preliminary":
        intro += _preliminary_intro_sentence(snapshot["tariff_basis"], l3_source)
    elif stage == "final" and revision > 1 and snapshot.get("transitioned_from_preliminary"):
        # QA指摘2026-09-26 item10: 速報からの確定遷移だけこの文言にする。
        intro += f"{_jp_date(snapshot['revised'])}に請求書と検針値の数値で確定版に更新しました（第{revision}版）。"
    elif stage == "final" and revision > 1:
        intro += f"{_jp_date(snapshot['revised'])}に数値を更新しました（第{revision}版）。"
    parts.append(intro)
    parts.append("")

    # 2. まとめ（QA指摘2026-09-26 item11: 天候の文は「天候と発電」節のみにする）
    parts.append("## まとめ")
    parts.append(headline)
    table_rows = [("発電量", energy["solar_kwh"], _kwh)]
    table_rows.append((_buy_row_label(l3_source), l3["buy_kwh"], _kwh))
    table_rows.append((_sell_row_label(l3_source), l3["sell_kwh"], _kwh))
    solar_kwh, sell_sensor = energy["solar_kwh"], energy["sell_kwh_sensor"]
    self_consumption_pct = None
    if solar_kwh is not None and sell_sensor is not None and solar_kwh > 0:
        self_consumption_pct = round((solar_kwh - sell_sensor) / solar_kwh * 100, 1)
        if not (0 <= self_consumption_pct <= 100):
            self_consumption_pct = None
    if self_consumption_pct is not None:
        table_rows.append(("自家消費率", self_consumption_pct, lambda v: f"{v:.1f}%"))
    table_rows.append(("家庭用蓄電池への充電量", energy["nichicon_charge_kwh"], _kwh))
    table_rows.append(("ポータブル電源への充電量", energy["ecoflow_charge_kwh"], _kwh))
    table_rows.append(("買電の削減量（試算）", round(l0["buy_kwh"] - l3["buy_kwh"], 1), _kwh))
    factor = _co2_factor_for(snapshot["billing_month"])
    grid_reduction_kwh = l0["buy_kwh"] - l3["buy_kwh"]
    if factor is not None and grid_reduction_kwh > 0:
        co2_kg = round(grid_reduction_kwh * factor["t_per_kwh"] * 1000, 1)
        table_rows.append(("CO2排出削減量（試算）", co2_kg, lambda v: f"{v:.1f}kg"))
    parts.append("")
    parts.append("| 項目 | 値 |")
    parts.append("|---|---|")
    for label, value, fmt in table_rows:
        if value is None:
            continue
        parts.append(f"| {label} | {fmt(value)} |")
    parts.append("")
    if self_consumption_pct is not None:
        # QA指摘2026-09-26 item11: 注記は行を出したときだけ。方法論的な記述（分子・分母等）は削る。
        parts.append("自家消費率は発電量のうち自宅で使った割合です。")
        parts.append("")

    # 3. 構成別の電気代（QA指摘2026-09-26 item11: 列見出しをダッシュボードの表記に揃える。
    # QA再指摘2026-09-26 スタイル: 見出しを「電気代の4層比較」から改名）
    parts.append("## 構成別の電気代")
    parts.append("")
    parts.append("| 構成 | 実質電気代（売電16円で計算） | 実質電気代（売電8円で計算） | 買電 kWh | 売電 kWh |")
    parts.append("|---|---|---|---|---|")
    for key in ("L0", "L1", "L2"):
        row = layers[key]
        parts.append(
            f"| {_FOUR_LAYER_LABELS_L0_L2[key]} | {_yen(row['net_cost_fit_yen'])} | "
            f"{_yen(row['net_cost_post_fit_yen'])} | {_kwh(row['buy_kwh'])} | {_kwh(row['sell_kwh'])} |"
        )
    # QA再指摘2026-09-26 item4: 出典(請求書/検針値/センサー計測値)はこの行のラベルではなく
    # 「まとめ」節の買電量・売電量の行見出し(_buy_row_label/_sell_row_label)で伝える。
    parts.append(
        f"| ＋SolarChargeController（実測） | {_yen(l3['net_cost_fit_yen'])} | "
        f"{_yen(l3['net_cost_post_fit_yen'])} | {_kwh(l3['buy_kwh'])} | {_kwh(l3['sell_kwh'])} |"
    )
    parts.append("")
    parts.append("実質電気代 = 電気料金 − 売電収入。マイナスは受け取りが上回ったことを示します。")
    if snapshot["estimation"] == "scaled":
        parts.append(
            f"※試算3層は{snapshot['days_usable']}/{snapshot['days_total']}日分の実測から日数比で換算しています。"
        )
    parts.append("")

    # 4. SolarChargeController の効果
    parts.append("## SolarChargeController の効果")
    scc_text, add_standby_note = _scc_sentence(
        l3["net_cost_fit_yen"], l2["net_cost_fit_yen"], band["net_cost_fit_yen_min"], band["net_cost_fit_yen_max"],
        sunny_share,
    )
    parts.append(scc_text)
    if add_standby_note:
        parts.append(_STANDBY_NOTE)
    parts.append(f"仕組みは [SolarChargeController とは]({SOLAR_CHARGE_CONTROLLER_URL}) をご覧ください。")
    parts.append("")

    # 5. 天候と発電
    parts.append("## 天候と発電")
    if energy["solar_kwh"] is not None:
        parts.append(f"この期間の発電量は{_kwh(energy['solar_kwh'])}でした。")
    prev_sentence = _comparison_sentence("前月", _pct_change(energy["solar_kwh"], comparison["prev_solar_kwh"]))
    if prev_sentence:
        parts.append(prev_sentence)
    yoy_sentence = _comparison_sentence("前年同月", _pct_change(energy["solar_kwh"], comparison["yoy_solar_kwh"]))
    if yoy_sentence:
        parts.append(yoy_sentence)
    parts.append(_WEATHER_SENTENCES[weather_tag])
    parts.append("※気象データではなく、発電量だけから推定した分類です。")
    parts.append("")

    # 6. CO2排出削減量
    parts.append("## CO2排出削減量")
    if factor is None:
        parts.append("この請求月に適用できるCO2排出係数が未設定のため、推定できません。")
    elif grid_reduction_kwh <= 0:
        parts.append("太陽光・蓄電池なしの場合と比べた買電の削減はありませんでした。")
    else:
        co2_kg = round(grid_reduction_kwh * factor["t_per_kwh"] * 1000, 1)
        parts.append(
            f"太陽光・蓄電池なしの場合と比べて、買電量が{_kwh(grid_reduction_kwh)}減り、"
            f"CO2排出量を約{co2_kg:.1f}kg削減できたと試算されます"
            f"（[{factor['label']}]({factor['url']})で換算）。"
        )
        parts.append(
            "※この試算には売電分は含みません。FIT電源の環境価値は証書として別に取引されるため、"
            "二重に数えないようにしています。"
        )
        # QA指摘2026-09-26 item11: SCC分が負のときは「わずかに増えています」ではなくkWh数値を示す。
        # ちょうど0のときは触れる材料が無いため文を出さない。
        scc_buy_kwh = round(l2["buy_kwh"] - l3["buy_kwh"], 1)
        if scc_buy_kwh > 0:
            scc_co2_kg = round(scc_buy_kwh * factor["t_per_kwh"] * 1000, 1)
            parts.append(f"そのうちSolarChargeControllerの効果分は約{scc_co2_kg:.1f}kgです。")
        elif scc_buy_kwh < 0:
            parts.append(f"家庭用蓄電池だけの試算と比べると、ポータブル電源を使った分だけ買電が{_kwh(abs(scc_buy_kwh))}多くなりました。")
    parts.append("")

    # 7. この数字について（QA指摘2026-09-26 item5: コード名(L0〜L3)を出さない）
    parts.append("## この数字について")
    parts.append(
        "- 太陽光・蓄電池なし／太陽光のみ／太陽光＋家庭用蓄電池の3つは、実測データから組み立てたシミュレーションの試算値です。"
    )
    parts.append("- ＋SolarChargeControllerは実測値です。")
    parts.append("- 売電単価はFIT期間中16円、FIT終了後は8円と仮定しています。")
    parts.append("- 本記事は自動生成です。")
    parts.append("")

    return "\n".join(parts).rstrip("\n") + "\n"


def check_rendered(markdown: str) -> list[str]:
    """G17: レンダラの自己検査。違反メッセージのリストを返す（空なら合格）。"""
    problems: list[str] = []
    lines = markdown.split("\n")
    if not lines or lines[0] != "---":
        return ["front matter の開始行(---)がありません"]
    try:
        end_idx = lines.index("---", 1)
    except ValueError:
        return ["front matter の終端行(---)がありません"]
    front_lines = lines[1:end_idx]
    body = "\n".join(lines[end_idx + 1:])

    keys = set()
    draft_ok = False
    for line in front_lines:
        m = _FRONT_MATTER_KEY_RE.match(line)
        if m:
            keys.add(m.group(1))
            if line.strip() == "draft: false":
                draft_ok = True
    if keys != _EXPECTED_FRONT_MATTER_KEYS:
        problems.append(f"front matter のキーが不正です: {sorted(keys)}")
    if not draft_ok:
        problems.append("draft: false がありません")

    if "<" in body:
        problems.append("本文に '<' が含まれています")
    if "![" in body:
        problems.append("本文に '![' が含まれています")

    for m in _LINK_RE.finditer(body):
        target = m.group(1)
        if target not in ALLOWED_LINK_TARGETS:
            problems.append(f"許可されていないリンク先です: {target!r}")

    for pattern in validate_metrics._DENY_PATTERNS:
        m = pattern.search(body)
        if m:
            problems.append(f"本文に秘匿/事業者名の疑いがある文字列を検出しました: {m.group(0)!r}")
    if validate_metrics._TIME_OF_DAY_RE.search(body):
        problems.append("本文に時間帯粒度らしき文字列が含まれています")

    return problems


def run(posts_dir: Path, out_dir: Path) -> int:
    """posts_dir/*.json をレンダリングして out_dir に書き出す。戻り値: 書き出した件数。
    check_rendered が1件でも違反を返したら例外を送出しビルドを止める（G17）。異なる
    billing_monthが同じreport_month(出力ファイル名)を主張したらDuplicateReportMonthError
    （QA指摘2026-09-26 item3）。"""
    if not posts_dir.exists():
        return 0
    out_dir.mkdir(parents=True, exist_ok=True)
    written = 0
    seen_report_months: dict[str, str] = {}
    for path in sorted(posts_dir.glob("*.json")):
        m = POST_FILE_RE.match(path.name)
        if not m:
            continue
        billing_month = m.group(1)
        if billing_month in SUPPRESSED_BILLING_MONTHS:
            continue
        if billing_month < monthly_report.FIRST_REPORT_BILLING_MONTH:
            continue
        snapshot = json.loads(path.read_text(encoding="utf-8"))
        report_month = snapshot["report_month"]
        if report_month in seen_report_months:
            raise DuplicateReportMonthError(
                f"render_monthly_posts.py: report_month({report_month})がbilling_month "
                f"{seen_report_months[report_month]!r} と {billing_month!r} の両方から主張されています"
            )
        seen_report_months[report_month] = billing_month

        markdown = render_markdown(snapshot)
        problems = check_rendered(markdown)
        if problems:
            raise SystemExit(
                f"render_monthly_posts.py: {path.name}: 自己検査(G17)に失敗しました: {problems}"
            )
        out_path = out_dir / f"{report_month}.md"
        out_path.write_text(markdown, encoding="utf-8")
        written += 1
    return written


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--posts-dir", type=Path, required=True, help="posts/YYYY-MM.json のあるディレクトリ")
    parser.add_argument("--out", type=Path, required=True, help="Markdown の出力先ディレクトリ")
    args = parser.parse_args()

    written = run(args.posts_dir, args.out)
    print(f"render_monthly_posts.py: wrote {written} post(s) under {args.out}")


if __name__ == "__main__":
    main()
