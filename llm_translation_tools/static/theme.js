(() => {
  "use strict";

  const storageKey = "translation-workbench.theme";
  let storedTheme = "";
  try {
    storedTheme = localStorage.getItem(storageKey) || "";
  } catch {
    // A saved preference is optional; system appearance remains a safe default.
  }

  const theme = ["light", "dark"].includes(storedTheme)
    ? storedTheme
    : window.matchMedia?.("(prefers-color-scheme: dark)").matches ? "dark" : "light";
  document.documentElement.dataset.theme = theme;
  document.documentElement.style.colorScheme = theme;
})();
