// Profile builder: load current profile, edit, and PUT it back.
// Redirects to /login if the session is missing or expired.
(function () {
  const form = document.getElementById("profile-form");
  const msg = document.getElementById("msg");
  const greeting = document.getElementById("greeting");

  const LIST_FIELDS = ["favorite_genres", "favorite_authors", "examples", "dislikes"];

  function show(text, kind) {
    msg.textContent = text;
    msg.className = "msg show " + kind;
  }

  // "a, b, c" or newline-separated -> ["a","b","c"] (trimmed, empties dropped).
  function parseList(raw) {
    return raw
      .split(/[\n,]+/)
      .map((s) => s.trim())
      .filter((s) => s.length > 0);
  }

  function fillList(id, arr) {
    document.getElementById(id).value = (arr || []).join(", ");
  }

  async function load() {
    let res;
    try {
      res = await fetch("/api/profile", { headers: { Accept: "application/json" } });
    } catch (_) {
      show("Couldn't reach the server.", "error");
      return;
    }
    if (res.status === 401) {
      window.location.href = "/login";
      return;
    }
    if (!res.ok) {
      show("Couldn't load your profile.", "error");
      return;
    }
    const p = await res.json();
    greeting.textContent = "Hi " + (p.display_name || "there") + " — tell us what you love.";
    LIST_FIELDS.forEach((f) => fillList(f, p[f]));

    const cp = p.content_preferences || {};
    document.getElementById("max_violence").value = cp.max_violence || "moderate";
    document.getElementById("max_language").value = cp.max_language || "moderate";
    document.getElementById("romance_ok").checked = cp.romance_ok !== false;
    document.getElementById("explicit_ok").checked = cp.explicit_ok === true;
    document.getElementById("reading_pace").value = p.reading_pace || "";
    document.getElementById("preferred_length").value = p.preferred_length || "";
  }

  form.addEventListener("submit", async function (e) {
    e.preventDefault();
    const btn = form.querySelector("button[type=submit]");
    btn.disabled = true;

    const payload = {
      content_preferences: {
        max_violence: document.getElementById("max_violence").value,
        max_language: document.getElementById("max_language").value,
        romance_ok: document.getElementById("romance_ok").checked,
        explicit_ok: document.getElementById("explicit_ok").checked,
      },
      reading_pace: document.getElementById("reading_pace").value || null,
      preferred_length: document.getElementById("preferred_length").value || null,
    };
    LIST_FIELDS.forEach((f) => {
      payload[f] = parseList(document.getElementById(f).value);
    });

    try {
      const res = await fetch("/api/profile", {
        method: "PUT",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(payload),
      });
      if (res.status === 401) {
        window.location.href = "/login";
        return;
      }
      if (res.ok) {
        show("Saved. Your preferences are up to date.", "ok");
      } else {
        show("Please check your entries and try again.", "error");
      }
    } catch (_) {
      show("Something went wrong. Please try again.", "error");
    } finally {
      btn.disabled = false;
    }
  });

  document.getElementById("logout").addEventListener("click", async function () {
    try {
      await fetch("/api/logout", { method: "POST" });
    } catch (_) { /* ignore */ }
    window.location.href = "/login";
  });

  load();
})();
