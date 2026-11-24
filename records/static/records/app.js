// Dirty-form guard: the only client state in this UI.
document.addEventListener("DOMContentLoaded", () => {
  const form = document.querySelector('form.record-form[method="post"]');
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
