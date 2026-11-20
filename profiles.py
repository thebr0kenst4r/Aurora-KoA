"""
profiles.py
Master Mode Registry, Base Profile Class & Global Site-Wide Mode Persistence.
Links each APP_MODE option ('gaming', 'realtor', etc.) to its dedicated mode package
under modes/<mode_key>/ (e.g., modes/gaming/profile.py, modes/realtor/profile.py).
Stores the active site mode globally via @st.cache_resource + site_state.json +
GitHub 'db-vault' sync so switching modes with the Sovereign Key updates the
active mode for ALL visitors on that deployment and persists across reboots.
"""

import os
import json
import copy
import importlib
from datetime import datetime, timezone
from dataclasses import dataclass, field
from typing import List, Dict, Tuple, Any, Optional, Callable
import streamlit as st

from modes import MODE_PACKAGE_REGISTRY

SITE_STATE_FILE = "site_state.json"
CUSTOM_MODES_FILE = "custom_modes.json"


def get_secret_or_env(key: str, default: str = "") -> str:
    """Safely retrieves a configuration value from environment variables or Streamlit secrets."""
    val = os.environ.get(key)
    if val is not None and str(val).strip() != "":
        return str(val).strip()
    try:
        if key in st.secrets:
            return str(st.secrets[key]).strip()
    except Exception:
        pass
    return default


def get_deployment_namespace() -> str:
    """
    Identifies this deployment instance on GitHub db-vault so a Gaming site and a Realtor site
    can share the same GitHub repository without overwriting each other's active site mode.
    """
    explicit_id = get_secret_or_env("SITE_ID", "")
    if explicit_id:
        return explicit_id.lower().strip().replace(" ", "_")
    base_secret_mode = get_secret_or_env("APP_MODE", "gaming").lower().strip()
    return base_secret_mode if base_secret_mode else "gaming"


@st.cache_resource
def _get_global_site_state_holder() -> Dict[str, Any]:
    """
    Process-wide singleton shared across ALL browser sessions on this Streamlit deployment.
    """
    default_mode = get_secret_or_env("APP_MODE", "gaming").lower().strip() or "gaming"
    active_mode = default_mode

    if os.path.exists(SITE_STATE_FILE):
        try:
            with open(SITE_STATE_FILE, "r", encoding="utf-8") as f:
                data = json.load(f)
                if isinstance(data, dict) and data.get("active_mode"):
                    active_mode = str(data["active_mode"]).lower().strip()
        except Exception as e:
            print(f"[Global Mode State Notice] Could not read {SITE_STATE_FILE}: {e}")

    custom_modes: List[str] = []
    if os.path.exists(CUSTOM_MODES_FILE):
        try:
            with open(CUSTOM_MODES_FILE, "r", encoding="utf-8") as f:
                cdata = json.load(f)
                if isinstance(cdata, list):
                    custom_modes = [str(m).lower().strip() for m in cdata if str(m).strip()]
        except Exception:
            pass

    return {
        "active_mode": active_mode,
        "custom_modes": custom_modes,
    }


@dataclass
class BotProfile:
    """
    Base configuration class for all deployment modes.
    Subclasses in modes/gaming/profile.py and modes/realtor/profile.py define the Python defaults.
    """
    mode_key: str = "base"
    page_title: str = "Aurora — The Broken Star"
    page_icon: str = "✨"
    bot_name: str = "Aurora"
    header_title: str = "✨ Aurora — The Broken Star"
    header_subtitle: str = "Eternal Strategist, Guardian & Celestial Lorekeeper"
    gateway_status_text: str = "Gateway Connected • Public Sanctum & Celestial Resonance Active"
    creator_name: str = "Aerith"
    creator_identifiers: List[str] = field(
        default_factory=lambda: ["aerith", "aeri", "thebr0kenst4r", "the broken star"]
    )
    db_filename: str = "aurora_core.db"
    banner_html: str = ""
    welcome_message: str = "I am Aurora. The celestial resonance chamber is open. How may I assist your campaign?"

    self_identity_description: str = ""
    creator_relationship_instruction: str = ""
    system_instruction: str = ""
    about_creator_markdown: str = ""

    trusted_domains: List[str] = field(default_factory=list)
    access_tiers: List[str] = field(
        default_factory=lambda: ["Everyone", "Alliance Only", "Sovereign Only", "Draft"]
    )

    enable_discord: bool = True
    enable_discord_voice: bool = True
    enable_auto_learn: bool = True
    enable_auto_scout: bool = True
    enable_calculator: bool = True
    enable_lore_vault: bool = True
    enable_keep_alive_ping: bool = True
    backup_interval_hours: float = 3.0
    app_public_url: str = "https://aurora-koa.streamlit.app"
    calculator_type: str = "gaming_march"
    calculator_renderer: Optional[Callable[[], None]] = field(default=None, repr=False)

    ui_strings: Dict[str, str] = field(default_factory=dict)

    seed_docs: List[Tuple[str, str, str, str, str, str]] = field(default_factory=list)
    seed_jokes: List[str] = field(default_factory=list)
    seed_media: List[Tuple[str, str, str, str]] = field(default_factory=list)

    _python_defaults: Dict[str, Any] = field(default_factory=dict, init=False, repr=False)

    def snapshot_python_defaults(self) -> None:
        self._python_defaults = {
            "page_title": self.page_title,
            "page_icon": self.page_icon,
            "bot_name": self.bot_name,
            "header_title": self.header_title,
            "header_subtitle": self.header_subtitle,
            "gateway_status_text": self.gateway_status_text,
            "creator_name": self.creator_name,
            "banner_html": self.banner_html,
            "welcome_message": self.welcome_message,
            "self_identity_description": self.self_identity_description,
            "creator_relationship_instruction": self.creator_relationship_instruction,
            "system_instruction": self.system_instruction,
            "about_creator_markdown": self.about_creator_markdown,
            "trusted_domains": copy.deepcopy(self.trusted_domains),
            "access_tiers": copy.deepcopy(self.access_tiers),
            "enable_discord": self.enable_discord,
            "enable_discord_voice": self.enable_discord_voice,
            "enable_auto_learn": self.enable_auto_learn,
            "enable_auto_scout": self.enable_auto_scout,
            "enable_calculator": self.enable_calculator,
            "enable_lore_vault": self.enable_lore_vault,
            "enable_keep_alive_ping": self.enable_keep_alive_ping,
            "backup_interval_hours": self.backup_interval_hours,
            "app_public_url": self.app_public_url,
            "calculator_type": self.calculator_type,
            "ui_strings": copy.deepcopy(self.ui_strings),
        }

    def revert_to_python_defaults(self) -> None:
        if not self._python_defaults:
            return
        for k, v in self._python_defaults.items():
            setattr(self, k, copy.deepcopy(v))

    def get_python_default(self, attr_or_str_key: str) -> Any:
        if attr_or_str_key in self._python_defaults:
            return self._python_defaults[attr_or_str_key]
        return self._python_defaults.get("ui_strings", {}).get(attr_or_str_key, "")

    def apply_overrides(self, overrides: Dict[str, Any]) -> None:
        if not overrides or not isinstance(overrides, dict):
            return
        try:
            scalar_attrs = [
                "page_title", "page_icon", "bot_name", "header_title",
                "header_subtitle", "gateway_status_text", "creator_name",
                "banner_html", "welcome_message", "self_identity_description",
                "creator_relationship_instruction", "system_instruction",
                "about_creator_markdown", "enable_discord", "enable_discord_voice",
                "enable_auto_learn", "enable_auto_scout", "enable_calculator",
                "enable_lore_vault", "enable_keep_alive_ping", "backup_interval_hours",
                "app_public_url", "calculator_type",
            ]
            for attr in scalar_attrs:
                if attr in overrides and overrides[attr] is not None:
                    val = overrides[attr]
                    if isinstance(val, str) and val.strip() == "":
                        continue
                    setattr(self, attr, val)

            if "trusted_domains" in overrides and isinstance(overrides["trusted_domains"], list):
                cleaned_domains = [str(d).strip().lower() for d in overrides["trusted_domains"] if str(d).strip()]
                if cleaned_domains:
                    self.trusted_domains = cleaned_domains

            if "access_tiers" in overrides and isinstance(overrides["access_tiers"], list):
                cleaned_tiers = [str(t).strip() for t in overrides["access_tiers"] if str(t).strip()]
                if cleaned_tiers:
                    self.access_tiers = cleaned_tiers

            if "ui_strings" in overrides and isinstance(overrides["ui_strings"], dict):
                for s_key, s_val in overrides["ui_strings"].items():
                    if s_val is not None and str(s_val).strip() != "":
                        self.ui_strings[s_key] = str(s_val)
        except Exception as e:
            print(f"[Profile Override Notice] Error applying override, reverting to Python defaults: {e}")
            self.revert_to_python_defaults()

    def get_str(self, key: str, fallback: str = "") -> str:
        val = self.ui_strings.get(key, "")
        if val and str(val).strip():
            return str(val)
        default_val = self._python_defaults.get("ui_strings", {}).get(key, "")
        if default_val:
            return str(default_val)
        return fallback

    def render_calculator(self) -> None:
        """Invokes the modular calculator attached to this mode."""
        if callable(self.calculator_renderer):
            self.calculator_renderer()
        elif self.calculator_type == "realtor_mortgage":
            from modes.realtor.payment_calculator import render_realtor_payment_calculator
            render_realtor_payment_calculator()
        else:
            from modes.gaming.march_calculator import render_gaming_march_calculator
            render_gaming_march_calculator()

    def build_effective_system_prompt(self) -> str:
        sections = []
        if self.self_identity_description.strip():
            sections.append(f"[Self-Identity & Voice]:\n{self.self_identity_description.strip()}")
        if self.system_instruction.strip():
            sections.append(self.system_instruction.strip())
        if self.creator_relationship_instruction.strip():
            sections.append(f"[Creator & Sovereign Relationship Protocol]:\n{self.creator_relationship_instruction.strip()}")
        return "\n\n".join(sections)

    def render_dynamic_banner(self) -> str:
        if self.banner_html and "{header_title}" in self.banner_html:
            try:
                return self.banner_html.format(
                    header_title=self.header_title,
                    header_subtitle=self.header_subtitle,
                    gateway_status_text=self.gateway_status_text,
                )
            except Exception:
                pass
        return self.banner_html


class DomainConfig:
    """
    Central Mode Factory & Global Site-Wide Mode Router.
    Loads the dedicated mode subpackage under modes/<mode_key>/ and manages
    site-wide persistent mode switching for all users.
    """

    @classmethod
    def get_available_modes(cls, custom_modes_from_db: Optional[List[str]] = None) -> List[str]:
        holder = _get_global_site_state_holder()
        modes = list(MODE_PACKAGE_REGISTRY.keys())
        for m in holder.get("custom_modes", []):
            if m and m not in modes:
                modes.append(m)
        if custom_modes_from_db:
            for m in custom_modes_from_db:
                if m and m not in modes:
                    modes.append(m)
        return modes

    @classmethod
    def register_custom_mode(cls, mode_slug: str, db: Any = None) -> None:
        clean_slug = mode_slug.lower().strip().replace(" ", "_")
        if not clean_slug:
            return
        holder = _get_global_site_state_holder()
        if clean_slug not in holder["custom_modes"] and clean_slug not in MODE_PACKAGE_REGISTRY:
            holder["custom_modes"].append(clean_slug)
        try:
            with open(CUSTOM_MODES_FILE, "w", encoding="utf-8") as f:
                json.dump(holder["custom_modes"], f, indent=2)
            if db is not None and hasattr(db, "sync_json_file_to_vault"):
                db.sync_json_file_to_vault(CUSTOM_MODES_FILE, f"backups/{CUSTOM_MODES_FILE}", f"Registered custom mode {clean_slug}")
        except Exception as e:
            print(f"[Custom Mode Registry Notice]: {e}")

    @classmethod
    def get_active_mode_key(cls) -> str:
        """Returns the active mode for the ENTIRE deployment site (shared across all visitors)."""
        holder = _get_global_site_state_holder()
        return holder["active_mode"]

    @classmethod
    def set_active_mode_key(cls, new_mode: str, db: Any = None) -> str:
        """
        Persistently switches the active mode for ALL visitors on this site,
        writes site_state.json locally, and syncs it to GitHub db-vault.
        """
        clean_mode = new_mode.lower().strip().replace(" ", "_")
        if not clean_mode:
            return cls.get_active_mode_key()

        holder = _get_global_site_state_holder()
        holder["active_mode"] = clean_mode

        payload = {
            "active_mode": clean_mode,
            "deployment_namespace": get_deployment_namespace(),
            "updated_at": datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC"),
        }
        try:
            with open(SITE_STATE_FILE, "w", encoding="utf-8") as f:
                json.dump(payload, f, indent=2)
            if db is not None and hasattr(db, "sync_json_file_to_vault"):
                ns = get_deployment_namespace()
                db.sync_json_file_to_vault(
                    SITE_STATE_FILE,
                    f"backups/site_state_{ns}.json",
                    f"Switched deployment [{ns}] mode globally to '{clean_mode}'",
                )
        except Exception as e:
            print(f"[Global Mode Persist Notice]: {e}")

        return clean_mode

    @classmethod
    def load_base_profile_from_python_file(cls, mode_key: str = "") -> BotProfile:
        target_mode = (mode_key or cls.get_active_mode_key()).lower().strip()

        if target_mode in MODE_PACKAGE_REGISTRY:
            module_name, class_name = MODE_PACKAGE_REGISTRY[target_mode]
            try:
                mod = importlib.import_module(module_name)
                prof_cls = getattr(mod, class_name)
                profile: BotProfile = prof_cls()
            except Exception as e:
                print(f"Error importing {module_name}.{class_name}: {e}")
                from modes.gaming.profile import GamingProfile
                profile = GamingProfile()
        else:
            from modes.gaming.profile import GamingProfile
            profile = GamingProfile()
            profile.mode_key = target_mode
            profile.db_filename = f"{target_mode}_core.db"
            profile.page_title = f"{target_mode.title()} AI Core"
            profile.header_title = f"✨ {target_mode.title()} AI Core"
            profile.enable_discord = False
            profile.enable_auto_scout = False

        profile.snapshot_python_defaults()
        return profile

    @classmethod
    def load_profile(cls, mode_key: str = "", db: Any = None) -> BotProfile:
        profile = cls.load_base_profile_from_python_file(mode_key)
        if db is not None and hasattr(db, "get_mode_override"):
            overrides = db.get_mode_override(profile.mode_key)
            if overrides:
                profile.apply_overrides(overrides)
        return profile
