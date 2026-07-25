/* kedge hub — the landing page.
 *
 * No framework, no build step, no CDN, in keeping with the chat pane next door. Four things
 * happen here, in order down the file:
 *   list      — render every registered workbook with the state the server derived from disk;
 *   add       — a server-side file browser, a path box, and drag-and-drop;
 *   open      — POST the open, then read its progress off an SSE stream and draw a checklist;
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
      throw new Error(detail);
    }
    return response.status === 204 ? null : response.json();
  }

  const state = { steps: [], workbooks: [], attached: null, browsing: null };

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
    if (data.open_workbook) {
      $("open-current-name").textContent = data.open_workbook.name;
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
    drawSteps();
    if (!dialog.open) dialog.showModal();

    let job;
    try {
      job = await api("/api/hub/open", {
        method: "POST",
        body: JSON.stringify({ key: item.key, reattach: Boolean(reattach) }),
      });
    } catch (error) {
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

  async function forget(item) {
    try {
      await api(`/api/hub/workbooks/${item.key}`, { method: "DELETE" });
      say(`Removed ${item.name} from the list. Nothing on disk was touched.`, "ok");
      await refresh();
    } catch (error) {
      say(error.message, null);
    }
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
    setup();
    try {
      await refresh();
    } catch (error) {
      say(`The kedge server is not answering: ${error.message}`);
    }
    // The registry's derived state changes underneath this page — a marimo shuts itself down on
    // its idle timeout, a reconciliation is run in another terminal — so it is re-read on a slow
    // timer rather than only on load.
    setInterval(() => refresh().catch(() => {}), 20000);
  }

  boot();
})();
