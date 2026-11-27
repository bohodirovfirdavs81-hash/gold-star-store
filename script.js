(() => {
  const tg = window.Telegram && window.Telegram.WebApp ? window.Telegram.WebApp : null;
  if (tg) {
    tg.ready();
    tg.expand();
  }

  const initData = tg ? (tg.initData || "") : "";
  const state = {
    tab: "home",
    category: "all",
    me: null,
    categories: [],
    products: [],
    history: null,
    selectedProduct: null,
    submittingTopup: false,
    submittingOrder: false,
  };

  function showToast(msg) {
    const el = document.getElementById("toast");
    if (!el) return;
    el.textContent = msg;
    el.classList.remove("hidden");
    clearTimeout(showToast._t);
    showToast._t = setTimeout(() => el.classList.add("hidden"), 2800);
  }

  function formatUzs(val) {
    const n = Number(val || 0);
    return `${n.toLocaleString("ru-RU").replace(/\u00a0/g, " ")} UZS`;
  }

  function escapeHtml(str) {
    return String(str || "")
      .replace(/&/g, "&amp;")
      .replace(/</g, "&lt;")
      .replace(/>/g, "&gt;")
      .replace(/"/g, "&quot;");
  }

  async function apiRequest(path, opts = {}) {
    const headers = {
      "Content-Type": "application/json",
      ...(opts.headers || {}),
    };
    if (initData) {
      headers["X-Telegram-Init-Data"] = initData;
    }
    const resp = await fetch(path, {
      method: opts.method || "GET",
      headers,
      body: opts.body ? JSON.stringify(opts.body) : undefined,
    });
    const data = await resp.json().catch(() => ({}));
    if (!resp.ok || data.ok === false) {
      throw new Error(data.error || `Ошибка запроса (${resp.status})`);
    }
    return data;
  }

  function switchTab(tabName) {
    state.tab = tabName;
    document.querySelectorAll(".tab-panel").forEach((p) => {
      p.classList.toggle("active", p.id === `tab-${tabName}`);
    });
    document.querySelectorAll(".nav-btn").forEach((b) => {
      b.classList.toggle("active", b.dataset.tab === tabName);
    });
    if (tabName === "history" && initData) {
      loadHistory().catch((e) => showToast(e.message));
    }
  }

  function renderProfileAndWallet() {
    const me = state.me;
    if (me && me.brand) {
      document.getElementById("brandTitle").textContent = String(me.brand).toUpperCase();
      document.title = me.brand;
    }

    const bal = me && me.wallet ? me.wallet.balance_uzs : 0;
    const bonus = me && me.wallet ? me.wallet.bonus_uzs : 0;
    const ordersCount = me && me.stats ? me.stats.orders_count : 0;

    document.getElementById("homeBalance").textContent = formatUzs(bal);
    document.getElementById("homeBonus").textContent = formatUzs(bonus);
    document.getElementById("profileBalance").textContent = formatUzs(bal);
    document.getElementById("profileBonus").textContent = formatUzs(bonus);
    document.getElementById("profileOrdersCount").textContent = String(ordersCount);

    const u = me ? me.user : null;
    const fullName = u ? [u.first_name, u.last_name].filter(Boolean).join(" ") || "Пользователь" : "Гость";
    const handle = u && u.username ? `@${u.username}` : "без username";
    const uidText = u ? `ID: ${u.id}` : "ID: —";

    document.getElementById("userChip").textContent = u && u.username ? `@${u.username}` : fullName;
    document.getElementById("profileName").textContent = fullName;
    document.getElementById("profileHandle").textContent = `${handle} · ${uidText}`;

    if (me && me.topup_config) {
      document.getElementById("settingsLimits").textContent =
        `От ${formatUzs(me.topup_config.min_uzs)} до ${formatUzs(me.topup_config.max_uzs)}`;
      document.getElementById("topupLimitsHint").textContent =
        `Допустимая сумма: от ${formatUzs(me.topup_config.min_uzs)} до ${formatUzs(me.topup_config.max_uzs)}.`;
    }
  }

  function renderCategories() {
    const homeCats = document.getElementById("homeCategories");
    const filterBar = document.getElementById("categoryFilterBar");

    homeCats.innerHTML = "";
    state.categories.forEach((c) => {
      const tile = document.createElement("button");
      tile.type = "button";
      tile.className = "cat-tile";
      tile.innerHTML = `<div class="cat-tile-title">${escapeHtml(c.title)}</div><div class="cat-tile-sub">Открыть</div>`;
      tile.addEventListener("click", () => {
        state.category = c.slug;
        renderCatalog();
        switchTab("catalog");
      });
      homeCats.appendChild(tile);
    });

    filterBar.innerHTML = "";
    const allBtn = document.createElement("button");
    allBtn.type = "button";
    allBtn.className = `chip-btn ${state.category === "all" ? "active" : ""}`;
    allBtn.dataset.slug = "all";
    allBtn.textContent = "Все";
    allBtn.addEventListener("click", () => {
      state.category = "all";
      renderCatalog();
    });
    filterBar.appendChild(allBtn);

    state.categories.forEach((c) => {
      const btnEl = document.createElement("button");
      btnEl.type = "button";
      btnEl.className = `chip-btn ${state.category === c.slug ? "active" : ""}`;
      btnEl.dataset.slug = c.slug;
      btnEl.textContent = c.title;
      btnEl.addEventListener("click", () => {
        state.category = c.slug;
        renderCatalog();
      });
      filterBar.appendChild(btnEl);
    });
  }

  function buildProductCard(prod) {
    const card = document.createElement("div");
    card.className = "product-card";
    const modeLabel = prod.fulfillment_mode === "manual" ? "Ручная" : "Авто";
    card.innerHTML = `
      <div class="product-top">
        <div>
          <div class="product-title">${escapeHtml(prod.title)}</div>
          <div class="muted">${escapeHtml(prod.description || "")}</div>
        </div>
        <span class="product-mode">${escapeHtml(modeLabel)}</span>
      </div>
      <div class="product-bottom">
        <div class="product-price">${formatUzs(prod.price_uzs)}</div>
        <button class="btn btn-gold btn-sm" type="button">Купить</button>
      </div>
    `;
    card.querySelector("button").addEventListener("click", () => openOrderModal(prod));
    return card;
  }

  function renderCatalog() {
    const popularEl = document.getElementById("homePopular");
    const catalogEl = document.getElementById("catalogList");

    popularEl.innerHTML = "";
    state.products.slice(0, 3).forEach((p) => popularEl.appendChild(buildProductCard(p)));

    document.querySelectorAll("#categoryFilterBar .chip-btn").forEach((btnEl) => {
      btnEl.classList.toggle("active", btnEl.dataset.slug === state.category);
    });

    const filtered = state.category === "all"
      ? state.products
      : state.products.filter((p) => p.category_slug === state.category);

    catalogEl.innerHTML = "";
    if (!filtered.length) {
      catalogEl.innerHTML = `<div class="empty-card"><p class="muted">В выбранной категории пока нет товаров.</p></div>`;
      return;
    }
    filtered.forEach((p) => catalogEl.appendChild(buildProductCard(p)));
  }

  function renderHistory() {
    const hData = state.history || { topups: [], orders: [], transactions: [] };
    const topupsEl = document.getElementById("topupsHistoryList");
    const ordersEl = document.getElementById("ordersHistoryList");
    const txEl = document.getElementById("txHistoryList");

    topupsEl.innerHTML = hData.topups.length
      ? hData.topups.map((t) => `
          <div class="history-item">
            <div class="history-row-top">
              <span>Заявка #${t.id}</span>
              <span>${formatUzs(t.amount_uzs)}</span>
            </div>
            <div class="history-meta">Статус: ${escapeHtml(t.status)}</div>
          </div>
        `).join("")
      : `<div class="empty-card"><p class="muted">Заявок на пополнение пока нет.</p></div>`;

    ordersEl.innerHTML = hData.orders.length
      ? hData.orders.map((o) => `
          <div class="history-item">
            <div class="history-row-top">
              <span>${escapeHtml(o.product_title)} × ${o.qty}</span>
              <span>${formatUzs(o.total_uzs)}</span>
            </div>
            <div class="history-meta">Получатель: ${escapeHtml(o.recipient)} · Статус: ${escapeHtml(o.status)}</div>
          </div>
        `).join("")
      : `<div class="empty-card"><p class="muted">Заказов пока нет.</p></div>`;

    txEl.innerHTML = hData.transactions.length
      ? hData.transactions.map((tx) => `
          <div class="history-item">
            <div class="history-row-top">
              <span>${escapeHtml(tx.note || tx.kind)}</span>
              <span>${formatUzs(tx.amount_uzs)}</span>
            </div>
            <div class="history-meta">Баланс после: ${formatUzs(tx.balance_after_uzs)} · Бонусы: ${formatUzs(tx.bonus_after_uzs)}</div>
          </div>
        `).join("")
      : `<div class="empty-card"><p class="muted">Операций по кошельку пока нет.</p></div>`;
  }

  async function loadMeAndProducts() {
    if (!initData) {
      document.getElementById("authNotice").classList.remove("hidden");
      renderProfileAndWallet();
      return;
    }
    const [meRes, prodRes] = await Promise.all([
      apiRequest("/api/me"),
      apiRequest("/api/products"),
    ]);
    state.me = meRes;
    state.categories = prodRes.categories || [];
    state.products = prodRes.products || [];
    renderProfileAndWallet();
    renderCategories();
    renderCatalog();
  }

  async function loadHistory() {
    if (!initData) return;
    state.history = await apiRequest("/api/history");
    renderHistory();
  }

  function openTopupModal() {
    document.getElementById("topupAmountInput").value = "";
    document.getElementById("topupRequisitesBox").classList.add("hidden");
    document.getElementById("topupModal").classList.remove("hidden");
  }

  function openOrderModal(prod) {
    state.selectedProduct = prod;
    document.getElementById("orderModalTitle").textContent = prod.title;
    document.getElementById("orderModalDesc").textContent = prod.description || "";
    document.getElementById("orderModalPrice").textContent = formatUzs(prod.price_uzs);
    document.getElementById("orderQtyInput").value = "1";
    const recInput = document.getElementById("orderRecipientInput");
    if (!recInput.value && state.me && state.me.user && state.me.user.username) {
      recInput.value = `@${state.me.user.username}`;
    }
    document.getElementById("orderModal").classList.remove("hidden");
  }

  document.addEventListener("DOMContentLoaded", () => {
    document.querySelectorAll(".nav-btn").forEach((b) => {
      b.addEventListener("click", () => switchTab(b.dataset.tab));
    });
    document.querySelectorAll("[data-jump-tab]").forEach((b) => {
      b.addEventListener("click", () => switchTab(b.dataset.jumpTab));
    });
    document.getElementById("goCatalogBtn").addEventListener("click", () => switchTab("catalog"));
    document.getElementById("refreshHistoryBtn").addEventListener("click", () => {
      loadHistory().then(() => showToast("История обновлена")).catch((e) => showToast(e.message));
    });

    document.getElementById("openTopupBtn").addEventListener("click", openTopupModal);
    document.getElementById("closeTopupModal").addEventListener("click", () => {
      document.getElementById("topupModal").classList.add("hidden");
    });

    const submitTopupBtn = document.getElementById("submitTopupBtn");
    submitTopupBtn.addEventListener("click", async () => {
      if (state.submittingTopup) return;
      if (!initData) {
        return showToast("Откройте магазин в Telegram Mini App");
      }
      const amt = Number(document.getElementById("topupAmountInput").value);
      state.submittingTopup = true;
      submitTopupBtn.disabled = true;
      try {
        const res = await apiRequest("/api/topups", {
          method: "POST",
          body: { amount_uzs: amt },
        });
        document.getElementById("topupRequisitesText").textContent = res.requisites || "";
        document.getElementById("topupRequisitesBox").classList.remove("hidden");
        showToast(`Заявка #${res.topup.id} создана`);
        if (state.tab === "history") {
          await loadHistory();
        }
      } catch (err) {
        showToast(err.message);
      } finally {
        state.submittingTopup = false;
        submitTopupBtn.disabled = false;
      }
    });

    document.getElementById("closeOrderModal").addEventListener("click", () => {
      document.getElementById("orderModal").classList.add("hidden");
    });

    const confirmOrderBtn = document.getElementById("confirmOrderBtn");
    confirmOrderBtn.addEventListener("click", async () => {
      if (state.submittingOrder) return;
      if (!initData) {
        return showToast("Откройте магазин в Telegram Mini App");
      }
      if (!state.selectedProduct) return;
      const recipient = document.getElementById("orderRecipientInput").value.trim();
      const qty = Number(document.getElementById("orderQtyInput").value || 1);
      const idemKey = `web_${state.me?.user?.id || 0}_${state.selectedProduct.id}_${qty}_${recipient}`;
      state.submittingOrder = true;
      confirmOrderBtn.disabled = true;
      try {
        const res = await apiRequest("/api/orders", {
          method: "POST",
          body: {
            product_id: state.selectedProduct.id,
            qty,
            recipient,
            idempotency_key: idemKey,
          },
        });
        document.getElementById("orderModal").classList.add("hidden");
        await loadMeAndProducts();
        if (state.tab === "history") {
          await loadHistory();
        }
        showToast(`Заказ #${res.order.id} оформлен`);
      } catch (err) {
        showToast(err.message);
      } finally {
        state.submittingOrder = false;
        confirmOrderBtn.disabled = false;
      }
    });

    loadMeAndProducts().catch((err) => showToast(err.message));
  });
})();