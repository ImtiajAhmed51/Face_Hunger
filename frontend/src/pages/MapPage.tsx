import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import maplibregl, { type GeoJSONSource, type Map as MapLibre } from "maplibre-gl";
import "maplibre-gl/dist/maplibre-gl.css";
import { Protocol } from "pmtiles";
import { number } from "../api";
import { useApp } from "../context";
import { useResource, useSelection } from "../hooks";
import { useT } from "../i18n";
import type { Media } from "../types";
import { Icon } from "../components/Icon";
import { VirtualMediaGrid } from "../components/MediaGrid";
import { MediaViewer } from "../components/MediaViewer";
import { Empty, ErrorNotice, Loading, PageHeader } from "../components/ui";
import { useWindowedPages } from "../virtual/useWindowedPages";
import { idsBetween } from "../virtual/geometry";
import { DARK, LIGHT, bounds, buildStyle, isLocalUrl, type PmtilesInfo } from "../map/style";

interface Points { count: number; ids: number[]; lat: number[]; lon: number[]; video: number[] }
interface MapConfig { tiles_enabled: boolean; pmtiles: PmtilesInfo | null; pmtiles_url: string | null; error: string | null }
interface Cluster { id: number; count: number; lon: number; lat: number }

let protocolAdded = false;

export function MapPage() {
  const t = useT();
  const { theme } = useApp();
  const points = useResource<Points>("/map/points");
  const config = useResource<MapConfig>("/map/config");
  const container = useRef<HTMLDivElement>(null);
  const mapRef = useRef<MapLibre | null>(null);
  const [bbox, setBbox] = useState<[number, number, number, number] | null>(null);
  const [clusters, setClusters] = useState<Cluster[]>([]);
  const [renderMs, setRenderMs] = useState<number | null>(null);
  const [viewer, setViewer] = useState<number | null>(null);
  const [anchor, setAnchor] = useState<number | null>(null);
  const dark = theme === "dark" || (theme === "system" && typeof matchMedia !== "undefined" && matchMedia("(prefers-color-scheme: dark)").matches);

  const geojson = useMemo(() => {
    const data = points.data;
    if (!data) return null;
    const features = data.ids.map((id, i) => ({
      type: "Feature" as const, properties: { id, video: data.video[i] },
      geometry: { type: "Point" as const, coordinates: [data.lon[i], data.lat[i]] },
    }));
    return { type: "FeatureCollection" as const, features };
  }, [points.data]);

  const markers = useRef(new Map<number, maplibregl.Marker>());
  const refreshClusters = useCallback(() => {
    const map = mapRef.current;
    if (!map || !map.getSource("media")) return;
    const seen = new Map<number, Cluster>();
    for (const f of map.querySourceFeatures("media", { filter: ["has", "point_count"] })) {
      const id = f.properties?.cluster_id as number;
      if (!seen.has(id)) {
        const [lon, lat] = (f.geometry as GeoJSON.Point).coordinates;
        seen.set(id, { id, count: f.properties?.point_count as number, lon, lat });
      }
    }
    setClusters([...seen.values()].sort((a, b) => b.count - a.count).slice(0, 12));
    // Count labels as HTML markers: text layers would need font glyphs from a server.
    const live = markers.current;
    for (const [id, marker] of live) {
      if (!seen.has(id)) { marker.remove(); live.delete(id); }
    }
    for (const cluster of [...seen.values()].slice(0, 150)) {
      if (live.has(cluster.id)) continue;
      const el = document.createElement("span");
      el.className = "map-count";
      el.textContent = cluster.count >= 1000 ? `${Math.round(cluster.count / 100) / 10}k` : String(cluster.count);
      el.setAttribute("aria-hidden", "true");
      live.set(cluster.id, new maplibregl.Marker({ element: el }).setLngLat([cluster.lon, cluster.lat]).addTo(map));
    }
  }, []);

  const selectCluster = useCallback(async (clusterId: number) => {
    const map = mapRef.current;
    const source = map?.getSource("media") as GeoJSONSource | undefined;
    if (!map || !source) return;
    const leaves = await source.getClusterLeaves(clusterId, Infinity, 0);
    const box = bounds(leaves.map((f) => (f.geometry as GeoJSON.Point).coordinates as [number, number]));
    if (box) setBbox(box);
    const zoom = await source.getClusterExpansionZoom(clusterId);
    const center = leaves[0] ? (leaves[0].geometry as GeoJSON.Point).coordinates as [number, number] : map.getCenter().toArray();
    map.easeTo({ center: box ? [(box[0] + box[2]) / 2, (box[1] + box[3]) / 2] : center, zoom,
      duration: matchMedia("(prefers-reduced-motion: reduce)").matches ? 0 : 400 });
  }, []);

  useEffect(() => {
    if (!container.current || !geojson || !config.data || mapRef.current) return;
    if (!protocolAdded) {
      maplibregl.addProtocol("pmtiles", new Protocol().tile);
      protocolAdded = true;
    }
    const started = performance.now();
    const pmtilesUrl = config.data.pmtiles_url ? `${location.origin}${config.data.pmtiles_url}` : null;
    const map = new maplibregl.Map({
      container: container.current,
      style: buildStyle({ pmtilesUrl, pmtiles: config.data.pmtiles, palette: dark ? DARK : LIGHT }),
      center: [0, 20], zoom: 1.3, attributionControl: false, renderWorldCopies: false,
      // Belt and braces: nothing outside this app's origin is ever requested.
      transformRequest: (url) => (isLocalUrl(url) ? { url } : { url: "data:application/json,{}" }),
    });
    mapRef.current = map;
    map.on("load", () => {
      map.addSource("media", { type: "geojson", data: geojson, cluster: true, clusterRadius: 48, clusterMaxZoom: 15 });
      map.addLayer({ id: "clusters", type: "circle", source: "media", filter: ["has", "point_count"],
        paint: { "circle-color": "#0a84ff", "circle-opacity": 0.85, "circle-stroke-color": "#ffffff", "circle-stroke-width": 1.5,
          "circle-radius": ["step", ["get", "point_count"], 12, 20, 16, 200, 22, 2000, 30, 20000, 38] } });
      map.addLayer({ id: "point", type: "circle", source: "media", filter: ["!", ["has", "point_count"]],
        paint: { "circle-color": ["match", ["get", "video"], 1, "#ff9f0a", "#0a84ff"], "circle-radius": 5,
          "circle-stroke-color": "#ffffff", "circle-stroke-width": 1 } });
      const box = bounds(geojson.features.map((f) => f.geometry.coordinates as [number, number]));
      if (box) map.fitBounds(box, { padding: 40, duration: 0, maxZoom: 12 });
      map.once("idle", () => { setRenderMs(Math.round(performance.now() - started)); refreshClusters(); });
    });
    map.on("moveend", refreshClusters);
    map.on("click", "clusters", (e) => {
      const id = e.features?.[0]?.properties?.cluster_id;
      if (id !== undefined) void selectCluster(id);
    });
    map.on("click", "point", (e) => {
      const f = e.features?.[0];
      if (!f) return;
      const [lon, lat] = (f.geometry as GeoJSON.Point).coordinates;
      setBbox([lon - 1e-5, lat - 1e-5, lon + 1e-5, lat + 1e-5]);
    });
    for (const layer of ["clusters", "point"]) {
      map.on("mouseenter", layer, () => { map.getCanvas().style.cursor = "pointer"; });
      map.on("mouseleave", layer, () => { map.getCanvas().style.cursor = ""; });
    }
    const live = markers.current;
    return () => { live.forEach((m) => m.remove()); live.clear(); map.remove(); mapRef.current = null; };
  }, [geojson, config.data, dark, refreshClusters, selectCluster]);

  // Map points are rounded to 5 decimals (~1 m); pad by one unit so edge points of a
  // cluster, which may have been rounded inward, are never dropped from the grid.
  const bboxParam = bbox ? [bbox[0] - 1e-5, bbox[1] - 1e-5, bbox[2] + 1e-5, bbox[3] + 1e-5]
    .map((v) => v.toFixed(5)).join(",") : null;
  const windowed = useWindowedPages<Media>(bboxParam ? `/media?bbox=${bboxParam}` : null, 120);
  const selection = useSelection(bbox ? bbox.join(",") : "none");

  return (
    <>
      <PageHeader eyebrow={t("map.eyebrow")} title={t("map.title")} description={t("map.description")} />
      <ErrorNotice error={points.error || config.error} retry={() => { points.reload(); config.reload(); }} />
      {points.loading || config.loading ? <Loading label={t("common.loading")} /> : !points.data?.count ? (
        <Empty icon="search" title={t("map.none")} description={t("map.noneDescription")} />
      ) : (
        <>
          <div className="collection-bar">
            <span className="muted small-text" role="status">
              {t("map.count", { count: number(points.data.count) })}
              {renderMs !== null && ` · ${renderMs} ms`}
            </span>
            <span className="muted small-text">
              {config.data?.error ? t("map.tilesError", { error: config.data.error }) : !config.data?.pmtiles ? t("map.tilesOff") : config.data.pmtiles.attribution}
            </span>
          </div>
          <div className="map-layout">
            <div className="map-frame">
              <div ref={container} className="map-canvas" role="region" aria-label={t("map.canvas")} data-render-ms={renderMs ?? undefined} />
              <div className="map-controls">
                <button className="icon-button" aria-label={t("map.zoomIn")} onClick={() => mapRef.current?.zoomIn()}><Icon name="plus" size={18} /></button>
                <button className="icon-button" aria-label={t("map.zoomOut")} onClick={() => mapRef.current?.zoomOut()}><span aria-hidden="true">−</span></button>
                <button className="icon-button" aria-label={t("map.fit")} onClick={() => {
                  const data = points.data!;
                  const box = bounds(data.lon.map((lon, i) => [lon, data.lat[i]]));
                  if (box) mapRef.current?.fitBounds(box, { padding: 40, maxZoom: 12 });
                }}><Icon name="expand" size={18} /></button>
              </div>
            </div>
            <ul className="map-places" aria-label={t("map.title")}>
              {clusters.map((cluster) => (
                <li key={cluster.id}>
                  <button className="button small" onClick={() => void selectCluster(cluster.id)}>
                    {cluster.lat.toFixed(2)}, {cluster.lon.toFixed(2)} · {number(cluster.count)}
                  </button>
                </li>
              ))}
            </ul>
          </div>
          {bbox && (
            <section className="collection" aria-label={t("map.selection", { count: number(windowed.count) })}>
              <div className="collection-bar">
                <strong>{t("map.selection", { count: number(windowed.count) })}</strong>
                <button className="button ghost small" onClick={() => setBbox(null)}>{t("map.clear")}</button>
              </div>
              <VirtualMediaGrid windowed={windowed} selected={selection.selected} label={t("map.selection", { count: windowed.count })}
                onOpen={setViewer} onClear={selection.clear} onSelectAll={() => selection.addRange(windowed.loaded.map((m) => m.id))}
                onSelect={(id, index, mode) => {
                  if (mode === "range" && anchor !== null) selection.addRange(idsBetween(anchor, index, windowed.getItem));
                  else { setAnchor(index); selection.toggle(id); }
                }} />
            </section>
          )}
        </>
      )}
      {viewer !== null && <MediaViewer id={viewer} ids={windowed.loaded.map((m) => m.id)} onClose={() => setViewer(null)} />}
    </>
  );
}

export default MapPage;
