// Theme control, shared by every page. Applied before first paint by the inline
// snippet in each document head; this file wires the buttons and persistence.
(function () {
  var KEY = "overturn-theme", root = document.documentElement;
  var buttons = document.querySelectorAll(".theme button");
  function apply(mode) {
    if (mode === "light" || mode === "dark") root.setAttribute("data-theme", mode);
    else root.removeAttribute("data-theme");
    buttons.forEach(function (b) {
      b.setAttribute("aria-pressed", String(b.dataset.mode === (mode || "auto")));
    });
  }
  var saved = null;
  // Storage throws outright in some privacy modes. A failure must leave the
  // page working on the system preference rather than blank.
  try { saved = localStorage.getItem(KEY); } catch (e) {}
  apply(saved || "auto");
  buttons.forEach(function (b) {
    b.addEventListener("click", function () {
      apply(b.dataset.mode);
      try {
        if (b.dataset.mode === "auto") localStorage.removeItem(KEY);
        else localStorage.setItem(KEY, b.dataset.mode);
      } catch (e) {}
    });
  });
})();
