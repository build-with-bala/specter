"""Anti-bot evasion scripts injected via CDP.

Each evasion is a JavaScript snippet registered with
``Page.addScriptToEvaluateOnNewDocument`` so it runs before any
page code, including bot-detection libraries like DataDome,
Cloudflare Turnstile, PerimeterX, and Akamai Bot Manager.

Covered evasions:
  * ``navigator.webdriver`` -- set to ``false`` / removed.
  * ``chrome.runtime`` -- mock the extension API object.
  * ``Permissions API`` -- override ``navigator.permissions.query``.
  * ``navigator.plugins`` -- populate with realistic entries.
  * ``window.chrome`` -- full mock of the Chrome app object.
  * ``WebDriver flags`` -- remove ``__webdriver_evaluate`` etc.

Usage::

    from specter.stealth.evasions import apply_evasions
    await apply_evasions(cdp_session)
"""

from __future__ import annotations

import logging
from typing import Any

from specter.core.cdp_client import CDPSession

logger = logging.getLogger(__name__)


# ── individual evasion scripts ────────────────────────────────────

EVASION_WEBDRIVER = """
// --- navigator.webdriver = false ---
Object.defineProperty(navigator, 'webdriver', {
    get: () => false,
    configurable: true,
});
// Delete legacy automation indicators.
delete navigator.__webdriver_evaluate;
delete navigator.__webdriver_unwrap;
delete navigator.__selenium_evaluate;
delete navigator.__fxdriver_evaluate;
delete navigator.__driver_evaluate;
delete navigator.__webdriver_script_fn;
delete navigator.__lastWatirAlert;
delete navigator.__lastWatirConfirm;
delete navigator.__lastWatirPrompt;
delete document.__webdriver_evaluate;
delete document.__selenium_evaluate;
delete document.__fxdriver_evaluate;
delete document.__driver_evaluate;
"""

EVASION_CHROME_RUNTIME = """
// --- chrome.runtime mock ---
if (!window.chrome) window.chrome = {};
if (!window.chrome.runtime) {
    window.chrome.runtime = {
        id: undefined,
        connect: function() { return { onMessage: { addListener: function(){} }, postMessage: function(){} }; },
        sendMessage: function() {},
        onConnect: { addListener: function(){} },
        onMessage: { addListener: function(){} },
        getManifest: function() { return {}; },
        getURL: function(path) { return ''; },
        PlatformOs: { MAC: 'mac', WIN: 'win', ANDROID: 'android', CROS: 'cros', LINUX: 'linux', OPENBSD: 'openbsd' },
        PlatformArch: { ARM: 'arm', X86_32: 'x86-32', X86_64: 'x86-64', MIPS: 'mips', MIPS64: 'mips64' },
        PlatformNaclArch: { ARM: 'arm', X86_32: 'x86-32', X86_64: 'x86-64', MIPS: 'mips', MIPS64: 'mips64' },
        RequestUpdateCheckStatus: { THROTTLED: 'throttled', NO_UPDATE: 'no_update', UPDATE_AVAILABLE: 'update_available' },
        OnInstalledReason: { INSTALL: 'install', UPDATE: 'update', CHROME_UPDATE: 'chrome_update', SHARED_MODULE_UPDATE: 'shared_module_update' },
        OnRestartRequiredReason: { APP_UPDATE: 'app_update', OS_UPDATE: 'os_update', PERIODIC: 'periodic' },
    };
}
"""

EVASION_PERMISSIONS = """
// --- Permissions API override ---
const _origQuery = navigator.permissions.query.bind(navigator.permissions);
navigator.permissions.query = function(parameters) {
    if (parameters.name === 'notifications') {
        return Promise.resolve({ state: Notification.permission, onchange: null });
    }
    return _origQuery(parameters).catch(() =>
        Promise.resolve({ state: 'prompt', onchange: null })
    );
};
// Prevent toString detection.
navigator.permissions.query.toString = () => 'function query() { [native code] }';
"""

EVASION_PLUGINS = """
// --- navigator.plugins population ---
(function() {
    const pluginData = [
        { name: 'Chrome PDF Plugin', filename: 'internal-pdf-viewer', description: 'Portable Document Format',
          mimeTypes: [{ type: 'application/x-google-chrome-pdf', suffixes: 'pdf', description: 'Portable Document Format' }] },
        { name: 'Chrome PDF Viewer', filename: 'mhjfbmdgcfjbbpaeojofohoefgiehjai', description: '',
          mimeTypes: [{ type: 'application/pdf', suffixes: 'pdf', description: '' }] },
        { name: 'Native Client', filename: 'internal-nacl-plugin', description: '',
          mimeTypes: [
              { type: 'application/x-nacl', suffixes: '', description: 'Native Client Executable' },
              { type: 'application/x-pnacl', suffixes: '', description: 'Portable Native Client Executable' },
          ] },
    ];

    function makeMimeType(mt, plugin) {
        const obj = Object.create(MimeType.prototype);
        Object.defineProperties(obj, {
            type:        { get: () => mt.type },
            suffixes:    { get: () => mt.suffixes },
            description: { get: () => mt.description },
            enabledPlugin: { get: () => plugin },
        });
        return obj;
    }

    function makePlugin(pd) {
        const obj = Object.create(Plugin.prototype);
        const mimes = pd.mimeTypes.map(mt => makeMimeType(mt, obj));
        Object.defineProperties(obj, {
            name:        { get: () => pd.name },
            filename:    { get: () => pd.filename },
            description: { get: () => pd.description },
            length:      { get: () => mimes.length },
        });
        mimes.forEach((m, i) => {
            Object.defineProperty(obj, i, { get: () => m });
            Object.defineProperty(obj, m.type, { get: () => m });
        });
        obj.item = idx => mimes[idx] || null;
        obj.namedItem = name => mimes.find(m => m.type === name) || null;
        Object.defineProperty(obj.item, 'toString', { value: () => 'function item() { [native code] }' });
        Object.defineProperty(obj.namedItem, 'toString', { value: () => 'function namedItem() { [native code] }' });
        return obj;
    }

    const plugins = pluginData.map(makePlugin);
    const pluginArray = Object.create(PluginArray.prototype);
    Object.defineProperty(pluginArray, 'length', { get: () => plugins.length });
    plugins.forEach((p, i) => {
        Object.defineProperty(pluginArray, i, { get: () => p });
        Object.defineProperty(pluginArray, p.name, { get: () => p });
    });
    pluginArray.item = idx => plugins[idx] || null;
    pluginArray.namedItem = name => plugins.find(p => p.name === name) || null;
    pluginArray.refresh = () => {};

    Object.defineProperty(navigator, 'plugins', { get: () => pluginArray });
})();
"""

EVASION_WINDOW_CHROME = """
// --- window.chrome mock ---
if (!window.chrome) window.chrome = {};
window.chrome.app = {
    isInstalled: false,
    InstallState: { DISABLED: 'disabled', INSTALLED: 'installed', NOT_INSTALLED: 'not_installed' },
    RunningState: { CANNOT_RUN: 'cannot_run', READY_TO_RUN: 'ready_to_run', RUNNING: 'running' },
    getDetails: function() { return null; },
    getIsInstalled: function() { return false; },
    installState: function(cb) { if (cb) cb('not_installed'); },
    runningState: function() { return 'cannot_run'; },
};
window.chrome.csi = function() {
    return {
        startE: Date.now(),
        onloadT: Date.now(),
        pageT: Date.now() - performance.timing.navigationStart,
        tran: 15,
    };
};
window.chrome.loadTimes = function() {
    return {
        commitLoadTime: Date.now() / 1000,
        connectionInfo: 'h2',
        finishDocumentLoadTime: Date.now() / 1000,
        finishLoadTime: Date.now() / 1000,
        firstPaintAfterLoadTime: 0,
        firstPaintTime: Date.now() / 1000,
        navigationType: 'Other',
        npnNegotiatedProtocol: 'h2',
        requestTime: Date.now() / 1000,
        startLoadTime: Date.now() / 1000,
        wasAlternateProtocolAvailable: false,
        wasFetchedViaSpdy: true,
        wasNpnNegotiated: true,
    };
};
"""

EVASION_IFRAME_CONTENT_WINDOW = """
// --- iframe.contentWindow protection ---
// Some bot detectors create a hidden iframe and check if its
// contentWindow has navigator.webdriver set.
const _origAttach = Element.prototype.attachShadow;
if (_origAttach) {
    Element.prototype.attachShadow = function() {
        return _origAttach.apply(this, arguments);
    };
    Element.prototype.attachShadow.toString = () => 'function attachShadow() { [native code] }';
}

// Proxy HTMLIFrameElement.contentWindow so the webdriver flag
// is false inside iframes too.
try {
    const _origContentWindow = Object.getOwnPropertyDescriptor(HTMLIFrameElement.prototype, 'contentWindow');
    if (_origContentWindow && _origContentWindow.get) {
        Object.defineProperty(HTMLIFrameElement.prototype, 'contentWindow', {
            get: function() {
                const win = _origContentWindow.get.call(this);
                if (win) {
                    try {
                        Object.defineProperty(win.navigator, 'webdriver', {
                            get: () => false, configurable: true,
                        });
                    } catch(e) {}
                }
                return win;
            },
        });
    }
} catch(e) {}
"""

EVASION_CODECS = """
// --- Media codec detection fix ---
// Headless Chrome often reports different codec support.
const _origCanPlay = HTMLMediaElement.prototype.canPlayType;
HTMLMediaElement.prototype.canPlayType = function(type) {
    if (type === 'video/mp4; codecs="avc1.42E01E"') return 'probably';
    if (type === 'video/webm; codecs="vp8, vorbis"') return 'probably';
    if (type === 'audio/mpeg') return 'probably';
    return _origCanPlay.call(this, type);
};
HTMLMediaElement.prototype.canPlayType.toString = () => 'function canPlayType() { [native code] }';
"""

EVASION_LANGUAGES = """
// --- navigator.languages fix ---
Object.defineProperty(navigator, 'languages', {
    get: () => ['en-US', 'en'],
    configurable: true,
});
Object.defineProperty(navigator, 'language', {
    get: () => 'en-US',
    configurable: true,
});
"""

# ── collected evasion list ────────────────────────────────────────

ALL_EVASIONS: dict[str, str] = {
    "webdriver":          EVASION_WEBDRIVER,
    "chrome_runtime":     EVASION_CHROME_RUNTIME,
    "permissions":        EVASION_PERMISSIONS,
    "plugins":            EVASION_PLUGINS,
    "window_chrome":      EVASION_WINDOW_CHROME,
    "iframe":             EVASION_IFRAME_CONTENT_WINDOW,
    "codecs":             EVASION_CODECS,
    "languages":          EVASION_LANGUAGES,
}


# ── public API ────────────────────────────────────────────────────

async def apply_evasions(
    session: CDPSession,
    *,
    only: list[str] | None = None,
    exclude: list[str] | None = None,
) -> list[str]:
    """Inject anti-detection scripts into a CDP session.

    Scripts are registered with ``Page.addScriptToEvaluateOnNewDocument``
    so they execute before any page JavaScript on every navigation.

    Parameters
    ----------
    session:
        Active CDP session (must have Page domain enabled).
    only:
        If given, apply *only* these evasion keys.
    exclude:
        If given, skip these evasion keys.

    Returns
    -------
    List of evasion keys that were applied.
    """
    exclude_set = set(exclude or [])
    applied: list[str] = []

    for key, script in ALL_EVASIONS.items():
        if only and key not in only:
            continue
        if key in exclude_set:
            continue
        await session.send("Page.addScriptToEvaluateOnNewDocument", {
            "source": script,
        })
        applied.append(key)
        logger.debug("Applied evasion: %s", key)

    logger.info("Applied %d evasion scripts: %s", len(applied), ", ".join(applied))
    return applied


async def remove_all_evasions(session: CDPSession) -> None:
    """Remove all injected scripts.

    Note: this removes *all* scripts added via
    ``addScriptToEvaluateOnNewDocument``, not just evasions.
    """
    await session.send("Page.removeScriptToEvaluateOnNewDocument")
    logger.info("Removed all injected scripts")


async def test_evasions(session: CDPSession) -> dict[str, bool]:
    """Run quick self-tests to verify evasions are working.

    Returns a dict mapping test name to pass/fail.
    """
    tests: dict[str, str] = {
        "webdriver_false":    "navigator.webdriver === false",
        "chrome_exists":      "!!window.chrome",
        "chrome_app_exists":  "!!window.chrome.app",
        "chrome_runtime":     "!!window.chrome.runtime",
        "plugins_populated":  "navigator.plugins.length > 0",
        "languages_set":      "navigator.languages.length > 0",
        "permissions_ok":     "typeof navigator.permissions.query === 'function'",
    }
    results: dict[str, bool] = {}
    for name, expr in tests.items():
        try:
            r = await session.send("Runtime.evaluate", {
                "expression": expr,
                "returnByValue": True,
            })
            results[name] = bool(r.get("result", {}).get("value", False))
        except Exception:
            results[name] = False
    return results
