/* 뉴스 페이지 — 헤드라인·출처·날짜만 표시, 본문은 싣지 않는다.
   각 항목은 원문 링크로 나가는 <a target="_blank">일 뿐이라
   본문 재게시가 아니다(저작권·AdSense 정책 문제 없음). */
(function () {
  "use strict";
  var F = window.FT;

  function esc(s) {
    var d = document.createElement("div");
    d.textContent = s || "";
    return d.innerHTML;
  }

  function itemHTML(it) {
    // 제목만 번역(본문은 다루지 않음). 번역 실패 시 영어 원문이 메인으로 대체.
    var main = it.titleKo || it.title;
    var sub = it.titleKo ? esc(it.title) : "";
    return (
      '<a class="news-item" href="' + esc(it.link) + '" target="_blank" rel="noopener noreferrer">' +
        '<div class="news-top">' +
          '<span class="news-source">' + esc(it.source) + "</span>" +
          '<span class="news-date">' + esc(it.publishedAt || "") + "</span>" +
        "</div>" +
        '<div class="news-title">' + esc(main) + "</div>" +
        (sub ? '<div class="news-title-en">' + sub + "</div>" : "") +
        '<span class="news-go">원문 보기 →</span>' +
      "</a>"
    );
  }

  function render(data) {
    var items = data.items || [];
    var list = document.getElementById("newsList");
    var empty = document.getElementById("newsEmpty");
    if (!items.length) {
      empty.hidden = false;
      list.innerHTML = "";
      return;
    }
    empty.hidden = true;
    list.innerHTML = items.map(itemHTML).join("");

    var note = document.getElementById("newsSourceNote");
    if (note && data.meta) {
      note.textContent = "출처: " + (data.meta.source || "") +
        (data.meta.lastFetched ? " · 최종 수집: " + data.meta.lastFetched : "");
    }
  }

  fetch("news.json")
    .then(function (r) { return r.json(); })
    .then(render)
    .catch(function () {
      document.getElementById("newsEmpty").hidden = false;
      document.getElementById("newsEmpty").textContent =
        "뉴스를 불러오지 못했습니다. file:// 로 열었다면 http 서버로 실행하세요.";
    });
})();
