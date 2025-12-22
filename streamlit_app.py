# streamlit_app.py
#
# Streamlit itinerary planner (Photon + OSRM) + Supabase Sync + PIN Protection
#
# Install:
#   pip install streamlit folium streamlit-folium requests polyline streamlit-searchbox streamlit-sortables supabase fpdf2
#
# Run:
#   streamlit run streamlit_app.py

import hmac
import uuid
import re
import json
import io
from datetime import date, timedelta, datetime
from typing import Dict, List, Optional, Tuple

import requests
import streamlit as st
import folium
from streamlit_folium import st_folium
import polyline as polyline_lib
from streamlit_searchbox import st_searchbox
from supabase import create_client, Client
from fpdf import FPDF
from fpdf.enums import XPos, YPos  # <--- Add this line
import hashlib
import googlemaps # <--- Make sure to import this



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

# ==============================================================================
# SUPABASE & CONFIG PRE-LOAD
# ==============================================================================
# We initialize Supabase early to fetch the Trip Name for the Browser Tab Title
try:
    SUPABASE_URL = st.secrets["SUPABASE_URL"]
    SUPABASE_KEY = st.secrets["SUPABASE_KEY"]
    supabase: Client = create_client(SUPABASE_URL, SUPABASE_KEY)
except Exception:
    st.error("Missing Supabase secrets. Please set SUPABASE_URL and SUPABASE_KEY.")
    st.stop()

try:
    # Initialize Google Maps Client
    GMAPS_KEY = st.secrets["GOOGLE_MAPS_KEY"]
    gmaps = googlemaps.Client(key=GMAPS_KEY)
except Exception:
    st.error("Missing GOOGLE_MAPS_KEY in secrets.")
    st.stop()


# Logic to peek at the Trip ID in the URL before the app fully loads
browser_tab_title = "Viaggio"
try:
    # 1. Get ID from URL
    if hasattr(st, "query_params"):
        # Streamlit 1.30+
        pre_id = st.query_params.get("trip_id")
    else:
        # Streamlit < 1.30
        pre_qp = st.experimental_get_query_params()
        pre_id = pre_qp.get("trip_id", [None])[0]

    # 2. If ID exists, fetch just the name
    if pre_id:
        # Lightweight query for title only
        res = supabase.table("itineraries").select("trip_data").eq("trip_id", pre_id).execute()
        if res.data and len(res.data) > 0:
            browser_tab_title = res.data[0]["trip_data"].get("name", "Viaggio")
except Exception:
    pass # If any error occurs (DB down, bad ID), keep default title

# 3. Set Page Config with the dynamic title
st.set_page_config(page_title=browser_tab_title, layout="wide", page_icon="🗺️")


# ==============================================================================
# SECURITY: TRIP TOKENS
# ==============================================================================
def get_trip_token(trip_id: str) -> str:
    """Generates a secure, unique token for a specific trip ID."""
    # Use your APP_PIN as the secret 'salt'
    secret = st.secrets.get("APP_PIN", "default_secret")
    msg = f"{trip_id}{secret}"
    # Return first 12 chars of the SHA256 hash
    return hashlib.sha256(msg.encode()).hexdigest()[:12]

def validate_trip_token(trip_id: str, token: str) -> bool:
    """Checks if the token provided matches the trip ID."""
    if not token or not trip_id:
        return False
    expected = get_trip_token(trip_id)
    # Use secure compare to prevent timing attacks
    return hmac.compare_digest(expected, token)
# ==============================================================================
# CONSTANTS & SETUP
# ==============================================================================
PHOTON_SEARCH = "https://photon.komoot.io/api/"
OSRM_ROUTE = "https://router.project-osrm.org/route/v1/driving/{lon1},{lat1};{lon2},{lat2}"
DEFAULT_USER_AGENT = "itinerary-planner-cloud/1.0"




# ----------------------- Session / DB Sync -----------------------

def get_trip_id_from_url():
    if hasattr(st, "query_params"):
        qp = st.query_params
    else:
        qp = st.experimental_get_query_params()
        
    if "trip_id" in qp:
        val = qp["trip_id"]
        return val[0] if isinstance(val, list) else val
    return None
     
@st.cache_data(ttl=10, show_spinner=False)
@st.cache_data(ttl=10, show_spinner=False)
def get_all_trips_summary():
    """Fetches a list of (id, name, last_updated) for all trips in DB."""
    try:
        res = supabase.table("itineraries").select("trip_id, trip_data").execute()
        trips = []
        for row in res.data:
            t_data = row.get("trip_data", {})
            
            # --- NEW: Filter out soft-deleted trips ---
            if t_data.get("is_deleted") is True:
                continue
            # ------------------------------------------

            name = t_data.get("name", "Senza Nome")
            # Fetch timestamp, default to empty string if missing
            last_updated = t_data.get("last_updated", "")
            tid = row.get("trip_id")
            if tid:
                trips.append({"id": tid, "name": name, "last_updated": last_updated})
        
        # Keep sorting by name for the UI Dropdown list
        trips.sort(key=lambda x: x["name"])
        return trips
    except Exception:
        return []
        
def load_from_supabase(trip_id: str):
    try:
        response = supabase.table("itineraries").select("trip_data").eq("trip_id", trip_id).execute()
        if response.data and len(response.data) > 0:
            return response.data[0]["trip_data"]
    except Exception as e:
        pass
    return None

def save_to_supabase():
    ss = st.session_state
    # Only save if dirty AND we have edit permission (safety check)
    if not ss.get("dirty", False):
        return
    if not ss.get("can_edit", False):
        return

    trip_id = ss["current_trip_id"]
    
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
    except Exception as e:
        st.warning(f"Sincronizzazione fallita: {e}")
        
@st.cache_data(ttl=10, show_spinner=False)




def init_state(force_id: str = None):
    ss = st.session_state
    
    # 1. Force reset if switching trips
    if force_id:
        if "initialized" in ss: del ss["initialized"]
        ss["current_trip_id"] = force_id
    
    # 2. CRITICAL FIX: Only skip if initialized AND 'stops' actually exists
    if "initialized" in ss and "stops" in ss:
        return

    # 3. Determine Trip ID
    url_id = force_id or get_trip_id_from_url()
    target_id = None
    
    if url_id:
        target_id = url_id
    else:
        # Load most recent trip
        existing_trips = get_all_trips_summary()
        if existing_trips:
            # Sort by last_updated (descending)
            most_recent = max(existing_trips, key=lambda x: x.get("last_updated", "") or "")
            target_id = most_recent["id"]
        else:
            target_id = str(uuid.uuid4())[:8]

    # 4. Load Data
    data = load_from_supabase(target_id)
    
    if data:
        ss["current_trip_id"] = target_id
        populate_state_from_data(data)
    else:
        ss["current_trip_id"] = target_id
        set_defaults() # Ensure this function sets ss["stops"] = []

    # 5. Finalize
    if hasattr(st, "query_params"):
        st.query_params["trip_id"] = target_id
    else:
        st.experimental_set_query_params(trip_id=target_id)

    ss["initialized"] = True
    
def get_unique_new_trip_name():
    """Generates 'Il Mio Viaggio', 'Il Mio Viaggio (2)', etc. based on existing trips."""
    # Fetch existing names to avoid duplicates
    existing_trips = get_all_trips_summary()
    existing_names = {t["name"] for t in existing_trips}

    base = "Il Mio Viaggio"
    if base not in existing_names:
        return base

    c = 2
    while True:
        candidate = f"{base} ({c})"
        if candidate not in existing_names:
            return candidate
        c += 1

def set_defaults():
    ss = st.session_state
    
    # --- DATA RESET (Use '=' to overwrite old trip data) ---
    ss["trip_name"] = get_unique_new_trip_name()
    ss["stops"] = []
    ss["legs_between"] = []
    ss["trip_start_date"] = date.today()
    ss["next_stop_id"] = 1
    ss["map_center"] = None
    ss["dirty"] = True  # Mark dirty so the new empty trip saves to DB immediately
    
    # --- UI STATE RESET ---
    ss["map_version"] = 0
    ss["search_lookup"] = {}
    ss["last_selected_label"] = None
    ss["search_key_version"] = 0
    ss["sortable_key_version"] = 0
    ss["sortable_items_cache"] = None
    ss["pending_stop"] = None
    ss["pending_preview"] = None
    ss["show_editor"] = False
    ss["editing_stop_idx"] = None
    
    # --- PERSISTENT SETTINGS (Keep these if they exist) ---
    ss.setdefault("user_agent", DEFAULT_USER_AGENT)
    # Don't logout the user if they are already authenticated
    ss.setdefault("can_edit", False)

def populate_state_from_data(data: dict):
    ss = st.session_state
    ss["trip_name"] = data.get("name", "Viaggio Importato")
    try:
        ss["trip_start_date"] = date.fromisoformat(data.get("trip_start_date"))
    except:
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

    ss["search_lookup"] = {}
    ss["last_selected_label"] = None
    ss["search_key_version"] = 0
    ss["sortable_key_version"] = 0
    ss["pending_stop"] = None
    ss["pending_preview"] = None
    ss["show_editor"] = False
    ss["dirty"] = False
    ss["map_center"] = compute_center(ss["stops"])
    ss["map_version"] = 0


def ensure_legs_alignment():
    ss = st.session_state
    
    # SAFETY CHECK: If 'stops' is missing, don't crash. 
    # This prevents the KeyError if init_state failed.
    if "stops" not in ss:
        return

    needed = max(0, len(ss["stops"]) - 1)
    
    # Ensure 'legs_between' list exists
    if "legs_between" not in ss:
        ss["legs_between"] = []
        
    cur = len(ss["legs_between"])
    if cur < needed:
        ss["legs_between"].extend([None] * (needed - cur))
    elif cur > needed:
        ss["legs_between"] = ss["legs_between"][:needed]


def mark_dirty():
    ss = st.session_state
    ss["dirty"] = True
    ss["map_version"] += 1


# ----------------------- Dates -----------------------
def day_to_date(day_num: int) -> date:
    return st.session_state["trip_start_date"] + timedelta(days=int(day_num) - 1)

def fmt_date(d: Optional[date]) -> str:
    # DD/MM/YY
    return d.strftime("%d/%m/%y") if d else ""


# ----------------------- External calls -----------------------
@st.cache_data(ttl=3600, show_spinner=False)
def forward_search(query: str, user_agent: str, limit: int = 15) -> List[Dict]:
    if len(query) < 2:
        return []

    params = {"q": query, "limit": limit, "lang": "en"}
    headers = {"User-Agent": user_agent}

    try:
        r = requests.get(PHOTON_SEARCH, params=params, headers=headers, timeout=5)
        r.raise_for_status()
        data = r.json()
        
        results = []
        seen_labels = set()
        
        for feature in data.get("features", []):
            props = feature.get("properties", {})
            coords = feature.get("geometry", {}).get("coordinates", [])
            
            if len(coords) == 2:
                name = props.get("name")
                city = props.get("city")
                state = props.get("state")
                country = props.get("country")
                
                parts = [p for p in [name, city, state, country] if p]
                display_name = ", ".join(parts)
                
                if display_name in seen_labels:
                    continue
                seen_labels.add(display_name)
                
                results.append({
                    "name": display_name,
                    "lat": float(coords[1]),
                    "lon": float(coords[0]),
                })
        return results
    except Exception:
        return []


def google_driving_route(lat1: float, lon1: float, lat2: float, lon2: float) -> Dict:
    """
    Fetches driving route from Google Maps Directions API.
    Returns the same structure as the old OSRM function for compatibility.
    """
    try:
        # Request driving directions
        # mode="driving" is default, but explicit is safer
        # departure_time="now" ensures traffic data is used (if available)
        directions = gmaps.directions(
            origin=(lat1, lon1),
            destination=(lat2, lon2),
            mode="driving",
            units="metric"
        )

        if not directions:
            raise RuntimeError("No route found")

        route = directions[0]
        leg = route['legs'][0]

        # Google returns an encoded polyline for the 'overview_polyline'
        # We need to decode it to a list of (lat, lon) for Folium
        encoded_poly = route['overview_polyline']['points']
        coords_latlon = polyline_lib.decode(encoded_poly)

        return {
            "distance_m": float(leg['distance']['value']),  # meters
            "duration_s": float(leg['duration']['value']),  # seconds
            "geometry_latlon": coords_latlon,
        }
    except Exception as e:
        # Fallback to straight line if API fails or quota exceeded
        return {
            "distance_m": None, 
            "duration_s": None,
            "geometry_latlon": interpolate_line(lat1, lon1, lat2, lon2)
        }


def interpolate_line(lat1: float, lon1: float, lat2: float, lon2: float, n: int = 80) -> List[Tuple[float, float]]:
    if n <= 1:
        return [(lat1, lon1), (lat2, lon2)]
    pts = []
    for i in range(n + 1):
        t = i / n
        pts.append((lat1 + t * (lat2 - lat1), lon1 + t * (lon2 - lon1)))
    return pts


# ----------------------- Helpers -----------------------
def compute_center(stops: List[Dict]) -> Optional[Tuple[float, float]]:
    if not stops:
        return None
    return (sum(s["lat"] for s in stops) / len(stops), sum(s["lon"] for s in stops) / len(stops))


def hhmm_from_seconds(seconds: Optional[float]) -> Optional[str]:
    if seconds is None:
        return None
    try:
        total_minutes = int(round(float(seconds) / 60.0))
    except Exception:
        return None
    h = total_minutes // 60
    m = total_minutes % 60
    # No leading zero on hours: 2h13m
    return f"{h}h{m:02d}m"


def driving_seconds_for_leg(leg: Optional[Dict]) -> int:
    if not leg:
        return 0
    mode = (leg.get("mode") or "").lower()
    # CHANGED: Only count car/auto as "Driving" time
    if mode in {"car", "auto"} and leg.get("duration_s") is not None:
        try:
            return int(round(float(leg["duration_s"])))
        except Exception:
            return 0
    return 0


def leg_summary(leg: Optional[Dict]) -> str:
    if not leg:
        return "—"
    mode_raw = (leg.get("mode") or "—").lower()
    mode_map = {
        "car": "Auto", "bus": "Bus", "train": "Treno", "plane": "Aereo", "ferry": "Traghetto"
    }
    display_mode = mode_map.get(mode_raw, mode_raw.title())
    if mode_raw in {"car", "bus", "train", "auto"} and leg.get("distance_m") is not None and leg.get("duration_s") is not None:
        km = leg["distance_m"] / 1000.0
        hhmm = hhmm_from_seconds(leg["duration_s"])
        return f"{display_mode} · {km:.1f} km · {hhmm}"
    return display_mode


# ----------------------- Overnight-as-night blocks -----------------------
def itinerary_day_blocks(stops: List[Dict], legs_between: List[Optional[Dict]]) -> List[Dict]:
    n = len(stops)
    if n == 0:
        return []

    overnight_idxs = [i for i, s in enumerate(stops) if bool(s.get("overnight", False))]

    if not overnight_idxs:
        legs = []
        drive = 0
        for li in range(0, n - 1):
            leg = legs_between[li] if li < len(legs_between) else None
            if leg:
                legs.append((li, leg))
                drive += driving_seconds_for_leg(leg)
        return [{"day": 1, "date": day_to_date(1), "start": 0, "end": n - 1, "legs": legs, "drive_seconds": drive}]

    blocks: List[Dict] = []
    day = 1
    start = 0

    for ov in overnight_idxs:
        if ov < start:
            continue
        end = ov
        legs = []
        drive = 0
        for li in range(start, end):
            leg = legs_between[li] if li < len(legs_between) else None
            if leg:
                legs.append((li, leg))
                drive += driving_seconds_for_leg(leg)

        blocks.append({"day": day, "date": day_to_date(day), "start": start, "end": end, "legs": legs, "drive_seconds": drive})

        start = end
        day += 1

    if start < n - 1:
        end = n - 1
        legs = []
        drive = 0
        for li in range(start, end):
            leg = legs_between[li] if li < len(legs_between) else None
            if leg:
                legs.append((li, leg))
                drive += driving_seconds_for_leg(leg)
        blocks.append({"day": day, "date": day_to_date(day), "start": start, "end": end, "legs": legs, "drive_seconds": drive})

    return blocks


# ----------------------- Map -----------------------
def build_map(stops: List[Dict], legs_between: List[Optional[Dict]]) -> folium.Map:
    ss = st.session_state
    center = ss["map_center"] or compute_center(stops) or (20.0, 0.0)
    zoom = 6 if stops else 3
    m = folium.Map(location=center, zoom_start=zoom, control_scale=True)

    if ss["pending_preview"] is not None:
        p = ss["pending_preview"]
        folium.Marker(
            [p["lat"], p["lon"]],
            tooltip="Prossima tappa (anteprima)",
            popup=f"<b>Prossima tappa</b><br>{p['name']}",
            icon=folium.Icon(color="green", icon="search"),
        ).add_to(m)

    for s in stops:
        is_overnight = s.get("overnight")
        if is_overnight:
            # CHANGED: Use 'bed' icon with 'fa' prefix
            icon = folium.Icon(color="blue", icon="bed", prefix="fa")
            od_str = "Sì"
        else:
            icon = folium.Icon(color="gray", icon="map-pin", prefix="fa")
            od_str = "No"
  
        
        # CHANGED: Clean popup, no internal IDs
        popup = f"<b>{s['name']}</b><br>Notte: {od_str}"
        if s.get("note"):
            popup += f"<br>Note: {s['note']}"
            
        folium.Marker([s["lat"], s["lon"]], popup=popup, tooltip=s["name"], icon=icon).add_to(m)
        
    for i in range(len(stops) - 1):
        leg = legs_between[i] if i < len(legs_between) else None
        if not leg or not leg.get("geometry_latlon"):
            continue

        mode = (leg.get("mode") or "").lower()
        dash = "1,0"
        if mode == "bus":
            dash = "6,6"
        elif mode == "train":
            dash = "2,8"
        elif mode == "plane":
            dash = "8,10"

        # CHANGED: Clean tooltip, no IDs
        # NEW: Maps to Italian (Auto, Aereo, etc.)
        mode_map = {
            "car": "Auto", "bus": "Bus", "train": "Treno", 
            "plane": "Aereo", "ferry": "Traghetto"
        }
        display_mode = mode_map.get(mode, mode.title())

        tooltip = f"{display_mode} · {stops[i]['name']} → {stops[i+1]['name']}"
        
        if leg.get("duration_s") is not None:
            tooltip += f" · {hhmm_from_seconds(leg['duration_s'])}"
        if leg.get("distance_m") is not None:
            tooltip += f" · {leg['distance_m']/1000:.1f} km"

        color = "red" if mode == "plane" else "#3388ff"

        folium.PolyLine(
            leg["geometry_latlon"],
            weight=4,
            opacity=0.9,
            dash_array=dash,
            tooltip=tooltip,
            color=color,
        ).add_to(m)

    return m


# ----------------------- Add stop / legs -----------------------
def add_stop_internal(name: str, lat: float, lon: float, overnight: bool, note: str):
    ss = st.session_state
    stop_id = f"S{ss['next_stop_id']}"
    ss["next_stop_id"] += 1
    ss["stops"].append(
        {
            "id": stop_id,
            "name": name,
            "lat": float(lat),
            "lon": float(lon),
            "overnight": bool(overnight),
            "note": (note or "").strip(),
        }
    )
    ensure_legs_alignment()
    ss["map_center"] = (float(lat), float(lon))
    mark_dirty()


def set_leg_between(prev_idx: int, mode: str, note: str):
    ss = st.session_state
    a = ss["stops"][prev_idx]
    b = ss["stops"][prev_idx + 1]
    mode = (mode or "").lower()

    leg: Optional[Dict] = {"mode": mode, "note": (note or "").strip()}

    if mode in {"car", "bus", "train"}:
        leg.update(google_driving_route(a["lat"], a["lon"], b["lat"], b["lon"]))
    elif mode == "plane":
        leg.update(
            {
                "distance_m": None,
                "duration_s": None,
                "geometry_latlon": interpolate_line(a["lat"], a["lon"], b["lat"], b["lon"]),
            }
        )
    elif mode == "—":
        leg = None
    else:
        leg = None

    ss["legs_between"][prev_idx] = leg
    mark_dirty()


# ----------------------- Reorder + renumber -----------------------
def renumber_stops_and_update():
    ss = st.session_state
    for i, s in enumerate(ss["stops"], start=1):
        s["id"] = f"S{i}"
    ss["next_stop_id"] = len(ss["stops"]) + 1
    ss["sortable_key_version"] += 1
    mark_dirty()

def apply_stop_reorder(new_order_ids: List[str]):
    ss = st.session_state
    old_stops = ss["stops"]
    old_legs = ss["legs_between"]

    id_to_stop = {s["id"]: s for s in old_stops}
    if set(new_order_ids) != set(id_to_stop.keys()):
        return

    # old adjacency -> leg
    leg_map: Dict[Tuple[str, str], Dict] = {}
    for i in range(len(old_stops) - 1):
        leg = old_legs[i] if i < len(old_legs) else None
        if leg:
            leg_map[(old_stops[i]["id"], old_stops[i + 1]["id"])] = leg

    new_stops = [id_to_stop[sid] for sid in new_order_ids]

    new_legs: List[Optional[Dict]] = []
    for i in range(len(new_stops) - 1):
        frm_id = new_stops[i]["id"]
        to_id = new_stops[i + 1]["id"]

        kept = leg_map.get((frm_id, to_id))
        if kept:
            new_legs.append(kept)
            continue

        # Default to car and auto-route
        A = new_stops[i]
        B = new_stops[i + 1]
        try:
            route = google_driving_route(A["lat"], A["lon"], B["lat"], B["lon"])
            new_legs.append({"mode": "car", "note": "", **route})
        except:
            new_legs.append(None)

    ss["stops"] = new_stops
    ss["legs_between"] = new_legs
    ss["map_center"] = compute_center(new_stops)
    mark_dirty()


def rebuild_legs_from_old(old_stops: List[Dict], old_legs: List[Optional[Dict]], new_stops: List[Dict]) -> List[Optional[Dict]]:
    leg_map: Dict[Tuple[str, str], Dict] = {}
    for i in range(len(old_stops) - 1):
        leg = old_legs[i] if i < len(old_legs) else None
        if leg:
            leg_map[(old_stops[i]["id"], old_stops[i + 1]["id"])] = leg

    new_legs: List[Optional[Dict]] = []
    for i in range(len(new_stops) - 1):
        frm_id = new_stops[i]["id"]
        to_id = new_stops[i + 1]["id"]
        kept = leg_map.get((frm_id, to_id))
        if kept:
            new_legs.append(kept)
        else:
            new_legs.append(None) 
    return new_legs


# ----------------------- Autocomplete -----------------------
def search_api_labels(query: str) -> List[str]:
    ss = st.session_state
    q = (query or "").strip()
    if len(q) < 2:
        ss["search_lookup"] = {}
        return []
    
    ua = ss.get("user_agent", DEFAULT_USER_AGENT)
    
    results = forward_search(q, ua, limit=10)
    
    lookup: Dict[str, Dict] = {}
    labels: List[str] = []
    for r in results:
        label = r["name"]
        j = 2
        while label in lookup:
            label = f"{r['name']} ({j})"
            j += 1
        lookup[label] = r
        labels.append(label)

    ss["search_lookup"] = lookup
    return labels


# ----------------------- UI helpers -----------------------
def stop_row_html(s: Dict) -> str:
    overnight = bool(s.get("overnight", False))
    bg = "#e8f0ff" if overnight else "#ffffff"
    border = "#9bb7ff" if overnight else "#e5e7eb"
    badge = "🌙 Pernottamento" if overnight else ""
    note = (s.get("note") or "").strip()
    note_html = f"<div style='margin-top:6px;color:#444;font-size:0.92rem;'><em>{note}</em></div>" if note else ""
    # CHANGED: No S1/S2 ID shown
    return f"""
    <div style="background:{bg};border:1px solid {border};border-radius:12px;padding:12px 14px;margin:8px 0;">
      <div style="display:flex;gap:10px;align-items:baseline;justify-content:space-between;">
        <div style="font-size:1.02rem;"><strong>{s['name']}</strong></div>
        <div style="font-size:0.92rem;color:#333;">{badge}</div>
      </div>
      {note_html}
    </div>
    """

# ----------------------- PDF Generator (Fixed Layout) -----------------------
class TripPDF(FPDF):
    def header(self):
        # Only show header on pages after the first one
        if self.page_no() > 1:
            self.set_font("Helvetica", "I", 8)
            self.set_text_color(128, 128, 128)
            name = st.session_state.get("trip_name", "Viaggio")
            # Safe encode
            safe_name = name.encode('latin-1', 'replace').decode('latin-1')
            self.cell(0, 10, safe_name, border=False, align="R")
            self.ln(10)

    def footer(self):
        # Position at 1.5 cm from bottom
        self.set_y(-15)
        self.set_font("Helvetica", "I", 8)
        self.set_text_color(128, 128, 128)
        self.cell(0, 10, f"Pagina {self.page_no()}/{{nb}}", align="C")

def generate_pdf_bytes(trip_name, start_date, stops, legs):
    pdf = TripPDF()
    pdf.alias_nb_pages()
    pdf.add_page()
    pdf.set_auto_page_break(auto=True, margin=25) 
    
    # 1. TITLE PAGE HEADER
    pdf.set_font("Helvetica", "B", 24)
    safe_name = trip_name.encode('latin-1', 'replace').decode('latin-1')
    
    # FIX: Replaced ln=True
    pdf.cell(0, 10, safe_name.upper(), new_x=XPos.LMARGIN, new_y=YPos.NEXT, align="L")
    
    pdf.set_font("Helvetica", "", 12)
    pdf.set_text_color(50, 50, 50)
    
    # FIX: Replaced ln=True
    pdf.cell(0, 10, f"Inizio viaggio: {start_date.strftime('%d/%m/%Y')}", new_x=XPos.LMARGIN, new_y=YPos.NEXT)
    pdf.ln(10)
    
    # 2. GENERATE BLOCKS
    blocks = itinerary_day_blocks(stops, legs)
    
    for b in blocks:
        if pdf.get_y() > 250: 
            pdf.add_page()

        # --- DAY HEADER ---
        pdf.set_fill_color(240, 240, 240)
        pdf.set_font("Helvetica", "B", 12)
        pdf.set_text_color(0, 0, 0)
        
        date_str = b["date"].strftime("%d/%m/%y")
        
        time_parts = []
        if b["drive_seconds"] > 0: 
            time_parts.append(f"Guida ({hhmm_from_seconds(b['drive_seconds'])})")
        
        modes = set()
        for li, leg in b["legs"]:
             if leg: modes.add(leg.get("mode", "car").lower())
        
        if "plane" in modes or "aereo" in modes: time_parts.append("Volo")
        if "train" in modes or "treno" in modes: time_parts.append("Treno")
        if "bus" in modes: time_parts.append("Bus")
        if "ferry" in modes or "traghetto" in modes: time_parts.append("Traghetto")
        if not time_parts and len(b["legs"]) > 0: time_parts.append("")
        
        mid_part = f"   |   {' + '.join(time_parts)}" if time_parts else " "
        header_text = f"GIORNO {b['day']}  -  {date_str}{mid_part}"
        
        safe_header = header_text.encode('latin-1', 'replace').decode('latin-1')
        
        # FIX: Replaced ln=True
        pdf.cell(0, 10, safe_header, new_x=XPos.LMARGIN, new_y=YPos.NEXT, fill=True, border=False)
        pdf.ln(2)
        
        # --- STOPS LOOP ---
        pdf.set_font("Helvetica", "", 11)
        
        for i in range(b["start"], b["end"] + 1):
            s = stops[i]
            is_overnight = s.get("overnight", False)
            
            name = s['name'].encode('latin-1', 'replace').decode('latin-1')
            note = s.get('note', '').strip().encode('latin-1', 'replace').decode('latin-1')
            
            box_h = 10
            if note: box_h += 6
            
            if pdf.get_y() + box_h > 270:
                pdf.add_page()
            
            if is_overnight:
                pdf.set_fill_color(250, 250, 250) 
                pdf.set_draw_color(0, 0, 0)
                pdf.set_line_width(0.5)
            else:
                pdf.set_fill_color(255, 255, 255) 
                pdf.set_draw_color(180, 180, 180)
                pdf.set_line_width(0.2)
                
            x = pdf.get_x()
            y = pdf.get_y()
            pdf.rect(x, y, 190, box_h, 'FD')
            
            pdf.set_xy(x + 3, y + 2)
            pdf.set_font("Helvetica", "B", 11)
            pdf.set_text_color(0, 0, 0)
            
            # FIX: Replaced ln=True
            pdf.cell(0, 6, name, new_x=XPos.LMARGIN, new_y=YPos.NEXT)
            
            if note:
                pdf.set_x(x + 3)
                pdf.set_font("Helvetica", "I", 9)
                pdf.set_text_color(80, 80, 80)
                # FIX: Replaced ln=True
                pdf.cell(0, 5, note, new_x=XPos.LMARGIN, new_y=YPos.NEXT)
            
            pdf.set_y(y + box_h + 2) 
            
            if i < b["end"]:
                leg = legs[i]
                if leg:
                    if pdf.get_y() + 6 > 270:
                        pdf.add_page()

                    summ = leg_summary(leg)
                    lnote = f" ({leg['note']})" if leg.get("note") else ""
                    full_leg = f"      |   {summ}{lnote}".encode('latin-1', 'replace').decode('latin-1')
                    
                    pdf.set_font("Helvetica", "", 8)
                    pdf.set_text_color(100, 100, 100)
                    
                    # FIX: Replaced ln=True
                    pdf.cell(0, 5, full_leg, new_x=XPos.LMARGIN, new_y=YPos.NEXT)
                    pdf.ln(1)
                    
        pdf.ln(4)

    return bytes(pdf.output())


# ======================= APP =======================
init_state()
ensure_legs_alignment()
ss = st.session_state


# ==============================================================================
# GATEKEEPER (3-Tier Access Control)
# ==============================================================================
def check_access():
    ss = st.session_state
    
    # --- TIER 3: ADMIN (Can see/edit everything) ---
    if ss.get("can_edit"):
        return True

    # Initialize "Allowed Trips" set for Tier 2 users
    if "allowed_view_ids" not in ss:
        ss["allowed_view_ids"] = set()

    # --- TIER 2: LINK ACCESS (Can see specific trip) ---
    # 1. Get params from URL
    if hasattr(st, "query_params"):
        qp = st.query_params
        url_token = qp.get("token")
        url_trip = qp.get("trip_id")
    else:
        qp = st.experimental_get_query_params()
        url_token = qp.get("token", [None])[0]
        url_trip = qp.get("trip_id", [None])[0]

    # 2. Check if URL grants access to a specific trip
    if url_trip and url_token:
        if validate_trip_token(url_trip, url_token):
            ss["allowed_view_ids"].add(url_trip)
            # If we are viewing the allowed trip, PASS
            if ss["current_trip_id"] == url_trip:
                return True

    # 3. Check if we previously authorized this trip in this session
    # 3. Check if we previously authorized this trip in this session
    # Use .get() to avoid crashing if current_trip_id hasn't been set yet
    if ss.get("current_trip_id") and ss.get("current_trip_id") in ss.get("allowed_view_ids", set()):
        return True
    # --- TIER 1: BLOCKED (Show Login) ---
    st.markdown("### 🔒 Accesso Limitato")
    st.caption("Inserisci il PIN Amministratore o un Token Viaggio.")
    
    user_input = st.text_input("PIN o Token", type="password")
    
    if user_input:
        # Check A: Is it the Admin PIN? -> Upgrade to Tier 3
        secret_pin = st.secrets.get("APP_PIN", "0000")
        if hmac.compare_digest(user_input, str(secret_pin)):
            ss["can_edit"] = True
            st.rerun()
        
        # Check B: Is it a valid Token for the CURRENT trip? -> Grant Tier 2
        elif validate_trip_token(ss["current_trip_id"], user_input):
            ss["allowed_view_ids"].add(ss["current_trip_id"])
            st.rerun()
            
        else:
            st.error("Accesso negato")

    return False

# STOP APP HERE if access is not granted
if not check_access():
    st.stop()
# ==============================================================================
# ----------------------- Sidebar: Auth & Tools -----------------------
with st.sidebar:
    st.header("⚙️ Menu")
    
    # 1. AUTHENTICATION (Login/Logout)
    if not ss.get("can_edit"):
        with st.expander("🔐 Accesso Modifiche", expanded=True):
            def password_entered():
                secret_pin = st.secrets.get("APP_PIN", "0000")
                if hmac.compare_digest(st.session_state["pin_input"], str(secret_pin)):
                    st.session_state["can_edit"] = True
                    st.session_state["pin_input"] = ""
                else:
                    st.error("PIN Errato")
            
            st.text_input("Inserisci PIN", type="password", key="pin_input", on_change=password_entered)
    else:
        st.success(f"🔓 Modifica Attiva")
        if st.button("🔒 Esci (Blocca)", key="logout_btn"):
            ss["can_edit"] = False
            st.rerun()

    st.divider()

    # 2. PDF DOWNLOAD (Replaces Print Toggle)
    if ss.get("stops"):
        pdf_data = generate_pdf_bytes(
            ss["trip_name"], 
            ss["trip_start_date"], 
            ss["stops"], 
            ss["legs_between"]
        )
        st.download_button(
            label="📄 Scarica PDF (Grayscale)",
            data=pdf_data,
            file_name=f"{ss['trip_name'].replace(' ', '_')}.pdf",
            mime="application/pdf"
        )
    
    # 3. EDIT TOOLS (Only if can_edit)
    if ss.get("can_edit"):    
    
        st.divider()
        st.markdown("**🛠️ Strumenti**")
    
        if st.button("🔄 Aggiorna tutto con Google Maps"):
            # 1. Setup Progress
            progress_bar = st.progress(0)
            status_text = st.empty()
        
            stops = ss["stops"]
            legs = ss["legs_between"]
            total = len(stops) - 1
            updated_count = 0
        
            # 2. Iterate through all legs
            for i in range(total):
                leg = legs[i]
                # Only update "road" legs (ignore flights or empty legs)
                if leg and leg.get("mode") in {"car", "bus", "train"}:
                    status_text.text(f"Ricalcolo tratta {i+1} di {total}...")
                    
                    A = stops[i]
                    B = stops[i+1]
                    
                    # 3. Call Google API
                    # (Using the google_driving_route function you just added)
                    try:
                        new_data = google_driving_route(A["lat"], A["lon"], B["lat"], B["lon"])
                        
                        # Optional: Mark source if you added the badge logic
                        new_data["source"] = "google"
                        
                        # Update the leg in place
                        leg.update(new_data)
                        updated_count += 1
                    except Exception as e:
                        st.warning(f"Errore tratta {i+1}: {e}")
                
                # Update bar
                progress_bar.progress((i + 1) / total)
                
            # 3. Save & Finish
            status_text.text("Salvataggio in corso...")
            mark_dirty()
            save_to_supabase() # Force immediate save so you don't lose the API data
            status_text.success(f"Fatto! {updated_count} tratte aggiornate.")
            st.rerun()
            
        st.divider()
        st.markdown("**Gestione**")
        if st.button("©️ Clona Viaggio"):
            new_id = str(uuid.uuid4())[:8]
            ss["current_trip_id"] = new_id
            ss["trip_name"] = f"Copia di {ss['trip_name']}"
            if hasattr(st, "query_params"): st.query_params["trip_id"] = new_id
            else: st.experimental_set_query_params(trip_id=new_id)
            mark_dirty() 
            st.success(f"Clonato! ID: {new_id}")
            st.rerun()
            
    #3b SHARE LINK (Only if can_edit)        
    if ss.get("can_edit"):
        # In st.sidebar...
        st.divider()
        st.markdown("**🔗 Condivisione (Sola Lettura)**")
        
        # Generate Link for CURRENT Trip
        tid = ss["current_trip_id"]
        token = get_trip_token(tid)
        
        magic_link = "https://itinerari-picaciam.streamlit.app"+f"?trip_id={tid}&token={token}"
        
        st.code(magic_link, language="text")
        st.caption(f"Chi ha questo link può vedere **solo** il viaggio '{ss['trip_name']}', ma non può modificarlo.")
        st.divider()
    
    # EXPORT (Visible to everyone)
    export_data = {
        "name": ss["trip_name"],
        "trip_start_date": ss["trip_start_date"].isoformat(),
        "stops": ss["stops"],
        "legs_between": ss["legs_between"],
        "last_updated": datetime.now().isoformat()
    }
    st.download_button(
        label="⬇️ Esporta JSON",
        data=json.dumps(export_data, indent=2),
        file_name=f"itinerario_{ss['current_trip_id']}.json",
        mime="application/json"
    )
    
    # IMPORT (Only if can_edit)
    if ss.get("can_edit"):
        uploaded_file = st.file_uploader("⬆️ Importa JSON", type=["json"])
        
        if uploaded_file is not None:
            try:
                # 1. Load the data into memory first
                new_data = json.load(uploaded_file)
                
                # 2. Check if the current trip has existing stops
                current_stops = ss.get("stops", [])
                has_existing_data = len(current_stops) > 0

                # 3. Helper function to execute the overwrite
                def perform_import():
                    populate_state_from_data(new_data)
                    mark_dirty() 
                    st.success("Caricato!")
                    st.rerun()

                # 4. Logic: Immediate import vs. Confirmation
                if not has_existing_data:
                    # Trip is empty, import immediately
                    perform_import()
                else:
                    # Trip has data, ask for confirmation
                    st.warning(f"⚠️ Il viaggio attuale ha già {len(current_stops)} tappe.")
                    st.markdown("Importando il file **sovrascriverai** tutto.")
                    
                    if st.button("✅ Conferma Sovrascrittura", type="primary", key="confirm_import_btn"):
                        perform_import()

            except Exception as e:
                st.error(f"Errore nel file: {e}")
                
    st.caption(f"ID: `{ss['current_trip_id']}`")


# ----------------------- Main Header -----------------------
all_trips = get_all_trips_summary()

# 1. FILTER TRIPS based on Access Tier
if ss.get("can_edit"):
    # TIER 3 (Admin): See "NEW" + ALL trips
    available_trips = all_trips
    trip_options = ["NEW"] + [t["id"] for t in available_trips]
else:
    # TIER 2 (Link): See ONLY trips allowed by token
    allowed = ss.get("allowed_view_ids", set())
    available_trips = [t for t in all_trips if t["id"] in allowed]
    trip_options = [t["id"] for t in available_trips]

def format_trip_option(option_id):
    if option_id == "NEW":
        return "➕ Nuovo Viaggio..."
    for t in all_trips:
        if t["id"] == option_id:
            return f"📂 {t['name']}"
    return option_id

# Determine current index safely
try:
    current_idx = trip_options.index(ss["current_trip_id"])
except ValueError:
    current_idx = 0

# 2. Layout: Selector (Top)
c_sel, c_rest = st.columns([1, 3])
with c_sel:
    # Logic: Only show dropdown if user has multiple options
    if len(trip_options) > 1:
        selected_trip = st.selectbox(
            "Viaggio", 
            options=trip_options, 
            index=current_idx,
            format_func=format_trip_option,
            label_visibility="collapsed"
        )
    else:
        # If single trip access, lock selection to current
        selected_trip = ss["current_trip_id"]

# Logic: Reload app if selection changes
if selected_trip != "NEW" and selected_trip != ss["current_trip_id"]:
    init_state(force_id=selected_trip)
    if hasattr(st, "query_params"): st.query_params["trip_id"] = selected_trip
    else: st.experimental_set_query_params(trip_id=selected_trip)
    st.rerun()
elif selected_trip == "NEW" and ss["current_trip_id"] in [t["id"] for t in all_trips]:
    # Only triggers if "NEW" was in the list (Edit mode only)
    new_id = str(uuid.uuid4())[:8]
    init_state(force_id=new_id)
    if hasattr(st, "query_params"): st.query_params["trip_id"] = new_id
    else: st.experimental_set_query_params(trip_id=new_id)
    st.rerun()

# 3. Trip Details (Title, Date, ID)
if ss.get("can_edit"):
    # --- EDIT MODE LAYOUT ---
    c1, c2, c3 = st.columns([3, 2, 2])
    with c1:
        new_name = st.text_input("Nome Viaggio", ss["trip_name"])
        if new_name != ss["trip_name"]:
            ss["trip_name"] = new_name
            mark_dirty()
    with c2:
        new_start = st.date_input("Inizio", ss["trip_start_date"])
        if new_start != ss["trip_start_date"]:
            ss["trip_start_date"] = new_start
            mark_dirty()
    with c3:
        st.text_input("Cloud ID", value=ss['current_trip_id'], disabled=True)
        st.caption("Condividi ID per collaborare.")
else:
    # --- READ-ONLY LAYOUT ---
    st.title(ss["trip_name"])
    st.write(f"📅 **Data Inizio:** {fmt_date(ss['trip_start_date'])}")


# ----------------------- Search / Add Stop (Edit Only) -----------------------
if ss.get("can_edit"):
    st.markdown("### Aggiungi tappa")
    selected_label = st_searchbox(
        search_api_labels,
        key=f"searchbox_{ss['search_key_version']}",
        placeholder="Cerca un luogo...",
        label="Cerca",
    )

    selection = None
    if selected_label:
        selection = ss.get("search_lookup", {}).get(selected_label)

    # 1. Update Preview if selection changes
    if selection and selected_label != ss["last_selected_label"]:
        ss["last_selected_label"] = selected_label
        ss["pending_preview"] = {"name": selection["name"], "lat": selection["lat"], "lon": selection["lon"]}
        ss["map_center"] = (selection["lat"], selection["lon"])

    # 2. Unified Add Form (Aligned)
    if ss["pending_preview"]:
        p = ss["pending_preview"]
        with st.container(border=True):
            st.markdown(f"**Selezionato:** {p['name']}")
            
            is_first = (len(ss["stops"]) == 0)
            is_same_place = False
            if not is_first:
                last = ss["stops"][-1]
                if last["name"] == p["name"]:
                    is_same_place = True
            
            with st.form("add_stop_form"):
                c_left, c_right = st.columns(2)
                
                with c_left:
                    st.caption("Dettagli Tappa")
                    name_override = st.text_input("Nome (opzionale)", value="")
                
                with c_right:
                    mode = "Auto"
                    leg_note = ""
                    if is_first:
                        st.caption("Punto di partenza")
                        st.info("Nessuno spostamento")
                    elif is_same_place:
                        st.caption("Stesso luogo")
                        st.info("Nessuno spostamento")
                    else:
                        st.caption(f"Spostamento da {ss['stops'][-1]['name']}")
                        mode = st.selectbox("Mezzo", ["Auto", "Treno", "Aereo", "Bus", "Altro"], index=0)

                c_note_l, c_note_r = st.columns(2)
                with c_note_l:
                    stop_note = st.text_input("Note tappa", value="")
                with c_note_r:
                    if not is_first and not is_same_place:
                        leg_note = st.text_input("Note spostamento", value="")

                overnight = st.checkbox("Pernottamento", value=True)

                st.write("") 
                if st.form_submit_button("Aggiungi Tappa", type="primary", use_container_width=True):
                    final_name = name_override.strip() if name_override.strip() else p["name"]
                    add_stop_internal(final_name, p["lat"], p["lon"], overnight, stop_note)
                    
                    if not is_first:
                        prev_idx = len(ss["stops"]) - 2
                        if is_same_place:
                            ss["legs_between"][prev_idx] = None
                            mark_dirty()
                        else:
                            m_map = {"Auto": "car", "Treno": "train", "Aereo": "plane", "Bus": "bus", "Altro": "car"}
                            internal_mode = m_map.get(mode, "car")
                            set_leg_between(prev_idx, internal_mode, leg_note)
                    
                    ss["pending_preview"] = None
                    ss["last_selected_label"] = None
                    ss["search_key_version"] += 1
                    st.rerun()

st.divider()

# Map
m = build_map(ss["stops"], ss["legs_between"])
st_folium(m, height=720, width=None, key=f"map_{ss['map_version']}", returned_objects=[])

st.divider()

# ------------------------------------------------------------------------------
# ITINERARY SECTION
# ------------------------------------------------------------------------------
st.markdown("## Itinerario")

# --- Drag & Drop (Keep your existing drag & drop code block here if you have it) ---
# [Paste your Drag & Drop code here if you want to keep it, otherwise skip]

if not ss["stops"]:
    st.info("Nessuna tappa. Aggiungine una cercando qui sopra." if ss.get("can_edit") else "Nessuna tappa definita.")
else:
    # 1. Calculate Day Blocks
    blocks = itinerary_day_blocks(ss["stops"], ss["legs_between"])
    
    # 2. Loop through each "Day"
    for b_idx, b in enumerate(blocks):
        # Calculate summary info for the header
        start_stop = ss["stops"][b["start"]]
        end_stop = ss["stops"][b["end"]]
        date_str = fmt_date(b["date"])
        
        # Header Logic
        time_parts = []
        if b["drive_seconds"] > 0:
            time_parts.append(f"Guida ({hhmm_from_seconds(b['drive_seconds'])})")
        
        modes = set()
        for li, leg in b["legs"]:
            if leg: modes.add(leg.get("mode", "car").lower())

        if "plane" in modes: time_parts.append("Volo")
        if "train" in modes: time_parts.append("Treno")
        if "bus" in modes: time_parts.append("Bus")
        
        time_str = " + ".join(time_parts)
        mid_part = f" · {time_str}" if time_str else ""
        
        if b["start"] == b["end"]:
            header = f"Giorno {b['day']} ({date_str}) · {start_stop['name']}"
        else:
            header = f"Giorno {b['day']} ({date_str}){mid_part} · {start_stop['name']} ➝ {end_stop['name']}"

        # 3. Display Logic
        # We use an expander for the visual container
        with st.expander(header, expanded=False):
            
            # --- EDIT MODE FOR THIS DAY ---
            # If we are editing THIS specific day
            if ss.get("editing_day_idx") == b_idx:
                
                st.markdown(f"#### ✏️ Modifica Giorno {b['day']}")
                
                # --- THE FORM (Solves the Slowness) ---
                # Everything inside here is "frozen" until you click Save
                with st.form(key=f"day_form_{b_idx}"):
                    
                    # We loop through every stop in this day block
                    for i in range(b["start"], b["end"] + 1):
                        s = ss["stops"][i]
                        k_sfx = f"{b_idx}_{i}"
                        
                        # A. STOP UI
                        st.markdown(f"**{i+1}. {s['name']}**")
                        c1, c2, c3 = st.columns([2, 3, 1])
                        with c1:
                            # We can't easily allow "Search" inside a bulk form without complex logic,
                            # so we stick to renaming. Use the top search bar to add new places.
                            new_name = st.text_input("Nome", value=s['name'], key=f"d_name_{k_sfx}")
                        with c2:
                            new_note = st.text_input("Note", value=s.get("note", ""), key=f"d_note_{k_sfx}")
                        with c3:
                            # User can toggle overnight status here
                            new_ov = st.checkbox("Notte", value=s.get("overnight", False), key=f"d_ov_{k_sfx}")

                        # B. LEG UI (If there is a next stop)
                        if i < len(ss["stops"]) - 1:
                            leg = ss["legs_between"][i]
                            
                            # Only show leg editor if it's NOT the last stop of the trip
                            # (Even if it's the last stop of the DAY, we might want to edit the overnight transport?
                            # Usually Day N ends at a stop. Leg i connects Stop i to Stop i+1.)
                            
                            # If this is the last stop of the BLOCK, the leg connects to the next day.
                            # We allow editing it here so you can set "Night Train" etc.
                            
                            st.caption(f"🔻 Spostamento verso {ss['stops'][i+1]['name']}")
                            
                            cur_mode = leg.get("mode", "—") if leg else "—"
                            cur_l_note = leg.get("note", "") if leg else ""
                            
                            l1, l2 = st.columns([1, 4])
                            with l1:
                                def fmt_mode(m):
                                    return {"car":"Auto","bus":"Bus","train":"Treno","plane":"Aereo","ferry":"Traghetto","—":"—"}.get(m, m)
                                
                                modes_list = ["—", "car", "bus", "train", "plane", "ferry"]
                                idx_mode = modes_list.index(cur_mode) if cur_mode in modes_list else 0
                                
                                new_mode = st.selectbox("Mezzo", modes_list, index=idx_mode, format_func=fmt_mode, key=f"d_mode_{k_sfx}", label_visibility="collapsed")
                            with l2:
                                new_l_note = st.text_input("Note viaggio", value=cur_l_note, key=f"d_lnote_{k_sfx}", label_visibility="collapsed", placeholder="Note spostamento...")
                            
                            st.divider()

                    # --- FORM ACTIONS ---
                    col_save, col_cancel = st.columns([1, 4])
                    with col_save:
                        submitted = st.form_submit_button("💾 Salva Giorno", type="primary")
                    with col_cancel:
                        # Forms don't have "Cancel" buttons nicely, so we use a flag check outside or just rerun
                        pass

                if submitted:
                    # 1. Update Data for every stop in the block
                    for i in range(b["start"], b["end"] + 1):
                        k_sfx = f"{b_idx}_{i}"
                        
                        # Update Stop
                        ss["stops"][i]["name"] = st.session_state[f"d_name_{k_sfx}"]
                        ss["stops"][i]["note"] = st.session_state[f"d_note_{k_sfx}"]
                        ss["stops"][i]["overnight"] = st.session_state[f"d_ov_{k_sfx}"]
                        
                        # Update Leg (if exists)
                        if i < len(ss["stops"]) - 1:
                            new_m = st.session_state[f"d_mode_{k_sfx}"]
                            new_ln = st.session_state[f"d_lnote_{k_sfx}"]
                            
                            old_leg = ss["legs_between"][i]
                            old_m = old_leg.get("mode") if old_leg else None
                            
                            # Recalculate if mode changed or didn't exist
                            needs_route = (new_m != "—") and (new_m != old_m or not old_leg)
                            
                            if new_m == "—":
                                ss["legs_between"][i] = None
                            elif needs_route:
                                # Logic to call API (Google or OSRM)
                                # We assume 'google_driving_route' exists from your previous code
                                # or fallback to 'osrm_driving_route'
                                A, B_stop = ss["stops"][i], ss["stops"][i+1]
                                
                                # Try Google first if available
                                try:
                                    if "google_driving_route" in globals():
                                        # Use helper if available, else simple
                                        if "get_stop_date" in globals():
                                            d_date = get_stop_date(i)
                                            route = google_driving_route(A["lat"], A["lon"], B_stop["lat"], B_stop["lon"], trip_date=d_date)
                                        else:
                                            route = google_driving_route(A["lat"], A["lon"], B_stop["lat"], B_stop["lon"])
                                        # Add source tag
                                        route["source"] = "google"
                                    else:
                                        route = osrm_driving_route(A["lat"], A["lon"], B_stop["lat"], B_stop["lon"])
                                except:
                                    route = interpolate_line(A["lat"], A["lon"], B_stop["lat"], B_stop["lon"])
                                    
                                if new_m == "plane":
                                    # Plane overrides geometry
                                    route["distance_m"] = None
                                    route["duration_s"] = None
                                    route["geometry_latlon"] = interpolate_line(A["lat"], A["lon"], B_stop["lat"], B_stop["lon"])
                                    
                                ss["legs_between"][i] = {"mode": new_m, "note": new_ln, **route}
                            else:
                                # Just update note
                                if ss["legs_between"][i]:
                                    ss["legs_between"][i]["note"] = new_ln

                    ss["editing_day_idx"] = None
                    mark_dirty()
                    st.rerun()

            else:
                # --- READ ONLY MODE (The Clean List) ---
                
                # Show stops
                for i in range(b["start"], b["end"] + 1):
                    s = ss["stops"][i]
                    st.markdown(stop_row_html(s), unsafe_allow_html=True)
                    
                    # Show Leg
                    if i < b["end"]: # Internal legs of the day
                        leg = ss["legs_between"][i]
                        if leg:
                            summ = leg_summary(leg)
                            note_md = f" — *{leg['note']}*" if leg.get("note") else ""
                            st.caption(f"🔻 **{summ}**{note_md}")
                
                # Show Button to Enter Edit Mode
                if ss.get("can_edit"):
                    st.button("✏️ Modifica Giorno", key=f"edit_day_btn_{b_idx}", on_click=lambda idx=b_idx: ss.update({"editing_day_idx": idx}))

# Autosave
if ss.get("dirty", False):
    save_to_supabase()


# ==============================================================================
# FOOTER: DANGER ZONE (Soft Delete)
# ==============================================================================
if ss.get("can_edit"):
    st.divider()
    with st.expander("Elimina Viaggio"):
        st.write(f"Stai per eliminare: **{ss['trip_name']}**")
        st.caption("Il viaggio verrà nascosto dalla lista, ma rimarrà nel database (potrai ripristinarlo manualmente da Supabase rimuovendo il flag 'is_deleted').")
        
        # Use session state to handle the confirmation flow
        if "confirm_delete" not in ss:
            ss["confirm_delete"] = False

        if not ss["confirm_delete"]:
            if st.button("🗑 Nascondi/Elimina Viaggio"):
                ss["confirm_delete"] = True
                st.rerun()
        else:
            st.warning("Sei sicuro? Il viaggio non sarà più visibile nell'app.")
            col_d1, col_d2 = st.columns([1, 1])
            with col_d1:
                if st.button("❌ Annulla"):
                    ss["confirm_delete"] = False
                    st.rerun()
            with col_d2:
                if st.button("✅ Conferma Eliminazione", type="primary"):
                    # 1. Prepare data with is_deleted = True
                    trip_id = ss["current_trip_id"]
                    data_to_save = {
                        "name": ss["trip_name"],
                        "trip_start_date": ss["trip_start_date"].isoformat(),
                        "stops": ss["stops"],
                        "legs_between": ss["legs_between"],
                        "last_updated": datetime.now().isoformat(),
                        "is_deleted": True  # <--- The Soft Delete Flag
                    }

                    # 2. Push to Supabase
                    try:
                        supabase.table("itineraries").upsert({
                            "trip_id": trip_id, 
                            "trip_data": data_to_save
                        }).execute()
                        
                        st.success("Viaggio eliminato.")
                        
                        # 3. SAFER RESET STATE
                        # We do NOT delete 'stops' key to avoid KeyError in the sidebar.
                        # Instead, we delete 'initialized' and 'current_trip_id' so init_state() 
                        # knows it must run fresh and overwrite the data.
                        
                        # 3. SAFER RESET STATE
                        # We force init_state to run from scratch
                        keys_to_clear = ["initialized", "current_trip_id", "stops", "legs_between", "trip_name"]
                        for k in keys_to_clear:
                            if k in ss:
                                del ss[k]
                        
                        # Clear URL
                        if hasattr(st, "query_params"): 
                            st.query_params.clear()
                        else:
                            st.experimental_set_query_params()
                            
                        # Force reload
                        st.rerun()
                        
                        
                    except Exception as e:
                        st.error(f"Errore durante l'eliminazione: {e}")
