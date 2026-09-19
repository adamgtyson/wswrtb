// Rough "ask for a book" page: member checkboxes + a prompt, results as plain text.
// Deliberately a placeholder — Session 5 replaces this with real book cards and the
// "who's reading" polish. Redirects to /login on a 401. All server-supplied text is set
// via textContent (never innerHTML), so titles and names can't inject markup.
(function () {
  const pageMsg = document.getElementById("page-msg");
  const nameEl = document.getElementById("group-name");
  const memberChecks = document.getElementById("member-checks");
  const resultsCard = document.getElementById("results-card");
  const resultsList = document.getElementById("results");
  const promptEl = document.getElementById("prompt");
  const askBtn = document.getElementById("ask-btn");
  const pickerWrap = document.getElementById("group-picker-wrap");
  const picker = document.getElementById("group-picker");

  let currentGroupId = null;

  function show(el, text, kind) {
    el.textContent = text;
    el.className = "msg show " + kind;
  }
  function clear(el) {
    el.textContent = "";
    el.className = "msg";
  }

  function bounceIfUnauth(res) {
    if (res.status === 401) {
      window.location.href = "/login";
      return true;
    }
    return false;
  }

  async function getJSON(url) {
    const res = await fetch(url, { headers: { Accept: "application/json" } });
    if (bounceIfUnauth(res)) return null;
    return { ok: res.ok, status: res.status, body: res.ok ? await res.json() : null };
  }

  function el(tag, cls, text) {
    const node = document.createElement(tag);
    if (cls) node.className = cls;
    if (text !== undefined) node.textContent = text;
    return node;
  }

  // ---- Members ----
  function renderMembers(members) {
    memberChecks.textContent = "";
    members.forEach((m) => {
      const label = el("label", "check");
      const box = document.createElement("input");
      box.type = "checkbox";
      box.value = m.user_id;
      box.checked = true; // default: everyone in the group is reading
      label.appendChild(box);
      label.appendChild(document.createTextNode(m.display_name));
      memberChecks.appendChild(label);
    });
  }

  function selectedMemberIds() {
    return Array.from(memberChecks.querySelectorAll("input[type=checkbox]"))
      .filter((box) => box.checked)
      .map((box) => parseInt(box.value, 10));
  }

  // ---- Results ----
  // Session 4 added verified Google Books metadata to each result. This stays the rough
  // placeholder it always was — a cover, a page count and an unverified flag, nothing
  // more. The real book cards are Session 5's job.
  function renderResults(recommendations) {
    resultsList.textContent = "";
    recommendations.forEach((rec) => {
      const li = document.createElement("li");

      if (rec.thumbnail_url) {
        const cover = document.createElement("img");
        cover.className = "cover";
        cover.src = rec.thumbnail_url;
        cover.alt = "Cover of " + rec.title;
        cover.loading = "lazy";
        li.appendChild(cover);
      }

      const meta = el("div", "meta");
      const heading = rec.year ? rec.title + " (" + rec.year + ")" : rec.title;
      meta.appendChild(el("span", "name", heading));
      meta.appendChild(el("span", "sub", "by " + rec.author));
      if (rec.page_count) {
        meta.appendChild(el("span", "sub", rec.page_count + " pages"));
      }
      meta.appendChild(el("span", "sub", rec.reason));
      // Only shown when Google Books was unreachable — the book itself is unchecked.
      if (!rec.verified) {
        meta.appendChild(el("span", "pill", "Unverified"));
      }

      li.appendChild(meta);
      resultsList.appendChild(li);
    });
    resultsCard.classList.remove("hidden");
  }

  // Distinct copy per failure so the page is useful while testing, without ever
  // echoing server internals.
  function messageForStatus(status) {
    if (status === 400) return "Some selected readers aren't in this group. Reload and try again.";
    if (status === 422) return "Please enter a request and pick at least one reader.";
    if (status === 429) return "Recommendations are rate limited right now. Try again later.";
    if (status === 502) return "Couldn't get recommendations right now. Please try again.";
    if (status === 503) return "The recommendation service isn't configured yet.";
    return "Something went wrong. Please try again.";
  }

  document.getElementById("ask-form").addEventListener("submit", async (e) => {
    e.preventDefault();
    clear(pageMsg);

    const memberIds = selectedMemberIds();
    const prompt = promptEl.value.trim();
    if (!memberIds.length) {
      show(pageMsg, "Pick at least one reader.", "error");
      return;
    }
    if (!prompt) {
      show(pageMsg, "Tell us what you're in the mood for.", "error");
      return;
    }

    askBtn.disabled = true;
    show(pageMsg, "Asking… this takes a few seconds.", "ok");
    try {
      const res = await fetch("/api/groups/" + currentGroupId + "/recommend", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ prompt: prompt, member_user_ids: memberIds }),
      });
      if (bounceIfUnauth(res)) return;
      if (res.ok) {
        const body = await res.json();
        clear(pageMsg);
        if (!body.recommendations.length) {
          show(pageMsg, "No recommendations came back. Try rewording your request.", "error");
          return;
        }
        renderResults(body.recommendations);
      } else {
        show(pageMsg, messageForStatus(res.status), "error");
      }
    } catch (_) {
      show(pageMsg, "Something went wrong. Please try again.", "error");
    } finally {
      askBtn.disabled = false;
    }
  });

  document.getElementById("logout").addEventListener("click", async () => {
    try {
      await fetch("/api/logout", { method: "POST" });
    } catch (_) { /* ignore */ }
    window.location.href = "/login";
  });

  // ---- Bootstrap ----
  async function loadGroup(groupId) {
    currentGroupId = groupId;
    clear(pageMsg);
    resultsCard.classList.add("hidden");

    const r = await getJSON("/api/groups/" + groupId + "/members");
    if (!r) return;
    if (!r.ok) {
      show(pageMsg, "Couldn't load that group's members.", "error");
      return;
    }
    renderMembers(r.body.members);
  }

  async function init() {
    const r = await getJSON("/api/me/groups");
    if (!r) return;
    if (!r.ok) {
      show(pageMsg, "Couldn't load your groups.", "error");
      return;
    }
    const groups = r.body.groups;
    if (!groups.length) {
      show(pageMsg, "You're not in any group yet.", "error");
      return;
    }
    nameEl.textContent = "Ask for a book · " + groups[0].name;
    if (groups.length > 1) {
      pickerWrap.classList.remove("hidden");
      groups.forEach((g) => {
        const opt = document.createElement("option");
        opt.value = g.id;
        opt.textContent = g.name;
        picker.appendChild(opt);
      });
      picker.addEventListener("change", () => {
        const chosen = groups.find((g) => g.id === parseInt(picker.value, 10));
        if (chosen) nameEl.textContent = "Ask for a book · " + chosen.name;
        loadGroup(parseInt(picker.value, 10));
      });
    }
    await loadGroup(groups[0].id);
  }

  init();
})();
