"""
modes/gaming/march_calculator.py
Modular 6-Element King of Avalon March & Planar Synergy Calculator
(Fire, Ice, Wind, Earth, Light, Dark) with Primary vs. Cushion troop breakdown.
"""

import streamlit as st


def render_gaming_march_calculator() -> None:
    """Renders the interactive King of Avalon March & 6-Element Planar Synergy Calculator."""
    col1, col2 = st.columns(2)
    with col1:
        march_cap = st.number_input(
            "Total March Capacity (Troops):",
            min_value=1000,
            max_value=10000000,
            value=350000,
            step=10000,
            key="gaming_calc_march_cap",
        )
        preset = st.selectbox(
            "Tactical Formation Preset:",
            [
                "Standard Balanced Rally (45% Inf / 27% Cav / 28% Bow)",
                "Heavy Sanctum / Garrison Anchor (55% Inf / 20% Cav / 25% Bow)",
                "High-Burst Flank Strike (38% Inf / 35% Cav / 27% Bow)",
                "Light / Dark Planar Burst (42% Inf / 30% Cav / 28% Bow)",
                "Golem / Single-Target Boss (1% Inf / 44% Cav / 55% Bow)",
                "Custom Ratio",
            ],
            key="gaming_calc_preset",
        )
    with col2:
        element_options = ["Fire 🔥", "Ice ❄️", "Wind 🌪️", "Earth 🪨", "Light ☀️", "Dark 🌑"]
        my_element = st.selectbox("Your Primary Planar Element:", element_options, key="gaming_calc_my_elem")
        enemy_element = st.selectbox("Target / Enemy Element:", element_options, key="gaming_calc_enemy_elem")

    if "Standard" in preset:
        inf_pct, cav_pct, bow_pct = 45, 27, 28
    elif "Heavy" in preset:
        inf_pct, cav_pct, bow_pct = 55, 20, 25
    elif "High-Burst" in preset:
        inf_pct, cav_pct, bow_pct = 38, 35, 27
    elif "Planar Burst" in preset:
        inf_pct, cav_pct, bow_pct = 42, 30, 28
    elif "Golem" in preset:
        inf_pct, cav_pct, bow_pct = 1, 44, 55
    else:
        c1, c2, c3 = st.columns(3)
        inf_pct = c1.slider("Infantry %", 1, 85, 45, key="gaming_calc_inf_slider")
        cav_pct = c2.slider("Cavalry %", 1, 80, 27, key="gaming_calc_cav_slider")
        bow_pct = max(0, 100 - inf_pct - cav_pct)
        c3.metric("Bowmen % (Auto-balanced)", f"{bow_pct}%")

    inf_count = int(march_cap * (inf_pct / 100.0))
    cav_count = int(march_cap * (cav_pct / 100.0))
    bow_count = march_cap - inf_count - cav_count

    st.subheader("🛡️ Computed Troop Allocation (Primary Tier vs. Cushion Tier)")
    m1, m2, m3 = st.columns(3)
    m1.metric(
        "🛡️ Infantry Frontline",
        f"{inf_count:,}",
        f"{inf_pct}% (Main: {int(inf_count * 0.75):,} | Cushion: {int(inf_count * 0.25):,})",
    )
    m2.metric(
        "🐎 Cavalry Flank",
        f"{cav_count:,}",
        f"{cav_pct}% (Main: {int(cav_count * 0.75):,} | Cushion: {int(cav_count * 0.25):,})",
    )
    m3.metric(
        "🏹 Bowmen Backline",
        f"{bow_count:,}",
        f"{bow_pct}% (Main: {int(bow_count * 0.80):,} | Cushion: {int(bow_count * 0.20):,})",
    )

    primal_counters = {
        "Ice ❄️": "Fire 🔥",
        "Fire 🔥": "Wind 🌪️",
        "Wind 🌪️": "Earth 🪨",
        "Earth 🪨": "Ice ❄️",
    }
    celestial_pair = {"Light ☀️", "Dark 🌑"}

    if {my_element, enemy_element} == celestial_pair:
        st.warning(
            f"⚡ **Celestial Duality Clash ({my_element} ↔ {enemy_element}):** "
            "Light and Dark mutually counter each other with amplified Planar Damage! "
            "Both marches deal heightened burst—reinforce Infantry frontline (+3–5%) and prioritize Planar Resistance & Penetration."
        )
    elif my_element in celestial_pair and enemy_element not in celestial_pair:
        st.success(
            f"✨ **Transcendent Planar Advantage ({my_element} vs {enemy_element}):** "
            f"{my_element} sits outside the 4-element primal cycle—immune to primal counter penalties while retaining full Planar Resonance."
        )
    elif enemy_element in celestial_pair and my_element not in celestial_pair:
        st.info(
            f"🌌 **Primal vs. Celestial Matchup ({my_element} vs {enemy_element}):** "
            f"{enemy_element} bypasses standard primal counter disadvantages. Rely on raw troop stats, Planar Resistance, and frontline cushion."
        )
    elif primal_counters.get(my_element) == enemy_element:
        st.success(
            f"🌟 **Elemental Advantage Active ({my_element} counters {enemy_element}):** "
            "+15% Planar Damage & Mitigation resonance!"
        )
    elif primal_counters.get(enemy_element) == my_element:
        st.warning(
            f"⚠️ **Elemental Disadvantage ({enemy_element} counters {my_element}):** "
            "Increase Infantry frontline cushion by +5% to absorb burst."
        )
    else:
        st.info(
            f"⚖️ **Neutral Elemental Matchup ({my_element} vs {enemy_element}):** "
            "Standard lethality and Planar Penetration multipliers apply."
        )

    st.caption(
        "**6-Element Planar Matrix:** `❄️ Ice > 🔥 Fire > 🌪️ Wind > 🪨 Earth > ❄️ Ice`  |  "
        "`☀️ Light ↔ 🌑 Dark` (Mutual Planar Counter; transcendent to the 4 primal elements)"
    )
