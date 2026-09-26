/*
 * 実績ダッシュボードのグラフ描画。/metrics/ ページ専用（layouts/_partials/extend_footer.html から
 * .Type == "metrics" のときだけ読み込まれる）。
 *
 * データは <script type="application/json"> 経由で埋め込まれた daily.json / layers.json
 * （scripts/blog-metrics/aggregate.sh, bill_model.py, layer_model.py の生成物）を読む。
 * 日次・月次の集計値のみを扱い、時間帯別の値やデバイス個体情報はそもそも埋め込まれていない。
 *
 * layers.json は階段表の4つの構成（L0=太陽光も蓄電池もない場合/L1=太陽光だけの場合/
 * L2=太陽光＋蓄電池の場合/L3=太陽光＋蓄電池＋SolarChargeController、実際の構成）の
 * 請求期間ベース比較に、分岐のL1S（太陽光＋SolarChargeController、家庭用蓄電池なし試算）を
 * 加えた5つの構成を持つ。L0・L1・L1S・L2は試算、L3のみ実測。L1Sは「家庭用蓄電池が無い
 * ご家庭なら」の分岐（noHomeBatteryBranchHtml/renderL1sBranchTable）でのみ表示し、
 * 階段表(LAYER_ORDER)には含めない。available:false の構成は描画しない（捏造しない）。
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

  // "2026-09-02" -> "9/2"（月・日とも先頭ゼロなし）。今月ここまでカードの案内文で使う。
  function formatMD(dateStr) {
    if (!dateStr) return "";
    var parts = dateStr.split("-");
    return parseInt(parts[1], 10) + "/" + parseInt(parts[2], 10);
  }

  // 階段表示（オーナー指摘2026-09-23: 「4つの値の関係が分からない」への対応）の色分け。
  // 1つ前の構成より電気代が下がった(diff<0)ら緑、上がった(diff>=0)ら控えめな赤。
  function diffClass(diff) {
    return diff < 0 ? "metrics-diff-decrease" : "metrics-diff-increase";
  }

  // 符号付きの金額文字列（例: "−11,497 円" / "+465 円"）。
  function diffAmountText(diff) {
    var sign = diff < 0 ? "−" : "+";
    return sign + Math.round(Math.abs(diff)).toLocaleString("ja-JP") + " 円";
  }

  // 月ごとの比較表用: 見出し列が設備名を表すため、セル自体には金額だけを入れる。
  function diffCellHtml(prevVal, curVal, tag) {
    tag = tag || "td";
    var diff = curVal - prevVal;
    return "<" + tag + " class=\"" + diffClass(diff) + "\">" + diffAmountText(diff) + "</" + tag + ">";
  }

  // --- 4つの構成（L0/L1/L2/L3、data/metrics/layers.json） -------------------------------
  // L0=太陽光も蓄電池もない場合（試算）／L1=太陽光だけの場合（試算）／
  // L2=太陽光＋蓄電池の場合（試算）／L3=太陽光＋蓄電池＋SolarChargeController（実際、実測）。
  // L0〜L2 は5分プロファイルからのシミュレーション値。
  var LAYER_NAMES = {
    L0: "太陽光も蓄電池もない場合（試算）",
    L1: "太陽光だけの場合（試算）",
    L1S: "太陽光＋SolarChargeController（蓄電池なし、試算）",
    L2: "太陽光＋蓄電池の場合（試算）",
    L3: "太陽光＋蓄電池＋SolarChargeController（実際）",
  };
  var LAYER_COLORS = { L0: "#c9484f", L1: "#f4a92b", L1S: "#8e6bbf", L2: "#3fa66b", L3: "#4d8fd6" };
  // 階段表（何もない→太陽光→蓄電池→SolarChargeController）はL0〜L3の4つのまま変えない。
  // L1S（蓄電池なしでSolarChargeControllerを導入した場合の分岐）は別枠（noHomeBatteryBranchHtml）
  // と月次分岐表（renderL1sBranchTable）で示す（DDR §6、旧DDRの却下案4「階段表に5行目」）。
  var LAYER_ORDER = ["L0", "L1", "L2", "L3"];
  // 階段表示で「1つ前の構成との差」に付ける設備名（その段で新たに足された設備）。
  var LAYER_DIFF_SUBJECT = { L1: "太陽光", L2: "蓄電池", L3: "SolarChargeController" };
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

  // 前後の構成の電気代の差を「設備名で ±金額」の形にする（階段表の3列目）。どちらかが
  // 未算出(available:false)なら算出不能として "―" を返す。
  function stepDiffCellHtml(subject, prevLayer, curLayer) {
    if (!prevLayer || !prevLayer.available || !curLayer || !curLayer.available) {
      return "<td>―</td>";
    }
    var diff = curLayer[layerChartMetric] - prevLayer[layerChartMetric];
    return "<td class=\"" + diffClass(diff) + "\">" + escapeHtml(subject) + "で " + diffAmountText(diff) + "</td>";
  }

  // 「今月ここまでの電気代」カード。確定月を待たず、今そろっているデータだけで
  // 4つの構成を比較する（オーナー承認機能・2026-09-06、2026-09-23に読者向け文言へ簡素化、
  // 同日オーナー指摘「4つの値の関係が分からない」を受けて階段表示に変更）。
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
    var layersByKey = { L0: ip.layers.L0, L1: ip.layers.L1, L2: ip.layers.L2, L3: ip.layers.L3 };
    var l0 = layersByKey.L0, l2 = layersByKey.L2, l3 = layersByKey.L3;

    var introText = "";
    if (ip.usage_period) {
      introText = "<p>同じ期間（" + formatMD(ip.usage_period.start) + "〜" + formatMD(ip.period_end_actual) +
        " の " + escapeHtml(ip.days_covered) + " 日分）の電気代（買った電気の料金 − 売った電気の収入）を、" +
        "設備構成ごとに計算して並べています。上から順に設備を足していくと、電気代がどう変わるかが分かります。</p>";
    }

    var rows = LAYER_ORDER.map(function (key, i) {
      var cur = layersByKey[key];
      var diffCell = i === 0 ? "<td>―</td>" : stepDiffCellHtml(LAYER_DIFF_SUBJECT[key], layersByKey[LAYER_ORDER[i - 1]], cur);
      return "<tr><td>" + LAYER_NAMES[key] + "</td><td>" + inProgressAmountText(cur) + "</td>" + diffCell + "</tr>";
    }).join("");

    var l0l3Available = l0 && l0.available && l3 && l3.available;
    var totalDiff = l0l3Available ? (l3[layerChartMetric] - l0[layerChartMetric]) : null;
    var totalHtml = l0l3Available
      ? "<p><strong>合計の効果（何もない場合 → 実際）: <span class=\"" + diffClass(totalDiff) + "\">" +
        diffAmountText(totalDiff) + "</span></strong></p>"
      : "";

    var l2l3Available = l2 && l2.available && l3 && l3.available;
    var l2l3Diff = l2l3Available ? (l3[layerChartMetric] - l2[layerChartMetric]) : null;
    var cloudyNote = (l2l3Available && l2l3Diff > 0)
      ? "<p class=\"metrics-notes\">曇りの日が多い月は、ポータブル電源の充放電ロスの分だけ、蓄電池だけの場合より少し高くなることがあります。</p>"
      : "";

    el.innerHTML =
      "<p>" + badges.join(" ") + "</p>" +
      introText +
      "<table><thead><tr><th>構成</th><th>電気代</th><th>1つ前の構成との差</th></tr></thead>" +
      "<tbody>" + rows + "</tbody></table>" +
      totalHtml +
      cloudyNote +
      noHomeBatteryBranchHtml(layersByKey.L1, ip.layers.L1S);
  }

  // 「家庭用蓄電池が無いご家庭なら」分岐（DDR §6）: 階段表とは別枠で、太陽光だけの場合と
  // L1S（太陽光＋SolarChargeController、蓄電池なし試算）を比較する。L1Sがunavailableの
  // ときは金額を「―」にし、reason_labelだけを表示する（reason_detailは内部情報のため出さない）。
  function noHomeBatteryBranchHtml(l1, l1s) {
    var row2Cells;
    if (l1s && l1s.available) {
      row2Cells = "<td>" + inProgressAmountText(l1s) + "</td>" + stepDiffCellHtml("SolarChargeController", l1, l1s);
    } else {
      var reasonLabel = l1s && l1s.unavailable_reason ? l1s.unavailable_reason.reason_label : "";
      row2Cells = "<td>―</td><td>" + escapeHtml(reasonLabel) + "</td>";
    }
    var diffForNote = (l1 && l1.available && l1s && l1s.available) ? (l1s[layerChartMetric] - l1[layerChartMetric]) : null;
    var cloudyNote = diffForNote !== null && diffForNote > 0
      ? "<p class=\"metrics-notes\">曇りの日が多い月は、ポータブル電源の待機電力や充電・放電のロスの分だけ、太陽光だけの場合より高くなることがあります。</p>"
      : "";
    return (
      "<h3>家庭用蓄電池が無いご家庭なら</h3>" +
      "<table><thead><tr><th>構成</th><th>電気代</th><th>太陽光だけとの差</th></tr></thead>" +
      "<tbody>" +
      "<tr><td>" + LAYER_NAMES.L1 + "</td><td>" + inProgressAmountText(l1) + "</td><td>―</td></tr>" +
      "<tr><td>" + LAYER_NAMES.L1S + "</td>" + row2Cells + "</tr>" +
      "</tbody></table>" +
      "<p class=\"metrics-notes\">ポータブル電源には、今つないでいる家電だけをつなぐ前提で試算しています。</p>" +
      cloudyNote
    );
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
    var datasets = ["L0", "L1", "L1S", "L2", "L3"].map(function (key) {
      return {
        label: LAYER_NAMES[key],
        data: daily.map(function (d) {
          var layer = d.layers[key];
          return layer && layer.available ? layer[layerChartMetric] : null;
        }),
        borderColor: LAYER_COLORS[key],
        backgroundColor: "transparent",
        borderDash: key === "L1S" ? [6, 4] : undefined,
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

  // 月ごとの比較表。カードと同じ「1つ前の構成との差」を各構成の間に列として挟む
  // （オーナー指摘2026-09-23「4つの値の関係が分からない」対応、カードと同じ階段の考え方）。
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
      var l0 = m.layers.L0.net_cost_fit_yen, l1 = m.layers.L1.net_cost_fit_yen;
      var l2 = m.layers.L2.net_cost_fit_yen, l3 = m.layers.L3.net_cost_fit_yen;
      var monthLabel = escapeHtml(billing_month) +
        (m.layers.L0.estimation === "scaled" ? " " + badge("一部の日を補って計算") : "");
      return "<tr>" +
        "<td>" + monthLabel + "</td>" +
        "<td>" + yen(l0) + "</td>" +
        diffCellHtml(l0, l1) +
        "<td>" + yen(l1) + "</td>" +
        diffCellHtml(l1, l2) +
        "<td>" + yen(l2) + "</td>" +
        diffCellHtml(l2, l3) +
        "<td>" + yen(l3) + "</td>" +
        diffCellHtml(l0, l3) +
        "</tr>";
    }).join("");
    var c = cumulative.net_cost_fit_yen;
    el.innerHTML =
      "<table>" +
      "<thead><tr><th>請求月</th>" +
      "<th>" + LAYER_NAMES.L0 + "</th><th>太陽光の効果</th>" +
      "<th>" + LAYER_NAMES.L1 + "</th><th>蓄電池の効果</th>" +
      "<th>" + LAYER_NAMES.L2 + "</th><th>SolarChargeControllerの効果</th>" +
      "<th>" + LAYER_NAMES.L3 + "</th><th>合計の効果</th></tr></thead>" +
      "<tbody>" + rows + "</tbody>" +
      "<tfoot><tr><th>累計（" + escapeHtml(cumulative.months_included) + "請求月）</th>" +
      "<th>" + yen(c.L0) + "</th>" + diffCellHtml(c.L0, c.L1, "th") +
      "<th>" + yen(c.L1) + "</th>" + diffCellHtml(c.L1, c.L2, "th") +
      "<th>" + yen(c.L2) + "</th>" + diffCellHtml(c.L2, c.L3, "th") +
      "<th>" + yen(c.L3) + "</th>" + diffCellHtml(c.L0, c.L3, "th") +
      "</tr></tfoot>" +
      "</table>";
  }

  // 月ごとの「家庭用蓄電池が無いご家庭なら」分岐表（DDR §6）。cumulative.billing_monthsの
  // 各月についてL1/L1S/差を並べる。L1Sがunavailableの月は「―」。1か月もL1Sが無ければ
  // 何も描かない（既存の月次表と別枠、常にFIT単価net_cost_fit_yen基準で固定表示する）。
  function renderL1sBranchTable(layers) {
    var el = document.getElementById("metrics-l1s-branch-table");
    if (!el) return;
    var cumulative = layers && layers.cumulative;
    if (!cumulative || !cumulative.available) {
      el.innerHTML = "";
      return;
    }
    var monthsByKey = {};
    (layers.months || []).forEach(function (m) { monthsByKey[m.billing_month] = m; });
    var anyL1sAvailable = false;
    var rows = cumulative.billing_months.map(function (billing_month) {
      var m = monthsByKey[billing_month];
      var l1 = m.layers.L1, l1s = m.layers.L1S;
      var l1sAmount = "―", diffCell = "<td>―</td>";
      if (l1s && l1s.available) {
        anyL1sAvailable = true;
        l1sAmount = yen(l1s.net_cost_fit_yen);
        diffCell = diffCellHtml(l1.net_cost_fit_yen, l1s.net_cost_fit_yen);
      }
      return "<tr><td>" + escapeHtml(billing_month) + "</td>" +
        "<td>" + yen(l1.net_cost_fit_yen) + "</td>" +
        "<td>" + l1sAmount + "</td>" + diffCell + "</tr>";
    }).join("");
    if (!anyL1sAvailable) {
      el.innerHTML = "";
      return;
    }
    var totalRow = "";
    if (cumulative.net_cost_fit_yen.L1S !== undefined) {
      totalRow = "<tfoot><tr><th>累計</th>" +
        "<th>" + yen(cumulative.net_cost_fit_yen.L1) + "</th>" +
        "<th>" + yen(cumulative.net_cost_fit_yen.L1S) + "</th>" +
        diffCellHtml(cumulative.net_cost_fit_yen.L1, cumulative.net_cost_fit_yen.L1S, "th") +
        "</tr></tfoot>";
    }
    el.innerHTML =
      "<h3>家庭用蓄電池が無いご家庭なら（月ごと）</h3>" +
      "<table><thead><tr><th>請求月</th><th>" + LAYER_NAMES.L1 + "</th><th>" + LAYER_NAMES.L1S +
      "</th><th>SolarChargeControllerの効果</th></tr></thead>" +
      "<tbody>" + rows + "</tbody>" + totalRow +
      "</table>";
  }

  // 追補(2026-09-26「速報＋改訂」方式): layers.preliminary_months[]（確定条件
  // (is_closable、請求書の実額・検針値の反映)を満たさないが、請求期間が終了済みで暫定単価
  // により試算できる月）を「速報」バッジ付きで表示する。確定月(months[])とは別の小さな表で、
  // 累計(cumulative)には含めない（追補A': 確定した月はmonths[]にだけ入る）。
  // QA指摘2026-09-26 item13: 月表記は記事と同じ「YYYY年M月分」（usage_period.startの
  // 年月）にし、金額はFIT/卒FITトグル(layerChartMetric)に追随させる。
  function renderPreliminaryMonths(layers) {
    var el = document.getElementById("metrics-preliminary-months");
    if (!el) return;
    var months = (layers && layers.preliminary_months) || [];
    // QA再指摘2026-09-26 N1(二重防御): L3がbuy_source=="billed"かつsell_source==
    // "official_meter"（=買電・売電とも確定した月）はlayer_model.py側で既に除外される
    // はずだが、ダッシュボード側でも同じ条件で除外する。
    // QA再指摘2026-09-26 item2: 買電・売電が確定していても単価(tariff_provisional)が
    // まだ暫定のままの月は「完全には確定していない」ため速報表に残す。
    var isFullyConfirmed = function (m) {
      var l3 = m.layers && m.layers.L3;
      return !!l3 && l3.buy_source === "billed" && l3.sell_source === "official_meter" &&
        m.tariff_provisional === false;
    };
    var rows = months
      .filter(function (m) { return m.layers && m.layers.L3 && m.layers.L3.available && !isFullyConfirmed(m); })
      .map(function (m) {
        var ym = yearMonthOf(m.usage_period.start);
        var label = ym.year + "年" + ym.month + "月分";
        return "<tr><td>" + escapeHtml(label) + " " + badge("速報") + "</td>" +
          "<td>" + yen(m.layers.L3[layerChartMetric]) + "</td></tr>";
      })
      .join("");
    if (!rows) {
      el.innerHTML = "";
      return;
    }
    el.innerHTML =
      "<table><thead><tr><th>対象月</th><th>実質電気代（速報）</th></tr></thead>" +
      "<tbody>" + rows + "</tbody></table>" +
      "<p class=\"metrics-notes\">電気料金は前月の単価で仮計算。請求書の反映後に確定値へ更新します。</p>";
  }

  // 速報表の金額をFIT/卒FITトグルに追随させる。renderLayerToggle自体は変更せず、
  // トグルボタンに別途クリックリスナーを追加するだけにする（担当者が同時に変更中のため
  // renderLayerToggleには触れない。リスナーの実行順はDOM上の追加順なので、
  // renderLayerToggle側のlayerChartMetric更新より後に呼ばれる）。
  function bindPreliminaryMonthsToPriceToggle(layers) {
    var fitBtn = document.getElementById("layer-price-toggle-fit");
    var postFitBtn = document.getElementById("layer-price-toggle-postfit");
    if (!fitBtn || !postFitBtn) return;
    fitBtn.addEventListener("click", function () { renderPreliminaryMonths(layers); });
    postFitBtn.addEventListener("click", function () { renderPreliminaryMonths(layers); });
  }

  // 「月ごとの電気代と節約額」。確定月（4層すべてがそろう請求月）が無い間は、いつ最初の
  // 月が表示されるかだけを1文で示す（グラフ・表・長い説明文は出さない）。速報行がある場合は
  // その案内文自体を出さない（QA指摘2026-09-26 item13）。
  function renderMonthlySection(layers) {
    var el = document.getElementById("metrics-monthly-section");
    if (!el) return;
    var cumulative = layers && layers.cumulative;
    var hasPreliminaryRows = ((layers && layers.preliminary_months) || []).some(function (m) {
      return m.layers && m.layers.L3 && m.layers.L3.available;
    });
    if (!cumulative || !cumulative.available) {
      var placeholder = "";
      if (!hasPreliminaryRows) {
        var ip = layers && layers.in_progress;
        if (ip && ip.usage_period) {
          var startYM = yearMonthOf(ip.usage_period.start);
          var endMonth = monthOf(ip.usage_period.end);
          placeholder = "<p>最初の月（" + startYM.year + "年" + startYM.month + "月分）は" + endMonth + "月上旬に表示されます。</p>";
        } else {
          placeholder = "<p>月ごとの電気代を表示するためのデータがまだありません。</p>";
        }
      }
      el.innerHTML = placeholder + "<div id=\"metrics-preliminary-months\"></div>";
      renderPreliminaryMonths(layers);
      return;
    }
    var c = cumulative.net_cost_fit_yen;
    var savingL2L3Yen = c.L2 - c.L3;
    el.innerHTML =
      "<p>直近" + cumulative.months_included + "請求月の累計で <strong>" + yen(cumulative.saving_yen_fit) +
      "</strong> 節約できています（そのうち SolarChargeController の効果: " + yen(savingL2L3Yen) + "）。</p>" +
      "<canvas id=\"chart-layer-bills\" height=\"140\"></canvas>" +
      "<div id=\"metrics-layer-cumulative-table\"></div>" +
      "<div id=\"metrics-l1s-branch-table\"></div>" +
      "<div id=\"metrics-preliminary-months\"></div>";
    renderLayerBillsChart(layers);
    renderLayerCumulativeTable(layers);
    renderL1sBranchTable(layers);
    renderPreliminaryMonths(layers);
  }

  // 「FIT期間中16円／FIT終了後8円」の2択セグメント。今月ここまでカード・日次グラフ・
  // 月ごとの棒グラフを連動して切り替える（累計表は常にFIT実態のまま、元の挙動を踏襲）。
  // 単価の数字は layers.params から読む（オーナー指摘2026-09-下旬「ボタンの意味が
  // 分からない」への対応で、単発ボタンから見出し付き2択＋説明文＋卒FIT時ラベルに変更）。
  function renderLayerToggle(layers) {
    var fitBtn = document.getElementById("layer-price-toggle-fit");
    var postFitBtn = document.getElementById("layer-price-toggle-postfit");
    var assumptionEl = document.getElementById("metrics-price-toggle-assumption");
    if (!fitBtn || !postFitBtn || !layers) return;

    var params = layers.params || {};
    var fitPrice = typeof params.sell_price_yen_per_kwh_fit === "number" ? params.sell_price_yen_per_kwh_fit : 16;
    var postFitPrice = typeof params.sell_price_yen_per_kwh_post_fit === "number" ? params.sell_price_yen_per_kwh_post_fit : 8;

    fitBtn.innerHTML = "FIT期間中 " + fitPrice + "円<span class=\"metrics-price-toggle-sub\">（現在）</span>";
    postFitBtn.innerHTML = "FIT終了後 " + postFitPrice + "円<span class=\"metrics-price-toggle-sub\">（想定）</span>";

    // ボタンの見た目(aria-pressed)と卒FIT時ラベルだけを同期する（再描画はしない）。
    function syncButtons() {
      var isPostFit = layerChartMetric === "net_cost_post_fit_yen";
      fitBtn.setAttribute("aria-pressed", String(!isPostFit));
      postFitBtn.setAttribute("aria-pressed", String(isPostFit));
      if (assumptionEl) {
        if (isPostFit) {
          assumptionEl.textContent = "FIT終了後（売電" + postFitPrice + "円）を想定した試算";
          assumptionEl.hidden = false;
        } else {
          assumptionEl.textContent = "";
          assumptionEl.hidden = true;
        }
      }
    }

    function select(metric) {
      if (layerChartMetric === metric) return;
      layerChartMetric = metric;
      syncButtons();
      renderInProgressCard(layers);
      renderDailyLayersChart(layers);
      renderLayerBillsChart(layers);
    }

    fitBtn.addEventListener("click", function () { select("net_cost_fit_yen"); });
    postFitBtn.addEventListener("click", function () { select("net_cost_post_fit_yen"); });

    syncButtons();
  }

  function init() {
    var daily = readJSON("metrics-daily-data");
    var layers = readJSON("metrics-layers-data");
    renderIntroSolarSummary(daily, layers);
    renderInProgressCard(layers);
    renderDailyLayersChart(layers);
    renderMonthlySection(layers);
    renderLayerToggle(layers);
    bindPreliminaryMonthsToPriceToggle(layers);
  }

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", init);
  } else {
    init();
  }
})();
