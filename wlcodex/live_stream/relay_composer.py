"""Pure Relay composer templates and attachment client behaviour."""

from __future__ import annotations

from html import escape
from pathlib import Path

from wlcodex.live_stream.relay_navigation import relay_workspace_href

def _marvis_relay_task_composer(
    *,
    token_suffix: str,
    selected_workspace: str,
    access_token: str = "",
    placeholder: str = "请输入任务",
) -> str:
    workspace_dock = _marvis_relay_workspace_dock(
        selected_workspace,
        access_token=access_token,
    )
    return f"""
    {workspace_dock}
    <form class="marvis-relay-composer" data-marvis-task-composer action="/api/relay/tasks{token_suffix}">
      <div class="marvis-relay-mode-strip" aria-label="接力模式">
        <label><input type="radio" name="execution_mode" value="standard" checked><span>标准执行</span></label>
        <label><input type="radio" name="execution_mode" value="plan_first"><span>先计划</span></label>
        <label><input type="radio" name="execution_mode" value="goal"><span>目标验收</span></label>
      </div>
      <p class="marvis-relay-execution-contract" data-relay-execution-contract>标准执行：系统根据任务自动选择角色与子代理。</p>
      <button class="marvis-relay-plus" type="button" aria-label="添加" data-marvis-attach-open>+</button>
      <input name="title" autocomplete="off" placeholder="{escape(placeholder)}">
      <input type="hidden" name="prompt" value="">
      <div class="marvis-relay-goal-contract" data-relay-goal-contract hidden>
        <label>目标<input name="execution_goal" autocomplete="off" placeholder="可验证的业务目标"></label>
        <label>验收条件<textarea name="acceptance_criteria" rows="3" placeholder="每行一条：实现证据、独立测试或审计证据"></textarea></label>
      </div>
      <input type="hidden" name="workspace" value="{escape(selected_workspace)}">
      <button class="marvis-relay-submit" type="submit" aria-label="发送任务" data-marvis-submit>
        <span class="marvis-relay-submit-arrow" aria-hidden="true">↑</span>
      </button>
      <div class="marvis-relay-composer-attachments" data-marvis-attachment-strip hidden></div>
      <p class="marvis-relay-mutation-status" data-relay-mutation-status role="status" aria-live="polite"></p>
    </form>
    {_marvis_relay_attachment_sheet_html()}
    """


def _marvis_relay_workspace_dock(workspace: str, *, access_token: str = "") -> str:
    workspace = str(workspace or "")
    label = Path(workspace).name or workspace or "选择工作区"
    href = relay_workspace_href(workspace, access_token)
    return f"""
    <div class="marvis-relay-workspace-dock" aria-label="当前工作区">
      <span class="marvis-relay-workspace-folder" aria-hidden="true"></span>
      <span class="marvis-relay-workspace-label">工作区</span>
      <a class="marvis-relay-workspace-chip" href="{escape(href)}" title="{escape(workspace or label)}">
        <span class="marvis-relay-workspace-name">{escape(label)}</span>
        <span class="marvis-relay-workspace-action">选择</span>
      </a>
    </div>
    """


def _marvis_relay_attachment_sheet_html() -> str:
    return """
    <div class="marvis-relay-attachment-backdrop" data-marvis-attachment-backdrop hidden></div>
    <section class="marvis-relay-attachment-sheet" data-marvis-attachment-sheet hidden aria-modal="true" role="dialog" aria-label="添加到对话">
      <button class="marvis-relay-attachment-close" type="button" aria-label="关闭" data-marvis-attachment-close>×</button>
      <h2>添加到对话</h2>
      <div class="marvis-relay-attachment-grid" aria-label="附件类型">
        <button class="marvis-relay-attachment-tile" type="button" data-marvis-pick-image>
          <img class="marvis-relay-sheet-icon-native marvis-relay-sheet-icon-native-small" src="/static/marvis/attachment-icon-album-marvis.png" alt="" aria-hidden="true">
          <span>相册</span>
        </button>
        <button class="marvis-relay-attachment-tile" type="button" data-marvis-pick-file>
          <img class="marvis-relay-sheet-icon-native marvis-relay-sheet-icon-native-small" src="/static/marvis/attachment-icon-local-file-marvis.png" alt="" aria-hidden="true">
          <span>本地文件</span>
        </button>
      </div>
      <div class="marvis-relay-skill-section">
        <p>我的技能</p>
        <button class="marvis-relay-skill-row" type="button" aria-label="添加技能">
          <img class="marvis-relay-sheet-icon-native marvis-relay-sheet-icon-native-skill" src="/static/marvis/attachment-icon-skills-marvis.png" alt="" aria-hidden="true">
          <span class="marvis-relay-skill-text">
            <strong>添加技能</strong>
            <small>有200+技能可供使用</small>
          </span>
          <span class="marvis-relay-skill-chevron" aria-hidden="true">›</span>
        </button>
      </div>
      <input type="file" accept="image/*" multiple hidden data-marvis-image-input>
      <input type="file" accept=".txt,.md,.markdown,.json,.jsonl,.log,.csv,.tsv,.yaml,.yml,.toml,.ini,.py,.js,.ts,.tsx,.jsx,.css,.html,.xml,.sh,.zsh,.sql,text/*,application/json" multiple hidden data-marvis-file-input>
    </section>
    """


def _marvis_relay_attachment_script() -> str:
    return r"""
    function setupMarvisRelayAttachments() {
      const composer = document.querySelector("[data-marvis-task-composer], [data-marvis-followup-composer]");
      const sheet = document.querySelector("[data-marvis-attachment-sheet]");
      if (!composer || !sheet) {
        return { payload: () => ({}), clear: () => {}, hasAttachments: () => false };
      }
      const backdrop = document.querySelector("[data-marvis-attachment-backdrop]");
      const openButton = composer.querySelector("[data-marvis-attach-open]");
      const closeButton = sheet.querySelector("[data-marvis-attachment-close]");
      const imageInput = sheet.querySelector("[data-marvis-image-input]");
      const fileInput = sheet.querySelector("[data-marvis-file-input]");
      const strip = composer.querySelector("[data-marvis-attachment-strip]");
      const state = { images: [], files: [] };
      let previouslyFocused = null;
      const focusableSelector = "a[href], button:not([disabled]), input:not([disabled]), textarea:not([disabled]), select:not([disabled]), [tabindex]:not([tabindex='-1'])";
      function setBackgroundInert(isOpen) {
        const phone = sheet.closest(".marvis-relay-phone");
        if (!phone) return;
        Array.from(phone.children).forEach((child) => {
          if (child === sheet || child === backdrop) return;
          child.inert = isOpen;
          child.setAttribute("aria-hidden", isOpen ? "true" : "false");
        });
      }
      const textFilePattern = /\.(txt|md|markdown|json|jsonl|log|csv|tsv|yaml|yml|toml|ini|py|js|ts|tsx|jsx|css|html|xml|sh|zsh|sql)$/i;
      function openSheet() {
        previouslyFocused = document.activeElement;
        setBackgroundInert(true);
        sheet.hidden = false;
        backdrop.hidden = false;
        requestAnimationFrame(() => {
          sheet.classList.add("open");
          backdrop.classList.add("visible");
          closeButton?.focus();
        });
      }
      function closeSheet() {
        if (sheet.hidden) return;
        sheet.classList.remove("open");
        backdrop.classList.remove("visible");
        setBackgroundInert(false);
        window.setTimeout(() => {
          sheet.hidden = true;
          backdrop.hidden = true;
          if (previouslyFocused instanceof HTMLElement) previouslyFocused.focus();
        }, 180);
      }
      function readRelayImageAttachment(file) {
        return new Promise((resolve, reject) => {
          if (!file || !String(file.type || "").startsWith("image/")) {
            reject(new Error("请选择图片文件"));
            return;
          }
          const reader = new FileReader();
          reader.onload = () => resolve({
            filename: file.name || "image",
            mime_type: file.type || "image/*",
            size: file.size || 0,
            url: String(reader.result || "")
          });
          reader.onerror = () => reject(new Error("图片读取失败"));
          reader.readAsDataURL(file);
        });
      }
      function readRelayTextAttachment(file) {
        return new Promise((resolve, reject) => {
          if (!file) {
            reject(new Error("请选择文件"));
            return;
          }
          const mime = String(file.type || "");
          const name = String(file.name || "attachment.txt");
          if (mime && !mime.startsWith("text/") && mime !== "application/json" && !textFilePattern.test(name)) {
            reject(new Error(`${name} 暂只支持文本/代码文件`));
            return;
          }
          if (file.size > 1024 * 1024) {
            reject(new Error(`${name} 超过 1MB，请拆小后再上传`));
            return;
          }
          const reader = new FileReader();
          reader.onload = () => resolve({
            filename: name,
            mime_type: mime || "text/plain",
            size: file.size || 0,
            text: String(reader.result || "")
          });
          reader.onerror = () => reject(new Error("文件读取失败"));
          reader.readAsText(file);
        });
      }
      function renderRelayAttachmentStrip() {
        const hasImages = state.images.length > 0;
        const hasFiles = state.files.length > 0;
        composer?.classList.toggle("has-image-attachments", hasImages);
        if (!strip) return;
        strip.innerHTML = "";
        strip.hidden = !hasImages && !hasFiles;
        state.images.forEach((item, index) => {
          const preview = document.createElement("button");
          preview.type = "button";
          preview.className = "marvis-relay-composer-image-preview";
          preview.title = "移除图片";
          const img = document.createElement("img");
          img.src = item.url || "";
          img.alt = "";
          const remove = document.createElement("span");
          remove.className = "marvis-relay-composer-image-remove";
          remove.setAttribute("aria-hidden", "true");
          preview.append(img, remove);
          preview.addEventListener("click", () => {
            state.images.splice(index, 1);
            renderRelayAttachmentStrip();
          });
          strip.appendChild(preview);
        });
        state.files.forEach((item, index) => {
          const chip = document.createElement("button");
          chip.type = "button";
          chip.className = "marvis-relay-composer-attachment is-file";
          chip.title = item.filename || "文件";
          chip.innerHTML = '<span class="marvis-relay-attachment-icon" aria-hidden="true"></span><span></span><b aria-hidden="true">&#215;</b>';
          chip.querySelector("span:nth-child(2)").textContent = item.filename || "文件";
          chip.addEventListener("click", () => {
            state.files.splice(index, 1);
            renderRelayAttachmentStrip();
          });
          strip.appendChild(chip);
        });
        document.dispatchEvent(new CustomEvent("marvis-relay-attachments-changed"));
      }
      function addErrorChip(message) {
        if (!strip || !message) return;
        strip.hidden = false;
        const chip = document.createElement("span");
        chip.className = "marvis-relay-composer-attachment is-error";
        chip.textContent = message;
        strip.appendChild(chip);
        window.setTimeout(() => {
          chip.remove();
          if (!strip.children.length) strip.hidden = true;
        }, 3500);
      }
      openButton?.addEventListener("click", openSheet);
      closeButton?.addEventListener("click", closeSheet);
      backdrop?.addEventListener("click", closeSheet);
      sheet.addEventListener("keydown", (event) => {
        if (event.key === "Escape") {
          event.preventDefault();
          closeSheet();
          return;
        }
        if (event.key !== "Tab") return;
        const controls = Array.from(sheet.querySelectorAll(focusableSelector));
        if (!controls.length) return;
        const first = controls[0];
        const last = controls[controls.length - 1];
        if (event.shiftKey && document.activeElement === first) {
          event.preventDefault();
          last.focus();
        } else if (!event.shiftKey && document.activeElement === last) {
          event.preventDefault();
          first.focus();
        }
      });
      sheet.querySelector("[data-marvis-pick-image]")?.addEventListener("click", () => imageInput?.click());
      sheet.querySelector("[data-marvis-pick-file]")?.addEventListener("click", () => fileInput?.click());
      imageInput?.addEventListener("change", async () => {
        for (const file of Array.from(imageInput.files || [])) {
          try {
            state.images.push(await readRelayImageAttachment(file));
          } catch (error) {
            addErrorChip(error?.message || "图片读取失败");
          }
        }
        imageInput.value = "";
        renderRelayAttachmentStrip();
        closeSheet();
      });
      fileInput?.addEventListener("change", async () => {
        for (const file of Array.from(fileInput.files || [])) {
          try {
            state.files.push(await readRelayTextAttachment(file));
          } catch (error) {
            addErrorChip(error?.message || "文件读取失败");
          }
        }
        fileInput.value = "";
        renderRelayAttachmentStrip();
        closeSheet();
      });
      const api = {
        payload() {
          return {
            images: state.images.map((item) => ({...item})),
            files: state.files.map((item) => ({...item}))
          };
        },
        clear() {
          state.images = [];
          state.files = [];
          renderRelayAttachmentStrip();
        },
        hasAttachments() {
          return state.images.length > 0 || state.files.length > 0;
        }
      };
      window.readRelayImageAttachment = readRelayImageAttachment;
      window.readRelayTextAttachment = readRelayTextAttachment;
      window.marvisRelayAttachments = api;
      return api;
    }
    function appendMarvisAttachmentList(parent, attachments = {}) {
      if (!parent) return;
      const images = Array.isArray(attachments.images) ? attachments.images : [];
      const files = Array.isArray(attachments.files) ? attachments.files : [];
      if (!images.length && !files.length) return;
      const imageList = document.createElement("div");
      imageList.className = "marvis-relay-message-images";
      images.forEach((item) => {
        const src = String(item?.url || item?.data_url || "");
        if (!src) return;
        const image = document.createElement("img");
        image.className = "marvis-relay-message-image";
        image.src = src;
        image.alt = "";
        image.loading = "lazy";
        imageList.appendChild(image);
      });
      if (imageList.children.length) parent.appendChild(imageList);
      if (!files.length) return;
      const list = document.createElement("div");
      list.className = "marvis-relay-attachment-list";
      const addChip = (item) => {
        const chip = document.createElement("span");
        chip.className = "marvis-relay-attachment-chip marvis-relay-attachment-chip-file";
        chip.innerHTML = '<span class="marvis-relay-attachment-icon" aria-hidden="true"></span><span></span>';
        chip.querySelector("span:last-child").textContent = item.filename || "文件";
        list.appendChild(chip);
      };
      files.forEach((item) => addChip(item));
      parent.appendChild(list);
    }
    setupMarvisRelayAttachments();
    """



