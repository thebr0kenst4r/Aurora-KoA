"""
modes/realtor/payment_calculator.py
Modular Mortgage & Monthly Payment Calculator for Realtor Mode.
Computes Principal & Interest, Down Payment ($ and %), Property Taxes,
Homeowner's Insurance, PMI, HOA dues, and Total Interest Paid over the loan term.
"""

import streamlit as st


def render_realtor_payment_calculator() -> None:
    """Renders the interactive Mortgage & Monthly Payment Estimator."""
    col1, col2, col3 = st.columns(3)
    with col1:
        home_price = st.number_input(
            "Home Purchase Price ($):",
            min_value=25000,
            max_value=25000000,
            value=450000,
            step=10000,
            key="realtor_calc_home_price",
        )
        down_pct = st.slider(
            "Down Payment (%):",
            min_value=0.0,
            max_value=80.0,
            value=20.0,
            step=0.5,
            key="realtor_calc_down_pct",
        )
        down_amount = home_price * (down_pct / 100.0)
        loan_amount = max(0.0, home_price - down_amount)
        st.caption(f"Down Payment: **${down_amount:,.0f}** | Loan Principal: **${loan_amount:,.0f}**")

    with col2:
        interest_rate = st.number_input(
            "Annual Interest Rate (APR %):",
            min_value=0.1,
            max_value=20.0,
            value=6.25,
            step=0.125,
            key="realtor_calc_interest_rate",
        )
        loan_term_years = st.selectbox(
            "Loan Term (Years):",
            [30, 20, 15, 10],
            index=0,
            key="realtor_calc_loan_term",
        )

    with col3:
        prop_tax_rate = st.number_input(
            "Annual Property Tax Rate (%):",
            min_value=0.0,
            max_value=6.0,
            value=1.15,
            step=0.05,
            key="realtor_calc_tax_rate",
        )
        annual_ins = st.number_input(
            "Annual Homeowner's Insurance ($):",
            min_value=0,
            max_value=25000,
            value=1500,
            step=100,
            key="realtor_calc_annual_ins",
        )
        monthly_hoa = st.number_input(
            "Monthly HOA / Condo Dues ($):",
            min_value=0,
            max_value=5000,
            value=0,
            step=25,
            key="realtor_calc_monthly_hoa",
        )

    n_months = loan_term_years * 12
    monthly_r = (interest_rate / 100.0) / 12.0

    if monthly_r > 0 and loan_amount > 0:
        monthly_pi = loan_amount * (monthly_r * (1 + monthly_r) ** n_months) / ((1 + monthly_r) ** n_months - 1)
    else:
        monthly_pi = loan_amount / n_months if n_months else 0.0

    monthly_tax = (home_price * (prop_tax_rate / 100.0)) / 12.0
    monthly_ins = annual_ins / 12.0
    monthly_pmi = ((loan_amount * 0.0055) / 12.0) if (down_pct < 20.0 and loan_amount > 0) else 0.0
    total_monthly = monthly_pi + monthly_tax + monthly_ins + monthly_pmi + monthly_hoa

    total_pi_paid = monthly_pi * n_months
    total_interest_paid = max(0.0, total_pi_paid - loan_amount)

    st.subheader(f"💵 Estimated Monthly Payment: **${total_monthly:,.2f} / month**")
    k1, k2, k3, k4, k5 = st.columns(5)
    k1.metric("Principal & Interest", f"${monthly_pi:,.2f}")
    k2.metric("Property Taxes", f"${monthly_tax:,.2f}")
    k3.metric("Home Insurance", f"${monthly_ins:,.2f}")
    k4.metric("Estimated PMI", f"${monthly_pmi:,.2f}", "0% at 20%+ down" if down_pct >= 20 else "Required (<20% down)")
    k5.metric("HOA Dues", f"${monthly_hoa:,.2f}")

    st.caption(
        f"**Loan Summary:** Total Principal & Interest over {loan_term_years} yrs: **${total_pi_paid:,.0f}**  |  "
        f"Total Interest Paid: **${total_interest_paid:,.0f}**  |  "
        f"Est. Buyer Closing Costs (3%): **${loan_amount * 0.03:,.0f}**"
    )
