// Group management: roster for every member; invite-code tools + member removal for owners.
// Redirects to /login on a 401. All user-supplied text is set via textContent (never
// innerHTML) so display names and codes can't inject markup.
(function () {
  const pageMsg = document.getElementById("page-msg");
  const codeMsg = document.getElementById("code-msg");
  const nameEl = document.getElementById("group-name");
  const subEl = document.getElementById("group-sub");
  const memberList = document.getElementById("member-list");
  const codeList = document.getElementById("code-list");
  const codesCard = document.getElementById("codes-card");
  const pickerWrap = document.getElementById("group-picker-wrap");
  const picker = document.getElementById("group-picker");

  let currentGroupId = null;
  let ownerUserId = null; // resolved from the roster (the row with role 'owner')

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

  // ---- Roster ----
  function renderMembers(data) {
    memberList.textContent = "";
    ownerUserId = null;
    data.members.forEach((m) => {
      if (m.role === "owner") ownerUserId = m.user_id;

      const li = document.createElement("li");
      const meta = el("div", "meta");
      meta.appendChild(el("span", "name", m.display_name));
      const subBits = [];
      if (m.email) subBits.push(m.email);
      if (m.joined_at) subBits.push("joined " + m.joined_at.slice(0, 10));
      if (subBits.length) meta.appendChild(el("span", "sub", subBits.join(" · ")));
      li.appendChild(meta);

      const actions = el("div", "actions");
      actions.appendChild(el("span", "pill" + (m.role === "owner" ? " owner" : ""), m.role));

      // Owners get a two-click Remove for every non-owner member.
      if (data.is_owner && m.role !== "owner") {
        actions.appendChild(makeRemoveControl(m));
      }
      li.appendChild(actions);
      memberList.appendChild(li);
    });
  }

  function makeRemoveControl(member) {
    const btn = el("button", "small ghost", "Remove");
    let armed = false;
    btn.addEventListener("click", async () => {
      if (!armed) {
        armed = true;
        btn.textContent = "Confirm remove";
        btn.className = "small danger";
        // Disarm if the owner doesn't confirm within a few seconds.
        setTimeout(() => {
          if (armed) {
            armed = false;
            btn.textContent = "Remove";
            btn.className = "small ghost";
          }
        }, 4000);
        return;
      }
      btn.disabled = true;
      const res = await fetch(
        "/api/groups/" + currentGroupId + "/members/" + member.user_id,
        { method: "DELETE", headers: { Accept: "application/json" } }
      );
      if (bounceIfUnauth(res)) return;
      if (res.ok) {
        show(pageMsg, "Removed " + member.display_name + " from the group.", "ok");
        await loadRoster();
      } else {
        btn.disabled = false;
        armed = false;
        btn.textContent = "Remove";
        btn.className = "small ghost";
        show(pageMsg, "Couldn't remove that member.", "error");
      }
    });
    return btn;
  }

  // ---- Invite codes ----
  function renderCodes(data) {
    codeList.textContent = "";
    if (!data.invite_codes.length) {
      codeList.appendChild(el("li", "sub", "No codes yet — create one below."));
      return;
    }
    data.invite_codes.forEach((c) => {
      const li = document.createElement("li");
      const meta = el("div", "meta");
      meta.appendChild(el("span", "name", c.code));
      meta.appendChild(
        el("span", "sub", c.redemption_count + " / " + c.max_redemptions + " seats used")
      );
      li.appendChild(meta);

      const actions = el("div", "actions");
      actions.appendChild(
        el("span", "pill " + (c.active ? "on" : "off"), c.active ? "active" : "inactive")
      );
      if (c.active) {
        const btn = el("button", "small ghost", "Deactivate");
        btn.addEventListener("click", async () => {
          btn.disabled = true;
          const res = await fetch(
            "/api/groups/" + currentGroupId + "/invite-codes/" + c.id + "/deactivate",
            { method: "PATCH", headers: { Accept: "application/json" } }
          );
          if (bounceIfUnauth(res)) return;
          if (res.ok) {
            await loadCodes();
          } else {
            btn.disabled = false;
            show(codeMsg, "Couldn't deactivate that code.", "error");
          }
        });
        actions.appendChild(btn);
      }
      li.appendChild(actions);
      codeList.appendChild(li);
    });
  }

  async function loadRoster() {
    const r = await getJSON("/api/groups/" + currentGroupId + "/members");
    if (!r) return;
    if (r.ok) renderMembers(r.body);
  }

  async function loadCodes() {
    const r = await getJSON("/api/groups/" + currentGroupId + "/invite-codes");
    if (r && r.ok) renderCodes(r.body);
  }

  async function loadGroup(groupId) {
    currentGroupId = groupId;
    clear(pageMsg);
    clear(codeMsg);

    const g = await getJSON("/api/groups/" + groupId);
    if (!g) return;
    if (!g.ok) {
      show(pageMsg, "Couldn't load that group.", "error");
      return;
    }
    nameEl.textContent = g.body.name;
    const isOwner = g.body.role === "owner";
    subEl.textContent = isOwner
      ? "You own this group. Manage members and invite codes below."
      : "You're a member of this group.";

    codesCard.classList.toggle("hidden", !isOwner);

    await loadRoster();
    if (isOwner) await loadCodes();
  }

  // ---- Create-code form ----
  document.getElementById("create-code-form").addEventListener("submit", async (e) => {
    e.preventDefault();
    clear(codeMsg);
    const codeInput = document.getElementById("new-code");
    const seatsInput = document.getElementById("new-seats");
    const payload = { max_redemptions: parseInt(seatsInput.value, 10) };
    const custom = codeInput.value.trim();
    if (custom) payload.code = custom;

    const btn = e.target.querySelector("button[type=submit]");
    btn.disabled = true;
    try {
      const res = await fetch("/api/groups/" + currentGroupId + "/invite-codes", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(payload),
      });
      if (bounceIfUnauth(res)) return;
      if (res.ok) {
        const body = await res.json();
        show(codeMsg, "Created code " + body.code + " (" + body.max_redemptions + " seats).", "ok");
        codeInput.value = "";
        await loadCodes();
      } else if (res.status === 409) {
        show(codeMsg, "That code already exists — choose another.", "error");
      } else {
        show(codeMsg, "Please check the code and seat count and try again.", "error");
      }
    } catch (_) {
      show(codeMsg, "Something went wrong. Please try again.", "error");
    } finally {
      btn.disabled = false;
    }
  });

  document.getElementById("logout").addEventListener("click", async () => {
    try {
      await fetch("/api/logout", { method: "POST" });
    } catch (_) { /* ignore */ }
    window.location.href = "/login";
  });

  // ---- Bootstrap: resolve which group to show ----
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
    if (groups.length > 1) {
      pickerWrap.classList.remove("hidden");
      groups.forEach((g) => {
        const opt = document.createElement("option");
        opt.value = g.id;
        opt.textContent = g.name + (g.role === "owner" ? " (owner)" : "");
        picker.appendChild(opt);
      });
      picker.addEventListener("change", () => loadGroup(parseInt(picker.value, 10)));
    }
    await loadGroup(groups[0].id);
  }

  init();
})();
