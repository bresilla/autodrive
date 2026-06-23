#!/usr/bin/env python3
"""
Browser route editor for AutoDrive GeoJSON routes.

It serves a local Leaflet map, proxies a remote api_server.py state endpoint for
the current machine position, and saves a GeoJSON LineString that
08_stream_waypoints.py can load:

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
  <title>AutoDrive Route Viewer</title>
  <link rel="stylesheet" href="https://unpkg.com/leaflet@1.9.4/dist/leaflet.css"
    integrity="sha256-p4NxAoJBhIINfQxG1pM9g8tI7kLXeIkJFZ7aUXk6nF8=" crossorigin="">
  <style>
    .leaflet-pane,
    .leaflet-tile,
    .leaflet-marker-icon,
    .leaflet-marker-shadow,
    .leaflet-tile-container,
    .leaflet-pane > svg,
    .leaflet-pane > canvas,
    .leaflet-zoom-box,
    .leaflet-image-layer,
    .leaflet-layer {
      position: absolute;
      left: 0;
      top: 0;
    }
    .leaflet-container {
      overflow: hidden;
      -webkit-tap-highlight-color: transparent;
    }
    .leaflet-tile,
    .leaflet-marker-icon,
    .leaflet-marker-shadow {
      user-select: none;
      -webkit-user-drag: none;
    }
    .leaflet-tile {
      filter: inherit;
      visibility: hidden;
    }
    .leaflet-tile-loaded {
      visibility: inherit;
    }
    .leaflet-zoom-animated {
      transform-origin: 0 0;
    }
    .leaflet-interactive {
      cursor: pointer;
    }
    .leaflet-control {
      position: relative;
      z-index: 800;
      pointer-events: visiblePainted;
      pointer-events: auto;
    }
    .leaflet-top,
    .leaflet-bottom {
      position: absolute;
      z-index: 1000;
      pointer-events: none;
    }
    .leaflet-top { top: 0; }
    .leaflet-right { right: 0; }
    .leaflet-bottom { bottom: 0; }
    .leaflet-left { left: 0; }
    .leaflet-control {
      float: left;
      clear: both;
    }
    .leaflet-right .leaflet-control {
      float: right;
    }
    .leaflet-top .leaflet-control {
      margin-top: 10px;
    }
    .leaflet-bottom .leaflet-control {
      margin-bottom: 10px;
    }
    .leaflet-left .leaflet-control {
      margin-left: 10px;
    }
    .leaflet-right .leaflet-control {
      margin-right: 10px;
    }
    .leaflet-control-zoom a {
      display: block;
      width: 30px;
      height: 30px;
      line-height: 30px;
      text-align: center;
      text-decoration: none;
      background: #fff;
      color: #111827;
      border-bottom: 1px solid #ccc;
    }
    .leaflet-control-attribution {
      background: rgba(255, 255, 255, 0.85);
      padding: 0 5px;
      font-size: 11px;
    }
    .leaflet-control-layers {
      background: #fff;
      padding: 7px;
    }
    .leaflet-pane { z-index: 400; }
    .leaflet-tile-pane { z-index: 200; }
    .leaflet-overlay-pane { z-index: 400; }
    .leaflet-shadow-pane { z-index: 500; }
    .leaflet-marker-pane { z-index: 600; }
    .leaflet-tooltip-pane { z-index: 650; }
    .leaflet-popup-pane { z-index: 700; }
    :root {
      color-scheme: light;
      font-family: Inter, ui-sans-serif, system-ui, -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
      --bg: #f7f7f4;
      --panel: #ffffff;
      --text: #1d2428;
      --muted: #667176;
      --line: #d7ddd9;
      --accent: #0b6e4f;
      --accent-strong: #07543d;
      --warn: #9a4b13;
      --bad: #9d1c21;
    }
    * { box-sizing: border-box; }
    body {
      margin: 0;
      min-height: 100vh;
      background: var(--bg);
      color: var(--text);
      overflow: hidden;
    }
    .app {
      display: grid;
      grid-template-columns: minmax(300px, 360px) minmax(0, 1fr);
      min-height: 100vh;
    }
    aside {
      background: var(--panel);
      border-right: 1px solid var(--line);
      padding: 16px;
      display: flex;
      flex-direction: column;
      gap: 14px;
      overflow: auto;
    }
    h1 {
      font-size: 20px;
      line-height: 1.15;
      margin: 0;
      letter-spacing: 0;
    }
    h2 {
      font-size: 13px;
      line-height: 1.2;
      margin: 0 0 8px;
      color: var(--muted);
      text-transform: uppercase;
      letter-spacing: 0;
    }
    .group {
      border-top: 1px solid var(--line);
      padding-top: 14px;
    }
    .row {
      display: flex;
      gap: 8px;
      align-items: center;
      margin: 8px 0;
    }
    .row.wrap { flex-wrap: wrap; }
    label {
      display: grid;
      gap: 5px;
      font-size: 12px;
      color: var(--muted);
      width: 100%;
    }
    input, select, textarea, button {
      font: inherit;
      border-radius: 6px;
    }
    input, select, textarea {
      border: 1px solid var(--line);
      padding: 8px 9px;
      background: #fff;
      color: var(--text);
      min-width: 0;
    }
    textarea {
      min-height: 150px;
      resize: vertical;
      font-family: ui-monospace, SFMono-Regular, Menlo, Consolas, monospace;
      font-size: 12px;
      line-height: 1.35;
    }
    button {
      border: 1px solid var(--line);
      background: #fff;
      color: var(--text);
      padding: 8px 10px;
      cursor: pointer;
      min-height: 36px;
    }
    button.primary {
      background: var(--accent);
      border-color: var(--accent);
      color: #fff;
    }
    button.primary:hover { background: var(--accent-strong); }
    button:disabled {
      opacity: 0.45;
      cursor: not-allowed;
    }
    .status {
      border: 1px solid var(--line);
      border-radius: 6px;
      padding: 10px;
      background: #fbfcfb;
      font-size: 13px;
      line-height: 1.45;
    }
    .status strong {
      display: block;
      font-size: 12px;
      color: var(--muted);
      font-weight: 600;
    }
    .status.bad { border-color: #e7b8ba; color: var(--bad); }
    .status.warn { border-color: #efc391; color: var(--warn); }
    .stats {
      display: grid;
      grid-template-columns: 1fr 1fr;
      gap: 8px;
    }
    .stat {
      border: 1px solid var(--line);
      border-radius: 6px;
      padding: 9px;
      background: #fbfcfb;
    }
    .stat span {
      display: block;
      color: var(--muted);
      font-size: 12px;
      margin-bottom: 3px;
    }
    .stat b {
      font-size: 15px;
      font-weight: 650;
    }
    #map {
      width: 100%;
      height: 100vh;
      min-height: 480px;
    }
    .leaflet-container {
      font: inherit;
      background: #d9e2df;
    }
    .machine-marker {
      width: 34px;
      height: 34px;
      border-radius: 50%;
      border: 4px solid #ffffff;
      background: #e11d48;
      box-shadow: 0 0 0 3px rgba(225, 29, 72, 0.35), 0 8px 24px rgba(0, 0, 0, 0.35);
      position: relative;
    }
    .machine-marker::after {
      content: "MACHINE";
      position: absolute;
      left: 40px;
      top: 4px;
      padding: 3px 6px;
      border-radius: 4px;
      background: rgba(255, 255, 255, 0.94);
      color: #111827;
      font-size: 12px;
      font-weight: 800;
      white-space: nowrap;
      box-shadow: 0 1px 5px rgba(0, 0, 0, 0.25);
    }
    .route-handle {
      width: 24px;
      height: 24px;
      border-radius: 50%;
      border: 3px solid #111827;
      background: #ffffff;
      color: #111827;
      display: grid;
      place-items: center;
      font-size: 11px;
      font-weight: 800;
      box-shadow: 0 2px 8px rgba(0, 0, 0, 0.35);
    }
    .route-handle.start {
      background: #ff2f00;
      color: #ffffff;
    }
    .route-handle.selected {
      border-color: #2563eb;
      box-shadow: 0 0 0 4px rgba(37, 99, 235, 0.35), 0 2px 8px rgba(0, 0, 0, 0.35);
    }
    .leaflet-control-layers {
      border-radius: 6px;
      border: 1px solid rgba(0, 0, 0, 0.18);
      box-shadow: 0 2px 12px rgba(0, 0, 0, 0.18);
    }
    @media (max-width: 820px) {
      body { overflow: auto; }
      .app { grid-template-columns: 1fr; }
      aside {
        order: 2;
        max-height: none;
        border-right: 0;
        border-top: 1px solid var(--line);
      }
      #map {
        height: 56vh;
        min-height: 360px;
      }
    }
  </style>
</head>
<body>
  <main class="app">
    <aside>
      <header>
        <h1>AutoDrive Route Viewer</h1>
      </header>

      <section class="group">
        <h2>Live Position</h2>
        <label>AutoDrive API URL
          <input id="apiUrl" value="__API_URL__">
        </label>
        <div class="row wrap">
          <button id="pollBtn" class="primary">Stop Polling</button>
          <button id="browserGpsBtn">Browser GPS</button>
          <button id="centerBtn">Center</button>
        </div>
        <div id="positionStatus" class="status warn">
          <strong>Status</strong>
          Reading position...
        </div>
      </section>

      <section class="group">
        <h2>Route</h2>
        <div class="row wrap">
          <button id="addPositionBtn" class="primary">Add Position</button>
          <button id="appendModeBtn">Append End</button>
          <button id="deletePointBtn">Delete Point</button>
          <button id="undoBtn">Undo</button>
          <button id="clearBtn">Clear</button>
        </div>
        <div class="stats">
          <div class="stat"><span>Points</span><b id="pointCount">0</b></div>
          <div class="stat"><span>Length</span><b id="routeLength">0 m</b></div>
          <div class="stat"><span>Max Gap</span><b id="maxGap">0 m</b></div>
          <div class="stat"><span>Selected</span><b id="selectedPoint">none</b></div>
        </div>
      </section>

      <section class="group">
        <h2>Save GeoJSON</h2>
        <label>Save target
          <select id="saveTarget">
            <option value="line">line.geojson</option>
            <option value="uturn">u_field.geojson</option>
          </select>
        </label>
        <div class="row wrap">
          <button id="saveBtn" class="primary">Save</button>
          <button id="downloadBtn">Download</button>
          <button id="copyBtn">Copy</button>
        </div>
        <textarea id="geojsonText" spellcheck="false"></textarea>
      </section>
    </aside>
    <section id="map" aria-label="Route map"></section>
  </main>

  <script src="https://unpkg.com/leaflet@1.9.4/dist/leaflet.js"
    integrity="sha256-20nQCchB9co0qIjJZRGuk2/Z9VM+kNiyxNV1lvTlZBo=" crossorigin=""></script>
  <script>
    const map = L.map("map", { zoomControl: true, preferCanvas: true, maxZoom: 24 }).setView([50.6970, 5.3312], 17);
    const gridLayer = L.gridLayer({ attribution: "No tile background" });
    gridLayer.createTile = function(coords) {
      const tile = document.createElement("canvas");
      tile.width = 256;
      tile.height = 256;
      const ctx = tile.getContext("2d");
      ctx.fillStyle = "#eef2ef";
      ctx.fillRect(0, 0, 256, 256);
      ctx.strokeStyle = "#cbd5cf";
      ctx.lineWidth = 1;
      ctx.strokeRect(0.5, 0.5, 255, 255);
      ctx.fillStyle = "#6b756f";
      ctx.font = "12px sans-serif";
      ctx.fillText(`${coords.z}/${coords.x}/${coords.y}`, 10, 22);
      return tile;
    };
    const baseLayers = {
      "Stable Map": L.tileLayer(
        "https://{s}.basemaps.cartocdn.com/light_all/{z}/{x}/{y}{r}.png",
        {
          subdomains: "abcd",
          maxZoom: 24,
          maxNativeZoom: 19,
          attribution: "&copy; OpenStreetMap contributors &copy; CARTO"
        }
      ),
      "OpenStreetMap": L.tileLayer(
        "https://{s}.tile.openstreetmap.org/{z}/{x}/{y}.png",
        {
          maxZoom: 24,
          maxNativeZoom: 19,
          attribution: "&copy; OpenStreetMap contributors"
        }
      ),
      "Satellite": L.tileLayer(
        "https://server.arcgisonline.com/ArcGIS/rest/services/World_Imagery/MapServer/tile/{z}/{y}/{x}",
        {
          maxZoom: 24,
          maxNativeZoom: 18,
          attribution: "Tiles &copy; Esri"
        }
      ),
      "No Tiles": gridLayer
    };
    baseLayers["Stable Map"].addTo(map);
    L.control.layers(baseLayers, null, { position: "topright", collapsed: false }).addTo(map);
    map.whenReady(() => {
      setTimeout(() => map.invalidateSize(true), 50);
      setTimeout(() => map.invalidateSize(true), 350);
    });
    window.addEventListener("resize", () => map.invalidateSize(true));

    const route = [];
    let currentPosition = null;
    let polling = true;
    let pollTimer = null;
    let watchId = null;
    let hasCenteredOnMachine = false;
    let selectedIndex = null;

    const routeLayer = L.polyline([], { color: "#ff2f00", weight: 7, opacity: 0.95 }).addTo(map);
    const trailLayer = L.polyline([], { color: "#2563eb", weight: 4, opacity: 0.85 }).addTo(map);
    const pointLayer = L.layerGroup().addTo(map);
    const machineIcon = L.divIcon({
      className: "",
      html: "<div class='machine-marker'></div>",
      iconSize: [34, 34],
      iconAnchor: [17, 17]
    });
    const currentMarker = L.marker([0, 0], { icon: machineIcon, zIndexOffset: 1000 });
    const positionTrail = [];

    const els = {
      apiUrl: document.getElementById("apiUrl"),
      pollBtn: document.getElementById("pollBtn"),
      browserGpsBtn: document.getElementById("browserGpsBtn"),
      centerBtn: document.getElementById("centerBtn"),
      addPositionBtn: document.getElementById("addPositionBtn"),
      appendModeBtn: document.getElementById("appendModeBtn"),
      deletePointBtn: document.getElementById("deletePointBtn"),
      undoBtn: document.getElementById("undoBtn"),
      clearBtn: document.getElementById("clearBtn"),
      saveBtn: document.getElementById("saveBtn"),
      downloadBtn: document.getElementById("downloadBtn"),
      copyBtn: document.getElementById("copyBtn"),
      saveTarget: document.getElementById("saveTarget"),
      positionStatus: document.getElementById("positionStatus"),
      pointCount: document.getElementById("pointCount"),
      routeLength: document.getElementById("routeLength"),
      maxGap: document.getElementById("maxGap"),
      selectedPoint: document.getElementById("selectedPoint"),
      geojsonText: document.getElementById("geojsonText")
    };

    function meters(a, b) {
      const r = 6371008.8;
      const lat1 = a[0] * Math.PI / 180;
      const lat2 = b[0] * Math.PI / 180;
      const dlat = (b[0] - a[0]) * Math.PI / 180;
      const dlon = (b[1] - a[1]) * Math.PI / 180;
      const s = Math.sin(dlat / 2) ** 2 + Math.cos(lat1) * Math.cos(lat2) * Math.sin(dlon / 2) ** 2;
      return 2 * r * Math.atan2(Math.sqrt(s), Math.sqrt(1 - s));
    }

    function formatDistance(m) {
      if (!Number.isFinite(m)) return "0 m";
      if (m >= 1000) return `${(m / 1000).toFixed(2)} km`;
      return `${m.toFixed(m < 10 ? 1 : 0)} m`;
    }

    function routeStats() {
      let length = 0;
      let maxGap = 0;
      for (let i = 1; i < route.length; i += 1) {
        const gap = meters(route[i - 1], route[i]);
        length += gap;
        maxGap = Math.max(maxGap, gap);
      }
      return { length, maxGap };
    }

    function makeGeoJSON() {
      return {
        type: "FeatureCollection",
        features: [
          {
            type: "Feature",
            properties: {
              source: "autodrive-route-viewer",
              created_at: new Date().toISOString()
            },
            geometry: {
              type: "LineString",
              coordinates: route.map(([lat, lon]) => [
                Number(lon.toFixed(7)),
                Number(lat.toFixed(7))
              ])
            }
          }
        ]
      };
    }

    function renderRoute() {
      routeLayer.setLatLngs(route);
      routeLayer.bringToFront();
      pointLayer.clearLayers();
      route.forEach((latlng, index) => {
        const classNames = [
          "route-handle",
          index === 0 ? "start" : "",
          index === selectedIndex ? "selected" : ""
        ].filter(Boolean).join(" ");
        const marker = L.marker(latlng, {
          draggable: true,
          icon: L.divIcon({
            className: "",
            html: `<div class="${classNames}">${index}</div>`,
            iconSize: [24, 24],
            iconAnchor: [12, 12]
          }),
          zIndexOffset: index === selectedIndex ? 800 : 500
        });
        marker.on("click", (event) => {
          L.DomEvent.stopPropagation(event);
          selectedIndex = index;
          renderRoute();
        });
        marker.on("drag", (event) => {
          const p = event.target.getLatLng();
          route[index] = [p.lat, p.lng];
          routeLayer.setLatLngs(route);
          els.geojsonText.value = JSON.stringify(makeGeoJSON(), null, 2);
        });
        marker.on("dragend", (event) => {
          const p = event.target.getLatLng();
          route[index] = [p.lat, p.lng];
          selectedIndex = index;
          renderRoute();
        });
        marker.bindTooltip(`Point ${index}`, { permanent: false });
        marker.addTo(pointLayer);
      });

      const stats = routeStats();
      els.pointCount.textContent = String(route.length);
      els.routeLength.textContent = formatDistance(stats.length);
      els.maxGap.textContent = formatDistance(stats.maxGap);
      if (selectedIndex !== null && selectedIndex >= route.length) {
        selectedIndex = route.length ? route.length - 1 : null;
      }
      els.selectedPoint.textContent = selectedIndex === null ? "none" : String(selectedIndex);
      els.geojsonText.value = JSON.stringify(makeGeoJSON(), null, 2);
      els.saveBtn.disabled = route.length < 2;
      els.downloadBtn.disabled = route.length < 2;
      els.copyBtn.disabled = route.length < 2;
      els.deletePointBtn.disabled = selectedIndex === null;
    }

    function setStatus(text, mode = "warn") {
      els.positionStatus.className = `status ${mode}`;
      els.positionStatus.innerHTML = `<strong>Status</strong>${text}`;
    }

    function setPosition(lat, lon, details) {
      if (!Number.isFinite(lat) || !Number.isFinite(lon)) return;
      currentPosition = [lat, lon];
      positionTrail.push(currentPosition);
      while (positionTrail.length > 120) positionTrail.shift();
      trailLayer.setLatLngs(positionTrail);
      currentMarker.setLatLng(currentPosition);
      if (!map.hasLayer(currentMarker)) currentMarker.addTo(map);
      currentMarker.setZIndexOffset(1000);
      trailLayer.bringToFront();
      routeLayer.bringToFront();
      setStatus(`${lat.toFixed(7)}, ${lon.toFixed(7)}<br>${details || ""}`, "ok");
      if (!hasCenteredOnMachine) {
        hasCenteredOnMachine = true;
        map.invalidateSize(true);
        map.setView(currentPosition, Math.max(map.getZoom(), 18), { animate: false });
      }
    }

    async function pollApiOnce(center = false) {
      const response = await fetch(`/state?api=${encodeURIComponent(els.apiUrl.value)}`, { cache: "no-store" });
      if (!response.ok) throw new Error(`HTTP ${response.status}`);
      const state = await response.json();
      const pos = state.position || {};
      if (pos.lat === null || pos.lon === null || pos.lat === undefined || pos.lon === undefined) {
        setStatus(`API online=${state.online ? "Y" : "-"}, but no position.`, "warn");
        return;
      }
      const detail = `online=${state.online ? "Y" : "-"} speed=${Number(pos.speed_kph || 0).toFixed(1)} kph`;
      setPosition(Number(pos.lat), Number(pos.lon), detail);
      if (center) map.setView(currentPosition, Math.max(map.getZoom(), 17));
    }

    async function pollLoop() {
      if (!polling) return;
      try {
        await pollApiOnce(false);
      } catch (error) {
        setStatus(`API read failed: ${error.message}`, "bad");
      }
      pollTimer = setTimeout(pollLoop, 1000);
    }

    function addRoutePoint(latlng) {
      const point = [latlng.lat, latlng.lng];
      if (selectedIndex === null) {
        route.push(point);
        selectedIndex = route.length - 1;
      } else {
        route.splice(selectedIndex + 1, 0, point);
        selectedIndex += 1;
      }
      renderRoute();
    }

    map.on("click", (event) => addRoutePoint(event.latlng));

    els.pollBtn.addEventListener("click", async () => {
      polling = !polling;
      els.pollBtn.textContent = polling ? "Stop Polling" : "Start Polling";
      if (pollTimer) clearTimeout(pollTimer);
      if (polling) {
        try {
          await pollApiOnce(true);
        } catch (error) {
          setStatus(`API read failed: ${error.message}`, "bad");
        }
        pollLoop();
      }
    });

    els.browserGpsBtn.addEventListener("click", () => {
      if (!navigator.geolocation) {
        setStatus("Browser geolocation is not available.", "bad");
        return;
      }
      if (watchId !== null) {
        navigator.geolocation.clearWatch(watchId);
        watchId = null;
        els.browserGpsBtn.textContent = "Browser GPS";
        return;
      }
      watchId = navigator.geolocation.watchPosition(
        (pos) => {
          setPosition(pos.coords.latitude, pos.coords.longitude, `browser accuracy=${formatDistance(pos.coords.accuracy)}`);
          map.setView(currentPosition, Math.max(map.getZoom(), 18));
        },
        (error) => setStatus(`Browser GPS failed: ${error.message}`, "bad"),
        { enableHighAccuracy: true, maximumAge: 1000, timeout: 10000 }
      );
      els.browserGpsBtn.textContent = "Stop GPS";
    });

    els.centerBtn.addEventListener("click", () => {
      if (currentPosition) map.setView(currentPosition, Math.max(map.getZoom(), 18));
    });

    els.addPositionBtn.addEventListener("click", () => {
      if (!currentPosition) {
        setStatus("No current position to add.", "warn");
        return;
      }
      addRoutePoint({ lat: currentPosition[0], lng: currentPosition[1] });
    });

    els.appendModeBtn.addEventListener("click", () => {
      selectedIndex = null;
      renderRoute();
    });

    els.deletePointBtn.addEventListener("click", () => {
      if (selectedIndex === null) return;
      route.splice(selectedIndex, 1);
      if (route.length === 0) {
        selectedIndex = null;
      } else if (selectedIndex >= route.length) {
        selectedIndex = route.length - 1;
      }
      renderRoute();
    });

    els.undoBtn.addEventListener("click", () => {
      route.pop();
      if (selectedIndex !== null && selectedIndex >= route.length) {
        selectedIndex = route.length ? route.length - 1 : null;
      }
      renderRoute();
    });

    els.clearBtn.addEventListener("click", () => {
      if (route.length > 0 && !window.confirm("Clear the current route?")) return;
      route.length = 0;
      selectedIndex = null;
      renderRoute();
    });

    els.saveTarget.addEventListener("change", renderRoute);

    els.saveBtn.addEventListener("click", async () => {
      if (route.length < 2) return;
      const response = await fetch("/save", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ target: els.saveTarget.value, geojson: makeGeoJSON() })
      });
      const body = await response.json();
      if (!response.ok) {
        setStatus(body.error || "Save failed.", "bad");
        return;
      }
      setStatus(`Saved ${body.path}<br>${route.length} route points.`, "ok");
    });

    els.downloadBtn.addEventListener("click", () => {
      if (route.length < 2) return;
      const blob = new Blob([JSON.stringify(makeGeoJSON(), null, 2) + "\\n"], { type: "application/geo+json" });
      const link = document.createElement("a");
      link.href = URL.createObjectURL(blob);
      link.download = `${els.saveTarget.value}.geojson`;
      link.click();
      URL.revokeObjectURL(link.href);
    });

    els.copyBtn.addEventListener("click", async () => {
      if (route.length < 2) return;
      await navigator.clipboard.writeText(els.geojsonText.value);
      setStatus("GeoJSON copied.", "ok");
    });

    renderRoute();
    pollApiOnce(true)
      .catch((error) => setStatus(`API read failed: ${error.message}`, "bad"))
      .finally(() => pollLoop());
  </script>
</body>
</html>
"""


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Serve the AutoDrive route viewer web UI.")
    parser.add_argument("--host", default=DEFAULT_HOST, help=f"bind host (default: {DEFAULT_HOST})")
    parser.add_argument("--port", default=DEFAULT_PORT, type=int, help=f"HTTP port (default: {DEFAULT_PORT})")
    parser.add_argument("--api", default=DEFAULT_API, help=f"AutoDrive API state URL (default: {DEFAULT_API})")
    parser.add_argument("--api-timeout", default=2.0, type=float, help="remote API request timeout in seconds")
    parser.add_argument(
        "--output",
        default=DEFAULT_OUTPUT,
        help=f"default generated route file in this repo (default: {DEFAULT_OUTPUT})",
    )
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
        "features": [
            {
                "type": "Feature",
                "properties": {
                    "source": "autodrive-route-viewer",
                    "saved_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                },
                "geometry": {
                    "type": "LineString",
                    "coordinates": coords,
                },
            }
        ],
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
    class RouteViewerHandler(BaseHTTPRequestHandler):
        server_version = "AutoDriveRouteViewer/1.0"

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
            elif path == "/health":
                self._send_json({"ok": True})
            else:
                self._send_json({"error": "not found"}, HTTPStatus.NOT_FOUND)

        def do_POST(self) -> None:
            path = urlparse(self.path).path
            if path != "/save":
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
                geojson = payload.get("geojson")
                coords = extract_linestring(geojson)
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

    return RouteViewerHandler


def main() -> None:
    args = parse_args()
    if args.api_timeout <= 0:
        raise SystemExit("--api-timeout must be greater than zero")
    server = ThreadingHTTPServer(
        (args.host, args.port),
        make_handler(args.output, args.api, args.api_timeout),
    )
    print(f"AutoDrive route viewer listening on http://{args.host}:{args.port}", file=sys.stderr)
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
