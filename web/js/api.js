// Way2Go API client (P15). Talks to the live `priorities` support F1 added
// to POST /route (docs/API.md) -- unlike P14a, this no longer needs to
// translate chips into `preferences` overrides itself; the backend does
// that now.

const CONFIG = window.WAY2GO_CONFIG || { apiBase: "http://localhost:8000" };

export class LocationError extends Error {}

export async function fetchBuildings() {
  try {
    const res = await fetch(`${CONFIG.apiBase}/buildings`);
    if (!res.ok) return [];
    const body = await res.json();
    return body.buildings || [];
  } catch (e) {
    return [];
  }
}

export async function postRoute(body) {
  const res = await fetch(`${CONFIG.apiBase}/route`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
  });
  if (res.status === 422) {
    const detail = await res.json().catch(() => ({}));
    throw new LocationError(typeof detail.detail === "string" ? detail.detail : "location not found");
  }
  if (!res.ok) {
    throw new Error(`route request failed (${res.status})`);
  }
  return res.json();
}

// Fixed Step-free comparison body (per the F1/P14a spec for this card):
// all four hard-limit chips at Essential, regardless of the user's own
// chip selection.
export const STEPFREE_PRIORITIES = {
  avoid_stairs: "essential",
  avoid_steep: "essential",
  curb_cuts: "essential",
  accessible_entrance: "essential",
};

export function buildRequestBody(kind, origin, destination, priorities) {
  const base = {
    origin,
    destination,
    conditions: { darkness: "auto", ice: "auto" },
    include_seed_reports: true,
  };
  if (kind === "fastest") return { ...base, profiles: ["fastest"] };
  if (kind === "stepfree") return { ...base, priorities: STEPFREE_PRIORITIES };
  return { ...base, priorities: priorities || {} };
}

export function getCurrentLocation() {
  return new Promise((resolve, reject) => {
    if (!navigator.geolocation) {
      reject(new Error("geolocation not supported"));
      return;
    }
    navigator.geolocation.getCurrentPosition(
      (pos) => resolve({ lat: pos.coords.latitude, lon: pos.coords.longitude }),
      () => reject(new Error("geolocation failed")),
      { timeout: 10000 }
    );
  });
}
