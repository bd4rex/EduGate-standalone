(() => {
  "use strict";
  const host = window.EduGateKnowledgeAdmin;
  const byId = (id) => document.getElementById(id);
  const fields = ["subject", "grade", "edition", "term", "chapter"];
  const escape = (value) => String(value ?? "").replace(/[&<>"']/g, (char) => ({"&":"&amp;", "<":"&lt;", ">":"&gt;", '"':"&quot;", "'":"&#39;"}[char]));
  let sourceId = "", appliedSource = null, filesStamp = "", dirty = false, polling = false, lastJob = "";
  let policyLoaded = false, policyDirty = false;

  async function loadPolicy(force = false) {
    if (!host.authenticated() || (policyDirty && !force)) return;
    try {
      const policy = await host.request("/knowledge/policy");
      byId("knowledge-blacklist-enabled").checked = policy.enabled;
      byId("knowledge-blacklist-topics").value = policy.topics.map((topic) => [topic.label, ...topic.aliases].join("|")).join("\n");
      byId("knowledge-blacklist-message").value = policy.message;
      for (const id of ["knowledge-blacklist-enabled", "knowledge-blacklist-topics", "knowledge-blacklist-message", "knowledge-policy-save"]) byId(id).disabled = false;
      byId("knowledge-policy-status").textContent = policy.enabled && policy.topics.length ? `已启用 ${policy.topics.length} 个禁答话题。新提问会按这些规则检查。` : "当前没有启用的禁答话题。";
      policyLoaded = true; policyDirty = false;
    } catch (error) { byId("knowledge-policy-status").textContent = `规则读取失败：${error.message}`; }
  }

  function readScope() {
    return Object.fromEntries([
      ["file_ids", [...byId("knowledge-scope-files").querySelectorAll("input:checked")].map((input) => input.value)],
      ...fields.map((field) => [field, byId(`knowledge-scope-${field}`).value.trim()]),
    ]);
  }

  function renderScope() {
    const scenario = host.scenario();
    if (!scenario) return;
    const next = scenario.knowledge_source_id || "";
    if (next !== appliedSource) { appliedSource = next; sourceId = next || host.sources()[0]?.id || ""; dirty = false; filesStamp = ""; }
    if (!sourceId) sourceId = host.sources()[0]?.id || "";
    const scope = dirty ? readScope() : (scenario.knowledge_scope || {file_ids: []});
    const options = host.sources().map((source) => `<option value="${escape(source.id)}">${escape(source.name)}</option>`).join("");
    if (byId("knowledge-scope-source").innerHTML !== options) byId("knowledge-scope-source").innerHTML = options;
    byId("knowledge-scope-source").value = sourceId;
    fields.forEach((field) => { if (!dirty) byId(`knowledge-scope-${field}`).value = scope[field] || ""; });
    const files = host.files().filter((file) => file.source_id === sourceId && file.metadata?.audience !== "teacher");
    const stamp = JSON.stringify([sourceId, files.map((file) => [file.id, file.filename])]);
    const selectionChanged = !dirty && JSON.stringify(readScope().file_ids.sort()) !== JSON.stringify([...(scope.file_ids || [])].sort());
    if (filesStamp !== stamp || selectionChanged) {
      byId("knowledge-scope-files").innerHTML = files.map((file) => `<label><input type="checkbox" value="${escape(file.id)}" ${(scope.file_ids || []).includes(file.id) ? "checked" : ""} /><span>${escape(file.filename)}</span></label>`).join("") || '<p class="hint">本库暂无学生可用资料。</p>';
      filesStamp = stamp;
    }
    byId("knowledge-scope-note").textContent = dirty ? "范围有修改，应用后才会影响学生问答。" : "范围设置保存后生效。";
  }

  async function refreshStatus() {
    if (polling || !host.authenticated()) return;
    polling = true;
    try {
      const status = await host.request("/knowledge/status");
      if (!policyLoaded) await loadPolicy();
      byId("knowledge-status").textContent = !status.chunk_count ? "等待导入课堂资料" : status.mode === "hybrid" ? (status.embedded_count === status.chunk_count ? "中文混合检索已就绪" : "混合检索已启用，部分资料待重建") : "当前使用中文关键词检索";
      byId("knowledge-status-detail").textContent = `${status.chunk_count} 个资料片段 · ${status.embedded_count} 个语义片段` + (status.model_ready ? " · 语义计算在本机完成" : " · 准备约 90 MB 的本地模型后可使用语义检索");
      const job = status.job || {};
      byId("knowledge-job").textContent = [job.message, ...(job.errors || []), status.model_error].filter(Boolean).join("；");
      byId("knowledge-job").classList.toggle("knowledge-warning", job.status === "failed");
      byId("knowledge-prepare").disabled = job.status === "running";
      byId("knowledge-reindex").disabled = job.status === "running";
      byId("knowledge-prepare").textContent = status.model_ready ? "重新准备语义模型" : "准备语义模型";
      if (job.id && job.id !== lastJob && ["completed", "failed"].includes(job.status)) {
        lastJob = job.id;
        await host.refresh();
      }
      renderScope();
    } catch (error) {
      byId("knowledge-status").textContent = `状态读取失败：${error.message}`;
    } finally { polling = false; }
  }

  async function startJob(action) {
    try {
      await host.request("/knowledge/jobs", {method:"POST", body:JSON.stringify({action})});
      await refreshStatus();
    } catch (error) { host.toast(error.message, true); }
  }

  async function search() {
    const button = byId("knowledge-search"), query = byId("knowledge-question").value.trim();
    if (!sourceId || !query) { host.toast("请先选择知识库并输入问题。", true); return; }
    button.disabled = true;
    const results = byId("knowledge-search-results");
    results.textContent = "正在查找资料…";
    try {
      const result = await host.request("/knowledge/search", {method:"POST", body:JSON.stringify({source_id:sourceId, query, scope:readScope(), mode:byId("knowledge-search-mode").value})});
      if (result.blocked) {
        results.textContent = result.policy_decision === "blocked" ? `命中课堂黑名单${result.blocked_topic ? `：${result.blocked_topic}` : ""}。${result.block_reason}` : result.block_reason;
        return;
      }
      results.innerHTML = `<p class="hint">${result.mode === "hybrid" ? "混合检索" : result.mode === "vector" ? "语义检索" : "关键词检索"} · ${result.elapsed_ms} ms · ${result.hits.length} 个候选片段</p>`;
      if (!result.hits.length) results.insertAdjacentHTML("beforeend", "<p>当前范围内未找到资料依据，请检查文件、章节和版本筛选。</p>");
      result.hits.forEach((hit, index) => {
        const card = document.createElement("article");
        card.className = "knowledge-result";
        card.innerHTML = `<strong>[K${index+1}] ${escape(hit.filename)}</strong><div class="hint">${escape(hit.chapter || "正文")}${hit.page ? ` · 第 ${hit.page} 页/张` : ""}${hit.semantic_score != null ? ` · 语义相似度 ${hit.semantic_score.toFixed(3)}` : ""}</div><pre>${escape(hit.context_text || hit.text)}</pre>`;
        results.append(card);
      });
    } catch (error) { results.textContent = `试问失败：${error.message}`; }
    finally { button.disabled = false; }
  }

  function dialog(title) {
    const element = document.createElement("dialog");
    element.className = "knowledge-dialog";
    element.setAttribute("aria-label", title);
    element.innerHTML = `<div class="dialog-heading"><strong>${escape(title)}</strong><button class="button ghost" type="button">关闭</button></div><div class="dialog-content"></div>`;
    element.querySelector("button").onclick = () => element.close();
    element.addEventListener("close", () => element.remove());
    document.body.append(element);
    element.showModal();
    return element;
  }

  async function preview(id) {
    const element = dialog("资料解析预览"), content = element.querySelector(".dialog-content");
    let offset = 0;
    async function load() {
      content.textContent = "正在读取…";
      try {
        const data = await host.request(`/knowledge/files/${encodeURIComponent(id)}/preview?offset=${offset}`);
        content.innerHTML = `<h3>${escape(data.file.filename)}</h3><p class="hint">共 ${data.total} 段 · 当前 ${offset+1}–${offset+data.chunks.length} 段</p><p class="knowledge-warning">${escape([data.file.index_error, ...(data.file.warnings || [])].filter(Boolean).join("；"))}</p><div class="actions"><button type="button" class="button soft" data-original>下载原文件</button><button type="button" class="button ghost" data-previous ${offset === 0 ? "disabled" : ""}>上一组</button><button type="button" class="button ghost" data-next ${offset+data.chunks.length >= data.total ? "disabled" : ""}>下一组</button></div>`;
        for (const chunk of data.chunks) content.insertAdjacentHTML("beforeend", `<section class="preview-chunk"><strong>${escape(chunk.chapter || "正文")}${chunk.page ? ` · 第 ${chunk.page} 页/张` : ""}</strong><pre>${escape(chunk.text)}</pre></section>`);
        content.querySelector("[data-previous]").onclick = () => { offset = Math.max(0, offset-100); load(); };
        content.querySelector("[data-next]").onclick = () => { offset += 100; load(); };
        content.querySelector("[data-original]").onclick = async () => {
          try {
            const blob = await host.original(id), url = URL.createObjectURL(blob), link = document.createElement("a");
            link.href = url; link.download = data.file.filename; link.click();
            setTimeout(() => URL.revokeObjectURL(url), 5000);
          } catch (error) { host.toast(error.message, true); }
        };
      } catch (error) { content.textContent = error.message; }
    }
    await load();
  }

  function editMetadata(id) {
    const file = host.files().find((item) => item.id === id);
    if (!file) return;
    const element = dialog("资料属性"), content = element.querySelector(".dialog-content"), metadata = file.metadata || {};
    const labels = {subject:"学科", grade:"年级", edition:"教材版本", term:"学期"};
    content.innerHTML = `<p>${escape(file.filename)}</p><form><div class="metadata-grid">${Object.entries(labels).map(([key,label]) => `<label>${label}<input name="${key}" maxlength="100" value="${escape(metadata[key])}" /></label>`).join("")}</div><label>适用范围<select name="audience"><option value="student">学生课堂资料</option><option value="teacher">仅教师参考（不进入学生问答）</option></select></label><p class="hint">教师参考答案建议单独成文件后设为“仅教师参考”。</p><button class="button primary" type="submit">保存资料属性</button><p role="status" data-save-status></p></form>`;
    const form = content.querySelector("form");
    form.elements.audience.value = metadata.audience || "student";
    form.onsubmit = async (event) => {
      event.preventDefault();
      const button = form.querySelector("button"); button.disabled = true;
      try {
        await host.request(`/knowledge/files/${encodeURIComponent(id)}/metadata`, {method:"PATCH", body:JSON.stringify(Object.fromEntries(new FormData(form)))});
        await host.refresh(); element.close(); host.toast("资料属性已保存。");
      } catch (error) { form.querySelector("[data-save-status]").textContent = error.message; }
      finally { button.disabled = false; }
    };
  }

  byId("knowledge-tools").addEventListener("input", (event) => {
    if (event.target.id.startsWith("knowledge-scope-") || event.target.closest("#knowledge-scope-files")) {
      dirty = true; byId("knowledge-scope-note").textContent = "范围有修改，应用后才会影响学生问答。";
    }
  });
  byId("knowledge-save-scope").onclick = async () => {
    const button = byId("knowledge-save-scope"); button.disabled = true;
    try {
      if (!sourceId) throw new Error("请先在控制页选择课堂知识库。");
      await host.request("/config/scenarios/default", {method:"PUT", body:JSON.stringify({knowledge_source_id:sourceId, knowledge_scope:readScope()})});
      dirty = false; await host.refresh(); host.toast("已应用课堂资料范围。");
    } catch (error) { host.toast(error.message, true); }
    finally { button.disabled = false; }
  };
  byId("knowledge-scope-source").onchange = () => {
    sourceId = byId("knowledge-scope-source").value;
    fields.forEach((field) => { byId(`knowledge-scope-${field}`).value = ""; });
    byId("knowledge-scope-files").querySelectorAll("input").forEach((input) => { input.checked = false; });
    dirty = true; filesStamp = ""; renderScope();
  };
  byId("knowledge-policy").addEventListener("input", () => {
    policyDirty = true;
    byId("knowledge-policy-status").textContent = "规则有修改，保存后对新提问生效。";
  });
  byId("knowledge-policy-reload").onclick = () => loadPolicy(true);
  byId("knowledge-policy-save").onclick = async () => {
    const button = byId("knowledge-policy-save"); button.disabled = true;
    try {
      const topics = byId("knowledge-blacklist-topics").value.split(/\r?\n/).map((line) => line.trim()).filter(Boolean).map((line) => {
        const [label, ...aliases] = line.split("|").map((value) => value.trim()).filter(Boolean);
        return {label, aliases};
      });
      await host.request("/knowledge/policy", {method:"PUT",body:JSON.stringify({enabled:byId("knowledge-blacklist-enabled").checked,topics,message:byId("knowledge-blacklist-message").value.trim()})});
      policyDirty = false; await loadPolicy(true); host.toast("课堂黑名单已保存。");
    } catch (error) { byId("knowledge-policy-status").textContent = error.message; }
    finally { button.disabled = false; }
  };
  byId("knowledge-prepare").onclick = () => startJob("prepare");
  byId("knowledge-reindex").onclick = () => startJob("reindex");
  byId("knowledge-refresh").onclick = refreshStatus;
  byId("knowledge-search").onclick = search;
  byId("knowledge-question").addEventListener("keydown", (event) => { if (event.key === "Enter") search(); });
  byId("file-list").addEventListener("click", (event) => {
    const previewButton = event.target.closest("[data-knowledge-preview]");
    const editButton = event.target.closest("[data-knowledge-metadata]");
    if (previewButton) preview(previewButton.dataset.knowledgePreview);
    if (editButton) editMetadata(editButton.dataset.knowledgeMetadata);
  });
  window.addEventListener("edugate:knowledge-data", renderScope);
  window.addEventListener("edugate:resource-panel-change", () => refreshStatus());
  window.addEventListener("edugate:tab-change", (event) => { if (event.detail === "settings") refreshStatus(); });
  setInterval(() => { if (!document.hidden && !byId("knowledge-tools").closest("[hidden]")) refreshStatus(); }, 2000);
  renderScope(); refreshStatus();
})();
