/* 공통 유틸 — 홈(app.js)과 상세(stock.js)가 함께 사용 */
(function (w) {
  "use strict";
  var FT = {};

  FT.fmtPct = function (v) {
    if (v === null || v === undefined || isNaN(v)) return "—";
    return (v >= 0 ? "+" : "") + Number(v).toFixed(1) + "%";
  };
  // 한국식: 상승=빨강(up), 하락=파랑(down)
  FT.pctClass = function (v) {
    if (v === null || v === undefined || isNaN(v)) return "";
    return v >= 0 ? "up" : "down";
  };
  FT.fmtUsd = function (v) {
    if (v === null || v === undefined || isNaN(v)) return "—";
    return "$" + Number(v).toFixed(2);
  };
  FT.fmtAmount = function (r) {
    if (!r) return "—";
    // 공시 원문의 금액 구간을 그대로(콤마) 표기한다. 반올림하면 구간 경계가 흐려진다.
    function c(n) { return String(n).replace(/\B(?=(\d{3})+(?!\d))/g, ","); }
    return "$" + c(r[0]) + "–$" + c(r[1]);
  };
  FT.daysBetween = function (a, b) {
    return Math.round((new Date(b) - new Date(a)) / 86400000);
  };
  FT.addDays = function (s, d) {
    var dt = new Date(s); dt.setDate(dt.getDate() + d);
    return dt.toISOString().slice(0, 10);
  };
  FT.fmtDate = function (s) {
    var d = new Date(s);
    return (d.getMonth() + 1) + "월 " + d.getDate() + "일";
  };
  FT.fmtYmd = function (s) {
    var d = new Date(s);
    return d.getFullYear() + "." + (d.getMonth() + 1) + "." + d.getDate();
  };

  // 공시 시점가 기준 "2개월 보유" 수익률
  FT.honestReturn = function (t) {
    if (!t.priceAtDisclosure || !t.priceAfter2mFromDisclosure) return null;
    return ((t.priceAfter2mFromDisclosure - t.priceAtDisclosure) / t.priceAtDisclosure) * 100;
  };
  // 공시 후 지금까지 계속 추적한 누적 수익률
  FT.trackingReturn = function (t) {
    if (typeof t.trackingReturnPct === "number") return t.trackingReturnPct;
    if (t.priceAtDisclosure && t.priceLatest) {
      return ((t.priceLatest - t.priceAtDisclosure) / t.priceAtDisclosure) * 100;
    }
    return null;
  };
  // 공시일로부터 지난 일수(추적 기간)
  FT.daysSinceDisclosure = function (t, today) {
    return FT.daysBetween(t.disclosureDate, today || new Date().toISOString().slice(0, 10));
  };
  // 최근 공시(홈 'NEW' 배지용): today 기준 N일 이내
  FT.isRecent = function (t, days, today) {
    return FT.daysSinceDisclosure(t, today) <= (days || 14);
  };

  // priceHistory([[date,close]...]) → 반응형 SVG 라인차트 문자열.
  // 첫 점(=공시 진입가)에 마커, 마지막 점에 현재가 표시. 색은 등락에 따라.
  FT.lineChartSVG = function (history, opts) {
    opts = opts || {};
    var compact = !!opts.compact;
    var W = opts.w || 640, H = opts.h || (compact ? 56 : 180), P = compact ? 6 : 28;
    if (!history || history.length < 2) {
      return compact ? "" : '<div class="chart-empty">추적 데이터가 아직 없습니다.</div>';
    }
    var ys = history.map(function (p) { return p[1]; });
    var min = Math.min.apply(null, ys), max = Math.max.apply(null, ys);
    if (min === max) { min -= 1; max += 1; }
    var n = history.length;
    function X(i) { return P + (i / (n - 1)) * (W - 2 * P); }
    function Y(v) { return H - P - ((v - min) / (max - min)) * (H - 2 * P); }

    var up = ys[n - 1] >= ys[0];
    var stroke = up ? "var(--up)" : "var(--down)";
    var pts = history.map(function (p, i) { return X(i) + "," + Y(p[1]); }).join(" ");
    var area = "M" + X(0) + "," + (H - P) + " L" +
      history.map(function (p, i) { return X(i) + "," + Y(p[1]); }).join(" L") +
      " L" + X(n - 1) + "," + (H - P) + " Z";

    var entry = history[0], last = history[n - 1];
    // 가로 그리드 4줄 — 선만 있으면 값의 높낮이를 가늠할 수 없다. (compact는 생략)
    var grid = "";
    for (var g = 0; !compact && g <= 3; g++) {
      var gy = P + (g / 3) * (H - 2 * P);
      grid += '<line x1="' + P + '" x2="' + (W - P) + '" y1="' + gy + '" y2="' + gy +
        '" stroke="var(--border)" stroke-width="1" stroke-dasharray="3 4"/>';
    }

    var gid = "ftg" + (FT._gid = (FT._gid || 0) + 1);
    var svg =
      '<svg class="linechart" viewBox="0 0 ' + W + " " + H + '" preserveAspectRatio="none" role="img" aria-label="공시 후 주가 추적">' +
        '<defs><linearGradient id="' + gid + '" x1="0" x2="0" y1="0" y2="1">' +
          '<stop offset="0%" stop-color="' + stroke + '" stop-opacity="0.25"/>' +
          '<stop offset="100%" stop-color="' + stroke + '" stop-opacity="0"/>' +
        '</linearGradient></defs>' +
        grid +
        '<path d="' + area + '" fill="url(#' + gid + ')"/>' +
        '<polyline points="' + pts + '" fill="none" stroke="' + stroke + '" stroke-width="2.5" stroke-linejoin="round"/>' +
        '<circle cx="' + X(0) + '" cy="' + Y(entry[1]) + '" r="4" fill="var(--accent)"/>' +
        '<circle cx="' + X(n - 1) + '" cy="' + Y(last[1]) + '" r="4" fill="' + stroke + '"/>' +
      "</svg>";

    var pct = ((last[1] - entry[1]) / entry[1]) * 100;
    if (compact) {
      return '<div class="chart-wrap compact"><div class="chart-body">' + svg + "</div></div>";
    }
    // 축 라벨은 SVG 밖(HTML)에 둔다 — preserveAspectRatio="none"이 SVG 내부 글자를 늘려버린다.
    var scale =
      '<div class="chart-scale">' +
        "<span>" + FT.fmtUsd(max) + "</span><span>" + FT.fmtUsd(min) + "</span>" +
      "</div>";
    var legend =
      '<div class="chart-legend">' +
        '<span><i class="dot accent"></i>공시 진입 ' + FT.fmtUsd(entry[1]) + " (" + FT.fmtYmd(entry[0]) + ")</span>" +
        '<span><i class="dot ' + FT.pctClass(pct) + '"></i>현재 ' + FT.fmtUsd(last[1]) + " (" + FT.fmtYmd(last[0]) + ")</span>" +
        '<span class="chart-ret ' + FT.pctClass(pct) + '">' + FT.fmtPct(pct) + "</span>" +
      "</div>";
    return '<div class="chart-wrap"><div class="chart-body">' + svg + scale + "</div>" + legend + "</div>";
  };

  FT.loadData = function (cb, err) {
    fetch("data.json").then(function (r) { return r.json(); }).then(cb).catch(err);
  };
  FT.getParam = function (name) {
    return new URLSearchParams(w.location.search).get(name);
  };

  w.FT = FT;
})(window);
