// Review links (editor.html?share=TOKEN): a frozen revision with pinned comments,
// and for approvers, approve / request changes. Builds on window.ED (editor.js).
(() => {
  "use strict";
  const ED = window.ED;
  if (!ED) return;
  const { S, $, el, btn, api, post, download, toast } = ED;
  let token = null;
  let meta = null;
  let pendingPin = null;
  let pollTimer = 0;

  function enterReviewLayout() {
    S.readOnly = true;
    document.body.classList.add("reviewing");
    $("tabs").hidden = true;
    for (const id of ["pane-chat", "pane-tools", "pane-log"]) $(id).hidden = true;
    $("pane-review").hidden = false;
    $("top-actions").hidden = true;
    $("mode-tag").textContent = "Review";
    $("empty").hidden = true;
    $("canvas-wrap").hidden = false;
    $("area-btn").hidden = true;
    $("find-form").hidden = true;
    ED.resize();
  }

  async function start(t) {
    token = t;
    enterReviewLayout();
    ED.busy("Opening the review…");
    try {
      meta = await api(`/api/editor/shares/${encodeURIComponent(token)}`);
      const geo = await api(`/api/editor/shares/${encodeURIComponent(token)}/geometry`);
      S.session = { id: null, name: meta.name, digest: meta.digest, log: [] };
      S.hidden = new Set(meta.digest.layers.filter((l) => !l.on).map((l) => l.name));
      $("file-name").hidden = false;
      $("file-name").textContent = `${meta.name} · revision ${meta.rev}`;
      ED.busy("");
      ED.setScene(geo, true);
      render();
      await loadComments();
      pollTimer = setInterval(loadComments, 20000);
    } catch (err) {
      ED.busy("");
      $("review").replaceChildren(el("h2", "dlg-title", "This link doesn't work"), el("p", null, err.message));
    }
  }

  function render() {
    const box = $("review");
    box.replaceChildren();
    const approver = meta.role === "approver";
    box.append(el("h2", "review-title", meta.label ? `For ${meta.label}` : "Review"),
      el("p", "small muted", `${meta.name}, revision ${meta.rev}. Frozen copy made ${new Date(meta.created * 1000).toLocaleString()}; the link works until ${new Date(meta.expires * 1000).toLocaleDateString()}.`));
    const d = meta.decision;
    if (d) {
      box.append(el("p", "tag " + (d.decision === "approved" ? "tag-accent-2" : "tag-accent"),
        `${d.decision === "approved" ? "Approved" : "Changes requested"} by ${d.author} · ${new Date(d.time * 1000).toLocaleString()}${d.note ? " — " + d.note : ""}`));
    }
    const dl = el("div", "row tool-row");
    if (meta.allowDownload) dl.append(btn("Download DXF", "btn btn-secondary", () => download(`/api/editor/shares/${token}/drawing.dxf`, "drawing.dxf")));
    dl.append(btn("Download PDF", "btn btn-secondary", () => download(`/api/editor/shares/${token}/export.pdf`, "drawing.pdf")));
    box.append(dl);

    const name = el("input", "input");
    name.placeholder = "Your name";
    name.value = ED.myName();
    name.addEventListener("change", () => ED.local("bd-name", name.value.trim().slice(0, 40) || null));
    const text = el("textarea", "input");
    text.rows = 3;
    text.placeholder = "Add a comment…";
    const pinState = el("span", "small muted", "Not pinned to a spot.");
    const pinBtn = btn("Pin to a spot", "btn btn-ghost", () => {
      S.pinMode = !S.pinMode;
      pinBtn.setAttribute("aria-pressed", String(S.pinMode));
      pinState.textContent = S.pinMode ? "Click the drawing where your comment applies." : (pendingPin ? "Pinned." : "Not pinned to a spot.");
    });
    S.onPin = (x, y) => {
      pendingPin = { x, y };
      S.pinMode = false;
      pinBtn.setAttribute("aria-pressed", "false");
      pinState.textContent = "Pinned. It will show as a numbered marker.";
      S.pins = S.pins.filter((p) => !p.pending).concat([{ x, y, n: "+", pending: true, author: "you", text: "(new comment)" }]);
      ED.requestDraw();
    };
    const postBtn = btn("Post comment", "btn btn-primary", async () => {
      if (!text.value.trim()) return toast("Write a comment first.");
      try {
        await post(`/api/editor/shares/${token}/comments`, { text: text.value, author: name.value || ED.myName() || "Guest", x: pendingPin ? pendingPin.x : null, y: pendingPin ? pendingPin.y : null });
        text.value = "";
        pendingPin = null;
        pinState.textContent = "Not pinned to a spot.";
        await loadComments();
        toast("Comment posted.");
      } catch (e) { toast(e.message); }
    });
    box.append(el("h4", "tool-sub", "Comments"), field("Name", name), text, el("div", "row tool-row"));
    box.lastChild.append(pinBtn, postBtn);
    box.append(pinState);
    if (approver) {
      const note = el("textarea", "input");
      note.rows = 2;
      note.placeholder = "Note (optional): what needs to change, or conditions of approval";
      const decide = async (decision) => {
        if (!(name.value || ED.myName())) { toast("Enter your name first."); name.focus(); return; }
        try {
          await post(`/api/editor/shares/${token}/decision`, { decision, author: name.value || ED.myName(), note: note.value });
          meta = await api(`/api/editor/shares/${token}`);
          render();
          await loadComments();
          toast(decision === "approved" ? "Approved. The sender has been told." : "Sent. The sender has been told what to change.");
        } catch (e) { toast(e.message); }
      };
      box.append(el("h4", "tool-sub", "Your decision"), note, el("div", "row tool-row"));
      box.lastChild.append(btn("Approve this revision", "btn btn-primary", () => decide("approved")), btn("Request changes", "btn btn-secondary", () => decide("changes_requested")));
    }
    const list = el("div", "comments");
    list.id = "review-comments";
    box.append(list);
  }
  function field(label, input) {
    const l = el("label", "tool-field");
    l.append(el("span", "small", label), input);
    return l;
  }

  async function loadComments() {
    let comments = [];
    try { comments = (await api(`/api/editor/shares/${token}/comments`)).comments; } catch (e) { return; }
    const top = comments.filter((c) => !c.replyTo);
    S.pins = top.map((c, i) => ({ ...c, n: i + 1 })).concat(pendingPin ? [{ ...pendingPin, n: "+", pending: true, author: "you", text: "(new comment)" }] : []);
    ED.requestDraw();
    const list = $("review-comments");
    if (!list) return;
    list.replaceChildren();
    if (!top.length) list.append(el("p", "muted small", "No comments yet."));
    top.forEach((c, i) => {
      const d = el("div", "comment" + (c.resolved ? " resolved" : ""));
      d.id = "c-" + c.id;
      d.append(el("div", "c-head", `#${i + 1} ${c.author}${c.owner ? " (sender)" : ""} · ${new Date(c.time * 1000).toLocaleString()}${c.resolved ? " · resolved" : ""}`), el("p", null, c.text));
      for (const r of comments.filter((x) => x.replyTo === c.id)) d.append(el("p", "c-reply", `${r.author}${r.owner ? " (sender)" : ""}: ${r.text}`));
      const reply = el("input", "input");
      reply.placeholder = "Reply…";
      reply.addEventListener("keydown", async (e) => {
        if (e.key !== "Enter" || !reply.value.trim()) return;
        try {
          await post(`/api/editor/shares/${token}/comments`, { text: reply.value, author: ED.myName() || "Guest", reply_to: c.id });
          await loadComments();
        } catch (err) { toast(err.message); }
      });
      d.append(reply);
      list.append(d);
    });
  }

  ED.hooks.event.push((kind, d) => {
    if (kind !== "pin-click" || !token) return;
    const c = document.getElementById("c-" + d.id);
    if (c) { c.scrollIntoView({ block: "center" }); c.classList.add("flash"); setTimeout(() => c.classList.remove("flash"), 1500); }
  });
  window.addEventListener("beforeunload", () => clearInterval(pollTimer));
  window.EDReview = { start };
})();
