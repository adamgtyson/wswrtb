// Dark/light theme toggle, persisted in localStorage. Applied ASAP to avoid a flash.
(function () {
  const KEY = "wswrtb-theme";
  const saved = localStorage.getItem(KEY);
  const prefersDark = window.matchMedia && window.matchMedia("(prefers-color-scheme: dark)").matches;
  const initial = saved || (prefersDark ? "dark" : "light");
  document.documentElement.setAttribute("data-theme", initial);

  window.WSWRTB_toggleTheme = function () {
    const current = document.documentElement.getAttribute("data-theme");
    const next = current === "dark" ? "light" : "dark";
    document.documentElement.setAttribute("data-theme", next);
    localStorage.setItem(KEY, next);
    const btn = document.getElementById("theme-toggle");
    if (btn) btn.textContent = next === "dark" ? "☀︎ Light" : "☾ Dark";
  };

  document.addEventListener("DOMContentLoaded", function () {
    const btn = document.getElementById("theme-toggle");
    if (btn) {
      btn.textContent = initial === "dark" ? "☀︎ Light" : "☾ Dark";
      btn.addEventListener("click", window.WSWRTB_toggleTheme);
    }
  });
})();
