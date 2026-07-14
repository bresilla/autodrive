#!/usr/bin/env python3
"""
route_maker.py — map-first route editor for AutoDrive GeoJSON routes.

The browser UI proxies a remote api_server.py /state endpoint, shows the live
machine position, lets you draw/edit a GeoJSON LineString, and saves it to a
route file consumed by the streaming steps:

    ./route_maker.py --api http://172.30.0.137:8080/state
    ./08_stream_waypoints.py --route line

Tools: Select / Draw / Turn. The Turn tool lays a circular arc of any sweep
(45 / 90 / 180 deg) at a chosen radius and side, optionally running a straight
leg on afterwards so the turn joins the next swath instead of dead-ending. It
checks the result against PROTOCOL.md 8.5 live (<=30 deg/segment, 0.3-4.5 m
spacing) before you commit it to the route.

Single file on purpose: the UI is plain HTML/CSS/JS inlined below. No build
step, no package manager, no node_modules — edit the HTML string and reload.

The previous version is kept as route_viewer.py, untouched, as a fallback.
"""

from __future__ import annotations

import argparse
import json
import os
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


HTML = r"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>AutoDrive Route Maker</title>
  <!-- NB: the integrity hash here used to be mangled (sha256-p4Nx...aUXk6nF8=), so the
       browser silently blocked leaflet.css and the map had no stylesheet at all. This is
       the hash unpkg actually serves. If you bump the Leaflet version, recompute it:
         curl -s <url> | openssl dgst -sha256 -binary | openssl base64 -A -->
  <link rel="stylesheet" href="https://unpkg.com/leaflet@1.9.4/dist/leaflet.css"
    integrity="sha256-p4NxAoJBhIIN+hmNHrzRCf9tD/miZyoHS5obTRR9BMY=" crossorigin="">
  <style>
    :root{
      color-scheme:dark;
      font-family:"IBM Plex Sans","Segoe UI",ui-sans-serif,system-ui,sans-serif;
      --bg:#0d1418;--sidebar:#111a1f;--card:#18232a;--card-2:#1e2b33;
      --line:rgba(202,224,233,.10);--line-2:rgba(202,224,233,.16);
      --ink:#edf4f7;--muted:#8fa3ac;--dim:#63757e;
      --teal:#8fd2e4;--orange:#e67f4e;--green:#53d689;--danger:#ef9a82;
      --mono:"IBM Plex Mono",ui-monospace,SFMono-Regular,Menlo,Consolas,monospace;
    }
    *{box-sizing:border-box}
    html,body{height:100%}
    body{margin:0;color:var(--ink);background:var(--bg);overflow:hidden}
    button,input,select,textarea{font:inherit;color:inherit}

    /* ============ app shell: one sidebar, one map. That is all. ============ */
    .app{display:flex;height:100vh;width:100vw}
    .sidebar{
      flex:0 0 316px;width:316px;display:flex;flex-direction:column;min-height:0;
      background:var(--sidebar);border-right:1px solid var(--line-2)
    }
    #map{flex:1;min-width:0;background:#e9e4da}

    /* ---- header ---- */
    .head{display:flex;align-items:center;justify-content:space-between;gap:10px;
      padding:14px 14px 12px;border-bottom:1px solid var(--line)}
    .brand{display:grid;gap:1px;min-width:0}
    .brand small{font-size:9px;font-weight:800;letter-spacing:.18em;text-transform:uppercase;color:var(--dim)}
    .brand strong{font-size:15px;font-weight:800;letter-spacing:-.01em}
    .pill{padding:4px 9px;border-radius:999px;font-size:9px;font-weight:900;letter-spacing:.1em;
      background:rgba(83,214,137,.14);border:1px solid rgba(83,214,137,.3);color:#8ff0ae;white-space:nowrap}
    .pill.off{background:rgba(239,154,130,.14);border-color:rgba(239,154,130,.32);color:var(--danger)}

    /* ---- file + save: pinned under the header, never scrolls away ---- */
    .filebar{padding:12px 14px;border-bottom:1px solid var(--line);display:grid;gap:9px;
      background:linear-gradient(180deg,rgba(143,210,228,.05),transparent)}
    .file-row{display:flex;gap:8px;align-items:stretch}
    .file-row select{
      flex:1;min-width:0;padding:0 10px;height:38px;border-radius:10px;font-size:12px;font-weight:700;
      font-family:var(--mono);background:var(--card);border:1px solid var(--line-2);cursor:pointer
    }
    .save{
      flex:0 0 auto;display:flex;align-items:center;gap:7px;height:38px;padding:0 15px;border:0;cursor:pointer;
      border-radius:10px;font-size:12px;font-weight:800;letter-spacing:.02em;
      background:rgba(143,210,228,.16);border:1px solid rgba(143,210,228,.3);color:#dff2f9;
      transition:background 130ms ease,border-color 130ms ease,opacity 130ms ease
    }
    .save:hover:not(:disabled){background:rgba(143,210,228,.26)}
    .save:disabled{opacity:.4;cursor:not-allowed}
    .save.dirty{background:rgba(230,127,78,.2);border-color:rgba(230,127,78,.42);color:#ffd9c4}
    .save.dirty:hover{background:rgba(230,127,78,.3)}
    .save .dot{width:6px;height:6px;border-radius:999px;background:var(--orange);flex:0 0 auto}
    .save:not(.dirty) .dot{display:none}
    .file-meta{display:flex;align-items:center;gap:8px;font-size:10px;color:var(--dim);font-family:var(--mono)}
    .file-meta .grow{flex:1}
    .ghost{
      height:26px;min-width:26px;padding:0 7px;border-radius:7px;cursor:pointer;
      background:transparent;border:1px solid var(--line-2);color:var(--muted);font-size:12px;font-weight:800;
      display:inline-flex;align-items:center;justify-content:center
    }
    .ghost:hover:not(:disabled){background:var(--card);color:var(--ink)}
    .ghost:disabled{opacity:.3;cursor:not-allowed}
    .auto{display:inline-flex;align-items:center;gap:5px;cursor:pointer;
      font-size:9px;font-weight:800;letter-spacing:.1em;text-transform:uppercase;color:var(--dim)}
    .auto input{width:13px;height:13px;margin:0;accent-color:var(--orange);cursor:pointer}

    /* ---- tools: one segmented control, always visible ---- */
    .tools{display:grid;grid-template-columns:repeat(3,1fr);gap:0;margin:12px 14px 0;
      background:var(--card);border:1px solid var(--line-2);border-radius:10px;overflow:hidden}
    .tool{
      display:grid;gap:2px;justify-items:center;padding:9px 4px;border:0;background:transparent;cursor:pointer;
      color:var(--muted);border-right:1px solid var(--line);transition:background 120ms ease,color 120ms ease
    }
    .tool:last-child{border-right:0}
    .tool:hover{background:rgba(255,255,255,.04);color:var(--ink)}
    .tool b{font-size:11px;font-weight:800;letter-spacing:.02em}
    .tool kbd{font-size:8px;font-family:var(--mono);color:var(--dim);background:none;border:0;padding:0}
    .tool.on{background:rgba(230,127,78,.18);color:#ffd0b6;box-shadow:inset 0 -2px 0 var(--orange)}
    .tool.on kbd{color:rgba(255,208,182,.6)}

    /* ---- scrolling body ---- */
    .scroll{flex:1;min-height:0;overflow-y:auto;overflow-x:hidden;padding:12px 14px 16px;display:grid;gap:10px;align-content:start}
    .scroll::-webkit-scrollbar{width:8px}
    .scroll::-webkit-scrollbar-thumb{background:var(--line-2);border-radius:99px}

    .card{background:var(--card);border:1px solid var(--line);border-radius:12px;overflow:hidden}
    .card > summary,.card > .card-head{
      display:flex;align-items:center;gap:7px;padding:9px 11px;cursor:pointer;list-style:none;
      font-size:9px;font-weight:800;letter-spacing:.14em;text-transform:uppercase;color:var(--muted);
      user-select:none
    }
    .card > .card-head{cursor:default}
    .card > summary::-webkit-details-marker{display:none}
    .card > summary:hover{color:var(--ink)}
    .card > summary:before{content:"▸";font-size:9px;color:var(--dim);transition:transform 140ms ease}
    .card[open] > summary:before{transform:rotate(90deg)}
    .card > summary .count{margin-left:auto;font-family:var(--mono);font-size:10px;color:var(--dim);letter-spacing:0}
    .card-body{padding:0 11px 11px;display:grid;gap:9px}

    label{display:grid;gap:5px;font-size:9px;font-weight:800;letter-spacing:.1em;text-transform:uppercase;color:var(--dim)}
    input[type=text],input[type=number],input:not([type]),select,textarea{
      width:100%;padding:8px 9px;border-radius:8px;font-size:12px;
      background:var(--card-2);border:1px solid var(--line-2);outline:none
    }
    input:focus,select:focus,textarea:focus{border-color:rgba(143,210,228,.4)}
    textarea{min-height:170px;resize:vertical;font:11px/1.4 var(--mono)}
    .btn{
      padding:8px 10px;border-radius:8px;cursor:pointer;font-size:11px;font-weight:700;
      background:var(--card-2);border:1px solid var(--line-2);color:var(--ink);
      transition:background 120ms ease,border-color 120ms ease
    }
    .btn:hover:not(:disabled){background:#26343d;border-color:var(--line-2)}
    .btn:disabled{opacity:.35;cursor:not-allowed}
    .btn.on{background:rgba(143,210,228,.18);border-color:rgba(143,210,228,.34);color:#dff2f9}
    .btn.danger{color:var(--danger);border-color:rgba(239,154,130,.22);background:rgba(239,154,130,.08)}
    .btn.danger:hover:not(:disabled){background:rgba(239,154,130,.16)}
    .btn.wide{grid-column:1/-1}
    .r2{display:grid;grid-template-columns:1fr 1fr;gap:7px}
    .r3{display:grid;grid-template-columns:repeat(3,1fr);gap:7px}
    .presets{display:grid;grid-template-columns:1fr 1fr 1fr 72px;gap:6px}
    .preset{padding:7px 0;font-family:var(--mono)}
    .presets input{text-align:center;font-family:var(--mono)}
    .hint{margin:0;font-size:10px;line-height:1.45;color:var(--dim)}
    kbd{display:inline-block;padding:1px 4px;border-radius:4px;font:9px/1.5 var(--mono);font-weight:700;
      background:rgba(231,240,244,.08);border:1px solid var(--line-2);color:var(--muted)}

    /* readouts */
    .readout{display:grid;grid-template-columns:1fr 1fr;gap:1px;background:var(--line);
      border:1px solid var(--line);border-radius:9px;overflow:hidden}
    .readout div{background:var(--card-2);padding:7px 9px;min-width:0}
    .readout span{display:block;font-size:8px;font-weight:800;letter-spacing:.12em;text-transform:uppercase;color:var(--dim)}
    .readout b{display:block;font-size:12px;font-family:var(--mono);white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
    .status{padding:8px 9px;border-radius:8px;font-size:11px;line-height:1.4;font-family:var(--mono);
      background:var(--card-2);border:1px solid var(--line-2);color:var(--muted);word-break:break-word}
    .status.ok{border-color:rgba(83,214,137,.26);color:#9ff0bb}
    .status.bad{border-color:rgba(239,154,130,.3);color:var(--danger)}

    /* point list */
    .points{display:grid;gap:4px;max-height:270px;overflow-y:auto}
    .prow{display:flex;align-items:center;gap:8px;width:100%;padding:6px 8px;border-radius:8px;cursor:pointer;
      background:var(--card-2);border:1px solid transparent;text-align:left}
    .prow:hover{border-color:var(--line-2)}
    .prow.on{background:rgba(230,127,78,.16);border-color:rgba(230,127,78,.36)}
    .prow .n{flex:0 0 auto;min-width:22px;height:20px;display:grid;place-items:center;border-radius:5px;
      background:rgba(143,210,228,.12);color:var(--teal);font:9px/1 var(--mono);font-weight:800}
    .prow:first-child .n{background:rgba(230,127,78,.2);color:#ffc7a8}
    .prow .c{font:10px/1.3 var(--mono);color:var(--muted);white-space:nowrap;overflow:hidden;text-overflow:ellipsis}

    /* ============ map overlays ============ */
    .hintbar{
      position:absolute;bottom:14px;left:50%;transform:translateX(-50%);z-index:900;
      padding:6px 12px;border-radius:999px;white-space:nowrap;font-size:10px;color:var(--muted);
      background:rgba(13,20,24,.9);border:1px solid var(--line-2);backdrop-filter:blur(10px);
      box-shadow:0 8px 24px rgba(0,0,0,.3)
    }
    .hintbar b{color:var(--orange);font-weight:800;letter-spacing:.08em;text-transform:uppercase;font-size:9px}
    .toasts{position:absolute;bottom:52px;left:50%;transform:translateX(-50%);z-index:950;
      display:grid;gap:5px;justify-items:center;pointer-events:none}
    .toast{padding:7px 13px;border-radius:9px;font-size:11px;font-weight:700;white-space:nowrap;
      background:rgba(13,20,24,.94);border:1px solid var(--line-2);color:var(--ink);
      backdrop-filter:blur(10px);box-shadow:0 8px 24px rgba(0,0,0,.32);animation:tin 160ms ease}
    .toast.ok{border-color:rgba(83,214,137,.34);color:#9ff0bb}
    .toast.bad{border-color:rgba(239,154,130,.36);color:var(--danger)}
    .toast.out{opacity:0;transform:translateY(4px);transition:all 200ms ease}
    @keyframes tin{from{opacity:0;transform:translateY(5px)}to{opacity:1;transform:none}}

    .maptools{position:absolute;top:12px;right:12px;z-index:900;display:grid;gap:6px;justify-items:end}
    .seg{display:flex;background:rgba(13,20,24,.9);border:1px solid var(--line-2);border-radius:9px;
      overflow:hidden;backdrop-filter:blur(10px);box-shadow:0 8px 24px rgba(0,0,0,.3)}
    .seg button{padding:6px 11px;border:0;background:transparent;cursor:pointer;color:var(--muted);
      font-size:10px;font-weight:800;border-right:1px solid var(--line)}
    .seg button:last-child{border-right:0}
    .seg button:hover{color:var(--ink)}
    .seg button.on{background:rgba(143,210,228,.2);color:#dff2f9}

    body.draw #map,body.uturn #map{cursor:crosshair}
    .leaflet-container{background:#e9e4da}
    .leaflet-control-attribution{background:rgba(255,255,255,.8)!important;font-size:9px!important}

    .machine{display:flex;flex-direction:column;align-items:center;gap:5px;pointer-events:auto}
    .arrow{position:relative;width:22px;height:22px;background:#0d1418;
      clip-path:polygon(50% 0%,100% 100%,50% 78%,0% 100%);
      filter:drop-shadow(0 4px 10px rgba(0,0,0,.3));transform:rotate(var(--h,0deg));transform-origin:50% 70%}
    .arrow.none{opacity:.3}
    .arrow:after{content:"";position:absolute;inset:2px;background:var(--teal);
      clip-path:polygon(50% 0%,100% 100%,50% 78%,0% 100%)}
    .mlabel{padding:2px 7px;border-radius:999px;background:rgba(13,20,24,.92);
      border:1px solid rgba(143,210,228,.3);color:var(--teal);
      font:9px/1.5 var(--mono);font-weight:800;letter-spacing:.06em;white-space:nowrap}
    .h{width:16px;height:16px;border:3px solid #fff;border-radius:999px;background:#5d8791;
      box-shadow:0 3px 10px rgba(0,0,0,.35)}
    .h.first{width:20px;height:20px;background:linear-gradient(135deg,var(--teal),var(--orange))}
    .h.on{background:var(--orange);box-shadow:0 0 0 4px rgba(230,127,78,.3),0 3px 10px rgba(0,0,0,.35)}

    @media(max-width:900px){
      body{overflow:auto}
      .app{flex-direction:column;height:auto}
      .sidebar{flex:none;width:100%;border-right:0;border-bottom:1px solid var(--line-2)}
      .scroll{max-height:none}
      #map{height:62vh;min-height:340px}
      .hintbar{display:none}
    }
  </style>
</head>
<body>
  <div class="app">
    <aside class="sidebar">
      <div class="head">
        <div class="brand">
          <small>AutoDrive</small>
          <strong>Route Maker</strong>
        </div>
        <span class="pill" id="livePill">LIVE</span>
      </div>

      <div class="filebar">
        <div class="file-row">
          <select id="target" title="Route file being edited">
            <option value="line">line.geojson</option>
            <option value="uturn">u_field.geojson</option>
          </select>
          <button id="saveBtn" class="save" title="Save to disk (Ctrl+S)">
            <span class="dot"></span><span id="saveLabel">Save</span>
          </button>
        </div>
        <div class="file-meta">
          <button id="undoBtn" class="ghost" title="Undo (Ctrl+Z)">&#8630;</button>
          <button id="redoBtn" class="ghost" title="Redo (Ctrl+Shift+Z)">&#8631;</button>
          <span class="grow" id="fileSummary">0 pts &middot; 0 m</span>
          <label class="auto" title="Write the file 1.5s after each edit">
            <input type="checkbox" id="autosave"> Auto
          </label>
        </div>
      </div>

      <div class="tools">
        <button class="tool on" data-mode="select" type="button"><b>Select</b><kbd>V</kbd></button>
        <button class="tool" data-mode="draw" type="button"><b>Draw</b><kbd>D</kbd></button>
        <button class="tool" data-mode="uturn" type="button"><b>Turn</b><kbd>U</kbd></button>
      </div>

      <div class="scroll">
        <div class="card">
          <div class="card-head">Route</div>
          <div class="card-body">
            <div class="readout">
              <div><span>Points</span><b id="pointCount">0</b></div>
              <div><span>Length</span><b id="routeLength">0 m</b></div>
              <div><span>Max gap</span><b id="maxGap">0 m</b></div>
              <div><span>Selected</span><b id="selectedPoint">none</b></div>
            </div>
            <div class="r3">
              <button id="fitBtn" class="btn">Fit</button>
              <button id="reverseBtn" class="btn">Reverse</button>
              <button id="clearBtn" class="btn danger">Clear</button>
            </div>
          </div>
        </div>

        <details class="card" id="cardPoint">
          <summary>Selected point <span class="count" id="selCount">none</span></summary>
          <div class="card-body">
            <div class="r2">
              <label>Lat <input id="selectedLat" inputmode="decimal" placeholder="--"></label>
              <label>Lon <input id="selectedLon" inputmode="decimal" placeholder="--"></label>
            </div>
            <div class="r2">
              <button id="applyPointBtn" class="btn">Apply</button>
              <button id="deletePointBtn" class="btn danger">Delete</button>
            </div>
            <p class="hint">Arrows nudge 0.25 m &middot; <kbd>Shift</kbd>+arrow 2.5 m &middot; <kbd>Del</kbd> removes.</p>
          </div>
        </details>

        <details class="card" id="cardPoints">
          <summary>All points <span class="count" id="listCount">0</span></summary>
          <div class="card-body">
            <div id="pointList" class="points"></div>
          </div>
        </details>

        <details class="card" id="cardUturn">
          <summary>Turn tool <span class="count" id="turnCount">--</span></summary>
          <div class="card-body">
            <label>Arc angle</label>
            <div class="presets">
              <button class="btn preset" data-angle="45" type="button">45&deg;</button>
              <button class="btn preset" data-angle="90" type="button">90&deg;</button>
              <button class="btn preset on" data-angle="180" type="button">180&deg;</button>
              <input id="turnAngle" type="number" min="5" max="180" step="5" value="180">
            </div>
            <div class="r3">
              <label>Radius m <input id="uturnRadius" type="number" min="0.5" max="500" step="0.5" value="6"></label>
              <label>Side
                <select id="uturnSide">
                  <option value="left">Left</option>
                  <option value="right">Right</option>
                </select>
              </label>
              <label>Pts/180&deg; <input id="uturnPoints" type="number" min="6" max="96" step="1" value="18"></label>
            </div>
            <label>Continue straight after the turn, m
              <input id="turnContinue" type="number" min="0" max="5000" step="1" value="0">
            </label>
            <div id="turnStats" class="status">--</div>
            <button id="insertUturnBtn" class="btn wide">Insert at selection</button>
            <p class="hint">Or hit <kbd>U</kbd> and click the map to drop the turn there.
              <b>Continue</b> runs a straight leg on after the arc, so the turn joins the next swath
              instead of dead-ending.</p>
          </div>
        </details>

        <details class="card" id="cardMachine">
          <summary>Machine <span class="count" id="machineCount">--</span></summary>
          <div class="card-body">
            <label>API URL <input id="apiUrl" value="__API_URL__"></label>
            <div class="r3">
              <button id="pollBtn" class="btn on">Pause</button>
              <button id="centerBtn" class="btn">Center</button>
              <button id="addCurrentBtn" class="btn">Add pt</button>
            </div>
            <div id="positionStatus" class="status">Waiting for data.</div>
          </div>
        </details>

        <details class="card" id="cardJson">
          <summary>GeoJSON</summary>
          <div class="card-body">
            <div class="r3">
              <button id="revertBtn" class="btn">Revert</button>
              <button id="copyBtn" class="btn">Copy</button>
              <button id="downloadBtn" class="btn">Download</button>
            </div>
            <textarea id="geojsonText" spellcheck="false"></textarea>
            <div class="r2">
              <button id="formatBtn" class="btn">Format</button>
              <button id="importBtn" class="btn">Import box</button>
            </div>
          </div>
        </details>
      </div>
    </aside>

    <main id="map">
      <div class="maptools">
        <div class="seg">
          <button id="mapLightBtn" class="on" type="button">Light</button>
          <button id="mapSatelliteBtn" type="button">Satellite</button>
        </div>
      </div>
      <div id="toasts" class="toasts"></div>
      <div id="hintbar" class="hintbar"></div>
    </main>
  </div>

  <script src="https://unpkg.com/leaflet@1.9.4/dist/leaflet.js"
    integrity="sha256-20nQCchB9co0qIjJZRGuk2/Z9VM+kNiyxNV1lvTlZBo=" crossorigin=""></script>
  <script>
    // keyboard:false — arrow keys nudge the selected point, they do not pan.
    const map = L.map("map", { zoomControl: true, keyboard: false, preferCanvas: true, maxZoom: 24 })
      .setView([50.6970, 5.3312], 17);
    map.zoomControl.setPosition("bottomright");   // top-right is the basemap switch
    const baseMaps = {
      light: L.tileLayer("https://{s}.basemaps.cartocdn.com/light_all/{z}/{x}/{y}{r}.png", {
        subdomains: "abcd", maxZoom: 24, maxNativeZoom: 19, attribution: "&copy; OpenStreetMap &copy; CARTO"
      }),
      satellite: L.tileLayer("https://server.arcgisonline.com/ArcGIS/rest/services/World_Imagery/MapServer/tile/{z}/{y}/{x}", {
        maxZoom: 24, maxNativeZoom: 19, attribution: "Tiles &copy; Esri"
      })
    };
    let activeBaseMap = baseMaps.light.addTo(map);
    map.whenReady(() => { setTimeout(() => map.invalidateSize(true), 60); setTimeout(() => map.invalidateSize(true), 400); });
    window.addEventListener("resize", () => map.invalidateSize(true));

    const FILE_NAMES = { line: "line.geojson", uturn: "u_field.geojson" };
    const HINTS = {
      select: "<b>Select</b> drag a point &middot; click the line to insert &middot; <kbd>Del</kbd> remove &middot; arrows nudge",
      draw: "<b>Draw</b> click the map to append a point &middot; <kbd>Esc</kbd> done",
      uturn: "<b>Turn</b> click the map to drop the arc &middot; <kbd>Enter</kbd> at selection &middot; <kbd>Esc</kbd> done"
    };

    const route = [];
    const trail = [];
    let selectedIndex = null;
    let currentPosition = null;
    let currentHeading = null;
    let polling = true;
    let pollTimer = null;
    let centeredOnce = false;
    let mode = "select";
    let history = [];
    let histAt = -1;
    let savedKey = "[]";
    let loadedTarget = null;
    let autosaveTimer = null;
    const pollIntervalMs = 250;

    const casing = L.polyline([], { color: "#12262d", weight: 8, opacity: .9, lineCap: "round", lineJoin: "round", interactive: false }).addTo(map);
    const routeLayer = L.polyline([], { color: "#e67f4e", weight: 4.5, opacity: .97, lineCap: "round", lineJoin: "round" }).addTo(map);
    const uturnPreview = L.polyline([], { color: "#8fd2e4", weight: 4, opacity: .75, dashArray: "8 7", interactive: false }).addTo(map);
    const trailLayer = L.polyline([], { color: "#8fd2e4", weight: 3, opacity: .8, interactive: false }).addTo(map);
    const pointLayer = L.layerGroup().addTo(map);
    const machineIcon = L.divIcon({ className: "", iconSize: [70, 46], iconAnchor: [35, 16],
      html: "<div class='machine'><div class='arrow'></div><div class='mlabel'>MACHINE</div></div>" });
    const machineMarker = L.marker([0, 0], { icon: machineIcon, zIndexOffset: 1000 });

    const el = {};
    for (const id of ["apiUrl", "livePill", "pollBtn", "centerBtn", "mapLightBtn", "mapSatelliteBtn",
      "fitBtn", "insertUturnBtn", "uturnRadius", "uturnSide", "uturnPoints", "positionStatus",
      "addCurrentBtn", "reverseBtn", "clearBtn", "selectedLat", "selectedLon", "applyPointBtn",
      "deletePointBtn", "pointList", "pointCount", "routeLength", "maxGap", "selectedPoint",
      "target", "saveBtn", "saveLabel", "autosave", "undoBtn", "redoBtn", "revertBtn", "importBtn",
      "copyBtn", "downloadBtn", "formatBtn", "geojsonText", "toasts", "hintbar", "fileSummary",
      "selCount", "listCount", "machineCount", "cardPoints", "cardJson", "cardUturn",
      "turnAngle", "turnContinue", "turnStats", "turnCount"]) {
      el[id] = document.getElementById(id);
    }

    function toast(text, kind) {
      const node = document.createElement("div");
      node.className = "toast" + (kind ? " " + kind : "");
      node.textContent = text;
      el.toasts.appendChild(node);
      setTimeout(() => { node.classList.add("out"); setTimeout(() => node.remove(), 220); }, 2200);
    }

    // -------------------------------------------------------------------- geo
    function meters(a, b) {
      const r = 6371008.8;
      const lat1 = a[0] * Math.PI / 180, lat2 = b[0] * Math.PI / 180;
      const dlat = (b[0] - a[0]) * Math.PI / 180, dlon = (b[1] - a[1]) * Math.PI / 180;
      const s = Math.sin(dlat / 2) ** 2 + Math.cos(lat1) * Math.cos(lat2) * Math.sin(dlon / 2) ** 2;
      return 2 * r * Math.atan2(Math.sqrt(s), Math.sqrt(1 - s));
    }
    function fmtMeters(m) { return m >= 1000 ? (m / 1000).toFixed(2) + " km" : m.toFixed(m < 10 ? 1 : 0) + " m"; }
    function movementHeading(previous, current) {
      const lat = previous[0] * Math.PI / 180;
      const north = (current[0] - previous[0]) * 111320;
      const east = (current[1] - previous[1]) * 111320 * Math.cos(lat);
      if (Math.hypot(east, north) < 0.05) return null;
      return (Math.atan2(east, north) * 180 / Math.PI + 360) % 360;
    }
    function offsetPoint(origin, eastM, northM) {
      return [origin[0] + northM / 111320,
              origin[1] + eastM / (111320 * Math.cos(origin[0] * Math.PI / 180))];
    }
    function cleanHeading(value) {
      if (value === null || value === undefined || value === "") return null;
      const h = Number(value);
      if (!Number.isFinite(h)) return null;
      return ((h % 360) + 360) % 360;
    }
    function clampNumber(value, min, max, fallback) {
      const n = Number(value);
      if (!Number.isFinite(n)) return fallback;
      return Math.min(max, Math.max(min, n));
    }
    function routeStats() {
      let length = 0, maxGap = 0;
      for (let i = 1; i < route.length; i++) {
        const d = meters(route[i - 1], route[i]);
        length += d; maxGap = Math.max(maxGap, d);
      }
      return { length, maxGap };
    }

    // ---------------------------------------------------------- history/dirty
    function cloneRoute() { return route.map((p) => [p[0], p[1]]); }
    function routeKey() { return JSON.stringify(route.map(([a, b]) => [Number(a.toFixed(7)), Number(b.toFixed(7))])); }
    function isDirty() { return routeKey() !== savedKey; }
    function resetHistory() { history = [cloneRoute()]; histAt = 0; }
    function pushHistory() {
      history = history.slice(0, histAt + 1);
      history.push(cloneRoute());
      if (history.length > 300) history.shift();
      histAt = history.length - 1;
    }
    function restore(snapshot) {
      route.length = 0;
      snapshot.forEach((p) => route.push([p[0], p[1]]));
      if (selectedIndex !== null && selectedIndex >= route.length) {
        selectedIndex = route.length ? route.length - 1 : null;
      }
      render();
    }
    function commit() { pushHistory(); render(); scheduleAutosave(); }
    function undo() {
      if (histAt <= 0) { toast("Nothing to undo"); return; }
      histAt -= 1; restore(history[histAt]); scheduleAutosave();
    }
    function redo() {
      if (histAt >= history.length - 1) { toast("Nothing to redo"); return; }
      histAt += 1; restore(history[histAt]); scheduleAutosave();
    }

    // ------------------------------------------------------------------ modes
    function setMode(next) {
      mode = next;
      document.querySelectorAll(".tool").forEach((b) => b.classList.toggle("on", b.dataset.mode === mode));
      document.body.classList.toggle("draw", mode === "draw");
      document.body.classList.toggle("uturn", mode === "uturn");
      el.hintbar.innerHTML = HINTS[mode];
      previewUturn();
    }

    // --------------------------------------------------------------- geometry
    function toFront() {
      trailLayer.bringToFront(); casing.bringToFront(); routeLayer.bringToFront(); uturnPreview.bringToFront();
      pointLayer.eachLayer((l) => l.bringToFront && l.bringToFront());
      if (map.hasLayer(machineMarker)) machineMarker.setZIndexOffset(1000);
    }
    function setBaseMap(name) {
      const next = baseMaps[name];
      if (!next || next === activeBaseMap) return;
      map.removeLayer(activeBaseMap);
      activeBaseMap = next.addTo(map);
      el.mapLightBtn.classList.toggle("on", name === "light");
      el.mapSatelliteBtn.classList.toggle("on", name === "satellite");
      activeBaseMap.once("load", toFront);
      toFront();
    }
    function nearestSegment(latlng) {
      if (route.length < 2) return null;
      const p = map.latLngToLayerPoint(latlng);
      let best = null, bestDist = Infinity;
      for (let i = 0; i < route.length - 1; i++) {
        const d = L.LineUtil.pointToSegmentDistance(p,
          map.latLngToLayerPoint(route[i]), map.latLngToLayerPoint(route[i + 1]));
        if (d < bestDist) { bestDist = d; best = i; }
      }
      return best;
    }

    // ----------------------------------------------------------------- u-turn
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
    function norm360(deg) { return ((deg % 360) + 360) % 360; }

    // A circular arc of ANY sweep, not just a 180° semicircle. Turning `right` puts the
    // centre on the right-hand normal; the point then sweeps about it. sweep=180 gives the
    // classic U-turn, 90 a quarter turn, 45 an eighth. End heading = start ± sweep.
    function arcSegment(start, headingDeg, radius, right, sweepDeg, steps) {
      const h = headingDeg * Math.PI / 180;
      const nE = right ? Math.cos(h) : -Math.cos(h);
      const nN = right ? -Math.sin(h) : Math.sin(h);
      const cE = radius * nE, cN = radius * nN;   // centre, relative to start
      const rE = -cE, rN = -cN;                   // centre -> start
      const sweep = sweepDeg * Math.PI / 180;
      const pts = [];
      for (let i = 0; i <= steps; i++) {
        const t = sweep * (i / steps) * (right ? -1 : 1);
        const cos = Math.cos(t), sin = Math.sin(t);
        pts.push(offsetPoint(start, cE + rE * cos - rN * sin, cN + rE * sin + rN * cos));
      }
      return { pts, end: pts[pts.length - 1], heading: norm360(headingDeg + (right ? sweepDeg : -sweepDeg)) };
    }

    function straightSegment(start, headingDeg, lengthM, steps) {
      const h = headingDeg * Math.PI / 180;
      const fE = Math.sin(h), fN = Math.cos(h);
      const pts = [];
      for (let i = 0; i <= steps; i++) {
        const d = lengthM * i / steps;
        pts.push(offsetPoint(start, fE * d, fN * d));
      }
      return { pts, end: pts[pts.length - 1], heading: norm360(headingDeg) };
    }

    // The turn = arc, then optionally a straight leg so it joins the next swath
    // instead of dead-ending in mid-field.
    function buildUturn(start, headingDeg) {
      const radius = clampNumber(el.uturnRadius.value, 0.5, 500, 6);
      const angle = clampNumber(el.turnAngle.value, 5, 180, 180);
      const per180 = Math.round(clampNumber(el.uturnPoints.value, 6, 96, 18));
      const runOn = clampNumber(el.turnContinue.value, 0, 5000, 0);
      const right = el.uturnSide.value === "right";

      const steps = Math.max(3, Math.round(per180 * angle / 180));
      const arc = arcSegment(start, headingDeg, radius, right, angle, steps);
      let pts = arc.pts;
      if (runOn > 0) {
        const spacing = Math.PI * radius / per180;      // match the arc's point density
        const legSteps = Math.max(1, Math.round(runOn / Math.max(0.3, spacing)));
        pts = pts.concat(straightSegment(arc.end, arc.heading, runOn, legSteps).pts.slice(1));
      }
      return pts;
    }

    // Spacing and per-segment heading change decide whether the Display will actually
    // steer this: PROTOCOL.md §8.5 wants gentle curves, <=30 deg/segment, and routes.py
    // notes AgJunction likes 0.3-4.5 m between waypoints.
    function turnQuality(pts) {
      let maxGap = 0, maxTurn = 0;
      for (let i = 1; i < pts.length; i++) maxGap = Math.max(maxGap, meters(pts[i - 1], pts[i]));
      for (let i = 2; i < pts.length; i++) {
        const a = movementHeading(pts[i - 2], pts[i - 1]);
        const b = movementHeading(pts[i - 1], pts[i]);
        if (a === null || b === null) continue;
        let d = Math.abs(b - a);
        if (d > 180) d = 360 - d;
        maxTurn = Math.max(maxTurn, d);
      }
      return { maxGap, maxTurn };
    }
    function setTurnStats(pts) {
      if (!pts || pts.length < 2) {
        el.turnStats.className = "status";
        el.turnStats.textContent = "--";
        el.turnCount.textContent = "--";
        return;
      }
      const q = turnQuality(pts);
      const offset = meters(pts[0], pts[pts.length - 1]);
      const problems = [];
      if (q.maxTurn > 30) problems.push("turn " + q.maxTurn.toFixed(0) + " deg/seg > 30");
      if (q.maxGap > 4.5) problems.push("gap " + q.maxGap.toFixed(1) + " m > 4.5");
      if (q.maxGap < 0.3) problems.push("gap " + q.maxGap.toFixed(2) + " m < 0.3");
      el.turnCount.textContent = pts.length + " pts";
      el.turnStats.className = "status" + (problems.length ? " bad" : " ok");
      el.turnStats.innerHTML = problems.length
        ? problems.join("<br>")
        : pts.length + " pts &middot; offset " + fmtMeters(offset) +
          "<br>gap " + q.maxGap.toFixed(2) + " m &middot; " + q.maxTurn.toFixed(0) + " deg/seg";
    }

    // Preview whenever the Turn card is open, not only while the tool is armed —
    // you want to see the arc while you are dialling the radius in.
    function previewUturn() {
      if (mode !== "uturn" && !el.cardUturn.open) {
        uturnPreview.setLatLngs([]);
        setTurnStats(null);
        return;
      }
      let start = null, index = selectedIndex;
      if (index !== null && route[index]) start = route[index];
      else if (currentPosition) start = currentPosition;
      else if (route.length) { start = route[route.length - 1]; index = route.length - 1; }
      if (!start) { uturnPreview.setLatLngs([]); setTurnStats(null); return; }
      const pts = buildUturn(start, headingForUturn(start, index));
      uturnPreview.setLatLngs(pts);
      setTurnStats(pts);
      toFront();
    }
    function insertUturn(explicitStart) {
      let start = explicitStart, index = selectedIndex;
      let insertAt = route.length, includeStart = true;
      if (!start && index !== null && route[index]) { start = route[index]; insertAt = index + 1; includeStart = false; }
      else if (!start && currentPosition) start = currentPosition;
      else if (!start && route.length) { start = route[route.length - 1]; index = route.length - 1; includeStart = false; }
      if (!start) { toast("No start point for the turn", "bad"); return; }
      const arc = buildUturn(start, headingForUturn(start, index));
      if (route.length && includeStart && meters(route[route.length - 1], start) < 0.3) includeStart = false;
      const points = includeStart ? arc : arc.slice(1);
      route.splice(insertAt, 0, ...points);
      selectedIndex = insertAt + points.length - 1;
      commit();
      const q = turnQuality(arc);
      toast(Math.round(clampNumber(el.turnAngle.value, 5, 180, 180)) + "° turn inserted — " +
        points.length + " pts" + (q.maxTurn > 30 ? " (⚠ " + q.maxTurn.toFixed(0) + " deg/seg)" : ""),
        q.maxTurn > 30 ? "bad" : "ok");
    }

    // ------------------------------------------------------------ route edits
    function selectPoint(index) {
      selectedIndex = index;
      render();
      if (index !== null && route[index]) map.panTo(route[index], { animate: false });
    }
    function appendPoint(latlng) {
      route.push([latlng.lat, latlng.lng]);
      selectedIndex = route.length - 1;
      commit();
    }
    function insertOnSegment(latlng) {
      const seg = nearestSegment(latlng);
      if (seg === null) return;
      route.splice(seg + 1, 0, [latlng.lat, latlng.lng]);
      selectedIndex = seg + 1;
      commit();
      toast("Inserted point " + selectedIndex);
    }
    function deleteSelected() {
      if (selectedIndex === null) { toast("No point selected"); return; }
      route.splice(selectedIndex, 1);
      selectedIndex = route.length ? Math.min(selectedIndex, route.length - 1) : null;
      commit();
    }
    function nudge(key, big) {
      if (selectedIndex === null) return;
      const step = big ? 2.5 : 0.25;
      const d = { ArrowUp: [0, step], ArrowDown: [0, -step], ArrowLeft: [-step, 0], ArrowRight: [step, 0] }[key];
      if (!d) return;
      route[selectedIndex] = offsetPoint(route[selectedIndex], d[0], d[1]);
      commit();
      map.panTo(route[selectedIndex], { animate: false });
    }
    function replaceRoute(points) {
      route.length = 0;
      points.forEach((p) => route.push(p));
      selectedIndex = route.length ? 0 : null;
      resetHistory();
      render();
      fitAll();
    }
    // The route wins. The machine can be kilometres away (a different field, or no
    // fix yet) and folding it into the bounds zooms the route down to a dot — use
    // the Center button for the machine instead.
    function fitAll() {
      const pts = route.length ? route.slice() : (currentPosition ? [currentPosition] : []);
      if (!pts.length) return;
      map.invalidateSize(true);
      if (pts.length === 1) map.setView(pts[0], Math.max(map.getZoom(), 18), { animate: false });
      else map.fitBounds(pts, { padding: [40, 40], maxZoom: 21, animate: false });
    }

    // ---------------------------------------------------------------- geojson
    function geojson() {
      return {
        type: "FeatureCollection",
        features: [{
          type: "Feature",
          properties: { source: "autodrive-route-viewer" },
          geometry: { type: "LineString",
            coordinates: route.map(([lat, lon]) => [Number(lon.toFixed(7)), Number(lat.toFixed(7))]) }
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
      if (!line || !Array.isArray(line.coordinates) || line.coordinates.length < 2) {
        throw new Error("Expected a LineString with at least two points");
      }
      return line.coordinates.map((c) => {
        const lon = Number(c[0]), lat = Number(c[1]);
        if (!Number.isFinite(lat) || !Number.isFinite(lon) || lat < -90 || lat > 90 || lon < -180 || lon > 180) {
          throw new Error("Invalid coordinate");
        }
        return [lat, lon];
      });
    }

    // ------------------------------------------------------------ save / load
    function fileName(path) { return String(path).split("/").pop(); }
    async function save(auto) {
      if (route.length < 2) { if (!auto) toast("Need at least 2 points to save", "bad"); return; }
      const target = el.target.value;
      if (!auto && loadedTarget && target !== loadedTarget &&
          !confirm("Overwrite " + FILE_NAMES[target] + "? You loaded " + FILE_NAMES[loadedTarget] + ".")) return;
      try {
        const res = await fetch("/save", { method: "POST", headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ target, geojson: geojson() }) });
        const body = await res.json();
        if (!res.ok) { toast(body.error || "Save failed", "bad"); return; }
        savedKey = routeKey();
        loadedTarget = target;
        render();
        toast((auto ? "Autosaved " : "Saved ") + fileName(body.path) + " — " + body.points + " pts", "ok");
      } catch (err) { toast("Save failed: " + err.message, "bad"); }
    }
    function scheduleAutosave() {
      if (!el.autosave.checked) return;
      clearTimeout(autosaveTimer);
      autosaveTimer = setTimeout(() => { if (isDirty()) save(true); }, 1500);
    }
    async function loadTarget(target, quiet) {
      const res = await fetch("/route?target=" + encodeURIComponent(target), { cache: "no-store" });
      const body = await res.json();
      if (!res.ok) { if (!quiet) toast(body.error || "Load failed", "bad"); return; }
      const pts = parseGeoJSON(body.geojson);
      replaceRoute(pts);
      savedKey = routeKey();
      loadedTarget = target;
      el.target.value = target;   // keep the dropdown honest about what is actually open
      render();
      toast("Loaded " + fileName(body.path) + " — " + pts.length + " pts", "ok");
    }

    // ----------------------------------------------------------------- render
    function renderPointList() {
      if (!el.cardPoints.open) return;
      el.pointList.innerHTML = "";
      if (!route.length) {
        el.pointList.innerHTML = "<p class='hint'>No points yet. Hit <kbd>D</kbd> and click the map.</p>";
        return;
      }
      const MAX = 200;
      let from = 0, to = route.length;
      if (route.length > MAX) {
        const c = selectedIndex === null ? 0 : selectedIndex;
        to = Math.min(route.length, Math.max(c + MAX / 2, MAX));
        from = Math.max(0, to - MAX);
      }
      const frag = document.createDocumentFragment();
      if (from > 0) {
        const n = document.createElement("p");
        n.className = "hint"; n.textContent = from + " earlier points hidden";
        frag.appendChild(n);
      }
      for (let i = from; i < to; i++) {
        const [lat, lon] = route[i];
        const row = document.createElement("button");
        row.className = "prow" + (i === selectedIndex ? " on" : "");
        row.type = "button";
        row.innerHTML = "<span class='n'>" + i + "</span><span class='c'>" +
          lat.toFixed(7) + ", " + lon.toFixed(7) + "</span>";
        row.onclick = () => selectPoint(i);
        frag.appendChild(row);
      }
      if (to < route.length) {
        const n = document.createElement("p");
        n.className = "hint"; n.textContent = (route.length - to) + " later points hidden";
        frag.appendChild(n);
      }
      el.pointList.appendChild(frag);
    }
    function renderJSON() {
      if (!el.cardJson.open) return;
      el.geojsonText.value = JSON.stringify(geojson(), null, 2);
    }
    function renderMarkers() {
      pointLayer.clearLayers();
      route.forEach((point, index) => {
        const cls = ["h", index === 0 ? "first" : "", index === selectedIndex ? "on" : ""].filter(Boolean).join(" ");
        const marker = L.marker(point, {
          draggable: true,
          zIndexOffset: index === selectedIndex ? 800 : 500,
          icon: L.divIcon({ className: "", iconSize: [20, 20], iconAnchor: [10, 10],
            html: "<div class='" + cls + "'></div>" })
        });
        marker.on("click", (ev) => { L.DomEvent.stopPropagation(ev); selectPoint(index); });
        // During a drag only the polylines move: no JSON re-serialise, no marker
        // rebuild, no list rebuild. That is what made dragging lag before.
        marker.on("drag", (ev) => {
          const p = ev.target.getLatLng();
          route[index] = [p.lat, p.lng];
          casing.setLatLngs(route);
          routeLayer.setLatLngs(route);
        });
        marker.on("dragend", (ev) => {
          const p = ev.target.getLatLng();
          route[index] = [p.lat, p.lng];
          selectedIndex = index;
          commit();
        });
        marker.addTo(pointLayer);
      });
    }
    function render() {
      if (selectedIndex !== null && selectedIndex >= route.length) {
        selectedIndex = route.length ? route.length - 1 : null;
      }
      casing.setLatLngs(route);
      routeLayer.setLatLngs(route);
      renderMarkers();
      toFront();

      const st = routeStats();
      const dirty = isDirty();
      const sel = selectedIndex === null ? "none" : String(selectedIndex);
      el.pointCount.textContent = String(route.length);
      el.routeLength.textContent = fmtMeters(st.length);
      el.maxGap.textContent = fmtMeters(st.maxGap);
      el.selectedPoint.textContent = sel;
      el.selCount.textContent = sel;
      el.listCount.textContent = String(route.length);
      el.fileSummary.textContent = route.length + " pts · " + fmtMeters(st.length);
      el.selectedLat.value = selectedIndex === null ? "" : route[selectedIndex][0].toFixed(7);
      el.selectedLon.value = selectedIndex === null ? "" : route[selectedIndex][1].toFixed(7);
      el.applyPointBtn.disabled = selectedIndex === null;
      el.deletePointBtn.disabled = selectedIndex === null;
      el.saveBtn.disabled = route.length < 2;
      el.saveBtn.classList.toggle("dirty", dirty);
      el.saveLabel.textContent = dirty ? "Save" : "Saved";
      el.copyBtn.disabled = route.length < 2;
      el.downloadBtn.disabled = route.length < 2;
      el.undoBtn.disabled = histAt <= 0;
      el.redoBtn.disabled = histAt >= history.length - 1;

      renderPointList();
      renderJSON();
      previewUturn();
    }

    // ---------------------------------------------------------------- machine
    function applyArrow(heading) {
      const node = machineMarker.getElement();
      const arrow = node && node.querySelector(".arrow");
      if (!arrow) return;
      if (heading === null) { arrow.classList.add("none"); return; }
      arrow.classList.remove("none");
      arrow.style.setProperty("--h", heading + "deg");
    }
    function updateHeading(headingDeg) {
      const h = cleanHeading(headingDeg);
      if (h === null) { applyArrow(currentHeading); previewUturn(); return false; }
      currentHeading = h;
      applyArrow(currentHeading);
      previewUturn();
      return true;
    }
    function setStatus(text, kind) {
      el.positionStatus.className = "status" + (kind ? " " + kind : "");
      el.positionStatus.innerHTML = text;
    }
    function setCurrentPosition(lat, lon, headingDeg, detail) {
      const previous = currentPosition;
      currentPosition = [lat, lon];
      trail.push(currentPosition);
      while (trail.length > 160) trail.shift();
      trailLayer.setLatLngs(trail);
      machineMarker.setLatLng(currentPosition);
      if (!map.hasLayer(machineMarker)) machineMarker.addTo(map);
      if (!updateHeading(headingDeg) && previous) updateHeading(movementHeading(previous, currentPosition));
      toFront();
      const detailText = headingDeg === null && currentHeading !== null
        ? detail.replace("hdg=-", "hdg=" + currentHeading.toFixed(1) + " tracked") : detail;
      setStatus(lat.toFixed(7) + ", " + lon.toFixed(7) + "<br>" + detailText, "ok");
      el.machineCount.textContent = lat.toFixed(4) + ", " + lon.toFixed(4);
      if (!centeredOnce && !route.length) {
        centeredOnce = true;
        map.invalidateSize(true);
        map.setView(currentPosition, Math.max(map.getZoom(), 18), { animate: false });
      }
    }
    async function pollOnce(center) {
      const res = await fetch("/state?api=" + encodeURIComponent(el.apiUrl.value), { cache: "no-store" });
      if (!res.ok) throw new Error("HTTP " + res.status);
      const state = await res.json();
      const pos = state.position || {};
      if (pos.lat === null || pos.lon === null || pos.lat === undefined || pos.lon === undefined) {
        updateHeading(cleanHeading(pos.heading_deg));
        setStatus("API online=" + (state.online ? "Y" : "-") + ", no position.");
        el.machineCount.textContent = "no fix";
        return;
      }
      const heading = cleanHeading(pos.heading_deg);
      setCurrentPosition(Number(pos.lat), Number(pos.lon), heading,
        "online=" + (state.online ? "Y" : "-") +
        " " + (heading === null ? "hdg=-" : "hdg=" + heading.toFixed(1)) +
        " speed=" + Number(pos.speed_kph || 0).toFixed(1) + " kph");
      if (center) map.setView(currentPosition, Math.max(map.getZoom(), 18), { animate: false });
    }
    async function pollLoop() {
      if (!polling) return;
      try {
        await pollOnce(false);
        el.livePill.textContent = "LIVE";
        el.livePill.classList.remove("off");
      } catch (err) {
        setStatus("API read failed: " + err.message, "bad");
        el.livePill.textContent = "OFF";
        el.livePill.classList.add("off");
        el.machineCount.textContent = "offline";
      }
      pollTimer = setTimeout(pollLoop, pollIntervalMs);
    }

    // ----------------------------------------------------------------- wiring
    document.querySelectorAll(".tool").forEach((b) => b.addEventListener("click", () => setMode(b.dataset.mode)));
    el.cardPoints.addEventListener("toggle", renderPointList);
    el.cardJson.addEventListener("toggle", renderJSON);

    map.on("click", (ev) => {
      if (mode === "draw") { appendPoint(ev.latlng); return; }
      if (mode === "uturn") { insertUturn([ev.latlng.lat, ev.latlng.lng]); return; }
      selectPoint(null);
    });
    routeLayer.on("click", (ev) => {
      if (mode !== "select") return;
      L.DomEvent.stopPropagation(ev);
      insertOnSegment(ev.latlng);
    });

    document.addEventListener("keydown", (ev) => {
      const meta = ev.ctrlKey || ev.metaKey;
      const key = ev.key;
      if (meta && key.toLowerCase() === "s") { ev.preventDefault(); save(false); return; }
      if (meta && key.toLowerCase() === "z") { ev.preventDefault(); if (ev.shiftKey) redo(); else undo(); return; }
      if (meta && key.toLowerCase() === "y") { ev.preventDefault(); redo(); return; }
      const tag = ev.target && ev.target.tagName;
      if (tag === "INPUT" || tag === "TEXTAREA" || tag === "SELECT") return;
      if (key === "Escape") { setMode("select"); selectPoint(null); return; }
      if (key === "Delete" || key === "Backspace") { ev.preventDefault(); deleteSelected(); return; }
      if (key === "v" || key === "V") { setMode("select"); return; }
      if (key === "d" || key === "D") { setMode("draw"); return; }
      if (key === "u" || key === "U") { setMode("uturn"); return; }
      if (key === "f" || key === "F") { fitAll(); return; }
      if (key === "Enter" && mode === "uturn") { insertUturn(); return; }
      if (key.indexOf("Arrow") === 0) { ev.preventDefault(); nudge(key, ev.shiftKey); }
    });
    window.addEventListener("beforeunload", (ev) => {
      if (!isDirty()) return;
      ev.preventDefault();
      ev.returnValue = "";
    });

    el.saveBtn.onclick = () => save(false);
    el.undoBtn.onclick = undo;
    el.redoBtn.onclick = redo;
    el.autosave.onchange = () => {
      if (el.autosave.checked) { toast("Autosave on — writes " + FILE_NAMES[el.target.value] + " as you edit"); scheduleAutosave(); }
      else { clearTimeout(autosaveTimer); toast("Autosave off"); }
    };
    el.target.onchange = async () => {
      if (isDirty() && !confirm("Discard unsaved changes and open " + FILE_NAMES[el.target.value] + "?")) {
        el.target.value = loadedTarget || el.target.value;
        return;
      }
      await loadTarget(el.target.value, false).catch((err) => toast("Load failed: " + err.message, "bad"));
    };
    el.pollBtn.onclick = async () => {
      polling = !polling;
      el.pollBtn.textContent = polling ? "Pause" : "Resume";
      el.pollBtn.classList.toggle("on", polling);
      if (pollTimer) clearTimeout(pollTimer);
      if (polling) { await pollOnce(true).catch((err) => setStatus(err.message, "bad")); pollLoop(); }
    };
    el.mapLightBtn.onclick = () => setBaseMap("light");
    el.mapSatelliteBtn.onclick = () => setBaseMap("satellite");
    el.centerBtn.onclick = () => currentPosition
      ? map.setView(currentPosition, Math.max(map.getZoom(), 18), { animate: false })
      : toast("No machine position yet", "bad");
    el.fitBtn.onclick = fitAll;
    el.insertUturnBtn.onclick = () => insertUturn();
    el.uturnRadius.oninput = previewUturn;
    el.uturnSide.onchange = previewUturn;
    el.uturnPoints.oninput = previewUturn;
    el.turnContinue.oninput = previewUturn;
    el.cardUturn.addEventListener("toggle", previewUturn);

    function syncPresets() {
      const angle = Number(el.turnAngle.value);
      document.querySelectorAll(".preset").forEach((b) => b.classList.toggle("on", Number(b.dataset.angle) === angle));
    }
    document.querySelectorAll(".preset").forEach((b) => {
      b.addEventListener("click", () => {
        el.turnAngle.value = b.dataset.angle;
        syncPresets();
        previewUturn();
      });
    });
    el.turnAngle.oninput = () => { syncPresets(); previewUturn(); };
    el.addCurrentBtn.onclick = () => currentPosition
      ? appendPoint({ lat: currentPosition[0], lng: currentPosition[1] })
      : toast("No machine position yet", "bad");
    el.reverseBtn.onclick = () => {
      if (route.length < 2) return;
      const old = selectedIndex;
      route.reverse();
      selectedIndex = old === null ? null : route.length - 1 - old;
      commit();
      toast("Route reversed");
    };
    el.clearBtn.onclick = () => {
      if (route.length && !confirm("Clear all " + route.length + " points? (Ctrl+Z undoes this)")) return;
      route.length = 0; trail.length = 0; selectedIndex = null;
      trailLayer.setLatLngs(trail);
      commit();
    };
    el.applyPointBtn.onclick = () => {
      if (selectedIndex === null) return;
      const lat = Number(el.selectedLat.value), lon = Number(el.selectedLon.value);
      if (!Number.isFinite(lat) || !Number.isFinite(lon) || lat < -90 || lat > 90 || lon < -180 || lon > 180) {
        toast("Invalid coordinates", "bad"); return;
      }
      route[selectedIndex] = [lat, lon];
      commit();
      map.panTo(route[selectedIndex], { animate: false });
    };
    el.deletePointBtn.onclick = deleteSelected;
    el.revertBtn.onclick = async () => {
      if (isDirty() && !confirm("Discard unsaved changes and reload from disk?")) return;
      await loadTarget(el.target.value, false).catch((err) => toast("Load failed: " + err.message, "bad"));
    };
    el.importBtn.onclick = () => {
      try {
        const pts = parseGeoJSON(el.geojsonText.value);
        replaceRoute(pts);
        render();
        toast("Imported " + pts.length + " points", "ok");
      } catch (err) { toast("Import failed: " + err.message, "bad"); }
    };
    el.copyBtn.onclick = async () => {
      await navigator.clipboard.writeText(JSON.stringify(geojson(), null, 2));
      toast("GeoJSON copied", "ok");
    };
    el.downloadBtn.onclick = () => {
      const blob = new Blob([JSON.stringify(geojson(), null, 2) + "\n"], { type: "application/geo+json" });
      const link = document.createElement("a");
      link.href = URL.createObjectURL(blob);
      link.download = FILE_NAMES[el.target.value];
      link.click();
      URL.revokeObjectURL(link.href);
    };
    el.formatBtn.onclick = () => {
      try {
        el.geojsonText.value = JSON.stringify(JSON.parse(el.geojsonText.value), null, 2);
        toast("JSON formatted", "ok");
      } catch (err) { toast("Format failed: " + err.message, "bad"); }
    };

    // ------------------------------------------------------------------- boot
    setMode("select");
    resetHistory();
    render();
    loadTarget(el.target.value, true).catch(() => {});
    // center=false: the machine must not yank the view away from the route you opened.
    pollOnce(false)
      .catch((err) => { setStatus("API read failed: " + err.message, "bad"); el.machineCount.textContent = "offline"; })
      .finally(() => pollLoop());
  </script>
</body>
</html>
"""


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Serve the AutoDrive route maker web UI.")
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


def write_route(path_out: Path, coords: list[list[float]]) -> None:
    """Write the route file atomically, keeping the previous version as .bak.

    This is the file 08_stream_waypoints.py drives from. A truncating in-place
    write (the old behaviour) leaves it corrupt if we die mid-write.
    """
    payload = json.dumps(feature_collection(coords), indent=2) + "\n"
    if path_out.exists():
        path_out.with_suffix(path_out.suffix + ".bak").write_text(path_out.read_text())
    tmp = path_out.with_name(path_out.name + ".tmp")
    tmp.write_text(payload)
    os.replace(tmp, path_out)


def make_handler(default_output: str, upstream_api: str, api_timeout_s: float):
    class RouteEditorHandler(BaseHTTPRequestHandler):
        server_version = "AutoDriveRouteMaker/1.0"

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
                write_route(path_out, coords)
            except Exception as exc:
                self._send_json({"error": str(exc)}, HTTPStatus.BAD_REQUEST)
                return
            self._send_json({"ok": True, "path": str(path_out), "points": len(coords)})

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
                # No Access-Control-Allow-Origin: the UI is served from this same
                # origin, so it does not need CORS — and without it a random page
                # in the operator's browser cannot POST /save and rewrite the route
                # file the machine is about to drive.
                self.send_header("Cache-Control", "no-store")
                self.send_header("Content-Type", content_type)
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)
            except BrokenPipeError:
                pass

    return RouteEditorHandler


def main() -> None:
    args = parse_args()
    if args.api_timeout <= 0:
        raise SystemExit("--api-timeout must be greater than zero")
    server = ThreadingHTTPServer(
        (args.host, args.port),
        make_handler(args.output, args.api, args.api_timeout),
    )
    url = f"http://{args.host}:{args.port}"
    print("", file=sys.stderr)
    print("  ┌──────────────────────────────────────────────┐", file=sys.stderr)
    print("  │  OPEN THIS IN YOUR BROWSER:                  │", file=sys.stderr)
    print(f"  │  {url:<43}│", file=sys.stderr)
    print("  └──────────────────────────────────────────────┘", file=sys.stderr)
    print("", file=sys.stderr)
    print(f"  machine position from : {args.api}", file=sys.stderr)
    print(f"  saves to              : {', '.join(SAVE_TARGETS.values())}", file=sys.stderr)
    print("  Ctrl+C to stop", file=sys.stderr)
    print("", file=sys.stderr)

    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nstopping", file=sys.stderr)
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
