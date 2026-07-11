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

  function createMutationClient(options) {
    const config = options || {};
    const requestFetch = config.fetch || global.fetch.bind(global);
    const prefix = config.prefix || "mutation";
    const keyAttribute = config.keyAttribute || "wlcodexMutationKey";

    async function request(url, body, button, requestOptions) {
      const currentOptions = requestOptions || {};
      const existingKey = button && button.dataset ? button.dataset[keyAttribute] : "";
      const key = existingKey || mutationKey(prefix);
      if (button && button.dataset) button.dataset[keyAttribute] = key;
      if (button) {
        button.disabled = true;
        button.setAttribute("aria-busy", "true");
      }
      try {
        const response = await requestFetch(url, withMutationKey({
          ...currentOptions,
          method: currentOptions.method || "POST",
          headers: {
            "Content-Type": "application/json",
            ...(currentOptions.headers || {}),
          },
          body: JSON.stringify(body || {}),
        }, key));
        const payload = await response.json().catch(() => ({}));
        if (!response.ok) {
          throw new Error(payload.error || payload.message || `请求失败 (${response.status})`);
        }
        if (button && button.dataset) delete button.dataset[keyAttribute];
        return payload;
      } finally {
        if (button) {
          button.disabled = false;
          button.removeAttribute("aria-busy");
        }
      }
    }

    return {request};
  }

  function createSseConnection(options) {
    const config = options || {};
    const EventSourceClass = config.EventSource || global.EventSource;
    const reconnectInitialDelay = Number(config.initialReconnectDelay || 500);
    const reconnectMaxDelay = Number(config.maxReconnectDelay || 5000);
    const bindings = [];
    let source = null;
    let reconnectTimer = null;
    let reconnectDelay = reconnectInitialDelay;

    function closeSource() {
      if (!source) return;
      source.close();
      source = null;
    }
    function clearReconnect() {
      if (!reconnectTimer) return;
      global.clearTimeout(reconnectTimer);
      reconnectTimer = null;
    }
    function addEventListener(name, handler) {
      bindings.push([name, handler]);
      if (source) source.addEventListener(name, handler);
    }
    function close() {
      clearReconnect();
      closeSource();
    }
    function scheduleReconnect() {
      if (document.visibilityState === "hidden" || reconnectTimer) return;
      closeSource();
      if (typeof config.onReconnectScheduled === "function") config.onReconnectScheduled();
      reconnectTimer = global.setTimeout(() => {
        reconnectTimer = null;
        connect();
        reconnectDelay = Math.min(reconnectDelay * 2, reconnectMaxDelay);
      }, reconnectDelay);
    }
    function connect() {
      if (document.visibilityState === "hidden") return null;
      close();
      if (typeof EventSourceClass !== "function") {
        if (typeof config.onUnavailable === "function") config.onUnavailable();
        return null;
      }
      const next = new EventSourceClass(config.url());
      source = next;
      bindings.forEach(([name, handler]) => next.addEventListener(name, handler));
      next.onopen = () => {
        if (source !== next) return;
        reconnectDelay = reconnectInitialDelay;
        if (typeof config.onOpen === "function") config.onOpen();
      };
      next.onmessage = (event) => {
        if (source !== next || typeof config.onMessage !== "function") return;
        config.onMessage(event);
      };
      next.onerror = () => {
        if (source !== next) return;
        if (typeof config.onError === "function") config.onError();
        scheduleReconnect();
      };
      return next;
    }
    return {addEventListener, close, connect, scheduleReconnect, get source() { return source; }};
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
    const closeDelay = Math.max(0, Number(config.closeDelay || 0));
    let restoreFocus = null;
    let closeTimer = null;
    function inertTargets() {
      if (typeof config.inertTargets === "function") return config.inertTargets();
      return appRoot && appRoot !== dialog ? [appRoot] : [];
    }
    function setBackgroundInert(isOpen) {
      Array.from(inertTargets() || []).forEach((node) => {
        if (!node || node === dialog) return;
        if ("inert" in node) node.inert = isOpen;
        node.setAttribute("aria-hidden", isOpen ? "true" : "false");
      });
    }
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
      if (closeTimer) {
        global.clearTimeout(closeTimer);
        closeTimer = null;
      }
      restoreFocus = trigger || document.activeElement;
      dialog.hidden = false;
      dialog.setAttribute("aria-modal", "true");
      setBackgroundInert(true);
      if (typeof config.onOpen === "function") config.onOpen();
      const first = focusable()[0];
      if (first) first.focus();
    }
    function close() {
      if (dialog.hidden || closeTimer) return;
      if (typeof config.onBeforeClose === "function") config.onBeforeClose();
      setBackgroundInert(false);
      const finalize = () => {
        closeTimer = null;
        dialog.hidden = true;
        if (typeof config.onClosed === "function") config.onClosed();
        if (restoreFocus && typeof restoreFocus.focus === "function") restoreFocus.focus();
        restoreFocus = null;
      };
      if (closeDelay) {
        closeTimer = global.setTimeout(finalize, closeDelay);
        return;
      }
      finalize();
    }
    dialog.addEventListener("keydown", keydown);
    return {open, close, destroy() { dialog.removeEventListener("keydown", keydown); }};
  }

  global.WLCodexSurfaceRuntime = Object.freeze({
    mutationKey,
    withMutationKey,
    createMutationClient,
    createSseConnection,
    createConnectionState,
    createConditionalScroller,
    createDialog,
  });
})(window);
