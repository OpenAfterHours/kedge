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

  /* A body the browser knows how to frame for itself. Announcing a content type over one of these
     is not merely redundant, it is destructive: setting the header explicitly is what stops the
     browser generating the `multipart/form-data; boundary=...` a `FormData` needs, and the
     boundary is the only way the server can find the parts. Every dropped workbook 422'd on this
     -- for long enough that `~/.kedge/dropped` was never once created. */
  const selfDescribing = (body) =>
    (typeof FormData !== "undefined" && body instanceof FormData) ||
    (typeof Blob !== "undefined" && body instanceof Blob) ||
    (typeof URLSearchParams !== "undefined" && body instanceof URLSearchParams);

  async function api(path, options) {
    const body = options && options.body;
    const response = await fetch(path, {
      headers: body && !selfDescribing(body) ? { "Content-Type": "application/json" } : {},
      ...options,
    });
    if (!response.ok) {
      let detail = response.statusText;
      try {
        detail = (await response.json()).detail || detail;
      } catch (_) {
        /* the body was not JSON; the status text will do */
      }
      /* A `detail` is a string when kedge raised the HTTPException and a list of {loc, msg, type}
         when FastAPI rejected the request before the handler ran. Left as it was, `new Error` on
         that list stringified to "[object Object]" -- the one error shape that tells the user
         nothing at all, on the one path where they have done nothing wrong. */
      if (Array.isArray(detail)) {
        detail = detail.map((item) => (item && item.msg) || JSON.stringify(item)).join("; ");
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
     running, and did the last reconciliation pass.

     `source_state` rather than `exists` decides the framing, and that is the whole of the fix.
     An absent workbook has two readings — released on purpose, or lost — and reading the boolean
     alone rendered the successful end of a conversion as breakage, then returned early so the
     released row showed none of the notebook, plan and reconciliation it had every right to.
     `exists` is still read, on one row only: `released` with the file still there is a release
     whose delete did not finish, and the two facts are kept apart on the server precisely so the
     hub can say both rather than collapse them into one comfortable lie. */
  function pillsFor(item, current) {
    const pills = [];
    /* Ahead of every branch below, including the early return: a model credential sitting in
       plaintext in the project directory is not something an unrelated condition gets to
       suppress, and a workbook that has been moved still has the file. The server sends dotted
       key *names* and never values, which is why the tooltip can name them at all. */
    if (item.assistant_keys && item.assistant_keys.length) {
      pills.push(
        el(
          "span",
          {
            class: "pill bad",
            title:
              `.marimo.toml in this workbook's project directory holds ` +
              `${item.assistant_keys.join(", ")} in plain text. kedge neither reads nor sends ` +
              `them; clear them in marimo's own settings panel.`,
          },
          icon("i-warn"),
          "Key in .marimo.toml",
        ),
      );
    }
    /* Shown only where a kernel is actually up. kedge writes the lockdown at launch, so a
       workbook nobody has opened has no `.marimo.toml` and reads as "not enforced" — true, and
       harmless, because there is no marimo to be enforcing it against. A warning that sat on
       every unopened card would be permanently amber, which is how a signal stops being read. */
    if (Boolean(current || (item.marimo && item.marimo.live)) && item.assistant_enforced === false) {
      pills.push(
        el(
          "span",
          {
            class: "pill warn",
            title:
              "marimo's own AI assistant is live for this notebook. What it sends goes outside " +
              "kedge's tool surface and does not appear in the outbound payload log.",
          },
          icon("i-warn"),
          "marimo AI live",
        ),
      );
    }
    if (item.source_state === "missing") {
      pills.push(el("span", { class: "pill bad" }, icon("i-warn"), "File missing"));
      return pills;
    }
    if (item.source_state === "released") {
      pills.push(el("span", { class: "pill ok" }, icon("i-check"), "Released"));
      if (item.exists) {
        pills.push(
          el("span", { class: "pill bad" }, icon("i-warn"), "Workbook still on disk"),
        );
      }
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
    const released = item.source_state === "released";
    const card = el("div", {
      class:
        "card" +
        (item.source_state === "missing" ? " missing" : "") +
        (released ? (item.exists ? " released unfinished" : " released") : "") +
        (current ? " current" : ""),
    });

    card.append(
      el(
        "div",
        { class: "card-head" },
        el("span", { class: "card-name", text: item.name }),
        el("span", { class: "pills" }, pillsFor(item, current)),
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

    /* A released row is a running process, so its note explains rather than warns. The one case
       that genuinely is a fault gets its own sentence: released with the file still on disk means
       the delete did not finish, the registry is claiming something the filesystem did not do,
       and a user never told that goes on editing a spreadsheet nothing reads any more. */
    if (released) {
      card.append(
        item.exists
          ? el("p", {
              class: "card-note bad",
              text:
                `${item.name} is recorded as released, but the workbook is still on disk. The ` +
                `delete did not finish — usually because Excel had the file open. Release it ` +
                `again to clear it, or delete the file yourself.`,
            })
          : el("p", {
              class: "card-note",
              text:
                `Released ${ago(item.released_at)}: the spreadsheet is gone and this notebook is ` +
                `the process. Everything kedge derived from it is still here.`,
            }),
      );
    }

    const actions = el("div", { class: "card-actions" });
    if (item.source_state === "missing") {
      actions.append(
        el("span", {
          class: "hub-hint",
          text:
            "This file is no longer where kedge last saw it. Move it back, or forget it — " +
            "which deletes the notebook and the plans kedge made from it.",
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

    /* Release is offered only where there is something to keep. A workbook kedge has not built a
       notebook from has not become a process yet, so releasing it would delete the spreadsheet
       and leave an empty folder -- a purge of the wrong half, under a word promising the
       opposite. The one exception is a released row whose file is still there, which is a
       half-finished release and needs the button back to finish it. */
    if (item.notebook_exists && (!released || item.exists)) {
      actions.append(
        el(
          "button",
          {
            class: "ghost-button",
            title:
              "Delete the workbook and keep the notebook, the plans, the run records and the " +
              "conversation.",
            onclick: () => release(item),
          },
          icon("i-flag"),
          el("span", { text: released ? "Finish releasing" : "Release" }),
        ),
      );
    }
    actions.append(
      el(
        "button",
        {
          class: "ghost-button",
          title: "Delete this workbook and everything kedge made from it.",
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

  /* Reports rather than announces, because a drop can carry several files and the caller has to
     compose one banner out of all of them. Saying it here meant a drop of three workbooks showed
     only whatever the third one did. */
  async function upload(file) {
    const form = new FormData();
    form.append("file", file, file.name);
    try {
      const data = await api("/api/hub/upload", { method: "POST", body: form });
      return { ok: true, name: data.workbook.name, saved: data.saved_to };
    } catch (error) {
      return { ok: false, name: file.name, message: error.message };
    }
  }

  /* What a drop is actually carrying. A folder reaches `dataTransfer.files` as an entry with no
     extension and no bytes, so without this it was refused as "not a .xlsx or .xlsm file" -- true,
     unhelpful, and not what the user did. `webkitGetAsEntry` is the only reliable way to tell one
     from a genuinely empty file; the size-and-type heuristic is the fallback where it is absent. */
  function sortDrop(transfer) {
    const files = Array.from(transfer ? transfer.files : []);
    const items = Array.from(transfer && transfer.items ? transfer.items : []);
    const directories = new Set();
    items.forEach((item, index) => {
      if (item.kind !== "file" || !item.webkitGetAsEntry) return;
      const entry = item.webkitGetAsEntry();
      if (entry && entry.isDirectory && files[index]) directories.add(files[index]);
    });
    const looksLikeFolder = (file) =>
      directories.has(file) || (file.size === 0 && !file.type && !file.name.includes("."));
    return {
      folders: files.filter(looksLikeFolder).map((file) => file.name),
      workbooks: files.filter((file) => !looksLikeFolder(file)),
    };
  }

  async function receiveDrop(transfer) {
    const { folders, workbooks } = sortDrop(transfer);

    if (!folders.length && !workbooks.length) {
      say("That drop carried no file. Drag a .xlsx or .xlsm workbook, or use Browse.", null);
      return;
    }

    const notes = folders.map(
      (name) =>
        `${name} is a folder. kedge registers one workbook at a time -- open it and drag the ` +
        `.xlsx or .xlsm file inside.`,
    );
    const results = [];
    for (const [index, file] of workbooks.entries()) {
      const progress = workbooks.length > 1 ? ` (${index + 1} of ${workbooks.length})` : "";
      say(`Copying ${file.name}${progress}...`, null);
      results.push(await upload(file));
    }

    const added = results.filter((result) => result.ok);
    const refused = results.filter((result) => !result.ok);
    if (added.length === 1 && !refused.length && !notes.length) {
      say(`Added ${added[0].name}, saved to ${added[0].saved}.`, "ok");
    } else {
      if (added.length) notes.unshift(`Added ${added.map((r) => r.name).join(", ")}.`);
      for (const result of refused) notes.push(`${result.name}: ${result.message}`);
      say(notes.join(" "), refused.length || folders.length ? null : "ok");
    }
    if (added.length) await refresh();
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

  /* Forgetting deletes, so the dialogue enumerates. A user cannot be expected to know that a
     signed-off run record and eight months of conversation are inside the phrase "forget this
     workbook", and a generic "are you sure?" over that set of files is not consent. The counts
     come from the server reading the actual directory, not from a sentence written once.

     The instruction leads and the justification follows, as every other blocking message in kedge
     does -- a user who is stuck needs to know where to type before they need to know why. */
  async function forget(item) {
    const dialog = $("forgetting");
    let preview;
    try {
      preview = await api(`/api/hub/workbooks/${item.key}/deletion`);
    } catch (error) {
      say(error.message, null);
      return;
    }

    if (preview.open) {
      say(
        `${item.name} is the workbook this server has open. Close it first, then forget it.`,
        null,
      );
      return;
    }

    $("forgetting-title").textContent = `Forget ${item.name}`;
    $("forgetting-label").textContent = `Type ${item.name} to confirm`;
    $("forgetting-list").replaceChildren(
      ...[
        preview.workbook_exists && `the workbook itself, ${preview.workbook}`,
        ...preview.items,
      ]
        .filter(Boolean)
        .map((line) => el("li", { text: line })),
    );

    /* A released workbook has no file left to delete, and a list that quietly omits it reads as a
       list that forgot to mention it -- so it is said, in the right words for why it is gone. A
       release is a decision the user took; "not where kedge last saw it" is not. */
    const notes = [];
    if (!preview.workbook_exists) {
      notes.push(
        item.source_state === "released"
          ? `${item.name} was released, so the workbook itself has already gone. Forgetting ` +
              `removes what kedge derived from it, which the release deliberately kept.`
          : `${preview.workbook} is not there, so there is no workbook to delete. Forgetting ` +
              `removes what kedge derived from it.`,
      );
    }
    if (preview.external.length) {
      notes.push(
        `Also configured outside the project directory: ${preview.external.join(", ")}. These go ` +
          `too, unless another registered workbook is using them.`,
      );
    }
    const note = $("forgetting-note");
    note.textContent = notes.join(" ");
    note.hidden = !notes.length;

    const input = $("forgetting-confirm");
    const go = $("forgetting-go");
    input.value = "";
    go.disabled = true;
    const check = () => {
      go.disabled = input.value.trim().toLowerCase() !== item.name.toLowerCase();
    };
    input.oninput = check;
    go.onclick = async () => {
      go.disabled = true;
      dialog.close();
      say(`Forgetting ${item.name}...`, null);
      try {
        const data = await api(`/api/hub/workbooks/${item.key}`, { method: "DELETE" });
        const left = data.left_behind && data.left_behind.length;
        say(
          `Forgot ${item.name}. ${data.removed} item(s) and ${data.sessions} chat session(s) ` +
            `deleted.` +
            (left ? ` Left in place, because another workbook uses it: ${data.left_behind}.` : ""),
          "ok",
        );
      } catch (error) {
        say(error.message, null);
      }
      await refresh();
    };

    if (!dialog.open) dialog.showModal();
    input.focus();
  }

  /* Releasing is the mirror image of forgetting, and so is its confirmation. That one enumerates
     what will be destroyed, to make the user hesitate over consequences they cannot hold in their
     head — a directory they have never opened, signed-off run records, months of conversation.
     This one enumerates what *survives*, because that is the only thing that makes deleting the
     spreadsheet a whole process was built on a reasonable click.

     No type-to-confirm box, deliberately. The destructive scope here is one named file the user
     has just decided is obsolete, with nothing kedge made at risk; demanding they type its name
     over that teaches them the gate means nothing in particular, which is exactly what would
     devalue it on Forget, where it is load-bearing — and it puts maximum friction on the happy
     path, which is how people learn to click through friction. What the copy owes them instead is
     the one fact a Recycle Bin habit hides: the file is deleted, not recycled. Focus lands on
     Keep it, so a stray Enter is the safe answer. */
  async function release(item) {
    const dialog = $("releasing");
    let preview;
    try {
      preview = await api(`/api/hub/workbooks/${item.key}/release`);
    } catch (error) {
      say(error.message, null);
      return;
    }

    if (preview.open) {
      say(
        `${item.name} is the workbook this server has open, and releasing it deletes the ` +
          `spreadsheet a running kernel may still be reading. Close it first, then release it.`,
        null,
      );
      return;
    }

    $("releasing-title").textContent = `Release ${item.name}`;
    $("releasing-lede").textContent = preview.workbook_exists
      ? "The workbook is deleted and everything else is kept. It is deleted rather than moved to " +
        "the Recycle Bin, so take a copy first if you want one. The notebook goes on running " +
        "monthly, which is the point."
      : "The workbook has already gone. Releasing records that as a decision, so kedge stops " +
        "showing this process as a file somebody lost.";
    $("releasing-target").textContent = preview.workbook;
    $("releasing-target").hidden = !preview.workbook_exists;

    /* Whether the translation was ever accepted is the one fact here that a release can destroy
       rather than merely delete. Everything in the list below survives; this does not, because the
       spreadsheet is the only thing the notebook's arithmetic could ever be measured against. Said
       high, before the reassurance, and in the right words for each of the three answers -- a
       recorded failure is still a record, and grading it as a pass would be the reassuring lie. */
    const unchecked = preview.acceptance === "none";
    const passed =
      !unchecked && String(preview.acceptance_status).toLowerCase().includes("passed");
    const acceptance = $("releasing-acceptance");
    acceptance.className = "releasing-acceptance " + (unchecked ? "bad" : passed ? "ok" : "warn");
    acceptance.textContent = unchecked
      ? `Reconcile this conversion before you release it. The translation has never been checked ` +
        `against the spreadsheet, and the spreadsheet is the only thing it could ever be checked ` +
        `against — release it and that check can never be made, for the life of the notebook.`
      : passed
        ? `The translation was accepted ${ago(preview.accepted_at)} (${preview.acceptance_status}), ` +
          `and that record is kept and cited from here on. It is what makes the spreadsheet safe ` +
          `to let go of.`
        : `The translation check is on record as ${preview.acceptance_status}, taken ` +
          `${ago(preview.accepted_at)}. The record is kept and cited from here on, but read it ` +
          `before you delete the only thing it was ever measured against.`;
    /* The heading goes with the list. "Kept, exactly as it is:" over nothing at all is the
       dialogue's reassurance contradicted by its own layout, on the one screen where the layout
       is the argument. */
    $("releasing-list").replaceChildren(
      ...preview.kept.map((line) => el("li", { text: line })),
    );
    $("releasing-keeps").hidden = !preview.kept.length;

    const notes = [];
    if (!preview.notebook_exists) {
      // The route refuses this outright, so the note says so rather than describing a release
      // that will not happen. The button is hidden here too; this is the stale-page case.
      notes.push(
        "Convert this workbook first, or forget it instead. kedge has not generated a notebook " +
          "from it, so there is no process to release it to and the server will refuse.",
      );
    }
    /* A live marimo on this notebook is not a refusal -- only the workbook *this* server has open
       is refused, and another kedge serving it is allowed to go on. It is worth saying, though:
       its kernel reads the workbook, so a cell that ran before the file went is not evidence
       about the notebook as it stands now. */
    if (preview.marker === "live") {
      notes.push(
        `A marimo is still serving this notebook on port ${preview.marker_port}. Releasing does ` +
          `not stop it, and its marker and token are kept — but its kernel reads the workbook, ` +
          `so re-run the notebook there afterwards rather than trusting what is on screen.`,
      );
    }
    const note = $("releasing-note");
    note.textContent = notes.join(" ");
    note.hidden = !notes.length;

    /* The typing gate, armed for the unchecked case alone. It is the same criterion the Forget
       dialogue's box meets and an ordinary release does not: a consequence the user cannot hold in
       their head, because a notebook whose translation was never checked and one whose check
       passed look identical on screen for ever afterwards. Instruction first — the label names
       reconciling as the fix and the box is the way past it, in that order. */
    const input = $("releasing-confirm");
    const label = $("releasing-label");
    const go = $("releasing-go");
    input.hidden = !unchecked;
    label.hidden = !unchecked;
    label.textContent = unchecked ? `Or type ${item.name} to release it unchecked` : "";
    $("releasing-go-label").textContent = unchecked
      ? "Release without a check"
      : "Release the workbook";
    input.value = "";
    go.disabled = unchecked;
    input.oninput = () => {
      go.disabled = input.value.trim().toLowerCase() !== item.name.toLowerCase();
    };
    go.onclick = async () => {
      go.disabled = true;
      dialog.close();
      say(`Releasing ${item.name}...`, null);
      try {
        const data = await api(`/api/hub/workbooks/${item.key}/release`, { method: "POST" });
        /* The marker sentence is carried through when something is still serving the notebook,
           and — the case that would otherwise go quiet — when the dialogue said something was and
           it turned out not to be. A server can die between the preview and the click, and this
           is the one place the promise made a moment ago can be corrected. A stale pair tidied
           after saying nothing about it is housekeeping the user did not ask about. */
        const brokePromise = preview.marker === "live" && data.marker === "cleared";
        const marimo =
          data.marker === "kept" || brokePromise ? ` ${data.marker_detail}` : "";
        say(
          (data.removed
            ? `Released ${data.name}: the workbook has been deleted. Everything kedge derived ` +
                `from it is still here, and the notebook is the process now.`
            : `Recorded ${data.name} as released. The workbook had already gone; everything ` +
                `kedge derived from it is still here.`) + marimo,
          "ok",
        );
      } catch (error) {
        say(error.message, null);
      }
      await refresh();
    };

    if (!dialog.open) dialog.showModal();
    // "Keep it" is the form's first submit button, so Enter closes the dialogue safely wherever
    // focus sits. Focus goes to the box only when there is one to fill in.
    (unchecked ? input : $("releasing-keep")).focus();
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
      await receiveDrop(event.dataTransfer);
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
