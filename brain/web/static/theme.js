"use strict";

// Loaded in <head> without defer: a saved theme or a closed sidebar applies before the first paint.
try {
  const theme = localStorage.getItem("brain.theme");
  if (theme === "light" || theme === "dark") document.documentElement.dataset.theme = theme;
  if (localStorage.getItem("brain.side") === "closed") document.documentElement.dataset.side = "closed";
} catch {
  // Storage blocked: the system theme applies and the sidebar stays open.
}
