"use strict";

/* Coding Workspace page.
 *
 * Three rules this file follows without exception:
 *
 * 1. textContent, never innerHTML. Everything rendered here comes from a
 *    project the user chose, or from a model. Both are untrusted text.
 *    CLAUDE.md's Phase 4/7 rules make this permanent, and
 *    tests/test_no_developer_instructions_in_ui.py greps for it.
 *
 * 2. Nothing here decides anything. Whether a command needs approval,
 *    whether a path is allowed, which provider is used — all server-side.
 *    A page left open from before a setting changed must not be able to
 *    act on the old answer.
 *
 * 3. The page never claims a state it has not verified. A preview is
 *    "running" only when the server says its owned process is alive and
 *    its endpoint answers; a browser check that did not run says so
 *    rather than showing zero problems.
 */

(function () {
  const root = document.getElementById("coding-root");
  if (!root) return;                       // not this page

  const el = id => document.getElementById(id);

  const state = {
    projectId: "",
    projectName: "",
    taskId: "",
    plan: null,            // the *task* plan
    creationPlan: null,    // the *new project* plan — a different thing
    poll: null,
    lastApprovalKey: "",
    steps: 0,
    focusBeforeDialog: null,
  };

  async function codingGet(path) {
    const token = getSessionCookie();
    const response = await fetch(path, {
      headers: token ? {"X-JARVIS-Session-Token": token} : {},
    });
    if (!response.ok) throw await errorFromResponse(response);
    return response.json();
  }

  async function codingBlob(path) {
    const token = getSessionCookie();
    const response = await fetch(path, {
      headers: token ? {"X-JARVIS-Session-Token": token} : {},
    });
    if (!response.ok) throw await errorFromResponse(response);
    return response.blob();
  }

  // ---------------------------------------------------------------- utils

  function clear(node) {
    while (node.firstChild) node.removeChild(node.firstChild);
    return node;
  }

  function make(tag, className, text) {
    const node = document.createElement(tag);
    if (className) node.className = className;
    if (text !== undefined && text !== null) node.textContent = String(text);
    return node;
  }

  function showError(id, message) {
    const node = el(id);
    if (!node) return;
    node.textContent = message || "";
    node.hidden = !message;
  }

  /* A long absolute path must not push the page sideways. §4 forbids
   * horizontal page overflow, so paths go in a container that scrolls
   * itself and is reachable by keyboard to do so. */
  function pathNode(text) {
    const node = make("div", "path-box");
    node.tabIndex = 0;
    node.textContent = text;
    return node;
  }

  // ------------------------------------------------------------- tabs

  const tabs = Array.from(document.querySelectorAll('#coding-tabs [role="tab"]'));

  function selectTab(tab) {
    tabs.forEach(t => {
      const selected = t === tab;
      t.setAttribute("aria-selected", selected ? "true" : "false");
      t.tabIndex = selected ? 0 : -1;
      const panel = el(t.getAttribute("aria-controls"));
      if (panel) panel.hidden = !selected;
    });
    tab.focus();
  }

  tabs.forEach((tab, index) => {
    tab.addEventListener("click", () => selectTab(tab));
    tab.addEventListener("keydown", event => {
      // Arrow-key tab navigation is what a screen-reader user expects from
      // a tablist; without it the roles describe a widget that does not
      // behave like one.
      let next = null;
      if (event.key === "ArrowRight") next = tabs[(index + 1) % tabs.length];
      else if (event.key === "ArrowLeft") next = tabs[(index - 1 + tabs.length) % tabs.length];
      else if (event.key === "Home") next = tabs[0];
      else if (event.key === "End") next = tabs[tabs.length - 1];
      if (next) { event.preventDefault(); selectTab(next); }
    });
  });

  function goToTab(id) {
    const tab = el(id);
    if (tab) selectTab(tab);
  }

  // --------------------------------------------------------- status

  async function loadStatus() {
    let status;
    try {
      status = await codingGet("/coding/status");
    } catch (e) {
      showError("coding-add-error", e.message);
      return;
    }

    const privacy = el("coding-privacy");
    if (status.privacy_mode) {
      privacy.textContent = status.privacy_note;
      privacy.hidden = false;
    } else {
      privacy.hidden = true;
    }

    const list = clear(el("coding-disabled-list"));
    (status.disabled_in_this_version || []).forEach(item => {
      list.appendChild(make("li", null, item));
    });
    const limits = status.limits || {};
    list.appendChild(make("li", null,
      `A task stops on its own after ${limits.max_steps} steps, ` +
      `${limits.max_commands} commands, ${limits.max_files_edited} edited files ` +
      `or ${limits.max_elapsed_minutes} minutes, whichever comes first.`));

    renderProtected(status.protected || {});
  }

  /* The protected-file list comes from the server, which derives it from
   * the same sets the code enforces. Writing it into the page by hand
   * would let the two drift the first time somebody adds an entry and
   * does not think to update the template. */
  function renderProtected(protectedInfo) {
    const note = el("coding-protected-note");
    if (note) note.textContent = protectedInfo.note || "";
    const container = clear(el("coding-protected"));
    if (!container) return;

    const groups = [
      ["By name", protectedInfo.filenames],
      ["By extension", protectedInfo.suffixes],
      ["Whole folders", protectedInfo.directories],
      ["And anything matching", protectedInfo.pattern_examples],
    ];
    groups.forEach(([label, values]) => {
      if (!values || !values.length) return;
      container.appendChild(make("h3", "card-title", label));
      const box = make("div", "path-box");
      box.tabIndex = 0;
      box.setAttribute("role", "region");
      box.setAttribute("aria-label", `Protected ${label.toLowerCase()}`);
      box.textContent = values.join("  ");
      container.appendChild(box);
    });
  }

  // -------------------------------------------------------- projects

  async function loadProjects() {
    const container = clear(el("coding-projects"));
    let data;
    try {
      data = await codingGet("/coding/projects");
    } catch (e) {
      container.appendChild(make("p", "form-error", e.message));
      return;
    }

    if (!data.projects.length) {
      container.appendChild(make("p", "empty",
        "No projects yet. Add one below — nothing happens until you do."));
      return;
    }

    data.projects.forEach(project => {
      const card = make("article", "card");
      const head = make("div", "flex items-center justify-between");
      head.style.flexWrap = "wrap";
      head.style.gap = "0.5rem";

      const left = make("div");
      left.appendChild(make("h3", "card-title", project.name));
      const stack = project.stack && project.stack.label ? project.stack.label : "Unknown stack";
      left.appendChild(make("p", "text-sm text-muted", stack));
      head.appendChild(left);

      const right = make("div", "btn-row");
      const open = make("button", "btn btn-primary btn-sm", "Open");
      open.type = "button";
      open.addEventListener("click", () => openProject(project));
      right.appendChild(open);

      const remove = make("button", "btn btn-ghost btn-sm", "Remove");
      remove.type = "button";
      remove.setAttribute("aria-label", `Remove ${project.name} from JARVIS's list`);
      remove.addEventListener("click", () => removeProject(project));
      right.appendChild(remove);
      head.appendChild(right);
      card.appendChild(head);

      card.appendChild(pathNode(project.root));

      if (!project.available) {
        card.appendChild(make("p", "form-error",
          "That folder is not there any more. JARVIS will not guess where it went."));
      } else if (project.git && project.git.is_repository) {
        const branch = project.git.branch || "(detached)";
        const dirty = project.has_pre_existing_changes;
        card.appendChild(make("p", "text-sm",
          dirty
            ? `Git branch ${branch} — you have uncommitted changes. JARVIS will work in a ` +
              "separate worktree so they are not touched."
            : `Git branch ${branch} — nothing uncommitted.`));
      } else {
        card.appendChild(make("p", "text-sm text-muted",
          "Not a Git repository. JARVIS cannot make an isolated branch for a task here, " +
          "and will say so before starting."));
      }

      container.appendChild(card);
    });
  }

  async function openProject(project) {
    try {
      await API.post(`/coding/projects/${encodeURIComponent(project.id)}/open`, {});
    } catch (e) {
      showError("coding-add-error", e.message);
      return;
    }
    state.projectId = project.id;
    state.projectName = project.name;
    el("coding-no-project").hidden = true;
    el("coding-task-area").hidden = false;
    goToTab("tab-task");
    loadDiff();
    loadHistory();
  }

  async function removeProject(project) {
    const token = getSessionCookie();
    const response = await fetch(`/coding/projects/${encodeURIComponent(project.id)}`, {
      method: "DELETE",
      headers: token ? { "X-JARVIS-Session-Token": token } : {},
    });
    if (!response.ok) {
      showError("coding-add-error", `Could not remove that project (${response.status}).`);
      return;
    }
    const body = await response.json();
    showError("coding-add-error", "");
    if (state.projectId === project.id) {
      state.projectId = "";
      el("coding-task-area").hidden = true;
      el("coding-no-project").hidden = false;
    }
    // Say plainly that nothing was deleted. "Removed" on its own reads,
    // to a lot of people, like the folder is gone.
    announce(body.message);
    loadProjects();
  }

  // ------------------------------------------------ native folder picker

  /* A folder is chosen in a real Windows dialog, opened by the process
   * that owns the window — not by this page, which cannot open one, and
   * not by the server, which has no window.
   *
   * The page mints a request, hands the id across pywebview's bridge, and
   * reads the answer back from the server. The answer deliberately does
   * not come back through the bridge: routing it through an authenticated
   * endpoint is what lets the server know a person really picked the
   * folder, rather than taking this page's word for it.
   */
  const picker = {
    add: {chosen: "", requestId: ""},
    new: {chosen: "", requestId: ""},
  };

  function bridgeAvailable() {
    return !!(window.pywebview && window.pywebview.api &&
              typeof window.pywebview.api.choose_folder === "function");
  }

  async function pickFolder(which, purpose, chosenId, errorId) {
    showError(errorId, "");
    if (!bridgeAvailable()) {
      // Said plainly rather than silently falling back: a Browse button
      // that quietly does nothing is worse than no Browse button.
      showError(errorId,
        "JARVIS is running in a browser rather than its own window, so Windows " +
        "cannot show a folder dialog here. Use “Type the folder path instead”.");
      openManual(which);
      return;
    }

    let minted;
    try {
      minted = await API.post("/coding/folder-dialog", {purpose});
    } catch (e) {
      showError(errorId, e.message);
      return;
    }
    const requestId = minted.request.request_id;

    let result;
    try {
      result = await window.pywebview.api.choose_folder(requestId, minted.request.prompt);
    } catch (e) {
      try { await API.post(`/coding/folder-dialog/${encodeURIComponent(requestId)}/cancel`, {}); }
      catch (ignored) { /* the request expires on its own */ }
      showError(errorId, "The folder dialog could not be opened.");
      return;
    }

    // The bridge's answer is a convenience; the server's is the record.
    let state;
    try {
      state = await codingGet(`/coding/folder-dialog/${encodeURIComponent(requestId)}`);
    } catch (e) {
      showError(errorId, e.message);
      return;
    }
    const request = state.request;

    if (request.state === "selected") {
      picker[which].chosen = request.path;
      picker[which].requestId = requestId;
      el(chosenId).textContent = request.path;
      announce("Folder chosen.");
    } else if (request.state === "cancelled") {
      // Cancel changes nothing. No request is registered, no field is
      // filled in, and any previous choice stands.
      announce("Folder selection cancelled. Nothing was changed.");
    } else {
      showError(errorId, request.error || "No folder was chosen.");
    }
  }

  function openManual(which) {
    const details = el(which === "add" ? "coding-add-manual" : "coding-new-manual");
    if (details) { details.open = true; }
  }

  function clearChoice(which, chosenId) {
    picker[which].chosen = "";
    picker[which].requestId = "";
    el(chosenId).textContent = "No folder chosen yet.";
  }

  el("coding-add-browse").addEventListener("click", () =>
    pickFolder("add", "add_project", "coding-add-chosen", "coding-add-error"));
  el("coding-new-browse").addEventListener("click", () =>
    pickFolder("new", "new_project_parent", "coding-new-chosen", "coding-new-error"));

  el("coding-add-form").addEventListener("submit", async event => {
    event.preventDefault();
    showError("coding-add-error", "");
    const typed = el("coding-add-path").value.trim();
    const requestId = picker.add.requestId;
    if (!requestId && !typed) {
      showError("coding-add-error",
        "Choose a folder with Browse, or type its full path.");
      return;
    }
    try {
      await API.post("/coding/projects", {
        // The id is preferred when both are present: it is the one the
        // server can verify a person actually chose.
        request_id: requestId,
        path: requestId ? "" : typed,
        name: el("coding-add-name").value.trim(),
      });
    } catch (e) {
      showError("coding-add-error", e.message);
      // A spent or expired selection cannot be reused, so the button must
      // not keep offering it.
      clearChoice("add", "coding-add-chosen");
      return;
    }
    el("coding-add-path").value = "";
    el("coding-add-name").value = "";
    clearChoice("add", "coding-add-chosen");
    loadProjects();
    loadStatus();
  });

  // -------------------------------------------------------- templates

  async function loadTemplates() {
    let data;
    try {
      data = await codingGet("/coding/templates");
    } catch (e) { return; }
    const select = clear(el("coding-new-template"));
    data.templates.forEach(template => {
      const option = document.createElement("option");
      option.value = template.key;
      option.textContent = template.title;
      option.dataset.description = template.description || "";
      select.appendChild(option);
    });
    const describe = () => {
      const chosen = select.options[select.selectedIndex];
      el("coding-new-template-desc").textContent =
        chosen ? (chosen.dataset.description || "") : "";
    };
    select.addEventListener("change", describe);
    describe();
  }

  // --------------------------------------------- new project, in two steps

  /* Step one produces a plan and writes nothing. Step two creates what the
   * plan describes, and takes only the plan's id — so the thing the user
   * read is the thing that runs. */

  function creationPlanRow(list, label, value) {
    const item = make("li");
    item.appendChild(make("span", "kv-key", label));
    item.appendChild(make("span", "kv-value", value));
    list.appendChild(item);
  }

  function renderCreationPlan(plan) {
    const body = clear(el("coding-create-plan-body"));

    const list = make("ul", "kv-list");
    creationPlanRow(list, "Will be created at", plan.destination);
    creationPlanRow(list, "Project name", plan.project_name);
    creationPlanRow(list, "Starting point", `${plan.template_title} (${plan.stack})`);
    creationPlanRow(list, "Files", `${plan.file_count} file(s)`);
    creationPlanRow(list, "Git", plan.git_init
      ? `git init, initial branch: ${plan.initial_branch || "not configured"}`
      : "No repository is created");
    creationPlanRow(list, "Dependencies", plan.dependencies.length
      ? `${plan.dependencies.length} listed, none installed` : "None");
    creationPlanRow(list, "Commands JARVIS will run", plan.commands.length
      ? plan.commands.join(", ") : "None");
    creationPlanRow(list, "Network access", plan.network_use === "none"
      ? "None — every template is bundled with JARVIS" : plan.network_use);
    creationPlanRow(list, "Approximate size", `${Math.max(1, Math.round(plan.approximate_bytes / 1024))} KB`);
    body.appendChild(list);

    body.appendChild(make("h3", "card-subtitle", "Exactly these files"));
    const files = make("ul", "plain-list text-sm");
    plan.files.forEach(f => files.appendChild(make("li", null, f)));
    body.appendChild(files);

    if (plan.dependencies.length) {
      body.appendChild(make("h3", "card-subtitle", "Dependencies listed, not installed"));
      const deps = make("ul", "plain-list text-sm");
      plan.dependencies.forEach(d => deps.appendChild(make("li", null, d)));
      body.appendChild(deps);
    }

    body.appendChild(make("h3", "card-subtitle", "Not created"));
    const not = make("ul", "plain-list text-sm");
    plan.protected_not_created.forEach(p => not.appendChild(make("li", null, p)));
    body.appendChild(not);

    body.appendChild(make("h3", "card-subtitle", "Checked afterwards"));
    const checks = make("ul", "plain-list text-sm");
    plan.validation.forEach(v => checks.appendChild(make("li", null, v)));
    body.appendChild(checks);

    if (plan.conflicts.length) {
      const warn = make("div", "qa-blocked");
      warn.appendChild(make("h3", "card-subtitle", "This cannot be created"));
      const ul = make("ul", "plain-list text-sm");
      plan.conflicts.forEach(c => ul.appendChild(make("li", null, c)));
      warn.appendChild(ul);
      body.appendChild(warn);
    }

    el("coding-create-plan-confirm").disabled = !plan.creatable;
    el("coding-create-plan").hidden = false;
    el("coding-create-plan").focus();
    announce(plan.creatable
      ? "Review what will be created, then confirm."
      : "This project cannot be created. See the reason on screen.");
  }

  function hideCreationPlan() {
    state.creationPlan = null;
    el("coding-create-plan").hidden = true;
    showError("coding-create-plan-error", "");
  }

  el("coding-new-form").addEventListener("submit", async event => {
    event.preventDefault();
    showError("coding-new-error", "");
    hideCreationPlan();
    const typed = el("coding-new-parent").value.trim();
    const requestId = picker.new.requestId;
    if (!requestId && !typed) {
      showError("coding-new-error",
        "Choose the folder to create it in with Browse, or type its full path.");
      return;
    }
    let data;
    try {
      data = await API.post("/coding/projects/plan", {
        parent_request_id: requestId,
        parent_path: requestId ? "" : typed,
        name: el("coding-new-name").value.trim(),
        template: el("coding-new-template").value,
      });
    } catch (e) {
      showError("coding-new-error", e.message);
      clearChoice("new", "coding-new-chosen");
      return;
    }
    // The request has been spent producing this plan; the plan now carries
    // the canonical parent, so the picker choice is no longer needed.
    clearChoice("new", "coding-new-chosen");
    el("coding-new-chosen").textContent = data.plan.parent_path;
    state.creationPlan = data.plan;
    renderCreationPlan(data.plan);
  });

  el("coding-create-plan-confirm").addEventListener("click", async () => {
    if (!state.creationPlan) return;
    showError("coding-create-plan-error", "");
    el("coding-create-plan-confirm").disabled = true;
    try {
      await API.post("/coding/projects/create", {plan_id: state.creationPlan.plan_id});
    } catch (e) {
      showError("coding-create-plan-error", e.message);
      el("coding-create-plan-confirm").disabled = false;
      return;
    }
    el("coding-new-name").value = "";
    el("coding-new-parent").value = "";
    el("coding-new-chosen").textContent = "No folder chosen yet.";
    hideCreationPlan();
    announce("Project created.");
    loadProjects();
    loadStatus();
  });

  el("coding-create-plan-change").addEventListener("click", async () => {
    await abandonPlan();
    el("coding-new-name").focus();
  });

  el("coding-create-plan-cancel").addEventListener("click", async () => {
    await abandonPlan();
    announce("Cancelled. Nothing was created.");
  });

  async function abandonPlan() {
    const plan = state.creationPlan;
    hideCreationPlan();
    if (!plan) return;
    try {
      await API.post(
        `/coding/projects/plan/${encodeURIComponent(plan.plan_id)}/cancel`, {});
    } catch (e) { /* a plan that is already gone needs no cancelling */ }
  }

  // ------------------------------------------------------------- plan

  el("coding-task-form").addEventListener("submit", async event => {
    event.preventDefault();
    showError("coding-task-error", "");
    const request = el("coding-task-request").value.trim();
    if (!request) { showError("coding-task-error", "Describe what you would like done."); return; }
    let data;
    try {
      data = await API.post("/coding/tasks/plan",
        { project_id: state.projectId, request });
    } catch (e) {
      showError("coding-task-error", e.message);
      return;
    }
    state.plan = data.plan;
    state.taskId = data.plan.task_id;
    renderPlan(data.plan);
  });

  function renderPlan(plan) {
    const body = clear(el("coding-plan-body"));

    body.appendChild(make("h3", "card-title", "What JARVIS understood"));
    body.appendChild(make("p", "text-sm", plan.objective));

    body.appendChild(make("h3", "card-title", "Where it will work"));
    const iso = plan.isolation || {};
    body.appendChild(make("p", "text-sm", iso.reason || ""));
    if (!iso.possible) {
      const warn = make("p", "form-error", "");
      warn.textContent =
        "JARVIS cannot make an isolated branch here. Starting anyway means it edits your " +
        "files directly. It will still show every change and will never undo anything you " +
        "changed yourself.";
      body.appendChild(warn);
      const label = make("label", "checkbox-row");
      const box = document.createElement("input");
      box.type = "checkbox";
      box.id = "coding-allow-in-place";
      label.appendChild(box);
      label.appendChild(make("span", null, "Work directly in my project folder"));
      body.appendChild(label);
    }

    const pre = plan.pre_existing_changes || {};
    const preCount = (pre.modified || []).length + (pre.untracked || []).length +
                     (pre.staged || []).length;
    body.appendChild(make("h3", "card-title", "Your own unsaved work"));
    body.appendChild(make("p", "text-sm",
      preCount
        ? `${preCount} file(s) you have changed but not committed. JARVIS records them now ` +
          "so the Changes tab can always tell your work from its own. It never overwrites, " +
          "stashes, cleans or resets them."
        : "Nothing uncommitted in this project right now."));

    body.appendChild(make("h3", "card-title", "Commands it may run"));
    const commands = plan.expected_commands || [];
    if (commands.length) {
      const list = make("ul", "plain-list text-sm");
      commands.forEach(entry => {
        list.appendChild(make("li", null,
          `${entry.intent}: ${entry.argv.join(" ")}  (from ${entry.source})`));
      });
      body.appendChild(list);
    } else {
      body.appendChild(make("p", "text-sm text-muted",
        "This project declares no commands, so JARVIS has none it can run without asking."));
    }

    body.appendChild(make("h3", "card-title", "It will ask you first for"));
    const approvals = make("ul", "plain-list text-sm");
    (plan.operations_requiring_approval || []).forEach(item => {
      approvals.appendChild(make("li", null, item));
    });
    body.appendChild(approvals);

    body.appendChild(make("h3", "card-title", "How it will check its work"));
    const validation = make("ul", "plain-list text-sm");
    (plan.validation_plan || []).forEach(item => validation.appendChild(make("li", null, item)));
    body.appendChild(validation);

    body.appendChild(make("h3", "card-title", "Which AI, and what leaves this computer"));
    const provider = plan.provider || {};
    const providerLine = make("p", "text-sm");
    providerLine.textContent =
      `${provider.display || provider.provider || "unknown"}` +
      (provider.model ? ` (${provider.model})` : "") +
      ` — ${provider.location === "cloud" ? "a cloud service" : "on this computer"}.`;
    body.appendChild(providerLine);
    if (provider.note) body.appendChild(make("p", "text-sm", provider.note));
    if (provider.context_scope) body.appendChild(make("p", "text-sm text-muted", provider.context_scope));
    if (provider.capability_note) body.appendChild(make("p", "text-sm", provider.capability_note));
    if (provider.blocked) {
      body.appendChild(make("p", "form-error",
        provider.reason || "No AI provider is available, so this task cannot start."));
    }

    body.appendChild(make("h3", "card-title", "Risk"));
    body.appendChild(make("p", "text-sm", `Assessed as ${plan.risk_level}.`));

    el("coding-plan").hidden = false;
    el("coding-plan-start").disabled = Boolean(provider.blocked);
    el("coding-plan-start").focus();
  }

  el("coding-plan-edit").addEventListener("click", () => {
    el("coding-plan").hidden = true;
    el("coding-task-request").focus();
  });

  el("coding-plan-cancel").addEventListener("click", () => {
    el("coding-plan").hidden = true;
    state.plan = null;
    state.taskId = "";
  });

  el("coding-plan-start").addEventListener("click", async () => {
    showError("coding-plan-error", "");
    const box = el("coding-allow-in-place");
    try {
      await API.post("/coding/tasks/start", {
        task_id: state.taskId,
        allow_in_place: Boolean(box && box.checked),
      });
    } catch (e) {
      showError("coding-plan-error", e.message);
      return;
    }
    el("coding-plan").hidden = true;
    el("coding-running").hidden = false;
    state.steps = 0;
    clear(el("coding-activity"));
    goToTab("tab-activity");
    startPolling();
  });

  // ---------------------------------------------------------- running

  function startPolling() {
    stopPolling();
    state.poll = setInterval(refreshTask, 1500);
    refreshTask();
  }

  function stopPolling() {
    if (state.poll) { clearInterval(state.poll); state.poll = null; }
  }

  async function refreshTask() {
    if (!state.taskId) return;
    let live, record;
    try {
      [live, record] = await Promise.all([
        codingGet(`/coding/tasks/${encodeURIComponent(state.taskId)}/live`),
        codingGet(`/coding/tasks/${encodeURIComponent(state.taskId)}`),
      ]);
    } catch (e) {
      return;                                  // transient; the next tick retries
    }

    renderActivity(record.task);
    renderProgress(live, record.task);
    renderPreview(live.preview);

    if (live.pending_approval) {
      openApproval(live.pending_approval);
    } else {
      closeApproval();
    }

    const finished = ["completed", "failed", "stopped", "interrupted"];
    if (!live.live && finished.includes(record.task.state)) {
      stopPolling();
      el("coding-running").hidden = true;
      renderResults(record.task);
      loadDiff();
      loadHistory();
      announce(`Task ${record.task.state}.`);
    }
  }

  function renderProgress(live, task) {
    const budget = live.budget || {};
    const steps = budget.steps || {};
    const used = (steps.ceiling || 0) - (steps.remaining || 0);
    const percent = steps.ceiling ? Math.round((used / steps.ceiling) * 100) : 0;
    el("coding-progress-bar").style.width = `${percent}%`;
    // The words carry the same information as the bar, because the bar
    // conveys it by length and colour alone.
    el("coding-progress-text").textContent = live.live
      ? `Step ${used} of ${steps.ceiling || "?"}. ` +
        `${task.files_changed.length} file(s) changed so far. ` +
        `Running for ${Math.round(live.elapsed_seconds || 0)} seconds.`
      : `Not running. State: ${task.state}.`;
  }

  function renderActivity(task) {
    const container = el("coding-activity");
    const steps = task.steps || [];
    if (steps.length === state.steps) return;      // nothing new
    state.steps = steps.length;

    clear(container);
    if (!steps.length) {
      container.appendChild(make("p", "empty", "Nothing has run yet."));
      return;
    }
    steps.forEach(step => {
      const row = make("div", "activity-row");
      const mark = step.ok === false ? "Refused" : step.ok === null ? "Waiting" : "Done";
      const badge = make("span", `badge badge-${step.ok === false ? "err" : step.ok === null ? "warn" : "ok"}`, mark);
      row.appendChild(badge);
      row.appendChild(make("span", "activity-kind", step.kind));
      row.appendChild(make("span", "activity-summary", step.summary));

      const detail = step.detail || {};
      if (detail.stdout || detail.stderr) {
        const output = make("pre", "command-output");
        output.tabIndex = 0;                     // keyboard-reachable to scroll
        output.setAttribute("role", "region");
        output.setAttribute("aria-label", `Output of ${step.summary}`);
        output.textContent = `${detail.stdout || ""}${detail.stderr || ""}`;
        row.appendChild(output);
      }
      if (detail.diff) {
        row.appendChild(diffNode(detail.diff, `Changes to ${detail.path || "a file"}`));
      }
      container.appendChild(row);
    });
  }

  function diffNode(text, label) {
    const box = make("pre", "diff-box");
    box.tabIndex = 0;
    box.setAttribute("role", "region");
    box.setAttribute("aria-label", label);
    text.split("\n").forEach(line => {
      const cls = line.startsWith("+") && !line.startsWith("+++") ? "diff-add"
                : line.startsWith("-") && !line.startsWith("---") ? "diff-del"
                : "diff-ctx";
      const span = make("span", cls, line + "\n");
      // A screen reader hears "plus" as punctuation or not at all, so the
      // added/removed distinction is spelled out rather than left to the
      // colour and the leading character.
      if (cls === "diff-add") span.setAttribute("aria-label", `added: ${line.slice(1)}`);
      if (cls === "diff-del") span.setAttribute("aria-label", `removed: ${line.slice(1)}`);
      box.appendChild(span);
    });
    return box;
  }

  // --------------------------------------------------------- approval

  function openApproval(request) {
    const key = `${request.kind}:${request.summary}`;
    if (state.lastApprovalKey === key) return;    // already showing this one
    state.lastApprovalKey = key;

    el("coding-approval-summary").textContent = request.summary;
    const detail = clear(el("coding-approval-detail"));
    const info = request.detail || {};

    if (Array.isArray(info.argv)) {
      detail.appendChild(make("p", "text-sm", "The exact command:"));
      detail.appendChild(pathNode(info.argv.join(" ")));
    }
    if (info.reason) detail.appendChild(make("p", "text-sm", info.reason));
    if (info.installs_packages) {
      detail.appendChild(make("h3", "card-title", "This installs software"));
      const list = make("ul", "plain-list text-sm");
      [["packages", "Package"], ["registry", "From"], ["lockfile", "Lockfile"],
       ["runs_scripts", "May run install scripts"], ["disk_impact", "Disk"]]
        .forEach(([key2, label]) => {
          if (info[key2] !== undefined && info[key2] !== null && info[key2] !== "") {
            list.appendChild(make("li", null, `${label}: ${info[key2]}`));
          }
        });
      detail.appendChild(list);
    }
    if (info.path) {
      detail.appendChild(make("p", "text-sm", "The exact file:"));
      detail.appendChild(pathNode(info.path));
    }

    state.focusBeforeDialog = document.activeElement;
    el("coding-approval-backdrop").hidden = false;
    el("coding-approval").hidden = false;
    el("coding-approve").focus();
    document.addEventListener("keydown", trapFocus, true);
  }

  function closeApproval() {
    if (el("coding-approval").hidden) return;
    el("coding-approval").hidden = true;
    el("coding-approval-backdrop").hidden = true;
    state.lastApprovalKey = "";
    document.removeEventListener("keydown", trapFocus, true);
    if (state.focusBeforeDialog && state.focusBeforeDialog.focus) {
      state.focusBeforeDialog.focus();
    }
  }

  function trapFocus(event) {
    const dialog = el("coding-approval");
    if (dialog.hidden) return;
    if (event.key === "Escape") {
      // Escape declines rather than dismissing: a dialog that can be
      // closed without answering leaves the task waiting forever with
      // nothing on screen to say so.
      event.preventDefault();
      decide(false);
      return;
    }
    if (event.key !== "Tab") return;
    const focusable = dialog.querySelectorAll("button, [href], input, select, textarea, [tabindex]:not([tabindex='-1'])");
    if (!focusable.length) return;
    const first = focusable[0];
    const last = focusable[focusable.length - 1];
    if (event.shiftKey && document.activeElement === first) {
      event.preventDefault(); last.focus();
    } else if (!event.shiftKey && document.activeElement === last) {
      event.preventDefault(); first.focus();
    }
  }

  async function decide(granted) {
    try {
      await API.post("/coding/tasks/decide", { task_id: state.taskId, granted });
    } catch (e) {
      announce(e.message);
      return;
    }
    closeApproval();
    announce(granted ? "Allowed." : "Not allowed. JARVIS will continue another way.");
  }

  el("coding-approve").addEventListener("click", () => decide(true));
  el("coding-decline").addEventListener("click", () => decide(false));

  // ---------------------------------------------------------- preview

  function renderPreview(preview) {
    const container = clear(el("coding-preview"));
    if (!preview) {
      container.appendChild(make("p", "empty", "No preview is running."));
      return;
    }

    // "Running" here is the server's verified answer — owned process
    // alive and endpoint answering — not a flag this page kept.
    const badge = make("span", `badge badge-${preview.running ? "ok" : "warn"}`,
                       preview.running ? "Running" : "Not running");
    container.appendChild(badge);

    if (preview.running && preview.url) {
      container.appendChild(make("p", "text-sm", `Serving at ${preview.url}`));
      container.appendChild(make("p", "text-sm text-muted",
        `Bound to ${preview.bound_to} — this computer only. Nothing on your network can reach it.`));
    } else if (preview.last_error) {
      container.appendChild(make("p", "form-error", preview.last_error));
    }

    container.appendChild(browserCheckNode(preview.browser));
  }

  // ---------------------------------------------------- browser check

  // What each of the seven states means, in words a person can act on.
  // The version this replaces had two — findings, or "not available" —
  // so a timeout, a refused navigation, a machine with no browser and a
  // cancelled check all rendered identically.
  const QA_STATES = {
    passed:               {badge: "ok",   label: "Passed"},
    failed:               {badge: "warn", label: "Failed"},
    blocked:              {badge: "warn", label: "Blocked by security policy"},
    timed_out:            {badge: "warn", label: "Timed out"},
    engine_unavailable:   {badge: "muted", label: "Browser engine unavailable"},
    preview_unavailable:  {badge: "muted", label: "Preview unavailable"},
    cancelled:            {badge: "muted", label: "Cancelled"},
  };

  // `null` means nothing looked; `0` means something looked and found
  // none. Rendering both as "0" is the defect this whole subsystem exists
  // to avoid, so the two never share a code path.
  function count(value, singular, plural) {
    if (value === null || value === undefined) return `${singular}: not checked`;
    return `${value} ${value === 1 ? singular : (plural || singular + "s")}`;
  }

  function kvRow(list, label, value) {
    const item = make("li");
    item.appendChild(make("span", "kv-key", label));
    item.appendChild(make("span", "kv-value", value));
    list.appendChild(item);
  }

  function browserCheckNode(qa) {
    const box = make("div", "qa-block");
    box.appendChild(make("h3", "card-title", "Browser check"));

    if (!qa) {
      box.appendChild(make("p", "empty",
        "No browser check has run for this preview yet. Nothing is claimed " +
        "about console errors, failed requests or layout."));
      return box;
    }

    const state = QA_STATES[qa.state] || {badge: "muted", label: qa.headline || "Unknown"};
    const header = make("p");
    header.appendChild(make("span", `badge badge-${state.badge}`, state.label));
    if (qa.engine) {
      header.appendChild(make("span", "text-sm text-muted",
        ` ${qa.engine}${qa.engine_version ? " " + qa.engine_version : ""}`));
    }
    box.appendChild(header);

    if (qa.reason) box.appendChild(make("p", "text-sm", qa.reason));
    if (qa.fix) box.appendChild(make("p", "text-sm text-muted", qa.fix));

    // `opened` is the server's record that a browser actually loaded the
    // page. Gating on it rather than on the state name matters: a check
    // that fell over before launching is also "failed", and it has no
    // findings. Inventing rows of zeroes for it is what must not happen.
    if (!qa.opened) {
      if (qa.blocked_origins && qa.blocked_origins.length) {
        box.appendChild(blockedNode(qa.blocked_origins));
      }
      return box;
    }

    const list = make("ul", "kv-list");
    kvRow(list, "Route", qa.route || "/");
    kvRow(list, "HTTP status", qa.http_status === null ? "not checked" : String(qa.http_status));
    kvRow(list, "Title", qa.title || "(none)");
    kvRow(list, "Language", qa.lang || "(not declared)");
    kvRow(list, "Top-level headings", count(qa.h1_count, "<h1> element"));
    kvRow(list, "Console errors", count(qa.console_errors, "error"));
    kvRow(list, "Page errors", count(qa.page_errors, "uncaught error"));
    kvRow(list, "Failed requests", count(qa.failed_requests, "request"));
    kvRow(list, "Broken images", count(qa.broken_images, "image"));
    kvRow(list, "Accessibility", count(qa.accessibility_findings, "finding"));
    kvRow(list, "Duration", `${qa.duration_seconds}s`);
    if (qa.checked_at) {
      kvRow(list, "Checked at", new Date(qa.checked_at * 1000).toLocaleString());
    }
    box.appendChild(list);

    if (qa.truncated) {
      box.appendChild(make("p", "text-sm text-muted",
        "There was more output than JARVIS keeps. The counts above are complete; " +
        "the examples below are the first few."));
    }

    detailList(box, "Console errors", qa.console_messages);
    detailList(box, "Uncaught page errors", qa.page_error_messages);
    detailList(box, "Failed requests", (qa.failed_request_details || [])
      .concat(qa.http_error_details || []));
    detailList(box, "Broken images", qa.broken_image_details);

    if (qa.accessibility_details && qa.accessibility_details.length) {
      box.appendChild(make("h4", "card-subtitle", "Accessibility findings"));
      const ul = make("ul", "plain-list text-sm");
      qa.accessibility_details.forEach(f => {
        const li = make("li");
        li.appendChild(make("code", null, f.rule));
        li.appendChild(make("span", null, ` — ${f.detail}`));
        ul.appendChild(li);
      });
      box.appendChild(ul);
    }
    if (qa.accessibility_rules && qa.accessibility_rules.length) {
      const details = document.createElement("details");
      details.className = "text-sm";
      const summary = document.createElement("summary");
      // So "0 findings" can be read for what it is: nine structural rules
      // passed, not a full accessibility audit passed.
      summary.textContent =
        `What this checks (${qa.accessibility_rules.length} rules, not a full audit)`;
      details.appendChild(summary);
      const ul = make("ul", "plain-list");
      qa.accessibility_rules.forEach(r =>
        ul.appendChild(make("li", null, `${r.rule} — ${r.description}`)));
      details.appendChild(ul);
      box.appendChild(details);
    }

    if (qa.horizontal_overflow) {
      box.appendChild(make("h4", "card-subtitle", "Horizontal overflow by width"));
      const ul = make("ul", "plain-list text-sm");
      Object.keys(qa.horizontal_overflow).forEach(width => {
        const v = qa.horizontal_overflow[width];
        const li = make("li", null,
          `${width}px — ${v.overflows
            ? `overflows (content ${v.scroll_width}px in ${v.client_width}px)`
            : "no overflow"}`);
        if (v.overflows && v.culprits && v.culprits.length) {
          li.appendChild(make("span", "text-muted", ` · ${v.culprits.join(", ")}`));
        }
        ul.appendChild(li);
      });
      box.appendChild(ul);
    }

    if (qa.reduced_motion) {
      box.appendChild(make("p", "text-sm",
        qa.reduced_motion.respects_reduced_motion
          ? "Reduced motion: respected — nothing animates when the system asks for less motion."
          : `Reduced motion: ${qa.reduced_motion.still_animating} element(s) still animate` +
            (qa.reduced_motion.examples && qa.reduced_motion.examples.length
              ? ` (${qa.reduced_motion.examples.join(", ")})` : "") + "."));
    }

    if (qa.blocked_origins && qa.blocked_origins.length) {
      box.appendChild(blockedNode(qa.blocked_origins));
    }
    if (qa.downloads_blocked) {
      box.appendChild(make("p", "text-sm text-muted",
        `${qa.downloads_blocked} download(s) the page started were refused.`));
    }
    if (qa.dialogs_dismissed) {
      box.appendChild(make("p", "text-sm text-muted",
        `${qa.dialogs_dismissed} dialog(s) the page opened were dismissed.`));
    }
    if (qa.scanned_all === false) {
      box.appendChild(make("p", "text-sm text-muted",
        "The page has more elements than JARVIS scans, so these findings cover " +
        "part of it rather than all of it."));
    }

    if (qa.screenshot) {
      const image = document.createElement("img");
      image.alt = `Screenshot of ${qa.route || "the page"} taken during the browser check.`;
      image.className = "preview-shot";
      box.appendChild(image);
      codingBlob(`/coding/screenshots/${encodeURIComponent(qa.screenshot)}`)
        .then(blob => {
          const url = URL.createObjectURL(blob);
          image.addEventListener("load", () => URL.revokeObjectURL(url), {once: true});
          image.src = url;
        })
        .catch(error => {
          image.remove();
          box.appendChild(make("p", "form-error", error.message));
        });
    }
    return box;
  }

  function blockedNode(entries) {
    const wrap = make("div", "qa-blocked");
    wrap.appendChild(make("h4", "card-subtitle",
      "Requests JARVIS stopped from leaving this computer"));
    wrap.appendChild(make("p", "text-sm text-muted",
      "Browser checks only ever open this task's own preview on 127.0.0.1. " +
      "Nothing below was fetched."));
    const ul = make("ul", "plain-list text-sm");
    entries.forEach(entry => ul.appendChild(make("li", null, entry)));
    wrap.appendChild(ul);
    return wrap;
  }

  function detailList(box, label, entries) {
    if (!entries || !entries.length) return;
    box.appendChild(make("h4", "card-subtitle", label));
    const ul = make("ul", "plain-list text-sm");
    entries.forEach(entry => ul.appendChild(make("li", null, entry)));
    box.appendChild(ul);
  }

  // ---------------------------------------------------------- toolchain

  const TOOL_STATES = {
    available:   {badge: "ok",    label: "Available"},
    missing:     {badge: "muted", label: "Not installed"},
    unsupported: {badge: "warn",  label: "Unsupported version"},
    refused:     {badge: "warn",  label: "Refused"},
  };

  async function loadToolchain() {
    const container = clear(el("coding-tools"));
    container.appendChild(make("p", "empty", "Checking…"));
    let data;
    try {
      const suffix = state.projectId
        ? `?project_id=${encodeURIComponent(state.projectId)}` : "";
      data = await codingGet(`/coding/toolchain${suffix}`);
    } catch (e) {
      clear(container).appendChild(make("p", "form-error", e.message));
      return;
    }
    clear(container);

    const list = make("ul", "plain-list");
    list.style.listStyle = "none";
    list.style.paddingLeft = "0";
    data.tools.forEach(tool => {
      const item = make("li");
      item.style.marginBottom = "0.75rem";
      const shown = TOOL_STATES[tool.state] || {badge: "muted", label: tool.state};
      const head = make("p");
      head.appendChild(make("strong", null, tool.display));
      head.appendChild(make("span", `badge badge-${shown.badge}`, shown.label));
      if (tool.version) head.appendChild(make("span", "text-sm text-muted", ` ${tool.version}`));
      item.appendChild(head);
      if (tool.found_via) {
        item.appendChild(make("p", "text-xs text-muted", `Found via ${tool.found_via}.`));
      }
      if (tool.detail) item.appendChild(make("p", "text-sm", tool.detail));
      // Always shown, available or not: what depends on a tool is the
      // reason a person cares whether it is there.
      tool.depends.forEach(d =>
        item.appendChild(make("p", "text-xs text-muted", `Needed for: ${d}`)));
      list.appendChild(item);
    });
    container.appendChild(list);

    if (data.cannot_run && data.cannot_run.length) {
      const warn = make("div", "qa-blocked");
      warn.appendChild(make("h3", "card-subtitle", "What cannot run right now"));
      const ul = make("ul", "plain-list text-sm");
      data.cannot_run.forEach(line => ul.appendChild(make("li", null, line)));
      warn.appendChild(ul);
      container.appendChild(warn);
    }

    if (data.project_tools) {
      container.appendChild(make("h3", "card-subtitle", "This project's own tools"));
      const ul = make("ul", "plain-list text-sm");
      data.project_tools.forEach(row => {
        const li = make("li");
        li.appendChild(make("strong", null, `${row.display}: `));
        li.appendChild(make("span", null, row.detail));
        ul.appendChild(li);
      });
      container.appendChild(ul);
    }

    if (data.virtual_environments) {
      container.appendChild(make("h3", "card-subtitle", "Python environment"));
      const ul = make("ul", "plain-list text-sm");
      data.virtual_environments.forEach(v =>
        ul.appendChild(make("li", null, v.detail)));
      container.appendChild(ul);
    }

    container.appendChild(make("p", "text-xs text-muted",
      "Nothing was installed and nothing on this computer was changed."));
    announce("Tool check finished.");
  }

  el("coding-tools-refresh").addEventListener("click", loadToolchain);
  el("tab-tools").addEventListener("click", loadToolchain);

  // ---------------------------------------------------- preview, on demand

  /* The Preview panel used to be filled in only while a task was running,
   * so opening it at any other time showed whatever the last task had
   * left there — or nothing at all, over a preview that was in fact still
   * running. It now asks the server, which answers from the owned
   * process rather than from a remembered flag. */
  async function loadPreview() {
    if (!state.projectId) {
      renderPreview(null);
      return;
    }
    try {
      const data = await codingGet(
        `/coding/preview/${encodeURIComponent(state.projectId)}`);
      renderPreview(data.preview);
    } catch (e) {
      renderPreview(null);
    }
  }

  el("tab-preview").addEventListener("click", loadPreview);

  // ------------------------------------------------------------- diff

  async function loadDiff() {
    const container = clear(el("coding-diff"));
    if (!state.projectId) {
      container.appendChild(make("p", "empty", "No project open."));
      return;
    }
    let data;
    try {
      const path = state.taskId
        ? `/coding/tasks/${encodeURIComponent(state.taskId)}/diff`
        : `/coding/projects/${encodeURIComponent(state.projectId)}/diff`;
      data = await codingGet(path);
    } catch (e) {
      container.appendChild(make("p", "form-error", e.message));
      return;
    }

    if (!data.changed.length) {
      container.appendChild(make("p", "empty", "Nothing has changed in this project."));
      return;
    }

    const mine = data.changed.filter(c => c.changed_by === "jarvis");
    const yours = data.changed.filter(c => c.changed_by !== "jarvis");

    if (yours.length) {
      container.appendChild(make("h3", "card-title", "Yours — JARVIS did not touch these"));
      const list = make("ul", "plain-list text-sm");
      yours.forEach(c => list.appendChild(make("li", null, c.path)));
      container.appendChild(list);
    }
    if (mine.length) {
      container.appendChild(make("h3", "card-title", "Changed by JARVIS"));
      const list = make("ul", "plain-list text-sm");
      mine.forEach(c => list.appendChild(make("li", null, c.path)));
      container.appendChild(list);
    }
    if (data.diff) {
      container.appendChild(make("h3", "card-title", "The full diff"));
      container.appendChild(diffNode(data.diff, "Full working-tree diff"));
    }
  }

  // ---------------------------------------------------------- results

  function renderResults(task) {
    const container = clear(el("coding-results"));
    const result = task.result || {};

    container.appendChild(make("h3", "card-title", "Outcome"));
    container.appendChild(make("p", "text-sm", result.summary || `The task ${task.state}.`));

    const changed = task.files_changed || [];
    container.appendChild(make("h3", "card-title", `Files JARVIS changed (${changed.length})`));
    if (changed.length) {
      const list = make("ul", "plain-list text-sm");
      changed.forEach(f => {
        list.appendChild(make("li", null,
          `${f.path} — ${f.kind}, +${f.lines_added || 0} / -${f.lines_removed || 0}`));
      });
      container.appendChild(list);
    } else {
      container.appendChild(make("p", "text-sm text-muted", "None."));
    }

    // The summary above is the model's own words. What follows is what
    // JARVIS actually observed, so the two can be compared.
    const commandSteps = (task.steps || []).filter(s => s.kind === "command");
    container.appendChild(make("h3", "card-title", "Commands that actually ran"));
    if (commandSteps.length) {
      const list = make("ul", "plain-list text-sm");
      commandSteps.forEach(s => list.appendChild(make("li", null, s.summary)));
      container.appendChild(list);
    } else {
      container.appendChild(make("p", "text-sm text-muted",
        "None. Nothing was run, so nothing verified these changes."));
    }

    const row = make("div", "btn-row");
    row.style.marginTop = "1rem";
    if (changed.length) {
      const commit = make("button", "btn btn-primary btn-sm", "Propose a commit");
      commit.type = "button";
      commit.addEventListener("click", () => proposeCommit(task));
      row.appendChild(commit);

      const undo = make("button", "btn btn-ghost btn-sm", "Undo JARVIS's changes");
      undo.type = "button";
      undo.addEventListener("click", () => undoChanges(task));
      row.appendChild(undo);
    }
    const deliver = make("button", "btn btn-primary btn-sm", "Download reviewed changes");
    deliver.type = "button";
    deliver.addEventListener("click", () => exportChanges(task));
    row.appendChild(deliver);

    const exportBtn = make("button", "btn btn-ghost btn-sm", "Export a redacted report");
    exportBtn.type = "button";
    exportBtn.addEventListener("click", () => exportReport(task));
    row.appendChild(exportBtn);
    container.appendChild(row);
    container.appendChild(make("p", "text-xs text-muted",
      "JARVIS does not push, open a pull request, merge or deploy. A commit stays on this " +
      "computer."));
    container.appendChild(make("p", "form-error", "", ));
  }

  async function proposeCommit(task) {
    const message = window.prompt("Commit message:", `JARVIS: ${task.request}`.slice(0, 72));
    if (!message) return;
    let data;
    try {
      data = await API.post("/coding/tasks/commit",
        { task_id: task.id, message, approved: false });
    } catch (e) { announce(e.message); return; }

    const proposal = data.proposal || {};
    const files = (proposal.paths || []).join(", ");
    if (!window.confirm(
      `Commit these files locally?\n\n${files}\n\nMessage: ${proposal.message}\n\n` +
      "Nothing is pushed anywhere.")) return;

    try {
      const done = await API.post("/coding/tasks/commit",
        { task_id: task.id, message, approved: true });
      announce(done.message);
      loadDiff();
    } catch (e) { announce(e.message); }
  }

  async function undoChanges(task) {
    if (!window.confirm(
      "Put back the files JARVIS changed in this task?\n\n" +
      "Any file you have edited since JARVIS wrote it is left exactly as it is. " +
      "Nothing you changed yourself is touched.")) return;
    try {
      const data = await API.post("/coding/tasks/undo", { task_id: task.id });
      announce(data.message);
      loadDiff();
    } catch (e) { announce(e.message); }
  }

  async function exportChanges(task) {
    let planned;
    try {
      planned = await API.post("/coding/tasks/export/plan", {task_id: task.id});
    } catch (e) { announce(e.message); return; }
    const plan = planned.plan || {};
    const paths = (plan.paths || []).join("\n");
    if (!window.confirm(
      `Download these reviewed task-worktree changes as a ZIP?\n\n${paths}\n\n` +
      "The ZIP contains changes.patch, final files and a manifest. " +
      "Your main project is not modified.")) return;
    try {
      const done = await API.post("/coding/tasks/export", {
        task_id: task.id, plan_id: plan.plan_id,
      });
      const blob = await codingBlob(done.download_url);
      const url = URL.createObjectURL(blob);
      const link = document.createElement("a");
      link.href = url;
      link.download = `jarvis-task-${task.id}.zip`;
      document.body.appendChild(link);
      link.click();
      link.remove();
      URL.revokeObjectURL(url);
      announce("Reviewed task changes downloaded. Your main project was not modified.");
    } catch (e) { announce(e.message); }
  }

  async function exportReport(task) {
    try {
      const data = await codingGet(`/coding/tasks/${encodeURIComponent(task.id)}/report`);
      const text = JSON.stringify(data.report, null, 2);
      const box = clear(el("coding-results"));
      const pre = make("pre", "command-output");
      pre.tabIndex = 0;
      pre.setAttribute("role", "region");
      pre.setAttribute("aria-label", "Redacted task report");
      pre.textContent = text;
      box.appendChild(make("p", "text-sm",
        "Paths, outcomes and hashes only. No file contents, no secrets, no environment."));
      box.appendChild(pre);
      const back = make("button", "btn btn-ghost btn-sm", "Back to the result");
      back.type = "button";
      back.addEventListener("click", () => renderResults(task));
      box.appendChild(back);
    } catch (e) { announce(e.message); }
  }

  // ---------------------------------------------------------- history

  async function loadHistory() {
    const container = clear(el("coding-history"));
    let data;
    try {
      data = await codingGet(`/coding/tasks?project_id=${encodeURIComponent(state.projectId)}`);
    } catch (e) {
      container.appendChild(make("p", "form-error", e.message));
      return;
    }
    if (!data.tasks.length) {
      container.appendChild(make("p", "empty", "No tasks yet."));
      return;
    }
    if (data.interrupted && data.interrupted.length) {
      container.appendChild(make("p", "notice",
        `${data.interrupted.length} task(s) were interrupted when JARVIS last closed. ` +
        "They are not running and were not resumed — open one to see what it had done."));
    }
    const list = make("ul", "plain-list text-sm");
    data.tasks.forEach(task => {
      const item = make("li");
      item.appendChild(make("span", `badge badge-${task.state === "completed" ? "ok" : task.state === "failed" ? "danger" : "warn"}`, task.state));
      item.appendChild(make("span", null, ` ${task.request} — ${task.files_changed} file(s), ${task.steps} step(s)`));
      list.appendChild(item);
    });
    container.appendChild(list);
  }

  // ------------------------------------------------------------ misc

  function announce(message) {
    const region = el("coding-progress-text");
    if (region) region.textContent = message;
  }

  el("coding-stop").addEventListener("click", async () => {
    try {
      const data = await API.post("/coding/tasks/stop", { task_id: state.taskId });
      announce(data.message || "Stopped.");
      if (data.survivors && data.survivors.length) {
        announce(`Stopped, but ${data.survivors.length} process(es) did not exit. ` +
                 "They are named in the log.");
      }
    } catch (e) { announce(e.message); }
    refreshTask();
  });

  el("coding-stop-all").addEventListener("click", async () => {
    if (!window.confirm("Stop every command and preview Coding Workspace started?")) return;
    try {
      const data = await API.post("/coding/processes/stop-all", {});
      announce(`Stopped ${data.stopped} process group(s).`);
    } catch (e) { announce(e.message); }
  });

  // Leaving the page must not leave a poll running against a task the
  // user has navigated away from.
  window.addEventListener("beforeunload", stopPolling);
  window.addEventListener("pagehide", stopPolling);

  loadStatus();
  loadProjects();
  loadTemplates();
})();
