(function installOutpostMap(global) {
  "use strict";

  const TILE_SIZE = 256;
  const MAX_LATITUDE = 85.05112878;
  const MODE_KEY = "outpost.map.basemap-mode";
  const controllers = new Set();
  let manifestPromise = null;
  let basemapMode = readMode();
  let basemapModeSaved = true;

  function readMode(fallback = "local-first") {
    try {
      return localStorage.getItem(MODE_KEY) === "offline-only" ? "offline-only" : "local-first";
    } catch (_) {
      return fallback;
    }
  }

  function onStorage(event) {
    if (event.key !== MODE_KEY && event.key !== null) return;
    try { if (event.storageArea !== localStorage) return; } catch (_) { return; }
    basemapMode = event.newValue === "offline-only" ? "offline-only" : "local-first";
    basemapModeSaved = true;
    for (const controller of controllers) controller._applyBasemapMode();
  }

  function clamp(value, minimum, maximum) {
    return Math.min(maximum, Math.max(minimum, value));
  }

  function normalizeLongitude(value) {
    return ((value + 180) % 360 + 360) % 360 - 180;
  }

  function project(latitude, longitude, zoom) {
    const scale = TILE_SIZE * 2 ** zoom;
    const lat = clamp(Number(latitude), -MAX_LATITUDE, MAX_LATITUDE);
    const sin = Math.sin(lat * Math.PI / 180);
    return {
      x: (normalizeLongitude(Number(longitude)) + 180) / 360 * scale,
      y: (0.5 - Math.log((1 + sin) / (1 - sin)) / (4 * Math.PI)) * scale,
    };
  }

  function unproject(x, y, zoom) {
    const scale = TILE_SIZE * 2 ** zoom;
    const longitude = normalizeLongitude(x / scale * 360 - 180);
    const n = Math.PI - 2 * Math.PI * y / scale;
    return {
      lat: clamp(180 / Math.PI * Math.atan(Math.sinh(n)), -MAX_LATITUDE, MAX_LATITUDE),
      lon: longitude,
    };
  }

  function loadManifest(refresh = false) {
    if (!manifestPromise || refresh) {
      const abort = new AbortController();
      const deadline = setTimeout(() => abort.abort(), 5000);
      manifestPromise = fetch("/tiles/manifest.json", {cache: "no-store", signal: abort.signal})
        .then(async response => {
          const value = await response.json().catch(() => null);
          if (!response.ok) {
            return {
              status: value?.status === "unreadable" ? "unreadable" :
                response.status === 404 || value?.status === "missing" ? "missing" : "unreachable",
              manifest: null,
            };
          }
          if (!value || typeof value !== "object" || Array.isArray(value)) {
            return {status: "unreadable", manifest: null};
          }
          // Older backends do not include status. Presence is not a coverage certificate.
          if (!value.status || value.status === "ready") {
            return {status: "ready", manifest: value};
          }
          return {
            status: value.status === "unreadable" ? "unreadable" :
              value.status === "missing" ? "missing" : "unreachable",
            manifest: null,
          };
        })
        .catch(() => ({status: "unreachable", manifest: null}))
        .finally(() => clearTimeout(deadline));
    }
    return manifestPromise;
  }

  function element(value, root, fallbackClass) {
    if (value instanceof Element) return value;
    if (typeof value === "string") return root.querySelector(value);
    return root.querySelector(`.${fallbackClass}`);
  }

  class Controller {
    constructor(options) {
      if (!options?.root) throw new Error("OutpostMap requires a root element.");
      this.root = options.root;
      this.tilesLayer = element(options.tiles, this.root, "outpost-map-tiles");
      this.markersLayer = element(options.markers, this.root, "outpost-map-markers");
      this.coordinates = element(options.coordinates, this.root, "outpost-map-coordinates");
      this.emptyState = element(options.empty, this.root, "outpost-map-empty");
      this.detail = element(options.detail, this.root, "outpost-map-detail");
      if (!this.tilesLayer || !this.markersLayer) {
        throw new Error("OutpostMap requires tile and marker layers.");
      }

      this.options = options;
      this.minZoom = Number.isFinite(options.minZoom) ? options.minZoom : 2;
      this.maxZoom = Number.isFinite(options.maxZoom) ? options.maxZoom : 19;
      const initial = options.initialView || {};
      this.initialView = {
        lat: Number.isFinite(initial.lat) ? initial.lat : 40.4406,
        lon: Number.isFinite(initial.lon) ? initial.lon : -79.9959,
        zoom: Number.isFinite(initial.zoom) ? initial.zoom : 11,
      };
      this.view = {...this.initialView};
      this.view.zoom = clamp(Math.round(this.view.zoom), this.minZoom, this.maxZoom);
      this.markerDefinitions = new Map();
      this.markerElements = new Map();
      this.tileElements = new Map();
      this.tileRequests = new WeakMap();
      this.selectedId = null;
      this.drag = null;
      this.renderFrame = null;
      this.destroyed = false;
      this.tilePack = {status: "checking", manifest: null};
      this.manifestEpoch = 0;
      this.tileReloadToken = "";
      if (!controllers.size && basemapModeSaved) basemapMode = readMode(basemapMode);
      this.basemapMode = options.offlineOnly ? "offline-only" : basemapMode;
      this.modeSaved = basemapModeSaved;
      this.metrics = {
        frames: 0,
        pointerMoves: 0,
        tileCreates: 0,
        tileRemoves: 0,
        markerCreates: 0,
        markerRemoves: 0,
        renderMilliseconds: 0,
        maxFrameMilliseconds: 0,
      };

      this.root.classList.add("outpost-map");
      this.root.outpostMapController = this;
      this.root.tabIndex = this.root.tabIndex < 0 ? 0 : this.root.tabIndex;
      if (!this.root.hasAttribute("role")) this.root.setAttribute("role", "region");
      this.root.setAttribute(
        "aria-keyshortcuts",
        "ArrowUp ArrowDown ArrowLeft ArrowRight + - 0 Home Escape",
      );
      this._ensureSupportElements();
      this._bindEvents();
      if (!controllers.size) global.addEventListener("storage", onStorage);
      controllers.add(this);
      this._refreshManifest();
      this.requestRender();
    }

    _ensureSupportElements() {
      this.attribution = element(this.options.attribution, this.root, "outpost-map-attribution");
      if (!this.attribution) {
        this.attribution = document.createElement("div");
        this.attribution.className = "outpost-map-attribution";
        this.root.appendChild(this.attribution);
      }
      let footer = this.root.querySelector(".outpost-map-footer");
      if (!footer) {
        footer = document.createElement("div");
        footer.className = "outpost-map-footer";
        this.root.appendChild(footer);
      }
      if (this.coordinates) footer.appendChild(this.coordinates);
      footer.appendChild(this.attribution);
      this.basemapState = this.root.querySelector(".outpost-map-basemap-state");
      if (!this.basemapState) {
        this.basemapState = document.createElement("p");
        this.basemapState.className = "outpost-map-basemap-state";
        this.basemapState.hidden = true;
        this.basemapState.textContent = "Basemap unavailable · coordinates and markers remain active";
        this.root.appendChild(this.basemapState);
      }
      this.basemapState.setAttribute("role", "status");
      this.sourceControls = document.createElement("div");
      this.sourceControls.className = "outpost-map-source-controls";
      const label = document.createElement("label");
      label.title = "Use only local tiles at this Outpost address in this browser. Other dashboard feeds are unaffected.";
      this.offlineControl = document.createElement("input");
      this.offlineControl.type = "checkbox";
      this.offlineControl.disabled = Boolean(this.options.offlineOnly);
      this.offlineControl.checked = this.basemapMode === "offline-only";
      label.append(this.offlineControl, document.createTextNode("Offline only"));
      this.retryControl = document.createElement("button");
      this.retryControl.type = "button";
      this.retryControl.className = "ui-icon-button";
      this.retryControl.dataset.mapAction = "retry-tiles";
      this.retryControl.textContent = "↻";
      this.retryControl.title = "Retry tiles and recheck local pack";
      this.retryControl.setAttribute("aria-label", this.retryControl.title);
      this.sourceControls.append(label, this.retryControl);
      this.sourcePanel = document.createElement("div");
      this.sourcePanel.className = "outpost-map-source-panel";
      this.sourcePanel.append(this.sourceControls, this.basemapState);
      this.root.appendChild(this.sourcePanel);
    }

    async _refreshManifest(refresh = false) {
      if (this.destroyed) return;
      const epoch = ++this.manifestEpoch;
      // The service caches successful tile responses for a day, including corrupt bytes.
      // An explicit repair retry must not reuse that cached representation.
      if (refresh) this.tileReloadToken = crypto.getRandomValues(new Uint32Array(2)).join("-");
      this.tilePack = {status: "checking", manifest: null};
      this.retryControl.disabled = true;
      this._clearTiles();
      this.requestRender();
      const tilePack = await loadManifest(refresh);
      if (this.destroyed || epoch !== this.manifestEpoch) return;
      this.tilePack = tilePack;
      if (tilePack.manifest?.format === "outpost-vector-v1") {
        try {
          const {createVectorMap} = await import("/vector-map.js");
          if (this.destroyed || epoch !== this.manifestEpoch) return;
          this.vector = createVectorMap(this, tilePack.manifest);
        } catch (_) {
          this.vectorFailed = true;
        }
      }
      this.retryControl.disabled = false;
      this.requestRender();
    }

    setBasemapMode(mode) {
      if (this.destroyed || this.options.offlineOnly || !["local-first", "offline-only"].includes(mode)) return;
      basemapMode = mode;
      basemapModeSaved = true;
      try { localStorage.setItem(MODE_KEY, mode); } catch (_) { basemapModeSaved = false; }
      for (const controller of controllers) {
        controller._applyBasemapMode();
      }
    }

    _applyBasemapMode() {
      this.modeSaved = basemapModeSaved;
      const effectiveMode = this.options.offlineOnly ? "offline-only" : basemapMode;
      this.offlineControl.checked = effectiveMode === "offline-only";
      if (this.basemapMode !== effectiveMode) {
        // Retire callbacks synchronously, before another image event can start a fallback.
        this.basemapMode = effectiveMode;
        if (!this.vector) this._clearTiles();
      }
      this.requestRender();
    }

    _updateAttribution(counts) {
      const manifest = this.tilePack.manifest;
      const local = counts.localRequested && manifest ?
        [manifest.source, manifest.attribution].filter(value => typeof value === "string" && value)
          .join(" · ") || "Local basemap (attribution not supplied)" : "";
      const signature = JSON.stringify([local, counts.onlineRequested > 0]);
      if (signature === this.attributionSignature) return;
      this.attributionSignature = signature;
      this.attribution.replaceChildren();
      if (local) this.attribution.appendChild(document.createTextNode(`Local: ${local}`));
      if (counts.onlineRequested) {
        if (local) this.attribution.appendChild(document.createTextNode(" · "));
        const link = document.createElement("a");
        link.href = "https://www.openstreetmap.org/copyright";
        link.target = "_blank";
        link.rel = "noreferrer";
        link.textContent = "© OpenStreetMap contributors";
        this.attribution.appendChild(link);
      }
      this.attribution.hidden = !this.attribution.hasChildNodes();
    }

    _bindEvents() {
      this.onPointerDown = event => {
        if (event.button !== 0 || !event.isPrimary) return;
        if (event.target instanceof Element && event.target.closest("button, a, aside, label, input, select")) return;
        try {
          this.root.setPointerCapture(event.pointerId);
        } catch (_) {
          // Synthetic pointer events and older browsers may not support capture.
        }
        this.root.classList.add("dragging");
        this.drag = {
          x: event.clientX,
          y: event.clientY,
          center: project(this.view.lat, this.view.lon, this.view.zoom),
          moved: false,
        };
      };
      this.onPointerMove = event => {
        if (!this.drag) return;
        const dx = event.clientX - this.drag.x;
        const dy = event.clientY - this.drag.y;
        if (Math.abs(dx) + Math.abs(dy) > 2) this.drag.moved = true;
        const next = unproject(
          this.drag.center.x - dx,
          this.drag.center.y - dy,
          this.view.zoom,
        );
        this.view.lat = next.lat;
        this.view.lon = next.lon;
        this.metrics.pointerMoves += 1;
        this.requestRender();
      };
      this.onPointerUp = event => {
        if (!this.drag) return;
        const moved = this.drag.moved;
        this.drag = null;
        this.root.classList.remove("dragging");
        try {
          this.root.releasePointerCapture(event.pointerId);
        } catch (_) {
          // Pointer capture is best effort.
        }
        if (!moved && this.options.onBackground) this.options.onBackground();
      };
      this.onWheel = event => {
        if (event.target instanceof Element && event.target.closest("aside, input, select")) return;
        event.preventDefault();
        this.zoomBy(event.deltaY < 0 ? 1 : -1);
      };
      this.onClick = event => {
        if (!(event.target instanceof Element)) return;
        const control = event.target.closest("[data-map-action]");
        if (!control || !this.root.contains(control)) return;
        const action = control.dataset.mapAction;
        if (action === "zoom-in") this.zoomBy(1);
        if (action === "zoom-out") this.zoomBy(-1);
        if (action === "fit") this.options.onFit?.();
        if (action === "retry-tiles") this._refreshManifest(true);
        if (action === "home") {
          if (this.options.onHome) this.options.onHome();
          else this.setView(this.initialView);
        }
      };
      this.onKeyDown = event => {
        if (event.target instanceof Element && event.target.closest("input, select, textarea")) return;
        const step = event.shiftKey ? 160 : 64;
        const actions = {
          ArrowLeft: () => this.panByPixels(-step, 0),
          ArrowRight: () => this.panByPixels(step, 0),
          ArrowUp: () => this.panByPixels(0, -step),
          ArrowDown: () => this.panByPixels(0, step),
          "+": () => this.zoomBy(1),
          "=": () => this.zoomBy(1),
          "-": () => this.zoomBy(-1),
          _: () => this.zoomBy(-1),
          "0": () => this.options.onFit?.(),
          Home: () => this.options.onHome ? this.options.onHome() : this.setView(this.initialView),
          Escape: () => {
            this.clearSelection();
            this.options.onEscape?.();
          },
        };
        if (!actions[event.key]) return;
        event.preventDefault();
        actions[event.key]();
      };
      this.onResize = () => this.requestRender();
      this.onModeChange = () => this.setBasemapMode(
        this.offlineControl.checked ? "offline-only" : "local-first",
      );

      this.offlineControl.addEventListener("change", this.onModeChange);
      this.root.addEventListener("pointerdown", this.onPointerDown);
      this.root.addEventListener("pointermove", this.onPointerMove);
      this.root.addEventListener("pointerup", this.onPointerUp);
      this.root.addEventListener("pointercancel", this.onPointerUp);
      this.root.addEventListener("wheel", this.onWheel, {passive: false});
      this.root.addEventListener("click", this.onClick);
      this.root.addEventListener("keydown", this.onKeyDown);
      if ("ResizeObserver" in global) {
        this.resizeObserver = new ResizeObserver(this.onResize);
        this.resizeObserver.observe(this.root);
      } else {
        global.addEventListener("resize", this.onResize);
      }
    }

    setView(next) {
      if (Number.isFinite(next.lat)) this.view.lat = clamp(next.lat, -MAX_LATITUDE, MAX_LATITUDE);
      if (Number.isFinite(next.lon)) this.view.lon = normalizeLongitude(next.lon);
      if (Number.isFinite(next.zoom)) {
        this.view.zoom = clamp(Math.round(next.zoom), this.minZoom, this.maxZoom);
      }
      this.requestRender();
      return this.getView();
    }

    getView() {
      return {...this.view};
    }

    setMaxZoom(value) {
      this.maxZoom = Math.max(this.minZoom, Math.round(value));
      return this.setView({zoom: Math.min(this.view.zoom, this.maxZoom)});
    }

    zoomBy(delta) {
      return this.setView({zoom: this.view.zoom + delta});
    }

    panByPixels(dx, dy) {
      const center = project(this.view.lat, this.view.lon, this.view.zoom);
      const next = unproject(center.x + dx, center.y + dy, this.view.zoom);
      return this.setView(next);
    }

    fit(values, options = {}) {
      const points = values
        .map(value => ({lat: Number(value.lat), lon: Number(value.lon)}))
        .filter(value => Number.isFinite(value.lat) && Number.isFinite(value.lon));
      if (!points.length) {
        this.requestRender();
        return false;
      }
      const maximum = clamp(
        options.maxZoom ?? this.maxZoom,
        this.minZoom,
        this.maxZoom,
      );
      if (points.length === 1) {
        this.setView({...points[0], zoom: maximum});
        return true;
      }
      const padding = options.padding ?? 56;
      const availableWidth = Math.max(64, this.root.clientWidth - padding * 2);
      const availableHeight = Math.max(64, this.root.clientHeight - padding * 2);
      const projectBounds = value => {
        const projectedPoints = points.map(point => project(point.lat, point.lon, value));
        const worldWidth = TILE_SIZE * 2 ** value;
        const anchor = projectedPoints[0].x;
        for (const point of projectedPoints) {
          point.x += Math.round((anchor - point.x) / worldWidth) * worldWidth;
        }
        return projectedPoints;
      };
      let zoom = maximum;
      let projected = [];
      for (; zoom > this.minZoom; zoom -= 1) {
        projected = projectBounds(zoom);
        const xs = projected.map(value => value.x);
        const ys = projected.map(value => value.y);
        if (Math.max(...xs) - Math.min(...xs) <= availableWidth &&
            Math.max(...ys) - Math.min(...ys) <= availableHeight) break;
      }
      projected = projectBounds(zoom);
      const xs = projected.map(value => value.x);
      const ys = projected.map(value => value.y);
      const center = unproject(
        (Math.min(...xs) + Math.max(...xs)) / 2,
        (Math.min(...ys) + Math.max(...ys)) / 2,
        zoom,
      );
      this.setView({...center, zoom});
      return true;
    }

    setMarkers(definitions) {
      if (this.destroyed) return;
      const next = new Map();
      for (const definition of definitions) {
        const id = String(definition.id);
        const lat = Number(definition.lat);
        const lon = Number(definition.lon);
        if (!id || !Number.isFinite(lat) || !Number.isFinite(lon)) continue;
        next.set(id, {...definition, id, lat, lon});
      }
      this.markerDefinitions = next;
      this._syncMarkerElements();
      if (this.selectedId && !next.has(this.selectedId)) {
        this.clearSelection();
        if (this.detail) this.detail.hidden = true;
      }
      this.requestRender();
    }

    _syncMarkerElements() {
      for (const [id, marker] of this.markerElements) {
        if (this.markerDefinitions.has(id)) continue;
        marker.remove();
        this.markerElements.delete(id);
        this.metrics.markerRemoves += 1;
      }
      for (const [id, definition] of this.markerDefinitions) {
        let marker = this.markerElements.get(id);
        if (!marker) {
          marker = document.createElement("button");
          marker.type = "button";
          marker.dataset.markerId = id;
          const symbol = document.createElement("span");
          symbol.className = definition.variant === "footprint" ?
            "outpost-map-footprint-symbol" : "outpost-map-marker-symbol";
          symbol.setAttribute("aria-hidden", "true");
          marker.appendChild(symbol);
          marker.addEventListener("click", event => {
            event.stopPropagation();
            if (this.destroyed) return;
            const current = this.markerDefinitions.get(marker.dataset.markerId);
            if (!current) return;
            this.select(current.id);
            current.onActivate?.(current.data ?? current, event);
          });
          this.markersLayer.appendChild(marker);
          this.markerElements.set(id, marker);
          this.metrics.markerCreates += 1;
        }
        marker.className = `outpost-map-marker ${definition.className || ""}`.trim();
        marker.title = definition.title || definition.label || "Map marker";
        marker.setAttribute("aria-label", definition.label || marker.title);
        marker.classList.toggle("selected", id === this.selectedId);
        marker.setAttribute("aria-pressed", String(id === this.selectedId));
      }
    }

    select(id) {
      this.selectedId = id == null ? null : String(id);
      for (const [markerId, marker] of this.markerElements) {
        const selected = markerId === this.selectedId;
        marker.classList.toggle("selected", selected);
        marker.setAttribute("aria-pressed", String(selected));
      }
    }

    clearSelection() {
      this.select(null);
    }

    setEmpty(empty) {
      if (this.emptyState) this.emptyState.hidden = !empty;
    }

    requestRender() {
      if (this.destroyed || this.renderFrame !== null) return;
      this.renderFrame = requestAnimationFrame(() => {
        this.renderFrame = null;
        this._render();
      });
    }

    renderNow() {
      if (this.renderFrame !== null) cancelAnimationFrame(this.renderFrame);
      this.renderFrame = null;
      this._render();
    }

    _render() {
      if (this.destroyed) return;
      const startedAt = performance.now();
      this.metrics.frames += 1;
      const width = this.root.clientWidth;
      const height = this.root.clientHeight;
      if (!width || !height) return;
      const center = project(this.view.lat, this.view.lon, this.view.zoom);
      const viewport = {
        width,
        height,
        center,
        left: center.x - width / 2,
        top: center.y - height / 2,
      };
      if (this.vector) this.vector.render(viewport);
      else if (this.tilePack.manifest?.format === "outpost-vector-v1") {
        this.basemapState.textContent = this.vectorFailed ?
          "Offline map renderer unavailable · retry or use a browser with WebGL support" : "Loading offline map…";
        this.basemapState.hidden = false;
      } else this._renderTiles(viewport);
      this._renderMarkers(viewport);
      if (this.coordinates) {
        this.coordinates.textContent =
          `${this.view.lat.toFixed(5)}, ${this.view.lon.toFixed(5)} · z${this.view.zoom}`;
      }
      this.options.onViewChange?.(this.getView());
      const duration = performance.now() - startedAt;
      this.metrics.renderMilliseconds += duration;
      this.metrics.maxFrameMilliseconds = Math.max(
        this.metrics.maxFrameMilliseconds,
        duration,
      );
    }

    _renderTiles(viewport) {
      const needed = new Set();
      const zoom = this.view.zoom;
      const maximum = 2 ** zoom;
      const firstX = Math.floor(viewport.left / TILE_SIZE);
      const lastX = Math.ceil((viewport.left + viewport.width) / TILE_SIZE) - 1;
      const firstY = Math.floor(viewport.top / TILE_SIZE);
      const lastY = Math.ceil((viewport.top + viewport.height) / TILE_SIZE) - 1;
      for (let x = firstX; x <= lastX; x += 1) {
        for (let y = firstY; y <= lastY; y += 1) {
          if (y < 0 || y >= maximum) continue;
          const wrappedX = (x % maximum + maximum) % maximum;
          const key = `${zoom}/${x}/${y}`;
          needed.add(key);
          let image = this.tileElements.get(key);
          if (!image) {
            image = document.createElement("img");
            image.className = "outpost-map-tile";
            image.alt = "";
            image.draggable = false;
            image.referrerPolicy = "strict-origin-when-cross-origin";
            image.dataset.tileKey = key;
            image.dataset.state = "waiting";
            image.style.visibility = "hidden";
            this.tilesLayer.appendChild(image);
            this.tileElements.set(key, image);
            this.metrics.tileCreates += 1;
          }
          if (image.dataset.state === "waiting" && this.tilePack.status !== "checking") {
            const manifest = this.tilePack.manifest;
            if (manifest) {
              // The existing route handles legacy JPEG bytes even under a .png filename.
              const extension = manifest.tile_extension === "png" ? "png" : "jpg";
              const retry = this.tileReloadToken ? `?retry=${this.tileReloadToken}` : "";
              this._requestTile(key, image, "local", `/tiles/${zoom}/${wrappedX}/${y}.${extension}${retry}`);
            } else {
              image.dataset.localUnavailable = "true";
              this._onlineTile(key, image);
            }
          }
          image.style.transform =
            `translate3d(${Math.round(x * TILE_SIZE - viewport.left)}px, ` +
            `${Math.round(y * TILE_SIZE - viewport.top)}px, 0)`;
        }
      }
      for (const [key, image] of this.tileElements) {
        if (needed.has(key)) continue;
        this._removeTile(key, image);
      }
      this._updateBasemapState();
    }

    _requestTile(key, image, source, url) {
      if (this.destroyed || this.tileElements.get(key) !== image) return;
      if (source === "online" && this.basemapMode === "offline-only") return;
      const token = {};
      this.tileRequests.set(image, token);
      const current = () => !this.destroyed && this.tileElements.get(key) === image &&
        this.tileRequests.get(image) === token;
      image.dataset.source = source;
      image.dataset[`${source}Requested`] = "true";
      image.dataset.state = "loading";
      image.onload = () => {
        if (!current()) return;
        this.tileRequests.delete(image);
        image.onload = image.onerror = null;
        image.dataset.state = "loaded";
        image.style.visibility = "visible";
        this.requestRender();
      };
      image.onerror = () => {
        if (!current()) return;
        this.tileRequests.delete(image);
        image.onload = image.onerror = null;
        if (source === "local") {
          image.dataset.localUnavailable = "true";
          this._onlineTile(key, image);
        } else {
          image.dataset.state = "failed";
          image.removeAttribute("src");
        }
        this.requestRender();
      };
      image.src = url;
    }

    _onlineTile(key, image) {
      if (this.destroyed || this.tileElements.get(key) !== image) return;
      if (this.basemapMode === "offline-only") {
        image.dataset.state = "failed";
        image.removeAttribute("src");
        return;
      }
      const [zoom, x, y] = key.split("/").map(Number);
      const maximum = 2 ** zoom;
      const wrappedX = (x % maximum + maximum) % maximum;
      this._requestTile(key, image, "online", `https://tile.openstreetmap.org/${zoom}/${wrappedX}/${y}.png`);
    }

    _removeTile(key, image) {
      image.onload = image.onerror = null;
      this.tileRequests.delete(image);
      image.removeAttribute("src");
      image.remove();
      this.tileElements.delete(key);
      this.metrics.tileRemoves += 1;
    }

    _clearTiles() {
      this.vector?.destroy();
      this.vector = null;
      this.vectorFailed = false;
      for (const [key, image] of this.tileElements) this._removeTile(key, image);
    }

    _tileCounts() {
      const counts = {total: this.tileElements.size, waiting: 0, loading: 0, failed: 0,
        localLoaded: 0, onlineLoaded: 0, localUnavailable: 0, localRequested: 0, onlineRequested: 0};
      for (const image of this.tileElements.values()) {
        const state = image.dataset.state;
        if (state === "loaded") counts[`${image.dataset.source}Loaded`] += 1;
        else counts[state] += 1;
        for (const field of ["localUnavailable", "localRequested", "onlineRequested"]) {
          if (image.dataset[field] === "true") counts[field] += 1;
        }
      }
      return counts;
    }

    _updateBasemapState() {
      const counts = this._tileCounts();
      const status = this.tilePack.status;
      const messages = [];
      if (status === "checking") messages.push("Checking local tile pack…");
      else if (status === "missing") messages.push("No offline tile pack installed");
      else if (status === "unreadable") messages.push("Offline tile pack unreadable");
      else if (status === "unreachable") messages.push("Offline tile status unavailable");
      else if (counts.localUnavailable) {
        messages.push(`Local coverage incomplete: ${counts.localUnavailable}/${counts.total} visible tiles unavailable`);
      }
      if (counts.failed) messages.push(`${counts.failed}/${counts.total} basemap tiles unavailable`);
      if (counts.onlineRequested) {
        messages.push(counts.onlineLoaded ? "Internet fallback in use" :
          counts.loading ? "Internet fallback loading…" : "Internet fallback unavailable");
      } else if (counts.loading) messages.push("Loading local tiles…");
      if (!this.modeSaved) messages.push("Mode not saved · applies to this page only");
      if (messages.length) messages.push("Coordinates and markers remain active");
      const message = messages.join(" · ");
      if (this.basemapState.textContent !== message) this.basemapState.textContent = message;
      this.basemapState.hidden = !message;
      this.root.dataset.offlineTiles = status;
      this.root.dataset.basemapMode = this.basemapMode;
      this._updateAttribution(counts);
    }

    _renderMarkers(viewport) {
      const worldWidth = TILE_SIZE * 2 ** this.view.zoom;
      const metersPerPixel = Math.cos(this.view.lat * Math.PI / 180) *
        2 * Math.PI * 6378137 / worldWidth;
      for (const [id, definition] of this.markerDefinitions) {
        const marker = this.markerElements.get(id);
        if (!marker) continue;
        const point = project(definition.lat, definition.lon, this.view.zoom);
        while (point.x - viewport.center.x > worldWidth / 2) point.x -= worldWidth;
        while (viewport.center.x - point.x > worldWidth / 2) point.x += worldWidth;
        marker.style.left = `${point.x - viewport.left}px`;
        marker.style.top = `${point.y - viewport.top}px`;
        const size = typeof definition.size === "function" ?
          definition.size({
            zoom: this.view.zoom,
            centerLatitude: this.view.lat,
            metersPerPixel,
          }) : definition.size;
        if (Number.isFinite(size)) marker.style.setProperty("--marker-size", `${Math.max(8, size)}px`);
        else marker.style.removeProperty("--marker-size");
      }
    }

    getDiagnostics() {
      return {
        ...this.metrics,
        averageFrameMilliseconds: this.metrics.frames ?
          this.metrics.renderMilliseconds / this.metrics.frames : 0,
        liveTiles: this.tileElements.size,
        liveMarkers: this.markerElements.size,
        basemapMode: this.basemapMode,
        tileCounts: this._tileCounts(),
        view: this.getView(),
      };
    }

    destroy() {
      if (this.destroyed) return;
      this.destroyed = true;
      if (this.renderFrame !== null) cancelAnimationFrame(this.renderFrame);
      this.renderFrame = null;
      this.drag = null;
      this.root.classList.remove("dragging");
      this.root.removeEventListener("pointerdown", this.onPointerDown);
      this.root.removeEventListener("pointermove", this.onPointerMove);
      this.root.removeEventListener("pointerup", this.onPointerUp);
      this.root.removeEventListener("pointercancel", this.onPointerUp);
      this.root.removeEventListener("wheel", this.onWheel);
      this.root.removeEventListener("click", this.onClick);
      this.root.removeEventListener("keydown", this.onKeyDown);
      this.offlineControl.removeEventListener("change", this.onModeChange);
      this.sourcePanel.remove();
      this._clearTiles();
      for (const marker of this.markerElements.values()) marker.remove();
      this.metrics.markerRemoves += this.markerElements.size;
      this.markerElements.clear();
      this.markerDefinitions.clear();
      controllers.delete(this);
      if (!controllers.size) global.removeEventListener("storage", onStorage);
      this.resizeObserver?.disconnect();
      if (!this.resizeObserver) global.removeEventListener("resize", this.onResize);
      delete this.root.outpostMapController;
    }
  }

  global.OutpostMap = Object.freeze({Controller, project, unproject});
})(window);
