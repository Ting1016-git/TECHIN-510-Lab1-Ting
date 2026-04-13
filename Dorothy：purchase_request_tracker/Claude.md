# Project Context

## What This Project Is
A Streamlit web app that manages purchase requests for GIX 
(University of Washington Global Innovation Exchange). Built 
to replace Dorothy's manual Excel-based workflow on OneDrive.

## Tech Stack
- Python 3.11+
- Streamlit for the web interface
- SQLite for persistent storage
- Plotly for interactive charts
- Gmail SMTP for email notifications

## User Roles
- Student (CFO): submit requests, view history, edit/withdraw
- Instructor: register courses and budgets, approve/reject requests
- Admin (Dorothy): final approval, mark received, manage windows

## Key Business Rules
- Two-step approval: Instructor first → Dorothy final
- Weekly submission window: closes every Monday at 1PM
- Real-time budget check: auto-block over-budget submissions
- Records retained for 5 years, never deleted only archived
- GPA minimum 2.7 for course petitions (separate feature)

## Development Commands
- Run the app: `streamlit run app.py`
- Install dependencies: `pip install -r requirements.txt`
- Activate venv: `source .venv/bin/activate`

## File Structure
- `app.py` — Main application (~1500+ lines, single-file)
- `purchases.db` — SQLite database
- `.cursorrules` — Cursor AI configuration

## Important Notes
- This is a TECHIN 510 course project at UW GIX
- Target users: GIX students, instructors, and admin staff
- Always verify the app still runs after any changes
- Use UW purple (#4b2e83) for all UI elements
- Never use Altair or Matplotlib — Plotly only for charts