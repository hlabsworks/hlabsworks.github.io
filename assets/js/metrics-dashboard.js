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

  function renderSummary(monthly) {
    var el = document.getElementById("metrics-summary");
    if (!el || !monthly || monthly.length === 0) return;
    var latest = monthly[monthly.length - 1];
    el.innerHTML =
      "<ul>" +
      "<li>直近集計月（" + latest.month + "、月途中の可能性あり）</li>" +
      "<li>太陽光発電量: " + kwh(latest.solar_kwh) + "</li>" +
      "<li>買電量: " + kwh(latest.buy_kwh) + " / 売電量: " + kwh(latest.sell_kwh) + "</li>" +
      "<li>蓄電池(ニチコン)充電量: " + kwh(latest.nichicon_charge_kwh) + " / ポータブル電源(EcoFlow)充電量: " + kwh(latest.ecoflow_charge_kwh) + "</li>" +
      "<li>節約額試算: " + yen(latest.saving_yen) + "</li>" +
      "</ul>";
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

  function init() {
    var daily = readJSON("metrics-daily-data");
    var monthly = readJSON("metrics-monthly-data");
    renderSummary(monthly);
    renderMonthlyEnergyChart(monthly);
    renderMonthlySavingChart(monthly);
    renderDailyEnergyChart(daily);
  }

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", init);
  } else {
    init();
  }
})();
