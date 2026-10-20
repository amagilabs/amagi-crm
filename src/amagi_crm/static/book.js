// Public booking page. Talks only to /public/*; no token involved.
"use strict";

const $ = (sel) => document.querySelector(sel);
let chosen = null;

const todayISO = () => { const d = new Date(); d.setMinutes(d.getMinutes() - d.getTimezoneOffset()); return d.toISOString().slice(0, 10); };
const fmtTime = (iso) => iso.slice(11, 16);
const dayLabel = (day) => new Date(day + "T12:00:00").toLocaleDateString(undefined, { weekday: "long", month: "long", day: "numeric" });

async function loadSlots() {
  const day = $("#day").value;
  $("#day-title").textContent = dayLabel(day);
  const res = await fetch("/public/slots?date=" + encodeURIComponent(day));
  const data = await res.json().catch(() => ({}));
  const slots = res.ok ? data.slots : [];
  $("#slot-note").textContent = slots.length
    ? data.slot_minutes + "-minute appointments. Times are shown in the office's time zone."
    : "No open times on this day. Try the next one.";
  $("#slots").replaceChildren(...slots.map((s) => {
    const b = document.createElement("button");
    b.type = "button";
    b.className = "btn";
    b.textContent = fmtTime(s);
    b.addEventListener("click", () => choose(s));
    return b;
  }));
}

function choose(slot) {
  chosen = slot;
  $("#chosen").textContent = dayLabel(slot.slice(0, 10)) + " at " + fmtTime(slot);
  $("#pick").hidden = true;
  $("#book-form").hidden = false;
  $("#book-error").textContent = "";
  $("#name").focus();
}

$("#book-back").addEventListener("click", () => { $("#book-form").hidden = true; $("#pick").hidden = false; loadSlots(); });

$("#book-form").addEventListener("submit", async (ev) => {
  ev.preventDefault();
  const fd = new FormData(ev.target);
  const body = { start: chosen };
  for (const k of ["name", "email", "phone", "message"]) {
    const v = (fd.get(k) || "").toString().trim();
    if (v) body[k] = v;
  }
  const res = await fetch("/public/book", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) });
  const data = await res.json().catch(() => ({}));
  if (!res.ok) {
    const detail = Array.isArray(data.detail) ? data.detail.map((d) => d.msg).join("; ") : data.detail;
    $("#book-error").textContent = detail || "Booking failed. Please try again.";
    return;
  }
  $("#book-form").hidden = true;
  $("#done").hidden = false;
  $("#done-detail").textContent = dayLabel(data.start.slice(0, 10)) + ", " + fmtTime(data.start) + " to " + fmtTime(data.end) + ".";
});

for (const [sel, delta] of [["#day-prev", -1], ["#day-next", 1]]) {
  $(sel).addEventListener("click", () => {
    const d = new Date($("#day").value + "T12:00:00");
    d.setDate(d.getDate() + delta);
    $("#day").value = d.toISOString().slice(0, 10);
    loadSlots();
  });
}
$("#day").addEventListener("change", loadSlots);
$("#day").value = todayISO();
$("#day").min = todayISO();
loadSlots();
