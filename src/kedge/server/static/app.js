/* kedge chat pane.
 *
 * No framework, no build step, no CDN — plain modules-in-one-file, the same house style as the
 * rest of the OpenAfterHours tooling, and local-first: nothing here reaches off the machine.
 *
 * The interesting parts, in order down the file:
 *   markdown  — a small renderer that is safe against untrusted model output and tolerant of
 *               half-arrived fenced blocks, because prose is rendered while it streams;
 *   stream    — SSE read off a fetch body reader rather than EventSource, which cannot POST and
 *               gives no way to abort;
 *   turn      — the block model that interleaves prose and activity in arrival order, so the
 *               same code renders a live turn and a replayed one;
 *   pending   — the three decisions the model is not allowed to take on its own. `delete_cell`,
 *               `propose_plan` and `amend_plan` record a request and refuse; this is where the
 *               user says yes or no.
 */

(() => {
  "use strict";

  // ── small helpers ──────────────────────────────────────────────────────────────────

  const $ = (id) => document.getElementById(id);

  function el(tag, attrs, ...children) {
    const node = document.createElement(tag);
    for (const [key, value] of Object.entries(attrs || {})) {
      if (value === null || value === undefined || value === false) continue;
      if (key === "class") node.className = value;
      else if (key === "html") node.innerHTML = value;
      else if (key === "text") node.textContent = value;
      else if (key.startsWith("on")) node.addEventListener(key.slice(2), value);
      else node.setAttribute(key, value === true ? "" : value);
    }
    for (const child of children.flat()) {
      if (child === null || child === undefined || child === false) continue;
      node.append(child.nodeType ? child : document.createTextNode(String(child)));
    }
    return node;
  }

  function icon(name, cls) {
    const svg = document.createElementNS("http://www.w3.org/2000/svg", "svg");
    svg.setAttribute("class", "icon" + (cls ? " " + cls : ""));
    const use = document.createElementNS("http://www.w3.org/2000/svg", "use");
    use.setAttribute("href", "#" + name);
    svg.append(use);
    return svg;
  }

  function ago(iso) {
    const then = new Date(iso).getTime();
    if (Number.isNaN(then)) return "";
    const seconds = Math.max(0, (Date.now() - then) / 1000);
    if (seconds < 60) return "just now";
    if (seconds < 3600) return `${Math.floor(seconds / 60)} min ago`;
    if (seconds < 86400) return `${Math.floor(seconds / 3600)} h ago`;
    if (seconds < 604800) return `${Math.floor(seconds / 86400)} d ago`;
    return new Date(then).toLocaleDateString("en-GB");
  }

  async function api(path, options) {
    const response = await fetch(path, {
      headers: { "Content-Type": "application/json" },
      ...options,
    });
    if (!response.ok) {
      let detail = response.statusText;
      try {
        detail = (await response.json()).detail || detail;
      } catch (_) {
        /* the body was not JSON; the status text will do */
      }
      throw new Error(detail);
    }
    return response.status === 204 ? null : response.json();
  }

  // ── markdown ───────────────────────────────────────────────────────────────────────

  const ESCAPES = { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" };
  const escapeHtml = (text) => String(text).replace(/[&<>"']/g, (c) => ESCAPES[c]);

  const safeUrl = (url) => /^(https?:|mailto:|#|\/)/i.test(url.trim());

  /* Inline code is lifted out of the text before bold and italic are applied, so a run of
     backticks cannot be chewed through by them. The placeholder is NUL because escapeHtml has
     already removed every character that could plausibly collide with it. */
  const SENTINEL = String.fromCharCode(0);
  const RESTORE_SENTINEL = new RegExp(SENTINEL + "(\\d+)" + SENTINEL, "g");

  const PY_KEYWORDS =
    "and|as|assert|async|await|break|class|continue|def|del|elif|else|except|finally|" +
    "for|from|global|if|import|in|is|lambda|nonlocal|not|or|pass|raise|return|try|while|" +
    "with|yield|None|True|False|match|case";
  const PY_BUILTINS =
    "print|len|range|list|dict|set|tuple|str|int|float|bool|sum|min|max|abs|round|sorted|" +
    "enumerate|zip|isinstance|open|type|self|pl|mo";

  const PY_TOKENS = new RegExp(
    [
      "(#[^\\n]*)",
      "([rbfu]{0,2}\"\"\"[\\s\\S]*?\"\"\"|[rbfu]{0,2}'''[\\s\\S]*?''')",
      "([rbfu]{0,2}\"(?:\\\\.|[^\"\\\\\\n])*\"|[rbfu]{0,2}'(?:\\\\.|[^'\\\\\\n])*')",
      "(\\b\\d[\\d_]*\\.?\\d*(?:[eE][+-]?\\d+)?\\b)",
      "(@[A-Za-z_][\\w.]*)",
      `\\b(${PY_KEYWORDS})\\b`,
      `\\b(${PY_BUILTINS})\\b`,
      "([A-Za-z_]\\w*)(?=\\s*\\()",
    ].join("|"),
    "g",
  );

  const TOKEN_CLASSES = ["c-com", "c-str", "c-str", "c-num", "c-dec", "c-kw", "c-bi", "c-fn"];

  function highlight(code, language) {
    if (!/^(py|python|)$/i.test(language || "")) return escapeHtml(code);
    let out = "";
    let last = 0;
    let match;
    PY_TOKENS.lastIndex = 0;
    while ((match = PY_TOKENS.exec(code)) !== null) {
      out += escapeHtml(code.slice(last, match.index));
      const group = match.slice(1).findIndex((value) => value !== undefined);
      out += `<span class="${TOKEN_CLASSES[group] || ""}">${escapeHtml(match[0])}</span>`;
      last = match.index + match[0].length;
    }
    return out + escapeHtml(code.slice(last));
  }

  function inlineMarkdown(text) {
    const codes = [];
    let out = escapeHtml(text).replace(/`([^`]+)`/g, (_, body) => {
      codes.push(body);
      return SENTINEL + (codes.length - 1) + SENTINEL;
    });
    out = out
      .replace(/\*\*([^*\n]+)\*\*/g, "<strong>$1</strong>")
      .replace(/(^|[^*\w])\*([^*\n]+)\*/g, "$1<em>$2</em>")
      .replace(/~~([^~\n]+)~~/g, "<del>$1</del>")
      .replace(/\[([^\]\n]+)\]\(([^)\s]+)\)/g, (whole, label, href) =>
        safeUrl(href)
          ? `<a href="${href}" target="_blank" rel="noreferrer noopener">${label}</a>`
          : whole,
      );
    return out.replace(RESTORE_SENTINEL, (_, index) => `<code>${codes[index]}</code>`);
  }

  function codeBlockHtml(code, language) {
    const label = language ? `<span class="code-lang">${escapeHtml(language)}</span>` : "";
    return `<div class="code-block">${label}<pre><code>${highlight(code, language)}</code></pre></div>`;
  }

  /* Block-level markdown. Deliberately small: headings, lists, quotes, rules, fenced code and
     paragraphs are what a model writing about a notebook actually emits. An unterminated fence
     renders as a code block anyway, which is what makes streaming look right rather than
     flickering between prose and code as the closing fence arrives. */
  function renderMarkdown(source) {
    const lines = String(source).split("\n");
    let html = "";
    let index = 0;

    const flushList = (items, ordered) => {
      const tag = ordered ? "ol" : "ul";
      return `<${tag}>${items.map((item) => `<li>${inlineMarkdown(item)}</li>`).join("")}</${tag}>`;
    };

    while (index < lines.length) {
      const line = lines[index];
      const fence = line.match(/^\s*```(\S*)\s*$/);
      if (fence) {
        const body = [];
        index += 1;
        while (index < lines.length && !/^\s*```\s*$/.test(lines[index])) {
          body.push(lines[index]);
          index += 1;
        }
        index += 1;
        html += codeBlockHtml(body.join("\n"), fence[1] || "");
        continue;
      }

      if (/^\s*$/.test(line)) {
        index += 1;
        continue;
      }

      const heading = line.match(/^(#{1,6})\s+(.*)$/);
      if (heading) {
        const level = Math.min(heading[1].length, 4);
        html += `<h${level}>${inlineMarkdown(heading[2])}</h${level}>`;
        index += 1;
        continue;
      }

      if (/^\s*([-*_])\1{2,}\s*$/.test(line)) {
        html += "<hr>";
        index += 1;
        continue;
      }

      const bullet = line.match(/^\s*[-*+]\s+(.*)$/);
      const numbered = line.match(/^\s*\d+[.)]\s+(.*)$/);
      if (bullet || numbered) {
        const ordered = Boolean(numbered);
        const items = [];
        while (index < lines.length) {
          const item = lines[index].match(ordered ? /^\s*\d+[.)]\s+(.*)$/ : /^\s*[-*+]\s+(.*)$/);
          if (!item) break;
          items.push(item[1]);
          index += 1;
        }
        html += flushList(items, ordered);
        continue;
      }

      if (/^\s*>\s?/.test(line)) {
        const quoted = [];
        while (index < lines.length && /^\s*>\s?/.test(lines[index])) {
          quoted.push(lines[index].replace(/^\s*>\s?/, ""));
          index += 1;
        }
        html += `<blockquote>${renderMarkdown(quoted.join("\n"))}</blockquote>`;
        continue;
      }

      const paragraph = [];
      while (index < lines.length) {
        const next = lines[index];
        if (
          /^\s*$/.test(next) ||
          /^\s*```/.test(next) ||
          /^#{1,6}\s/.test(next) ||
          /^\s*[-*+]\s/.test(next) ||
          /^\s*\d+[.)]\s/.test(next) ||
          /^\s*>/.test(next)
        ) {
          break;
        }
        paragraph.push(next);
        index += 1;
      }
      html += `<p>${inlineMarkdown(paragraph.join("\n"))}</p>`;
    }
    return html;
  }

  // ── application state ──────────────────────────────────────────────────────────────

  const state = {
    context: null,
    sessionId: null,
    sessions: [],
    turn: null, // { controller, turnId, view }
    pending: { deletions: [], amendments: [], proposals: [] },
    autoScroll: true,
  };

  const transcript = $("transcript");
  const promptBox = $("prompt");

  const PHASE_LABELS = {
    analysing: "Analysing",
    thinking: "Thinking",
    editing: "Editing the notebook",
    running: "Running cells",
    stopping: "Stopping",
  };

  /* How long a cancelled turn is given to close its own stream before the connection is dropped
     from this end. The loop abandons its model call the moment the flag is set, so this is slack
     for the round trip rather than a guess at how long a step takes -- and dropping the connection
     is not a worse outcome, because the server tears a turn down when its client disconnects. */
  const CANCEL_GRACE_MS = 4000;

  const SUGGESTIONS = [
    "Summarise what this workbook actually does, stage by stage.",
    "Translate the haircut lookup on Calc!H2:H50000 and reconcile it against the cached values.",
    "Which stages of the plan are still unconverted, and why?",
    "Check whether the join key in the hand-in is unique before we rely on it.",
  ];

  // ── turn rendering ─────────────────────────────────────────────────────────────────

  /* When a replayed message was written, or nothing at all for one happening now.

     A stored turn is deliberately replayed through the same renderer as a live one, so that a
     resumed conversation looks like the conversation it is. The cost of that is a failure recorded
     hours ago being indistinguishable from one that has just happened — a stale "Fatal: the model
     endpoint refused" reads as the endpoint refusing right now, and the user goes looking for a
     bug that was fixed between the two. Dating the replayed heads is the whole of the remedy: a
     live turn has no stamp, because "just now" is what an undated message already means. */
  function stamp(when) {
    if (!when) return null;
    const at = new Date(when);
    if (Number.isNaN(at.getTime())) return null;
    return el("span", {
      class: "message-when",
      text: ago(when),
      title: at.toLocaleString("en-GB"),
    });
  }

  /* One assistant message. Prose and activity are appended in arrival order, so the trail is
     genuinely interleaved with the reasoning rather than parked in a drawer at the bottom —
     which is the whole point of PLAN M3's "the user is not sat there wondering what is
     happening". Prose accumulates into the block currently at the end; an activity event closes
     that block, so the next token starts a new one. */
  function createTurnView(when) {
    const phase = el("span", { class: "phase-chip", hidden: true });
    const phaseText = el("span", { text: "" });
    phase.append(el("span", { class: "pulse" }), phaseText);

    const tokensNote = el("span", { class: "tokens-note" });
    const head = el(
      "div",
      { class: "message-head" },
      el("span", { class: "role", text: "kedge" }),
      phase,
      stamp(when),
      tokensNote,
    );
    const blocks = el("div", { class: "blocks" });
    const root = el("div", { class: "message assistant" }, head, blocks);

    const view = {
      root,
      blocks,
      phase,
      phaseText,
      tokensNote,
      proseNode: null,
      proseText: "",
      pending: false,
    };

    view.setPhase = (name) => {
      if (!name) {
        view.phase.hidden = true;
        return;
      }
      view.phase.hidden = false;
      view.phaseText.textContent = PHASE_LABELS[name] || name;
    };

    view.flushProse = () => {
      if (!view.proseNode) return;
      view.proseNode.innerHTML = renderMarkdown(view.proseText);
      view.pending = false;
    };

    view.addToken = (text) => {
      if (!view.proseNode) {
        view.proseNode = el("div", { class: "prose" });
        view.proseText = "";
        view.blocks.append(view.proseNode);
      }
      view.proseText += text;
      if (!view.pending) {
        view.pending = true;
        requestAnimationFrame(() => {
          view.flushProse();
          maybeScroll();
        });
      }
    };

    view.addTrail = (node) => {
      view.flushProse();
      view.proseNode = null;
      view.blocks.append(node);
      maybeScroll();
    };

    return view;
  }

  function trailItem({ kind, title, iconName, tone, detail, args, preview, violations, cellId }) {
    const body = el("div", { class: "trail-body" });
    const heading = el("div", { class: "trail-title" }, el("span", { class: "trail-kind", text: kind }));
    if (title) heading.append(el("span", { text: title }));
    if (cellId) heading.append(el("span", { class: "trail-cell-id", text: cellId }));
    body.append(heading);
    if (args) body.append(el("div", { class: "trail-args", text: args }));
    if (detail) body.append(el("div", { class: "trail-detail", text: detail }));
    if (preview) body.append(el("pre", { class: "trail-preview", text: preview }));
    if (violations && violations.length) {
      body.append(
        el(
          "ul",
          { class: "trail-violations" },
          violations.map((violation) => el("li", { text: violation })),
        ),
      );
    }
    return el("div", { class: "trail" + (tone ? " " + tone : "") }, icon(iconName), body);
  }

  /* The one place an event becomes something on screen. Live streaming and replaying a stored
     session both come through here, so a resumed conversation looks exactly like a live one. */
  function applyEvent(view, event) {
    switch (event.type) {
      case "status":
        view.setPhase(event.phase);
        break;

      case "token":
        view.addToken(event.text);
        break;

      case "tool_call":
        view.addTrail(
          trailItem({
            kind: "Tool",
            title: event.name,
            iconName: "i-tool",
            tone: "accent",
            args: event.args_summary || null,
          }),
        );
        break;

      case "tool_result":
        view.addTrail(
          trailItem({
            kind: event.ok ? "Result" : "Failed",
            title: event.name,
            iconName: event.ok ? "i-check" : "i-cross",
            tone: event.ok ? "ok" : "bad",
            detail: event.summary || null,
          }),
        );
        break;

      case "cell_created":
        view.addTrail(
          trailItem({
            kind: "Cell created",
            title: event.name,
            cellId: event.cell_id,
            iconName: "i-cell",
            tone: "accent",
            preview: event.preview || null,
          }),
        );
        break;

      case "cell_running":
        view.addTrail(
          trailItem({
            kind: "Running",
            cellId: event.cell_id,
            iconName: "i-play",
            tone: "running",
          }),
        );
        break;

      case "cell_result":
        view.addTrail(
          trailItem({
            kind: event.ok ? "Cell ran" : "Cell failed",
            cellId: event.cell_id,
            iconName: event.ok ? "i-check" : "i-cross",
            tone: event.ok ? "ok" : "bad",
            detail: event.error || null,
          }),
        );
        break;

      case "validation":
        view.addTrail(
          trailItem({
            kind: "Validation",
            title: event.ok
              ? "passed"
              : `rejected (${(event.violations || []).length})`,
            iconName: "i-shield",
            tone: event.ok ? "ok" : "bad",
            violations: event.ok ? null : event.violations,
          }),
        );
        break;

      /* Not folded into "error". The turn is waiting for a word, not broken, and colouring it
         like a failure is what makes a user start the conversation again from scratch — which
         throws away the very context the pause exists to keep. */
      case "paused":
        view.addTrail(
          trailItem({
            kind: "Paused",
            title: event.steps ? `after ${event.steps} steps` : null,
            iconName: "i-stop",
            tone: "accent",
            detail: event.message,
          }),
        );
        break;

      case "error":
        view.addTrail(
          trailItem({
            kind: event.recoverable ? "Problem" : "Fatal",
            iconName: "i-warn",
            tone: "bad",
            detail: event.message,
          }),
        );
        break;

      case "done":
        view.flushProse();
        view.setPhase(null);
        if (event.tokens_used) {
          view.tokensNote.textContent = `${event.tokens_used.toLocaleString("en-GB")} tokens`;
        }
        break;

      default:
        break;
    }
  }

  // ── transcript ─────────────────────────────────────────────────────────────────────

  function maybeScroll() {
    if (state.autoScroll) transcript.scrollTop = transcript.scrollHeight;
  }

  transcript.addEventListener("scroll", () => {
    const distance = transcript.scrollHeight - transcript.scrollTop - transcript.clientHeight;
    state.autoScroll = distance < 80;
  });

  function addUserMessage(text, when) {
    transcript.append(
      el(
        "div",
        { class: "message user" },
        el(
          "div",
          { class: "message-head" },
          el("span", { class: "role", text: "You" }),
          stamp(when),
        ),
        el("div", { class: "bubble", text }),
      ),
    );
    maybeScroll();
  }

  function showWelcome() {
    transcript.replaceChildren(
      el(
        "div",
        { class: "welcome" },
        el("h2", { text: "What shall we work on?" }),
        el("p", {
          text:
            "kedge reads the workbook's structure, proposes cells, runs them, and reconciles the " +
            "result against the values Excel last calculated. Everything it does appears here as " +
            "it happens.",
        }),
        el(
          "div",
          { class: "suggestions" },
          SUGGESTIONS.map((suggestion) =>
            el("button", {
              class: "suggestion",
              type: "button",
              text: suggestion,
              onclick: () => {
                promptBox.value = suggestion;
                promptBox.focus();
                autoGrow();
              },
            }),
          ),
        ),
      ),
    );
  }

  // ── sessions ───────────────────────────────────────────────────────────────────────

  async function refreshSessions() {
    const data = await api("/api/sessions");
    state.sessions = data.sessions;
    renderSessionList();
  }

  function renderSessionList() {
    const list = $("session-list");
    if (!state.sessions.length) {
      list.replaceChildren(el("li", { class: "session-empty", text: "No chats yet." }));
      return;
    }
    list.replaceChildren(
      ...state.sessions.map((session) => {
        const item = el(
          "li",
          {
            class: "session-item" + (session.id === state.sessionId ? " active" : ""),
            onclick: () => openSession(session.id),
          },
          el(
            "div",
            { class: "session-item-main" },
            el("div", { class: "session-title", text: session.title }),
            el("div", {
              class: "session-meta",
              text: `${session.message_count} message${session.message_count === 1 ? "" : "s"} · ${ago(session.updated_at)}`,
            }),
          ),
        );
        item.append(
          el(
            "button",
            {
              class: "icon-button session-delete",
              title: "Delete this chat",
              "aria-label": "Delete this chat",
              onclick: async (event) => {
                event.stopPropagation();
                await api(`/api/sessions/${session.id}`, { method: "DELETE" });
                if (state.sessionId === session.id) state.sessionId = null;
                await refreshSessions();
                if (!state.sessionId) await ensureSession();
              },
            },
            icon("i-bin"),
          ),
        );
        return item;
      }),
    );
  }

  async function newSession() {
    const data = await api("/api/sessions", {
      method: "POST",
      body: JSON.stringify({ model: $("model-input").value || null }),
    });
    state.sessionId = data.session.id;
    localStorage.setItem("kedge.session", state.sessionId);
    showWelcome();
    $("drift-notice").hidden = true;
    await refreshSessions();
    promptBox.focus();
  }

  async function openSession(sessionId) {
    const data = await api(`/api/sessions/${sessionId}`);
    state.sessionId = sessionId;
    localStorage.setItem("kedge.session", sessionId);
    if (data.session.model) $("model-input").value = data.session.model;

    const notice = $("drift-notice");
    notice.hidden = !data.drifted;
    notice.classList.remove("bad");
    if (data.drifted) {
      notice.textContent =
        "The notebook has changed since this chat was started. kedge rebuilds notebook state " +
        "from the live kernel each turn, so it will see the current cells — but earlier " +
        "messages in this chat describe an older notebook.";
    }

    transcript.replaceChildren();
    if (!data.messages.length) {
      showWelcome();
    } else {
      for (const message of data.messages) {
        if (message.role === "user") {
          addUserMessage(message.content, message.created_at);
          continue;
        }
        if (message.role !== "assistant") continue;
        const view = createTurnView(message.created_at);
        transcript.append(view.root);
        if (message.events && message.events.length) {
          for (const event of message.events) applyEvent(view, event);
        } else if (message.content) {
          view.addToken(message.content);
        }
        view.flushProse();
        view.setPhase(null);
      }
    }
    renderSessionList();
    state.autoScroll = true;
    maybeScroll();
    refreshPending().catch(() => {});
  }

  async function ensureSession() {
    const remembered = localStorage.getItem("kedge.session");
    if (remembered && state.sessions.some((session) => session.id === remembered)) {
      await openSession(remembered);
      return;
    }
    if (state.sessions.length) {
      await openSession(state.sessions[0].id);
      return;
    }
    await newSession();
  }

  // ── pending decisions ──────────────────────────────────────────────────────────────

  /* `delete_cell` never deletes, `propose_plan` never writes a plan and `amend_plan` never
     amends: all three record the request, tell the model plainly that nothing has happened, and
     hand it to the user. This panel is that hand-off made visible. A deletion shows what reads
     the doomed cell's names, because that is the part of the decision the user cannot work out
     from the conversation; an amendment shows the model's rationale next to the change it wants;
     a proposal shows the whole decomposition, because approving one unblocks the notebook and
     PLAN 2.2 is emphatic that reviewing twelve lines now beats reviewing forty cells later.

     Proposals lead. A plan is the largest decision on the panel and everything else is written
     against it. */
  async function refreshPending() {
    const panel = $("pending");
    if (!state.sessionId) {
      panel.hidden = true;
      return;
    }
    let data;
    try {
      data = await api(`/api/sessions/${state.sessionId}/pending`);
    } catch (_) {
      panel.hidden = true;
      return;
    }
    state.pending = data;
    const cards = [
      ...(data.proposals || []).map((item, index) => proposalCard(item, index)),
      ...data.deletions.map((item, index) => deletionCard(item, index)),
      ...data.amendments.map((item, index) => amendmentCard(item, index)),
    ];
    panel.replaceChildren(...cards);
    panel.hidden = cards.length === 0;
  }

  /* `confirmClass` is not decoration. A deletion's confirm button destroys something and is
     styled to say so; a plan proposal's confirm button *accepts* something the user has just
     read, and styling that destructive while "Discard it" sits quiet beside it points the eye at
     the wrong choice. Danger styling means danger, or it means nothing. */
  function decisionCard({
    kind,
    title,
    tone,
    body,
    confirmLabel,
    confirmClass,
    dismissLabel,
    onConfirm,
    onDismiss,
  }) {
    return el(
      "div",
      { class: "decision " + (tone || "") },
      el(
        "div",
        { class: "decision-head" },
        icon(tone === "bad" ? "i-warn" : "i-shield"),
        el("span", { class: "decision-kind", text: kind }),
        el("span", { class: "decision-title", text: title }),
      ),
      body,
      el(
        "div",
        { class: "decision-actions" },
        el("button", {
          class: confirmClass || "danger-button",
          type: "button",
          text: confirmLabel,
          onclick: onConfirm,
        }),
        el("button", {
          class: "ghost-button",
          type: "button",
          text: dismissLabel || "Keep it",
          onclick: onDismiss,
        }),
      ),
    );
  }

  function deletionCard(item, index) {
    const body = el("div", { class: "decision-body" });
    body.append(el("p", { text: item.reason }));
    if (item.descendants.length) {
      body.append(
        el("p", {
          class: "decision-warn",
          text:
            `${item.descendants.length} cell(s) read names this cell defines and will break: ` +
            item.descendants.join(", "),
        }),
      );
    } else {
      body.append(el("p", { class: "decision-muted", text: "Nothing else reads what it defines." }));
    }
    return decisionCard({
      kind: "Delete cell",
      title: item.cell,
      tone: item.descendants.length ? "bad" : "",
      body,
      confirmLabel: "Delete it",
      onConfirm: () => decide(`pending/deletions/${index}`, "POST", "The cell was deleted."),
      onDismiss: () => decide(`pending/deletions/${index}`, "DELETE", "Deletion declined."),
    });
  }

  /* A whole plan, rendered to be *read* rather than acknowledged, and the bar is what the CLI
     shows before the same decision -- `plan.review.render_plan`. Anything left out here is
     something the user is being asked to approve unseen, so a stage carries its assumptions
     ("what a reviewer checks first"), its dependencies, the operations it claims and the pattern
     it translates, and a checkpoint carries the question it will ask rather than a note that one
     exists.

     The order is the order a decision gets made in. The triage verdict leads: a workbook kedge
     would decline to convert must not be planned and approved with the word never appearing.
     Verification blockers get their own line rather than a clause after the score -- "1.00
     convertible" with "cannot be reconciled" trailing behind a colon inverts the emphasis of the
     rule that matters most. The review warnings carry the only automatic check that the
     decomposition covers the workbook. And the approval blockers are pre-flighted so the button
     says what clicking it will actually do: a plan with an unacknowledged drop lands as a draft,
     and finding that out afterwards is finding it out too late. */
  function proposalCard(item, index) {
    const body = el("div", { class: "decision-body" });
    const stop = item.verdict === "stop";

    if (stop) {
      body.append(
        el("p", {
          class: "decision-blocker",
          text:
            "kedge triage recommends NOT converting this workbook. Read the blockers below " +
            "before approving anything: a plan written against a STOP verdict produces a " +
            "notebook that looks more complete than it is.",
        }),
      );
    } else if (item.verdict === "proceed_with_care") {
      body.append(
        el("p", { class: "decision-warn", text: "kedge triage: convertible, with reservations." }),
      );
    }

    if (item.summary) body.append(el("p", { text: item.summary }));

    body.append(
      el("p", {
        class: "decision-muted",
        text:
          `kedge triage scores this workbook ${item.convertible.toFixed(2)} convertible` +
          (item.complexity === null || item.complexity === undefined
            ? "."
            : `, complexity ${item.complexity.toFixed(2)}.`) +
          " That figure is kedge's own, scored from the analysis, not the model's opinion of itself.",
      }),
    );
    for (const blocker of item.blockers) {
      body.append(el("p", { class: "decision-warn", text: `Blocker: ${blocker}` }));
    }
    /* Non-negotiable 6: a workbook with no cached values still scores 1.00 convertible, which is
       arithmetically right and reads exactly wrong. "Cannot be reconciled" is not a footnote to a
       good score, it is the thing that makes the good score unprovable. */
    for (const blocker of item.verification_blockers || []) {
      body.append(el("p", { class: "decision-blocker", text: `Cannot be reconciled: ${blocker}` }));
    }
    if (item.analysis_stale) {
      body.append(
        el("p", {
          class: "decision-warn",
          text:
            "The workbook has been saved since it was analysed, so this plan — and the score " +
            "above — is a reading of a file that no longer exists. Re-analyse before approving.",
        }),
      );
    }

    body.append(
      el("p", {
        class: "decision-section",
        text: `Stages (${item.stages.length}), in the order they will be scaffolded`,
      }),
    );
    const stages = el("ul", { class: "decision-stages" });
    for (const stage of item.stages) stages.append(stageItem(stage));
    body.append(stages);

    if (item.open_questions.length) {
      body.append(
        el("p", {
          class: "decision-section",
          text: `Open questions (${item.open_questions.length})`,
        }),
      );
    }
    for (const question of item.open_questions) {
      const text = typeof question === "string" ? question : question.question;
      const context = typeof question === "string" ? null : question.context;
      body.append(el("p", { class: "decision-warn", text: `Open question: ${text}` }));
      if (context) body.append(el("p", { class: "decision-muted", text: `context: ${context}` }));
    }

    for (const drop of item.dropped) {
      body.append(
        el("p", { class: "decision-warn", text: `Proposes dropping ${drop.range}: ${drop.reason}` }),
      );
    }

    for (const warning of item.warnings || []) {
      body.append(el("p", { class: "decision-warn", text: `Check: ${warning}` }));
    }
    if (item.warnings_complete === false) {
      body.append(
        el("p", {
          class: "decision-muted",
          text:
            "No workbook analysis is loaded in this chat, so the coverage checks — operations " +
            "claimed by no stage, and stage operation ids that are not in the analysis — did not " +
            "run. The warnings above are the ones that need no analysis.",
        }),
      );
    }

    const blockers = item.approval_blockers || [];
    for (const blocker of blockers) {
      body.append(el("p", { class: "decision-blocker", text: `Blocks approval: ${blocker}` }));
    }

    return decisionCard({
      kind: "Process plan",
      title: `v${item.version}, ${item.stages.length} stage(s)`,
      tone: stop ? "bad" : "",
      body,
      confirmLabel: approvalLabel(blockers.length, item.unacknowledged_drops || 0),
      confirmClass: blockers.length ? "ghost-button" : "primary-button",
      dismissLabel: "Discard it",
      onConfirm: () => decide(`pending/proposals/${index}`, "POST", "The plan was written."),
      onDismiss: () => decide(`pending/proposals/${index}`, "DELETE", "Plan discarded."),
    });
  }

  /* "Approve this plan" is a promise the button cannot always keep: `approve` refuses a plan with
     an unacknowledged drop and it lands as a draft instead. Saying so on the button is the
     difference between a decision and a surprise. */
  function approvalLabel(blockerCount, dropCount) {
    if (!blockerCount) return "Approve this plan";
    if (dropCount && dropCount === blockerCount) {
      return `Save as draft — ${dropCount} drop${dropCount === 1 ? "" : "s"} need${
        dropCount === 1 ? "s" : ""
      } acknowledging`;
    }
    return `Save as draft — ${blockerCount} thing${blockerCount === 1 ? "" : "s"} block approval`;
  }

  function stageItem(stage) {
    const li = el(
      "li",
      {},
      el("span", { class: "decision-stage-id", text: stage.id }),
      el("span", {
        class: "decision-muted",
        text: ` ${stage.kind} · confidence ${stage.confidence}`,
      }),
      el("div", { text: stage.intent }),
    );
    const detail = (label, value) =>
      li.append(el("div", { class: "decision-muted", text: `${label}: ${value}` }));

    if ((stage.depends_on || []).length) detail("after", stage.depends_on.join(", "));
    if ((stage.sources || []).length) detail("sources", stage.sources.join(", "));
    if (stage.excel_pattern) detail("pattern", stage.excel_pattern);
    const operations = stage.operations || [];
    if (operations.length) {
      const shown = operations.slice(0, 6).join(", ");
      detail("operations", operations.length > 6 ? `${shown} (+${operations.length - 6} more)` : shown);
    }
    /* The stage field whose own docstring says "these are what a reviewer checks first". A wrong
       assumption -- "one row per counterparty" on a file that has two -- is the cheapest error to
       catch here and the most expensive to catch in forty scaffolded cells. */
    for (const assumption of stage.assumptions || []) {
      li.append(el("div", { class: "decision-assumes", text: `assumes: ${assumption}` }));
    }
    if (stage.checkpoint) {
      li.append(
        el("div", {
          class: "decision-asks",
          text: `not automated — asks: ${stage.checkpoint.question}`,
        }),
      );
      detail("options", (stage.checkpoint.options || []).join(", "));
      if (stage.checkpoint.guidance) detail("guidance", stage.checkpoint.guidance);
      /* `require_note` defaults to true and the note is the whole improvement over someone typing
         a number into Excel with no record of why. A plan that turns it off is weakening the
         control it is proposing, which the user should see before approving it. */
      if (stage.checkpoint.require_note === false) {
        li.append(
          el("div", { class: "decision-warn", text: "no note is required to clear this checkpoint" }),
        );
      }
    }
    if (stage.notes) detail("note", stage.notes);
    return li;
  }

  function amendmentCard(item, index) {
    const body = el("div", { class: "decision-body" });
    body.append(el("p", { text: item.change }));
    body.append(el("p", { class: "decision-muted", text: `Because: ${item.rationale}` }));
    return decisionCard({
      kind: "Amend the plan",
      title: item.stage ? `stage ${item.stage}` : "plan-level",
      body,
      confirmLabel: "Approve and write a new plan version",
      confirmClass: "primary-button",
      onConfirm: () =>
        decide(`pending/amendments/${index}`, "POST", "A new plan version was written."),
      onDismiss: () => decide(`pending/amendments/${index}`, "DELETE", "Amendment declined."),
    });
  }

  async function decide(path, method, success) {
    try {
      const data = await api(`/api/sessions/${state.sessionId}/${path}`, { method });
      let message = success;
      if (data.version) {
        message =
          `Plan v${data.version} written` +
          (data.approved
            ? " and approved."
            : `, as a draft — it cannot be approved yet: ${(data.blockers || []).join("; ")}`);
      }
      if (data.ok === false && data.detail) message = `The kernel refused: ${data.detail}`;
      notice(message);
    } catch (error) {
      notice(error.message, true);
    }
    await refreshPending();
  }

  function notice(message, bad) {
    const node = $("drift-notice");
    node.hidden = false;
    node.textContent = message;
    node.classList.toggle("bad", Boolean(bad));
  }

  // ── the turn ───────────────────────────────────────────────────────────────────────

  function setRunning(running) {
    $("send").disabled = running;
    $("stop").hidden = !running;
    // Re-armed rather than left as cancelTurn found it: the button is disabled for the length of
    // one cancellation, not for the rest of the session.
    $("stop").disabled = false;
    $("stop-label").textContent = "Stop";
    promptBox.disabled = false;
  }

  /* SSE off a fetch body reader. EventSource would be less code but cannot POST, so the message
     would have to travel in a query string, and it gives no handle to abort — and Escape must
     cancel the turn in flight. */
  async function* readEvents(response) {
    const reader = response.body.getReader();
    const decoder = new TextDecoder();
    let buffer = "";
    for (;;) {
      const { value, done } = await reader.read();
      if (done) break;
      buffer += decoder.decode(value, { stream: true });
      let split;
      while ((split = buffer.indexOf("\n\n")) >= 0) {
        const frame = buffer.slice(0, split);
        buffer = buffer.slice(split + 2);
        const data = frame
          .split("\n")
          .filter((line) => line.startsWith("data:"))
          .map((line) => line.slice(5).trim())
          .join("\n");
        if (!data) continue;
        try {
          yield JSON.parse(data);
        } catch (_) {
          /* a frame we cannot parse is not worth killing the turn over */
        }
      }
    }
  }

  async function send(message) {
    if (!state.sessionId || state.turn) return;

    if (transcript.querySelector(".welcome")) transcript.replaceChildren();
    addUserMessage(message);

    const view = createTurnView();
    view.setPhase("thinking");
    transcript.append(view.root);
    state.autoScroll = true;
    maybeScroll();

    const controller = new AbortController();
    state.turn = { controller, turnId: null, view };
    setRunning(true);

    try {
      const response = await fetch(`/api/sessions/${state.sessionId}/turns`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ message, model: $("model-input").value || null }),
        signal: controller.signal,
      });
      if (!response.ok || !response.body) {
        throw new Error(`the server answered ${response.status}`);
      }
      state.turn.turnId = response.headers.get("X-Kedge-Turn-Id");
      for await (const event of readEvents(response)) {
        applyEvent(view, event);
      }
    } catch (error) {
      applyEvent(view, {
        type: "error",
        recoverable: true,
        message:
          error.name === "AbortError"
            ? "Stopped. The connection was dropped rather than waited out, so anything the " +
              "turn had already done to the notebook stands — the pane shows the truth."
            : `The connection to the kedge server failed: ${error.message}`,
      });
      view.setPhase(null);
    } finally {
      view.flushProse();
      if (state.turn && state.turn.grace) clearTimeout(state.turn.grace);
      state.turn = null;
      setRunning(false);
      refreshSessions().catch(() => {});
      // A turn is the only thing that can record a pending deletion, plan proposal or amendment,
      // so this is the one moment the panel can change without the user having clicked something.
      refreshPending().catch(() => {});
      promptBox.focus();
    }
  }

  /* Stop has to say something the instant it is pressed. Cancellation is cooperative all the way
     down — the browser asks, the server sets a flag, the loop abandons what it is waiting on — and
     every one of those hops is invisible, so a button that acknowledges nothing reads as a button
     that did nothing, and the user presses it again. The chip goes to "Stopping", the button
     disables itself, and if the stream has not closed within the grace period the connection is
     dropped from this end so the UI is returned either way. */
  async function cancelTurn() {
    const turn = state.turn;
    if (!turn || turn.stopping) return;
    turn.stopping = true;
    turn.view.setPhase("stopping");
    $("stop").disabled = true;
    $("stop-label").textContent = "Stopping";

    if (turn.turnId) {
      try {
        await api(`/api/turns/${turn.turnId}/cancel`, { method: "POST" });
        turn.grace = setTimeout(() => {
          if (state.turn === turn) turn.controller.abort();
        }, CANCEL_GRACE_MS);
        return; // the loop finishes cooperatively and the stream closes itself
      } catch (_) {
        /* the turn had already finished; fall through to aborting the read */
      }
    }
    turn.controller.abort();
  }

  // ── health ─────────────────────────────────────────────────────────────────────────

  const HEALTH_STATES = {
    running: ["ok", "Kernel running"],
    unreachable: ["bad", "Kernel unreachable"],
    absent: ["warn", "No kernel attached"],
  };

  async function pollHealth() {
    const badge = $("health");
    try {
      const data = await api("/api/health");
      const [tone, label] = HEALTH_STATES[data.marimo.state] || ["warn", data.marimo.state];
      badge.className = "health " + tone;
      $("health-label").textContent = label;
      badge.title = data.marimo.detail || `marimo at ${data.marimo.base_url || "—"}`;
      // This poll already knows the two things the pane cannot work out for itself. A kernel that
      // is running while the pane holds no frame means the context the pane was drawn from is out
      // of date; an absent one may mean the workbook has been closed from the hub, and a chat
      // against a workbook this server no longer has open is a page about nothing. Either way the
      // answer is to re-read the context, which knows what to do with both.
      if (
        (data.marimo.state === "running" && !$("notebook-frame").src) ||
        data.marimo.state === "absent"
      ) {
        await refreshContext();
      }
    } catch (_) {
      badge.className = "health bad";
      $("health-label").textContent = "Server unreachable";
      badge.title = "The kedge server is not answering.";
    }
  }

  // ── models ─────────────────────────────────────────────────────────────────────────

  async function loadModels() {
    const input = $("model-input");
    try {
      const data = await api("/api/models");
      $("model-options").replaceChildren(
        ...data.models.map((name) => el("option", { value: name })),
      );
      if (!input.value) input.value = data.selected || data.models[0] || "";
      input.title =
        data.source === "endpoint"
          ? "Listed by the configured endpoint. Any name may be typed."
          : data.detail || "Type a model name.";
    } catch (_) {
      input.title = "The model list could not be fetched. Type a model name.";
    }
  }

  // ── notebook pane ──────────────────────────────────────────────────────────────────

  function applyContext(context) {
    state.context = context;
    $("crumb-workbook").textContent = context.workbook.name;
    $("crumb-workbook").title = context.workbook.path;
    $("crumb-notebook").textContent = context.notebook.name;
    $("notebook-title").textContent = context.notebook.name;
    document.title = `${context.workbook.name} — kedge`;

    $("sidebar-foot").replaceChildren(
      el("div", { text: context.demo ? "Demo mode: scripted agent, no model called." : "" }),
      el("code", { text: context.workbook.path }),
    );

    const frame = $("notebook-frame");
    const placeholder = $("notebook-placeholder");
    if (context.notebook_url) {
      /* The token rides in the query string on purpose. An iframe that loads unauthenticated
         lands on marimo's login page, which is the one endpoint setting X-Frame-Options: DENY,
         and the frame breaks (PLAN 1.3). */
      if (frame.src !== context.notebook_url) frame.src = context.notebook_url;
      frame.hidden = false;
      placeholder.hidden = true;
      $("open-notebook").href = context.notebook_url;
      // Un-hidden as well as pointed, because the branch below hides them and this one has to be
      // able to undo that: a pane that fills in later must come back with its controls.
      $("open-notebook").hidden = false;
      $("reload-notebook").hidden = false;
    } else {
      frame.hidden = true;
      placeholder.hidden = false;
      $("placeholder-detail").textContent = context.demo
        ? "Demo mode: no marimo server was started, so there is nothing to frame."
        : "No marimo server is attached yet. kedge is watching for one and will frame it here " +
          "as soon as it answers.";
      $("open-notebook").hidden = true;
      $("reload-notebook").hidden = true;
    }
  }

  /* Re-read the context and redraw the pane from it.
     The pane used to be drawn once, from the context fetched at boot, which assumed the notebook
     URL is knowable the moment the page loads. It is not always: a shell opened while marimo is
     still coming up, or restored by a back navigation, gets `notebook_url: null` and then sits on
     "No notebook attached" for ever with a live kernel on the other side of the loopback and no
     way back but a manual reload. */
  async function refreshContext() {
    try {
      const context = await api("/api/context");
      if (context.attached) {
        applyContext(context);
        return;
      }
      // The workbook was closed from the hub while this tab sat here. There is no chat to have
      // and no notebook to frame, and the hub is where a workbook is chosen — the same call boot
      // makes, for the same reason.
      if (!state.turn) window.location.replace(context.hub_url || "/hub");
    } catch (_) {
      /* the health badge already reports an unreachable server */
    }
  }

  // ── splitter ───────────────────────────────────────────────────────────────────────

  function setupSplitter() {
    const panes = $("panes");
    const splitter = $("splitter");
    const stored = Number(localStorage.getItem("kedge.split"));
    if (stored >= 20 && stored <= 80) panes.style.setProperty("--chat-w", `${stored}%`);

    const setFraction = (percent) => {
      const clamped = Math.min(80, Math.max(20, percent));
      panes.style.setProperty("--chat-w", `${clamped}%`);
      localStorage.setItem("kedge.split", String(Math.round(clamped)));
    };

    splitter.addEventListener("pointerdown", (event) => {
      splitter.setPointerCapture(event.pointerId);
      splitter.classList.add("dragging");
      document.body.style.userSelect = "none";
      /* The iframe would otherwise swallow every pointermove the moment the cursor crossed it. */
      $("notebook-frame").style.pointerEvents = "none";
    });
    splitter.addEventListener("pointermove", (event) => {
      if (!splitter.classList.contains("dragging")) return;
      const bounds = panes.getBoundingClientRect();
      setFraction(((event.clientX - bounds.left) / bounds.width) * 100);
    });
    const release = (event) => {
      if (!splitter.classList.contains("dragging")) return;
      splitter.releasePointerCapture(event.pointerId);
      splitter.classList.remove("dragging");
      document.body.style.userSelect = "";
      $("notebook-frame").style.pointerEvents = "";
    };
    splitter.addEventListener("pointerup", release);
    splitter.addEventListener("pointercancel", release);
    splitter.addEventListener("keydown", (event) => {
      const current = parseFloat(getComputedStyle(panes).getPropertyValue("--chat-w")) || 46;
      if (event.key === "ArrowLeft") setFraction(current - 2);
      else if (event.key === "ArrowRight") setFraction(current + 2);
    });
  }

  // ── composer ───────────────────────────────────────────────────────────────────────

  function autoGrow() {
    promptBox.style.height = "auto";
    promptBox.style.height = `${Math.min(promptBox.scrollHeight, 190)}px`;
  }

  function setupComposer() {
    promptBox.addEventListener("input", autoGrow);
    promptBox.addEventListener("keydown", (event) => {
      if (event.key === "Enter" && !event.shiftKey && !event.isComposing) {
        event.preventDefault();
        $("composer").requestSubmit();
      }
    });
    $("composer").addEventListener("submit", (event) => {
      event.preventDefault();
      const message = promptBox.value.trim();
      if (!message || state.turn) return;
      promptBox.value = "";
      autoGrow();
      send(message);
    });
    $("stop").addEventListener("click", cancelTurn);

    document.addEventListener("keydown", (event) => {
      if (event.key !== "Escape") return;
      if (state.turn) {
        event.preventDefault();
        cancelTurn();
      }
    });
  }

  // ── chrome ─────────────────────────────────────────────────────────────────────────

  function setupChrome() {
    $("new-chat").addEventListener("click", () => newSession());
    $("toggle-sidebar").addEventListener("click", () => {
      const collapsed = $("sidebar").classList.toggle("collapsed");
      localStorage.setItem("kedge.sidebar", collapsed ? "collapsed" : "open");
    });
    if (localStorage.getItem("kedge.sidebar") === "collapsed") {
      $("sidebar").classList.add("collapsed");
    }

    $("reload-notebook").addEventListener("click", () => {
      const frame = $("notebook-frame");
      if (frame.src) frame.src = frame.src;
    });

    $("model-input").addEventListener("change", () => {
      if (!state.sessionId) return;
      api(`/api/sessions/${state.sessionId}`, {
        method: "PATCH",
        body: JSON.stringify({ model: $("model-input").value || null }),
      }).catch(() => {});
    });

    const panes = $("panes");
    panes.dataset.pane = "chat";
    for (const button of $("pane-switch").querySelectorAll("button")) {
      button.addEventListener("click", () => {
        for (const other of $("pane-switch").querySelectorAll("button")) {
          other.classList.toggle("active", other === button);
        }
        panes.dataset.pane = button.dataset.pane;
      });
    }
  }

  // ── boot ───────────────────────────────────────────────────────────────────────────

  async function boot() {
    // Defended for the reason hub.js's boot is: one missing element must cost its own listener,
    // not the transcript, the composer and the notebook pane along with it.
    try {
      setupChrome();
      setupSplitter();
      setupComposer();
    } catch (error) {
      console.error("kedge: part of the shell could not be wired up", error);
    }
    try {
      const context = await api("/api/context");
      if (!context.attached) {
        // A server started with `kedge hub` has no workbook. Drawing an empty chat against one
        // that does not exist is worse than going where the workbooks are.
        window.location.replace(context.hub_url || "/hub");
        return;
      }
      applyContext(context);
    } catch (error) {
      transcript.replaceChildren(
        el(
          "div",
          { class: "welcome" },
          el("h2", { text: "The kedge server is not answering" }),
          el("p", { text: String(error.message) }),
        ),
      );
      return;
    }
    await loadModels();
    await refreshSessions();
    await ensureSession();
    pollHealth();
    setInterval(pollHealth, 5000);
    setInterval(renderSessionList, 60000);
    promptBox.focus();
  }

  boot();
})();
