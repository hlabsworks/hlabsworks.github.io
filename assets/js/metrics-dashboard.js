/*
 * 実績ダッシュボードのグラフ描画。/metrics/ ページ専用（layouts/_partials/extend_footer.html から
 * .Type == "metrics" のときだけ読み込まれる）。
 *
 * データは <script type="application/json"> 経由で埋め込まれた daily.json / layers.json
 * （scripts/blog-metrics/aggregate.sh, bill_model.py, layer_model.py の生成物）を読む。
 * 日次・月次の集計値のみを扱い、時間帯別の値やデバイス個体情報はそもそも埋め込まれていない。
 *
 * layers.json は4つの構成（L0=太陽光も蓄電池もない場合/L1=太陽光だけの場合/
 * L2=太陽光＋蓄電池の場合/L3=太陽光＋蓄電池＋SolarChargeController、実際の構成）の
 * 請求期間ベース比較。L0〜L2は試算、L3のみ実測。available:false の構成は描画しない（捏造しない）。
 * 数値の算出ロジックの詳細は /metrics/methodology/ を参照。
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

  function escapeHtml(s) {
    if (s === null || s === undefined) return "";
    return String(s).replace(/[&<>"']/g, function (c) {
      return { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c];
    });
  }

  function badge(text) {
    return "<span class=\"metrics-badge\">" + escapeHtml(text) + "</span>";
  }

  // 請求月の文字列("2026-09-02"等)から月番号(9)だけを取り出す（月ごとの節約額表示がまだ
  // 無い間の「最初の月（2026年9月分）は10月上旬に表示されます」文言に使う）。
  function monthOf(dateStr) {
    if (!dateStr) return null;
    return parseInt(dateStr.split("-")[1], 10);
  }

  function yearMonthOf(dateStr) {
    if (!dateStr) return null;
    var parts = dateStr.split("-");
    return { year: parseInt(parts[0], 10), month: parseInt(parts[1], 10) };
  }

  // --- 4つの構成（L0/L1/L2/L3、data/metrics/layers.json） -------------------------------
  // L0=太陽光も蓄電池もない場合（試算）／L1=太陽光だけの場合（試算）／
  // L2=太陽光＋蓄電池の場合（試算）／L3=太陽光＋蓄電池＋SolarChargeController（実際、実測）。
  // L0〜L2 は5分プロファイルからのシミュレーション値。
  var LAYER_NAMES = {
    L0: "太陽光も蓄電池もない場合（試算）",
    L1: "太陽光だけの場合（試算）",
    L2: "太陽光＋蓄電池の場合（試算）",
    L3: "太陽光＋蓄電池＋SolarChargeController（実際）",
  };
  var LAYER_COLORS = { L0: "#c9484f", L1: "#f4a92b", L2: "#3fa66b", L3: "#4d8fd6" };
  var layerChartMetric = "net_cost_fit_yen"; // "net_cost_fit_yen"(売電16円) | "net_cost_post_fit_yen"(卒FIT8円)
  var layerChartInstance = null;
  var dailyLayerChartInstance = null;

  // 冒頭カード: in_progress期間（今月ここまで）のdaily.json solar_kwhを合算した1行。
  function renderIntroSolarSummary(daily, layers) {
    var el = document.getElementById("metrics-intro-solar");
    if (!el) return;
    var ip = layers && layers.in_progress;
    if (!ip || !ip.usage_period || !daily || daily.length === 0) {
      el.innerHTML = "";
      return;
    }
    var start = ip.usage_period.start;
    var end = ip.period_end_actual;
    var sum = 0;
    var any = false;
    daily.forEach(function (d) {
      if (d.date >= start && d.date <= end) {
        sum += d.solar_kwh || 0;
        any = true;
      }
    });
    if (!any) {
      el.innerHTML = "";
      return;
    }
    el.innerHTML = "<p>今月の発電量: " + kwh(sum) + "</p>";
  }

  function inProgressAmountText(layer) {
    if (!layer || !layer.available) return "―";
    return yen(layer[layerChartMetric]);
  }

  // 「今月ここまでの電気代」カード。確定月を待たず、今そろっているデータだけで
  // 4つの構成を比較する（オーナー承認機能・2026-09-06、2026-09-23に読者向け文言へ簡素化）。
  function renderInProgressCard(layers) {
    var el = document.getElementById("metrics-in-progress-card");
    if (!el) return;
    var ip = layers && layers.in_progress;
    if (!ip) {
      el.innerHTML = "<p>今月ここまでの電気代を表示するためのデータがまだありません。</p>";
      return;
    }
    var badges = [badge("試算"), badge("途中集計")];
    if (ip.tariff_provisional) badges.push(badge("電気料金は前月の単価で仮計算"));
    var l0 = ip.layers.L0, l1 = ip.layers.L1, l2 = ip.layers.L2, l3 = ip.layers.L3;
    var deltaText = (l0 && l0.available && l3 && l3.available)
      ? yen(l0[layerChartMetric] - l3[layerChartMetric])
      : "―";
    var l2l3Available = l2 && l2.available && l3 && l3.available;
    var l2l3Delta = l2l3Available ? (l2[layerChartMetric] - l3[layerChartMetric]) : null;
    var l2l3DeltaText = l2l3Available ? yen(l2l3Delta) : "―";
    var cloudyNote = (l2l3Available && l2l3Delta < 0)
      ? "<p class=\"metrics-notes\">曇りの日が多い月は、ポータブル電源の充放電ロスの分だけ、蓄電池だけの場合より少し高くなることがあります。</p>"
      : "";
    el.innerHTML =
      "<p>" + badges.join(" ") + "</p>" +
      "<ul>" +
      "<li>" + LAYER_NAMES.L0 + ": " + inProgressAmountText(l0) + "</li>" +
      "<li>" + LAYER_NAMES.L1 + ": " + inProgressAmountText(l1) + "</li>" +
      "<li>" + LAYER_NAMES.L2 + ": " + inProgressAmountText(l2) + "</li>" +
      "<li>" + LAYER_NAMES.L3 + ": " + inProgressAmountText(l3) + "</li>" +
      "<li>太陽光・蓄電池・SolarChargeControllerを全部入れた効果: " + deltaText + "</li>" +
      "<li>そのうち SolarChargeController の効果: " + l2l3DeltaText + "</li>" +
      "</ul>" +
      cloudyNote;
  }

  function renderDailyLayersEmptyState(daily) {
    var el = document.getElementById("metrics-daily-layers-empty");
    var canvas = document.getElementById("chart-daily-layers");
    if (!el) return;
    if (daily && daily.length > 0) {
      el.innerHTML = "";
      if (canvas) canvas.style.display = "";
      return;
    }
    el.innerHTML = "<p>日ごとの電気代を表示するためのデータがまだありません。</p>";
    if (canvas) canvas.style.display = "none";
  }

  function renderDailyLayersChart(layers) {
    var daily = layers && layers.daily;
    renderDailyLayersEmptyState(daily);
    var canvas = document.getElementById("chart-daily-layers");
    if (!canvas || !window.Chart || !daily || daily.length === 0) return;
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
          y: { title: { display: true, text: "円/日" } },
        },
      },
    });
  }

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
        scales: { y: { title: { display: true, text: "円" } } },
      },
    });
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
    var rows = cumulative.billing_months.map(function (billing_month) {
      var m = monthsByKey[billing_month];
      var l0 = m.layers.L0, l1 = m.layers.L1, l2 = m.layers.L2, l3 = m.layers.L3;
      var monthLabel = escapeHtml(billing_month) +
        (l0.estimation === "scaled" ? " " + badge("一部の日を補って計算") : "");
      return "<tr>" +
        "<td>" + monthLabel + "</td>" +
        "<td>" + yen(l0.net_cost_fit_yen) + "</td>" +
        "<td>" + yen(l1.net_cost_fit_yen) + "</td>" +
        "<td>" + yen(l2.net_cost_fit_yen) + "</td>" +
        "<td>" + yen(l3.net_cost_fit_yen) + "</td>" +
        "<td>" + yen(l0.net_cost_fit_yen - l3.net_cost_fit_yen) + "</td>" +
        "<td>" + yen(l2.net_cost_fit_yen - l3.net_cost_fit_yen) + "</td>" +
        "</tr>";
    }).join("");
    var c = cumulative.net_cost_fit_yen;
    el.innerHTML =
      "<table>" +
      "<thead><tr><th>請求月</th>" +
      "<th>" + LAYER_NAMES.L0 + "</th><th>" + LAYER_NAMES.L1 + "</th>" +
      "<th>" + LAYER_NAMES.L2 + "</th><th>" + LAYER_NAMES.L3 + "</th>" +
      "<th>全部入れた効果</th><th>SolarChargeControllerの効果</th></tr></thead>" +
      "<tbody>" + rows + "</tbody>" +
      "<tfoot><tr><th>累計（" + escapeHtml(cumulative.months_included) + "請求月）</th><th>" + yen(c.L0) +
      "</th><th>" + yen(c.L1) + "</th><th>" + yen(c.L2) + "</th><th>" + yen(c.L3) +
      "</th><th>" + yen(cumulative.saving_yen_fit) + "</th><th>" + yen(c.L2 - c.L3) + "</th></tr></tfoot>" +
      "</table>";
  }

  // 「月ごとの電気代と節約額」。確定月（4層すべてがそろう請求月）が無い間は、いつ最初の
  // 月が表示されるかだけを1文で示す（グラフ・表・長い説明文は出さない）。
  function renderMonthlySection(layers) {
    var el = document.getElementById("metrics-monthly-section");
    if (!el) return;
    var cumulative = layers && layers.cumulative;
    if (!cumulative || !cumulative.available) {
      var ip = layers && layers.in_progress;
      if (ip && ip.usage_period) {
        var startYM = yearMonthOf(ip.usage_period.start);
        var endMonth = monthOf(ip.usage_period.end);
        el.innerHTML = "<p>最初の月（" + startYM.year + "年" + startYM.month + "月分）は" + endMonth + "月上旬に表示されます。</p>";
      } else {
        el.innerHTML = "<p>月ごとの電気代を表示するためのデータがまだありません。</p>";
      }
      return;
    }
    var c = cumulative.net_cost_fit_yen;
    var savingL2L3Yen = c.L2 - c.L3;
    el.innerHTML =
      "<p>直近" + cumulative.months_included + "請求月の累計で <strong>" + yen(cumulative.saving_yen_fit) +
      "</strong> 節約できています（そのうち SolarChargeController の効果: " + yen(savingL2L3Yen) + "）。</p>" +
      "<canvas id=\"chart-layer-bills\" height=\"140\"></canvas>" +
      "<div id=\"metrics-layer-cumulative-table\"></div>";
    renderLayerBillsChart(layers);
    renderLayerCumulativeTable(layers);
  }

  // 「売電16円で計算／卒FIT（8円）で計算」トグル。今月ここまでカード・日次グラフ・
  // 月ごとの棒グラフを連動して切り替える（累計表は常にFIT実態のまま、元の挙動を踏襲）。
  function renderLayerToggle(layers) {
    var btn = document.getElementById("layer-chart-toggle");
    if (!btn || !layers) return;
    btn.addEventListener("click", function () {
      layerChartMetric = layerChartMetric === "net_cost_fit_yen" ? "net_cost_post_fit_yen" : "net_cost_fit_yen";
      btn.textContent = layerChartMetric === "net_cost_fit_yen" ? "卒FIT（8円）で計算" : "売電16円で計算";
      renderInProgressCard(layers);
      renderDailyLayersChart(layers);
      renderLayerBillsChart(layers);
    });
  }

  function init() {
    var daily = readJSON("metrics-daily-data");
    var layers = readJSON("metrics-layers-data");
    renderIntroSolarSummary(daily, layers);
    renderInProgressCard(layers);
    renderDailyLayersChart(layers);
    renderMonthlySection(layers);
    renderLayerToggle(layers);
  }

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", init);
  } else {
    init();
  }
})();
