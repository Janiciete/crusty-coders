// Stub for P16 (auth). `init(ctx)` is called once at startup with the
// shared `ctx` object (see app.js); P16 will populate #auth-slot and
// #auth-dialog and read/write preferences via ctx.getPrefs()/setPrefs().
// No-op today -- the rest of the app works fully without it.
export function init(ctx) {}
