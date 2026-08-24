/* 개별 종목 상세 페이지 */
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
        pt(0, t.action === "sell" ? "sell" : "buy", "거래일", F.fmtDate(t.transactionDate)) +
        pt(pDisc, "disclosure", "공시일", F.fmtDate(t.disclosureDate)) +
        pt(100, "now", "+2개월", "따라 매도") +
      '</div><div class="blackbox-note">빗금 = <b>' + delay + '일 블랙박스</b></div></div>'
    );
  }
  function pt(left, cls, lab, sub) {
    return '<div class="tl-point" style="left:' + left + '%;"><div class="tl-dot ' + cls +
      '"></div><div class="tl-label"><b>' + lab + "</b>" + sub + "</div></div>";
  }

  function tradeBlock(t) {
    var hr = F.honestReturn(t);
    var tr = F.trackingReturn(t);
    var dsince = F.daysSinceDisclosure(t);
    return (
      '<div class="detail-trade">' +
        '<div class="dt-head"><span class="tag ' + t.action + '">' +
          (t.action === "buy" ? "매수" : "매도") + " " + F.fmtAmount(t.amountRange) + "</span>" +
          '<span class="dt-dates">거래 ' + F.fmtYmd(t.transactionDate) + " · 공시 " + F.fmtYmd(t.disclosureDate) + "</span></div>" +
        timelineHTML(t) +
        F.lineChartSVG(t.priceHistory, {}) +
        '<div class="tc-metrics">' +
          m("거래일 가격", F.fmtUsd(t.priceAtTransaction), "내부자 매수가") +
          m("공시 시점가", F.fmtUsd(t.priceAtDisclosure), "실제 진입가") +
          mp("2개월 규칙", hr, "공시 후 2개월") +
          mp("공시 후 지금까지", tr, "D+" + dsince + " 추적중") +
        "</div>" +
        (t.catalyst ? '<div class="tc-catalyst"><b>이후 사건:</b> ' + t.catalyst + "</div>" : "") +
        (t.note ? '<div class="dt-note">' + t.note + "</div>" : "") +
      "</div>"
    );
  }
  function m(lab, val, sub) {
    return '<div class="metric"><div class="m-lab">' + lab + '</div><div class="m-val">' + val +
      '</div><div class="m-lab" style="margin-top:2px">' + sub + "</div></div>";
  }
  function mp(lab, v, sub) {
    return '<div class="metric"><div class="m-lab">' + lab + '</div><div class="m-val ' + F.pctClass(v) + '">' +
      F.fmtPct(v) + '</div><div class="m-lab" style="margin-top:2px">' + sub + "</div></div>";
  }

  function render(data) {
    var ticker = (F.getParam("ticker") || "").toUpperCase();
    var box = document.getElementById("detail");
    var trades = data.trades.filter(function (t) { return t.ticker === ticker; });
    if (!ticker || !trades.length) {
      box.innerHTML = '<p class="muted">해당 종목을 찾을 수 없습니다. <a href="index.html">홈으로</a></p>';
      return;
    }
    trades.sort(function (a, b) { return new Date(b.disclosureDate) - new Date(a.disclosureDate); });
    var head = trades[0];
    var tr = F.trackingReturn(head);

    document.title = head.companyKo + " (" + ticker + ") — 트럼프 매매 추적 | 트럼프 팔로우";

    box.innerHTML =
      '<div class="detail-header">' +
        '<div class="dh-title"><span class="tc-ticker">' + ticker + "</span>" +
          "<h2>" + head.companyKo + '<span class="en">' + head.companyEn + "</span></h2></div>" +
        '<div class="dh-meta">' +
          '<span class="tag sector">' + head.sector + "</span>" +
          '<span class="dh-price">현재 ' + F.fmtUsd(head.priceLatest) +
            (head.priceLatestDate ? " (" + F.fmtYmd(head.priceLatestDate) + ")" : "") + "</span>" +
          '<span class="dh-ret ' + F.pctClass(tr) + '">공시 후 ' + F.fmtPct(tr) + "</span>" +
        "</div>" +
      "</div>" +
      '<p class="detail-sub">트럼프의 <b>' + ticker + "</b> 관련 공시 " + trades.length + "건. " +
        "각 거래의 공시 지연(블랙박스)과 공시 이후 추적 수익률입니다. " +
        (head.priceValues === "illustrative" ? '<span class="muted">(가격은 파이프라인 연결 전 예시 값)</span>' : "") + "</p>" +
      trades.map(tradeBlock).join("");
  }

  F.loadData(render, function () {
    document.getElementById("detail").innerHTML =
      '<p class="muted">데이터를 불러오지 못했습니다. 로컬은 <code>python3 -m http.server</code> 로 실행하세요.</p>';
  });
})();
