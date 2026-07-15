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
    // Prefer existing denial toast / comfy toast systems
    try {
        if (app.ui?.dialog?.show) {
            // non-blocking: use a lightweight toast if available
        }
        if (window.app?.extensionManager?.toast?.add) {
            window.app.extensionManager.toast.add({
                severity: type === "error" ? "error" : "info",
                summary: "Workflow Run",
                detail: message,
                life: 3500,
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
    el.style.cssText =
        "background:rgba(20,20,30,0.92);color:#e8f0ff;border:1px solid rgba(100,160,255,0.45);" +
        "padding:10px 14px;border-radius:10px;font:13px/1.35 system-ui,sans-serif;" +
        "box-shadow:0 8px 24px rgba(0,0,0,0.35);max-width:360px;opacity:0;transition:opacity .2s;";
    host.appendChild(el);
    requestAnimationFrame(() => {
        el.style.opacity = "1";
    });
    setTimeout(() => {
        el.style.opacity = "0";
        setTimeout(() => el.remove(), 250);
    }, 4000);
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

function renderActiveBar(payload) {
    const bar = ensureStatusBar();
    const active = payload?.active || [];
    if (!active.length) {
        bar.style.display = "none";
        bar.textContent = "";
        return;
    }
    const bits = active.slice(0, 4).map((r) => {
        const who = r.username || "unknown";
        const wf = r.workflow_name || "Unnamed";
        const st = r.status === "running" ? "▶" : "…";
        return `${st} ${who}: ${wf}`;
    });
    const more = active.length > 4 ? ` (+${active.length - 4} more)` : "";
    bar.textContent = `Runs · ${bits.join("  |  ")}${more}`;
    bar.title = active
        .map(
            (r) =>
                `${r.status || "?"} | ${r.username || "?"} | ${r.workflow_name || "?"} | ${r.prompt_id || ""}`
        )
        .join("\n");
    bar.style.display = "block";
}

async function pollActive() {
    try {
        const res = await fetch(ACTIVE_URL, { credentials: "include" });
        if (!res.ok) return;
        const data = await res.json();
        renderActiveBar(data);
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
                        if (res.ok) {
                            showToast(
                                `Queued as ${username || cachedMe?.username || "user"}: ${workflowName}`
                            );
                            pollActive();
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
