(function () {
  if (!document.body?.hasAttribute("data-marvis-relay-view")) return;

  const root = document.documentElement;
  const viewport = window.visualViewport;

  function updateViewportVars() {
    const layoutHeight = window.innerHeight || root.clientHeight || 0;
    const visualHeight = viewport?.height || layoutHeight;
    const offsetTop = viewport?.offsetTop || 0;
    const keyboardOffset = Math.max(0, layoutHeight - visualHeight - offsetTop);

    document.body.style.setProperty("--marvis-visual-viewport-height", `${Math.round(visualHeight)}px`);
    document.body.style.setProperty("--marvis-keyboard-offset", `${Math.round(keyboardOffset)}px`);
  }

  function keepComposerVisible(event) {
    const target = event.target;
    if (!(target instanceof HTMLElement)) return;
    if (!target.closest(".marvis-relay-composer")) return;
    window.setTimeout(() => {
      target.scrollIntoView({ block: "nearest", inline: "nearest" });
      updateViewportVars();
    }, 80);
  }

  updateViewportVars();
  viewport?.addEventListener("resize", updateViewportVars);
  viewport?.addEventListener("scroll", updateViewportVars);
  window.addEventListener("resize", updateViewportVars);
  document.addEventListener("focusin", keepComposerVisible);
  document.addEventListener("visibilitychange", updateViewportVars);
})();
