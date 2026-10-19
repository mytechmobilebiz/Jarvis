import { StrictMode } from "react";
import { createRoot } from "react-dom/client";
// Shared styles first, so a page's own stylesheet (imported through App) wins over them at equal specificity.
import "@fontsource-variable/inter";
import "./styles.css";
import "./tiles.css";
import "./shell/shell.css";
import App from "./App";

createRoot(document.getElementById("root")!).render(
  <StrictMode>
    <App />
  </StrictMode>,
);
