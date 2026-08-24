(function () {
  "use strict";

  var KRW_UP = "up", KRW_DOWN = "down";

  function fmtPct(v) {
    if (v === null || isNaN(v)) return "—";
    var s = (v >= 0 ? "+" : "") + v.toFixed(1) + "%";
    return s;
  }
  function pctClass(v) {
    if (v === null || isNaN(v)) return "";
    return v >= 0 ? KRW_UP : KRW_DOWN; // 상승=빨강, 하락=파랑
  }
  function fmtUsd(v) {
    if (v === null || isNaN(v)) return "—";
    return "$" + v.toFixed(2);
  }
  function fmtAmount(range) {
    function k(n) { return n >= 1000 ? (n / 1000) + "K" : String(n); }
    return "$" + k(range[0]) + "–$" + k(range[1]);
  }
  function daysBetween(a, b) {
    var d1 = new Date(a), d2 = new Date(b);
    return Math.round((d2 - d1) / 86400000);
  }
  function fmtDate(s) {
    var d = new Date(s);
    return (d.getMonth() + 1) + "월 " + d.getDate() + "일";
  }
  function fmtYmd(s) {
    var d = new Date(s);
    return d.getFullYear() + "." + (d.getMonth() + 1) + "." + d.getDate();
  }

  // 공시 시점가 기준 "따라 사서 2개월 뒤 판매" 수익률
  function honestReturn(t) {
    if (!t.priceAtDisclosure || !t.priceAfter2mFromDisclosure) return null;
    return ((t.priceAfter2mFromDisclosure - t.priceAtDisclosure) / t.priceAtDisclosure) * 100;
  }

  function timelineHTML(t) {
    var start = new Date(t.transactionDate).getTime();
    var disc = new Date(t.disclosureDate).getTime();
    var two = disc + 60 * 86400000; // 공시 + 2개월
    var end = two;
    var span = Math.max(end - start, 1);

    function pos(ts) { return ((ts - start) / span) * 100; }
    var pDisc = pos(disc);
    var delay = daysBetween(t.transactionDate, t.disclosureDate);

    return (
      '<div class="timeline">' +
        '<div class="tl-track">' +
          '<div class="tl-line"></div>' +
          '<div class="tl-blackbox" style="left:0%; width:' + pDisc + '%;"></div>' +
          point(0, t.action === "sell" ? "sell" : "buy", "거래일", fmtDate(t.transactionDate)) +
          point(pDisc, "disclosure", "공시일", fmtDate(t.disclosureDate)) +
          point(100, "now", "+2개월", "따라 매도") +
        '</div>' +
        '<div class="blackbox-note">빗금 구간 = <b>' + delay + '일 블랙박스</b> · 이 기간엔 아무도 몰랐습니다</div>' +
      '</div>'
    );
  }
  function point(left, cls, lab, sub) {
    return (
      '<div class="tl-point" style="left:' + left + '%;">' +
        '<div class="tl-dot ' + cls + '"></div>' +
        '<div class="tl-label"><b>' + lab + '</b>' + sub + '</div>' +
      '</div>'
    );
  }

  function cardHTML(t) {
    var hr = honestReturn(t);
    var vsTx = t.priceAtTransaction && t.priceAtDisclosure
      ? ((t.priceAtDisclosure - t.priceAtTransaction) / t.priceAtTransaction) * 100 : null;

    return (
      '<article class="trade-card" data-action="' + t.action + '">' +
        '<div class="tc-top">' +
          '<div class="tc-id">' +
            '<span class="tc-ticker">' + t.ticker + '</span>' +
            '<span class="tc-name">' + t.companyKo + '<span class="en">' + t.companyEn + '</span></span>' +
          '</div>' +
          '<div class="tc-tags">' +
            '<span class="tag sector">' + t.sector + '</span>' +
            '<span class="tag ' + t.action + '">' + (t.action === "buy" ? "매수" : "매도") + " " + fmtAmount(t.amountRange) + '</span>' +
          '</div>' +
        '</div>' +
        timelineHTML(t) +
        '<div class="tc-metrics">' +
          metric("거래일 가격", fmtUsd(t.priceAtTransaction), "내부자 매수가") +
          metric("공시 시점가", fmtUsd(t.priceAtDisclosure), "당신이 살 수 있던 값") +
          metric("2개월 뒤", fmtUsd(t.priceAfter2mFromDisclosure), "따라 매도 시점") +
          metricPct("따라 했다면", hr) +
        '</div>' +
        '<div class="tc-catalyst"><b>이후 사건:</b> ' + (t.catalyst || "—") +
          (t.note ? '<br><span style="color:var(--muted)">' + t.note + '</span>' : '') +
        '</div>' +
      '</article>'
    );
  }
  function metric(lab, val, sub) {
    return '<div class="metric"><div class="m-lab">' + lab + '</div><div class="m-val">' + val +
      '</div><div class="m-lab" style="margin-top:2px">' + sub + '</div></div>';
  }
  function metricPct(lab, v) {
    return '<div class="metric"><div class="m-lab">' + lab + '</div><div class="m-val ' + pctClass(v) + '">' +
      fmtPct(v) + '</div><div class="m-lab" style="margin-top:2px">2개월 보유 기준</div></div>';
  }

  function renderHeroStats(trades) {
    var buys = trades.filter(function (t) { return t.action === "buy"; }).length;
    var tickers = {};
    trades.forEach(function (t) { tickers[t.ticker] = 1; });
    var box = document.getElementById("heroStats");
    box.innerHTML =
      statBox(trades.length + "건", "추적된 공시") +
      statBox(Object.keys(tickers).length + "개", "종목") +
      statBox(buys + "건", "매수 공시");
  }
  function statBox(num, lab) {
    return '<div class="stat-box"><div class="num">' + num + '</div><div class="lab">' + lab + '</div></div>';
  }

  function renderHonest(trades) {
    var rets = trades.map(honestReturn).filter(function (v) { return v !== null; });
    var wins = rets.filter(function (v) { return v > 0; }).length;
    var winRate = rets.length ? (wins / rets.length) * 100 : null;
    var avg = rets.length ? rets.reduce(function (a, b) { return a + b; }, 0) / rets.length : null;
    var best = rets.length ? Math.max.apply(null, rets) : null;
    var worst = rets.length ? Math.min.apply(null, rets) : null;

    var box = document.getElementById("honestStats");
    box.innerHTML =
      hstat(winRate === null ? "—" : winRate.toFixed(0) + "%", "승률 (2개월 보유)") +
      hstat('<span class="' + pctClass(avg) + '">' + fmtPct(avg) + "</span>", "평균 수익률") +
      hstat('<span class="up">' + fmtPct(best) + "</span>", "최고") +
      hstat('<span class="down">' + fmtPct(worst) + "</span>", "최악");
  }
  function hstat(num, lab) {
    return '<div class="hstat"><div class="num">' + num + '</div><div class="lab">' + lab + '</div></div>';
  }

  function applyFilter(trades, filter) {
    var list = document.getElementById("tradeList");
    var shown = trades.filter(function (t) { return filter === "all" || t.action === filter; });
    list.innerHTML = shown.length
      ? shown.map(cardHTML).join("")
      : '<p class="muted">해당 조건의 공시가 없습니다.</p>';
  }

  function render(data) {
    var trades = data.trades.slice().sort(function (a, b) {
      return new Date(b.disclosureDate) - new Date(a.disclosureDate);
    });
    renderHeroStats(trades);
    renderHonest(trades);
    applyFilter(trades, "all");

    document.querySelectorAll(".chip").forEach(function (chip) {
      chip.addEventListener("click", function () {
        document.querySelectorAll(".chip").forEach(function (c) { c.classList.remove("is-active"); });
        chip.classList.add("is-active");
        applyFilter(trades, chip.getAttribute("data-filter"));
      });
    });

    var note = document.getElementById("dataSourceNote");
    if (data.meta) {
      note.textContent = "데이터: " + (data.meta.dataSource === "sample" ? "예시 데이터 — " : "") +
        (data.meta.note || "") + " (최종 업데이트: " + (data.meta.lastUpdated || "-") + ")";
    }
  }

  fetch("data.json")
    .then(function (r) { return r.json(); })
    .then(render)
    .catch(function () {
      document.getElementById("tradeList").innerHTML =
        '<p class="muted">데이터를 불러오지 못했습니다. 로컬에서 열었다면 file:// 제약 때문입니다. ' +
        '간단한 서버(예: <code>python3 -m http.server</code>)로 실행하거나 GitHub Pages에 올리면 정상 동작합니다.</p>';
    });
})();
