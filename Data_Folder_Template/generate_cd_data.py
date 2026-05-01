import pandas as pd  # type: ignore
import numpy as np  # type: ignore
from datetime import datetime, timedelta
import os

rng = np.random.default_rng(42)
N = 20000

today = datetime(2026, 4, 30)

months_choices = np.array([3,6,9,12,18,24,36,48,60])
branches = [f"BR-{i:03d}" for i in range(1,151)]
branch_to_id = {b:int(b.split('-')[1]) for b in branches}

payout_map = {"MONTHLY":"M","QUARTERLY":"Q","AT_MATURITY":"A"}
rate_type_map = {"FIXED":"F","VARIABLE":"V"}
bool_map = {"YES":1,"NO":0}
status_map = {"ACTIVE":"A","MATURED":"M","CLOSED":"C"}
accrual_map = {"30_360":"30360","ACT_365":"ACT365"}

cust_nums = rng.integers(1, 8000, size=N)
customer_id = [f"CUST{n:06d}" for n in cust_nums]
cd_keys = np.arange(1000001, 1000001+N)
cd_number = [f"CD-{k}" for k in cd_keys]

start = datetime(2020,1,1)
end = datetime(2026,3,31)
issue_date = [start + timedelta(days=int(d)) for d in rng.integers(0,(end-start).days,size=N)]
term_months = rng.choice(months_choices, size=N)

principal = np.round(rng.uniform(500,250000,size=N),2)
rate = np.round(np.clip(0.8 + (term_months/60)*3.2 + rng.normal(0,0.35,N),0.25,5.25),3)

payout = rng.choice(list(payout_map.keys()), size=N)
rate_type = rng.choice(list(rate_type_map.keys()), size=N)
auto_renew = rng.choice(["YES","NO"], size=N, p=[0.7,0.3])
brokered = rng.choice(["YES","NO"], size=N, p=[0.15,0.85])
accrual = rng.choice(list(accrual_map.keys()), size=N)
branch = rng.choice(branches, size=N)

maturity = [d + timedelta(days=int(m*30.4)) for d,m in zip(issue_date,term_months)]
status = ["MATURED" if m < today else "ACTIVE" for m in maturity]

src = pd.DataFrame({
    "Customer_ID": customer_id,
    "CD_Number": cd_number,
    "Issue_Date": [d.strftime("%Y-%m-%d") for d in issue_date],
    "Term_Months": term_months,
    "Principal_Amount": principal,
    "Interest_Rate": rate,
    "Interest_Payout_Frequency": payout,
    "Rate_Type": rate_type,
    "Auto_Renew": auto_renew,
    "Status": status,
    "Branch_Code": branch,
    "Currency": "USD",
    "Interest_Accrual_Method": accrual,
    "Brokered_Flag": brokered
})

dest = pd.DataFrame({
    "cust_key": [int(c.replace("CUST","")) for c in src.Customer_ID],
    "cd_key": [int(c.replace("CD-","")) for c in src.CD_Number],
    "issue_dt": [datetime.strptime(d,"%Y-%m-%d").strftime("%d-%b-%Y").upper() for d in src.Issue_Date],
    "term_mo": src.Term_Months,
    "principal_amt": src.Principal_Amount,
    "rate_pct": src.Interest_Rate,
    "payout_freq_cd": [payout_map[v] for v in src.Interest_Payout_Frequency],
    "rate_type_cd": [rate_type_map[v] for v in src.Rate_Type],
    "auto_renew_ind": [bool_map[v] for v in src.Auto_Renew],
    "status_cd": [status_map[v] for v in src.Status],
    "branch_id": [branch_to_id[v] for v in src.Branch_Code],
    "ccy_cd": "USD",
    "accrual_cd": [accrual_map[v] for v in src.Interest_Accrual_Method],
    "brokered_ind": [bool_map[v] for v in src.Brokered_Flag]
})

os.makedirs("cd_sample_20k", exist_ok=True)
src.to_csv("cd_sample_20k/cd_source.csv", index=False)
dest.to_csv("cd_sample_20k/cd_destination.csv", index=False)
print("Done: 20K CD data generated in ./cd_sample_20k/")