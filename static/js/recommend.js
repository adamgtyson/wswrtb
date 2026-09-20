// The "ask for a book" page: who's reading, a prompt, and real book cards.
//
// Three features live here as of Session 5 — cards built from the Google Books metadata
// the API returns, thumbs up/down feedback, and a Recent panel that replays a stored
// result set without spending another AI call.
//
// Hard rule: every server-supplied string is set with textContent, and every node is
// built with createElement. No raw-HTML insertion API is used anywhere in this file, and
// no markup is assembled from template literals. Book titles, authors, descriptions and
// display names all come from outside this app — Claude, Google Books, and other members
// — and none of it is allowed to become markup.
(function () {
  const pageMsg = document.getElementById("page-msg");
  const nameEl = document.getElementById("group-name");
  const memberChecks = document.getElementById("member-checks");
  const resultsCard = document.getElementById("results-card");
  const resultsEl = document.getElementById("results");
  const resultsTitle = document.getElementById("results-title");
  const promptEl = document.getElementById("prompt");
  const askBtn = document.getElementById("ask-btn");
  const pickerWrap = document.getElementById("group-picker-wrap");
  const picker = document.getElementById("group-picker");
  const recentCard = document.getElementById("recent-card");
  const recentList = document.getElementById("recent-list");
  const recentToggle = document.getElementById("recent-toggle");
  const replayBanner = document.getElementById("replay-banner");
  const replayText = document.getElementById("replay-text");
  const askAgainBtn = document.getElementById("ask-again");

  // Ratings this member already holds, keyed by the title the recommendation carried.
  // Mirrors the feedback table's (user_id, title) key.
  const ratings = new Map();

  let currentGroupId = null;
  // The search currently being replayed, so "Ask again" knows what to restore.
  let replaying = null;

  const THUMB_UP = "\u{1F44D}";
  const THUMB_DOWN = "\u{1F44E}";
  const UNVERIFIED_HINT = "We couldn't reach Google Books to confirm this one";

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

  // SQLite stores CURRENT_TIMESTAMP as UTC "YYYY-MM-DD HH:MM:SS" with no zone marker,
  // which a browser would otherwise read as local time and display hours out. Convert it
  // explicitly, and fall back to the raw string if anything about it is unexpected — a
  // plain-looking date beats a crash on the one label a replayed set must always show.
  function formatStamp(raw) {
    if (!raw) return "";
    const parsed = new Date(String(raw).replace(" ", "T") + "Z");
    if (isNaN(parsed.getTime())) return String(raw);
    return parsed.toLocaleString();
  }

  // ---- Members ----
  function renderMembers(members) {
    memberChecks.textContent = "";
    members.forEach((m) => {
      const label = el("label", "check");
      const box = document.createElement("input");
      box.type = "checkbox";
      box.value = m.user_id;
      box.dataset.name = m.display_name;
      box.checked = true; // default: everyone in the group is reading
      label.appendChild(box);
      label.appendChild(document.createTextNode(m.display_name));
      memberChecks.appendChild(label);
    });
  }

  function memberBoxes() {
    return Array.from(memberChecks.querySelectorAll("input[type=checkbox]"));
  }

  function selectedMemberIds() {
    return memberBoxes()
      .filter((box) => box.checked)
      .map((box) => parseInt(box.value, 10));
  }

  // ---- Feedback ----
  // A rating is keyed on the title the RECOMMENDATION carried (Claude's title), never the
  // Google Books canonical one — the server's uniqueness key is (user_id, title), so
  // sending the canonical title would open a second row for the same book.
  async function loadRatings(titles) {
    if (!titles.length) return;
    const params = new URLSearchParams();
    titles.forEach((t) => params.append("titles", t));
    const r = await getJSON("/api/feedback?" + params.toString());
    if (!r || !r.ok) return; // ratings are a nicety; never block the results on them
    Object.keys(r.body.ratings).forEach((title) =>
      ratings.set(title, r.body.ratings[title])
    );
  }

  function paintRating(controls, title) {
    const current = ratings.get(title);
    controls.up.setAttribute("aria-pressed", current === 1 ? "true" : "false");
    controls.down.setAttribute("aria-pressed", current === -1 ? "true" : "false");
  }

  // Optimistic: paint the new state immediately, then put it back if the server refuses.
  // Clicking the rating you already hold clears it, so the pair behaves as a toggle.
  async function sendRating(rec, wanted, controls) {
    const title = rec.title;
    const previous = ratings.has(title) ? ratings.get(title) : null;
    const next = previous === wanted ? null : wanted;

    if (next === null) ratings.delete(title);
    else ratings.set(title, next);
    paintRating(controls, title);

    try {
      let res;
      if (next === null) {
        res = await fetch("/api/feedback?title=" + encodeURIComponent(title), {
          method: "DELETE",
        });
      } else {
        res = await fetch("/api/feedback", {
          method: "PUT",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({
            title: title,
            author: rec.author,
            google_books_id: rec.google_books_id || null,
            rating: next,
          }),
        });
      }
      if (bounceIfUnauth(res)) return;
      if (!res.ok) throw new Error("rating rejected");
      clear(pageMsg);
    } catch (_) {
      if (previous === null) ratings.delete(title);
      else ratings.set(title, previous);
      paintRating(controls, title);
      show(pageMsg, "Couldn't save that rating. Please try again.", "error");
    }
  }

  function buildRatingControls(rec) {
    const actions = el("div", "rate");

    const up = el("button", "ghost small rate-btn", THUMB_UP);
    up.type = "button";
    up.setAttribute("aria-label", "Thumbs up: " + rec.title);
    up.setAttribute("aria-pressed", "false");

    const down = el("button", "ghost small rate-btn down", THUMB_DOWN);
    down.type = "button";
    down.setAttribute("aria-label", "Thumbs down: " + rec.title);
    down.setAttribute("aria-pressed", "false");

    const controls = { up: up, down: down };
    up.addEventListener("click", () => sendRating(rec, 1, controls));
    down.addEventListener("click", () => sendRating(rec, -1, controls));

    actions.appendChild(up);
    actions.appendChild(down);

    // Only ever shown when Google Books was unreachable — the book itself is unchecked.
    if (!rec.verified) {
      const pill = el("span", "pill", "Unverified");
      pill.title = UNVERIFIED_HINT;
      actions.appendChild(pill);
    }
    paintRating(controls, rec.title);
    return actions;
  }

  // ---- Cards ----
  function buildCover(rec, displayTitle) {
    if (rec.thumbnail_url) {
      const cover = document.createElement("img");
      cover.className = "cover-lg";
      cover.src = rec.thumbnail_url;
      cover.alt = "Cover of " + displayTitle;
      cover.loading = "lazy";
      return cover;
    }
    // Same footprint as a real cover, so a missing image doesn't shift the layout.
    const placeholder = el("div", "cover-ph");
    placeholder.setAttribute("role", "img");
    placeholder.setAttribute("aria-label", "No cover available for " + displayTitle);
    return placeholder;
  }

  function buildDescription(text) {
    const wrap = document.createDocumentFragment();
    const body = el("p", "desc collapsed", text);
    const toggle = el("button", "linkish", "Show more");
    toggle.type = "button";
    toggle.setAttribute("aria-expanded", "false");
    toggle.addEventListener("click", () => {
      const collapsed = body.classList.toggle("collapsed");
      toggle.textContent = collapsed ? "Show more" : "Show less";
      toggle.setAttribute("aria-expanded", collapsed ? "false" : "true");
    });
    wrap.appendChild(body);
    wrap.appendChild(toggle);
    return wrap;
  }

  function buildCard(rec) {
    // Prefer the canonical values Google Books confirmed — carrying them is the whole
    // reason Session 4 put them in the response — and fall back to what Claude said
    // when nothing was confirmed.
    const displayTitle = (rec.verified && rec.canonical_title) || rec.title;
    const displayAuthor =
      rec.verified && rec.canonical_authors && rec.canonical_authors.length
        ? rec.canonical_authors.join(", ")
        : rec.author;

    const card = el("article", "book");
    card.appendChild(buildCover(rec, displayTitle));

    const body = el("div", "book-body");
    body.appendChild(
      el("h3", "book-title", rec.year ? displayTitle + " (" + rec.year + ")" : displayTitle)
    );
    body.appendChild(el("p", "book-author", "by " + displayAuthor));

    // Secondary metadata, each omitted entirely when the API didn't supply it — no
    // "null pages", no dangling separators.
    const bits = [];
    if (rec.page_count) bits.push(rec.page_count + " pages");
    if (rec.published_date) bits.push("Published " + rec.published_date);
    if (bits.length) body.appendChild(el("p", "book-meta", bits.join(" · ")));

    body.appendChild(el("p", "book-reason", rec.reason));
    // Already truncated server-side, so collapsing here is purely a layout concern.
    if (rec.description) body.appendChild(buildDescription(rec.description));

    body.appendChild(buildRatingControls(rec));
    card.appendChild(body);
    return card;
  }

  // `saved` is null for a fresh set, or the replayed search for a stored one.
  function renderResults(recommendations, saved) {
    resultsEl.textContent = "";
    replaying = saved;

    if (saved) {
      resultsTitle.textContent = "Saved recommendations";
      replayText.textContent = "Saved results from " + formatStamp(saved.created_at);
      replayBanner.classList.remove("hidden");
      resultsEl.classList.add("replayed");
    } else {
      resultsTitle.textContent = "Recommendations";
      replayBanner.classList.add("hidden");
      resultsEl.classList.remove("replayed");
    }

    recommendations.forEach((rec) => resultsEl.appendChild(buildCard(rec)));
    resultsCard.classList.remove("hidden");
  }

  async function showResults(recommendations, saved) {
    await loadRatings(recommendations.map((rec) => rec.title));
    renderResults(recommendations, saved);
  }

  // ---- Recent searches ----
  function renderRecent(searches) {
    recentList.textContent = "";
    searches.forEach((search) => {
      const li = document.createElement("li");

      const meta = el("div", "meta");
      meta.appendChild(el("span", "name", search.prompt));
      const who =
        search.watching && search.watching.length ? search.watching.join(", ") : "—";
      meta.appendChild(el("span", "sub", search.result_count + " books · " + who));
      meta.appendChild(el("span", "sub", formatStamp(search.created_at)));

      const actions = el("div", "actions");
      const open = el("button", "ghost small", "Open");
      open.type = "button";
      open.addEventListener("click", () => openRecent(search.id));
      actions.appendChild(open);

      li.appendChild(meta);
      li.appendChild(actions);
      recentList.appendChild(li);
    });
    recentCard.classList.toggle("hidden", searches.length === 0);
  }

  async function loadRecent() {
    if (currentGroupId === null) return;
    const r = await getJSON("/api/groups/" + currentGroupId + "/recent-searches");
    if (!r || !r.ok) return; // history is a convenience; never block the page on it
    renderRecent(r.body.searches);
  }

  // Replay: this spends nothing, which is the whole point of storing the results.
  async function openRecent(searchId) {
    clear(pageMsg);
    const r = await getJSON(
      "/api/groups/" + currentGroupId + "/recent-searches/" + searchId
    );
    if (!r) return;
    if (!r.ok) {
      show(pageMsg, "Couldn't open that search. It may have been cleared.", "error");
      return;
    }
    if (!r.body.recommendations.length) {
      show(pageMsg, "That search has no saved books left.", "error");
      return;
    }
    await showResults(r.body.recommendations, r.body);
    resultsCard.scrollIntoView({ behavior: "smooth", block: "start" });
  }

  recentToggle.addEventListener("click", () => {
    const hidden = recentList.classList.toggle("hidden");
    recentToggle.textContent = hidden ? "Show" : "Hide";
    recentToggle.setAttribute("aria-expanded", hidden ? "false" : "true");
  });

  // Refills the form from a replayed search, so spending a fresh call stays a deliberate
  // second action rather than something that happens just by looking at history. Readers
  // are matched by display name, which is what the stored `watching` column holds; if a
  // name no longer matches anyone in the group, everyone is selected rather than nobody.
  askAgainBtn.addEventListener("click", () => {
    if (!replaying) return;
    promptEl.value = replaying.prompt;
    const wanted = new Set(replaying.watching || []);
    memberBoxes().forEach((box) => {
      box.checked = wanted.has(box.dataset.name);
    });
    if (!selectedMemberIds().length) memberBoxes().forEach((box) => (box.checked = true));
    promptEl.scrollIntoView({ behavior: "smooth", block: "center" });
    promptEl.focus();
  });

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
        await showResults(body.recommendations, null);
        await loadRecent(); // this ask just became the newest entry
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
    recentCard.classList.add("hidden");
    ratings.clear();
    replaying = null;

    const r = await getJSON("/api/groups/" + groupId + "/members");
    if (!r) return;
    if (!r.ok) {
      show(pageMsg, "Couldn't load that group's members.", "error");
      return;
    }
    renderMembers(r.body.members);
    await loadRecent();
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
