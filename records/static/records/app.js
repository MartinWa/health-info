// The only client state: the measurement drawer and a double-tap guard.
document.addEventListener("DOMContentLoaded", () => {
  const openDrawer = (id) => {
    const drawer = document.getElementById(id);
    if (!drawer) return;
    if (typeof drawer.showModal === "function") drawer.showModal();
    else drawer.setAttribute("open", "");
    drawer.querySelector("input:not([type=hidden]), select, textarea")?.focus();
  };

  document.addEventListener("click", (event) => {
    const opener = event.target.closest("[data-drawer-open]");
    if (opener) {
      event.preventDefault();
      openDrawer(opener.dataset.drawerOpen);
      return;
    }
    if (event.target.closest("[data-drawer-close]")) {
      event.target.closest("dialog")?.close();
    }
  });

  document.querySelectorAll("dialog.drawer").forEach((dialog) => {
    // A click on the backdrop reports the dialog itself as the target.
    dialog.addEventListener("click", (event) => {
      if (event.target === dialog) dialog.close();
    });
  });

  // One tap logs a dose; a second tap while the first post is in flight is dropped.
  document.querySelectorAll("form[data-once]").forEach((form) => {
    form.addEventListener("submit", (event) => {
      if (form.dataset.submitting) {
        event.preventDefault();
        return;
      }
      form.dataset.submitting = "1";
    });
  });
  window.addEventListener("pageshow", () => {
    document.querySelectorAll("form[data-once]").forEach((form) => {
      delete form.dataset.submitting;
    });
  });

  // Dirty-form guard: leaving a half-filled form asks first.
  const form = document.querySelector('form.record-form[method="post"], form[data-dirty][method="post"]');
  if (!form) return;
  const snapshot = () => new URLSearchParams(new FormData(form)).toString();
  const initial = snapshot();
  let submitting = false;
  form.addEventListener("submit", () => {
    submitting = true;
  });
  window.addEventListener("beforeunload", (event) => {
    if (submitting || snapshot() === initial) return;
    event.preventDefault();
    event.returnValue = "";
  });
});
