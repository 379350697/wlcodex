"use strict";

// Honour WLCODEX_PLAYWRIGHT_NODE_MODULES even when the invoking directory has
// another node_modules/playwright.  Node's normal resolver intentionally
// prefers the latter, so NODE_PATH alone is insufficient for this wrapper.
const Module = require("module");
const path = require("path");

const selected = String(process.env.WLCODEX_RESOLVED_PLAYWRIGHT_NODE_MODULES || "").trim();
if (selected) {
  const originalResolveFilename = Module._resolveFilename;
  Module._resolveFilename = function resolvePlaywrightFromSelectedInstall(
    request,
    parent,
    isMain,
    options,
  ) {
    if (request === "playwright" || request.startsWith("playwright/")) {
      return originalResolveFilename(
        path.join(selected, request),
        parent,
        isMain,
        options,
      );
    }
    return originalResolveFilename(request, parent, isMain, options);
  };
}
