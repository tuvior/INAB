const dropZone = document.getElementById("drop-zone");
const uploadForm = document.getElementById("upload-form");
const fileInput = document.getElementById("file-input");
const fileName = document.getElementById("file-name");
const csvAccountPanel = document.getElementById("csv-account-panel");
const csvAccountSelect = document.getElementById("csv-account-select");
const previewButton = document.getElementById("preview-button");
const selfNameList = document.getElementById("self-name-list");
const addSelfNameButton = document.querySelector("[data-add-self-name]");
const scrollRestoreKey = `inab:scroll:${window.location.pathname}${window.location.search}`;

try {
  const savedScroll = sessionStorage.getItem(scrollRestoreKey);
  if (savedScroll !== null) {
    sessionStorage.removeItem(scrollRestoreKey);
    const nextScrollY = Number.parseInt(savedScroll, 10);
    if (Number.isFinite(nextScrollY)) {
      requestAnimationFrame(() => window.scrollTo({ top: nextScrollY }));
    }
  }
} catch {
  // Ignore storage failures; form submissions should still work normally.
}

document.querySelectorAll("form[method='post'], form[method='POST']").forEach((form) => {
  form.addEventListener("submit", () => {
    const action = form.getAttribute("action") || window.location.href;
    const actionUrl = new URL(action, window.location.href);
    const currentUrl = new URL(window.location.href);
    if (actionUrl.pathname !== currentUrl.pathname || actionUrl.search !== currentUrl.search) {
      return;
    }
    try {
      sessionStorage.setItem(scrollRestoreKey, String(window.scrollY));
    } catch {
      // Ignore storage failures; the server-side POST/redirect remains authoritative.
    }
  });
});

if (selfNameList && addSelfNameButton) {
  const createSelfNameRow = (value = "") => {
    const row = document.createElement("div");
    row.className = "own-name-row";

    const input = document.createElement("input");
    input.name = "self_names";
    input.placeholder = "Alex Example";
    input.value = value;
    input.setAttribute("aria-label", "Own-name alias");

    const removeButton = document.createElement("button");
    removeButton.className = "secondary";
    removeButton.type = "button";
    removeButton.dataset.removeSelfName = "";
    removeButton.textContent = "Remove";

    row.append(input, removeButton);
    return row;
  };

  const ensureSelfNameRow = () => {
    if (selfNameList.querySelectorAll(".own-name-row").length === 0) {
      selfNameList.append(createSelfNameRow());
    }
  };

  addSelfNameButton.addEventListener("click", () => {
    const row = createSelfNameRow();
    selfNameList.append(row);
    row.querySelector("input").focus();
  });

  selfNameList.addEventListener("click", (event) => {
    if (!(event.target instanceof Element)) {
      return;
    }
    const removeButton = event.target.closest("[data-remove-self-name]");
    if (!removeButton) {
      return;
    }
    removeButton.closest(".own-name-row").remove();
    ensureSelfNameRow();
  });
}

if (dropZone && fileInput && fileName) {
  const uploadCanSubmit = () => {
    const file = fileInput.files.length > 0 ? fileInput.files[0] : null;
    const isCsv = Boolean(file && file.name.toLowerCase().endsWith(".csv"));
    const csvAccountSelected = Boolean(csvAccountSelect && csvAccountSelect.value);
    return Boolean(file && (!isCsv || csvAccountSelected));
  };

  const updateCsvAccountRequirement = () => {
    const file = fileInput.files.length > 0 ? fileInput.files[0] : null;
    const isCsv = Boolean(file && file.name.toLowerCase().endsWith(".csv"));
    const csvAccountSelected = Boolean(csvAccountSelect && csvAccountSelect.value);
    if (csvAccountPanel) {
      csvAccountPanel.hidden = !isCsv;
    }
    if (csvAccountSelect) {
      csvAccountSelect.required = isCsv;
    }
    if (previewButton) {
      const canSubmit = uploadCanSubmit();
      previewButton.disabled = !canSubmit;
      previewButton.classList.toggle("is-disabled", !canSubmit);
      if (!file) {
        previewButton.textContent = "Choose file to preview";
      } else if (isCsv && !csvAccountSelected) {
        previewButton.textContent = "Select account to preview";
      } else {
        previewButton.textContent = "Preview import";
      }
    }
  };

  ["dragenter", "dragover"].forEach((eventName) => {
    dropZone.addEventListener(eventName, (event) => {
      event.preventDefault();
      dropZone.classList.add("dragging");
    });
  });

  ["dragleave", "drop"].forEach((eventName) => {
    dropZone.addEventListener(eventName, (event) => {
      event.preventDefault();
      dropZone.classList.remove("dragging");
    });
  });

  dropZone.addEventListener("drop", (event) => {
    if (event.dataTransfer.files.length > 0) {
      fileInput.files = event.dataTransfer.files;
      fileName.textContent = event.dataTransfer.files[0].name;
      updateCsvAccountRequirement();
    }
  });

  fileInput.addEventListener("change", () => {
    if (fileInput.files.length > 0) {
      fileName.textContent = fileInput.files[0].name;
    }
    updateCsvAccountRequirement();
  });

  if (csvAccountSelect) {
    csvAccountSelect.addEventListener("change", updateCsvAccountRequirement);
  }

  if (uploadForm) {
    uploadForm.addEventListener("submit", (event) => {
      updateCsvAccountRequirement();
      if (!uploadCanSubmit()) {
        event.preventDefault();
      }
    });
  }

  updateCsvAccountRequirement();
}

const previewRows = Array.from(document.querySelectorAll("[data-preview-row]"));
const previewSearch = document.getElementById("preview-search");
const previewFilterButtons = Array.from(document.querySelectorAll("[data-preview-filter]"));

if (previewRows.length > 0) {
  let activeFilter = "all";

  const applyPreviewFilters = () => {
    const query = previewSearch ? previewSearch.value.trim().toLowerCase() : "";
    previewRows.forEach((row) => {
      const status = row.dataset.status || "";
      const searchText = row.dataset.search || "";
      const matchesStatus = activeFilter === "all" || status === activeFilter;
      const matchesSearch = !query || searchText.includes(query);
      row.hidden = !(matchesStatus && matchesSearch);
    });
  };

  previewFilterButtons.forEach((button) => {
    button.addEventListener("click", () => {
      activeFilter = button.dataset.previewFilter || "all";
      previewFilterButtons.forEach((item) => item.classList.toggle("active", item === button));
      applyPreviewFilters();
    });
  });

  if (previewSearch) {
    previewSearch.addEventListener("input", applyPreviewFilters);
  }

  applyPreviewFilters();
}
