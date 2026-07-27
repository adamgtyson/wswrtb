// Signup: POST /api/register, then land on the profile builder on success.
(function () {
  const form = document.getElementById("signup-form");
  const msg = document.getElementById("msg");

  function show(text, kind) {
    msg.textContent = text;
    msg.className = "msg show " + kind;
  }

  form.addEventListener("submit", async function (e) {
    e.preventDefault();
    const btn = form.querySelector("button[type=submit]");
    btn.disabled = true;

    const payload = {
      display_name: document.getElementById("display_name").value.trim(),
      email: document.getElementById("email").value.trim(),
      password: document.getElementById("password").value,
      invite_code: document.getElementById("invite_code").value.trim(),
    };

    try {
      const res = await fetch("/api/register", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(payload),
      });
      if (res.ok) {
        // Registered and logged in — go build the preference profile.
        window.location.href = "/profile";
        return;
      }
      let detail = "That invite code is invalid or full.";
      try {
        const data = await res.json();
        if (res.status === 422) {
          detail = "Please check your details and try again.";
        } else if (data && data.detail && typeof data.detail === "string") {
          detail = data.detail;
        }
      } catch (_) { /* keep default */ }
      show(detail, "error");
    } catch (_) {
      show("Something went wrong. Please try again.", "error");
    } finally {
      btn.disabled = false;
    }
  });
})();
