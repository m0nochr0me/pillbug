(function () {
  const overlay = document.getElementById("dashboard-confirm-overlay");
  const dialog = document.getElementById("dashboard-confirm-dialog");
  const title = document.getElementById("dashboard-confirm-title");
  const message = document.getElementById("dashboard-confirm-message");
  const cancelButton = document.getElementById("dashboard-confirm-cancel");
  const confirmButton = document.getElementById("dashboard-confirm-submit");

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

  function onCancel(event) {
    event.preventDefault();
    finish(false);
  }

  function onConfirm(event) {
    event.preventDefault();
    finish(true);
  }

  overlay.addEventListener("click", () => finish(false));
  cancelButton.addEventListener("click", onCancel);
  confirmButton.addEventListener("click", onConfirm);
  dialog.addEventListener("cancel", onCancel);
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
