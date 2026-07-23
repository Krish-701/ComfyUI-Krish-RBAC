/**
 * Workflow run tracker
 * - Tags each queued prompt with workflow name + username
 * - Toasts "Queued as {name}: {workflow}"
 * - Floating "who is running" status bar (polls active runs)
 */
import { app } from "../../scripts/app.js";
import { api } from "../../scripts/api.js";

const ME_URL = "/usgromana/api/me";
const ACTIVE_URL = "/usgromana/api/workflow-runs/active";
const QUEUE_STATUS_URL = "/usgromana/api/queue-status";

let cachedMe = null;
let statusEl = null;
let pollTimer = null;
/** prompt_ids already notified (admin/power live feed) */
let knownJobKeys = new Set();
let jobNotifyPrimed = false;

async function fetchMe() {
    try {
        const res = await fetch(ME_URL, { credentials: "include" });
        if (!res.ok) return null;
        cachedMe = await res.json();
        return cachedMe;
    } catch {
        return null;
    }
}

function getWorkflowName() {
    try {
        // Common ComfyUI / frontend fields
        const g = app.graph;
        if (g?.extra?.workflowName) return String(g.extra.workflowName);
        if (g?.extra?.name) return String(g.extra.name);
        if (g?.config?.title) return String(g.config.title);

        // Desktop / newer UIs sometimes set these
        if (app.workflow?.filename) return String(app.workflow.filename);
        if (app.workflow?.name) return String(app.workflow.name);
        if (typeof app.ui?.getWorkflowName === "function") {
            const n = app.ui.getWorkflowName();
            if (n) return String(n);
        }

        // Last-loaded path stored by some themes
        if (window.__usgromanaLastWorkflowName) {
            return String(window.__usgromanaLastWorkflowName);
        }

        // Document title fallback (often "WorkflowName - ComfyUI")
        const title = (document.title || "").replace(/\s*[-|].*$/, "").trim();
        if (title && !/^comfyui$/i.test(title)) return title;
    } catch {
        /* ignore */
    }
    return "Unnamed workflow";
}

function showToast(message, type = "info") {
    const life = type === "error" ? 7000 : 5000;
    // Prefer existing denial toast / comfy toast systems
    try {
        if (window.app?.extensionManager?.toast?.add) {
            window.app.extensionManager.toast.add({
                severity:
                    type === "error" ? "error" : type === "warn" ? "warn" : "info",
                summary: type === "error" ? "Queue Limit" : "Workflow Queue",
                detail: message,
                life,
            });
            return;
        }
    } catch {
        /* fall through */
    }

    let host = document.getElementById("usgromana-run-toasts");
    if (!host) {
        host = document.createElement("div");
        host.id = "usgromana-run-toasts";
        host.style.cssText =
            "position:fixed;top:16px;right:16px;z-index:100000;display:flex;flex-direction:column;gap:8px;pointer-events:none;";
        document.body.appendChild(host);
    }
    const el = document.createElement("div");
    el.textContent = message;
    const border =
        type === "error"
            ? "rgba(255,100,100,0.55)"
            : "rgba(100,160,255,0.45)";
    el.style.cssText =
        `background:rgba(20,20,30,0.92);color:#e8f0ff;border:1px solid ${border};` +
        "padding:10px 14px;border-radius:10px;font:13px/1.35 system-ui,sans-serif;" +
        "box-shadow:0 8px 24px rgba(0,0,0,0.35);max-width:420px;opacity:0;transition:opacity .2s;white-space:pre-wrap;";
    host.appendChild(el);
    requestAnimationFrame(() => {
        el.style.opacity = "1";
    });
    setTimeout(() => {
        el.style.opacity = "0";
        setTimeout(() => el.remove(), 250);
    }, life);
}

function formatQueueMessage(workflowName, username, data) {
    const q = data?.usgromana_queue || data || {};
    const wait = q.waiting_number ?? data?.waiting_number;
    const ahead = q.jobs_ahead ?? data?.jobs_ahead;
    const active = q.active ?? data?.queue_active;
    const max = q.max_jobs ?? data?.queue_max;
    const who = username || cachedMe?.username || "user";
    const wf = workflowName || "workflow";
    let msg = `Queued as ${who}: ${wf}`;
    if (wait != null) {
        msg += `\nYour waiting number: #${wait}`;
        if (ahead != null && ahead > 0) {
            msg += ` (${ahead} job${ahead === 1 ? "" : "s"} ahead)`;
        } else if (ahead === 0) {
            msg += " (next / running soon)";
        }
    }
    if (max && max > 0 && active != null) {
        msg += `\nYour slots: ${active}/${max} in use`;
    }
    return msg;
}

let queueBadgeEl = null;

function ensureQueueBadge() {
    if (queueBadgeEl && document.body.contains(queueBadgeEl)) return queueBadgeEl;
    queueBadgeEl = document.createElement("div");
    queueBadgeEl.id = "usgromana-queue-badge";
    queueBadgeEl.style.cssText =
        "position:fixed;top:12px;left:50%;transform:translateX(-50%);z-index:99991;" +
        "background:linear-gradient(135deg,rgba(40,80,160,0.95),rgba(20,40,90,0.95));" +
        "color:#e8f0ff;border:1px solid rgba(120,170,255,0.5);padding:6px 14px;" +
        "border-radius:999px;font:12px/1.3 system-ui,sans-serif;font-weight:600;" +
        "box-shadow:0 4px 16px rgba(0,0,0,0.35);display:none;pointer-events:none;" +
        "backdrop-filter:blur(8px);";
    document.body.appendChild(queueBadgeEl);
    return queueBadgeEl;
}

function updateQueueBadge(qStatus) {
    const badge = ensureQueueBadge();
    if (!qStatus || !qStatus.active) {
        badge.style.display = "none";
        badge.textContent = "";
        return;
    }
    const wait = qStatus.waiting_number;
    const active = qStatus.active;
    const max = qStatus.max_jobs;
    const unlimited = qStatus.unlimited;
    let text = wait != null ? `Queue position #${wait}` : "In queue";
    if (qStatus.jobs_ahead > 0) text += ` · ${qStatus.jobs_ahead} ahead`;
    else if (qStatus.jobs_ahead === 0 && qStatus.running > 0) text += " · running";
    if (!unlimited && max > 0) text += ` · slots ${active}/${max}`;
    badge.textContent = text;
    badge.style.display = "block";
}

function ensureStatusBar() {
    if (statusEl && document.body.contains(statusEl)) return statusEl;
    statusEl = document.createElement("div");
    statusEl.id = "usgromana-active-runs-bar";
    statusEl.style.cssText =
        "position:fixed;left:50%;transform:translateX(-50%);bottom:18px;z-index:99990;" +
        "background:rgba(12,16,28,0.92);color:#dce8ff;border:1px solid rgba(90,140,255,0.4);" +
        "padding:8px 14px;border-radius:999px;font:12px/1.4 system-ui,sans-serif;" +
        "box-shadow:0 6px 20px rgba(0,0,0,0.35);display:none;max-width:min(92vw,720px);" +
        "white-space:nowrap;overflow:hidden;text-overflow:ellipsis;backdrop-filter:blur(8px);";
    document.body.appendChild(statusEl);
    return statusEl;
}

function renderActiveBar(payload, queueStatus) {
    const bar = ensureStatusBar();
    const active = payload?.active || [];
    const q = queueStatus || {};
    const parts = [];

    if (q.waiting_number != null && (q.active > 0 || active.length)) {
        parts.push(
            `You: wait #${q.waiting_number}` +
                (q.max_jobs > 0 ? ` · slots ${q.active || 0}/${q.max_jobs}` : "")
        );
    }

    if (active.length) {
        const bits = active.slice(0, 3).map((r) => {
            const who = r.username || "unknown";
            const wf = r.workflow_name || "Unnamed";
            const st = r.status === "running" ? "▶" : "…";
            return `${st} ${who}: ${wf}`;
        });
        const more = active.length > 3 ? ` (+${active.length - 3})` : "";
        parts.push(`Queue · ${bits.join(" | ")}${more}`);
    }

    if (!parts.length) {
        bar.style.display = "none";
        bar.textContent = "";
        return;
    }
    bar.textContent = parts.join("  ·  ");
    bar.title = [
        q.waiting_number != null
            ? `Your waiting number: #${q.waiting_number} (${q.jobs_ahead || 0} ahead)`
            : "",
        ...(active || []).map(
            (r) =>
                `${r.status || "?"} | ${r.username || "?"} | ${r.workflow_name || "?"} | job:${r.job_id || r.prompt_id || ""}`
        ),
    ]
        .filter(Boolean)
        .join("\n");
    bar.style.display = "block";
}

function jobKey(r) {
    return `${r.prompt_id || r.job_id || ""}|${r.username || ""}|${r.status || ""}`;
}

function canReceiveLiveJobAlerts() {
    if (!cachedMe) return false;
    return !!(
        cachedMe.can_view_all_runs ||
        cachedMe.is_admin ||
        cachedMe.role === "admin" ||
        cachedMe.role === "power"
    );
}

/**
 * Admin / power: toast when any user queues or starts a job (live).
 */
function notifyNewJobs(activeList) {
    if (!canReceiveLiveJobAlerts()) return;
    const active = Array.isArray(activeList) ? activeList : [];
    const me = (cachedMe?.username || "").toLowerCase();

    if (!jobNotifyPrimed) {
        // First poll: seed without spamming toasts for existing jobs
        for (const r of active) {
            knownJobKeys.add(jobKey(r));
            if (r.prompt_id) knownJobKeys.add(`id:${r.prompt_id}`);
        }
        jobNotifyPrimed = true;
        return;
    }

    for (const r of active) {
        const key = jobKey(r);
        const idKey = r.prompt_id ? `id:${r.prompt_id}` : null;
        const isNew = !knownJobKeys.has(key) && !(idKey && knownJobKeys.has(idKey) && r.status === "queued");
        // Notify on new prompt_id, or transition to running
        const isNewId = idKey && !knownJobKeys.has(idKey);
        const becameRunning =
            r.status === "running" &&
            idKey &&
            knownJobKeys.has(idKey) &&
            !knownJobKeys.has(key);

        knownJobKeys.add(key);
        if (idKey) knownJobKeys.add(idKey);

        if (!isNewId && !becameRunning) continue;

        const who = r.username || "user";
        // Still notify for own jobs if admin/power (they want to see all activity)
        const wf = r.workflow_name || "Unnamed workflow";
        const jid = r.job_id || r.prompt_id || "";
        const jshort =
            jid && String(jid).length > 14
                ? String(jid).slice(0, 8) + "…"
                : jid;

        if (isNewId && r.status === "queued") {
            showToast(
                `🔔 Job queued\nUser: ${who}\nWorkflow: ${wf}` +
                    (jshort ? `\nJob: ${jshort}` : ""),
                "info"
            );
        } else if (becameRunning || (isNewId && r.status === "running")) {
            showToast(
                `▶ Job running\nUser: ${who}\nWorkflow: ${wf}` +
                    (jshort ? `\nJob: ${jshort}` : ""),
                "info"
            );
        }
    }

    // Cap memory: drop old keys if set grows huge
    if (knownJobKeys.size > 500) {
        knownJobKeys = new Set(
            active.flatMap((r) => [jobKey(r), r.prompt_id ? `id:${r.prompt_id}` : null].filter(Boolean))
        );
    }
}

function annotateComfyQueueUsernames(activeList) {
    if (!canReceiveLiveJobAlerts()) return;
    const active = Array.isArray(activeList) ? activeList : [];
    if (!active.length) return;
    // Best-effort: label queue sidebar items with username for admin/power
    try {
        const labels = document.querySelectorAll(
            ".comfy-queue-item, .queue-item, [class*='queue'] [class*='item'], .p-treenode-label"
        );
        // Map by short job id fragment
        const byId = {};
        for (const r of active) {
            const id = String(r.prompt_id || r.job_id || "");
            if (id) byId[id] = r;
            if (id.length > 8) byId[id.slice(0, 8)] = r;
        }
        labels.forEach((el) => {
            if (el.dataset.usgromanaNamed) return;
            const text = el.textContent || "";
            for (const [frag, r] of Object.entries(byId)) {
                if (frag.length >= 6 && text.includes(frag)) {
                    const who = r.username || "?";
                    if (!text.includes(`[${who}]`)) {
                        el.insertAdjacentHTML(
                            "beforeend",
                            ` <span style="opacity:.75;font-size:11px;color:#8ec5ff;">[${who}]</span>`
                        );
                    }
                    el.dataset.usgromanaNamed = "1";
                    break;
                }
            }
        });
    } catch {
        /* ignore DOM differences across Comfy versions */
    }
}

async function pollActive() {
    try {
        const [activeRes, statusRes] = await Promise.all([
            fetch(ACTIVE_URL, { credentials: "include" }),
            fetch(QUEUE_STATUS_URL, { credentials: "include" }),
        ]);
        const data = activeRes.ok ? await activeRes.json() : { active: [] };
        const qStatus = statusRes.ok ? await statusRes.json() : null;
        notifyNewJobs(data.active || []);
        renderActiveBar(data, qStatus);
        updateQueueBadge(qStatus);
        annotateComfyQueueUsernames(data.active || []);
    } catch {
        /* ignore */
    }
}

function startPolling() {
    if (pollTimer) return;
    pollActive();
    // Admin/power: faster live feed (~2s). Others: every 4s.
    const interval = canReceiveLiveJobAlerts() ? 2000 : 4000;
    pollTimer = setInterval(pollActive, interval);
}

const SEED_MAX = Number.MAX_SAFE_INTEGER;

/**
 * Re-roll seeds when control_after_generate is randomize/increment so
 * user/power runs do not stick on one seed (template / multi-user quirk).
 */
function applySeedControlsToPrompt(body) {
    if (!body || typeof body !== "object") return;
    const prompt = body.prompt || body.output;
    if (!prompt || typeof prompt !== "object") return;

    const seedKeys = ["seed", "noise_seed", "noiseSeed", "rand_seed"];
    for (const node of Object.values(prompt)) {
        if (!node || typeof node !== "object") continue;
        const inputs = node.inputs;
        if (!inputs || typeof inputs !== "object") continue;

        const ctrl = String(
            inputs.control_after_generate ??
                inputs.control_before_generate ??
                inputs.seed_mode ??
                ""
        ).toLowerCase();

        for (const sk of seedKeys) {
            if (!(sk in inputs) || typeof inputs[sk] === "object") continue;
            if (ctrl === "randomize" || ctrl === "random") {
                inputs[sk] = Math.floor(Math.random() * SEED_MAX);
            } else if (ctrl === "increment" || ctrl === "inc") {
                const cur = Number(inputs[sk]) || 0;
                inputs[sk] = (cur + 1) % (SEED_MAX + 1);
            } else if (ctrl === "decrement" || ctrl === "dec") {
                const cur = Number(inputs[sk]) || 0;
                inputs[sk] = (cur - 1 + SEED_MAX + 1) % (SEED_MAX + 1);
            }
        }
    }
}

/** Update on-canvas seed widgets after a successful queue so the next run advances. */
function advanceGraphSeedWidgets() {
    try {
        const nodes = app?.graph?._nodes || [];
        for (const node of nodes) {
            const widgets = node.widgets || [];
            if (!widgets.length) continue;

            let seedWidgets = [];
            let ctrlWidget = null;
            for (const w of widgets) {
                const n = String(w?.name || "").toLowerCase();
                if (n === "seed" || n === "noise_seed" || n === "noiseseed") {
                    seedWidgets.push(w);
                }
                if (
                    n.includes("control_after_generate") ||
                    n.includes("control_before_generate") ||
                    n === "seed_mode"
                ) {
                    ctrlWidget = w;
                }
            }
            if (!seedWidgets.length) continue;

            const mode = String(ctrlWidget?.value ?? "fixed").toLowerCase();
            for (const seedW of seedWidgets) {
                let next = seedW.value;
                if (mode === "randomize" || mode === "random") {
                    next = Math.floor(Math.random() * SEED_MAX);
                } else if (mode === "increment" || mode === "inc") {
                    next = (Number(seedW.value) || 0) + 1;
                } else if (mode === "decrement" || mode === "dec") {
                    next = (Number(seedW.value) || 0) - 1;
                } else {
                    continue; // fixed
                }
                seedW.value = next;
                try {
                    if (typeof seedW.callback === "function") seedW.callback(next);
                } catch {
                    /* ignore */
                }
            }
        }
        app.graph?.setDirtyCanvas?.(true, true);
    } catch {
        /* ignore DOM / graph differences */
    }
}

function tagPromptPayload(body) {
    if (!body || typeof body !== "object") return body;
    const workflowName = getWorkflowName();
    const username = cachedMe?.username || null;

    // Ensure seeds advance for randomize/increment modes
    try {
        applySeedControlsToPrompt(body);
    } catch {
        /* ignore */
    }

    if (!body.extra_data || typeof body.extra_data !== "object") {
        body.extra_data = {};
    }
    body.extra_data.usgromana_workflow = workflowName;
    body.extra_data.workflow_name = workflowName;
    if (username) {
        body.extra_data.usgromana_username = username;
    }
    body.extra_data.usgromana = {
        ...(body.extra_data.usgromana || {}),
        workflow_name: workflowName,
        username: username,
    };

    // Also stamp workflow object when present
    try {
        const png = body.extra_data.extra_pnginfo;
        if (png && typeof png === "object" && png.workflow && typeof png.workflow === "object") {
            if (!png.workflow.filename && !png.workflow.name) {
                png.workflow.name = workflowName;
            }
            png.usgromana_run_by = username;
            png.usgromana_workflow = workflowName;
        }
    } catch {
        /* ignore */
    }
    return { body, workflowName, username };
}

function installQueueHooks() {
    // Stamp workflow name on the graph payload when queuePrompt is used.
    // Toast + tagging happen once in the fetch interceptor (queuePrompt uses fetch).
    if (api && typeof api.queuePrompt === "function" && !api.__usgromanaRunTracked) {
        const original = api.queuePrompt.bind(api);
        api.queuePrompt = async function (number, prompt) {
            try {
                if (prompt && typeof prompt === "object") {
                    if (prompt.workflow && typeof prompt.workflow === "object") {
                        const name = getWorkflowName();
                        if (!prompt.workflow.name && !prompt.workflow.filename) {
                            prompt.workflow.name = name;
                        }
                    }
                }
            } catch {
                /* ignore */
            }
            return original(number, prompt);
        };
        api.__usgromanaRunTracked = true;
    }

    // Fetch interceptor for /prompt — inject runner + workflow metadata, show toast once
    if (!window.__usgromanaPromptFetchHooked) {
        const origFetch = window.fetch.bind(window);
        window.fetch = async function (input, init) {
            try {
                const url =
                    typeof input === "string"
                        ? input
                        : input && input.url
                          ? input.url
                          : "";
                const method = (init?.method || (input && input.method) || "GET").toUpperCase();
                const isPrompt =
                    method === "POST" &&
                    (url === "/prompt" ||
                        url.endsWith("/prompt") ||
                        url.includes("/api/prompt"));

                if (isPrompt && init?.body && typeof init.body === "string") {
                    try {
                        const parsed = JSON.parse(init.body);
                        const { body, workflowName, username } = tagPromptPayload(parsed);
                        init = { ...init, body: JSON.stringify(body) };
                        const res = await origFetch(input, init);
                        let data = null;
                        try {
                            data = await res.clone().json();
                        } catch {
                            data = null;
                        }
                        if (res.ok) {
                            // Advance on-canvas seed widgets for next run (user/power fix)
                            advanceGraphSeedWidgets();
                            showToast(
                                formatQueueMessage(workflowName, username, data),
                                "info"
                            );
                            pollActive();
                        } else if (res.status === 429 || data?.code === "QUEUE_LIMIT") {
                            const err =
                                data?.error ||
                                "Queue limit reached. Wait until a job finishes.";
                            const q = data?.usgromana_queue;
                            let msg = err;
                            if (q?.waiting_number) {
                                msg += `\nYour current waiting number: #${q.waiting_number}`;
                            }
                            if (q?.active != null && q?.max_jobs) {
                                msg += `\nSlots in use: ${q.active}/${q.max_jobs}`;
                            }
                            showToast(msg, "error");
                        } else if (data?.error) {
                            showToast(String(data.error), "error");
                        }
                        return res;
                    } catch {
                        /* fall through to normal fetch */
                    }
                }
            } catch {
                /* ignore */
            }
            return origFetch(input, init);
        };
        window.__usgromanaPromptFetchHooked = true;
    }
}

// Track workflow load names when possible
function installWorkflowNameCapture() {
    try {
        const origLoad = app.loadGraphData?.bind(app);
        if (origLoad && !app.__usgromanaLoadTracked) {
            app.loadGraphData = async function (graphData, ...rest) {
                try {
                    const name =
                        graphData?.extra?.workflowName ||
                        graphData?.name ||
                        graphData?.filename ||
                        graphData?.extra?.name;
                    if (name) window.__usgromanaLastWorkflowName = String(name);
                } catch {
                    /* ignore */
                }
                return origLoad(graphData, ...rest);
            };
            app.__usgromanaLoadTracked = true;
        }
    } catch {
        /* ignore */
    }
}

app.registerExtension({
    name: "Usgromana.WorkflowRunTracker",
    async setup() {
        await fetchMe();
        installQueueHooks();
        installWorkflowNameCapture();
        startPolling();
        // Refresh identity occasionally (role changes / re-login)
        setInterval(fetchMe, 60000);
        console.log("[Usgromana] Workflow run tracker active");
    },
});
