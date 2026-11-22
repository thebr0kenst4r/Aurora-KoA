"""
modes/__init__.py
Dynamically discovers all built-in and Aurora-generated mode subpackages under modes/<mode_key>/.
Any subpackage containing `profile.py` with a `BotProfile` subclass is automatically registered.
"""

import os
from typing import Dict, Tuple

MODE_PACKAGE_REGISTRY: Dict[str, Tuple[str, str]] = {
    "gaming": ("modes.gaming.profile", "GamingProfile"),
    "realtor": ("modes.realtor.profile", "RealtorProfile"),
}


def discover_mode_packages() -> Dict[str, Tuple[str, str]]:
    """Scans `modes/` for both built-in and dynamically created mode subpackages."""
    discovered = dict(MODE_PACKAGE_REGISTRY)
    modes_dir = os.path.dirname(os.path.abspath(__file__))
    if not os.path.isdir(modes_dir):
        return discovered

    for entry in sorted(os.listdir(modes_dir)):
        sub_path = os.path.join(modes_dir, entry)
        profile_py = os.path.join(sub_path, "profile.py")
        if os.path.isdir(sub_path) and os.path.exists(profile_py) and entry not in discovered:
            expected_class = f"{entry.title().replace('_', '')}Profile"
            discovered[entry] = (f"modes.{entry}.profile", expected_class)
    return discovered
