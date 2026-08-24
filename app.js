/* 홈 페이지 렌더링 */
(function () {
  "use strict";
  var F = window.FT;

  function timelineHTML(t) {
    var start = new Date(t.transactionDate).getTime();
    var disc = new Date(t.disclosureDate).getTime();
    var end = disc + 60 * 86400000;
    var span = Math.max(end - start, 1);
    var pDisc = ((disc - start) / span) * 100;
    var delay = F.daysBetween(t.transactionDate, t.disclosureDate);
    return (
      '<div class="timeline"><div class="tl-track">' +
        '<div class="tl-line"></div>' +
        '<div class="tl-blackbox" style="left:0%; width:' + pDisc + '%;"></div>' +
        point(0, t.action === "sell" ? "sell" : "buy", "거래일", F.fmtDate(t.transactionDate)) +
        point(pDisc, "disclosure", "공시일", F.fmtDate(t.disclosureDate)) +
        point(100, "now", "+2개월", "따라 매도") +
      '</div><div class="blackbox-note">빗금 = <b>' + delay + '일 블랙박스</b> · 이 기간엔 아무도 몰랐습니다</div></div>'
    );
  }
  function point(left, cls, lab, sub) {
    // 양 끝점은 라벨 절반이 카드 밖으로 나가 잘린다 → 시작/끝은 안쪽 정렬로 고정.
    var edge = left <= 0 ? " at-start" : (left >= 100 ? " at-end" : "");
    return '<div class="tl-point' + edge + '" style="left:' + left + '%;"><div class="tl-dot ' + cls +
      '"></div><div class="tl-label"><b>' + lab + "</b>" + sub + "</div></div>";
  }

  function cardHTML(t, today) {
    var hr = F.honestReturn(t);
    var tr = F.trackingReturn(t);
    var dsince = F.daysSinceDisclosure(t, today);
    var recent = F.isRecent(t, 14, today);
    return (
      '<a class="trade-card" href="stock.html?ticker=' + encodeURIComponent(t.ticker) + '" data-action="' + t.action + '">' +
        '<div class="tc-top"><div class="tc-id">' +
          '<span class="tc-ticker">' + t.ticker + "</span>" +
          '<span class="tc-name">' + t.companyKo + '<span class="en">' + t.companyEn + "</span></span>" +
          (recent ? '<span class="new-badge">NEW</span>' : "") +
        "</div><div class=\"tc-tags\">" +
          '<span class="tag sector">' + t.sector + "</span>" +
          '<span class="tag ' + t.action + '">' + (t.action === "buy" ? "매수" : "매도") + " " + F.fmtAmount(t.amountRange) + "</span>" +
        "</div></div>" +
        timelineHTML(t) +
        '<div class="tc-metrics">' +
          metric("공시 시점가", F.fmtUsd(t.priceAtDisclosure), "당신이 살 수 있던 값") +
          metricPct("2개월 규칙", hr, "공시 후 2개월") +
          metricPct("공시 후 지금까지", tr, "D+" + dsince + " 추적중") +
        "</div>" +
        '<div class="tc-catalyst"><b>이후 사건:</b> ' + (t.catalyst || "—") +
          '<span class="see-more">상세 보기 →</span></div>' +
      "</a>"
    );
  }
  function metric(lab, val, sub) {
    return '<div class="metric"><div class="m-lab">' + lab + '</div><div class="m-val">' + val +
      '</div><div class="m-lab" style="margin-top:2px">' + sub + "</div></div>";
  }
  function metricPct(lab, v, sub) {
    return '<div class="metric"><div class="m-lab">' + lab + '</div><div class="m-val ' + F.pctClass(v) + '">' +
      F.fmtPct(v) + '</div><div class="m-lab" style="margin-top:2px">' + sub + "</div></div>";
  }

  function renderHeroStats(trades) {
    var tickers = {};
    trades.forEach(function (t) { tickers[t.ticker] = 1; });
    // 이 사이트의 핵심 주장을 숫자로 — '건수'보다 '얼마나 늦게 알려졌나'가 중요하다.
    var delays = trades.map(function (t) {
      return F.daysBetween(t.transactionDate, t.disclosureDate);
    });
    var avg = delays.length
      ? Math.round(delays.reduce(function (a, b) { return a + b; }, 0) / delays.length) : null;
    var max = delays.length ? Math.max.apply(null, delays) : null;
    document.getElementById("heroStats").innerHTML =
      statBox(Object.keys(tickers).length + "개", "추적 종목") +
      statBox(avg === null ? "—" : avg + "일", "평균 공시 지연", true) +
      statBox(max === null ? "—" : max + "일", "최장 지연");
  }
  function statBox(num, lab, highlight) {
    return '<div class="stat-box' + (highlight ? " is-key" : "") + '"><div class="num">' + num +
      '</div><div class="lab">' + lab + "</div></div>";
  }

  function renderHonest(trades) {
    var rets = trades.map(F.honestReturn).filter(function (v) { return v !== null; });
    var wins = rets.filter(function (v) { return v > 0; }).length;
    var winRate = rets.length ? (wins / rets.length) * 100 : null;
    var avg = rets.length ? rets.reduce(function (a, b) { return a + b; }, 0) / rets.length : null;
    var best = rets.length ? Math.max.apply(null, rets) : null;
    var worst = rets.length ? Math.min.apply(null, rets) : null;
    document.getElementById("honestStats").innerHTML =
      hstat(winRate === null ? "—" : winRate.toFixed(0) + "%", "승률 (2개월 보유)") +
      hstat('<span class="' + F.pctClass(avg) + '">' + F.fmtPct(avg) + "</span>", "평균 수익률") +
      hstat('<span class="up">' + F.fmtPct(best) + "</span>", "최고") +
      hstat('<span class="down">' + F.fmtPct(worst) + "</span>", "최악");
  }
  function hstat(num, lab) {
    return '<div class="hstat"><div class="num">' + num + '</div><div class="lab">' + lab + "</div></div>";
  }

  function applyFilter(trades, filter, today) {
    var shown = trades.filter(function (t) { return filter === "all" || t.action === filter; });
    document.getElementById("tradeList").innerHTML = shown.length
      ? shown.map(function (t) { return cardHTML(t, today); }).join("")
      : '<p class="muted">해당 조건의 공시가 없습니다.</p>';
  }

  function render(data) {
    var today = (data.meta && data.meta.lastUpdated) || new Date().toISOString().slice(0, 10);
    var trades = data.trades.slice().sort(function (a, b) {
      return new Date(b.disclosureDate) - new Date(a.disclosureDate);
    });
    renderHeroStats(trades);
    renderHonest(trades);
    applyFilter(trades, "all", today);

    document.querySelectorAll(".chip").forEach(function (chip) {
      chip.addEventListener("click", function () {
        document.querySelectorAll(".chip").forEach(function (c) { c.classList.remove("is-active"); });
        chip.classList.add("is-active");
        applyFilter(trades, chip.getAttribute("data-filter"), today);
      });
    });

    var note = document.getElementById("dataSourceNote");
    if (data.meta) {
      note.textContent = "데이터: " + (data.meta.dataSource === "sample" ? "예시 데이터 — " : "") +
        (data.meta.note || "") + " (최종 업데이트: " + (data.meta.lastUpdated || "-") + ")";
    }
    var cav = document.getElementById("caveats");
    if (cav && data.meta && data.meta.caveats && data.meta.caveats.length) {
      cav.innerHTML = '<div class="cav-title">읽기 전에 — 정직한 전제</div><ul>' +
        data.meta.caveats.map(function (c) { return "<li>" + c + "</li>"; }).join("") + "</ul>";
    }
  }

  F.loadData(render, function () {
    document.getElementById("tradeList").innerHTML =
      '<p class="muted">데이터를 불러오지 못했습니다. 로컬에서 열었다면 file:// 제약 때문입니다. ' +
      '<code>python3 -m http.server</code> 로 실행하거나 GitHub Pages에 올리면 정상 동작합니다.</p>';
  });
})();
