// Fusion Backend Admin Dashboard
// Stores backend URL + admin key in memory only (not localStorage, so re-enter each session).

let state = {
  backendUrl: "",
  adminKey: "",
  currentPlayerId: null,
};

const $ = (id) => document.getElementById(id);

async function api(path, { method = "GET", body = null } = {}) {
  const res = await fetch(`${state.backendUrl}${path}`, {
    method,
    headers: {
      "Content-Type": "application/json",
      "X-Admin-Key": state.adminKey,
    },
    body: body ? JSON.stringify(body) : null,
  });
  const data = await res.json().catch(() => ({}));
  if (!res.ok) throw new Error(data.error || `Request failed (${res.status})`);
  return data;
}

// ---- Login ----
$("connect-btn").addEventListener("click", async () => {
  const url = $("backend-url").value.trim().replace(/\/$/, "");
  const key = $("admin-key").value.trim();
  $("login-error").textContent = "";

  if (!url || !key) {
    $("login-error").textContent = "Enter both backend URL and admin key.";
    return;
  }

  state.backendUrl = url;
  state.adminKey = key;

  try {
    await api("/api/admin/players");
    $("login-screen").classList.add("hidden");
    $("app-screen").classList.remove("hidden");
    loadPlayers();
  } catch (e) {
    $("login-error").textContent = "Connection failed: " + e.message;
  }
});

$("disconnect-btn").addEventListener("click", () => {
  state = { backendUrl: "", adminKey: "", currentPlayerId: null };
  $("app-screen").classList.add("hidden");
  $("login-screen").classList.remove("hidden");
  $("admin-key").value = "";
});

// ---- Tab navigation ----
document.querySelectorAll(".nav-btn[data-tab]").forEach((btn) => {
  btn.addEventListener("click", () => showTab(btn.dataset.tab));
});
$("back-to-players").addEventListener("click", () => showTab("players"));
$("back-to-catalog").addEventListener("click", () => showTab("catalog"));

function showTab(tabName) {
  document.querySelectorAll(".tab").forEach((t) => t.classList.add("hidden"));
  document.querySelectorAll(".nav-btn").forEach((b) => b.classList.remove("active"));
  $(`tab-${tabName}`).classList.remove("hidden");
  // Highlight the matching sidebar entry even for sub-pages like item-create
  const navMatch = tabName === "item-create" ? "catalog" : tabName;
  const navBtn = document.querySelector(`.nav-btn[data-tab="${navMatch}"]`);
  if (navBtn) navBtn.classList.add("active");

  if (tabName === "catalog") loadCatalog();
  if (tabName === "currencies") loadCurrencies();
  if (tabName === "cloudscript") loadCloudScripts();
  if (tabName === "item-create") populateCurrencyDropdown();
}

// ---- Player detail sub-tab navigation ----
document.querySelectorAll(".subnav-btn[data-subtab]").forEach((btn) => {
  btn.addEventListener("click", () => showSubTab(btn.dataset.subtab));
});

function showSubTab(subtabName) {
  document.querySelectorAll(".subtab").forEach((t) => t.classList.add("hidden"));
  document.querySelectorAll(".subnav-btn").forEach((b) => b.classList.remove("active"));
  $(`subtab-${subtabName}`).classList.remove("hidden");
  const btn = document.querySelector(`.subnav-btn[data-subtab="${subtabName}"]`);
  if (btn) btn.classList.add("active");

  if (subtabName === "logins") loadPlayerLogins(state.currentPlayerId);
  if (subtabName === "cloudscript-player") loadPlayerCloudScriptPanel(state.currentPlayerId);
}

// ---- Players list ----
$("refresh-players").addEventListener("click", loadPlayers);

async function loadPlayers() {
  const tbody = document.querySelector("#players-table tbody");
  tbody.innerHTML = `<tr><td colspan="6">Loading…</td></tr>`;
  try {
    const { players } = await api("/api/admin/players");
    tbody.innerHTML = "";
    if (players.length === 0) {
      tbody.innerHTML = `<tr><td colspan="6">No players yet. They'll appear here after first game launch.</td></tr>`;
      return;
    }
    players.forEach((p) => {
      const tr = document.createElement("tr");
      tr.innerHTML = `
        <td><a class="player-id-link" data-id="${p.player_id}">${p.player_id}</a></td>
        <td>${p.display_name || "—"}</td>
        <td>${formatDate(p.created_at)}</td>
        <td>${formatDate(p.last_login)}</td>
        <td><span class="badge ${p.banned ? "badge-banned" : "badge-ok"}">${p.banned ? "Banned" : "Active"}</span></td>
        <td><button class="view-btn" data-id="${p.player_id}">View</button></td>
      `;
      tbody.appendChild(tr);
    });
    document.querySelectorAll(".player-id-link, .view-btn").forEach((el) => {
      el.addEventListener("click", () => openPlayerDetail(el.dataset.id));
    });
  } catch (e) {
    tbody.innerHTML = `<tr><td colspan="6">Error: ${e.message}</td></tr>`;
  }
}

function formatDate(iso) {
  if (!iso) return "—";
  const d = new Date(iso);
  return d.toLocaleString();
}

// ---- Player detail ----
async function openPlayerDetail(playerId) {
  state.currentPlayerId = playerId;
  $("detail-nav").style.display = "block";
  showTab("player-detail");
  showSubTab("overview");
  $("detail-title").textContent = `Player — ${playerId}`;

  try {
    await populateSetCurrencyDropdown();
    const { player, currencies, data, inventory } = await api(`/api/admin/players/${playerId}`);

    // Overview
    $("detail-account").innerHTML = `
      <div class="row-item"><span>Device ID</span><span>${player.device_id.slice(0, 12)}…</span></div>
      <div class="row-item"><span>Created</span><span>${formatDate(player.created_at)}</span></div>
      <div class="row-item"><span>Last login</span><span>${formatDate(player.last_login)}</span></div>
    `;
    $("detail-data").textContent = Object.keys(data).length ? JSON.stringify(data, null, 2) : "// no cloud save data yet";

    // Bans
    $("detail-ban-status").innerHTML = `
      <div class="row-item">
        <span>Status</span>
        <span class="badge ${player.banned ? "badge-banned" : "badge-ok"}">${player.banned ? "Banned" : "Active"}</span>
      </div>
      ${player.banned && player.ban_reason ? `<div class="row-item"><span>Reason</span><span>${player.ban_reason}</span></div>` : ""}
      ${player.banned ? `<div class="row-item"><span>Expires</span><span>${player.ban_expires_at ? formatDate(player.ban_expires_at) : "Permanent"}</span></div>` : ""}
    `;
    const banBtn = $("ban-toggle-btn");
    banBtn.textContent = player.banned ? "Unban Player" : "Ban Player";
    banBtn.style.background = player.banned ? "var(--ok)" : "var(--danger)";
    banBtn.style.color = "#fff";
    banBtn.style.width = "100%";
    banBtn.style.padding = "10px";
    banBtn.style.marginTop = "8px";
    banBtn.onclick = async () => {
      const reason = $("ban-reason-input").value.trim() || null;
      const durationRaw = $("ban-duration-input").value.trim();
      const duration_minutes = durationRaw ? parseInt(durationRaw, 10) : null;
      await api(`/api/admin/players/${playerId}/ban`, { method: "POST", body: { banned: !player.banned, reason, duration_minutes } });
      $("ban-reason-input").value = "";
      $("ban-duration-input").value = "";
      openPlayerDetail(playerId);
      showSubTab("bans");
    };

    // Currency
    $("detail-currencies").innerHTML = Object.entries(currencies).length
      ? Object.entries(currencies).map(([code, amt]) => `<div class="row-item"><span>${code}</span><span>${amt}</span></div>`).join("")
      : `<p style="color:var(--steel-400)">No currencies yet.</p>`;

    // Inventory
    $("detail-inventory").innerHTML = inventory.length
      ? inventory.map((i) => `
          <div class="row-item">
            <span>${i.name} ×${i.quantity}</span>
            <button class="small-btn revoke-item-btn" data-instance="${i.instance_id}">Revoke</button>
          </div>`).join("")
      : `<p style="color:var(--steel-400)">No items yet.</p>`;

    // Wire up per-item revoke buttons
    document.querySelectorAll(".revoke-item-btn").forEach((btn) => {
      btn.addEventListener("click", async () => {
        if (!confirm("Revoke this item from the player? This cannot be undone.")) return;
        try {
          await api(`/api/admin/players/${playerId}/inventory/${btn.dataset.instance}`, { method: "DELETE" });
          openPlayerDetail(playerId);
          showSubTab("inventory");
        } catch (e) {
          alert("Error: " + e.message);
        }
      });
    });
  } catch (e) {
    alert("Failed to load player: " + e.message);
  }
}

async function loadPlayerLogins(playerId) {
  const container = $("detail-logins");
  container.innerHTML = `<p style="color:var(--steel-400)">Loading…</p>`;
  try {
    const { logins } = await api(`/api/admin/players/${playerId}/logins`);
    container.innerHTML = logins.length
      ? logins.map((ts) => `<div class="row-item"><span>${formatDate(ts)}</span></div>`).join("")
      : `<p style="color:var(--steel-400)">No login history yet.</p>`;
  } catch (e) {
    container.innerHTML = `<p style="color:#f87171">Error: ${e.message}</p>`;
  }
}

async function loadPlayerCloudScriptPanel(playerId) {
  const listEl = $("cs-player-script-list");
  const logEl = $("cs-player-log");
  listEl.innerHTML = `<p style="color:var(--steel-400)">Loading…</p>`;
  logEl.innerHTML = `<p style="color:var(--steel-400)">Loading…</p>`;

  try {
    const { scripts } = await api("/api/admin/cloudscript");
    listEl.innerHTML = scripts.length
      ? scripts.map((s) => `
          <div class="row-item">
            <span>${s.script_name}${s.description ? ` — ${s.description}` : ""}</span>
            <button class="small-btn run-script-btn" data-name="${s.script_name}">Run</button>
          </div>`).join("")
      : `<p style="color:var(--steel-400)">No cloud scripts yet — create one in the Cloud Script tab.</p>`;

    document.querySelectorAll(".run-script-btn").forEach((btn) => {
      btn.addEventListener("click", async () => {
        try {
          const result = await api(`/api/admin/cloudscript/${btn.dataset.name}/execute/${playerId}`, { method: "POST" });
          alert(result.success ? `Script ran successfully.` : `Script ran with errors — check the log below.`);
          loadPlayerCloudScriptPanel(playerId);
        } catch (e) {
          alert("Error: " + e.message);
        }
      });
    });
  } catch (e) {
    listEl.innerHTML = `<p style="color:#f87171">Error: ${e.message}</p>`;
  }

  try {
    const { log } = await api(`/api/admin/players/${playerId}/cloudscript-log`);
    logEl.innerHTML = log.length
      ? log.map((entry) => `
          <div class="row-item" style="flex-direction:column; align-items:flex-start;">
            <div style="display:flex; justify-content:space-between; width:100%;">
              <span>${entry.script_name}</span>
              <span class="badge ${entry.success ? "badge-ok" : "badge-banned"}">${entry.success ? "Success" : "Error"}</span>
            </div>
            <span style="color:var(--steel-400); font-size:11px;">${formatDate(entry.executed_at)}</span>
          </div>`).join("")
      : `<p style="color:var(--steel-400)">No executions yet for this player.</p>`;
  } catch (e) {
    logEl.innerHTML = `<p style="color:#f87171">Error: ${e.message}</p>`;
  }
}

$("delete-account-btn").addEventListener("click", async () => {
  const playerId = state.currentPlayerId;
  if (!confirm(`Permanently delete account ${playerId}? This deletes all their data, currency, and inventory. This cannot be undone.`)) return;
  try {
    await api(`/api/admin/players/${playerId}`, { method: "DELETE" });
    showTab("players");
    loadPlayers();
  } catch (e) {
    alert("Error: " + e.message);
  }
});

async function populateSetCurrencyDropdown() {
  const select = $("set-currency-code");
  select.innerHTML = `<option value="">Loading…</option>`;
  try {
    const { currencies } = await api("/api/currencies");
    select.innerHTML = currencies.length
      ? currencies.map((c) => `<option value="${c.code}">${c.name} (${c.code})</option>`).join("")
      : `<option value="">No currencies yet — create one in the Currency tab</option>`;
  } catch (e) {
    select.innerHTML = `<option value="">Failed to load</option>`;
  }
}

$("set-currency-btn").addEventListener("click", async () => {
  const code = $("set-currency-code").value;
  const amount = parseInt($("set-currency-amount").value, 10);
  if (!code || isNaN(amount)) return alert("Enter a currency code and amount.");
  try {
    await api(`/api/admin/players/${state.currentPlayerId}/currency`, { method: "POST", body: { currency_code: code, amount } });
    $("set-currency-code").value = "";
    $("set-currency-amount").value = "";
    openPlayerDetail(state.currentPlayerId);
  } catch (e) {
    alert("Error: " + e.message);
  }
});

$("grant-item-btn").addEventListener("click", async () => {
  const itemId = $("grant-item-id").value.trim();
  const qty = parseInt($("grant-quantity").value, 10) || 1;
  if (!itemId) return alert("Enter an item_id.");
  try {
    await api(`/api/admin/players/${state.currentPlayerId}/inventory/grant`, { method: "POST", body: { item_id: itemId, quantity: qty } });
    $("grant-item-id").value = "";
    openPlayerDetail(state.currentPlayerId);
  } catch (e) {
    alert("Error: " + e.message);
  }
});

// ---- Catalog ----
$("new-item-btn").addEventListener("click", () => showTab("item-create"));

async function loadCatalog() {
  const tbody = document.querySelector("#catalog-table tbody");
  tbody.innerHTML = `<tr><td colspan="5">Loading…</td></tr>`;
  try {
    const { catalog } = await api("/api/catalog");
    tbody.innerHTML = "";
    if (catalog.length === 0) {
      tbody.innerHTML = `<tr><td colspan="5">No items yet. Click "+ New Item" to add one.</td></tr>`;
      return;
    }
    catalog.forEach((item) => {
      const tr = document.createElement("tr");
      tr.innerHTML = `
        <td>${item.item_id}</td>
        <td>${item.name}</td>
        <td>${item.item_class || "—"}</td>
        <td>${item.price} ${item.currency_code}</td>
        <td><button class="small-btn delete-catalog-btn" data-id="${item.item_id}">Delete</button></td>
      `;
      tbody.appendChild(tr);
    });
    document.querySelectorAll(".delete-catalog-btn").forEach((btn) => {
      btn.addEventListener("click", async () => {
        if (!confirm(`Delete catalog item "${btn.dataset.id}"? Existing player inventories keep the item; only future purchases/grants are affected.`)) return;
        try {
          await api(`/api/admin/catalog/${btn.dataset.id}`, { method: "DELETE" });
          loadCatalog();
        } catch (e) {
          alert("Error: " + e.message);
        }
      });
    });
  } catch (e) {
    tbody.innerHTML = `<tr><td colspan="5">Error: ${e.message}</td></tr>`;
  }
}

async function populateCurrencyDropdown() {
  const select = $("item-currency");
  select.innerHTML = `<option value="">Loading currencies…</option>`;
  try {
    const { currencies } = await api("/api/currencies");
    if (currencies.length === 0) {
      select.innerHTML = `<option value="">No currencies yet — create one in the Currency tab first</option>`;
      return;
    }
    select.innerHTML = currencies.map((c) => `<option value="${c.code}">${c.name} (${c.code})</option>`).join("");
  } catch (e) {
    select.innerHTML = `<option value="">Failed to load currencies</option>`;
  }
}

$("catalog-form").addEventListener("submit", async (e) => {
  e.preventDefault();
  $("catalog-form-error").textContent = "";
  const body = {
    item_id: $("item-id").value.trim(),
    name: $("item-name").value.trim(),
    description: $("item-desc").value.trim() || null,
    currency_code: $("item-currency").value,
    price: parseInt($("item-price").value, 10),
    item_class: $("item-class").value.trim() || null,
    icon_url: $("item-icon").value.trim() || null,
  };
  if (!body.currency_code) {
    $("catalog-form-error").textContent = "Select a currency (create one first if the list is empty).";
    return;
  }
  try {
    await api("/api/admin/catalog", { method: "POST", body });
    e.target.reset();
    showTab("catalog");
  } catch (err) {
    $("catalog-form-error").textContent = "Error: " + err.message;
  }
});

// ---- Currencies ----
async function loadCurrencies() {
  const tbody = document.querySelector("#currency-table tbody");
  tbody.innerHTML = `<tr><td colspan="6">Loading…</td></tr>`;
  try {
    const { currencies } = await api("/api/currencies");
    tbody.innerHTML = "";
    if (currencies.length === 0) {
      tbody.innerHTML = `<tr><td colspan="6">No currencies yet. Create one below.</td></tr>`;
      return;
    }
    currencies.forEach((c) => {
      const tr = document.createElement("tr");
      tr.innerHTML = `
        <td>${c.code}</td>
        <td>${c.name}</td>
        <td>${c.initial_amount ?? 0}</td>
        <td>${c.daily_amount ? c.daily_amount + "/day" : "—"}</td>
        <td>${c.daily_max ?? "—"}</td>
        <td><button class="small-btn delete-currency-btn" data-code="${c.code}">Delete</button></td>
      `;
      tbody.appendChild(tr);
    });
    document.querySelectorAll(".delete-currency-btn").forEach((btn) => {
      btn.addEventListener("click", async () => {
        if (!confirm(`Delete currency "${btn.dataset.code}"? Catalog items or player balances using it will keep referencing a currency that no longer exists.`)) return;
        try {
          await api(`/api/admin/currencies/${btn.dataset.code}`, { method: "DELETE" });
          loadCurrencies();
        } catch (e) {
          alert("Error: " + e.message);
        }
      });
    });
  } catch (e) {
    tbody.innerHTML = `<tr><td colspan="6">Error: ${e.message}</td></tr>`;
  }
}

$("currency-form").addEventListener("submit", async (e) => {
  e.preventDefault();
  $("currency-form-error").textContent = "";
  const code = $("new-currency-code").value.trim().toUpperCase();
  const name = $("new-currency-name").value.trim();
  const maxRaw = $("new-currency-max").value.trim();
  if (!code || !name) {
    $("currency-form-error").textContent = "Both code and name are required.";
    return;
  }
  try {
    await api("/api/admin/currencies", {
      method: "POST",
      body: {
        code,
        name,
        initial_amount: parseInt($("new-currency-initial").value, 10) || 0,
        daily_amount: parseInt($("new-currency-daily").value, 10) || 0,
        daily_max: maxRaw ? parseInt(maxRaw, 10) : null,
      },
    });
    e.target.reset();
    loadCurrencies();
  } catch (err) {
    $("currency-form-error").textContent = "Error: " + err.message;
  }
});

// ---- Cloud Script ----
async function loadCloudScripts() {
  const tbody = document.querySelector("#cloudscript-table tbody");
  tbody.innerHTML = `<tr><td colspan="4">Loading…</td></tr>`;
  try {
    const { scripts } = await api("/api/admin/cloudscript");
    tbody.innerHTML = "";
    if (scripts.length === 0) {
      tbody.innerHTML = `<tr><td colspan="4">No scripts yet. Create one below.</td></tr>`;
      return;
    }
    scripts.forEach((s) => {
      const tr = document.createElement("tr");
      tr.innerHTML = `
        <td>${s.script_name}</td>
        <td>${s.description || "—"}</td>
        <td>${s.actions.length} action(s)</td>
        <td><button class="small-btn delete-cloudscript-btn" data-name="${s.script_name}">Delete</button></td>
      `;
      tbody.appendChild(tr);
    });
    document.querySelectorAll(".delete-cloudscript-btn").forEach((btn) => {
      btn.addEventListener("click", async () => {
        if (!confirm(`Delete cloud script "${btn.dataset.name}"? Any Unity code calling it by name will start failing.`)) return;
        try {
          await api(`/api/admin/cloudscript/${btn.dataset.name}`, { method: "DELETE" });
          loadCloudScripts();
        } catch (e) {
          alert("Error: " + e.message);
        }
      });
    });
  } catch (e) {
    tbody.innerHTML = `<tr><td colspan="4">Error: ${e.message}</td></tr>`;
  }
}

$("cloudscript-form").addEventListener("submit", async (e) => {
  e.preventDefault();
  $("cloudscript-form-error").textContent = "";

  const name = $("cs-name").value.trim();
  const description = $("cs-description").value.trim() || null;
  let actions;
  try {
    actions = JSON.parse($("cs-actions").value);
    if (!Array.isArray(actions)) throw new Error("Actions must be a JSON array.");
  } catch (parseErr) {
    $("cloudscript-form-error").textContent = "Invalid JSON in actions: " + parseErr.message;
    return;
  }

  try {
    await api("/api/admin/cloudscript", { method: "POST", body: { script_name: name, description, actions } });
    e.target.reset();
    loadCloudScripts();
  } catch (err) {
    $("cloudscript-form-error").textContent = "Error: " + err.message;
  }
});
