(() => {
  "use strict";

  const tg =
    window.Telegram && window.Telegram.WebApp
      ? window.Telegram.WebApp
      : null;

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

    pendingTopup: null,
    pendingOrder: null,
  };

  function getElement(id) {
    return document.getElementById(id);
  }

  function setText(id, value) {
    const el = getElement(id);
    if (el) el.textContent = String(value ?? "");
  }

  function showToast(msg) {
    const el = getElement("toast");
    if (!el) return;

    el.textContent = String(msg || "Произошла ошибка");
    el.classList.remove("hidden");

    clearTimeout(showToast._t);
    showToast._t = setTimeout(() => {
      el.classList.add("hidden");
    }, 2800);
  }

  function formatUzs(val) {
    const n = Number(val || 0);

    if (!Number.isFinite(n)) {
      return "0 UZS";
    }

    return `${n.toLocaleString("ru-RU").replace(/\u00a0/g, " ")} UZS`;
  }

  function escapeHtml(str) {
    return String(str ?? "")
      .replace(/&/g, "&amp;")
      .replace(/</g, "&lt;")
      .replace(/>/g, "&gt;")
      .replace(/"/g, "&quot;")
      .replace(/'/g, "&#039;");
  }

  function createIdempotencyKey(prefix) {
    let randomPart = "";

    if (
      window.crypto &&
      typeof window.crypto.randomUUID === "function"
    ) {
      randomPart = window.crypto.randomUUID();
    } else if (
      window.crypto &&
      typeof window.crypto.getRandomValues === "function"
    ) {
      const bytes = new Uint8Array(16);
      window.crypto.getRandomValues(bytes);

      randomPart = Array.from(bytes)
        .map((byte) => byte.toString(16).padStart(2, "0"))
        .join("");
    } else {
      randomPart =
        Date.now().toString(36) +
        "_" +
        Math.random().toString(36).slice(2) +
        "_" +
        Math.random().toString(36).slice(2);
    }

    return `${prefix}_${randomPart}`;
  }

  async function apiRequest(path, opts = {}) {
    if (!initData) {
      throw new Error("Откройте магазин через Telegram Mini App");
    }

    const headers = {
      "Content-Type": "application/json",
      ...(opts.headers || {}),
      "X-Telegram-Init-Data": initData,
    };

    const resp = await fetch(path, {
      method: opts.method || "GET",
      headers,
      body:
        opts.body !== undefined
          ? JSON.stringify(opts.body)
          : undefined,
    });

    const payload = await resp.json().catch(() => ({}));

    if (!resp.ok || payload.ok === false) {
      const message =
        payload.error ||
        payload.message ||
        `Ошибка запроса (${resp.status})`;

      throw new Error(message);
    }

    // Новый API возвращает:
    // { ok: true, message: "...", data: {...} }
    // Поддерживаем и старый формат ответа.
    if (
      payload &&
      Object.prototype.hasOwnProperty.call(payload, "data")
    ) {
      return payload.data;
    }

    return payload;
  }

  function switchTab(tabName) {
    state.tab = tabName;

    document.querySelectorAll(".tab-panel").forEach((panel) => {
      panel.classList.toggle(
        "active",
        panel.id === `tab-${tabName}`
      );
    });

    document.querySelectorAll(".nav-btn").forEach((button) => {
      button.classList.toggle(
        "active",
        button.dataset.tab === tabName
      );
    });

    if (tabName === "history" && initData) {
      loadHistory().catch((error) => {
        showToast(error.message);
      });
    }
  }

  function renderProfileAndWallet() {
    const me = state.me;

    if (me && me.brand) {
      setText("brandTitle", String(me.brand).toUpperCase());
      document.title = me.brand;
    }

    const wallet = me && me.wallet ? me.wallet : {};

    const balance = wallet.balance_uzs ?? 0;
    const bonus = wallet.bonus_uzs ?? 0;

    const stats = me && me.stats ? me.stats : {};
    const ordersCount = stats.orders_count ?? 0;

    setText("homeBalance", formatUzs(balance));
    setText("homeBonus", formatUzs(bonus));
    setText("profileBalance", formatUzs(balance));
    setText("profileBonus", formatUzs(bonus));
    setText("profileOrdersCount", ordersCount);

    const user = me ? me.user : null;

    const fullName = user
      ? (
          [user.first_name, user.last_name]
            .filter(Boolean)
            .join(" ") || "Пользователь"
        )
      : "Гость";

    const handle =
      user && user.username
        ? `@${user.username}`
        : "без username";

    const uidText = user
      ? `ID: ${user.id}`
      : "ID: —";

    setText(
      "userChip",
      user && user.username
        ? `@${user.username}`
        : fullName
    );

    setText("profileName", fullName);
    setText("profileHandle", `${handle} · ${uidText}`);

    if (me && me.topup_config) {
      const min = me.topup_config.min_uzs;
      const max = me.topup_config.max_uzs;

      setText(
        "settingsLimits",
        `От ${formatUzs(min)} до ${formatUzs(max)}`
      );

      setText(
        "topupLimitsHint",
        `Допустимая сумма: от ${formatUzs(min)} до ${formatUzs(max)}.`
      );
    }
  }

  function renderCategories() {
    const homeCats = getElement("homeCategories");
    const filterBar = getElement("categoryFilterBar");

    if (!homeCats || !filterBar) return;

    homeCats.innerHTML = "";

    state.categories.forEach((category) => {
      const tile = document.createElement("button");

      tile.type = "button";
      tile.className = "cat-tile";

      tile.innerHTML = `
        <div class="cat-tile-title">
          ${escapeHtml(category.title)}
        </div>
        <div class="cat-tile-sub">Открыть</div>
      `;

      tile.addEventListener("click", () => {
        state.category = category.slug;
        renderCatalog();
        switchTab("catalog");
      });

      homeCats.appendChild(tile);
    });

    filterBar.innerHTML = "";

    const allButton = document.createElement("button");

    allButton.type = "button";
    allButton.className =
      `chip-btn ${state.category === "all" ? "active" : ""}`;

    allButton.dataset.slug = "all";
    allButton.textContent = "Все";

    allButton.addEventListener("click", () => {
      state.category = "all";
      renderCatalog();
    });

    filterBar.appendChild(allButton);

    state.categories.forEach((category) => {
      const button = document.createElement("button");

      button.type = "button";
      button.className =
        `chip-btn ${
          state.category === category.slug ? "active" : ""
        }`;

      button.dataset.slug = category.slug;
      button.textContent = category.title;

      button.addEventListener("click", () => {
        state.category = category.slug;
        renderCatalog();
      });

      filterBar.appendChild(button);
    });
  }

  function buildProductCard(product) {
    const card = document.createElement("div");

    card.className = "product-card";

    const modeLabel =
      product.fulfillment_mode === "manual"
        ? "Ручная"
        : "Авто";

    card.innerHTML = `
      <div class="product-top">
        <div>
          <div class="product-title">
            ${escapeHtml(product.title)}
          </div>

          <div class="muted">
            ${escapeHtml(product.description || "")}
          </div>
        </div>

        <span class="product-mode">
          ${escapeHtml(modeLabel)}
        </span>
      </div>

      <div class="product-bottom">
        <div class="product-price">
          ${formatUzs(product.price_uzs)}
        </div>

        <button class="btn btn-gold btn-sm" type="button">
          Купить
        </button>
      </div>
    `;

    const buyButton = card.querySelector("button");

    if (buyButton) {
      buyButton.addEventListener("click", () => {
        openOrderModal(product);
      });
    }

    return card;
  }

  function renderCatalog() {
    const popularEl = getElement("homePopular");
    const catalogEl = getElement("catalogList");

    if (!popularEl || !catalogEl) return;

    popularEl.innerHTML = "";

    state.products
      .slice(0, 3)
      .forEach((product) => {
        popularEl.appendChild(buildProductCard(product));
      });

    document
      .querySelectorAll("#categoryFilterBar .chip-btn")
      .forEach((button) => {
        button.classList.toggle(
          "active",
          button.dataset.slug === state.category
        );
      });

    const filtered =
      state.category === "all"
        ? state.products
        : state.products.filter(
            (product) =>
              product.category_slug === state.category
          );

    catalogEl.innerHTML = "";

    if (!filtered.length) {
      catalogEl.innerHTML = `
        <div class="empty-card">
          <p class="muted">
            В выбранной категории пока нет товаров.
          </p>
        </div>
      `;

      return;
    }

    filtered.forEach((product) => {
      catalogEl.appendChild(buildProductCard(product));
    });
  }

  function renderHistory() {
    const history = state.history || {};

    const topups = Array.isArray(history.topups)
      ? history.topups
      : [];

    const orders = Array.isArray(history.orders)
      ? history.orders
      : [];

    const transactions = Array.isArray(history.transactions)
      ? history.transactions
      : [];

    const topupsEl = getElement("topupsHistoryList");
    const ordersEl = getElement("ordersHistoryList");
    const txEl = getElement("txHistoryList");

    if (!topupsEl || !ordersEl || !txEl) return;

    topupsEl.innerHTML = topups.length
      ? topups.map((topup) => `
          <div class="history-item">
            <div class="history-row-top">
              <span>Заявка #${escapeHtml(topup.id)}</span>
              <span>${formatUzs(topup.amount_uzs)}</span>
            </div>

            <div class="history-meta">
              Статус: ${escapeHtml(topup.status)}
            </div>
          </div>
        `).join("")
      : `
        <div class="empty-card">
          <p class="muted">
            Заявок на пополнение пока нет.
          </p>
        </div>
      `;

    ordersEl.innerHTML = orders.length
      ? orders.map((order) => {
          const total =
            order.total_price_uzs ??
            order.total_uzs ??
            0;

          return `
            <div class="history-item">
              <div class="history-row-top">
                <span>
                  ${escapeHtml(order.product_title)}
                  × ${escapeHtml(order.qty)}
                </span>

                <span>${formatUzs(total)}</span>
              </div>

              <div class="history-meta">
                Получатель: ${escapeHtml(order.recipient || "—")}
                · Статус: ${escapeHtml(order.status)}
              </div>
            </div>
          `;
        }).join("")
      : `
        <div class="empty-card">
          <p class="muted">Заказов пока нет.</p>
        </div>
      `;

    txEl.innerHTML = transactions.length
      ? transactions.map((transaction) => {
          const title =
            transaction.description ||
            transaction.note ||
            transaction.transaction_type ||
            transaction.kind ||
            "Операция";

          const balanceAfter =
            transaction.balance_after ??
            transaction.balance_after_uzs ??
            0;

          const bonusAfter =
            transaction.bonus_after ??
            transaction.bonus_after_uzs ??
            0;

          return `
            <div class="history-item">
              <div class="history-row-top">
                <span>${escapeHtml(title)}</span>
                <span>${formatUzs(transaction.amount_uzs)}</span>
              </div>

              <div class="history-meta">
                Баланс после: ${formatUzs(balanceAfter)}
                · Бонусы: ${formatUzs(bonusAfter)}
              </div>
            </div>
          `;
        }).join("")
      : `
        <div class="empty-card">
          <p class="muted">
            Операций по кошельку пока нет.
          </p>
        </div>
      `;
  }

  async function loadMeAndProducts() {
    const authNotice = getElement("authNotice");

    if (!initData) {
      if (authNotice) {
        authNotice.classList.remove("hidden");
      }

      renderProfileAndWallet();
      return;
    }

    const [
      meData,
      categoriesData,
      productsData,
    ] = await Promise.all([
      apiRequest("/api/me"),
      apiRequest("/api/categories"),
      apiRequest("/api/products"),
    ]);

    state.me = meData || {};

    state.categories = Array.isArray(categoriesData)
      ? categoriesData
      : [];

    state.products = Array.isArray(productsData)
      ? productsData
      : [];

    if (authNotice) {
      authNotice.classList.add("hidden");
    }

    renderProfileAndWallet();
    renderCategories();
    renderCatalog();
  }

  async function loadHistory() {
    if (!initData) return;

    const historyData = await apiRequest("/api/history");

    state.history = historyData || {
      topups: [],
      orders: [],
      transactions: [],
    };

    renderHistory();
  }

  function openTopupModal() {
    const amountInput = getElement("topupAmountInput");
    const requisitesBox = getElement("topupRequisitesBox");
    const modal = getElement("topupModal");

    if (amountInput) amountInput.value = "";
    if (requisitesBox) requisitesBox.classList.add("hidden");
    if (modal) modal.classList.remove("hidden");

    state.pendingTopup = null;
  }

  function openOrderModal(product) {
    state.selectedProduct = product;
    state.pendingOrder = null;

    setText("orderModalTitle", product.title);
    setText(
      "orderModalDesc",
      product.description || ""
    );
    setText(
      "orderModalPrice",
      formatUzs(product.price_uzs)
    );

    const qtyInput = getElement("orderQtyInput");
    if (qtyInput) qtyInput.value = "1";

    const recipientInput = getElement("orderRecipientInput");

    if (
      recipientInput &&
      !recipientInput.value &&
      state.me &&
      state.me.user &&
      state.me.user.username
    ) {
      recipientInput.value =
        `@${state.me.user.username}`;
    }

    const modal = getElement("orderModal");
    if (modal) modal.classList.remove("hidden");
  }

  function bindClick(id, callback) {
    const element = getElement(id);

    if (element) {
      element.addEventListener("click", callback);
    }
  }

  function initializeEvents() {
    document.querySelectorAll(".nav-btn").forEach((button) => {
      button.addEventListener("click", () => {
        switchTab(button.dataset.tab);
      });
    });

    document.querySelectorAll("[data-jump-tab]").forEach((button) => {
      button.addEventListener("click", () => {
        switchTab(button.dataset.jumpTab);
      });
    });

    bindClick("goCatalogBtn", () => {
      switchTab("catalog");
    });

    bindClick("refreshHistoryBtn", () => {
      loadHistory()
        .then(() => showToast("История обновлена"))
        .catch((error) => showToast(error.message));
    });

    bindClick("openTopupBtn", openTopupModal);

    bindClick("closeTopupModal", () => {
      const modal = getElement("topupModal");
      if (modal) modal.classList.add("hidden");
    });

    const submitTopupButton = getElement("submitTopupBtn");

    if (submitTopupButton) {
      submitTopupButton.addEventListener("click", async () => {
        if (state.submittingTopup) return;

        if (!initData) {
          showToast("Откройте магазин в Telegram Mini App");
          return;
        }

        const amountInput = getElement("topupAmountInput");
        const amount = Number(amountInput?.value);

        if (!Number.isSafeInteger(amount) || amount <= 0) {
          showToast("Введите корректную сумму");
          return;
        }

        state.submittingTopup = true;
        submitTopupButton.disabled = true;

        try {
          // Повтор запроса с той же суммой использует тот же ключ.
          // Новая сумма получает новый ключ.
          if (
            !state.pendingTopup ||
            state.pendingTopup.amount !== amount
          ) {
            state.pendingTopup = {
              amount,
              key: createIdempotencyKey("topup"),
            };
          }

          const result = await apiRequest("/api/topups", {
            method: "POST",
            body: {
              amount_uzs: amount,
              idempotency_key: state.pendingTopup.key,
            },
          });

          const topup = result.topup || result;

          setText(
            "topupRequisitesText",
            result.requisites || ""
          );

          const requisitesBox = getElement("topupRequisitesBox");

          if (requisitesBox) {
            requisitesBox.classList.remove("hidden");
          }

          showToast(
            `Заявка #${topup.id} создана`
          );

          state.pendingTopup = null;

          if (state.tab === "history") {
            await loadHistory();
          }
        } catch (error) {
          // Ключ сохраняется, чтобы повторный запрос
          // не создал вторую заявку.
          showToast(error.message);
        } finally {
          state.submittingTopup = false;
          submitTopupButton.disabled = false;
        }
      });
    }

    bindClick("closeOrderModal", () => {
      const modal = getElement("orderModal");
      if (modal) modal.classList.add("hidden");
    });

    const confirmOrderButton = getElement("confirmOrderBtn");

    if (confirmOrderButton) {
      confirmOrderButton.addEventListener("click", async () => {
        if (state.submittingOrder) return;

        if (!initData) {
          showToast("Откройте магазин в Telegram Mini App");
          return;
        }

        if (!state.selectedProduct) return;

        const recipientInput = getElement("orderRecipientInput");
        const qtyInput = getElement("orderQtyInput");

        const recipient = recipientInput
          ? recipientInput.value.trim()
          : "";

        const qty = Number(qtyInput?.value || 1);

        if (!Number.isSafeInteger(qty) || qty < 1 || qty > 50) {
          showToast("Количество должно быть от 1 до 50");
          return;
        }

        const productId = Number(state.selectedProduct.id);

        if (!Number.isSafeInteger(productId) || productId <= 0) {
          showToast("Некорректный товар");
          return;
        }

        const fingerprint = JSON.stringify({
          product_id: productId,
          qty,
          recipient,
        });

        // Один ключ для повторной отправки того же заказа.
        // Новый заказ получает новый уникальный ключ.
        if (
          !state.pendingOrder ||
          state.pendingOrder.fingerprint !== fingerprint
        ) {
          state.pendingOrder = {
            fingerprint,
            key: createIdempotencyKey("order"),
          };
        }

        state.submittingOrder = true;
        confirmOrderButton.disabled = true;

        try {
          const result = await apiRequest("/api/orders", {
            method: "POST",
            body: {
              product_id: productId,
              qty,
              recipient,
              idempotency_key: state.pendingOrder.key,
            },
          });

          const order = result.order || result;

          state.pendingOrder = null;

          const modal = getElement("orderModal");

          if (modal) {
            modal.classList.add("hidden");
          }

          await loadMeAndProducts();

          if (state.tab === "history") {
            await loadHistory();
          }

          showToast(`Заказ #${order.id} оформлен`);
        } catch (error) {
          // Не меняем ключ при ошибке:
          // повторная отправка не должна создать дубль.
          showToast(error.message);
        } finally {
          state.submittingOrder = false;
          confirmOrderButton.disabled = false;
        }
      });
    }
  }

  document.addEventListener("DOMContentLoaded", () => {
    initializeEvents();

    loadMeAndProducts().catch((error) => {
      showToast(error.message);
    });
  });
})();