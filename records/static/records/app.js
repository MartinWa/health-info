// Two client guards: a double-tap guard on dose rows and a dirty-form prompt.
document.addEventListener("DOMContentLoaded", () => {
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
