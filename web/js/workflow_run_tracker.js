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

async function pollActive() {
    try {
        const [activeRes, statusRes] = await Promise.all([
            fetch(ACTIVE_URL, { credentials: "include" }),
            fetch(QUEUE_STATUS_URL, { credentials: "include" }),
        ]);
        const data = activeRes.ok ? await activeRes.json() : { active: [] };
        const qStatus = statusRes.ok ? await statusRes.json() : null;
        renderActiveBar(data, qStatus);
    } catch {
        /* ignore */
    }
}

function startPolling() {
    if (pollTimer) return;
    pollActive();
    pollTimer = setInterval(pollActive, 4000);
}

function tagPromptPayload(body) {
    if (!body || typeof body !== "object") return body;
    const workflowName = getWorkflowName();
    const username = cachedMe?.username || null;

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
