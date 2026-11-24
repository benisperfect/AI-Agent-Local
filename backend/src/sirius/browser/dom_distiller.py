"""Semantic DOM & ARIA Distiller with `data-agent-id` Selector Indexing."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from sirius.os_control.base import Rect

_DISTILLER_JS = r"""
(() => {
  const INTERACTIVE_TAGS = new Set(["a", "button", "input", "textarea", "select", "summary", "option"]);
  const INTERACTIVE_ROLES = new Set([
    "button", "link", "menuitem", "tab", "checkbox", "radio", "switch",
    "combobox", "textbox", "searchbox", "option", "slider"
  ]);

  let seq = 1;
  const elements = [];
  let hasLargeCanvas = false;

  function isVisible(el) {
    if (!el || !(el instanceof Element)) return false;
    const style = window.getComputedStyle(el);
    if (style.display === "none" || style.visibility === "hidden" || parseFloat(style.opacity || "1") < 0.05) {
      return false;
    }
    const rect = el.getBoundingClientRect();
    return rect.width >= 3 && rect.height >= 3;
  }

  function inferLabel(el) {
    const aria = el.getAttribute("aria-label") || "";
    if (aria.trim()) return aria.trim();
    const labelledBy = el.getAttribute("aria-labelledby");
    if (labelledBy) {
      const ref = document.getElementById(labelledBy);
      if (ref && ref.textContent) return ref.textContent.trim();
    }
    if (el.id) {
      const lbl = document.querySelector(`label[for="${CSS.escape(el.id)}"]`);
      if (lbl && lbl.textContent) return lbl.textContent.trim();
    }
    const placeholder = el.getAttribute("placeholder") || "";
    if (placeholder.trim()) return placeholder.trim();
    const title = el.getAttribute("title") || el.getAttribute("alt") || "";
    if (title.trim()) return title.trim();
    const txt = (el.innerText || el.textContent || "").replace(/\s+/g, " ").trim();
    return txt.slice(0, 120);
  }

  function traverse(root) {
    if (!root) return;
    const treeWalker = document.createTreeWalker(root, NodeFilter.SHOW_ELEMENT);
    let node = treeWalker.currentNode;
    while (node) {
      if (node instanceof Element) {
        const tag = node.tagName.toLowerCase();
        if (tag === "canvas" && isVisible(node)) {
          const r = node.getBoundingClientRect();
          if (r.width >= 200 && r.height >= 150) hasLargeCanvas = true;
        }

        const role = (node.getAttribute("role") || "").toLowerCase();
        const editable = node.getAttribute("contenteditable") === "true";
        const clickable = node.hasAttribute("onclick") || INTERACTIVE_TAGS.has(tag) || INTERACTIVE_ROLES.has(role) || editable;

        if (clickable && isVisible(node) && elements.length < 180) {
          const agentId = `s-${seq++}`;
          node.setAttribute("data-agent-id", agentId);
          const rect = node.getBoundingClientRect();
          let val = "";
          if ("value" in node && typeof node.value === "string") {
            const inputType = (node.getAttribute("type") || "").toLowerCase();
            val = inputType === "password" ? "••••••••" : node.value.slice(0, 80);
          }
          elements.push({
            agent_id: agentId,
            tag: tag,
            role: role || tag,
            input_type: (node.getAttribute("type") || "").toLowerCase(),
            label: inferLabel(node),
            value: val,
            href: tag === "a" ? (node.getAttribute("href") || "").slice(0, 140) : "",
            disabled: Boolean(node.disabled || node.getAttribute("aria-disabled") === "true"),
            bounds: {
              x: Math.round(rect.left),
              y: Math.round(rect.top),
              width: Math.round(rect.width),
              height: Math.round(rect.height),
            }
          });
        }

        if (node.shadowRoot) {
          traverse(node.shadowRoot);
        }
      }
      node = treeWalker.nextNode();
    }
  }

  if (document.body) {
    traverse(document.body);
  }

  const bodyText = document.body ? (document.body.innerText || "").replace(/\s+/g, " ").trim().slice(0, 2500) : "";

  return {
    url: window.location.href,
    title: document.title || "",
    elements: elements,
    excerpt: bodyText,
    has_large_canvas: hasLargeCanvas,
    needs_vision_fallback: hasLargeCanvas && elements.length < 4,
  };
})()
"""


def get_dom_distiller_js() -> str:
    """Return self-contained browser JS snippet that stamps `data-agent-id` and extracts semantics."""
    return _DISTILLER_JS


@dataclass(frozen=True)
class DistilledElement:
    agent_id: str
    tag: str
    role: str
    input_type: str
    label: str
    value: str
    href: str
    disabled: bool
    bounds: Rect

    def to_compact_line(self) -> str:
        val_str = f" val={self.value!r}" if self.value else ""
        type_str = f"[{self.input_type}]" if self.input_type else ""
        return (
            f"[{self.agent_id}] <{self.tag}{type_str}> role={self.role} "
            f"label={self.label!r}{val_str} "
            f"@({self.bounds.x},{self.bounds.y},{self.bounds.width}x{self.bounds.height})"
        )


@dataclass
class DistilledPage:
    url: str
    title: str
    elements: list[DistilledElement] = field(default_factory=list)
    excerpt: str = ""
    has_large_canvas: bool = False
    needs_vision_fallback: bool = False

    @classmethod
    def from_raw_dict(cls, data: dict[str, Any]) -> DistilledPage:
        elems: list[DistilledElement] = []
        for raw_el in data.get("elements", []):
            if not isinstance(raw_el, dict):
                continue
            b = raw_el.get("bounds") or {}
            elems.append(
                DistilledElement(
                    agent_id=str(raw_el.get("agent_id", "")),
                    tag=str(raw_el.get("tag", "div")),
                    role=str(raw_el.get("role", "generic")),
                    input_type=str(raw_el.get("input_type", "")),
                    label=str(raw_el.get("label", "")),
                    value=str(raw_el.get("value", "")),
                    href=str(raw_el.get("href", "")),
                    disabled=bool(raw_el.get("disabled", False)),
                    bounds=Rect(
                        x=int(b.get("x", 0)),
                        y=int(b.get("y", 0)),
                        width=int(b.get("width", 0)),
                        height=int(b.get("height", 0)),
                    ),
                )
            )
        return cls(
            url=str(data.get("url", "")),
            title=str(data.get("title", "")),
            elements=elems,
            excerpt=str(data.get("excerpt", "")),
            has_large_canvas=bool(data.get("has_large_canvas", False)),
            needs_vision_fallback=bool(data.get("needs_vision_fallback", False)),
        )

    def to_llm_summary(self, max_elements: int = 100) -> str:
        header = (
            f"URL: {self.url}\n"
            f"Title: {self.title}\n"
            f"NeedsVisionFallback: {self.needs_vision_fallback}\n"
            f"Interactive Elements ({len(self.elements)} total):"
        )
        el_lines = [el.to_compact_line() for el in self.elements[:max_elements]]
        body_section = f"\nPage Text Excerpt:\n{self.excerpt[:1200]}" if self.excerpt else ""
        return "\n".join([header, *el_lines]) + body_section
