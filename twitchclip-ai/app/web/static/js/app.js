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
    // Offizieller Twitch-Clip-Player erst bei Klick laden (spart Bandbreite, keine Tracker vorab)
    var play = target.closest("[data-embed-src]");
    if (play) {
      var box = play.closest("[data-embed-target]");
      if (box) {
        var frame = document.createElement("iframe");
        frame.src = play.getAttribute("data-embed-src");
        frame.title = "Twitch-Clip";
        frame.allowFullscreen = true;
        frame.setAttribute("allow", "autoplay; fullscreen");
        frame.setAttribute("referrerpolicy", "strict-origin-when-cross-origin");
        frame.className = "embed-frame";
        box.replaceChildren(frame);
      }
      return;
    }
    var close = target.closest("[data-flash-close]");
    if (close) {
      var flash = close.closest("[data-flash]");
      if (flash) flash.remove();
    }
  });

  // Autorisierungslink: beim Antippen komplett markieren (einfaches Kopieren auf dem Handy)
  document.addEventListener("focusin", function (event) {
    var el = event.target;
    if (el instanceof HTMLInputElement && el.hasAttribute("data-select-on-focus")) el.select();
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

  // TikTok-Freigabeformular: Optionen je nach Auswahl ein-/ausblenden (Server prüft alles erneut)
  function updatePublishForm(form) {
    var method = form.querySelector("[data-method]:checked");
    var direct = method && method.value === "direct_post";
    form.querySelectorAll("[data-direct-only]").forEach(function (el) {
      el.hidden = !direct;
      el.querySelectorAll("select, input").forEach(function (input) { input.disabled = !direct || input.hasAttribute("data-locked"); });
    });
    var privacy = form.querySelector("[data-privacy]");
    if (privacy) privacy.required = !!direct;
    var commercial = form.querySelector("[data-commercial]");
    var options = form.querySelector("[data-commercial-options]");
    if (options) options.hidden = !(commercial && commercial.checked);
    var branded = form.querySelector("[data-branded]");
    var isBranded = !!(direct && commercial && commercial.checked && branded && branded.checked);
    if (privacy) {
      var self = privacy.querySelector("option[value=SELF_ONLY]");
      if (self) {
        self.disabled = isBranded;
        if (isBranded && privacy.value === "SELF_ONLY") privacy.value = "";
      }
    }
    var music = form.querySelector("[data-consent-music]");
    var brandedText = form.querySelector("[data-consent-branded]");
    if (music && brandedText) {
      music.hidden = isBranded;
      brandedText.hidden = !isBranded;
    }
  }
  document.querySelectorAll("[data-publish-form]").forEach(function (form) {
    form.querySelectorAll("input[disabled], select[disabled]").forEach(function (el) { el.setAttribute("data-locked", ""); });
    form.addEventListener("change", function () { updatePublishForm(form); });
    updatePublishForm(form);
  });
})();
