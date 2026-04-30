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

let authRequired = false;

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

function setupNav() {
  document.querySelectorAll("[data-tab]").forEach((btn) => {
    btn.addEventListener("click", () => showTab(btn.getAttribute("data-tab")));
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
    showTab("onboard");
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
    } catch (e) {
      $("obStatus").textContent = `Error: ${e.message}`;
    }
  });

  $("chatForm")?.addEventListener("submit", async (ev) => {
    ev.preventDefault();
    const msg = $("chatMessage").value;
    const out = await api("v1/chat", { method: "POST", body: JSON.stringify({ message: msg }) });
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
      $("whoopStatus").textContent = JSON.stringify({ authorization_url: url, hint: "popup_blocked_use_copy_or_link" }, null, 2);
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

  if (ok) await whoopRefresh();
}

main().catch((e) => {
  console.error(e);
  const el = $("loginErr") || document.body;
  el.textContent = (el.textContent || "") + "\n" + String(e);
});
