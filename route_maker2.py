#!/usr/bin/env python3
"""
route_maker2.py — route editor + live waypoint streaming to the machine.

route_maker.py, plus a Play/Stop control that streams the route to the Display
over CAN and draws what the stream is actually doing on the map:

    ./route_maker2.py --can-bus can0 --api http://172.30.0.137:8080/state

The streaming logic is NOT reimplemented here. This module imports send_points.py
and drives it — sp.stream_next_window, sp.estimate_progress_index, sp.send_adjob.
That is deliberate: this repo already has five near-identical copies of the
streaming loop that have drifted apart (and grown different bugs), and a sixth
would be worse than none. Change the protocol behaviour in send_points.py and it
changes here too.

While playing, the map shows the interpolated route split into:

    done      — points the machine has passed (GPS progress index)
    sent      — the window currently on the Display
    overlap   — the tail of that window that the NEXT batch will re-send
    pending   — points not yet streamed
    trigger   — the index that fires the next batch

Press Stop and it all disappears; you are back in the editor.

*** THIS TRANSMITS ON THE CAN BUS. *** Once enough points are on the Display it
raises RunCommand, and AutoDrive takes over the steering (per spec/spec2.md the
operator still supplies forward motion). Stop sends ADJOB systemActive=false.

Single file on purpose: the UI is plain HTML/CSS/JS inlined below. No build
step, no package manager, no node_modules — edit the HTML string and reload.

route_maker.py (editor only, never touches CAN) and route_viewer.py (the
original) are both kept alongside, untouched.
"""

from __future__ import annotations

import argparse
import json
import math
import os
import sys
import threading
import time
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.error import URLError
from urllib.parse import parse_qs, urlparse
from urllib.request import urlopen

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import autodrive as a
import routes
import send_points as sp

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
    /* an explicit display: (flex/grid) otherwise overrides the `hidden` attribute */
    [hidden]{display:none!important}
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

    /* ---- scrolling body ----
       flex column, NOT grid: as a grid, .card{overflow:hidden} zeroes each card's
       min-content contribution, so a definite-height grid container compresses the
       rows instead of scrolling and tall cards get their contents cut off. */
    .scroll{flex:1;min-height:0;overflow-y:auto;overflow-x:hidden;padding:12px 14px 16px;
      display:flex;flex-direction:column;gap:10px}
    .scroll::-webkit-scrollbar{width:8px}
    .scroll::-webkit-scrollbar-thumb{background:var(--line-2);border-radius:99px}

    .card{flex:0 0 auto;background:var(--card);border:1px solid var(--line);border-radius:12px;overflow:hidden}
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
    /* Side toggle: one click flips Left <-> Right (was a dropdown) */
    .sidetoggle{width:100%;padding:8px 9px;border-radius:8px;font-size:12px;font-weight:800;cursor:pointer;
      background:var(--card-2);border:1px solid var(--line-2);color:var(--ink);text-align:center;letter-spacing:.02em;
      white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
    .sidetoggle[data-side=left]{border-color:rgba(143,210,228,.4);color:#dff2f9;background:rgba(143,210,228,.12)}
    .sidetoggle[data-side=right]{border-color:rgba(230,127,78,.42);color:#ffc7a8;background:rgba(230,127,78,.12)}
    .sidetoggle[data-side=straight]{border-color:rgba(83,214,137,.42);color:#9ff0bb;background:rgba(83,214,137,.12)}
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
    /* Select/Draw ride on the map, same pill as the basemap switch, top-centre.
       There is no Turn button: opening the Turn card in the sidebar arms it. */
    .modeswitch{position:absolute;top:12px;left:50%;transform:translateX(-50%);z-index:900}
    .seg{display:flex;background:rgba(13,20,24,.9);border:1px solid var(--line-2);border-radius:9px;
      overflow:hidden;backdrop-filter:blur(10px);box-shadow:0 8px 24px rgba(0,0,0,.3)}
    .seg button{padding:6px 11px;border:0;background:transparent;cursor:pointer;color:var(--muted);
      font-size:10px;font-weight:800;border-right:1px solid var(--line);
      display:inline-flex;align-items:center;gap:6px}
    .seg button:last-child{border-right:0}
    .seg button:hover{color:var(--ink)}
    .seg button.on{background:rgba(143,210,228,.2);color:#dff2f9}
    .seg button kbd{font-size:8px;padding:1px 3px;opacity:.75}
    /* the mode pill uses the tool accent (orange), the basemap pill stays teal */
    .modeswitch .seg button.on{background:rgba(230,127,78,.24);color:#ffd0b6;border-color:transparent}
    .modeswitch .seg button.on kbd{background:rgba(255,208,182,.16);border-color:rgba(255,208,182,.28);color:#ffd0b6}
    /* Turn armed = the card is open; flag it on the summary since it has no button now */
    .card.armed{border-color:rgba(230,127,78,.4)}
    .card.armed > summary{color:#ffd0b6}
    .card.armed > summary:before{color:var(--orange)}

    /* ---- streaming ---- */
    .card.stream{border-color:rgba(143,210,228,.22)}
    .card.stream.live{border-color:rgba(83,214,137,.5);box-shadow:0 0 0 1px rgba(83,214,137,.18)}
    .card.stream.fault{border-color:rgba(239,154,130,.5)}
    .play{
      width:100%;padding:11px;border:0;border-radius:9px;cursor:pointer;
      font-size:12px;font-weight:800;letter-spacing:.04em;
      background:rgba(83,214,137,.18);border:1px solid rgba(83,214,137,.42);color:#9ff0bb;
      transition:background 130ms ease
    }
    .play:hover:not(:disabled){background:rgba(83,214,137,.28)}
    .play:disabled{opacity:.4;cursor:not-allowed}
    .play.stop{background:rgba(217,65,65,.2);border-color:rgba(217,65,65,.55);color:#ff9c9c}
    .play.stop:hover{background:rgba(217,65,65,.32)}
    .chk{display:flex;align-items:center;gap:7px;font-size:9px;color:var(--dim);cursor:pointer}
    .chk input{width:13px;height:13px;margin:0;accent-color:var(--teal);cursor:pointer}
    .gates{display:flex;gap:4px}
    .gates span{
      flex:1;text-align:center;padding:4px 0;border-radius:5px;font:8px/1.4 var(--mono);font-weight:800;
      background:var(--card-2);border:1px solid var(--line);color:var(--dim);letter-spacing:.04em
    }
    .gates span.on{background:rgba(83,214,137,.18);border-color:rgba(83,214,137,.4);color:#9ff0bb}
    .gates span.warn{background:rgba(230,127,78,.18);border-color:rgba(230,127,78,.42);color:#ffd0b6}
    .log{
      margin:0;max-height:112px;overflow:auto;padding:7px 8px;border-radius:8px;
      background:#0c1216;border:1px solid var(--line);
      font:9px/1.5 var(--mono);color:var(--muted);white-space:pre-wrap;word-break:break-word
    }
    .legend{
      position:absolute;bottom:14px;left:14px;z-index:900;display:grid;gap:5px;
      padding:9px 11px;border-radius:10px;font:9px/1.3 var(--mono);color:var(--muted);
      background:rgba(13,20,24,.92);border:1px solid var(--line-2);backdrop-filter:blur(10px);
      box-shadow:0 8px 24px rgba(0,0,0,.32)
    }
    .legend span{display:flex;align-items:center;gap:7px;white-space:nowrap}
    .legend .sw{width:16px;height:3px;border-radius:2px;flex:0 0 auto}
    .legend .sw.done{background:#53d689}
    .legend .sw.sent{background:#8fd2e4}
    .legend .sw.overlap{background:#e6c14e;height:7px}
    .legend .sw.pending{background:#8fa3ac;opacity:.6}
    .legend .sw.trig{width:9px;height:9px;border-radius:999px;background:transparent;border:2px solid #e67f4e}
    /* while streaming the editor is read-only — say so, do not just silently ignore clicks */
    body.streaming #map{cursor:not-allowed}

    /* ---- top-level PLAN | EXECUTE ---- */
    .modetabs{display:grid;grid-template-columns:1fr 1fr;gap:6px;padding:10px 14px 4px}
    .modetab{
      padding:10px;border-radius:9px;cursor:pointer;font-size:12px;font-weight:800;letter-spacing:.06em;
      background:var(--card);border:1px solid var(--line-2);color:var(--muted);
      transition:background 120ms ease,color 120ms ease,border-color 120ms ease
    }
    .modetab:hover{color:var(--ink)}
    .modetab.on[data-app="plan"]{background:rgba(143,210,228,.2);border-color:rgba(143,210,228,.42);color:#dff2f9}
    .modetab.on[data-app="execute"]{background:rgba(83,214,137,.2);border-color:rgba(83,214,137,.46);color:#9ff0bb}
    .modetab:disabled{opacity:.4;cursor:not-allowed}
    .appview{flex:1;min-height:0;display:flex;flex-direction:column}

    /* ---- Select / Draw, back inside the Plan panel (Turn arms via its own card) ---- */
    .tools{display:grid;grid-template-columns:repeat(2,1fr);gap:0;margin:8px 14px 0;
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

    /* ---- Execute header ---- */
    .exec-file{padding:2px 0 2px;display:grid;gap:8px}
    .exec-file-row{display:flex;align-items:baseline;justify-content:space-between;gap:10px;
      padding:9px 11px;border-radius:10px;background:var(--card);border:1px solid var(--line-2)}
    .exec-label{font-size:9px;font-weight:800;letter-spacing:.12em;text-transform:uppercase;color:var(--dim)}
    .exec-file-row b{font:12px/1 var(--mono);font-weight:700;color:var(--ink)}
    .exec-warn{padding:8px 10px;border-radius:8px;font-size:10px;
      background:rgba(230,127,78,.14);border:1px solid rgba(230,127,78,.36);color:#ffd0b6}
    .exec-warn b{color:#ffd9c4}

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

      <div class="modetabs">
        <button class="modetab on" data-app="plan" type="button">&#9998;&nbsp; Plan</button>
        <button class="modetab" data-app="execute" type="button">&#9654;&nbsp; Execute</button>
      </div>

      <section class="appview" id="view-plan">
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
              <button class="btn preset on" data-angle="90" type="button">90&deg;</button>
              <button class="btn preset" data-angle="180" type="button">180&deg;</button>
              <input id="turnAngle" type="number" min="5" max="180" step="5" value="90">
            </div>
            <div class="r3">
              <label>Radius m <input id="uturnRadius" type="number" min="0.5" max="500" step="0.5" value="12"></label>
              <label>Side
                <button type="button" id="uturnSide" class="sidetoggle" data-side="left">Left &#8592;</button>
              </label>
              <label>Pts/180&deg; <input id="uturnPoints" type="number" min="6" max="96" step="1" value="18"></label>
            </div>
            <label>Continue straight after the turn, m
              <input id="turnContinue" type="number" min="0" max="5000" step="1" value="0">
            </label>
            <div id="turnStats" class="status">--</div>
            <button id="insertUturnBtn" class="btn wide">Insert at selection</button>
            <p class="hint">The ghost turn always trails the working point. <kbd>U</kbd> cycles it
              left&nbsp;/&nbsp;right&nbsp;/&nbsp;straight (or <kbd>L</kbd>/<kbd>R</kbd>/<kbd>S</kbd>);
              clicking drops points, <kbd>I</kbd> inserts the turn.
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
      </section>

      <section class="appview" id="view-execute" hidden>
        <div class="scroll">
          <div class="exec-file">
            <div class="exec-file-row">
              <span class="exec-label">Streaming file</span>
              <b id="execFile">line.geojson</b>
            </div>
            <div id="execDirty" class="exec-warn" hidden>Unsaved edits in <b>Plan</b> — save first; the machine streams the file on disk.</div>
          </div>
          <div class="card stream" id="cardStream">
            <div class="card-head">Stream to machine <span class="count" id="streamPhase">idle</span></div>
            <div class="card-body">
              <button id="playBtn" class="play">&#9654;&nbsp; Play</button>
              <div class="r3">
                <label>Window <input id="pWindow" type="number" min="1" max="2000" step="10" value="200"></label>
                <label>Trigger % <input id="pTrigger" type="number" min="1" max="100" step="5" value="70"></label>
                <label>Overlap <input id="pOverlap" type="number" min="0" max="500" step="1" value="3"></label>
              </div>
              <div class="r3">
                <label>Spacing m <input id="pSpacing" type="number" min="0.05" max="10" step="0.1" value="1.0"></label>
                <label>Ahead <input id="pAhead" type="number" min="1" max="5" step="1" value="5"></label>
                <label>Back <input id="pBack" type="number" min="0" max="100" step="1" value="6"></label>
              </div>
              <label class="chk"><input type="checkbox" id="pNoGate"> Skip inside-field gate</label>
              <div class="readout" id="streamReadout">
                <div><span>Progress</span><b id="sProgress">--</b></div>
                <div><span>Sent</span><b id="sSentUntil">--</b></div>
                <div><span>Next trigger</span><b id="sTrigger">--</b></div>
                <div><span>Overlap (re-sent)</span><b id="sOverlap">--</b></div>
                <div><span>Batches</span><b id="sBatches">--</b></div>
                <div><span>Frames</span><b id="sFrames">--</b></div>
              </div>
              <div id="streamGates" class="gates">
                <span data-gate="ppp">PPP</span><span data-gate="allowed">ALLOWED</span>
                <span data-gate="inside">INSIDE</span><span data-gate="run">RUN</span>
                <span data-gate="engaged">ENGAGED</span>
              </div>
              <div id="streamStatus" class="status">Idle — press Play to stream this route to the machine.</div>
              <pre id="streamLog" class="log"></pre>
            </div>
          </div>
        </div>
      </section>
    </aside>

    <main id="map">
      <div class="maptools">
        <div class="seg">
          <button id="mapLightBtn" class="on" type="button">Light</button>
          <button id="mapSatelliteBtn" type="button">Satellite</button>
        </div>
      </div>
      <div id="legend" class="legend" hidden>
        <span><i class="sw done"></i>done</span>
        <span><i class="sw sent"></i>on display</span>
        <span><i class="sw overlap"></i>overlap (re-sent)</span>
        <span><i class="sw pending"></i>not sent</span>
        <span><i class="sw trig"></i>trigger</span>
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
      select: "<b>Select</b> drag a point &middot; drag the line to move all &middot; click line to insert &middot; <kbd>U</kbd> turn L/R/straight &middot; <kbd>I</kbd> drop turn",
      draw: "<b>Draw</b> click to append a point &middot; <kbd>U</kbd> ghost L/R/straight &middot; <kbd>I</kbd> drop turn &middot; <kbd>Esc</kbd> done",
      uturn: "<b>Turn armed</b> click to drop a point (ghost follows) &middot; <kbd>U</kbd> cycles L/R/straight &middot; <kbd>I</kbd> insert &middot; <kbd>Esc</kbd> stop"
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

    // ---- streaming layers. Drawn only while playing, removed on stop. -------
    // Strong, saturated colours so they read on both the light and satellite basemaps.
    // Order matters: overlap sits UNDER sent so the amber halo reads as a highlight of
    // the re-sent tail rather than a separate line.
    const xBase    = L.polyline([], { color: "#31424b", weight: 3, opacity: .6, interactive: false });
    const xPending = L.polyline([], { color: "#9aa7ad", weight: 4, opacity: .8, dashArray: "3 7", interactive: false });
    const xOverlap = L.polyline([], { color: "#ffb020", weight: 15, opacity: .55, lineCap: "round", interactive: false });
    const xSent    = L.polyline([], { color: "#12b5d8", weight: 6, opacity: 1, lineCap: "round", interactive: false });
    const xDone    = L.polyline([], { color: "#1fd06a", weight: 6, opacity: 1, lineCap: "round", interactive: false });
    const xDots    = L.layerGroup();
    const xTrigger = L.circleMarker([0, 0], { radius: 9, color: "#ff6a2b", weight: 4, fill: true,
                                              fillColor: "#ff6a2b", fillOpacity: .25, interactive: false });
    const xHead    = L.circleMarker([0, 0], { radius: 8, color: "#ffffff", weight: 3, fillColor: "#1fd06a",
                                              fillOpacity: 1, interactive: false });
    const STREAM_LAYERS = [xBase, xPending, xOverlap, xSent, xDone, xDots, xTrigger, xHead];
    const MAX_DOTS = 6000;          // interpolated routes can be thousands of points
    let streamPoints = [];          // [[lat, lon, headland], ...] from /stream/route
    let streaming = false;
    let streamTimer = null;
    let markers = [];               // one draggable marker per route point, index-aligned
    // Exact end-tangent of the last inserted segment, so chained turns/legs start
    // from the true tangent (not the polyline's last chord, which drifts a few deg
    // per turn and makes the outgoing straights fan apart). {lat, lon, heading}.
    let lastInsert = null;

    const el = {};
    for (const id of ["apiUrl", "livePill", "pollBtn", "centerBtn", "mapLightBtn", "mapSatelliteBtn",
      "fitBtn", "insertUturnBtn", "uturnRadius", "uturnSide", "uturnPoints", "positionStatus",
      "addCurrentBtn", "reverseBtn", "clearBtn", "selectedLat", "selectedLon", "applyPointBtn",
      "deletePointBtn", "pointList", "pointCount", "routeLength", "maxGap", "selectedPoint",
      "target", "saveBtn", "saveLabel", "autosave", "undoBtn", "redoBtn", "revertBtn", "importBtn",
      "copyBtn", "downloadBtn", "formatBtn", "geojsonText", "toasts", "hintbar", "fileSummary",
      "selCount", "listCount", "machineCount", "cardPoints", "cardJson", "cardUturn",
      "turnAngle", "turnContinue", "turnStats", "turnCount",
      "cardStream", "playBtn", "streamPhase", "streamStatus", "streamLog", "streamGates",
      "legend", "pWindow", "pTrigger", "pOverlap", "pSpacing", "pAhead", "pBack", "pNoGate",
      "sProgress", "sSentUntil", "sTrigger", "sOverlap", "sBatches", "sFrames",
      "execFile", "execDirty"]) {
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
    // Select/Draw live on the map pill. Turn has no button — it is armed by opening
    // the Turn card, so `uturn` simply leaves both pill buttons unlit.
    function setMode(next) {
      mode = next;
      document.querySelectorAll(".tool").forEach((b) => b.classList.toggle("on", b.dataset.mode === mode));
      document.body.classList.toggle("draw", mode === "draw");
      document.body.classList.toggle("uturn", mode === "uturn");
      el.cardUturn.classList.toggle("armed", mode === "uturn");
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
      // If the working point is still the exact end of the last inserted segment,
      // chain off its true tangent instead of the drifting last chord.
      if (lastInsert && start && meters(start, [lastInsert.lat, lastInsert.lon]) < 0.05) return lastInsert.heading;
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
      const radius = clampNumber(el.uturnRadius.value, 0.5, 500, 12);
      const angle = clampNumber(el.turnAngle.value, 5, 180, 90);
      const per180 = Math.round(clampNumber(el.uturnPoints.value, 6, 96, 18));
      const runOn = clampNumber(el.turnContinue.value, 0, 5000, 0);
      const side = el.uturnSide.dataset.side;      // "left" | "right" | "straight"
      const steps = Math.max(3, Math.round(per180 * angle / 180));

      // Straight: a dead-ahead leg the SAME total length the curve would have had
      // (arc path length + run-on), so you can preview/lay a straight continuation.
      if (side === "straight") {
        const arcLen = radius * angle * Math.PI / 180;
        const legSteps = runOn > 0 ? Math.max(1, Math.round(runOn / Math.max(0.3, Math.PI * radius / per180))) : 0;
        return { pts: straightSegment(start, headingDeg, arcLen + runOn, steps + legSteps).pts, endHeading: norm360(headingDeg) };
      }

      const right = side === "right";
      const arc = arcSegment(start, headingDeg, radius, right, angle, steps);
      let pts = arc.pts;
      if (runOn > 0) {
        const spacing = Math.PI * radius / per180;      // match the arc's point density
        const legSteps = Math.max(1, Math.round(runOn / Math.max(0.3, spacing)));
        pts = pts.concat(straightSegment(arc.end, arc.heading, runOn, legSteps).pts.slice(1));
      }
      return { pts, endHeading: arc.heading };   // arc.heading = exact end tangent
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

    // The turn ghost is ALWAYS on while editing: it trails the working point so you
    // can always see where a left / right / straight continuation would land. It is
    // hidden only in the Execute view (or mid-stream), where the route is read-only.
    function previewUturn() {
      if (appMode === "execute" || streaming) {
        uturnPreview.setLatLngs([]);
        setTurnStats(null);
        return;
      }
      let start = null, index = selectedIndex;
      if (index !== null && route[index]) start = route[index];
      else if (currentPosition) start = currentPosition;
      else if (route.length) { start = route[route.length - 1]; index = route.length - 1; }
      if (!start) { uturnPreview.setLatLngs([]); setTurnStats(null); return; }
      const pts = buildUturn(start, headingForUturn(start, index)).pts;
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
      const built = buildUturn(start, headingForUturn(start, index));
      const arc = built.pts;
      if (route.length && includeStart && meters(route[route.length - 1], start) < 0.3) includeStart = false;
      const points = includeStart ? arc : arc.slice(1);
      route.splice(insertAt, 0, ...points);
      selectedIndex = insertAt + points.length - 1;
      // Remember this segment's exact end tangent so the next insert chains off it
      // and the outgoing straights stay perfectly parallel.
      lastInsert = { lat: route[selectedIndex][0], lon: route[selectedIndex][1], heading: built.endHeading };
      commit();
      const q = turnQuality(arc);
      toast(Math.round(clampNumber(el.turnAngle.value, 5, 180, 90)) + "° turn inserted — " +
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
      markers = [];
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
        markers[index] = marker;
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

    // =====================================================================
    // STREAMING
    // =====================================================================
    const PHASE_TEXT = {
      idle: "Idle. Save the route first, then Play.",
      arming: "Arming — sending ADJOB, waiting for the Display to return a DSAP anchor.",
      streaming: "Streaming.",
      done: "Route complete.",
      stopped: "Stopped. Job stood down (ADJOB systemActive=false).",
      error: "Error."
    };

    // Called only inside Execute mode. The interpolated preview (xBase/xPending/xDots)
    // is already on the map; this only adds/removes the LIVE bands and locks the panel.
    function setStreamingUI(on) {
      streaming = on;
      document.body.classList.toggle("streaming", on);
      el.legend.hidden = !on;
      el.playBtn.classList.toggle("stop", on);
      el.playBtn.innerHTML = on ? "&#9632;&nbsp; Stop" : "&#9654;&nbsp; Play";
      [el.pWindow, el.pTrigger, el.pOverlap, el.pSpacing, el.pAhead, el.pBack, el.pNoGate]
        .forEach((i) => { i.disabled = on; });
      // Can't leave Execute mid-run.
      document.querySelectorAll(".modetab").forEach((b) => { if (b.dataset.app === "plan") b.disabled = on; });

      if (on) {
        [xOverlap, xSent, xDone, xTrigger, xHead].forEach((l) => l.addTo(map));
      } else {
        [xOverlap, xSent, xDone, xTrigger, xHead].forEach((l) => { if (map.hasLayer(l)) map.removeLayer(l); });
        el.cardStream.classList.remove("live", "fault");
        // Back to the clean preview: every point pending again.
        if (streamPoints.length) xPending.setLatLngs(streamPoints.map((p) => [p[0], p[1]]));
      }
      toFront();
    }

    // The interpolated points that actually go on the wire — NOT the vertices you drew.
    // Each is a visible dot with a dark ring so it reads on any basemap; headland pts orange.
    function drawStreamDots(points) {
      streamPoints = points;
      xBase.setLatLngs(points.map((p) => [p[0], p[1]]));
      xDots.clearLayers();
      if (points.length > MAX_DOTS) {
        toast(points.length + " interpolated pts — dots hidden above " + MAX_DOTS);
      } else {
        points.forEach((p) => {
          L.circleMarker([p[0], p[1]], {
            radius: p[2] ? 4 : 3, interactive: false,
            color: "#0d1418", weight: 1.2,                 // dark ring = visible on light + satellite
            fillColor: p[2] ? "#ff8a3d" : "#eaf4f8", fillOpacity: 1
          }).addTo(xDots);
        });
      }
      const b = L.latLngBounds(points.map((p) => [p[0], p[1]]));
      map.fitBounds(b, { padding: [50, 50], maxZoom: 20, animate: false });
    }

    async function fetchStreamRoute() {
      const res = await fetch("/stream/route", { cache: "no-store" });
      const body = await res.json();
      if (!body.points || !body.points.length) return false;
      drawStreamDots(body.points);
      return true;
    }

    // Preview: what WILL be streamed, straight from the file, before you press Play.
    // This is what makes the interpolated points visible the moment you enter Execute.
    async function fetchPreview() {
      const q = "?route=" + encodeURIComponent(el.target.value) +
                "&spacing=" + encodeURIComponent(Number(el.pSpacing.value) || 1.0);
      const res = await fetch("/stream/preview" + q, { cache: "no-store" });
      const body = await res.json();
      if (!res.ok) { toast(body.error || "Preview failed", "bad"); return false; }
      drawStreamDots(body.points);
      xDone.setLatLngs([]); xSent.setLatLngs([]); xOverlap.setLatLngs([]);
      xPending.setLatLngs(body.points.map((p) => [p[0], p[1]]));   // all pending until Play
      if (map.hasLayer(xTrigger)) map.removeLayer(xTrigger);
      if (map.hasLayer(xHead)) map.removeLayer(xHead);
      toast(body.points.length + " interpolated pts @ " + Number(el.pSpacing.value).toFixed(2) + " m", "ok");
      return true;
    }

    function renderStreamLayers(st) {
      const P = streamPoints;
      const n = P.length;
      if (n < 2) return;
      const clamp = (i) => Math.max(0, Math.min(n - 1, i));
      const seg = (i, j) => {
        const from = clamp(i), to = clamp(j);
        return to <= from ? [] : P.slice(from, to + 1).map((p) => [p[0], p[1]]);
      };
      const prog = clamp(st.progress);
      const sentUntil = clamp(st.sent_until - 1);
      const winEnd = clamp(st.window_end - 1);
      const nextStart = clamp(st.next_window_start);

      xDone.setLatLngs(seg(0, prog));                      // machine has driven these
      xSent.setLatLngs(seg(prog, sentUntil));              // on the Display, ahead of the machine
      // Amber = [next batch start .. current window end]: the points the next batch re-sends
      // (send_points.py's overlap region). Sits under the teal so it reads as a highlight.
      xOverlap.setLatLngs(st.phase === "streaming" && st.overlap_count > 0 ? seg(nextStart, winEnd) : []);
      xPending.setLatLngs(seg(sentUntil, n - 1));          // never streamed yet
      xHead.setLatLng(P[prog]);
      if (!map.hasLayer(xHead)) xHead.addTo(map);
      if (st.phase === "streaming" && st.window_end < n) {
        xTrigger.setLatLng(P[clamp(st.trigger_index)]);
        if (!map.hasLayer(xTrigger)) xTrigger.addTo(map);
      } else if (map.hasLayer(xTrigger)) {
        map.removeLayer(xTrigger);
      }
      toFront();
    }

    function renderStreamPanel(st) {
      const live = st.phase === "streaming" || st.phase === "arming";
      const fault = st.phase === "error";
      el.cardStream.classList.toggle("live", live);
      el.cardStream.classList.toggle("fault", fault);
      el.streamPhase.textContent = st.phase;
      el.streamStatus.className = "status" + (fault ? " bad" : (st.phase === "streaming" ? " ok" : ""));
      el.streamStatus.textContent = (PHASE_TEXT[st.phase] || st.phase) + (st.message && fault ? " " + st.message : "");

      const n = st.total || 0;
      el.sProgress.textContent = n ? st.progress + " / " + (n - 1) : "--";
      el.sSentUntil.textContent = n ? st.sent_until + " / " + n : "--";
      el.sTrigger.textContent = st.phase === "streaming"
        ? (st.window_end < n ? String(st.trigger_index) : "done") : "--";
      el.sOverlap.textContent = st.phase === "streaming" ? st.overlap_count + " pts" : "--";
      el.sBatches.textContent = String(st.batches);
      el.sFrames.textContent = String(st.frames);

      const gates = { ppp: st.ppp, allowed: st.allowed, inside: st.inside || el.pNoGate.checked,
                      run: st.run_command, engaged: st.engaged };
      el.streamGates.querySelectorAll("[data-gate]").forEach((g) => {
        g.classList.toggle("on", Boolean(gates[g.dataset.gate]));
      });
      if (st.reject) {
        el.streamGates.querySelector('[data-gate="engaged"]').classList.add("warn");
      }
      el.streamLog.textContent = (st.log || []).slice(-14).join("\n");
      el.streamLog.scrollTop = el.streamLog.scrollHeight;
    }

    async function streamPoll() {
      try {
        const res = await fetch("/stream/state", { cache: "no-store" });
        const st = await res.json();
        if (!streamPoints.length) await fetchStreamRoute();
        renderStreamPanel(st);
        if (streamPoints.length) renderStreamLayers(st);

        if (st.phase === "done" || st.phase === "stopped" || st.phase === "error") {
          clearTimeout(streamTimer);
          streamTimer = null;
          const kind = st.phase === "error" ? "bad" : "ok";
          toast(st.phase === "done" ? "Route complete" :
                st.phase === "error" ? "Stream error: " + st.message : "Stream stopped", kind);
          // Leave the final picture up for a beat, then hand the editor back.
          setTimeout(() => { if (!streaming) return; setStreamingUI(false); }, 2500);
          return;
        }
      } catch (err) {
        toast("Stream poll failed: " + err.message, "bad");
      }
      streamTimer = setTimeout(streamPoll, 200);
    }

    async function startStream() {
      if (isDirty()) { toast("Save the route before streaming it", "bad"); return; }
      if (route.length < 2) { toast("Nothing to stream", "bad"); return; }
      const target = el.target.value;
      const bus = el.cardStream.dataset.canBus || "?";
      if (!confirm(
        "Stream " + FILE_NAMES[target] + " to the machine on " + bus + "?\n\n" +
        "This transmits on the CAN bus. Once enough points are on the Display, RunCommand " +
        "goes high and AutoDrive takes over the STEERING.\n\n" +
        "Area clear? Hand on the e-stop?")) return;

      const body = {
        route: target,
        window_size: Number(el.pWindow.value),
        trigger_fraction: Number(el.pTrigger.value) / 100,
        overlap_offset: Number(el.pOverlap.value),
        max_spacing: Number(el.pSpacing.value),
        nearest_ahead: Number(el.pAhead.value),
        nearest_backtrack: Number(el.pBack.value),
        no_inside_gate: el.pNoGate.checked,
      };
      try {
        const res = await fetch("/stream/start", {
          method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) });
        const out = await res.json();
        if (!res.ok) { toast(out.error || "Start failed", "bad"); return; }
        setStreamingUI(true);
        toast("Streaming " + FILE_NAMES[target], "ok");
        streamPoll();
      } catch (err) {
        toast("Start failed: " + err.message, "bad");
      }
    }

    async function stopStream() {
      try {
        await fetch("/stream/stop", { method: "POST" });
        toast("Stopping — standing the job down");
      } catch (err) {
        toast("Stop failed: " + err.message, "bad");
      }
    }

    el.playBtn.onclick = () => (streaming ? stopStream() : startStream());

    // ---- top-level Plan / Execute -------------------------------------------
    let appMode = "plan";
    function setAppMode(next) {
      if (next === appMode) return;
      if (appMode === "execute" && streaming) { toast("Stop the stream before leaving Execute", "bad"); return; }
      if (next === "execute" && isDirty()) { el.execDirty.hidden = false; }
      appMode = next;
      document.querySelectorAll(".modetab").forEach((b) => b.classList.toggle("on", b.dataset.app === appMode));
      document.getElementById("view-plan").hidden = appMode !== "plan";
      document.getElementById("view-execute").hidden = appMode !== "execute";
      document.body.classList.toggle("exec-mode", appMode === "execute");

      if (appMode === "execute") {
        // Show the interpolated points that WILL stream, straight away.
        el.execFile.textContent = FILE_NAMES[el.target.value];
        el.execDirty.hidden = !isDirty();
        map.removeLayer(pointLayer);
        routeLayer.setStyle({ opacity: .12 }); casing.setStyle({ opacity: .12 });
        [xBase, xPending, xDots].forEach((l) => l.addTo(map));
        fetchPreview().catch((err) => toast("Preview failed: " + err.message, "bad"));
      } else {
        STREAM_LAYERS.forEach((l) => { if (map.hasLayer(l)) map.removeLayer(l); });
        xDots.clearLayers(); streamPoints = [];
        if (!map.hasLayer(pointLayer)) pointLayer.addTo(map);
        routeLayer.setStyle({ opacity: .97 }); casing.setStyle({ opacity: .9 });
        el.hintbar.innerHTML = HINTS[mode];
        render();
      }
      setTimeout(() => map.invalidateSize(true), 50);
    }
    document.querySelectorAll(".modetab").forEach((b) => b.addEventListener("click", () => setAppMode(b.dataset.app)));
    // Re-preview when the spacing that drives interpolation changes, while in Execute.
    el.pSpacing.addEventListener("change", () => { if (appMode === "execute" && !streaming) fetchPreview().catch(() => {}); });

    // ----------------------------------------------------------------- wiring
    // Picking Select or Draw disarms the Turn tool by closing its card.
    document.querySelectorAll(".tool").forEach((b) => b.addEventListener("click", () => {
      el.cardUturn.open = false;
      setMode(b.dataset.mode);
    }));
    el.cardPoints.addEventListener("toggle", renderPointList);
    el.cardJson.addEventListener("toggle", renderJSON);

    map.on("click", (ev) => {
      if (streaming) return;                 // read-only while the machine is driving this route
      if (mode === "draw") { appendPoint(ev.latlng); return; }
      // Turn tool armed: a plain click just drops a normal point (and the ghost
      // arc follows it). The arc is only committed when you press I / Enter.
      if (mode === "uturn") { appendPoint(ev.latlng); return; }
      selectPoint(null);
    });
    // Click on the line inserts a point; press-and-drag on the line moves the
    // whole route. We start a drag on mousedown and only treat it as a move once
    // the pointer travels past a small threshold, so a plain click still inserts.
    let routeDrag = null;
    function onRouteDragMove(ev) {
      if (!routeDrag) return;
      const dLat = ev.latlng.lat - routeDrag.origin.lat;
      const dLng = ev.latlng.lng - routeDrag.origin.lng;
      if (!routeDrag.moved) {
        const px = map.latLngToLayerPoint(ev.latlng).distanceTo(routeDrag.originPx);
        if (px < 4) return;                  // ignore sub-4px jitter: this is still a click
        routeDrag.moved = true;
      }
      for (let i = 0; i < route.length; i++) {
        route[i] = [routeDrag.base[i][0] + dLat, routeDrag.base[i][1] + dLng];
        if (markers[i]) markers[i].setLatLng(route[i]);
      }
      casing.setLatLngs(route);
      routeLayer.setLatLngs(route);
      previewUturn();
    }
    function onRouteDragEnd() {
      if (!routeDrag) return;
      const moved = routeDrag.moved;
      routeDrag = null;
      map.off("mousemove", onRouteDragMove);
      map.off("mouseup", onRouteDragEnd);
      map.dragging.enable();
      if (moved) { suppressLineClick = true; commit(); }
    }
    let suppressLineClick = false;
    routeLayer.on("mousedown", (ev) => {
      if (streaming || mode !== "select") return;
      L.DomEvent.stop(ev);                    // don't let the map start its own drag/pan
      routeDrag = {
        origin: ev.latlng,
        originPx: map.latLngToLayerPoint(ev.latlng),
        base: route.map((p) => [p[0], p[1]]),
        moved: false,
      };
      map.dragging.disable();
      map.on("mousemove", onRouteDragMove);
      map.on("mouseup", onRouteDragEnd);
    });
    routeLayer.on("click", (ev) => {
      if (streaming || mode !== "select") return;
      L.DomEvent.stopPropagation(ev);
      if (suppressLineClick) { suppressLineClick = false; return; }  // this "click" ended a drag
      insertOnSegment(ev.latlng);
    });

    document.addEventListener("keydown", (ev) => {
      const meta = ev.ctrlKey || ev.metaKey;
      const key = ev.key;
      if (streaming) return;                 // no edit shortcuts mid-run
      if (meta && key.toLowerCase() === "s") { ev.preventDefault(); save(false); return; }
      if (meta && key.toLowerCase() === "z") { ev.preventDefault(); if (ev.shiftKey) redo(); else undo(); return; }
      if (meta && key.toLowerCase() === "y") { ev.preventDefault(); redo(); return; }
      const tag = ev.target && ev.target.tagName;
      if (tag === "INPUT" || tag === "TEXTAREA" || tag === "SELECT") return;
      if (key === "Escape") { el.cardUturn.open = false; setMode("select"); selectPoint(null); return; }
      if (key === "Delete" || key === "Backspace") { ev.preventDefault(); deleteSelected(); return; }
      if (key === "v" || key === "V") { el.cardUturn.open = false; setMode("select"); return; }
      if (key === "d" || key === "D") { el.cardUturn.open = false; setMode("draw"); return; }
      // The turn ghost is always on. U cycles its type (left -> right -> straight);
      // L / R / S set it directly. These work in any edit mode.
      if (key === "u" || key === "U") { setTurnSide(); return; }
      if (key === "l" || key === "L") { setTurnSide("left"); return; }
      if (key === "r" || key === "R") { setTurnSide("right"); return; }
      if (key === "s" || key === "S") { setTurnSide("straight"); return; }
      if (key === "f" || key === "F") { fitAll(); return; }
      // I (insert) commits the ghost into the route at the working point; Enter does
      // the same while the Turn card/tool is armed.
      if (key === "i" || key === "I") { insertUturn(); return; }
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
    // Side toggle: cycling always passes through straight in the middle —
    // left -> straight -> right -> straight -> left -> ... Pass a value to set it
    // directly (L/R/S), or no argument to advance the cycle (button click / U key).
    const SIDE_CYCLE = ["left", "straight", "right", "straight"];
    const SIDE_LABEL = { left: "Left &#8592;", right: "Right &#8594;", straight: "Straight" };
    let sideCycleIdx = 0;
    function setTurnSide(side) {
      if (side) {
        el.uturnSide.dataset.side = side;
        sideCycleIdx = SIDE_CYCLE.indexOf(side);      // resync so the next U continues the cycle
      } else {
        sideCycleIdx = (sideCycleIdx + 1) % SIDE_CYCLE.length;
        el.uturnSide.dataset.side = SIDE_CYCLE[sideCycleIdx];
      }
      el.uturnSide.innerHTML = SIDE_LABEL[el.uturnSide.dataset.side];
      previewUturn();
    }
    el.uturnSide.onclick = () => setTurnSide();
    el.uturnPoints.oninput = previewUturn;
    el.turnContinue.oninput = previewUturn;
    // Opening the Turn card IS the Turn tool. Closing it hands you back to Select.
    el.cardUturn.addEventListener("toggle", () => {
      if (el.cardUturn.open) setMode("uturn");
      else if (mode === "uturn") setMode("select");
      else previewUturn();
    });

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
    // Pick up the CAN bus name, and re-attach to a stream already in flight (so a
    // browser refresh mid-run does not orphan the machine behind a dead UI).
    fetch("/stream/state", { cache: "no-store" }).then((r) => r.json()).then((st) => {
      el.cardStream.dataset.canBus = st.can_bus;
      el.streamPhase.textContent = st.phase;
      if (st.phase === "arming" || st.phase === "streaming") {
        setAppMode("execute");          // jump straight to the Execute tab...
        setStreamingUI(true);           // ...already live
        toast("Re-attached to a stream already running on " + st.can_bus, "ok");
        streamPoll();
      }
    }).catch(() => {});
    // center=false: the machine must not yank the view away from the route you opened.
    pollOnce(false)
      .catch((err) => { setStatus("API read failed: " + err.message, "bad"); el.machineCount.textContent = "offline"; })
      .finally(() => pollLoop());
  </script>
</body>
</html>
"""


# =============================================================================
# STREAMING CONTROLLER
#
# Runs send_points.py's loop on a worker thread and publishes a snapshot the
# browser can poll. Every protocol decision (window, trigger, overlap, progress
# estimate, ADJOB) is send_points.py's — this only sequences it and reports.
# =============================================================================

class StreamController:

    def __init__(self, can_bus: str):
        self.can_bus = can_bus
        self.lock = threading.Lock()
        self.stop_event = threading.Event()
        self.thread: threading.Thread | None = None
        self.points: list[list[float]] = []      # interpolated route as [lat, lon, headland]
        self._reset()

    # -- state -----------------------------------------------------------
    def _reset(self) -> None:
        with self.lock:
            self.state = {
                "phase": "idle",          # idle | arming | streaming | done | stopped | error
                "message": "",
                "route": None,
                "can_bus": self.can_bus,
                "anchor": None,
                "total": 0,
                "progress": 0,
                "window_start": 0,
                "window_end": 0,
                "next_window_start": 0,
                "trigger_index": 0,
                "overlap_count": 0,
                "sent_until": 0,
                "batches": 0,
                "frames": 0,
                "active": False,
                "run_command": False,
                "engaged": False,
                "reject": 0,
                "speed_kph": 0.0,
                "ppp": False,
                "allowed": False,
                "inside": False,
                "elapsed_s": 0.0,
                "log": [],
            }

    def _set(self, **kw) -> None:
        with self.lock:
            self.state.update(kw)

    def _log(self, msg: str) -> None:
        line = f"{time.strftime('%H:%M:%S')}  {msg}"
        print(line, file=sys.stderr)
        with self.lock:
            self.state["log"] = (self.state["log"] + [line])[-60:]

    def snapshot(self) -> dict:
        with self.lock:
            return dict(self.state)

    def route_points(self) -> list[list[float]]:
        with self.lock:
            return list(self.points)

    def running(self) -> bool:
        return self.thread is not None and self.thread.is_alive()

    # -- control ---------------------------------------------------------
    def start(self, params: dict) -> None:
        if self.running():
            raise ValueError("already streaming")
        self._reset()
        self.stop_event.clear()
        self.thread = threading.Thread(target=self._run, args=(params,), daemon=True)
        self.thread.start()

    def stop(self) -> None:
        self.stop_event.set()

    # -- the loop --------------------------------------------------------
    def _run(self, p: dict) -> None:
        bus = None
        try:
            route_name = p["route"]
            spacing = float(p["max_spacing"])
            window_size = int(p["window_size"])
            trigger_fraction = float(p["trigger_fraction"])
            overlap_offset = int(p["overlap_offset"])   # send_points.py's --overlap-from-trigger-offset
            nearest_ahead = int(p["nearest_ahead"])
            nearest_backtrack = int(p["nearest_backtrack"])
            no_inside_gate = bool(p["no_inside_gate"])
            timeout_s = float(p["timeout"])

            # Geometry first, so the UI can draw the interpolated route while we
            # are still waiting for an anchor. lat/lon do not depend on the datum.
            path, gate_route, datum_lat, datum_lon = sp.route_for_max_spacing(route_name, spacing)
            total = len(gate_route)
            if total > a.PROTOCOL_U16_MAX:
                raise ValueError(f"{total} points after interpolation; protocol max is {a.PROTOCOL_U16_MAX}")
            pts = []
            for rp in gate_route:
                lat, lon = a.enu_to_wgs_approx(rp.x, rp.y, datum_lat, datum_lon)
                pts.append([lat, lon, 1 if rp.is_headland else 0])
            with self.lock:
                self.points = pts

            field = routes.bounding_field(gate_route, sp.FIELD_MARGIN_M)
            job_id = p.get("job_id") or int(time.time()) % (a.PROTOCOL_U16_MAX + 1)
            max_spacing = sp.max_route_spacing(gate_route)

            self._set(phase="arming", route=route_name, total=total)
            self._log(f"{route_name}: {total} pts, max spacing {max_spacing:.2f} m, "
                      f"window={window_size}, trigger={trigger_fraction:.0%}, "
                      f"overlap_offset={overlap_offset}, job_id={job_id}")
            if max_spacing < sp.MIN_SPACING_M:
                self._log(f"note: {max_spacing:.2f} m spacing is below the AgJunction {sp.MIN_SPACING_M} m minimum")

            bus = a.make_bus(self.can_bus)
            status = a.MachineStatus()

            # ---- arming: ADJOB systemActive until the Display gives us an anchor
            t0 = time.monotonic()
            last_adjob = -999.0
            active_sent = False
            while not (active_sent and status.anchor_lat is not None):
                if self.stop_event.is_set():
                    self._finish("stopped", "stopped before anchor", bus, None, 0, 0, job_id)
                    return
                if time.monotonic() - t0 >= timeout_s:
                    self._finish("error", f"no anchor within {timeout_s:.0f}s — check PPP / AutoDrive allowed / inside gate",
                                 bus, None, 0, 0, job_id)
                    return
                frame = bus.recv(timeout=0.05)
                if frame is not None:
                    a.process_frame(frame, status)
                inside = sp.inside_field(status, field, datum_lat, datum_lon)
                active = status.gps_ppp_available and status.autodrive_allowed and (no_inside_gate or inside)
                self._set(ppp=status.gps_ppp_available, allowed=status.autodrive_allowed,
                          inside=inside, active=active, elapsed_s=time.monotonic() - t0,
                          engaged=status.autodrive_engaged, reject=status.reject_reason,
                          speed_kph=status.speed_kph)
                now = time.monotonic() - t0
                if now - last_adjob >= a.ADJOB_PERIOD_S:
                    last_adjob = now
                    if active and not active_sent:
                        active_sent = True
                        status.anchor_lat = None
                        status.anchor_lon = None
                        self._log("gates open — requesting job (ADJOB systemActive=true)")
                    sp.send_adjob(bus, active, False, 0, total, job_id)

            anchor_lat, anchor_lon = status.anchor_lat, status.anchor_lon
            self._set(anchor=[anchor_lat, anchor_lon])
            self._log(f"anchor {anchor_lat:.7f},{anchor_lon:.7f}")

            # ---- re-resolve the route about the anchor, exactly as send_points does
            route = sp.load_line_from_anchor(path, spacing, anchor_lat, anchor_lon)
            waypoints = sp.build_waypoints(route)
            xy = sp.route_xy(route)
            n = len(waypoints)
            self._set(total=n)

            current_index = 0
            window_start = window_end = sent_until = 0
            batches = frames = 0
            run_command = False

            start, end, sent = sp.stream_next_window(bus, status, waypoints, 0, window_size)
            batches += 1
            frames += sent
            window_start, window_end = start, end
            sent_until = max(sent_until, end)
            # send_points.py line 323, verbatim: latched once here and left as-is.
            run_command = sent_until >= min(a.FUTURE_POINT_COUNT, n)
            self._log(f"batch 1: sent [{start}..{end - 1}] ({sent} frames)")
            self._set(phase="streaming", window_start=window_start, window_end=window_end,
                      sent_until=sent_until, batches=batches, frames=frames, run_command=run_command)

            # ---- run loop
            last_report = -999.0
            while current_index < n - 1:
                if self.stop_event.is_set():
                    self._finish("stopped", "stopped by operator", bus, status, current_index, n, job_id)
                    return

                frame = bus.recv(timeout=0.02)
                if frame is not None:
                    a.process_frame(frame, status)

                # field polygon lives in the datum frame (built from gate_route), so
                # inside_field must be evaluated in datum coords — exactly as
                # send_points.py does (its line 340). estimate_progress_index below
                # uses the anchor frame because xy was re-resolved about the anchor.
                inside = sp.inside_field(status, field, datum_lat, datum_lon)
                active = status.gps_ppp_available and status.autodrive_allowed and (no_inside_gate or inside)
                current_index = sp.estimate_progress_index(
                    status, xy, anchor_lat, anchor_lon, current_index,
                    nearest_backtrack, nearest_ahead,
                )

                # Exactly send_points.py's windowing (its lines 352-359): trigger at a
                # fraction of the current window, next batch starts at trigger+offset.
                window_len = max(1, window_end - window_start)
                trigger_index = min(window_end - 1, window_start + math.floor(window_len * trigger_fraction))
                next_start = min(window_end, trigger_index + overlap_offset)

                if active and window_end < n and current_index >= trigger_index:
                    start, end, sent = sp.stream_next_window(bus, status, waypoints, next_start, window_size)
                    batches += 1
                    frames += sent
                    window_start, window_end = start, end
                    previous = sent_until
                    sent_until = max(sent_until, end)
                    self._log(f"batch {batches}: progress {current_index} crossed trigger {trigger_index}; "
                              f"sent [{start}..{end - 1}] ({sent} frames, new [{previous}..{sent_until - 1}])")

                now = time.monotonic() - t0
                if now - last_adjob >= a.ADJOB_PERIOD_S:
                    last_adjob = now
                    sp.send_adjob(bus, active, run_command, current_index, n, job_id)

                if now - last_report >= 0.2:
                    last_report = now
                    self._set(
                        progress=current_index, window_start=window_start, window_end=window_end,
                        next_window_start=next_start, trigger_index=trigger_index,
                        overlap_count=max(0, window_end - next_start),
                        sent_until=sent_until, batches=batches, frames=frames,
                        active=active, run_command=run_command,
                        engaged=status.autodrive_engaged, reject=status.reject_reason,
                        speed_kph=status.speed_kph, ppp=status.gps_ppp_available,
                        allowed=status.autodrive_allowed, inside=inside, elapsed_s=now,
                    )

                if window_end >= n and current_index >= n - 1:
                    break

            self._finish("done", "route complete", bus, status, current_index, n, job_id)

        except BaseException as exc:                       # noqa: BLE001 - surface anything to the UI
            self._log(f"ERROR: {exc}")
            self._set(phase="error", message=str(exc))
            self._deactivate(bus, 0, 0, 0)

    def _finish(self, phase, message, bus, status, current_index, total, job_id) -> None:
        self._log(message)
        self._deactivate(bus, current_index, total, job_id)
        self._set(phase=phase, message=message, active=False, run_command=False,
                  progress=current_index)

    def _deactivate(self, bus, current_index: int, total: int, job_id: int) -> None:
        """Hand the job back: ADJOB systemActive=false, RunCommand=false.

        send_points.py just exits and leaves the last ADJOB standing; for a UI with
        a Stop button that is not good enough, so we explicitly stand the job down.
        """
        if bus is None:
            return
        try:
            for _ in range(3):
                sp.send_adjob(bus, False, False, current_index, total, job_id)
                time.sleep(0.05)
            self._log("ADJOB systemActive=false sent (job stood down)")
        except Exception as exc:                            # noqa: BLE001
            self._log(f"deactivate failed: {exc}")
        try:
            bus.bus.shutdown()
        except Exception:                                   # noqa: BLE001, S110
            pass


STREAM: StreamController | None = None


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Serve the AutoDrive route maker web UI (with live streaming).")
    parser.add_argument("--can-bus", default=a.CAN_BUS,
                        help=f"SocketCAN interface to stream on (default: {a.CAN_BUS}; vcan0 = bench)")
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


def _num(body: dict, key: str, default: float, lo: float, hi: float) -> float:
    """Read a numeric field, clamped. The UI can send anything; the CAN bus cannot."""
    value = body.get(key)
    if value is None or value == "":
        return default
    n = float(value)
    if not (lo <= n <= hi):
        raise ValueError(f"{key} must be between {lo} and {hi} (got {n})")
    return n


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
            elif path == "/stream/state":
                self._send_json(STREAM.snapshot())
            elif path == "/stream/route":
                self._send_json({"ok": True, "points": STREAM.route_points()})
            elif path == "/stream/preview":
                # The interpolated points that WILL be streamed — computed from the file,
                # no CAN bus, no anchor. This is what Execute mode shows before you Play.
                try:
                    params = parse_qs(parsed.query)
                    route = (params.get("route", ["line"])[0] or "line")
                    if route not in routes.ROUTES:
                        raise ValueError(f"unknown route {route!r}")
                    spacing = float(params.get("spacing", [sp.DEFAULT_MAX_SPACING_M])[0])
                    if not 0.05 <= spacing <= 10.0:
                        raise ValueError("spacing must be 0.05..10 m")
                    _, gate_route, dlat, dlon = sp.route_for_max_spacing(route, spacing)
                    pts = [[*a.enu_to_wgs_approx(rp.x, rp.y, dlat, dlon), 1 if rp.is_headland else 0]
                           for rp in gate_route]
                except Exception as exc:                    # noqa: BLE001
                    self._send_json({"error": str(exc)}, HTTPStatus.BAD_REQUEST)
                    return
                self._send_json({"ok": True, "points": pts,
                                 "max_spacing": sp.max_route_spacing(gate_route)})
            elif path == "/health":
                self._send_json({"ok": True})
            else:
                self._send_json({"error": "not found"}, HTTPStatus.NOT_FOUND)

        def do_POST(self) -> None:
            path = urlparse(self.path).path
            if path == "/stream/start":
                self._stream_start()
                return
            if path == "/stream/stop":
                STREAM.stop()
                self._send_json({"ok": True})
                return
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
                coords = extract_linestring(payload.get("geojson"))
                path_out = target_path(target, default_output)
                write_route(path_out, coords)
            except Exception as exc:
                self._send_json({"error": str(exc)}, HTTPStatus.BAD_REQUEST)
                return
            self._send_json({"ok": True, "path": str(path_out), "points": len(coords)})

        def _stream_start(self) -> None:
            try:
                length = int(self.headers.get("Content-Length", "0"))
                if length <= 0 or length > 100_000:
                    raise ValueError("Request body must be 1..100000 bytes")
                body = json.loads(self.rfile.read(length).decode("utf-8"))
                if not isinstance(body, dict):
                    raise ValueError("Request body must be an object")

                route = str(body.get("route") or "line")
                if route not in routes.ROUTES:
                    raise ValueError(f"unknown route {route!r}")

                params = {
                    "route": route,
                    "max_spacing": _num(body, "max_spacing", sp.DEFAULT_MAX_SPACING_M, 0.05, 10.0),
                    "window_size": int(_num(body, "window_size", sp.DEFAULT_WINDOW_SIZE, 1, 2000)),
                    "trigger_fraction": _num(body, "trigger_fraction", sp.DEFAULT_TRIGGER_FRACTION, 0.01, 1.0),
                    "overlap_offset": int(_num(body, "overlap_offset", sp.DEFAULT_OVERLAP_FROM_TRIGGER_OFFSET, 0, 500)),
                    "nearest_ahead": int(_num(body, "nearest_ahead", sp.DEFAULT_NEAREST_AHEAD, 1, sp.MAX_PROGRESS_JUMP)),
                    "nearest_backtrack": int(_num(body, "nearest_backtrack", sp.DEFAULT_NEAREST_BACKTRACK, 0, 100)),
                    "timeout": _num(body, "timeout", 10.0, 1.0, 120.0),
                    "no_inside_gate": bool(body.get("no_inside_gate")),
                    "job_id": int(body["job_id"]) if body.get("job_id") else None,
                }
                STREAM.start(params)
            except Exception as exc:                        # noqa: BLE001
                self._send_json({"error": str(exc)}, HTTPStatus.BAD_REQUEST)
                return
            self._send_json({"ok": True, "state": STREAM.snapshot()})

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
    global STREAM
    args = parse_args()
    if args.api_timeout <= 0:
        raise SystemExit("--api-timeout must be greater than zero")
    STREAM = StreamController(args.can_bus)
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
    print(f"  CAN bus (streaming)   : {args.can_bus}", file=sys.stderr)
    if args.can_bus.startswith("vcan"):
        print("                          (virtual bus — run ./simulator.py for a bench loop)", file=sys.stderr)
    else:
        print("                          *** REAL BUS — Play will transmit and AutoDrive will steer ***", file=sys.stderr)
    print("  Ctrl+C to stop", file=sys.stderr)
    print("", file=sys.stderr)

    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nstopping", file=sys.stderr)
    finally:
        if STREAM is not None and STREAM.running():
            print("standing down the streaming job...", file=sys.stderr)
            STREAM.stop()
            if STREAM.thread is not None:
                STREAM.thread.join(timeout=5.0)
        server.server_close()


if __name__ == "__main__":
    main()
