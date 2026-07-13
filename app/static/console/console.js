(function () {
  const bodyEl = document.body;
  const themeBtns = document.querySelectorAll(".theme-btn");

  function applyTheme(theme) {
    const t = ["linear", "glass", "neo"].includes(theme) ? theme : "neo";
    bodyEl.classList.remove("theme-linear", "theme-glass", "theme-neo");
    bodyEl.classList.add(`theme-${t}`);
    localStorage.setItem("kb_console_theme", t);
    themeBtns.forEach((b) => b.classList.toggle("active", b.dataset.theme === t));
  }

  applyTheme(localStorage.getItem("kb_console_theme") || "neo");
  themeBtns.forEach((btn) => {
    btn.addEventListener("click", () => applyTheme(btn.dataset.theme || "neo"));
  });

  const state = {
    results: [],
    selectedId: null,
    lastSearchActor: "manual",
  };

  let configCache = null;

  const tabButtons = document.querySelectorAll("[data-tab]");
  const searchForm = document.getElementById("search-form");
  const upsertForm = document.getElementById("upsert-form");
  const resultList = document.getElementById("result-list");
  const detailPanel = document.getElementById("detail-panel");
  const resultMeta = document.getElementById("result-meta");
  const loadEditorBtn = document.getElementById("load-editor-btn");
  const deleteItemBtn = document.getElementById("delete-item-btn");
  const upsertAlert = document.getElementById("upsert-alert");
  const healthText = document.getElementById("health-text");
  const healthDot = document.querySelector(".status-dot");
  const searchHint = document.getElementById("search-hint");

  const toggleFiltersBtn = document.getElementById("toggle-filters");
  const advancedFiltersPanel = document.getElementById("advanced-filters");
  if (toggleFiltersBtn && advancedFiltersPanel) {
    toggleFiltersBtn.addEventListener("click", () => {
      advancedFiltersPanel.classList.toggle("show");
    });
  }

  tabButtons.forEach((btn) => {
    btn.addEventListener("click", () => {
      tabButtons.forEach((b) => b.classList.remove("active"));
      btn.classList.add("active");
      document.querySelectorAll(".tab-content").forEach((section) => {
        section.classList.toggle("active", section.id === btn.dataset.tab);
      });
    });
  });

  function el(id) {
    return document.getElementById(id);
  }

  function toCsvArray(value) {
    if (!value) return [];
    return String(value)
      .split(",")
      .map((s) => s.trim())
      .filter(Boolean);
  }

  function escapeHtml(raw) {
    return String(raw)
      .replaceAll("&", "&amp;")
      .replaceAll("<", "&lt;")
      .replaceAll(">", "&gt;")
      .replaceAll('"', "&quot;")
      .replaceAll("'", "&#39;");
  }

  function cleanSnippet(raw) {
    if (!raw) return "";
    let text = String(raw);
    text = text.replace(/```[\s\S]*?```/g, (m) => m.replace(/```\w*\n?|```/g, ""));
    text = text.replace(/^#{1,6}\s+/gm, "");
    text = text.replace(/\*\*(.+?)\*\*/g, "$1");
    text = text.replace(/(^|[^*])\*(?!\*)([^*\n]+)\*/g, "$1$2");
    text = text.replace(/`([^`\n]+)`/g, "$1");
    text = text.replace(/^>\s?/gm, "");
    text = text.replace(/\s+/g, " ").trim();
    return text;
  }

  function groupChunksByItem(chunks) {
    const map = new Map();
    chunks.forEach((c, i) => {
      const itemId = String(c.knowledge_item_id || "");
      if (!map.has(itemId)) {
        map.set(itemId, {
          itemId,
          title: c.title || "",
          version: c.version,
          refNos: [],
          snippets: [],
        });
      }
      const g = map.get(itemId);
      g.refNos.push(i + 1);
      const cleaned = cleanSnippet(c.snippet);
      if (cleaned && !g.snippets.includes(cleaned)) {
        g.snippets.push(cleaned);
      }
    });
    return Array.from(map.values());
  }

  function showAlert(type, message) {
    if (!upsertAlert) return;
    upsertAlert.className = `alert alert-${type}`;
    upsertAlert.classList.remove("d-none");
    upsertAlert.textContent = message;
  }

  function clearAlert() {
    if (!upsertAlert) return;
    upsertAlert.className = "alert mt-3 mb-0 d-none";
    upsertAlert.textContent = "";
  }

  function setSearchHint(payload) {
    if (!searchHint) return;
    const active = [];
    ["project", "module", "feature", "source_uri"].forEach((k) => {
      if (payload[k]) active.push(`${k}=${payload[k]}`);
    });
    if ((payload.tags || []).length) active.push(`tags=${payload.tags.join(",")}`);
    searchHint.textContent = `实际过滤: ${active.length ? active.join(" | ") : "无"}`;
  }

  async function apiFetch(path, options = {}) {
    const response = await fetch(path, {
      headers: {
        "Content-Type": "application/json",
        ...(options.headers || {}),
      },
      ...options,
    });

    if (!response.ok) {
      let message = `${response.status} ${response.statusText}`;
      try {
        const body = await response.json();
        if (body && body.detail) {
          message = typeof body.detail === "string" ? body.detail : JSON.stringify(body.detail);
        }
      } catch {
        // ignore
      }
      throw new Error(message);
    }

    const contentType = response.headers.get("content-type") || "";
    if (contentType.includes("application/json")) {
      return response.json();
    }
    return response.text();
  }

  async function refreshHealth() {
    if (!healthText || !healthDot) return;
    try {
      const data = await apiFetch("/health", { method: "GET", headers: {} });
      healthText.textContent = data.status === "ok" ? "运行中" : String(data.status || "未知");
      healthDot.classList.remove("err");
      healthDot.classList.add("ok");
    } catch (err) {
      healthText.textContent = `异常: ${err.message}`;
      healthDot.classList.remove("ok");
      healthDot.classList.add("err");
    }
  }

  async function loadAppVersion() {
    const badge = document.getElementById("app-version-badge");
    if (!badge) return;
    try {
      const data = await apiFetch("/v1/system/version", { method: "GET", headers: {} });
      const v = (data && data.version) ? String(data.version) : "dev";
      badge.textContent = "v" + v;
      badge.title = "产品版本号";
    } catch (err) {
      badge.textContent = "v?";
      badge.title = "版本号读取失败: " + err.message;
    }
  }

  function renderResults() {
    if (!resultList) return;
    if (!state.results.length) {
      resultList.innerHTML = '<div class="empty-note">未命中，请调整条件后重试。</div>';
      return;
    }

    resultList.innerHTML = state.results
      .map((item) => {
        const active = item.knowledge_item_id === state.selectedId ? "active" : "";
        return `
          <div class="result-item ${active}" data-item-id="${escapeHtml(item.knowledge_item_id)}">
            <div class="result-title">${escapeHtml(item.title)}</div>
            <div class="result-meta">ID: ${escapeHtml(item.knowledge_item_id)} | v${item.version} | score ${(item.score || 0).toFixed(4)}</div>
            <div class="result-snippet">${escapeHtml(item.snippet || "")}</div>
          </div>
        `;
      })
      .join("");

    Array.from(resultList.querySelectorAll(".result-item")).forEach((itemEl) => {
      itemEl.addEventListener("click", () => {
        const itemId = itemEl.getAttribute("data-item-id");
        if (!itemId) return;
        state.selectedId = itemId;
        renderResults();
        loadItemDetail(itemId, state.lastSearchActor);
      });
    });
  }

  function renderItemDetail(item) {
    if (!detailPanel) return;
    const sources = (item.sources || []).map((s) => `${escapeHtml(s.type)}: ${escapeHtml(s.uri)}`).join("<br>") || "-";
    const markdown = item.content_markdown || "";
    const html = window.marked ? window.marked.parse(markdown) : `<pre>${escapeHtml(markdown)}</pre>`;

    detailPanel.innerHTML = `
      <div class="detail-kv"><span class="key">knowledge_item_id:</span>${escapeHtml(item.knowledge_item_id)}</div>
      <div class="detail-kv"><span class="key">title:</span>${escapeHtml(item.title)}</div>
      <div class="detail-kv"><span class="key">domain/project:</span>${escapeHtml(item.domain)} / ${escapeHtml(item.project)}</div>
      <div class="detail-kv"><span class="key">module/feature:</span>${escapeHtml(item.module || "-")} / ${escapeHtml(item.feature || "-")}</div>
      <div class="detail-kv"><span class="key">type/version/status:</span>${escapeHtml(item.type)} / v${item.version} / ${escapeHtml(item.status)}</div>
      <div class="detail-kv"><span class="key">source_uri:</span>${escapeHtml(item.source_uri || "-")}</div>
      <div class="detail-kv"><span class="key">updated_at:</span>${escapeHtml(item.updated_at)}</div>
      <div class="detail-kv"><span class="key">sources:</span><div>${sources}</div></div>
      <div class="detail-markdown">${html}</div>
    `;
  }

  async function loadItemDetail(itemId, actor) {
    try {
      if (loadEditorBtn) loadEditorBtn.disabled = true;
      if (deleteItemBtn) deleteItemBtn.disabled = true;
      const item = await apiFetch(`/v1/knowledge/items/${encodeURIComponent(itemId)}?actor=${encodeURIComponent(actor)}`, {
        method: "GET",
        headers: {},
      });
      renderItemDetail(item);
      if (loadEditorBtn) {
        loadEditorBtn.disabled = false;
        loadEditorBtn.onclick = () => loadItemToEditor(item);
      }
      if (deleteItemBtn) {
        deleteItemBtn.disabled = false;
        deleteItemBtn.onclick = () => deleteSelectedItem(item);
      }
    } catch (err) {
      if (detailPanel) detailPanel.innerHTML = `<div class="empty-note">读取详情失败: ${escapeHtml(err.message)}</div>`;
      if (loadEditorBtn) loadEditorBtn.disabled = true;
      if (deleteItemBtn) deleteItemBtn.disabled = true;
    }
  }

  async function deleteSelectedItem(item) {
    const itemId = item && item.knowledge_item_id;
    if (!itemId) return;
    const title = item.title || itemId;
    if (!window.confirm(`确认删除「${title}」？删除后将不再出现在检索和详情中。`)) {
      return;
    }

    if (deleteItemBtn) deleteItemBtn.disabled = true;
    try {
      const actor = state.lastSearchActor || "console";
      await apiFetch(`/v1/console/knowledge/items/${encodeURIComponent(itemId)}?actor=${encodeURIComponent(actor)}`, {
        method: "DELETE",
        headers: {},
      });
      state.results = state.results.filter((r) => r.knowledge_item_id !== itemId);
      state.selectedId = state.results[0]?.knowledge_item_id || null;
      renderResults();
      if (resultMeta) resultMeta.textContent = state.results.length ? `${state.results.length} 条 | 已删除 ${itemId}` : `已删除 ${itemId}`;
      if (state.selectedId) {
        await loadItemDetail(state.selectedId, state.lastSearchActor);
      } else {
        if (detailPanel) detailPanel.innerHTML = '<div class="empty-note">已删除当前知识条目。</div>';
        if (loadEditorBtn) loadEditorBtn.disabled = true;
        if (deleteItemBtn) deleteItemBtn.disabled = true;
      }
    } catch (err) {
      if (detailPanel) detailPanel.innerHTML = `<div class="empty-note">删除失败: ${escapeHtml(err.message)}</div>`;
      if (deleteItemBtn) deleteItemBtn.disabled = false;
    }
  }

  function loadItemToEditor(item) {
    if (!upsertForm) return;
    const map = {
      knowledge_item_id: item.knowledge_item_id || "",
      title: item.title || "",
      domain: item.domain || "work",
      type: item.type || "fact",
      project: item.project || "",
      module: item.module || "",
      feature: item.feature || "",
      tags: (item.tags || []).join(","),
      source_uri: item.source_uri || "",
      author: "manual",
      change_note: "update from console",
      summary: item.summary || "",
      content_markdown: item.content_markdown || "",
      acl_actors: "",
    };

    Object.entries(map).forEach(([name, value]) => {
      const input = upsertForm.elements.namedItem(name);
      if (input) input.value = value;
    });

    const publicRead = upsertForm.elements.namedItem("public_read");
    if (publicRead && "checked" in publicRead) publicRead.checked = true;

    const editorTabBtn = document.querySelector('[data-tab="editor-tab"]');
    if (editorTabBtn) editorTabBtn.click();
  }

  if (searchForm) {
    searchForm.addEventListener("submit", async (event) => {
      event.preventDefault();

      const fd = new FormData(searchForm);
      const payload = {
        query: String(fd.get("query") || "").trim(),
        domain: String(fd.get("domain") || "work"),
        project: String(fd.get("project") || "").trim() || null,
        module: String(fd.get("module") || "").trim() || null,
        feature: String(fd.get("feature") || "").trim() || null,
        tags: toCsvArray(fd.get("tags")),
        source_uri: String(fd.get("source_uri") || "").trim() || null,
        top_k: Number(fd.get("top_k") || 8),
        actor: String(fd.get("actor") || "manual").trim() || "manual",
      };

      state.lastSearchActor = payload.actor;
      if (!payload.query) {
        if (resultMeta) resultMeta.textContent = "query 不能为空";
        return;
      }

      if (resultMeta) resultMeta.textContent = "查询中...";
      if (resultList) resultList.innerHTML = '<div class="empty-note">查询中...</div>';
      if (detailPanel) detailPanel.innerHTML = '<div class="empty-note">等待结果...</div>';
      if (loadEditorBtn) loadEditorBtn.disabled = true;
      if (deleteItemBtn) deleteItemBtn.disabled = true;
      setSearchHint(payload);

      try {
        const res = await apiFetch("/v1/knowledge/search", {
          method: "POST",
          body: JSON.stringify(payload),
        });

        state.results = res.results || [];
        state.selectedId = null;
        renderResults();

        if (!state.results.length) {
          if (resultMeta) resultMeta.textContent = "0 条结果";
          if (detailPanel) detailPanel.innerHTML = '<div class="empty-note">未命中，可尝试放宽过滤条件或更换关键词。</div>';
          if (deleteItemBtn) deleteItemBtn.disabled = true;
          return;
        }

        const ids = (res.knowledge_item_ids || []).join(",");
        const trace = res.trace_id || "-";
        if (resultMeta) resultMeta.textContent = `${state.results.length} 条 | trace_id=${trace} | ids=${ids}`;

        state.selectedId = state.results[0].knowledge_item_id;
        renderResults();
        await loadItemDetail(state.selectedId, payload.actor);
      } catch (err) {
        state.results = [];
        state.selectedId = null;
        renderResults();
        if (resultMeta) resultMeta.textContent = `查询失败: ${err.message}`;
        if (detailPanel) detailPanel.innerHTML = `<div class="empty-note">${escapeHtml(err.message)}</div>`;
      }
    });
  }

  const searchResetBtn = el("search-reset");
  if (searchResetBtn) {
    searchResetBtn.addEventListener("click", () => {
      state.results = [];
      state.selectedId = null;
      if (resultMeta) resultMeta.textContent = "未查询";
      if (resultList) resultList.innerHTML = '<div class="empty-note">请先提交检索。</div>';
      if (detailPanel) detailPanel.innerHTML = '<div class="empty-note">点击左侧结果查看知识详情。</div>';
      if (loadEditorBtn) loadEditorBtn.disabled = true;
      if (deleteItemBtn) deleteItemBtn.disabled = true;
      if (searchHint) searchHint.textContent = "留空字段不会参与过滤。";
    });
  }

  if (upsertForm) {
    upsertForm.addEventListener("submit", async (event) => {
      event.preventDefault();
      clearAlert();

      const fd = new FormData(upsertForm);
      const publicRead = upsertForm.elements.namedItem("public_read");
      const payload = {
        knowledge_item_id: String(fd.get("knowledge_item_id") || "").trim() || null,
        title: String(fd.get("title") || "").trim(),
        domain: String(fd.get("domain") || "work"),
        project: String(fd.get("project") || "").trim(),
        module: String(fd.get("module") || "").trim(),
        feature: String(fd.get("feature") || "").trim(),
        tags: toCsvArray(fd.get("tags")),
        source_uri: String(fd.get("source_uri") || "").trim(),
        type: String(fd.get("type") || "fact"),
        content_markdown: String(fd.get("content_markdown") || "").trim(),
        summary: String(fd.get("summary") || "").trim(),
        author: String(fd.get("author") || "manual").trim(),
        change_note: String(fd.get("change_note") || "").trim(),
        public_read: Boolean(publicRead && publicRead.checked),
        acl_actors: toCsvArray(fd.get("acl_actors")),
      };

      if (!payload.title || !payload.project || !payload.author || !payload.content_markdown) {
        showAlert("warning", "请填写必填字段（title / project / author / content_markdown）。");
        return;
      }

      try {
        const res = await apiFetch("/v1/knowledge/items/upsert", {
          method: "POST",
          body: JSON.stringify(payload),
        });
        showAlert("success", `保存成功：knowledge_item_id=${res.knowledge_item_id}，version=${res.version}`);
        if (res.knowledge_item_id) {
          upsertForm.elements.namedItem("knowledge_item_id").value = res.knowledge_item_id;
        }
      } catch (err) {
        showAlert("danger", `保存失败：${err.message}`);
      }
    });
  }

  const askForm = el("ask-form");
  if (askForm) {
    askForm.addEventListener("submit", async (event) => {
      event.preventDefault();

      const payload = {
        question: String(el("ask-question")?.value || "").trim(),
        domain: String(el("ask-domain")?.value || "work"),
        project: String(el("ask-project")?.value || "").trim() || null,
        top_k_chunks: Number(el("ask-topk")?.value || 5),
        actor: String(el("ask-actor")?.value || "manual").trim() || "manual",
      };

      const answerEl = el("ask-answer");
      const answerWarningEl = el("ask-answer-warning");
      const chunksEl = el("ask-chunks");
      if (answerWarningEl) answerWarningEl.hidden = true;

      if (!payload.question) {
        if (answerEl) answerEl.textContent = "问题不能为空。";
        return;
      }

      if (answerEl) answerEl.textContent = "问答中...";
      if (chunksEl) chunksEl.textContent = "加载片段中...";

      try {
        const res = await apiFetch("/v1/knowledge/ask", {
          method: "POST",
          body: JSON.stringify(payload),
        });

        if (answerEl) {
          const answerText =
            res.answer || (res.llm_available ? "模型未返回有效答案。" : "LLM 未启用或配置不完整。");
          if (res.answer && window.marked) {
            answerEl.classList.remove("empty-note");
            answerEl.classList.add("ask-answer-rendered");
            answerEl.innerHTML = window.marked.parse(answerText);
          } else {
            answerEl.classList.add("empty-note");
            answerEl.classList.remove("ask-answer-rendered");
            answerEl.textContent = answerText;
          }
        }
        if (answerWarningEl) {
          answerWarningEl.hidden = !res.truncated;
          if (!res.truncated) {
            answerWarningEl.textContent = "";
          } else if (configCache?.llm_max_tokens_auto !== false) {
            answerWarningEl.textContent =
              "回答达到模型供应商的输出长度上限，回答可能不完整。当前已是自动模式，可继续追问，或调整/更换模型。";
          } else {
            answerWarningEl.textContent =
              "回答达到模型输出长度上限，回答可能不完整。请继续提问，或在系统设置中启用自动控制或调大 Max Tokens。";
          }
        }

        const grouped = groupChunksByItem(res.chunks_used || []);
        if (chunksEl) {
          if (grouped.length === 0) {
            chunksEl.classList.add("empty-note");
            chunksEl.classList.remove("ask-chunks-rendered");
            chunksEl.innerHTML = "无引用片段。";
          } else {
            chunksEl.classList.remove("empty-note");
            chunksEl.classList.add("ask-chunks-rendered");
            chunksEl.innerHTML = grouped
              .map((g, gi) => {
                const refs = g.refNos.join("][");
                const snippets = g.snippets
                  .map(
                    (s) => `<li class="ask-snippet">${escapeHtml(s)}</li>`
                  )
                  .join("");
                return `
                <div class="ask-citation">
                  <div class="ask-citation-head">
                    <span class="ask-citation-ref">[${refs}]</span>
                    <span class="ask-citation-title">${escapeHtml(g.title || "")}</span>
                    <span class="ask-citation-meta">v${g.version || "-"} · ${escapeHtml(g.itemId)}</span>
                  </div>
                  <ul class="ask-snippets">${snippets}</ul>
                </div>`;
              })
              .join("");
          }
        }
      } catch (err) {
        if (answerEl) answerEl.textContent = `问答失败：${err.message}`;
        if (answerWarningEl) answerWarningEl.hidden = true;
        if (chunksEl) chunksEl.textContent = "无引用片段。";
      }
    });
  }

  function syncLlmMaxTokensState() {
    const autoInput = el("cfg_llm_max_tokens_auto");
    const tokenInput = el("cfg_llm_max_tokens");
    if (tokenInput) tokenInput.disabled = !!autoInput?.checked;
  }

  function fillSettings(d) {
    const servicePort = Number(d.service_port || 18000);
    if (el("cfg_service_port")) el("cfg_service_port").value = servicePort;
    if (el("cfg_api_base_url")) el("cfg_api_base_url").value = `http://127.0.0.1:${servicePort}`;

    const uiTheme = d.ui_theme || "neo";
    if (el("cfg_ui_theme")) el("cfg_ui_theme").value = uiTheme;
    applyTheme(uiTheme);

    if (el("cfg_llm_enabled")) el("cfg_llm_enabled").checked = !!d.llm_enabled;
    if (el("cfg_llm_api_key")) el("cfg_llm_api_key").value = d.llm_api_key || "";
    if (el("cfg_llm_base_url")) el("cfg_llm_base_url").value = d.llm_base_url || "https://api.openai.com/v1";
    if (el("cfg_llm_model")) el("cfg_llm_model").value = d.llm_model || "gpt-4o-mini";
    if (el("cfg_llm_timeout_sec")) el("cfg_llm_timeout_sec").value = d.llm_timeout_sec ?? 30;
    if (el("cfg_llm_temperature")) el("cfg_llm_temperature").value = d.llm_temperature ?? 0.2;
    if (el("cfg_llm_max_tokens")) el("cfg_llm_max_tokens").value = d.llm_max_tokens ?? 1024;
    if (el("cfg_llm_max_tokens_auto")) el("cfg_llm_max_tokens_auto").checked = d.llm_max_tokens_auto !== false;
    syncLlmMaxTokensState();

    if (el("cfg_embedding_enabled")) el("cfg_embedding_enabled").checked = !!d.embedding_enabled;
    if (el("cfg_embedding_api_key")) el("cfg_embedding_api_key").value = d.embedding_api_key || "";
    if (el("cfg_embedding_base_url")) el("cfg_embedding_base_url").value = d.embedding_base_url || "";
    if (el("cfg_embedding_model")) el("cfg_embedding_model").value = d.embedding_model || "";
    if (el("cfg_embedding_dim")) el("cfg_embedding_dim").value = d.embedding_dim ?? 384;
    if (el("cfg_embedding_timeout_sec")) el("cfg_embedding_timeout_sec").value = d.embedding_timeout_sec ?? 20;

    if (el("cfg_rerank_enabled")) el("cfg_rerank_enabled").checked = !!d.rerank_enabled;
    if (el("cfg_rerank_api_key")) el("cfg_rerank_api_key").value = d.rerank_api_key || "";
    if (el("cfg_rerank_base_url")) el("cfg_rerank_base_url").value = d.rerank_base_url || "";
    if (el("cfg_rerank_model")) el("cfg_rerank_model").value = d.rerank_model || "";
    if (el("cfg_rerank_path")) el("cfg_rerank_path").value = d.rerank_path || "/rerank";
    if (el("cfg_rerank_timeout_sec")) el("cfg_rerank_timeout_sec").value = d.rerank_timeout_sec ?? 20;

    if (el("cfg_enrichment_enabled")) el("cfg_enrichment_enabled").checked = !!d.enrichment_enabled;
  }

  async function saveSettings() {
    const statusEl = el("cfg_status");

    const payload = {
      service_port: Number(el("cfg_service_port")?.value || 18000),
      api_base_url: "",
      grafana_url: "http://127.0.0.1:3000",
      ui_theme: String(el("cfg_ui_theme")?.value || "neo"),
      llm_enabled: !!el("cfg_llm_enabled")?.checked,
      llm_api_key: String(el("cfg_llm_api_key")?.value || "").trim(),
      llm_base_url: String(el("cfg_llm_base_url")?.value || "").trim(),
      llm_model: String(el("cfg_llm_model")?.value || "").trim(),
      llm_timeout_sec: Number(el("cfg_llm_timeout_sec")?.value || 30),
      llm_temperature: Number(el("cfg_llm_temperature")?.value || 0.2),
      llm_max_tokens: Number(el("cfg_llm_max_tokens")?.value || 1024),
      llm_max_tokens_auto: !!el("cfg_llm_max_tokens_auto")?.checked,
    };

    if (el("cfg_embedding_enabled")) {
      payload.embedding_enabled = !!el("cfg_embedding_enabled").checked;
      payload.embedding_api_key = String(el("cfg_embedding_api_key")?.value || "").trim();
      payload.embedding_base_url = String(el("cfg_embedding_base_url")?.value || "").trim();
      payload.embedding_model = String(el("cfg_embedding_model")?.value || "").trim();
      payload.embedding_dim = Number(el("cfg_embedding_dim")?.value || 384);
      payload.embedding_timeout_sec = Number(el("cfg_embedding_timeout_sec")?.value || 20);
    }

    if (el("cfg_rerank_enabled")) {
      payload.rerank_enabled = !!el("cfg_rerank_enabled").checked;
      payload.rerank_api_key = String(el("cfg_rerank_api_key")?.value || "").trim();
      payload.rerank_base_url = String(el("cfg_rerank_base_url")?.value || "").trim();
      payload.rerank_model = String(el("cfg_rerank_model")?.value || "").trim();
      payload.rerank_path = String(el("cfg_rerank_path")?.value || "").trim() || "/rerank";
      payload.rerank_timeout_sec = Number(el("cfg_rerank_timeout_sec")?.value || 20);
    }

    if (el("cfg_enrichment_enabled")) {
      payload.enrichment_enabled = !!el("cfg_enrichment_enabled").checked;
    }

    payload.api_base_url = `http://127.0.0.1:${payload.service_port}`;

    if (statusEl) statusEl.textContent = "保存中...";
    const d = await apiFetch("/v1/system/config", { method: "PUT", body: JSON.stringify(payload) });
    configCache = d;
    fillSettings(d);
    if (statusEl) statusEl.textContent = `保存成功: ${d.updated_at || ""}`;
    return d;
  }

  const servicePortInput = el("cfg_service_port");
  if (servicePortInput) {
    servicePortInput.addEventListener("input", (event) => {
      const port = Number(event.target.value || 18000);
      if (el("cfg_api_base_url")) el("cfg_api_base_url").value = `http://127.0.0.1:${port}`;
    });
  }

  const llmMaxTokensAutoInput = el("cfg_llm_max_tokens_auto");
  if (llmMaxTokensAutoInput) {
    llmMaxTokensAutoInput.addEventListener("change", syncLlmMaxTokensState);
  }

  const settingsForm = el("settings-form");
  if (settingsForm) {
    settingsForm.addEventListener("submit", async (event) => {
      event.preventDefault();
      const statusEl = el("cfg_status");
      try {
        const d = await saveSettings();
        if (statusEl) {
          statusEl.textContent = `保存成功。端口变更后请重启并访问 http://127.0.0.1:${d.service_port || 18000}/console`;
        }
      } catch (err) {
        if (statusEl) statusEl.textContent = `保存失败: ${err.message}`;
      }
    });
  }

  const saveRestartBtn = el("cfg_save_restart");
  if (saveRestartBtn) {
    saveRestartBtn.addEventListener("click", async () => {
      const statusEl = el("cfg_status");
      try {
        const d = await saveSettings();
        if (statusEl) statusEl.textContent = "正在重启服务...";
        await apiFetch("/v1/system/restart", { method: "POST", body: "{}" });

        const targetPort = Number(d.service_port || 18000);
        const targetBase = `http://127.0.0.1:${targetPort}`;
        const startedAt = Date.now();

        const timer = setInterval(async () => {
          try {
            const r = await fetch(`${targetBase}/health`, { cache: "no-store" });
            if (r.ok) {
              clearInterval(timer);
              window.location.href = `${targetBase}/console?t=restored`;
              return;
            }
          } catch {
            // ignore during restart window
          }

          if (Date.now() - startedAt > 45000) {
            clearInterval(timer);
            if (statusEl) statusEl.textContent = `重启超时，请手动打开 ${targetBase}/console`;
          }
        }, 1500);
      } catch (err) {
        if (statusEl) statusEl.textContent = `重启失败: ${err.message}`;
      }
    });
  }

  if (resultList) resultList.innerHTML = '<div class="empty-note">请先提交检索。</div>';
  refreshHealth();
  loadAppVersion();

  // === Phase 4 Batch C: 系统状态顶部 banner ===
  // 优先级:reindex 进行中 > warming_up > 未启用 embedding
  async function refreshSystemBanner() {
    const banner = document.getElementById("system-banner");
    const txt = document.getElementById("system-banner-text");
    const action = document.getElementById("system-banner-action");
    if (!banner) return;

    // 拉两份状态
    let embedStatus = null, reindexStatus = null;
    try {
      const r1 = await fetch("/v1/system/embedding-service/status");
      if (r1.ok) embedStatus = await r1.json();
    } catch (e) { /* 忽略 */ }
    try {
      const r2 = await fetch("/v1/system/rebuild-vector-index/status");
      if (r2.ok) reindexStatus = await r2.json();
    } catch (e) { /* 忽略 */ }

    const hide = () => {
      banner.style.display = "none";
      action.style.display = "none";
    };
    const show = (text, bg, border, showAction) => {
      banner.style.background = bg;
      banner.style.border = "1px solid " + border;
      banner.style.color = "#e5e7eb";
      banner.style.display = "flex";
      txt.innerHTML = text;
      action.style.display = showAction ? "inline" : "none";
    };

    // 优先级 1: reindex 进行中 (AC3)
    if (reindexStatus && reindexStatus.status === "running") {
      const total = reindexStatus.total || 0;
      const done = reindexStatus.processed || 0;
      const remain = total - done;
      const etaSec = remain > 0 ? Math.ceil(remain / 100) : 0;
      const etaText = etaSec >= 60 ? `~${Math.ceil(etaSec / 60)} 分钟` : `~${etaSec} 秒`;
      const blocked = reindexStatus.threshold_blocked_writes
        ? ' · <span style="color:#fbbf24;">期间写 API 返 202</span>' : '';
      show(
        `🔄 正在重建索引 <b>${done}/${total}</b> — 当前为关键词检索,预计剩余 ${etaText}${blocked}`,
        "#1e293b", "#f59e0b", false
      );
      return;
    }

    // 优先级 2: warming_up
    if (embedStatus && embedStatus.warming_up) {
      show(
        `⏳ Embedding 模型加载中 — 检索接口暂返 202,请稍候(通常 30-60 秒)`,
        "#1e293b", "#f59e0b", false
      );
      return;
    }

    // 优先级 3: mode=disabled / 未装 / 已装但没跑
    // 2026-07-01 fix：老逻辑只看 installed 不看 running 会漏 "已装未跑"，
    // 骗用户"Embedding 好了"但检索实际走 hash fallback（VectorIndex._embed_with_fallback）。
    // 现在必须 installed && running 才隐藏横幅，其余状态各自出准确文案。
    if (embedStatus) {
      const mode = embedStatus.mode;
      const installed = !!embedStatus.installed;
      const running = !!embedStatus.running;
      const lastError = String(embedStatus.last_error || "");
      if (mode === "disabled") {
        show(
          `ℹ️ 未启用 Embedding 服务,当前仅关键词检索。语义检索可在引导页配置。`,
          "#0f172a", "#3b82f6", true
        );
        return;
      }
      if (mode === "local" && !installed) {
        show(
          `ℹ️ Embedding 服务未安装,当前仅关键词检索。请到引导页完成配置。`,
          "#0f172a", "#3b82f6", true
        );
        return;
      }
      if (mode === "local" && installed && !running) {
        const errSlice = lastError ? lastError.slice(0, 80).replace(/[<>&]/g, s => ({'<':'&lt;','>':'&gt;','&':'&amp;'})[s]) : "";
        const errTail = errSlice ? ` · <span style="color:#fca5a5;">${errSlice}</span>` : "";
        show(
          `⚠️ Embedding 服务已安装但未在运行,当前仅关键词检索${errTail}<br>` +
          `<span style="opacity:.85;">右键托盘 → "▶ 启动 Embedding 服务" 可手动拉起</span>`,
          "#1e293b", "#f59e0b", false
        );
        return;
      }
      // installed && running → 真正就绪，不显示横幅
    }
    hide();
  }
  refreshSystemBanner();
  setInterval(refreshSystemBanner, 6000);

  (async () => {
    try {
      configCache = await apiFetch("/v1/system/config", { method: "GET", headers: {} });
      fillSettings(configCache || {});
    } catch {
      // settings API may be unavailable; keep console usable
    }
  })();
})();
