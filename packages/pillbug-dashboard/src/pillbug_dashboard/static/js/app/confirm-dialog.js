(function () {
  const overlay = document.getElementById("confirm-overlay");
  const dialog = document.getElementById("confirm-dialog");
  const title = document.getElementById("confirm-title");
  const message = document.getElementById("confirm-message");
  const cancelButton = document.getElementById("confirm-cancel");
  const confirmButton = document.getElementById("confirm-submit");

  if (!overlay || !dialog || !title || !message || !cancelButton || !confirmButton) {
    return;
  }

  let resolvePending = null;

  function finish(result) {
    const resolve = resolvePending;
    resolvePending = null;

    overlay.classList.remove("active");
    confirmButton.classList.remove("danger-text");

    if (dialog.open) {
      dialog.close();
    }

    if (resolve) {
      resolve(result);
    }
  }

  overlay.addEventListener("click", () => finish(false));
  cancelButton.addEventListener("click", (event) => {
    event.preventDefault();
    finish(false);
  });
  confirmButton.addEventListener("click", (event) => {
    event.preventDefault();
    finish(true);
  });
  dialog.addEventListener("cancel", (event) => {
    event.preventDefault();
    finish(false);
  });
  dialog.addEventListener("close", () => {
    if (resolvePending) {
      finish(dialog.returnValue === "confirm");
    }
  });

  window.PillbugDashboardConfirm = {
    open(options) {
      if (resolvePending) {
        finish(false);
      }

      const config = options && typeof options === "object" ? options : {};
      title.textContent = config.title || "Confirm action";
      message.textContent = config.message || "Proceed?";
      cancelButton.textContent = config.cancelLabel || "Cancel";
      confirmButton.textContent = config.confirmLabel || "Confirm";
      confirmButton.classList.toggle("danger-text", config.tone === "danger");

      overlay.classList.add("active");
      dialog.returnValue = "cancel";
      dialog.showModal();

      return new Promise((resolve) => {
        resolvePending = resolve;
      });
    },
  };
})();
