const api = async (path, opts = {}) => {
  const r = await fetch(path, {
    credentials: "include",
    headers: {
      "Content-Type": "application/json",
      ...(opts.headers || {}),
    },
    ...opts,
  });
  const text = await r.text();
  let json;
  try {
    json = text ? JSON.parse(text) : null;
  } catch {
    json = { _raw: text };
  }
  if (!r.ok) {
    const err = new Error(json?.detail || r.statusText || String(r.status));
    err.status = r.status;
    err.body = json;
    throw err;
  }
  return json;
};

const $ = (id) => document.getElementById(id);

const TAB_STORAGE_KEY = "nemoclaw-dash-tab";

let authRequired = false;
let selectedDomainSlug = "";

async function loadMe() {
  try {
    await api("v1/storage/summary");
    authRequired = false;
    return true;
  } catch (e) {
    if (e.status === 401) {
      authRequired = true;
      return false;
    }
    throw e;
  }
}

function showTab(name) {
  document.querySelectorAll(".tab").forEach((el) => el.classList.add("hidden"));
  const t = $(`tab-${name}`);
  if (t) t.classList.remove("hidden");
}

function fmtBytes(n) {
  if (n == null || n === undefined) return "—";
  const u = ["B", "KB", "MB", "GB"];
  let v = Number(n);
  let i = 0;
  while (v >= 1024 && i < u.length - 1) {
    v /= 1024;
    i += 1;
  }
  return `${v.toFixed(i === 0 ? 0 : 1)} ${u[i]}`;
}

function renderDedCatalog(cat) {
  const meta = $("dedCatalogMeta");
  if (meta) {
    meta.textContent = `managed_by_agent: ${cat?.managed_by_agent || "—"}`;
  }
  const tb = $("dedStoresTbody");
  const emptyEl = $("dedStoresEmpty");
  const stores = cat?.stores || [];
  if (emptyEl) {
    if (stores.length === 0) emptyEl.classList.remove("hidden");
    else emptyEl.classList.add("hidden");
  }
  if (!tb) return;
  tb.innerHTML = "";
  for (const st of stores) {
    const tr = document.createElement("tr");
    const wal =
      st.kind === "sqlite"
        ? `${st.wal_exists ? "wal" : "—"} / ${st.shm_exists ? "shm" : "—"}`
        : "—";
    const mk = (txt) => {
      const td = document.createElement("td");
      td.textContent = txt;
      return td;
    };
    tr.appendChild(mk(st.title ?? ""));
    tr.appendChild(mk(st.kind ?? ""));
    tr.appendChild(mk(st.consumers ?? ""));
    tr.appendChild(mk(st.exists ? "yes" : "no"));
    tr.appendChild(mk(fmtBytes(st.size_bytes)));
    tr.appendChild(mk(st.mtime_iso ?? "—"));
    tr.appendChild(mk(wal));
    const pathTd = document.createElement("td");
    pathTd.className = "path-cell";
    pathTd.textContent = st.path ?? "";
    tr.appendChild(pathTd);
    tb.appendChild(tr);

    if (st.kind === "sqlite" && Array.isArray(st.tables) && st.tables.length) {
      const sub = document.createElement("tr");
      const td = document.createElement("td");
      td.colSpan = 8;
      const inner = document.createElement("table");
      inner.className = "catalog-table-nested";
      inner.setAttribute("aria-label", `Tables: ${st.title || st.id}`);
      const thr = document.createElement("tr");
      for (const h of ["SQLite table", "Rows"]) {
        const th = document.createElement("th");
        th.textContent = h;
        thr.appendChild(th);
      }
      inner.appendChild(thr);
      for (const row of st.tables) {
        const ttr = document.createElement("tr");
        const a = document.createElement("td");
        a.textContent = String(row.name ?? "");
        const b = document.createElement("td");
        b.textContent = row.row_count < 0 ? "?" : String(row.row_count);
        ttr.appendChild(a);
        ttr.appendChild(b);
        inner.appendChild(ttr);
      }
      td.appendChild(inner);
      sub.appendChild(td);
      tb.appendChild(sub);
    }
  }
}

async function loadDedCatalog() {
  const errEl = $("dedCatalogErr");
  if (errEl) errEl.textContent = "";
  $("dedStoresEmpty")?.classList.add("hidden");
  try {
    const j = await api("v1/storage/catalog?tables=1");
    renderDedCatalog(j);
  } catch (e) {
    if (errEl) errEl.textContent = String(e.message || e);
    $("dedStoresEmpty")?.classList.remove("hidden");
  }
}

function extractDomainList(j) {
  if (!j || typeof j !== "object") return [];
  const nested = [
    j.domains,
    j.catalog,
    Array.isArray(j.catalog?.domains) ? j.catalog.domains : null,
    j.items,
    j.domain_catalog,
    j.data?.domains,
  ].find((x) => Array.isArray(x));
  if (nested) return nested;
  if (Array.isArray(j)) return j;
  return [];
}

function normalizeDomainRow(raw, idx) {
  if (typeof raw === "string") {
    return { slug: raw, label: raw, rowCount: "—", sources: "—", updated: "—" };
  }
  const slug =
    raw.slug ??
    raw.domain_slug ??
    raw.domain ??
    raw.id ??
    raw.name ??
    `domain_${idx}`;
  const label = raw.display_name ?? raw.title ?? slug;
  const rc =
    raw.row_count ??
    raw.rows ??
    raw.count ??
    raw.n ??
    (typeof raw.total_rows === "number" ? raw.total_rows : null);
  let sources = raw.sources ?? raw.source_summary;
  if (Array.isArray(sources)) sources = sources.join(", ");
  else if (sources && typeof sources === "object") sources = JSON.stringify(sources);
  const updated =
    raw.last_updated_at ??
    raw.last_updated ??
    raw.updated_at ??
    raw.last_event_at ??
    raw.modified ??
    "—";
  return {
    slug: String(slug),
    label: String(label),
    rowCount: rc != null ? String(rc) : "—",
    sources: sources != null && sources !== "" ? String(sources) : "—",
    updated: updated != null ? String(updated) : "—",
  };
}

function extractRowsPayload(j) {
  if (!j || typeof j !== "object") return [];
  const dataRows = Array.isArray(j.data) ? j.data : j.data?.rows;
  const arr = [j.rows, j.items, j.records, dataRows, j.samples].find((x) => Array.isArray(x));
  return arr || [];
}

async function loadDeDomainsCatalog() {
  const errEl = $("dedomCatalogErr");
  const meta = $("dedomMeta");
  const tb = $("dedomTbody");
  const emptyEl = $("dedomEmpty");
  if (errEl) errEl.textContent = "";
  if (meta) meta.textContent = "";
  if (tb) tb.innerHTML = "";
  emptyEl?.classList.add("hidden");
  try {
    const j = await api("v1/data-entry/catalog");
    const list = extractDomainList(j).map(normalizeDomainRow);
    if (meta) {
      const hint = j.note ?? j.message ?? "";
      meta.textContent = hint ? String(hint) : `${list.length} domain(s)`;
    }
    if (list.length === 0) {
      emptyEl?.classList.remove("hidden");
      return;
    }
    for (let i = 0; i < list.length; i += 1) {
      const d = list[i];
      const tr = document.createElement("tr");
      tr.dataset.slug = d.slug;
      if (d.slug === selectedDomainSlug) tr.classList.add("row-selected");
      const mk = (txt) => {
        const td = document.createElement("td");
        td.textContent = txt;
        return td;
      };
      tr.appendChild(mk(d.label));
      tr.appendChild(mk(d.rowCount));
      tr.appendChild(mk(d.sources));
      tr.appendChild(mk(d.updated));
      tr.addEventListener("click", () => {
        selectedDomainSlug = d.slug;
        document.querySelectorAll("#dedomTbody tr").forEach((r) => r.classList.remove("row-selected"));
        tr.classList.add("row-selected");
        loadDomainRows(d.slug);
      });
      tb.appendChild(tr);
    }
    if (selectedDomainSlug) {
      const still = list.some((x) => x.slug === selectedDomainSlug);
      if (still) await loadDomainRows(selectedDomainSlug);
      else selectedDomainSlug = "";
    }
  } catch (e) {
    if (errEl)
      errEl.textContent = `GET /v1/data-entry/catalog: ${e.message || e}`;
    emptyEl?.classList.remove("hidden");
  }
}

async function loadDomainRows(slug) {
  const hint = $("dedomRowsHint");
  const errEl = $("dedomRowsErr");
  const out = $("dedomRowsOut");
  if (errEl) errEl.textContent = "";
  if (out) out.textContent = "";
  if (hint) hint.textContent = slug ? `Domain: ${slug}` : "Select a domain.";
  if (!slug) return;
  try {
    const j = await api(`v1/data-entry/domain/${encodeURIComponent(slug)}/rows?limit=50`);
    const rows = extractRowsPayload(j);
    if (out) out.textContent = rows.length ? JSON.stringify(rows, null, 2) : "(no rows)";
  } catch (e) {
    if (errEl)
      errEl.textContent = `GET /v1/data-entry/domain/…/rows: ${e.message || e}`;
    if (out) out.textContent = "";
  }
}

function extractMealsArray(j) {
  if (!j || typeof j !== "object") return [];
  const paths = [
    j.meals,
    j.meal_log,
    j.items,
    j.data?.meals,
    j.context?.meals,
    j.insights?.meals,
    j.food_log,
    j.recent_meals,
  ];
  for (const p of paths) {
    if (Array.isArray(p)) return p;
  }
  return [];
}

function collectKeys(rows, max = 12) {
  const keys = new Set();
  for (const row of rows.slice(0, 40)) {
    if (row && typeof row === "object" && !Array.isArray(row)) {
      Object.keys(row).forEach((k) => keys.add(k));
      if (keys.size >= max) break;
    }
  }
  return [...keys].slice(0, max);
}

function renderMealTable(rows) {
  const wrap = $("flTableWrap");
  const thead = $("flThead");
  const tbody = $("flTbody");
  const emptyEl = $("flEmpty");
  const fb = $("flFallback");
  fb?.classList.add("hidden");
  fb && (fb.textContent = "");
  if (!wrap || !thead || !tbody) return;
  const objs = rows.filter((r) => r && typeof r === "object");
  if (objs.length === 0) {
    wrap.classList.add("hidden");
    emptyEl?.classList.remove("hidden");
    return;
  }
  emptyEl?.classList.add("hidden");
  wrap.classList.remove("hidden");
  const cols = collectKeys(objs);
  thead.innerHTML = "";
  const hr = document.createElement("tr");
  for (const c of cols) {
    const th = document.createElement("th");
    th.textContent = c;
    hr.appendChild(th);
  }
  thead.appendChild(hr);
  tbody.innerHTML = "";
  for (const row of objs.slice(0, 50)) {
    const tr = document.createElement("tr");
    for (const c of cols) {
      const td = document.createElement("td");
      const v = row[c];
      td.textContent =
        v == null
          ? ""
          : typeof v === "object"
            ? JSON.stringify(v)
            : String(v);
      tr.appendChild(td);
    }
    tbody.appendChild(tr);
  }
}

async function loadFoodLogPanel() {
  const errEl = $("flErr");
  const meta = $("flMeta");
  const emptyEl = $("flEmpty");
  const wrap = $("flTableWrap");
  const fb = $("flFallback");
  if (errEl) errEl.textContent = "";
  if (meta) meta.textContent = "";
  emptyEl?.classList.add("hidden");
  wrap?.classList.add("hidden");
  fb?.classList.add("hidden");
  $("flTbody").innerHTML = "";
  $("flThead").innerHTML = "";
  if (emptyEl) emptyEl.textContent = "No meals in this window.";

  let mealsErr = null;
  try {
    const j = await api("v1/data-entry/meals?days=14");
    if (meta) meta.textContent = "Source: GET /v1/data-entry/meals?days=14";
    const meals = extractMealsArray(j);
    if (meals.length) renderMealTable(meals);
    else {
      renderMealTable([]);
      emptyEl?.classList.remove("hidden");
    }
    return;
  } catch (e) {
    mealsErr = e;
  }

  try {
    const j = await api("v1/data-entry/insight-context?days=14");
    if (meta)
      meta.textContent =
        "Source: GET /v1/data-entry/insight-context?days=14 (meals endpoint unavailable)";
    const meals = extractMealsArray(j);
    if (meals.length) {
      renderMealTable(meals);
      return;
    }
    renderMealTable([]);
    if (fb) {
      fb.textContent = JSON.stringify(j, null, 2);
      fb.classList.remove("hidden");
    }
    emptyEl?.classList.remove("hidden");
    if (emptyEl)
      emptyEl.textContent =
        "No structured meal list in this response; raw insight-context JSON is below.";
  } catch (e2) {
    if (errEl)
      errEl.textContent = [
        `GET /v1/data-entry/meals?days=14: ${mealsErr?.message || mealsErr}`,
        `GET /v1/data-entry/insight-context?days=14: ${e2.message || e2}`,
      ].join("\n");
    emptyEl?.classList.remove("hidden");
    if (emptyEl) emptyEl.textContent = "Could not load food log.";
  }
}

async function hydrateProfileFromServer() {
  const hint = $("obProfileHint");
  if (!hint) return;
  hint.textContent = "";
  try {
    const j = await api("v1/profile");
    const profile = j?.profile && typeof j.profile === "object" ? j.profile : {};
    const ob = profile.onboarding && typeof profile.onboarding === "object" ? profile.onboarding : null;
    if (ob) {
      const goalSel = $("obGoal");
      const intSel = $("obIntensity");
      const notes = $("obNotes");
      if (ob.goal_primary && goalSel) {
        const opt = [...goalSel.options].some((o) => o.value === ob.goal_primary);
        if (opt) goalSel.value = ob.goal_primary;
      }
      if (ob.coaching_intensity && intSel) {
        const opt = [...intSel.options].some((o) => o.value === ob.coaching_intensity);
        if (opt) intSel.value = ob.coaching_intensity;
      }
      if (notes != null && ob.notes !== undefined && ob.notes !== null) notes.value = String(ob.notes);
    }
    const parts = [];
    parts.push("Profile loaded from server.");
    if (profile.onboarding_completed === true) parts.push("Onboarding is marked complete.");
    else if (profile.onboarding_completed === false) parts.push("Onboarding not marked complete yet.");
    hint.textContent = parts.join(" ");
    updateObSummaryPre();
  } catch (e) {
    hint.textContent = `Could not load profile (refresh may still work after save): ${e.message || e}`;
    updateObSummaryPre();
  }
}

function initialTabName() {
  let saved = "";
  try {
    saved = sessionStorage.getItem(TAB_STORAGE_KEY) || "";
  } catch {
    /* ignore */
  }
  if (saved && $(`tab-${saved}`)) return saved;
  return "onboard";
}

function onTabActivated(name) {
  if (name === "data-stores") loadDedCatalog();
  else if (name === "data-domains") loadDeDomainsCatalog();
  else if (name === "food-log") loadFoodLogPanel();
}

function setupNav() {
  document.querySelectorAll("[data-tab]").forEach((btn) => {
    btn.addEventListener("click", () => {
      const name = btn.getAttribute("data-tab");
      showTab(name);
      try {
        sessionStorage.setItem(TAB_STORAGE_KEY, name);
      } catch {
        /* ignore */
      }
      onTabActivated(name);
    });
  });
}

async function refreshAuthBar() {
  const bar = $("authBar");
  if (!bar) return;
  if (!authRequired) {
    bar.textContent = "API open (no dashboard password)";
    return;
  }
  bar.innerHTML =
    '<button type="button" id="logoutBtn">Sign out</button>';
  $("logoutBtn")?.addEventListener("click", async () => {
    await api("v1/auth/logout", { method: "POST", body: "{}" });
    window.location.reload();
  });
}

function obSummary() {
  const goal = $("obGoal").value;
  const intensity = $("obIntensity").value;
  const notes = $("obNotes").value.trim();
  return { goal_primary: goal, coaching_intensity: intensity, notes, saved_at: new Date().toISOString() };
}

function updateObSummaryPre() {
  $("obSummary").textContent = JSON.stringify(obSummary(), null, 2);
}

async function main() {
  setupNav();
  let ok = false;
  try {
    ok = await loadMe();
  } catch (e) {
    $("loginPanel")?.classList.remove("hidden");
    $("loginErr").textContent = String(e.message || e);
  }

  if (ok) {
    $("loginPanel")?.classList.add("hidden");
    $("mainNav")?.classList.remove("hidden");
    const tab = initialTabName();
    showTab(tab);
    await hydrateProfileFromServer();
    if (tab !== "data-stores") loadDedCatalog();
    onTabActivated(tab);
  } else if (!($("loginErr")?.textContent)) {
    $("loginPanel")?.classList.remove("hidden");
  }

  await refreshAuthBar();

  $("loginForm")?.addEventListener("submit", async (ev) => {
    ev.preventDefault();
    const pw = $("loginPassword").value;
    $("loginErr").textContent = "";
    try {
      await api("v1/auth/login", { method: "POST", body: JSON.stringify({ password: pw }) });
      window.location.reload();
    } catch (e) {
      $("loginErr").textContent = e.message || String(e);
    }
  });

  ["obGoal", "obIntensity", "obNotes"].forEach((id) => {
    $(id)?.addEventListener("change", updateObSummaryPre);
    $(id)?.addEventListener("input", updateObSummaryPre);
  });
  updateObSummaryPre();

  $("obPreviewChat")?.addEventListener("click", async () => {
    const msg = `Onboarding context: ${JSON.stringify(obSummary())}`;
    const out = await api("v1/chat", { method: "POST", body: JSON.stringify({ message: msg }) });
    $("obChatPreview").textContent = JSON.stringify(out, null, 2);
  });

  $("obSave")?.addEventListener("click", async () => {
    const summary = obSummary();
    $("obStatus").textContent = "";
    try {
      await api("v1/profile", {
        method: "PUT",
        body: JSON.stringify({ onboarding: summary, onboarding_completed: true }),
      });
      await api("v1/goals", {
        method: "POST",
        body: JSON.stringify({
          title: `Primary: ${summary.goal_primary}`,
          body_json: summary,
        }),
      });
      $("obStatus").textContent = "Saved profile + goal.";
      await hydrateProfileFromServer();
    } catch (e) {
      $("obStatus").textContent = `Error: ${e.message}`;
    }
  });

  const readFileAsBase64 = (file) =>
    new Promise((resolve, reject) => {
      const r = new FileReader();
      r.onload = () => {
        const s = String(r.result || "");
        const i = s.indexOf(",");
        resolve(i >= 0 ? s.slice(i + 1) : s);
      };
      r.onerror = () => reject(r.error);
      r.readAsDataURL(file);
    });

  $("chatForm")?.addEventListener("submit", async (ev) => {
    ev.preventDefault();
    const msg = ($("chatMessage")?.value || "").trim();
    const input = $("chatImages");
    const files = input?.files || [];
    const images = [];
    for (let i = 0; i < Math.min(files.length, 4); i += 1) {
      const f = files[i];
      const mime = (f.type || "image/jpeg").split(";")[0].trim();
      const b64 = await readFileAsBase64(f);
      images.push({ mime_type: mime, data_base64: b64 });
    }
    if (!msg && images.length === 0) {
      $("chatOut").textContent = "Enter a message or attach at least one image.";
      return;
    }
    const body = { message: msg, images };
    const out = await api("v1/chat", { method: "POST", body: JSON.stringify(body) });
    $("chatOut").textContent = JSON.stringify(out, null, 2);
  });

  $("tlLoad")?.addEventListener("click", async () => {
    const src = $("tlSource").value.trim();
    const q = src ? `?source=${encodeURIComponent(src)}&limit=80` : "?limit=80";
    const out = await api(`v1/timeline${q}`);
    $("tlOut").textContent = JSON.stringify(out, null, 2);
  });

  $("deReg")?.addEventListener("click", async () => {
    const domain = $("deDomain").value.trim();
    const out = await api("v1/data/domain", {
      method: "POST",
      body: JSON.stringify({ display_name: domain, schema_hint: ["items", "meal"] }),
    });
    $("deOut").textContent = JSON.stringify(out, null, 2);
  });

  $("deIngest")?.addEventListener("click", async () => {
    const domain = $("deDomain").value.trim();
    let payload;
    try {
      payload = JSON.parse($("dePayload").value || "{}");
    } catch (e) {
      $("deOut").textContent = "Invalid JSON: " + e.message;
      return;
    }
    const out = await api("v1/data/ingest", {
      method: "POST",
      body: JSON.stringify({
        domain,
        source: $("deSource").value.trim() || "manual",
        payload,
      }),
    });
    $("deOut").textContent = JSON.stringify(out, null, 2);
  });

  const whoopRefresh = async () => {
    $("whoopStatus").textContent = JSON.stringify(await api("v1/connectors/whoop/status"), null, 2);
    $("appleStatus").textContent = JSON.stringify(await api("v1/connectors/apple-health/status"), null, 2);
  };

  $("whoopRefresh")?.addEventListener("click", whoopRefresh);
  const hideWhoopAuthExtras = () => {
    $("whoopAuthErr").textContent = "";
    $("whoopCopyAuthUrl")?.classList.add("hidden");
    $("whoopAuthLink")?.classList.add("hidden");
  };
  $("whoopUrl")?.addEventListener("click", async () => {
    hideWhoopAuthExtras();
    try {
      const u = await api("v1/connectors/whoop/authorize-url");
      const url = u?.authorization_url;
      if (!url || typeof url !== "string") {
        $("whoopAuthErr").textContent = "No authorization_url in response.";
        $("whoopStatus").textContent = JSON.stringify(u, null, 2);
        return;
      }
      $("whoopStatus").textContent = JSON.stringify(
        {
          authorization_url: url,
          redirect_uri: u.redirect_uri,
          redirect_provenance: u.redirect_provenance,
          dashboard_hint: u.dashboard_hint,
          hint: "Register redirect_uri exactly in WHOOP Developer Dashboard; popup_blocked_use_copy_or_link",
        },
        null,
        2,
      );
      if (typeof u.dashboard_hint === "string" && u.dashboard_hint) {
        $("whoopAuthErr").textContent = u.dashboard_hint;
      }
      const win = window.open(url, "_blank", "noopener");
      if (!win || win.closed) {
        $("whoopCopyAuthUrl")?.classList.remove("hidden");
        $("whoopAuthLink")?.classList.remove("hidden");
        const a = $("whoopAuthLink");
        if (a) a.href = url;
        $("whoopAuthErr").textContent =
          "Popup may be blocked. Use “Copy authorize URL” or the link below, then complete sign-in at WHOOP.";
      }
    } catch (e) {
      const detail = e?.body?.detail;
      $("whoopAuthErr").textContent =
        typeof detail === "string"
          ? detail
          : e?.message || String(e);
      $("whoopStatus").textContent = JSON.stringify(
        { error: e?.message || String(e), detail: e?.body?.detail ?? null },
        null,
        2,
      );
    }
  });
  $("whoopCopyAuthUrl")?.addEventListener("click", async () => {
    const pre = $("whoopStatus")?.textContent || "";
    let url = "";
    try {
      const j = JSON.parse(pre);
      url = j.authorization_url || "";
    } catch {
      /* ignore */
    }
    if (!url) return;
    try {
      await navigator.clipboard.writeText(url);
      $("whoopAuthErr").textContent = "Copied authorize URL to clipboard.";
    } catch {
      $("whoopAuthErr").textContent = "Could not copy (clipboard blocked). Use the link above.";
    }
  });
  $("whoopSync")?.addEventListener("click", async () => {
    const out = await api("v1/connectors/whoop/sync", { method: "POST", body: "{}" });
    $("whoopStatus").textContent = JSON.stringify(out, null, 2);
  });
  $("appleUpload")?.addEventListener("click", async () => {
    const f = $("appleFile").files[0];
    if (!f) {
      alert("Choose a .zip file");
      return;
    }
    const fd = new FormData();
    fd.append("file", f, f.name);
    const r = await fetch("v1/connectors/apple-health/import", { method: "POST", credentials: "include", body: fd });
    const ct = r.headers.get("content-type") || "";
    const text = await r.text();
    let display = text;
    if (!r.ok || (ct && !ct.includes("json"))) {
      const proxyHint =
        r.status === 504 || (ct.includes("text/html") && /504|Gateway Time-?out/i.test(text))
          ? "\n\nHint: Large Apple Health exports can take many minutes. Raise nginx proxy_read_timeout / proxy_send_timeout (e.g. 3600s) and any load balancer idle timeout above the expected import duration."
          : "";
      display = `HTTP ${r.status}${r.statusText ? ` ${r.statusText}` : ""}${proxyHint}\n\n${text}`;
    }
    $("appleStatus").textContent = display;
    await whoopRefresh();
  });

  $("dbgSessions")?.addEventListener("click", async () => {
    const out = await api("v1/debug/sessions");
    $("dbgSessionsOut").textContent = JSON.stringify(out, null, 2);
  });
  $("dbgTrace")?.addEventListener("click", async () => {
    const tid = $("dbgTaskId").value.trim();
    const out = await api(`v1/debug/session/${encodeURIComponent(tid)}`);
    $("dbgTraceOut").textContent = JSON.stringify(out, null, 2);
  });
  $("dbgAnalyze")?.addEventListener("click", async () => {
    const tid = $("dbgTaskId").value.trim();
    const body = tid ? { task_id: tid } : {};
    const out = await api("v1/debug/analyze", {
      method: "POST",
      body: JSON.stringify(body),
      headers: { "Content-Type": "application/json" },
    });
    $("dbgTraceOut").textContent = JSON.stringify(out, null, 2);
  });

  $("bkExport")?.addEventListener("click", async () => {
    const out = await api("v1/storage/export-raw-jsonl", {
      method: "POST",
      body: JSON.stringify({ dest_relative: "artifacts/dashboard_export.jsonl" }),
    });
    $("bkOut").textContent = JSON.stringify(out, null, 2);
  });
  $("bkSummary")?.addEventListener("click", async () => {
    $("bkSumOut").textContent = JSON.stringify(await api("v1/storage/summary"), null, 2);
  });

  $("dedReload")?.addEventListener("click", loadDedCatalog);
  $("dedomReload")?.addEventListener("click", loadDeDomainsCatalog);
  $("flReload")?.addEventListener("click", loadFoodLogPanel);
  $("dedBootstrap")?.addEventListener("click", async () => {
    const errEl = $("dedCatalogErr");
    if (errEl) errEl.textContent = "";
    try {
      await api("v1/data-entry/health-store/bootstrap", { method: "POST", body: "{}" });
      await loadDedCatalog();
    } catch (e) {
      if (errEl) errEl.textContent = String(e.message || e);
    }
  });

  if (ok) await whoopRefresh();
}

main().catch((e) => {
  console.error(e);
  const el = $("loginErr") || document.body;
  el.textContent = (el.textContent || "") + "\n" + String(e);
});
