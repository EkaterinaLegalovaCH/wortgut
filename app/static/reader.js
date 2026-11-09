/*
 * Wortgut reader interaction — vanilla JS, no dependencies.
 *
 * One job: tap a word → ask the local API for its lemma/translations/rank →
 * show a popup near the word → save it. Everything is a local HTTP call, so it
 * all keeps working with the network switched off.
 */

(function () {
  "use strict";

  const reader = document.querySelector(".reader");
  const popup = document.getElementById("popup");
  if (!reader || !popup) return;

  const textId = reader.dataset.textId;
  const els = {
    surface: document.getElementById("popup-surface"),
    pos: document.getElementById("popup-pos"),
    lemma: document.getElementById("popup-lemma"),
    translation: document.getElementById("popup-translation"),
    freq: document.getElementById("popup-freq"),
    input: document.getElementById("popup-input"),
    save: document.getElementById("popup-save"),
    know: document.getElementById("popup-know"),
    close: document.getElementById("popup-close"),
    status: document.getElementById("popup-status"),
  };

  let current = null;

  function setStatus(message, isError) {
    els.status.textContent = message;
    els.status.classList.toggle("error", Boolean(isError));
  }

  function placePopup(anchor) {
    const box = anchor.getBoundingClientRect();
    const top = window.scrollY + box.bottom + 8;
    const popupWidth = popup.offsetWidth || 340;
    const left = Math.max(
      8,
      Math.min(window.scrollX + box.left, window.scrollX + document.documentElement.clientWidth - popupWidth - 8)
    );
    popup.style.top = top + "px";
    popup.style.left = left + "px";
  }

  function renderTranslations(translations) {
    // One dictionary entry per line. A lemma can have several entries (the
    // source mixes senses, e.g. the animal and the medical reading of "Wolf"),
    // so lines stay separate instead of being merged into one confusing string.
    els.translation.textContent = "";
    els.translation.className = "popup-translation";

    if (translations.length === 0) {
      els.translation.textContent = "no entry found — type your own translation below";
      els.translation.className = "popup-translation miss";
      return;
    }

    const list = document.createElement("ul");
    for (const line of translations) {
      const item = document.createElement("li");
      item.textContent = line;
      list.appendChild(item);
    }
    els.translation.appendChild(list);
  }

  async function openPopup(wordEl) {
    const position = wordEl.dataset.position;
    setStatus("", false);
    els.input.value = "";
    els.translation.className = "popup-translation";
    els.translation.textContent = "…";

    try {
      const response = await fetch(
        "/api/lookup?text_id=" + encodeURIComponent(textId) + "&position=" + encodeURIComponent(position)
      );
      if (!response.ok) throw new Error("lookup failed with status " + response.status);
      const data = await response.json();

      current = { lemma: data.lemma, position: position, surface: data.surface };

      els.surface.textContent = data.surface;
      els.pos.textContent = data.pos || "";
      els.lemma.textContent = "lemma: " + data.lemma;

      renderTranslations(data.translations);
      if (data.translations.length > 0) {
        els.input.value =
          data.already_saved && data.saved_translation ? data.saved_translation : data.translations[0];
      }

      els.freq.textContent = data.frequency_rank
        ? "frequency rank " + data.frequency_rank + " (count " + data.frequency_count + ")"
        : "not in the frequency list";

      els.save.textContent = data.already_saved ? "Save again (+1 encounter)" : "Save word";
      els.know.disabled = false;
      els.know.textContent = "I know this";
      popup.hidden = false;
      placePopup(wordEl);
      current.element = wordEl;
    } catch (error) {
      setStatus("Lookup failed: " + error.message, true);
      popup.hidden = false;
      placePopup(wordEl);
    }
  }

  async function saveCurrentWord() {
    if (!current) return;
    setStatus("saving…", false);
    try {
      const response = await fetch("/api/words", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          lemma: current.lemma,
          translation: els.input.value,
          text_id: Number(textId),
          position: Number(current.position),
        }),
      });
      if (!response.ok) throw new Error("save failed with status " + response.status);
      const data = await response.json();

      if (current.element) current.element.classList.add("saved");
      setStatus("saved: " + data.lemma + " → " + data.translation, false);
      els.save.textContent = "Save again (+1 encounter)";
    } catch (error) {
      setStatus("Save failed: " + error.message, true);
    }
  }

  async function markCurrentWordKnown() {
    if (!current) return;
    setStatus("marking…", false);
    try {
      const response = await fetch("/api/know", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ lemma: current.lemma }),
      });
      if (!response.ok) throw new Error("mark failed with status " + response.status);
      const data = await response.json();

      if (current.element) current.element.classList.add("known");
      setStatus("marked as known: " + data.lemma, false);
      els.know.disabled = true;
      els.know.textContent = "Known";
    } catch (error) {
      setStatus("Mark failed: " + error.message, true);
    }
  }

  function closePopup() {
    popup.hidden = true;
    current = null;
  }

  reader.addEventListener("click", function (event) {
    const wordEl = event.target.closest(".word");
    if (wordEl) openPopup(wordEl);
  });

  els.save.addEventListener("click", saveCurrentWord);
  els.know.addEventListener("click", markCurrentWordKnown);
  els.close.addEventListener("click", closePopup);

  /*
   * A read event: the page counts as read once it has been visible for a while.
   * No click per word, no per-word bookkeeping in the browser — just "this page
   * was on screen long enough to have been read". The server resolves the words
   * from the same page window it rendered, and fires at most once per page load,
   * so coming back to a page and reading it again is another real encounter.
   */
  const DWELL_MS = 20000;
  const pageNumber = Number(reader.dataset.page || "1");
  let readTimer = null;
  let readSent = false;

  function sendReadEvent() {
    if (readSent) return;
    readSent = true;
    fetch("/api/read-events", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ text_id: Number(textId), page: pageNumber }),
    })
      .then(function (response) {
        if (!response.ok) throw new Error("read event failed with status " + response.status);
      })
      .catch(function () {
        // A failed write is retried once the page has been visible a while
        // longer; the counts are evidence, not a guarantee.
        readSent = false;
        setTimeout(armReadTimer, DWELL_MS);
      });
  }

  function armReadTimer() {
    clearTimeout(readTimer);
    if (readSent || document.visibilityState !== "visible") return;
    readTimer = setTimeout(sendReadEvent, DWELL_MS);
  }

  document.addEventListener("visibilitychange", armReadTimer);
  armReadTimer();

  document.addEventListener("keydown", function (event) {
    if (event.key === "Escape") closePopup();
  });

  document.addEventListener("click", function (event) {
    if (popup.hidden) return;
    if (popup.contains(event.target) || event.target.closest(".word")) return;
    closePopup();
  });
})();
