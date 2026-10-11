/* Collapsible headings. Clicking a heading toggles `.collapsed` on its container;
 * descendants marked `.collapse-body` are hidden while collapsed. State is
 * remembered per browser. */
(function () {
  "use strict";

  const KEY = "polestar.collapsed";
  let collapsed = new Set();
  try { collapsed = new Set(JSON.parse(localStorage.getItem(KEY) || "[]")); } catch { /* ignore */ }

  function save() {
    try { localStorage.setItem(KEY, JSON.stringify([...collapsed])); } catch { /* ignore */ }
  }

  const CHEVRON =
    '<svg class="chevron" viewBox="0 0 24 24" aria-hidden="true"><path d="m6 9 6 6 6-6"/></svg>';

  /** Turn `heading`'s contents into a toggle button for `container`. */
  function bind(container, heading, key) {
    if (!container || !heading || heading.dataset.collapseBound) return;
    heading.dataset.collapseBound = "1";

    const btn = document.createElement("button");
    btn.type = "button";
    btn.className = "collapse-btn";
    while (heading.firstChild) btn.appendChild(heading.firstChild);
    btn.insertAdjacentHTML("beforeend", CHEVRON);
    heading.appendChild(btn);
    container.classList.add("collapsible");

    const apply = () => {
      const isCollapsed = collapsed.has(key);
      container.classList.toggle("collapsed", isCollapsed);
      btn.setAttribute("aria-expanded", String(!isCollapsed));
    };
    btn.addEventListener("click", () => {
      if (collapsed.has(key)) collapsed.delete(key);
      else collapsed.add(key);
      save();
      apply();
    });
    apply();
  }

  /** Bind every `[data-collapse]` heading under `root` to its `[data-collapsible]` ancestor. */
  function bindAll(root = document) {
    root.querySelectorAll("[data-collapse]").forEach((heading) => {
      bind(heading.closest("[data-collapsible]"), heading, heading.dataset.collapse);
    });
  }

  window.Collapse = { bind, bindAll };
})();
