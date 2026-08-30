// The only script on the public site: the light/dark toggle.
//
// Light is the default and it is not negotiated with the operating system.
// The palette was designed against the light tokens, so an unconfigured
// visitor sees the version that was actually designed. A reader who wants
// dark asks for it once and the choice sticks.
//
// The document head applies the stored choice before first paint; this file
// only wires the control and writes the choice down.
//
// The choice is written twice, to localStorage and to a cookie, because this
// site and the review queue behind it are one product at one address but not
// one runtime. These pages are static and read localStorage; the queue is
// server-rendered and reads the `overturn_theme` cookie before it emits any
// HTML, and it ships no JavaScript of its own to consult storage with. Writing
// only one of the two is what made a reader who chose dark on this page arrive
// at a light queue -- the exact seam this design set out to close.
(function () {
  "use strict";

  var KEY = "overturn-theme";
  var COOKIE = "overturn_theme";
  var root = document.documentElement;
  var button = document.querySelector(".theme");
  if (!button) return;

  function isDark() {
    return root.getAttribute("data-theme") === "dark";
  }

  function apply(dark) {
    if (dark) root.setAttribute("data-theme", "dark");
    else root.removeAttribute("data-theme");
    // The label stays "Dark mode" in both states; aria-pressed carries whether
    // it is on. A button whose name changes to describe the next state reads
    // as a lie to a screen reader.
    button.setAttribute("aria-pressed", String(dark));
  }

  apply(isDark());

  button.addEventListener("click", function () {
    var next = !isDark();
    apply(next);
    // Storage throws outright in some privacy modes. Losing the preference on
    // reload is a smaller failure than losing the button.
    try {
      if (next) localStorage.setItem(KEY, "dark");
      else localStorage.removeItem(KEY);
    } catch (e) {}
    // Same choice, in the form the server can read. A year, because a
    // preference that expires is a preference the reader has to set again.
    document.cookie = next
      ? COOKIE + "=dark;path=/;max-age=31536000;samesite=lax"
      : COOKIE + "=;path=/;max-age=0;samesite=lax";
  });
})();

// Wide content — tables, mostly — lives in a horizontal scroller so the page
// itself never scrolls sideways. A scroller a mouse can reach and a keyboard
// cannot is a broken control, so give it a tab stop; but only when it actually
// overflows, otherwise every table becomes a tab stop that does nothing.
(function () {
  "use strict";

  function sync() {
    var boxes = document.querySelectorAll(".scroll");
    for (var i = 0; i < boxes.length; i++) {
      var box = boxes[i];
      if (box.scrollWidth > box.clientWidth + 1) {
        box.setAttribute("tabindex", "0");
        box.setAttribute("role", "region");
        if (!box.hasAttribute("aria-label")) box.setAttribute("aria-label", "Table, scrolls sideways");
      } else {
        box.removeAttribute("tabindex");
        box.removeAttribute("role");
      }
    }
  }

  sync();
  // Fonts landing and the window resizing both change the answer.
  if (document.fonts && document.fonts.ready) document.fonts.ready.then(sync);
  var t;
  window.addEventListener("resize", function () {
    clearTimeout(t);
    t = setTimeout(sync, 150);
  });
})();
