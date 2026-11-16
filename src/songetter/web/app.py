"""FastAPI web application and REST API for Songetter (Milestone 3).

Provides:
- Server-side responsive HTML UI via Jinja2 (`GET /`)
- Library directory validation, selection, flat track listing, streaming, and organization
- CSV upload, validation, URL deduplication, and optional auto-download (`/api/upload`)
- URL / Spotify / CSV / record batch download, deduplication, and organization jobs (`/api/jobs*`)
- Multi-source federated metadata search, CSV export, and direct download dispatch (`/api/search*`)
"""

from __future__ import annotations

import mimetypes
import os
import threading
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Union
from urllib.parse import unquote

from fastapi import Body, FastAPI, File, Form, HTTPException, Query, Request, Response, UploadFile
from fastapi.responses import FileResponse, HTMLResponse
from jinja2 import Environment, select_autoescape
from pydantic import BaseModel, Field

from songetter import __version__
from songetter import downloader as _downloader
from songetter import search as _search
from songetter.config import AUDIO_SUFFIXES
from songetter.csv_processor import (
    CSVProcessorError,
    TrackRecord,
    _is_valid_url,
    parse_csv,
)
from songetter.deduplicator import (
    DuplicateResolutionResult,
    extract_metadata,
    resolve_duplicates,
)
from songetter.downloader import DownloadSummary, TrackDownloadResult, deduplicate_records
from songetter.library import (
    InvalidPathError,
    LibraryError,
    NotADirectoryError as LibraryNotADirectoryError,
    PermissionDeniedError,
    validate_library_directory,
)
from songetter.organizer import flatten_and_organize_library
from songetter.search import (
    SearchError,
    SearchResult,
    UnsupportedSearchSourceError,
)

__all__ = ["app", "create_app"]

# Expose module-level references while also honoring patches on songetter.search / songetter.downloader.
_ORIG_SEARCH_TRACKS_WITH_ERRORS = _search.search_tracks_with_errors
_ORIG_LIST_SEARCH_SOURCES = _search.list_search_sources
_ORIG_SEARCH_RESULTS_TO_CSV = _search.search_results_to_csv
_ORIG_DOWNLOADER = _downloader.Downloader

search_tracks_with_errors = _ORIG_SEARCH_TRACKS_WITH_ERRORS
list_search_sources = _ORIG_LIST_SEARCH_SOURCES
search_results_to_csv = _ORIG_SEARCH_RESULTS_TO_CSV
Downloader = _ORIG_DOWNLOADER

_AUDIO_MEDIA_TYPES: Dict[str, str] = {
    ".flac": "audio/flac",
    ".mp3": "audio/mpeg",
    ".m4a": "audio/mp4",
    ".ogg": "audio/ogg",
    ".opus": "audio/opus",
    ".wav": "audio/wav",
    ".aiff": "audio/aiff",
    ".wv": "audio/x-wavpack",
    ".tta": "audio/x-tta",
}

_JINJA_ENV = Environment(autoescape=select_autoescape(["html", "xml"]))

_DASHBOARD_TEMPLATE = _JINJA_ENV.from_string("""<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0">
  <title>Songetter — Server-Side Music Library Manager</title>
  <style>
    :root {
      --bg: #0b0f19;
      --surface: #121826;
      --surface-alt: #182032;
      --border: #263147;
      --text: #e5e9f0;
      --muted: #9aa5b8;
      --accent: #6366f1;
      --accent-hover: #4f46e5;
      --success: #10b981;
      --warning: #f59e0b;
      --danger: #ef4444;
      --radius: 10px;
    }
    * { box-sizing: border-box; }
    body {
      margin: 0;
      font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, Inter, sans-serif;
      background: var(--bg);
      color: var(--text);
      line-height: 1.5;
    }
    .container {
      max-width: 1240px;
      margin: 0 auto;
      padding: 24px 20px 64px;
    }
    header {
      display: flex;
      flex-wrap: wrap;
      justify-content: space-between;
      align-items: center;
      gap: 12px;
      margin-bottom: 24px;
      padding-bottom: 16px;
      border-bottom: 1px solid var(--border);
    }
    header h1 { margin: 0; font-size: 1.75rem; letter-spacing: -0.02em; }
    header .subtitle { margin: 4px 0 0; color: var(--muted); font-size: 0.92rem; }
    .pill {
      display: inline-flex;
      align-items: center;
      gap: 6px;
      padding: 4px 10px;
      border-radius: 999px;
      font-size: 0.78rem;
      font-weight: 600;
      background: var(--surface-alt);
      border: 1px solid var(--border);
      color: var(--muted);
    }
    .pill.ok { border-color: rgba(16, 185, 129, 0.45); color: #6ee7b7; background: rgba(16, 185, 129, 0.12); }
    .pill.warn { border-color: rgba(245, 158, 11, 0.45); color: #fcd34d; background: rgba(245, 158, 11, 0.12); }
    .grid { display: grid; grid-template-columns: 1fr; gap: 20px; }
    @media (min-width: 960px) {
      .grid-two { display: grid; grid-template-columns: 1fr 1fr; gap: 20px; }
    }
    .card {
      background: var(--surface);
      border: 1px solid var(--border);
      border-radius: var(--radius);
      padding: 20px;
    }
    .card h2 { margin: 0 0 6px; font-size: 1.12rem; }
    .card p.desc { margin: 0 0 16px; color: var(--muted); font-size: 0.88rem; }
    .row { display: flex; flex-wrap: wrap; gap: 10px; align-items: center; }
    .field { display: flex; flex-direction: column; gap: 5px; flex: 1 1 200px; }
    label { font-size: 0.82rem; color: var(--muted); font-weight: 600; }
    input[type="text"], input[type="number"], select, textarea {
      width: 100%;
      padding: 9px 12px;
      border-radius: 7px;
      border: 1px solid var(--border);
      background: var(--surface-alt);
      color: var(--text);
      font-size: 0.92rem;
    }
    textarea { min-height: 96px; resize: vertical; font-family: ui-monospace, SFMono-Regular, Menlo, monospace; }
    input[type="file"] {
      padding: 8px;
      border-radius: 7px;
      border: 1px dashed var(--border);
      background: var(--surface-alt);
      color: var(--text);
      width: 100%;
    }
    .checkbox-row {
      display: inline-flex;
      align-items: center;
      gap: 7px;
      font-size: 0.88rem;
      color: var(--text);
      cursor: pointer;
    }
    button {
      padding: 9px 16px;
      border: none;
      border-radius: 7px;
      font-size: 0.9rem;
      font-weight: 600;
      cursor: pointer;
      color: #fff;
      background: var(--accent);
      transition: background 0.15s ease;
    }
    button:hover { background: var(--accent-hover); }
    button.secondary { background: var(--surface-alt); border: 1px solid var(--border); color: var(--text); }
    button.secondary:hover { background: #202a40; }
    button.success { background: #059669; }
    button.success:hover { background: #047857; }
    .status-box {
      margin-top: 12px;
      padding: 10px 12px;
      border-radius: 7px;
      font-size: 0.88rem;
      background: var(--surface-alt);
      border: 1px solid var(--border);
      display: none;
    }
    .status-box.visible { display: block; }
    .status-box.ok { border-color: rgba(16, 185, 129, 0.45); color: #a7f3d0; }
    .status-box.err { border-color: rgba(239, 68, 68, 0.45); color: #fecaca; }
    .status-box.warn { border-color: rgba(245, 158, 11, 0.45); color: #fde68a; }
    .source-chips { display: flex; flex-wrap: wrap; gap: 8px; margin: 10px 0 14px; }
    .source-chip {
      display: inline-flex;
      align-items: center;
      gap: 6px;
      padding: 6px 10px;
      border-radius: 7px;
      border: 1px solid var(--border);
      background: var(--surface-alt);
      font-size: 0.84rem;
      cursor: pointer;
    }
    .table-wrap { width: 100%; overflow-x: auto; margin-top: 14px; border: 1px solid var(--border); border-radius: 8px; }
    table { width: 100%; border-collapse: collapse; font-size: 0.86rem; }
    th, td { text-align: left; padding: 9px 11px; border-bottom: 1px solid var(--border); vertical-align: middle; }
    th { background: var(--surface-alt); color: var(--muted); font-weight: 600; font-size: 0.78rem; text-transform: uppercase; letter-spacing: 0.04em; }
    tr:last-child td { border-bottom: none; }
    .muted { color: var(--muted); }
    .mono { font-family: ui-monospace, SFMono-Regular, Menlo, Monaco, Consolas, monospace; font-size: 0.82rem; }
    .metrics { display: grid; grid-template-columns: repeat(auto-fit, minmax(120px, 1fr)); gap: 10px; margin: 12px 0; }
    .metric { background: var(--surface-alt); border: 1px solid var(--border); border-radius: 8px; padding: 10px 12px; }
    .metric .label { font-size: 0.74rem; text-transform: uppercase; color: var(--muted); }
    .metric .value { font-size: 1.25rem; font-weight: 700; margin-top: 2px; }
    audio { height: 28px; max-width: 210px; }
    a { color: #818cf8; text-decoration: none; }
    a:hover { text-decoration: underline; }
  </style>
</head>
<body>
  <div class="container">
    <header>
      <div>
        <h1>Songetter</h1>
        <p class="subtitle">Server-side flat music library ingestion, federated catalog search, and quality deduplication (v{{ version }})</p>
      </div>
      <div class="row">
        <span id="header-library-badge" class="pill {{ 'ok' if library_exists else 'warn' }}">
          Library: {{ library_dir }}
        </span>
        <span class="pill">Metadata-only lyrics (no .lrc sidecars)</span>
      </div>
    </header>

    <div class="grid">
      <section class="card" aria-labelledby="library-heading">
        <h2 id="library-heading">1. Server Library Folder / Directory</h2>
        <p class="desc">Select a writable directory or folder on the server where tracks are stored in a flat <span class="mono">{Artist} - {Title}.{ext}</span> layout.</p>
        <form id="library-form" action="/api/library" method="post">
          <div class="row">
            <div class="field">
              <label for="library-path">Library Folder / Directory Path</label>
              <input type="text" id="library-path" name="path" value="{{ library_dir }}" placeholder="/library or ./Music" required>
            </div>
            <div style="padding-top: 20px;">
              <label class="checkbox-row">
                <input type="checkbox" id="library-create" name="create" checked>
                Create directory if missing
              </label>
            </div>
            <div style="padding-top: 20px;" class="row">
              <button type="submit">Select Library Directory</button>
              <button type="button" id="btn-organize-library" class="secondary">Deduplicate &amp; Flatten</button>
            </div>
          </div>
        </form>
        <div id="library-status" class="status-box" role="status"></div>
      </section>

      <section class="card" aria-labelledby="search-heading">
        <h2 id="search-heading">2. Federated Multi-Source Catalog Search</h2>
        <p class="desc">Search across Deezer, TIDAL, iTunes, MusicBrainz, Qobuz, and Spotify, export results as CSV, or download directly into your library.</p>
        <form id="search-form" action="/api/search" method="get">
          <div class="row">
            <div class="field" style="flex: 2 1 280px;">
              <label for="search-query">Search Query</label>
              <input type="text" id="search-query" name="q" placeholder="Artist, track title, or album..." required>
            </div>
            <div class="field" style="flex: 1 1 140px;">
              <label for="search-type">Search Type</label>
              <select id="search-type" name="search_type">
                <option value="track" selected>Track</option>
                <option value="album">Album</option>
                <option value="artist">Artist</option>
              </select>
            </div>
            <div class="field" style="flex: 0 1 110px;">
              <label for="search-limit">Limit</label>
              <input type="number" id="search-limit" name="limit" value="10" min="1" max="100">
            </div>
            <div style="padding-top: 20px;">
              <label class="checkbox-row">
                <input type="checkbox" id="search-deduplicate" name="deduplicate">
                Deduplicate results
              </label>
            </div>
            <div style="padding-top: 20px;">
              <button type="submit">Search Catalog</button>
            </div>
          </div>
          <div style="margin-top: 12px;">
            <label>Sources</label>
            <div id="search-sources-container" class="source-chips">
              <label class="source-chip">
                <input type="checkbox" name="sources" value="all" id="source-all">
                <span>All configured (all)</span>
              </label>
              {% for src in sources %}
              <label class="source-chip">
                <input type="checkbox" name="sources" value="{{ src.id }}" {% if src.id == 'deezer' %}checked{% endif %}>
                <span>{{ src.name }} ({{ src.id }})</span>
                <span class="pill {{ 'ok' if src.configured else 'warn' }}">{{ 'ready' if src.configured else 'auth needed' }}</span>
              </label>
              {% endfor %}
            </div>
          </div>
        </form>

        <div id="search-status" class="status-box" role="status"></div>
        <div id="search-errors" class="status-box warn" role="alert"></div>

        <div class="row" style="margin-top: 12px; justify-content: space-between;">
          <div class="row">
            <button type="button" id="btn-download-search" class="success">Download to Library</button>
            <button type="button" id="btn-export-search" class="secondary">Export CSV</button>
            <label class="checkbox-row">
              <input type="checkbox" id="use-preview-if-no-url">
              Fallback to preview URL if track URL missing
            </label>
          </div>
          <span id="search-count" class="muted">0 results</span>
        </div>

        <div class="table-wrap">
          <table id="search-results-table">
            <thead>
              <tr>
                <th><input type="checkbox" id="search-select-all" title="Select all results"></th>
                <th>Source</th>
                <th>Artist</th>
                <th>Title</th>
                <th>Album</th>
                <th>Duration</th>
                <th>Quality / Preview</th>
                <th>Link</th>
              </tr>
            </thead>
            <tbody id="search-results-body">
              <tr><td colspan="8" class="muted">Run a search to inspect catalog matches.</td></tr>
            </tbody>
          </table>
        </div>
      </section>

      <div class="grid-two">
        <section class="card" aria-labelledby="upload-heading">
          <h2 id="upload-heading">3. Upload CSV Playlist / URL Batch</h2>
          <p class="desc">Upload an RFC 4180 CSV file or paste Spotify / direct audio URLs below for pre-download checking and batch ingestion.</p>
          <form id="upload-form" action="/api/upload" method="post" enctype="multipart/form-data">
            <div class="field">
              <label for="csv-file">CSV File</label>
              <input type="file" id="csv-file" name="file" accept=".csv" required>
            </div>
            <div class="row" style="margin-top: 10px;">
              <label class="checkbox-row">
                <input type="checkbox" id="csv-auto-download" name="auto_download">
                Auto-download after upload
              </label>
            </div>
            <div class="row" style="margin-top: 12px;">
              <button type="submit">Upload &amp; Parse CSV</button>
              <span id="upload-summary" class="muted"></span>
            </div>
          </form>
          <div id="upload-status" class="status-box" role="status"></div>

          <div style="margin-top: 16px;">
            <label for="batch-urls">Spotify / Direct Audio URLs (one per line, optional)</label>
            <textarea id="batch-urls" name="urls" placeholder="https://open.spotify.com/track/...&#10;https://example.com/Artist%20-%20Title.mp3"></textarea>
          </div>

          <div class="table-wrap">
            <table>
              <thead>
                <tr><th>#</th><th>Artist</th><th>Title</th><th>URL</th></tr>
              </thead>
              <tbody id="csv-preview-body">
                <tr><td colspan="4" class="muted">No CSV uploaded yet.</td></tr>
              </tbody>
            </table>
          </div>
        </section>

        <section class="card" aria-labelledby="jobs-heading">
          <h2 id="jobs-heading">4. Job Execution &amp; Status Monitor</h2>
          <p class="desc">Run uploaded CSV tracks or pasted Spotify/audio URLs through the downloader with pre-download skipping, deduplication, and flat naming.</p>
          <form id="job-form" action="/api/jobs" method="post">
            <div class="row">
              <div class="field" style="flex: 0 1 130px;">
                <label for="job-concurrency">Concurrency</label>
                <input type="number" id="job-concurrency" name="max_concurrent" value="2" min="1" max="16">
              </div>
              <div style="padding-top: 20px;" class="row">
                <label class="checkbox-row"><input type="checkbox" id="job-skip-existing" checked> Skip existing tracks</label>
                <label class="checkbox-row"><input type="checkbox" id="job-auto-dedup" checked> Deduplicate quality</label>
                <label class="checkbox-row"><input type="checkbox" id="job-auto-organize" checked> Flatten &amp; standardize</label>
              </div>
            </div>
            <div class="row" style="margin-top: 12px;">
              <button type="submit" class="success">Start Download Job</button>
              <button type="button" id="btn-refresh-jobs" class="secondary">Refresh Jobs</button>
            </div>
          </form>

          <div id="job-status-box" class="status-box" role="status"></div>

          <div class="metrics" id="active-job-metrics" style="display: none;">
            <div class="metric"><div class="label">Status</div><div class="value" id="m-status">-</div></div>
            <div class="metric"><div class="label">Total</div><div class="value" id="m-total">0</div></div>
            <div class="metric"><div class="label">Downloaded</div><div class="value" id="m-downloaded">0</div></div>
            <div class="metric"><div class="label">Skipped</div><div class="value" id="m-skipped">0</div></div>
            <div class="metric"><div class="label">Failed</div><div class="value" id="m-failed">0</div></div>
            <div class="metric"><div class="label">Deduplicated</div><div class="value" id="m-dedup">0</div></div>
          </div>

          <div class="table-wrap">
            <table>
              <thead>
                <tr><th>Job ID</th><th>Status</th><th>Downloaded</th><th>Skipped</th><th>Failed</th></tr>
              </thead>
              <tbody id="jobs-list-body">
                <tr><td colspan="5" class="muted">No jobs dispatched yet.</td></tr>
              </tbody>
            </table>
          </div>
        </section>
      </div>

      <section class="card" aria-labelledby="tracks-heading">
        <div class="row" style="justify-content: space-between; margin-bottom: 8px;">
          <div>
            <h2 id="tracks-heading">5. Flat Library Tracks</h2>
            <p class="desc" id="library-tracks-summary">Inspect and stream audio tracks in the active server library directory.</p>
          </div>
          <button type="button" id="btn-refresh-library" class="secondary">Refresh Library</button>
        </div>

        <div class="table-wrap">
          <table id="library-tracks-table">
            <thead>
              <tr>
                <th>Filename</th>
                <th>Artist</th>
                <th>Title</th>
                <th>Format</th>
                <th>Bitrate</th>
                <th>File Size</th>
                <th>Duration</th>
                <th>Stream</th>
              </tr>
            </thead>
            <tbody id="library-tracks-body">
              <tr><td colspan="8" class="muted">Loading library tracks...</td></tr>
            </tbody>
          </table>
        </div>
      </section>
    </div>
  </div>

  <script>
    (function () {
      let currentSearchResults = [];

      function escapeHtml(value) {
        return String(value ?? "")
          .replace(/&/g, "&amp;")
          .replace(/</g, "&lt;")
          .replace(/>/g, "&gt;")
          .replace(/"/g, "&quot;")
          .replace(/'/g, "&#39;");
      }

      function setStatus(elementId, message, variant) {
        const el = document.getElementById(elementId);
        if (!el) return;
        if (!message) {
          el.className = "status-box";
          el.textContent = "";
          return;
        }
        el.className = "status-box visible " + (variant || "");
        el.textContent = message;
      }

      function formatDuration(seconds) {
        if (seconds === null || seconds === undefined || Number.isNaN(Number(seconds))) return "-";
        const total = Math.max(0, Math.round(Number(seconds)));
        return Math.floor(total / 60) + ":" + String(total % 60).padStart(2, "0");
      }

      function formatBytes(bytes) {
        const num = Number(bytes || 0);
        if (!num) return "0 B";
        const units = ["B", "KB", "MB", "GB"];
        let val = num, idx = 0;
        while (val >= 1024 && idx < units.length - 1) { val /= 1024; idx++; }
        return val.toFixed(idx === 0 ? 0 : 1) + " " + units[idx];
      }

      function formatBitrate(bps) {
        const num = Number(bps || 0);
        return num ? Math.round(num / 1000) + " kbps" : "-";
      }

      async function fetchJson(url, options) {
        const resp = await fetch(url, options);
        const text = await resp.text();
        let data = {};
        try { data = text ? JSON.parse(text) : {}; } catch { data = { detail: text }; }
        if (!resp.ok) throw new Error(data.detail || ("HTTP " + resp.status));
        return data;
      }

      async function refreshLibraryTracks() {
        try {
          const data = await fetchJson("/api/library/tracks");
          const badge = document.getElementById("header-library-badge");
          const summary = document.getElementById("library-tracks-summary");
          const tbody = document.getElementById("library-tracks-body");
          const activePath = data.path || data.library_dir || "";
          badge.className = "pill ok";
          badge.textContent = "Library: " + activePath;
          summary.textContent = (data.tracks || []).length + " track(s) in " + activePath;

          if (!data.tracks || data.tracks.length === 0) {
            tbody.innerHTML = '<tr><td colspan="8" class="muted">No audio tracks found in library.</td></tr>';
            return;
          }

          tbody.innerHTML = data.tracks.map(function (t) {
            const canStream = t.filename && !t.filename.includes("/") && !t.filename.includes("\\\\");
            const player = canStream
              ? '<audio controls preload="none" src="/api/library/tracks/' + encodeURIComponent(t.filename) + '/stream"></audio>'
              : '<span class="muted">Organize to stream</span>';
            return (
              "<tr>" +
              '<td class="mono">' + escapeHtml(t.filename) + "</td>" +
              "<td>" + escapeHtml(t.artist) + "</td>" +
              "<td>" + escapeHtml(t.title) + "</td>" +
              "<td>" + escapeHtml(t.format || "") + "</td>" +
              "<td>" + escapeHtml(formatBitrate(t.bitrate)) + "</td>" +
              "<td>" + escapeHtml(formatBytes(t.file_size)) + "</td>" +
              "<td>" + escapeHtml(formatDuration(t.duration)) + "</td>" +
              "<td>" + player + "</td>" +
              "</tr>"
            );
          }).join("");
        } catch (err) {
          setStatus("library-status", "Failed to load library tracks: " + err.message, "err");
        }
      }

      function getSelectedSearchSources() {
        const checkboxes = Array.from(document.querySelectorAll('#search-sources-container input[name="sources"]:checked'));
        if (checkboxes.length > 0) {
          const values = checkboxes.map(function (cb) { return cb.value; });
          return values.includes("all") ? ["all"] : values;
        }
        return ["all"];
      }

      function getSelectedResultsPayload() {
        const selectedChecks = Array.from(document.querySelectorAll(".search-row-select:checked"));
        if (selectedChecks.length > 0) {
          return selectedChecks.map(function (cb) { return currentSearchResults[Number(cb.dataset.index)]; }).filter(Boolean);
        }
        return currentSearchResults.slice();
      }

      function renderSearchResults(results) {
        currentSearchResults = results || [];
        document.getElementById("search-count").textContent = currentSearchResults.length + " result(s)";
        const tbody = document.getElementById("search-results-body");
        if (currentSearchResults.length === 0) {
          tbody.innerHTML = '<tr><td colspan="8" class="muted">No matching tracks found.</td></tr>';
          return;
        }
        tbody.innerHTML = currentSearchResults.map(function (r, idx) {
          const previewHtml = r.preview_url ? '<audio controls preload="none" src="' + escapeHtml(r.preview_url) + '"></audio>' : '<span class="muted">-</span>';
          const quality = r.quality_hint ? '<span class="pill">' + escapeHtml(r.quality_hint) + "</span> " : "";
          const linkHtml = r.url ? '<a href="' + escapeHtml(r.url) + '" target="_blank" rel="noopener noreferrer">Open</a>' : '<span class="muted">-</span>';
          return (
            "<tr>" +
            '<td><input type="checkbox" class="search-row-select" data-index="' + idx + '" checked></td>' +
            '<td><span class="pill">' + escapeHtml(r.source) + "</span></td>" +
            "<td>" + escapeHtml(r.artist) + "</td>" +
            "<td>" + escapeHtml(r.title) + "</td>" +
            "<td>" + escapeHtml(r.album || "-") + "</td>" +
            "<td>" + escapeHtml(formatDuration(r.duration_seconds)) + "</td>" +
            "<td>" + quality + previewHtml + "</td>" +
            "<td>" + linkHtml + "</td>" +
            "</tr>"
          );
        }).join("");
      }

      function renderJobMetrics(job) {
        const metrics = document.getElementById("active-job-metrics");
        if (!job) { metrics.style.display = "none"; return; }
        metrics.style.display = "grid";
        document.getElementById("m-status").textContent = job.status || "-";
        document.getElementById("m-total").textContent = String(job.total_tracks ?? (job.tracks || []).length ?? 0);
        document.getElementById("m-downloaded").textContent = String(job.downloaded ?? 0);
        document.getElementById("m-skipped").textContent = String(job.skipped ?? 0);
        document.getElementById("m-failed").textContent = String(job.failed ?? 0);
        document.getElementById("m-dedup").textContent = String((job.duplicates_resolved || []).length);
      }

      async function refreshJobs() {
        try {
          const data = await fetchJson("/api/jobs");
          const jobs = data.jobs || [];
          const tbody = document.getElementById("jobs-list-body");
          if (jobs.length === 0) {
            tbody.innerHTML = '<tr><td colspan="5" class="muted">No jobs dispatched yet.</td></tr>';
            return;
          }
          renderJobMetrics(jobs[0]);
          tbody.innerHTML = jobs.map(function (job) {
            return (
              "<tr>" +
              '<td class="mono">' + escapeHtml(job.job_id.slice(0, 10)) + "</td>" +
              "<td>" + escapeHtml(job.status) + "</td>" +
              "<td>" + escapeHtml(job.downloaded) + "</td>" +
              "<td>" + escapeHtml(job.skipped) + "</td>" +
              "<td>" + escapeHtml(job.failed) + "</td>" +
              "</tr>"
            );
          }).join("");
        } catch (err) {
          setStatus("job-status-box", "Failed to load jobs: " + err.message, "err");
        }
      }

      document.getElementById("library-form").addEventListener("submit", async function (event) {
        event.preventDefault();
        const path = document.getElementById("library-path").value;
        const create = document.getElementById("library-create").checked;
        try {
          const data = await fetchJson("/api/library", {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ path: path, create: create }),
          });
          document.getElementById("library-path").value = data.path;
          setStatus("library-status", "Active library directory validated: " + data.path, "ok");
          await refreshLibraryTracks();
        } catch (err) {
          setStatus("library-status", err.message, "err");
        }
      });

      document.getElementById("btn-organize-library").addEventListener("click", async function () {
        try {
          const data = await fetchJson("/api/organize", { method: "POST", headers: { "Content-Type": "application/json" }, body: "{}" });
          setStatus("library-status", "Organized " + data.organized_count + " file(s); deleted " + data.deleted_count + " duplicate(s).", "ok");
          await refreshLibraryTracks();
        } catch (err) {
          setStatus("library-status", err.message, "err");
        }
      });

      document.getElementById("search-form").addEventListener("submit", async function (event) {
        event.preventDefault();
        const query = document.getElementById("search-query").value.trim();
        const sources = getSelectedSearchSources();
        const searchType = document.getElementById("search-type").value;
        const limit = Number(document.getElementById("search-limit").value || 10);
        const deduplicate = document.getElementById("search-deduplicate").checked;
        setStatus("search-errors", "", "");
        setStatus("search-status", "Searching " + sources.join(", ") + "...", "");
        try {
          const params = new URLSearchParams({ q: query, sources: sources.join(","), limit: String(limit), search_type: searchType, deduplicate: String(deduplicate) });
          const data = await fetchJson("/api/search?" + params.toString());
          renderSearchResults(data.results || []);
          setStatus("search-status", "Found " + (data.results || []).length + " result(s).", "ok");
          const errorEntries = Object.entries(data.errors || {});
          if (errorEntries.length > 0) {
            setStatus("search-errors", "Provider errors: " + errorEntries.map(function (kv) { return kv[0] + ": " + kv[1]; }).join(" | "), "warn");
          }
        } catch (err) {
          setStatus("search-status", err.message, "err");
        }
      });

      document.getElementById("search-select-all").addEventListener("change", function (event) {
        const checked = event.target.checked;
        document.querySelectorAll(".search-row-select").forEach(function (cb) { cb.checked = checked; });
      });

      document.getElementById("btn-export-search").addEventListener("click", async function () {
        const items = getSelectedResultsPayload();
        if (items.length === 0) { setStatus("search-status", "Run a search first before exporting CSV.", "err"); return; }
        try {
          const resp = await fetch("/api/search/export", {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ results: items }),
          });
          if (!resp.ok) throw new Error("Export failed (HTTP " + resp.status + ")");
          const blob = await resp.blob();
          const url = URL.createObjectURL(blob);
          const a = document.createElement("a");
          a.href = url;
          a.download = "songetter_search.csv";
          document.body.appendChild(a);
          a.click();
          a.remove();
          URL.revokeObjectURL(url);
        } catch (err) {
          setStatus("search-status", err.message, "err");
        }
      });

      document.getElementById("btn-download-search").addEventListener("click", async function () {
        const items = getSelectedResultsPayload();
        if (items.length === 0) { setStatus("search-status", "Select or search for at least one result first.", "err"); return; }
        const usePreview = document.getElementById("use-preview-if-no-url").checked;
        try {
          const data = await fetchJson("/api/search/download", {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ results: items, use_preview_if_no_url: usePreview }),
          });
          setStatus("search-status", "Job " + data.job_id.slice(0, 8) + " completed (" + data.submitted_tracks + " track(s)).", "ok");
          await refreshJobs();
          await refreshLibraryTracks();
        } catch (err) {
          setStatus("search-status", err.message, "err");
        }
      });

      document.getElementById("upload-form").addEventListener("submit", async function (event) {
        event.preventDefault();
        const fileInput = document.getElementById("csv-file");
        if (!fileInput.files || fileInput.files.length === 0) { setStatus("upload-status", "Choose a CSV file first.", "err"); return; }
        const formData = new FormData();
        formData.append("file", fileInput.files[0]);
        const autoDownload = document.getElementById("csv-auto-download").checked;
        try {
          const data = await fetchJson("/api/upload?auto_download=" + String(autoDownload), { method: "POST", body: formData });
          document.getElementById("upload-summary").textContent = data.valid_tracks + " unique track(s) (of " + data.total_tracks + ")";
          setStatus("upload-status", "Parsed " + data.valid_tracks + " track(s) from " + data.filename + ".", "ok");
          const tbody = document.getElementById("csv-preview-body");
          tbody.innerHTML = (data.preview || []).map(function (row, idx) {
            return "<tr><td>" + (idx + 1) + "</td><td>" + escapeHtml(row.artist || "-") + "</td><td>" + escapeHtml(row.title || "-") + '</td><td class="mono">' + escapeHtml(row.url) + "</td></tr>";
          }).join("");
          if (data.job_id) {
            await refreshJobs();
            await refreshLibraryTracks();
          }
        } catch (err) {
          setStatus("upload-status", err.message, "err");
        }
      });

      document.getElementById("job-form").addEventListener("submit", async function (event) {
        event.preventDefault();
        const maxConcurrent = Number(document.getElementById("job-concurrency").value || 2);
        const skipExisting = document.getElementById("job-skip-existing").checked;
        const autoDedup = document.getElementById("job-auto-dedup").checked;
        const autoOrganize = document.getElementById("job-auto-organize").checked;
        const rawUrls = document.getElementById("batch-urls").value.split(/[\\r\\n]+/).map(function (s) { return s.trim(); }).filter(Boolean);
        const bodyPayload = {
          max_concurrent: maxConcurrent,
          skip_existing: skipExisting,
          auto_deduplicate: autoDedup,
          auto_organize: autoOrganize,
        };
        if (rawUrls.length > 0) bodyPayload.urls = rawUrls;
        try {
          const job = await fetchJson("/api/jobs", {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify(bodyPayload),
          });
          setStatus("job-status-box", "Job " + job.job_id.slice(0, 8) + " completed: " + job.downloaded + " downloaded, " + job.skipped + " skipped, " + job.failed + " failed.", "ok");
          renderJobMetrics(job);
          await refreshJobs();
          await refreshLibraryTracks();
        } catch (err) {
          setStatus("job-status-box", err.message, "err");
        }
      });

      document.getElementById("btn-refresh-library").addEventListener("click", refreshLibraryTracks);
      document.getElementById("btn-refresh-jobs").addEventListener("click", refreshJobs);

      refreshLibraryTracks();
      refreshJobs();
    })();
  </script>
</body>
</html>
""")


class LibrarySelectRequest(BaseModel):
    """Payload for selecting and validating the active music library directory."""

    path: Optional[str] = None
    create: bool = False
    create_if_missing: bool = False


class OrganizeRequest(BaseModel):
    """Optional payload for running deduplication and flat organization."""

    library_path: Optional[str] = None


class TrackInput(BaseModel):
    """JSON representation of a track queued for download."""

    url: str
    artist: Optional[str] = None
    title: Optional[str] = None


class JobCreateRequest(BaseModel):
    """Payload for triggering a batch download, dedup, and organization job."""

    urls: Optional[List[str]] = None
    records: Optional[List[Dict[str, Any]]] = None
    csv_text: Optional[str] = None
    tracks: Optional[List[Union[TrackInput, str]]] = None
    library_path: Optional[str] = None
    create: bool = False
    max_concurrent: int = Field(default=2, ge=1, le=16)
    skip_existing: bool = True
    auto_organize: bool = True
    auto_deduplicate: bool = True


class SearchRequest(BaseModel):
    """JSON payload for executing a multi-source metadata search."""

    query: Optional[str] = None
    q: Optional[str] = None
    sources: Optional[Union[str, List[str]]] = None
    source: Optional[Union[str, List[str]]] = None
    limit: int = 10
    search_type: Optional[str] = None
    type: Optional[str] = None
    deduplicate: bool = False


class SearchExportRequest(SearchRequest):
    """JSON payload for exporting submitted or queried search results to CSV."""

    results: Optional[List[Dict[str, Any]]] = None


class SearchDownloadRequest(SearchRequest):
    """JSON payload for downloading search results or query hits into the library."""

    results: Optional[List[Dict[str, Any]]] = None
    tracks: Optional[List[Dict[str, Any]]] = None
    library_path: Optional[str] = None
    create: bool = False
    use_preview_if_no_url: bool = False
    max_concurrent: int = Field(default=2, ge=1, le=16)
    skip_existing: bool = True
    auto_organize: bool = True
    auto_deduplicate: bool = True


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _resolve_initial_library_dir(default_library_dir: str | Path | None) -> Path:
    if default_library_dir is not None and str(default_library_dir).strip():
        return Path(default_library_dir).expanduser().resolve()
    env_dir = (os.environ.get("SONGETTER_LIBRARY_DIR") or "").strip()
    if env_dir:
        return Path(env_dir).expanduser().resolve()
    return Path("./Music").resolve()


def _invoke_list_search_sources() -> List[Dict[str, Any]]:
    if list_search_sources is not _ORIG_LIST_SEARCH_SOURCES:
        return list_search_sources()
    return _search.list_search_sources()


def _invoke_search_tracks_with_errors(
    **kwargs: Any,
) -> tuple[List[SearchResult], Dict[str, str]]:
    if search_tracks_with_errors is not _ORIG_SEARCH_TRACKS_WITH_ERRORS:
        return search_tracks_with_errors(**kwargs)
    return _search.search_tracks_with_errors(**kwargs)


def _invoke_search_results_to_csv(results: Sequence[SearchResult]) -> str:
    if search_results_to_csv is not _ORIG_SEARCH_RESULTS_TO_CSV:
        return search_results_to_csv(results)
    return _search.search_results_to_csv(results)


def _get_downloader_cls() -> Any:
    if Downloader is not _ORIG_DOWNLOADER:
        return Downloader
    return _downloader.Downloader


def _validate_directory_or_http_error(path: Union[str, Path, None], create: bool = False) -> Path:
    try:
        return validate_library_directory(path, create=create)
    except PermissionDeniedError as exc:
        raise HTTPException(status_code=403, detail=str(exc)) from exc
    except (InvalidPathError, LibraryNotADirectoryError, NotADirectoryError) as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except LibraryError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


def _resolve_target_library(
    app: FastAPI,
    library_path: Optional[str],
    *,
    create: bool = False,
    allow_initial_create: bool = True,
) -> Path:
    if library_path is not None:
        return _validate_directory_or_http_error(library_path, create=create)
    with app.state.jobs_lock:
        configured = getattr(app.state, "library_dir", None)
        initial_dir = getattr(app.state, "_initial_library_dir", None)
    if configured is None:
        raise HTTPException(
            status_code=400,
            detail="No library directory selected. Configure one via POST /api/library or pass library_path.",
        )
    should_create = create or (
        allow_initial_create
        and not Path(configured).exists()
        and initial_dir is not None
        and Path(configured) == Path(initial_dir)
    )
    return _validate_directory_or_http_error(configured, create=should_create)


def _scan_library_payload(library_dir: Optional[Path]) -> Dict[str, Any]:
    if library_dir is None:
        return {"path": "", "library_dir": None, "tracks": []}

    lib_path = Path(library_dir)
    if not lib_path.exists() or not lib_path.is_dir():
        return {"path": str(lib_path), "library_dir": None, "tracks": []}

    tracks: List[Dict[str, Any]] = []
    audio_paths = sorted(
        (
            p
            for p in lib_path.rglob("*")
            if p.is_file() and p.suffix.lower() in AUDIO_SUFFIXES
        ),
        key=lambda p: (p.relative_to(lib_path).as_posix().lower(), str(p).lower()),
    )
    for audio_path in audio_paths:
        meta = extract_metadata(audio_path)
        rel_name = (
            audio_path.name
            if audio_path.parent == lib_path
            else audio_path.relative_to(lib_path).as_posix()
        )
        tracks.append(
            {
                "filename": rel_name,
                "artist": meta.artist,
                "title": meta.title,
                "bitrate": meta.bitrate,
                "file_size": meta.file_size,
                "duration": meta.duration,
                "format": audio_path.suffix.lstrip(".").lower(),
                "path": str(meta.path),
            }
        )
    return {"path": str(lib_path), "library_dir": str(lib_path), "tracks": tracks}


def _normalize_sources(
    sources: Optional[Union[str, Sequence[str]]],
    source: Optional[Union[str, Sequence[str]]],
) -> List[str]:
    raw_input = sources if sources is not None else source
    if raw_input is None:
        return ["all"]

    tokens: List[str] = []
    items = [raw_input] if isinstance(raw_input, str) else list(raw_input)
    for item in items:
        for part in str(item).split(","):
            cleaned = part.strip().lower()
            if cleaned:
                tokens.append(cleaned)

    if not tokens:
        raise HTTPException(status_code=400, detail="Search source name cannot be empty.")
    return tokens


def _execute_search(
    *,
    query: str,
    sources: List[str],
    limit: int,
    search_type: str,
    deduplicate: bool,
) -> tuple[List[SearchResult], Dict[str, str]]:
    capped_limit = max(0, min(int(limit), 100))
    source_arg: Union[str, List[str]] = sources[0] if len(sources) == 1 else sources
    try:
        return _invoke_search_tracks_with_errors(
            query=query,
            sources=source_arg,
            limit=capped_limit,
            search_type=search_type,
            deduplicate=deduplicate,
        )
    except UnsupportedSearchSourceError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except SearchError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


def _run_search_payload(
    payload: SearchRequest,
    *,
    require_query: bool = False,
    missing_query_detail: str = "Search query cannot be empty.",
) -> tuple[str, List[str], List[SearchResult], Dict[str, str]]:
    effective_query = payload.query if payload.query is not None else (payload.q or "")
    if require_query and not effective_query.strip():
        raise HTTPException(status_code=400, detail=missing_query_detail)
    normalized_sources = _normalize_sources(payload.sources, payload.source)
    effective_type = (payload.search_type or payload.type or "track").strip().lower()
    results, errors = _execute_search(
        query=effective_query,
        sources=normalized_sources,
        limit=payload.limit,
        search_type=effective_type,
        deduplicate=payload.deduplicate,
    )
    return effective_query, normalized_sources, results, errors


def _dict_to_search_result(item: Dict[str, Any]) -> SearchResult:
    raw_duration = item.get("duration_seconds")
    duration_seconds: Optional[int] = None
    if raw_duration is not None and raw_duration != "":
        try:
            duration_seconds = int(raw_duration)
        except (TypeError, ValueError):
            duration_seconds = None

    raw_extra = item.get("extra")
    extra = dict(raw_extra) if isinstance(raw_extra, dict) else {}

    return SearchResult(
        source=str(item.get("source") or ""),
        track_id=str(item.get("track_id") or ""),
        title=str(item.get("title") or ""),
        artist=str(item.get("artist") or ""),
        album=str(item["album"]) if item.get("album") is not None else None,
        duration_seconds=duration_seconds,
        url=str(item.get("url") or ""),
        preview_url=str(item["preview_url"]) if item.get("preview_url") else None,
        cover_url=str(item["cover_url"]) if item.get("cover_url") else None,
        isrc=str(item["isrc"]) if item.get("isrc") else None,
        quality_hint=str(item["quality_hint"]) if item.get("quality_hint") else None,
        extra=extra,
    )


def _serialize_track_result(track: TrackDownloadResult) -> Dict[str, Any]:
    return {
        "url": track.url,
        "artist": track.artist,
        "title": track.title,
        "status": track.status,
        "file_path": str(track.file_path) if track.file_path is not None else None,
        "error": track.error,
    }


def _serialize_duplicate_result(dup: DuplicateResolutionResult) -> Dict[str, Any]:
    return {
        "retained": str(dup.retained),
        "deleted": [str(p) for p in dup.deleted],
        "artist": dup.artist,
        "title": dup.title,
        "reason": dup.reason,
    }


def _copy_job_snapshot(job: Dict[str, Any]) -> Dict[str, Any]:
    return {
        **job,
        "tracks": [dict(t) for t in job.get("tracks", [])],
        "duplicates_resolved": [dict(d) for d in job.get("duplicates_resolved", [])],
        "organized_files": list(job.get("organized_files", [])),
    }


def _execute_download_job(
    app: FastAPI,
    records: Sequence[TrackRecord],
    library_dir: Path,
    *,
    max_concurrent: int = 2,
    skip_existing: bool = True,
    auto_organize: bool = True,
    auto_deduplicate: bool = True,
) -> Dict[str, Any]:
    job_id = uuid.uuid4().hex
    created_at = _utc_now_iso()
    downloader_cls = _get_downloader_cls()
    downloader = downloader_cls(
        library_dir=library_dir,
        max_concurrent=max_concurrent,
        skip_existing=skip_existing,
        auto_organize=auto_organize,
        auto_deduplicate=auto_deduplicate,
    )
    summary: DownloadSummary = downloader.download(list(records))
    job_record: Dict[str, Any] = {
        "job_id": job_id,
        "status": "completed",
        "library_path": str(library_dir),
        "library_dir": str(library_dir),
        "total_tracks": len(records),
        "downloaded": summary.downloaded,
        "skipped": summary.skipped,
        "failed": summary.failed,
        "tracks": [_serialize_track_result(t) for t in summary.tracks],
        "duplicates_resolved": [
            _serialize_duplicate_result(d) for d in summary.duplicates_resolved
        ],
        "organized_files": [str(p) for p in summary.organized_files],
        "error": None,
        "created_at": created_at,
        "completed_at": _utc_now_iso(),
    }
    with app.state.jobs_lock:
        app.state.jobs[job_id] = job_record
        return _copy_job_snapshot(job_record)


def _build_records_from_job_request(app: FastAPI, req: JobCreateRequest) -> List[TrackRecord]:
    has_explicit_input = any(
        field is not None
        for field in (req.urls, req.records, req.csv_text, req.tracks)
    )
    if not has_explicit_input:
        with app.state.jobs_lock:
            cached = list(app.state.last_upload_records)
        if not cached:
            raise HTTPException(
                status_code=400,
                detail="No tracks available. Upload a CSV via POST /api/upload or supply urls/records/csv_text.",
            )
        return cached

    records: List[TrackRecord] = []

    if req.records is not None:
        for item in req.records:
            raw_url = str(item.get("url") or "").strip()
            if not _is_valid_url(raw_url):
                raise HTTPException(
                    status_code=400,
                    detail=f"Invalid track URL in records: {item.get('url')!r}",
                )
            raw_artist = item.get("artist")
            raw_title = item.get("title")
            artist = str(raw_artist).strip() if raw_artist is not None and str(raw_artist).strip() else None
            title = str(raw_title).strip() if raw_title is not None and str(raw_title).strip() else None
            raw_row = {str(k): str(v) for k, v in item.items() if v is not None}
            records.append(TrackRecord(url=raw_url, artist=artist, title=title, raw_row=raw_row))

    if req.urls is not None:
        for raw_url in req.urls:
            cleaned = (raw_url or "").strip()
            if not cleaned:
                continue
            if not _is_valid_url(cleaned):
                raise HTTPException(
                    status_code=400,
                    detail=f"Invalid URL: {cleaned!r}",
                )
            records.append(TrackRecord(url=cleaned, raw_row={"url": cleaned}))

    if req.csv_text is not None:
        try:
            records.extend(parse_csv(req.csv_text))
        except CSVProcessorError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    if req.tracks is not None:
        for item in req.tracks:
            if isinstance(item, str):
                cleaned = item.strip()
                if cleaned:
                    if not _is_valid_url(cleaned):
                        raise HTTPException(status_code=400, detail=f"Invalid URL: {cleaned!r}")
                    records.append(TrackRecord(url=cleaned, raw_row={"url": cleaned}))
            else:
                cleaned = (item.url or "").strip()
                if cleaned:
                    if not _is_valid_url(cleaned):
                        raise HTTPException(status_code=400, detail=f"Invalid URL: {cleaned!r}")
                    artist = item.artist.strip() if item.artist and item.artist.strip() else None
                    title = item.title.strip() if item.title and item.title.strip() else None
                    records.append(TrackRecord(url=cleaned, artist=artist, title=title))

    if not records:
        raise HTTPException(
            status_code=400,
            detail="No valid tracks provided.",
        )
    return records


def _parse_bool_flag(raw: Optional[str], fallback: bool) -> bool:
    if raw is None:
        return fallback
    return raw.strip().lower() in {"1", "true", "yes", "on"}


def create_app(default_library_dir: str | Path | None = None) -> FastAPI:
    """Create and configure the Songetter FastAPI application."""
    application = FastAPI(
        title="Songetter",
        version=__version__,
        description="Server-side music library ingestion, multi-source search, and organization API.",
    )

    initial_dir = _resolve_initial_library_dir(default_library_dir)
    application.state._initial_library_dir = initial_dir
    application.state.library_dir = initial_dir
    application.state.jobs = {}
    application.state.jobs_lock = threading.RLock()
    application.state.last_upload_records = []

    @application.get("/", response_class=HTMLResponse)
    def index() -> HTMLResponse:
        with application.state.jobs_lock:
            lib_dir: Path = application.state.library_dir
        sources = _invoke_list_search_sources()
        html = _DASHBOARD_TEMPLATE.render(
            version=__version__,
            library_dir=str(lib_dir),
            library_exists=lib_dir.exists() and lib_dir.is_dir(),
            sources=sources,
        )
        return HTMLResponse(content=html, status_code=200)

    @application.post("/api/library")
    def select_library(payload: LibrarySelectRequest) -> Dict[str, str]:
        resolved = _validate_directory_or_http_error(
            payload.path,
            create=payload.create or payload.create_if_missing,
        )
        with application.state.jobs_lock:
            application.state.library_dir = resolved
        return {"path": str(resolved), "status": "ok"}

    @application.get("/api/library")
    def get_library() -> Dict[str, Any]:
        with application.state.jobs_lock:
            lib_dir = Path(application.state.library_dir)
        return {
            "path": str(lib_dir),
            "exists": lib_dir.exists() and lib_dir.is_dir(),
        }

    @application.get("/api/library/tracks")
    def get_library_tracks() -> Dict[str, Any]:
        with application.state.jobs_lock:
            lib_dir = application.state.library_dir
        return _scan_library_payload(lib_dir)

    @application.get("/api/library/tracks/{filename:path}/stream")
    def stream_library_track(filename: str) -> FileResponse:
        decoded = unquote(filename)
        if (
            not filename
            or not decoded
            or "\x00" in filename
            or "\x00" in decoded
            or "/" in filename
            or "\\" in filename
            or ".." in filename
            or "/" in decoded
            or "\\" in decoded
            or ".." in decoded
            or filename in (".", "..")
        ):
            raise HTTPException(status_code=400, detail="Invalid track filename.")

        with application.state.jobs_lock:
            lib_dir = Path(application.state.library_dir).resolve()

        target = (lib_dir / filename).resolve()
        if target.parent != lib_dir:
            raise HTTPException(status_code=400, detail="Invalid track path.")

        if (
            not target.exists()
            or not target.is_file()
            or target.suffix.lower() not in AUDIO_SUFFIXES
        ):
            raise HTTPException(status_code=404, detail="Track not found.")

        media_type = _AUDIO_MEDIA_TYPES.get(
            target.suffix.lower(),
            mimetypes.guess_type(target.name)[0] or "application/octet-stream",
        )
        return FileResponse(path=target, media_type=media_type, filename=target.name)

    @application.post("/api/upload")
    async def upload_csv(
        request: Request,
        file: UploadFile = File(...),
        auto_download_form: bool = Form(default=False, alias="auto_download"),
    ) -> Dict[str, Any]:
        content = await file.read()
        try:
            records = parse_csv(content, filename=file.filename)
        except CSVProcessorError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

        unique_records = deduplicate_records(records)
        with application.state.jobs_lock:
            application.state.last_upload_records = list(unique_records)

        auto_download = _parse_bool_flag(
            request.query_params.get("auto_download"),
            auto_download_form,
        )
        job_id: Optional[str] = None
        if auto_download:
            lib_dir = _resolve_target_library(application, None, allow_initial_create=True)
            job_data = _execute_download_job(application, unique_records, lib_dir)
            job_id = job_data["job_id"]

        return {
            "filename": file.filename or "upload.csv",
            "total_tracks": len(records),
            "valid_tracks": len(unique_records),
            "preview": [
                {"url": r.url, "artist": r.artist, "title": r.title}
                for r in unique_records
            ],
            "job_id": job_id,
        }

    @application.post("/api/jobs")
    def create_job(
        payload: Optional[JobCreateRequest] = Body(default=None),
    ) -> Dict[str, Any]:
        req = payload or JobCreateRequest()
        records = _build_records_from_job_request(application, req)
        library_dir = _resolve_target_library(
            application,
            req.library_path,
            create=req.create,
            allow_initial_create=True,
        )
        return _execute_download_job(
            application,
            records,
            library_dir,
            max_concurrent=req.max_concurrent,
            skip_existing=req.skip_existing,
            auto_organize=req.auto_organize,
            auto_deduplicate=req.auto_deduplicate,
        )

    @application.get("/api/jobs")
    def list_jobs() -> Dict[str, Any]:
        with application.state.jobs_lock:
            jobs = [
                _copy_job_snapshot(job)
                for job in reversed(list(application.state.jobs.values()))
            ]
        return {"jobs": jobs, "count": len(jobs)}

    @application.get("/api/jobs/{job_id}")
    def get_job(job_id: str) -> Dict[str, Any]:
        with application.state.jobs_lock:
            job = application.state.jobs.get(job_id)
            if job is None:
                raise HTTPException(status_code=404, detail=f"Job not found: {job_id}")
            return _copy_job_snapshot(job)

    @application.post("/api/organize")
    def organize_library(
        payload: Optional[OrganizeRequest] = Body(default=None),
    ) -> Dict[str, Any]:
        path_override = payload.library_path if payload is not None else None
        lib_dir = _resolve_target_library(
            application,
            path_override,
            create=False,
            allow_initial_create=False,
        )
        duplicates = resolve_duplicates(lib_dir)
        organized = flatten_and_organize_library(lib_dir)
        return {
            "status": "ok",
            "path": str(lib_dir),
            "duplicates_resolved_count": len(duplicates),
            "deleted_count": sum(len(d.deleted) for d in duplicates),
            "organized_count": len(organized),
            "filenames": [p.name for p in organized],
            "duplicates_resolved": [_serialize_duplicate_result(d) for d in duplicates],
        }

    @application.get("/api/search/sources")
    def get_search_sources() -> Dict[str, Any]:
        return {"sources": _invoke_list_search_sources()}

    @application.get("/api/search")
    def search_get(
        q: str = Query(default=""),
        query: Optional[str] = Query(default=None),
        source: Optional[List[str]] = Query(default=None),
        sources: Optional[List[str]] = Query(default=None),
        limit: int = Query(default=10),
        search_type: Optional[str] = Query(default=None),
        type_param: Optional[str] = Query(default=None, alias="type"),
        deduplicate: bool = Query(default=False),
    ) -> Dict[str, Any]:
        effective_query = q if q != "" else (query or "")
        normalized_sources = _normalize_sources(sources, source)
        effective_type = (search_type or type_param or "track").strip().lower()
        results, errors = _execute_search(
            query=effective_query,
            sources=normalized_sources,
            limit=limit,
            search_type=effective_type,
            deduplicate=deduplicate,
        )
        return {
            "query": effective_query,
            "sources": normalized_sources,
            "count": len(results),
            "results": [r.to_dict() for r in results],
            "errors": errors,
        }

    @application.post("/api/search")
    def search_post(payload: SearchRequest) -> Dict[str, Any]:
        effective_query, normalized_sources, results, errors = _run_search_payload(payload)
        return {
            "query": effective_query,
            "sources": normalized_sources,
            "count": len(results),
            "results": [r.to_dict() for r in results],
            "errors": errors,
        }

    @application.post("/api/search/export")
    def export_search_csv(payload: SearchExportRequest) -> Response:
        if payload.results is not None:
            search_results = [_dict_to_search_result(item) for item in payload.results]
        else:
            _, _, search_results, _ = _run_search_payload(
                payload,
                require_query=True,
                missing_query_detail="Provide either 'results' or a non-empty 'query' to export CSV.",
            )

        csv_payload = _invoke_search_results_to_csv(search_results)
        return Response(
            content=csv_payload,
            media_type="text/csv; charset=utf-8",
            headers={"Content-Disposition": 'attachment; filename="songetter_search.csv"'},
        )

    @application.post("/api/search/download")
    def download_search_results(payload: SearchDownloadRequest) -> Dict[str, Any]:
        library_dir = _resolve_target_library(
            application,
            payload.library_path,
            create=payload.create,
            allow_initial_create=True,
        )

        raw_items: Optional[List[Dict[str, Any]]] = (
            payload.results if payload.results is not None else payload.tracks
        )

        if raw_items is not None:
            search_results = [_dict_to_search_result(item) for item in raw_items]
        else:
            _, _, search_results, _ = _run_search_payload(
                payload,
                require_query=True,
                missing_query_detail="Provide 'results', 'tracks', or a non-empty 'query' to download.",
            )

        records = [
            sr.to_track_record(use_preview_if_no_url=payload.use_preview_if_no_url)
            for sr in search_results
        ]
        valid_records = [r for r in records if (r.url or "").strip()]
        if not valid_records:
            raise HTTPException(
                status_code=400,
                detail="No downloadable track URLs found in search items.",
            )

        job = _execute_download_job(
            application,
            valid_records,
            library_dir,
            max_concurrent=payload.max_concurrent,
            skip_existing=payload.skip_existing,
            auto_organize=payload.auto_organize,
            auto_deduplicate=payload.auto_deduplicate,
        )
        return {
            "job_id": job["job_id"],
            "status": job["status"],
            "submitted_tracks": len(valid_records),
            "total_tracks": len(valid_records),
            "library_dir": job["library_dir"],
        }

    return application


app = create_app()
