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

  form.addEventListener("change", (event) => {
    if (!grid.contains(event.target)) {
      return;
    }
    if (event.target.matches(checkboxSelector(grid))) {
      updateActionSummary(grid);
    }
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
