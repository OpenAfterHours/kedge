/* kedge hub — the landing page.
 *
 * No framework, no build step, no CDN, in keeping with the chat pane next door. Four things
 * happen here, in order down the file:
 *   list      — render every registered workbook with the state the server derived from disk;
 *   add       — a server-side file browser, a path box, and drag-and-drop;
 *   open      — POST the open, then read its progress off an SSE stream and draw a checklist;
 *   close     — let go of the open workbook, so the wrong click is a click to undo and not a
 *               restart; the server refuses a second workbook and this is the way past that;
 *   transition— once the stream says ready, move to the chat + iframe view at /.
 *
 * The step list is drawn up front from the server's own list of steps, so the user can see what
 * is about to happen before it starts, and every step carries the server's own sentence about
 * what it found. That is PLAN M3's "the user is not sat there wondering what is happening" —
 * a spinner would have been three lines and would not have said which port marimo came up on.
 */

(() => {
  "use strict";

  const $ = (id) => document.getElementById(id);

  function el(tag, attrs, ...children) {
    const node = document.createElement(tag);
    for (const [key, value] of Object.entries(attrs || {})) {
      if (value === null || value === undefined || value === false) continue;
      if (key === "class") node.className = value;
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
    if (!iso) return "never";
    const then = new Date(iso).getTime();
    if (Number.isNaN(then)) return "unknown";
    const seconds = Math.max(0, (Date.now() - then) / 1000);
    if (seconds < 60) return "just now";
    if (seconds < 3600) return `${Math.floor(seconds / 60)} min ago`;
    if (seconds < 86400) return `${Math.floor(seconds / 3600)} h ago`;
    if (seconds < 604800) return `${Math.floor(seconds / 86400)} d ago`;
    return new Date(then).toLocaleDateString("en-GB");
  }

  function bytes(size) {
    if (!size) return "";
    const units = ["B", "kB", "MB", "GB"];
    let value = size;
    let unit = 0;
    while (value >= 1024 && unit < units.length - 1) {
      value /= 1024;
      unit += 1;
    }
    return `${value < 10 && unit ? value.toFixed(1) : Math.round(value)} ${units[unit]}`;
  }

  async function api(path, options) {
    const response = await fetch(path, {
      headers: options && options.body ? { "Content-Type": "application/json" } : {},
      ...options,
    });
    if (!response.ok) {
      let detail = response.statusText;
      try {
        detail = (await response.json()).detail || detail;
      } catch (_) {
        /* the body was not JSON; the status text will do */
      }
      const error = new Error(detail);
      // Carried because one refusal here is answerable rather than fatal: a 409 from the open
      // route means a different workbook is holding this server, and that is a thing the user can
      // be offered a way out of instead of a sentence.
      error.status = response.status;
      throw error;
    }
    return response.status === 204 ? null : response.json();
  }

  const state = { steps: [], workbooks: [], attached: null, browsing: null, settings: null };

  // ── banner ─────────────────────────────────────────────────────────────────────────

  function say(message, tone) {
    const banner = $("banner");
    banner.textContent = message;
    banner.className = "banner" + (tone ? " " + tone : "");
    banner.hidden = !message;
  }

  // ── the list ───────────────────────────────────────────────────────────────────────

  const CACHED_VALUE_LABELS = {
    present: "present",
    partial: "partial",
    absent: "absent",
    not_applicable: "n/a",
    unknown: "not analysed",
  };

  /* Every pill answers one question a user standing in front of a list of workbooks actually
     has: is the file still there, is there a notebook, has a plan been approved, is something
     running, and did the last reconciliation pass. */
  function pillsFor(item) {
    const pills = [];
    if (!item.exists) {
      pills.push(el("span", { class: "pill bad" }, icon("i-warn"), "File missing"));
      return pills;
    }
    if (item.changed_on_disk) {
      pills.push(el("span", { class: "pill warn" }, "Changed since kedge last saw it"));
    }
    if (item.marimo && item.marimo.live) {
      pills.push(
        el("span", { class: "pill accent" }, `marimo live on ${item.marimo.port}`),
      );
    }
    pills.push(
      item.notebook_exists
        ? el("span", { class: "pill ok" }, icon("i-check"), "Notebook")
        : el("span", { class: "pill" }, "No notebook yet"),
    );
    if (item.approved_version) {
      pills.push(el("span", { class: "pill ok" }, `Plan v${item.approved_version} approved`));
    } else if (item.plan_version) {
      pills.push(
        el("span", { class: "pill warn" }, `Plan v${item.plan_version} ${item.plan_state}`),
      );
    } else {
      pills.push(el("span", { class: "pill" }, "No plan"));
    }
    if (item.reconciliation) {
      const passed = String(item.reconciliation).toLowerCase().includes("passed");
      pills.push(
        el("span", { class: "pill " + (passed ? "ok" : "warn") }, `Reconciled: ${item.reconciliation}`),
      );
    }
    return pills;
  }

  function factsFor(item) {
    const facts = [];
    const findings = item.findings || {};

    if (item.analysis_present) {
      facts.push(
        el(
          "span",
          { class: "fact" },
          el("span", { class: "fact-label", text: "Findings" }),
          el("span", {
            class: "fact-value",
            text:
              `${findings.total || 0}` +
              (findings.error ? ` (${findings.error} error)` : ""),
          }),
        ),
      );
      facts.push(
        el(
          "span",
          { class: "fact" },
          el("span", { class: "fact-label", text: "Operations" }),
          el("span", { class: "fact-value", text: String(item.operation_count || 0) }),
        ),
      );
      facts.push(
        el(
          "span",
          { class: "fact" },
          el("span", { class: "fact-label", text: "Cached values" }),
          el("span", {
            class: "fact-value",
            text: CACHED_VALUE_LABELS[item.cached_values] || item.cached_values,
          }),
        ),
      );
    } else {
      facts.push(
        el(
          "span",
          { class: "fact" },
          el("span", { class: "fact-label", text: "Findings" }),
          el("span", { class: "fact-value muted", text: "not analysed yet" }),
        ),
      );
    }

    /* Convertibility is the plan's own honest assessment of how much of the workbook it believes
       it can translate. Absent until a plan exists, and said so rather than shown as zero. */
    if (item.convertible === null || item.convertible === undefined) {
      facts.push(
        el(
          "span",
          { class: "fact" },
          el("span", { class: "fact-label", text: "Convertible" }),
          el("span", { class: "fact-value muted", text: "no plan yet" }),
        ),
      );
    } else {
      const percent = Math.round(item.convertible * 100);
      const bar = el("span", { class: "convert-bar" }, el("span", { style: `width:${percent}%` }));
      facts.push(
        el(
          "span",
          { class: "fact" },
          el("span", { class: "fact-label", text: "Convertible" }),
          bar,
          el("span", { class: "fact-value", text: `${percent}%` }),
        ),
      );
    }

    facts.push(
      el(
        "span",
        { class: "fact" },
        el("span", { class: "fact-label", text: "Last opened" }),
        el("span", { class: "fact-value", text: ago(item.last_opened_at) }),
      ),
    );
    return facts;
  }

  function cardFor(item) {
    const current = state.attached && state.attached.key === item.key;
    const card = el("div", {
      class: "card" + (item.exists ? "" : " missing") + (current ? " current" : ""),
    });

    card.append(
      el(
        "div",
        { class: "card-head" },
        el("span", { class: "card-name", text: item.name }),
        el("span", { class: "pills" }, pillsFor(item)),
      ),
      el("code", { class: "card-path", text: item.path }),
      el("div", { class: "facts" }, factsFor(item)),
    );

    if (item.blockers && item.blockers.length) {
      card.append(
        el(
          "ul",
          { class: "blockers" },
          item.blockers.slice(0, 3).map((blocker) => el("li", { text: blocker })),
        ),
      );
    }

    const actions = el("div", { class: "card-actions" });
    if (!item.exists) {
      actions.append(
        el("span", {
          class: "hub-hint",
          text: "This file is no longer where kedge last saw it. Move it back, or forget it.",
        }),
      );
    } else if (current) {
      actions.append(
        el("a", { class: "primary-button", href: "/" }, icon("i-book"), el("span", { text: "Back to this notebook" })),
      );
    } else if (item.marimo && item.marimo.live) {
      /* A marimo is already up for this workbook and our marker says we started it. Reattaching
         is offered rather than assumed, and spawning a second one is offered too, because the
         user may genuinely want a clean kernel. */
      actions.append(
        el(
          "button",
          { class: "primary-button", onclick: () => openWorkbook(item, true) },
          icon("i-link"),
          el("span", { text: "Reattach to the running kernel" }),
        ),
        el(
          "button",
          { class: "ghost-button", onclick: () => openWorkbook(item, false) },
          el("span", { text: "Start a fresh one instead" }),
        ),
      );
    } else {
      actions.append(
        el(
          "button",
          { class: "primary-button", onclick: () => openWorkbook(item, false) },
          icon("i-play"),
          el("span", { text: "Open" }),
        ),
      );
    }

    if (item.report_available) {
      actions.append(
        el(
          "a",
          { class: "ghost-button", href: `/api/hub/report/${item.key}`, target: "_blank", rel: "noreferrer" },
          icon("i-external"),
          el("span", { text: "Report" }),
        ),
      );
    }
    actions.append(el("div", { class: "topbar-spacer" }));
    actions.append(
      el(
        "button",
        {
          class: "ghost-button",
          title: "Remove from this list. Nothing on disk is touched.",
          onclick: () => forget(item),
        },
        icon("i-bin"),
        el("span", { text: "Forget" }),
      ),
    );
    card.append(actions);
    return card;
  }

  function render() {
    const list = $("hub-list");
    if (!state.workbooks.length) {
      list.replaceChildren(
        el(
          "div",
          { class: "empty" },
          el("h2", { text: "kedge has not seen any workbooks yet" }),
          el("p", {
            text:
              "Browse for an .xlsx or .xlsm, paste its full path, or drag one onto this page. " +
              "kedge reads it where it sits and writes what it derives into a folder beside it.",
          }),
        ),
      );
      return;
    }
    list.replaceChildren(...state.workbooks.map(cardFor));
  }

  async function refresh() {
    const data = await api("/api/hub/state");
    state.steps = data.steps || [];
    state.workbooks = data.workbooks || [];
    state.attached = data.open_workbook;
    $("foot-registry").textContent = `registry: ${data.registry_path}`;

    const back = $("open-current");
    back.hidden = !data.attached;
    $("close-current").hidden = !data.attached;
    if (data.open_workbook) {
      $("open-current-name").textContent = data.open_workbook.name;
      $("close-current").title =
        `Close ${data.open_workbook.name}: its marimo is stopped and this server returns to the ` +
        `hub, free to open another workbook.`;
    }
    render();
  }

  // ── adding ─────────────────────────────────────────────────────────────────────────

  async function addByPath(path) {
    if (!path.trim()) return;
    try {
      const data = await api("/api/hub/workbooks", {
        method: "POST",
        body: JSON.stringify({ path: path.trim() }),
      });
      say(`Added ${data.workbook.name}.`, "ok");
      $("path-input").value = "";
      await refresh();
    } catch (error) {
      say(error.message, null);
    }
  }

  async function upload(file) {
    const form = new FormData();
    form.append("file", file, file.name);
    try {
      const data = await api("/api/hub/upload", { method: "POST", body: form });
      say(`Added ${data.workbook.name}, saved to ${data.saved_to}.`, "ok");
      await refresh();
    } catch (error) {
      say(error.message, null);
    }
  }

  // ── the file browser ───────────────────────────────────────────────────────────────

  async function browse(path) {
    const dialog = $("browser");
    if (!dialog.open) dialog.showModal();
    let data;
    try {
      data = await api("/api/hub/browse" + (path ? `?path=${encodeURIComponent(path)}` : ""));
    } catch (error) {
      $("browse-body").replaceChildren(el("p", { class: "browser-note", text: error.message }));
      return;
    }
    state.browsing = data;
    $("browse-path").textContent = data.path;
    $("browse-up").disabled = !data.parent;

    $("browse-roots").replaceChildren(
      ...data.roots.map((root) =>
        el("button", {
          class: "ghost-button",
          type: "button",
          text: root,
          onclick: () => browse(root),
        }),
      ),
    );

    const rows = [
      ...data.directories.map((entry) =>
        el(
          "button",
          { class: "row", type: "button", onclick: () => browse(entry.path) },
          icon("i-folder"),
          el("span", { text: entry.name }),
        ),
      ),
      ...data.workbooks.map((entry) =>
        el(
          "button",
          {
            class: "row workbook",
            type: "button",
            onclick: async () => {
              $("browser").close();
              await addByPath(entry.path);
            },
          },
          icon("i-sheet"),
          el("span", { text: entry.name }),
          el("span", { class: "row-size", text: bytes(entry.size_bytes) }),
        ),
      ),
    ];
    $("browse-body").replaceChildren(
      ...(rows.length ? rows : [el("p", { class: "browser-note", text: "Nothing here." })]),
    );
    $("browse-note").textContent =
      `${data.workbooks.length} workbook(s), ${data.directories.length} folder(s)` +
      (data.other_file_count ? `, ${data.other_file_count} other file(s) not shown` : "");
  }

  // ── opening ────────────────────────────────────────────────────────────────────────

  const STEP_LABELS = {
    bridge: "Check the marimo bridge",
    cleanup: "Clean up after any previous run",
    analysing: "Analyse the workbook",
    planning: "Find an approved process plan",
    notebook: "Prepare the notebook file",
    launching: "Start marimo",
    session: "Bootstrap the kernel session",
    scaffolding: "Scaffold from the plan",
    agent: "Wire up the agent",
  };

  function drawSteps() {
    $("steps").replaceChildren(
      ...state.steps.map((name) =>
        el(
          "li",
          { class: "step", "data-step": name },
          el("span", { class: "step-mark" }),
          el(
            "span",
            {},
            el("span", { class: "step-name", text: STEP_LABELS[name] || name }),
            el("span", { class: "step-detail" }),
          ),
        ),
      ),
    );
  }

  const STEP_MARKS = { ok: "i-check", failed: "i-cross" };

  function markStep(step, condition, detail) {
    const node = $("steps").querySelector(`[data-step="${step}"]`);
    if (!node) return;
    node.className = "step " + condition;
    const mark = node.querySelector(".step-mark");
    const glyph = STEP_MARKS[condition];
    mark.replaceChildren(...(glyph ? [icon(glyph, "tick")] : []));
    node.querySelector(".step-detail").textContent = detail || "";
  }

  /* SSE off a fetch body reader rather than EventSource, for the same reason the chat pane does
     it: one code path for both, and a handle to abort. */
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
          /* a frame we cannot parse is not worth abandoning the open over */
        }
      }
    }
  }

  async function openWorkbook(item, reattach) {
    say("");
    const dialog = $("opening");
    $("opening-title").textContent = reattach ? `Reattaching to ${item.name}` : `Opening ${item.name}`;
    $("opening-path").textContent = item.path;
    $("opening-detail").textContent = "";
    $("opening-detail").className = "opening-detail";
    $("opening-go").hidden = true;
    $("opening-close").hidden = true;
    $("opening-switch").hidden = true;
    drawSteps();
    if (!dialog.open) dialog.showModal();

    let job;
    try {
      job = await api("/api/hub/open", {
        method: "POST",
        body: JSON.stringify({ key: item.key, reattach: Boolean(reattach) }),
      });
    } catch (error) {
      // A 409 means another workbook is holding this server. That refusal is correct — one server
      // owns one workbook and one marimo — but it is answerable, and being told to restart the
      // process from inside the browser that cannot restart it is a dead end rather than an
      // answer. Offer the switch; do not take it, because closing stops a live kernel.
      if (error.status === 409 && state.attached) {
        offerSwitch(item, reattach, error.message);
        return;
      }
      fail(error.message);
      return;
    }

    try {
      const response = await fetch(`/api/hub/open/${job.job_id}`);
      if (!response.ok || !response.body) throw new Error(`the server answered ${response.status}`);
      for await (const event of readEvents(response)) {
        if (event.type === "open_progress") {
          markStep(event.step, event.state, event.detail);
          if (event.detail) $("opening-detail").textContent = event.detail;
        } else if (event.type === "error") {
          fail(event.message);
        } else if (event.type === "open_ready") {
          ready(event);
        }
      }
    } catch (error) {
      fail(`The progress stream dropped: ${error.message}. The open may still be running; ` +
           `reload this page to see where it got to.`);
    }
  }

  function ready(event) {
    const detail = $("opening-detail");
    detail.className = "opening-detail";
    detail.textContent = event.demo
      ? "Open, in demo mode: no model endpoint was reachable, so the scripted agent will answer."
      : event.notebook_url
        ? "Open. The notebook is live and the chat is wired up."
        : "Open, but no notebook URL came back — the notebook pane will be empty.";
    $("opening-go").hidden = false;
    $("opening-close").hidden = false;
    // Going straight there without a click would steal the page from a user still reading a
    // failed step, so the transition is offered rather than taken.
    $("opening-go").focus();
  }

  function fail(message) {
    const detail = $("opening-detail");
    detail.className = "opening-detail bad";
    detail.textContent = message;
    $("opening-close").hidden = false;
  }

  /* The open was refused because another workbook holds this server. Say so, and put the one
     action that resolves it next to the sentence that describes it. */
  function offerSwitch(item, reattach, message) {
    const detail = $("opening-detail");
    detail.className = "opening-detail warn";
    detail.textContent =
      `${message} Closing stops its marimo kernel; the notebook, the plan and every chat are on ` +
      `disk and come back when you reopen it.`;
    $("opening-close").hidden = false;

    const button = $("opening-switch");
    button.hidden = false;
    button.replaceChildren(
      icon("i-cross"),
      el("span", { text: `Close ${state.attached.name} and open ${item.name}` }),
    );
    button.onclick = async () => {
      button.disabled = true;
      try {
        await api("/api/hub/close", { method: "POST" });
      } catch (error) {
        button.disabled = false;
        fail(error.message);
        return;
      }
      await refresh();
      button.disabled = false;
      await openWorkbook(item, reattach);
    };
  }

  async function closeWorkbook() {
    const open = state.attached;
    if (!open) return;
    try {
      const data = await api("/api/hub/close", { method: "POST" });
      say(data.detail, "ok");
    } catch (error) {
      say(error.message, null);
      return;
    }
    await refresh();
  }

  async function forget(item) {
    try {
      await api(`/api/hub/workbooks/${item.key}`, { method: "DELETE" });
      say(`Removed ${item.name} from the list. Nothing on disk was touched.`, "ok");
      await refresh();
    } catch (error) {
      say(error.message, null);
    }
  }

  // ── settings ───────────────────────────────────────────────────────────────────────

  /* The model endpoint, its key, and which model to use. Before this panel existed the only way
     to configure any of it was to hand-edit ~/.kedge/config.toml and run `keyring set` in a
     terminal — and a first run with no key opens in demo mode, so the fix lived somewhere the
     user had never been shown.

     The key is write-only from here: the server never sends one back, so the box is always empty
     on open and an empty box means "leave the stored one alone". The model is a free-text box
     with a dropdown beside it rather than a dropdown alone, because plenty of OpenAI-compatible
     servers do not implement /models and the user must still be able to name one. */

  function settingsSay(message, tone) {
    const banner = $("settings-banner");
    banner.textContent = message || "";
    banner.className = "banner" + (tone ? " " + tone : "");
    banner.hidden = !message;
  }

  const APPLIED = {
    now: "Saved. The agent is using it now.",
    next_open: "Saved. It takes effect when you open a workbook.",
    unusable:
      "Saved, but the endpoint could not be used, so this workbook stays in demo mode. " +
      "Test connection will say why.",
  };

  function keyNote(data) {
    const key = data.api_key;
    if (key.status === "unavailable") {
      return `This machine has no working keyring backend, so kedge cannot store or read a key.
              You can still set one with \`${key.set_command}\`.`;
    }
    if (key.status === "present") {
      return `A key is stored under ${key.service}/${key.entry}. Leave this empty to keep it.`;
    }
    return `No key is stored under ${key.service}/${key.entry}. Until there is one, kedge opens
            workbooks in demo mode: the scripted agent answers and nothing is sent to a model.`;
  }

  /* The efforts come from the server rather than being written out here, so the panel cannot
     offer a value kedge.config would reject. The empty option is not one of them: it is the
     absence of the setting, which is a different thing from "none" and the right default for a
     model that does not reason at all. */
  function fillReasoning(data) {
    const select = $("settings-reasoning");
    const efforts = data.reasoning_efforts || [];
    select.replaceChildren(el("option", { value: "" }, "Do not mention it"));
    for (const effort of efforts) select.append(el("option", { value: effort }, effort));
    select.value = data.reasoning_effort || "";
  }

  function fillSettings(data) {
    state.settings = data;
    $("settings-url").value = data.base_url;
    $("settings-model").value = data.model;
    $("settings-ref").value = data.api_key_ref;
    fillReasoning(data);
    $("settings-key").value = "";
    $("settings-key").placeholder = data.api_key.status === "present" ? "•".repeat(16) : "";
    $("settings-key-note").textContent = keyNote(data);
    $("settings-forget").disabled = data.api_key.status !== "present";

    /* A project kedge.toml layered over the file this panel writes still wins. Saying so is the
       difference between "kedge ignored me" and "that value is pinned by this file". */
    const pinned = Object.entries(data.overridden_by_project);
    const note = $("settings-url-note");
    if (pinned.length) {
      const names = pinned.map(([key]) => key).join(", ");
      note.textContent = `Note: ${names} ${pinned.length > 1 ? "are" : "is"} overridden by
        ${pinned[0][1]}, which wins over anything saved here. Edit that file to change it.`;
      note.className = "field-note warn";
    } else {
      note.textContent = "";
      note.className = "field-note";
    }
    $("settings-where").textContent = `Saved to ${data.config_path}. Values not set there fall
      back to kedge's defaults; a kedge.toml beside a workbook overrides both.`;
    updateNudge(data);
  }

  function updateNudge(data) {
    const nudge = $("settings-nudge");
    if (!data || data.api_key.status === "present") {
      nudge.hidden = true;
      return;
    }
    $("settings-nudge-text").textContent =
      data.api_key.status === "unavailable"
        ? `This machine has no working keyring, so kedge cannot read an API key. Workbooks will
           open in demo mode.`
        : `No API key is stored yet, so workbooks open in demo mode — the scripted agent answers
           and nothing is sent to a model.`;
    nudge.hidden = false;
  }

  function showModels(names, detail, tone) {
    const select = $("settings-model-select");
    select.replaceChildren();
    select.hidden = !names.length;
    if (names.length) {
      select.append(
        el("option", { value: "", disabled: true, selected: true },
           `${names.length} model${names.length === 1 ? "" : "s"} from the endpoint…`),
      );
      for (const name of names) select.append(el("option", { value: name }, name));
    }
    const note = $("settings-model-note");
    note.textContent = detail || "";
    note.className = "field-note" + (tone ? " " + tone : "");
  }

  function settingsPayload() {
    const key = $("settings-key").value.trim();
    return {
      base_url: $("settings-url").value.trim(),
      api_key_ref: $("settings-ref").value.trim(),
      // Only ever sent when the user typed one. An empty box means "keep what is stored".
      api_key: key || null,
    };
  }

  async function probeModels(quiet) {
    const button = $("settings-fetch");
    button.disabled = true;
    if (!quiet) $("settings-model-note").textContent = "Asking the endpoint…";
    try {
      const data = await api("/api/settings/model/probe", {
        method: "POST",
        body: JSON.stringify(settingsPayload()),
      });
      showModels(data.models, data.detail, data.ok ? null : "warn");
    } catch (error) {
      showModels([], error.message, "warn");
    } finally {
      button.disabled = false;
    }
  }

  async function saveSettings() {
    const button = $("settings-save");
    button.disabled = true;
    settingsSay("");
    try {
      const data = await api("/api/settings/model", {
        method: "PUT",
        body: JSON.stringify({
          ...settingsPayload(),
          model: $("settings-model").value.trim(),
          // Always sent, including empty: empty is how the panel clears the setting, and a field
          // omitted from this body means "leave it alone".
          reasoning_effort: $("settings-reasoning").value,
        }),
      });
      fillSettings(data);
      settingsSay(APPLIED[data.applied] || "Saved.", data.applied === "unusable" ? null : "ok");
      // Outside the catch: the save succeeded, and a list that failed to redraw must not be
      // reported as a save that failed.
      refresh().catch(() => {});
    } catch (error) {
      settingsSay(error.message, null);
    } finally {
      button.disabled = false;
    }
  }

  async function forgetKey() {
    settingsSay("");
    try {
      const data = await api("/api/settings/model/key", { method: "DELETE" });
      fillSettings(data);
      settingsSay("The stored key has been removed from the keyring.", "ok");
    } catch (error) {
      settingsSay(error.message, null);
    }
  }

  async function openSettings() {
    const dialog = $("settings");
    settingsSay("");
    showModels([], "");
    try {
      fillSettings(await api("/api/settings/model"));
    } catch (error) {
      settingsSay(error.message, null);
      return;
    }
    if (!dialog.open) dialog.showModal();
    // A stored key means the list can usually be had for free, so the picker is populated before
    // the user goes looking for it. Quietly: a dead endpoint is not worth an error on open.
    if (state.settings && state.settings.api_key.status === "present") await probeModels(true);
  }

  // ── wiring ─────────────────────────────────────────────────────────────────────────

  function setup() {
    $("refresh").addEventListener("click", () => refresh().catch((e) => say(e.message)));
    $("browse-open").addEventListener("click", () => browse(null));
    $("browse-up").addEventListener("click", () => {
      if (state.browsing && state.browsing.parent) browse(state.browsing.parent);
    });
    $("path-add").addEventListener("click", (event) => {
      event.preventDefault();
      addByPath($("path-input").value);
    });
    $("path-input").addEventListener("keydown", (event) => {
      if (event.key === "Enter") {
        event.preventDefault();
        addByPath($("path-input").value);
      }
    });
    $("opening-close").addEventListener("click", () => {
      $("opening").close();
      refresh().catch(() => {});
    });
    $("close-current").addEventListener("click", () => closeWorkbook());

    $("settings-open").addEventListener("click", () => openSettings());
    $("settings-nudge-open").addEventListener("click", () => openSettings());
    $("settings-cancel").addEventListener("click", () => $("settings").close());
    $("settings-save").addEventListener("click", () => saveSettings());
    $("settings-test").addEventListener("click", () => probeModels(false));
    $("settings-fetch").addEventListener("click", () => probeModels(false));
    $("settings-forget").addEventListener("click", () => forgetKey());
    $("settings-model-select").addEventListener("change", (event) => {
      if (!event.target.value) return;
      $("settings-model").value = event.target.value;
    });
    // The endpoint the list came from is no longer the one in the box, so the list is stale.
    $("settings-url").addEventListener("input", () => showModels([], ""));

    let depth = 0;
    const overlay = $("drop-overlay");
    document.addEventListener("dragenter", (event) => {
      event.preventDefault();
      depth += 1;
      overlay.hidden = false;
    });
    document.addEventListener("dragover", (event) => event.preventDefault());
    document.addEventListener("dragleave", (event) => {
      event.preventDefault();
      depth = Math.max(0, depth - 1);
      if (!depth) overlay.hidden = true;
    });
    document.addEventListener("drop", async (event) => {
      event.preventDefault();
      depth = 0;
      overlay.hidden = true;
      const files = Array.from(event.dataTransfer ? event.dataTransfer.files : []);
      for (const file of files) await upload(file);
    });
  }

  async function boot() {
    /* Wiring is done first and defended, because every listener below is optional to the page
       being *readable* and none of them is worth the whole page. An uncaught throw here — one
       getElementById returning null against markup this script does not match — stops boot before
       the first render and leaves a kedge page with a header and nothing under it, which reads as
       "kedge is broken" rather than as "reload me". */
    try {
      setup();
    } catch (error) {
      say(`Part of this page could not be wired up (${error.message}). Reload it.`);
    }
    try {
      await refresh();
    } catch (error) {
      say(`The kedge server is not answering: ${error.message}`);
    }
    // Without a key every workbook opens in demo mode, which is a surprise worth heading off on
    // the page where the user is about to choose one.
    try {
      updateNudge(await api("/api/settings/model"));
    } catch (_) {
      /* the banner above has already said the server is not answering */
    }
    // The registry's derived state changes underneath this page — a marimo shuts itself down on
    // its idle timeout, a reconciliation is run in another terminal — so it is re-read on a slow
    // timer rather than only on load.
    setInterval(() => refresh().catch(() => {}), 20000);
  }

  boot();
})();
