# streamlit_app.py
#
# Streamlit itinerary planner (Google Geocoding + Google Maps Routing) + Supabase Sync + PIN Protection
#
# Install:
#   pip install streamlit folium streamlit-folium requests polyline streamlit-searchbox streamlit-sortables supabase fpdf2 googlemaps
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
from fpdf.enums import XPos, YPos
import hashlib
import googlemaps 

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
    if hasattr(st, "query_params"):
        pre_id = st.query_params.get("trip_id")
    else:
        pre_qp = st.experimental_get_query_params()
        pre_id = pre_qp.get("trip_id", [None])[0]

    if pre_id:
        res = supabase.table("itineraries").select("trip_data").eq("trip_id", pre_id).execute()
        if res.data and len(res.data) > 0:
            browser_tab_title = res.data[0]["trip_data"].get("name", "Viaggio")
except Exception:
    pass 

st.set_page_config(page_title=browser_tab_title, layout="wide", page_icon="🗺️")


# ==============================================================================
# SECURITY: TRIP TOKENS
# ==============================================================================
def get_trip_token(trip_id: str) -> str:
    secret = st.secrets.get("APP_PIN", "default_secret")
    msg = f"{trip_id}{secret}"
    return hashlib.sha256(msg.encode()).hexdigest()[:12]

def validate_trip_token(trip_id: str, token: str) -> bool:
    if not token or not trip_id:
        return False
    expected = get_trip_token(trip_id)
    return hmac.compare_digest(expected, token)

# ==============================================================================
# CONSTANTS & SETUP
# ==============================================================================
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
def get_all_trips_summary():
    try:
        res = supabase.table("itineraries").select("trip_id, trip_data").execute()
        trips = []
        for row in res.data:
            t_data = row.get("trip_data", {})
            if t_data.get("is_deleted") is True:
                continue
            name = t_data.get("name", "Senza Nome")
            last_updated = t_data.get("last_updated", "")
            tid = row.get("trip_id")
            if tid:
                trips.append({"id": tid, "name": name, "last_updated": last_updated})
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

def init_state(force_id: str = None):
    ss = st.session_state
    
    if force_id:
        if "initialized" in ss: del ss["initialized"]
        ss["current_trip_id"] = force_id
    
    if "initialized" in ss and "stops" in ss:
        return

    url_id = force_id or get_trip_id_from_url()
    target_id = None
    
    if url_id:
        target_id = url_id
    else:
        existing_trips = get_all_trips_summary()
        if existing_trips:
            most_recent = max(existing_trips, key=lambda x: x.get("last_updated", "") or "")
            target_id = most_recent["id"]
        else:
            target_id = str(uuid.uuid4())[:8]

    data = load_from_supabase(target_id)
    
    if data:
        ss["current_trip_id"] = target_id
        populate_state_from_data(data)
    else:
        ss["current_trip_id"] = target_id
        set_defaults() 

    if hasattr(st, "query_params"):
        st.query_params["trip_id"] = target_id
    else:
        st.experimental_set_query_params(trip_id=target_id)

    ss["initialized"] = True
    
def get_unique_new_trip_name():
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
    ss["trip_name"] = get_unique_new_trip_name()
    ss["stops"] = []
    ss["legs_between"] = []
    ss["trip_start_date"] = date.today()
    ss["next_stop_id"] = 1
    ss["map_center"] = None
    ss["dirty"] = True
    ss["map_version"] = 0
    ss["search_lookup"] = {}
    ss["last_selected_label"] = None
    ss["search_key_version"] = 0
    ss["sortable_key_version"] = 0
    ss["pending_stop"] = None
    ss["pending_preview"] = None
    ss["show_editor"] = False
    ss["editing_stop_idx"] = None
    ss.setdefault("user_agent", DEFAULT_USER_AGENT)
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
    if "stops" not in ss:
        return
    needed = max(0, len(ss["stops"]) - 1)
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
    return d.strftime("%d/%m/%y") if d else ""


# ----------------------- External calls (GOOGLE GEOCODING & DIRECTIONS) -----------------------
@st.cache_data(ttl=3600, show_spinner=False)
def fetch_locations_google(query: str) -> List[Dict]:
    """
    Uses Google Geocoding API to find places.
    Requires 'Geocoding API' to be enabled in Google Cloud Console.
    """
    if not query or len(query) < 2:
        return []

    try:
        results_raw = gmaps.geocode(query, language='it')
        
        results = []
        for r in results_raw:
            display_name = r.get("formatted_address", query)
            loc = r.get("geometry", {}).get("location", {})
            
            if loc:
                results.append({
                    "name": display_name,
                    "lat": float(loc["lat"]),
                    "lon": float(loc["lng"]),
                })
        return results
    except Exception as e:
        return []


def google_driving_route(lat1: float, lon1: float, lat2: float, lon2: float) -> Dict:
    """
    Fetches driving route from Google Maps Directions API.
    """
    try:
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
        encoded_poly = route['overview_polyline']['points']
        coords_latlon = polyline_lib.decode(encoded_poly)

        return {
            "distance_m": float(leg['distance']['value']),
            "duration_s": float(leg['duration']['value']),
            "geometry_latlon": coords_latlon,
        }
    except Exception:
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
    return f"{h}h{m:02d}m"


def driving_seconds_for_leg(leg: Optional[Dict]) -> int:
    if not leg:
        return 0
    mode = (leg.get("mode") or "").lower()
    # CHANGE: Only count 'car'/'auto'. Train/Bus/Plane = 0 driving time.
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
    
    status = leg.get("booking_status", "todo")
    status_icon = ""
    if status == "booked": status_icon = " ✅"
    elif status == "provisional": status_icon = " ⚠️"
    
    # CHANGE: Only show distance/time for CAR. Train/Bus behave like Plane.
    if mode_raw in {"car", "auto"} and leg.get("distance_m") is not None and leg.get("duration_s") is not None:
        km = leg["distance_m"] / 1000.0
        hhmm = hhmm_from_seconds(leg["duration_s"])
        return f"{display_mode}{status_icon} · {km:.1f} km · {hhmm}"
        
    return f"{display_mode}{status_icon}"


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


from folium.features import DivIcon

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

    # -----------------------------------------------------------
    # 1. GROUP STOPS (Same Logic as before)
    # -----------------------------------------------------------
    grouped_markers = {}
    current_day = 1

    for i, s in enumerate(stops):
        is_overnight = s.get("overnight")
        arr_date = day_to_date(current_day)
        dep_day = current_day + 1 if is_overnight else current_day
        dep_date = day_to_date(dep_day)
        
        k = (round(s["lat"], 4), round(s["lon"], 4))
        
        date_str_arr = fmt_date(arr_date)
        date_str_dep = fmt_date(dep_date)
        
        if is_overnight:
            visit_text = f"Giorno {current_day} ({date_str_arr}) ➝ {dep_day} ({date_str_dep})"
        else:
            visit_text = f"Giorno {current_day} ({date_str_arr})"

        # Status Priority: todo=0, provisional=1, booked=2
        status_priority = {"todo": 0, "provisional": 1, "booked": 2}
        this_status_val = status_priority.get(s.get("booking_status", "todo"), 0)

        if k not in grouped_markers:
            grouped_markers[k] = {
                "lat": s["lat"],
                "lon": s["lon"],
                "name": s["name"],
                "visits": [visit_text],
                "notes": [s.get("note", "").strip()],
                "statuses": [this_status_val],
                "is_overnight": is_overnight,
                "last_seen_index": i
            }
        else:
            existing = grouped_markers[k]
            # Consecutive Merge Check
            if existing.get("last_seen_index") == i - 1:
                parts = existing["visits"][-1].split("➝")
                start_part = parts[0]
                existing["visits"][-1] = f"{start_part} ➝ {dep_day} ({date_str_dep})"
                existing["last_seen_index"] = i
                # For consecutive nights, we usually want to KEEP the 'worst' status or just append.
                # Here, we append the status so we can visualize the "split" (e.g. Night 1 Booked, Night 2 Todo)
                existing["statuses"].append(this_status_val)
            else:
                # Return visit
                existing["visits"].append(visit_text)
                existing["statuses"].append(this_status_val)
                existing["last_seen_index"] = i
            
            if s.get("note"):
                existing["notes"].append(s.get("note").strip())
            if is_overnight: existing["is_overnight"] = True
        
        if is_overnight:
            current_day += 1

    # -----------------------------------------------------------
    # 2. DRAW MARKERS (Standard vs Multi-Pin)
    # -----------------------------------------------------------
    for k, data in grouped_markers.items():
        count = len(data["statuses"])
        
        # --- BUILD POPUP CONTENT ---
        popup_html = f"<b>{data['name']}</b><hr style='margin:4px 0'>"
        for v in data["visits"]:
            popup_html += f"• {v}<br>"
        
        unique_notes = list(set([n for n in data["notes"] if n]))
        if unique_notes:
            popup_html += "<hr style='margin:4px 0'><b>Note:</b><br>"
            for n in unique_notes:
                popup_html += f"- {n}<br>"
        
        # Tooltip
        tooltip = f"{data['name']} ({count} step)" if count > 1 else f"{data['name']}"

        # --- DRAWING LOGIC ---
        
        # Colors: 0=Red, 1=Orange, 2=Green
        def get_color_hex(val):
            if val == 2: return "#22c55e" # Green
            if val == 1: return "#f97316" # Orange
            return "#ef4444"              # Red

        if count == 1:
            # STANDARD SINGLE MARKER (Looks cleaner for single stops)
            val = data["statuses"][0]
            if val == 2: c = "green"
            elif val == 1: c = "orange"
            else: c = "red"
            
            icon = folium.Icon(color=c, icon="bed" if data["is_overnight"] else "map-pin", prefix="fa")
            folium.Marker(
                [data["lat"], data["lon"]],
                popup=popup_html,
                tooltip=tooltip,
                icon=icon
            ).add_to(m)
            
        else:
            # MULTI-PIN "FAN" (DivIcon)
            # We construct HTML to overlay FontAwesome icons slightly offset
            
            icons_html = ""
            # We limit to 4 icons max to prevent a massive line if you stay 14 nights
            display_limit = min(count, 4) 
            
            # Base width: 24px per icon, but overlapping by 12px
            overlap = 14
            total_width = (display_limit * overlap) + 10 
            
            for idx in range(display_limit):
                c_hex = get_color_hex(data["statuses"][idx])
                # Offset calculation
                left_pos = idx * overlap
                z_index = 100 - idx # Draw first on top, or last on top? Let's put First on TOP.
                
                icons_html += f"""
                <div style="position: absolute; left: {left_pos}px; top: 0; z-index: {z_index};">
                    <i class="fa fa-map-marker" style="font-size: 32px; color: {c_hex}; 
                    text-shadow: -1px -1px 0 #fff, 1px -1px 0 #fff, -1px 1px 0 #fff, 1px 1px 0 #fff;"></i>
                </div>
                """
                # Note: The text-shadow creates a white border around the pin so they don't blend together
            
            # Small badge if we have more than 4
            if count > 4:
                icons_html += f"""
                <div style="position: absolute; left: {left_pos + 20}px; top: 0; background: white; border:1px solid #ccc; border-radius:50%; padding: 2px 5px; font-size: 10px; font-weight: bold;">+{count-4}</div>
                """

            div_icon = DivIcon(
                icon_size=(total_width, 36),
                icon_anchor=(total_width / 2, 36), # Center horizontally, anchor at bottom
                html=f'<div style="position: relative; width: {total_width}px; height: 36px;">{icons_html}</div>'
            )
            
            folium.Marker(
                [data["lat"], data["lon"]],
                popup=popup_html,
                tooltip=tooltip,
                icon=div_icon
            ).add_to(m)

    # 3. DRAW LEGS (Standard)
    current_day = 1
    for i in range(len(stops) - 1):
        leg = legs_between[i] if i < len(legs_between) else None
        s_prev = stops[i]
        is_prev_overnight = s_prev.get("overnight")
        leg_day = current_day + 1 if is_prev_overnight else current_day
        leg_date_str = fmt_date(day_to_date(leg_day))
        if is_prev_overnight: current_day += 1

        if not leg or not leg.get("geometry_latlon"): continue

        mode = (leg.get("mode") or "").lower()
        booking_status = leg.get("booking_status", "todo")
        color = "green" if booking_status == "booked" else "orange" if booking_status == "provisional" else "#3388ff"
        
        dash = None
        if mode == "bus": dash = "3, 8"
        elif mode == "train": dash = "10, 10"
        elif mode == "plane": dash = "20, 20"

        mode_map = {"car": "Auto", "bus": "Bus", "train": "Treno", "plane": "Aereo", "ferry": "Traghetto"}
        display_mode = mode_map.get(mode, mode.title())

        tooltip = f"Giorno {leg_day} ({leg_date_str}) · {display_mode} · {stops[i]['name']} → {stops[i+1]['name']}"
        if leg.get("duration_s") is not None:
            tooltip += f" · {hhmm_from_seconds(leg['duration_s'])}"
        if leg.get("distance_m") is not None:
            tooltip += f" · {leg['distance_m']/1000:.1f} km"

        folium.PolyLine(
            leg["geometry_latlon"],
            weight=4, opacity=0.9, dash_array=dash, tooltip=tooltip, color=color,
        ).add_to(m)

    return m


# ----------------------- Add stop / legs -----------------------
def add_stop_internal(name: str, lat: float, lon: float, overnight: bool, note: str, booking_status: str, leg_status: str):
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
            "booking_status": booking_status, 
            "note": (note or "").strip(),
        }
    )
    ensure_legs_alignment()
    ss["map_center"] = (float(lat), float(lon))
    mark_dirty()


def set_leg_between(prev_idx: int, mode: str, note: str, status: str = "todo"):
    ss = st.session_state
    a = ss["stops"][prev_idx]
    b = ss["stops"][prev_idx + 1]
    mode = (mode or "").lower()

    leg: Optional[Dict] = {"mode": mode, "note": (note or "").strip(), "booking_status": status}

    # CHANGE: Only call Google Driving Route for CAR/AUTO
    if mode in {"car", "auto"}:
        leg.update(google_driving_route(a["lat"], a["lon"], b["lat"], b["lon"]))
    else:
        # Train, Bus, Plane, Ferry, etc. -> Straight line, no duration
        leg.update({
            "distance_m": None,
            "duration_s": None,
            "geometry_latlon": interpolate_line(a["lat"], a["lon"], b["lat"], b["lon"]),
        })

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

        # Force Google Routing on Reorder (Default to CAR for recalculation)
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
    
    # CALLS THE NEW GOOGLE FUNCTION
    results = fetch_locations_google(q)
    
    lookup: Dict[str, Dict] = {}
    labels: List[str] = []
    for r in results:
        # PURE SEARCH NAME ONLY (Google Formatted Address)
        label = r["name"]
        lookup[label] = r
        if label not in labels:
            labels.append(label)

    ss["search_lookup"] = lookup
    return labels


# ----------------------- UI helpers -----------------------
def stop_row_html(s: Dict) -> str:
    overnight = bool(s.get("overnight", False))
    status = s.get("booking_status", "todo")
    
    if not overnight:
        bg = "#ffffff"
        border = "#e5e7eb"
        badge = ""
    else:
        # Define Badge & Colors based on Status
        if status == "booked":
            bg = "#dcfce7" # Light green
            border = "#86efac"
            badge = "✅ Prenotato"
        elif status == "provisional":
            bg = "#ffedd5" # Light orange
            border = "#fdba74"
            badge = "⚠️ Provvisorio"
        else: # todo
            bg = "#fee2e2" # Light red
            border = "#fca5a5"
            badge = "🛑 Da prenotare"

    note = (s.get("note") or "").strip()
    note_html = f"<div style='margin-top:6px;color:#444;font-size:0.92rem;'><em>{note}</em></div>" if note else ""
    return f"""
    <div style="background:{bg};border:1px solid {border};border-radius:12px;padding:12px 14px;margin:8px 0;">
      <div style="display:flex;gap:10px;align-items:baseline;justify-content:space-between;">
        <div style="font-size:1.02rem;"><strong>{s['name']}</strong></div>
        <div style="font-size:0.85rem;color:#333;font-weight:bold;">{badge}</div>
      </div>
      {note_html}
    </div>
    """

# ----------------------- PDF Generator (Fixed Layout) -----------------------
class TripPDF(FPDF):
    def header(self):
        if self.page_no() > 1:
            self.set_font("Helvetica", "I", 8)
            self.set_text_color(128, 128, 128)
            name = st.session_state.get("trip_name", "Viaggio")
            safe_name = name.encode('latin-1', 'replace').decode('latin-1')
            self.cell(0, 10, safe_name, border=False, align="R")
            self.ln(10)

    def footer(self):
        self.set_y(-15)
        self.set_font("Helvetica", "I", 8)
        self.set_text_color(128, 128, 128)
        self.cell(0, 10, f"Pagina {self.page_no()}/{{nb}}", align="C")

def generate_pdf_bytes(trip_name, start_date, stops, legs):
    pdf = TripPDF()
    pdf.alias_nb_pages()
    pdf.add_page()
    pdf.set_auto_page_break(auto=True, margin=25) 
    
    pdf.set_font("Helvetica", "B", 24)
    safe_name = trip_name.encode('latin-1', 'replace').decode('latin-1')
    pdf.cell(0, 10, safe_name.upper(), new_x=XPos.LMARGIN, new_y=YPos.NEXT, align="L")
    
    pdf.set_font("Helvetica", "", 12)
    pdf.set_text_color(50, 50, 50)
    pdf.cell(0, 10, f"Inizio viaggio: {start_date.strftime('%d/%m/%Y')}", new_x=XPos.LMARGIN, new_y=YPos.NEXT)
    pdf.ln(10)
    
    blocks = itinerary_day_blocks(stops, legs)
    
    for b in blocks:
        if pdf.get_y() > 250: 
            pdf.add_page()

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
        
        pdf.cell(0, 10, safe_header, new_x=XPos.LMARGIN, new_y=YPos.NEXT, fill=True, border=False)
        pdf.ln(2)
        
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
            pdf.cell(0, 6, name, new_x=XPos.LMARGIN, new_y=YPos.NEXT)
            
            if note:
                pdf.set_x(x + 3)
                pdf.set_font("Helvetica", "I", 9)
                pdf.set_text_color(80, 80, 80)
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
                    pdf.cell(0, 5, full_leg, new_x=XPos.LMARGIN, new_y=YPos.NEXT)
                    pdf.ln(1)
        pdf.ln(4)
    return bytes(pdf.output())


# ======================= APP =======================
init_state()
ensure_legs_alignment()
ss = st.session_state

# ==============================================================================
# GATEKEEPER
# ==============================================================================
def check_access():
    ss = st.session_state
    if ss.get("can_edit"):
        return True
    if "allowed_view_ids" not in ss:
        ss["allowed_view_ids"] = set()

    if hasattr(st, "query_params"):
        qp = st.query_params
        url_token = qp.get("token")
        url_trip = qp.get("trip_id")
    else:
        qp = st.experimental_get_query_params()
        url_token = qp.get("token", [None])[0]
        url_trip = qp.get("trip_id", [None])[0]

    if url_trip and url_token:
        if validate_trip_token(url_trip, url_token):
            ss["allowed_view_ids"].add(url_trip)
            if ss["current_trip_id"] == url_trip:
                return True

    if ss.get("current_trip_id") and ss.get("current_trip_id") in ss.get("allowed_view_ids", set()):
        return True

    st.markdown("### 🔒 Accesso Limitato")
    st.caption("Inserisci il PIN Amministratore o un Token Viaggio.")
    user_input = st.text_input("PIN o Token", type="password")
    
    if user_input:
        secret_pin = st.secrets.get("APP_PIN", "0000")
        if hmac.compare_digest(user_input, str(secret_pin)):
            ss["can_edit"] = True
            st.rerun()
        elif validate_trip_token(ss["current_trip_id"], user_input):
            ss["allowed_view_ids"].add(ss["current_trip_id"])
            st.rerun()
        else:
            st.error("Accesso negato")
    return False

if not check_access():
    st.stop()

# ==============================================================================
# ----------------------- Sidebar -----------------------
with st.sidebar:
    st.header("⚙️ Menu")
    
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
    
    if ss.get("can_edit"):    
        st.divider()
        st.markdown("**🛠️ Strumenti**")
    
        if st.button("🔄 Aggiorna tutto con Google Maps"):
            progress_bar = st.progress(0)
            status_text = st.empty()
            stops = ss["stops"]
            legs = ss["legs_between"]
            total = len(stops) - 1
            updated_count = 0
            for i in range(total):
                leg = legs[i]
                if leg and leg.get("mode") in {"car", "bus", "train"}:
                    status_text.text(f"Ricalcolo tratta {i+1} di {total}...")
                    A = stops[i]
                    B = stops[i+1]
                    try:
                        # CHANGE: Only Google if car/auto
                        if leg.get("mode") in {"car", "auto"}:
                            new_data = google_driving_route(A["lat"], A["lon"], B["lat"], B["lon"])
                            new_data["source"] = "google"
                            leg.update(new_data)
                            updated_count += 1
                        else:
                            # If it was train/bus, strip the driving data
                            leg.update({
                                "distance_m": None, 
                                "duration_s": None,
                                "geometry_latlon": interpolate_line(A["lat"], A["lon"], B["lat"], B["lon"])
                            })
                    except Exception as e:
                        st.warning(f"Errore tratta {i+1}: {e}")
                progress_bar.progress((i + 1) / total)
            status_text.text("Salvataggio in corso...")
            mark_dirty()
            save_to_supabase() 
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
            
    if ss.get("can_edit"):
        st.divider()
        st.markdown("**🔗 Condivisione (Sola Lettura)**")
        tid = ss["current_trip_id"]
        token = get_trip_token(tid)
        magic_link = "https://itinerari-picaciam.streamlit.app"+f"?trip_id={tid}&token={token}"
        st.code(magic_link, language="text")
        st.caption(f"Chi ha questo link può vedere **solo** il viaggio '{ss['trip_name']}'.")
        st.divider()
    
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
    
    if ss.get("can_edit"):
        uploaded_file = st.file_uploader("⬆️ Importa JSON", type=["json"])
        if uploaded_file is not None:
            try:
                new_data = json.load(uploaded_file)
                current_stops = ss.get("stops", [])
                has_existing_data = len(current_stops) > 0

                def perform_import():
                    populate_state_from_data(new_data)
                    mark_dirty() 
                    st.success("Caricato!")
                    st.rerun()

                if not has_existing_data:
                    perform_import()
                else:
                    st.warning(f"⚠️ Il viaggio attuale ha già {len(current_stops)} tappe.")
                    st.markdown("Importando il file **sovrascriverai** tutto.")
                    if st.button("✅ Conferma Sovrascrittura", type="primary", key="confirm_import_btn"):
                        perform_import()
            except Exception as e:
                st.error(f"Errore nel file: {e}")
                
    st.caption(f"ID: `{ss['current_trip_id']}`")


# ----------------------- Main Header -----------------------
all_trips = get_all_trips_summary()
if ss.get("can_edit"):
    available_trips = all_trips
    trip_options = ["NEW"] + [t["id"] for t in available_trips]
else:
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

try:
    current_idx = trip_options.index(ss["current_trip_id"])
except ValueError:
    current_idx = 0

c_sel, c_rest = st.columns([1, 3])
with c_sel:
    if len(trip_options) > 1:
        selected_trip = st.selectbox(
            "Viaggio", 
            options=trip_options, 
            index=current_idx,
            format_func=format_trip_option,
            label_visibility="collapsed"
        )
    else:
        selected_trip = ss["current_trip_id"]

if selected_trip != "NEW" and selected_trip != ss["current_trip_id"]:
    init_state(force_id=selected_trip)
    if hasattr(st, "query_params"): st.query_params["trip_id"] = selected_trip
    else: st.experimental_set_query_params(trip_id=selected_trip)
    st.rerun()
elif selected_trip == "NEW" and ss["current_trip_id"] in [t["id"] for t in all_trips]:
    new_id = str(uuid.uuid4())[:8]
    init_state(force_id=new_id)
    if hasattr(st, "query_params"): st.query_params["trip_id"] = new_id
    else: st.experimental_set_query_params(trip_id=new_id)
    st.rerun()

if ss.get("can_edit"):
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

    if selection and selected_label != ss["last_selected_label"]:
        ss["last_selected_label"] = selected_label
        ss["pending_preview"] = {"name": selection["name"], "lat": selection["lat"], "lon": selection["lon"]}
        ss["map_center"] = (selection["lat"], selection["lon"])

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
            
            # --- FORM START ---
            with st.form("add_stop_form"):
                # NO NAME FIELD - Use search result directly
                c_mode, c_dummy = st.columns([1, 1])
                
                with c_mode:
                    mode = "Auto"
                    leg_note = ""
                    leg_status = "todo"
                    
                    if is_first:
                        st.info("Punto di partenza (Nessuno spostamento)")
                    elif is_same_place:
                        st.info("Stesso luogo (Nessuno spostamento)")
                    else:
                        st.caption(f"Spostamento da {ss['stops'][-1]['name']}")
                        # We use cols for mode and status
                        m_col, s_col = st.columns([1, 1])
                        with m_col:
                            mode = st.selectbox("Mezzo", ["Auto", "Treno", "Aereo", "Bus", "Altro"], index=0)
                        with s_col:
                            leg_status = st.selectbox(
                                "Stato Spostamento", 
                                ["todo", "provisional", "booked"], 
                                format_func=lambda x: {"todo": "Da fare", "provisional": "Provvisorio", "booked": "Prenotato"}.get(x, x),
                                index=0
                            )

                c_note_l, c_note_r = st.columns(2)
                with c_note_l:
                    stop_note = st.text_input("Note tappa", value="")
                with c_note_r:
                    if not is_first and not is_same_place:
                        leg_note = st.text_input("Note spostamento", value="")

                # Booking status for Stop
                c_ov_cb, c_ov_st = st.columns([1, 2])
                with c_ov_cb:
                    st.write("")
                    st.write("")
                    overnight = st.checkbox("Pernottamento", value=True)
                with c_ov_st:
                    if overnight:
                        booking_status = st.selectbox(
                            "Stato Hotel", 
                            ["todo", "provisional", "booked"], 
                            format_func=lambda x: {"todo": "Da prenotare", "provisional": "Provvisorio", "booked": "Prenotato"}.get(x, x),
                            index=0
                        )
                    else:
                        booking_status = "todo"

                st.write("") 
                if st.form_submit_button("Aggiungi Tappa", type="primary", use_container_width=True):
                    final_name = p["name"] # Explicitly use Search Name
                    add_stop_internal(final_name, p["lat"], p["lon"], overnight, stop_note, booking_status, leg_status)
                    
                    if not is_first:
                        prev_idx = len(ss["stops"]) - 2
                        if is_same_place:
                            ss["legs_between"][prev_idx] = None
                            mark_dirty()
                        else:
                            m_map = {"Auto": "car", "Treno": "train", "Aereo": "plane", "Bus": "bus", "Altro": "car"}
                            internal_mode = m_map.get(mode, "car")
                            set_leg_between(prev_idx, internal_mode, leg_note, leg_status)
                    
                    ss["pending_preview"] = None
                    ss["last_selected_label"] = None
                    ss["search_key_version"] += 1
                    st.rerun()

st.divider()

m = build_map(ss["stops"], ss["legs_between"])
st_folium(m, height=720, width=None, key=f"map_{ss['map_version']}", returned_objects=[])

st.divider()

# ------------------------------------------------------------------------------
# ITINERARY SECTION
# ------------------------------------------------------------------------------
st.markdown("## Itinerario")

if not ss.get("stops"):
    st.info("Nessuna tappa. Aggiungine una cercando qui sopra." if ss.get("can_edit") else "Nessuna tappa definita.")
else:
    blocks = itinerary_day_blocks(ss["stops"], ss["legs_between"])
    
    for b_idx, b in enumerate(blocks):
        start_stop = ss["stops"][b["start"]]
        end_stop = ss["stops"][b["end"]]
        date_str = fmt_date(b["date"])
        
        time_parts = []
        if b["drive_seconds"] > 0:
            time_parts.append(f"Guida ({hhmm_from_seconds(b['drive_seconds'])})")
        
        modes = set()
        for li, leg in b["legs"]:
            if leg: modes.add(leg.get("mode", "car").lower())
        
        # --- FIX: REMOVE PREVIOUS DAY'S LEG FROM HEADER SUMMARY ---
        # Only internal legs count for the day's "Type"
        # The incoming leg (e.g., arrival flight) is shown in the Edit UI but
        # usually doesn't count as "This day's travel" for the header summary.
        
        if "plane" in modes: time_parts.append("Volo")
        if "train" in modes: time_parts.append("Treno")
        
        time_str = " + ".join(time_parts)
        mid_part = f" · {time_str}" if time_str else ""
        
        header = f"Giorno {b['day']} ({date_str}){mid_part} · {start_stop['name']}"
        if b["start"] != b["end"]:
            header += f" ➝ {end_stop['name']}"

        with st.expander(header, expanded=False):
            if ss.get("editing_day_idx") != b_idx:
                for i in range(b["start"], b["end"] + 1):
                    s = ss["stops"][i]
                    st.markdown(stop_row_html(s), unsafe_allow_html=True)
                    if i < b["end"]:
                        leg = ss["legs_between"][i]
                        if leg:
                            summ = leg_summary(leg)
                            # --- FIX: .strip() ensures "*note*" becomes italic instead of literal ---
                            note_txt = leg['note'].strip() if leg.get("note") else ""
                            note_md = f" — *{note_txt}*" if note_txt else ""
                            
                            st.caption(f"🔻 **{summ}**{note_md}")
                
                if ss.get("can_edit"):
                    st.button("✏️ Modifica Giorno", key=f"btn_edit_{b_idx}", on_click=lambda idx=b_idx: ss.update({"editing_day_idx": idx}))
            
            # EDIT MODE
            else:
                st.markdown(f"#### ✏️ Modifica Giorno {b['day']}")
                
                # 1. Incoming Leg
                if b["start"] > 0:
                    prev_stop = ss["stops"][b["start"]-1]
                    inc_leg = ss["legs_between"][b["start"]-1]
                    st.caption(f"🏁 **Arrivo da {prev_stop['name']}**")
                    
                    cur_m = inc_leg.get("mode", "—") if inc_leg else "—"
                    cur_n = inc_leg.get("note", "") if inc_leg else ""
                    cur_s = inc_leg.get("booking_status", "todo") if inc_leg else "todo"
                    
                    c1, c2, c3 = st.columns([1, 1, 3])
                    with c1:
                        def fmt_m(m): return {"car":"Auto","bus":"Bus","train":"Treno","plane":"Aereo","ferry":"Traghetto","—":"—"}.get(m, m)
                        modes = ["—", "car", "bus", "train", "plane", "ferry"]
                        idx_m = modes.index(cur_m) if cur_m in modes else 0
                        
                        def update_inc_leg(idx=b["start"]-1):
                            # FIX: Use .get() to prevent KeyError on Enter
                            n_m = st.session_state.get(f"inc_mode_{b_idx}")
                            n_n = st.session_state.get(f"inc_note_{b_idx}")
                            n_s = st.session_state.get(f"inc_stat_{b_idx}")
                            
                            # Safety check: if keys are missing during reload, skip update
                            if n_m is None: return

                            old = ss["legs_between"][idx]
                            
                            if n_m == "—":
                                ss["legs_between"][idx] = None
                            elif (n_m != "—") and (not old or old.get("mode") != n_m):
                                A, B = ss["stops"][idx], ss["stops"][idx+1]
                                # CHANGE: Only call Google if car/auto
                                if n_m in {"car", "auto"}:
                                    try:
                                        rt = google_driving_route(A["lat"], A["lon"], B["lat"], B["lon"])
                                        rt["source"] = "google"
                                    except: rt = interpolate_line(A["lat"], A["lon"], B["lat"], B["lon"])
                                else:
                                    # Plane/Train/Bus = Straight Line, no duration
                                    rt = {
                                        "distance_m": None, 
                                        "duration_s": None, 
                                        "geometry_latlon": interpolate_line(A["lat"], A["lon"], B["lat"], B["lon"])
                                    }
                                
                                rt.update({"mode": n_m, "note": n_n, "booking_status": n_s})
                                ss["legs_between"][idx] = rt
                            elif ss["legs_between"][idx]:
                                ss["legs_between"][idx]["note"] = n_n
                                ss["legs_between"][idx]["booking_status"] = n_s
                            mark_dirty()

                        st.selectbox("Mezzo", modes, index=idx_m, format_func=fmt_m, key=f"inc_mode_{b_idx}", on_change=update_inc_leg)
                    with c2:
                         st.selectbox("Stato", ["todo", "provisional", "booked"], index=["todo", "provisional", "booked"].index(cur_s), format_func=lambda x: {"todo": "Da fare", "provisional": "Provvisorio", "booked": "Prenotato"}.get(x, x), key=f"inc_stat_{b_idx}", on_change=update_inc_leg)
                    with c3:
                        st.text_input("Note Arrivo", value=cur_n, key=f"inc_note_{b_idx}", on_change=update_inc_leg)
                    st.divider()

                # 2. Stops Loop
                for i in range(b["start"], b["end"] + 1):
                    s = ss["stops"][i]
                    k_sfx = f"{b_idx}_{i}"
                    
                    new_loc = st_searchbox(
                        search_api_labels,
                        key=f"search_{k_sfx}",
                        placeholder=f"📍 Cambia luogo per {s['name']}...",
                        label=None
                    )
                    
                    if new_loc:
                        found = ss.get("search_lookup", {}).get(new_loc)
                        if found:
                            lat_diff = abs(s["lat"] - found["lat"])
                            lon_diff = abs(s["lon"] - found["lon"])
                            
                            if lat_diff > 0.0001 or lon_diff > 0.0001:
                                ss["stops"][i]["lat"] = found["lat"]
                                ss["stops"][i]["lon"] = found["lon"]
                                ss["stops"][i]["name"] = found["name"] 
                                ss["map_center"] = (found["lat"], found["lon"])
                                
                                # Recalc Incoming
                                if i > 0:
                                    l_idx = i - 1
                                    leg = ss["legs_between"][l_idx]
                                    if leg:
                                        A, B = ss["stops"][l_idx], ss["stops"][i]
                                        # CHANGE: Only Google if car/auto
                                        if leg.get("mode") in {"car", "auto"}:
                                            try:
                                                rt = google_driving_route(A["lat"], A["lon"], B["lat"], B["lon"])
                                                rt["source"] = "google"
                                                leg.update(rt)
                                            except: pass
                                        else:
                                            leg.update({
                                                "distance_m": None, "duration_s": None,
                                                "geometry_latlon": interpolate_line(A["lat"], A["lon"], B["lat"], B["lon"])
                                            })
                                
                                # Recalc Outgoing
                                if i < len(ss["stops"]) - 1:
                                    l_idx = i
                                    leg = ss["legs_between"][l_idx]
                                    if leg:
                                        A, B = ss["stops"][i], ss["stops"][i+1]
                                        # CHANGE: Only Google if car/auto
                                        if leg.get("mode") in {"car", "auto"}:
                                            try:
                                                rt = google_driving_route(A["lat"], A["lon"], B["lat"], B["lon"])
                                                rt["source"] = "google"
                                                leg.update(rt)
                                            except: pass
                                        else:
                                            leg.update({
                                                "distance_m": None, "duration_s": None,
                                                "geometry_latlon": interpolate_line(A["lat"], A["lon"], B["lat"], B["lon"])
                                            })
                                
                                # Close edit panel automatically
                                ss["editing_day_idx"] = None
                                
                                mark_dirty()
                                st.rerun()

                    # --- STOP FIELDS ---
                    def update_stop(idx=i, k=k_sfx):
                        # FIX: Use .get() here too just in case
                        if f"d_name_{k}" in st.session_state:
                            ss["stops"][idx]["name"] = st.session_state[f"d_name_{k}"]
                        if f"d_note_{k}" in st.session_state:
                            ss["stops"][idx]["note"] = st.session_state[f"d_note_{k}"]
                        if f"d_ov_{k}" in st.session_state:
                            ss["stops"][idx]["overnight"] = st.session_state[f"d_ov_{k}"]
                        if f"d_stat_{k}" in st.session_state:
                            ss["stops"][idx]["booking_status"] = st.session_state[f"d_stat_{k}"]
                        mark_dirty()

                    c_nm, c_nt = st.columns([2, 3])
                    with c_nm:
                        st.text_input("Nome", value=s['name'], key=f"d_name_{k_sfx}", on_change=update_stop)
                    with c_nt:
                        st.text_input("Note", value=s.get("note", ""), key=f"d_note_{k_sfx}", on_change=update_stop)
                        
                    c_ov_cb, c_ov_st = st.columns([1, 2])
                    with c_ov_cb:
                        st.write("") # Spacer
                        st.checkbox("Pernottamento", value=s.get("overnight", False), key=f"d_ov_{k_sfx}", on_change=update_stop)
                    with c_ov_st:
                        cur_status = s.get("booking_status", "todo")
                        st.selectbox("Stato Hotel", ["todo", "provisional", "booked"], index=["todo", "provisional", "booked"].index(cur_status), format_func=lambda x: {"todo": "Da prenotare", "provisional": "Provvisorio", "booked": "Prenotato"}.get(x, x), key=f"d_stat_{k_sfx}", on_change=update_stop)


                    # --- OUTGOING LEG ---
                    if i < len(ss["stops"]) - 1:
                        leg = ss["legs_between"][i]
                        st.caption(f"🔻 Verso {ss['stops'][i+1]['name']}")
                        
                        l_m = leg.get("mode", "—") if leg else "—"
                        l_n = leg.get("note", "") if leg else ""
                        l_s = leg.get("booking_status", "todo") if leg else "todo"
                        
                        # FIX: Explicitly define the list here to avoid conflict with the 'modes' set used in the header
                        modes_list = ["—", "car", "bus", "train", "plane", "ferry"]
                        
                        def update_leg(idx=i, k=k_sfx):
                            # FIX: Use .get() to prevent KeyError on Enter
                            n_m = st.session_state.get(f"d_mode_{k}")
                            n_n = st.session_state.get(f"d_lnote_{k}")
                            n_s = st.session_state.get(f"d_lstat_{k}")

                            if n_m is None: return

                            old = ss["legs_between"][idx]
                            
                            if n_m == "—":
                                ss["legs_between"][idx] = None
                            elif (n_m != "—") and (not old or old.get("mode") != n_m):
                                A, B = ss["stops"][idx], ss["stops"][idx+1]
                                
                                # CHANGE: Only call Google if car/auto
                                if n_m in {"car", "auto"}:
                                    try:
                                        rt = google_driving_route(A["lat"], A["lon"], B["lat"], B["lon"])
                                        rt["source"] = "google"
                                    except: rt = interpolate_line(A["lat"], A["lon"], B["lat"], B["lon"])
                                    # Car gets full stats
                                    rt.update({"mode": n_m, "note": n_n, "booking_status": n_s})
                                else:
                                    # Train/Bus/Plane = Straight line, No stats
                                    rt = {
                                        "distance_m": None, "duration_s": None,
                                        "geometry_latlon": interpolate_line(A["lat"], A["lon"], B["lat"], B["lon"]),
                                        "mode": n_m, "note": n_n, "booking_status": n_s
                                    }
                                    
                                ss["legs_between"][idx] = rt
                            elif ss["legs_between"][idx]:
                                ss["legs_between"][idx]["note"] = n_n
                                ss["legs_between"][idx]["booking_status"] = n_s
                            mark_dirty()

                        lc1, lc2, lc3 = st.columns([1, 1, 3])
                        with lc1:
                            idx_l = modes_list.index(l_m) if l_m in modes_list else 0
                            st.selectbox("Mezzo", modes_list, index=idx_l, format_func=lambda x: {"car":"Auto","bus":"Bus","train":"Treno","plane":"Aereo","ferry":"Traghetto","—":"—"}.get(x, x), key=f"d_mode_{k_sfx}", label_visibility="collapsed", on_change=update_leg)
                        with lc2:
                            st.selectbox("Stato Leg", ["todo", "provisional", "booked"], index=["todo", "provisional", "booked"].index(l_s), format_func=lambda x: {"todo": "Da fare", "provisional": "Provvisorio", "booked": "Prenotato"}.get(x, x), key=f"d_lstat_{k_sfx}", label_visibility="collapsed", on_change=update_leg)
                        with lc3:
                            st.text_input("Note Leg", value=l_n, key=f"d_lnote_{k_sfx}", label_visibility="collapsed", on_change=update_leg)
                        st.divider()

                # Close Button
                if st.button("✅ Chiudi Modifica", key=f"close_{b_idx}"):
                    ss["editing_day_idx"] = None
                    st.rerun()

if ss.get("dirty", False):
    save_to_supabase()

if ss.get("can_edit"):
    st.divider()
    with st.expander("Elimina Viaggio"):
        st.write(f"Stai per eliminare: **{ss['trip_name']}**")
        st.caption("Il viaggio verrà nascosto dalla lista.")
        
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
                    trip_id = ss["current_trip_id"]
                    data_to_save = {
                        "name": ss["trip_name"],
                        "trip_start_date": ss["trip_start_date"].isoformat(),
                        "stops": ss["stops"],
                        "legs_between": ss["legs_between"],
                        "last_updated": datetime.now().isoformat(),
                        "is_deleted": True
                    }

                    try:
                        supabase.table("itineraries").upsert({
                            "trip_id": trip_id, 
                            "trip_data": data_to_save
                        }).execute()
                        
                        st.success("Viaggio eliminato.")
                        
                        # --- CRITICAL FIXES ---
                        # 1. Clear the cache so the app doesn't reload the deleted trip
                        get_all_trips_summary.clear()
                        
                        # 2. Reset the deletion confirmation state
                        keys_to_clear = [
                            "initialized", "current_trip_id", "stops", 
                            "legs_between", "trip_name", "confirm_delete"
                        ]
                        for k in keys_to_clear:
                            if k in ss:
                                del ss[k]
                        
                        if hasattr(st, "query_params"): 
                            st.query_params.clear()
                        else:
                            st.experimental_set_query_params()
                            
                        st.rerun()
                    except Exception as e:
                        st.error(f"Errore durante l'eliminazione: {e}")