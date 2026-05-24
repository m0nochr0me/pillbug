(function () {
  const overlay = document.getElementById("confirm-overlay");
  const dialog = document.getElementById("confirm-dialog");
  const title = document.getElementById("confirm-title");
  const message = document.getElementById("confirm-message");
  const cancelButton = document.getElementById("confirm-cancel");
  const confirmButton = document.getElementById("confirm-submit");
  const commentField = document.getElementById("confirm-comment-field");
  const commentLabel = document.getElementById("confirm-comment-label");
  const commentInput = document.getElementById("confirm-comment-input");

  if (!overlay || !dialog || !title || !message || !cancelButton || !confirmButton) {
    return;
  }

  let resolvePending = null;
  let commentEnabled = false;

  function readComment() {
    if (!commentEnabled || !commentInput) {
      return null;
    }
    const value = commentInput.value.trim();
    return value || null;
  }

  function finish(confirmed) {
    const resolve = resolvePending;
    resolvePending = null;

    let result;
    if (!confirmed) {
      result = false;
    } else {
      result = { confirmed: true, comment: readComment() };
    }

    overlay.classList.remove("active");
    confirmButton.classList.remove("danger-text");

    if (dialog.open) {
      dialog.close();
    }

    if (commentField) {
      commentField.hidden = true;
    }
    if (commentInput) {
      commentInput.value = "";
    }
    commentEnabled = false;

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

      commentEnabled = Boolean(config.withComment);
      if (commentField && commentInput) {
        commentField.hidden = !commentEnabled;
        commentInput.value = "";
        if (commentEnabled && commentLabel) {
          commentLabel.textContent = config.commentLabel || "Comment (optional)";
        }
      }

      overlay.classList.add("active");
      dialog.returnValue = "cancel";
      dialog.showModal();

      if (commentEnabled && commentInput) {
        window.setTimeout(() => commentInput.focus(), 0);
      }

      return new Promise((resolve) => {
        resolvePending = resolve;
      });
    },
  };
})();
