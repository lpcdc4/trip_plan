# streamlit_app.py
#
# Streamlit itinerary planner (OSM + OSRM) + Supabase Sync
#
# Install:
#   pip install streamlit folium streamlit-folium requests polyline streamlit-searchbox streamlit-sortables supabase
#
# Run locally:
#   streamlit run streamlit_app.py

import json
import re
import uuid
import time
from typing import Dict, List, Optional, Tuple
from datetime import date, timedelta, datetime

import requests
import streamlit as st
import folium
from streamlit_folium import st_folium
import polyline as polyline_lib
from streamlit_searchbox import st_searchbox
from supabase import create_client, Client

# ---------- optional drag & drop dependency ----------
HAS_SORTABLES = False
sort_items = None
try:
    from streamlit_sortables import sort_items as _sort_items
    sort_items = _sort_items
    HAS_SORTABLES = True
except Exception:
    HAS_SORTABLES = False
    sort_items = None

# ----------------------- External services -----------------------
NOMINATIM_SEARCH = "https://nominatim.openstreetmap.org/search"
OSRM_ROUTE = "https://router.project-osrm.org/route/v1/driving/{lon1},{lat1};{lon2},{lat2}"
DEFAULT_USER_AGENT = "my-trip-planner-app-v1 (contact: myemail@example.com)"

# ----------------------- Supabase Setup -----------------------
# We try to grab secrets from st.secrets (Streamlit Cloud) or fail gracefully
try:
    SUPABASE_URL = st.secrets["SUPABASE_URL"]
    SUPABASE_KEY = st.secrets["SUPABASE_KEY"]
    supabase: Client = create_client(SUPABASE_URL, SUPABASE_KEY)
except Exception:
    st.error("Missing Supabase secrets. Please set SUPABASE_URL and SUPABASE_KEY in .streamlit/secrets.toml")
    st.stop()

# ----------------------- Session / DB Sync -----------------------

def get_trip_id_from_url():
    """Get trip_id from query params or generate a new one."""
    qp = st.query_params
    if "trip_id" in qp:
        return qp["trip_id"]
    return None

def load_from_supabase(trip_id: str):
    """Fetch JSON blob from Supabase."""
    try:
        response = supabase.table("itineraries").select("trip_data").eq("trip_id", trip_id).execute()
        if response.data and len(response.data) > 0:
            return response.data[0]["trip_data"]
    except Exception as e:
        st.error(f"Error loading from database: {e}")
    return None

def save_to_supabase():
    """Push current state to Supabase."""
    ss = st.session_state
    if not ss.get("dirty", False):
        return

    trip_id = ss["current_trip_id"]
    
    # Prepare data for JSONB
    data_to_save = {
        "name": ss["trip_name"],
        "trip_start_date": ss["trip_start_date"].isoformat(),
        "stops": ss["stops"],
        "legs_between": ss["legs_between"],
        "last_updated": datetime.now().isoformat()
    }

    try:
        supabase.table("itineraries").upsert({
            "trip_id": trip_id, 
            "trip_data": data_to_save
        }).execute()
        
        ss["dirty"] = False
        ss["last_synced"] = datetime.now()
    except Exception as e:
        st.warning(f"Sync failed: {e}")

def init_state():
    ss = st.session_state
    
    # Check if we already initialized
    if "initialized" in ss:
        return

    # 1. Determine Trip ID
    url_id = get_trip_id_from_url()
    
    if url_id:
        # Try loading existing
        data = load_from_supabase(url_id)
        if data:
            ss["current_trip_id"] = url_id
            populate_state_from_data(data)
        else:
            # ID in URL but not in DB -> Treat as new
            ss["current_trip_id"] = url_id
            set_defaults()
    else:
        # No ID -> Generate one
        new_id = str(uuid.uuid4())[:8] # Short ID
        ss["current_trip_id"] = new_id
        set_defaults()
        # Set URL param so user can bookmark/share immediately
        st.query_params["trip_id"] = new_id

    ss["initialized"] = True

def set_defaults():
    ss = st.session_state
    ss.setdefault("trip_name", "My Trip")
    ss.setdefault("user_agent", DEFAULT_USER_AGENT)
    ss.setdefault("trip_start_date", date.today())
    ss.setdefault("stops", [])
    ss.setdefault("legs_between", [])
    ss.setdefault("next_stop_id", 1)
    
    # UI helpers
    ss.setdefault("map_center", None)
    ss.setdefault("map_version", 0)
    ss.setdefault("dirty", True) # Force initial save to create row
    ss.setdefault("search_lookup", {})
    ss.setdefault("last_selected_label", None)
    ss.setdefault("search_key_version", 0)
    ss.setdefault("sortable_key_version", 0)
    ss.setdefault("sortable_items_cache", None)
    ss.setdefault("pending_stop", None)
    ss.setdefault("pending_preview", None)
    ss.setdefault("show_editor", False)

def populate_state_from_data(data: dict):
    ss = st.session_state
    ss["trip_name"] = data.get("name", "Imported Trip")
    
    tsd = data.get("trip_start_date")
    if tsd:
        try:
            ss["trip_start_date"] = date.fromisoformat(tsd)
        except:
            ss["trip_start_date"] = date.today()
    else:
        ss["trip_start_date"] = date.today()

    ss["stops"] = data.get("stops", [])
    ss["legs_between"] = data.get("legs_between", [])
    
    # Recalculate next ID
    mx = 0
    for s in ss["stops"]:
        sid = str(s.get("id", ""))
        if sid.startswith("S"):
            try:
                mx = max(mx, int(sid[1:]))
            except: pass
    ss["next_stop_id"] = mx + 1

    # Default UI states
    ss["user_agent"] = DEFAULT_USER_AGENT
    ss["map_center"] = compute_center(ss["stops"])
    ss["map_version"] = 0
    ss["dirty"] = False
    ss["search_lookup"] = {}
    ss["last_selected_label"] = None
    ss["search_key_version"] = 0
    ss["sortable_key_version"] = 0
    ss["sortable_items_cache"] = None
    ss["pending_stop"] = None
    ss["pending_preview"] = None
    ss["show_editor"] = False

def ensure_legs_alignment():
    ss = st.session_state
    needed = max(0, len(ss["stops"]) - 1)
    cur = len(ss["legs_between"])
    if cur < needed:
        ss["legs_between"].extend([None] * (needed - cur))
    elif cur > needed:
        ss["legs_between"] = ss["legs_between"][:needed]

def mark_dirty():
    st.session_state["dirty"] = True
    st.session_state["map_version"] += 1

# ----------------------- Helper Logic (Same as original) -----------------------

def day_to_date(day_num: int) -> date:
    return st.session_state["trip_start_date"] + timedelta(days=int(day_num) - 1)

def fmt_date(d: Optional[date]) -> str:
    return d.isoformat() if d else ""

def forward_search(query: str, user_agent: str, limit: int = 8) -> List[Dict]:
    if len(query) < 3:
        return []

    params = {"q": query, "format": "jsonv2", "limit": limit, "addressdetails": 0}
    headers = {"User-Agent": user_agent}

    try:
        r = requests.get(NOMINATIM_SEARCH, params=params, headers=headers, timeout=5)
        r.raise_for_status()
        results = r.json()
        return [
            {"name": x.get("display_name", query), "lat": float(x["lat"]), "lon": float(x["lon"])}
            for x in (results or [])
        ]
    except Exception as e:
        # This will show the exact error in your app window
        st.error(f"⚠️ Search Error: {e}")
        return []

def osrm_driving_route(lat1, lon1, lat2, lon2) -> Dict:
    url = OSRM_ROUTE.format(lat1=lat1, lon1=lon1, lat2=lat2, lon2=lon2)
    params = {"overview": "full", "geometries": "polyline"}
    r = requests.get(url, params=params, timeout=10)
    r.raise_for_status()
    data = r.json()
    if data.get("code") != "Ok" or not data.get("routes"):
        raise RuntimeError(f"OSRM routing failed")
    route0 = data["routes"][0]
    return {
        "distance_m": float(route0["distance"]),
        "duration_s": float(route0["duration"]),
        "geometry_latlon": polyline_lib.decode(route0["geometry"]),
    }

def interpolate_line(lat1, lon1, lat2, lon2, n=80):
    pts = []
    for i in range(n + 1):
        t = i / n
        pts.append((lat1 + t * (lat2 - lat1), lon1 + t * (lon2 - lon1)))
    return pts

def compute_center(stops: List[Dict]):
    if not stops: return None
    return (sum(s["lat"] for s in stops) / len(stops), sum(s["lon"] for s in stops) / len(stops))

def hhmm_from_seconds(seconds: Optional[float]) -> str:
    if seconds is None: return "—"
    m = int(round(float(seconds) / 60.0))
    return f"{m // 60:02d}:{m % 60:02d}"

def leg_summary(leg: Optional[Dict]) -> str:
    if not leg: return "—"
    mode = (leg.get("mode") or "—").upper()
    if mode in {"CAR", "BUS", "TRAIN"} and leg.get("distance_m") is not None:
        km = leg["distance_m"] / 1000.0
        return f"{mode} · {km:.1f} km · {hhmm_from_seconds(leg.get('duration_s'))}"
    return mode

def itinerary_day_blocks(stops, legs_between):
    n = len(stops)
    if n == 0: return []
    overnight_idxs = [i for i, s in enumerate(stops) if s.get("overnight")]
    
    # Helper to sum drive time
    def get_legs_in_range(s, e):
        ls = []
        d = 0
        for i in range(s, e):
            lg = legs_between[i] if i < len(legs_between) else None
            if lg:
                ls.append((i, lg))
                if lg.get("mode") in ["car","bus","train"]:
                    d += lg.get("duration_s", 0)
        return ls, d

    if not overnight_idxs:
        l, d = get_legs_in_range(0, n-1)
        return [{"day": 1, "date": day_to_date(1), "start": 0, "end": n-1, "legs": l, "drive_seconds": d}]

    blocks = []
    day = 1
    start = 0
    for ov in overnight_idxs:
        if ov < start: continue
        end = ov
        l, d = get_legs_in_range(start, end)
        blocks.append({"day": day, "date": day_to_date(day), "start": start, "end": end, "legs": l, "drive_seconds": d})
        start = end
        day += 1

    if start < n - 1:
        l, d = get_legs_in_range(start, n-1)
        blocks.append({"day": day, "date": day_to_date(day), "start": start, "end": n-1, "legs": l, "drive_seconds": d})
    
    return blocks

# ----------------------- Map -----------------------
def build_map(stops, legs_between):
    ss = st.session_state
    center = ss["map_center"] or compute_center(stops) or (20.0, 0.0)
    zoom = 6 if stops else 3
    m = folium.Map(location=center, zoom_start=zoom, control_scale=True)

    if ss["pending_preview"]:
        p = ss["pending_preview"]
        folium.Marker([p["lat"], p["lon"]], icon=folium.Icon(color="green", icon="search")).add_to(m)

    for s in stops:
        icon = folium.Icon(color="blue" if s.get("overnight") else "gray", icon="home" if s.get("overnight") else "info-sign")
        folium.Marker([s["lat"], s["lon"]], tooltip=s["id"], popup=s["name"], icon=icon).add_to(m)

    for i in range(len(stops) - 1):
        leg = legs_between[i] if i < len(legs_between) else None
        if not leg or not leg.get("geometry_latlon"): continue
        
        mode = leg.get("mode", "car")
        color = "red" if mode == "plane" else "#3388ff"
        dash = "8,10" if mode == "plane" else None
        
        folium.PolyLine(leg["geometry_latlon"], weight=4, opacity=0.8, color=color, dash_array=dash).add_to(m)

    return m

# ----------------------- Modifiers -----------------------
def add_stop_internal(name, lat, lon, overnight, note):
    ss = st.session_state
    stop_id = f"S{ss['next_stop_id']}"
    ss["next_stop_id"] += 1
    ss["stops"].append({
        "id": stop_id, "name": name, "lat": lat, "lon": lon, 
        "overnight": overnight, "note": note
    })
    ensure_legs_alignment()
    ss["map_center"] = (lat, lon)
    mark_dirty()

def set_leg_between(idx, mode, note):
    ss = st.session_state
    a = ss["stops"][idx]
    b = ss["stops"][idx + 1]
    
    if mode in ["car", "bus", "train"]:
        route = osrm_driving_route(a["lat"], a["lon"], b["lat"], b["lon"])
        ss["legs_between"][idx] = {"mode": mode, "note": note, **route}
    elif mode == "plane":
        ss["legs_between"][idx] = {
            "mode": "plane", "note": note, "distance_m": None, "duration_s": None,
            "geometry_latlon": interpolate_line(a["lat"], a["lon"], b["lat"], b["lon"])
        }
    else:
        ss["legs_between"][idx] = None
    mark_dirty()

def apply_reorder(new_ids):
    ss = st.session_state
    id_map = {s["id"]: s for s in ss["stops"]}
    old_legs = { (ss["stops"][i]["id"], ss["stops"][i+1]["id"]): ss["legs_between"][i] 
                 for i in range(len(ss["stops"])-1) if ss["legs_between"][i] }
    
    new_stops = [id_map[uid] for uid in new_ids]
    new_legs = []
    
    for i in range(len(new_stops) - 1):
        pair = (new_stops[i]["id"], new_stops[i+1]["id"])
        if pair in old_legs:
            new_legs.append(old_legs[pair])
        else:
            # Re-route default car
            try:
                a, b = new_stops[i], new_stops[i+1]
                rt = osrm_driving_route(a["lat"], a["lon"], b["lat"], b["lon"])
                new_legs.append({"mode": "car", "note": "Auto-routed", **rt})
            except:
                new_legs.append(None)
    
    ss["stops"] = new_stops
    ss["legs_between"] = new_legs
    # Renumber
    for i, s in enumerate(ss["stops"], 1): s["id"] = f"S{i}"
    ss["next_stop_id"] = len(ss["stops"]) + 1
    mark_dirty()

# ======================= APP LAYOUT =======================
st.set_page_config(page_title="Itinerary Sync", layout="wide")

# 1. Initialize State (Load from DB or Create New)
init_state()
ensure_legs_alignment()
ss = st.session_state

# 2. Top Bar
st.title(f"🗺️ {ss['trip_name']}")
st.caption(f"Trip ID: `{ss['current_trip_id']}` (Bookmark this URL to access on other devices)")

c1, c2 = st.columns(2)
with c1:
    new_name = st.text_input("Trip Name", ss["trip_name"])
    if new_name != ss["trip_name"]:
        ss["trip_name"] = new_name
        mark_dirty()
with c2:
    new_date = st.date_input("Start Date", ss["trip_start_date"])
    if new_date != ss["trip_start_date"]:
        ss["trip_start_date"] = new_date
        mark_dirty()

st.divider()

# 3. Search & Add
st.markdown("### Add Stop")
search_res = st_searchbox(
    lambda q: [f"{x['name']}::{i}" for i, x in enumerate(forward_search(q, ss['user_agent']))] if len(q)>2 else [],
    key=f"sb_{ss['search_key_version']}", placeholder="Search city or place..."
)

if search_res:
    # Parse format "Name::Index"
    name_str, idx_str = search_res.rsplit("::", 1)
    # We re-fetch or cache logic simplified here:
    # In real usage, caching the search result object is better, but this suffices for brevity
    # We trigger a rerun to process selection if needed
    if ss.get("last_selected_raw") != search_res:
         # To be perfectly clean we'd re-search or store map, 
         # but here let's just use the name to search 1 result for lat/lon
         candidates = forward_search(name_str, ss['user_agent'], limit=1)
         if candidates:
             sel = candidates[0]
             ss["pending_preview"] = sel
             ss["map_center"] = (sel["lat"], sel["lon"])
             ss["last_selected_raw"] = search_res
             st.rerun()

if ss["pending_preview"]:
    p = ss["pending_preview"]
    with st.expander("Confirm New Stop", expanded=True):
        st.write(f"**Selected:** {p['name']}")
        c_a, c_b = st.columns(2)
        with c_a: is_overnight = st.checkbox("Overnight stop?", value=True)
        with c_b: note_txt = st.text_input("Note")
        
        if st.button("Add to Itinerary", type="primary"):
            # If not first, ask for leg mode? Defaults to car for speed in this version
            add_stop_internal(p['name'], p['lat'], p['lon'], is_overnight, note_txt)
            
            # Auto-route previous leg if exists
            if len(ss["stops"]) > 1:
                prev_idx = len(ss["stops"]) - 2
                set_leg_between(prev_idx, "car", "")
            
            ss["pending_preview"] = None
            ss["search_key_version"] += 1
            st.rerun()

# 4. Map & List
m = build_map(ss["stops"], ss["legs_between"])
st_folium(m, height=500, width=None, key=f"map_{ss['map_version']}")

st.divider()

# Drag & Drop
if ss["stops"] and HAS_SORTABLES:
    with st.expander("Reorder Stops"):
        items = [f"{s['id']} | {s['name']}" for s in ss["stops"]]
        sorted_items = sort_items(items, direction="vertical", key=f"sort_{ss['sortable_key_version']}")
        
        new_ids = [x.split(" | ")[0] for x in sorted_items]
        curr_ids = [s["id"] for s in ss["stops"]]
        if new_ids != curr_ids:
            apply_reorder(new_ids)
            st.rerun()

# Day Blocks
blocks = itinerary_day_blocks(ss["stops"], ss["legs_between"])
for b in blocks:
    st.markdown(f"#### Day {b['day']} ({fmt_date(b['date'])}) — {hhmm_from_seconds(b['drive_seconds'])} driving")
    for i in range(b['start'], b['end'] + 1):
        s = ss["stops"][i]
        icon = "🌙" if s['overnight'] else "📍"
        st.info(f"**{s['id']}** {icon} {s['name']}  \n_{s.get('note','')}_")
    
    if b['legs']:
        with st.expander(f"Travel details (Day {b['day']})"):
            for li, leg in b['legs']:
                st.caption(f"{ss['stops'][li]['id']} ➝ {ss['stops'][li+1]['id']}: {leg_summary(leg)}")

# 5. Sync/Save
# We trigger save at end of run if dirty
if ss["dirty"]:
    save_to_supabase()
    st.toast("Changes saved to cloud!")
