#!/usr/bin/env python3
"""
Map-first route editor for AutoDrive GeoJSON routes.

The browser UI proxies a remote api_server.py /state endpoint, shows the live
machine position, lets you draw/edit a GeoJSON LineString, and saves it to a
route file consumed by 08_stream_waypoints.py:

    ./route_viewer.py --api http://172.30.0.137:8080/state
    ./08_stream_waypoints.py --route line
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.error import URLError
from urllib.parse import parse_qs, urlparse
from urllib.request import urlopen


HERE = Path(__file__).resolve().parent
DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 8090
DEFAULT_API = "http://172.30.0.137:8080/state"
DEFAULT_OUTPUT = "line.geojson"
SAVE_TARGETS = {
    "line": "line.geojson",
    "uturn": "u_field.geojson",
}


HTML = """<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>AutoDrive Route Editor</title>
  <link rel="stylesheet" href="https://unpkg.com/leaflet@1.9.4/dist/leaflet.css"
    integrity="sha256-p4NxAoJBhIINfQxG1pM9g8tI7kLXeIkJFZ7aUXk6nF8=" crossorigin="">
  <style>
    .leaflet-pane,.leaflet-tile,.leaflet-marker-icon,.leaflet-marker-shadow,
    .leaflet-tile-container,.leaflet-pane>svg,.leaflet-pane>canvas,
    .leaflet-zoom-box,.leaflet-image-layer,.leaflet-layer{position:absolute;left:0;top:0}
    .leaflet-container{overflow:hidden;-webkit-tap-highlight-color:transparent}
    .leaflet-tile,.leaflet-marker-icon,.leaflet-marker-shadow{user-select:none;-webkit-user-drag:none}
    .leaflet-tile{visibility:hidden}.leaflet-tile-loaded{visibility:inherit}
    .leaflet-zoom-animated{transform-origin:0 0}.leaflet-interactive{cursor:pointer}
    .leaflet-control{position:relative;z-index:800;pointer-events:auto}
    .leaflet-top,.leaflet-bottom{position:absolute;z-index:1000;pointer-events:none}
    .leaflet-top{top:0}.leaflet-right{right:0}.leaflet-bottom{bottom:0}.leaflet-left{left:0}
    .leaflet-control{float:left;clear:both}.leaflet-right .leaflet-control{float:right}
    .leaflet-top .leaflet-control{margin-top:10px}.leaflet-bottom .leaflet-control{margin-bottom:10px}
    .leaflet-left .leaflet-control{margin-left:10px}.leaflet-right .leaflet-control{margin-right:10px}
    .leaflet-control-attribution{background:rgba(255,255,255,.85);padding:0 5px;font-size:11px}
    .leaflet-pane{z-index:400}.leaflet-tile-pane{z-index:200}.leaflet-overlay-pane{z-index:400}
    .leaflet-shadow-pane{z-index:500}.leaflet-marker-pane{z-index:600}.leaflet-tooltip-pane{z-index:650}.leaflet-popup-pane{z-index:700}

    :root{
      color-scheme:dark;
      font-family:"IBM Plex Sans","Segoe UI",ui-sans-serif,system-ui,sans-serif;
      color:#edf4f7;background:#0d1418;letter-spacing:0;
      --bg:#0d1418;--panel:rgba(17,24,29,.88);--panel-strong:rgba(20,28,34,.96);
      --line:rgba(202,224,233,.12);--ink:#edf4f7;--muted:#9eb0b8;
      --accent:#8eb9c7;--accent-strong:#6f98a8;--teal:#8fd2e4;
      --teal-soft:rgba(143,210,228,.14);--button-fill:rgba(231,240,244,.08);
      --button-fill-hover:rgba(231,240,244,.12);--button-fill-strong:rgba(143,210,228,.18);
      --button-line:rgba(231,240,244,.12);--button-line-strong:rgba(143,210,228,.26);
      --button-ink:#dce8ed;--danger:#ef9a82;--danger-soft:rgba(239,154,130,.14);
      --warn-soft:rgba(234,146,117,.14);--shadow-lg:0 20px 48px rgba(0,0,0,.32);
      --shadow-sm:0 10px 24px rgba(0,0,0,.22);--orange:#e67f4e;--red:#d94141;
    }
    *{box-sizing:border-box}
    body{margin:0;min-height:100vh;overflow:hidden;color:var(--ink);background:#f6f0e7}
    #map{position:fixed;inset:0;background:#f6f0e7}
    .floating{position:fixed;z-index:1200}
    .left-stack{left:10px;top:10px;display:grid;gap:8px}
    .panel{
      position:fixed;left:58px;top:10px;width:286px;max-height:calc(100vh - 20px);z-index:1190;
      display:grid;gap:9px;padding:12px;overflow:hidden;
      grid-template-rows:auto minmax(0,1fr);
      background:var(--panel-strong);border:1px solid var(--line);
      border-radius:20px;box-shadow:var(--shadow-sm);backdrop-filter:blur(12px);color:var(--ink)
    }
    body.panel-closed .panel{display:none}
    .panel-head{display:flex;justify-content:space-between;align-items:baseline;gap:12px}
    .section{margin:0 0 4px;color:var(--muted);font-size:11px;font-weight:700;letter-spacing:.14em;text-transform:uppercase}
    h1{font-size:17px;line-height:1.1;margin:0;font-weight:800;letter-spacing:-.01em}
    .hint{margin:0;color:var(--muted);font-size:11px;line-height:1.35}
    .pill{
      display:inline-flex;align-items:center;justify-content:center;min-width:52px;padding:6px 8px;
      border-radius:999px;border:1px solid rgba(83,214,137,.28);background:rgba(83,214,137,.18);
      color:#8ff0ae;font-size:11px;font-weight:800;letter-spacing:.05em;text-transform:uppercase;white-space:nowrap
    }
    .panel-toggle{
      position:relative;
      width:36px;height:36px;padding:0;display:inline-flex;align-items:center;justify-content:center;
      border-radius:12px;background:rgba(236,243,246,.9);color:#102028;
      border:1px solid rgba(16,32,40,.32);font-weight:800;box-shadow:0 10px 24px rgba(0,0,0,.18)
    }
    .panel-toggle:hover{background:rgba(244,248,250,.98);border-color:rgba(16,32,40,.42)}
    .panel-toggle.is-active{
      background:rgba(143,210,228,.96);color:#102028;border-color:rgba(16,32,40,.38);
      box-shadow:0 0 0 3px rgba(143,210,228,.28),0 10px 24px rgba(0,0,0,.22)
    }
    .panel-toggle.is-active:after{content:"";position:absolute;right:5px;top:5px;width:7px;height:7px;border-radius:999px;background:#102028}
    .tool-toggle{
      position:relative;width:36px;height:36px;padding:0;display:inline-flex;align-items:center;justify-content:center;
      border-radius:12px;background:rgba(236,243,246,.9);color:#102028;border:1px solid rgba(16,32,40,.32);
      font-weight:900;box-shadow:0 10px 24px rgba(0,0,0,.18)
    }
    .tool-toggle.is-active{
      background:rgba(230,127,78,.96);color:#102028;border-color:rgba(16,32,40,.38);
      box-shadow:0 0 0 3px rgba(230,127,78,.28),0 10px 24px rgba(0,0,0,.22)
    }
    .tool-toggle.is-active:after{content:"";position:absolute;right:5px;top:5px;width:7px;height:7px;border-radius:999px;background:#102028}
    .panel-view{display:none;gap:9px;min-height:0;overflow:auto}
    .panel-view.active{display:grid}
    h2{font-size:10px;text-transform:uppercase;color:var(--muted);margin:0;font-weight:700;letter-spacing:.14em}
    label{display:grid;gap:6px;font-size:10px;color:var(--muted);font-weight:700;letter-spacing:.08em;text-transform:uppercase}
    input,select,textarea,button{font:inherit}
    input,select,textarea{
      width:100%;border:1px solid var(--line);border-radius:10px;background:rgba(20,28,34,.96);
      color:var(--ink);padding:8px 10px;outline:none;font-size:12px
    }
    input:focus,select:focus,textarea:focus{outline:2px solid rgba(33,95,121,.12);border-color:rgba(33,95,121,.22)}
    textarea{min-height:210px;resize:vertical;font:11px/1.34 "IBM Plex Mono",ui-monospace,SFMono-Regular,Menlo,Consolas,monospace}
    button{
      border:0;cursor:pointer;transition:background 140ms ease,color 140ms ease,box-shadow 140ms ease,transform 140ms ease,border-color 140ms ease,opacity 140ms ease
    }
    button:hover{transform:translateY(-1px)}
    button.primary,button.danger,button:not(.panel-toggle):not(.tool-toggle):not(.point-row){
      border-radius:12px;padding:8px 10px;min-height:32px;background:var(--button-fill);color:var(--button-ink);
      border:1px solid var(--button-line);font-size:12px;font-weight:700;box-shadow:inset 0 1px 0 rgba(255,255,255,.03)
    }
    button.primary{background:var(--button-fill-strong);border-color:var(--button-line-strong)}
    button.primary:hover{background:rgba(143,210,228,.22);border-color:rgba(143,210,228,.34)}
    button.danger{color:var(--danger);border-color:rgba(239,154,130,.18);background:var(--danger-soft)}
    button.active{background:rgba(143,210,228,.14);border-color:rgba(143,210,228,.28);color:var(--teal)}
    button:disabled{opacity:.42;cursor:not-allowed}
    .grid2{display:grid;grid-template-columns:1fr 1fr;gap:7px}
    .grid3{display:grid;grid-template-columns:repeat(3,1fr);gap:7px}
    .dock-actions{display:grid;grid-template-columns:1fr 1fr;gap:7px}
    .map-switch{display:grid;grid-template-columns:1fr 1fr;gap:7px}
    .map-switch button.active{background:rgba(143,210,228,.22);border-color:rgba(143,210,228,.38);color:#dff8ff;box-shadow:0 0 0 2px rgba(143,210,228,.12)}
    .turn-controls{display:grid;grid-template-columns:1fr 1fr;gap:7px}
    .turn-controls label:first-child{grid-column:1/-1}
    body.uturn-mode #map{cursor:crosshair}
    .panel-card{display:grid;gap:8px;padding:10px;border-radius:14px;background:rgba(20,28,34,.72);border:1px solid rgba(202,224,233,.08);box-shadow:inset 0 1px 0 rgba(255,255,255,.02)}
    .stack{display:grid;gap:7px}
    .status,.stat{
      border:1px solid rgba(202,224,233,.08);border-radius:14px;background:rgba(24,33,39,.92);
      padding:8px 10px;font-size:12px;line-height:1.3;box-shadow:inset 0 1px 0 rgba(255,255,255,.02)
    }
    .status strong,.stat span{display:block;color:var(--muted);font-size:10px;font-weight:700;letter-spacing:.08em;text-transform:uppercase;margin-bottom:2px}
    .ok{border-color:rgba(83,214,137,.28);color:#8ff0ae}.warn{border-color:rgba(234,146,117,.22);color:#efb08e}.bad{border-color:rgba(239,154,130,.28);color:var(--danger)}
    .stats,.mini-stats{display:grid;grid-template-columns:repeat(2,1fr);gap:7px}
    .stat{min-width:0}
    .stat b{display:block;font-size:13px;white-space:nowrap;overflow:hidden;text-overflow:ellipsis;color:var(--ink)}
    .point-list{display:flex;flex-direction:column;gap:6px;max-height:min(40vh,360px);overflow:auto;padding-right:3px}
    .point-row{
      display:flex;align-items:center;gap:8px;width:100%;padding:8px 9px;border:1px solid transparent;
      border-radius:12px;background:rgba(22,30,36,.96)!important;color:var(--ink);text-align:left;
      box-shadow:inset 0 1px 0 rgba(255,255,255,.02)
    }
    .point-row.selected{border-color:rgba(126,199,220,.22);background:rgba(126,199,220,.14)!important}
    .badge{display:grid;place-items:center;width:23px;height:23px;border-radius:999px;background:var(--button-fill);border:1px solid var(--button-line);color:var(--teal);font-size:10px;font-weight:900}
    .point-row:first-child .badge{background:var(--orange)}
    .meta{font-size:11px;color:var(--muted);line-height:1.25;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
    .machine-marker{display:flex;flex-direction:column;align-items:center;gap:6px;pointer-events:auto}
    .machine-arrow{position:relative;width:24px;height:24px;background:#111;clip-path:polygon(50% 0%,100% 100%,50% 78%,0% 100%);filter:drop-shadow(0 6px 14px rgba(0,0,0,.28));transform:rotate(var(--heading,0deg));transform-origin:50% 70%}
    .machine-arrow.no-heading{opacity:.35}
    .machine-arrow:after{content:"";position:absolute;inset:2px;background:var(--teal);clip-path:polygon(50% 0%,100% 100%,50% 78%,0% 100%)}
    .machine-label{padding:4px 8px;border-radius:999px;background:rgba(17,24,29,.92);border:1px solid rgba(143,210,228,.24);color:#dfffea;font-size:11px;font-weight:700;letter-spacing:.04em;text-transform:uppercase;white-space:nowrap}
    .route-handle{width:20px;height:20px;border:3px solid #f6f0e7;border-radius:999px;background:#6f9299;color:transparent;box-shadow:0 7px 18px rgba(20,33,41,.28)}
    .route-handle.start{width:24px;height:24px;background:linear-gradient(135deg,#8eb9c7,#ef9969)}
    .route-handle.selected{background:#e67f4e;border-color:#fff;box-shadow:0 0 0 5px rgba(230,127,78,.28),0 7px 18px rgba(20,33,41,.28)}
    @media(max-width:820px){
      body{overflow:auto}.left-stack{position:relative;left:auto;top:auto;display:flex;margin:12px}.panel{position:relative;left:auto;top:auto;width:auto;margin:0 12px 12px;max-height:none}
      #map{position:relative;height:58vh}
    }
  </style>
</head>
<body>
  <div id="map"></div>
  <div class="floating left-stack">
    <button class="panel-toggle is-active" data-panel="route" type="button" title="Route panel" aria-label="Route panel" aria-pressed="true">R</button>
    <button class="panel-toggle" data-panel="points" type="button" title="Points panel" aria-label="Points panel" aria-pressed="false">P</button>
    <button class="panel-toggle" data-panel="json" type="button" title="JSON panel" aria-label="JSON panel" aria-pressed="false">J</button>
    <button class="tool-toggle" id="uturnStackBtn" type="button" title="U-turn tool" aria-label="U-turn tool" aria-pressed="false">U</button>
  </div>

  <aside class="panel">
    <div class="panel-head">
      <div class="brand">
        <p class="section">AutoDrive</p>
        <h1>Route Editor</h1>
      </div>
      <div class="pill" id="livePill">LIVE</div>
    </div>

    <section id="panel-route" class="panel-view active">
      <div class="panel-card">
        <h2>Machine</h2>
        <label>API URL <input id="apiUrl" value="__API_URL__"></label>
        <div class="dock-actions">
          <button id="pollBtn" class="primary">Pause</button>
          <button id="centerBtn">Center</button>
        </div>
        <div class="map-switch" aria-label="Map style">
          <button id="mapLightBtn" class="active" type="button">Light</button>
          <button id="mapSatelliteBtn" type="button">Satellite</button>
        </div>
        <div id="positionStatus" class="status warn"><strong>Position</strong>Waiting for data.</div>
      </div>

      <div class="panel-card">
        <h2>Route</h2>
        <div class="mini-stats">
          <div class="stat"><span>Pts</span><b id="pointCount">0</b></div>
          <div class="stat"><span>Len</span><b id="routeLength">0 m</b></div>
          <div class="stat"><span>Gap</span><b id="maxGap">0 m</b></div>
          <div class="stat"><span>Sel</span><b id="selectedPoint">none</b></div>
        </div>
        <div class="dock-actions">
          <button id="fitBtn">Fit</button>
          <button id="addCurrentBtn" class="primary">Add Current</button>
        </div>
      </div>

      <div class="panel-card">
        <h2>U-turn</h2>
        <div class="turn-controls">
          <label>Radius m <input id="uturnRadius" type="number" min="0.5" max="500" step="0.5" value="6"></label>
          <label>Side
            <select id="uturnSide">
              <option value="left">Left</option>
              <option value="right">Right</option>
            </select>
          </label>
          <label>Points <input id="uturnPoints" type="number" min="6" max="96" step="1" value="18"></label>
        </div>
        <div class="dock-actions">
          <button id="uturnToolBtn">U Tool</button>
          <button id="insertUturnBtn" class="primary">Insert</button>
        </div>
        <p class="hint">Select a point or use machine heading, then insert or tap the map.</p>
      </div>
    </section>

    <section id="panel-points" class="panel-view">
      <div class="panel-card">
        <h2>Selected Point</h2>
        <div class="grid2">
          <label>Lat <input id="selectedLat" inputmode="decimal" placeholder="--"></label>
          <label>Lon <input id="selectedLon" inputmode="decimal" placeholder="--"></label>
        </div>
        <div class="dock-actions">
          <button id="applyPointBtn">Apply</button>
          <button id="deletePointBtn" class="danger">Delete</button>
        </div>
        <div class="dock-actions">
          <button id="appendBtn">Append</button>
          <button id="reverseBtn">Reverse</button>
        </div>
        <button id="clearBtn" class="danger">Clear Route</button>
      </div>
      <div class="panel-card">
        <h2>Points</h2>
        <div id="pointList" class="point-list"><div class="hint">No points yet. Click the map to add.</div></div>
      </div>
    </section>

    <section id="panel-json" class="panel-view">
      <div class="panel-card">
        <h2>File</h2>
        <div class="grid2">
          <label>Target
            <select id="target">
              <option value="line">line.geojson</option>
              <option value="uturn">u_field.geojson</option>
            </select>
          </label>
          <label>&nbsp;<button id="saveBtn" class="primary">Save</button></label>
        </div>
        <div class="dock-actions">
          <button id="loadBtn">Load</button>
          <button id="importBtn">Import</button>
        </div>
        <div class="dock-actions">
          <button id="copyBtn">Copy</button>
          <button id="downloadBtn">Download</button>
        </div>
        <button id="formatBtn">Format JSON</button>
        <div id="fileStatus" class="status warn"><strong>File</strong>No changes saved.</div>
      </div>
      <div class="panel-card">
        <h2>Raw JSON</h2>
        <textarea id="geojsonText" spellcheck="false"></textarea>
      </div>
    </section>
  </aside>

  <script src="https://unpkg.com/leaflet@1.9.4/dist/leaflet.js"
    integrity="sha256-20nQCchB9co0qIjJZRGuk2/Z9VM+kNiyxNV1lvTlZBo=" crossorigin=""></script>
  <script>
    const map = L.map("map", { zoomControl: false, preferCanvas: true, maxZoom: 24 }).setView([50.6970, 5.3312], 17);
    const baseMaps = {
      light: L.tileLayer("https://{s}.basemaps.cartocdn.com/light_all/{z}/{x}/{y}{r}.png", {
        subdomains: "abcd", maxZoom: 24, maxNativeZoom: 19, attribution: "&copy; OpenStreetMap &copy; CARTO"
      }),
      satellite: L.tileLayer("https://server.arcgisonline.com/ArcGIS/rest/services/World_Imagery/MapServer/tile/{z}/{y}/{x}", {
        maxZoom: 24, maxNativeZoom: 19, attribution: "Tiles &copy; Esri"
      })
    };
    let activeBaseMap = baseMaps.light.addTo(map);
    map.whenReady(() => { setTimeout(() => map.invalidateSize(true), 50); setTimeout(() => map.invalidateSize(true), 350); });
    window.addEventListener("resize", () => map.invalidateSize(true));

    const route = [];
    const trail = [];
    let selectedIndex = null;
    let currentPosition = null;
    let currentHeading = null;
    let polling = true;
    let pollTimer = null;
    let centeredOnce = false;
    let uturnMode = false;
    const pollIntervalMs = 250;

    const routeCasingLayer = L.polyline([], { color: "#17333b", weight: 8, opacity: 0.88, lineCap: "round", lineJoin: "round" }).addTo(map);
    const routeLayer = L.polyline([], { color: "#e67f4e", weight: 5, opacity: 0.96, lineCap: "round", lineJoin: "round" }).addTo(map);
    const uturnPreviewLayer = L.polyline([], { color: "#8fd2e4", weight: 4, opacity: 0.72, dashArray: "8 7", lineCap: "round", lineJoin: "round" }).addTo(map);
    const trailLayer = L.polyline([], { color: "#8fd2e4", weight: 3, opacity: 0.82 }).addTo(map);
    const pointLayer = L.layerGroup().addTo(map);
    const machineIcon = L.divIcon({ className: "", html: "<div class='machine-marker'><div class='machine-arrow'></div><div class='machine-label'>Machine</div></div>", iconSize: [80, 54], iconAnchor: [40, 18] });
    const machineMarker = L.marker([0, 0], { icon: machineIcon, zIndexOffset: 1000 });

    const el = {
      apiUrl: document.getElementById("apiUrl"),
      livePill: document.getElementById("livePill"),
      pollBtn: document.getElementById("pollBtn"),
      centerBtn: document.getElementById("centerBtn"),
      mapLightBtn: document.getElementById("mapLightBtn"),
      mapSatelliteBtn: document.getElementById("mapSatelliteBtn"),
      fitBtn: document.getElementById("fitBtn"),
      uturnStackBtn: document.getElementById("uturnStackBtn"),
      uturnToolBtn: document.getElementById("uturnToolBtn"),
      insertUturnBtn: document.getElementById("insertUturnBtn"),
      uturnRadius: document.getElementById("uturnRadius"),
      uturnSide: document.getElementById("uturnSide"),
      uturnPoints: document.getElementById("uturnPoints"),
      positionStatus: document.getElementById("positionStatus"),
      addCurrentBtn: document.getElementById("addCurrentBtn"),
      appendBtn: document.getElementById("appendBtn"),
      reverseBtn: document.getElementById("reverseBtn"),
      clearBtn: document.getElementById("clearBtn"),
      selectedLat: document.getElementById("selectedLat"),
      selectedLon: document.getElementById("selectedLon"),
      applyPointBtn: document.getElementById("applyPointBtn"),
      deletePointBtn: document.getElementById("deletePointBtn"),
      pointList: document.getElementById("pointList"),
      pointCount: document.getElementById("pointCount"),
      routeLength: document.getElementById("routeLength"),
      maxGap: document.getElementById("maxGap"),
      selectedPoint: document.getElementById("selectedPoint"),
      target: document.getElementById("target"),
      saveBtn: document.getElementById("saveBtn"),
      loadBtn: document.getElementById("loadBtn"),
      importBtn: document.getElementById("importBtn"),
      copyBtn: document.getElementById("copyBtn"),
      downloadBtn: document.getElementById("downloadBtn"),
      formatBtn: document.getElementById("formatBtn"),
      geojsonText: document.getElementById("geojsonText"),
      fileStatus: document.getElementById("fileStatus"),
    };

    document.querySelectorAll(".panel-toggle").forEach((btn) => {
      btn.addEventListener("click", () => {
        const active = btn.classList.contains("is-active");
        document.querySelectorAll(".panel-toggle").forEach((item) => {
          item.classList.remove("is-active");
          item.setAttribute("aria-pressed", "false");
        });
        document.querySelectorAll(".panel-view").forEach((item) => item.classList.remove("active"));
        if (active && !document.body.classList.contains("panel-closed")) {
          document.body.classList.add("panel-closed");
        } else {
          document.body.classList.remove("panel-closed");
          btn.classList.add("is-active");
          btn.setAttribute("aria-pressed", "true");
          document.getElementById(`panel-${btn.dataset.panel}`).classList.add("active");
        }
        setTimeout(() => map.invalidateSize(true), 50);
      });
    });

    function meters(a, b) {
      const r = 6371008.8;
      const lat1 = a[0] * Math.PI / 180, lat2 = b[0] * Math.PI / 180;
      const dlat = (b[0] - a[0]) * Math.PI / 180, dlon = (b[1] - a[1]) * Math.PI / 180;
      const s = Math.sin(dlat / 2) ** 2 + Math.cos(lat1) * Math.cos(lat2) * Math.sin(dlon / 2) ** 2;
      return 2 * r * Math.atan2(Math.sqrt(s), Math.sqrt(1 - s));
    }
    function fmtMeters(m) { return m >= 1000 ? `${(m / 1000).toFixed(2)} km` : `${m.toFixed(m < 10 ? 1 : 0)} m`; }
    function movementHeading(previous, current) {
      const lat = previous[0] * Math.PI / 180;
      const north = (current[0] - previous[0]) * 111320;
      const east = (current[1] - previous[1]) * 111320 * Math.cos(lat);
      if (Math.hypot(east, north) < 0.05) return null;
      return (Math.atan2(east, north) * 180 / Math.PI + 360) % 360;
    }
    function offsetPoint(origin, eastM, northM) {
      const lat = origin[0] + northM / 111320;
      const lon = origin[1] + eastM / (111320 * Math.cos(origin[0] * Math.PI / 180));
      return [lat, lon];
    }
    function cleanHeading(value) {
      if (value === null || value === undefined || value === "") return null;
      const heading = Number(value);
      if (!Number.isFinite(heading)) return null;
      return ((heading % 360) + 360) % 360;
    }
    function headingFromState(state) {
      const pos = state.position || {};
      return cleanHeading(pos.heading_deg);
    }
    function applyMachineArrow(heading) {
      const marker = machineMarker.getElement();
      const arrow = marker && marker.querySelector(".machine-arrow");
      if (!arrow) return;
      if (heading === null) {
        arrow.classList.add("no-heading");
        return;
      }
      arrow.classList.remove("no-heading");
      arrow.style.setProperty("--heading", `${heading}deg`);
    }
    function updateMachineHeading(headingDeg) {
      const heading = cleanHeading(headingDeg);
      if (heading === null) {
        applyMachineArrow(currentHeading);
        previewUturn();
        return false;
      }
      currentHeading = heading;
      applyMachineArrow(currentHeading);
      previewUturn();
      return true;
    }
    function routeStats() {
      let length = 0, maxGap = 0;
      for (let i = 1; i < route.length; i++) { const d = meters(route[i - 1], route[i]); length += d; maxGap = Math.max(maxGap, d); }
      return { length, maxGap };
    }
    function setStatus(node, label, text, mode) {
      node.className = `status ${mode || "warn"}`;
      node.innerHTML = `<strong>${label}</strong>${text}`;
    }
    function bringGeometryToFront() {
      trailLayer.bringToFront();
      routeCasingLayer.bringToFront();
      routeLayer.bringToFront();
      uturnPreviewLayer.bringToFront();
      pointLayer.eachLayer((layer) => layer.bringToFront && layer.bringToFront());
      if (map.hasLayer(machineMarker)) machineMarker.setZIndexOffset(1000);
    }
    function setBaseMap(name) {
      const next = baseMaps[name];
      if (!next || next === activeBaseMap) return;
      map.removeLayer(activeBaseMap);
      activeBaseMap = next.addTo(map);
      el.mapLightBtn.classList.toggle("active", name === "light");
      el.mapSatelliteBtn.classList.toggle("active", name === "satellite");
      activeBaseMap.once("load", bringGeometryToFront);
      bringGeometryToFront();
    }
    function clampNumber(value, min, max, fallback) {
      const n = Number(value);
      if (!Number.isFinite(n)) return fallback;
      return Math.min(max, Math.max(min, n));
    }
    function headingForUturn(start, explicitIndex) {
      const index = explicitIndex === undefined ? selectedIndex : explicitIndex;
      let heading = null;
      if (index !== null && route[index]) {
        if (index > 0) heading = movementHeading(route[index - 1], route[index]);
        if (heading === null && index < route.length - 1) heading = movementHeading(route[index], route[index + 1]);
      }
      if (heading === null && currentPosition && meters(start, currentPosition) < 0.5) heading = currentHeading;
      if (heading === null && currentHeading !== null) heading = currentHeading;
      if (heading === null && route.length > 1) heading = movementHeading(route[route.length - 2], route[route.length - 1]);
      return heading === null ? 0 : heading;
    }
    function buildUturn(start, headingDeg) {
      const radius = clampNumber(el.uturnRadius.value, 0.5, 500, 6);
      const steps = Math.round(clampNumber(el.uturnPoints.value, 6, 96, 18));
      const side = el.uturnSide.value === "right" ? "right" : "left";
      const h = headingDeg * Math.PI / 180;
      const right = [Math.cos(h), -Math.sin(h)];
      const sideNormal = side === "right" ? right : [-right[0], -right[1]];
      const centerEast = radius * sideNormal[0];
      const centerNorth = radius * sideNormal[1];
      const radialEast = -centerEast;
      const radialNorth = -centerNorth;
      const arc = [];
      for (let i = 0; i <= steps; i++) {
        const theta = Math.PI * i / steps * (side === "right" ? -1 : 1);
        const cosT = Math.cos(theta), sinT = Math.sin(theta);
        const east = centerEast + radialEast * cosT - radialNorth * sinT;
        const north = centerNorth + radialEast * sinT + radialNorth * cosT;
        arc.push(offsetPoint(start, east, north));
      }
      return arc;
    }
    function previewUturn() {
      if (!uturnMode) { uturnPreviewLayer.setLatLngs([]); return; }
      let start = null;
      let index = selectedIndex;
      if (index !== null && route[index]) start = route[index];
      else if (currentPosition) start = currentPosition;
      else if (route.length) { start = route[route.length - 1]; index = route.length - 1; }
      if (!start) { uturnPreviewLayer.setLatLngs([]); return; }
      uturnPreviewLayer.setLatLngs(buildUturn(start, headingForUturn(start, index)));
      bringGeometryToFront();
    }
    function setUturnMode(active) {
      uturnMode = active;
      document.body.classList.toggle("uturn-mode", active);
      el.uturnStackBtn.classList.toggle("is-active", active);
      el.uturnStackBtn.setAttribute("aria-pressed", active ? "true" : "false");
      el.uturnToolBtn.classList.toggle("active", active);
      el.uturnToolBtn.textContent = active ? "Active" : "U Tool";
      previewUturn();
    }
    function geojson() {
      return {
        type: "FeatureCollection",
        features: [{
          type: "Feature",
          properties: { source: "autodrive-route-viewer", created_at: new Date().toISOString() },
          geometry: { type: "LineString", coordinates: route.map(([lat, lon]) => [Number(lon.toFixed(7)), Number(lat.toFixed(7))]) }
        }]
      };
    }
    function parseGeoJSON(value) {
      const obj = typeof value === "string" ? JSON.parse(value) : value;
      let geoms = [];
      if (obj.type === "FeatureCollection") geoms = (obj.features || []).map((f) => f && f.geometry).filter(Boolean);
      else if (obj.type === "Feature") geoms = [obj.geometry];
      else geoms = [obj];
      const line = geoms.find((g) => g && g.type === "LineString");
      if (!line || !Array.isArray(line.coordinates) || line.coordinates.length < 2) throw new Error("Expected LineString with at least two points");
      return line.coordinates.map((c) => {
        const lon = Number(c[0]), lat = Number(c[1]);
        if (!Number.isFinite(lat) || !Number.isFinite(lon) || lat < -90 || lat > 90 || lon < -180 || lon > 180) throw new Error("Invalid coordinate");
        return [lat, lon];
      });
    }
    function selectPoint(index) {
      selectedIndex = index;
      render();
      if (index !== null && route[index]) map.panTo(route[index], { animate: false });
    }
    function addPoint(latlng) {
      const point = [latlng.lat, latlng.lng];
      if (selectedIndex === null) { route.push(point); selectedIndex = route.length - 1; }
      else { route.splice(selectedIndex + 1, 0, point); selectedIndex += 1; }
      render();
    }
    function insertUturn(explicitStart) {
      let start = explicitStart;
      let index = selectedIndex;
      let insertAt = route.length;
      let includeStart = true;
      if (!start && index !== null && route[index]) {
        start = route[index];
        insertAt = index + 1;
        includeStart = false;
      } else if (!start && currentPosition) {
        start = currentPosition;
      } else if (!start && route.length) {
        start = route[route.length - 1];
        index = route.length - 1;
        includeStart = false;
      }
      if (!start) {
        setStatus(el.fileStatus, "File", "No start point for U-turn.", "warn");
        return;
      }
      const arc = buildUturn(start, headingForUturn(start, index));
      if (route.length && includeStart && meters(route[route.length - 1], start) < 0.3) includeStart = false;
      const points = includeStart ? arc : arc.slice(1);
      route.splice(insertAt, 0, ...points);
      selectedIndex = insertAt + points.length - 1;
      render();
      setStatus(el.fileStatus, "File", `U-turn inserted: ${points.length} points, radius ${Number(el.uturnRadius.value).toFixed(1)} m.`, "warn");
    }
    function replaceRoute(points) {
      route.length = 0;
      points.forEach((p) => route.push(p));
      selectedIndex = route.length ? 0 : null;
      render();
      fitAll();
    }
    function fitAll() {
      const pts = [];
      if (currentPosition) pts.push(currentPosition);
      route.forEach((p) => pts.push(p));
      if (pts.length === 0) return;
      map.invalidateSize(true);
      if (pts.length === 1) map.setView(pts[0], Math.max(map.getZoom(), 18), { animate: false });
      else map.fitBounds(pts, { padding: [36, 36], maxZoom: 21, animate: false });
    }
    function render() {
      if (selectedIndex !== null && selectedIndex >= route.length) selectedIndex = route.length ? route.length - 1 : null;
      routeCasingLayer.setLatLngs(route);
      routeLayer.setLatLngs(route);
      bringGeometryToFront();
      pointLayer.clearLayers();
      route.forEach((point, index) => {
        const classes = ["route-handle", index === 0 ? "start" : "", index === selectedIndex ? "selected" : ""].filter(Boolean).join(" ");
        const marker = L.marker(point, {
          draggable: true,
          icon: L.divIcon({ className: "", html: `<div class="${classes}"></div>`, iconSize: [24, 24], iconAnchor: [12, 12] }),
          zIndexOffset: index === selectedIndex ? 800 : 500
        });
        marker.on("click", (ev) => { L.DomEvent.stopPropagation(ev); selectPoint(index); });
        marker.on("drag", (ev) => { const p = ev.target.getLatLng(); route[index] = [p.lat, p.lng]; routeCasingLayer.setLatLngs(route); routeLayer.setLatLngs(route); el.geojsonText.value = JSON.stringify(geojson(), null, 2); });
        marker.on("dragend", (ev) => { const p = ev.target.getLatLng(); route[index] = [p.lat, p.lng]; selectedIndex = index; render(); });
        marker.addTo(pointLayer);
      });
      const st = routeStats();
      el.pointCount.textContent = String(route.length);
      el.routeLength.textContent = fmtMeters(st.length);
      el.maxGap.textContent = fmtMeters(st.maxGap);
      el.selectedPoint.textContent = selectedIndex === null ? "none" : String(selectedIndex);
      el.selectedLat.value = selectedIndex === null ? "" : route[selectedIndex][0].toFixed(7);
      el.selectedLon.value = selectedIndex === null ? "" : route[selectedIndex][1].toFixed(7);
      el.applyPointBtn.disabled = selectedIndex === null;
      el.deletePointBtn.disabled = selectedIndex === null;
      el.saveBtn.disabled = route.length < 2;
      el.copyBtn.disabled = route.length < 2;
      el.downloadBtn.disabled = route.length < 2;
      el.geojsonText.value = JSON.stringify(geojson(), null, 2);
      el.pointList.innerHTML = "";
      if (route.length === 0) {
        el.pointList.innerHTML = "<div class='hint'>No points yet. Click the map to add.</div>";
      } else {
        route.forEach(([lat, lon], index) => {
          const row = document.createElement("button");
          row.className = `point-row${index === selectedIndex ? " selected" : ""}`;
          row.type = "button";
          row.innerHTML = `<span class="badge">${index}</span><span class="meta">${lat.toFixed(7)}, ${lon.toFixed(7)}</span>`;
          row.onclick = () => selectPoint(index);
          el.pointList.appendChild(row);
        });
      }
      previewUturn();
    }
    function setCurrentPosition(lat, lon, headingDeg, detail) {
      const previousPosition = currentPosition;
      currentPosition = [lat, lon];
      trail.push(currentPosition);
      while (trail.length > 160) trail.shift();
      trailLayer.setLatLngs(trail);
      machineMarker.setLatLng(currentPosition);
      if (!map.hasLayer(machineMarker)) machineMarker.addTo(map);
      if (!updateMachineHeading(headingDeg) && previousPosition) {
        updateMachineHeading(movementHeading(previousPosition, currentPosition));
      }
      bringGeometryToFront();
      const headingDetail = headingDeg === null && currentHeading !== null
        ? detail.replace("hdg=-", `hdg=${currentHeading.toFixed(1)} deg tracked`)
        : detail;
      setStatus(el.positionStatus, "Position", `${lat.toFixed(7)}, ${lon.toFixed(7)}<br>${headingDetail}`, "ok");
      if (!centeredOnce) { centeredOnce = true; map.invalidateSize(true); map.setView(currentPosition, Math.max(map.getZoom(), 18), { animate: false }); }
    }
    async function pollOnce(center) {
      const res = await fetch(`/state?api=${encodeURIComponent(el.apiUrl.value)}`, { cache: "no-store" });
      if (!res.ok) throw new Error(`HTTP ${res.status}`);
      const state = await res.json();
      const pos = state.position || {};
      if (pos.lat === null || pos.lon === null || pos.lat === undefined || pos.lon === undefined) {
        updateMachineHeading(headingFromState(state));
        setStatus(el.positionStatus, "Position", `API online=${state.online ? "Y" : "-"}, no position.`, "warn");
        return;
      }
      const heading = headingFromState(state);
      const headingText = heading === null ? "hdg=-" : `hdg=${heading.toFixed(1)} deg`;
      setCurrentPosition(Number(pos.lat), Number(pos.lon), heading, `online=${state.online ? "Y" : "-"} ${headingText} speed=${Number(pos.speed_kph || 0).toFixed(1)} kph`);
      if (center) map.setView(currentPosition, Math.max(map.getZoom(), 18), { animate: false });
    }
    async function pollLoop() {
      if (!polling) return;
      try { await pollOnce(false); el.livePill.textContent = "LIVE"; }
      catch (err) { setStatus(el.positionStatus, "Position", `API read failed: ${err.message}`, "bad"); el.livePill.textContent = "OFF"; }
      pollTimer = setTimeout(pollLoop, pollIntervalMs);
    }

    map.on("click", (ev) => {
      if (uturnMode) {
        insertUturn([ev.latlng.lat, ev.latlng.lng]);
        return;
      }
      addPoint(ev.latlng);
    });
    el.pollBtn.onclick = async () => {
      polling = !polling;
      el.pollBtn.textContent = polling ? "Pause" : "Resume";
      el.pollBtn.classList.toggle("primary", polling);
      if (pollTimer) clearTimeout(pollTimer);
      if (polling) { await pollOnce(true).catch((err) => setStatus(el.positionStatus, "Position", err.message, "bad")); pollLoop(); }
    };
    el.mapLightBtn.onclick = () => setBaseMap("light");
    el.mapSatelliteBtn.onclick = () => setBaseMap("satellite");
    el.centerBtn.onclick = () => currentPosition && map.setView(currentPosition, Math.max(map.getZoom(), 18), { animate: false });
    el.fitBtn.onclick = fitAll;
    el.uturnStackBtn.onclick = () => setUturnMode(!uturnMode);
    el.uturnToolBtn.onclick = () => setUturnMode(!uturnMode);
    el.insertUturnBtn.onclick = () => insertUturn();
    el.uturnRadius.oninput = previewUturn;
    el.uturnSide.onchange = previewUturn;
    el.uturnPoints.oninput = previewUturn;
    el.addCurrentBtn.onclick = () => currentPosition ? addPoint({ lat: currentPosition[0], lng: currentPosition[1] }) : setStatus(el.positionStatus, "Position", "No current position.", "warn");
    el.appendBtn.onclick = () => { selectedIndex = null; render(); };
    el.reverseBtn.onclick = () => { if (route.length < 2) return; const old = selectedIndex; route.reverse(); selectedIndex = old === null ? null : route.length - 1 - old; render(); setStatus(el.fileStatus, "File", "Route reversed. Save to write it.", "warn"); };
    el.clearBtn.onclick = () => {
      if ((route.length || trail.length) && !confirm("Clear route and machine path?")) return;
      route.length = 0;
      trail.length = 0;
      selectedIndex = null;
      trailLayer.setLatLngs(trail);
      render();
    };
    el.applyPointBtn.onclick = () => {
      if (selectedIndex === null) return;
      const lat = Number(el.selectedLat.value), lon = Number(el.selectedLon.value);
      if (!Number.isFinite(lat) || !Number.isFinite(lon) || lat < -90 || lat > 90 || lon < -180 || lon > 180) { setStatus(el.fileStatus, "File", "Invalid selected point coordinates.", "bad"); return; }
      route[selectedIndex] = [lat, lon]; render(); map.panTo(route[selectedIndex], { animate: false });
    };
    el.deletePointBtn.onclick = () => {
      if (selectedIndex === null) return;
      route.splice(selectedIndex, 1);
      selectedIndex = route.length ? Math.min(selectedIndex, route.length - 1) : null;
      render();
    };
    el.loadBtn.onclick = async () => {
      const res = await fetch(`/route?target=${encodeURIComponent(el.target.value)}`, { cache: "no-store" });
      const body = await res.json();
      if (!res.ok) { setStatus(el.fileStatus, "File", body.error || "Load failed.", "bad"); return; }
      try { const pts = parseGeoJSON(body.geojson); replaceRoute(pts); setStatus(el.fileStatus, "File", `Loaded ${body.path}<br>${pts.length} points.`, "ok"); }
      catch (err) { setStatus(el.fileStatus, "File", `Load failed: ${err.message}`, "bad"); }
    };
    el.importBtn.onclick = () => {
      try { const pts = parseGeoJSON(el.geojsonText.value); replaceRoute(pts); setStatus(el.fileStatus, "File", `Imported ${pts.length} points from JSON.`, "ok"); }
      catch (err) { setStatus(el.fileStatus, "File", `Import failed: ${err.message}`, "bad"); }
    };
    el.saveBtn.onclick = async () => {
      const res = await fetch("/save", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ target: el.target.value, geojson: geojson() }) });
      const body = await res.json();
      if (!res.ok) { setStatus(el.fileStatus, "File", body.error || "Save failed.", "bad"); return; }
      setStatus(el.fileStatus, "File", `Saved ${body.path}<br>${body.points} points.`, "ok");
    };
    el.copyBtn.onclick = async () => { await navigator.clipboard.writeText(el.geojsonText.value); setStatus(el.fileStatus, "File", "GeoJSON copied.", "ok"); };
    el.downloadBtn.onclick = () => {
      const blob = new Blob([JSON.stringify(geojson(), null, 2) + "\\n"], { type: "application/geo+json" });
      const link = document.createElement("a"); link.href = URL.createObjectURL(blob); link.download = `${el.target.value}.geojson`; link.click(); URL.revokeObjectURL(link.href);
    };
    el.formatBtn.onclick = () => {
      try { el.geojsonText.value = JSON.stringify(JSON.parse(el.geojsonText.value), null, 2); setStatus(el.fileStatus, "File", "JSON formatted.", "ok"); }
      catch (err) { setStatus(el.fileStatus, "File", `Format failed: ${err.message}`, "bad"); }
    };

    render();
    pollOnce(true).catch((err) => setStatus(el.positionStatus, "Position", `API read failed: ${err.message}`, "bad")).finally(() => pollLoop());
  </script>
</body>
</html>
"""


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Serve the AutoDrive route editor web UI.")
    parser.add_argument("--host", default=DEFAULT_HOST, help=f"bind host (default: {DEFAULT_HOST})")
    parser.add_argument("--port", default=DEFAULT_PORT, type=int, help=f"HTTP port (default: {DEFAULT_PORT})")
    parser.add_argument("--api", default=DEFAULT_API, help=f"AutoDrive API state URL (default: {DEFAULT_API})")
    parser.add_argument("--api-timeout", default=2.0, type=float, help="remote API request timeout in seconds")
    parser.add_argument("--output", default=DEFAULT_OUTPUT, help=f"default route file in this repo (default: {DEFAULT_OUTPUT})")
    return parser.parse_args()


def fetch_state(url: str, timeout_s: float) -> dict[str, object]:
    parsed = urlparse(url)
    if parsed.scheme not in {"http", "https"}:
        raise ValueError("API URL must start with http:// or https://")
    with urlopen(url, timeout=timeout_s) as response:
        return json.loads(response.read().decode("utf-8"))


def feature_collection(coords: list[list[float]]) -> dict[str, object]:
    return {
        "type": "FeatureCollection",
        "features": [{
            "type": "Feature",
            "properties": {
                "source": "autodrive-route-viewer",
                "saved_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            },
            "geometry": {"type": "LineString", "coordinates": coords},
        }],
    }


def extract_linestring(geojson: object) -> list[list[float]]:
    if not isinstance(geojson, dict):
        raise ValueError("GeoJSON body must be an object")
    geometries: list[dict[str, object]] = []
    if geojson.get("type") == "FeatureCollection":
        for feature in geojson.get("features", []):
            if isinstance(feature, dict) and isinstance(feature.get("geometry"), dict):
                geometries.append(feature["geometry"])
    elif geojson.get("type") == "Feature" and isinstance(geojson.get("geometry"), dict):
        geometries.append(geojson["geometry"])
    else:
        geometries.append(geojson)

    for geom in geometries:
        if geom.get("type") != "LineString":
            continue
        coords = geom.get("coordinates")
        if not isinstance(coords, list) or len(coords) < 2:
            raise ValueError("LineString must contain at least two coordinates")
        clean: list[list[float]] = []
        for coord in coords:
            if not isinstance(coord, list | tuple) or len(coord) < 2:
                raise ValueError("Each coordinate must be [lon, lat]")
            lon = float(coord[0])
            lat = float(coord[1])
            if not (-180 <= lon <= 180 and -90 <= lat <= 90):
                raise ValueError("Coordinate outside valid lon/lat range")
            clean.append([round(lon, 7), round(lat, 7)])
        return clean
    raise ValueError("Expected a GeoJSON LineString")


def target_path(target: str, default_output: str) -> Path:
    filename = SAVE_TARGETS.get(target, default_output)
    path = (HERE / filename).resolve()
    if path.parent != HERE:
        raise ValueError("Save target must stay inside the repository directory")
    return path


def make_handler(default_output: str, upstream_api: str, api_timeout_s: float):
    class RouteEditorHandler(BaseHTTPRequestHandler):
        server_version = "AutoDriveRouteEditor/1.0"

        def log_message(self, format: str, *args: object) -> None:
            print(f"{self.address_string()} - {format % args}", file=sys.stderr)

        def do_GET(self) -> None:
            parsed = urlparse(self.path)
            path = parsed.path
            if path in {"/", "/index.html"}:
                html = HTML.replace("__API_URL__", upstream_api)
                self._send_bytes(html.encode("utf-8"), "text/html; charset=utf-8")
            elif path == "/state":
                try:
                    params = parse_qs(parsed.query)
                    api = params.get("api", [upstream_api])[0] or upstream_api
                    state = fetch_state(api, api_timeout_s)
                except (OSError, URLError, TimeoutError, ValueError, json.JSONDecodeError) as exc:
                    self._send_json({"error": f"API read failed: {exc}"}, HTTPStatus.BAD_GATEWAY)
                    return
                self._send_json(state)
            elif path == "/route":
                try:
                    params = parse_qs(parsed.query)
                    target = params.get("target", ["line"])[0] or "line"
                    path_in = target_path(target, default_output)
                    geojson = json.loads(path_in.read_text())
                except Exception as exc:
                    self._send_json({"error": str(exc)}, HTTPStatus.BAD_REQUEST)
                    return
                self._send_json({"ok": True, "path": str(path_in), "geojson": geojson})
            elif path == "/health":
                self._send_json({"ok": True})
            else:
                self._send_json({"error": "not found"}, HTTPStatus.NOT_FOUND)

        def do_POST(self) -> None:
            if urlparse(self.path).path != "/save":
                self._send_json({"error": "not found"}, HTTPStatus.NOT_FOUND)
                return
            try:
                length = int(self.headers.get("Content-Length", "0"))
                if length <= 0 or length > 1_000_000:
                    raise ValueError("Request body must be 1..1000000 bytes")
                payload = json.loads(self.rfile.read(length).decode("utf-8"))
                if not isinstance(payload, dict):
                    raise ValueError("Request body must be an object")
                target = str(payload.get("target") or "line")
                coords = extract_linestring(payload.get("geojson"))
                path_out = target_path(target, default_output)
                path_out.write_text(json.dumps(feature_collection(coords), indent=2) + "\n")
            except Exception as exc:
                self._send_json({"error": str(exc)}, HTTPStatus.BAD_REQUEST)
                return
            self._send_json({"ok": True, "path": str(path_out), "points": len(coords)})

        def do_OPTIONS(self) -> None:
            self.send_response(HTTPStatus.NO_CONTENT)
            self._send_common_headers()
            self.end_headers()

        def _send_json(self, body: object, status: HTTPStatus = HTTPStatus.OK) -> None:
            self._send_bytes(
                json.dumps(body, indent=2, sort_keys=True).encode("utf-8"),
                "application/json; charset=utf-8",
                status,
            )

        def _send_bytes(self, data: bytes, content_type: str,
                        status: HTTPStatus = HTTPStatus.OK) -> None:
            try:
                self.send_response(status)
                self._send_common_headers()
                self.send_header("Content-Type", content_type)
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)
            except BrokenPipeError:
                pass

        def _send_common_headers(self) -> None:
            self.send_header("Cache-Control", "no-store")
            self.send_header("Access-Control-Allow-Origin", "*")
            self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
            self.send_header("Access-Control-Allow-Headers", "Content-Type")

    return RouteEditorHandler


def main() -> None:
    args = parse_args()
    if args.api_timeout <= 0:
        raise SystemExit("--api-timeout must be greater than zero")
    server = ThreadingHTTPServer(
        (args.host, args.port),
        make_handler(args.output, args.api, args.api_timeout),
    )
    print(f"AutoDrive route editor listening on http://{args.host}:{args.port}", file=sys.stderr)
    print(f"Reading position from {args.api}", file=sys.stderr)
    print("Save target 'line' writes line.geojson for:", file=sys.stderr)
    print("  ./08_stream_waypoints.py --route line", file=sys.stderr)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nstopping", file=sys.stderr)
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
