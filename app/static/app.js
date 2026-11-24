"use strict";

const $ = (selector, root = document) => root.querySelector(selector);
const esc = (value) =>
  String(value ?? "").replace(
    /[&<>"']/g,
    (c) =>
      ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" })[
        c
      ],
  );
const icon = (name) =>
  `<svg aria-hidden="true"><use href="/static/icons.svg#${name}"/></svg>`;
const state = {
  config: null,
  catalog: {},
  campaigns: [],
  current: null,
  offerStream: null,
  createRequestId: null,
  busy: false,
};
let toastTimer;
const searchVersions = {};

async function api(path, options = {}) {
  const response = await fetch(`/api${path}`, {
    ...options,
    headers: { "Content-Type": "application/json", ...(options.headers || {}) },
    body: options.body === undefined ? undefined : JSON.stringify(options.body),
  });
  const result = await response
    .json()
    .catch(() => ({ message: "Сервер вернул неожиданный ответ." }));
  if (!response.ok) {
    const fields = result.errors
      ?.map((e) => `${e.field}: ${e.message}`)
      .join("; ");
    const error = new Error(result.message + (fields ? ` ${fields}` : ""));
    Object.assign(error, { code: result.code, details: result });
    throw error;
  }
  return result;
}

function toast(message) {
  clearTimeout(toastTimer);
  $("#toast").textContent = message;
  $("#toast").hidden = false;
  toastTimer = setTimeout(() => {
    $("#toast").hidden = true;
  }, 4200);
}

function showError(error, form = null) {
  const target = form ? $(".form-error", form) : $("#global-error");
  target.textContent = error.message || "Не удалось выполнить действие.";
  target.hidden = false;
  if (error.details?.operation_id) {
    const info = document.createElement("div");
    info.textContent = `Операция: ${error.details.operation_id}. Проверьте её статус в API перед повторным созданием.`;
    target.append(info);
  }
  if (!form) target.scrollIntoView({ behavior: "smooth", block: "nearest" });
}

async function run(work, form = null) {
  if (state.busy) return;
  state.busy = true;
  $("#global-error").hidden = true;
  if (form) $(".form-error", form).hidden = true;
  const enabled = [...document.querySelectorAll("button:not(:disabled)")];
  enabled.forEach((b) => {
    b.disabled = true;
  });
  document.body.setAttribute("aria-busy", "true");
  try {
    await work();
  } catch (error) {
    showError(error, form);
  } finally {
    state.busy = false;
    document.body.removeAttribute("aria-busy");
    enabled.forEach((b) => {
      if (b.isConnected) b.disabled = false;
    });
  }
}

function shortName(name) {
  return name.startsWith(state.config.campaign_prefix)
    ? name.slice(state.config.campaign_prefix.length)
    : name;
}

function renderNav() {
  $("#nav-count").textContent = state.campaigns.length;
  $("#campaign-list").innerHTML = state.campaigns.length
    ? state.campaigns
        .map(
          (c) => `
    <button class="campaign-link ${state.current?.id === c.id ? "active" : ""}" data-action="select-campaign" data-id="${c.id}" ${state.current?.id === c.id ? 'aria-current="page"' : ""}>
      <span class="campaign-dot"></span><span class="campaign-text">${esc(shortName(c.name))}<span class="small-id">ID ${c.id}</span></span>${c.dirty_count ? '<span class="draft-dot" title="Есть черновик"></span>' : ""}
    </button>`,
        )
        .join("")
    : '<p class="sidebar-note">Загрузите кампанию из Keitaro или создайте новую.</p>';
  $("#mobile-campaign").innerHTML =
    state.campaigns
      .map((c) => `<option value="${c.id}">${esc(c.name)}</option>`)
      .join("") || '<option value="">Нет кампаний</option>';
  if (state.current) $("#mobile-campaign").value = state.current.id;
}

const catalogName = (key, id) =>
  state.catalog[key]?.find((r) => r.id === id)?.name ||
  (id ? `ID ${id}` : "Не задано");

function renderCampaign() {
  const c = state.current;
  if (!c) {
    $("#campaign-content").innerHTML =
      `<div class="empty-state"><h2>Ваша первая кампания</h2><p>Создайте кампанию с двумя потоками<br>или загрузите существующую из Keitaro.</p><button class="button secondary" data-action="import">${icon("download")}Загрузить из Keitaro</button><button class="button primary" data-action="create">${icon("plus")}Создать кампанию</button></div>`;
    renderNav();
    return;
  }
  const dirty = c.streams.filter((s) => s.dirty).length;
  const active = c.streams.reduce(
    (n, s) => n + s.offers.filter((o) => o.state === "active").length,
    0,
  );
  const date = new Date(
    c.synced_at.endsWith("Z") || /[+-]\d\d:\d\d$/.test(c.synced_at)
      ? c.synced_at
      : c.synced_at + "Z",
  );
  const synced = new Intl.DateTimeFormat("ru", {
    day: "2-digit",
    month: "2-digit",
    hour: "2-digit",
    minute: "2-digit",
  }).format(date);
  $("#campaign-content").innerHTML = `
    <section class="campaign-panel" aria-label="Выбранная кампания">
      <div class="campaign-top"><div class="campaign-title-block"><div class="campaign-meta"><span>КАМПАНИЯ #${c.id}</span><span class="badge ${c.state === "active" ? "success" : "neutral"}">${c.state === "active" ? "Активна" : "Отключена"}</span>${!c.writable ? '<span class="badge warning">Только просмотр</span>' : ""}</div><h2 class="campaign-title">${esc(c.name)}</h2><div class="campaign-settings"><span>${icon("globe")}${esc(catalogName("domains", c.domain_id))}</span><span>${icon("layers")}${esc(catalogName("groups", c.group_id))}</span><span>${esc(catalogName("traffic_sources", c.traffic_source_id))}</span></div></div>
      <div class="sync-block"><button class="button secondary" data-action="sync" ${dirty ? 'disabled title="Сначала опубликуйте или отмените черновики"' : ""}>${icon("refresh")}Получить потоки</button><p>${dirty ? "Сначала опубликуйте или отмените черновики" : `Синхронизация ${esc(synced)}`}</p></div></div>
      <div class="campaign-stats"><div class="stat"><strong>${c.streams.filter((s) => s.remote_present).length}</strong><span>потока</span></div><div class="stat"><strong>${active}</strong><span>активных офферов</span></div><div class="stat ${dirty ? "has-drafts" : ""}"><strong>${dirty}</strong><span>черновиков</span></div></div>
    </section>
    <div class="section-heading"><h2>Потоки кампании</h2><p>Изменения вступают в силу после публикации</p></div>
    ${
      [...c.streams]
        .sort((a, b) => a.position - b.position)
        .map(renderStream)
        .join("") ||
      '<div class="empty-state"><p>В этой кампании пока нет потоков.</p></div>'
    }`;
  renderNav();
}

function renderStream(s) {
  const writable = state.current.writable && s.remote_present;
  const rows = s.offers
    .map((o) => {
      const active = o.state === "active";
      const label =
        o.state === "removed"
          ? "В архиве"
          : o.state === "disabled"
            ? "Отключён в Keitaro"
            : o.is_new
              ? "Новый оффер"
              : o.changed
                ? "Изменён"
                : "";
      return `<tr class="offer-row ${active ? "" : "removed"}"><td><div class="offer-name-cell"><span class="offer-avatar">${esc(o.name.slice(0, 2).toUpperCase())}</span><div><div class="offer-title">${esc(o.name)}</div><div class="offer-info"><span>#${o.id}</span>${label ? `<span class="${active ? "offer-state" : ""}">${label}</span>` : ""}</div></div></td><td>${active ? `<div class="share-cell"><div class="share-input-wrap"><input type="number" min="0" max="100" step="1" value="${o.share}" aria-label="Доля оффера #${o.id}" data-share data-stream="${s.id}" data-offer="${o.id}" ${!writable ? "disabled" : ""}><span>%</span></div><button class="icon-button pin-button ${o.pinned ? "pinned" : ""}" data-action="pin" data-stream="${s.id}" data-offer="${o.id}" aria-pressed="${o.pinned}" aria-label="${o.pinned ? "Открепить" : "Закрепить"} долю оффера #${o.id}" title="${o.pinned ? "Доля закреплена. Нажмите, чтобы открепить" : "Закрепить долю"}" ${!writable ? "disabled" : ""}>${icon("pin")}</button></div>` : `<span class="share-inactive">${o.state === "disabled" ? esc(o.share) : 0}%</span>`}</td><td class="offer-action"><button class="row-action ${active ? "" : "restore"}" data-action="${active ? "remove" : "restore"}" data-stream="${s.id}" data-offer="${o.id}" aria-label="${active ? "Убрать" : "Вернуть"} оффер #${o.id}" ${!writable ? "disabled" : ""}>${icon(active ? "close" : "refresh")}${active ? "Убрать" : "Вернуть"}</button></td></tr>`;
    })
    .join("");
  let offset = 0;
  const bars = s.offers
    .filter((o) => o.state === "active")
    .map((o) => {
      const x = offset;
      offset += o.share;
      return `<rect x="${x}" width="${o.share}" y="0" height="5"><title>${esc(o.name)}: ${o.share}%</title></rect>`;
    })
    .join("");
  return `<article class="stream-card ${s.dirty ? "dirty" : ""}" data-stream-card="${s.id}"><header class="stream-head"><span class="stream-symbol ${s.editable ? "offer" : ""}">${icon(s.editable ? "layers" : "globe")}</span><div><h3>${esc(s.name)}</h3><div class="stream-subtitle">Поток #${s.id} · ${s.editable ? "Офферы" : "Без офферов"}</div></div><div class="stream-status">${!s.remote_present ? '<span class="badge neutral">Удалён в Keitaro</span>' : s.dirty ? '<span class="badge warning">Не опубликован</span>' : `<span class="clean-status">${icon("check")}Синхронизирован</span>`}</div></header>
    ${
      !s.editable
        ? `<div class="redirect-body">${s.remote_present ? "Поток без офферов. Его фильтры, редиректы и остальные настройки остаются в Keitaro." : "Поток больше не существует в Keitaro. Локальная история сохранена."}</div>`
        : `${s.offers.length ? `<div class="offer-table-wrap"><table class="offer-table"><thead><tr><th scope="col">ОФФЕР</th><th scope="col">ДОЛЯ ТРАФИКА</th><th scope="col">ДЕЙСТВИЕ</th></tr></thead><tbody>${rows}</tbody></table></div>` : '<div class="stream-empty">В потоке пока нет офферов. Добавьте первый, чтобы распределить трафик.</div>'}
      <div class="allocation"><div class="allocation-bars"><svg viewBox="0 0 100 5" preserveAspectRatio="none" role="img" aria-label="Распределение трафика, всего ${s.total_share}%">${bars}</svg></div><span class="allocation-total ${s.total_share !== 100 ? "invalid" : ""}">Всего<strong>${s.total_share}%</strong></span>${s.total_share !== 100 && s.offers.some((o) => o.state === "active") ? `<button class="row-action restore" data-action="rebalance" data-stream="${s.id}" ${!writable ? "disabled" : ""}>Пересчитать</button>` : ""}</div>
      <div class="stream-footer"><button class="button secondary add-offer" data-action="add" data-stream="${s.id}" ${!writable ? "disabled" : ""}>${icon("plus")}Добавить оффер</button><div class="footer-actions"><button class="button quiet" data-action="cancel" data-stream="${s.id}" ${!s.dirty || !writable ? "disabled" : ""}>Отменить</button><button class="button primary" data-action="publish" data-stream="${s.id}" ${!s.dirty || !writable || s.total_share !== 100 ? "disabled" : ""}>Опубликовать</button></div></div>${s.dirty ? `<div class="draft-note">Черновик сохранён локально. ${s.total_share === 100 ? "Keitaro получит изменения после публикации." : "Для публикации нужен активный оффер и сумма долей 100%."}</div>` : ""}`
    }</article>`;
}

async function refreshList() {
  state.campaigns = await api("/campaigns");
}

async function selectCampaign(id) {
  state.current = await api(`/campaigns/${id}`);
  localStorage.setItem("adrobot-selected", String(id));
  renderCampaign();
}

async function refreshCurrent() {
  const id = state.current.id;
  await refreshList();
  await selectCampaign(id);
}

function openDialog(id) {
  const dialog = $(`#${id}`);
  $(".form-error", dialog).hidden = true;
  dialog.showModal();
}

function selectOptions(selector, rows, placeholder = null) {
  const select = $(selector),
    old = select.value;
  select.innerHTML =
    (placeholder ? `<option value="">${esc(placeholder)}</option>` : "") +
    rows
      .map((r) => `<option value="${r.id}">${esc(r.name)} · #${r.id}</option>`)
      .join("");
  if (rows.some((r) => String(r.id) === old)) select.value = old;
  else if (!placeholder) select.selectedIndex = -1;
}

async function loadOffers(target, query = "") {
  const version = (searchVersions[target] || 0) + 1;
  searchVersions[target] = version;
  const result = await api(`/offers?q=${encodeURIComponent(query)}`);
  if (searchVersions[target] !== version) return;
  let rows = result.items;
  if (target === "#offer-select") {
    const s = state.current.streams.find((s) => s.id === state.offerStream);
    rows = rows.filter(
      (o) => !s.offers.some((r) => r.id === o.id && r.state === "active"),
    );
    $("#offer-count").textContent = rows.length
      ? `Найдено: ${rows.length}${result.total > 50 ? ". Уточните поиск: показаны первые 50 совпадений." : ""}`
      : "Нет доступных офферов. Измените запрос.";
  }
  selectOptions(
    target,
    rows,
    target === "#create-offer"
      ? rows.length
        ? "Выберите оффер"
        : "Нет совпадений"
      : null,
  );
}

async function loadRemoteCampaigns(query = "") {
  const version = (searchVersions.import || 0) + 1;
  searchVersions.import = version;
  const rows = await api(`/remote-campaigns?q=${encodeURIComponent(query)}`);
  if (searchVersions.import !== version) return;
  selectOptions(
    "#import-select",
    rows.map((r) => ({
      ...r,
      name: r.name + (r.writable ? "" : " (только просмотр)"),
    })),
  );
}

async function openCreate() {
  $("#create-form").reset();
  state.createRequestId = crypto.randomUUID();
  $("#campaign-name").value = state.config.campaign_prefix;
  $("#prefix-hint").textContent =
    `Обязательный префикс: ${state.config.campaign_prefix}`;
  $("#country").innerHTML = state.config.countries
    .map((c) => `<option value="${c.code}">${c.code} · ${esc(c.name)}</option>`)
    .join("");
  $("#country").value = "AU";
  openDialog("create-dialog");
  try {
    state.catalog = await api("/catalog");
    for (const [target, key] of [
      ["#domain", "domains"],
      ["#group", "groups"],
      ["#source", "traffic_sources"],
    ]) {
      selectOptions(target, state.catalog[key], "Выберите значение");
      if (state.catalog[key].length === 1)
        $(target).value = state.catalog[key][0].id;
    }
    await loadOffers("#create-offer");
  } catch (error) {
    showError(error, $("#create-form"));
  }
}

document.addEventListener("click", (event) => {
  const close = event.target.closest("[data-close]");
  if (close && !state.busy) {
    $(`#${close.dataset.close}`).close();
    return;
  }
  const button = event.target.closest("[data-action]");
  if (!button || button.disabled || state.busy) return;
  const { action } = button.dataset;
  if (action === "create") {
    openCreate();
    return;
  }
  if (action === "import") {
    $("#import-query").value = "";
    openDialog("import-dialog");
    loadRemoteCampaigns().catch((e) => showError(e, $("#import-form")));
    return;
  }
  if (action === "select-campaign") {
    run(() => selectCampaign(Number(button.dataset.id)));
    return;
  }
  if (action === "add") {
    state.offerStream = Number(button.dataset.stream);
    $("#offer-query").value = "";
    openDialog("offer-dialog");
    loadOffers("#offer-select").catch((e) => showError(e, $("#offer-form")));
    return;
  }
  run(async () => {
    if (action === "sync") {
      state.current = await api(`/campaigns/${state.current.id}/sync`, {
        method: "POST",
        body: {},
      });
      await refreshList();
      renderCampaign();
      toast("Потоки получены из Keitaro");
      return;
    }
    const s = state.current.streams.find(
      (s) => s.id === Number(button.dataset.stream),
    );
    let path = `/streams/${s.id}/${action}`;
    const body = { revision: s.revision };
    if (["remove", "restore", "pin"].includes(action)) {
      const o = s.offers.find((o) => o.id === Number(button.dataset.offer));
      path = `/streams/${s.id}/offers/${o.id}/${action}`;
      if (action === "pin")
        Object.assign(body, { pinned: !o.pinned, share: o.share });
    }
    await api(path, { method: "POST", body });
    await refreshCurrent();
    const messages = {
      publish: "Изменения опубликованы в Keitaro",
      cancel: "Черновик отменён",
      remove: "Оффер перемещён в архив черновика",
      restore: "Оффер возвращён в черновик",
      pin: "Распределение долей обновлено",
      rebalance: "Доли пересчитаны",
    };
    toast(messages[action]);
  });
});

document.addEventListener("change", (event) => {
  const input = event.target.closest("[data-share]");
  if (!input) return;
  const s = state.current.streams.find(
    (s) => s.id === Number(input.dataset.stream),
  );
  const o = s.offers.find((o) => o.id === Number(input.dataset.offer));
  const share = Number(input.value);
  if (
    input.value === "" ||
    !Number.isInteger(share) ||
    share < 0 ||
    share > 100
  ) {
    input.value = o.share;
    showError(new Error("Доля должна быть целым числом от 0 до 100."));
    return;
  }
  if (share === o.share) return;
  run(async () => {
    try {
      await api(`/streams/${s.id}/offers/${o.id}/pin`, {
        method: "POST",
        body: { revision: s.revision, pinned: true, share },
      });
    } catch (error) {
      input.value = o.share;
      throw error;
    }
    await refreshCurrent();
    toast("Доля закреплена. Остальной трафик перераспределён.");
  });
});

$("#mobile-campaign").addEventListener("change", (e) =>
  run(() => selectCampaign(Number(e.target.value))),
);

function searchOnInput(selector, work, form) {
  let timer;
  $(selector).addEventListener("input", (event) => {
    clearTimeout(timer);
    const value = event.target.value;
    timer = setTimeout(
      () => work(value).catch((e) => showError(e, $(form))),
      250,
    );
  });
}
searchOnInput(
  "#offer-query",
  (q) => loadOffers("#offer-select", q),
  "#offer-form",
);
searchOnInput(
  "#create-offer-query",
  (q) => loadOffers("#create-offer", q),
  "#create-form",
);
searchOnInput("#import-query", loadRemoteCampaigns, "#import-form");

$("#offer-form").addEventListener("submit", (event) => {
  event.preventDefault();
  const offerId = Number($("#offer-select").value);
  if (!offerId) return;
  run(async () => {
    const s = state.current.streams.find((s) => s.id === state.offerStream);
    await api(`/streams/${s.id}/offers`, {
      method: "POST",
      body: { revision: s.revision, offer_id: offerId },
    });
    $("#offer-dialog").close();
    await refreshCurrent();
    toast("Оффер добавлен в черновик. Доли пересчитаны.");
  }, event.target);
});

$("#import-form").addEventListener("submit", (event) => {
  event.preventDefault();
  const id = Number($("#import-select").value);
  if (!id) return;
  run(async () => {
    state.current = await api(`/campaigns/${id}/sync`, {
      method: "POST",
      body: {},
    });
    await refreshList();
    renderCampaign();
    $("#import-dialog").close();
    toast("Кампания загружена");
  }, event.target);
});

$("#create-form").addEventListener("submit", (event) => {
  event.preventDefault();
  const form = event.target;
  const body = Object.fromEntries(new FormData(form));
  for (const key of ["offer_id", "domain_id", "group_id", "traffic_source_id"])
    body[key] = Number(body[key]);
  body.request_id = state.createRequestId;
  run(async () => {
    state.current = await api("/campaigns", { method: "POST", body });
    await refreshList();
    renderCampaign();
    $("#create-dialog").close();
    toast("Кампания создана. Оба потока настроены.");
  }, form);
});

for (const dialog of document.querySelectorAll("dialog")) {
  dialog.addEventListener("cancel", (e) => {
    if (state.busy) e.preventDefault();
  });
}

async function boot() {
  try {
    state.config = await api("/config");
    const demo = state.config.mode === "demo";
    $("#mode-badge").textContent = demo ? "Деморежим" : "Keitaro API";
    $("#sidebar-mode").textContent = demo
      ? "Локальный демотрекер"
      : "Реальный трекер";
    if (demo) {
      $("#mode-notice").innerHTML =
        "<strong>Деморежим.</strong> Можно создавать кампании и публиковать изменения в локальном демотрекере. Данные сохраняются после перезапуска. Реальный Keitaro не затрагивается.";
      $("#mode-notice").hidden = false;
    }
    const results = await Promise.allSettled([api("/catalog"), refreshList()]);
    if (results[0].status === "fulfilled") state.catalog = results[0].value;
    else showError(results[0].reason);
    if (results[1].status === "rejected") throw results[1].reason;
    if (!state.campaigns.length && demo) {
      state.current = await api("/campaigns/1001/sync", {
        method: "POST",
        body: {},
      });
      await refreshList();
    } else if (state.campaigns.length) {
      const saved = Number(localStorage.getItem("adrobot-selected"));
      const selected =
        state.campaigns.find((c) => c.id === saved) || state.campaigns[0];
      state.current = await api(`/campaigns/${selected.id}`);
    }
    renderCampaign();
  } catch (error) {
    showError(error);
    renderCampaign();
  }
}
boot();
