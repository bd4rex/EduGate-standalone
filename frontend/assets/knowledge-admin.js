(() => {
  "use strict";
  const host = window.EduGateKnowledgeAdmin;
  const byId = (id) => document.getElementById(id);
  const fields = ["subject", "grade", "edition", "term", "chapter"];
  const escape = (value) => String(value ?? "").replace(/[&<>"']/g, (char) => ({"&":"&amp;", "<":"&lt;", ">":"&gt;", '"':"&quot;", "'":"&#39;"}[char]));
  let sourceId = "", appliedSource = null, filesStamp = "", dirty = false, polling = false, lastJob = "";
  let policyLoaded = false, policyDirty = false, savedPolicy = null, policyVersion = 0, policyLoad = 0;
  let scopeSaving = false, policySaving = false, searching = false, searchVersion = 0, resultContext = "";
  let scopeError = "", policyError = "";
  let trialHistory = [], busyKind = "";
  const policyFields = ["knowledge-blacklist-enabled", "knowledge-blacklist-topics", "knowledge-blacklist-message"];

  function normalizedScope(scope = {}) {
    return {file_ids: [...(scope.file_ids || [])].sort(), ...Object.fromEntries(fields.map((key) => [key, scope[key] || ""]))};
  }

  function currentContext() {
    const scenario = host.scenario();
    return JSON.stringify([scenario, savedPolicy]);
  }

  function clearTrial() {
    searchVersion += 1;
    resultContext = "";
    trialHistory = [];
    byId("knowledge-search-results").innerHTML = '<div class="trial-empty"><span class="ui-icon icon-message" aria-hidden="true"></span><strong>试问一个课堂问题，查看学生会收到的回答</strong><p>回答、检查结果和引用依据会显示在这里。</p></div>';
    byId("knowledge-evidence-results").textContent = "";
    byId("knowledge-trial-status").textContent = "";
    byId("knowledge-trial-round").textContent = "新一轮试问";
  }

  function renderTrialContext() {
    if (resultContext && resultContext !== currentContext()) clearTrial();
    const scenario = host.scenario(), scope = scenario?.knowledge_scope || {}, source = host.sources().find((item) => item.id === scenario?.knowledge_source_id);
    byId("knowledge-trial-source").textContent = source?.name || "未挂载课堂资料";
    const labels = {subject:"学科", grade:"年级", edition:"版本", term:"学期", chapter:"章节"};
    const filters = fields.filter((key) => scope[key]).map((key) => `${labels[key]}：${scope[key]}`);
    byId("knowledge-trial-scope").textContent = source ? [scope.file_ids?.length ? `已选 ${scope.file_ids.length} 个文件` : "不限文件", ...filters].join(" · ") : scenario?.knowledge_strict ? "严格依据模式下将提示资料不足" : "当前允许使用通用知识";
    byId("knowledge-trial-policy").textContent = !policyLoaded ? "等待规则读取" : savedPolicy?.enabled ? (savedPolicy.topics.length ? `已启用 ${savedPolicy.topics.length} 个禁答话题` : "已开启，尚无禁答话题") : "未启用禁答话题";
    const strategyDirty = Boolean(host.strategyDirty?.());
    const pending = [dirty ? "课堂范围" : "", policyDirty ? "黑名单" : "", strategyDirty ? "教学策略" : ""].filter(Boolean);
    const notice = byId("knowledge-trial-notice");
    notice.hidden = !pending.length && policyLoaded;
    const steps = [dirty ? "到“课堂范围”应用设置" : "", policyDirty ? "保存左侧黑名单" : "", strategyDirty ? "到“控制”保存教学策略" : ""].filter(Boolean);
    notice.textContent = pending.length ? `${pending.join("和")}有未保存的修改。请先${steps.join("，")}，再试问。` : "请先读取黑名单规则后再试问。";
    const unavailable = searching || scopeSaving || policySaving || !host.authenticated() || !policyLoaded || Boolean(pending.length);
    byId("knowledge-search").disabled = unavailable || trialHistory.length >= 20;
    byId("knowledge-search-evidence").disabled = unavailable || !source;
    byId("knowledge-search").textContent = searching && busyKind === "answer" ? "正在生成并检查…" : trialHistory.length ? "继续追问" : "生成试问回答";
    byId("knowledge-search").setAttribute("aria-busy", String(searching));
    byId("knowledge-trial-reset").disabled = searching;
    byId("knowledge-question").disabled = searching;
    byId("knowledge-trial-round").textContent = trialHistory.length ? `已完成 ${trialHistory.length / 2} 轮 / 最多 10 轮` : "新一轮试问";
    byId("knowledge-trial-model").textContent = `当前模型：${scenario?.model ? (host.modelName?.(scenario.model) || scenario.model) : "未配置"} · ${scenario?.knowledge_strict ? "仅依据课堂资料" : "可使用通用知识"} · 使用已保存的教学策略`;
  }

  function renderScopeState() {
    const state = byId("knowledge-scope-state");
    const applied = Boolean(host.scenario()?.knowledge_source_id);
    state.textContent = scopeSaving ? "正在应用…" : dirty || !applied ? "未应用" : "已应用";
    state.dataset.dirty = String(dirty);
    byId("knowledge-scope-note").textContent = scopeError || (!applied ? "请先应用资料范围，学生问答和统一试问才会使用所选知识库。" : dirty ? "范围有修改，应用后才会影响学生问答和统一试问。" : "范围设置保存后生效；当前显示已应用的范围。");
    byId("knowledge-scope-note").classList.toggle("knowledge-warning", Boolean(scopeError));
    [byId("knowledge-scope-source"), byId("knowledge-save-scope"), byId("knowledge-clear-files"), ...fields.map((field) => byId(`knowledge-scope-${field}`)), ...byId("knowledge-scope-files").querySelectorAll("input")].forEach((input) => { input.disabled = scopeSaving; });
    const selected = readScope().file_ids.length;
    const count = byId("knowledge-scope-files").querySelectorAll("input").length;
    byId("knowledge-file-selection").textContent = !count ? "请先在“资料管理”上传本课资料。" : selected ? `已选 ${selected} / ${count} 个文件，仍需符合右侧筛选。` : `不限文件 · 本库共 ${count} 个学生资料文件`;
    renderTrialContext();
  }

  function renderPolicyState() {
    const note = byId("knowledge-policy-state");
    note.textContent = policySaving ? "保存中…" : policyDirty ? "未保存" : policyLoaded ? "已保存" : "读取中";
    note.dataset.dirty = String(policyDirty);
    byId("knowledge-policy-reload").textContent = policyDirty ? "撤销修改" : "重新读取";
    byId("knowledge-policy-save").disabled = policySaving || !policyLoaded;
    if (policyError) byId("knowledge-policy-status").textContent = policyError;
    else if (policyDirty) byId("knowledge-policy-status").textContent = "规则有修改，保存后对新提问和统一试问生效。";
    byId("knowledge-policy-status").classList.toggle("knowledge-warning", Boolean(policyError));
    renderTrialContext();
  }

  async function loadPolicy(force = false) {
    if (!host.authenticated() || (policyDirty && !force)) return;
    const version = policyVersion, requestId = ++policyLoad;
    try {
      const policy = await host.request("/knowledge/policy");
      if (requestId !== policyLoad) return;
      savedPolicy = policy;
      policyLoaded = true;
      policyError = "";
      if (version !== policyVersion) { renderPolicyState(); return; }
      byId("knowledge-blacklist-enabled").checked = policy.enabled;
      byId("knowledge-blacklist-topics").value = policy.topics.map((topic) => [topic.label, ...topic.aliases].join("|")).join("\n");
      byId("knowledge-blacklist-message").value = policy.message;
      for (const id of policyFields) byId(id).disabled = policySaving;
      byId("knowledge-policy-status").textContent = policy.enabled && policy.topics.length ? `已启用 ${policy.topics.length} 个禁答话题。新提问会按这些规则检查。` : "当前没有启用的禁答话题。";
      policyDirty = false;
      renderPolicyState();
    } catch (error) {
      if (requestId === policyLoad) {
        policyLoaded = false;
        policyError = `规则读取失败：${error.message}`;
        renderPolicyState();
      }
      throw error;
    }
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
    renderScopeState();
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
      byId("knowledge-job").textContent = [job.status !== "completed" ? job.message : "", ...(job.errors || []), status.model_error].filter(Boolean).join("；");
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

  async function searchEvidence() {
    if (searching || dirty || policyDirty || host.strategyDirty?.() || scopeSaving || policySaving || !policyLoaded) { renderTrialContext(); return; }
    const query = byId("knowledge-question").value.trim();
    if (!query) { host.toast("请输入一个学生可能提出的问题。", true); byId("knowledge-question").focus(); return; }
    searching = true;
    busyKind = "evidence";
    const version = ++searchVersion;
    const results = byId("knowledge-evidence-results");
    results.setAttribute("aria-busy", "true");
    results.textContent = "正在读取已保存设置、检查黑名单并查找资料…";
    renderTrialContext();
    try {
      // Refresh saved settings before testing; unsaved forms never change the trial.
      await host.refresh();
      await loadPolicy();
      if (version !== searchVersion || dirty || policyDirty) return;
      const scenario = host.scenario();
      if (!scenario?.knowledge_source_id) throw new Error("请先选择课堂知识库并应用设置。");
      const context = currentContext();
      const policyActive = savedPolicy.enabled && savedPolicy.topics.length > 0;
      const result = await host.request("/knowledge/search", {method:"POST", body:JSON.stringify({source_id:scenario.knowledge_source_id, query, scope:scenario.knowledge_scope || {}, mode:byId("knowledge-search-mode").value})});
      if (version !== searchVersion || context !== currentContext()) return;
      resultContext = context;
      const stage = (label, title, outcome) => `<div class="trial-stage" data-outcome="${outcome}"><span>${label}</span><strong>${escape(title)}</strong></div>`;
      if (result.blocked) {
        const blocked = result.policy_decision === "blocked";
        results.innerHTML = `<div class="trial-stage-row">${stage("1 · 黑名单检查", blocked ? `命中${result.blocked_topic ? `：${result.blocked_topic}` : "禁答话题"}` : "检查暂不可用", blocked ? "blocked" : "warning")}${stage("2 · 课堂范围检索", "未执行", "idle")}</div><p class="trial-result-note">${escape(result.block_reason)}</p><p class="trial-result-note">${blocked ? "此问题会被课堂黑名单拦截，不继续检索资料。" : "本次无法完成话题检查，未继续检索；请检查模型服务后重试。"}</p>`;
        return;
      }
      results.innerHTML = `<div class="trial-stage-row">${stage("1 · 黑名单检查", policyActive ? "已通过" : "未启用禁答话题", policyActive ? "pass" : "idle")}${stage("2 · 课堂范围检索", result.hits.length ? `找到 ${result.hits.length} 个候选片段` : "未找到资料依据", result.hits.length ? "pass" : "warning")}</div><p class="hint">${result.mode === "hybrid" ? "混合检索" : result.mode === "vector" ? "语义检索" : "关键词检索"} · ${escape(result.elapsed_ms)} ms · 仅展示资料依据</p><div class="trial-hit-list"></div>`;
      if (!result.hits.length) results.insertAdjacentHTML("beforeend", "<p class=\"trial-result-note\">当前范围内未找到资料依据，请检查文件、章节和版本筛选。这不等于问题被黑名单拦截。</p>");
      result.hits.forEach((hit, index) => {
        const card = document.createElement("details");
        card.className = "knowledge-result";
        card.open = index === 0;
        card.innerHTML = `<summary><strong>[K${index+1}] ${escape(hit.filename)}</strong></summary><div class="hint">${escape(hit.chapter || "正文")}${hit.page ? ` · 第 ${hit.page} 页/张` : ""}${hit.semantic_score != null ? ` · 语义相似度 ${hit.semantic_score.toFixed(3)}` : ""}</div><pre>${escape(hit.context_text || hit.text)}</pre>`;
        results.querySelector(".trial-hit-list").append(card);
      });
    } catch (error) { if (version === searchVersion) results.textContent = `试问未完成：${error.message}`; }
    finally { searching = false; busyKind = ""; results.setAttribute("aria-busy", "false"); renderTrialContext(); }
  }

  function appendTrialAnswer(query, response) {
    const results = byId("knowledge-search-results"), info = response.edugate_trial;
    const knowledge = response.edugate_knowledge || {}, sources = knowledge.sources || [];
    const hitCount = knowledge.retrieval?.hit_count ?? sources.length;
    const stage = (label, title, outcome) => `<div class="trial-stage" data-outcome="${outcome}"><span>${label}</span><strong>${escape(title)}</strong></div>`;
    const policy = info.question_policy;
    const questionLabel = policy === "blocked" ? `命中：${info.question_topic || "禁答话题"}` : policy === "unavailable" ? "检查暂不可用" : policy === "allowed" ? "已通过" : "未启用禁答话题";
    const decisions = {supported:"资料依据核对通过", assisted: hitCount ? `检索到 ${hitCount} 个资料片段` : "无资料命中，允许通用知识", no_evidence:"未找到资料依据", insufficient_evidence:"资料不足以回答", evidence_check_unavailable:"依据核对暂不可用", local_reply:"课堂范围说明", blocked_topic:"未继续检索或引用", policy_check_unavailable:"未执行"};
    const answerLabel = !info.generation_requested ? "课堂提示 / 拒答" : info.answer_policy === "blocked" ? "回答已被拦截" : info.answer_policy === "unavailable" ? "回答检查未通过" : info.answer_policy_checked ? "回答与引用已通过" : "回答已生成";
    const article = document.createElement("details");
    article.className = "trial-conversation-turn"; article.open = true;
    article.innerHTML = `<summary>第 ${trialHistory.length / 2 + 1} 轮 · ${escape(query)}</summary><div class="trial-stage-row trial-answer-stages">${stage("1 · 提问黑名单",questionLabel,policy === "allowed" ? "pass" : ["blocked","unavailable"].includes(policy) ? "warning" : "idle")}${stage("2 · 课堂资料依据",decisions[info.retrieval_decision] || "未检索",["supported","assisted"].includes(info.retrieval_decision) ? "pass" : "warning")}${stage("3 · 最终回答",answerLabel,info.generation_requested && !["blocked","unavailable"].includes(info.answer_policy) ? "pass" : "warning")}</div><h3>学生会收到的回答</h3><div class="trial-answer-text">${escape(info.answer)}</div><p class="trial-result-note">${escape(host.modelName?.(info.model) || info.model)} · ${escape(info.elapsed_ms)} ms · 教师试问，不计入学生记录</p><div class="trial-answer-sources"></div>`;
    for (const source of sources) {
      const detail = document.createElement("details"); detail.className = "knowledge-result";
      detail.innerHTML = `<summary><strong>[${escape(source.id)}] ${escape(source.filename)}</strong></summary><p class="hint">${escape(source.chapter || "正文")}${source.page ? ` · 第 ${source.page} 页/张` : ""}</p><pre>${escape(source.text)}</pre>`;
      article.querySelector(".trial-answer-sources").append(detail);
    }
    if (!trialHistory.length) results.replaceChildren();
    results.querySelectorAll(".trial-conversation-turn").forEach((turn) => { turn.open = false; });
    results.append(article);
  }

  async function generateTrial() {
    if (searching || dirty || policyDirty || host.strategyDirty?.() || scopeSaving || policySaving || !policyLoaded || trialHistory.length >= 20) { renderTrialContext(); return; }
    const query = byId("knowledge-question").value.trim();
    if (!query) { host.toast("请输入一个学生可能提出的问题。", true); byId("knowledge-question").focus(); return; }
    searching = true; busyKind = "answer";
    const version = ++searchVersion, status = byId("knowledge-trial-status");
    status.classList.remove("knowledge-warning");
    status.textContent = "正在检查课堂规则并生成回答；完整检查后显示结果…";
    byId("knowledge-search-results").setAttribute("aria-busy", "true");
    renderTrialContext();
    try {
      await host.refresh(); await loadPolicy();
      if (version !== searchVersion || dirty || policyDirty || host.strategyDirty?.()) return;
      const context = currentContext();
      const response = await host.request("/admin/classroom/trial", {method:"POST", body:JSON.stringify({messages:[...trialHistory,{role:"user",content:query}]})});
      if (version !== searchVersion || context !== currentContext()) return;
      if (!response.edugate_trial?.answer?.trim()) throw new Error("模型未返回可显示的文本回答，请检查模型服务后重试。");
      appendTrialAnswer(query, response);
      trialHistory.push({role:"user",content:query},{role:"assistant",content:response.edugate_trial.answer});
      resultContext = context;
      byId("knowledge-question").value = "";
      byId("knowledge-question").placeholder = "可以继续追问；测试另一个问题时可点击“新一轮试问”。";
      status.textContent = trialHistory.length >= 20 ? "本轮已完成 10 次提问，请点击“新一轮试问”继续。" : ["blocked_topic","policy_check_unavailable","no_evidence","insufficient_evidence","evidence_check_unavailable"].includes(response.edugate_trial.final_decision) ? "本次试问返回课堂提示或拒答，可核对规则后开始新一轮。" : "回答已完成，可以继续追问或开始新一轮试问。";
    } catch (error) {
      if (version === searchVersion) {
        status.classList.add("knowledge-warning");
        status.textContent = error.message.includes("AI service is disabled") ? "课堂 AI 当前已关闭，请到“控制”开启后再试问。" : `试问未完成：${error.message}`;
      }
    } finally {
      searching = false; busyKind = "";
      byId("knowledge-search-results").setAttribute("aria-busy", "false");
      renderTrialContext();
    }
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

  function markScopeDraft() {
    scopeError = "";
    const scenario = host.scenario();
    dirty = sourceId !== scenario?.knowledge_source_id || JSON.stringify(normalizedScope(readScope())) !== JSON.stringify(normalizedScope(scenario?.knowledge_scope));
    clearTrial();
    renderScopeState();
  }
  byId("knowledge-tools").addEventListener("input", (event) => {
    if (event.target.id.startsWith("knowledge-scope-") || event.target.closest("#knowledge-scope-files")) {
      if (event.target.id !== "knowledge-scope-source") markScopeDraft();
    }
  });
  byId("knowledge-save-scope").onclick = async () => {
    if (scopeSaving) return;
    scopeSaving = true;
    scopeError = "";
    let saved = false;
    const inputs = [byId("knowledge-scope-source"), byId("knowledge-save-scope"), byId("knowledge-clear-files"), ...fields.map((field) => byId(`knowledge-scope-${field}`)), ...byId("knowledge-scope-files").querySelectorAll("input")];
    inputs.forEach((input) => { input.disabled = true; });
    renderScopeState();
    try {
      if (!sourceId) throw new Error("请先在控制页选择课堂知识库。");
      await host.request("/config/scenarios/default", {method:"PUT", body:JSON.stringify({knowledge_source_id:sourceId, knowledge_scope:readScope()})});
      saved = true;
      dirty = false; await host.refresh(); clearTrial(); renderScope(); host.toast("已应用课堂资料范围，可以前往统一试问。");
    } catch (error) {
      if (saved) dirty = true;
      scopeError = saved ? `范围已保存，但页面刷新失败，请重新应用以同步状态：${error.message}` : error.message;
      host.toast(scopeError, true);
    }
    finally { scopeSaving = false; inputs.forEach((input) => { input.disabled = false; }); renderScopeState(); }
  };
  byId("knowledge-scope-source").onchange = () => {
    scopeError = "";
    sourceId = byId("knowledge-scope-source").value;
    fields.forEach((field) => { byId(`knowledge-scope-${field}`).value = ""; });
    byId("knowledge-scope-files").querySelectorAll("input").forEach((input) => { input.checked = false; });
    dirty = true; filesStamp = ""; clearTrial(); renderScope();
  };
  byId("knowledge-clear-files").onclick = () => {
    byId("knowledge-scope-files").querySelectorAll("input").forEach((input) => { input.checked = false; });
    markScopeDraft();
  };
  byId("knowledge-open-trial").onclick = () => {
    byId("settings-view-policy-tab").click();
    byId("knowledge-question").focus();
  };
  byId("knowledge-edit-scope").onclick = () => {
    byId("settings-view-scope-tab").click();
    byId("settings-view-scope-tab").focus();
  };
  byId("knowledge-policy").addEventListener("input", (event) => {
    if (!policyFields.includes(event.target.id)) return;
    policyVersion += 1;
    policyError = "";
    policyDirty = true;
    clearTrial(); renderPolicyState();
  });
  byId("knowledge-policy-reload").onclick = () => loadPolicy(true).catch((error) => host.toast(error.message, true));
  byId("knowledge-policy-save").onclick = async () => {
    if (policySaving) return;
    if (!byId("knowledge-blacklist-message").value.trim()) {
      policyError = "请填写拦截后给学生的提示。";
      renderPolicyState();
      byId("knowledge-blacklist-message").focus();
      return;
    }
    policySaving = true;
    policyError = "";
    policyFields.forEach((id) => { byId(id).disabled = true; });
    byId("knowledge-policy-reload").disabled = true;
    renderPolicyState();
    try {
      const topics = byId("knowledge-blacklist-topics").value.split(/\r?\n/).map((line) => line.trim()).filter(Boolean).map((line) => {
        const [label, ...aliases] = line.split("|").map((value) => value.trim()).filter(Boolean);
        return {label, aliases};
      });
      const policy = await host.request("/knowledge/policy", {method:"PUT",body:JSON.stringify({enabled:byId("knowledge-blacklist-enabled").checked,topics,message:byId("knowledge-blacklist-message").value.trim()})});
      policyLoad += 1;
      savedPolicy = policy; policyLoaded = true; policyDirty = false;
      byId("knowledge-policy-status").textContent = policy.enabled && policy.topics.length ? `已启用 ${policy.topics.length} 个禁答话题。新提问会按这些规则检查。` : "当前没有启用的禁答话题。";
      clearTrial(); host.toast("课堂黑名单已保存，可以在右侧试问。");
    } catch (error) { policyError = error.message; }
    finally {
      policySaving = false;
      policyFields.forEach((id) => { byId(id).disabled = !policyLoaded; });
      byId("knowledge-policy-reload").disabled = false;
      renderPolicyState();
    }
  };
  byId("knowledge-prepare").onclick = () => startJob("prepare");
  byId("knowledge-reindex").onclick = () => startJob("reindex");
  byId("knowledge-refresh").onclick = refreshStatus;
  byId("knowledge-search").onclick = generateTrial;
  byId("knowledge-search-evidence").onclick = searchEvidence;
  byId("knowledge-trial-reset").onclick = () => {
    clearTrial(); byId("knowledge-question").value = "";
    byId("knowledge-question").placeholder = "例如：为什么教室里的电脑有不同的 IP 地址？";
    renderTrialContext(); byId("knowledge-question").focus();
  };
  byId("knowledge-question").addEventListener("input", () => { byId("knowledge-evidence-results").textContent = ""; });
  byId("knowledge-search-mode").addEventListener("change", () => { byId("knowledge-evidence-results").textContent = ""; });
  byId("knowledge-question").addEventListener("keydown", (event) => {
    if (event.key === "Enter" && (event.ctrlKey || event.metaKey) && !event.isComposing) { event.preventDefault(); generateTrial(); }
  });
  byId("file-list").addEventListener("click", (event) => {
    const previewButton = event.target.closest("[data-knowledge-preview]");
    const editButton = event.target.closest("[data-knowledge-metadata]");
    if (previewButton) preview(previewButton.dataset.knowledgePreview);
    if (editButton) editMetadata(editButton.dataset.knowledgeMetadata);
  });
  window.addEventListener("edugate:knowledge-data", renderScope);
  window.addEventListener("edugate:strategy-draft-change", renderTrialContext);
  window.addEventListener("edugate:resource-panel-change", () => refreshStatus());
  window.addEventListener("edugate:tab-change", (event) => { if (event.detail === "settings") refreshStatus(); });
  window.addEventListener("beforeunload", (event) => {
    if (dirty || policyDirty) { event.preventDefault(); event.returnValue = ""; }
  });
  setInterval(() => { if (!document.hidden && !byId("knowledge-tools").closest("[hidden]")) refreshStatus(); }, 2000);
  renderScope(); refreshStatus();
})();
