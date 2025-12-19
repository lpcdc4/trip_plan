# itinerary.py
#
# Streamlit itinerary planner (OSM + OSRM)
# - Map full width + search box above
# - Add stop -> if not first stop, choose mode and create leg from previous stop
# - Overnight stops are NIGHT boundaries and shown twice (arrival/end of Day D and start of Day D+1)
# - Daily driving time (hh:mm) computed from legs in that day block
# - Autosave JSON + map HTML on every change
# - Import previously saved JSON
# - Drag & drop reorder stops + optional renumber (updates autosaved JSON)
#
# Install:
#   pip install streamlit folium streamlit-folium requests polyline streamlit-searchbox streamlit-sortables
#
# Run:
#   streamlit run itinerary.py

import json
import time
import datetime
import re
from pathlib import Path
from typing import Dict, List, Optional, Tuple
from datetime import date, timedelta

import requests
import streamlit as st
from streamlit_autorefresh import st_autorefresh
from supabase import create_client
import folium
from streamlit_folium import st_folium
import polyline as polyline_lib
from streamlit_searchbox import st_searchbox

# ---------- optional drag & drop dependency ----------
HAS_SORTABLES = False
sort_items = None
try:
    # Most common: streamlit_sortables
    from streamlit_sortables import sort_items as _sort_items  # type: ignore

    sort_items = _sort_items
    HAS_SORTABLES = True
except Exception:
    HAS_SORTABLES = False
    sort_items = None


# ----------------------- Simple PIN gate (Streamlit Secrets) -----------------------
def require_pin():
    pin = st.secrets.get("PIN", "")
    if not pin:
        return
    if st.session_state.get("_pin_ok"):
        return
    st.title("Private itinerary")
    entered = st.text_input("PIN", type="password")
    if st.button("Enter"):
        if entered == pin:
            st.session_state["_pin_ok"] = True
            st.rerun()
        else:
            st.error("Wrong PIN")
    st.stop()

# ----------------------- Supabase (shared storage) -----------------------
@st.cache_resource
def sb_client():
    url = st.secrets.get("SUPABASE_URL", "")
    key = st.secrets.get("SUPABASE_SERVICE_ROLE_KEY", "")
    if not url or not key:
        return None
    return create_client(url, key)

def shared_id():
    return st.secrets.get("ITINERARY_ID", "main")

def load_shared():
    sb = sb_client()
    if sb is None:
        return None
    try:
        res = sb.table("itineraries").select("data,updated_at").eq("id", shared_id()).single().execute()
        return res.data
    except Exception:
        return None

def save_shared(payload: dict):
    sb = sb_client()
    if sb is None:
        return False
    try:
        # Force updated_at to change so other clients detect updates.
        now_iso = datetime.datetime.utcnow().replace(tzinfo=datetime.timezone.utc).isoformat()
        sb.table("itineraries").upsert(
            {"id": shared_id(), "data": payload, "updated_at": now_iso}
        ).execute()
        return True
    except Exception:
        return False


def ensure_state():
    ss = st.session_state
    ss.setdefault("trip_name", "My Trip")
    ss.setdefault("user_agent", DEFAULT_USER_AGENT)
    ss.setdefault("trip_start_date", date.today())

def _touch_input_activity():
    ss = st.session_state
    ss["pause_refresh"] = True
    ss["last_input_time"] = time.time()

def _detect_typing_activity():
    ss = st.session_state
    keys = [k for k in ss.keys() if k.startswith("searchbox_") or k.startswith("chgstop_sb_")]
    snap = ss.get("_typing_snapshot", {})
    new_snap = {}
    changed = False
    for k in keys:
        v = ss.get(k)
        new_snap[k] = v
        if snap.get(k) != v:
            changed = True
    ss["_typing_snapshot"] = new_snap
    if changed:
        _touch_input_activity()

    ss.setdefault("stops", [])          # list[dict]
    ss.setdefault("legs_between", [])   # list[Optional[dict]] length = len(stops)-1

    ss.setdefault("next_stop_id", 1)

    ss.setdefault("map_center", None)
    ss.setdefault("map_version", 0)

    ss.setdefault("dirty", False)
    ss.setdefault("last_save_paths", None)

    ss.setdefault("search_lookup", {})
    ss.setdefault("last_selected_label", None)
    ss.setdefault("search_key_version", 0)
    ss.setdefault("pause_refresh", False)
    ss.setdefault("last_input_time", 0.0)
    ss.setdefault("last_seen_updated_at", "")
    ss.setdefault("_typing_snapshot", {})

    ss.setdefault("sortable_key_version", 0)
    ss.setdefault("sortable_items_cache", None)

    ss.setdefault("pending_stop", None)     # dict while confirming add
    ss.setdefault("pending_preview", None)  # dict for preview marker

    ss.setdefault("show_editor", False)


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
def forward_search(query: str, user_agent: str, limit: int = 8) -> List[Dict]:
    params = {"q": query, "format": "jsonv2", "limit": limit, "addressdetails": 0}
    headers = {"User-Agent": user_agent}
    r = requests.get(NOMINATIM_SEARCH, params=params, headers=headers, timeout=30)
    r.raise_for_status()
    results = r.json() or []
    out = []
    for x in results:
        out.append(
            {
                "name": x.get("display_name", query),
                "lat": float(x["lat"]),
                "lon": float(x["lon"]),
            }
        )
    return out


def osrm_driving_route(lat1: float, lon1: float, lat2: float, lon2: float) -> Dict:
    url = OSRM_ROUTE.format(lat1=lat1, lon1=lon1, lat2=lat2, lon2=lon2)
    params = {"overview": "full", "geometries": "polyline"}
    r = requests.get(url, params=params, timeout=30)
    r.raise_for_status()
    data = r.json()
    if data.get("code") != "Ok" or not data.get("routes"):
        raise RuntimeError(f"OSRM routing failed: {data}")
    route0 = data["routes"][0]
    coords_latlon = polyline_lib.decode(route0["geometry"])
    return {
        "distance_m": float(route0["distance"]),
        "duration_s": float(route0["duration"]),
        "geometry_latlon": coords_latlon,
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
    """
    Overnight stops act as NIGHT boundaries.
    Overnight stops are shown twice (end of Day D, start of Day D+1).
    We DO NOT create an extra trailing day containing only the last overnight stop.
    """
    n = len(stops)
    if n == 0:
        return []

    overnight_idxs = [i for i, s in enumerate(stops) if bool(s.get("overnight", False))]

    # No overnight stops -> single day
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

        # next day starts at the overnight stop (duplicate)
        start = end
        day += 1

    # Tail block only if there is at least one NEW stop after start
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

        # Plane legs: red + show a plane icon at midpoint
        color = "red" if mode == "plane" else "#3388ff"

        folium.PolyLine(
            leg["geometry_latlon"],
            weight=4,
            opacity=0.9,
            dash_array=dash,
            tooltip=tooltip,
            color=color,
        ).add_to(m)

        if mode == "plane" and isinstance(leg.get("geometry_latlon"), list) and len(leg["geometry_latlon"]) >= 2:
            mid = leg["geometry_latlon"][len(leg["geometry_latlon"]) // 2]
            folium.Marker(
                location=mid,
                icon=folium.Icon(color="red", icon="plane", prefix="fa"),
                tooltip="PLANE",
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
    """
    Reorder stops to match new_order_ids.
    Preserve old legs by exact adjacency when possible.
    For new adjacencies, auto-build a leg so driving time survives.
    """
    ss = st.session_state
    old_stops = ss["stops"]
    old_legs = ss["legs_between"]

    id_to_stop = {s["id"]: s for s in old_stops}
    if set(new_order_ids) != set(id_to_stop.keys()):
        raise ValueError("Reorder list does not match current stops.")

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

        # infer mode (default car) based on nearby old legs
        mode = "car"
        for (a, b), leg in leg_map.items():
            if a == frm_id or b == frm_id or a == to_id or b == to_id:
                m = (leg.get("mode") or "").lower()
                if m in {"car", "bus", "train", "plane"}:
                    mode = m
                    break

        A = new_stops[i]
        B = new_stops[i + 1]
        if mode in {"car", "bus", "train"}:
            try:
                route = osrm_driving_route(A["lat"], A["lon"], B["lat"], B["lon"])
                new_legs.append({"mode": mode, "note": "", **route})
            except Exception:
                new_legs.append(
                    {
                        "mode": mode,
                        "note": "routing failed",
                        "distance_m": None,
                        "duration_s": None,
                        "geometry_latlon": interpolate_line(A["lat"], A["lon"], B["lat"], B["lon"]),
                    }
                )
        elif mode == "plane":
            new_legs.append(
                {
                    "mode": "plane",
                    "note": "",
                    "distance_m": None,
                    "duration_s": None,
                    "geometry_latlon": interpolate_line(A["lat"], A["lon"], B["lat"], B["lon"]),
                }
            )
        else:
            new_legs.append(None)

    ss["stops"] = new_stops
    ss["legs_between"] = new_legs
    ss["map_center"] = compute_center(new_stops)
    mark_dirty()



def rebuild_legs_from_old(old_stops: List[Dict], old_legs: List[Optional[Dict]], new_stops: List[Dict]) -> List[Optional[Dict]]:
    """
    Given an OLD (stops, legs_between) and a NEW stop list, produce a new legs_between for the NEW list.

    Strategy:
    - Keep a leg if the exact (from_id,to_id) pair existed previously.
    - Otherwise infer a mode hint from legs touching either endpoint; default 'car'.
    - For car/bus/train: compute OSRM route; for plane: straight line; else None.
    """
    # old adjacency -> leg
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
            continue

        mode = "car"
        for (a, b), leg in leg_map.items():
            if a == frm_id or b == frm_id or a == to_id or b == to_id:
                m = (leg.get("mode") or "").lower()
                if m in {"car", "bus", "train", "plane"}:
                    mode = m
                    break

        A = new_stops[i]
        B = new_stops[i + 1]
        if mode in {"car", "bus", "train"}:
            try:
                route = osrm_driving_route(A["lat"], A["lon"], B["lat"], B["lon"])
                new_legs.append({"mode": mode, "note": "", **route})
            except Exception:
                new_legs.append(
                    {
                        "mode": mode,
                        "note": "routing failed",
                        "distance_m": None,
                        "duration_s": None,
                        "geometry_latlon": interpolate_line(A["lat"], A["lon"], B["lat"], B["lon"]),
                    }
                )
        elif mode == "plane":
            new_legs.append(
                {
                    "mode": "plane",
                    "note": "",
                    "distance_m": None,
                    "duration_s": None,
                    "geometry_latlon": interpolate_line(A["lat"], A["lon"], B["lat"], B["lon"]),
                }
            )
        else:
            new_legs.append(None)

    return new_legs

# ----------------------- Autosave -----------------------
def autosave():
    ss = st.session_state
    payload = {
        "name": ss["trip_name"],
        "trip_start_date": ss["trip_start_date"].isoformat() if hasattr(ss["trip_start_date"], "isoformat") else str(ss["trip_start_date"]),
        "stops": ss["stops"],
        "legs_between": ss["legs_between"],
    }

    # Shared save (Supabase)
    save_shared(payload)

    # Local fallback (export/debug)
    out_dir = Path("exports")
    out_dir.mkdir(exist_ok=True)
    (out_dir / "autosave_itinerary.json").write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")

    ss["dirty"] = False


def import_json_bytes(raw: bytes):
    ss = st.session_state
    try:
        data = json.loads(raw.decode("utf-8"))
    except Exception as e:
        raise ValueError(f"Invalid JSON: {e}")

    ss["trip_name"] = str(data.get("name", "Imported Trip"))

    tsd = data.get("trip_start_date")
    if tsd:
        try:
            y, m, d = [int(x) for x in str(tsd).split("-")]
            ss["trip_start_date"] = date(y, m, d)
        except Exception:
            pass

    stops = data.get("stops")
    legs_between = data.get("legs_between")

    if not isinstance(stops, list):
        raise ValueError("JSON must contain a list field 'stops'.")

    clean_stops = []
    for s in stops:
        for k in ("id", "name", "lat", "lon"):
            if k not in s:
                raise ValueError(f"Stop missing '{k}': {s}")
        clean_stops.append(
            {
                "id": str(s["id"]),
                "name": str(s["name"]),
                "lat": float(s["lat"]),
                "lon": float(s["lon"]),
                "overnight": bool(s.get("overnight", False)),
                "note": str(s.get("note", "") or ""),
            }
        )

    ss["stops"] = clean_stops

    if legs_between is None:
        legs_between = [None] * max(0, len(clean_stops) - 1)
    if not isinstance(legs_between, list):
        raise ValueError("Field 'legs_between' must be a list (or absent).")

    needed = max(0, len(clean_stops) - 1)
    if len(legs_between) < needed:
        legs_between = legs_between + [None] * (needed - len(legs_between))
    if len(legs_between) > needed:
        legs_between = legs_between[:needed]

    ss["legs_between"] = legs_between

    mx = 0
    for s in clean_stops:
        sid = str(s.get("id", ""))
        if sid.startswith("S"):
            try:
                mx = max(mx, int(sid[1:]))
            except Exception:
                pass
    ss["next_stop_id"] = mx + 1

    ss["pending_stop"] = None
    ss["pending_preview"] = None
    ss["map_center"] = compute_center(clean_stops)
    mark_dirty()


# ----------------------- Autocomplete -----------------------
def search_api_labels(query: str) -> List[str]:
    ss = st.session_state
    q = (query or "").strip()
    if len(q) < 2:
        ss["search_lookup"] = {}
        return []
    try:
        results = forward_search(q, ss["user_agent"], limit=6)
    except Exception:
        ss["search_lookup"] = {}
        return []

    lookup: Dict[str, Dict] = {}
    labels: List[str] = []
    for r in results:
        base = r["name"]
        label = base
        j = 2
        while label in lookup:
            label = f"{base} ({j})"
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


def poll_shared_if_newer():
    ss = st.session_state
    row = load_shared()
    if not row:
        return
    updated_at = str(row.get("updated_at") or "")
    if not updated_at:
        return
    if updated_at != ss.get("last_seen_updated_at", ""):
        data = row.get("data") or {}
        ss["trip_name"] = data.get("name", ss.get("trip_name", "My Trip"))
        try:
            if data.get("trip_start_date"):
                ss["trip_start_date"] = date.fromisoformat(data["trip_start_date"])
        except Exception:
            pass
        ss["stops"] = data.get("stops", ss.get("stops", []))
        ss["legs_between"] = data.get("legs_between", ss.get("legs_between", []))
        ss["dirty"] = False
        ss["map_version"] += 1
        ss["last_seen_updated_at"] = updated_at

def bootstrap_shared_if_missing():
    ss = st.session_state
    if ss.get("_bootstrapped_shared"):
        return
    ss["_bootstrapped_shared"] = True
    row = load_shared()
    if not row:
        autosave()
    else:
        ss["last_seen_updated_at"] = str(row.get("updated_at") or "")

# ======================= APP =======================
st.set_page_config(page_title="Itinerary", layout="wide")
require_pin()
ensure_state()

# Realtime sync: rerun every 1s when idle; pause while typing in search widgets.
_detect_typing_activity()
idle_for = time.time() - float(st.session_state.get("last_input_time", 0.0))
if st.session_state.get("pause_refresh") and idle_for > 2.0:
    st.session_state["pause_refresh"] = False
if not st.session_state.get("pause_refresh"):
    st_autorefresh(interval=1000, key="__rt_tick")

bootstrap_shared_if_missing()
poll_shared_if_newer()
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
    uploaded = st.file_uploader("Import saved JSON", type=["json"])
    if uploaded is not None:
        try:
            import_json_bytes(uploaded.getvalue())
            st.success("Imported.")
            st.rerun()
        except Exception as e:
            st.error(str(e))
st.divider()

# Search above map
st.markdown("### Add next stop (search OSM)")
selected_label = st_searchbox(
    search_api_labels,
    key=f"searchbox_{ss['search_key_version']}",
    placeholder="Type a place…",
    label="Search",
)

selection = None
if selected_label:
    selection = ss.get("search_lookup", {}).get(selected_label)

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

# Confirm add + transport prompt
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
                ss["search_key_version"] += 1  # reset search
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
                ss["search_key_version"] += 1  # reset search
                ss["last_selected_label"] = None
                st.rerun()

st.divider()

# Map
m = build_map(ss["stops"], ss["legs_between"])
st_folium(m, height=720, width=None, key=f"map_{ss['map_version']}")
st.divider()

# Itinerary
st.markdown("## Itinerary")

# Move editor toggle below itinerary
btn_col1, btn_col2 = st.columns([1, 5])
with btn_col1:
    if st.button("Edit itinerary" if not ss["show_editor"] else "Hide editor", use_container_width=True):
        ss["show_editor"] = not ss["show_editor"]
        st.rerun()


# Drag & drop reorder UI (auto-apply on drag)
if ss["stops"]:
    with st.expander("Reorder stops (drag & drop)", expanded=False):
        if not HAS_SORTABLES:
            st.error("Drag & drop requires: pip install streamlit-sortables")
        else:
            # The sortable widget returns a list; we parse stop IDs from "S# | name".
            # If the order changed, we immediately apply it (and auto-renumber).
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

# Editor (hidden)
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

                # Change / replace this stop (without deleting)
                with st.expander("Change this stop (search OSM)", expanded=False):
                    # local searchbox key must be unique and stable per row
                    def _search_local(q: str) -> List[str]:
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
                            # Rebuild legs so geometry/times update for adjacent legs
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
                    # rebuild legs to preserve what still matches and recompute what must change
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

# Autosave always
autosave()

with st.expander("Save / Export", expanded=False):
    if ss["last_save_paths"]:
        jp, hp = ss["last_save_paths"]
        st.success("Autosaved.")
        st.write(jp)
        st.write(hp)
    else:
        st.caption("Autosave will write to exports/ once you make changes.")
    if st.button("Force save now", use_container_width=True):
        ss["dirty"] = True
        autosave()
        st.rerun()
