const state = {
  accounts: [],
  selectedAccount: "",
  domains: [],
  selected: new Set(),
  search: "",
  status: "all",
  settings: {},
  busyTimer: null,
  busyStartedAt: null,
  autoSaveTimer: null
};

const statusText = {
  ok: "正常",
  warning: "预警",
  expired: "已过期",
  dns_failed: "DNS 失效",
  ns_failed: "NS 失效",
  check_error: "检测错误",
  unknown: "未检测"
};

const dnsStatusText = {
  ok: "DNS 正常",
  dns_failed: "DNS 失效",
  ns_failed: "NS 失效",
  check_error: "检测错误"
};

function $(id) {
  return document.getElementById(id);
}

function toast(message, duration = 2600) {
  const node = $("toast");
  node.textContent = message;
  node.classList.add("show");
  window.clearTimeout(node.hideTimer);
  if (duration > 0) {
    node.hideTimer = window.setTimeout(() => node.classList.remove("show"), duration);
  }
}

async function api(path, options = {}) {
  const response = await fetch(path, {
    headers: {"Content-Type": "application/json", ...(options.headers || {})},
    ...options
  });
  if (response.status === 401) {
    window.location.href = "/login";
    return null;
  }
  const data = await response.json().catch(() => ({}));
  if (!response.ok) {
    throw new Error(data.error || "请求失败");
  }
  return data;
}

function selectedItems() {
  return [...state.selected].map((key) => {
    const [account, ...domainParts] = key.split("/");
    return {account, domain: domainParts.join("/")};
  });
}

function rowKey(row) {
  return `${row.account}/${row.domain}`;
}

function filenameFromDisposition(value) {
  const match = /filename="?([^"]+)"?/i.exec(value || "");
  return match ? match[1] : `selected-domains-${Date.now()}.csv`;
}

function downloadBlob(blob, filename) {
  const url = URL.createObjectURL(blob);
  const link = document.createElement("a");
  link.href = url;
  link.download = filename;
  document.body.appendChild(link);
  link.click();
  link.remove();
  URL.revokeObjectURL(url);
}

function updateSelectionSummary() {
  const count = state.selected.size;
  $("selectedCount").textContent = `已选择 ${count} 项`;
  $("clearSelectionBtn").disabled = count === 0;
  $("exportBtn").disabled = count === 0;
}

function setBusy(isBusy, message = "正在执行") {
  const bar = $("activityBar");
  const text = $("activityText");
  const buttons = [$("checkAllBtn"), $("runSchedulerBtn"), $("syncBtn"), $("deleteBtn"), $("exportBtn")].filter(Boolean);
  window.clearInterval(state.busyTimer);
  state.busyTimer = null;

  if (!isBusy) {
    bar.hidden = true;
    buttons.forEach((button) => {
      button.disabled = false;
      if (button.dataset.label) {
        button.textContent = button.dataset.label;
      }
    });
    return;
  }

  state.busyStartedAt = Date.now();
  buttons.forEach((button) => {
    button.dataset.label ||= button.textContent;
    button.disabled = true;
  });
  bar.hidden = false;
  const render = () => {
    const seconds = Math.floor((Date.now() - state.busyStartedAt) / 1000);
    text.textContent = `${message}，已用时 ${seconds} 秒`;
  };
  render();
  state.busyTimer = window.setInterval(render, 1000);
}

function settingsPayloadFromForm() {
  const payload = {
    scheduleEnabled: $("scheduleEnabled").checked,
    scheduleTime: $("scheduleTime").value,
    thresholdDays: Number($("thresholdDays").value),
    telegramEnabled: $("telegramEnabled").checked,
    telegramChatId: $("telegramChatId").value.trim(),
    telegramMention: $("telegramMention").value.trim(),
    telegramVerifySsl: $("telegramVerifySsl").checked
  };
  const token = $("telegramBotToken").value.trim();
  if (token) payload.telegramBotToken = token;
  return payload;
}

async function saveSettingsFromForm() {
  const data = await api("/api/settings", {
    method: "PATCH",
    body: JSON.stringify(settingsPayloadFromForm())
  });
  $("telegramBotToken").value = "";
  state.settings = data.settings;
  return data.settings;
}

function scheduleSettingsAutoSave() {
  window.clearTimeout(state.autoSaveTimer);
  state.autoSaveTimer = window.setTimeout(async () => {
    try {
      const data = await api("/api/settings", {
        method: "PATCH",
        body: JSON.stringify({
          scheduleEnabled: $("scheduleEnabled").checked,
          scheduleTime: $("scheduleTime").value,
          thresholdDays: Number($("thresholdDays").value),
          telegramEnabled: $("telegramEnabled").checked,
          telegramChatId: $("telegramChatId").value.trim(),
          telegramMention: $("telegramMention").value.trim(),
          telegramVerifySsl: $("telegramVerifySsl").checked
        })
      });
      state.settings = data.settings;
      toast("配置已自动保存");
      await Promise.all([loadDashboard(), loadDomains()]);
    } catch (error) {
      toast(error.message, 6000);
    }
  }, 500);
}

async function loadAccounts() {
  const data = await api("/api/accounts");
  state.accounts = data.accounts;
  if (!$("addAccount").value && data.accounts.length) {
    $("addAccount").value = data.accounts[0].name;
  }
  renderAccounts();
}

async function loadSettings() {
  const data = await api("/api/settings");
  state.settings = data.settings;
  $("scheduleEnabled").checked = Boolean(data.settings.scheduleEnabled);
  $("scheduleTime").value = data.settings.scheduleTime || "09:00";
  $("thresholdDays").value = data.settings.thresholdDays || 14;
  $("telegramEnabled").checked = Boolean(data.settings.telegramEnabled);
  $("telegramBotToken").placeholder = data.settings.telegramBotTokenConfigured
    ? "已保存 token，留空不修改"
    : "Bot Token";
  $("telegramChatId").value = data.settings.telegramChatId || "";
  $("telegramMention").value = data.settings.telegramMention || "@bwops";
  $("telegramVerifySsl").checked = data.settings.telegramVerifySsl !== false;
}

async function loadDashboard() {
  const data = await api("/api/dashboard");
  $("statTotal").textContent = data.counts.total || 0;
  $("statOk").textContent = data.counts.ok || 0;
  $("statWarning").textContent = data.counts.warning || 0;
  $("statExpired").textContent = data.counts.expired || 0;
  $("statDnsIssue").textContent =
    (data.counts.dns_failed || 0) + (data.counts.ns_failed || 0) + (data.counts.check_error || 0);
  renderRunInfo(state.settings);
}

function runInfoLine(label, at, result) {
  if (!at) return `${label}：暂无记录`;
  return `${label}：${at}，${result || ""}`;
}

function renderRunInfo(settings) {
  const node = $("lastRunInfo");
  const legacyIsCheck = (settings.lastResult || "").startsWith("已检测");
  const autoAt = settings.lastAutoRunAt || (legacyIsCheck ? settings.lastRunAt : "");
  node.innerHTML = [
    `最后自动检测：${autoAt || "暂无记录"}`,
    `下次自动检测：${nextScheduleText(settings)}`,
    `最后 Telegram 发送：${settings.lastTelegramAt || "暂无记录"}`
  ].map((line) => `<span>${escapeHtml(line)}</span>`).join("");
}

function nextScheduleText(settings) {
  if (!settings.scheduleEnabled) return "未启用";
  const scheduleTime = settings.scheduleTime || "09:00";
  const now = new Date();
  const today = [
    now.getFullYear(),
    String(now.getMonth() + 1).padStart(2, "0"),
    String(now.getDate()).padStart(2, "0")
  ].join("-");
  const lastAutoRunDate = settings.lastAutoRunDate || "";
  if (lastAutoRunDate === today) return `明天 ${scheduleTime}`;
  return `今天 ${scheduleTime}`;
}

async function loadDomains() {
  const params = new URLSearchParams();
  if (state.selectedAccount) params.set("account", state.selectedAccount);
  if (state.search) params.set("q", state.search);
  if (state.status) params.set("status", state.status);
  const data = await api(`/api/domains?${params.toString()}`);
  state.domains = data.domains;
  state.selected.clear();
  $("selectAll").checked = false;
  renderDomains();
  updateSelectionSummary();
}

function renderAccounts() {
  const select = $("accountSelect");
  select.innerHTML = "";
  const total = state.accounts.reduce((sum, account) => sum + account.domainCount, 0);
  const allOption = document.createElement("option");
  allOption.value = "";
  allOption.textContent = `全部账户（${total} 个）`;
  select.appendChild(allOption);

  state.accounts.forEach((account) => {
    const option = document.createElement("option");
    option.value = account.name;
    option.textContent = `${account.name}（${account.domainCount} 个${account.configured ? " · API" : ""}）`;
    select.appendChild(option);
  });

  select.value = state.selectedAccount || "";
}

function renderDomains() {
  const tbody = $("domainTable");
  tbody.innerHTML = "";
  if (!state.domains.length) {
    tbody.innerHTML = `<tr><td colspan="9">没有匹配的域名</td></tr>`;
    return;
  }

  const attentionStatuses = new Set(["warning", "expired", "dns_failed", "ns_failed", "check_error"]);
  const shouldGroup = state.status === "all";
  const attentionRows = state.domains.filter((row) => attentionStatuses.has(row.status));
  const normalRows = state.domains.filter((row) => !attentionStatuses.has(row.status));
  const groups = shouldGroup
    ? [
        {title: `需要关注（${attentionRows.length}）`, meta: "即将到期、已过期或 DNS / NS 问题的域名", rows: attentionRows},
        {title: `运行正常（${normalRows.length}）`, meta: "状态正常且剩余天数大于阈值的域名", rows: normalRows}
      ]
    : [{title: "", meta: "", rows: state.domains}];

  groups.forEach((group) => {
    if (shouldGroup) {
      const groupRow = document.createElement("tr");
      groupRow.className = "group-row";
      groupRow.innerHTML = `<td colspan="9"><strong>${escapeHtml(group.title)}</strong><span>${escapeHtml(group.meta)}</span></td>`;
      tbody.appendChild(groupRow);
    }
    if (!group.rows.length) {
      const emptyRow = document.createElement("tr");
      emptyRow.className = "empty-group-row";
      emptyRow.innerHTML = `<td colspan="9">暂无域名</td>`;
      tbody.appendChild(emptyRow);
      return;
    }
    group.rows.forEach((row) => {
    const tr = document.createElement("tr");
    const key = rowKey(row);
    const days = row.daysLeft === null || row.daysLeft === undefined ? "-" : `${row.daysLeft} 天`;
    const expires = row.expiresAt || "-";
    if (state.selected.has(key)) tr.classList.add("selected-row");
    tr.innerHTML = `
      <td><input class="row-check" type="checkbox" ${state.selected.has(key) ? "checked" : ""}></td>
      <td><strong>${escapeHtml(row.domain)}</strong></td>
      <td>${escapeHtml(row.account)}</td>
      <td><span class="status-pill status-${row.status}">${statusText[row.status] || row.status}</span></td>
      <td>${expires}</td>
      <td><strong class="days-left status-text-${row.status}">${days}</strong></td>
      <td class="ns-cell">${renderDnsText(row)}</td>
      <td><input class="note-input" value="${escapeAttribute(row.note || "")}" placeholder="备注"></td>
      <td>
        <div class="row-actions">
          <button class="secondary save-row" type="button">保存</button>
          <button class="secondary check-row" type="button">检测</button>
        </div>
      </td>
    `;
    tr.querySelector(".row-check").addEventListener("change", (event) => {
      if (event.target.checked) {
        state.selected.add(key);
        tr.classList.add("selected-row");
      } else {
        state.selected.delete(key);
        tr.classList.remove("selected-row");
      }
      updateSelectionSummary();
    });
    tr.querySelector(".save-row").addEventListener("click", async () => {
      try {
        const note = tr.querySelector(".note-input").value;
        await api(`/api/domains/${row.account}/${row.domain}`, {
          method: "PATCH",
          body: JSON.stringify({note})
        });
        toast("已保存备注");
        await loadDomains();
      } catch (error) {
        toast(error.message, 6000);
      }
    });
    tr.querySelector(".check-row").addEventListener("click", async () => {
      try {
        setBusy(true, `正在检测 ${row.domain}`);
        await api("/api/domains/check", {
          method: "POST",
          body: JSON.stringify({items: [{account: row.account, domain: row.domain}]})
        });
        toast("检测完成", 6000);
        await refreshAll();
      } catch (error) {
        toast(error.message, 9000);
      } finally {
        setBusy(false);
      }
    });
    tbody.appendChild(tr);
    });
  });
}

function renderDnsText(row) {
  if (!row.dnsStatus) return "-";
  const label = dnsStatusText[row.dnsStatus] || "DNS 异常";
  return `${label}${row.ns ? ` · ${escapeHtml(row.ns)}` : ""}`;
}

function escapeHtml(value) {
  return String(value)
    .replaceAll("&", "&amp;")
    .replaceAll("<", "&lt;")
    .replaceAll(">", "&gt;");
}

function escapeAttribute(value) {
  return escapeHtml(value).replaceAll('"', "&quot;");
}

async function refreshAll() {
  await loadSettings();
  await loadAccounts();
  await Promise.all([loadDashboard(), loadDomains()]);
}

async function boot() {
  bindEvents();
  await refreshAll();
}

function bindEvents() {
  $("reloadBtn").addEventListener("click", () => refreshAll().catch((error) => toast(error.message, 6000)));
  $("accountSelect").addEventListener("change", (event) => {
    state.selectedAccount = event.target.value;
    if (state.selectedAccount) {
      $("addAccount").value = state.selectedAccount;
    }
    loadDomains().catch((error) => toast(error.message, 6000));
  });
  $("logoutBtn").addEventListener("click", async () => {
    await api("/api/logout", {method: "POST"});
    window.location.href = "/login";
  });
  $("exportBtn").addEventListener("click", async () => {
    const items = selectedItems();
    if (!items.length) {
      toast("先选择要导出的域名");
      return;
    }
    try {
      const response = await fetch("/api/export.csv", {
        method: "POST",
        headers: {"Content-Type": "application/json"},
        body: JSON.stringify({items})
      });
      if (response.status === 401) {
        window.location.href = "/login";
        return;
      }
      if (!response.ok) {
        const data = await response.json().catch(() => ({}));
        throw new Error(data.error || "导出失败");
      }
      const blob = await response.blob();
      downloadBlob(blob, filenameFromDisposition(response.headers.get("Content-Disposition")));
      toast(`已导出 ${items.length} 个域名`);
    } catch (error) {
      toast(error.message, 6000);
    }
  });
  $("searchInput").addEventListener("input", (event) => {
    state.search = event.target.value.trim();
    window.clearTimeout(state.searchTimer);
    state.searchTimer = window.setTimeout(() => loadDomains().catch((error) => toast(error.message, 6000)), 200);
  });
  $("statusFilter").addEventListener("change", (event) => {
    state.status = event.target.value;
    loadDomains().catch((error) => toast(error.message, 6000));
  });
  $("clearSelectionBtn").addEventListener("click", () => {
    state.selected.clear();
    $("selectAll").checked = false;
    renderDomains();
    updateSelectionSummary();
  });
  $("selectAll").addEventListener("change", (event) => {
    state.selected.clear();
    if (event.target.checked) {
      state.domains.forEach((row) => state.selected.add(rowKey(row)));
    }
    renderDomains();
    updateSelectionSummary();
  });
  ["scheduleEnabled", "scheduleTime", "thresholdDays", "telegramEnabled", "telegramChatId", "telegramMention", "telegramVerifySsl"].forEach((id) => {
    $(id).addEventListener("change", scheduleSettingsAutoSave);
  });
  $("thresholdDays").addEventListener("input", scheduleSettingsAutoSave);
  $("settingsForm").addEventListener("submit", async (event) => {
    event.preventDefault();
    try {
      await saveSettingsFromForm();
      toast("配置已保存");
      await refreshAll();
    } catch (error) {
      toast(error.message, 6000);
    }
  });
  $("runSchedulerBtn").addEventListener("click", async () => {
    try {
      setBusy(true, "正在发送当前预警到 Telegram");
      $("runSchedulerBtn").textContent = "正在发送...";
      await saveSettingsFromForm();
      const data = await api("/api/telegram/send", {method: "POST"});
      toast(data.result || `已检测 ${data.checked} 个域名`, 9000);
      await refreshAll();
    } catch (error) {
      toast(error.message, 9000);
    } finally {
      setBusy(false);
    }
  });
  $("addForm").addEventListener("submit", async (event) => {
    event.preventDefault();
    try {
      const account = $("addAccount").value.trim();
      const domains = $("addDomains").value;
      const data = await api("/api/domains", {
        method: "POST",
        body: JSON.stringify({account, domains})
      });
      toast(`已添加 ${data.added} 个域名`);
      $("addDomains").value = "";
      state.selectedAccount = account;
      await refreshAll();
    } catch (error) {
      toast(error.message, 6000);
    }
  });
  $("deleteBtn").addEventListener("click", async () => {
    const items = selectedItems();
    if (!items.length) {
      toast("先选择要删除的域名");
      return;
    }
    if (!window.confirm(`确认删除 ${items.length} 个域名？`)) return;
    try {
      const data = await api("/api/domains", {
        method: "DELETE",
        body: JSON.stringify({items})
      });
      toast(`已删除 ${data.deleted} 个域名`);
      await refreshAll();
    } catch (error) {
      toast(error.message, 6000);
    }
  });
  $("syncBtn").addEventListener("click", async () => {
    if (!state.selectedAccount) {
      toast("同步 GoDaddy 前请先选择具体账户");
      return;
    }
    try {
      const data = await api(`/api/domains/sync/${state.selectedAccount}`, {method: "POST"});
      toast(`已同步 ${data.synced} 个 GoDaddy 域名`);
      await refreshAll();
    } catch (error) {
      toast(error.message, 6000);
    }
  });
  $("checkAllBtn").addEventListener("click", async () => {
    const items = state.selected.size
      ? selectedItems()
      : state.domains.map((row) => ({account: row.account, domain: row.domain}));
    if (!items.length) {
      toast("没有需要检测的域名");
      return;
    }
    try {
      setBusy(true, `正在检测 ${items.length} 个域名`);
      $("checkAllBtn").textContent = "正在刷新...";
      toast("正在检测，域名多时会稍慢", 0);
      const data = await api("/api/domains/check", {
        method: "POST",
        body: JSON.stringify({items})
      });
      toast(`已检测 ${data.checked} 个域名`, 6000);
      await refreshAll();
    } catch (error) {
      toast(error.message, 9000);
    } finally {
      setBusy(false);
    }
  });
}

boot().catch((error) => toast(error.message, 9000));
