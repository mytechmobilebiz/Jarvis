/** The dashboard's own version (package.json), fixed at build time by vite.config.ts. */
declare const __APP_VERSION__: string;

/** A file's text, imported with Vite's "?raw" suffix (the tests read the release's VERSION file this way). */
declare module "*?raw" {
  const text: string;
  export default text;
}
