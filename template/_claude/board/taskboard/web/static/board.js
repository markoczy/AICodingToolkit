// Polls a fingerprint of the task files. On a change the page fetches itself again and swaps in the new
// content in place. The board keeps the search text and each column's scroll position. A task page fetches
// the task by its id (so a task moved to another state is followed there), swaps the read-only parts and keeps
// every answer or note form that holds text or has the focus as it is (text, cursor, focus), giving it the new
// version hash; a kept form whose question is gone stays with a note and can't be sent. When a page fetch fails,
// the page retries only after a pause instead of on every poll. The keyword search filters the cards in place:
// every word of the query must occur (case-insensitive) in a card's data-search text, which the server builds
// from the task's id, title and sections. The plan view has nothing to refresh. The header's toggle switches
// between the dark and the light theme and keeps the choice in localStorage (base.html applies it on load).
(function () {
  var body = document.body;
  var known = body.dataset.fingerprint;
  var interval = parseInt(body.dataset.refreshMs, 10) || 5000;
  var url = body.dataset.fingerprintUrl;
  var search = document.getElementById("search");
  var notice = document.getElementById("changed");
  var noticeKind = null;
  var SEARCH_KEY = "taskBoardSearch";
  var RETRY_MS = 60000;
  var failed = null;
  var retryAt = 0;

  function queryWords() {
    return search ? search.value.toLowerCase().split(/\s+/).filter(function (w) { return w; }) : [];
  }

  function applySearch() {
    var words = queryWords();
    document.querySelectorAll(".board .column").forEach(function (column) {
      var cards = column.querySelectorAll(".card");
      var shown = 0;
      cards.forEach(function (card) {
        var text = card.dataset.search || "";
        var match = words.every(function (w) { return text.indexOf(w) >= 0; });
        card.hidden = !match;
        if (match) { shown += 1; }
      });
      var count = column.querySelector(".count");
      if (count) { count.textContent = words.length ? shown + " of " + cards.length : String(cards.length); }
    });
  }

  function remember() {
    try { window.sessionStorage.setItem(SEARCH_KEY, search.value); } catch (e) {}
  }

  function showNotice(kind, text, linkText, href, after) {
    notice.textContent = text + " ";
    var link = document.createElement("a");
    link.href = href;
    link.textContent = linkText;
    notice.appendChild(link);
    if (after) { notice.appendChild(document.createTextNode(" " + after)); }
    notice.hidden = false;
    noticeKind = kind;
  }

  function showReloadNotice() {
    showNotice("reload", "The task files changed on disk.", "Reload", window.location.href,
               "to see the current version.");
  }

  function boardUrl() {
    var brand = document.querySelector(".tb-brand");
    return brand ? brand.href : "/";
  }

  function parse(html) {
    return new DOMParser().parseFromString(html, "text/html");
  }

  // A refresh resolves to true when the change is handled (known is then up to date) and to false when the
  // page could not be fetched.
  function refreshBoard() {
    return fetch(window.location.href, {cache: "no-store"})
      .then(function (r) { return r.ok ? r.text() : null; })
      .then(function (html) {
        if (html === null) { return false; }
        var page = parse(html);
        var fresh = page.getElementById("board-live");
        var live = document.getElementById("board-live");
        if (!fresh || !live) {
          window.location.reload();
          return true;
        }
        var scrolls = {};
        live.querySelectorAll(".column").forEach(function (c) { scrolls[c.dataset.state] = c.scrollTop; });
        var board = live.querySelector(".board");
        var boardLeft = board ? board.scrollLeft : 0;
        live.innerHTML = fresh.innerHTML;
        applySearch();
        live.querySelectorAll(".column").forEach(function (c) {
          if (scrolls[c.dataset.state]) { c.scrollTop = scrolls[c.dataset.state]; }
        });
        board = live.querySelector(".board");
        if (board) { board.scrollLeft = boardLeft; }
        known = page.body.dataset.fingerprint || known;
        return true;
      });
  }

  function formsOf(root) {
    var out = {};
    root.querySelectorAll("form[data-form]").forEach(function (f) { out[f.dataset.form] = f; });
    return out;
  }

  function setFormNote(form, text) {
    var note = form.querySelector(".form-note");
    if (!text) {
      if (note) { note.remove(); }
      return;
    }
    if (!note) {
      note = document.createElement("p");
      note.className = "form-note tb-message tb-message-error";
      form.insertBefore(note, form.firstChild);
    }
    note.textContent = text;
  }

  function setSendable(form, sendable) {
    form.querySelectorAll("button[type=submit]").forEach(function (b) { b.disabled = !sendable; });
  }

  // Forms the user is working in: text in the field, or the focus inside the form.
  function formsInUse(live) {
    var active = document.activeElement;
    return Array.prototype.filter.call(live.querySelectorAll("form[data-form]"), function (form) {
      var field = form.querySelector("textarea");
      return field && (field.value !== "" || form.contains(active));
    });
  }

  // Puts a form the user is working in into the fetched content: in place of the fetched form of the same
  // kind and question (taking over its action, label and hidden version and token fields), or, when the fetched
  // page has no such form any more, next to the other forms with a note and its button disabled.
  function keepForm(form, fresh, freshForms) {
    var match = freshForms[form.dataset.form];
    if (match) {
      match.querySelectorAll("input[type=hidden]").forEach(function (input) {
        var own = form.querySelector('input[type=hidden][name="' + input.name + '"]');
        if (own) { own.value = input.value; }
      });
      form.setAttribute("action", match.getAttribute("action"));
      var label = form.querySelector("label");
      var freshLabel = match.querySelector("label");
      if (label && freshLabel) { label.replaceWith(freshLabel); }
      setFormNote(form, null);
      setSendable(form, true);
      match.replaceWith(form);
      return;
    }
    var isAnswer = form.dataset.form.indexOf("answer:") === 0;
    setFormNote(form, isAnswer
      ? "This question was answered or removed meanwhile. Your text is kept here so you can copy it; it can't be sent."
      : "Notes can't be added to this task now. Your text is kept here so you can copy it.");
    setSendable(form, false);
    var input = fresh.querySelector("section.input") || fresh;
    var noteForm = freshForms.note;
    if (isAnswer && noteForm && noteForm.parentNode === input) {
      input.insertBefore(form, noteForm);
    } else {
      input.appendChild(form);
    }
  }

  function swapTask(page) {
    var live = document.getElementById("task-live");
    var head = document.getElementById("task-head");
    var fresh = page.getElementById("task-live");
    var freshHead = page.getElementById("task-head");
    var active = document.activeElement;
    var caret = null;
    if (active && active.tagName === "TEXTAREA" && live.contains(active)) {
      caret = {field: active, start: active.selectionStart, end: active.selectionEnd,
               direction: active.selectionDirection, scroll: active.scrollTop};
    }
    var scrollX = window.scrollX;
    var scrollY = window.scrollY;
    var freshForms = formsOf(fresh);
    formsInUse(live).forEach(function (form) { keepForm(form, fresh, freshForms); });
    Array.prototype.forEach.call(fresh.attributes, function (a) { live.setAttribute(a.name, a.value); });
    live.replaceChildren.apply(live, Array.prototype.slice.call(fresh.childNodes));
    head.replaceChildren.apply(head, Array.prototype.slice.call(freshHead.childNodes));
    document.title = page.title;
    if (caret && document.contains(caret.field)) {
      caret.field.focus({preventScroll: true});
      try { caret.field.setSelectionRange(caret.start, caret.end, caret.direction); } catch (e) {}
      caret.field.scrollTop = caret.scroll;
    }
    window.scrollTo(scrollX, scrollY);
  }

  function markGone(live) {
    live.querySelectorAll("form[data-form]").forEach(function (form) {
      setFormNote(form, "The task file is gone. Your text is kept here so you can copy it.");
      setSendable(form, false);
    });
  }

  // The task is fetched by its id, which redirects to wherever its file is now; a 404 means it is gone.
  function refreshTask(seen) {
    var live = document.getElementById("task-live");
    if (!live || !document.getElementById("task-head")) {
      showReloadNotice();
      known = seen;
      return Promise.resolve(true);
    }
    return fetch(live.dataset.locateUrl, {cache: "no-store"}).then(function (r) {
      if (r.status === 404) {
        markGone(live);
        showNotice("gone", "This task's file is no longer on the board (deleted, or renamed to another id).",
                   "Back to the board", boardUrl());
        known = seen;
        return true;
      }
      if (!r.ok && r.status < 500) {
        showReloadNotice();
        known = seen;
        return true;
      }
      if (!r.ok) { return false; }
      return r.text().then(function (html) {
        var page = parse(html);
        if (!page.getElementById("task-live") || !page.getElementById("task-head")) {
          showReloadNotice();
          known = seen;
          return true;
        }
        var oldUrl = live.dataset.taskUrl;
        swapTask(page);
        var newUrl = live.dataset.taskUrl;
        if (newUrl !== oldUrl) {
          try { window.history.replaceState(null, "", newUrl); } catch (e) {}
          showNotice("moved", "This task moved to " + live.dataset.state + "; the page now shows it there.",
                     "Back to the board", boardUrl());
        } else if (noticeKind !== "moved") {
          notice.hidden = true;
          noticeKind = null;
        }
        known = page.body.dataset.fingerprint || seen;
        return true;
      });
    });
  }

  function poll() {
    fetch(url, {cache: "no-store"})
      .then(function (r) { return r.ok ? r.json() : null; })
      .then(function (data) {
        if (!data || data.fingerprint === known) { return; }
        var seen = data.fingerprint;
        if (seen === failed && Date.now() < retryAt) { return; }
        var refresh = body.dataset.mode === "board" ? refreshBoard : refreshTask;
        return refresh(seen).catch(function () { return false; }).then(function (ok) {
          if (ok) {
            failed = null;
          } else {
            failed = seen;
            retryAt = Date.now() + RETRY_MS;
          }
        });
      })
      .catch(function () {})
      .then(function () { setTimeout(poll, interval); });
  }
  if (body.dataset.mode !== "plan") { setTimeout(poll, interval); }

  var THEME_KEY = "taskBoardTheme";
  var themeToggle = document.getElementById("theme-toggle");
  function showTheme() {
    var light = document.documentElement.dataset.theme === "light";
    themeToggle.textContent = light ? "Dark mode" : "Light mode";
  }
  if (themeToggle) {
    showTheme();
    themeToggle.hidden = false;
    themeToggle.addEventListener("click", function () {
      var light = document.documentElement.dataset.theme !== "light";
      if (light) { document.documentElement.dataset.theme = "light"; }
      else { delete document.documentElement.dataset.theme; }
      try { window.localStorage.setItem(THEME_KEY, light ? "light" : "dark"); } catch (e) {}
      showTheme();
    });
  }

  if (search) {
    try { search.value = window.sessionStorage.getItem(SEARCH_KEY) || ""; } catch (e) {}
    search.parentElement.hidden = false;
    search.addEventListener("input", function () { applySearch(); remember(); });
    search.addEventListener("keydown", function (e) {
      if (e.key === "Escape") { search.value = ""; applySearch(); remember(); }
    });
    applySearch();
  }

  var filters = document.querySelector("form.filters");
  if (filters) {
    // The "Waiting on" choice is remembered per browser (localStorage) and applied when the page is opened
    // without one in its address.
    var waiting = filters.querySelector("select[name=waiting]");
    var WAITING_KEY = "taskBoardWaiting";
    if (waiting) {
      var stored = "";
      try { stored = window.localStorage.getItem(WAITING_KEY) || ""; } catch (e) {}
      if (stored && !/[?&]waiting=/.test(window.location.search) && waiting.value !== stored) {
        waiting.value = stored;
        if (waiting.value === stored) { filters.submit(); }
      }
    }
    filters.addEventListener("change", function () {
      if (waiting) {
        try { window.localStorage.setItem(WAITING_KEY, waiting.value); } catch (e) {}
      }
      filters.submit();
    });
  }

  // The header line of the periodic update (only when the board updates): refreshed from /api/update.
  var banner = document.getElementById("update-banner");
  var UPDATE_POLL_MS = 15000;
  function pollUpdate() {
    fetch(banner.dataset.updateUrl, {cache: "no-store"})
      .then(function (r) { return r.ok ? r.json() : null; })
      .then(function (info) {
        if (!info) { return; }
        banner.textContent = info.text || "";
        banner.hidden = !info.text;
        banner.className = "update-banner " + (info.level || "");
      })
      .catch(function () {})
      .then(function () { setTimeout(pollUpdate, UPDATE_POLL_MS); });
  }
  if (banner && banner.dataset.updateUrl) {
    setTimeout(pollUpdate, UPDATE_POLL_MS);
  }
})();
