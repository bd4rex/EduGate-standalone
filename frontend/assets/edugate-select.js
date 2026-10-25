/* Shared, keyboard-accessible select UI. Native selects remain the data source. */
(() => {
  "use strict";
  const controls = new Map();
  const menu = document.createElement("div");
  menu.id = "edugate-select-options";
  menu.className = "edugate-select-menu";
  menu.setAttribute("role", "listbox");
  menu.hidden = true;
  document.body.append(menu);
  let active = null;
  let highlighted = -1;
  let search = "";
  let searchAt = 0;

  function labelFor(select) {
    return select.getAttribute("aria-label") || Array.from(select.labels || []).map(label =>
      Array.from(label.childNodes).filter(node => node.nodeType === Node.TEXT_NODE).map(node => node.textContent.trim()).join(" ")
    ).join(" ").trim() || "选择选项";
  }

  function sync(select, button) {
    const text = select.selectedOptions[0]?.textContent.trim() || "请选择";
    const label = button.querySelector(".edugate-select-value");
    if (label.textContent !== text) label.textContent = text;
    button.disabled = select.disabled || !select.options.length;
    button.setAttribute("aria-label", labelFor(select));
    button.title = text;
  }

  function close() {
    if (active) {
      controls.get(active)?.setAttribute("aria-expanded", "false");
      controls.get(active)?.removeAttribute("aria-activedescendant");
    }
    active = null;
    menu.hidden = true;
    menu.replaceChildren();
    search = "";
  }

  function position() {
    if (!active) return;
    const button = controls.get(active);
    if (!button?.isConnected || !button.getClientRects().length) return close();
    const rect = button.getBoundingClientRect();
    const width = Math.min(Math.max(rect.width, 240), window.innerWidth - 24);
    const below = window.innerHeight - rect.bottom - 12;
    const above = rect.top - 12;
    const placeAbove = below < 180 && above > below;
    menu.style.width = `${width}px`;
    menu.style.left = `${Math.max(12, Math.min(rect.left, window.innerWidth - width - 12))}px`;
    menu.style.maxHeight = `${Math.min(320, Math.max(80, placeAbove ? above - 6 : below - 6))}px`;
    menu.style.top = placeAbove ? "auto" : `${rect.bottom + 6}px`;
    menu.style.bottom = placeAbove ? `${window.innerHeight - rect.top + 6}px` : "auto";
  }

  function highlight(index) {
    if (!active || index < 0) return;
    highlighted = index;
    for (const option of menu.children) option.classList.toggle("is-highlighted", Number(option.dataset.index) === index);
    const target = menu.children[index];
    controls.get(active).setAttribute("aria-activedescendant", target.id);
    target.scrollIntoView({ block: "nearest" });
  }

  function enabledIndexes() {
    return Array.from(active.options).flatMap((option, index) => option.disabled || option.parentElement.disabled ? [] : [index]);
  }

  function choose(index) {
    if (!active || !enabledIndexes().includes(index)) return;
    const select = active;
    const button = controls.get(select);
    const changed = select.selectedIndex !== index;
    select.selectedIndex = index;
    sync(select, button);
    close();
    button.focus();
    if (changed) {
      select.dispatchEvent(new Event("input", { bubbles: true }));
      select.dispatchEvent(new Event("change", { bubbles: true }));
    }
  }

  function open(select) {
    close();
    active = select;
    const button = controls.get(select);
    button.setAttribute("aria-expanded", "true");
    menu.setAttribute("aria-label", labelFor(select));
    menu.replaceChildren(...Array.from(select.options, (option, index) => {
      const row = document.createElement("div");
      row.id = `edugate-option-${index}`;
      row.className = "edugate-select-option";
      row.dataset.index = index;
      row.setAttribute("role", "option");
      row.setAttribute("aria-label", option.textContent.trim());
      row.setAttribute("aria-selected", String(option.selected));
      row.setAttribute("aria-disabled", String(option.disabled || Boolean(option.parentElement.disabled)));
      row.textContent = option.textContent.trim();
      row.addEventListener("pointermove", () => { if (enabledIndexes().includes(index)) highlight(index); });
      row.addEventListener("click", () => choose(index));
      return row;
    }));
    menu.hidden = false;
    position();
    highlight(enabledIndexes().includes(select.selectedIndex) ? select.selectedIndex : enabledIndexes()[0] ?? -1);
  }

  function enhance(select) {
    if (controls.has(select) || select.multiple || select.size > 1) return;
    const wrapper = document.createElement("span");
    wrapper.className = "edugate-select";
    const button = document.createElement("button");
    button.type = "button";
    button.className = "edugate-select-trigger";
    button.setAttribute("role", "combobox");
    button.setAttribute("aria-haspopup", "listbox");
    button.setAttribute("aria-expanded", "false");
    button.setAttribute("aria-controls", menu.id);
    button.innerHTML = '<span class="edugate-select-value"></span><span class="edugate-select-chevron" aria-hidden="true"></span>';
    select.before(wrapper);
    wrapper.append(select, button);
    select.hidden = true;
    select.tabIndex = -1;
    controls.set(select, button);
    sync(select, button);
    select.addEventListener("change", () => sync(select, button));
    select.addEventListener("focus", () => button.focus());
    button.addEventListener("click", () => active === select ? close() : open(select));
    button.addEventListener("keydown", event => {
      if (["ArrowDown", "ArrowUp", "Home", "End", "Enter", " ", "Escape"].includes(event.key)) {
        event.preventDefault();
        if (event.key === "Escape") return close();
        if (event.key === "Enter" || event.key === " ") return active === select ? choose(highlighted) : open(select);
        if (active !== select) open(select);
        const indexes = enabledIndexes();
        const current = indexes.indexOf(highlighted);
        const next = event.key === "Home" ? 0 : event.key === "End" ? indexes.length - 1 : Math.max(0, Math.min(indexes.length - 1, current + (event.key === "ArrowDown" ? 1 : -1)));
        highlight(indexes[next] ?? -1);
      } else if (event.key === "Tab") close();
      else if (event.key.length === 1 && !event.ctrlKey && !event.metaKey && !event.altKey) {
        event.preventDefault();
        if (active !== select) open(select);
        search = Date.now() - searchAt > 700 ? event.key : search + event.key;
        searchAt = Date.now();
        const index = enabledIndexes().find(i => select.options[i].textContent.trim().toLocaleLowerCase().startsWith(search.toLocaleLowerCase()));
        if (index !== undefined) highlight(index);
      }
    });
  }

  function refresh() {
    for (const [select, button] of controls) {
      if (!select.isConnected) {
        if (active === select) close();
        controls.delete(select);
      } else sync(select, button);
    }
    document.querySelectorAll("select").forEach(enhance);
  }
  // Reconcile dynamic model/source lists. Ignore mutations made by the custom UI.
  new MutationObserver(records => {
    if (records.some(record => record.target.closest?.("select") || Array.from(record.addedNodes).some(node => node.nodeType === 1 && (node.matches("select") || node.querySelector("select"))) || Array.from(controls.keys()).some(select => !select.isConnected))) {
      if (active && records.some(record => record.target === active || active.contains(record.target))) close();
      refresh();
    }
  }).observe(document.body, { childList: true, subtree: true, attributes: true, attributeFilter: ["disabled", "selected", "aria-label"] });
  document.addEventListener("pointerdown", event => { if (active && !menu.contains(event.target) && !controls.get(active).contains(event.target)) close(); });
  document.addEventListener("focusin", event => { if (active && event.target !== controls.get(active)) close(); });
  document.addEventListener("scroll", event => { if (!menu.contains(event.target)) position(); }, true);
  window.addEventListener("resize", position);
  window.EduGateSelect = { refresh, close };
  refresh();
})();
