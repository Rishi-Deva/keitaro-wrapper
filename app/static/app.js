// Shared helpers. All tracker data is rendered with textContent (never innerHTML).
"use strict";

async function api(method, path, body) {
  // custom header = CSRF protection (the server rejects state-changing /api calls without it)
  const opts = { method, headers: { "X-Requested-With": "keitaro-wrapper" } };
  if (body !== undefined) {
    opts.headers["Content-Type"] = "application/json";
    opts.body = JSON.stringify(body);
  }
  let resp;
  try {
    resp = await fetch(path, opts);
  } catch (e) {
    throw new Error("Сервер недоступен: " + e.message);
  }
  let data = null;
  try { data = await resp.json(); } catch (_) { /* non-JSON */ }
  if (!resp.ok) {
    throw new Error(formatError(resp.status, data));
  }
  return data;
}

function formatError(status, data) {
  if (!data) return "Ошибка " + status;
  // FastAPI request validation
  if (Array.isArray(data.detail)) {
    return "Проверьте поля: " + data.detail.map(d => (d.loc || []).slice(1).join(".") + " — " + d.msg).join("; ");
  }
  let text = data.detail || data.error || ("Ошибка " + status);
  if (data.fields && typeof data.fields === "object") {
    const parts = Object.entries(data.fields).map(([k, v]) => k + ": " + (Array.isArray(v) ? v.join(", ") : v));
    if (parts.length) text += "\n" + parts.join("\n");
  }
  return text;
}

function el(tag, attrs, ...children) {
  const n = document.createElement(tag);
  for (const [k, v] of Object.entries(attrs || {})) {
    if (k === "class") n.className = v;
    else if (k.startsWith("on")) n.addEventListener(k.slice(2), v);
    else if (v === true) n.setAttribute(k, "");
    else if (v !== false && v != null) n.setAttribute(k, v);
  }
  for (const c of children) {
    if (c == null) continue;
    n.append(c instanceof Node ? c : document.createTextNode(String(c)));
  }
  return n;
}

function showMsg(node, text, kind) {
  node.className = "msg " + (kind || "");
  node.textContent = text || "";
}

function debounce(fn, ms) {
  let t;
  return (...a) => { clearTimeout(t); t = setTimeout(() => fn(...a), ms); };
}

// Offer autocomplete: calls onPick({id, name}) or onPick(null) when the text is edited.
function offerAutocomplete(input, onPick) {
  const wrap = input.parentElement;
  wrap.classList.add("ac");
  const list = el("ul", { role: "listbox", hidden: true });
  wrap.append(list);
  let items = [], idx = -1, seq = 0;

  function close() { list.hidden = true; idx = -1; }
  function render() {
    list.replaceChildren();
    if (!items.length) {
      list.append(el("li", { class: "muted" }, "Ничего не найдено"));
    }
    items.forEach((o, i) => {
      list.append(el("li", {
        class: i === idx ? "sel" : "",
        onmousedown: (e) => { e.preventDefault(); pick(o); },
      }, o.name || "(без имени)", el("span", { class: "id" }, "#" + o.id)));
    });
    list.hidden = false;
  }
  function pick(o) {
    input.value = (o.name || "") + " (#" + o.id + ")";
    close();
    onPick(o);
  }
  const search = debounce(async () => {
    const my = ++seq;
    try {
      const res = await api("GET", "/api/offers?q=" + encodeURIComponent(input.value.trim()));
      if (my !== seq) return; // stale response
      items = res; idx = -1; render();
    } catch (e) {
      if (my !== seq) return;
      items = []; list.replaceChildren(el("li", { class: "muted" }, e.message)); list.hidden = false;
    }
  }, 250);

  input.setAttribute("autocomplete", "off");
  input.addEventListener("input", () => { onPick(null); search(); });
  input.addEventListener("focus", search);
  input.addEventListener("blur", () => setTimeout(close, 100));
  input.addEventListener("keydown", (e) => {
    if (list.hidden || !items.length) return;
    if (e.key === "ArrowDown") { idx = (idx + 1) % items.length; render(); e.preventDefault(); }
    else if (e.key === "ArrowUp") { idx = (idx - 1 + items.length) % items.length; render(); e.preventDefault(); }
    else if (e.key === "Enter" && idx >= 0) { pick(items[idx]); e.preventDefault(); }
    else if (e.key === "Escape") { close(); }
  });
}
