# GIX Campus Wayfinder

A small [Streamlit](https://streamlit.io/) app that helps new GIX students find on-campus resources—makerspace, study spaces, printing, food, storage, transportation, and support—without hunting down staff or relying on word-of-mouth.

Resource data is stored in memory (`RESOURCES` in `app.py`). There is no database or external API.

## Features

- **Sidebar filters:** choose one or more categories and search by keyword (matches name, description, and tags).
- **Clear Filters:** resets the search box and restores all categories.
- **Results:** each resource appears in an expander with category, location, hours, contact, and description. The first three matches are expanded by default.

## Requirements

- Python 3.9+ (recommended)
- [Streamlit](https://docs.streamlit.io/)

Install Streamlit if needed:

```bash
pip install streamlit
```

## Run locally

From this directory:

```bash
cd gix_campus_wayfinder
streamlit run app.py
```

Streamlit will print a local URL (usually `http://localhost:8501`). Open it in your browser.

## Project layout

| File    | Purpose                                      |
|---------|----------------------------------------------|
| `app.py` | Streamlit UI, filters, and resource listing |

## License

Use and modify as needed for coursework or personal projects; attribute appropriately if you reuse it publicly.
