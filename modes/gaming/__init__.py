"""
modes/gaming/__init__.py
Exports GamingProfile (Aurora) and the modular King of Avalon March Calculator.
"""

from modes.gaming.march_calculator import render_gaming_march_calculator
from modes.gaming.profile import GamingProfile

__all__ = ["GamingProfile", "render_gaming_march_calculator"]
