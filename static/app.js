"use strict";

// --------------------------------------------------------------------------
// Frontend logic for the search typeahead.
// Covers the UI requirements: live debounced suggestions, keyboard navigation,
// submit on Enter/click, dummy-response display, trending section, and
// loading / error states.
// --------------------------------------------------------------------------

const box = document.getElementById("box");
const go = document.getElementById("go");
const dropdown = document.getElementById("dropdown");
const statusEl = document.getElementById("status");
const resultEl = document.getElementById("result");
const trendingList = document.getElementById("trending-list");

let suggestions = [];     // current suggestion strings
let activeIndex = -1;     // which dropdown item is highlighted (keyboard nav)
let debounceTimer = null;

function currentMode() {
  return document.querySelector('input[name="mode"]:checked').value;
}

// ---- DEBOUNCING ----------------------------------------------------------
// The assignment asks us to "avoid unnecessary backend calls". We wait 250ms
// after the user stops typing before calling /suggest, so one call is made per
// pause instead of one per keystroke.
box.addEventListener("input", () => {
  clearTimeout(debounceTimer);
  debounceTimer = setTimeout(fetchSuggestions, 250);
});

async function fetchSuggestions() {
  const q = box.value.trim();
  if (!q) {
    renderDropdown([]);          // empty input -> no suggestions
    return;
  }
  try {
    const res = await fetch(
      `/suggest?q=${encodeURIComponent(q)}&mode=${currentMode()}`
    );
    if (!res.ok) throw new Error(`HTTP ${res.status}`);
    const data = await res.json();
    renderDropdown(data.suggestions || []);
  } catch (err) {
    // error state
    statusEl.textContent = "Could not load suggestions: " + err.message;
    renderDropdown([]);
  }
}

function renderDropdown(items) {
  suggestions = items;
  activeIndex = -1;
  dropdown.innerHTML = "";
  if (!items.length) {
    dropdown.classList.remove("open");
    return;
  }
  items.forEach((item, i) => {
    const li = document.createElement("li");
    li.className = "item";
    li.setAttribute("role", "option");
    li.innerHTML = `<span class="rank">${i + 1}</span>
                    <span class="q">${escapeHtml(item.query)}</span>
                    <span class="c">${item.count.toLocaleString()}</span>`;
    li.addEventListener("mousedown", (e) => {
      e.preventDefault();        // keep focus in the box
      box.value = item.query;
      submitSearch();
    });
    dropdown.appendChild(li);
  });
  dropdown.classList.add("open");
}

// ---- KEYBOARD NAVIGATION -------------------------------------------------
box.addEventListener("keydown", (e) => {
  if (e.key === "ArrowDown") {
    e.preventDefault();
    move(1);
  } else if (e.key === "ArrowUp") {
    e.preventDefault();
    move(-1);
  } else if (e.key === "Enter") {
    // if a suggestion is highlighted, pick it; otherwise search the typed text
    if (activeIndex >= 0 && suggestions[activeIndex]) {
      box.value = suggestions[activeIndex].query;
    }
    submitSearch();
  } else if (e.key === "Escape") {
    renderDropdown([]);
  }
});

function move(delta) {
  const items = [...dropdown.children];
  if (!items.length) return;
  if (activeIndex >= 0) items[activeIndex].classList.remove("active");
  activeIndex = (activeIndex + delta + items.length) % items.length;
  items[activeIndex].classList.add("active");
}

go.addEventListener("click", submitSearch);

// ---- SUBMIT A SEARCH (POST /search) --------------------------------------
async function submitSearch() {
  const q = box.value.trim();
  if (!q) return;
  renderDropdown([]);
  statusEl.textContent = "Searching…";          // loading state
  resultEl.textContent = "";
  try {
    const res = await fetch("/search", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ query: q }),
    });
    if (!res.ok) throw new Error(`HTTP ${res.status}`);
    const data = await res.json();
    statusEl.textContent = "";
    // display the dummy response, as required
    resultEl.innerHTML =
      `Server response for <strong>${escapeHtml(q)}</strong>: ` +
      `<code>${escapeHtml(JSON.stringify(data))}</code>`;
    // trending may have changed; refresh it (after a short delay so the batch
    // writer has a chance to flush the new search)
    setTimeout(loadTrending, 1200);
  } catch (err) {
    statusEl.textContent = "Search failed: " + err.message;   // error state
  }
}

// ---- TRENDING SECTION (GET /trending) ------------------------------------
async function loadTrending() {
  try {
    const res = await fetch("/trending");
    if (!res.ok) throw new Error(`HTTP ${res.status}`);
    const data = await res.json();
    const items = data.trending || [];
    if (!items.length) {
      trendingList.innerHTML =
        '<li class="muted">Submit some searches to see trending queries.</li>';
      return;
    }
    trendingList.innerHTML = items
      .map(
        (it, i) =>
          `<li><span class="rank">${i + 1}</span>
               <span class="q">${escapeHtml(it.query)}</span>
               <span class="c">recent ${it.recent}</span></li>`
      )
      .join("");
  } catch (err) {
    trendingList.innerHTML =
      `<li class="muted">Could not load trending: ${escapeHtml(err.message)}</li>`;
  }
}

// re-fetch suggestions when the ranking mode changes (so the user sees the
// difference between basic and trending immediately)
document.querySelectorAll('input[name="mode"]').forEach((r) =>
  r.addEventListener("change", fetchSuggestions)
);

function escapeHtml(s) {
  return String(s).replace(/[&<>"']/g, (c) => ({
    "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;",
  }[c]));
}

// load trending once on page open
loadTrending();
