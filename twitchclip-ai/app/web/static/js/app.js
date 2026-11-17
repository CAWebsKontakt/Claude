// TwitchClip AI – minimales JavaScript (CSP: nur Dateien von 'self', kein Inline-Code)
(function () {
  "use strict";

  function setNav(open) {
    document.body.classList.toggle("nav-open", open);
    var toggle = document.querySelector("[data-nav-toggle]");
    if (toggle) toggle.setAttribute("aria-expanded", open ? "true" : "false");
  }

  document.addEventListener("click", function (event) {
    var target = event.target;
    if (!(target instanceof Element)) return;

    if (target.closest("[data-nav-toggle]")) {
      setNav(!document.body.classList.contains("nav-open"));
      return;
    }
    if (target.closest("[data-nav-close]")) {
      setNav(false);
      return;
    }
    var close = target.closest("[data-flash-close]");
    if (close) {
      var flash = close.closest("[data-flash]");
      if (flash) flash.remove();
    }
  });

  document.addEventListener("keydown", function (event) {
    if (event.key === "Escape") setNav(false);
  });

  // Sicherheitsabfrage für destruktive Formulare: <form data-confirm="Wirklich?">
  document.addEventListener("submit", function (event) {
    var form = event.target;
    if (!(form instanceof HTMLFormElement)) return;
    var message = form.getAttribute("data-confirm");
    if (message && !window.confirm(message)) {
      event.preventDefault();
      return;
    }
    // Doppelte Übermittlung verhindern
    var buttons = form.querySelectorAll("button[type=submit]");
    window.setTimeout(function () {
      buttons.forEach(function (b) { b.disabled = true; });
    }, 0);
  });

  // Nach "Zurück" im Browser (bfcache) Buttons wieder aktivieren
  window.addEventListener("pageshow", function () {
    document.querySelectorAll("button[type=submit]:disabled").forEach(function (b) { b.disabled = false; });
  });

  // Erfolgsmeldungen nach einigen Sekunden ausblenden
  window.setTimeout(function () {
    document.querySelectorAll(".flash-success[data-flash]").forEach(function (el) { el.remove(); });
  }, 6000);
})();
