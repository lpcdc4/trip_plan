# streamlit_app.py
#
# Streamlit itinerary planner (Photon + OSRM) + Supabase Sync
# RESTORED UI VERSION: Exact original UI with Cloud Backend
#
# Install:
#   pip install streamlit folium streamlit-folium requests polyline streamlit-searchbox streamlit-sortables supabase
#
# Run:
#   streamlit run streamlit_app.py

import json
import re
import uuid
import time
from pathlib import Path
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
# UPDATED: Using Photon (Komoot) for reliable, unblocked search
PHOTON_SEARCH = "https://photon.komoot.io/api/"
OSRM_ROUTE = "https://router.project-osrm.org/route/v1/driving/{lon1},{lat1};{lon2},{lat2}"
DEFAULT_USER_AGENT = "itinerary-planner-cloud/1.0"


# ----------------------- Supabase Setup -----------------------
try:
    SUPABASE_URL = st.secrets["SUPABASE_URL"]
    SUPABASE_KEY = st.secrets["SUPABASE_KEY"]
    supabase: Client = create_client(SUPABASE_URL, SUPABASE_KEY)
except Exception:
    st.error("Missing Supabase secrets. Please set SUPABASE_URL and SUPABASE_KEY.")
    st.stop()


# ----------------------- Session / DB Sync -----------------------
def get_trip_id_from_url():
    # Handle query params for different Streamlit versions
    if hasattr(st, "query_params"):
        qp = st.query_params
    else:
        qp = st.experimental_get_query_params()
        
    if "trip_id" in qp:
        val = qp["trip_id"]
        return val[0] if isinstance(val, list) else val
    return None

def load_from_supabase(trip_id: str):
    try:
        response = supabase.table("itineraries").select("trip_data").eq("trip_id", trip_id).execute()
        if response.data and len(response.data) > 0:
            return response.data[0]["trip_data"]
    except Exception as e:
        # st.error(f"DB Load Error: {e}") 
        pass
    return None

def save_to_supabase():
    ss = st.session_state
    if not ss.get("dirty", False):
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
        st.warning(f"Sync failed: {e}")

def init_state():
    ss = st.session_state
    if "initialized" in ss:
        return

    # Load ID from URL or create new
    url_id = get_trip_id_from_url()
    
    if url_id:
        data = load_from_supabase(url_id)
        if data:
            ss["current_trip_id"] = url_id
            populate_state_from_data(data)
        else:
            ss["current_trip_id"] = url_id
            set_defaults()
    else:
        new_id = str(uuid.uuid4())[:8]
        ss["current_trip_id"] = new_id
        set_defaults()
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
    ss.setdefault("map_center", None)
    ss.setdefault("map_version", 0)
    ss.setdefault("dirty", True)
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
    try:
        ss["trip_start_date"] = date.fromisoformat(data.get("trip_start_date"))
    except:
        ss["trip_start_date"] = date.today()

    ss["stops"] = data.get("stops", [])
    ss["legs_between"] = data.get("legs_between", [])
    
    # Recalculate IDs
    mx = 0
    for s in ss["stops"]:
        sid = str(s.get("id", ""))
        if sid.startswith("S"):
            try:
                mx = max(mx, int(sid[1:]))
            except: pass
    ss["next_stop_id"] = mx + 1

    # Reset UI
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
    needed = max(0, len(ss["stops"]) - 1)
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
    return d.isoformat() if d else ""


# ----------------------- External calls -----------------------
# UPDATED: Uses Photon + Caching
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


def osrm_driving_route(lat1: float, lon1: float, lat2: float, lon2: float) -> Dict:
    url = OSRM_ROUTE.format(lat1=lat1, lon1=lon1, lat2=lat2, lon2=lon2)
    params = {"overview": "full", "geometries": "polyline"}
    try:
        r = requests.get(url, params=params, timeout=10)
        r.raise_for_status()
        data = r.json()
        if data.get("code") != "Ok" or not data.get("routes"):
            raise RuntimeError(f"OSRM routing failed")
        route0 = data["routes"][0]
        coords_latlon = polyline_lib.decode(route0["geometry"])
        return {
            "distance_m": float(route0["distance"]),
            "duration_s": float(route0["duration"]),
            "geometry_latlon": coords_latlon,
        }
    except:
        # Fallback to straight line if routing fails
        return {
            "distance_m": None, "duration_s": None,
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
    return f"{h:02d}:{m:02d}"


def driving_seconds_for_leg(leg: Optional[Dict]) -> int:
    if not leg:
        return 0
    mode = (leg.get("mode") or "").lower()
    if mode in {"car", "bus", "train"} and leg.get("duration_s") is not None:
        try:
            return int(round(float(leg["duration_s"])))
        except Exception:
            return 0
    return 0


def leg_summary(leg: Optional[Dict]) -> str:
    if not leg:
        return "—"
    mode = (leg.get("mode") or "—").upper()
    if mode in {"CAR", "BUS", "TRAIN"} and leg.get("distance_m") is not None and leg.get("duration_s") is not None:
        km = leg["distance_m"] / 1000.0
        hhmm = hhmm_from_seconds(leg["duration_s"])
        return f"{mode} · {km:.1f} km · {hhmm}"
    return mode


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
            tooltip="Next stop (preview)",
            popup=f"<b>Next stop</b><br>{p['name']}",
            icon=folium.Icon(color="green", icon="search"),
        ).add_to(m)

    for s in stops:
        od = "Yes" if s.get("overnight") else "No"
        popup = f"<b>{s['name']}</b><br>ID: {s['id']}<br>Overnight: {od}"
        if s.get("note"):
            popup += f"<br>Note: {s['note']}"
        icon = folium.Icon(
            color="blue" if s.get("overnight") else "gray",
            icon="home" if s.get("overnight") else "info-sign",
        )
        folium.Marker([s["lat"], s["lon"]], popup=popup, tooltip=s["id"], icon=icon).add_to(m)

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

        tooltip = f"{mode.upper()} · {stops[i]['id']} → {stops[i+1]['id']}"
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
        leg.update(osrm_driving_route(a["lat"], a["lon"], b["lat"], b["lon"]))
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
            route = osrm_driving_route(A["lat"], A["lon"], B["lat"], B["lon"])
            new_legs.append({"mode": "car", "note": "Auto-routed", **route})
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
            new_legs.append(None) # Let user fill in, or could auto-route
    return new_legs


# ----------------------- Autocomplete -----------------------
# RESTORED: Uses the lookup dict pattern from original file
def search_api_labels(query: str) -> List[str]:
    ss = st.session_state
    q = (query or "").strip()
    if len(q) < 2:
        ss["search_lookup"] = {}
        return []
    
    # Use Photon backend
    results = forward_search(q, ss["user_agent"], limit=10)
    
    lookup: Dict[str, Dict] = {}
    labels: List[str] = []
    for r in results:
        label = r["name"]
        j = 2
        # Deduplicate identical labels in the dropdown
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
    badge = "🌙 overnight" if overnight else ""
    note = (s.get("note") or "").strip()
    note_html = f"<div style='margin-top:6px;color:#444;font-size:0.92rem;'><em>{note}</em></div>" if note else ""
    return f"""
    <div style="background:{bg};border:1px solid {border};border-radius:12px;padding:12px 14px;margin:8px 0;">
      <div style="display:flex;gap:10px;align-items:baseline;justify-content:space-between;">
        <div style="font-size:1.02rem;"><strong>{s['id']}</strong> — {s['name']}</div>
        <div style="font-size:0.92rem;color:#333;">{badge}</div>
      </div>
      {note_html}
    </div>
    """


# ======================= APP =======================
st.set_page_config(page_title="Itinerary", layout="wide")
init_state()
ensure_legs_alignment()
ss = st.session_state

# Header
c1, c2, c3, c4 = st.columns([3, 2, 2, 1])
with c1:
    new_name = st.text_input("Trip name", ss["trip_name"])
    if new_name != ss["trip_name"]:
        ss["trip_name"] = new_name
        mark_dirty()
with c2:
    new_start = st.date_input("Trip start date", ss["trip_start_date"])
    if new_start != ss["trip_start_date"]:
        ss["trip_start_date"] = new_start
        mark_dirty()
with c3:
    st.info(f"Cloud ID: `{ss['current_trip_id']}`")

st.divider()

# Search above map
st.markdown("### Add next stop")
# Uses original style: returns string label, looks up in dictionary
selected_label = st_searchbox(
    search_api_labels,
    key=f"searchbox_{ss['search_key_version']}",
    placeholder="Type a place…",
    label="Search",
)

selection = None
if selected_label:
    selection = ss.get("search_lookup", {}).get(selected_label)

# 1. Preview Stage (RESTORED from original)
if selection and selected_label != ss["last_selected_label"]:
    ss["last_selected_label"] = selected_label
    ss["pending_preview"] = {"name": selection["name"], "lat": selection["lat"], "lon": selection["lon"]}
    ss["map_center"] = (selection["lat"], selection["lon"])
    mark_dirty()

if selection:
    with st.container(border=True):
        st.write(f"**Selected:** {selection['name']}")
        cA, cB, cC = st.columns([1, 1, 2])
        with cA:
            overnight = st.checkbox("Overnight", value=True, key="add_stop_overnight")
        with cB:
            stop_note = st.text_input("Stop note (optional)", value="", key="add_stop_note")
        with cC:
            name_override = st.text_input("Name override (optional)", value="", key="add_stop_name_override")

        if st.button("Use this as next stop", use_container_width=True):
            ss["pending_stop"] = {
                "name": (name_override.strip() or selection["name"]),
                "lat": float(selection["lat"]),
                "lon": float(selection["lon"]),
                "overnight": bool(overnight),
                "note": stop_note.strip(),
            }
            st.rerun()

# 2. Confirm Stage (RESTORED from original)
if ss["pending_stop"] is not None:
    p = ss["pending_stop"]
    is_first = (len(ss["stops"]) == 0)

    with st.container(border=True):
        st.markdown("### Confirm add")
        st.write(f"**Next stop:** {p['name']}")

        if is_first:
            st.caption("First stop (no incoming leg).")
            if st.button("Add first stop", type="primary", use_container_width=True):
                add_stop_internal(p["name"], p["lat"], p["lon"], p["overnight"], p["note"])
                ss["pending_stop"] = None
                ss["pending_preview"] = None
                ss["search_key_version"] += 1
                ss["last_selected_label"] = None
                mark_dirty()
                st.rerun()
        else:
            st.write(f"**From:** {ss['stops'][-1]['name']}")
            with st.form("transport_form", clear_on_submit=False):
                c1, c2 = st.columns([1, 2])
                with c1:
                    mode = st.selectbox("Mode", ["car", "bus", "train", "plane", "—"], index=0)
                with c2:
                    leg_note = st.text_input("Leg note (optional)", value="")
                submit = st.form_submit_button("Add stop + leg", type="primary", use_container_width=True)

            if submit:
                add_stop_internal(p["name"], p["lat"], p["lon"], p["overnight"], p["note"])
                ensure_legs_alignment()
                prev_idx = len(ss["stops"]) - 2
                try:
                    if mode != "—":
                        set_leg_between(prev_idx, mode, leg_note)
                    else:
                        ss["legs_between"][prev_idx] = None
                        mark_dirty()
                except Exception as e:
                    st.error(f"Routing failed: {e}")

                ss["pending_stop"] = None
                ss["pending_preview"] = None
                ss["search_key_version"] += 1
                ss["last_selected_label"] = None
                st.rerun()

st.divider()

# Map
m = build_map(ss["stops"], ss["legs_between"])
# Keep the optimization so it doesn't lag
st_folium(m, height=720, width=None, key=f"map_{ss['map_version']}", returned_objects=[])

st.divider()

# Itinerary
st.markdown("## Itinerary")

btn_col1, btn_col2 = st.columns([1, 5])
with btn_col1:
    if st.button("Edit itinerary" if not ss["show_editor"] else "Hide editor", use_container_width=True):
        ss["show_editor"] = not ss["show_editor"]
        st.rerun()

# Drag & drop
if ss["stops"]:
    with st.expander("Reorder stops (drag & drop)", expanded=False):
        if not HAS_SORTABLES:
            st.error("Drag & drop requires: pip install streamlit-sortables")
        else:
            base_items = [f"{s['id']} | {s['name']}" for s in ss["stops"]]
            if ss.get("sortable_items_cache") is None or len(ss["sortable_items_cache"]) != len(base_items):
                ss["sortable_items_cache"] = base_items

            new_items = sort_items(
                ss["sortable_items_cache"],
                direction="vertical",
                key=f"sortable_stops_{ss['sortable_key_version']}",
            )
            ss["sortable_items_cache"] = list(new_items)

            new_order_ids = []
            for it in new_items:
                it = str(it)
                if " | " in it:
                    sid = it.split(" | ", 1)[0].strip()
                else:
                    m = re.match(r"^(S\d+)", it.strip())
                    sid = m.group(1) if m else it.strip()
                new_order_ids.append(sid)

            current_ids = [s["id"] for s in ss["stops"]]
            if new_order_ids and new_order_ids != current_ids:
                apply_stop_reorder(new_order_ids)
                renumber_stops_and_update()
                ss["sortable_items_cache"] = None
                st.success("Reordered.")
                st.rerun()

if not ss["stops"]:
    st.info("No stops yet. Add one from the search box above.")
else:
    blocks = itinerary_day_blocks(ss["stops"], ss["legs_between"])
    for b in blocks:
        drive_hhmm = hhmm_from_seconds(b["drive_seconds"]) if b["drive_seconds"] else "00:00"
        st.markdown(f"### Day {b['day']} ({fmt_date(b['date'])}) · Driving: **{drive_hhmm}**")

        for si in range(b["start"], b["end"] + 1):
            st.markdown(stop_row_html(ss["stops"][si]), unsafe_allow_html=True)

        if b["legs"]:
            with st.expander("Show legs for this day", expanded=False):
                for li, leg in b["legs"]:
                    frm = ss["stops"][li]
                    to = ss["stops"][li + 1]
                    st.write(f"**{frm['id']} → {to['id']}** — {leg_summary(leg)}")
                    if (leg.get("note") or "").strip():
                        st.caption(leg["note"])

# Editor (Hidden Section - RESTORED)
if ss["show_editor"]:
    st.divider()
    st.markdown("## Edit itinerary (stops + legs)")
    for i, s in enumerate(ss["stops"]):
        with st.container(border=True):
            ovtag = " (overnight)" if s.get("overnight") else ""
            st.write(f"### {i+1}. {s['id']} — {s['name']}{ovtag}")

            c1, c2, c3 = st.columns([1, 4, 1])
            with c1:
                new_ov = st.checkbox("Overnight", value=bool(s.get("overnight", False)), key=f"ov_{s['id']}_{i}")
                if new_ov != bool(s.get("overnight", False)):
                    s["overnight"] = bool(new_ov)
                    mark_dirty()
                    st.rerun()
            with c2:
                new_note = st.text_input("Stop note", value=str(s.get("note", "")), key=f"note_{s['id']}_{i}")
                if new_note != str(s.get("note", "")):
                    s["note"] = new_note
                    mark_dirty()

                # Change / replace this stop
                with st.expander("Change this stop (search)", expanded=False):
                    def _search_local(q: str) -> List[str]:
                        # Re-use global search logic
                        return search_api_labels(q)

                    sel = st_searchbox(_search_local, key=f"chgstop_sb_{i}_{ss['search_key_version']}", placeholder="Type a place…", label="Search")
                    cand = ss.get("search_lookup", {}).get(sel) if sel else None
                    if cand:
                        st.caption(f"Preview: {cand['name']}")
                        if st.button("Replace stop with this place", key=f"replace_stop_{i}"):
                            old_stops = list(ss["stops"])
                            old_legs = list(ss["legs_between"])
                            ss["stops"][i]["name"] = cand["name"]
                            ss["stops"][i]["lat"] = float(cand["lat"])
                            ss["stops"][i]["lon"] = float(cand["lon"])
                            # Rebuild legs
                            ss["legs_between"] = rebuild_legs_from_old(old_stops, old_legs, ss["stops"])
                            ensure_legs_alignment()
                            ss["map_center"] = (float(cand["lat"]), float(cand["lon"]))
                            ss["pending_preview"] = None
                            mark_dirty()
                            st.rerun()
            with c3:
                if st.button("Delete", key=f"del_{s['id']}_{i}"):
                    old_stops = list(ss["stops"])
                    old_legs = list(ss["legs_between"])
                    ss["stops"] = [x for j, x in enumerate(ss["stops"]) if j != i]
                    ss["legs_between"] = rebuild_legs_from_old(old_stops, old_legs, ss["stops"])
                    ensure_legs_alignment()
                    ss["map_center"] = compute_center(ss["stops"])
                    ss["pending_preview"] = None
                    mark_dirty()
                    st.rerun()

            if i < len(ss["stops"]) - 1:
                ensure_legs_alignment()
                leg = ss["legs_between"][i]
                st.write(f"**→ Leg:** {leg_summary(leg)}")

                cols = st.columns([2, 4, 1, 1])
                with cols[0]:
                    mode_i = st.selectbox(
                        "Mode",
                        ["—", "car", "bus", "train", "plane"],
                        index=0 if not leg else ["—", "car", "bus", "train", "plane"].index(leg.get("mode", "—")),
                        key=f"mode_{i}",
                    )
                with cols[1]:
                    leg_note_i = st.text_input("Leg note", value=(leg.get("note", "") if leg else ""), key=f"legnote_{i}")
                with cols[2]:
                    if st.button("Apply", key=f"apply_{i}"):
                        try:
                            if mode_i == "—":
                                ss["legs_between"][i] = None
                                mark_dirty()
                            else:
                                a = ss["stops"][i]
                                b2 = ss["stops"][i + 1]
                                if mode_i in {"car", "bus", "train"}:
                                    route = osrm_driving_route(a["lat"], a["lon"], b2["lat"], b2["lon"])
                                    ss["legs_between"][i] = {"mode": mode_i, "note": leg_note_i.strip(), **route}
                                else:  # plane
                                    ss["legs_between"][i] = {
                                        "mode": "plane",
                                        "note": leg_note_i.strip(),
                                        "distance_m": None,
                                        "duration_s": None,
                                        "geometry_latlon": interpolate_line(a["lat"], a["lon"], b2["lat"], b2["lon"]),
                                    }
                                mark_dirty()
                        except Exception as e:
                            st.error(f"Routing failed: {e}")
                        st.rerun()
                with cols[3]:
                    if st.button("Clear", key=f"clear_{i}"):
                        ss["legs_between"][i] = None
                        mark_dirty()
                        st.rerun()

# Autosave
if ss["dirty"]:
    save_to_supabase()
    st.toast("Saved to cloud.")
