// The Tools tab: health and clean-up, analysis, compare, tables, exports, recipes,
// batch, sharing and review, search, and settings. Builds on window.ED (editor.js).
(() => {
  "use strict";
  const ED = window.ED;
  if (!ED) return;
  const { S, $, el, btn, fmt, api, post, download, toast } = ED;
  const DEL = "#c2410c", ADD = "#3d7a3a", CHG = "#b7791f";
  const root = $("tools");
  const sid = () => ED.sid();
  const enc = encodeURIComponent;

  function section(id, title, hint, build, open) {
    const d = el("details", "tool");
    d.id = "tool-" + id;
    if (open) d.open = true;
    const sum = el("summary");
    sum.append(el("span", "tool-title", title), el("span", "tool-hint muted small", hint));
    const body = el("div", "tool-body");
    d.append(sum, body);
    let built = false;
    const ensure = () => { if (!built && sid()) { built = true; build(body); } };
    d.addEventListener("toggle", () => { if (d.open) ensure(); });
    d._rebuild = () => { body.replaceChildren(); built = false; if (d.open) ensure(); };
    root.append(d);
    if (open) ensure();
    return d;
  }
  function field(label, input) {
    const l = el("label", "tool-field");
    l.append(el("span", "small", label), input);
    return l;
  }
  function input(type, value, attrs) {
    const i = el("input", "input");
    i.type = type;
    if (value != null) i.value = value;
    Object.assign(i, attrs || {});
    return i;
  }
  function select(options, value) {
    const s = el("select", "input");
    for (const [v, label] of options) {
      const o = el("option", null, label);
      o.value = v;
      if (String(v) === String(value)) o.selected = true;
      s.append(o);
    }
    return s;
  }
  function row(...children) {
    const r = el("div", "row tool-row");
    r.append(...children.filter(Boolean));
    return r;
  }
  const ask = (text) => { ED.selectTab("chat"); ED.send(text); };
  function filePick(accept, multiple, onFiles) {
    const i = input("file");
    i.accept = accept;
    i.multiple = !!multiple;
    i.hidden = true;
    i.addEventListener("change", () => { if (i.files.length) onFiles([...i.files]); i.value = ""; });
    return i;
  }

  // ── check and clean ────────────────────────────────────────────────────
  function buildHealth(body) {
    const out = el("div", "tool-out");
    const run = async () => {
      out.replaceChildren(el("p", "muted small", "Checking…"));
      try { out.replaceChildren(ED.healthEl(await api(`/api/editor/sessions/${sid()}/health`))); } catch (e) { out.replaceChildren(el("p", "warn", e.message)); }
    };
    body.append(
      row(btn("Run the health check", "btn btn-primary", run), btn("Clean up the drawing", "btn btn-secondary", () => ask("Clean up the drawing"))),
      row(btn("Standardize layer names", "btn btn-secondary", () => ask("standardize layers")), btn("Check spelling", "btn btn-secondary", () => ask("check spelling")),
          btn("Purge everything unused", "btn btn-secondary", () => ask("purge"))),
      out,
    );
    const map = el("textarea", "input");
    map.rows = 3;
    map.placeholder = "Your own layer mapping, one per line:  old,NEW   (or JSON)";
    body.append(el("p", "small muted", "Company layer standard? Paste a mapping and apply it:"), map,
      row(btn("Apply my mapping", "btn btn-secondary", async () => {
        if (!map.value.trim()) return toast("Paste a mapping first.");
        try {
          const r = await post(`/api/editor/sessions/${sid()}/standards/custom`, { mapping: map.value });
          ED.selectTab("chat");
          ED.addMsg("sys", "Proposed: apply your layer mapping");
          ED.showProposal(r.proposal);
        } catch (e) { toast(e.message); }
      })));
    run();
  }

  // ── understand ─────────────────────────────────────────────────────────
  function buildUnderstand(body) {
    const blockFilter = input("text", "", { placeholder: "block name filter, e.g. DOOR*" });
    const minAisle = input("number", "2.8", { step: "0.1", min: "0.5" });
    body.append(
      row(btn("Explain this drawing", "btn btn-secondary", () => ask("Explain this drawing")), btn("Quantity take-off", "btn btn-secondary", () => ask("quantity takeoff"))),
      row(btn("Rooms and areas", "btn btn-secondary", () => ask("rooms")), btn("Door schedule", "btn btn-secondary", () => ask("door schedule")), btn("Window schedule", "btn btn-secondary", () => ask("window schedule"))),
      field("Schedule or bill of materials for blocks named", blockFilter),
      row(btn("Schedule", "btn btn-ghost", () => ask(blockFilter.value.trim() ? `schedule for ${blockFilter.value.trim()}` : "block schedule")),
          btn("Bill of materials", "btn btn-ghost", () => ask(blockFilter.value.trim() ? `BOM for ${blockFilter.value.trim()}` : "BOM"))),
      field("Warehouse: minimum aisle width (m)", minAisle),
      row(btn("Check rack rows and aisles", "btn btn-secondary", () => ask(`check the aisles against ${Number(minAisle.value) || 2.8} m`))),
      row(btn("What is selected?", "btn btn-ghost", () => ask("what is this?")), btn("What's in the marked area?", "btn btn-ghost", () => ask("what's in this area?"))),
    );
  }

  // ── compare ────────────────────────────────────────────────────────────
  function compareOverlay(r) {
    S.compare = {
      removed: (r.overlay.removed || []).map((it) => ED.prep({ ...it, l: "" })),
      added: new Set(r.overlay.added || []),
      changed: new Set(r.overlay.changed || []),
    };
    ED.showLegend("compare");
    // bring the changes into view: the changed areas, or the whole drawing
    const rs = r.regions || [];
    const box = rs.length ? rs.reduce((a, b) => [Math.min(a[0], b[0]), Math.min(a[1], b[1]), Math.max(a[2], b[2]), Math.max(a[3], b[3])]) : null;
    if (box) ED.fitTo(box, 0.25); else ED.fitView();
    ED.requestDraw();
  }
  function clearCompare() { S.compare = null; ED.showLegend(null); ED.requestDraw(); }
  $("legend-close").addEventListener("click", clearCompare);
  ED.hooks.draw.push((ctx, h) => {
    const c = S.compare;
    if (!c) return;
    h.strokeItems(c.removed, DEL, 2, [6, 4], 0.7);
    h.strokeItems(h.itemsOf(c.added), ADD, 2.2);
    h.strokeItems(h.itemsOf(c.changed), CHG, 2.2);
  });
  function buildCompare(body) {
    const out = el("div", "tool-out");
    const show = (r) => {
      out.replaceChildren();
      const c = r.counts;
      out.append(el("p", null, r.summary),
        el("p", "small muted", `Against ${r.other}: ${fmt(c.added)} added · ${fmt(c.removed)} removed · ${fmt(c.changed)} changed · ${fmt(c.unchanged)} the same.`));
      if (r.layers.length) out.append(ED.tableEl(["Layer", "Added", "Removed", "Changed"], r.layers.map((x) => [x.layer, x.added, x.removed, x.changed]), 8));
      const rev = input("text", "A", { maxLength: 8, size: 3 });
      const desc = input("text", "Revised as marked", { maxLength: 200 });
      out.append(row(btn("Show on drawing", "btn btn-secondary", () => compareOverlay(r)), btn("Hide", "btn btn-ghost", clearCompare), btn("Download report", "btn btn-ghost", () => download(r.reportUrl, "comparison.txt"))));
      if (r.regions && r.regions.length && !S.readOnly) {
        out.append(el("p", "small muted", `Mark the ${r.regions.length} changed area${r.regions.length === 1 ? "" : "s"} with revision clouds and add a revision-table row:`),
          row(field("Revision", rev), field("Description", desc)),
          row(btn("Draw revision clouds", "btn btn-primary", async () => {
            try {
              const p = await post(`/api/editor/sessions/${sid()}/compare/clouds`, { rev: rev.value || "A", description: desc.value });
              ED.selectTab("chat");
              ED.addMsg("sys", `Proposed: revision ${rev.value || "A"} clouds`);
              ED.showProposal(p.proposal);
            } catch (e) { toast(e.message); }
          })));
      }
      compareOverlay(r);
    };
    const pick = filePick(".dwg,.dxf", false, async ([f]) => {
      out.replaceChildren(el("p", "muted small", `Comparing with ${f.name}…`));
      const fd = new FormData();
      fd.append("file", f, f.name);
      try { show(await api(`/api/editor/sessions/${sid()}/compare`, { method: "POST", body: fd })); } catch (e) { out.replaceChildren(el("p", "warn", e.message)); }
    });
    body.append(pick,
      row(btn("What changed since I opened it", "btn btn-primary", async () => {
        out.replaceChildren(el("p", "muted small", "Comparing…"));
        try { show(await post(`/api/editor/sessions/${sid()}/compare/original`)); } catch (e) { out.replaceChildren(el("p", "warn", e.message)); }
      }), btn("Compare with another revision…", "btn btn-secondary", () => pick.click())),
      el("p", "small muted", "Older revision vs the open drawing: dashed red is gone, green is new, amber changed."),
      out);
  }

  // ── tables: draw one, or fill a title block ───────────────────────────
  function buildTable(body) {
    const out = el("div", "tool-out");
    const pick = filePick(".csv,.xlsx,.txt", false, async ([f]) => {
      const fd = new FormData();
      fd.append("file", f, f.name);
      try {
        const t = await api("/api/editor/parse-table", { method: "POST", body: fd });
        out.replaceChildren(el("p", "small", `${f.name}: ${t.columns.length} columns, ${fmt(t.count)} rows.`), ED.tableEl(t.columns, t.rows, 6));
        const rowSel = el("select", "input");
        t.rows.slice(0, 500).forEach((r, i) => { const o = el("option", null, `Row ${i + 1}: ${r.slice(0, 3).join(" · ")}`); o.value = String(i); rowSel.append(o); });
        out.append(row(btn("Draw it as a table", "btn btn-primary", () => {
          const e = S.scene && S.scene.extents;
          const th = e ? Math.max((e[2] - e[0]) / 250, 1e-3) : 2.5;
          const x = e ? e[2] + th * 6 : 0, y = e ? e[3] : 0;
          ED.stageOps([{ op: "add_table", x, y, rows: [t.columns].concat(t.rows.slice(0, 499)), title: f.name.replace(/\.[^.]+$/, "") }], `table from ${f.name}`);
        })),
        el("p", "small muted", "Or fill a title block: the column names are matched to block attribute tags (and {{TAG}} placeholders in text)."),
        field("Use the values from", rowSel),
        row(btn("Fill the title block", "btn btn-secondary", () => {
          const r = t.rows[Number(rowSel.value)] || [];
          const values = {};
          t.columns.forEach((c, i) => { if (c && r[i] !== undefined) values[c] = r[i]; });
          ED.stageOps([{ op: "fill_attributes", values }], "fill the title block");
        })));
      } catch (e) { out.replaceChildren(el("p", "warn", e.message)); }
    });
    body.append(pick, row(btn("Choose a CSV or Excel file…", "btn btn-secondary", () => pick.click())), out);
  }

  // ── export ─────────────────────────────────────────────────────────────
  function buildExport(body) {
    const paper = select([["A4", "A4"], ["A3", "A3"], ["A2", "A2"], ["A1", "A1"], ["A0", "A0"], ["LETTER", "Letter"], ["TABLOID", "Tabloid"]], "A3");
    const orient = select([["landscape", "Landscape"], ["portrait", "Portrait"]], "landscape");
    const title = input("text", "", { placeholder: "Title (defaults to the file name)" });
    const project = input("text", "", { placeholder: "Project" });
    const drawn = input("text", ED.myName(), { placeholder: "Drawn by" });
    const rev = input("text", "", { placeholder: "Rev", maxLength: 8, size: 3 });
    const hiddenQs = () => [...S.hidden].map((l) => "&hidden=" + enc(l)).join("");
    body.append(
      row(field("Paper", paper), field("Orientation", orient)),
      row(field("Title", title), field("Project", project)),
      row(field("Drawn by", drawn), field("Rev", rev)),
      row(btn("Download PDF", "btn btn-primary", () => download(`/api/editor/sessions/${sid()}/export.pdf?paper=${paper.value}&orientation=${orient.value}&title=${enc(title.value)}&project=${enc(project.value)}&drawn_by=${enc(drawn.value)}&rev=${enc(rev.value)}${hiddenQs()}`, "drawing.pdf")),
          btn("Download SVG", "btn btn-secondary", () => download(`/api/editor/sessions/${sid()}/export.svg?x=1${hiddenQs()}`, "drawing.svg"))),
      el("p", "small muted", "Layers hidden in the viewer are left off the PDF and SVG. The PDF is drawn to the next standard scale with a title block and scale bar."),
      row(btn("Change log (PDF)", "btn btn-ghost", () => download(`/api/editor/sessions/${sid()}/changelog.pdf`, "changes.pdf")),
          btn("Change log (text)", "btn btn-ghost", () => download(`/api/editor/sessions/${sid()}/changelog.txt`, "changes.txt"))),
      row(btn("Proof pack (.zip)", "btn btn-secondary", () => download(`/api/editor/sessions/${sid()}/proof-pack.zip`, "proof_pack.zip"))),
      el("p", "small muted", "The proof pack holds the original file, the edited DXF, the exact operations, a comparison, health before and after, PDFs and SHA-256 checksums."),
    );
  }

  // ── recipes ────────────────────────────────────────────────────────────
  function recipes() { try { return JSON.parse(ED.local("bd-recipes") || "[]"); } catch (e) { return []; } }
  function saveRecipes(list) { ED.local("bd-recipes", JSON.stringify(list.slice(0, 50))); renderRecipeLists(); }
  const recipeListeners = [];
  function renderRecipeLists() { recipeListeners.forEach((fn) => fn()); }
  function buildRecipes(body) {
    const list = el("ul", "tool-list");
    const render = () => {
      list.replaceChildren();
      const all = recipes();
      if (!all.length) list.append(el("li", "muted small", "No saved recipes yet. Make some changes, then record them, or import a recipe file."));
      all.forEach((r, i) => {
        const li = el("li");
        const n = (r.commands || []).length + (r.ops || []).length + (r.steps || []).length;
        li.append(el("strong", null, r.name), el("span", "muted small", ` ${n} step${n === 1 ? "" : "s"}`));
        li.append(row(
          btn("Apply here", "btn btn-ghost", async () => {
            try {
              const p = await post(`/api/editor/sessions/${sid()}/recipe/apply`, { recipe: r });
              ED.selectTab("chat");
              if (!p.proposal) { ED.addMsg("bot", `Recipe “${r.name}”: nothing to do in this drawing.` + (p.message ? " " + p.message.replace(/^There's nothing to change: /, "") : ""), "Built-in"); return; }
              ED.addMsg("sys", `Recipe: ${r.name}` + (p.truncated ? " (first 8 steps)" : ""));
              ED.showProposal(p.proposal);
            } catch (e) { toast(e.message); }
          }),
          btn("Download", "btn btn-ghost", () => {
            const url = URL.createObjectURL(new Blob([JSON.stringify(r, null, 2)], { type: "application/json" }));
            const a = document.createElement("a");
            a.href = url; a.download = (r.name || "recipe").replace(/[^\w\- ]+/g, "") + ".json";
            document.body.append(a); a.click(); a.remove();
          }),
          btn("Delete", "btn btn-ghost", () => { const all2 = recipes(); all2.splice(i, 1); saveRecipes(all2); }),
        ));
        list.append(li);
      });
    };
    recipeListeners.push(render);
    const pick = filePick(".json", false, async ([f]) => {
      try {
        const v = await post("/api/editor/recipes/validate", { recipe: JSON.parse(await f.text()) });
        saveRecipes(recipes().concat([v.recipe]));
        toast(`Recipe “${v.recipe.name}” saved.`);
      } catch (e) { toast(e.message || "That isn't a recipe file."); }
    });
    const cmds = el("textarea", "input");
    cmds.rows = 3;
    cmds.placeholder = "Or write one, a command per line:\npurge unused layers\ndelete duplicates\nstandardize layers";
    const name = input("text", "", { placeholder: "Recipe name" });
    body.append(
      row(btn("Record this session's changes", "btn btn-primary", async () => {
        try {
          const r = await api(`/api/editor/sessions/${sid()}/recipe`);
          const nm = window.prompt("Name this recipe:", r.name) || r.name;
          saveRecipes(recipes().concat([{ ...r, name: nm.slice(0, 120) }]));
          toast("Recipe saved in this browser.");
        } catch (e) { toast(e.message); }
      }), btn("Import a recipe file…", "btn btn-secondary", () => pick.click())),
      pick, row(field("Name", name)), cmds,
      row(btn("Save these commands", "btn btn-secondary", async () => {
        const lines = cmds.value.split("\n").map((s) => s.trim()).filter(Boolean);
        if (!lines.length) return toast("Write at least one command.");
        try {
          const v = await post("/api/editor/recipes/validate", { recipe: { name: name.value || "My recipe", commands: lines } });
          saveRecipes(recipes().concat([v.recipe]));
          cmds.value = "";
        } catch (e) { toast(e.message); }
      })),
      list);
    render();
  }

  // ── batch ──────────────────────────────────────────────────────────────
  function buildBatch(body) {
    let files = [];
    const chosen = el("p", "small muted", "No drawings chosen.");
    const recipeSel = el("select", "input");
    const fillRecipes = () => {
      recipeSel.replaceChildren();
      const all = recipes();
      if (!all.length) recipeSel.append(Object.assign(el("option", null, "Save a recipe first (Recipes, above)"), { value: "" }));
      all.forEach((r, i) => { const o = el("option", null, r.name); o.value = String(i); recipeSel.append(o); });
    };
    recipeListeners.push(fillRecipes);
    fillRecipes();
    const fmtSel = select([["DXF", "DXF"], ["DWG", "DWG"]], "DXF");
    const target = select([["", "Same version"], ["2018", "2018+"], ["2013", "2013"], ["2010", "2010"], ["2007", "2007"], ["2004", "2004"], ["2000", "2000"]], "");
    const dry = input("checkbox");
    const out = el("div", "tool-out");
    const pick = filePick(".dwg,.dxf", true, (fs) => { files = fs.slice(0, 200); chosen.textContent = `${files.length} drawing${files.length === 1 ? "" : "s"}: ${files.slice(0, 5).map((f) => f.name).join(", ")}${files.length > 5 ? "…" : ""}`; });
    const dryLabel = el("label", "small");
    dryLabel.append(dry, document.createTextNode(" Dry run (report only, change nothing)"));
    body.append(pick, row(btn("Choose drawings…", "btn btn-secondary", () => pick.click())), chosen,
      field("Recipe", recipeSel), row(field("Save as", fmtSel), field("Version", target)), dryLabel,
      row(btn("Run the batch", "btn btn-primary", async () => {
        const r = recipes()[Number(recipeSel.value)];
        if (!files.length) return toast("Choose some drawings first.");
        if (!r) return toast("Choose a recipe.");
        const recipe = { ...r, output: { format: fmtSel.value, target: target.value ? Number(target.value) : null } };
        const fd = new FormData();
        files.forEach((f) => fd.append("files", f, f.name));
        fd.append("recipe", JSON.stringify(recipe));
        fd.append("dry_run", dry.checked ? "true" : "false");
        out.replaceChildren(el("p", "muted small", "Uploading…"));
        try {
          let job = await api("/api/editor/batch", { method: "POST", body: fd });
          for (;;) {
            out.replaceChildren(el("p", "small", `${job.status}: ${job.done} of ${job.files} done`));
            if (job.status === "done" || job.status === "failed") break;
            await new Promise((res) => setTimeout(res, 700));
            job = await api(`/api/editor/batch/${job.id}`);
          }
          out.replaceChildren(ED.tableEl(["File", "Result", "Changes", "Health"], job.results.map((x) => [x.file, x.status + (x.errors && x.errors.length ? ` (${x.errors[0]})` : ""), x.changes || 0, x.healthBefore != null ? `${x.healthBefore} → ${x.healthAfter}` : "–"]), 20));
          out.append(row(job.downloadUrl ? btn("Download results (.zip)", "btn btn-primary", () => download(job.downloadUrl, "batch.zip")) : null,
                         job.reportUrl ? btn("Report", "btn btn-ghost", () => download(job.reportUrl, "batch_report.txt")) : null));
        } catch (e) { out.replaceChildren(el("p", "warn", e.message)); }
      })), out);
  }

  // ── share and review ───────────────────────────────────────────────────
  async function loadPins() {
    if (!sid()) return;
    try {
      const r = await api(`/api/editor/sessions/${sid()}/comments`);
      S.pins = r.comments.filter((c) => !c.replyTo).map((c, i) => ({ ...c, n: i + 1 }));
      S.allComments = r.comments;
      ED.requestDraw();
      renderComments();
    } catch (e) { /* comments are a nicety */ }
  }
  let commentsBox = null;
  function renderComments() {
    if (!commentsBox) return;
    commentsBox.replaceChildren();
    const all = S.allComments || [];
    if (!all.length) { commentsBox.append(el("p", "muted small", "No comments yet. Comments left on your review links appear here and as pins on the drawing.")); return; }
    for (const pin of S.pins) {
      const li = el("div", "comment" + (pin.resolved ? " resolved" : ""));
      li.id = "c-" + pin.id;
      li.append(el("div", "c-head", `#${pin.n} ${pin.author} · ${pin.label || pin.role} · ${new Date(pin.time * 1000).toLocaleString()}`), el("p", null, pin.text));
      for (const r of all.filter((c) => c.replyTo === pin.id)) li.append(el("p", "c-reply", `${r.author}${r.owner ? " (you)" : ""}: ${r.text}`));
      const reply = input("text", "", { placeholder: "Reply…" });
      li.append(row(
        pin.x != null ? btn("Show", "btn btn-ghost", () => zoomTo(pin.x, pin.y)) : null,
        btn(pin.resolved ? "Reopen" : "Resolve", "btn btn-ghost", async () => { await post(`/api/editor/sessions/${sid()}/shares/${pin.token}/comments/${pin.id}/resolve`, { resolved: !pin.resolved }); loadPins(); }),
      ), reply);
      reply.addEventListener("keydown", async (e) => {
        if (e.key !== "Enter" || !reply.value.trim()) return;
        await post(`/api/editor/sessions/${sid()}/shares/${pin.token}/comments`, { text: reply.value, reply_to: pin.id, author: ED.myName() || "Owner" });
        loadPins();
      });
      commentsBox.append(li);
    }
  }
  function zoomTo(x, y) {
    const e = S.scene && S.scene.extents;
    const span = e ? Math.max(e[2] - e[0], e[3] - e[1]) / 8 : 10;
    ED.fitTo([x - span, y - span, x + span, y + span], 0);
  }
  function buildShare(body) {
    const role = select([["viewer", "Viewer: see and comment"], ["approver", "Approver: comment and approve"], ["editor", "Editor: edit live with you"]], "approver");
    const label = input("text", "", { placeholder: "Who is it for? (e.g. Client – Acme)" });
    const result = el("div", "tool-out");
    const list = el("div", "tool-out");
    commentsBox = el("div", "comments");
    const refresh = async () => {
      try {
        const r = await api(`/api/editor/sessions/${sid()}/shares`);
        list.replaceChildren();
        if (!r.shares.length) { list.append(el("p", "muted small", "No links yet.")); }
        for (const sh of r.shares) {
          const d = sh.decision;
          const line = el("div", "share-line");
          line.append(el("strong", null, `${sh.role}${sh.label ? " · " + sh.label : ""}`),
            el("span", "small muted", sh.role === "editor" ? " live session" : ` revision ${sh.rev} · ${sh.comments} comment${sh.comments === 1 ? "" : "s"}${sh.open ? ` (${sh.open} open)` : ""}`));
          if (d) line.append(el("span", "tag " + (d.decision === "approved" ? "tag-accent-2" : "tag-accent"), d.decision === "approved" ? `Approved by ${d.author}` : `Changes requested by ${d.author}`));
          line.append(row(btn("Copy link", "btn btn-ghost", () => copyLink(sh.role === "editor" ? `join=${sh.token}` : `share=${sh.token}`)),
            btn("Revoke", "btn btn-ghost", async () => { await api(`/api/editor/sessions/${sid()}/shares/${sh.token}`, { method: "DELETE" }); refresh(); })));
          list.append(line);
        }
      } catch (e) { list.replaceChildren(el("p", "warn", e.message)); }
      loadPins();
    };
    body.append(field("Who can do what", role), field("Label", label),
      row(btn("Create link", "btn btn-primary", async () => {
        try {
          const r = await post(`/api/editor/sessions/${sid()}/shares`, { role: role.value, label: label.value });
          const url = new URL(r.link, location.href).href;
          result.replaceChildren(el("p", "small", r.role === "editor" ? "Anyone with this link edits this drawing with you, live:" : `A frozen copy of revision ${r.rev}. Anyone with this link can ${r.role === "approver" ? "comment and approve" : "view and comment"}:`),
            Object.assign(input("text", url, { readOnly: true }), { className: "input share-url" }), row(btn("Copy", "btn btn-secondary", () => copyLink(null, url))));
          refresh();
        } catch (e) { toast(e.message); }
      })), result, el("h4", "tool-sub", "Your links"), list, el("h4", "tool-sub", "Comments"), commentsBox);
    refresh();
  }
  function copyLink(q, full) {
    const url = full || new URL(`editor.html?${q}`, location.href).href;
    (navigator.clipboard ? navigator.clipboard.writeText(url) : Promise.reject()).then(() => toast("Link copied."), () => window.prompt("Copy this link:", url));
  }

  // ── search ─────────────────────────────────────────────────────────────
  function buildSearch(body) {
    const q = input("search", "", { placeholder: "Search all your drawings: text, layer, block, name" });
    const out = el("div", "tool-out");
    const sim = el("div", "tool-out");
    const run = async () => {
      if (!q.value.trim()) return;
      try {
        const r = await api(`/api/editor/search?q=${enc(q.value.trim())}`);
        out.replaceChildren();
        if (!r.results.length) out.append(el("p", "muted small", "No drawings in your workspace match."));
        for (const h of r.results) {
          const d = el("div", "share-line");
          d.append(el("strong", null, h.name), el("span", "small muted", ` last opened ${h.last ? new Date(h.last * 1000).toLocaleDateString() : "?"}`));
          d.append(el("p", "small", h.matches.slice(0, 4).map((m) => `${m.kind}: ${m.value}`).join(" · ")));
          out.append(d);
        }
      } catch (e) { out.replaceChildren(el("p", "warn", e.message)); }
    };
    q.addEventListener("keydown", (e) => { if (e.key === "Enter") run(); });
    body.append(row(q, btn("Search", "btn btn-secondary", run)), out,
      el("p", "small muted", "Only drawings opened with your workspace key are searched; the files themselves aren't kept, so open one again to edit it."),
      row(btn("Find drawings similar to this one", "btn btn-ghost", async () => {
        try {
          const r = await api(`/api/editor/sessions/${sid()}/similar`);
          sim.replaceChildren();
          if (!r.results.length) sim.append(el("p", "muted small", r.note || "Nothing similar in your workspace yet."));
          for (const x of r.results) sim.append(el("p", "small", `${x.name}: ${Math.round(x.similarity * 100)}% alike (shares ${x.shared.slice(0, 4).map((s) => s.split(":")[1]).join(", ")})`));
        } catch (e) { sim.replaceChildren(el("p", "warn", e.message)); }
      })), sim);
  }

  // ── settings ───────────────────────────────────────────────────────────
  function buildSettings(body) {
    const name = input("text", ED.myName(), { placeholder: "Your name", maxLength: 40 });
    name.addEventListener("change", () => { ED.local("bd-name", name.value.trim().slice(0, 40) || null); toast("Name saved."); });
    const ws = input("text", ED.workspaceKey(), { readOnly: true });
    const team = input("text", "", { placeholder: "Paste a team's workspace key" });
    const key = input("password", ED.accessKey(), { placeholder: "Access key (if your server needs one)" });
    key.addEventListener("change", () => { ED.local("bd-access-key", key.value.trim() || null); toast("Access key saved."); usage(); });
    const use = el("p", "small muted");
    const usage = async () => {
      try {
        const u = await api("/api/editor/usage");
        use.textContent = `AI today: ${u.used} of ${u.daily} used${u.name ? ` (key: ${u.name})` : ""}. Built-in commands are unlimited.`;
      } catch (e) { use.textContent = e.message; }
    };
    body.append(field("Your name (shown on comments and to people editing with you)", name),
      field("Your workspace key: what this browser remembers and can search", ws),
      row(btn("Copy key", "btn btn-ghost", () => copyLink(null, ws.value)), btn("New private key", "btn btn-ghost", () => {
        if (!window.confirm("Start a new, empty workspace? Memory and search from the old key won't show here any more.")) return;
        ED.local("bd-workspace", ED.randomId(24)); ws.value = ED.workspaceKey();
      })),
      row(team, btn("Use team key", "btn btn-secondary", () => {
        if (!/^[A-Za-z0-9_-]{16,128}$/.test(team.value.trim())) return toast("That isn't a workspace key (16+ letters, digits, - or _).");
        ED.local("bd-workspace", team.value.trim()); ws.value = ED.workspaceKey(); team.value = ""; toast("Using the team's workspace from now on.");
      })),
      field("Access key", key), use,
      row(btn("Forget this drawing's memory", "btn btn-ghost", async () => {
        try { await api(`/api/editor/sessions/${sid()}/memory`, { method: "DELETE" }); toast("Forgotten."); } catch (e) { toast(e.message); }
      })));
    usage();
  }

  // ── build the tab ──────────────────────────────────────────────────────
  let sections = [];
  function buildAll() {
    root.replaceChildren();
    if (!sid()) { root.append(el("p", "muted small tool-empty", "Open a drawing to use the tools.")); return; }
    sections = [
      section("health", "Check and clean", "health check, clean-up, layer standards, spelling", buildHealth, true),
      section("understand", "Understand", "explain, take-off, rooms, schedules, BOM, aisles", buildUnderstand),
      section("compare", "Compare revisions", "what changed, revision clouds", buildCompare),
      section("table", "Tables and title blocks", "CSV or Excel into the drawing", buildTable),
      section("export", "Export", "PDF, SVG, change log, proof pack", buildExport),
      section("recipes", "Recipes", "record and replay changes", buildRecipes),
      section("batch", "Batch", "a recipe on many drawings", buildBatch),
      section("share", "Share and review", "links, comments, approval, live editing", buildShare),
      section("search", "Search", "all your drawings, similar ones", buildSearch),
      section("settings", "Settings", "name, workspace, access key, memory", buildSettings),
    ];
  }
  ED.hooks.open.push(() => { S.compare = null; buildAll(); loadPins(); });
  ED.hooks.close.push(() => { S.compare = null; buildAll(); });
  ED.hooks.adopt.push(() => {
    const h = $("tool-health");
    if (h && h.open && h._rebuild) h._rebuild();
  });
  ED.hooks.event.push((kind, d) => {
    if (kind === "comment") { toast(`New comment from ${d.comment.author}${d.label ? " (" + d.label + ")" : ""}: ${d.comment.text.slice(0, 80)}`); loadPins(); }
    if (kind === "decision") { toast(`${d.decision.author} ${d.decision.decision === "approved" ? "approved" : "requested changes to"} revision ${d.decision.rev}${d.label ? " (" + d.label + ")" : ""}.`); const s = $("tool-share"); if (s && s._rebuild) s._rebuild(); }
    if (kind === "pin-click") {
      ED.selectTab("tools");
      const s = $("tool-share");
      if (s) { s.open = true; setTimeout(() => { const c = document.getElementById("c-" + d.id); if (c) { c.scrollIntoView({ block: "center" }); c.classList.add("flash"); setTimeout(() => c.classList.remove("flash"), 1500); } }, 50); }
    }
  });
  buildAll();
  window.EDTools = { recipes, saveRecipes, loadPins };
})();
