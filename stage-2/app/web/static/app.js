"use strict";

const TK = {
  session: {
    get() {
      try {
        const raw = sessionStorage.getItem("tk_session");
        return raw ? JSON.parse(raw) : null;
      } catch (e) {
        return null;
      }
    },
    set(token, displayName) {
      sessionStorage.setItem("tk_session", JSON.stringify({ token, displayName }));
    },
    clear() {
      sessionStorage.removeItem("tk_session");
    },
  },
};

function qs(id) {
  return document.getElementById(id);
}

function setText(el, text) {
  if (el) el.textContent = text;
}

// Presence toggles: testids documented as "present only when ..." or "absent" must be
// removed from the DOM entirely, not merely CSS-hidden -- a hidden-but-attached node
// still matches a selector wait. Each toggle remembers its original parent so show()
// can restore it in the same place.
function presence(id) {
  const el = qs(id);
  if (!el) return { el: null, show() {}, hide() {} };
  const parent = el.parentNode;
  let attached = true;
  return {
    el,
    show() {
      el.classList.remove("hidden");
      if (!attached) {
        parent.appendChild(el);
        attached = true;
      }
    },
    hide() {
      if (attached) {
        el.remove();
        attached = false;
      }
    },
  };
}

async function apiFetch(path, { method = "GET", body, token, idempotencyKey } = {}) {
  const headers = {};
  if (body !== undefined) headers["Content-Type"] = "application/json";
  if (token) headers["Authorization"] = `Bearer ${token}`;
  if (idempotencyKey) headers["Idempotency-Key"] = idempotencyKey;
  const response = await fetch(path, {
    method,
    headers,
    body: body !== undefined ? JSON.stringify(body) : undefined,
  });
  const text = await response.text();
  let data = null;
  if (text) {
    try {
      data = JSON.parse(text);
    } catch (e) {
      data = null;
    }
  }
  return { ok: response.ok, status: response.status, data };
}

function tableLabel(detail, tableId) {
  const table = detail.tables.find((t) => t.id === tableId);
  return (table && table.label) || tableId;
}

function tableCapacity(detail, tableId) {
  const table = detail.tables.find((t) => t.id === tableId);
  return table ? table.capacity : 0;
}

function timeOf(startsAtLocal) {
  return startsAtLocal.split("T")[1];
}

// ---- nav, shown on every page -------------------------------------------------------

let navCurrentUser = null;
let navLogoutButton = null;

function renderNav() {
  navCurrentUser = navCurrentUser || presence("current-user");
  navLogoutButton = navLogoutButton || presence("logout-button");
  const session = TK.session.get();
  if (session) {
    setText(navCurrentUser.el, session.displayName);
    navCurrentUser.show();
    navLogoutButton.show();
  } else {
    navCurrentUser.hide();
    navLogoutButton.hide();
  }
  if (navLogoutButton.el && !navLogoutButton.el.dataset.wired) {
    navLogoutButton.el.dataset.wired = "1";
    navLogoutButton.el.addEventListener("click", () => {
      TK.session.clear();
      window.location.href = "/";
    });
  }
}

// ---- signup / login ------------------------------------------------------------------

function initSignup() {
  const form = qs("signup-form");
  if (!form) return;
  const authError = presence("auth-error");
  authError.hide();
  form.addEventListener("submit", async (event) => {
    event.preventDefault();
    authError.hide();
    const email = qs("signup-email").value.trim();
    const password = qs("signup-password").value;
    const displayName = qs("signup-display-name").value.trim();
    try {
      const { ok, data } = await apiFetch("/auth/signup", {
        method: "POST",
        body: { email, password, display_name: displayName },
      });
      if (!ok) {
        setText(authError.el, (data && data.error && data.error.message) || "Signup failed.");
        authError.show();
        return;
      }
      TK.session.set(data.token, data.display_name);
      window.location.href = "/";
    } catch (e) {
      setText(authError.el, "Could not reach the server. Please try again.");
      authError.show();
    }
  });
}

function initLogin() {
  const form = qs("login-form");
  if (!form) return;
  const authError = presence("auth-error");
  authError.hide();
  form.addEventListener("submit", async (event) => {
    event.preventDefault();
    authError.hide();
    const email = qs("login-email").value.trim();
    const password = qs("login-password").value;
    try {
      const { ok, data } = await apiFetch("/auth/login", {
        method: "POST",
        body: { email, password },
      });
      if (!ok) {
        setText(authError.el, (data && data.error && data.error.message) || "Login failed.");
        authError.show();
        return;
      }
      TK.session.set(data.token, data.display_name);
      window.location.href = "/";
    } catch (e) {
      setText(authError.el, "Could not reach the server. Please try again.");
      authError.show();
    }
  });
}

// ---- search, availability grid and booking -------------------------------------------

let searchToken = 0;
let currentRestaurantDetail = null;
let pendingBooking = null;

let searchAuthError = null;
let searchStatus = null;
let noSlots = null;
let availabilityGrid = null;
let bookingForm = null;
let bookingErrorToggle = null;
let bookingUncertainToggle = null;
let confirmationToggle = null;

// Cached once at init, before any container is detached: getElementById cannot find a
// node once its ancestor has been removed from the document, so these must not be
// re-looked-up by id after booking-form / confirmation are hidden.
let bookingSummaryEl = null;
let bookingPartySizeEl = null;
let bookingSubmitEl = null;
let confirmationReferenceEl = null;
let confirmationDetailsEl = null;
let confirmationTablesEl = null;

function isAvailable(slot, ids) {
  if (ids.length === 1) return slot.available_table_ids.includes(ids[0]);
  const wanted = ids.slice().sort().join(",");
  return slot.available_options.some(
    (option) => option.table_ids.length === ids.length &&
               option.table_ids.slice().sort().join(",") === wanted);
}

async function loadRestaurants() {
  const { ok, data } = await apiFetch("/restaurants");
  if (!ok) return;
  const select = qs("restaurant-select");
  select.innerHTML = "";
  for (const r of data.restaurants) {
    const option = document.createElement("option");
    option.value = r.id;
    option.textContent = r.name;
    select.appendChild(option);
  }
}

function renderGrid(detail, availability, search) {
  const partySize = search.partySize;
  const slots = availability.slots;
  if (!slots.length) {
    availabilityGrid.hide();
    noSlots.show();
    return;
  }
  noSlots.hide();
  availabilityGrid.show();
  availabilityGrid.el.innerHTML = "";

  const columns = detail.tables.map((t) => ({ ids: [t.id], label: t.label || t.id }));
  for (const pair of detail.combinable) {
    const capacity = pair.reduce((sum, tid) => sum + tableCapacity(detail, tid), 0);
    if (capacity >= partySize) {
      columns.push({ ids: pair, label: pair.map((tid) => tableLabel(detail, tid)).join(" + ") });
    }
  }

  const table = document.createElement("table");
  table.className = "day-grid";
  const thead = document.createElement("thead");
  const headRow = document.createElement("tr");
  headRow.appendChild(document.createElement("th"));
  for (const column of columns) {
    const th = document.createElement("th");
    th.textContent = column.label;
    headRow.appendChild(th);
  }
  thead.appendChild(headRow);
  table.appendChild(thead);

  const tbody = document.createElement("tbody");
  for (const slot of slots) {
    const row = document.createElement("tr");
    const timeHeader = document.createElement("th");
    timeHeader.scope = "row";
    timeHeader.textContent = timeOf(slot.starts_at_local);
    row.appendChild(timeHeader);
    for (const column of columns) {
      const cell = document.createElement("td");
      const button = document.createElement("button");
      button.type = "button";
      button.className = "slot-cell";
      button.setAttribute("data-testid", `slot-${column.ids.join("+")}-${timeOf(slot.starts_at_local)}`);
      const available = isAvailable(slot, column.ids);
      button.setAttribute("data-available", available ? "true" : "false");
      button.textContent = available ? "Available" : "—";
      button.disabled = !available;
      button.setAttribute(
        "aria-label", `${column.label} at ${timeOf(slot.starts_at_local)}, ${available ? "available" : "unavailable"}`);
      if (available) {
        button.addEventListener("click", () => {
          if (openBookingForm(slot, column, detail, search)) markSelected(button);
        });
      }
      cell.appendChild(button);
      row.appendChild(cell);
    }
    tbody.appendChild(row);
  }
  table.appendChild(tbody);
  availabilityGrid.el.appendChild(table);
}

// The search whose grid is on screen. A refresh after a 409 repeats exactly this search,
// and the booking form takes its restaurant and party size from it, never from inputs the
// diner may have edited since without searching.
let shownSearch = null;

function setSearchStatus(text, isError) {
  searchStatus.el.textContent = text;
  searchStatus.el.classList.toggle("error", Boolean(isError));
  if (text) searchStatus.show();
  else searchStatus.hide();
}

async function performSearch({ keepBookingForm = false } = {}) {
  const search = keepBookingForm && shownSearch ? shownSearch : {
    restaurantId: qs("restaurant-select").value,
    date: qs("date-input").value,
    partySize: Number(qs("party-size-input").value),
  };
  if (!search.restaurantId || !search.date || !search.partySize) return;
  const token = ++searchToken;
  searchAuthError.hide();
  if (!keepBookingForm) {
    // Loading: the previous grid goes away, so a cell from an older search can't be booked.
    availabilityGrid.hide();
    noSlots.hide();
    setSearchStatus("Finding tables…", false);
  }
  let detailResult, availResult;
  try {
    [detailResult, availResult] = await Promise.all([
      apiFetch(`/restaurants/${encodeURIComponent(search.restaurantId)}`),
      apiFetch(`/availability?restaurant_id=${encodeURIComponent(search.restaurantId)}` +
               `&date=${encodeURIComponent(search.date)}` +
               `&party_size=${encodeURIComponent(search.partySize)}`),
    ]);
  } catch (e) {
    if (token === searchToken && !keepBookingForm) {
      setSearchStatus("We couldn't reach Tablekeeper. Check your connection and search again.", true);
    }
    return;
  }
  if (token !== searchToken) return;   // a newer search has already started; discard this one
  if (!detailResult.ok || !availResult.ok) {
    if (!keepBookingForm) setSearchStatus("That search didn't work. Check the details and try again.", true);
    return;
  }
  setSearchStatus("", false);
  currentRestaurantDetail = detailResult.data;
  shownSearch = search;
  renderGrid(detailResult.data, availResult.data, search);
  if (!keepBookingForm) {
    bookingForm.hide();
    confirmationToggle.hide();
    pendingBooking = null;
  }
}

function runSearch() {
  return performSearch({ keepBookingForm: false });
}

function refreshAvailability() {
  return performSearch({ keepBookingForm: true });
}

function openBookingForm(slot, column, detail, search) {
  const session = TK.session.get();
  if (!session) {
    setText(searchAuthError.el, "Sign in to book a table.");
    searchAuthError.show();
    return false;
  }
  searchAuthError.hide();
  confirmationToggle.hide();
  bookingForm.show();
  bookingErrorToggle.hide();
  bookingUncertainToggle.hide();
  const restaurantId = search.restaurantId;
  const partySize = search.partySize;
  const labels = column.ids.map((tid) => tableLabel(detail, tid));
  setText(
    bookingSummaryEl,
    `${labels.join(" + ")} at ${timeOf(slot.starts_at_local)} on ${slot.starts_at_local.split("T")[0]}`);
  bookingPartySizeEl.value = partySize;
  pendingBooking = {
    key: null,
    body: null,
    restaurantId,
    tableIds: column.ids,
    startsAtLocal: slot.starts_at_local,
    restaurantName: detail.name,
  };
  return true;
}

function markSelected(button) {
  for (const cell of availabilityGrid.el.querySelectorAll(".slot-cell")) {
    cell.classList.toggle("is-selected", cell === button);
    if (cell.dataset.available === "true") cell.setAttribute("aria-pressed", String(cell === button));
  }
}

function buildBookingBody() {
  const partySize = Number(bookingPartySizeEl.value);
  const body = {
    restaurant_id: pendingBooking.restaurantId,
    starts_at_local: pendingBooking.startsAtLocal,
    party_size: partySize,
  };
  if (pendingBooking.tableIds.length === 1) body.table_id = pendingBooking.tableIds[0];
  else body.table_ids = pendingBooking.tableIds;
  return body;
}

function showConfirmation(reservation) {
  bookingErrorToggle.hide();
  bookingUncertainToggle.hide();
  confirmationToggle.show();
  setText(confirmationReferenceEl, reservation.reference);
  const restaurantName = pendingBooking ? pendingBooking.restaurantName : "";
  const labels = reservation.table_ids.map(
    (tid) => (currentRestaurantDetail ? tableLabel(currentRestaurantDetail, tid) : tid));
  setText(
    confirmationDetailsEl,
    `${restaurantName}, ${labels.join(" + ")}, ${reservation.starts_at_local.replace("T", " ")}`);
  setText(confirmationTablesEl, labels.join(", "));
}

async function submitBooking() {
  if (!pendingBooking) return;
  const session = TK.session.get();
  if (!session) {
    searchAuthError.show();
    return;
  }
  const body = buildBookingBody();
  if (!pendingBooking.key || JSON.stringify(pendingBooking.body) !== JSON.stringify(body)) {
    pendingBooking.key = crypto.randomUUID();
    pendingBooking.body = body;
    confirmationToggle.hide();   // a new request: an earlier confirmation is not its answer
  }
  bookingErrorToggle.hide();
  bookingUncertainToggle.hide();
  bookingSubmitEl.disabled = true;
  try {
    const { ok, status, data } = await apiFetch("/reservations", {
      method: "POST",
      token: session.token,
      idempotencyKey: pendingBooking.key,
      body,
    });
    if (ok) {
      // A 2xx whose body can't be read proves nothing: treat it as a lost answer.
      if (!data || !data.reference) throw new Error("unreadable booking answer");
      showConfirmation(data);
    } else if (status >= 500) {
      // The server failed mid-request: the booking may or may not exist. Same as a lost answer.
      throw new Error(`server error ${status}`);
    } else if (status === 409 && data && data.error && data.error.code === "table_unavailable") {
      setText(bookingErrorToggle.el, "That table was just taken. Pick another option below.");
      bookingErrorToggle.show();
      await refreshAvailability();
    } else {
      setText(
        bookingErrorToggle.el,
        (data && data.error && data.error.message) || "The booking could not be completed.");
      bookingErrorToggle.show();
    }
  } catch (e) {
    setText(
      bookingUncertainToggle.el,
      "We lost the connection before hearing back. Submit again to retry " +
      "— it will not create a duplicate booking.");
    bookingUncertainToggle.show();
  } finally {
    bookingSubmitEl.disabled = false;
  }
}

function initSearch() {
  const grid = qs("availability-grid");
  if (!grid) return;
  searchAuthError = presence("auth-error");
  const status = document.createElement("p");
  status.id = "search-status";
  status.className = "message";
  status.setAttribute("role", "status");
  grid.parentNode.insertBefore(status, grid);
  searchStatus = presence("search-status");
  noSlots = presence("no-slots");
  availabilityGrid = presence("availability-grid");
  bookingForm = presence("booking-form");
  bookingErrorToggle = presence("booking-error");
  bookingUncertainToggle = presence("booking-uncertain");
  confirmationToggle = presence("confirmation");

  // Cache booking-form / confirmation children by reference now, while everything is
  // still attached -- getElementById cannot find them once their container is hidden.
  bookingSummaryEl = qs("booking-summary");
  bookingPartySizeEl = qs("booking-party-size");
  bookingSubmitEl = qs("booking-submit");
  confirmationReferenceEl = qs("confirmation-reference");
  confirmationDetailsEl = qs("confirmation-details");
  confirmationTablesEl = qs("confirmation-tables");
  bookingSubmitEl.addEventListener("click", submitBooking);

  searchAuthError.hide();
  noSlots.hide();
  bookingForm.hide();
  bookingErrorToggle.hide();
  bookingUncertainToggle.hide();
  confirmationToggle.hide();

  const dateInput = qs("date-input");
  if (!dateInput.value) dateInput.value = new Date().toISOString().slice(0, 10);
  loadRestaurants().then(() => performSearch());
  qs("search-form").addEventListener("submit", (event) => {
    event.preventDefault();
    runSearch();
  });
}

// ---- lookup --------------------------------------------------------------------------

let currentReservation = null;
let reservationDetailToggle = null;
let reservationErrorToggle = null;
let reservationCancelButtonToggle = null;
let reservationStatusEl = null;
let reservationTablesEl = null;
let reservationSummaryEl = null;

async function renderReservation(reservation) {
  reservationDetailToggle.show();
  setText(reservationStatusEl, reservation.status);
  reservationStatusEl.setAttribute("data-status", reservation.status);
  let labels = reservation.table_ids;
  try {
    const { ok, data } = await apiFetch(`/restaurants/${encodeURIComponent(reservation.restaurant_id)}`);
    if (ok) labels = reservation.table_ids.map((tid) => tableLabel(data, tid));
  } catch (e) {
    // fall back to raw table ids if the restaurant lookup fails
  }
  setText(reservationTablesEl, labels.join(", "));
  setText(
    reservationSummaryEl,
    `${reservation.starts_at_local.replace("T", " ")}, party of ${reservation.party_size}`);
  if (reservation.status === "confirmed") reservationCancelButtonToggle.show();
  else reservationCancelButtonToggle.hide();
}

async function lookup() {
  const reference = qs("lookup-reference-input").value.trim();
  reservationErrorToggle.hide();
  reservationDetailToggle.hide();
  const session = TK.session.get();
  if (!session) {
    setText(reservationErrorToggle.el, "Sign in to look up your reservation.");
    reservationErrorToggle.show();
    return;
  }
  try {
    const { ok, data } = await apiFetch(
      `/reservations/${encodeURIComponent(reference)}`, { token: session.token });
    if (!ok) {
      setText(reservationErrorToggle.el, "No reservation found for that reference.");
      reservationErrorToggle.show();
      return;
    }
    currentReservation = data;
    await renderReservation(data);
  } catch (e) {
    setText(reservationErrorToggle.el, "Could not reach the server. Please try again.");
    reservationErrorToggle.show();
  }
}

async function cancelCurrent() {
  if (!currentReservation) return;
  const session = TK.session.get();
  reservationErrorToggle.hide();
  try {
    const { ok, data } = await apiFetch(
      `/reservations/${encodeURIComponent(currentReservation.reference)}/cancel`,
      { method: "POST", token: session.token });
    if (ok) {
      currentReservation = data;
      await renderReservation(data);
    } else {
      setText(
        reservationErrorToggle.el,
        (data && data.error && data.error.message) || "That cancellation was refused.");
      reservationErrorToggle.show();
    }
  } catch (e) {
    setText(reservationErrorToggle.el, "Could not reach the server. Please try again.");
    reservationErrorToggle.show();
  }
}

function initLookup() {
  const form = qs("lookup-form");
  if (!form) return;
  reservationDetailToggle = presence("reservation-detail");
  reservationErrorToggle = presence("reservation-error");
  reservationCancelButtonToggle = presence("reservation-cancel-button");
  reservationStatusEl = qs("reservation-status");
  reservationTablesEl = qs("reservation-tables");
  reservationSummaryEl = qs("reservation-summary");
  reservationDetailToggle.hide();
  reservationErrorToggle.hide();
  form.addEventListener("submit", (event) => {
    event.preventDefault();
    lookup();
  });
  reservationCancelButtonToggle.el.addEventListener("click", cancelCurrent);
}

document.addEventListener("DOMContentLoaded", () => {
  renderNav();
  initSignup();
  initLogin();
  initSearch();
  initLookup();
});
