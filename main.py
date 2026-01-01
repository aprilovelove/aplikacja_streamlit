import streamlit as st
import osmnx as ox
import networkx as nx
import folium
from streamlit_folium import st_folium
import json
import math
from typing import List, Tuple
from streamlit_js_eval import get_geolocation
from datetime import datetime
import qrcode
from io import BytesIO

# Importy z plików lokalnych
from auth import login_user, register_user
from database import SessionLocal, SavedRoute, User

# --- KONFIGURACJA I SŁOWNIKI ---

BIKE_PROFILES = {
    "Szosowy": {
        "good": ["asphalt", "concrete", "paved"],
        "neutral": ["sett", "unpaved"],
        "bad": ["gravel", "cobblestone", "dirt", "sand", "grass", "ground"]
    },
    "Gravel": {
        "good": ["asphalt", "gravel", "unpaved", "dirt", "compacted"],
        "neutral": ["concrete", "sett", "cobblestone"],
        "bad": ["sand", "grass"]
    },
    "MTB": {
        "good": ["gravel", "dirt", "sand", "grass", "ground", "cobblestone", "unpaved"],
        "neutral": ["asphalt", "concrete", "sett"],
        "bad": []
    }
}


# --- LOGIKA ANALITYCZNA I POMOCNICZA ---

def analyze_route_compatibility(G, route_nodes, bike_type):
    if not bike_type or bike_type == "Brak":
        return None, None
    edges = ox.routing.route_to_gdf(G, route_nodes)
    if 'surface' not in edges.columns:
        return "Brak danych o nawierzchni", "gray"
    surfaces = edges['surface'].dropna().tolist()
    if not surfaces:
        return "Brak danych o nawierzchni", "gray"
    score = 0
    profile = BIKE_PROFILES[bike_type]
    for s in surfaces:
        s_val = s[0] if isinstance(s, list) else s
        if s_val in profile["good"]:
            score += 1
        elif s_val in profile["neutral"]:
            score += 0.5
    ratio = score / len(surfaces)
    if ratio > 0.8:
        return "🟢 Trasa idealnie dopasowana", "green"
    elif ratio > 0.4:
        return "🟡 Trasa średnio dopasowana", "orange"
    else:
        return "🔴 Trasa niedopasowana", "red"


def create_gpx(geojson_data):
    coords = geojson_data['features'][0]['geometry']['coordinates']
    gpx = '<?xml version="1.0" encoding="UTF-8"?>\n'
    gpx += '<gpx version="1.1" creator="BikePlanner"><trk><name>Trasa Projektant</name><trkseg>\n'
    for lon, lat in coords:
        gpx += f'<trkpt lat="{lat}" lon="{lon}"></trkpt>\n'
    gpx += '</trkseg></trk></gpx>'
    return gpx


def generate_qr_image(lat, lon):
    data = f"http://osmand.net/go?lat={lat}&lon={lon}&z=14"
    qr = qrcode.QRCode(version=1, box_size=10, border=4)
    qr.add_data(data)
    qr.make(fit=True)
    img = qr.make_image(fill_color="black", back_color="white")
    buf = BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()


# --- LOGIKA GENERATORA ---

def calculate_square_corners(start_lon: float, start_lat: float, side_length: float) -> List[Tuple[float, float]]:
    R = 6371000
    corners = []
    current_lon, current_lat = start_lon, start_lat
    bearings = [0, 90, 180, 270]
    for bearing in bearings:
        corners.append((current_lon, current_lat))
        lat_rad, lon_rad = math.radians(current_lat), math.radians(current_lon)
        bearing_rad = math.radians(bearing)
        angular_distance = side_length / R
        new_lat_rad = math.asin(math.sin(lat_rad) * math.cos(angular_distance) +
                                math.cos(lat_rad) * math.sin(angular_distance) * math.cos(bearing_rad))
        new_lon_rad = lon_rad + math.atan2(math.sin(bearing_rad) * math.sin(angular_distance) * math.cos(lat_rad),
                                           math.cos(angular_distance) - math.sin(lat_rad) * math.sin(new_lat_rad))
        current_lon, current_lat = math.degrees(new_lon_rad), math.degrees(new_lat_rad)
    return corners


def find_path_avoiding_edges(G, start_node, end_node, forbidden_edges):
    temp_G = G.copy()
    for u, v in list(temp_G.edges()):
        if (u, v) in forbidden_edges or (v, u) in forbidden_edges:
            temp_G.remove_edge(u, v)
    try:
        return nx.shortest_path(temp_G, start_node, end_node, weight='length')
    except nx.NetworkXNoPath:
        return nx.shortest_path(G, start_node, end_node, weight='length')


def find_circular_route(G, corners):
    corner_nodes = [ox.nearest_nodes(G, lon, lat) for lon, lat in corners]
    route_segments = []
    used_edges = set()
    for i in range(len(corner_nodes)):
        start, end = corner_nodes[i], corner_nodes[(i + 1) % len(corner_nodes)]
        try:
            segment = find_path_avoiding_edges(G, start, end, used_edges)
            route_segments.extend(segment[:-1])
            for u, v in zip(segment[:-1], segment[1:]):
                used_edges.add((u, v))
                used_edges.add((v, u))
        except:
            return []
    if route_segments: route_segments.append(route_segments[0])
    return route_segments


def remove_backtracking(coordinates: List[List[float]]) -> List[List[float]]:
    if len(coordinates) < 3: return coordinates
    i, result = 0, []
    while i < len(coordinates):
        result.append(coordinates[i])
        found_backtrack = False
        for j in range(i + 2, min(i + 50, len(coordinates))):
            if coordinates[i] == coordinates[j]:
                i, found_backtrack = j, True
                break
        if not found_backtrack: i += 1
    return result


def clean_line_coordinates(coordinates: List[List[float]]) -> List[List[float]]:
    if not coordinates: return []
    cleaned = [coordinates[0]]
    for i in range(1, len(coordinates)):
        if coordinates[i] != coordinates[i - 1]: cleaned.append(coordinates[i])
    return remove_backtracking(cleaned)


# --- APLIKACJA STREAMLIT ---
st.set_page_config(page_title="Bike Planner Pro", layout="wide")

if 'user' not in st.session_state: st.session_state.user = None
if 'generated_geojson' not in st.session_state: st.session_state.generated_geojson = None
if 'map_center' not in st.session_state: st.session_state.map_center = [50.2859, 18.9549]
if 'load_info' not in st.session_state: st.session_state.load_info = None
if 'route_score' not in st.session_state: st.session_state.route_score = (None, None)


def load_route_action(geojson_data, name):
    data = json.loads(geojson_data)
    st.session_state.generated_geojson = data
    st.session_state.load_info = name
    # Pobieramy pierwszy punkt trasy, aby przesunąć tam mapę
    first_coord = data['features'][0]['geometry']['coordinates'][0]
    st.session_state.map_center = [first_coord[1], first_coord[0]]
    st.toast(f"Wczytano: {name}. Mapa została wycentrowana na starcie!", icon="🚲")


# --- SIDEBAR ---
with st.sidebar:
    if st.session_state.user is None:
        st.header("🔑 Logowanie")
        choice = st.radio("Akcja", ["Logowanie", "Rejestracja"])
        u = st.text_input("Użytkownik")
        p = st.text_input("Hasło", type="password")
        if choice == "Logowanie":
            if st.button("Zaloguj"):
                user = login_user(u, p)
                if user:
                    st.session_state.user = {"id": user.id, "name": user.username}
                    st.rerun()
                else:
                    st.error("Błędne dane")
        else:
            if st.button("Zarejestruj"):
                if register_user(u, p):
                    st.success("Konto utworzone!")
                else:
                    st.error("Użytkownik już istnieje.")
    else:
        st.success(f"Zalogowany jako: {st.session_state.user['name']}")
        if st.button("Wyloguj"):
            st.session_state.user = None
            st.rerun()

    st.divider()
    st.header("⚙️ Parametry Trasy")
    if st.button("Użyj mojej lokalizacji"):
        loc = get_geolocation()
        if loc:
            st.session_state.map_center = [loc['coords']['latitude'], loc['coords']['longitude']]
            st.rerun()

    lat_input = st.number_input("Szerokość (Lat)", value=st.session_state.map_center[0], format="%.6f")
    lon_input = st.number_input("Długość (Lon)", value=st.session_state.map_center[1], format="%.6f")
    dist_km = st.slider("Dystans (km)", 5, 100, 20)
    bike_type = st.selectbox("Typ roweru", ["Brak", "Szosowy", "Gravel", "MTB"])
    clean_option = st.checkbox("Wyczyść backtracking", value=True)
    generate_btn = st.button("🚀 Wygeneruj Trasę", type="primary")

# --- INTERFEJS GŁÓWNY ---
tab1, tab2, tab3 = st.tabs(["🚲 Generator", "🌍 Społeczność", "📂 Moje Trasy"])

with tab1:
    if st.session_state.load_info:
        st.info(f"📍 **Aktywna trasa:** {st.session_state.load_info}")
        if st.button("Wyczyść i zacznij od nowa"):
            st.session_state.generated_geojson = None
            st.session_state.load_info = None
            st.rerun()

    if generate_btn:
        with st.spinner("Generowanie pętli..."):
            try:
                side_m = (dist_km * 1000 * 0.65) / 4
                corners = calculate_square_corners(lon_input, lat_input, side_m)
                G = ox.graph_from_point((lat_input, lon_input), dist=side_m * 1.5, network_type="bike")
                route_nodes = find_circular_route(G, corners)
                if route_nodes:
                    nodes_df, _ = ox.graph_to_gdfs(G)
                    raw_coords = [[nodes_df.loc[n].y, nodes_df.loc[n].x] for n in route_nodes]
                    if clean_option:
                        clean_input = [[c[1], c[0]] for c in raw_coords]
                        cleaned = clean_line_coordinates(clean_input)
                        display_coords = [[c[1], c[0]] for c in cleaned]
                    else:
                        display_coords = raw_coords
                    dist = ox.routing.route_to_gdf(G, route_nodes)['length'].sum() / 1000
                    st.session_state.route_score = analyze_route_compatibility(G, route_nodes, bike_type)
                    st.session_state.load_info = f"Nowa trasa {round(dist, 1)} km"
                    st.session_state.generated_geojson = {
                        "type": "FeatureCollection",
                        "features": [{
                            "type": "Feature",
                            "geometry": {"type": "LineString", "coordinates": [[c[1], c[0]] for c in display_coords]},
                            "properties": {"length_km": round(dist, 2)}
                        }]
                    }
                    # Aktualizacja centrum mapy na punkt startowy wygenerowanej trasy
                    st.session_state.map_center = [lat_input, lon_input]
                else:
                    st.error("Nie znaleziono pętli.")
            except Exception as e:
                st.error(f"Błąd: {e}")

    # RENDERING MAPY I WYNIKÓW
    if st.session_state.generated_geojson:
        data = st.session_state.generated_geojson
        dist = data['features'][0]['properties']['length_km']

        # Pobieramy dynamicznie punkt startowy trasy do postawienia markera
        start_point = [data['features'][0]['geometry']['coordinates'][0][1],
                       data['features'][0]['geometry']['coordinates'][0][0]]

        c1, c2 = st.columns([1, 2])
        c1.metric("Długość", f"{dist} km")
        status, color = st.session_state.route_score
        if status: c2.markdown(f"**Dopasowanie:** :{color}[{status}]")

        # Używamy map_center z session_state do centrowania mapy
        m = folium.Map(location=st.session_state.map_center, zoom_start=13)
        folium.GeoJson(data, style_function=lambda x: {'color': '#2ecc71', 'weight': 5}).add_to(m)
        folium.Marker(start_point, popup="Start/Meta", icon=folium.Icon(color='red')).add_to(m)
        st_folium(m, width=1200, height=550, key="active_gen_map")

        st.divider()
        st.subheader("📲 Wyślij na telefon")
        col_down1, col_down2, col_down3 = st.columns([1, 1, 1])

        gpx_data = create_gpx(data)

        with col_down1:
            st.download_button("🗺️ POBIERZ PLIK GPX", gpx_data, "trasa.gpx", "application/gpx+xml",
                               use_container_width=True)
            st.caption("Pobierz i otwórz w OsmAnd.")

        with col_down2:
            st.write("Szybki podgląd lokalizacji (OsmAnd):")
            # QR kod generowany dla aktualnego punktu startowego trasy
            qr_img = generate_qr_image(start_point[0], start_point[1])
            st.image(qr_img, width=150)
            st.caption("Skanuj, aby wycelować mapę w telefonie.")

        with col_down3:
            if st.session_state.user:
                with st.popover("💾 Zapisz w profilu", use_container_width=True):
                    r_name = st.text_input("Nazwa trasy", "Moja Trasa")
                    r_vis = st.selectbox("Widoczność", ["public", "private"])
                    if st.button("Potwierdź Zapis"):
                        db = SessionLocal()
                        new_r = SavedRoute(user_id=st.session_state.user['id'], name=r_name,
                                           geojson_data=json.dumps(data), visibility=r_vis)
                        db.add(new_r)
                        db.commit()
                        db.close()
                        st.success("Zapisano!")
            else:
                st.button("💾 Zaloguj się by zapisać", disabled=True, use_container_width=True)

    else:
        st.info("Ustaw parametry i wygeneruj trasę.")
        m_preview = folium.Map(location=st.session_state.map_center, zoom_start=13)
        folium.Marker(st.session_state.map_center, icon=folium.Icon(color='blue')).add_to(m_preview)
        st_folium(m_preview, width=1200, height=550, key="preview_map")

# --- POZOSTAŁE ZAKŁADKI ---
with tab2:
    st.header("🌍 Społeczność")
    db = SessionLocal()
    routes = db.query(SavedRoute).filter_by(visibility='public').all()
    for r in routes:
        with st.container(border=True):
            c1, c2 = st.columns([3, 1])
            c1.write(f"**{r.name}** | Autor: {r.owner.username}")
            if c2.button("🚀 Wczytaj", key=f"pub_{r.id}"):
                load_route_action(r.geojson_data, r.name)
                st.rerun()
    db.close()

with tab3:
    if st.session_state.user:
        st.header("📂 Twoje Trasy")
        db = SessionLocal()
        my_routes = db.query(SavedRoute).filter_by(user_id=st.session_state.user['id']).all()
        for r in my_routes:
            with st.container(border=True):
                c1, c2, c3 = st.columns([2, 1, 1])
                c1.write(f"**{r.name}** ({r.visibility})")
                if c2.button("📂 Wczytaj", key=f"my_{r.id}"):
                    load_route_action(r.geojson_data, r.name)
                    st.rerun()
                if c3.button("🗑️ Usuń", key=f"del_{r.id}"):
                    db.delete(r);
                    db.commit();
                    st.rerun()
        db.close()
    else:
        st.warning("Zaloguj się!")