/*
 * 実績ダッシュボードのグラフ描画。/metrics/ ページ専用（layouts/_partials/extend_footer.html から
 * .Type == "metrics" のときだけ読み込まれる）。
 *
 * データは <script type="application/json"> 経由で埋め込まれた daily.json / monthly.json
 * （scripts/blog-metrics/aggregate.sh の生成物）を読む。日次・月次の集計値のみを扱い、
 * 時間帯別の値やデバイス個体情報はそもそも埋め込まれていない。
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
        "（太陽光が無い場合の反実仮想: " + yen(latestBill.bill_l0_no_solar.total_yen) + "）</li>" +
        "<li>売電量: " + sellSummaryText(latestBill) + "</li>" +
        "<li>節約額（公開単価ベース・FIT実態）: " + yen(latestBill.saving_yen_fit) +
        " / 節約額（卒FIT換算）: " + yen(latestBill.saving_yen_post_fit) + "</li>";
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
          { label: "推定請求額（実測・公開単価ベース）", data: months.map(function (r) { return r.bill_actual.total_yen; }), backgroundColor: "#4d8fd6" },
          { label: "反実仮想（太陽光なしと仮定）", data: months.map(function (r) { return r.bill_l0_no_solar.total_yen; }), backgroundColor: "#c9484f" }
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
        "<td>" + yen(r.bill_l0_no_solar.total_yen) + "</td>" +
        "<td>" + yen(r.saving_yen_fit) + "</td>" +
        "<td>" + yen(r.saving_yen_post_fit) + "</td>" +
        "</tr>";
    }).join("");
    var excludedNote = "";
    if (bills.excluded_months && bills.excluded_months.length > 0) {
      excludedNote =
        "<p class=\"metrics-notes\">除外された請求月: " +
        bills.excluded_months.map(function (m) { return m.billing_month + "（" + m.reason + "）"; }).join(" / ") +
        "</p>";
    }
    el.innerHTML =
      "<table>" +
      "<thead><tr><th>請求月</th><th>請求期間</th><th>買電量</th><th>売電量（公式メーター）</th><th>推定請求額</th><th>反実仮想（太陽光なし）</th><th>節約額(FIT実態)</th><th>節約額(卒FIT換算)</th></tr></thead>" +
      "<tbody>" + rows + "</tbody>" +
      "</table>" + excludedNote;
  }

  function init() {
    var daily = readJSON("metrics-daily-data");
    var monthly = readJSON("metrics-monthly-data");
    var bills = readJSON("metrics-bills-data");
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
