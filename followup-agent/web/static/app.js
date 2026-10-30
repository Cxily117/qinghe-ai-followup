/* 医护问答前端：原生 JS，无构建、无依赖。
 * 关键点（对应接口设计文档）：
 *   1. 不用 EventSource —— 它带不了 Authorization 头，改用 fetch + ReadableStream 手解 SSE；
 *   2. 打字机不直接 append delta —— 上游一个 chunk 可能几十字，直接贴就是"跳字"，
 *      这里用帧队列按 2~5 字匀速吐；
 *   3. replace 事件必须整体覆盖 —— 护栏删掉引用编号后，屏幕上不能还留着旧的 [1]；
 *   4. 所有后端错误都是 {code, message, request_id}，气泡里原样展示，便于医护报障。
 */
(function () {
  "use strict";

  const API = "/api/v1";
  const TOKEN_KEY = "qh_followup_token";

  const state = {
    token: localStorage.getItem(TOKEN_KEY) || "",
    user: null,
    sessions: [],
    sid: null,
    busy: false,
    // qa = 固定流程 RAG（快、可解释）；agent = 会调工具的智能体（能查患者、能多次检索）
    mode: "qa",
  };

  const $ = (sel) => document.querySelector(sel);
  const loginView = $("#login");
  const appView = $("#app");
  const loginForm = $("#login-form");
  const loginError = $("#login-error");
  const loginSubmit = $("#login-submit");
  const toggleRegister = $("#toggle-register");
  const messagesEl = $("#messages");
  const sessionListEl = $("#session-list");
  const composer = $("#composer");
  const input = $("#question");
  const counter = $("#counter");
  const sendBtn = $("#send");

  // 空状态模板：新建对话时用来还原（里面有三个示例问题）
  const EMPTY_HTML = messagesEl.innerHTML;
  let registerMode = false;

  /* ---------------------------------------------------------- 工具 */

  function esc(s) {
    return String(s == null ? "" : s).replace(/[&<>"']/g, (c) => ({
      "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;",
    }[c]));
  }

  // 极简排版：只处理加粗、换行、以及 [1] 这种引用编号（不做完整 markdown，避免引入库）
  function format(text) {
    return esc(text)
      .replace(/\*\*([^*\n]+)\*\*/g, "<strong>$1</strong>")
      .replace(/\[(\d{1,2})\]/g, '<sup class="cite" data-n="$1">$1</sup>')
      .replace(/\n/g, "<br>");
  }

  function fmtTime(iso) {
    if (!iso) return "";
    const s = String(iso).replace("T", " ");
    return s.slice(5, 16); // MM-DD HH:mm
  }

  function hideEmpty() {
    const empty = $("#empty-state");
    if (empty) empty.remove();
  }

  function scrollToBottom() {
    const nearBottom =
      messagesEl.scrollHeight - messagesEl.scrollTop - messagesEl.clientHeight < 160;
    if (nearBottom) messagesEl.scrollTop = messagesEl.scrollHeight;
  }

  /* ---------------------------------------------------------- HTTP */

  async function readError(res) {
    const ct = res.headers.get("content-type") || "";
    try {
      if (ct.includes("json")) {
        const d = await res.json();
        return { code: d.code || "HTTP_" + res.status, message: d.message || d.detail || "请求失败" };
      }
      const text = await res.text();
      return { code: "HTTP_" + res.status, message: text || "请求失败" };
    } catch (e) {
      return { code: "HTTP_" + res.status, message: "请求失败" };
    }
  }

  async function api(path, opts) {
    const o = opts || {};
    const headers = { Accept: "application/json" };
    if (o.body) headers["Content-Type"] = "application/json";
    if (o.auth !== false && state.token) headers.Authorization = "Bearer " + state.token;

    const res = await fetch(API + path, {
      method: o.method || "GET",
      headers,
      body: o.body ? JSON.stringify(o.body) : undefined,
    });

    // 401 统一跳登录：避免每个调用点各写一遍
    if (res.status === 401 && o.auth !== false) {
      logout(true);
      throw { code: "AUTH_REQUIRED", message: "登录已过期，请重新登录" };
    }
    if (!res.ok) throw await readError(res);
    const ct = res.headers.get("content-type") || "";
    return ct.includes("json") ? res.json() : res.text();
  }

  /* ---------------------------------------------------------- 登录 */

  function showLogin() {
    appView.hidden = true;
    loginView.hidden = false;
  }

  function logout(silent) {
    state.token = "";
    state.user = null;
    state.sessions = [];
    state.sid = null;
    localStorage.removeItem(TOKEN_KEY);
    showLogin();
    if (silent) {
      loginError.textContent = "登录已过期，请重新登录";
      loginError.hidden = false;
    }
  }

  toggleRegister.addEventListener("click", () => {
    registerMode = !registerMode;
    loginSubmit.textContent = registerMode ? "注册并登录" : "登录";
    toggleRegister.textContent = registerMode
      ? "已有账号？返回登录"
      : "没有账号？注册一个医护账号";
    loginError.hidden = true;
  });

  loginForm.addEventListener("submit", async (e) => {
    e.preventDefault();
    const username = $("#username").value.trim();
    const password = $("#password").value;
    if (!username || !password) return;

    loginError.hidden = true;
    loginSubmit.disabled = true;
    loginSubmit.textContent = registerMode ? "注册中…" : "登录中…";
    try {
      if (registerMode) {
        // 角色固定 staff：注册入口不该让人自己选成管理员
        await api("/auth/register", {
          method: "POST",
          auth: false,
          body: { username, password, full_name: username, role: "staff" },
        });
      }
      const data = await api("/auth/login", {
        method: "POST",
        auth: false,
        body: { username, password },
      });
      state.token = data.access_token;
      localStorage.setItem(TOKEN_KEY, state.token);
      await boot();
    } catch (err) {
      loginError.textContent = err.message || "登录失败，请稍后重试";
      loginError.hidden = false;
    } finally {
      loginSubmit.disabled = false;
      loginSubmit.textContent = registerMode ? "注册并登录" : "登录";
    }
  });

  $("#logout").addEventListener("click", () => logout(false));

  /* ---------------------------------------------------------- 会话列表 */

  async function loadSessions() {
    state.sessions = await api("/chat/sessions");
    renderSessions();
  }

  function renderSessions() {
    sessionListEl.innerHTML = "";
    if (!state.sessions.length) {
      const p = document.createElement("p");
      p.className = "session-empty";
      p.textContent = "还没有会话，直接提问即可创建。";
      sessionListEl.appendChild(p);
      return;
    }
    state.sessions.forEach((s) => {
      const btn = document.createElement("button");
      btn.type = "button";
      btn.className = "session-item" + (s.id === state.sid ? " active" : "");
      btn.innerHTML =
        '<span class="t">' + esc(s.title || "未命名会话") + "</span>" +
        '<span class="d">' + esc(fmtTime(s.updated_at)) + "</span>";
      btn.addEventListener("click", () => openSession(s.id));
      sessionListEl.appendChild(btn);
    });
  }

  function newSession() {
    if (state.busy) return;
    state.sid = null;
    messagesEl.innerHTML = EMPTY_HTML;
    renderSessions();
    input.focus();
  }

  $("#new-session").addEventListener("click", newSession);

  async function openSession(id) {
    if (state.busy || id === state.sid) return;
    state.sid = id;
    renderSessions();
    messagesEl.innerHTML = "";
    try {
      const msgs = await api("/chat/sessions/" + id + "/messages");
      msgs.forEach((m) => {
        if (m.role === "user") appendUser(m.content);
        else {
          const turn = createTurn();
          // 历史消息里的引用存在 meta.citation_detail 里（只有编号是不够的，医护要看文件名）
          turn.instant(m.content, (m.meta && m.meta.citation_detail) || []);
        }
      });
      messagesEl.scrollTop = messagesEl.scrollHeight;
    } catch (err) {
      const turn = createTurn();
      turn.error(err.code, err.message);
    }
  }

  /* ---------------------------------------------------------- 消息渲染 */

  function appendUser(text) {
    hideEmpty();
    const row = document.createElement("div");
    row.className = "msg user";
    const bubble = document.createElement("div");
    bubble.className = "bubble";
    bubble.textContent = text;
    row.appendChild(bubble);
    messagesEl.appendChild(row);
    scrollToBottom();
  }

  /**
   * 创建一条助手气泡，返回控制句柄。
   * 打字机状态（shown / pending / raf）关在闭包里，避免多条回答同时输出时互相串字。
   */
  function createTurn() {
    hideEmpty();
    const row = document.createElement("div");
    row.className = "msg assistant";
    const bubble = document.createElement("div");
    bubble.className = "bubble";
    const statusEl = document.createElement("div");
    statusEl.className = "status";
    // 工具步骤时间线：智能体模式下逐条追加，让"它在干什么"可见
    const stepsEl = document.createElement("div");
    stepsEl.className = "steps";
    stepsEl.hidden = true;
    const answerEl = document.createElement("div");
    answerEl.className = "answer";
    const citesEl = document.createElement("div");
    citesEl.className = "citations";
    bubble.append(stepsEl, statusEl, answerEl, citesEl);
    row.appendChild(bubble);
    messagesEl.appendChild(row);

    let shown = "";
    let pending = "";
    let raf = 0;

    function paint() {
      answerEl.innerHTML = format(shown);
    }

    function tick() {
      raf = 0;
      if (!pending) return;
      // 积压越多每帧吐越多，既保证"打字感"，又不会明显落后于流
      const step = pending.length > 60 ? 5 : pending.length > 20 ? 3 : 2;
      shown += pending.slice(0, step);
      pending = pending.slice(step);
      paint();
      scrollToBottom();
      if (pending) raf = requestAnimationFrame(tick);
    }

    return {
      el: row,

      setStatus(text) {
        if (!text) {
          statusEl.innerHTML = "";
          return;
        }
        statusEl.innerHTML = '<span class="spinner"></span><span>' + esc(text) + "</span>";
      },

      push(text) {
        if (!text) return;
        pending += text;
        if (!raf) raf = requestAnimationFrame(tick);
      },

      /**
       * 追加/更新一条工具步骤。
       * 按 seq 归并：同一工具先发 start 再发 ok，落在同一行上更新状态，
       * 不然后端每发两次事件就多出一行，时间线会翻倍。
       */
      addStep(ev) {
        stepsEl.hidden = false;
        let row = stepsEl.querySelector('[data-seq="' + ev.seq + '"]');
        if (!row) {
          row = document.createElement("div");
          row.dataset.seq = ev.seq;
          row.innerHTML = '<span class="dot"></span><span class="name"></span><span class="meta"></span>';
          stepsEl.appendChild(row);
        }
        row.className = "step" + (ev.status === "start" ? "" : " " + ev.status);
        row.querySelector(".name").textContent = ev.label || ev.tool || "";

        const d = ev.detail || {};
        let meta = "";
        if (ev.status === "start") meta = "进行中…";
        else if (ev.status === "blocked") meta = "未通过前置校验，已拦截";
        else if (ev.tool === "resolve_patient") meta = d.found ? d.patient || "已定位" : "未找到该患者";
        else if (ev.tool === "search_policy") meta = (d.hits || 0) + " 条条文";
        else if (ev.tool === "list_existing_tasks") meta = "已排 " + (d.existing || 0) + " 个节点";
        else if (ev.status === "error") meta = "执行失败";
        row.querySelector(".meta").textContent = meta;
        scrollToBottom();
      },

      /**
       * 待确认草稿卡片（智能体写操作的唯一出口）。
       *
       * 为什么卡片必须出现在气泡里而不是弹窗：
       * 医护需要边看制度引用边核对节点，"确认"这个动作要有上下文；
       * 弹窗一旦遮住回答，人只会条件反射点确定。
       *
       * 后端在发出 confirm_required 时已经**停止**了智能体循环，
       * 这里点不点、点哪个，都不会再触发模型——确认是真人的决定。
       */
      showConfirm(data) {
        const draft = data.draft || {};
        const items = draft.items || [];
        const card = document.createElement("div");
        card.className = "confirm-card";

        const rows = items
          .map(
            (it) =>
              '<li><span class="d">第 ' +
              esc(it.offset_days) +
              ' 天</span><span class="n">' +
              esc(it.name || "") +
              '</span><span class="dt">' +
              esc(it.due_date || "") +
              "</span></li>"
          )
          .join("");

        card.innerHTML =
          '<div class="cc-head"><span class="cc-badge">待确认</span>' +
          '<span class="cc-title">随访草稿（尚未写入系统）</span></div>' +
          '<div class="cc-meta">患者 ' +
          esc(draft.patient_ref || "-") +
          " · " +
          esc(draft.plan_type || "-") +
          " · " +
          items.length +
          " 个节点</div>" +
          '<ul class="cc-items">' +
          rows +
          "</ul>" +
          '<div class="cc-note">确认后写入「随访任务草稿」，仍需你在任务列表点确认才算定稿；取消则什么都不留。</div>' +
          '<div class="cc-actions">' +
          '<button class="btn primary" type="button" data-act="confirm">确认保存草稿</button>' +
          '<button class="btn ghost" type="button" data-act="cancel">取消</button>' +
          "</div>";

        const actions = card.querySelector(".cc-actions");
        actions.addEventListener("click", async (e) => {
          const btn = e.target.closest("button[data-act]");
          if (!btn) return;
          const act = btn.dataset.act;
          actions.querySelectorAll("button").forEach((b) => (b.disabled = true));
          btn.textContent = act === "confirm" ? "保存中…" : "取消中…";
          try {
            const r = await api("/agent/runs/" + data.run_uid + "/confirm", {
              method: "POST",
              body: { action: act, note: "" },
            });
            card.classList.add(act === "confirm" ? "done" : "cancelled");
            card.querySelector(".cc-badge").textContent = act === "confirm" ? "已保存" : "已取消";
            actions.remove();
            const tip = document.createElement("div");
            tip.className = "cc-result";
            tip.textContent =
              act === "confirm"
                ? "已保存为草稿 #" + (r.draft_id || "") + "，请到「任务草稿」核对后确认。"
                : "已取消，本次没有写入任何记录。";
            card.appendChild(tip);
          } catch (err) {
            actions.querySelectorAll("button").forEach((b) => (b.disabled = false));
            btn.textContent = act === "confirm" ? "确认保存草稿" : "取消";
            const tip = document.createElement("div");
            tip.className = "cc-result err";
            tip.textContent = (err && err.message) || "操作失败，请重试";
            card.appendChild(tip);
          }
          scrollToBottom();
        });

        bubble.appendChild(card);
        scrollToBottom();
      },

      replace(text) {
        pending = "";
        shown = text || "";
        paint();
      },

      finish(data) {
        statusEl.innerHTML = "";
        const finalText = data.final_text || "";
        if (finalText && finalText !== shown) {
          if (finalText.indexOf(shown) === 0) {
            pending += finalText.slice(shown.length); // 把没吐完的补齐，收尾也保持打字感
            if (!raf) raf = requestAnimationFrame(tick);
          } else {
            pending = "";
            shown = finalText;
            paint();
          }
        }
        renderCitations(citesEl, data.citations || []);
        if (data.flags && data.flags.empty_kb) {
          const note = document.createElement("div");
          note.className = "msg-note";
          note.textContent = "知识库暂无已发布制度，请先由管理员上传并发布。";
          bubble.appendChild(note);
        }
      },

      instant(text, citationDetail) {
        shown = text || "";
        pending = "";
        statusEl.innerHTML = "";
        paint();
        renderCitations(citesEl, citationDetail || []);
      },

      error(code, message, retryText) {
        statusEl.innerHTML = "";
        bubble.classList.add("error");
        const note = document.createElement("div");
        note.className = "msg-note";
        note.textContent = (code ? code + "：" : "") + (message || "请求失败");
        bubble.appendChild(note);
        if (retryText) {
          const actions = document.createElement("div");
          actions.className = "msg-actions";
          const btn = document.createElement("button");
          btn.type = "button";
          btn.className = "retry";
          btn.textContent = "重试";
          btn.addEventListener("click", () => {
            row.remove();
            send(retryText);
          });
          actions.appendChild(btn);
          bubble.appendChild(actions);
        }
      },
    };
  }

  /** 引用卡片：文件名 + 片段，可展开完整条文 */
  function renderCitations(container, list) {
    container.innerHTML = "";
    if (!list || !list.length) return;

    const head = document.createElement("div");
    head.className = "cite-head";
    head.textContent = "依据 " + list.length + " 条随访制度";
    container.appendChild(head);

    list.forEach((c) => {
      const card = document.createElement("div");
      card.className = "cite-card";
      card.dataset.n = String(c.n);

      const title = document.createElement("div");
      title.className = "cite-title";
      title.innerHTML =
        '<span class="n">' + esc(c.n) + "</span>" +
        "<span>" + esc(c.doc_title || "未命名制度") + "</span>" +
        '<span class="type">' + esc(c.doc_type || "") + "</span>";

      const preview = document.createElement("div");
      preview.className = "cite-preview";
      preview.textContent = c.preview || "";

      const more = document.createElement("button");
      more.type = "button";
      more.className = "cite-more";
      more.textContent = "展开原文";

      const full = document.createElement("div");
      full.className = "cite-full";
      full.hidden = true;

      more.addEventListener("click", async () => {
        if (!full.hidden) {
          full.hidden = true;
          more.textContent = "展开原文";
          return;
        }
        full.hidden = false;
        more.textContent = "收起";
        if (full.dataset.loaded) return;
        more.disabled = true;
        full.textContent = "正在读取原文…";
        try {
          const d = await api("/kb/citations/" + c.chunk_id);
          full.textContent = d.content || "（该片段内容为空）";
          full.dataset.loaded = "1";
        } catch (err) {
          full.textContent = "原文读取失败（" + err.code + "）：" + err.message;
        } finally {
          more.disabled = false;
        }
      });

      card.append(title, preview, more, full);
      container.appendChild(card);
    });
  }

  // 点击正文里的 [1] 跳到对应引用卡片
  messagesEl.addEventListener("click", (e) => {
    const chip = e.target.closest(".chip");
    if (chip) {
      send(chip.textContent);
      return;
    }
    const sup = e.target.closest("sup.cite");
    if (!sup) return;
    const msg = sup.closest(".msg");
    const card = msg && msg.querySelector('.cite-card[data-n="' + sup.dataset.n + '"]');
    if (!card) return;
    card.scrollIntoView({ behavior: "smooth", block: "nearest" });
    card.classList.add("flash");
    setTimeout(() => card.classList.remove("flash"), 1400);
  });

  /* ---------------------------------------------------------- 发送与 SSE */

  async function consumeSSE(res, turn, slowTimer) {
    const reader = res.body.getReader();
    const decoder = new TextDecoder("utf-8");
    let buffer = "";

    function handle(block) {
      let event = "message";
      let data = null;
      block.split("\n").forEach((line) => {
        if (line.indexOf("event:") === 0) event = line.slice(6).trim();
        else if (line.indexOf("data:") === 0) {
          try {
            data = JSON.parse(line.slice(5).trim());
          } catch (e) {
            /* 心跳或非 JSON 行，忽略 */
          }
        }
      });
      if (!data) return;

      switch (event) {
        case "session":
          if (data.session_id) state.sid = data.session_id;
          turn.setStatus(state.mode === "agent" ? "正在规划处理步骤…" : "正在检索随访规范…");
          break;

        case "run":
          // 智能体已启动：它要先想清楚查什么，这段时间比固定流程长
          turn.setStatus("正在规划处理步骤…");
          break;

        case "step":
          turn.addStep(data);
          if (data.status === "start") turn.setStatus(data.label || "处理中…");
          break;

        case "citations": {
          clearTimeout(slowTimer);
          const list = data.citations || [];
          turn.setStatus(
            list.length
              ? "已命中 " + list.length + " 条条文，正在生成回答…"
              : "未命中相关条文，正在生成回答…"
          );
          break;
        }

        case "delta":
          clearTimeout(slowTimer);
          turn.setStatus("");
          turn.push(data.text);
          break;

        case "replace":
          turn.replace(data.text);
          break;

        case "confirm_required":
          // 写操作被闸门拦下：智能体循环已终止，等真人在卡片上做决定
          clearTimeout(slowTimer);
          turn.setStatus("");
          turn.showConfirm(data);
          break;

        case "done":
          clearTimeout(slowTimer);
          turn.finish(data);
          break;

        case "error":
          clearTimeout(slowTimer);
          turn.error(data.code, data.message);
          break;

        default:
          break; // end / 未知事件：忽略
      }
    }

    for (;;) {
      const r = await reader.read();
      if (r.done) break;
      buffer += decoder.decode(r.value, { stream: true });
      let idx;
      while ((idx = buffer.indexOf("\n\n")) >= 0) {
        const block = buffer.slice(0, idx);
        buffer = buffer.slice(idx + 2);
        if (block.trim()) handle(block);
      }
    }
    if (buffer.trim()) handle(buffer);
  }

  async function send(text) {
    const question = (text == null ? input.value : text).trim();
    if (!question || state.busy) return;

    state.busy = true;
    sendBtn.disabled = true;
    input.value = "";
    updateCounter();

    appendUser(question);
    const turn = createTurn();
    const agentMode = state.mode === "agent";
    turn.setStatus(agentMode ? "正在规划处理步骤…" : "正在检索随访规范…");

    // 检索 + 建连超过 1.5 秒还没回音，补一句，避免用户以为卡死
    const slowTimer = setTimeout(() => {
      turn.setStatus(
        agentMode ? "仍在处理（智能体可能正在多轮核对制度）…" : "正在检索随访规范…（制度条文较多，仍在检索）"
      );
    }, 1500);

    try {
      const res = await fetch(API + (agentMode ? "/agent/chat" : "/chat"), {
        method: "POST",
        headers: {
          "Content-Type": "application/json",
          Accept: "text/event-stream",
          Authorization: "Bearer " + state.token,
        },
        body: JSON.stringify({ session_id: state.sid, question }),
      });
      if (!res.ok) throw await readError(res);
      await consumeSSE(res, turn, slowTimer);
    } catch (err) {
      clearTimeout(slowTimer);
      turn.error(err.code || "NETWORK", err.message || "网络异常，请确认服务已启动", question);
    } finally {
      clearTimeout(slowTimer);
      state.busy = false;
      sendBtn.disabled = false;
      input.focus();
      loadSessions().catch(() => {}); // 会话标题/时间可能变了，静默刷新
    }
  }

  /* ---------------------------------------------------------- 输入框 */

  function updateCounter() {
    counter.textContent = input.value.length + " / 2000";
  }

  function autosize() {
    input.style.height = "auto";
    input.style.height = Math.min(input.scrollHeight, 160) + "px";
  }

  input.addEventListener("input", () => {
    updateCounter();
    autosize();
  });

  input.addEventListener("keydown", (e) => {
    // isComposing：中文输入法选词时的回车不能当发送
    if (e.key === "Enter" && !e.shiftKey && !e.isComposing) {
      e.preventDefault();
      composer.requestSubmit();
    }
  });

  composer.addEventListener("submit", (e) => {
    e.preventDefault();
    send();
    autosize();
  });

  /* ---------------------------------------------------------- 模式切换 */

  const modeSeg = $("#mode-seg");
  const modeHint = $("#mode-hint");
  const MODE_HINT = {
    qa: "固定流程：检索一次 → 依条文作答",
    agent: "智能体：查患者 → 多轮检索 → 依条文作答（步骤可见）",
  };

  if (modeSeg) {
    modeSeg.addEventListener("click", (e) => {
      const btn = e.target.closest(".seg-btn");
      if (!btn || state.busy) return;
      state.mode = btn.dataset.mode === "agent" ? "agent" : "qa";
      modeSeg.querySelectorAll(".seg-btn").forEach((b) => {
        b.classList.toggle("active", b.dataset.mode === state.mode);
      });
      if (modeHint) modeHint.textContent = MODE_HINT[state.mode];
      input.focus();
    });
  }

  /* ---------------------------------------------------------- 启动 */

  async function boot() {
    if (!state.token) {
      showLogin();
      return;
    }
    try {
      state.user = await api("/auth/me");
    } catch (e) {
      logout(false);
      return;
    }
    const u = state.user;
    $("#who").textContent =
      (u.full_name || u.username) + (u.role === "admin" ? "（管理员）" : "");
    loginView.hidden = true;
    appView.hidden = false;
    loginError.hidden = true;
    await loadSessions();
    newSession();
  }

  boot();
})();
