(function () {
  var key = "foxpay-theme";
  var media = window.matchMedia ? window.matchMedia("(prefers-color-scheme: dark)") : null;
  var choice = null;

  try {
    choice = window.localStorage.getItem(key);
  } catch (error) {
    choice = null;
  }
  if (choice !== "light" && choice !== "dark") {
    choice = null;
  }

  function applyTheme() {
    var dark = choice ? choice === "dark" : Boolean(media && media.matches);
    document.documentElement.setAttribute("data-theme", dark ? "dark" : "light");
    var toggle = document.getElementById("theme-toggle");
    if (toggle) {
      toggle.setAttribute("aria-label", dark ? "Theme: dark" : "Theme: light");
      toggle.setAttribute("aria-pressed", String(dark));
      toggle.title = dark ? "Dark mode" : "Light mode";
    }
  }

  applyTheme();
  document.addEventListener("DOMContentLoaded", function () {
    var toggle = document.getElementById("theme-toggle");
    if (!toggle) {
      return;
    }
    applyTheme();
    toggle.addEventListener("click", function () {
      choice = document.documentElement.getAttribute("data-theme") === "dark" ? "light" : "dark";
      try {
        window.localStorage.setItem(key, choice);
      } catch (error) {
        // The choice still applies for this page when storage is unavailable.
      }
      applyTheme();
    });
  });

  if (media) {
    if (media.addEventListener) {
      media.addEventListener("change", applyTheme);
    } else if (media.addListener) {
      media.addListener(applyTheme);
    }
  }
})();
