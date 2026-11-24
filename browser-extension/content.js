// Sirius Companion Extension — Content Script (Semantic DOM Distiller & Action Synthesizer)

function findElementDeep(root, agentId) {
  if (!root) return null;
  const direct = root.querySelector(`[data-agent-id="${CSS.escape(agentId)}"]`);
  if (direct) return direct;
  const walker = document.createTreeWalker(root, NodeFilter.SHOW_ELEMENT);
  let node = walker.currentNode;
  while (node) {
    if (node instanceof Element && node.shadowRoot) {
      const found = findElementDeep(node.shadowRoot, agentId);
      if (found) return found;
    }
    node = walker.nextNode();
  }
  return null;
}

function distillLiveDom() {
  const INTERACTIVE_TAGS = new Set(["a", "button", "input", "textarea", "select", "summary", "option"]);
  const INTERACTIVE_ROLES = new Set([
    "button", "link", "menuitem", "tab", "checkbox", "radio", "switch",
    "combobox", "textbox", "searchbox", "option", "slider"
  ]);

  let seq = 1;
  const elements = [];
  let hasLargeCanvas = false;

  function isVisible(el) {
    const style = window.getComputedStyle(el);
    if (style.display === "none" || style.visibility === "hidden" || parseFloat(style.opacity || "1") < 0.05) {
      return false;
    }
    const r = el.getBoundingClientRect();
    return r.width >= 3 && r.height >= 3;
  }

  function inferLabel(el) {
    const aria = el.getAttribute("aria-label") || "";
    if (aria.trim()) return aria.trim();
    const placeholder = el.getAttribute("placeholder") || "";
    if (placeholder.trim()) return placeholder.trim();
    const title = el.getAttribute("title") || el.getAttribute("alt") || "";
    if (title.trim()) return title.trim();
    return (el.innerText || el.textContent || "").replace(/\s+/g, " ").trim().slice(0, 120);
  }

  function walk(root) {
    const tw = document.createTreeWalker(root, NodeFilter.SHOW_ELEMENT);
    let node = tw.currentNode;
    while (node) {
      if (node instanceof Element) {
        const tag = node.tagName.toLowerCase();
        if (tag === "canvas" && isVisible(node)) {
          const r = node.getBoundingClientRect();
          if (r.width >= 200 && r.height >= 150) hasLargeCanvas = true;
        }
        const role = (node.getAttribute("role") || "").toLowerCase();
        const clickable =
          node.hasAttribute("onclick") ||
          INTERACTIVE_TAGS.has(tag) ||
          INTERACTIVE_ROLES.has(role) ||
          node.getAttribute("contenteditable") === "true";

        if (clickable && isVisible(node) && elements.length < 180) {
          const agentId = `s-${seq++}`;
          node.setAttribute("data-agent-id", agentId);
          const rect = node.getBoundingClientRect();
          const inputType = (node.getAttribute("type") || "").toLowerCase();
          const val =
            "value" in node && typeof node.value === "string"
              ? inputType === "password"
                ? "••••••••"
                : node.value.slice(0, 80)
              : "";
          elements.push({
            agent_id: agentId,
            tag,
            role: role || tag,
            input_type: inputType,
            label: inferLabel(node),
            value: val,
            href: tag === "a" ? (node.getAttribute("href") || "").slice(0, 140) : "",
            disabled: Boolean(node.disabled || node.getAttribute("aria-disabled") === "true"),
            bounds: {
              x: Math.round(rect.left),
              y: Math.round(rect.top),
              width: Math.round(rect.width),
              height: Math.round(rect.height),
            },
          });
        }
        if (node.shadowRoot) walk(node.shadowRoot);
      }
      node = tw.nextNode();
    }
  }

  if (document.body) walk(document.body);
  const excerpt = document.body
    ? (document.body.innerText || "").replace(/\s+/g, " ").trim().slice(0, 2500)
    : "";

  return {
    url: window.location.href,
    title: document.title || "",
    elements,
    excerpt,
    has_large_canvas: hasLargeCanvas,
    needs_vision_fallback: hasLargeCanvas && elements.length < 4,
  };
}

chrome.runtime.onMessage.addListener((msg, _sender, sendResponse) => {
  const { action, payload } = msg;
  try {
    if (action === "distill_dom") {
      sendResponse(distillLiveDom());
      return false;
    }
    if (action === "click_element") {
      const el = findElementDeep(document, payload.agent_id);
      if (!el) throw new Error(`Element ${payload.agent_id} not found.`);
      el.scrollIntoView({ block: "center", inline: "center" });
      el.click();
      sendResponse({ ok: true, agent_id: payload.agent_id });
      return false;
    }
    if (action === "fill_element") {
      const el = findElementDeep(document, payload.agent_id);
      if (!el) throw new Error(`Element ${payload.agent_id} not found.`);
      el.focus();
      if ("value" in el) {
        el.value = payload.text;
      } else {
        el.textContent = payload.text;
      }
      el.dispatchEvent(new Event("input", { bubbles: true }));
      el.dispatchEvent(new Event("change", { bubbles: true }));
      if (payload.press_enter) {
        el.dispatchEvent(new KeyboardEvent("keydown", { key: "Enter", code: "Enter", bubbles: true }));
      }
      sendResponse({ ok: true, agent_id: payload.agent_id });
      return false;
    }
    if (action === "select_option") {
      const el = findElementDeep(document, payload.agent_id);
      if (!el || el.tagName.toLowerCase() !== "select") {
        throw new Error(`Select element ${payload.agent_id} not found.`);
      }
      el.value = payload.value;
      el.dispatchEvent(new Event("change", { bubbles: true }));
      sendResponse({ ok: true, selected: el.value });
      return false;
    }
    if (action === "scroll_page") {
      window.scrollBy({ left: payload.dx || 0, top: payload.dy || 550, behavior: "smooth" });
      sendResponse({ ok: true });
      return false;
    }
  } catch (err) {
    sendResponse({ ok: false, error: err instanceof Error ? err.message : String(err) });
  }
  return false;
});
