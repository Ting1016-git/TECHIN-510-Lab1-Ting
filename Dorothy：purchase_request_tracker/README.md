# Purchase Request Tracker (Streamlit + SQLite)

## Run
```bash
cd "/Users/limengting/Desktop/purchase_request_tracker"
pip install -r requirements.txt
streamlit run app.py
```

## Data storage
SQLite database file: `prt_data/purchase_requests.sqlite3`

Optional env override:
`PRTR_DB_PATH=/path/to/purchase_requests.sqlite3`

## Pages
1. **Submit Purchase Request** (CFOs): submit with real-time budget remaining + budget enforcement.
2. **Admin Dashboard** (Dorothy & Liao): filter + approve/reject with one click; budget summary per team.
3. **Received Confirmation**: mark approved orders as received; optional return flag/reason.
4. **Summary & Export**: totals per team + CSV export + archived history view.

