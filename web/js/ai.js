// P17/F5-simple (Stretch, xAI track): "Describe what matters" -> suggested
// chips. Populates #ai-slot (step 3, above the chips) once it appears in the
// DOM, lets the user type one sentence and press Suggest, and lights up the
// matching chips with their suggested importance level. Without
// XAI_API_KEY the backend returns 503 and this shows one friendly line --
// nothing else in the app changes. This is the only module allowed to say
// "AI" in its UI copy (team decision, CLAUDE.md §7/§11/§2).
//
// #ai-slot is inside step 3's <template>, so it's a brand-new DOM node
// every time step 3 is (re)rendered -- a MutationObserver re-attaches the
// form each time it shows up, instead of assuming init(ctx) (called once,
// before any step renders) can grab it directly.

const CHIP_IDS = ["avoid_stairs", "avoid_steep", "curb_cuts", "accessible_entrance", "well_lit"];
const UNAVAILABLE_TEXT = "Suggestions aren't available right now. Choose below.";

const CONFIG = window.WAY2GO_CONFIG || { apiBase: "http://localhost:8000" };

export function init(ctx) {
  // Remembers the most recent successful suggestion so the matching
  // importance-level radios (step 4) can be set the first time that step
  // renders after a Suggest -- step 4 doesn't exist yet while we're on
  // step 3, so this can't be done synchronously.
  let lastSuggestion = null; // { priorities, appliedToStep4 }

  function setupSlot(slot) {
    if (slot.dataset.aiReady) return;
    slot.dataset.aiReady = "1";
    slot.hidden = false;
    slot.innerHTML = "";

    const label = document.createElement("label");
    label.className = "ai-label";
    label.htmlFor = "ai-text-input";
    label.textContent = "Describe what matters to you";

    const row = document.createElement("div");
    row.className = "ai-row";

    const input = document.createElement("input");
    input.type = "text";
    input.id = "ai-text-input";
    input.className = "ai-text-input";
    input.maxLength = 300;
    input.placeholder = "e.g. Hills are hard for me and I need lit paths at night";

    const button = document.createElement("button");
    button.type = "button";
    button.className = "ai-suggest-btn";
    button.textContent = "Suggest";

    row.appendChild(input);
    row.appendChild(button);

    const note = document.createElement("p");
    note.className = "ai-note";
    note.textContent = "Your words are sent to xAI to suggest options. Nothing is saved.";

    const status = document.createElement("p");
    status.className = "ai-status";
    status.setAttribute("role", "status");
    status.hidden = true;

    slot.appendChild(label);
    slot.appendChild(row);
    slot.appendChild(note);
    slot.appendChild(status);

    const onSuggest = () => handleSuggest({ input, button, status });
    button.addEventListener("click", onSuggest);
    input.addEventListener("keydown", (e) => {
      if (e.key === "Enter") {
        e.preventDefault();
        onSuggest();
      }
    });
  }

  async function handleSuggest({ input, button, status }) {
    const text = input.value.trim();
    hideStatus(status);
    clearSuggestedBadges();
    if (!text) return;

    button.disabled = true;
    const prevLabel = button.textContent;
    button.textContent = "Suggesting…";

    try {
      const res = await fetch(`${CONFIG.apiBase}/preferences/parse`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ text }),
      });

      const data = await safeJson(res);

      if (!res.ok) {
        showStatus(status, (data && data.error) || UNAVAILABLE_TEXT, true);
        return;
      }

      const priorities = (data && data.priorities) || {};
      const applied = applySuggestions(priorities);

      if (applied.length === 0) {
        showStatus(status, UNAVAILABLE_TEXT, true);
        return;
      }

      lastSuggestion = { priorities, appliedToStep4: false };
      persistPrefs(priorities);
      showStatus(status, "Suggested chips are marked below.", false);
      ctx.announce("Suggestions added. You can change them.");
    } catch (e) {
      showStatus(status, UNAVAILABLE_TEXT, true);
    } finally {
      button.disabled = false;
      button.textContent = prevLabel;
    }
  }

  function persistPrefs(priorities) {
    try {
      const current =
        ctx.getPrefs() ||
        Object.fromEntries(CHIP_IDS.map((id) => [id, { selected: false, level: "essential" }]));
      CHIP_IDS.forEach((id) => {
        if (priorities[id]) {
          current[id] = { selected: true, level: priorities[id] };
        }
      });
      ctx.setPrefs(current);
    } catch (e) {
      // Non-fatal -- the on-screen chips below are already updated via the
      // real click/change events, independent of persistence.
    }
  }

  // Selects the matching chips on the already-rendered step 3 screen by
  // dispatching a real click on each -- that runs app.js's own listener, so
  // its internal state (not reachable from here) updates the normal way.
  function applySuggestions(priorities) {
    const applied = [];
    CHIP_IDS.forEach((id) => {
      const level = priorities[id];
      if (!level) return;
      const chip = document.querySelector(`.pill-chip[data-chip="${id}"]`);
      if (!chip) return;
      if (chip.getAttribute("aria-pressed") !== "true") {
        chip.click();
      }
      addSuggestedBadge(chip);
      applied.push(id);
    });
    return applied;
  }

  function addSuggestedBadge(chip) {
    if (chip.querySelector(".ai-suggested-badge")) return;
    const badge = document.createElement("span");
    badge.className = "ai-suggested-badge";
    badge.innerHTML = '<span aria-hidden="true">✨</span> Suggested';
    chip.appendChild(badge);
  }

  function clearSuggestedBadges() {
    document.querySelectorAll(".ai-suggested-badge").forEach((b) => b.remove());
  }

  function showStatus(status, text, isError) {
    status.textContent = text;
    status.hidden = false;
    status.classList.toggle("ai-status-error", !!isError);
  }

  function hideStatus(status) {
    status.hidden = true;
    status.textContent = "";
    status.classList.remove("ai-status-error");
  }

  async function safeJson(res) {
    try {
      return await res.json();
    } catch (e) {
      return null;
    }
  }

  // Sets the suggested importance-level radios the first time step 4
  // renders after a successful Suggest (dispatches a real change event so
  // app.js's own listener updates its internal state the normal way); does
  // nothing on later re-renders so manual edits afterward aren't clobbered.
  function tryApplyStep4Levels() {
    if (!lastSuggestion || lastSuggestion.appliedToStep4) return;
    const list = document.getElementById("importance-list");
    if (!list) return;
    let any = false;
    Object.entries(lastSuggestion.priorities).forEach(([id, level]) => {
      const radio = list.querySelector(`input[name="importance-${id}"][value="${level}"]`);
      if (radio && !radio.checked) {
        radio.checked = true;
        radio.dispatchEvent(new Event("change", { bubbles: true }));
        any = true;
      }
    });
    if (any) lastSuggestion.appliedToStep4 = true;
  }

  const observer = new MutationObserver(() => {
    const slot = document.getElementById("ai-slot");
    if (slot) setupSlot(slot);
    tryApplyStep4Levels();
  });
  observer.observe(document.body, { childList: true, subtree: true });

  // In case #ai-slot (or step 4) is already present by the time init runs.
  const slot = document.getElementById("ai-slot");
  if (slot) setupSlot(slot);
  tryApplyStep4Levels();
}
