(function () {
  const form = document.getElementById("changelist-form");
  let grid = document.querySelector(".post-card-grid");
  if (!form || !grid) {
    return;
  }

  const DEFAULT_INTERVAL = 20000;
  let refreshDelay = readRefreshInterval(grid);
  let refreshTimer = null;
  let activeController = null;
  let loading = false;
  let refreshPaused = false;

  const deleteModal = document.querySelector("[data-post-delete-modal]");
  const deleteTitle = deleteModal?.querySelector(".post-delete-modal__title");
  const deleteObjectLabel = deleteModal?.querySelector("[data-post-delete-object]");
  const deleteError = deleteModal?.querySelector("[data-post-delete-error]");
  const deleteCancel = deleteModal?.querySelector("[data-post-delete-cancel]");
  const deleteConfirm = deleteModal?.querySelector("[data-post-delete-confirm]");
  const deleteBackdrop = deleteModal?.querySelector("[data-post-delete-dismiss]");
  const deleteConfirmDefaultLabel = deleteConfirm?.textContent || "Usuń";
  let deleteContext = null;
  let escapeHandler = null;

  function readRefreshInterval(element) {
    const raw = parseInt(element.dataset.refreshIntervalMs || element.dataset.refreshInterval || "", 10);
    return Number.isFinite(raw) && raw > 0 ? raw : DEFAULT_INTERVAL;
  }

  function escapeName(name) {
    if (window.CSS && typeof window.CSS.escape === "function") {
      return window.CSS.escape(name);
    }
    return name.replace(/([^\w-])/g, "\\$1");
  }

  function checkboxSelector(element) {
    const name = element.dataset.actionCheckboxName;
    if (name) {
      return `input[type="checkbox"][name="${escapeName(name)}"]`;
    }
    return "input.action-select";
  }

  function currentCheckboxes(element = grid) {
    const selector = checkboxSelector(element);
    return Array.from(element.querySelectorAll(selector));
  }

  function collectSelectedValues(element = grid) {
    return new Set(
      currentCheckboxes(element)
        .filter((input) => input.checked)
        .map((input) => input.value),
    );
  }

  function restoreSelections(targetGrid, selectedValues) {
    const selector = checkboxSelector(targetGrid);
    targetGrid.querySelectorAll(selector).forEach((input) => {
      input.checked = selectedValues.has(input.value);
    });
  }

  function countSelected(element = grid) {
    return currentCheckboxes(element).filter((input) => input.checked).length;
  }

  function updateActionSummary(element = grid) {
    const selectedCount = countSelected(element);
    const counter = document.querySelector(".actions .action-counter");
    const perPage = parseInt(element.dataset.pageCount || element.dataset.pageSize || counter?.dataset.actionsIcnt || "0", 10) || 0;
    const totalCount = parseInt(element.dataset.totalCount || perPage || "0", 10) || 0;

    if (counter) {
      counter.dataset.actionsIcnt = String(perPage);
      const template = element.dataset.selectionNoteTemplate;
      if (template) {
        counter.textContent = template
          .replace("%(sel)s", String(selectedCount))
          .replace("%(cnt)s", String(perPage));
      } else {
        counter.textContent = `${selectedCount} / ${perPage}`;
      }
    }

    const allTemplate = element.dataset.selectionNoteAllTemplate;
    const allSpan = document.querySelector(".actions span.all");
    if (allSpan && allTemplate) {
      allSpan.textContent = allTemplate.replace("%(total_count)s", String(totalCount));
    }

    const questionSpan = document.querySelector(".actions span.question");
    if (questionSpan) {
      const shouldShow = totalCount > perPage && perPage > 0;
      questionSpan.classList.toggle("hidden", !shouldShow);
      const link = questionSpan.querySelector("a");
      if (link && allTemplate) {
        link.textContent = allTemplate.replace("%(total_count)s", String(totalCount));
      }
    }

    const clearSpan = document.querySelector(".actions span.clear");
    if (clearSpan) {
      clearSpan.classList.add("hidden");
    }

    const selectAcrossInput = document.querySelector(".actions input.select-across");
    if (selectAcrossInput) {
      selectAcrossInput.value = "0";
    }
  }

  function abortPendingRequest() {
    if (activeController) {
      activeController.abort();
      activeController = null;
    }
  }

  function clearTimer() {
    if (refreshTimer) {
      window.clearTimeout(refreshTimer);
      refreshTimer = null;
    }
  }

  function scheduleNext() {
    clearTimer();
    if (refreshPaused) {
      return;
    }
    refreshTimer = window.setTimeout(fetchCards, refreshDelay);
  }

  function buildRequestUrl() {
    const url = new URL(window.location.href, window.location.origin);
    url.searchParams.set("_partial", "cards");
    url.searchParams.set("_ts", String(Date.now()));
    return url.toString();
  }

  function replaceGrid(newGrid, preservedSelection) {
    grid.replaceWith(newGrid);
    grid = newGrid;
    refreshDelay = readRefreshInterval(grid);
    restoreSelections(grid, preservedSelection);
    updateActionSummary(grid);
    grid.dispatchEvent(new CustomEvent("post-cards:updated", { bubbles: true }));
  }

  async function fetchCards() {
    if (loading) {
      return;
    }
    loading = true;
    clearTimer();
    abortPendingRequest();

    const preservedSelection = collectSelectedValues();
    const controller = new AbortController();
    activeController = controller;

    try {
      const response = await fetch(buildRequestUrl(), {
        credentials: "same-origin",
        cache: "no-store",
        headers: {
          "X-Requested-With": "XMLHttpRequest",
          "Cache-Control": "no-cache",
        },
        signal: controller.signal,
      });
      if (!response.ok) {
        throw new Error(`Nieudane odświeżenie kart (${response.status})`);
      }
      const markup = await response.text();
      const template = document.createElement("template");
      template.innerHTML = markup.trim();
      const incomingGrid = template.content.querySelector(".post-card-grid");
      if (!incomingGrid) {
        return;
      }
      replaceGrid(incomingGrid, preservedSelection);
    } catch (error) {
      if (error.name !== "AbortError") {
        console.warn(error);
      }
    } finally {
      loading = false;
      activeController = null;
      scheduleNext();
    }
  }

  function getCsrfToken() {
    const input = form?.querySelector('input[name="csrfmiddlewaretoken"]');
    return input?.value || "";
  }

  function resetDeleteModalState() {
    if (!deleteModal) {
      return;
    }
    if (deleteError) {
      deleteError.textContent = "";
      deleteError.hidden = true;
    }
    if (deleteConfirm) {
      deleteConfirm.disabled = false;
      deleteConfirm.textContent = deleteConfirmDefaultLabel;
    }
    if (deleteCancel) {
      deleteCancel.disabled = false;
    }
  }

  function closeDeleteModal() {
    if (!deleteModal || !deleteContext) {
      return;
    }
    if (deleteContext.isSubmitting) {
      return;
    }
    deleteModal.hidden = true;
    document.body.classList.remove("post-delete-modal-open");
    if (escapeHandler) {
      document.removeEventListener("keydown", escapeHandler, true);
      escapeHandler = null;
    }
    const focusTarget = deleteContext.trigger;
    deleteContext = null;
    resetDeleteModalState();
    refreshPaused = false;
    if (!loading) {
      scheduleNext();
    }
    if (focusTarget && typeof focusTarget.focus === "function") {
      window.setTimeout(() => focusTarget.focus(), 0);
    }
  }

  function mapKindToTitle(kind) {
    if (kind === "draft") {
      return "draftu";
    }
    if (kind === "wpis") {
      return "wpisu";
    }
    return kind ? kind : "wpisu";
  }

  function mapKindToDescription(kind) {
    if (kind === "draft") {
      return "ten draft";
    }
    if (kind === "wpis") {
      return "ten wpis";
    }
    return "wybrany wpis";
  }

  function openDeleteModal(trigger) {
    if (!deleteModal) {
      return;
    }
    const kind = (trigger.dataset.deleteKind || "").toLowerCase();
    const objectLabel = trigger.dataset.deleteObject || mapKindToDescription(kind);
    deleteContext = {
      url: trigger.href,
      kind,
      trigger,
    };
    const titleSuffix = mapKindToTitle(kind);
    if (deleteTitle && titleSuffix) {
      deleteTitle.textContent = `Potwierdź usunięcie ${titleSuffix}`;
    }
    if (deleteObjectLabel) {
      deleteObjectLabel.textContent = objectLabel;
    }
    resetDeleteModalState();
    deleteModal.hidden = false;
    document.body.classList.add("post-delete-modal-open");
    refreshPaused = true;
    clearTimer();
    abortPendingRequest();
    window.setTimeout(() => {
      deleteConfirm?.focus();
    }, 0);
    escapeHandler = function (event) {
      if (event.key === "Escape") {
        event.preventDefault();
        closeDeleteModal();
      }
    };
    document.addEventListener("keydown", escapeHandler, true);
  }

  function setDeleteLoadingState(isLoading) {
    if (deleteContext) {
      deleteContext.isSubmitting = isLoading;
    }
    if (deleteConfirm) {
      deleteConfirm.disabled = isLoading;
      deleteConfirm.textContent = isLoading ? "Usuwanie…" : deleteConfirmDefaultLabel;
    }
    if (deleteCancel) {
      deleteCancel.disabled = isLoading;
    }
  }

  async function submitDelete() {
    if (!deleteContext || !deleteContext.url) {
      return;
    }
    const token = getCsrfToken();
    if (!token) {
      if (deleteError) {
        deleteError.textContent = "Brak tokenu CSRF – odśwież stronę i spróbuj ponownie.";
        deleteError.hidden = false;
      }
      return;
    }
    setDeleteLoadingState(true);
    if (deleteError) {
      deleteError.textContent = "";
      deleteError.hidden = true;
    }
    try {
      const body = new URLSearchParams();
      body.set("csrfmiddlewaretoken", token);
      body.set("post", "yes");
      const response = await fetch(deleteContext.url, {
        method: "POST",
        credentials: "same-origin",
        headers: {
          "X-Requested-With": "XMLHttpRequest",
          "X-CSRFToken": token,
        },
        body,
        redirect: "follow",
      });
      if (!response.ok) {
        throw new Error(`Nie udało się usunąć (${response.status})`);
      }
      if (response.url && response.url.includes("/delete")) {
        throw new Error("Serwer nie potwierdził usunięcia. Odśwież stronę i spróbuj ponownie.");
      }
      if (deleteContext) {
        deleteContext.isSubmitting = false;
      }
      closeDeleteModal();
      await fetchCards();
    } catch (error) {
      if (deleteError) {
        deleteError.textContent = error?.message || "Nie udało się usunąć wpisu. Spróbuj ponownie.";
        deleteError.hidden = false;
      }
      setDeleteLoadingState(false);
    }
  }

  form.addEventListener("change", (event) => {
    if (!grid.contains(event.target)) {
      return;
    }
    if (event.target.matches(checkboxSelector(grid))) {
      updateActionSummary(grid);
    }
  });

  if (deleteModal) {
    form.addEventListener("click", (event) => {
      const trigger = event.target.closest(".post-card__action--danger");
      if (!trigger || !grid.contains(trigger)) {
        return;
      }
      event.preventDefault();
      openDeleteModal(trigger);
    });
  }

  deleteCancel?.addEventListener("click", () => {
    closeDeleteModal();
  });

  deleteBackdrop?.addEventListener("click", () => {
    closeDeleteModal();
  });

  deleteConfirm?.addEventListener("click", () => {
    submitDelete();
  });

  window.addEventListener("beforeunload", () => {
    clearTimer();
    abortPendingRequest();
  });

  window.addEventListener("pagehide", () => {
    clearTimer();
    abortPendingRequest();
  });

  scheduleNext();
})();
