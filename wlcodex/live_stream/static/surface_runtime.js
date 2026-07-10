/* Shared browser primitives for the Native and Relay product surfaces.
 *
 * This deliberately stays framework-free: the server currently renders the
 * surfaces as HTML templates, but mutation identity, connection feedback,
 * conditional scrolling and accessible dialog behavior must not drift across
 * those templates.
 */
(function attachSurfaceRuntime(global) {
  "use strict";

  function mutationKey(prefix) {
    const safePrefix = String(prefix || "mutation").replace(/[^a-z0-9_.-]/gi, "-");
    if (global.crypto && typeof global.crypto.randomUUID === "function") {
      return safePrefix + "-" + global.crypto.randomUUID();
    }
    return safePrefix + "-" + Date.now().toString(36) + "-" + Math.random().toString(36).slice(2);
  }

  function withMutationKey(options, key) {
    const source = options || {};
    return {
      ...source,
      headers: {
        ...(source.headers || {}),
        "Idempotency-Key": key || mutationKey(),
      },
    };
  }

  function createConnectionState(onChange) {
    let value = "connecting";
    function set(next, detail) {
      value = String(next || "connecting");
      if (typeof onChange === "function") onChange(value, detail || "");
      return value;
    }
    return {get value() { return value; }, set};
  }

  function createConditionalScroller(options) {
    const config = options || {};
    const container = config.container || global;
    const threshold = Number(config.threshold || 72);
    const notice = config.notice || null;
    const getMetrics = typeof config.getMetrics === "function"
      ? config.getMetrics
      : () => ({
        scrollTop: container === global ? global.scrollY : container.scrollTop,
        clientHeight: container === global ? global.innerHeight : container.clientHeight,
        scrollHeight: container === global ? document.documentElement.scrollHeight : container.scrollHeight,
      });
    let pinned = true;

    function nearBottom() {
      const metrics = getMetrics();
      return metrics.scrollHeight - (metrics.scrollTop + metrics.clientHeight) <= threshold;
    }
    function sync() {
      pinned = nearBottom();
      if (pinned && notice) notice.hidden = true;
      return pinned;
    }
    function scrollToBottom(force) {
      if (!force && !pinned) {
        if (notice) notice.hidden = false;
        return false;
      }
      if (container === global) {
        global.scrollTo({top: document.documentElement.scrollHeight, behavior: force ? "smooth" : "auto"});
      } else {
        container.scrollTop = container.scrollHeight;
      }
      if (notice) notice.hidden = true;
      pinned = true;
      return true;
    }
    if (container && typeof container.addEventListener === "function") {
      container.addEventListener("scroll", sync, {passive: true});
    }
    if (notice) notice.addEventListener("click", () => scrollToBottom(true));
    return {nearBottom, sync, scrollToBottom, get pinned() { return pinned; }};
  }

  function createDialog(dialog, options) {
    if (!dialog) return {open() {}, close() {}, destroy() {}};
    const config = options || {};
    const appRoot = config.appRoot || document.querySelector("main") || document.body;
    let restoreFocus = null;
    function focusable() {
      return Array.from(dialog.querySelectorAll(
        'button:not([disabled]), [href], input:not([disabled]), select:not([disabled]), textarea:not([disabled]), [tabindex]:not([tabindex="-1"])'
      )).filter((node) => !node.hidden);
    }
    function keydown(event) {
      if (event.key === "Escape") {
        event.preventDefault();
        close();
        return;
      }
      if (event.key !== "Tab") return;
      const nodes = focusable();
      if (!nodes.length) return;
      const first = nodes[0];
      const last = nodes[nodes.length - 1];
      if (event.shiftKey && document.activeElement === first) {
        event.preventDefault();
        last.focus();
      } else if (!event.shiftKey && document.activeElement === last) {
        event.preventDefault();
        first.focus();
      }
    }
    function open(trigger) {
      restoreFocus = trigger || document.activeElement;
      dialog.hidden = false;
      dialog.setAttribute("aria-modal", "true");
      if (appRoot && appRoot !== dialog && "inert" in appRoot) appRoot.inert = true;
      const first = focusable()[0];
      if (first) first.focus();
    }
    function close() {
      dialog.hidden = true;
      if (appRoot && "inert" in appRoot) appRoot.inert = false;
      if (restoreFocus && typeof restoreFocus.focus === "function") restoreFocus.focus();
      restoreFocus = null;
    }
    dialog.addEventListener("keydown", keydown);
    return {open, close, destroy() { dialog.removeEventListener("keydown", keydown); }};
  }

  global.WLCodexSurfaceRuntime = Object.freeze({
    mutationKey,
    withMutationKey,
    createConnectionState,
    createConditionalScroller,
    createDialog,
  });
})(window);
