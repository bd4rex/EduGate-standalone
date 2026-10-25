/* Secondary navigation keeps related settings together without discarding forms. */
(() => {
  "use strict";
  const mainTabs = document.getElementById("main-tabs");
  if (mainTabs) {
    const syncNavigationHeight = () => document.documentElement.style.setProperty("--main-navigation-height", `${mainTabs.getBoundingClientRect().height}px`);
    syncNavigationHeight();
    if (typeof ResizeObserver !== "undefined") new ResizeObserver(syncNavigationHeight).observe(mainTabs);
    window.addEventListener("resize", syncNavigationHeight, { passive: true });
  }
  function workspace(rootId, label, groups) {
    const root = document.getElementById(rootId);
    if (!root) return;
    const navigation = document.createElement("nav");
    navigation.className = "workspace-tabs";
    navigation.setAttribute("role", "tablist");
    navigation.setAttribute("aria-label", label);
    const entries = [];
    groups.forEach(({ key, title, nodes, grid }) => {
      const panel = document.createElement("section");
      panel.className = `workspace-panel ${grid || ""}`;
      panel.id = `${rootId}-${key}`;
      panel.setAttribute("role", "tabpanel");
      panel.setAttribute("aria-labelledby", `${panel.id}-tab`);
      panel.hidden = entries.length > 0;
      nodes.forEach((selector) => {
        let node = root.querySelector(selector);
        if (!node) return;
        if (node.tagName === "DETAILS" && node.parentElement?.classList.contains("section")) node = node.parentElement;
        // Existing IDs, listeners and input values survive the move.
        const details = node.querySelector(":scope > details.collapsible-panel");
        if (details) {
          const summary = details.querySelector(":scope > summary");
          if (summary) {
            const heading = document.createElement("div");
            heading.className = "section-title workspace-panel-heading";
            const h2 = document.createElement("h2");
            h2.textContent = summary.textContent;
            heading.append(h2);
            if (details.id === "backup-panel") {
              const download = details.querySelector("#download-backup");
              if (download) heading.append(download);
            }
            node.prepend(heading);
            summary.hidden = true;
          }
          details.open = true;
          if (key === "models") {
            const picker = details.querySelector(".provider-picker");
            const provider = details.querySelector(".upstream-provider");
            const models = details.querySelector(".imported-models");
            if (picker && provider && models) {
              const layout = document.createElement("div");
              layout.className = "model-settings-grid";
              const editor = document.createElement("div");
              editor.className = "model-editor-column";
              picker.before(layout);
              editor.append(picker, provider);
              layout.append(editor, models);
            }
          }
        }
        panel.append(node);
      });
      if (key === "library") {
        const column = document.createElement("div");
        column.className = "resource-file-column";
        const upload = panel.querySelector(".resource-upload-section");
        const files = panel.querySelector(".resource-files-section");
        if (upload && files) { column.append(upload, files); panel.append(column); }
      }
      const button = document.createElement("button");
      button.type = "button";
      button.className = "workspace-tab";
      button.id = `${panel.id}-tab`;
      button.textContent = title;
      button.setAttribute("role", "tab");
      button.setAttribute("aria-controls", panel.id);
      button.setAttribute("aria-selected", String(entries.length === 0));
      button.tabIndex = entries.length === 0 ? 0 : -1;
      navigation.append(button);
      entries.push({ key, button, panel });
    });
    function select(key, focus = false) {
      root.querySelectorAll("details.action-menu[open]").forEach((menu) => { menu.open = false; });
      root.dataset.activePanel = key;
      entries.forEach((entry) => {
        const selected = entry.key === key;
        entry.panel.hidden = !selected;
        entry.button.setAttribute("aria-selected", String(selected));
        entry.button.tabIndex = selected ? 0 : -1;
        if (selected && focus) entry.button.focus();
      });
      const topic = rootId === "system-view" ? "edugate:system-panel-change" : "edugate:resource-panel-change";
      window.dispatchEvent(new CustomEvent(topic, { detail: key }));
      window.scrollTo({ top: 0 });
    }
    entries.forEach((entry, index) => {
      entry.button.addEventListener("click", () => select(entry.key));
      entry.button.addEventListener("keydown", (event) => {
        if (event.ctrlKey || event.metaKey || event.altKey || !["ArrowLeft", "ArrowRight", "Home", "End"].includes(event.key)) return;
        event.preventDefault();
        const next = event.key === "Home" ? 0 : event.key === "End" ? entries.length - 1
          : (index + (event.key === "ArrowRight" ? 1 : -1) + entries.length) % entries.length;
        select(entries[next].key, true);
      });
    });
    root.prepend(navigation);
    entries.forEach((entry) => root.append(entry.panel));
    root.dataset.activePanel = entries[0].key;
  }
  workspace("settings-view", "资源分类", [
    { key: "library", title: "资料管理", grid: "resource-management-grid", nodes: [".resource-knowledge-section", ".resource-upload-section", ".resource-files-section"] },
    { key: "scope", title: "课堂范围", nodes: ["#knowledge-tools"] },
    { key: "policy", title: "黑名单与试问", grid: "resource-policy-grid", nodes: ["#knowledge-policy", "#knowledge-trial"] },
  ]);
  workspace("system-view", "系统设置分类", [
    { key: "overview", title: "运行状态", nodes: [".system-overview-section"] },
    { key: "models", title: "模型服务", nodes: ["#model-api-panel"] },
    { key: "pages", title: "网页发布", nodes: ["#teaching-page-panel"] },
    { key: "integration", title: "接入配置", nodes: ["#downstream-api-panel"] },
    { key: "advanced", title: "高级设置", nodes: ["#advanced-settings-panel"] },
    { key: "logs", title: "运行日志", nodes: ["#system-log-panel"] },
    { key: "maintenance", title: "备份与升级", grid: "maintenance-grid", nodes: ["#backup-panel", "#program-upgrade-panel"] },
  ]);
})();
