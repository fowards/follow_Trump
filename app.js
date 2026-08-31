/* 홈 페이지 — 매매 내역을 정렬·필터·검색 가능한 표로 렌더링 */
(function () {
  "use strict";
  var F = window.FT;
  var STATE = { trades: [], today: "", search: "", action: "all", sector: "all",
                sortKey: "transactionDate", sortDir: "desc" };

  // 표 정렬용 파생값을 미리 계산해 각 거래에 붙인다.
  function enrich(t) {
    t._delay = F.daysBetween(t.transactionDate, t.disclosureDate);
    t._tracking = F.trackingReturn(t);
    t._amt = (t.amountRange && t.amountRange[1]) || 0;
    return t;
  }

  function actionCell(t) {
    var label = t.action === "buy" ? "매수" : (t.action === "sell" ? "매도" : "교환");
    var badges = "";
    if (t.actionInferred) badges += ' <span class="badge inferred" title="OCR이 매수/매도 칸을 못 읽어 보도 근거로 매수 표기">추정</span>';
    if (t.verifiedSource) badges += ' <span class="badge verified" title="뉴스·원문으로 확인됨">확인</span>';
    return '<span class="tag ' + t.action + '">' + label + "</span>" + badges;
  }

  function rowHTML(t) {
    var approx = t.transactionDateApprox
      ? ' <span class="badge approx" title="거래일이 스캔에서 훼손돼 공시일로 대체">근사</span>' : "";
    var trk = t._tracking;
    return (
      '<tr data-ticker="' + t.ticker + '" tabindex="0">' +
        '<td class="c-tick"><span class="tk">' + t.ticker + "</span>" +
          (t.companyKo && t.companyKo !== t.ticker ? '<span class="nm">' + t.companyKo + "</span>" : "") + "</td>" +
        '<td class="c-sec">' + (t.sector || "—") + "</td>" +
        '<td class="c-act">' + actionCell(t) + "</td>" +
        '<td class="num c-amt">' + F.fmtAmount(t.amountRange) + "</td>" +
        '<td class="c-date">' + F.fmtYmd(t.transactionDate) + approx + "</td>" +
        '<td class="num c-delay">' + t._delay + "일</td>" +
        '<td class="num ' + F.pctClass(trk) + '">' + F.fmtPct(trk) + "</td>" +
        '<td class="c-go">→</td>' +
      "</tr>"
    );
  }

  function currentRows() {
    var q = STATE.search.trim().toLowerCase();
    var rows = STATE.trades.filter(function (t) {
      if (STATE.action !== "all" && t.action !== STATE.action) return false;
      if (STATE.sector !== "all" && t.sector !== STATE.sector) return false;
      if (q) {
        var hay = (t.ticker + " " + (t.companyKo || "") + " " + (t.companyEn || "")).toLowerCase();
        if (hay.indexOf(q) === -1) return false;
      }
      return true;
    });
    var k = STATE.sortKey, dir = STATE.sortDir === "asc" ? 1 : -1;
    rows.sort(function (a, b) {
      var va, vb;
      if (k === "amount") { va = a._amt; vb = b._amt; }
      else if (k === "delay") { va = a._delay; vb = b._delay; }
      else if (k === "tracking") { va = a._tracking; vb = b._tracking; }
      else if (k === "transactionDate") { va = a.transactionDate; vb = b.transactionDate; }
      else { va = (a[k] || "").toString(); vb = (b[k] || "").toString(); }
      // 수익률 정렬 시 값 없음(null)은 항상 맨 아래로.
      if (k === "tracking") {
        if (va === null && vb === null) return 0;
        if (va === null) return 1;
        if (vb === null) return -1;
      }
      if (va < vb) return -1 * dir;
      if (va > vb) return 1 * dir;
      return 0;
    });
    return rows;
  }

  function renderTable() {
    var rows = currentRows();
    document.getElementById("tradeBody").innerHTML = rows.map(rowHTML).join("");
    document.getElementById("tableEmpty").hidden = rows.length > 0;
    document.getElementById("tradeCount").textContent =
      rows.length + "건 표시" + (rows.length !== STATE.trades.length ? " (전체 " + STATE.trades.length + ")" : "");
    document.querySelectorAll("#tradeTable th.sortable").forEach(function (th) {
      th.classList.remove("sort-asc", "sort-desc");
      if (th.getAttribute("data-sort") === STATE.sortKey)
        th.classList.add(STATE.sortDir === "asc" ? "sort-asc" : "sort-desc");
    });
    // 행 클릭 → 상세
    document.querySelectorAll("#tradeBody tr").forEach(function (tr) {
      function go() { w_location(tr.getAttribute("data-ticker")); }
      tr.addEventListener("click", go);
      tr.addEventListener("keydown", function (e) { if (e.key === "Enter") go(); });
    });
  }
  function w_location(ticker) {
    window.location.href = "stock.html?ticker=" + encodeURIComponent(ticker);
  }

  function renderHeroStats(trades) {
    var tickers = {};
    trades.forEach(function (t) { tickers[t.ticker] = 1; });
    var delays = trades.map(function (t) { return t._delay; });
    var avg = delays.length ? Math.round(delays.reduce(function (a, b) { return a + b; }, 0) / delays.length) : null;
    var max = delays.length ? Math.max.apply(null, delays) : null;
    document.getElementById("heroStats").innerHTML =
      statBox(Object.keys(tickers).length + "개", "추적 종목") +
      statBox(trades.length + "건", "매매 공시") +
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
      hstat('<span class="down">' + F.fmtPct(worst) + "</span>", "최악") +
      '<p class="hstat-note muted">2개월 보유 수익률을 계산할 수 있는 ' + rets.length +
      '건 기준(공시 후 2개월이 안 지난 최근 거래는 제외).</p>';
  }
  function hstat(num, lab) {
    return '<div class="hstat"><div class="num">' + num + '</div><div class="lab">' + lab + "</div></div>";
  }

  function populateSectors(trades) {
    var secs = {};
    trades.forEach(function (t) { if (t.sector) secs[t.sector] = (secs[t.sector] || 0) + 1; });
    var sel = document.getElementById("sectorFilter");
    Object.keys(secs).sort().forEach(function (s) {
      var o = document.createElement("option");
      o.value = s; o.textContent = s + " (" + secs[s] + ")";
      sel.appendChild(o);
    });
  }

  function wireControls() {
    var sb = document.getElementById("searchBox");
    sb.addEventListener("input", function () { STATE.search = sb.value; renderTable(); });
    document.querySelectorAll("#actionFilters .chip").forEach(function (chip) {
      chip.addEventListener("click", function () {
        document.querySelectorAll("#actionFilters .chip").forEach(function (c) { c.classList.remove("is-active"); });
        chip.classList.add("is-active");
        STATE.action = chip.getAttribute("data-filter"); renderTable();
      });
    });
    document.getElementById("sectorFilter").addEventListener("change", function (e) {
      STATE.sector = e.target.value; renderTable();
    });
    document.querySelectorAll("#tradeTable th.sortable").forEach(function (th) {
      th.addEventListener("click", function () {
        var k = th.getAttribute("data-sort");
        if (STATE.sortKey === k) STATE.sortDir = STATE.sortDir === "asc" ? "desc" : "asc";
        else { STATE.sortKey = k; STATE.sortDir = (k === "ticker" || k === "sector") ? "asc" : "desc"; }
        renderTable();
      });
    });
  }

  function render(data) {
    STATE.today = (data.meta && data.meta.lastUpdated) || new Date().toISOString().slice(0, 10);
    STATE.trades = (data.trades || []).map(enrich);

    renderHeroStats(STATE.trades);
    renderHonest(STATE.trades);
    populateSectors(STATE.trades);
    wireControls();
    renderTable();

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
    document.getElementById("tradeBody").innerHTML =
      '<tr><td colspan="8" class="muted">데이터를 불러오지 못했습니다. 로컬에서 열었다면 file:// 제약 때문입니다. ' +
      '<code>python3 -m http.server</code> 로 실행하거나 GitHub Pages에 올리면 정상 동작합니다.</td></tr>';
  });
})();
