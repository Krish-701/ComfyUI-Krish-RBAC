import { app } from "../../scripts/app.js";
import { api } from "../../scripts/api.js";
import { ComfyDialog, $el } from "../../scripts/ui.js";
import { installDenialToastWatcher } from "./js/denial_toasts.js";

const GROUPS = ["admin", "power", "user", "guest"];
let currentUser = null;
let groupsConfig = {};

// --- Extension Tab Registry API ---
/**
 * Registry for extension tabs in the Usgromana admin panel.
 * Extensions can register custom tabs to manage their own permissions or settings.
 */
window.UsgromanaAdminTabs = {
    _tabs: [],
    _defaultOrder: ["dashboard", "users", "perms", "ui-defaults", "ip", "env", "runs", "audit", "nsfw"],
    
    /**
     * Register a new tab in the admin panel.
     * @param {Object} config - Tab configuration
     * @param {string} config.id - Unique tab identifier (alphanumeric, lowercase, no spaces)
     * @param {string} config.label - Display name for the tab
     * @param {Function} config.render - Async function that renders the tab content
     *   @param {HTMLElement} container - The container element to render into
     *   @param {Object} context - Context object with available data
     *   @param {Array} context.usersList - List of all users
     *   @param {Object} context.groupsConfig - Groups configuration object
     *   @param {Object} context.currentUser - Current logged-in user object
     * @param {number} [config.order] - Optional order/position (lower numbers appear first, default: 100)
     * @param {string} [config.icon] - Optional icon class or text (not currently used, reserved for future)
     * @returns {boolean} - True if registration was successful, false if ID already exists
     * 
     * @example
     * window.UsgromanaAdminTabs.register({
     *   id: "myextension",
     *   label: "My Extension",
     *   order: 50,
     *   render: async (container, context) => {
     *     container.innerHTML = `<h3>My Extension Settings</h3>`;
     *     // Render your content here
     *   }
     * });
     */
    register(config) {
        if (!config || !config.id || !config.label || !config.render) {
            console.error("[Usgromana] Tab registration failed: missing required fields (id, label, render)");
            return false;
        }
        
        // Validate ID format
        if (!/^[a-z0-9_-]+$/.test(config.id)) {
            console.error("[Usgromana] Tab registration failed: id must be lowercase alphanumeric with underscores/hyphens only");
            return false;
        }
        
        // Check for duplicate IDs
        if (this._tabs.some(t => t.id === config.id)) {
            console.warn(`[Usgromana] Tab with id "${config.id}" already registered, skipping`);
            return false;
        }
        
        // Check for conflicts with built-in tabs
        if (this._defaultOrder.includes(config.id)) {
            console.error(`[Usgromana] Tab registration failed: id "${config.id}" conflicts with built-in tab`);
            return false;
        }
        
        const tab = {
            id: config.id,
            label: config.label,
            render: config.render,
            order: config.order !== undefined ? config.order : 100,
            icon: config.icon || null
        };
        
        this._tabs.push(tab);
        // Sort by order
        this._tabs.sort((a, b) => a.order - b.order);
        
        console.log(`[Usgromana] Registered extension tab: "${config.id}" (${config.label})`);
        return true;
    },
    
    /**
     * Unregister a tab by ID.
     * @param {string} id - Tab identifier to remove
     * @returns {boolean} - True if tab was found and removed
     */
    unregister(id) {
        const index = this._tabs.findIndex(t => t.id === id);
        if (index !== -1) {
            this._tabs.splice(index, 1);
            console.log(`[Usgromana] Unregistered extension tab: "${id}"`);
            return true;
        }
        return false;
    },
    
    /**
     * Get all registered extension tabs.
     * @returns {Array} - Array of tab configurations
     */
    getAll() {
        return [...this._tabs];
    },
    
    /**
     * Clear all registered extension tabs.
     */
    clear() {
        this._tabs = [];
        console.log("[Usgromana] Cleared all extension tabs");
    }
};

// Backend API endpoints (adjust if your backend uses different paths)
const IP_API_ENDPOINT = "/usgromana/api/ip-lists";
const USER_ENV_API_ENDPOINT = "/usgromana/api/user-env";
const UI_DEFAULTS_API_ENDPOINT = "/usgromana/api/ui-defaults";
const WORKFLOW_RUNS_API = "/usgromana/api/workflow-runs";
const WORKFLOW_RUNS_STATS_API = "/usgromana/api/workflow-runs/stats";
const WORKFLOW_RUNS_ACTIVE_API = "/usgromana/api/workflow-runs/active";
const WORKFLOW_RUNS_EXPORT_API = "/usgromana/api/workflow-runs/export";

// --- 1. BLOCKING MAP (The Enforcer) ---
// If a user lacks permission for the Key, these CSS selectors are hidden via !important
const CSS_BLOCK_MAP = {
    // --- Core UI ---
    "ui_queue_button": ["#queue-button", ".queue-button", "button.queue-button"],
    "ui_batch_widget": [".comfy-menu-queue-batch"],
    "ui_extra_options": [".comfy-menu-queue-extra"],
    
    // --- Sidebar / Left Toolbar ---
    // Core & Common Extensions
    "ui_side_history": ["#comfy-view-history-button", "[title='History']", ".pi-history"], // Often clock icon
    "ui_side_queue": ["#comfy-view-queue-button", "[title='Queue']", ".pi-list"], 
    "ui_side_assets": [
        "[title='Assets']",
        "[aria-label='Assets']",
        ".pi-folder",                 // Common icon for assets
        "#comfyui-browser-button",    // ComfyUI-Browser
        ".comfy-assets-tab",
        "button.assets-tab-button",
        ".assets-tab-button",
        ".assets-tab-button .side-bar-button-label"
    ],
    "ui_side_templates": [
        "[title='Templates']", 
        "[aria-label='Templates']",
        ".pi-copy",                   // Common icon for templates
        "#node-templates-button",     // Node Templates
        ".comfy-templates-tab",
        "button.templates-tab-button",
        ".templates-tab-button",
        ".templates-tab-button .side-bar-button-label"
    ],
    
    // --- Standard Menus (New Vue/Prime UI + legacy ids) ---
    // NOTE:
    // - We treat "Save", "Save As", "Export", and "Export (API)" all as "ui_menu_save"
    //   because they all modify or export workflows.
    // - "Open" is controlled by ui_menu_load.

    "ui_menu_save": [
        // Old ComfyUI top-bar save button (if still present anywhere)
        "#comfy-save-button",
        // New File menu entries
        "li.p-tieredmenu-item[aria-label='Save']",
        "li.p-tieredmenu-item[aria-label='Save As']",
        "li.p-tieredmenu-item[aria-label='Export']",
        "li.p-tieredmenu-item[aria-label='Export (API)']"
    ],
    "ui_menu_load": [
        // Old ComfyUI load button
        "#comfy-load-button",
        // New "Open" menu entry in the File menu
        "li.p-tieredmenu-item[aria-label='Open']"
    ],
    "ui_menu_refresh": ["#comfy-refresh-button"],

    // --- Workflow breadcrumb (Graph title dropdown) ---
    "ui_workflow_breadcrumb": [".subgraph-breadcrumb"],

    "ui_menu_clipspace": ["#comfy-clipspace-button"],
    "ui_menu_clear": ["#comfy-clear-button"],
    "ui_menu_manager": [
        ".comfyui-manager-menu-btn", 
        "button.comfyui-manager-menu-btn"
    ],
    "ui_menu_extensions": [
        "li.p-tieredmenu-item[aria-label='Manage Extensions']",
        "li.p-tieredmenu-item[aria-label='Manage Extensions'] *"
    ],
    "ui_menu_templates": [
        "li.p-tieredmenu-item[aria-label='Browse Templates']",
        "li.p-tieredmenu-item[aria-label='Browse Templates'] *"
    ],

    // --- Extensions (Hotbars, Overlays, & Settings Menu) ---
    "settings_comfy": [
        "li[aria-label='Comfy']",
        "li.p-listbox-option[aria-label='Comfy']"
    ],
    "settings_extension": [
        "li[aria-label='Extension']",
        "li.p-listbox-option[aria-label='Extension']"
    ],
    "settings_user": [
        "li[aria-label='User']",
        "li.p-listbox-option[aria-label='User']"
    ],
    "settings_keybinding": [
        "li[aria-label='Keybinding']",
        "li.p-listbox-option[aria-label='Keybinding']"
    ],
    "settings_appearance": [
        "li[aria-label='Appearance']",
        "li.p-listbox-option[aria-label='Appearance']"
    ],
    "settings_litegraph": [
        "li[aria-label='Lite Graph']",
        "li.p-listbox-option[aria-label='Lite Graph']"
    ],
    "Serttings_3D": [
        "li[aria-label='3D']",
        "li.p-listbox-option[aria-label='3D']"
    ],
    "settings_maskeditor": [
        "li[aria-label='Mask Editor']",
        "li.p-listbox-option[aria-label='Mask Editor']"
    ],
    "settings_usgromanasettings": [
        "li[aria-label='Usgromana']",
        "li.p-listbox-option[aria-label='Usgromana']"
    ],

    // iTools
    "settings_itools": [
        ".itools-floating-bar", 
        ".itools-menu-btn",
        ".itools-panel",
        "[id*='itools']"
    ],
    // Crystools
    "settings_crystools": [
        "#crystools-root",
        ".crystools-nav-bar",
        ".crystools-save-button",
        "[title^='Crystools']"
    ],
    // rgthree
    "settings_rgthree": [
        ".rgthree-menu-btn",
        ".rgthree-context-menu"
    ],
    // Gallery
    "settings_gallery": [
        ".gallery-container",
        "#gallery-button"
    ],
    // Impact Pack
    "settings_impact": [
        "#impact-pack-button" 
    ]
};

// --- 2. MODAL CSS (The Look & Feel) ---
const ADMIN_STYLES = `
/* Overlay Backdrop */
.usgromana-modal-overlay {
    position: fixed;
    inset: 0;
    width: 100vw;
    height: 100vh;
    /* a bit more transparent so the app shows through */
    background: radial-gradient(circle at top, rgba(0,0,0,0.75), rgba(0,0,0,0.92));
    backdrop-filter: blur(6px);
    z-index: 10000;
    display: flex;
    align-items: center;
    justify-content: center;
}

/* Main Window */
.usgromana-modal {
    position: relative;
    width: 960px;
    max-width: 96vw;
    height: 720px;
    max-height: 92vh;
    /* slightly more transparent card */
    background: rgba(12, 12, 16, 0.92);
    color: #f5f5f7;
    display: flex;
    flex-direction: column;
    border-radius: 12px;
    overflow: hidden;
    box-shadow:
        0 22px 60px rgba(0,0,0,0.95),
        0 0 0 1px rgba(255,255,255,0.08);
    font-family: system-ui, -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
}

/* Large transparent logo in the background */
.usgromana-modal::before {
    content: "";
    position: absolute;
    inset: 0;
    pointer-events: none;
    opacity: 0.06;  /* tweak if too bright/dim */
    background-image:
        url("/usgromana-web/assets/light_logo_transparent.png"),
        url("/usgromana/assets/light_logo_transparent.png");
    background-repeat: no-repeat;
    background-position: center 35%;
    background-size: 420px auto;
    mix-blend-mode: screen;
    z-index: 0;
}

/* Small logo badge in the top-right corner */
.usgromana-modal::after {
    content: "";
    position: absolute;
    top: 10px;
    right: 18px;
    width: 120px;
    height: 40px;
    pointer-events: none;
    background-image:
        url("/usgromana-web/assets/light_logo_transparent.png"),
        url("/usgromana/assets/light_logo_transparent.png");
    background-repeat: no-repeat;
    background-position: right center;
    background-size: contain;
    opacity: 0.4;
    z-index: 1;
}

/* Header */
.usgromana-modal-header {
    padding: 14px 20px;
    background: linear-gradient(
        to right,
        rgba(255,255,255,0.05),
        rgba(255,255,255,0.02)
    );
    border-bottom: 1px solid rgba(255,255,255,0.14);
    display: flex;
    justify-content: space-between;
    align-items: center;
    z-index: 2;
    position: relative;
}
.usgromana-modal-title {
    font-size: 17px;
    font-weight: 700;
    letter-spacing: 0.08em;
    text-transform: uppercase;
    color: #ffffff;
}
.usgromana-modal-subtitle {
    font-size: 12px;
    opacity: 0.9;
    color: #d0d0d0;
}
.usgromana-modal-close {
    cursor: pointer;
    font-size: 20px;
    color: #e0e0e0;
    background: none;
    border: none;
    transition: 0.16s ease;
    padding: 2px 6px;
    border-radius: 999px;
}
.usgromana-modal-close:hover {
    color: #ffffff;
    background: rgba(255,255,255,0.12);
    transform: translateY(-1px);
}

/* Body & Tabs */
.usgromana-modal-body {
    flex: 1;
    display: flex;
    flex-direction: column;
    overflow: hidden;
    background: radial-gradient(
        circle at top left,
        rgba(255,255,255,0.04),
        rgba(0,0,0,0.96)
    );
    z-index: 2;
    position: relative;
}
.usgromana-tabs {
    display: flex;
    background: #181b22;
    padding: 0 16px;
    border-bottom: 1px solid rgba(255,255,255,0.14);
    gap: 2px;
}
.usgromana-tab {
    padding: 10px 20px;
    cursor: pointer;
    font-size: 12px;
    font-weight: 600;
    color: #c5c8d3;
    border-bottom: 2px solid transparent;
    transition: 0.16s ease;
    text-transform: uppercase;
    letter-spacing: 0.08em;
}
.usgromana-tab:hover {
    color: #ffffff;
    background: rgba(255,255,255,0.06);
}
.usgromana-tab.active {
    color: #ffffff;
    border-bottom-color: var(--p-button-primary-bg, #3b82f6);
    background: rgba(59,130,246,0.18);
}

/* Content Area */
.usgromana-content {
    flex: 1;
    padding: 10px 0 0;
    overflow-y: auto;
    display: none;
}
.usgromana-content.active {
    display: block;
}

/* Tables */
.usgromana-table {
    width: 100%;
    border-collapse: collapse;
    font-size: 13px;
}
.usgromana-table th {
    text-align: left;
    padding: 12px 18px;
    border-bottom: 1px solid rgba(255,255,255,0.22);
    position: sticky;
    top: 0;
    z-index: 10;
    background: #171923;
    text-transform: uppercase;
    font-size: 11px;
    letter-spacing: 0.10em;
    color: #f9fafb;  /* bright */
}
.usgromana-table td {
    padding: 10px 18px;
    border-bottom: 1px solid rgba(255,255,255,0.10);
    vertical-align: middle;
    color: #e5e7f3;  /* brighter row text */
    font-size: 13px;
}
.usgromana-table tr:nth-child(even) td {
    background: rgba(255,255,255,0.02);
}
.usgromana-table tr:hover td {
    background: rgba(59,130,246,0.20);
}

/* Table Sections */
.usgromana-section-row td {
    background: #151821;
    font-weight: 800;
    text-transform: uppercase;
    letter-spacing: 0.14em;
    padding: 22px 18px 10px;
    color: #f9fafb;
    font-size: 11px;
    border-bottom: 2px solid rgba(255,255,255,0.24);
}

/* Checkbox cell */
.usgromana-check-cell {
    text-align: center;
    width: 80px;
    border-left: 1px solid rgba(255,255,255,0.15);
}

/* Buttons */
.usgromana-btn {
    background: var(--p-button-primary-bg, #3b82f6);
    color: var(--p-button-primary-text, #ffffff);
    border: 1px solid rgba(255,255,255,0.24);
    padding: 7px 16px;
    border-radius: 6px;
    cursor: pointer;
    font-weight: 600;
    font-size: 12px;
    transition: 0.16s ease;
    box-shadow: 0 4px 12px rgba(0,0,0,0.5);
}
.usgromana-btn:hover {
    opacity: 0.97;
    transform: translateY(-1px);
    box-shadow: 0 6px 18px rgba(0,0,0,0.7);
}
.usgromana-btn.secondary {
    background: rgba(255,255,255,0.04);
    color: #e5e7f3;
}
.usgromana-btn.danger {
    background: #7a2525;
    border-color: #aa3a3a;
}
.usgromana-btn.usgromana-btn-danger {
    background: #8b1f2f;
    border: 1px solid #b03a4a;
}

.usgromana-btn.usgromana-btn-danger:hover {
    background: #b03a4a;
    border-color: #d14f5d;
}

/* Launcher button in the Comfy Settings panel */
.usgromana-launch-btn {
    width: 100%;
    padding: 10px;
    font-weight: 600;
    border-radius: 8px;
    margin-top: 6px;

    /* Strong contrast on BOTH light and dark settings panels */
    background: #111827;                 /* dark slate */
    color: #f9fafb;
    border: 1px solid #1f2937;
    cursor: pointer;
    box-shadow: 0 4px 10px rgba(0,0,0,0.35);
    text-align: center;
}

.usgromana-launch-btn:hover {
    background: #1d4ed8;                 /* blue on hover */
    border-color: #1e40af;
    color: #ffffff;
    box-shadow: 0 6px 16px rgba(0,0,0,0.45);
}

/* Small info text */
.usgromana-note {
    font-size: 12px;
    opacity: 0.95;
    color: #d3d3dd;
}

/* Flex layouts */
.usgromana-row {
    display: flex;
    gap: 12px;
    align-items: flex-start;
}
.usgromana-row-space {
    display: flex;
    gap: 12px;
    justify-content: space-between;
    align-items: center;
}
.usgromana-col {
    display: flex;
    flex-direction: column;
    gap: 6px;
}

/* Inputs / textareas */
.usgromana-textarea,
.usgromana-input {
    background: #181a23;
    color: #f5f5f7;
    border-radius: 6px;
    border: 1px solid rgba(255,255,255,0.28);
    padding: 6px 8px;
    font-size: 12px;
    resize: vertical;
}
.usgromana-textarea {
    min-height: 140px;
    width: 100%;
    font-family: monospace;
}
.usgromana-select {
    background: #181a23;
    color: #f5f5f7;
    border-radius: 6px;
    border: 1px solid rgba(255,255,255,0.28);
    padding: 5px 8px;
    font-size: 12px;
}

/* Env file list / cards */
.usgromana-card {
    border-radius: 10px;
    border: 1px solid rgba(255,255,255,0.18);
    background: #13141c;
    padding: 12px 14px;
    margin: 4px 0 10px;
}
.usgromana-card-header {
    font-size: 13px;
    font-weight: 600;
    margin-bottom: 4px;
    color: #ffffff;
}
.usgromana-chip {
    display: inline-flex;
    padding: 2px 7px;
    border-radius: 999px;
    font-size: 10px;
    text-transform: uppercase;
    letter-spacing: 0.08em;
    border: 1px solid rgba(255,255,255,0.35);
}
.usgromana-file-list {
    max-height: 240px;
    overflow-y: auto;
    font-family: monospace;
    font-size: 11px;
    padding: 6px 8px;
    border-radius: 6px;
    border: 1px solid rgba(255,255,255,0.25);
    background: #101119;
    color: #f5f5f7;
}

/* Toast */
.usgromana-toast {
    position: fixed;
    top: 18px;
    left: 50%;
    transform: translateX(-50%);
    padding: 10px 16px;
    background: rgba(0,0,0,0.92);
    color: #f5f5f7;
    border-radius: 999px;
    font-size: 12px;
    z-index: 11000;
    box-shadow: 0 10px 30px rgba(0,0,0,0.8);
    border: 1px solid rgba(255,255,255,0.3);
}

/* Enforcement */
.usgromana-blocked-item {
    display: none !important;
    opacity: 0 !important;
    pointer-events: none !important;
}
`;
// --- DATA HELPERS ---
async function getData(endpoint) {
    try {
        const res = await api.fetchApi(endpoint);
        if (res.status === 200) return await res.json();
    } catch (e) { console.error(e); }
    return null;
}

function getSanitizedId(text) {
    if (!text) return "";
    return "settings_" + text.trim().toLowerCase().replace(/[^a-z0-9]/g, "");
}

// Helper function to escape HTML to prevent XSS
function escapeHtml(text) {
    if (typeof text !== 'string') return '';
    const div = document.createElement('div');
    div.textContent = text;
    return div.innerHTML;
}

// --- 3. ADMIN DIALOG CLASS ---
// Global reference to the current dialog instance
window._usgromanaDialogInstance = null;

class usgromanaDialog extends ComfyDialog {
    constructor() {
        super();
        this.overlay = $el("div.usgromana-modal-overlay");
        this.element = $el("div.usgromana-modal");
    }

    async show() {
        // Prevent multiple dialogs from being open at the same time
        if (window._usgromanaDialogInstance && window._usgromanaDialogInstance.overlay && 
            document.body.contains(window._usgromanaDialogInstance.overlay)) {
            console.log("[Usgromana] Dialog is already open, focusing existing dialog");
            // Focus the existing dialog by bringing it to front
            window._usgromanaDialogInstance.overlay.style.zIndex = "999999";
            return;
        }
        
        // Store this instance as the current dialog
        window._usgromanaDialogInstance = this;
        
        this.overlay.appendChild(this.element);
        document.body.appendChild(this.overlay);
        
        // Hide floating button when dialog opens
        if (window._usgromanaFloatingButton && window._usgromanaFloatingButton.button) {
            window._usgromanaFloatingButton.button.style.display = "none";
        }
        this.element.innerHTML = `<div style="padding:50px; text-align:center;">Loading System Configuration...</div>`;
        
        // Fetch identity first (always available). Groups/users only for admins.
        const me = await getData("/usgromana/api/me");
        currentUser = me;

        if (!currentUser || !currentUser.username) {
            this.element.innerHTML = `
                <div style="padding:40px; text-align:center; color:#ff6b6b;">
                    <h2>Login Required</h2>
                    <p>Sign in to view the workflow run log.</p>
                    <br><button id='s-close-btn' class='usgromana-btn'>Close</button>
                </div>`;
            this.element.querySelector("#s-close-btn").onclick = () => this.close();
            return;
        }

        const isAdmin = !!currentUser.is_admin;
        const canViewAllRuns = !!(currentUser.can_view_all_runs || isAdmin || currentUser.role === "power");
        // Admin: full policy UI. Power/user: Run Log only (scoped by role on the API).
        const fullAdmin = isAdmin;

        let groups = null;
        let usersList = [];
        if (fullAdmin) {
            const [groupsData, usersData] = await Promise.all([
                getData("/usgromana/api/groups"),
                getData("/usgromana/api/users"),
            ]);
            groups = groupsData;
            groupsConfig = groups?.groups || {};
            usersList = usersData?.users || [];
        } else {
            groupsConfig = {};
        }

        // Get extension tabs (admin only)
        const extensionTabs = fullAdmin ? window.UsgromanaAdminTabs.getAll() : [];
        
        // Build tabs HTML (built-in tabs first, then extension tabs)
        const builtInTabs = fullAdmin
            ? [
                { id: "dashboard", label: "Dashboard", order: 0 },
                { id: "users", label: "Users & Roles", order: 1 },
                { id: "perms", label: "Permissions", order: 2 },
                { id: "ui-defaults", label: "Default UI", order: 3 },
                { id: "ip", label: "IP Rules", order: 4 },
                { id: "env", label: "User Env", order: 5 },
                { id: "runs", label: "Run Log", order: 6 },
                { id: "queue", label: "Live Queue", order: 7 },
                { id: "audit", label: "Audit Log", order: 8 },
                { id: "nsfw", label: "NSFW Management", order: 9 }
            ]
            : canViewAllRuns
            ? [
                { id: "runs", label: "Run Log (All Users)", order: 0 },
                { id: "queue", label: "Live Queue", order: 1 }
              ]
            : [
                { id: "runs", label: "My Run Log", order: 0 }
            ];
        
        // Combine and sort all tabs
        const allTabs = [...builtInTabs, ...extensionTabs.map(t => ({ id: t.id, label: t.label, order: t.order }))];
        allTabs.sort((a, b) => a.order - b.order);
        const defaultTabId = fullAdmin ? "dashboard" : "runs";
        
        // Build tabs HTML - mark default tab as active
        // Escape tab.label to prevent XSS (tab.id is already validated during registration)
        const tabsHTML = allTabs.map((tab) => {
            const isActive = tab.id === defaultTabId;
            // ID is validated during registration (lowercase alphanumeric + underscore/hyphen), safe for HTML attributes
            // Label needs escaping as it's user-provided text
            const escapedLabel = escapeHtml(tab.label);
            return `<div class="usgromana-tab${isActive ? ' active' : ''}" data-tab="${tab.id}">${escapedLabel}</div>`;
        }).join("");
        
        // Build content containers HTML
        // tab.id is already validated during registration (lowercase alphanumeric + underscore/hyphen)
        const contentHTML = allTabs.map((tab) => {
            const isActive = tab.id === defaultTabId;
            return `<div class="usgromana-content${isActive ? ' active' : ''}" id="usgromana-tab-${tab.id}"></div>`;
        }).join("");
        
        // Render Layout
        const title = fullAdmin
            ? "Krish RBAC · Security Policy"
            : (canViewAllRuns ? "Krish RBAC · Run Log & Queue" : "Krish RBAC · My Runs");
        this.element.innerHTML = `
            <div class="usgromana-modal-header">
                <span class="usgromana-modal-title">${escapeHtml(title)}</span>
                <button class="usgromana-modal-close">✕</button>
            </div>
            <div class="usgromana-modal-body">
                <div class="usgromana-tabs">
                    ${tabsHTML}
                </div>
                ${contentHTML}
            </div>
        `;

        // Bindings
        this.element.querySelector(".usgromana-modal-close").onclick = () => this.close();
        this.overlay.onclick = (e) => { if (e.target === this.overlay) this.close(); };

        const tabs = this.element.querySelectorAll(".usgromana-tab");
        tabs.forEach(t => t.onclick = () => {
            tabs.forEach(x => x.classList.remove("active"));
            this.element.querySelectorAll(".usgromana-content").forEach(c => c.classList.remove("active"));
            t.classList.add("active");
            
            // Validate tab ID before using in querySelector to prevent injection
            const tabId = t.dataset.tab;
            if (tabId && /^[a-z0-9_-]+$/.test(tabId)) {
                const contentEl = this.element.querySelector(`#usgromana-tab-${tabId}`);
                if (contentEl) {
                    contentEl.classList.add("active");
                }
            }
        });

        // Fill Data - Built-in tabs (admin-only tabs skip for power/user)
        if (fullAdmin) {
            await this.renderDashboard(this.element.querySelector("#usgromana-tab-dashboard"));
            this.renderUsers(usersList, this.element.querySelector("#usgromana-tab-users"));
            this.renderPerms(this.element.querySelector("#usgromana-tab-perms"));
            await this.renderIpRules(this.element.querySelector("#usgromana-tab-ip"));
            this.renderUserEnv(this.element.querySelector("#usgromana-tab-env"), usersList);
            await this.renderUiDefaults(this.element.querySelector("#usgromana-tab-ui-defaults"));
            await this.renderAuditLog(this.element.querySelector("#usgromana-tab-audit"));
            this.renderNsfwManagement(this.element.querySelector("#usgromana-tab-nsfw"));
        }
        await this.renderRunLog(this.element.querySelector("#usgromana-tab-runs"), usersList);
        if (fullAdmin || canViewAllRuns) {
            await this.renderLiveQueue(this.element.querySelector("#usgromana-tab-queue"));
        }
        
        // Fill Data - Extension tabs
        const context = {
            usersList,
            groupsConfig,
            currentUser
        };
        
        for (const extTab of extensionTabs) {
            // Validate tab ID before using in querySelector (double-check, already validated during registration)
            if (!/^[a-z0-9_-]+$/.test(extTab.id)) {
                console.error(`[Usgromana] Invalid tab ID format: "${extTab.id}", skipping render`);
                continue;
            }
            
            const container = this.element.querySelector(`#usgromana-tab-${extTab.id}`);
            if (container) {
                try {
                    // Show loading state
                    container.innerHTML = `<div style="padding:20px; text-align:center; color:#c5c8d3;">Loading...</div>`;
                    // Render extension tab content
                    await extTab.render(container, context);
                } catch (error) {
                    console.error(`[Usgromana] Error rendering extension tab "${extTab.id}":`, error);
                    // Escape error message to prevent XSS
                    const errorMsg = String(error.message || "Unknown error").replace(/[<>]/g, "");
                    const escapedLabel = escapeHtml(extTab.label);
                    container.innerHTML = `
                        <div style="padding:20px; text-align:center; color:#ff6b6b;">
                            <h3>Error Loading Tab</h3>
                            <p>Failed to render "${escapedLabel}" tab.</p>
                            <p style="font-size:11px; color:#c5c8d3;">${errorMsg}</p>
                        </div>
                    `;
                }
            } else {
                console.warn(`[Usgromana] Container not found for extension tab: "${extTab.id}"`);
            }
        }
    }

    close() { 
        this.overlay.remove();
        
        // Clear the global instance if this is the current dialog
        if (window._usgromanaDialogInstance === this) {
            window._usgromanaDialogInstance = null;
        }
        
        // Show floating button when dialog closes
        if (window._usgromanaFloatingButton && window._usgromanaFloatingButton.button) {
            window._usgromanaFloatingButton.button.style.display = "flex";
        }
    }
    
    // Expose dialog class globally for floating button and other extensions
    static expose() {
        window.usgromanaDialog = usgromanaDialog;
        console.log("[Usgromana] usgromanaDialog exposed to window.usgromanaDialog");
    }

renderUsers(list, container) {
    const currentName = currentUser?.username || null;
    const self = this;

    let html = `
        <div class="usgromana-section" style="margin-bottom:18px;">
            <h3>Bulk Import Users (CSV)</h3>
            <p>
                Format: <code>name,email,password,role</code><br>
                Example: <code>nkrishnan,nkrishnan@pixstone.com,Nkri@Sh12,user</code><br>
                Users log in with their <strong>email</strong> (username also works). Roles:
                admin, power, user, guest.
            </p>
            <div class="usgromana-row" style="gap:8px; flex-wrap:wrap; align-items:center;">
                <input type="file" id="usgromana-bulk-file" accept=".csv,text/csv,text/plain" />
                <button class="usgromana-btn secondary" id="usgromana-bulk-import">Import CSV</button>
                <button class="usgromana-btn secondary" id="usgromana-users-export" title="Download name,email,password,role CSV">
                    Export Users CSV
                </button>
            </div>
            <p style="font-size:12px;opacity:.75;margin:6px 0 0;">
                Export columns: <code>name,email,password,role</code>.
                Password is the stored hash (plain passwords cannot be recovered). Use <strong>Reset PW</strong> to set a new password.
            </p>
            <div style="margin-top:10px;">
                <label class="usgromana-field-label">Or paste CSV</label>
                <textarea id="usgromana-bulk-text" class="usgromana-textarea" rows="5"
                    placeholder="name,email,password,role&#10;nkrishnan,nkrishnan@pixstone.com,Nkri@Sh12,user"></textarea>
            </div>
            <div style="margin-top:8px;">
                <small id="usgromana-bulk-status" class="usgromana-muted"></small>
            </div>
            <pre id="usgromana-bulk-result" style="margin-top:8px;max-height:160px;overflow:auto;font-size:12px;display:none;background:rgba(0,0,0,.25);padding:10px;border-radius:8px;"></pre>
        </div>

        <table class="usgromana-table">
            <thead>
                <tr>
                    <th>User Account</th>
                    <th>Email (login)</th>
                    <th>Assigned Group</th>
                    <th style="text-align:center;width:120px;">SFW Check</th>
                    <th style="text-align:right;width:180px;">Actions</th>
                </tr>
            </thead>
            <tbody>
    `;

    list.forEach(u => {
        const grp = (u.groups && u.groups.length) ? u.groups[0] : "user";
        const uname = u.username || "unknown";
        const email = u.email || "";
        const isSelf = currentName && uname === currentName;
        const isGuest = uname.toLowerCase() === "guest";

        // Per-user SFW: when enabled, NSFW-tagged images are hidden in gallery and Assets
        const sfwEnabled = u.sfw_check !== false;

        let actionsHtml = `
            <button class="usgromana-btn btn-save" data-user="${uname}">
                Save Changes
            </button>
        `;

        if (!isGuest) {
            actionsHtml += `
                <button class="usgromana-btn secondary btn-reset-pw" data-user="${uname}" title="Set a new password">
                    Reset PW
                </button>
            `;
        }

        const isDisabled = !!u.disabled;
        if (!isSelf && !isGuest) {
            actionsHtml += `
                <button class="usgromana-btn secondary btn-disable" data-user="${uname}" data-disabled="${isDisabled ? "1" : "0"}">
                    ${isDisabled ? "Enable" : "Disable"}
                </button>
            `;
            actionsHtml += `
                <button class="usgromana-btn usgromana-btn-danger btn-delete" data-user="${uname}">
                    Delete
                </button>
            `;
        }

        html += `
            <tr style="${isDisabled ? "opacity:0.55;" : ""}">
                <td><strong>${escapeHtml(uname)}</strong>${isDisabled ? ' <span style="color:#ff6b6b;font-size:11px;">(disabled)</span>' : ""}${u.must_change_password ? ' <span style="color:#e0c35a;font-size:11px;">(must change PW)</span>' : ""}</td>
                <td style="opacity:.9;font-size:12px;">${email ? escapeHtml(email) : "—"}</td>
                <td>
                    <select
                        class="usgromana-role-select"
                        data-user="${uname}"
                        style="background:var(--comfy-input-bg); color:var(--input-text); border:1px solid #555; padding:6px 10px; border-radius:4px; width: 150px;"
                    >
                        ${GROUPS.map(g => `
                            <option value="${g}" ${g === grp ? "selected" : ""}>
                                ${g.toUpperCase()}
                            </option>
                        `).join("")}
                    </select>
                </td>
                <td style="text-align:center">
                    <input
                        type="checkbox"
                        class="usgromana-sfw-toggle"
                        data-user="${uname}"
                        ${sfwEnabled ? "checked" : ""}
                    />
                </td>
                <td style="text-align:right; display:flex; flex-wrap:wrap; gap:6px; justify-content:flex-end;">
                    ${actionsHtml}
                </td>
            </tr>
        `;
    });

    html += `</tbody></table>`;
    container.innerHTML = html;

    // --- Bulk CSV import ---
    const bulkFile = container.querySelector("#usgromana-bulk-file");
    const bulkText = container.querySelector("#usgromana-bulk-text");
    const bulkBtn = container.querySelector("#usgromana-bulk-import");
    const bulkStatus = container.querySelector("#usgromana-bulk-status");
    const bulkResult = container.querySelector("#usgromana-bulk-result");

    const runBulkImport = async (csvContent) => {
        if (!csvContent || !String(csvContent).trim()) {
            bulkStatus.textContent = "Paste CSV text or choose a .csv file first.";
            return;
        }
        bulkBtn.disabled = true;
        bulkStatus.textContent = "Importing…";
        bulkResult.style.display = "none";
        try {
            const res = await fetch("/usgromana/api/users/bulk", {
                method: "POST",
                credentials: "include",
                headers: { "Content-Type": "application/json" },
                body: JSON.stringify({ csv: csvContent }),
            });
            const data = await res.json().catch(() => ({}));
            if (!res.ok) {
                bulkStatus.textContent = data.error || `Import failed (${res.status})`;
                bulkBtn.disabled = false;
                return;
            }
            const summary =
                `Created ${data.created_count || 0}, skipped ${data.skipped_count || 0}, errors ${data.error_count || 0}.`;
            bulkStatus.textContent = summary;
            bulkResult.style.display = "block";
            bulkResult.textContent = JSON.stringify(
                {
                    created: data.created || [],
                    skipped: data.skipped || [],
                    errors: data.errors || [],
                },
                null,
                2
            );
            // Brief summary before table refresh so the result is not lost silently
            window.alert(
                `Bulk import finished.\n${summary}\n\n` +
                `Created users login with their email address.`
            );
            const usersData = await getData("/usgromana/api/users");
            self.renderUsers(usersData?.users || [], container);
        } catch (e) {
            console.error("[usgromana] bulk import failed:", e);
            bulkStatus.textContent = "Import failed. See console.";
            bulkBtn.disabled = false;
        }
    };

    if (bulkBtn) {
        bulkBtn.onclick = async () => {
            // Prefer file if selected
            if (bulkFile?.files?.length) {
                const file = bulkFile.files[0];
                const text = await file.text();
                await runBulkImport(text);
                return;
            }
            await runBulkImport(bulkText?.value || "");
        };
    }
    if (bulkFile) {
        bulkFile.onchange = async () => {
            if (!bulkFile.files?.length) return;
            const text = await bulkFile.files[0].text();
            if (bulkText) bulkText.value = text;
        };
    }

    const exportUsersBtn = container.querySelector("#usgromana-users-export");
    if (exportUsersBtn) {
        exportUsersBtn.onclick = async () => {
            try {
                const res = await fetch("/usgromana/api/users/export?format=csv", {
                    credentials: "include",
                });
                if (!res.ok) {
                    const err = await res.json().catch(() => ({}));
                    alert(err.error || `Export failed (${res.status})`);
                    return;
                }
                const blob = await res.blob();
                const cd = res.headers.get("Content-Disposition") || "";
                const m = /filename="?([^"]+)"?/i.exec(cd);
                const fname = m?.[1] || "users_export.csv";
                const a = document.createElement("a");
                a.href = URL.createObjectURL(blob);
                a.download = fname;
                document.body.appendChild(a);
                a.click();
                a.remove();
                setTimeout(() => URL.revokeObjectURL(a.href), 2000);
            } catch (e) {
                console.error("[usgromana] users export failed:", e);
                alert("Users export failed. See console.");
            }
        };
    }

    // --- Save handler per user ---
    container.querySelectorAll(".btn-save").forEach(btn => {
        btn.onclick = async () => {
            const u = btn.dataset.user;
            const g = container.querySelector(`select[data-user="${u}"]`).value;

            const sfwCheckbox = container.querySelector(`.usgromana-sfw-toggle[data-user="${u}"]`);
            const sfw = sfwCheckbox ? sfwCheckbox.checked : true;

            btn.innerText = "Saving...";
            try {
                await api.fetchApi(`/usgromana/api/users/${u}`, {
                    method: "PUT",
                    body: JSON.stringify({
                        groups: [g],
                        sfw_check: sfw,
                    }),
                });
                btn.innerText = "Saved";
            } catch (e) {
                console.error("[usgromana] Failed to update user:", e);
                btn.innerText = "Error";
            }
            setTimeout(() => (btn.innerText = "Save Changes"), 1000);
        };
    });

    // --- Reset password (force change on next login by default) ---
    container.querySelectorAll(".btn-reset-pw").forEach(btn => {
        btn.onclick = async () => {
            const u = btn.dataset.user;
            const pw = window.prompt(
                `Reset password for "${u}"\n\nEnter a new temporary password:`
            );
            if (pw === null) return;
            const password = String(pw);
            const confirmPw = window.prompt(`Confirm new password for "${u}":`);
            if (confirmPw === null) return;
            if (String(confirmPw) !== password) {
                window.alert("Passwords do not match.");
                return;
            }
            const force = window.confirm(
                "Force user to change this password on next login?\n\nOK = yes (recommended)\nCancel = no"
            );

            btn.disabled = true;
            const originalText = btn.innerText;
            btn.innerText = "Saving…";
            try {
                const res = await fetch(
                    `/usgromana/api/users/${encodeURIComponent(u)}/password`,
                    {
                        method: "PUT",
                        credentials: "include",
                        headers: { "Content-Type": "application/json" },
                        body: JSON.stringify({ password, force_change: force }),
                    }
                );
                const data = await res.json().catch(() => ({}));
                if (!res.ok) {
                    window.alert(data.error || `Failed (${res.status})`);
                    btn.disabled = false;
                    btn.innerText = originalText;
                    return;
                }
                window.alert(
                    (data.message || `Password updated for ${u}.`) +
                    (force ? "\nThey must change it on next login." : "")
                );
                const usersData = await getData("/usgromana/api/users");
                self.renderUsers(usersData?.users || [], container);
            } catch (e) {
                console.error("[usgromana] password reset failed:", e);
                window.alert("Unexpected error while resetting password.");
                btn.disabled = false;
                btn.innerText = originalText;
            }
        };
    });

    // --- Disable / enable (soft ban) ---
    container.querySelectorAll(".btn-disable").forEach(btn => {
        btn.onclick = async () => {
            const u = btn.dataset.user;
            const currentlyDisabled = btn.dataset.disabled === "1";
            const next = !currentlyDisabled;
            if (!window.confirm(next
                ? `Disable account "${u}"? They cannot log in until re-enabled.`
                : `Enable account "${u}" again?`)) return;
            try {
                const res = await fetch(
                    `/usgromana/api/users/${encodeURIComponent(u)}/disabled`,
                    {
                        method: "PUT",
                        credentials: "include",
                        headers: { "Content-Type": "application/json" },
                        body: JSON.stringify({ disabled: next }),
                    }
                );
                const data = await res.json().catch(() => ({}));
                if (!res.ok) {
                    alert(data.error || `Failed (${res.status})`);
                    return;
                }
                const usersData = await getData("/usgromana/api/users");
                self.renderUsers(usersData?.users || [], container);
            } catch (e) {
                console.error("[usgromana] disable failed:", e);
                alert("Failed to update account status.");
            }
        };
    });

    // --- Delete handler per user (unchanged logic) ---
    container.querySelectorAll(".btn-delete").forEach(btn => {
        btn.onclick = async () => {
            const u = btn.dataset.user;
            const confirmed = window.confirm(
                `Are you sure you want to delete the user "${u}"?\nThis cannot be undone.`
            );
            if (!confirmed) return;

            btn.disabled = true;
            const originalText = btn.innerText;
            btn.innerText = "Deleting...";

            try {
                const res = await api.fetchApi(`/usgromana/api/users/${u}`, {
                    method: "DELETE",
                });

                if (res.status === 200) {
                    const usersData = await getData("/usgromana/api/users");
                    const usersList = usersData?.users || [];
                    self.renderUsers(usersList, container);
                } else {
                    let msg = "Failed to delete user.";
                    try {
                        const err = await res.json();
                        if (err && err.error) msg = err.error;
                    } catch {}
                    window.alert(msg);
                    btn.disabled = false;
                    btn.innerText = originalText;
                }
            } catch (e) {
                console.error("[usgromana] Failed to delete user:", e);
                window.alert("Unexpected error while deleting user.");
                btn.disabled = false;
                btn.innerText = originalText;
            }
        };
    });
}

        async renderIpRules(container) {
        container.innerHTML = `
            <div class="usgromana-section">
                <h3>IP Whitelist & Blacklist</h3>
                <p>
                    Configure IP-based access rules. Whitelisted IPs are always allowed,
                    blacklisted IPs are always denied (before other checks).
                </p>
                <div class="usgromana-row" style="margin-bottom:12px;">
                    <div>
                        <label class="usgromana-field-label" for="usgromana-ip-blacklist-after">
                            Auto-blacklist after failed login attempts
                        </label>
                        <input
                            type="number"
                            id="usgromana-ip-blacklist-after"
                            class="usgromana-input"
                            min="0"
                            step="1"
                            value="0"
                        />
                        <p style="margin-top:6px;font-size:12px;color:#9aa0a6;">
                            Set to <strong>0</strong> to never add IPs to the blacklist automatically.
                            Applies to failed login, registration, and API token generation.
                        </p>
                    </div>
                </div>
                <div class="usgromana-row">
                    <div>
                        <label class="usgromana-field-label">
                            Whitelist (one IP or CIDR per line)
                        </label>
                        <textarea class="usgromana-textarea" id="usgromana-ip-whitelist"></textarea>
                    </div>
                    <div>
                        <label class="usgromana-field-label">
                            Blacklist (one IP or CIDR per line)
                        </label>
                        <textarea class="usgromana-textarea" id="usgromana-ip-blacklist"></textarea>
                    </div>
                </div>
                <div style="display:flex; justify-content:flex-end; gap:8px; margin-top:8px;">
                    <button class="usgromana-btn secondary" id="usgromana-ip-refresh">Reload</button>
                    <button class="usgromana-btn" id="usgromana-ip-save">Save Rules</button>
                </div>
            </div>
        `;

        const wlEl = container.querySelector("#usgromana-ip-whitelist");
        const blEl = container.querySelector("#usgromana-ip-blacklist");
        const blacklistAfterEl = container.querySelector("#usgromana-ip-blacklist-after");
        const refreshBtn = container.querySelector("#usgromana-ip-refresh");
        const saveBtn = container.querySelector("#usgromana-ip-save");

        async function loadIpConfig() {
            const data = await getData(IP_API_ENDPOINT);
            const whitelist = (data?.whitelist || []).join("\n");
            const blacklist = (data?.blacklist || []).join("\n");
            wlEl.value = whitelist;
            blEl.value = blacklist;
            blacklistAfterEl.value = Number(data?.blacklist_after_attempts ?? 0);
        }

        await loadIpConfig();

        refreshBtn.onclick = () => loadIpConfig();

        saveBtn.onclick = async () => {
            const whitelist = wlEl.value
                .split(/\r?\n/)
                .map(l => l.trim())
                .filter(l => l.length > 0);
            const blacklist = blEl.value
                .split(/\r?\n/)
                .map(l => l.trim())
                .filter(l => l.length > 0);
            const blacklistAfterAttempts = parseInt(blacklistAfterEl.value, 10);

            if (
                Number.isNaN(blacklistAfterAttempts) ||
                blacklistAfterAttempts < 0 ||
                !Number.isInteger(blacklistAfterAttempts)
            ) {
                window.alert(
                    "Auto-blacklist attempts must be a whole number greater than or equal to 0."
                );
                return;
            }

            saveBtn.disabled = true;
            saveBtn.textContent = "Saving...";
            try {
                await api.fetchApi(IP_API_ENDPOINT, {
                    method: "PUT",
                    body: JSON.stringify({
                        whitelist,
                        blacklist,
                        blacklist_after_attempts: blacklistAfterAttempts,
                    }),
                });
                saveBtn.textContent = "Saved";
                setTimeout(() => (saveBtn.textContent = "Save Rules"), 1200);
            } catch (e) {
                console.error("[usgromana] Failed to save IP rules:", e);
                saveBtn.textContent = "Error";
                setTimeout(() => (saveBtn.textContent = "Save Rules"), 1500);
            } finally {
                saveBtn.disabled = false;
            }
        };
    }

renderUserEnv(container, usersList) {
    const users = usersList || [];

    const userOptions = users
        .map(u => {
            const name = u.username || "unknown";
            return `<option value="${name}">${name}</option>`;
        })
        .join("");

    container.innerHTML = `
        <div class="usgromana-section">
            <h3>User Environment & Folders</h3>
            <p>
                Manage per-user environment folders created by <code>user_env.py</code>.
                You can inspect files, purge cached folders, delete individual files,
                and mark a user's folder as the active Gallery root.
            </p>

            <div class="usgromana-row">
                <div>
                    <label class="usgromana-field-label">User</label>
                    <select id="usgromana-env-user" class="usgromana-select">
                        ${userOptions}
                    </select>
                </div>
                <div style="display:flex; align-items:flex-end; gap:8px; justify-content:flex-end;">
                    <button class="usgromana-btn secondary" id="usgromana-env-list">List Files</button>
                    <button class="usgromana-btn danger" id="usgromana-env-purge">Purge Folders</button>
                </div>
            </div>

            <div class="usgromana-row" style="align-items:center; margin-top:4px;">
                <div style="display:flex; align-items:center; gap:8px;">
                    <input type="checkbox" id="usgromana-env-gallery-toggle" />
                    <label for="usgromana-env-gallery-toggle">
                        Use this user's folder as Gallery root
                    </label>
                </div>
            </div>

            <div style="margin-top:12px;">
                <label class="usgromana-field-label">Folder Contents / Status</label>
                <textarea id="usgromana-env-output" class="usgromana-textarea" readonly></textarea>
            </div>

            <div class="usgromana-row" style="margin-top:8px; align-items:flex-end; gap:8px;">
                <div style="flex:1;">
                    <label class="usgromana-field-label">Delete Single File</label>
                    <select id="usgromana-env-file" class="usgromana-select">
                        <option value="">(no files loaded yet)</option>
                    </select>
                </div>
                <button class="usgromana-btn danger" id="usgromana-env-delete">Delete File</button>
            </div>
        </div>

        <div class="usgromana-section" style="margin-top:16px;">
            <h3>Workflow Management</h3>
            <p>
                Promote a user's workflow into the global/default workflow list
                so it becomes visible to all users.
            </p>

            <div class="usgromana-row">
                <div>
                    <label class="usgromana-field-label">User</label>
                    <select id="usgromana-wf-user" class="usgromana-select">
                        ${userOptions}
                    </select>
                </div>
                <div style="flex:1;">
                    <label class="usgromana-field-label">Workflow</label>
                    <select id="usgromana-wf-select" class="usgromana-select">
                        <option value="">(load workflows...)</option>
                    </select>
                </div>
                <div style="display:flex; align-items:flex-end; gap:8px;">
                    <button class="usgromana-btn secondary" id="usgromana-wf-load">Load Workflows</button>
                    <button class="usgromana-btn primary" id="usgromana-wf-promote">Promote to Default</button>
                </div>
            </div>
            <div style="margin-top:6px; display:flex; align-items:center; gap:8px;">
                <input type="checkbox" id="usgromana-wf-delete-source" />
                <label for="usgromana-wf-delete-source">
                    Remove from this user's workflow folder after promotion
                </label>
            </div>
            <div style="margin-top:6px;">
                <small id="usgromana-wf-status" class="usgromana-muted"></small>
            </div>
        </div>
    `;

    const userSelect = container.querySelector("#usgromana-env-user");
    const listBtn = container.querySelector("#usgromana-env-list");
    const purgeBtn = container.querySelector("#usgromana-env-purge");
    const galleryToggle = container.querySelector("#usgromana-env-gallery-toggle");
    const output = container.querySelector("#usgromana-env-output");
    const fileSelect = container.querySelector("#usgromana-env-file");
    const deleteBtn = container.querySelector("#usgromana-env-delete");

    const wfUserSelect = container.querySelector("#usgromana-wf-user");
    const wfSelect = container.querySelector("#usgromana-wf-select");
    const wfLoadBtn = container.querySelector("#usgromana-wf-load");
    const wfPromoteBtn = container.querySelector("#usgromana-wf-promote");
    const wfDeleteSource = container.querySelector("#usgromana-wf-delete-source");
    const wfStatus = container.querySelector("#usgromana-wf-status");

    let envFiles = [];

    function getSelectedUser() {
        return userSelect?.value || null;
    }

    function getWorkflowUser() {
        return wfUserSelect?.value || getSelectedUser() || null;
    }

    function populateEnvFileOptions(files) {
        envFiles = files || [];
        if (!fileSelect) return;

        fileSelect.innerHTML = "";

        if (!envFiles.length) {
            fileSelect.innerHTML = `<option value="">(no files)</option>`;
            return;
        }

        const placeholder = document.createElement("option");
        placeholder.value = "";
        placeholder.textContent = "(select a file...)";
        fileSelect.appendChild(placeholder);

        envFiles.forEach(path => {
            const opt = document.createElement("option");
            opt.value = path;
            opt.textContent = path;
            fileSelect.appendChild(opt);
        });
    }

    async function refreshStatus() {
        const user = getSelectedUser();
        if (!user) return;
        output.value = "Loading status...";
        try {
            const res = await api.fetchApi(USER_ENV_API_ENDPOINT, {
                method: "POST",
                body: JSON.stringify({ action: "status", user }),
            });
            if (res.status === 200) {
                const data = await res.json();
                galleryToggle.checked = !!data.is_gallery_root;
                const files = data.files || [];
                populateEnvFileOptions(files);
                output.value =
                    (data.message || "") +
                    (files.length
                        ? "\n\nFiles:\n" + files.join("\n")
                        : files.length === 0
                        ? "\n\n(no files reported)"
                        : "");
            } else {
                output.value = "Error loading status: " + res.status;
            }
        } catch (e) {
            console.error("[usgromana] env status error:", e);
            output.value = "Error loading status. See console.";
        }
    }

    userSelect.onchange = () => {
        if (wfUserSelect) wfUserSelect.value = userSelect.value;
        refreshStatus();
    };

    listBtn.onclick = async () => {
        const user = getSelectedUser();
        if (!user) return;
        output.value = "Listing files...";
        try {
            const res = await api.fetchApi(USER_ENV_API_ENDPOINT, {
                method: "POST",
                body: JSON.stringify({ action: "list", user }),
            });
            if (res.status === 200) {
                const data = await res.json();
                const files = data.files || [];
                populateEnvFileOptions(files);
                output.value = files.length
                    ? files.join("\n")
                    : "(no files found)";
            } else {
                output.value = "Error listing files: " + res.status;
            }
        } catch (e) {
            console.error("[usgromana] env list error:", e);
            output.value = "Error listing files. See console.";
        }
    };

    deleteBtn.onclick = async () => {
        const user = getSelectedUser();
        const file = fileSelect?.value;
        if (!user || !file) return;
        output.value = `Deleting '${file}'...`;
        try {
            const res = await api.fetchApi(USER_ENV_API_ENDPOINT, {
                method: "POST",
                body: JSON.stringify({ action: "delete_file", user, file }),
            });
            const data = await res.json();
            if (res.status === 200) {
                output.value = data.message || `Deleted '${file}'.`;
            } else {
                output.value = data.error || `Error deleting file: ${res.status}`;
            }
        } catch (e) {
            console.error("[usgromana] env delete_file error:", e);
            output.value = "Error deleting file. See console.";
        } finally {
            refreshStatus();
        }
    };

    purgeBtn.onclick = async () => {
        const user = getSelectedUser();
        if (!user) return;
        purgeBtn.disabled = true;
        output.value = "Purging...";
        try {
            const res = await api.fetchApi(USER_ENV_API_ENDPOINT, {
                method: "POST",
                body: JSON.stringify({ action: "purge", user }),
            });
            if (res.status === 200) {
                const data = await res.json();
                output.value = data.message || "Purge completed.";
            } else {
                output.value = "Error purging folders: " + res.status;
            }
        } catch (e) {
            console.error("[usgromana] env purge error:", e);
            output.value = "Error purging folders. See console.";
        } finally {
            purgeBtn.disabled = false;
            refreshStatus();
        }
    };

    galleryToggle.onchange = async () => {
        const user = getSelectedUser();
        if (!user) return;
        const enable = galleryToggle.checked;

        try {
            const res = await api.fetchApi(USER_ENV_API_ENDPOINT, {
                method: "POST",
                body: JSON.stringify({
                    action: "set_gallery_root",
                    user,
                    enable,
                }),
            });
            if (res.status === 200) {
                const data = await res.json();
                output.value = data.message || "Gallery root updated.";
            } else {
                output.value = "Error updating gallery root: " + res.status;
            }
        } catch (e) {
            console.error("[usgromana] env gallery toggle error:", e);
            output.value = "Error updating gallery root. See console.";
        }
    };

    // --- Workflow admin handlers ---

    wfUserSelect.onchange = () => {
        wfStatus.textContent = "";
        wfSelect.innerHTML = '<option value="">(load workflows...)</option>';
    };

    wfLoadBtn.onclick = async () => {
        const user = getWorkflowUser();
        if (!user) return;
        wfStatus.textContent = "Loading workflows...";
        wfSelect.innerHTML = '<option value="">(loading...)</option>';

        try {
            const res = await api.fetchApi(USER_ENV_API_ENDPOINT, {
                method: "POST",
                body: JSON.stringify({
                    action: "list_workflows",
                    user,
                }),
            });
            if (res.status === 200) {
                const data = await res.json();
                const workflows = data.workflows || [];
                wfSelect.innerHTML = "";

                if (!workflows.length) {
                    wfSelect.innerHTML =
                        '<option value="">(no workflows)</option>';
                } else {
                    workflows.forEach((wf) => {
                        const opt = document.createElement("option");
                        opt.value = wf;
                        opt.textContent = wf;
                        wfSelect.appendChild(opt);
                    });
                }

                wfStatus.textContent = `Found ${workflows.length} workflow(s) for ${user}.`;
            } else {
                wfStatus.textContent =
                    "Error loading workflows: " + res.status;
            }
        } catch (e) {
            console.error("[usgromana] list_workflows error:", e);
            wfStatus.textContent =
                "Error loading workflows. See console.";
        }
    };

    wfPromoteBtn.onclick = async () => {
        const user = getWorkflowUser();
        const workflow = wfSelect?.value;
        if (!user || !workflow) return;
        const delete_source = !!(wfDeleteSource && wfDeleteSource.checked);
        wfStatus.textContent = `Promoting '${workflow}'...`;

        try {
            const res = await api.fetchApi(USER_ENV_API_ENDPOINT, {
                method: "POST",
                body: JSON.stringify({
                    action: "promote_workflow",
                    user,
                    workflow,
                    delete_source,
                }),
            });
            const data = await res.json();
            if (res.status === 200) {
                wfStatus.textContent =
                    data.message || "Workflow promoted to defaults.";
                // If we deleted the source, refresh the list
                if (delete_source) {
                    wfLoadBtn.onclick();
                }
            } else {
                wfStatus.textContent =
                    data.error ||
                    "Error promoting workflow: " + res.status;
            }
        } catch (e) {
            console.error("[usgromana] promote_workflow error:", e);
            wfStatus.textContent =
                "Error promoting workflow. See console.";
        }
    };

    // Initial sync + status
    if (userSelect && wfUserSelect) {
        wfUserSelect.value = userSelect.value;
    }
    if (users.length > 0) {
        refreshStatus();
    }
}

async renderDashboard(container) {
    if (!container) return;
    container.innerHTML = `<div class="usgromana-section"><p>Loading dashboard…</p></div>`;
    const load = async () => {
        try {
            const res = await fetch("/usgromana/api/dashboard", { credentials: "include" });
            const d = res.ok ? await res.json() : {};
            if (!res.ok) {
                container.innerHTML = `<div class="usgromana-section" style="color:#ff6b6b;">${escapeHtml(d.error || "Failed to load")}</div>`;
                return;
            }
            const online = d.online_users || [];
            const top = d.top_users_hour || [];
            const active = d.active_jobs || [];
            const fmtAgo = (s) => {
                const n = Number(s) || 0;
                if (n < 60) return `${n}s ago`;
                if (n < 3600) return `${Math.floor(n / 60)}m ago`;
                return `${Math.floor(n / 3600)}h ago`;
            };
            container.innerHTML = `
                <div class="usgromana-section">
                    <h3>Krish RBAC Dashboard</h3>
                    <div style="display:flex;flex-wrap:wrap;gap:10px;margin:12px 0;">
                        <div style="padding:12px 16px;border-radius:10px;background:rgba(255,255,255,0.05);min-width:120px;">
                            <div style="font-size:11px;opacity:.7;">Online now</div>
                            <div style="font-size:22px;font-weight:700;color:#3dd68c;">${d.online_count || 0}</div>
                        </div>
                        <div style="padding:12px 16px;border-radius:10px;background:rgba(255,255,255,0.05);min-width:120px;">
                            <div style="font-size:11px;opacity:.7;">Queue (all)</div>
                            <div style="font-size:22px;font-weight:700;">${d.queue_length || 0}</div>
                        </div>
                        <div style="padding:12px 16px;border-radius:10px;background:rgba(255,255,255,0.05);min-width:120px;">
                            <div style="font-size:11px;opacity:.7;">Running</div>
                            <div style="font-size:22px;font-weight:700;color:#6eb6ff;">${d.running || 0}</div>
                        </div>
                        <div style="padding:12px 16px;border-radius:10px;background:rgba(255,255,255,0.05);min-width:120px;">
                            <div style="font-size:11px;opacity:.7;">Pending</div>
                            <div style="font-size:22px;font-weight:700;color:#e0c35a;">${d.pending || 0}</div>
                        </div>
                        <div style="padding:12px 16px;border-radius:10px;background:rgba(255,255,255,0.05);min-width:120px;">
                            <div style="font-size:11px;opacity:.7;">Jobs / last hour</div>
                            <div style="font-size:22px;font-weight:700;">${d.jobs_last_hour || 0}</div>
                        </div>
                        <div style="padding:12px 16px;border-radius:10px;background:rgba(255,255,255,0.05);min-width:120px;">
                            <div style="font-size:11px;opacity:.7;">All-time runs</div>
                            <div style="font-size:22px;font-weight:700;">${d.total_runs_all_time || 0}</div>
                        </div>
                    </div>

                    <h4 style="margin-top:8px;">Current online users</h4>
                    <p style="font-size:12px;opacity:.7;margin:0 0 8px;">Users active in the last ~3 minutes (any ComfyUI request).</p>
                    <div style="overflow:auto;max-height:220px;margin-bottom:14px;">
                        <table class="usgromana-table">
                            <thead>
                                <tr>
                                    <th>#</th>
                                    <th>Username</th>
                                    <th>Last seen</th>
                                    <th>Status</th>
                                </tr>
                            </thead>
                            <tbody>
                                ${
                                    online.length
                                        ? online
                                              .map(
                                                  (u, i) => `<tr>
                                    <td>${i + 1}</td>
                                    <td><strong>${escapeHtml(u.username || "")}</strong></td>
                                    <td>${fmtAgo(u.seconds_ago)}</td>
                                    <td style="color:#3dd68c;">● Online</td>
                                </tr>`
                                              )
                                              .join("")
                                        : `<tr><td colspan="4" style="text-align:center;opacity:.65;">No users online right now</td></tr>`
                                }
                            </tbody>
                        </table>
                    </div>

                    <div class="usgromana-row" style="gap:16px;align-items:flex-start;flex-wrap:wrap;">
                        <div style="flex:1;min-width:200px;">
                            <h4>Top users (last hour)</h4>
                            <ul style="margin:6px 0 0 16px;padding:0;">
                                ${top.length ? top.map(u => `<li><b>${escapeHtml(u.username)}</b> — ${u.jobs} job(s)</li>`).join("") : "<li style='opacity:.6;'>No jobs yet</li>"}
                            </ul>
                        </div>
                        <div style="flex:1;min-width:220px;">
                            <h4>Active queue now</h4>
                            <ul style="margin:6px 0 0 16px;padding:0;font-size:12px;">
                                ${active.length ? active.slice(0, 12).map(r => `<li>${r.status === "running" ? "▶" : "…"} <b>${escapeHtml(r.username || "?")}</b> — ${escapeHtml(r.workflow_name || "")}</li>`).join("") : "<li style='opacity:.6;'>Queue empty</li>"}
                            </ul>
                        </div>
                    </div>
                    <button class="usgromana-btn secondary" id="usgromana-dash-refresh" style="margin-top:12px;">Refresh</button>
                    <small id="usgromana-dash-auto" class="usgromana-muted" style="margin-left:10px;">Auto-refresh 10s</small>
                </div>`;
            container.querySelector("#usgromana-dash-refresh").onclick = () => load();
            if (container._dashTimer) clearInterval(container._dashTimer);
            container._dashTimer = setInterval(() => {
                if (container.isConnected) load();
                else clearInterval(container._dashTimer);
            }, 10000);
        } catch (e) {
            console.error(e);
            container.innerHTML = `<div class="usgromana-section" style="color:#ff6b6b;">Dashboard error</div>`;
        }
    };
    await load();
}

async renderAuditLog(container) {
    if (!container) return;
    container.innerHTML = `
        <div class="usgromana-section">
            <h3>Audit Log</h3>
            <p>Who changed roles, reset passwords, cancelled jobs, cleared logs, etc.</p>
            <div class="usgromana-row" style="gap:8px;flex-wrap:wrap;align-items:flex-end;">
                <div style="flex:1;min-width:180px;">
                    <label class="usgromana-field-label">Search</label>
                    <input type="search" id="usgromana-audit-q" class="usgromana-select" style="width:100%;padding:8px;" placeholder="actor, target, action…" />
                </div>
                <button class="usgromana-btn secondary" id="usgromana-audit-refresh">Refresh</button>
                <button class="usgromana-btn secondary" id="usgromana-audit-export">Export CSV</button>
            </div>
            <div style="margin-top:12px;overflow:auto;max-height:420px;">
                <table class="usgromana-table">
                    <thead><tr><th>Time</th><th>Action</th><th>Actor</th><th>Target</th><th>Detail</th><th>IP</th></tr></thead>
                    <tbody id="usgromana-audit-tbody"><tr><td colspan="6">Loading…</td></tr></tbody>
                </table>
            </div>
            <small id="usgromana-audit-meta" class="usgromana-muted"></small>
        </div>`;
    const tbody = container.querySelector("#usgromana-audit-tbody");
    const meta = container.querySelector("#usgromana-audit-meta");
    const qInput = container.querySelector("#usgromana-audit-q");
    const load = async () => {
        const q = new URLSearchParams();
        q.set("limit", "300");
        if (qInput.value.trim()) q.set("q", qInput.value.trim());
        try {
            const res = await fetch(`/usgromana/api/audit-log?${q}`, { credentials: "include" });
            const data = res.ok ? await res.json() : { entries: [] };
            const rows = data.entries || [];
            if (!rows.length) {
                tbody.innerHTML = `<tr><td colspan="6" style="text-align:center;opacity:.7;">No audit entries yet.</td></tr>`;
            } else {
                tbody.innerHTML = rows.map(e => `<tr>
                    <td style="white-space:nowrap;font-size:11px;">${escapeHtml(e.ts || "")}</td>
                    <td><code>${escapeHtml(e.action || "")}</code></td>
                    <td><strong>${escapeHtml(e.actor || "")}</strong></td>
                    <td>${escapeHtml(e.target || "—")}</td>
                    <td style="font-size:12px;">${escapeHtml(e.detail || "")}</td>
                    <td style="font-size:11px;opacity:.8;">${escapeHtml(e.ip || "")}</td>
                </tr>`).join("");
            }
            meta.textContent = `Showing ${rows.length} of ${data.total || rows.length}`;
        } catch (e) {
            tbody.innerHTML = `<tr><td colspan="6" style="color:#ff6b6b;">Failed to load</td></tr>`;
        }
    };
    container.querySelector("#usgromana-audit-refresh").onclick = () => load();
    qInput.onkeydown = (e) => { if (e.key === "Enter") load(); };
    container.querySelector("#usgromana-audit-export").onclick = async () => {
        const q = new URLSearchParams();
        if (qInput.value.trim()) q.set("q", qInput.value.trim());
        const res = await fetch(`/usgromana/api/audit-log/export?${q}`, { credentials: "include" });
        if (!res.ok) { alert("Export failed"); return; }
        const blob = await res.blob();
        const a = document.createElement("a");
        a.href = URL.createObjectURL(blob);
        a.download = "audit_log.csv";
        a.click();
        URL.revokeObjectURL(a.href);
    };
    await load();
}

async renderLiveQueue(container) {
    if (!container) return;
    container.innerHTML = `
        <div class="usgromana-section">
            <h3>Live Queue</h3>
            <p>All users' jobs. Admin/power can <strong>cancel</strong> any pending or running job.</p>
            <button class="usgromana-btn secondary" id="usgromana-lq-refresh">Refresh</button>
            <div style="margin-top:12px;overflow:auto;max-height:480px;">
                <table class="usgromana-table">
                    <thead><tr><th>#</th><th>Status</th><th>User</th><th>Workflow</th><th>Job ID</th><th></th></tr></thead>
                    <tbody id="usgromana-lq-tbody"><tr><td colspan="6">Loading…</td></tr></tbody>
                </table>
            </div>
        </div>`;
    const tbody = container.querySelector("#usgromana-lq-tbody");
    const load = async () => {
        try {
            const res = await fetch("/usgromana/api/workflow-runs/active", { credentials: "include" });
            const data = res.ok ? await res.json() : { active: [] };
            const rows = data.active || [];
            if (!rows.length) {
                tbody.innerHTML = `<tr><td colspan="6" style="text-align:center;opacity:.7;">Queue empty</td></tr>`;
                return;
            }
            tbody.innerHTML = rows.map((r, i) => {
                const jid = r.job_id || r.prompt_id || "";
                return `<tr>
                    <td>${i + 1}</td>
                    <td style="font-weight:600;color:${r.status === "running" ? "#6eb6ff" : "#e0c35a"};">${escapeHtml(r.status || "")}</td>
                    <td><strong>${escapeHtml(r.username || "?")}</strong></td>
                    <td>${escapeHtml(r.workflow_name || "")}</td>
                    <td><code style="font-size:11px;">${escapeHtml(String(jid).slice(0, 12))}${String(jid).length > 12 ? "…" : ""}</code></td>
                    <td><button class="usgromana-btn usgromana-btn-danger btn-cancel-job" data-pid="${escapeHtml(String(jid))}">Cancel</button></td>
                </tr>`;
            }).join("");
            tbody.querySelectorAll(".btn-cancel-job").forEach(btn => {
                btn.onclick = async () => {
                    const pid = btn.dataset.pid;
                    if (!pid || !confirm(`Cancel job ${pid}?`)) return;
                    btn.disabled = true;
                    const res2 = await fetch("/usgromana/api/queue/cancel", {
                        method: "POST",
                        credentials: "include",
                        headers: { "Content-Type": "application/json" },
                        body: JSON.stringify({ prompt_id: pid }),
                    });
                    const d = await res2.json().catch(() => ({}));
                    if (!res2.ok) {
                        alert(d.error || "Cancel failed");
                        btn.disabled = false;
                        return;
                    }
                    await load();
                };
            });
        } catch (e) {
            tbody.innerHTML = `<tr><td colspan="6" style="color:#ff6b6b;">Failed to load queue</td></tr>`;
        }
    };
    container.querySelector("#usgromana-lq-refresh").onclick = () => load();
    await load();
}

async renderRunLog(container, usersList) {
    if (!container) return;

    const canViewAll = !!(
        currentUser?.can_view_all_runs ||
        currentUser?.is_admin ||
        currentUser?.role === "power"
    );
    const isAdmin = !!currentUser?.is_admin;

    const userOptions = (usersList || [])
        .map((u) => {
            const name = u.username || "";
            return `<option value="${escapeHtml(name)}">${escapeHtml(name)}</option>`;
        })
        .join("");

    const scopeNote = canViewAll
        ? "You can see <strong>all users</strong> (admin/power). Regular users only see their own runs."
        : "You only see <strong>your own</strong> runs and outputs. Other users cannot see yours.";

    container.innerHTML = `
        <div class="usgromana-section">
            <h3>${canViewAll ? "Workflow Run Log" : "My Workflow Runs"}</h3>
            <p>
                Each run stores <strong>user</strong>, <strong>job id</strong>, <strong>time</strong>,
                and <strong>workflow name</strong>. ${scopeNote}
            </p>

            <div class="usgromana-row" style="gap:8px; flex-wrap:wrap; align-items:flex-end;">
                <div style="flex:1; min-width:220px;">
                    <label class="usgromana-field-label">Search (job id, user, workflow, status…)</label>
                    <input type="search" id="usgromana-runs-search" class="usgromana-select"
                        placeholder="Paste job id or type a name…"
                        style="width:100%; padding:8px 10px;" />
                </div>
                ${canViewAll ? `
                <div>
                    <label class="usgromana-field-label">Filter user</label>
                    <select id="usgromana-runs-user" class="usgromana-select">
                        <option value="">All users</option>
                        ${userOptions}
                    </select>
                </div>` : `<input type="hidden" id="usgromana-runs-user" value="" />`}
                <div style="display:flex; gap:8px; align-items:flex-end; flex-wrap:wrap;">
                    <button class="usgromana-btn secondary" id="usgromana-runs-refresh">Refresh</button>
                    <button class="usgromana-btn secondary" id="usgromana-runs-export-csv" title="Download CSV (opens in Excel)">Export CSV</button>
                    <button class="usgromana-btn secondary" id="usgromana-runs-export-xlsx" title="Download Excel .xlsx">Export Excel</button>
                    ${isAdmin ? `<button class="usgromana-btn danger" id="usgromana-runs-clear">Clear log</button>` : ""}
                </div>
            </div>

            <div id="usgromana-runs-active" style="margin-top:12px;"></div>
            <div id="usgromana-runs-stats" style="margin-top:12px;"></div>

            <div style="margin-top:14px; overflow:auto; max-height:420px;">
                <table class="usgromana-table" id="usgromana-runs-table">
                    <thead>
                        <tr>
                            <th>Time (UTC)</th>
                            <th>Job ID</th>
                            ${canViewAll ? "<th>User</th>" : ""}
                            <th>Workflow</th>
                            <th>Status</th>
                            <th>Duration</th>
                            <th>Nodes</th>
                        </tr>
                    </thead>
                    <tbody id="usgromana-runs-tbody">
                        <tr><td colspan="7" style="text-align:center;opacity:.7;">Loading…</td></tr>
                    </tbody>
                </table>
            </div>
            <div style="margin-top:6px;">
                <small id="usgromana-runs-meta" class="usgromana-muted"></small>
            </div>
        </div>
    `;

    const userSelect = container.querySelector("#usgromana-runs-user");
    const searchInput = container.querySelector("#usgromana-runs-search");
    const tbody = container.querySelector("#usgromana-runs-tbody");
    const statsEl = container.querySelector("#usgromana-runs-stats");
    const activeEl = container.querySelector("#usgromana-runs-active");
    const metaEl = container.querySelector("#usgromana-runs-meta");
    const refreshBtn = container.querySelector("#usgromana-runs-refresh");
    const clearBtn = container.querySelector("#usgromana-runs-clear");
    const exportCsvBtn = container.querySelector("#usgromana-runs-export-csv");
    const exportXlsxBtn = container.querySelector("#usgromana-runs-export-xlsx");
    const colSpan = canViewAll ? 7 : 6;

    const downloadExport = async (format) => {
        const filterUser = canViewAll ? (userSelect?.value || "").trim() : "";
        const search = (searchInput?.value || "").trim();
        const q = new URLSearchParams();
        q.set("format", format);
        if (filterUser) q.set("user", filterUser);
        if (search) q.set("q", search);
        try {
            const res = await fetch(`${WORKFLOW_RUNS_EXPORT_API}?${q}`, {
                credentials: "include",
            });
            if (!res.ok) {
                const err = await res.json().catch(() => ({}));
                alert(err.error || `Export failed (${res.status})`);
                return;
            }
            const blob = await res.blob();
            const cd = res.headers.get("Content-Disposition") || "";
            const m = /filename="?([^"]+)"?/i.exec(cd);
            const fname =
                m?.[1] ||
                (format === "xlsx"
                    ? "workflow_run_log.xlsx"
                    : "workflow_run_log.csv");
            const a = document.createElement("a");
            a.href = URL.createObjectURL(blob);
            a.download = fname;
            document.body.appendChild(a);
            a.click();
            a.remove();
            setTimeout(() => URL.revokeObjectURL(a.href), 2000);
        } catch (e) {
            console.error("[Usgromana] export failed:", e);
            alert("Export failed. See console.");
        }
    };

    const fmtDuration = (sec) => {
        if (sec == null || sec === "") return "—";
        const n = Number(sec);
        if (Number.isNaN(n)) return "—";
        if (n < 60) return `${n.toFixed(1)}s`;
        const m = Math.floor(n / 60);
        const s = Math.round(n % 60);
        return `${m}m ${s}s`;
    };

    const statusColor = (st) => {
        const s = (st || "").toLowerCase();
        if (s === "completed") return "#3dd68c";
        if (s === "running") return "#6eb6ff";
        if (s === "queued") return "#e0c35a";
        if (s === "error" || s === "failed") return "#ff6b6b";
        return "#ccc";
    };

    const shortJob = (id) => {
        if (!id) return "—";
        const s = String(id);
        if (s.length <= 14) return s;
        return s.slice(0, 8) + "…" + s.slice(-4);
    };

    const load = async () => {
        const filterUser = canViewAll ? (userSelect?.value || "").trim() : "";
        const search = (searchInput?.value || "").trim();
        const q = new URLSearchParams();
        q.set("limit", "300");
        if (filterUser) q.set("user", filterUser);
        if (search) q.set("q", search);

        tbody.innerHTML = `<tr><td colspan="${colSpan}" style="text-align:center;opacity:.7;">Loading…</td></tr>`;

        try {
            const statsQ = new URLSearchParams();
            if (filterUser) statsQ.set("user", filterUser);

            const [runsRes, statsRes, activeRes] = await Promise.all([
                fetch(`${WORKFLOW_RUNS_API}?${q}`, { credentials: "include" }),
                fetch(
                    `${WORKFLOW_RUNS_STATS_API}${statsQ.toString() ? `?${statsQ}` : ""}`,
                    { credentials: "include" }
                ),
                fetch(WORKFLOW_RUNS_ACTIVE_API, { credentials: "include" }),
            ]);

            const runsData = runsRes.ok ? await runsRes.json() : { runs: [], total: 0 };
            const statsData = statsRes.ok ? await statsRes.json() : { users: [], total_runs: 0 };
            const activeData = activeRes.ok ? await activeRes.json() : { active: [] };

            // Active banner
            const active = activeData.active || [];
            if (active.length) {
                activeEl.innerHTML = `
                    <div style="padding:10px 12px;border-radius:8px;background:rgba(60,120,255,0.12);border:1px solid rgba(100,160,255,0.35);">
                        <strong>Active now (${active.length})</strong>
                        <ul style="margin:6px 0 0 18px;padding:0;">
                            ${active
                                .map((r) => {
                                    const jid = r.job_id || r.prompt_id || "";
                                    return `<li>
                                        ${canViewAll ? `<b>${escapeHtml(r.username || "?")}</b> — ` : ""}
                                        ${escapeHtml(r.workflow_name || "Unnamed")}
                                        <span style="opacity:.7">(${escapeHtml(r.status || "")})</span>
                                        ${jid ? `<code style="margin-left:6px;font-size:11px;" title="${escapeHtml(String(jid))}">${escapeHtml(shortJob(jid))}</code>` : ""}
                                    </li>`;
                                })
                                .join("")}
                        </ul>
                    </div>`;
            } else {
                activeEl.innerHTML = `<div style="opacity:.7;font-size:13px;">No jobs currently running or queued.</div>`;
            }

            // Stats cards
            const users = statsData.users || [];
            if (users.length) {
                statsEl.innerHTML = `
                    <div style="display:flex;flex-wrap:wrap;gap:8px;">
                        <div style="padding:10px 12px;border-radius:8px;background:rgba(255,255,255,0.04);border:1px solid rgba(255,255,255,0.08);min-width:120px;">
                            <div style="font-size:11px;opacity:.7;">Total runs</div>
                            <div style="font-size:20px;font-weight:600;">${statsData.total_runs || 0}</div>
                        </div>
                        ${users
                            .slice(0, 8)
                            .map(
                                (u) => `
                            <div style="padding:10px 12px;border-radius:8px;background:rgba(255,255,255,0.04);border:1px solid rgba(255,255,255,0.08);min-width:140px;">
                                <div style="font-size:11px;opacity:.7;">${escapeHtml(u.username || "?")}</div>
                                <div style="font-size:18px;font-weight:600;">${u.total_runs || 0} runs</div>
                                <div style="font-size:11px;opacity:.65;margin-top:2px;">Last: ${escapeHtml(
                                    u.last_run_at || "—"
                                )}</div>
                            </div>`
                            )
                            .join("")}
                    </div>`;
            } else {
                statsEl.innerHTML = `<div style="opacity:.7;font-size:13px;">No run statistics yet. Queue a workflow to start logging.</div>`;
            }

            // History rows
            const runs = runsData.runs || [];
            if (!runs.length) {
                tbody.innerHTML = `<tr><td colspan="${colSpan}" style="text-align:center;opacity:.7;">${
                    search ? "No runs match your search." : "No runs recorded yet."
                }</td></tr>`;
            } else {
                tbody.innerHTML = runs
                    .map((r) => {
                        const st = r.status || "—";
                        const jid = r.job_id || r.prompt_id || "";
                        const jobCell = jid
                            ? `<code title="Click to copy full id" class="usgromana-job-id" data-job="${escapeHtml(String(jid))}" style="cursor:pointer;font-size:11px;word-break:break-all;">${escapeHtml(shortJob(jid))}</code>`
                            : "—";
                        return `<tr>
                            <td style="white-space:nowrap;">${escapeHtml(r.started_at || "—")}</td>
                            <td>${jobCell}</td>
                            ${canViewAll ? `<td><strong>${escapeHtml(r.username || "?")}</strong></td>` : ""}
                            <td>${escapeHtml(r.workflow_name || "Unnamed workflow")}</td>
                            <td style="color:${statusColor(st)};font-weight:600;">${escapeHtml(st)}</td>
                            <td>${fmtDuration(r.duration_sec)}</td>
                            <td>${r.node_count != null ? r.node_count : "—"}</td>
                        </tr>`;
                    })
                    .join("");

                tbody.querySelectorAll(".usgromana-job-id").forEach((el) => {
                    el.onclick = async () => {
                        const full = el.dataset.job || el.textContent;
                        try {
                            await navigator.clipboard.writeText(full);
                            el.style.outline = "1px solid #3dd68c";
                            setTimeout(() => { el.style.outline = ""; }, 800);
                        } catch {
                            window.prompt("Job ID:", full);
                        }
                    };
                });
            }

            const searchNote = search ? ` · search “${search}”` : "";
            metaEl.textContent = `Showing ${runs.length} of ${runsData.total || runs.length} run(s)${searchNote}.`;
        } catch (e) {
            console.error("[Usgromana] run log load failed:", e);
            tbody.innerHTML = `<tr><td colspan="${colSpan}" style="color:#ff6b6b;text-align:center;">Failed to load run log. See console.</td></tr>`;
            metaEl.textContent = "";
        }
    };

    let searchTimer = null;
    refreshBtn.onclick = () => load();
    if (exportCsvBtn) exportCsvBtn.onclick = () => downloadExport("csv");
    if (exportXlsxBtn) exportXlsxBtn.onclick = () => downloadExport("xlsx");
    if (userSelect && userSelect.tagName === "SELECT") {
        userSelect.onchange = () => load();
    }
    if (searchInput) {
        searchInput.oninput = () => {
            clearTimeout(searchTimer);
            searchTimer = setTimeout(() => load(), 280);
        };
        searchInput.onkeydown = (e) => {
            if (e.key === "Enter") {
                e.preventDefault();
                clearTimeout(searchTimer);
                load();
            }
        };
    }
    if (clearBtn) {
        clearBtn.onclick = async () => {
            const filterUser = canViewAll ? (userSelect?.value || "").trim() : "";
            const msg = filterUser
                ? `Clear run history for user "${filterUser}"?`
                : "Clear ALL workflow run history?";
            if (!confirm(msg)) return;
            try {
                const q = filterUser ? `?user=${encodeURIComponent(filterUser)}` : "";
                const res = await fetch(`${WORKFLOW_RUNS_API}${q}`, {
                    method: "DELETE",
                    credentials: "include",
                });
                const data = await res.json().catch(() => ({}));
                if (!res.ok) {
                    alert(data.error || `Clear failed (${res.status})`);
                    return;
                }
                await load();
            } catch (e) {
                console.error("[Usgromana] clear run log failed:", e);
                alert("Failed to clear run log.");
            }
        };
    }

    await load();
}

renderNsfwManagement(container) {
    container.innerHTML = `
        <div class="usgromana-section">
            <h3>NSFW Content Management</h3>
            <p>
                NSFW detection and tagging for files on disk. When a user has SFW enabled
                (Users tab), tagged NSFW images are hidden from the Usgromana Gallery and
                ComfyUI Media → Assets — this is separate from Default UI assets visibility.
            </p>

            <div class="usgromana-row" style="margin-top:16px; gap:8px; flex-wrap:wrap;">
                <button class="usgromana-btn" id="usgromana-nsfw-scan-new">
                    Scan New Images
                </button>
                <button class="usgromana-btn" id="usgromana-nsfw-scan-all">
                    Force Rescan All Images
                </button>
                <button class="usgromana-btn secondary" id="usgromana-nsfw-fix">
                    Fix Incorrect Tags
                </button>
                <button class="usgromana-btn danger" id="usgromana-nsfw-clear">
                    Clear All Tags
                </button>
            </div>

            <div style="margin-top:12px;">
                <label class="usgromana-field-label">Operation Status / Results</label>
                <textarea id="usgromana-nsfw-output" class="usgromana-textarea" readonly style="min-height:120px;"></textarea>
            </div>

            <div style="margin-top:12px; padding:12px; background:rgba(255,255,255,0.05); border-radius:6px;">
                <h4 style="margin:0 0 8px 0; font-size:14px;">About NSFW Scanning</h4>
                <ul style="margin:0; padding-left:20px; font-size:13px; opacity:0.9;">
                    <li><strong>Scan New Images:</strong> Only scans images that don't have NSFW tags yet.</li>
                    <li><strong>Force Rescan All:</strong> Clears all tags and rescans every image (slow, but thorough).</li>
                    <li><strong>Fix Incorrect Tags:</strong> Removes tags from images incorrectly marked as NSFW.</li>
                    <li><strong>Clear All Tags:</strong> Removes all NSFW metadata from images (forces rescan on next access).</li>
                </ul>
            </div>
        </div>
    `;

    const scanNewBtn = container.querySelector("#usgromana-nsfw-scan-new");
    const scanAllBtn = container.querySelector("#usgromana-nsfw-scan-all");
    const fixBtn = container.querySelector("#usgromana-nsfw-fix");
    const clearBtn = container.querySelector("#usgromana-nsfw-clear");
    const output = container.querySelector("#usgromana-nsfw-output");

    async function executeAction(action, params = {}) {
        const btnMap = {
            "scan_all": scanAllBtn,
            "fix_incorrect": fixBtn,
            "clear_all_tags": clearBtn
        };
        const btn = btnMap[action] || scanNewBtn;
        
        const originalText = btn.textContent;
        btn.disabled = true;
        btn.textContent = "Processing...";
        output.value = `Executing ${action}...\n`;

        try {
            const body = { action, ...params };
            if (action === "scan_all") {
                body.force_rescan = false;
            }
            
            const res = await api.fetchApi("/usgromana/api/nsfw-management", {
                method: "POST",
                body: JSON.stringify(body),
            });

            if (res.status === 200) {
                const data = await res.json();
                output.value = data.message || "Operation completed successfully.";
                if (data.stats) {
                    output.value += `\n\nStats:\n`;
                    output.value += `  - Scanned: ${data.stats.scanned || 0}\n`;
                    output.value += `  - NSFW Found: ${data.stats.nsfw_found || 0}\n`;
                    output.value += `  - Errors: ${data.stats.errors || 0}\n`;
                    output.value += `  - Total Images: ${data.stats.total_images || 0}`;
                }
                if (data.fixed_count !== undefined) {
                    output.value += `\n\nFixed ${data.fixed_count} images.`;
                }
            } else {
                const error = await res.json().catch(() => ({ error: `HTTP ${res.status}` }));
                output.value = `Error: ${error.error || `HTTP ${res.status}`}`;
            }
        } catch (e) {
            console.error("[usgromana] NSFW management error:", e);
            output.value = `Error: ${e.message || "See console for details."}`;
        } finally {
            btn.disabled = false;
            btn.textContent = originalText;
        }
    }

    scanNewBtn.onclick = () => executeAction("scan_all", { force_rescan: false });
    scanAllBtn.onclick = () => executeAction("scan_all", { force_rescan: true });
    fixBtn.onclick = () => executeAction("fix_incorrect");
    clearBtn.onclick = () => {
        if (window.confirm("Are you sure you want to clear ALL NSFW tags from all images? This cannot be undone.")) {
            executeAction("clear_all_tags");
        }
    };
}

    async renderUiDefaults(container) {
        container.innerHTML = `<div style="padding:20px; text-align:center; color:#c5c8d3;">Loading UI defaults...</div>`;

        const data = await getData(UI_DEFAULTS_API_ENDPOINT);
        const current = data?.assets_imports_visibility || "user_specific";
        const allowed = data?.allowed_assets_imports_visibility || [
            "user_specific",
            "allow_all",
            "disable_all",
        ];

        const UI_DEFAULT_MODES = [
            { value: "user_specific", header: "USER-SPECIFIC", title: "User-specific (default)" },
            { value: "allow_all", header: "ALLOW ALL", title: "Allow all" },
            { value: "disable_all", header: "DISABLE ALL", title: "Disable for all" },
        ].filter((m) => allowed.includes(m.value));

        const modeCount = UI_DEFAULT_MODES.length;

        const drawSection = (label) =>
            `<tr class="usgromana-section-row"><td colspan="${modeCount + 1}">${label}</td></tr>`;

        const drawModeRow = (label, settingKey, hint) => {
            let row = `<tr><td>${escapeHtml(label)}`;
            if (hint) {
                row += `<br><span style="font-size:11px;opacity:0.75;font-weight:normal;">${escapeHtml(hint)}</span>`;
            }
            row += `</td>`;
            UI_DEFAULT_MODES.forEach((mode) => {
                const checked = current === mode.value;
                row += `<td class="usgromana-check-cell">
                    <input type="radio"
                        class="ui-default-mode-chk"
                        name="usgromana-${settingKey}"
                        data-setting="${settingKey}"
                        data-value="${mode.value}"
                        title="${escapeHtml(mode.title)}"
                        ${checked ? "checked" : ""} />
                </td>`;
            });
            return row + `</tr>`;
        };

        container.innerHTML = `<table class="usgromana-table">
            <thead>
                <tr>
                    <th>Feature / Category</th>
                    ${UI_DEFAULT_MODES.map((m) => `<th class="usgromana-check-cell">${m.header}</th>`).join("")}
                </tr>
            </thead>
            <tbody>
                ${drawSection("Toggled Default UI Functions")}
                ${drawModeRow(
                    "Assets / Imports image visibility",
                    "assets_imports_visibility",
                    "Who may see which files in Media → Assets (user-specific / all / none). Does not enable or disable NSFW filtering — that is per-user SFW on the Users tab and always applies to guests in the gallery."
                )}
            </tbody>
        </table>
        <p id="usgromana-ui-defaults-status" style="margin-top:10px;font-size:12px;color:#9aa0a6;min-height:1.2em;"></p>`;

        const statusEl = container.querySelector("#usgromana-ui-defaults-status");

        const saveSetting = async (settingKey, value) => {
            statusEl.textContent = "Saving...";
            statusEl.style.color = "#9aa0a6";
            try {
                const res = await api.fetchApi(UI_DEFAULTS_API_ENDPOINT, {
                    method: "PUT",
                    body: JSON.stringify({ [settingKey]: value }),
                });
                if (res.status === 200) {
                    statusEl.textContent = "Saved.";
                    statusEl.style.color = "#7dcea0";
                } else {
                    const err = await res.json().catch(() => ({}));
                    statusEl.textContent = err.error || `Error (${res.status})`;
                    statusEl.style.color = "#ff6b6b";
                }
            } catch (e) {
                console.error("[usgromana] UI defaults save error:", e);
                statusEl.textContent = "Save failed. See console.";
                statusEl.style.color = "#ff6b6b";
            }
        };

        container.querySelectorAll(".ui-default-mode-chk").forEach((radio) => {
            radio.onchange = async () => {
                if (!radio.checked) return;
                await saveSetting(radio.dataset.setting, radio.dataset.value);
            };
        });
    }

    renderPerms(container) {
        // --- SCANNER: Find all Settings Categories ---
        const categories = new Set();
        
        // 1. Scan app.extensions
        if (app.extensions) app.extensions.forEach(e => { if(e.name) categories.add(e.name); });
        
        // 2. Scan Settings (Sidebar Buttons)
        if (app.ui.settings.settings) {
            const items = (app.ui.settings.settings instanceof Map) ? Array.from(app.ui.settings.settings.values()) : Object.values(app.ui.settings.settings);
            items.forEach(s => {
                let c = s.category;
                if(Array.isArray(c)) c = c[0];
                if(!c && s.id) c = s.id.split(".")[0];
                if(c) categories.add(c);
            });
        }
        
        // 3. Explicit Whitelist (Ensure these appear even if scanner misses them)
        [
            "User", "Comfy", "LiteGraph", "Appearance", "Extension", 
            "3D", "Mask Editor", "Keybinding", "About",
            "iTools", "Crystools", "rgthree", "Gallery", "Impact"
        ].forEach(c => categories.add(c));
        
        // Clean exclusions
        categories.delete("Usgromana"); 
        categories.delete("Usgromana.Configuration");
        const sortedCats = Array.from(categories).sort();

        // IDs that are already explicitly defined in Sections 1 & 2
        // (and in CSS_BLOCK_MAP). We will NOT auto-generate rows for these.
        const explicitIds = new Set(Object.keys(CSS_BLOCK_MAP));

        // --- DRAW TABLE ---
        let html = `<table class="usgromana-table">
            <thead><tr><th>Feature / Category</th>${GROUPS.map(g => `<th class="usgromana-check-cell">${g.toUpperCase()}</th>`).join("")}</tr></thead>
            <tbody>`;

        const drawRow = (label, id, header=false) => {
            if(header) return `<tr class="usgromana-section-row"><td colspan="${GROUPS.length+1}">${label}</td></tr>`;
            let row = `<tr><td>${label}</td>`;
            GROUPS.forEach(g => {
                let val = groupsConfig[g]?.[id];
                
                // --- CRITICAL DEFAULT LOGIC ---
                // If a setting is new (undefined), should we block it?
                // Guest: Block by default. 
                // Others: Allow by default.
                if (val === undefined) {
                    val = (g !== "guest"); 
                }
                
                // Admin is always true/enabled/visible
                if (g === "admin") val = true;

                row += `<td class="usgromana-check-cell"><input type="checkbox" class="perm-chk" data-group="${g}" data-key="${id}" ${val?"checked":""} ${g==="admin"?"disabled":""}></td>`;
            });
            return row + `</tr>`;
        };

        // Section 1: Backend Security
        html += drawRow("Core API Permissions", null, true);
        html += drawRow("Access ComfyUI-Manager", "can_access_manager");
        html += drawRow("Access General API", "can_access_api");
        html += drawRow("Run Workflows (Execute)", "can_run");
        html += drawRow("Modify Workflows (Save)", "can_modify_workflows");
        html += drawRow("Upload Files", "can_upload");
        html += drawRow("SettingsExtension", "settings_extension");
        html += drawRow("See Restricted Settings", "can_see_restricted_settings");

        // Section 2: Global UI
        html += drawRow("Interface Elements", null, true);
        html += drawRow("Allow Workflow Breadcrumb", "ui_workflow_breadcrumb");
        html += drawRow("Batch Count Widget", "ui_batch_widget");
        html += drawRow("Extra Options (Batch)", "ui_extra_options");
    
        html += drawRow("Sidebar / Floating Menu", null, true);
        html += drawRow("Sidebar Menu: Save", "ui_menu_save");
        html += drawRow("Sidebar Menu: Load", "ui_menu_load");
        html += drawRow("Sidebar Menu: Queue Button", "ui_queue_button");
        html += drawRow("Sidebar: History", "ui_side_history");
        html += drawRow("Sidebar: Queue", "ui_side_queue");
        html += drawRow("Sidebar: Assets", "ui_side_assets");
        html += drawRow("Sidebar: Templates", "ui_side_templates");
        html += drawRow("Sidebar Menu: Browse Templates", "ui_menu_templates");
        html += drawRow("Sidebar Menu: Manage Extensions", "ui_menu_extensions");
        html += drawRow("Sidebar Menu: Manager Button", "ui_menu_manager");

        //  Section 3: Settings Menu Options
        html += drawRow("Settings Menu", null, true);
        html += drawRow("Settings Menu: User", "settings_user");
        html += drawRow("Settings Menu: Usgromana", "settings_usgromanasettings");
        html += drawRow("Settings Menu: Mask Editor", "settings_maskeditor");
        html += drawRow("Settings Menu: Keybinding", "settings_keybinding");
        html += drawRow("Settings Menu: Appearance", "settings_makadiappearance");
        
        // Section 4: Extensions
        html += drawRow("Extension UI & Settings Categories", null, true);
        sortedCats.forEach(c => {
            const id = getSanitizedId(c);

            // If this id is already explicitly handled in Sections 1/2 (or in CSS_BLOCK_MAP),
            // skip it so we don't show duplicate/ghost toggles.
            if (explicitIds.has(id)) return;

            html += drawRow(c, id);
        });


        html += `</tbody></table>`;
        container.innerHTML = html;

        // Bind Checkboxes
        container.querySelectorAll(".perm-chk").forEach(chk => {
            chk.onchange = async () => {
                const g = chk.dataset.group;
                const k = chk.dataset.key;
                const v = chk.checked;
                
                if(!groupsConfig[g]) groupsConfig[g] = {};
                groupsConfig[g][k] = v;
                
                // Save to server
                await api.fetchApi("/usgromana/api/groups", { method: "PUT", body: JSON.stringify({ groups: { [g]: { [k]: v } } }) });
                
                // Apply immediately
                updateEnforcementStyles();
            };
        });
    }
}

// --- 4. ENFORCEMENT ENGINE (CSS INJECTION) ---

async function updateEnforcementStyles() {
    if (!currentUser) currentUser = await getData("/usgromana/api/me");
    if (!currentUser) return;

    if (!groupsConfig || Object.keys(groupsConfig).length === 0) {
        const d = await getData("/usgromana/api/groups");
        groupsConfig = d?.groups || {};
    }

    const role = currentUser.role || "user";

    // 🔧 SAFETY OVERRIDE:
    // On the *UI side*, "guest" is NEVER treated as admin,
    // even if the backend accidentally flagged it.
    if (role === "guest") {
        currentUser.is_admin = false;
    }

    const baseCfg = groupsConfig[role] || {};

    //console.log("[Usgromana] enforcement entry:", {
     //   role,
      //  is_admin: currentUser.is_admin,
       // baseCfgKeys: Object.keys(baseCfg),
        //guestCfgKeys: Object.keys(groupsConfig["guest"] || {})
    //});

    // --- BYPASS ADMIN COMPLETELY ---
    if (currentUser.is_admin) {
        const style = document.getElementById("Usgromana-css-block");
        if (style) style.textContent = "";
        return;
    }

    let css = "";

    // 🔒 HARDENED LOGIC FOR GUEST
    if (role === "guest") {
        const guestCfg = groupsConfig["guest"] || {};

        for (const [key, selectors] of Object.entries(CSS_BLOCK_MAP)) {
            // Always allow usgromana settings menu and logout for guests
            if (key === "settings_usgromanasettings" || key === "settings_Usgromanasettings") {
                continue; // Skip blocking this menu item
            }
            
            const allowed = guestCfg[key] === true; // only explicit true is allowed
            if (!allowed) {
                css +=
                    selectors.join(", ") +
                    " { display: none !important; opacity: 0 !important; pointer-events: none !important; } \n";
            }
        }

        css += `.usgromana-blocked-item { display: none !important; }`;
        // Always show logout button and usgromana menu - never hide them for guests
        css += `#usgromana-settings-logout-btn, [data-usgromana-always-visible="true"] { display: block !important; visibility: visible !important; opacity: 1 !important; }`;
        css += `li[aria-label='usgromana'], li[aria-label='Usgromana'], li.p-listbox-option[aria-label='usgromana'], li.p-listbox-option[aria-label='Usgromana'] { display: block !important; visibility: visible !important; opacity: 1 !important; }`;

        let styleTag = document.getElementById("usgromana-css-block");
        if (!styleTag) {
            styleTag = document.createElement("style");
            styleTag.id = "usgromana-css-block";
            document.head.appendChild(styleTag);
        }
        styleTag.textContent = css;

        enforceSidebar(guestCfg, role);
        enforceMenus(guestCfg, role);
        patchSaveConfirmDialog(guestCfg, role);
        
        // Ensure logout button is always visible for guests
        const logoutBtn = document.getElementById("usgromana-settings-logout-btn");
        if (logoutBtn) {
            logoutBtn.style.display = "block";
            logoutBtn.style.visibility = "visible";
            logoutBtn.style.opacity = "1";
            logoutBtn.classList.remove("usgromana-blocked-item");
        }
        
        return;
    }

    // ... rest of non-guest logic ...
    const cfg = baseCfg;

    console.log("[usgromana] enforcement (non-guest):", {
        role,
        cfgKeys: Object.keys(cfg),
        ui_menu_templates: cfg["ui_menu_templates"],
        ui_menu_extensions: cfg["ui_menu_extensions"]
    });

    // --- A. BLOCK GLOBAL UI ELEMENTS (Fastest) ---
    for (const [key, selectors] of Object.entries(CSS_BLOCK_MAP)) {
        let val = cfg[key];

        // ⚠️ Do NOT touch this default – it works for you:
        // undefined = allowed by default for non-guest
        if (val === undefined) {
            val = true;
        }

        if (val === false) {
            const rule =
                selectors.join(", ") +
                " { display: none !important; opacity: 0 !important; pointer-events: none !important; } \n";
            css += rule;
        }
    }

    css += `.usgromana-blocked-item { display: none !important; }`;
    // Always show logout button - never hide it for any user
    css += `#usgromana-settings-logout-btn, [data-usgromana-always-visible="true"] { display: block !important; visibility: visible !important; opacity: 1 !important; }`;

    // Apply to Head
    let styleTag = document.getElementById("usgromana-css-block");
    if (!styleTag) {
        styleTag = document.createElement("style");
        styleTag.id = "usgromana-css-block";
        document.head.appendChild(styleTag);
    }
    styleTag.textContent = css;

    // Trigger the JS sidebar scanner immediately
    enforceSidebar(cfg, role);
    enforceMenus(cfg, role);
    patchSaveConfirmDialog(cfg, role);
    
    // Ensure logout button is always visible
    const logoutBtn = document.getElementById("usgromana-settings-logout-btn");
    if (logoutBtn) {
        logoutBtn.style.display = "block";
        logoutBtn.style.visibility = "visible";
        logoutBtn.style.opacity = "1";
        logoutBtn.classList.remove("usgromana-blocked-item");
    }
}

// Sidebar Scanner: Runs periodically to hide settings menu buttons by text content
function enforceSidebar(cfg, role) {
    const modal = document.querySelector(".comfy-modal");
    if (!modal) return;

    const items = modal.querySelectorAll(
        "button, .comfy-settings-btn, tr, .pysssss-settings-category"
    );

    items.forEach(el => {
        // Never hide the logout button - it should always be visible
        if (el.id === "usgromana-settings-logout-btn" || 
            el.innerText?.includes("Logout current user") ||
            el.querySelector("#usgromana-settings-logout-btn")) {
            el.classList.remove("usgromana-blocked-item");
            el.style.display = "";
            return;
        }
        
        // Never hide the usgromana menu item - guests need it to logout
        const ariaLabel = el.getAttribute('aria-label');
        if (ariaLabel && (ariaLabel.toLowerCase() === 'usgromana' || ariaLabel === 'Usgromana')) {
            el.classList.remove("usgromana-blocked-item");
            el.style.display = "";
            return;
        }
        
        const txt = (el.innerText || "").trim();
        if (!txt || txt.length > 30 || txt === "Close" || txt === "Back") return;

        const catId = getSanitizedId(txt);

        let val = cfg[catId];
        
        // Always allow usgromana menu for guests
        if (catId === "usgromanasettings" || catId === "Usgromana" || txt.toLowerCase() === "Usgromana") {
            el.classList.remove("usgromana-blocked-item");
            el.style.display = "";
            return;
        }

        // Default logic:
        //  - guest: undefined = BLOCK
        //  - others: undefined = ALLOW
        if (val === undefined) {
            val = (role !== "guest");
        }

        if (val === false) {
            el.classList.add("usgromana-blocked-item");
            el.style.display = "none"; // Inline force
        } else {
            el.classList.remove("usgromana-blocked-item");
            el.style.display = "";
        }
    });
}

// Top menu enforcement: runs on the PrimeVue menubar
function enforceMenus(cfg, role) {
    const shouldBlock = (key) => {
        let val = cfg[key];

        // Same semantics as elsewhere:
        //  - guest: undefined = BLOCK
        //  - others: undefined = ALLOW
        if (val === undefined) {
            val = (role !== "guest");
        }
        return val === false;
    };

    // Block "Browse Templates"
    if (shouldBlock("ui_menu_templates")) {
        document
            .querySelectorAll("li.p-tieredmenu-item[aria-label='Browse Templates']")
            .forEach(el => el.remove());
    }

    // Block "Manage Extensions"
    if (shouldBlock("ui_menu_extensions")) {
        document
            .querySelectorAll("li.p-tieredmenu-item[aria-label='Manage Extensions']")
            .forEach(el => el.remove());
    }

    // Block File → Save / Save As / Export / Export (API)
    if (shouldBlock("ui_menu_save")) {
        document
            .querySelectorAll(
                "li.p-tieredmenu-item[aria-label='Save'], " +
                "li.p-tieredmenu-item[aria-label='Save As'], " +
                "li.p-tieredmenu-item[aria-label='Export'], " +
                "li.p-tieredmenu-item[aria-label='Export (API)']"
            )
            .forEach(el => el.remove());
    }

    // Block File → Open
    if (shouldBlock("ui_menu_load")) {
        document
            .querySelectorAll("li.p-tieredmenu-item[aria-label='Open']")
            .forEach(el => el.remove());
    }
}

function patchSaveConfirmDialog(cfg, role) {
    // Figure out if this role is allowed to save/modify
    let canModify = true;

    if (cfg["can_modify_workflows"] === false) {
        canModify = false;
    } else if (role === "guest") {
        // Guests are blocked unless explicitly allowed
        if (cfg["can_modify_workflows"] !== true && cfg["ui_menu_save"] !== true) {
            canModify = false;
        }
    }

    if (canModify) return;

    // Look for PrimeVue confirm dialogs
    const dialogs = document.querySelectorAll(".p-confirm-dialog, .p-dialog.p-confirm-dialog");
    dialogs.forEach((dlg) => {
        if (!dlg) return;

        // Avoid double-patching the same instance
        if (dlg.dataset.usgromanaPatched === "1") return;

        const titleEl =
            dlg.querySelector(".p-dialog-header .p-dialog-title") ||
            dlg.querySelector(".p-dialog-header") ||
            dlg.querySelector(".p-confirm-dialog-message h2");

        const msgEl =
            dlg.querySelector(".p-confirm-dialog-message") ||
            dlg.querySelector(".p-dialog-content");

        const rawTitle = (titleEl && titleEl.textContent) || "";
        const rawMsg = (msgEl && msgEl.textContent) || "";
        const combined = (rawTitle + " " + rawMsg).toLowerCase();

        // Only touch dialogs that look like "unsaved changes" / "save" prompts
        if (
            !combined ||
            (!combined.includes("save") &&
             !combined.includes("unsaved") &&
             !combined.includes("changes"))
        ) {
            return;
        }

        dlg.dataset.usgromanaPatched = "1";

        // --- Rewrite title / body text ---
        if (titleEl) {
            titleEl.textContent = "Save Changes?";
        }

        if (msgEl) {
            msgEl.innerHTML = `
                <div>
                    <h3 style="margin: 0 0 0.5rem;">Access denied</h3>
                    <p style="margin: 0 0 0.5rem;">
                        Your role is not allowed to save or modify workflows.
                    </p>
                    <p style="margin: 0;">
                        You may close the workflow without saving, or cancel to keep it open.
                    </p>
                </div>
            `;
        }

        // --- Hard-block ONLY the "Save" / "accept" button ---
        let saveBtn =
            dlg.querySelector(".p-confirm-dialog-accept") ||
            dlg.querySelector("button[data-pc-section='acceptbutton']");

        if (!saveBtn) {
            // Fallback: find a button whose label includes "save"
            dlg.querySelectorAll("button").forEach((btn) => {
                const label = (btn.textContent || "").trim().toLowerCase();
                if (!saveBtn && label.includes("save")) {
                    saveBtn = btn;
                }
            });
        }

        if (saveBtn) {
            // Visual hint that it's disabled
            saveBtn.style.opacity = "0.5";

            const block = (ev) => {
                ev.preventDefault();
                ev.stopPropagation();
                ev.stopImmediatePropagation();
                console.warn("[Usgromana] Blocked Save in confirm dialog for this role");
                // Do NOT close the dialog; user can still click "Close without saving" / "Cancel"
            };

            // Catch both click and pointerdown before PrimeVue sees them
            saveBtn.addEventListener("click", block, { capture: true });
            saveBtn.addEventListener("pointerdown", block, { capture: true });
        }

        // We do NOT touch the reject / cancel button:
        // - .p-confirm-dialog-reject
        // - button[data-pc-section='rejectbutton']
        // Those remain fully usable so they can bail out safely.
    });
}

// Workflow Save / Load Interception
function isWorkflowSaveAllowed() {
    if (!currentUser || !groupsConfig) return true; // fail-open for safety until we know
    const role = currentUser.role || "user";
    const cfg = groupsConfig[role] || {};
    // If ui_menu_save is explicitly false → disallow
    if (cfg["ui_menu_save"] === false) return false;
    // Guests default to disallowed if not explicitly true
    if (role === "guest" && cfg["ui_menu_save"] !== true) return false;
    return true;
}

function isWorkflowLoadAllowed() {
    if (!currentUser || !groupsConfig) return true;
    const role = currentUser.role || "user";
    const cfg = groupsConfig[role] || {};
    if (cfg["ui_menu_load"] === false) return false;
    if (role === "guest" && cfg["ui_menu_load"] !== true) return false;
    return true;
}

// Intercept "unsaved workflow" dialogs for roles that cannot save
function guardUnsavedWorkflowDialog() {
    // If the current role IS allowed to save, do nothing
    if (isWorkflowSaveAllowed()) return;

    // PrimeVue dialogs generally use .p-dialog
    const dialogs = document.querySelectorAll(".p-dialog");
    dialogs.forEach(dialog => {
        // Skip if we already patched this dialog
        if (dialog.dataset.usgromanaGuarded === "1") return;

        const text = (dialog.innerText || "").toLowerCase();

        // Heuristic: look for dialogs that are clearly about saving workflows / unsaved changes
        if (
            !text.includes("save") || 
            (!text.includes("workflow") && !text.includes("unsaved"))
        ) {
            return;
        }

        dialog.dataset.usgromanaGuarded = "1";

        // Find the "Save" button in this dialog
        let saveButton = null;
        dialog.querySelectorAll("button").forEach(btn => {
            const label = (btn.innerText || "").trim().toLowerCase();
            if (label === "save" || label === "save workflow") {
                saveButton = btn;
            }
        });

        // If we found a Save button, kill it
        if (saveButton) {
            // You can either disable it or remove it:
            // Option A: Disable + style
            // saveButton.disabled = true;
            // saveButton.classList.add("usgromana-blocked-item");

            // Option B: Just remove it entirely (cleanest UX for guests)
            saveButton.remove();

            console.warn("[Usgromana] Blocked workflow save from unsaved-changes dialog for this role.");
        }

        // Rewrite dialog content with an Access Denied style message
        const body = dialog.querySelector(".p-dialog-content");
        if (body) {
            body.innerHTML = `
                <p><strong>Access denied</strong></p>
                <p>Your role is not allowed to save or modify workflows.</p>
                <p>You may close the workflow without saving, or cancel to keep it open.</p>
            `;
        }
    });
}

// Intercept Ctrl+S / Ctrl+O globally for blocked roles
window.addEventListener("keydown", (ev) => {
    // Normalize
    const key = ev.key.toLowerCase();

    // Ctrl+S (save variants)
    if (ev.ctrlKey && !ev.shiftKey && key === "s") {
        if (!isWorkflowSaveAllowed()) {
            ev.preventDefault();
            ev.stopPropagation();
            console.warn("[Usgromana] Blocked Ctrl+S for this role");
            return;
        }
    }

    // Ctrl+O (open workflow)
    if (ev.ctrlKey && !ev.shiftKey && key === "o") {
        if (!isWorkflowLoadAllowed()) {
            ev.preventDefault();
            ev.stopPropagation();
            console.warn("[Usgromana] Blocked Ctrl+O for this role");
            return;
        }
    }
}, true); // use capture so we beat downstream listeners

// --- 5. INITIALIZATION ---

// Import logout functionality to ensure it loads
import("/usgromana/js/logout.js").catch(err => {
    console.error("[Usgromana] Failed to load logout.js:", err);
    // Fallback: try to load it directly
    const script = document.createElement("script");
    script.src = "/usgromana/js/logout.js";
    script.type = "module";
    document.head.appendChild(script);
});

app.registerExtension({
    name: "Usgromana.Settings",
    async setup() {
        // Expose dialog class globally for floating button and other extensions
        usgromanaDialog.expose();
        
        const style = document.createElement("style");
        style.innerHTML = ADMIN_STYLES;
        document.head.appendChild(style);

        installDenialToastWatcher({
            shouldNotify(url) {
                const u = (url || "").toLowerCase();
                if (u.includes("/api/userdata/workflows")) {
                    try {
                        return !isWorkflowSaveAllowed();
                    } catch {
                        return true;
                    }
                }
                if (
                    u.includes("comfyui-manager") ||
                    u.includes("/api/manager") ||
                    u.includes("/manager")
                ) {
                    return true;
                }
                return false;
            },
        });

        // Immediate Enforcement
        setTimeout(updateEnforcementStyles, 500);

        // Cache DOM queries to avoid repeated lookups
        let cachedModal = null;
        let cachedLogoutBtn = null;
        let cachedMenuItems = null;
        let lastMenuCheck = 0;
        const MENU_CACHE_DURATION = 2000; // Cache menu items for 2 seconds

        // Continuous Enforcement (for late loading extensions & settings modal opening)
        const enforcementInterval = setInterval(() => {
            if (!currentUser || !groupsConfig) return;

            const role = currentUser.role || "user";
            const cfg = groupsConfig[role] || {};

            // Cache modal query - only update if needed
            const now = Date.now();
            if (!cachedModal || !cachedModal.isConnected) {
                cachedModal = document.querySelector(".comfy-modal");
            }

            // Settings modal - only run expensive operations when modal is open
            if (cachedModal) {
                enforceSidebar(cfg, role);
            }

            // Menus & save-confirm popup
            enforceMenus(cfg, role);
            patchSaveConfirmDialog(cfg, role);
            
            // Ensure logout button is always visible for all users - cache the query
            if (!cachedLogoutBtn || !cachedLogoutBtn.isConnected) {
                cachedLogoutBtn = document.getElementById("usgromana-settings-logout-btn");
            }
            if (cachedLogoutBtn) {
                cachedLogoutBtn.style.display = "block";
                cachedLogoutBtn.style.visibility = "visible";
                cachedLogoutBtn.style.opacity = "1";
                cachedLogoutBtn.classList.remove("usgromana-blocked-item");
            }
            
            // Ensure usgromana menu item is always visible - cache query results
            if (now - lastMenuCheck > MENU_CACHE_DURATION || !cachedMenuItems || cachedMenuItems.length === 0) {
                cachedMenuItems = document.querySelectorAll('li[aria-label="Usgromana"], li.p-listbox-option[aria-label="Usgromana"]');
                lastMenuCheck = now;
            }
            cachedMenuItems.forEach(item => {
                if (item.isConnected) {
                    item.style.display = "block";
                    item.style.visibility = "visible";
                    item.style.opacity = "1";
                    item.classList.remove("usgromana-blocked-item");
                }
            });

            // If CSS block was nuked, rebuild it
            if (!document.getElementById("usgromana-css-block")) {
                updateEnforcementStyles();
            }
        }, 1000);

        // Store interval ID for potential cleanup (though this extension typically lives for the page lifetime)
        window._usgromanaEnforcementInterval = enforcementInterval;

        // Register "Manage Usgromana" Button in Settings
app.ui.settings.addSetting({
    id: "Usgromana.Configuration",
    name: "Usgromana",
    type: () => {
        const wrapper = document.createElement("div");
        wrapper.style.display = "flex";
        wrapper.style.flexDirection = "column";
        wrapper.style.gap = "6px";

        // Logout button (above) - ALWAYS visible for all users including guests
        const logoutBtn = document.createElement("button");
        logoutBtn.innerText = "Logout current user";
        logoutBtn.className = "usgromana-launch-btn";
        logoutBtn.style.background = "#7a2525";
        logoutBtn.style.borderColor = "#aa3a3a";
        logoutBtn.id = "usgromana-settings-logout-btn";
        // Ensure logout button is never hidden by enforcement
        logoutBtn.setAttribute('data-usgromana-always-visible', 'true');
        logoutBtn.style.display = "block"; // Force display
        
        logoutBtn.onclick = () => {
            // Hard redirect so cookies + state reset properly
            window.location.href = "/logout";
        };

        // Main management button
        const btn = document.createElement("button");
        btn.innerText = "Manage Usgromana Permissions";
        btn.className = "usgromana-launch-btn";
        btn.onclick = () => new usgromanaDialog().show();

        wrapper.appendChild(logoutBtn);
        wrapper.appendChild(btn);

        // Layout helper for settings table
        setTimeout(() => {
            const td = wrapper.closest("td");
            if (td) {
                td.colSpan = 2;
                if (td.previousElementSibling) {
                    td.previousElementSibling.style.display = "none";
                }
            }
        }, 100);
        
        return wrapper;
    }
});

    }
});
