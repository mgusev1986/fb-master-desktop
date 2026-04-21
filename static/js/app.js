/* FB Master — клиентский JS */

(function () {
  "use strict";

  /* ── Тема ─────────────────────────────────────── */
  const THEME_KEY = "fb-bo-theme";

  function getStoredTheme() {
    return localStorage.getItem(THEME_KEY) || "dark";
  }

  function applyTheme(theme) {
    document.documentElement.setAttribute("data-theme", theme);
    localStorage.setItem(THEME_KEY, theme);
    const icon = document.getElementById("theme-icon");
    if (icon) icon.className = theme === "dark" ? "ti ti-sun" : "ti ti-moon";
  }

  applyTheme(getStoredTheme());

  document.addEventListener("DOMContentLoaded", function () {
    const btn = document.getElementById("theme-toggle");
    if (btn) {
      btn.addEventListener("click", function () {
        const next = getStoredTheme() === "light" ? "dark" : "light";
        applyTheme(next);
      });
    }

    /* ── Upload zone drag & drop ─────────────────── */
    var zone = document.querySelector(".upload-zone");
    if (zone) {
      var fileInput = zone.querySelector('input[type="file"]');

      zone.addEventListener("click", function () {
        if (fileInput) fileInput.click();
      });

      zone.addEventListener("dragover", function (e) {
        e.preventDefault();
        zone.classList.add("dragover");
      });

      zone.addEventListener("dragleave", function () {
        zone.classList.remove("dragover");
      });

      zone.addEventListener("drop", function (e) {
        e.preventDefault();
        zone.classList.remove("dragover");
        if (fileInput && e.dataTransfer.files.length) {
          fileInput.files = e.dataTransfer.files;
          /* Только подставляем файлы; импорт — по кнопке (см. страницу /import) */
          fileInput.dispatchEvent(new Event("change", { bubbles: true }));
        }
      });
    }

  });

  /* ── Кастомное подтверждение (вне DOMContentLoaded: слушатель submit всегда висит на document) ── */
  (function setupConfirmModal() {
    var modal = null;
    var msgEl = null;
    var titleEl = null;
    var okBtn = null;
    var pendingForm = null;
    var pendingCallback = null;
    var defaultOkLabel = "Подтвердить";
    var wired = false;

    function close() {
      if (!modal || !okBtn) return;
      modal.hidden = true;
      modal.setAttribute("aria-hidden", "true");
      document.body.classList.remove("fb-confirm-modal-open");
      pendingForm = null;
      pendingCallback = null;
      okBtn.textContent = defaultOkLabel;
    }

    function applyVariant(variantRaw) {
      if (!okBtn) return;
      okBtn.classList.remove("btn-primary", "btn-danger");
      var variant = (variantRaw || "").toLowerCase();
      if (variant === "danger") {
        okBtn.classList.add("btn-danger");
      } else {
        okBtn.classList.add("btn-primary");
      }
    }

    function open(form) {
      if (!wireIfNeeded() || !modal || !msgEl || !titleEl || !okBtn) return;
      var message = form.getAttribute("data-confirm");
      if (!message) return;
      pendingForm = form;
      pendingCallback = null;
      msgEl.textContent = message;
      var customTitle = form.getAttribute("data-confirm-title");
      titleEl.textContent = customTitle || "Подтвердить действие";
      okBtn.textContent = defaultOkLabel;
      applyVariant(form.getAttribute("data-confirm-variant"));
      modal.hidden = false;
      modal.setAttribute("aria-hidden", "false");
      document.body.classList.add("fb-confirm-modal-open");
      okBtn.focus();
    }

    function wireIfNeeded() {
      if (wired) return true;
      modal = document.getElementById("fb-confirm-modal");
      msgEl = document.getElementById("fb-confirm-modal-message");
      titleEl = document.getElementById("fb-confirm-modal-title");
      okBtn = document.getElementById("fb-confirm-modal-ok");
      if (!modal || !msgEl || !titleEl || !okBtn) {
        modal = msgEl = titleEl = okBtn = null;
        return false;
      }
      defaultOkLabel = okBtn.textContent || "Подтвердить";

      modal.querySelectorAll("[data-fb-confirm-dismiss]").forEach(function (el) {
        el.addEventListener("click", function () {
          close();
        });
      });

      okBtn.addEventListener("click", function () {
        var cb = pendingCallback;
        var f = pendingForm;
        if (cb) {
          close();
          try {
            cb();
          } catch (err) {
            console.error(err);
          }
          return;
        }
        if (f) {
          close();
          f.submit();
          return;
        }
        close();
      });

      document.addEventListener(
        "keydown",
        function (e) {
          if (e.key === "Escape" && modal && !modal.hidden) {
            e.preventDefault();
            close();
          }
        },
        true
      );

      wired = true;
      return true;
    }

    /**
     * @param {{ message: string, title?: string, variant?: string, confirmLabel?: string, onConfirm: () => void }} opts
     */
    window.fbConfirm = function (opts) {
      if (!opts || typeof opts.onConfirm !== "function" || !opts.message) return;
      if (!wireIfNeeded() || !modal || !msgEl || !titleEl || !okBtn) return;
      pendingForm = null;
      pendingCallback = opts.onConfirm;
      msgEl.textContent = opts.message;
      titleEl.textContent = opts.title || "Подтвердить действие";
      okBtn.textContent = (opts.confirmLabel && String(opts.confirmLabel).trim()) || defaultOkLabel;
      applyVariant(opts.variant);
      modal.hidden = false;
      modal.setAttribute("aria-hidden", "false");
      document.body.classList.add("fb-confirm-modal-open");
      okBtn.focus();
    };

    document.addEventListener(
      "submit",
      function (e) {
        var form = e.target;
        if (!form || form.tagName !== "FORM") return;
        if (!form.hasAttribute("data-confirm")) return;
        if (!wireIfNeeded()) {
          /* Разметка ещё не готова — не блокируем отправку (иначе «мёртвая» кнопка) */
          return;
        }
        e.preventDefault();
        e.stopPropagation();
        open(form);
      },
      true
    );

    function tryEarlyWire() {
      wireIfNeeded();
    }

    if (document.readyState === "loading") {
      document.addEventListener("DOMContentLoaded", tryEarlyWire);
    } else {
      tryEarlyWire();
    }
  })();
})();
