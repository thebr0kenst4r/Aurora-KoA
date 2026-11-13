"""
profiles.py
Master Mode Registry & Base Profile Class.
Links each APP_MODE option ('gaming', 'realtor', etc.) to its dedicated Python module
(e.g., mode_gaming.py, mode_realtor.py).
Provides fallback-safe override layering so any string, persona description, trusted source,
or feature flag can be customized in the Admin Panel and stored in SQLite, while instantly
reverting to the hardcoded Python file defaults if reset or if an issue arises.
"""

import os
import copy
import importlib
from dataclasses import dataclass, field
from typing import List, Dict, Tuple, Any, Optional
import streamlit as st

# Master Registry mapping mode keys to their dedicated Python module & class name
MODE_REGISTRY: Dict[str, Tuple[str, str]] = {
    "gaming": ("mode_gaming", "GamingProfile"),
    "realtor": ("mode_realtor", "RealtorProfile"),
}


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


@dataclass
class BotProfile:
    """
    Base configuration class for all deployment modes.
    Subclasses in mode_gaming.py and mode_realtor.py define the authoritative Python defaults.
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

    # How the bot sees itself, talks, and relates to its Creator (Editable in Mode File & Admin Panel)
    self_identity_description: str = ""
    creator_relationship_instruction: str = ""
    system_instruction: str = ""
    about_creator_markdown: str = ""

    # Trusted Web Domains (Requires only 1 match from these domains for instant verification)
    trusted_domains: List[str] = field(default_factory=list)
    access_tiers: List[str] = field(
        default_factory=lambda: ["Everyone", "Alliance Only", "Sovereign Only", "Draft"]
    )

    # Feature & Automation Toggles (Defined in mode Python file, overridable in Admin Panel)
    enable_discord: bool = True
    enable_discord_voice: bool = True
    enable_auto_learn: bool = True
    enable_auto_scout: bool = True
    enable_calculator: bool = True
    enable_lore_vault: bool = True
    enable_keep_alive_ping: bool = True
    backup_interval_hours: float = 3.0
    app_public_url: str = "https://aurora-koa.streamlit.app"
    calculator_type: str = "gaming_march"  # "gaming_march" or "realtor_mortgage"

    # Displayed UI Strings Dictionary (Every title, subtitle, button, and input box on the site)
    ui_strings: Dict[str, str] = field(default_factory=dict)

    # Initial SQLite Seed Data
    seed_docs: List[Tuple[str, str, str, str, str, str]] = field(default_factory=list)
    seed_jokes: List[str] = field(default_factory=list)
    seed_media: List[Tuple[str, str, str, str]] = field(default_factory=list)

    # Internal Snapshot of Python File Defaults for Instant Safe Fallback
    _python_defaults: Dict[str, Any] = field(default_factory=dict, init=False, repr=False)

    def snapshot_python_defaults(self) -> None:
        """Records the pristine Python file defaults before any database overrides are applied."""
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
        """Restores all attributes and UI strings to the original values in the mode Python file."""
        if not self._python_defaults:
            return
        for k, v in self._python_defaults.items():
            setattr(self, k, copy.deepcopy(v))

    def get_python_default(self, attr_or_str_key: str) -> Any:
        """Returns the original hardcoded Python file value for any attribute or UI string key."""
        if attr_or_str_key in self._python_defaults:
            return self._python_defaults[attr_or_str_key]
        return self._python_defaults.get("ui_strings", {}).get(attr_or_str_key, "")

    def apply_overrides(self, overrides: Dict[str, Any]) -> None:
        """
        Safely applies database-stored admin overrides on top of the Python file defaults.
        If any field is malformed, empty, or raises an error, falls back to the Python default.
        """
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
                "app_public_url", "calculator_type"
            ]
            for attr in scalar_attrs:
                if attr in overrides and overrides[attr] is not None:
                    val = overrides[attr]
                    if isinstance(val, str) and val.strip() == "":
                        continue  # Empty string falls back to Python default
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
        """
        Central string accessor used by all UI components.
        Checks active (possibly overridden) ui_strings first, then Python file defaults, then fallback.
        """
        val = self.ui_strings.get(key, "")
        if val and str(val).strip():
            return str(val)
        default_val = self._python_defaults.get("ui_strings", {}).get(key, "")
        if default_val:
            return str(default_val)
        return fallback

    def build_effective_system_prompt(self) -> str:
        """Combines self-identity, core system instructions, and creator relationship rules."""
        sections = []
        if self.self_identity_description.strip():
            sections.append(f"[Self-Identity & Voice]:\n{self.self_identity_description.strip()}")
        if self.system_instruction.strip():
            sections.append(self.system_instruction.strip())
        if self.creator_relationship_instruction.strip():
            sections.append(f"[Creator & Sovereign Relationship Protocol]:\n{self.creator_relationship_instruction.strip()}")
        return "\n\n".join(sections)

    def render_dynamic_banner(self) -> str:
        """Renders the top HTML banner using the current customizable header_title, subtitle, and gateway text."""
        if self.banner_html and "{header_title}" in self.banner_html:
            try:
                return self.banner_html.format(
                    header_title=self.header_title,
                    header_subtitle=self.header_subtitle,
                    gateway_status_text=self.gateway_status_text
                )
            except Exception:
                pass
        return self.banner_html


class DomainConfig:
    """
    Central Mode Factory & Router.
    Loads the dedicated Python file for the active mode (e.g., mode_gaming.py or mode_realtor.py)
    and merges any active SQLite overrides.
    """

    @classmethod
    def get_available_modes(cls, custom_modes_from_db: Optional[List[str]] = None) -> List[str]:
        modes = list(MODE_REGISTRY.keys())
        if custom_modes_from_db:
            for m in custom_modes_from_db:
                if m not in modes:
                    modes.append(m)
        return modes

    @classmethod
    def get_active_mode_key(cls) -> str:
        if "session_app_mode_override" in st.session_state and st.session_state.session_app_mode_override:
            return str(st.session_state.session_app_mode_override).lower().strip()
        secret_mode = get_secret_or_env("APP_MODE", "gaming").lower().strip()
        return secret_mode if secret_mode else "gaming"

    @classmethod
    def load_base_profile_from_python_file(cls, mode_key: str = "") -> BotProfile:
        """
        Imports the dedicated Python file for the given mode_key and instantiates its Profile class,
        recording a pristine snapshot of its Python file defaults.
        """
        target_mode = (mode_key or cls.get_active_mode_key()).lower().strip()

        if target_mode in MODE_REGISTRY:
            module_name, class_name = MODE_REGISTRY[target_mode]
            try:
                mod = importlib.import_module(module_name)
                prof_cls = getattr(mod, class_name)
                profile: BotProfile = prof_cls()
            except Exception as e:
                print(f"Error importing {module_name}.{class_name}: {e}")
                from mode_gaming import GamingProfile
                profile = GamingProfile()
        else:
            # Custom mode created in Admin Panel inherits GamingProfile structure with its own DB file
            from mode_gaming import GamingProfile
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
        """Loads the Python file profile and applies any saved SQLite overrides if db is provided."""
        profile = cls.load_base_profile_from_python_file(mode_key)
        if db is not None and hasattr(db, "get_mode_override"):
            overrides = db.get_mode_override(profile.mode_key)
            if overrides:
                profile.apply_overrides(overrides)
        return profile