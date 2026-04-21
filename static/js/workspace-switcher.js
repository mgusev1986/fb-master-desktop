/*
 * Workspace switcher — dropdown-поведение в header'е.
 *
 * M1: файл-stub. Активируется в M2 после включения
 * FB_MASTER_MULTI_WORKSPACE_ENABLED. Подключение через
 * templates/_shell/base.html block shell_scripts.
 */
(function () {
  "use strict";

  function init() {
    var root = document.querySelector(".fbm-workspace-switcher");
    if (!root) return; // switcher не отрисован — выходим тихо

    var button = root.querySelector(".fbm-workspace-switcher__button");
    var menu = root.querySelector(".fbm-workspace-switcher__menu");
    if (!button || !menu) return;

    function open() {
      menu.hidden = false;
      button.setAttribute("aria-expanded", "true");
    }
    function close() {
      menu.hidden = true;
      button.setAttribute("aria-expanded", "false");
    }
    function toggle(ev) {
      ev.stopPropagation();
      if (menu.hidden) open(); else close();
    }

    button.addEventListener("click", toggle);
    document.addEventListener("click", function (ev) {
      if (!root.contains(ev.target)) close();
    });
    document.addEventListener("keydown", function (ev) {
      if (ev.key === "Escape") close();
    });
  }

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", init);
  } else {
    init();
  }
})();
