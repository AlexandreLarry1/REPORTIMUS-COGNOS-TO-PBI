"""
Synthetic dataset for the 'Analyse des clients' model (insurance customer analytics).
Reproducible (seeded). Builds 5 referentially-consistent tables that share keys:
State, Vehicle Class, Month/Month Order, Renew Offer Type.

Grains:
  Customer Analysis : one row per customer            (fact)
  Targets           : one row per Vehicle Class       (lookup)
  Offers            : State x Offer x Vehicle Class    (aggregate)
  Renewals          : State x Renew Offer Type x Month (aggregate)
  Policy Holders    : State x Month                    (aggregate)
"""
import numpy as np
import pandas as pd

rng = np.random.default_rng(42)          # <- single seed = fully reproducible
N = 2000                                  # number of customers
OUT = "/mnt/user-data/outputs"

# ---- shared dimension members (the foreign keys every table must agree on) ----
STATES   = ["Arizona", "California", "Nevada", "Oregon", "Washington"]
VCLASS   = ["Two-Door Car", "Four-Door Car", "SUV", "Sports Car", "Luxury Car", "Luxury SUV"]
VSIZE_OF = {"Two-Door Car": "Small", "Four-Door Car": "Medsize", "SUV": "Large",
            "Sports Car": "Small", "Luxury Car": "Medsize", "Luxury SUV": "Large"}
MONTHS   = ["January","February","March","April","May","June",
            "July","August","September","October","November","December"]
MONTH_ORDER = {m: i + 1 for i, m in enumerate(MONTHS)}
OFFERS   = ["Offer1", "Offer2", "Offer3", "Offer4"]
YEAR     = 2024

# relative CLTV / premium weight per vehicle class (luxury worth more)
VCLASS_W = {"Two-Door Car": 0.85, "Four-Door Car": 1.00, "SUV": 1.20,
            "Sports Car": 1.35, "Luxury Car": 1.70, "Luxury SUV": 1.90}

# ============================================================================
# 1) CUSTOMER ANALYSIS  (fact table, customer grain)
# ============================================================================
state   = rng.choice(STATES,  N, p=[.18, .40, .12, .15, .15])   # CA dominates, like the real sample
vclass  = rng.choice(VCLASS,  N, p=[.20, .34, .20, .10, .09, .07])
vsize   = np.array([VSIZE_OF[c] for c in vclass])
w       = np.array([VCLASS_W[c] for c in vclass])

income          = rng.lognormal(mean=10.6, sigma=0.45, size=N).clip(12000, 120000).round(0)
monthly_premium = (60 + income / 1200 + (w - 1) * 55 + rng.normal(0, 12, N)).clip(50, 300).round(0)
months_incept   = rng.integers(1, 100, N)
months_lastclaim = rng.integers(0, 36, N)
n_complaints    = rng.choice([0, 1, 2, 3, 4, 5], N, p=[.74, .12, .07, .04, .02, .01])
n_policies      = rng.integers(1, 10, N)

# CLTV: driven by income, vehicle-class weight, tenure, dampened by complaints; + noise
cltv = (1800
        + income * 0.18
        + w * 4200
        + months_incept * 35
        - n_complaints * 600
        + rng.normal(0, 1500, N)).clip(2000, 65000).round(2)

total_claim = (monthly_premium * rng.uniform(1.5, 5.0, N) + rng.normal(0, 120, N)).clip(50, 3200).round(2)
# min/max claim bracket the total (a single customer's claim spread)
min_claim = (total_claim * rng.uniform(0.05, 0.25, N)).round(2)
max_claim = (total_claim * rng.uniform(0.40, 0.95, N)).round(2)
# a derived measure: lifetime premiums paid minus claims (demonstrates a computed column)
prem_over_claim = (monthly_premium * months_incept - total_claim).round(2)

# expiry date spread across the year -> derive Month name + numeric key (good-practice keys)
expiry_doy   = rng.integers(0, 365, N)
expiry_date  = pd.to_datetime(f"{YEAR}-01-01") + pd.to_timedelta(expiry_doy, unit="D")
expiry_month = expiry_date.month_name()
month_key    = expiry_date.year * 100 + expiry_date.month     # YYYYMM, e.g. 202403

policy_type  = rng.choice(["Personal Auto", "Corporate Auto", "Special Auto"], N, p=[.75, .19, .06])
policy_level = np.array([f"{pt.split()[0]} L{lvl}"
                         for pt, lvl in zip(policy_type, rng.integers(1, 4, N))])

customer = pd.DataFrame({
    "Customer ID": [f"CU{100000 + i}" for i in range(N)],
    "Country": "United States",
    "State": state,
    "Customer Lifetime Value": cltv,
    "Coverage": rng.choice(["Basic", "Extended", "Premium"], N, p=[.55, .32, .13]),
    "Education": rng.choice(["High School or Below", "College", "Bachelor", "Master", "Doctor"],
                            N, p=[.30, .30, .25, .12, .03]),
    "Expiry Date": expiry_date.strftime("%Y-%m-%d"),
    "Employment Status": rng.choice(["Employed", "Unemployed", "Medical Leave", "Disabled", "Retired"],
                                    N, p=[.62, .15, .08, .08, .07]),
    "Gender": rng.choice(["F", "M"], N),
    "Income": income.astype(int),
    "Location Type": rng.choice(["Rural", "Suburban", "Urban"], N, p=[.19, .60, .21]),
    "Marital Status": rng.choice(["Single", "Married", "Divorced"], N, p=[.30, .55, .15]),
    "Monthly Premium Auto": monthly_premium.astype(int),
    "Months Since Last Claim": months_lastclaim,
    "Months Since Policy Inception": months_incept,
    "Number of Open Complaints": n_complaints,
    "Number of Policies": n_policies,
    "Policy Type": policy_type,
    "Policy Level": policy_level,
    "Renew Offer Type": rng.choice(OFFERS, N, p=[.42, .30, .18, .10]),
    "Sales Channel": rng.choice(["Agent", "Branch", "Call Center", "Web"], N, p=[.38, .27, .20, .15]),
    "Total Claim Amount": total_claim,
    "Min Claim Amount": min_claim,
    "Max Claim Amount": max_claim,
    "Vehicle Class": vclass,
    "Vehicle Size": vsize,
    "Premiums Over Claim": prem_over_claim,
    "Month Key": month_key.astype(int),
    "Expiry Month": expiry_month,
})

# ============================================================================
# 2) TARGETS  (lookup, Vehicle Class grain) -- consumed by the report by Vehicle Class
# ============================================================================
# 'Value' = actual avg CLTV per class (reconciles to the fact table); 'Target' a bit above it.
actual_by_class = customer.groupby("Vehicle Class")["Customer Lifetime Value"].mean().round(0)
targets = pd.DataFrame({"Vehicle Class": VCLASS})
targets["Value"]         = targets["Vehicle Class"].map(actual_by_class).astype(int)
targets["Target"]        = (targets["Value"] * 1.08).round(0).astype(int)
targets["Minimum Range"] = (targets["Value"] * 0.70).round(0).astype(int)
targets["Middle Range"]  = (targets["Value"] * 1.00).round(0).astype(int)
targets["Maximum Range"] = (targets["Value"] * 1.30).round(0).astype(int)
# State/Month columns exist in the module but the report uses this table at class grain only;
# we mark them as a whole-year, all-region snapshot and flag that in the data dictionary.
targets["State"]      = "All Regions"
targets["Month"]      = "Full Year"
targets["Month Order"] = 0
targets["Year"]       = YEAR
targets = targets[["State", "Vehicle Class", "Minimum Range", "Middle Range",
                   "Maximum Range", "Target", "Value", "Month", "Month Order", "Year"]]

# ============================================================================
# 3) OFFERS  (aggregate, State x Offer x Vehicle Class)
# ============================================================================
rows = []
for s in STATES:
    for o in OFFERS:
        for c in VCLASS:
            base = rng.integers(5, 60)
            acc_rate = {"Offer1": .55, "Offer2": .42, "Offer3": .30, "Offer4": .18}[o]
            accepted = int(round(base * acc_rate))
            declined = base - accepted
            rows.append([s, o, c, accepted, declined])
offers = pd.DataFrame(rows, columns=["State", "Offer", "Vehicle Class", "Accepted", "Declined"])

# ============================================================================
# 4) RENEWALS  (aggregate, State x Renew Offer Type x Month)
# ============================================================================
rows = []
for s in STATES:
    for o in OFFERS:
        for m in MONTHS:
            base = rng.integers(8, 80)
            acc_rate = {"Offer1": .55, "Offer2": .42, "Offer3": .30, "Offer4": .18}[o]
            seasonal = 1 + 0.20 * np.sin((MONTH_ORDER[m] - 1) / 12 * 2 * np.pi)  # mild seasonality
            accepted = int(round(base * acc_rate * seasonal))
            declined = int(round(base * (1 - acc_rate)))
            rows.append([s, o, m, MONTH_ORDER[m], accepted, declined])
renewals = pd.DataFrame(rows, columns=["State", "Renew Offer Type", "Month",
                                       "Month Order", "Accepted", "Declined"])

# ============================================================================
# 5) POLICY HOLDERS  (aggregate, State x Month) -- 'Delta' = monthly net change
# ============================================================================
rows = []
for s in STATES:
    level = rng.integers(800, 5000)            # starting book of policies in that state
    for m in MONTHS:
        delta = int(rng.normal(15, 60))         # net monthly gain/loss
        level += delta
        rows.append([s, m, MONTH_ORDER[m], delta])
policy_holders = pd.DataFrame(rows, columns=["State", "Month", "Month Order", "Delta"])

# ---- write out ----
customer.to_csv(f"{OUT}/customer_analysis.csv", index=False)
targets.to_csv(f"{OUT}/targets.csv", index=False)
offers.to_csv(f"{OUT}/offers.csv", index=False)
renewals.to_csv(f"{OUT}/renewals.csv", index=False)
policy_holders.to_csv(f"{OUT}/policy_holders.csv", index=False)

# ---- referential-integrity checks (good practice: assert, don't assume) ----
assert set(offers["State"]) <= set(STATES)
assert set(renewals["State"]) <= set(STATES)
assert set(policy_holders["State"]) <= set(STATES)
assert set(offers["Vehicle Class"]) <= set(VCLASS)
assert set(targets["Vehicle Class"]) == set(VCLASS)
assert set(customer["Renew Offer Type"]) <= set(OFFERS)
assert set(renewals["Renew Offer Type"]) <= set(OFFERS)
assert customer["Customer ID"].is_unique
assert targets["Vehicle Class"].is_unique

print("Rows:  customer=%d  targets=%d  offers=%d  renewals=%d  policy_holders=%d"
      % (len(customer), len(targets), len(offers), len(renewals), len(policy_holders)))
print("Avg CLTV by class (fact) vs Target value (lookup) reconcile:")
print(targets[["Vehicle Class", "Value", "Target"]].to_string(index=False))
print("\nIntegrity checks passed.")