// Imperative-JS island for the read-only map view (docs/spec-serve-ui.md
// section 6/7). Not content-hashed -- it gets Cache-Control: no-store from
// the server automatically, which is correct for a file that can change on
// re-vendor.
//
// CSP-compatible by construction: no inline handlers, no eval, no
// innerHTML/HTML-string interpolation (popups are built with
// document.createElement + textContent only).
(function () {
  "use strict";

  var mapEl = document.getElementById("map");
  var configEl = document.getElementById("map-config");
  if (!mapEl || !configEl) {
    return;
  }
  // Idempotent island: guard against double init if this script somehow
  // runs twice (e.g. a future Datastar morph re-executes it).
  if (mapEl.dataset.mapInitialized === "true") {
    return;
  }
  mapEl.dataset.mapInitialized = "true";

  var config = JSON.parse(configEl.textContent);
  var layers = config.layers || {};

  // Fixed per-layer palette -- no computed/derived colors.
  var PALETTE = {
    track_points: "#1f77b4",
    messages: "#d62728",
    waypoints: "#2ca02c",
    events: "#9467bd",
    tracks: "#ff7f0e",
    routes: "#8c564b",
    courses: "#17becf",
    trips: "#bcbd22",
  };
  var DEFAULT_COLOR = "#555555";
  // Fields shown in point-layer popups, in display order. Mirrors the
  // server's PROPERTY_ALLOWLIST; textContent-only, never HTML.
  var POPUP_FIELDS = ["name", "timestamp_utc", "event", "text", "device_name"];

  maplibregl.setWorkerUrl(mapEl.dataset.workerUrl);

  var map = new maplibregl.Map({
    container: "map",
    style: {
      version: 8,
      sources: {},
      layers: [
        {
          id: "background",
          type: "background",
          paint: { "background-color": "#e8e8e8" },
        },
      ],
    },
    center: [0, 0],
    zoom: 1,
  });

  function buildPopupContent(properties) {
    var container = document.createElement("div");
    POPUP_FIELDS.forEach(function (field) {
      var value = properties ? properties[field] : undefined;
      if (value === undefined || value === null || value === "") {
        return;
      }
      var row = document.createElement("div");
      var label = document.createElement("strong");
      label.textContent = field + ": ";
      row.appendChild(label);
      row.appendChild(document.createTextNode(String(value)));
      container.appendChild(row);
    });
    return container;
  }

  function setLayerVisibility(name, visible) {
    if (!map.getLayer(name)) {
      return;
    }
    map.setLayoutProperty(name, "visibility", visible ? "visible" : "none");
  }

  function toggleFor(name) {
    var match = null;
    document.querySelectorAll(".layer-toggle").forEach(function (checkbox) {
      if (checkbox.dataset.layer === name) {
        match = checkbox;
      }
    });
    return match;
  }

  function addLayers() {
    // Toggles can be clicked before "load" fires (their change listener is
    // wired up below, independent of map readiness), so a toggle event
    // reaching this point before the layer exists would otherwise be lost.
    // Read each checkbox's current state here and use it as the layer's
    // initial visibility instead of always adding visible.
    Object.keys(layers).forEach(function (name) {
      var info = layers[name];
      var color = PALETTE[name] || DEFAULT_COLOR;
      var toggle = toggleFor(name);
      var visible = !toggle || toggle.checked;
      var visibility = visible ? "visible" : "none";
      map.addSource(name, {
        type: "geojson",
        data: "/api/layers/" + name + ".geojson",
      });
      if (info.kind === "point") {
        map.addLayer({
          id: name,
          type: "circle",
          source: name,
          layout: { visibility: visibility },
          paint: {
            "circle-radius": 4,
            "circle-color": color,
          },
        });
        map.on("click", name, function (event) {
          var feature = event.features && event.features[0];
          if (!feature) {
            return;
          }
          new maplibregl.Popup()
            .setLngLat(event.lngLat)
            .setDOMContent(buildPopupContent(feature.properties))
            .addTo(map);
        });
      } else {
        map.addLayer({
          id: name,
          type: "line",
          source: name,
          layout: { visibility: visibility },
          paint: {
            "line-color": color,
            "line-width": 2,
          },
        });
      }
    });
  }

  function visibleBounds() {
    var bounds = null;
    document.querySelectorAll(".layer-toggle").forEach(function (checkbox) {
      if (!checkbox.checked) {
        return;
      }
      var info = layers[checkbox.dataset.layer];
      var bbox = info && info.bbox;
      if (!bbox) {
        return;
      }
      if (bounds === null) {
        bounds = bbox.slice();
        return;
      }
      bounds[0] = Math.min(bounds[0], bbox[0]);
      bounds[1] = Math.min(bounds[1], bbox[1]);
      bounds[2] = Math.max(bounds[2], bbox[2]);
      bounds[3] = Math.max(bounds[3], bbox[3]);
    });
    return bounds;
  }

  function fitToVisibleData() {
    var bounds = visibleBounds();
    if (bounds) {
      map.fitBounds(
        [
          [bounds[0], bounds[1]],
          [bounds[2], bounds[3]],
        ],
        { padding: 40, animate: false }
      );
    }
  }

  map.on("load", function () {
    addLayers();
    fitToVisibleData();
  });

  document.querySelectorAll(".layer-toggle").forEach(function (checkbox) {
    checkbox.addEventListener("change", function () {
      setLayerVisibility(checkbox.dataset.layer, checkbox.checked);
    });
  });

  // Custom-event contract stub (docs/spec-serve-ui.md section 6): other UI
  // pieces (none yet in phase A) can toggle layers or focus a feature
  // without reaching into map.js internals.
  window.addEventListener("garmin:layer-toggle", function (event) {
    var detail = event.detail || {};
    setLayerVisibility(detail.layer, !!detail.visible);
    var checkbox = toggleFor(detail.layer);
    if (checkbox) {
      checkbox.checked = !!detail.visible;
    }
  });

  window.addEventListener("garmin:focus-feature", function (event) {
    var detail = event.detail || {};
    var bbox = detail.bbox;
    if (!bbox) {
      return;
    }
    map.fitBounds(
      [
        [bbox[0], bbox[1]],
        [bbox[2], bbox[3]],
      ],
      { padding: 40, animate: false }
    );
  });
})();
