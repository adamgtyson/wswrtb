// Login: POST /api/login, then go to the profile page on success.
(function () {
  const form = document.getElementById("login-form");
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
      email: document.getElementById("email").value.trim(),
      password: document.getElementById("password").value,
    };

    try {
      const res = await fetch("/api/login", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(payload),
      });
      if (res.ok) {
        window.location.href = "/profile";
        return;
      }
      show("Invalid email or password.", "error");
    } catch (_) {
      show("Something went wrong. Please try again.", "error");
    } finally {
      btn.disabled = false;
    }
  });
})();
