// Mirror of hud/src/secret-input.ts's sanitizeSecret. The HUD renderer cannot
// import from this package (bare specifiers do not resolve on a file:// page),
// so the two copies are kept deliberately identical — change both together.
const NOISE = /[\s\u00ad\u00a0\u180e\u200b-\u200f\u2060-\u2064\ufeff]/g;

export function sanitizeSecret(value: string): string {
  return value.replace(NOISE, "");
}
