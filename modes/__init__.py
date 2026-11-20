"""
modes/__init__.py
Registry mapping mode keys to their dedicated subpackages under modes/<mode_key>/.
"""

MODE_PACKAGE_REGISTRY = {
    "gaming": ("modes.gaming.profile", "GamingProfile"),
    "realtor": ("modes.realtor.profile", "RealtorProfile"),
}
