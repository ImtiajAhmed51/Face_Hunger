import { mutate } from "./api";

// Render timings are measured and posted only while local diagnostics are on.
let on = false;
export function setDiagnostics(value: boolean): void { on = value; }

/** Time from a route change to the second painted frame; the name is the first path segment only. */
export function routeRendered(pathname: string): void {
  if (!on) return;
  const started = performance.now();
  const name = `route.${(pathname.split("/")[1] || "home").toLowerCase().replace(/[^a-z0-9_-]/g, "").slice(0, 30) || "home"}`;
  requestAnimationFrame(() => requestAnimationFrame(() => {
    void mutate("/diagnostics/client", { name, ms: Math.round((performance.now() - started) * 10) / 10 }).catch(() => {});
  }));
}
