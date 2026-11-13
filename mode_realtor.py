"""
mode_realtor.py
Dedicated Mode File for APP_MODE = "realtor" (Aria — Premier Estates AI Concierge).
Encapsulates all Realtor mode displayed strings, persona rules, trusted real estate sources,
mortgage calculator settings, and buyer/seller seed guides.
"""

from profiles import BotProfile


class RealtorProfile(BotProfile):
    """Aria — Real Estate AI Concierge (Realtor & Property Advisory Mode)."""

    def __init__(self):
        super().__init__(
            mode_key="realtor",
            page_title="Premier Estates AI Concierge",
            page_icon="🏡",
            bot_name="Aria Concierge",
            header_title="🏡 Premier Estates — AI Property Concierge",
            header_subtitle="Residential & Commercial Advisory • Mortgage Analytics • Market Intelligence",
            gateway_status_text="Client Advisory Portal Active • Verified Market & Financing Library Online",
            creator_name="Aerith",
            creator_identifiers=["aerith", "aeri", "broker", "admin", "thebr0kenst4r"],
            db_filename="realtor_core.db",
            enable_discord=False,
            enable_discord_voice=False,
            enable_auto_learn=True,
            enable_auto_scout=False,
            enable_calculator=True,
            enable_lore_vault=True,
            enable_keep_alive_ping=True,
            backup_interval_hours=3.0,
            app_public_url="https://aurora-koa.streamlit.app",
            calculator_type="realtor_mortgage",
            access_tiers=["Everyone", "Agent Team", "Broker Admin", "Draft"],
            trusted_domains=[
                "nar.realtor",
                "consumerfinance.gov",
                "freddiemac.com",
                "fanniemae.com",
                "hud.gov",
                "en.wikipedia.org"
            ],
            banner_html="""
            <div style="background: linear-gradient(135deg, #064e3b 0%, #0f766e 50%, #1e293b 100%);
                        padding: 1.5rem; border-radius: 14px; border: 1px solid rgba(45, 212, 191, 0.35);
                        box-shadow: 0 8px 24px rgba(0,0,0,0.25); margin-bottom: 1.2rem;">
                <h2 style="margin: 0; color: #f8fafc; font-weight: 700; letter-spacing: 0.5px;">
                    {header_title}
                </h2>
                <p style="margin: 0.35rem 0 0 0; color: #ccfbf1; font-size: 1.0rem; font-weight: 500;">
                    {header_subtitle}
                </p>
                <p style="margin: 0.25rem 0 0 0; color: #5eead4; font-size: 0.85rem;">
                    ✨ {gateway_status_text}
                </p>
            </div>
            """,
            welcome_message="Welcome! I am Aria, your Real Estate & Property Concierge. Are you exploring buying, selling, mortgage financing, or local market trends today?",
            self_identity_description=(
                "You are Aria, an executive Real Estate & Property Advisory Concierge. "
                "Your demeanor is warm, trustworthy, financially articulate, and client-focused."
            ),
            creator_relationship_instruction=(
                "Aerith is your Managing Broker and Creator. Do not mention her name unprompted in routine client "
                "conversations unless asked about agency leadership. When Aerith herself is speaking to you, greet her "
                "with warm, enthusiastic professional respect."
            ),
            system_instruction=(
                "Core Directives:\n"
                "1. Client Service: Provide clear, structured guidance on buying, selling, staging, mortgage financing, "
                "closing costs, inspections, and market valuation.\n"
                "2. Financial Clarity: Break down Principal & Interest, Property Taxes, Insurance, PMI, and HOA fees clearly."
            ),
            about_creator_markdown=(
                "### 🌟 About the Managing Broker & Architect\n"
                "This Property Advisory Portal was architected by **Aerith** to deliver instant, transparent "
                "real estate checklists, mortgage analytics, and neighborhood market intelligence."
            ),
            ui_strings={
                "sidebar_header": "Client & Agent Portal",
                "guest_badge": "Browsing as Prospective Client",
                "member_badge": "🤝 Agent Team Access Verified",
                "admin_badge": "🔑 Managing Broker Admin: Aerith",
                "alliance_section_title": "🤝 Agent Team Access",
                "alliance_login_prompt": "Enter Agent Team Passkey:",
                "alliance_login_button": "Unlock Agent Portal",
                "sovereign_section_title": "🔑 Broker Admin Login",
                "sovereign_login_prompt": "Enter Managing Broker Key:",
                "sovereign_login_button": "Unlock Broker Command",
                "logout_button": "Sign Out",
                "public_menu_title": "Client Advisory Sanctum",
                "admin_menu_title": "Broker Command",
                "chat_nav": "💬 Property Concierge Chat",
                "chat_header": "AI Real Estate Concierge",
                "chat_caption": "Ask about buying, selling, closing timelines, inspections, or financing strategies.",
                "chat_input_placeholder": "Ask about homes, mortgages, closing costs, or market trends...",
                "clear_chat_button": "✨ Clear Conversation",
                "archives_nav": "📚 Buyer & Seller Guides",
                "archives_header": "Real Estate Advisory Library",
                "archives_caption": "Verified checklists for buyers, sellers, financing, and property inspections.",
                "calc_nav": "🏦 Mortgage & Payment Calculator",
                "calc_header": "Interactive Mortgage & Monthly Payment Estimator",
                "calc_caption": "Calculate monthly Principal & Interest, Property Taxes, Insurance, PMI, and HOA dues.",
                "vault_nav": "🎥 Property Tours & About",
                "vault_header": "Featured Video Tours, Tips & About Us",
                "vault_caption": "Home staging walkthroughs, real estate tips, and agency leadership.",
                "joke_button": "🏡 Draw Daily Real Estate Tip",
                "creator_tab_title": "🌟 About the Broker",
                "humor_tab_title": "💡 Daily Buyer & Seller Tips",
                "media_tab_title": "🎥 Property Tours & Staging",
                "forge_nav": "🛠️ Knowledge & Listing Forge",
                "forge_header": "Broker Knowledge Base & Guide Editor",
                "studio_nav": "🎨 Mode, String & Persona Studio",
                "studio_header": "Interactive Mode, String & Persona Studio",
                "memory_nav": "🗂️ Client CRM Inspector",
                "memory_header": "Client Profile & Conversation Notes Inspector",
                "silence_nav": "🤫 Channel Controls",
                "silence_header": "Channel Controls",
                "broadcast_nav": "📢 Team Broadcaster",
                "broadcast_header": "Team Broadcaster",
                "db_nav": "🗄️ Database & Cloud Backup",
                "db_header": "Database Vault, Auto-Backup & Watchdog Controls",
            },
            seed_docs=[
                ("buyer checklist", "First-Time Homebuyer Checklist", "Buying", "Everyone",
                 "1. Check credit score & debt-to-income (DTI) ratio.\n2. Obtain a formal Mortgage Pre-Approval letter.\n3. Budget 2%–5% of loan amount for closing costs in addition to your down payment.\n4. Prioritize neighborhood fundamentals, school districts, and resale liquidity.", "Aerith"),
                ("closing costs", "Understanding Buyer & Seller Closing Costs", "Financing", "Everyone",
                 "Buyer closing costs typically range from 2% to 5% of the purchase price (loan origination, appraisal, title insurance, escrow, prepaid taxes). Sellers typically cover agent commissions, transfer taxes, and negotiated repair credits.", "Aerith"),
                ("staging", "High-ROI Home Staging & Curb Appeal Guide", "Selling", "Everyone",
                 "Top Staging Priorities: Declutter surfaces by 50%, neutralize bold wall colors with warm whites/greiges, maximize natural light, pressure-wash walkways, and refresh front entry hardware and landscaping.", "Aerith"),
                ("cma", "Comparative Market Analysis (CMA) Protocol", "Agent SOP", "Agent Team",
                 "When preparing a CMA: Select 3–5 closed comps within 0.5 miles sold in the last 90 days with +/- 15% square footage. Adjust for lot size, garage bays, pool, and kitchen/bath remodel condition.", "Aerith")
            ],
            seed_jokes=[
                "Tip of the Day: The three most important factors in real estate are still Location, Location, and locking your interest rate at the right time.",
                "Home Staging Rule #1: You are selling the square footage and natural light, not your antique porcelain collection!",
                "Always inspect the roof and HVAC age before waiving any inspection contingencies."
            ],
            seed_media=[
                ("Modern Home Staging & Lighting Guide", "Staging", "https://www.youtube.com/watch?v=jfKfPfyJRdk", "Preparing your listing for photography")
            ]
        )