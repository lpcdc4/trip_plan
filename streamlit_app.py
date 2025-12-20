# streamlit_app.py
#
# Streamlit itinerary planner (Photon + OSRM) + Supabase Sync + PIN Protection
#
# Install:
#   pip install streamlit folium streamlit-folium requests polyline streamlit-searchbox streamlit-sortables supabase
#
# Run:
#   streamlit run streamlit_app.py

import hmac
import uuid
import re
from datetime import date, timedelta, datetime
from typing import Dict, List, Optional, Tuple
import json  
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


# ----------------------- AUTHENTICATION -----------------------
def check_password():
    """Returns `True` if the user had a correct password."""
    
    if st.session_state.get("password_correct", False):
        return True

    def password_entered():
        # Retrieve PIN from secrets (Default to 0000 if not set)
        secret_pin = st.secrets.get("APP_PIN", "0000")
        if hmac.compare_digest(st.session_state["password_input"], str(secret_pin)):
            st.session_state["password_correct"] = True
            del st.session_state["password_input"] 
        else:
            st.session_state["password_correct"] = False

    st.set_page_config(page_title="Accesso Viaggio", layout="centered")
    st.title("🔒 Trip Planner Protetto")
    st.text_input(
        "Inserisci PIN per accedere:", 
        type="password", 
        on_change=password_entered, 
        key="password_input"
    )
    
    if "password_correct" in st.session_state and not st.session_state["password_correct"]:
        st.error("❌ PIN non corretto")

    return False

if not check_password():
    st.stop()


# ==============================================================================
# MAIN APP STARTS HERE
# ==============================================================================

# ----------------------- Constants -----------------------
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
        st.warning(f"Sincronizzazione fallita: {e}")

def init_state():
    ss = st.session_state
    if "initialized" in ss:
        return

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
    ss.setdefault("trip_name", "Il Mio Viaggio")
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
    ss.setdefault("editing_stop_idx", None)

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
# UPDATED: Uses Photon + Caching + Crash Fix
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
    return f"{h}h {m:02d}m"


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


#def leg_summary(leg: Optional[Dict]) -> str:
#    if not leg:
#        return "—"
#    mode = (leg.get("mode") or "—").upper()
#    if mode in {"CAR", "BUS", "TRAIN"} and leg.get("distance_m") is not None and leg.get("duration_s") is not None:
#        km = leg["distance_m"] / 1000.0
#        hhmm = hhmm_from_seconds(leg["duration_s"])
#        return f"{mode} · {km:.1f} km · {hhmm}"
#    return mode


def leg_summary(leg: Optional[Dict]) -> str:
    if not leg:
        return "—"
    
    # Capitalize and translate mode
    mode_raw = (leg.get("mode") or "—").lower()
    mode_map = {
        "car": "Auto",
        "bus": "Bus",
        "train": "Treno",
        "plane": "Aereo",
        "ferry": "Traghetto"
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
        # ICONS: Blue Home for Overnight, Gray Pin for day stop
        if is_overnight:
            icon = folium.Icon(color="blue", icon="home")
            od_str = "Sì"
        else:
            # "map-pin" is a standard FontAwesome icon, cleaner than "info-sign"
            icon = folium.Icon(color="gray", icon="map-pin", prefix="fa")
            od_str = "No"
            
        popup = f"<b>{s['name']}</b><br>ID: {s['id']}<br>Notte: {od_str}"
        if s.get("note"):
            popup += f"<br>Note: {s['note']}"
            
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
    
    # CRASH FIX: Use .get() so it doesn't crash if session_state isn't ready
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
st.set_page_config(page_title="Itinerario", layout="wide")
init_state()
ensure_legs_alignment()
ss = st.session_state

# ----------------------- Sidebar: Tools -----------------------
with st.sidebar:
    st.header("⚙️ Gestione Viaggio")
    
    # 1. CLONE (Version Control)
    if st.button("©️ Clona Viaggio (Nuova versione)", help="Crea una copia esatta di questo itinerario con un nuovo ID."):
        new_id = str(uuid.uuid4())[:8]
        ss["current_trip_id"] = new_id
        ss["trip_name"] = f"Copia di {ss['trip_name']}"
        
        # Update URL
        if hasattr(st, "query_params"):
            st.query_params["trip_id"] = new_id
        else:
            st.experimental_set_query_params(trip_id=new_id)
            
        mark_dirty() # Forces immediate save to new ID
        st.success(f"Viaggio clonato! Nuovo ID: {new_id}")
        st.rerun()

    st.divider()

    # 2. EXPORT JSON
    # Prepare data exactly like we save to Supabase
    export_data = {
        "name": ss["trip_name"],
        "trip_start_date": ss["trip_start_date"].isoformat(),
        "stops": ss["stops"],
        "legs_between": ss["legs_between"],
        "last_updated": datetime.now().isoformat()
    }
    json_str = json.dumps(export_data, indent=2)
    
    st.download_button(
        label="⬇️ Esporta JSON",
        data=json_str,
        file_name=f"itinerario_{ss['current_trip_id']}.json",
        mime="application/json"
    )

    # 3. IMPORT JSON
    uploaded_file = st.file_uploader("⬆️ Importa JSON", type=["json"])
    if uploaded_file is not None:
        try:
            data = json.load(uploaded_file)
            # Use the existing helper to load state
            populate_state_from_data(data)
            mark_dirty() # Mark as unsaved so it syncs to current ID
            st.success("Itinerario caricato con successo!")
            st.rerun()
        except Exception as e:
            st.error(f"Errore caricamento: {e}")

    st.divider()
    st.caption(f"Trip ID: `{ss['current_trip_id']}`")
    
# Header
c1, c2, c3 = st.columns([3, 2, 2])
with c1:
    new_name = st.text_input("Nome viaggio", ss["trip_name"])
    if new_name != ss["trip_name"]:
        ss["trip_name"] = new_name
        mark_dirty()
with c2:
    new_start = st.date_input("Data inizio viaggio", ss["trip_start_date"])
    if new_start != ss["trip_start_date"]:
        ss["trip_start_date"] = new_start
        mark_dirty()
with c3:
    # Using a disabled input ensures pixel-perfect alignment with the other boxes
    st.text_input("Cloud ID", value=ss['current_trip_id'], disabled=True)

# Search above map
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
        
        # Determine context
        is_first = (len(ss["stops"]) == 0)
        is_same_place = False
        if not is_first:
            last = ss["stops"][-1]
            if last["name"] == p["name"]:
                is_same_place = True
        
        with st.form("add_stop_form"):
            # We use 2 columns for perfect alignment
            c_left, c_right = st.columns(2)
            
            # --- ROW 1: Name vs Mode ---
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
                    # This Selectbox will align perfectly with the Name Input on the left
                    mode = st.selectbox("Mezzo", ["Auto", "Treno", "Aereo", "Bus", "Altro"], index=0)

            # --- ROW 2: Notes vs Notes ---
            c_note_l, c_note_r = st.columns(2)
            with c_note_l:
                stop_note = st.text_input("Note tappa", value="")
            with c_note_r:
                if not is_first and not is_same_place:
                    leg_note = st.text_input("Note spostamento", value="")

            # --- ROW 3: Checkbox (Full width or Left) ---
            overnight = st.checkbox("Pernottamento", value=True)

            st.write("") # Spacer
            if st.form_submit_button("Aggiungi Tappa", type="primary", use_container_width=True):
                # Add Stop
                final_name = name_override.strip() if name_override.strip() else p["name"]
                add_stop_internal(final_name, p["lat"], p["lon"], overnight, stop_note)
                
                # Add Leg (if needed)
                if not is_first:
                    prev_idx = len(ss["stops"]) - 2
                    if is_same_place:
                        ss["legs_between"][prev_idx] = None
                        mark_dirty()
                    else:
                        m_map = {"Auto": "car", "Treno": "train", "Aereo": "plane", "Bus": "bus", "Altro": "car"}
                        internal_mode = m_map.get(mode, "car")
                        set_leg_between(prev_idx, internal_mode, leg_note)
                
                # Reset
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
# ITINERARY SECTION (Replaces the old View + Editor sections)
# ------------------------------------------------------------------------------
st.markdown("## Itinerario")

# Drag & drop (Keep this bit)
if ss["stops"]:
    with st.expander("Riordina tappe (trascina e rilascia)", expanded=False):
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
                st.success("Ordine aggiornato.")
                st.rerun()

if not ss["stops"]:
    st.info("Nessuna tappa. Aggiungine una cercando qui sopra.")
else:
    blocks = itinerary_day_blocks(ss["stops"], ss["legs_between"])
    
    # --------------------------------------------------------------------------
    # FIXED LOOP: Uses enumerate(blocks) to generate unique keys for duplicate stops
    # --------------------------------------------------------------------------
    for b_idx, b in enumerate(blocks):
        start_stop = ss["stops"][b["start"]]
        end_stop = ss["stops"][b["end"]]
        date_str = fmt_date(b["date"])
        
        # Header Logic
        is_direct_link = (b["end"] == b["start"] + 1)
        is_same_loc = False
        if is_direct_link:
            leg = ss["legs_between"][b["start"]]
            if leg:
                dist = leg.get("distance_m")
                mode = leg.get("mode", "").lower()
                if mode == "plane": is_same_loc = False
                elif dist is not None and dist < 1000: is_same_loc = True
            else:
                if start_stop["name"] == end_stop["name"]: is_same_loc = True

        is_stay_day = is_direct_link and is_same_loc
        
        # Calculate header title
        modes = set()
        for li, leg in b["legs"]:
            if leg: modes.add(leg.get("mode", "car").lower())
        
        # ----------------------------------------------------------------------
        # ICONS IN HEADERS (Replaces words with Emojis)
        # ----------------------------------------------------------------------
        time_parts = []
        
        # 1. Driving -> 🚗
        if b["drive_seconds"] > 0:
            time_parts.append(f"🚗 {hhmm_from_seconds(b['drive_seconds'])}")
            
        # 2. Check for other specific modes
        modes = set()
        for li, leg in b["legs"]:
            if leg: modes.add(leg.get("mode", "car").lower())

        if "plane" in modes or "aereo" in modes:
            time_parts.append("✈️") 
        if "train" in modes or "treno" in modes:
            time_parts.append("🚆") 
        if "bus" in modes:
            time_parts.append("🚌")
        if "ferry" in modes or "traghetto" in modes:
            time_parts.append("⛴️")
            
        # Fallback if we have legs but no specific parts added
        if not time_parts and len(b["legs"]) > 0:
            time_parts.append("🏃")
            
        time_str = " + ".join(time_parts)
            
        header = ""
        if is_stay_day or b["start"] == b["end"]:
             header = f"Giorno {b['day']} ({date_str}) · {start_stop['name']}"
        else:
             mid_part = f" · {time_str}" if time_str else ""
             header = f"Giorno {b['day']} ({date_str}){mid_part} · {start_stop['name']} ➝ {end_stop['name']}"

        # Render Block
        with st.expander(header, expanded=False):
            for i in range(b["start"], b["end"] + 1):
                s = ss["stops"][i]
                
                # UNIQUE KEY SUFFIX: Combination of Stop Index + Block Index
                # This prevents crashes when a stop appears in multiple blocks (overnight)
                k_sfx = f"{i}_{b_idx}"

                # --- CHECK IF EDITING THIS STOP ---
                if ss.get("editing_stop_idx") == i:
                    with st.container(border=True):
                        st.write(f"**Modifica: {s['name']}**")
                        
                        # 1. STOP DETAILS
                        c_edit_1, c_edit_2 = st.columns([3, 2])
                        with c_edit_1:
                            new_name_edit = st.text_input("Nome", s['name'], key=f"edit_name_{k_sfx}")
                            new_note_edit = st.text_input("Note", s.get("note", ""), key=f"edit_note_{k_sfx}")
                        with c_edit_2:
                            new_ov_edit = st.checkbox("Pernottamento", s.get("overnight", False), key=f"edit_ov_{k_sfx}")
                            if st.button("🗑 Elimina Tappa", key=f"del_btn_{k_sfx}"):
                                old_stops = list(ss["stops"])
                                old_legs = list(ss["legs_between"])
                                ss["stops"] = [x for j, x in enumerate(ss["stops"]) if j != i]
                                ss["legs_between"] = rebuild_legs_from_old(old_stops, old_legs, ss["stops"])
                                ensure_legs_alignment()
                                ss["map_center"] = compute_center(ss["stops"])
                                ss["editing_stop_idx"] = None
                                mark_dirty()
                                st.rerun()

                        # 2. LEG DETAILS (Leaving this stop)
                        new_mode_edit = None
                        new_leg_note_edit = ""
                        
                        has_leg = (i < len(ss["stops"]) - 1)
                        if has_leg:
                            st.markdown("---")
                            st.caption(f"Spostamento verso {ss['stops'][i+1]['name']}")
                            cur_leg = ss["legs_between"][i]
                            cur_mode = cur_leg.get("mode", "—") if cur_leg else "—"
                            cur_leg_note = cur_leg.get("note", "") if cur_leg else ""
                            
                            cl1, cl2 = st.columns(2)
                            with cl1:
                                def fmt_mode(m):
                                    return {"car": "Auto", "bus": "Bus", "train": "Treno", "plane": "Aereo", "ferry": "Traghetto", "—": "—"}.get(m, m)
                                new_mode_edit = st.selectbox("Mezzo", ["—", "car", "bus", "train", "plane"], index=["—", "car", "bus", "train", "plane"].index(cur_mode) if cur_mode in ["—", "car", "bus", "train", "plane"] else 0, format_func=fmt_mode, key=f"edit_mode_{k_sfx}")
                            with cl2:
                                new_leg_note_edit = st.text_input("Note Spostamento", cur_leg_note, key=f"edit_leg_note_{k_sfx}")

                        st.write("")
                        if st.button("💾 Salva Modifiche", key=f"save_{k_sfx}", type="primary"):
                            # Update Stop
                            ss["stops"][i]["name"] = new_name_edit
                            ss["stops"][i]["note"] = new_note_edit
                            ss["stops"][i]["overnight"] = new_ov_edit
                            
                            # Update Leg
                            if has_leg:
                                leg_changed = False
                                if not ss["legs_between"][i]:
                                    leg_changed = True 
                                elif ss["legs_between"][i].get("mode") != new_mode_edit:
                                    leg_changed = True
                                
                                # Just update note?
                                if not leg_changed and ss["legs_between"][i]:
                                    ss["legs_between"][i]["note"] = new_leg_note_edit
                                
                                # Re-route if mode changed
                                if leg_changed or (new_mode_edit != "—" and not ss["legs_between"][i]):
                                    if new_mode_edit == "—":
                                        ss["legs_between"][i] = None
                                    else:
                                        a = ss["stops"][i]
                                        b_stop = ss["stops"][i+1]
                                        if new_mode_edit in {"car", "bus", "train"}:
                                            with st.spinner("Ricalcolo percorso..."):
                                                route = osrm_driving_route(a["lat"], a["lon"], b_stop["lat"], b_stop["lon"])
                                            ss["legs_between"][i] = {"mode": new_mode_edit, "note": new_leg_note_edit, **route}
                                        else:
                                            ss["legs_between"][i] = {
                                                "mode": "plane", "note": new_leg_note_edit, "distance_m": None, "duration_s": None,
                                                "geometry_latlon": interpolate_line(a["lat"], a["lon"], b_stop["lat"], b_stop["lon"])
                                            }

                            ss["editing_stop_idx"] = None
                            mark_dirty()
                            st.rerun()

                else:
                    # --- NORMAL VIEW ---
                    c_disp, c_btn = st.columns([12, 1])
                    with c_disp:
                        st.markdown(stop_row_html(s), unsafe_allow_html=True)
                    with c_btn:
                        st.write("") 
                        # Use the Unique Key Suffix here too
                        if st.button("✏️", key=f"edit_open_{k_sfx}", help="Modifica tappa"):
                            ss["editing_stop_idx"] = i
                            st.rerun()

                    # Show Leg Summary
                    if i < b["end"]:
                         if is_stay_day: continue
                         leg = ss["legs_between"][i]
                         if leg:
                             summ = leg_summary(leg)
                             note_md = f" — *{leg['note']}*" if leg.get("note") else ""
                             st.caption(f"🔻 **{summ}**{note_md}")
                         else:
                             st.caption("🔻 *Nessun dettaglio*")

# Autosave
if ss["dirty"]:
    save_to_supabase()
