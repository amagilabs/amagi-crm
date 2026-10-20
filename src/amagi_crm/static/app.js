// amagi-crm web UI. Plain JavaScript, no build step.
// Every value from the API is inserted with textContent (via h()), never innerHTML.
"use strict";

const state = { token: sessionStorage.getItem("amagi-crm-token"), me: null, stages: [], contacts: [], taskFilter: "open" };
const $ = (sel) => document.querySelector(sel);

function h(tag, attrs = {}, ...children) {
  const el = document.createElement(tag);
  for (const [k, v] of Object.entries(attrs)) {
    if (v === false || v === null || v === undefined) continue;
    if (k.startsWith("on")) el.addEventListener(k.slice(2), v);
    else if (k === "dataset") Object.assign(el.dataset, v);
    else if (v === true) el.setAttribute(k, "");
    else el.setAttribute(k, v);
  }
  for (const c of children.flat()) {
    if (c === null || c === undefined || c === false) continue;
    el.append(c instanceof Node ? c : document.createTextNode(String(c)));
  }
  return el;
}

async function api(path, opts = {}) {
  const res = await fetch(path, {
    method: opts.method || "GET",
    headers: { Authorization: "Bearer " + state.token, ...(opts.body ? { "Content-Type": "application/json" } : {}) },
    body: opts.body ? JSON.stringify(opts.body) : undefined,
  });
  if (res.status === 401) { signOut(); throw new Error("Your session has ended. Sign in again."); }
  if (res.status === 204) return null;
  const data = await res.json().catch(() => ({}));
  if (!res.ok) {
    const detail = Array.isArray(data.detail) ? data.detail.map((d) => d.msg).join("; ") : data.detail;
    throw new Error(detail || ("Request failed (" + res.status + ")"));
  }
  return data;
}

const money = (cents) => (cents / 100).toLocaleString(undefined, { style: "currency", currency: "USD", maximumFractionDigits: 0 });
const contactName = (c) => [c.first_name, c.last_name].filter(Boolean).join(" ") || c.email || c.company || ("Contact #" + c.id);
const canWrite = () => state.me && state.me.role !== "readonly";
const todayISO = () => { const d = new Date(); d.setMinutes(d.getMinutes() - d.getTimezoneOffset()); return d.toISOString().slice(0, 10); };
const fmtTime = (iso) => iso.slice(11, 16);
const fmtWhen = (iso) => iso.slice(0, 10) + " " + iso.slice(11, 16);

// --- sign in -----------------------------------------------------------------

async function start() {
  if (!state.token) return showSignIn();
  try {
    state.me = await api("/api/me");
  } catch (e) {
    return showSignIn(e.message);
  }
  $("#signin").hidden = true;
  $("#app").hidden = false;
  $("#whoami").textContent = state.me.name + " · " + state.me.role + " · " + state.me.timezone;
  document.querySelectorAll("[data-admin]").forEach((el) => (el.hidden = state.me.role !== "admin"));
  document.querySelectorAll("[data-write]").forEach((el) => (el.hidden = !canWrite()));
  state.stages = await api("/api/stages");
  route();
}

function showSignIn(message) {
  $("#app").hidden = true;
  $("#signin").hidden = false;
  $("#signin-error").textContent = message || "";
  $("#token").focus();
}

function signOut() {
  sessionStorage.removeItem("amagi-crm-token");
  state.token = null;
  state.me = null;
  showSignIn();
}

$("#signin-form").addEventListener("submit", (ev) => {
  ev.preventDefault();
  state.token = $("#token").value.trim();
  sessionStorage.setItem("amagi-crm-token", state.token);
  $("#token").value = "";
  start();
});
$("#signout").addEventListener("click", signOut);

// --- routing -----------------------------------------------------------------

const loaders = { contacts: loadContacts, pipeline: loadPipeline, tasks: loadTasks, calendar: loadCalendar, audit: loadAudit };

function route() {
  if (!state.me) return;
  let tab = location.hash.slice(1) || "contacts";
  if (!loaders[tab] || (tab === "audit" && state.me.role !== "admin")) tab = "contacts";
  document.querySelectorAll(".tab").forEach((s) => (s.hidden = s.id !== "tab-" + tab));
  document.querySelectorAll(".bar nav a").forEach((a) => a.toggleAttribute("aria-current", a.dataset.tab === tab));
  document.querySelector(".bar nav a[data-tab='" + tab + "']").setAttribute("aria-current", "page");
  loaders[tab]().catch((e) => alert(e.message));
}
window.addEventListener("hashchange", route);

// --- dialog ------------------------------------------------------------------

function openDialog({ title, fields, onSave, onDelete, saveLabel = "Save" }) {
  const dlg = $("#dialog");
  $("#dialog-title").textContent = title;
  const body = $("#dialog-body");
  body.replaceChildren(...fields);
  $("#dialog-error").textContent = "";
  $("#dialog-save").textContent = saveLabel;
  $("#dialog-save").hidden = !onSave;
  const del = $("#dialog-delete");
  del.hidden = !onDelete;
  del.onclick = onDelete ? async () => {
    if (!confirm("Delete this record? This can't be undone.")) return;
    try { await onDelete(); dlg.close(); } catch (e) { $("#dialog-error").textContent = e.message; }
  } : null;
  $("#dialog-form").onsubmit = async (ev) => {
    ev.preventDefault();
    try { await onSave(new FormData($("#dialog-form"))); dlg.close(); } catch (e) { $("#dialog-error").textContent = e.message; }
  };
  $("#dialog-cancel").onclick = () => dlg.close();
  dlg.showModal();
  const first = body.querySelector("input, select, textarea");
  if (first) first.focus();
}

function field(label, name, value = "", type = "text", extra = {}) {
  const id = "f-" + name;
  const input = type === "textarea"
    ? h("textarea", { id, name, ...extra }, value || "")
    : h("input", { id, name, type, value: value ?? "", ...extra });
  return h("div", {}, h("label", { for: id }, label), input);
}

function selectField(label, name, options, selected) {
  const id = "f-" + name;
  return h("div", {}, h("label", { for: id }, label),
    h("select", { id, name }, options.map(([v, text]) => h("option", { value: v, selected: String(v) === String(selected ?? "") }, text))));
}

const formObj = (fd, keys) => Object.fromEntries(keys.map((k) => [k, (fd.get(k) ?? "").toString().trim()]));
const orNull = (v) => (v === "" ? null : v);

async function contactOptions() {
  const data = await api("/api/contacts?limit=1000");
  state.contacts = data.items;
  return [["", "No contact"], ...data.items.map((c) => [c.id, contactName(c)])];
}

// --- contacts ----------------------------------------------------------------

let searchTimer;
$("#contact-search").addEventListener("input", () => { clearTimeout(searchTimer); searchTimer = setTimeout(loadContacts, 200); });
$("#add-contact").addEventListener("click", () => editContact(null));

async function loadContacts() {
  const q = $("#contact-search").value.trim();
  const data = await api("/api/contacts?limit=500" + (q ? "&search=" + encodeURIComponent(q) : ""));
  $("#contact-count").textContent = data.total + (data.total === 1 ? " contact" : " contacts") + (q ? " match" : "");
  const rows = data.items.map((c) => h("tr", { class: "clickable", tabindex: "0", onclick: () => editContact(c.id), onkeydown: (e) => { if (e.key === "Enter") editContact(c.id); } },
    h("td", {}, h("strong", {}, contactName(c))), h("td", {}, c.company || ""), h("td", {}, c.email || ""),
    h("td", {}, c.phone || ""), h("td", {}, (c.tags || []).map((t) => h("span", { class: "tag" }, t)))));
  $("#contact-rows").replaceChildren(...(rows.length ? rows : [h("tr", {}, h("td", { colspan: "5", class: "empty" }, q ? "No contacts match that search." : "No contacts yet. Add one, or run amagi-crm import contacts.csv."))]));
}

async function editContact(id) {
  const c = id ? await api("/api/contacts/" + id) : {};
  const keys = ["first_name", "last_name", "email", "phone", "company", "tags", "notes"];
  const ro = canWrite() ? {} : { readonly: true };
  openDialog({
    title: id ? contactName(c) : "New contact",
    fields: [
      h("div", { class: "row2" }, field("First name", "first_name", c.first_name, "text", ro), field("Last name", "last_name", c.last_name, "text", ro)),
      h("div", { class: "row2" }, field("Email", "email", c.email, "email", ro), field("Phone", "phone", c.phone, "tel", ro)),
      field("Company", "company", c.company, "text", ro),
      field("Tags (comma separated)", "tags", (c.tags || []).join(", "), "text", ro),
      field("Notes", "notes", c.notes, "textarea", ro),
    ],
    onSave: canWrite() ? async (fd) => {
      const body = formObj(fd, keys);
      body.tags = body.tags ? body.tags.split(",").map((t) => t.trim()).filter(Boolean) : [];
      for (const k of keys) if (k !== "tags") body[k] = orNull(body[k]);
      await api(id ? "/api/contacts/" + id : "/api/contacts", { method: id ? "PATCH" : "POST", body });
      loadContacts();
    } : null,
    onDelete: id && canWrite() ? async () => { await api("/api/contacts/" + id, { method: "DELETE" }); loadContacts(); } : null,
  });
}

// --- pipeline ----------------------------------------------------------------

$("#add-deal").addEventListener("click", () => editDeal(null));

async function loadPipeline() {
  const [board, contacts] = await Promise.all([api("/api/pipeline"), api("/api/contacts?limit=1000")]);
  state.contacts = contacts.items;
  const byId = Object.fromEntries(contacts.items.map((c) => [c.id, c]));
  const open = board.filter((col) => col.kind === "open");
  const openTotal = open.reduce((a, col) => a + col.total_cents, 0);
  const openCount = open.reduce((a, col) => a + col.count, 0);
  $("#pipeline-total").textContent = money(openTotal) + " open across " + openCount + (openCount === 1 ? " deal" : " deals");
  $("#board").replaceChildren(...board.map((col) => {
    const column = h("div", { class: "column " + col.kind, dataset: { stage: col.name } },
      h("div", { class: "column-head" }, h("h3", {}, col.name), h("span", { class: "note" }, col.count)),
      h("p", { class: "column-sum" }, money(col.total_cents)),
      col.deals.map((d) => h("button", {
        type: "button", class: "card", draggable: canWrite() ? "true" : "false", dataset: { id: d.id },
        onclick: () => editDeal(d.id),
        ondragstart: (e) => { e.dataTransfer.setData("text/plain", String(d.id)); e.currentTarget.classList.add("dragging"); },
        ondragend: (e) => e.currentTarget.classList.remove("dragging"),
      }, h("strong", {}, d.title), h("span", { class: "amount" }, money(d.amount_cents)), " ",
        h("span", {}, d.contact_id && byId[d.contact_id] ? "· " + contactName(byId[d.contact_id]) : ""))));
    column.addEventListener("dragover", (e) => { if (!canWrite()) return; e.preventDefault(); column.classList.add("drop"); });
    column.addEventListener("dragleave", () => column.classList.remove("drop"));
    column.addEventListener("drop", async (e) => {
      e.preventDefault();
      column.classList.remove("drop");
      const id = e.dataTransfer.getData("text/plain");
      const cards = [...column.querySelectorAll(".card")].filter((c) => c.dataset.id !== id);
      const position = cards.filter((c) => c.getBoundingClientRect().top + c.offsetHeight / 2 < e.clientY).length;
      try { await api("/api/deals/" + id + "/move", { method: "POST", body: { stage: col.name, position } }); } catch (err) { alert(err.message); }
      loadPipeline();
    });
    return column;
  }));
}

async function editDeal(id) {
  const d = id ? await api("/api/deals/" + id) : { stage_id: state.stages[0] && state.stages[0].id, amount_cents: 0 };
  const opts = await contactOptions();
  const ro = canWrite() ? {} : { readonly: true };
  openDialog({
    title: id ? d.title : "New deal",
    fields: [
      field("Title", "title", d.title, "text", { required: true, ...ro }),
      h("div", { class: "row2" },
        field("Amount (USD)", "amount", (d.amount_cents / 100).toString(), "number", { min: "0", step: "0.01", ...ro }),
        field("Expected close", "expected_close", d.expected_close, "date", ro)),
      h("div", { class: "row2" },
        selectField("Stage", "stage_id", state.stages.map((s) => [s.id, s.name]), d.stage_id),
        selectField("Contact", "contact_id", opts, d.contact_id)),
      field("Notes", "notes", d.notes, "textarea", ro),
    ],
    onSave: canWrite() ? async (fd) => {
      const f = formObj(fd, ["title", "amount", "expected_close", "stage_id", "contact_id", "notes"]);
      const body = {
        title: f.title, amount_cents: Math.round(parseFloat(f.amount || "0") * 100),
        expected_close: orNull(f.expected_close), stage_id: Number(f.stage_id),
        contact_id: f.contact_id ? Number(f.contact_id) : null, notes: orNull(f.notes),
      };
      await api(id ? "/api/deals/" + id : "/api/deals", { method: id ? "PATCH" : "POST", body });
      loadPipeline();
    } : null,
    onDelete: id && canWrite() ? async () => { await api("/api/deals/" + id, { method: "DELETE" }); loadPipeline(); } : null,
  });
}

// --- tasks -------------------------------------------------------------------

document.querySelectorAll("[data-filter]").forEach((b) => b.addEventListener("click", () => {
  state.taskFilter = b.dataset.filter;
  document.querySelectorAll("[data-filter]").forEach((x) => x.classList.toggle("on", x === b));
  loadTasks();
}));

$("#task-form").addEventListener("submit", async (ev) => {
  ev.preventDefault();
  const f = formObj(new FormData(ev.target), ["title", "due_date"]);
  try {
    await api("/api/tasks", { method: "POST", body: { title: f.title, due_date: orNull(f.due_date) } });
    ev.target.reset();
    loadTasks();
  } catch (e) { alert(e.message); }
});

function taskItem(t, reload) {
  const today = todayISO();
  return h("li", { class: t.done ? "done" : "" },
    h("input", { type: "checkbox", checked: t.done, disabled: !canWrite(), "aria-label": "Mark done: " + t.title,
      onchange: async (e) => { await api("/api/tasks/" + t.id, { method: "PATCH", body: { done: e.target.checked } }); reload(); } }),
    h("span", { class: "t-title" }, t.title),
    t.due_date ? h("span", { class: "due" + (!t.done && t.due_date < today ? " overdue" : "") }, (!t.done && t.due_date < today ? "Overdue · " : "Due ") + t.due_date) : null,
    canWrite() ? h("button", { type: "button", class: "btn quiet", onclick: async () => {
      if (!confirm("Delete this task?")) return;
      await api("/api/tasks/" + t.id, { method: "DELETE" }); reload();
    } }, "Delete") : null);
}

async function loadTasks() {
  const tasks = await api("/api/tasks?done=" + (state.taskFilter === "done"));
  $("#task-list").replaceChildren(...(tasks.length ? tasks.map((t) => taskItem(t, loadTasks))
    : [h("li", { class: "empty" }, state.taskFilter === "done" ? "Nothing finished yet." : "No open tasks.")]));
}

// --- calendar ----------------------------------------------------------------

$("#day").addEventListener("change", loadCalendar);
$("#day-today").addEventListener("click", () => { $("#day").value = todayISO(); loadCalendar(); });
for (const [sel, delta] of [["#day-prev", -1], ["#day-next", 1]]) {
  $(sel).addEventListener("click", () => {
    const d = new Date($("#day").value + "T12:00:00");
    d.setDate(d.getDate() + delta);
    $("#day").value = d.toISOString().slice(0, 10);
    loadCalendar();
  });
}

async function loadCalendar() {
  if (!$("#day").value) $("#day").value = todayISO();
  const day = $("#day").value;
  const [agenda, avail, contacts] = await Promise.all([api("/api/agenda?date=" + day), api("/api/availability?date=" + day), api("/api/contacts?limit=1000")]);
  state.contacts = contacts.items;
  const byId = Object.fromEntries(contacts.items.map((c) => [c.id, c]));
  const label = new Date(day + "T12:00:00").toLocaleDateString(undefined, { weekday: "long", month: "long", day: "numeric" });
  $("#day-title").textContent = label + " · " + agenda.appointments.length + (agenda.appointments.length === 1 ? " appointment" : " appointments");
  $("#agenda").replaceChildren(...(agenda.appointments.length ? agenda.appointments.map((a) => h("li", {},
    h("span", { class: "when" }, fmtTime(a.start) + "–" + fmtTime(a.end)),
    h("span", { class: "what" }, h("strong", {}, a.title),
      h("span", {}, [a.contact_id && byId[a.contact_id] ? contactName(byId[a.contact_id]) : null, a.location, a.source === "public" ? "Booked online" : null].filter(Boolean).join(" · "))),
    canWrite() ? h("button", { type: "button", class: "btn quiet", onclick: async () => {
      if (!confirm("Cancel this appointment?")) return;
      await api("/api/appointments/" + a.id + "/cancel", { method: "POST" }); loadCalendar();
    } }, "Cancel") : h("span"))) : [h("li", { class: "empty" }, "Nothing booked.")]));
  $("#agenda-tasks").replaceChildren(...(agenda.tasks_due.length ? agenda.tasks_due.map((t) => taskItem(t, loadCalendar)) : [h("li", { class: "empty" }, "No open tasks due.")]));
  $("#slot-note").textContent = avail.slots.length ? avail.slot_minutes + "-minute slots inside working hours. Pick one to book it." : "No open slots on this day.";
  $("#slots").replaceChildren(...avail.slots.map((s) => h("button", { type: "button", class: "btn", disabled: !canWrite(), onclick: () => bookSlot(s, avail.slot_minutes) }, fmtTime(s))));
}

async function bookSlot(startIso, minutes) {
  const opts = await contactOptions();
  openDialog({
    title: "Book " + fmtWhen(startIso),
    saveLabel: "Book",
    fields: [
      field("Title", "title", "", "text", { required: true, placeholder: "Consultation" }),
      h("div", { class: "row2" }, selectField("Contact", "contact_id", opts, ""), field("Length (minutes)", "duration_minutes", String(minutes), "number", { min: "5", step: "5" })),
      field("Location", "location", ""),
      field("Notes", "notes", "", "textarea"),
    ],
    onSave: async (fd) => {
      const f = formObj(fd, ["title", "contact_id", "duration_minutes", "location", "notes"]);
      await api("/api/appointments", { method: "POST", body: {
        title: f.title, start: startIso, duration_minutes: Number(f.duration_minutes || minutes),
        contact_id: f.contact_id ? Number(f.contact_id) : null, location: orNull(f.location), notes: orNull(f.notes) } });
      loadCalendar();
    },
  });
}

// --- audit -------------------------------------------------------------------

$("#audit-refresh").addEventListener("click", loadAudit);

async function loadAudit() {
  const [v, rows] = await Promise.all([api("/api/audit/verify"), api("/api/audit?limit=200")]);
  const status = $("#audit-status");
  status.className = "audit-status " + (v.ok ? "ok" : "bad");
  status.textContent = v.ok
    ? "Chain verified: " + v.entries + " entries, head " + v.head_hash.slice(0, 16) + "…"
    : "Chain broken at entry " + v.bad_id + ": " + v.reason;
  $("#audit-rows").replaceChildren(...rows.reverse().map((e) => h("tr", {},
    h("td", {}, fmtWhen(e.ts)), h("td", {}, e.actor), h("td", {}, e.action),
    h("td", {}, e.entity + (e.entity_id ? " #" + e.entity_id : "")), h("td", {}, JSON.parse(e.fields).join(", ")))));
}

start();
