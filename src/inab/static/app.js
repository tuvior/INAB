const dropZone = document.getElementById("drop-zone");
const fileInput = document.getElementById("file-input");
const fileName = document.getElementById("file-name");
const csvAccountPanel = document.getElementById("csv-account-panel");
const csvAccountSelect = document.getElementById("csv-account-select");

if (dropZone && fileInput && fileName) {
  const updateCsvAccountRequirement = () => {
    const file = fileInput.files.length > 0 ? fileInput.files[0] : null;
    const isCsv = Boolean(file && file.name.toLowerCase().endsWith(".csv"));
    if (csvAccountPanel) {
      csvAccountPanel.hidden = !isCsv;
    }
    if (csvAccountSelect) {
      csvAccountSelect.required = isCsv;
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

  updateCsvAccountRequirement();
}
