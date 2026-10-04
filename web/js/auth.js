// Way2Go auth module (F4 / P16). Optional email-link sign-in, synced
// preferences, and saved places via Supabase. Everything here is additive:
// if config is missing, or supabase-js fails to load, this module leaves
// #auth-slot/#auth-dialog empty and hidden and the app works exactly as it
// did before (guest flow unchanged). No passwords, no profile labels, no
// location stored except places the user explicitly saves.
//
// Privacy line shown in the dialog: "We save your route preferences and
// places, not who you are."

const SUPABASE_CDN_URL = "https://cdn.jsdelivr.net/npm/@supabase/supabase-js@2/dist/umd/supabase.js";

let supabaseClient = null;
let currentUser = null; // { id, email } | null
let ctxRef = null;
let lastRoutes = null;
let lastCompareKey = null;

// ---------------------------------------------------------------------------
// Bootstrap
// ---------------------------------------------------------------------------

export async function init(ctx) {
  ctxRef = ctx;

  const config = window.WAY2GO_CONFIG || {};
  const url = config.supabaseUrl;
  const anonKey = config.supabaseAnonKey;

  if (!url || !anonKey) {
    // No config: sign-in is hidden entirely, per this module's contract.
    return;
  }

  try {
    await loadSupabaseScript();
  } catch (e) {
    // CDN failed to load (offline demo, etc.) -- hide sign-in, never break
    // the rest of the app.
    return;
  }

  if (!window.supabase || typeof window.supabase.createClient !== "function") {
    return;
  }

  supabaseClient = window.supabase.createClient(url, anonKey, {
    auth: { detectSessionInUrl: true, persistSession: true, autoRefreshToken: true },
  });

  buildAuthSlot();
  buildAuthDialog();

  supabaseClient.auth.onAuthStateChange((_event, session) => {
    onSessionChange(session ? session.user : null);
  });

  const { data } = await supabaseClient.auth.getSession();
  await onSessionChange(data && data.session ? data.session.user : null);

  // Hook into every route shown: this is the real-world point at which the
  // app's preferences were just saved locally (app.js's step-4 "Find my
  // route" button calls its own savePreferences() right before calling
  // findRoute(), which is what leads here) -- ctx.setPrefs() itself is
  // never actually called by app.js's internal flow, only exposed on the
  // ctx contract, so this callback is the reliable sync point. See report.
  ctx.onRouteShown((routes, compareKey) => {
    lastRoutes = routes;
    lastCompareKey = compareKey;
    if (currentUser) syncPrefsToServer();
    renderSaveButton();
  });
}

function loadSupabaseScript() {
  return new Promise((resolve, reject) => {
    if (window.supabase) {
      resolve();
      return;
    }
    const script = document.createElement("script");
    script.src = SUPABASE_CDN_URL;
    script.async = true;
    script.onload = () => resolve();
    script.onerror = () => reject(new Error("supabase-js failed to load"));
    document.head.appendChild(script);
  });
}

// ---------------------------------------------------------------------------
// Session changes
// ---------------------------------------------------------------------------

async function onSessionChange(user) {
  const wasSignedIn = Boolean(currentUser);
  currentUser = user || null;
  renderAuthSlot();
  renderSaveButton();

  if (currentUser && !wasSignedIn) {
    await syncPrefsOnSignIn();
  }
}

async function syncPrefsOnSignIn() {
  if (!supabaseClient || !currentUser) return;
  try {
    const { data, error } = await supabaseClient
      .from("profiles")
      .select("preferences")
      .eq("user_id", currentUser.id)
      .maybeSingle();

    if (error) throw error;

    const serverPriorities = data && data.preferences && data.preferences.priorities;
    if (serverPriorities && Object.keys(serverPriorities).length > 0) {
      // Server wins on first load.
      ctxRef.setPrefs(serverPriorities);
    } else {
      // Nothing saved yet: upload the local (guest) preferences, if any.
      const local = ctxRef.getPrefs();
      if (local) await upsertPrefs(local);
    }
  } catch (e) {
    // Non-fatal: sign-in still succeeds, preferences just don't sync this time.
  }
}

async function syncPrefsToServer() {
  const local = ctxRef.getPrefs();
  if (!local) return;
  await upsertPrefs(local);
}

async function upsertPrefs(priorities) {
  if (!supabaseClient || !currentUser) return;
  try {
    await supabaseClient.from("profiles").upsert({
      user_id: currentUser.id,
      // Neutral keys only (CLAUDE.md §5): chip ids (avoid_stairs, avoid_steep,
      // curb_cuts, accessible_entrance, well_lit) and importance levels. No
      // profile names, disability labels, or diagnoses are ever stored here.
      preferences: { priorities },
      updated_at: new Date().toISOString(),
    });
  } catch (e) {
    // Non-fatal.
  }
}

// ---------------------------------------------------------------------------
// #auth-slot: "Sign in" pill / initial
// ---------------------------------------------------------------------------

function buildAuthSlot() {
  const slot = document.getElementById("auth-slot");
  if (!slot) return;
  slot.hidden = false;
  slot.innerHTML = `<button type="button" id="auth-slot-btn" class="auth-pill"></button>`;
  document.getElementById("auth-slot-btn").addEventListener("click", openDialog);
}

function renderAuthSlot() {
  const btn = document.getElementById("auth-slot-btn");
  if (!btn) return;
  if (currentUser) {
    const initial = (currentUser.email || "?").trim().charAt(0).toUpperCase();
    btn.textContent = initial || "•";
    btn.setAttribute("aria-label", `Signed in as ${currentUser.email}. Open account menu.`);
    btn.title = `Signed in as ${currentUser.email}`;
  } else {
    btn.textContent = "Sign in";
    btn.setAttribute("aria-label", "Sign in");
    btn.removeAttribute("title");
  }
}

// ---------------------------------------------------------------------------
// #auth-dialog
// ---------------------------------------------------------------------------

function buildAuthDialog() {
  const dialog = document.getElementById("auth-dialog");
  if (!dialog) return;
  dialog.setAttribute("role", "dialog");
  dialog.setAttribute("aria-modal", "true");
  dialog.setAttribute("aria-labelledby", "auth-dialog-title");
  dialog.classList.add("auth-dialog");

  dialog.addEventListener("keydown", (ev) => {
    if (ev.key === "Escape") {
      closeDialog();
      return;
    }
    if (ev.key === "Tab") trapFocus(ev, dialog);
  });

  renderDialogContent();
}

function renderDialogContent() {
  const dialog = document.getElementById("auth-dialog");
  if (!dialog) return;

  if (!currentUser) {
    dialog.innerHTML = `
      <div class="auth-dialog-panel">
        <button type="button" class="auth-dialog-close" id="auth-dialog-close" aria-label="Close">&times;</button>
        <h2 id="auth-dialog-title">Sign in</h2>
        <p class="auth-privacy-line">We save your route preferences and places, not who you are.</p>
        <div class="field">
          <label for="auth-email-input">Email</label>
          <input id="auth-email-input" type="email" class="search-input" autocomplete="email" placeholder="you@example.com" />
        </div>
        <button type="button" class="primary-button" id="auth-send-link-btn">Email me a sign-in link</button>
        <p class="field-hint" id="auth-status" role="status"></p>
      </div>`;

    document.getElementById("auth-dialog-close").addEventListener("click", closeDialog);
    document.getElementById("auth-send-link-btn").addEventListener("click", sendMagicLink);
    document.getElementById("auth-email-input").addEventListener("keydown", (ev) => {
      if (ev.key === "Enter") sendMagicLink();
    });
  } else {
    dialog.innerHTML = `
      <div class="auth-dialog-panel">
        <button type="button" class="auth-dialog-close" id="auth-dialog-close" aria-label="Close">&times;</button>
        <h2 id="auth-dialog-title">Signed in as ${escapeHtml(currentUser.email || "")}</h2>
        <ul class="auth-menu">
          <li><button type="button" class="choice-button" id="auth-saved-places-btn">Saved places</button></li>
          <li><button type="button" class="choice-button" id="auth-sign-out-btn">Sign out</button></li>
          <li><button type="button" class="choice-button auth-danger" id="auth-delete-data-btn">Delete my saved data</button></li>
        </ul>
        <div id="auth-saved-places-list" hidden></div>
        <p class="field-hint" id="auth-status" role="status"></p>
      </div>`;

    document.getElementById("auth-dialog-close").addEventListener("click", closeDialog);
    document.getElementById("auth-saved-places-btn").addEventListener("click", showSavedPlaces);
    document.getElementById("auth-sign-out-btn").addEventListener("click", signOut);
    document.getElementById("auth-delete-data-btn").addEventListener("click", deleteMyData);
  }
}

function openDialog() {
  renderDialogContent();
  const dialog = document.getElementById("auth-dialog");
  dialog.hidden = false;
  const firstField = dialog.querySelector("input, button");
  if (firstField) firstField.focus();
  document.addEventListener("keydown", dialogOutsideEscHandler);
}

function dialogOutsideEscHandler(ev) {
  if (ev.key === "Escape") closeDialog();
}

function closeDialog() {
  const dialog = document.getElementById("auth-dialog");
  if (dialog) dialog.hidden = true;
  document.removeEventListener("keydown", dialogOutsideEscHandler);
  const slotBtn = document.getElementById("auth-slot-btn");
  if (slotBtn) slotBtn.focus();
}

function trapFocus(ev, container) {
  const focusables = container.querySelectorAll("button, input, [href], select, textarea");
  if (focusables.length === 0) return;
  const first = focusables[0];
  const last = focusables[focusables.length - 1];
  if (ev.shiftKey && document.activeElement === first) {
    ev.preventDefault();
    last.focus();
  } else if (!ev.shiftKey && document.activeElement === last) {
    ev.preventDefault();
    first.focus();
  }
}

async function sendMagicLink() {
  const input = document.getElementById("auth-email-input");
  const status = document.getElementById("auth-status");
  const email = input.value.trim();
  if (!email) {
    status.textContent = "Enter an email address first.";
    return;
  }
  status.textContent = "Sending…";
  try {
    const { error } = await supabaseClient.auth.signInWithOtp({
      email,
      options: { emailRedirectTo: location.origin },
    });
    if (error) throw error;
    status.textContent = "Check your email for a link to sign in.";
  } catch (e) {
    status.textContent = "Couldn't send the link. Check the address and try again.";
  }
}

async function signOut() {
  if (!supabaseClient) return;
  await supabaseClient.auth.signOut();
  closeDialog();
}

async function deleteMyData() {
  const status = document.getElementById("auth-status");
  const confirmed = window.confirm(
    "This permanently deletes your saved preferences and saved places. This can't be undone. Continue?"
  );
  if (!confirmed) return;
  try {
    const { error } = await supabaseClient.rpc("delete_my_data");
    if (error) throw error;
    if (status) status.textContent = "Your saved data has been deleted.";
  } catch (e) {
    if (status) status.textContent = "Couldn't delete your data. Try again.";
    return;
  }
  await supabaseClient.auth.signOut();
  closeDialog();
}

// ---------------------------------------------------------------------------
// Saved places
// ---------------------------------------------------------------------------

async function showSavedPlaces() {
  const list = document.getElementById("auth-saved-places-list");
  const status = document.getElementById("auth-status");
  if (!list) return;
  list.hidden = false;
  list.innerHTML = "<p class=\"field-hint\">Loading…</p>";

  try {
    const { data, error } = await supabaseClient
      .from("saved_places")
      .select("id,name")
      .order("created_at", { ascending: false });
    if (error) throw error;

    if (!data || data.length === 0) {
      list.innerHTML = "<p class=\"field-hint\">No saved places yet. Save a destination from your results.</p>";
      return;
    }

    const ul = document.createElement("ul");
    ul.className = "auth-saved-places";
    data.forEach((place) => {
      const li = document.createElement("li");
      const btn = document.createElement("button");
      btn.type = "button";
      btn.className = "choice-button";
      btn.textContent = place.name;
      btn.addEventListener("click", () => fillDestination(place.name));
      li.appendChild(btn);
      ul.appendChild(li);
    });
    list.innerHTML = "";
    list.appendChild(ul);
  } catch (e) {
    if (status) status.textContent = "Couldn't load saved places.";
  }
}

function fillDestination(name) {
  // app.js's step 1 owns #destination-input; this only works while step 1
  // is on screen (known limitation -- auth.js has no access to app.js's
  // internal step state, see report).
  const input = document.getElementById("destination-input");
  if (input) {
    input.value = name;
    input.dispatchEvent(new Event("input", { bubbles: true }));
    closeDialog();
    input.focus();
  } else {
    closeDialog();
    showToast(`Go to "Where to?" and enter: ${name}`);
  }
}

function renderSaveButton() {
  const actions = document.querySelector(".results-actions");
  if (!actions) return;

  const existing = document.getElementById("auth-save-place-btn");
  if (!currentUser || !lastRoutes) {
    if (existing) existing.remove();
    return;
  }
  if (existing) return;

  const btn = document.createElement("button");
  btn.type = "button";
  btn.className = "choice-button auth-save-place-btn";
  btn.id = "auth-save-place-btn";
  btn.innerHTML = "&#9734; Save place";
  btn.addEventListener("click", saveCurrentDestination);
  actions.appendChild(btn);
}

async function saveCurrentDestination() {
  if (!currentUser || !lastRoutes || !lastCompareKey) return;
  const btn = document.getElementById("auth-save-place-btn");
  const response = lastRoutes[lastCompareKey];
  const route = response && response.routes && response.routes[0];
  const coords = route && route.geometry && route.geometry.coordinates;
  const name = (ctxRef.request && ctxRef.request.destination) || "";

  if (!coords || coords.length === 0 || !name) return;

  const [lon, lat] = coords[coords.length - 1];
  // [VERIFY] WKT "POINT(lon lat)" (no SRID prefix) is the format Supabase's
  // PostGIS geography column accepts over the JS client -- confirmed against
  // this project's own integration test (tests/test_supabase_integration.py
  // uses the same bare "POINT(lon lat)" string via supabase-py). Falling back
  // to the same string if an SRID-prefixed variant were ever required is not
  // needed here since this format is already confirmed working.
  const location = `POINT(${lon} ${lat})`;

  if (btn) {
    btn.disabled = true;
    btn.textContent = "Saving…";
  }
  try {
    const { error } = await supabaseClient.from("saved_places").insert({
      user_id: currentUser.id,
      name,
      location,
    });
    if (error) throw error;
    if (btn) btn.innerHTML = "&#9733; Saved";
    showToast(`Saved "${name}" to your places.`);
  } catch (e) {
    if (btn) {
      btn.disabled = false;
      btn.innerHTML = "&#9734; Save place";
    }
    showToast("Couldn't save that place. Try again.");
  }
}

// ---------------------------------------------------------------------------
// Toast
// ---------------------------------------------------------------------------

function showToast(text) {
  let toast = document.getElementById("auth-toast");
  if (!toast) {
    toast = document.createElement("div");
    toast.id = "auth-toast";
    toast.className = "auth-toast";
    toast.setAttribute("role", "status");
    document.body.appendChild(toast);
  }
  toast.textContent = text;
  toast.classList.add("is-visible");
  if (ctxRef && typeof ctxRef.announce === "function") ctxRef.announce(text);
  clearTimeout(toast._hideTimer);
  toast._hideTimer = setTimeout(() => toast.classList.remove("is-visible"), 4000);
}

function escapeHtml(str) {
  return String(str).replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
}
