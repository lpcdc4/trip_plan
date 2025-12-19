# streamlit_app.py
#
# Streamlit itinerary planner (Photon + OSRM) + Supabase Sync
#
# Install:
#   pip install streamlit folium streamlit-folium requests polyline streamlit-searchbox streamlit-sortables supabase
#
# Run locally:
#   streamlit run streamlit_app.py

import json
import uuid
from typing import Dict, List, Optional
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
# We use Photon (Komoot) because it is faster and less strict about blocking than OSM Nominatim
PHOTON_SEARCH = "https://photon.komoot.io/api/"
OSRM_ROUTE = "https://router.project-osrm.org/route/v1/driving/{lon1},{lat1};{lon2},{lat2}"
DEFAULT_USER_AGENT = "itinerary-planner-v2 (personal-project)"

# ----------------------- Supabase Setup -----------------------
try:
    SUPABASE_URL = st.secrets["SUPABASE_URL"]
    SUPABASE_KEY = st.secrets["SUPABASE_KEY"]
    supabase: Client = create_client(SUPABASE_URL, SUPABASE_KEY)
except Exception:
    st.error("Missing Supabase secrets. Please set SUPABASE_URL and SUPABASE_KEY in .streamlit/secrets.toml (local) or App Settings (Cloud).")
    st.stop()

# ----------------------- Session / DB Sync -----------------------

def get_trip_id_from_url():
    """Get trip_id from query params or generate a new one."""
    # Handle different Streamlit versions for query params
    if hasattr(st, "query_params"):
        qp = st.query_params
    else:
        qp = st.experimental_get_query_params()
        
    # qp might be a dict or internal object depending on version
    if "trip_id" in qp:
        val = qp["trip_id"]
        return val[0] if isinstance(val, list) else val
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
            ss["current_trip_id"] = url_id
            set_defaults()
    else:
        # No ID -> Generate one
        new_id = str(uuid.uuid4())[:8]
        ss["current_trip_id"] = new_id
        set_defaults()
        
        # Set URL param
        if hasattr(st, "query_params"):
            st.query_params["trip_id"] = new_id
        else:
            st.experimental_set_query_params(trip_id=new_id)

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
    ss.setdefault("dirty", True) # Force initial save
    ss.setdefault("latest_search_results", [])
    ss.setdefault("search_key_version", 0)
    ss.setdefault("sortable_key_version", 0)
    ss.setdefault("pending_stop", None)
    ss.setdefault("pending_preview", None)

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
    
    mx = 0
    for s in ss["stops"]:
        sid = str(s.get("id", ""))
        if sid.startswith("S"):
            try:
                mx = max(mx, int(sid[1:]))
            except: pass
    ss["next_stop_id"] = mx + 1

    ss["user_agent"] = DEFAULT_USER_AGENT
    ss["map_center"] = compute_center(ss["stops"])
    ss["map_version"] = 0
    ss["dirty"] = False
    ss["latest_search_results"] = []
    ss["search_key_version"] = 0
    ss["sortable_key_version"] = 0
    ss["pending_stop"] = None
    ss["pending_preview"] = None

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

# ----------------------- Logic Helpers -----------------------

def day_to_date(day_num: int) -> date:
    return st.session_state["trip_start_date"] + timedelta(days=int(day_num) - 1)

def fmt_date(d: Optional[date]) -> str:
    return d.isoformat() if d else ""

@st.cache_data(ttl=3600, show_spinner=False)
def forward_search(query: str, user_agent: str, limit: int = 10) -> List[Dict]:
    """Search using Photon API with caching and deduplication."""
    if len(query) < 3:
        return []

    params = {"q": query, "limit": limit, "lang": "en"}
    headers = {"User-Agent": user_agent}

    try:
        r = requests.get(PHOTON_SEARCH, params=params, headers=headers, timeout=3)
        r.raise_for_status()
        data = r.json()
        
        results = []
        seen_names = set() # To track duplicates
        
        for feature in data.get("features", []):
            props = feature.get("properties", {})
            coords = feature.get("geometry", {}).get("coordinates", [])
            
            if len(coords) == 2:
                name = props.get("name")
                city = props.get("city")
                country = props.get("country")
                
                # Create a clean label
                parts = [p for p in [name, city, country] if p]
                display_name = ", ".join(parts)
                
                # DEDUPLICATION: If we already have "Chennai, India", skip this one
                if display_name in seen_names:
                    continue
                
                seen_names.add(display_name)
                
                results.append({
                    "name": display_name,
                    "lat": float(coords[1]),
                    "lon": float(coords[0])
                })
        return results
    except Exception:
        return []

def osrm_driving_route(lat1, lon1, lat2, lon2) -> Dict:
    url = OSRM_ROUTE.format(lat1=lat1, lon1=lon1, lat2=lat2, lon2=lon2)
    params = {"overview": "full", "geometries": "polyline"}
    try:
        r = requests.get(url, params=params, timeout=10)
        r.raise_for_status()
        data = r.json()
        if data.get("code") != "Ok" or not data.get("routes"):
            return None
        route0 = data["routes"][0]
        return {
            "distance_m": float(route0["distance"]),
            "duration_s": float(route0["duration"]),
            "geometry_latlon": polyline_lib.decode(route0["geometry"]),
        }
    except:
        return None

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
        folium.Marker(
            [p["lat"], p["lon"]], 
            popup=p["name"],
            icon=folium.Icon(color="green", icon="search")
        ).add_to(m)

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
        if route:
            ss["legs_between"][idx] = {"mode": mode, "note": note, **route}
        else:
            # Fallback if OSRM fails
            ss["legs_between"][idx] = {
                "mode": mode, "note": note + " (routing failed)", "distance_m": 0, "duration_s": 0,
                "geometry_latlon": interpolate_line(a["lat"], a["lon"], b["lat"], b["lon"])
            }
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
                if rt:
                    new_legs.append({"mode": "car", "note": "Auto-routed", **rt})
                else:
                     new_legs.append({"mode": "car", "note": "Routing failed", "geometry_latlon": interpolate_line(a["lat"], a["lon"], b["lat"], b["lon"])})
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

# 1. Initialize State
init_state()
ensure_legs_alignment()
ss = st.session_state

# 2. Header
st.title(f"🗺️ {ss['trip_name']}")
st.caption(f"Trip ID: `{ss['current_trip_id']}` (Bookmark this URL)")

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

# -------------------------------------------------------------
# 3. Search & Add (Fast & Deduplicated)
# -------------------------------------------------------------
st.markdown("### Add Stop")

# We define the search function wrapper
def search_interface(query):
    # This now hits the cache first, making typing much smoother
    results = forward_search(query, ss['user_agent'])
    ss['latest_search_results'] = results
    return [(r['name'], i) for i, r in enumerate(results)]

# The search box
selected_index = st_searchbox(
    search_interface,
    key=f"sb_{ss['search_key_version']}",
    placeholder="Search city or place...",
    label=None,
    clear_on_submit=True # Helps reset the box after selection
)

# LOGIC FIX: We handle the selection immediately without a forced rerun
if selected_index is not None:
    try:
        selection = ss['latest_search_results'][selected_index]
        ss["pending_preview"] = selection
        ss["map_center"] = (selection["lat"], selection["lon"])
        # We DO NOT call st.rerun() here anymore. 
        # Streamlit will just flow down and render the confirmation box below.
    except (IndexError, KeyError, TypeError):
        pass

# The Confirm Box
if ss["pending_preview"]:
    p = ss["pending_preview"]
    with st.container(border=True):
        st.markdown(f"#### 📍 {p['name']}")
        c_a, c_b = st.columns([1, 2])
        with c_a: is_overnight = st.checkbox("Overnight stop?", value=True)
        with c_b: note_txt = st.text_input("Note (optional)")
        
        btn_col1, btn_col2 = st.columns([1, 4])
        with btn_col1:
            if st.button("Add Stop", type="primary", use_container_width=True):
                add_stop_internal(p['name'], p['lat'], p['lon'], is_overnight, note_txt)
                if len(ss["stops"]) > 1:
                    prev_idx = len(ss["stops"]) - 2
                    set_leg_between(prev_idx, "car", "")
                
                # Clear state
                ss["pending_preview"] = None
                ss["search_key_version"] += 1
                st.rerun() # Only rerun AFTER adding the stop
        with btn_col2:
            if st.button("Cancel"):
                ss["pending_preview"] = None
                st.rerun()

# 4. Map & List
m = build_map(ss["stops"], ss["legs_between"])
st_folium(
    m, 
    height=500, 
    width=None, 
    key=f"map_{ss['map_version']}", 
    returned_objects=[]  # <--- THIS IS THE SPEED FIX
)
st.divider()

if ss["stops"] and HAS_SORTABLES:
    with st.expander("Reorder Stops"):
        items = [f"{s['id']} | {s['name']}" for s in ss["stops"]]
        sorted_items = sort_items(items, direction="vertical", key=f"sort_{ss['sortable_key_version']}")
        new_ids = [x.split(" | ")[0] for x in sorted_items]
        curr_ids = [s["id"] for s in ss["stops"]]
        if new_ids != curr_ids:
            apply_reorder(new_ids)
            st.rerun()

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
if ss["dirty"]:
    save_to_supabase()
    st.toast("Changes saved to cloud!")
