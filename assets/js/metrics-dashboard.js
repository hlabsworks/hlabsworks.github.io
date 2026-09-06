/*
 * 実績ダッシュボードのグラフ描画。/metrics/ ページ専用（layouts/_partials/extend_footer.html から
 * .Type == "metrics" のときだけ読み込まれる）。
 *
 * データは <script type="application/json"> 経由で埋め込まれた daily.json / monthly.json /
 * bills.json / layers.json（scripts/blog-metrics/aggregate.sh, bill_model.py, layer_model.py の
 * 生成物）を読む。日次・月次の集計値のみを扱い、時間帯別の値やデバイス個体情報はそもそも
 * 埋め込まれていない。
 *
 * layers.json は4層（L0=太陽光・蓄電池なし/L1=太陽光のみ/L2=太陽光+蓄電池/L3=全部導入）の
 * 請求期間ベース比較（docs/design/20260905_layer-model-ddr.md、SolarChargeControllerリポジトリ）。
 * L0〜L2は推定、L3のみ実測。available:false の層は描画しない（捏造しない）。
 */
(function () {
  "use strict";

  function readJSON(id) {
    var el = document.getElementById(id);
    if (!el) return null;
    try {
      return JSON.parse(el.textContent);
    } catch (e) {
      console.error("metrics-dashboard: failed to parse " + id, e);
      return null;
    }
  }

  function yen(n) {
    if (n === null || n === undefined) return "―";
    return Math.round(n).toLocaleString("ja-JP") + " 円";
  }

  function kwh(n) {
    if (n === null || n === undefined) return "―";
    return n.toLocaleString("ja-JP", { maximumFractionDigits: 1 }) + " kWh";
  }

  function latestCompleteBillMonth(bills) {
    if (!bills || !bills.months || bills.months.length === 0) return null;
    return bills.months[bills.months.length - 1];
  }

  function diffPctText(diffPct) {
    if (diffPct === null || diffPct === undefined) return "";
    var sign = diffPct > 0 ? "+" : "";
    return "（センサー差 " + sign + diffPct.toFixed(1) + "%）";
  }

  function sellSummaryText(r) {
    if (r.sell_source === "tepco_official") {
      return kwh(r.sell_kwh_official) + diffPctText(r.sell_diff_pct) + "（東京電力パワーグリッド公式メーター実績）";
    }
    return kwh(r.sell_kwh) + "（センサー計測・公式突合未取得）";
  }

  // 読者向けQAレビュー対応（2026-09-06）: 理由は {reason_code, reason_label, reason_detail}
  // の3点セット（bill_model.py/layer_model.py が付与）。表示は reason_label のみを使い、
  // 内部ファイル名・キー名を含みうる reason_detail は title 属性（ホバー時のみ表示）に限定する。
  function escapeHtml(s) {
    if (s === null || s === undefined) return "";
    return String(s).replace(/[&<>"']/g, function (c) {
      return { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c];
    });
  }

  function reasonLabel(reasonObj) {
    return (reasonObj && reasonObj.reason_label) || "推定不可";
  }

  function reasonDetail(reasonObj) {
    return (reasonObj && reasonObj.reason_detail) || "";
  }

  function reasonSpan(text, detail) {
    return "<span title=\"" + escapeHtml(detail) + "\">" + escapeHtml(text) + "</span>";
  }

  // 反実仮想(L0)は復元負荷(load_true)が請求期間全日そろう月のみ表示する。サマリー行では
  // 理由を1回だけ「推定不可（理由ラベル）」の形で示す（同一行での理由文の重複を避ける）。
  function l0SummaryText(r) {
    if (r.bill_l0_no_solar) return yen(r.bill_l0_no_solar.total_yen);
    return "推定不可（" + escapeHtml(reasonLabel(r.l0_unavailable_reason)) + "）";
  }

  // 請求額再現表の「反実仮想」セル。本文は「推定不可」の一語のみにし、機械理由
  // (reason_detail、内部ファイル名等を含みうる)はtitle属性でホバー表示に限定する。
  function l0CellHtml(r) {
    if (r.bill_l0_no_solar) return "<td>" + yen(r.bill_l0_no_solar.total_yen) + "</td>";
    return "<td title=\"" + escapeHtml(reasonDetail(r.l0_unavailable_reason)) + "\">推定不可</td>";
  }

  // 節約額(saving_yen_*)セル。L0起因でnullの場合、反実仮想セルと同じ理由を繰り返さず
  // 「―」+titleのみにする（1行に同じ内部文言が複数回並ぶのを防ぐ）。
  function savingCellHtml(r, field) {
    if (r[field] === null || r[field] === undefined) {
      return "<td title=\"" + escapeHtml(reasonDetail(r.l0_unavailable_reason)) + "\">―</td>";
    }
    return "<td>" + yen(r[field]) + "</td>";
  }

  // サマリー行の節約額。反実仮想の行で既に理由ラベルを示しているため、こちらは
  // 「―」+title（ホバーで機械理由）のみにする。
  function savingSummaryHtml(r, field) {
    if (r[field] === null || r[field] === undefined) {
      return reasonSpan("―", reasonDetail(r.l0_unavailable_reason));
    }
    return yen(r[field]);
  }

  function renderSummary(monthly, bills) {
    var el = document.getElementById("metrics-summary");
    if (!el || !monthly || monthly.length === 0) return;
    var latest = monthly[monthly.length - 1];
    var html =
      "<ul>" +
      "<li>直近集計月（" + latest.month + "、月途中の可能性あり）</li>" +
      "<li>太陽光発電量: " + kwh(latest.solar_kwh) + "</li>" +
      "<li>買電量: " + kwh(latest.buy_kwh) + " / 売電量: " + kwh(latest.sell_kwh) + "</li>" +
      "<li>蓄電池(ニチコン)充電量: " + kwh(latest.nichicon_charge_kwh) + " / ポータブル電源(EcoFlow)充電量: " + kwh(latest.ecoflow_charge_kwh) + "</li>" +
      "<li>節約額試算（簡易・暦月集計）: " + yen(latest.saving_yen) + "</li>";
    var latestBill = latestCompleteBillMonth(bills);
    if (latestBill) {
      html +=
        "<li>直近の請求期間換算（" + latestBill.billing_month + "、" + latestBill.usage_period.start + "〜" + latestBill.usage_period.end + "）: " +
        "推定請求額 " + yen(latestBill.bill_actual.total_yen) +
        "（太陽光が無い場合の反実仮想: " + l0SummaryText(latestBill) + "）</li>" +
        "<li>売電量: " + sellSummaryText(latestBill) + "</li>" +
        "<li>節約額（請求実績単価ベース・FIT実態）: " + savingSummaryHtml(latestBill, "saving_yen_fit") +
        " / 節約額（卒FIT換算）: " + savingSummaryHtml(latestBill, "saving_yen_post_fit") + "</li>";
    }
    html += "</ul>";
    el.innerHTML = html;
  }

  function renderMonthlyEnergyChart(monthly) {
    var canvas = document.getElementById("chart-monthly-energy");
    if (!canvas || !window.Chart || !monthly || monthly.length === 0) return;
    new Chart(canvas.getContext("2d"), {
      type: "bar",
      data: {
        labels: monthly.map(function (r) { return r.month; }),
        datasets: [
          { label: "太陽光発電 (kWh)", data: monthly.map(function (r) { return r.solar_kwh; }), backgroundColor: "#f4a92b" },
          { label: "買電 (kWh)", data: monthly.map(function (r) { return r.buy_kwh; }), backgroundColor: "#c9484f" },
          { label: "売電 (kWh)", data: monthly.map(function (r) { return r.sell_kwh; }), backgroundColor: "#4d8fd6" },
          { label: "蓄電池+ポタ電 充電 (kWh)", data: monthly.map(function (r) { return r.self_consumption_shift_kwh; }), backgroundColor: "#3fa66b" }
        ]
      },
      options: {
        responsive: true,
        plugins: { legend: { position: "bottom" } },
        scales: { y: { beginAtZero: true, title: { display: true, text: "kWh" } } }
      }
    });
  }

  function renderMonthlySavingChart(monthly) {
    var canvas = document.getElementById("chart-monthly-saving");
    if (!canvas || !window.Chart || !monthly || monthly.length === 0) return;
    new Chart(canvas.getContext("2d"), {
      type: "bar",
      data: {
        labels: monthly.map(function (r) { return r.month; }),
        datasets: [
          { label: "節約額試算 (円)", data: monthly.map(function (r) { return r.saving_yen; }), backgroundColor: "#8a63d2" }
        ]
      },
      options: {
        responsive: true,
        plugins: { legend: { display: false } },
        scales: { y: { beginAtZero: true, title: { display: true, text: "円" } } }
      }
    });
  }

  function renderDailyEnergyChart(daily) {
    var canvas = document.getElementById("chart-daily-energy");
    if (!canvas || !window.Chart || !daily || daily.length === 0) return;
    new Chart(canvas.getContext("2d"), {
      type: "line",
      data: {
        labels: daily.map(function (r) { return r.date; }),
        datasets: [
          { label: "太陽光発電 (kWh)", data: daily.map(function (r) { return r.solar_kwh; }), borderColor: "#f4a92b", backgroundColor: "transparent", tension: 0.15, pointRadius: 0 },
          { label: "買電 (kWh)", data: daily.map(function (r) { return r.buy_kwh; }), borderColor: "#c9484f", backgroundColor: "transparent", tension: 0.15, pointRadius: 0 },
          { label: "売電 (kWh)", data: daily.map(function (r) { return r.sell_kwh; }), borderColor: "#4d8fd6", backgroundColor: "transparent", tension: 0.15, pointRadius: 0 }
        ]
      },
      options: {
        responsive: true,
        plugins: { legend: { position: "bottom" } },
        scales: {
          x: { ticks: { maxTicksLimit: 12 } },
          y: { beginAtZero: true, title: { display: true, text: "kWh/日" } }
        }
      }
    });
  }

  function renderBillReproductionChart(bills) {
    var canvas = document.getElementById("chart-bill-reproduction");
    if (!canvas || !window.Chart || !bills || !bills.months || bills.months.length === 0) return;
    var months = bills.months;
    new Chart(canvas.getContext("2d"), {
      type: "bar",
      data: {
        labels: months.map(function (r) { return r.billing_month; }),
        datasets: [
          { label: "推定請求額（実測・請求実績単価ベース）", data: months.map(function (r) { return r.bill_actual.total_yen; }), backgroundColor: "#4d8fd6" },
          { label: "反実仮想（太陽光なしと仮定・復元負荷ベース）", data: months.map(function (r) { return r.bill_l0_no_solar ? r.bill_l0_no_solar.total_yen : null; }), backgroundColor: "#c9484f" }
        ]
      },
      options: {
        responsive: true,
        plugins: { legend: { position: "bottom" } },
        scales: { y: { beginAtZero: true, title: { display: true, text: "円/請求期間" } } }
      }
    });
  }

  function renderSavingLayersChart(bills) {
    var canvas = document.getElementById("chart-saving-layers");
    if (!canvas || !window.Chart || !bills || !bills.months || bills.months.length === 0) return;
    var months = bills.months;
    new Chart(canvas.getContext("2d"), {
      type: "bar",
      data: {
        labels: months.map(function (r) { return r.billing_month; }),
        datasets: [
          { label: "節約額（FIT実態）", data: months.map(function (r) { return r.saving_yen_fit; }), backgroundColor: "#3fa66b" },
          { label: "節約額（卒FIT換算）", data: months.map(function (r) { return r.saving_yen_post_fit; }), backgroundColor: "#8a63d2" }
        ]
      },
      options: {
        responsive: true,
        plugins: { legend: { position: "bottom" } },
        scales: { y: { beginAtZero: true, title: { display: true, text: "円/請求期間" } } }
      }
    });
  }

  function renderBillsTable(bills) {
    var el = document.getElementById("metrics-bills-table");
    if (!el || !bills || !bills.months || bills.months.length === 0) return;
    var rows = bills.months.map(function (r) {
      return "<tr>" +
        "<td>" + r.billing_month + "</td>" +
        "<td>" + r.usage_period.start + " 〜 " + r.usage_period.end + "</td>" +
        "<td>" + kwh(r.bill_actual.buy_kwh) + "</td>" +
        "<td>" + sellSummaryText(r) + "</td>" +
        "<td>" + yen(r.bill_actual.total_yen) + "</td>" +
        l0CellHtml(r) +
        savingCellHtml(r, "saving_yen_fit") +
        savingCellHtml(r, "saving_yen_post_fit") +
        "</tr>";
    }).join("");
    var excludedNote = "";
    if (bills.excluded_months && bills.excluded_months.length > 0) {
      // 読者向けQAレビュー対応: reason_label（読者向け短文）のみ本文に出し、
      // reason_detail（daily.json・tariff.json等の内部名を含みうる）はtitleに限定する。
      excludedNote =
        "<p class=\"metrics-notes\">除外された請求月: " +
        bills.excluded_months.map(function (m) {
          return m.billing_month + "（" + reasonSpan(m.reason_label, m.reason_detail) + "）";
        }).join(" / ") +
        "</p>";
    }
    el.innerHTML =
      "<table>" +
      "<thead><tr><th>請求月</th><th>請求期間</th><th>買電量</th><th>売電量（公式メーター）</th><th>推定請求額</th><th>反実仮想（太陽光なし）</th><th>節約額(FIT実態)</th><th>節約額(卒FIT換算)</th></tr></thead>" +
      "<tbody>" + rows + "</tbody>" +
      "</table>" + excludedNote;
  }

  // --- 4層比較（L0/L1/L2/L3、data/metrics/layers.json） -----------------------------------
  // L0=太陽光・蓄電池・本システムなし（推定）／L1=太陽光のみ（推定）／L2=太陽光+蓄電池（推定）／
  // L3=全部導入（実測）。L0〜L2は5分プロファイルからのシミュレーション値であり「推定」バッジを付す。
  var LAYER_NAMES = {
    L0: "L0 太陽光・蓄電池なし（推定）",
    L1: "L1 太陽光のみ（推定）",
    L2: "L2 太陽光+蓄電池（推定）",
    L3: "L3 全部導入（実測）",
  };
  var LAYER_COLORS = { L0: "#c9484f", L1: "#f4a92b", L2: "#3fa66b", L3: "#4d8fd6" };
  var layerChartMetric = "net_cost_fit_yen"; // "net_cost_fit_yen" | "net_cost_post_fit_yen"
  var layerChartInstance = null;

  function renderLayerBillsChart(layers) {
    var canvas = document.getElementById("chart-layer-bills");
    if (!canvas || !window.Chart || !layers || !layers.months || layers.months.length === 0) return;
    var months = layers.months;
    var datasets = ["L0", "L1", "L2", "L3"].map(function (key) {
      return {
        label: LAYER_NAMES[key],
        data: months.map(function (m) {
          var layer = m.layers[key];
          return layer && layer.available ? layer[layerChartMetric] : null;
        }),
        backgroundColor: LAYER_COLORS[key],
      };
    });
    if (layerChartInstance) {
      layerChartInstance.destroy();
    }
    layerChartInstance = new Chart(canvas.getContext("2d"), {
      type: "bar",
      data: { labels: months.map(function (m) { return m.billing_month; }), datasets: datasets },
      options: {
        responsive: true,
        plugins: { legend: { position: "bottom" } },
        scales: { y: { title: { display: true, text: "円/請求期間（買電額 − 売電収入）" } } },
      },
    });
  }

  function renderLayerToggle(layers) {
    var btn = document.getElementById("layer-chart-toggle");
    if (!btn || !layers) return;
    btn.addEventListener("click", function () {
      layerChartMetric = layerChartMetric === "net_cost_fit_yen" ? "net_cost_post_fit_yen" : "net_cost_fit_yen";
      btn.textContent = layerChartMetric === "net_cost_fit_yen" ? "卒FIT換算(8円/kWh)に切替" : "FIT実態(16円/kWh)に切替";
      renderLayerBillsChart(layers);
      // オーナー承認機能（2026-09-06）: 月途中カード・日次グラフもFIT/卒FITトグルに連動させる。
      renderInProgressCard(layers);
      renderDailyLayersChart(layers);
    });
  }

  // オーナー承認機能（2026-09-06）: 確定月を待たず「今ある分」を見せるヘッドラインカード。
  function badge(text) {
    return "<span class=\"metrics-badge\">" + escapeHtml(text) + "</span>";
  }

  function inProgressAmountText(layer) {
    if (!layer || !layer.available) return "―";
    return yen(layer[layerChartMetric]);
  }

  function renderInProgressCard(layers) {
    var el = document.getElementById("metrics-in-progress-card");
    if (!el) return;
    var ip = layers && layers.in_progress;
    if (!ip) {
      el.innerHTML = "<p>今月ここまでの途中集計を表示するためのデータがまだありません。</p>";
      return;
    }
    var badges = [badge("推定"), badge("途中")];
    if (ip.tariff_provisional) {
      badges.push(badge("暫定単価（" + escapeHtml(ip.tariff_source_month) + " の単価を適用）"));
    }
    var l0 = ip.layers.L0, l1 = ip.layers.L1, l2 = ip.layers.L2, l3 = ip.layers.L3;
    var deltaText = (l0 && l0.available && l3 && l3.available)
      ? yen(l0[layerChartMetric] - l3[layerChartMetric])
      : "―";
    el.innerHTML =
      "<div class=\"metrics-in-progress-card\">" +
      "<p>" + badges.join(" ") + "</p>" +
      "<p>請求月 " + escapeHtml(ip.billing_month) + "（" + ip.usage_period.start + "〜" + ip.period_end_actual +
      "、" + ip.days_covered + "/" + ip.usage_period.days + "日分）</p>" +
      "<ul>" +
      "<li>L0（太陽光・蓄電池なし、推定）: " + inProgressAmountText(l0) + "</li>" +
      "<li>L1（太陽光のみ、推定）: " + inProgressAmountText(l1) + "</li>" +
      "<li>L2（＋蓄電池、推定）: " + inProgressAmountText(l2) + "</li>" +
      "<li>L3（全部導入、実測）: " + inProgressAmountText(l3) + "</li>" +
      "<li>節約額（L0→L3、ここまでの途中集計）: " + deltaText + "</li>" +
      "</ul>" +
      "</div>";
  }

  // オーナー承認機能（2026-09-06）: 08-28〜の日次4層系列（円/日、per_kwh_only簡易換算）。
  var dailyLayerChartInstance = null;

  function renderDailyLayersChart(layers) {
    var canvas = document.getElementById("chart-daily-layers");
    if (!canvas || !window.Chart || !layers || !layers.daily || layers.daily.length === 0) return;
    var daily = layers.daily;
    var datasets = ["L0", "L1", "L2", "L3"].map(function (key) {
      return {
        label: LAYER_NAMES[key],
        data: daily.map(function (d) {
          var layer = d.layers[key];
          return layer && layer.available ? layer[layerChartMetric] : null;
        }),
        borderColor: LAYER_COLORS[key],
        backgroundColor: "transparent",
        tension: 0.15,
        pointRadius: 0,
        spanGaps: true,
      };
    });
    if (dailyLayerChartInstance) {
      dailyLayerChartInstance.destroy();
    }
    dailyLayerChartInstance = new Chart(canvas.getContext("2d"), {
      type: "line",
      data: { labels: daily.map(function (d) { return d.date; }), datasets: datasets },
      options: {
        responsive: true,
        plugins: { legend: { position: "bottom" } },
        scales: {
          x: { ticks: { maxTicksLimit: 14 } },
          y: { title: { display: true, text: "円/日（簡易per_kwh_only換算、買電額 − 売電収入）" } },
        },
      },
    });
  }

  function renderLayerSummary(layers) {
    var el = document.getElementById("metrics-layer-summary");
    if (!el) return;
    var cumulative = layers && layers.cumulative;
    if (!cumulative || !cumulative.available) {
      // オーナー承認機能（2026-09-06）: 確定月が無い間の空状態文言を変更する。
      el.innerHTML = "<p>確定月はまだありません。以下は途中集計です。</p>";
      return;
    }
    var savingPerMonthYen = cumulative.saving_yen_fit / cumulative.months_included;
    el.innerHTML =
      "<p>" +
      "太陽光・蓄電池・ポータブル電源を導入したことで、直近" + cumulative.months_included + "請求月の合計で<strong>" +
      yen(cumulative.saving_yen_fit) + "</strong>節約できています" +
      "（太陽光・蓄電池が無かった場合の反実仮想 " + yen(cumulative.net_cost_fit_yen.L0) +
      " → 実際の請求(FIT実態) " + yen(cumulative.net_cost_fit_yen.L3) + "）。" +
      "1請求期間あたり平均 " + yen(savingPerMonthYen) + " のペースです。" +
      "</p>";
  }

  function renderLayerCumulativeTable(layers) {
    var el = document.getElementById("metrics-layer-cumulative-table");
    if (!el) return;
    var cumulative = layers && layers.cumulative;
    if (!cumulative || !cumulative.available) {
      el.innerHTML = "";
      return;
    }
    var monthsByKey = {};
    (layers.months || []).forEach(function (m) { monthsByKey[m.billing_month] = m; });
    // QA #9c: L1・L2も併記し「太陽光のみ」「＋蓄電池」の限界価値を可視化する。
    var rows = cumulative.billing_months.map(function (billing_month) {
      var m = monthsByKey[billing_month];
      var l0 = m.layers.L0, l1 = m.layers.L1, l2 = m.layers.L2, l3 = m.layers.L3;
      return "<tr>" +
        "<td>" + billing_month + "</td>" +
        "<td>" + yen(l0.net_cost_fit_yen) + "</td>" +
        "<td>" + yen(l1.net_cost_fit_yen) + "</td>" +
        "<td>" + yen(l2.net_cost_fit_yen) + "</td>" +
        "<td>" + yen(l3.net_cost_fit_yen) + "</td>" +
        "<td>" + yen(l0.net_cost_fit_yen - l3.net_cost_fit_yen) + "</td>" +
        "</tr>";
    }).join("");
    var c = cumulative.net_cost_fit_yen;
    el.innerHTML =
      "<table>" +
      "<thead><tr><th>請求月</th><th>L0（なし、推定）</th><th>L1（太陽光のみ、推定）</th>" +
      "<th>L2（＋蓄電池、推定）</th><th>L3（全部導入、実測）</th><th>節約額(L0→L3)</th></tr></thead>" +
      "<tbody>" + rows + "</tbody>" +
      "<tfoot><tr><th>累計（" + cumulative.months_included + "請求月）</th><th>" + yen(c.L0) +
      "</th><th>" + yen(c.L1) + "</th><th>" + yen(c.L2) + "</th><th>" + yen(c.L3) +
      "</th><th>" + yen(cumulative.saving_yen_fit) + "</th></tr></tfoot>" +
      "</table>";
  }

  // QA #8: coverage / uncertainty / boundary_storage(SOC) / max_export_w / buy_source を
  // 脚注表として開示する（DDR §2.7「注記で開示」）。
  function disclosureRowHtml(billingMonthLabel, coverageFraction, l2, l3, maxExportW, uncertainty, interpolatedBuckets) {
    var socText = l2 && l2.available ? l2.soc_start_pct + "% → " + l2.soc_end_pct + "%" : "―";
    var uncertaintyText = uncertainty && uncertainty.L1
      ? yen(uncertainty.L1.net_cost_fit_yen_min) + "〜" + yen(uncertainty.L1.net_cost_fit_yen_max)
        + " / L2: " + yen(uncertainty.L2.net_cost_fit_yen_min) + "〜" + yen(uncertainty.L2.net_cost_fit_yen_max)
      : "―";
    var buySourceText = l3 && l3.available
      ? (l3.buy_source === "billed" ? "請求実績" : "センサー計測") + " / " +
        (l3.sell_source === "tepco_official" ? "公式メーター" : "センサー計測")
      : "―";
    return "<tr>" +
      "<td>" + billingMonthLabel + "</td>" +
      "<td>" + (coverageFraction * 100).toFixed(1) + "%</td>" +
      "<td>" + socText + "</td>" +
      "<td>" + (maxExportW !== null && maxExportW !== undefined ? Math.round(maxExportW) + " W" : "―") + "</td>" +
      "<td>" + uncertaintyText + "</td>" +
      "<td>" + buySourceText + "</td>" +
      "<td>" + (interpolatedBuckets || 0) + "</td>" +
      "</tr>";
  }

  // QA #8 + オーナー承認機能（2026-09-06）: in_progress（月途中集計）の行と「補間バケット数」
  // 列を追加する（coverage = days_covered / 期間日数。5分バケットの欠落は1日3個まで
  // 線形補間して埋めている、DDR §0既知のノイズ対策）。
  function renderLayerDisclosureTable(layers) {
    var el = document.getElementById("metrics-layer-disclosure");
    if (!el) return;
    var monthRows = (layers && layers.months ? layers.months : []).map(function (m) {
      return disclosureRowHtml(
        m.billing_month, m.coverage, m.layers.L2, m.layers.L3, m.max_export_w, m.uncertainty, m.interpolated_buckets
      );
    });
    var ip = layers && layers.in_progress;
    if (ip) {
      monthRows.push(disclosureRowHtml(
        ip.billing_month + "（途中）", ip.days_covered / ip.usage_period.days, ip.layers.L2, ip.layers.L3, null, null,
        ip.interpolated_buckets
      ));
    }
    if (monthRows.length === 0) {
      el.innerHTML = "";
      return;
    }
    el.innerHTML =
      "<table>" +
      "<thead><tr><th>請求月</th><th>5分プロファイル coverage</th>" +
      "<th>蓄電池SOC（期間開始→終了、注記のみ・金額補正なし）</th><th>最大逆潮流推定(L1/L2)</th>" +
      "<th>不確かさ帯（バケット5/15/30分×効率1.00/0.95、net_cost_fit_yen L1 / L2）</th>" +
      "<th>L3買電・売電の出典</th><th>補間バケット数</th></tr></thead>" +
      "<tbody>" + monthRows.join("") + "</tbody>" +
      "</table>";
  }

  // QA #9b: 推定層(L0〜L2)が1つもavailableでない間は、4層比較チャート本体(トグル・canvas・
  // 累計表のみ)を出さず説明段落のみにする（空の凡例4本を描かない）。開示表
  // (#metrics-layer-disclosure) はL3のbuy_source/sell_source等を読者に届けるため、
  // 推定層の有無に関わらず常に表示する（QA再レビュー #1 対応）。
  function hasAnyEstimatedLayer(layers) {
    if (!layers || !layers.months) return false;
    return layers.months.some(function (m) {
      return (m.layers.L0 && m.layers.L0.available)
        || (m.layers.L1 && m.layers.L1.available)
        || (m.layers.L2 && m.layers.L2.available);
    });
  }

  function toggleLayerChartSection(layers) {
    var section = document.getElementById("metrics-layer-chart-section");
    if (!section) return;
    section.style.display = hasAnyEstimatedLayer(layers) ? "" : "none";
  }

  function init() {
    var daily = readJSON("metrics-daily-data");
    var monthly = readJSON("metrics-monthly-data");
    var bills = readJSON("metrics-bills-data");
    var layers = readJSON("metrics-layers-data");
    renderLayerSummary(layers);
    renderInProgressCard(layers);
    renderDailyLayersChart(layers);
    toggleLayerChartSection(layers);
    renderLayerBillsChart(layers);
    renderLayerToggle(layers);
    renderLayerCumulativeTable(layers);
    renderLayerDisclosureTable(layers);
    renderSummary(monthly, bills);
    renderMonthlyEnergyChart(monthly);
    renderMonthlySavingChart(monthly);
    renderDailyEnergyChart(daily);
    renderBillReproductionChart(bills);
    renderSavingLayersChart(bills);
    renderBillsTable(bills);
  }

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", init);
  } else {
    init();
  }
})();
