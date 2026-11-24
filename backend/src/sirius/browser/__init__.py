"""Universal Authenticated Browser & Website Control Subsystem for Sirius."""

from sirius.browser.agent import AuthenticatedBrowserAgent, BrowserExecutionMode
from sirius.browser.dom_distiller import DistilledElement, DistilledPage, get_dom_distiller_js
from sirius.browser.extension_bridge import BrowserExtensionBridge
from sirius.browser.profiles import BrowserKind, BrowserProfileInfo, discover_browser_profile

__all__ = [
    "AuthenticatedBrowserAgent",
    "BrowserExecutionMode",
    "BrowserExtensionBridge",
    "BrowserKind",
    "BrowserProfileInfo",
    "DistilledElement",
    "DistilledPage",
    "discover_browser_profile",
    "get_dom_distiller_js",
]
