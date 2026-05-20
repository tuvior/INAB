const dropZone = document.getElementById("drop-zone");
const uploadForm = document.getElementById("upload-form");
const fileInput = document.getElementById("file-input");
const fileName = document.getElementById("file-name");
const csvAccountPanel = document.getElementById("csv-account-panel");
const csvAccountSelect = document.getElementById("csv-account-select");
const previewButton = document.getElementById("preview-button");

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
