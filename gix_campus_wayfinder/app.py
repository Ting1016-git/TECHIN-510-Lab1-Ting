"""
GIX Campus Wayfinder — Streamlit application for new GIX students.

Surfaces on-campus resources (makerspace, study spaces, printing, food, etc.)
with sidebar filters by category and keyword. No database or external APIs;
data lives in the RESOURCES list at the top of this file.
"""

import html

import streamlit as st

RESOURCES = [
    {
        "name": "Makerspace",
        "category": "Fabrication",
        "location": "GIX Building, Level 2 — Fabrication Lab",
        "hours": "Mon–Fri 9:00–20:00; weekends by reservation",
        "description": "Shared workshop with 3D printers, laser cutter, hand tools, and safety orientation for student projects.",
        "tags": ["3D printing", "laser cutter", "tools", "prototyping", "fabrication"],
        "contact": "makerspace@gix.uw.edu",
    },
    {
        "name": "Electronics Bench",
        "category": "Fabrication",
        "location": "GIX Building, Level 2 — adjacent to Makerspace",
        "hours": "Same as Makerspace",
        "description": "Soldering stations, oscilloscopes, and components for IoT and hardware coursework.",
        "tags": ["soldering", "electronics", "hardware", "oscilloscope"],
        "contact": "Room 2-210",
    },
    {
        "name": "Quiet Study Room",
        "category": "Study",
        "location": "GIX Building, Level 3 — east wing",
        "hours": "Building access hours (typically 7:00–22:00)",
        "description": "Small enclosed room for focused work; first-come seating when not reserved for classes.",
        "tags": ["quiet", "focus", "exam", "study"],
        "contact": "Front desk — Room 1-101",
    },
    {
        "name": "Collaborative Studio",
        "category": "Study",
        "location": "GIX Building, Level 1 — open studio",
        "hours": "Mon–Sun 8:00–23:00",
        "description": "Large tables and whiteboards for team meetings, design sprints, and group assignments.",
        "tags": ["teamwork", "whiteboard", "group project", "study"],
        "contact": "studio@gix.uw.edu",
    },
    {
        "name": "Free Printing Station",
        "category": "Printing",
        "location": "GIX Building, Level 1 — near student lounge",
        "hours": "Mon–Fri 8:00–18:00",
        "description": "Quota-based black-and-white printing for coursework; color jobs at the staffed print desk.",
        "tags": ["printer", "printing", "poster", "coursework"],
        "contact": "printhelp@gix.uw.edu",
    },
    {
        "name": "Large-Format Plotter",
        "category": "Printing",
        "location": "GIX Building, Level 1 — Print Services",
        "hours": "Tue–Thu 10:00–16:00",
        "description": "Poster and banner printing for showcases and final presentations; submit files via the portal.",
        "tags": ["printing", "poster", "plotter", "banner", "showcase"],
        "contact": "Room 1-115",
    },
    {
        "name": "Bike Storage",
        "category": "Storage",
        "location": "GIX Building, ground level — covered bike cage",
        "hours": "24/7 with building badge",
        "description": "Secure racks for bicycles; register your bike with facilities for long-term use.",
        "tags": ["bike", "bicycle", "storage", "commute"],
        "contact": "facilities@gix.uw.edu",
    },
    {
        "name": "Locker Room",
        "category": "Storage",
        "location": "GIX Building, Level B — locker bank",
        "hours": "Badge access aligned with term dates",
        "description": "Semester locker rentals for project kits, tools, and personal items.",
        "tags": ["locker", "storage", "kits", "semester"],
        "contact": "Room B-05",
    },
    {
        "name": "Coffee Station",
        "category": "Food & Drink",
        "location": "GIX Building, Level 1 — student kitchen",
        "hours": "Mon–Fri 7:30–19:00",
        "description": "Complimentary drip coffee and hot water; bring your own mug to reduce waste.",
        "tags": ["coffee", "kitchen", "break", "caffeine"],
        "contact": "studentlife@gix.uw.edu",
    },
    {
        "name": "Microwave & Vending Nook",
        "category": "Food & Drink",
        "location": "GIX Building, Level 1 — next to Coffee Station",
        "hours": "Microwave: building hours; vending: 24/7",
        "description": "Microwaves for meals and vending machines with snacks and drinks.",
        "tags": ["food", "microwave", "vending", "snacks"],
        "contact": "Front desk — Room 1-101",
    },
    {
        "name": "Shuttle & Transit Info Board",
        "category": "Transportation",
        "location": "GIX Building, main lobby",
        "hours": "Lobby open during building hours",
        "description": "Printed schedules for campus shuttles, Link light rail, and bike-share stations nearby.",
        "tags": ["shuttle", "bus", "light rail", "commute", "transportation"],
        "contact": "commute@uw.edu",
    },
    {
        "name": "IT Help Desk",
        "category": "Support",
        "location": "GIX Building, Level 1 — IT suite",
        "hours": "Mon–Fri 9:00–17:00",
        "description": "UW NetID, Wi‑Fi, lab software installs, and loaner laptops for short-term use.",
        "tags": ["IT", "wifi", "laptop", "software", "support"],
        "contact": "gix-it@uw.edu",
    },
    {
        "name": "Student Success & Advising",
        "category": "Support",
        "location": "GIX Building, Level 3 — student services",
        "hours": "Mon–Fri 10:00–16:00 (appointment recommended)",
        "description": "Academic advising, visa questions, and referrals to wellness and career resources.",
        "tags": ["advising", "visa", "wellness", "support", "career"],
        "contact": "advising@gix.uw.edu",
    },
]

# Assert: verify all resources have the required fields and non-empty values
required_fields = ["name", "category", "location", "hours", "description", "tags", "contact"]
for resource in RESOURCES:
    for field in required_fields:
        assert field in resource and resource[field], (
            f"Resource missing or empty field: '{field}' in {resource.get('name', 'unknown')}"
        )
assert len(RESOURCES) >= 12, "Must have at least 12 resources"

ALL_CATEGORIES = [
    "Fabrication",
    "Study",
    "Printing",
    "Storage",
    "Food & Drink",
    "Transportation",
    "Support",
]

BADGE_COLORS = {
    "Fabrication": "#1f77b4",
    "Study": "#2ca02c",
    "Printing": "#9467bd",
    "Storage": "#8c564b",
    "Food & Drink": "#e377c2",
    "Transportation": "#7f7f7f",
    "Support": "#bcbd22",
}


def filter_resources(resources, search_term, categories):
    """
    Return resources matching optional keyword search and category selection.

    Search matches name, description, and tags (case-insensitive substring).
    """
    filtered = []
    # EDGE CASE 1: Empty search input
    # If the search box is empty (or only whitespace), treat it as "no filter" — show all resources
    # Do NOT show zero results when user clears the search bar
    query = (search_term or "").strip().lower()
    use_text_filter = bool(query)

    for r in resources:
        if categories and r["category"] not in categories:
            continue
        if use_text_filter:
            in_name = query in r["name"].lower()
            in_desc = query in r["description"].lower()
            in_tags = any(query in tag.lower() for tag in r["tags"])
            if not (in_name or in_desc or in_tags):
                continue
        filtered.append(r)

    return filtered


def sort_resources_by_category_then_name(resources):
    """Sort alphabetically by name within each category group (category order follows ALL_CATEGORIES)."""
    by_cat = {c: [] for c in ALL_CATEGORIES}
    for r in resources:
        by_cat.setdefault(r["category"], []).append(r)
    for cat in by_cat:
        by_cat[cat].sort(key=lambda x: x["name"].lower())
    ordered = []
    for cat in ALL_CATEGORIES:
        ordered.extend(by_cat.get(cat, []))
    return ordered


def category_badge_html(category: str) -> str:
    cat_esc = html.escape(category)
    color = BADGE_COLORS.get(category, "#666666")
    return (
        f'<span style="background-color:{color};color:white;padding:0.2rem 0.6rem;'
        f'border-radius:0.4rem;font-size:0.85rem;white-space:nowrap;">{cat_esc}</span>'
    )


def render_category_badge_markdown(category: str) -> None:
    """Colored category pill via st.markdown() and inline HTML (one distinct color per category)."""
    st.markdown(category_badge_html(category), unsafe_allow_html=True)


def resource_card_expander(res: dict, *, expanded: bool) -> None:
    """
    Each resource in st.expander. The header label shows name and category (plain text).
    Colored category badges use st.markdown() with inline HTML (see render_category_badge_markdown).
    """
    header_label = f"{res['name']} · {res['category']}"
    with st.expander(header_label, expanded=expanded):
        render_category_badge_markdown(res["category"])
        st.write(f"**Location:** {res['location']}")
        st.write(f"**Hours:** {res['hours']}")
        st.write(f"**Contact:** {res['contact']}")
        st.write(res["description"])


def main():
    st.set_page_config(page_title="GIX Campus Wayfinder", layout="wide")
    st.title("GIX Campus Wayfinder")
    st.caption("Find campus resources without hunting down staff or relying on word-of-mouth.")

    with st.sidebar:
        st.header("Filters")
        selected_categories = st.multiselect(
            "Category",
            options=ALL_CATEGORIES,
            default=st.session_state.get("categories", list(ALL_CATEGORIES)),
            key="categories",
            help="Narrow results by resource type. Clear all to show every category.",
        )
        search = st.text_input(
            "Keyword search",
            value=st.session_state.get("search", ""),
            key="search",
            placeholder="e.g. print, bike, advising…",
            help="Searches resource names, descriptions, and tags.",
        )
        if st.sidebar.button("Clear Filters"):
            st.session_state["search"] = ""
            st.session_state["categories"] = list(ALL_CATEGORIES)
            st.rerun()

    matches = filter_resources(RESOURCES, search, selected_categories)
    sorted_matches = sort_resources_by_category_then_name(matches)

    st.metric("Matching resources", len(sorted_matches))
    st.divider()

    # EDGE CASE 2: Search term with no matches
    # If search returns zero results, show a friendly st.warning() message
    # Do NOT crash or show a blank page
    if len(sorted_matches) == 0:
        st.warning("No resources found. Try a different search term or category.")
    else:
        filtered = sorted_matches
        for i, resource in enumerate(filtered):
            with st.expander(f"{resource['name']} — {resource['category']}", expanded=(i < 3)):
                # category badge
                st.markdown(f"**Category:** {resource['category']}")
                st.write(f"**Location:** {resource['location']}")
                st.write(f"**Hours:** {resource['hours']}")
                st.write(f"**Contact:** {resource['contact']}")
                st.write(resource['description'])
            st.divider()


if __name__ == "__main__":
    main()
