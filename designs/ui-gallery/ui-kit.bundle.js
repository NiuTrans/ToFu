const VARIANT_CLASS = {
  primary: " ui-btn--primary",
  secondary: " ui-btn--secondary",
  ghost: " ui-btn--ghost",
  danger: " ui-btn--danger"
};
const SIZE_CLASS = {
  sm: " ui-btn--sm",
  md: "",
  lg: " ui-btn--lg"
};
function createButton(options, ownerDocument = document) {
  const variant = options.variant ?? "secondary";
  const size = options.size ?? "md";
  const button = ownerDocument.createElement("button");
  button.type = options.type ?? "button";
  button.className = `ui-btn${VARIANT_CLASS[variant]}${SIZE_CLASS[size]}`;
  if (options.wrap) button.classList.add("ui-btn--wrap");
  button.textContent = options.label;
  if (options.title) button.title = options.title;
  if (options.disabled) button.disabled = true;
  if (options.onClick) button.addEventListener("click", options.onClick);
  return button;
}
const ICON_SIZE_CLASS = {
  sm: " ui-icon-btn--sm",
  md: "",
  lg: " ui-icon-btn--lg"
};
function createIconButton(options) {
  const size = options.size ?? "md";
  const button = document.createElement("button");
  button.type = "button";
  button.className = `ui-icon-btn${ICON_SIZE_CLASS[size]}`;
  button.setAttribute("aria-label", options.label);
  button.title = options.title ?? options.label;
  button.appendChild(options.icon);
  if (options.disabled) button.disabled = true;
  if (options.onClick) button.addEventListener("click", options.onClick);
  return button;
}

let fieldSeq = 0;
function createField(options, document = globalThis.document) {
  const control = options.control ?? (() => {
    if (options.choices) {
      const select = document.createElement("select");
      for (const choice of options.choices) {
        const option = document.createElement("option");
        option.value = choice.value;
        option.textContent = choice.label;
        if (choice.value === (options.value ?? "")) option.setAttribute("selected", "");
        select.appendChild(option);
      }
      return select;
    }
    const input = document.createElement("input");
    input.className = "ui-input";
    input.type = options.inputType ?? "text";
    input.setAttribute("value", options.value ?? "");
    return input;
  })();
  fieldSeq += 1;
  const controlId = options.controlId ?? `ui-field-control-${fieldSeq}`;
  control.id = controlId;
  const label = document.createElement("label");
  label.className = "ui-field__label";
  label.htmlFor = controlId;
  label.textContent = options.label;
  if (options.required) {
    const marker = document.createElement("span");
    marker.className = "ui-field__label-required";
    marker.textContent = " *";
    label.appendChild(marker);
  }
  const el = document.createElement("div");
  el.className = "ui-field";
  el.appendChild(label);
  if (options.choices && !options.control) {
    const selectWrapper = document.createElement("div");
    selectWrapper.className = "ui-select";
    selectWrapper.appendChild(control);
    el.appendChild(selectWrapper);
  } else el.appendChild(control);
  if (options.hint) {
    const hint = document.createElement("div");
    hint.className = "ui-field__hint";
    hint.id = `${controlId}-hint`;
    hint.textContent = options.hint;
    el.appendChild(hint);
    control.setAttribute("aria-describedby", hint.id);
  }
  const error = document.createElement("div");
  error.className = "ui-field__error";
  error.id = `${controlId}-error`;
  error.setAttribute("role", "alert");
  el.appendChild(error);
  return { el, control };
}
function setFieldError(field, message) {
  const error = field.el.querySelector(".ui-field__error");
  if (!error) return;
  if (message) {
    error.textContent = message;
    field.el.classList.add("ui-field--error");
    field.control.setAttribute("aria-invalid", "true");
    field.control.setAttribute("aria-describedby", error.id);
  } else {
    error.textContent = "";
    field.el.classList.remove("ui-field--error");
    field.control.removeAttribute("aria-invalid");
    const hint = field.el.querySelector(".ui-field__hint");
    if (hint) {
      field.control.setAttribute("aria-describedby", hint.id);
    } else {
      field.control.removeAttribute("aria-describedby");
    }
  }
}

function createDialog(options) {
  const el = document.createElement("dialog");
  el.className = "ui-dialog";
  if (options.appearance === "project") el.classList.add("ui-dialog--project");
  const header = document.createElement("div");
  header.className = "ui-dialog__header";
  const title = document.createElement("h2");
  title.className = "ui-dialog__title";
  title.textContent = options.title;
  header.appendChild(title);
  el.appendChild(header);
  const body = document.createElement("div");
  body.className = "ui-dialog__body";
  if (typeof options.body === "string") {
    body.textContent = options.body;
  } else {
    body.appendChild(options.body);
  }
  el.appendChild(body);
  let resolveOpen = null;
  let settled = true;
  const settle = (result) => {
    if (settled) return;
    settled = true;
    const resolve = resolveOpen;
    resolveOpen = null;
    options.onClose?.(result);
    resolve?.(result);
  };
  const actions = options.actions ?? [];
  if (actions.length > 0) {
    const footer = document.createElement("div");
    footer.className = "ui-dialog__footer";
    for (const action of actions) {
      footer.appendChild(createButton({
        label: action.label,
        variant: action.variant ?? "secondary",
        onClick: () => {
          settle(action.id);
          el.close(action.id);
        }
      }));
    }
    el.appendChild(footer);
  }
  el.addEventListener("keydown", (event) => {
    if (event.key !== "Tab" || !el.open) return;
    const controls = [...el.querySelectorAll("a[href],button,input,select,textarea,[tabindex]")].filter((node) => node.tabIndex >= 0 && !node.matches(":disabled") && !node.closest("[hidden],[inert]") && getComputedStyle(node).display !== "none" && getComputedStyle(node).visibility !== "hidden");
    const first = controls[0], last = controls.at(-1);
    if (!first || !last) {
      event.preventDefault();
      el.focus();
      return;
    }
    const active = document.activeElement;
    if (event.shiftKey ? active === first || active === el || !el.contains(active) : active === last || active === el || !el.contains(active)) {
      event.preventDefault();
      (event.shiftKey ? last : first).focus();
    }
  });
  el.addEventListener("close", () => {
    settle(el.returnValue === "" ? null : el.returnValue);
  });
  let pressedOutside = false;
  const isOutsideDialog = (event) => {
    const rect = el.getBoundingClientRect();
    return event.clientX < rect.left || event.clientX > rect.right || event.clientY < rect.top || event.clientY > rect.bottom;
  };
  el.addEventListener("mousedown", (event) => {
    pressedOutside = isOutsideDialog(event);
  });
  el.addEventListener("click", (event) => {
    if (pressedOutside && isOutsideDialog(event)) {
      settle(null);
      el.close("");
    }
    pressedOutside = false;
  });
  return {
    el,
    open: () => {
      if (el.open) {
        return Promise.resolve(null);
      }
      settled = false;
      const promise = new Promise((resolve) => {
        resolveOpen = resolve;
      });
      el.showModal();
      return promise;
    },
    close: (result = null) => {
      if (!el.open) return;
      settle(result);
      el.close(result ?? "");
    }
  };
}

const CLOSE_ICON_SVG$2 = '<svg width="14" height="14" viewBox="0 0 16 16" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round"><path d="M4 4l8 8M12 4l-8 8"/></svg>';
function createDrawer(options) {
  const el = document.createElement("dialog");
  el.className = "ui-dialog ui-drawer";
  const header = document.createElement("div");
  header.className = "ui-dialog__header";
  const title = document.createElement("h2");
  title.className = "ui-dialog__title";
  title.textContent = options.title;
  header.appendChild(title);
  const icon = document.createElement("span");
  icon.innerHTML = CLOSE_ICON_SVG$2;
  const closeButton = createIconButton({
    icon: icon.firstElementChild ?? icon,
    label: "关闭面板",
    onClick: () => el.close("")
  });
  header.appendChild(closeButton);
  el.appendChild(header);
  const body = document.createElement("div");
  body.className = "ui-dialog__body";
  if (typeof options.body === "string") {
    body.textContent = options.body;
  } else {
    body.appendChild(options.body);
  }
  el.appendChild(body);
  let resolveOpen = null;
  el.addEventListener("close", () => {
    options.onClose?.();
    const resolve = resolveOpen;
    resolveOpen = null;
    resolve?.();
  });
  let pressedOutside = false;
  const isOutsideDialog = (event) => {
    const rect = el.getBoundingClientRect();
    return event.clientX < rect.left || event.clientX > rect.right || event.clientY < rect.top || event.clientY > rect.bottom;
  };
  el.addEventListener("mousedown", (event) => {
    pressedOutside = isOutsideDialog(event);
  });
  el.addEventListener("click", (event) => {
    if (pressedOutside && isOutsideDialog(event)) el.close("");
    pressedOutside = false;
  });
  return {
    el,
    open: () => {
      if (el.open) return Promise.resolve();
      const promise = new Promise((resolve) => {
        resolveOpen = resolve;
      });
      el.showModal();
      return promise;
    },
    close: () => {
      if (el.open) el.close("");
    }
  };
}

function createChoice(options, doc = document) {
  const type = options.type ?? "checkbox";
  const input = doc.createElement("input");
  input.type = type === "radio" ? "radio" : "checkbox";
  if (type === "switch") {
    input.className = "ui-switch";
    input.setAttribute("role", "switch");
  }
  if (options.name) input.name = options.name;
  if (options.value) input.value = options.value;
  if (options.checked) input.checked = true;
  if (options.disabled) input.disabled = true;
  if (options.onChange) {
    input.addEventListener("change", () => options.onChange?.(input.checked, input));
  }
  const text = doc.createElement("span");
  text.textContent = options.label;
  const el = doc.createElement("label");
  el.className = "ui-choice";
  el.appendChild(input);
  el.appendChild(text);
  return { el, input };
}

let tabsSeq = 0;
function createTabs(options, document = globalThis.document) {
  tabsSeq += 1;
  const baseId = `ui-tabs-${tabsSeq}`;
  const enabled = options.tabs.filter((tab) => !tab.disabled);
  let current = options.activeId ?? enabled[0]?.id ?? options.tabs[0]?.id;
  const list = document.createElement("div");
  list.className = "ui-tabs__list";
  list.setAttribute("role", "tablist");
  const el = document.createElement("div");
  el.className = "ui-tabs";
  el.appendChild(list);
  const buttons = /* @__PURE__ */ new Map();
  const activate = (id, focus = false) => {
    const tab = options.tabs.find((item) => item.id === id);
    if (!tab || tab.disabled) return;
    current = id;
    for (const item of options.tabs) {
      const selected = item.id === id;
      const button = buttons.get(item.id);
      if (button) {
        button.setAttribute("aria-selected", String(selected));
        button.tabIndex = selected ? 0 : -1;
        if (selected && focus) button.focus();
      }
      item.panel.hidden = !selected;
    }
    options.onChange?.(id);
  };
  for (const [index, tab] of options.tabs.entries()) {
    const button = document.createElement("button");
    button.type = "button";
    button.className = "ui-tabs__tab";
    button.id = `${baseId}-tab-${tab.id}`;
    button.textContent = tab.label;
    button.setAttribute("role", "tab");
    button.setAttribute("aria-controls", `${baseId}-panel-${tab.id}`);
    if (tab.disabled) button.disabled = true;
    button.addEventListener("click", () => activate(tab.id));
    button.addEventListener("keydown", (event) => {
      const ids = enabled.map((item) => item.id);
      const at = ids.indexOf(current);
      let next = null;
      if (event.key === "ArrowRight") next = ids[(at + 1) % ids.length];
      else if (event.key === "ArrowLeft") next = ids[(at - 1 + ids.length) % ids.length];
      else if (event.key === "Home") next = ids[0];
      else if (event.key === "End") next = ids[ids.length - 1];
      if (next) {
        event.preventDefault();
        activate(next, true);
      }
    });
    buttons.set(tab.id, button);
    list.appendChild(button);
    tab.panel.id = `${baseId}-panel-${tab.id}`;
    tab.panel.classList.add("ui-tabs__panel");
    tab.panel.setAttribute("role", "tabpanel");
    tab.panel.setAttribute("aria-labelledby", button.id);
    tab.panel.tabIndex = 0;
    tab.panel.hidden = true;
    el.appendChild(tab.panel);
    void index;
  }
  if (current) activate(current);
  return { el, activate: (id) => activate(id), activeId: () => current };
}

function createMenuItem(entry, ownerDocument = document) {
  const item = ownerDocument.createElement("button");
  item.type = "button";
  item.className = `ui-menu__item${entry.danger ? " ui-menu__item--danger" : ""}`;
  item.textContent = entry.label;
  item.setAttribute("role", "menuitem");
  item.tabIndex = -1;
  if (entry.disabled) item.disabled = true;
  return item;
}
function createDropdown(options) {
  const el = document.createElement("div");
  el.className = "ui-dropdown";
  const trigger = createButton({
    label: options.label,
    variant: options.variant ?? "secondary",
    size: options.size,
    disabled: options.disabled
  });
  trigger.setAttribute("aria-haspopup", "menu");
  trigger.setAttribute("aria-expanded", "false");
  el.appendChild(trigger);
  const menu = document.createElement("div");
  menu.className = `ui-menu${options.align === "end" ? " ui-menu--align-end" : ""}`;
  menu.setAttribute("role", "menu");
  menu.hidden = true;
  el.appendChild(menu);
  const itemButtons = [];
  for (const entry of options.items) {
    if (entry === "separator") {
      const separator = document.createElement("hr");
      separator.className = "ui-menu__separator";
      menu.appendChild(separator);
      continue;
    }
    const item = createMenuItem(entry);
    item.addEventListener("click", () => {
      close();
      options.onSelect(entry.id);
    });
    itemButtons.push(item);
    menu.appendChild(item);
  }
  let open = false;
  const close = (returnFocus = false) => {
    if (!open) return;
    open = false;
    menu.hidden = true;
    trigger.setAttribute("aria-expanded", "false");
    document.removeEventListener("pointerdown", onOutsidePointer, true);
    if (returnFocus) trigger.focus();
  };
  const onOutsidePointer = (event) => {
    if (!el.contains(event.target)) close();
  };
  const focusItem = (index) => {
    const enabled = itemButtons.filter((button) => !button.disabled);
    if (enabled.length === 0) return;
    const clamped = (index % enabled.length + enabled.length) % enabled.length;
    enabled[clamped].focus();
  };
  const openMenu = (focusFirst = true) => {
    if (open || trigger.disabled) return;
    open = true;
    menu.hidden = false;
    trigger.setAttribute("aria-expanded", "true");
    document.addEventListener("pointerdown", onOutsidePointer, true);
    if (focusFirst) focusItem(0);
  };
  trigger.addEventListener("click", () => open ? close(true) : openMenu(false));
  trigger.addEventListener("keydown", (event) => {
    if (event.key === "ArrowDown") {
      event.preventDefault();
      if (open) {
        focusItem(0);
      } else {
        openMenu(true);
      }
    } else if (event.key === "Enter" || event.key === " ") {
      if (!open) {
        event.preventDefault();
        openMenu(true);
      }
    } else if (event.key === "Escape" && open) {
      event.preventDefault();
      close();
    }
  });
  menu.addEventListener("keydown", (event) => {
    const enabled = itemButtons.filter((button) => !button.disabled);
    const at = enabled.indexOf(document.activeElement);
    if (event.key === "Escape") {
      event.preventDefault();
      close(true);
    } else if (event.key === "ArrowDown") {
      event.preventDefault();
      focusItem(at + 1);
    } else if (event.key === "ArrowUp") {
      event.preventDefault();
      focusItem(at - 1);
    } else if (event.key === "Home") {
      event.preventDefault();
      focusItem(0);
    } else if (event.key === "End") {
      event.preventDefault();
      focusItem(enabled.length - 1);
    } else if (event.key === "Tab") {
      close();
    }
  });
  return { el, open: openMenu, close, isOpen: () => open };
}

const MAX_VISIBLE = 4;
const DEFAULT_DURATION = 4e3;
let region = null;
function toastRegion() {
  if (!region || !region.isConnected) {
    region = document.createElement("div");
    region.className = "ui-toast-region";
    region.setAttribute("role", "status");
    region.setAttribute("aria-live", "polite");
    document.body.appendChild(region);
  }
  return region;
}
const CLOSE_ICON_SVG$1 = '<svg width="12" height="12" viewBox="0 0 16 16" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round"><path d="M4 4l8 8M12 4l-8 8"/></svg>';
function toast(message, options = {}) {
  const container = toastRegion();
  const el = document.createElement("div");
  const variant = options.variant ?? "default";
  el.className = `ui-toast${variant === "default" ? "" : ` ui-toast--${variant}`}`;
  const body = document.createElement("div");
  body.className = "ui-toast__body";
  if (options.title) {
    const title = document.createElement("div");
    title.className = "ui-toast__title";
    title.textContent = options.title;
    body.appendChild(title);
  }
  const text = document.createElement("div");
  text.textContent = message;
  body.appendChild(text);
  el.appendChild(body);
  const dismiss = () => {
    window.clearTimeout(timer);
    el.remove();
  };
  const icon = document.createElement("span");
  icon.innerHTML = CLOSE_ICON_SVG$1;
  const closeButton = createIconButton({
    icon: icon.firstElementChild ?? icon,
    label: "关闭通知",
    size: "sm",
    onClick: dismiss
  });
  el.appendChild(closeButton);
  const timer = window.setTimeout(
    dismiss,
    options.duration ?? DEFAULT_DURATION
  );
  if ((options.duration ?? DEFAULT_DURATION) === 0) window.clearTimeout(timer);
  while (container.children.length >= MAX_VISIBLE) {
    container.firstElementChild?.remove();
  }
  container.appendChild(el);
  return { el, dismiss };
}
function dismissAllToasts() {
  region?.remove();
  region = null;
}

const CLOSE_ICON_SVG = '<svg width="12" height="12" viewBox="0 0 16 16" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round"><path d="M4 4l8 8M12 4l-8 8"/></svg>';
function createAlert(options) {
  const el = document.createElement("div");
  el.className = `ui-alert ui-alert--${options.variant}`;
  el.setAttribute("role", options.variant === "error" ? "alert" : "status");
  const title = document.createElement("div");
  title.className = "ui-alert__title";
  title.textContent = options.title;
  el.appendChild(title);
  if (options.body) {
    const body = document.createElement("div");
    body.textContent = options.body;
    el.appendChild(body);
  }
  const dismiss = () => {
    el.remove();
    options.onDismiss?.();
  };
  if (options.dismissible) {
    const icon = document.createElement("span");
    icon.innerHTML = CLOSE_ICON_SVG;
    const button = createIconButton({
      icon: icon.firstElementChild ?? icon,
      label: "关闭提示",
      size: "sm",
      onClick: dismiss
    });
    button.classList.add("ui-alert__dismiss");
    el.appendChild(button);
  }
  return { el, dismiss };
}

function createSpinner(options = {}) {
  const el = document.createElement("span");
  const size = options.size ?? "md";
  el.className = `ui-spinner${size === "md" ? "" : ` ui-spinner--${size}`}`;
  el.setAttribute("role", "status");
  el.setAttribute("aria-label", options.label ?? "加载中");
  return el;
}
function createSkeleton(options = {}) {
  const wrap = document.createElement("div");
  wrap.setAttribute("aria-hidden", "true");
  const lines = options.lines ?? 0;
  if (lines > 0) {
    for (let i = 0; i < lines; i += 1) {
      const line = document.createElement("div");
      line.className = "ui-skeleton ui-skeleton--text";
      if (i === lines - 1) line.style.width = "60%";
      wrap.appendChild(line);
    }
  } else {
    const block = document.createElement("div");
    block.className = "ui-skeleton";
    block.style.height = options.height ?? "80px";
    wrap.appendChild(block);
  }
  return wrap;
}
function createProgress(options = {}) {
  const el = document.createElement("div");
  const indeterminate = options.indeterminate ?? options.value === void 0;
  el.className = `ui-progress${indeterminate ? " ui-progress--indeterminate" : ""}`;
  el.setAttribute("role", "progressbar");
  if (options.label) el.setAttribute("aria-label", options.label);
  const bar = document.createElement("div");
  bar.className = "ui-progress__bar";
  el.appendChild(bar);
  const setValue = (value) => {
    const clamped = Math.min(100, Math.max(0, value));
    el.style.setProperty("--ui-progress-value", `${clamped}%`);
    el.setAttribute("aria-valuenow", String(Math.round(clamped)));
  };
  if (!indeterminate) {
    el.setAttribute("aria-valuemin", "0");
    el.setAttribute("aria-valuemax", "100");
    setValue(options.value ?? 0);
  }
  return { el, setValue };
}
function createEmptyState(options) {
  const el = document.createElement("div");
  el.className = "ui-empty";
  if (options.icon) {
    const icon = document.createElement("div");
    icon.className = "ui-empty__icon";
    icon.appendChild(options.icon);
    el.appendChild(icon);
  }
  const title = document.createElement("div");
  title.className = "ui-empty__title";
  title.textContent = options.title;
  el.appendChild(title);
  if (options.description) {
    const description = document.createElement("div");
    description.className = "ui-empty__description";
    description.textContent = options.description;
    el.appendChild(description);
  }
  if (options.action) {
    const action = document.createElement("div");
    action.className = "ui-empty__action";
    action.appendChild(createButton(options.action));
    el.appendChild(action);
  }
  return el;
}

export { createAlert, createButton, createChoice, createDialog, createDrawer, createDropdown, createEmptyState, createField, createIconButton, createMenuItem, createProgress, createSkeleton, createSpinner, createTabs, dismissAllToasts, setFieldError, toast };
