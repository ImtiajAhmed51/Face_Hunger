/**
 * Local-only MapLibre style. No glyphs, no sprites and no remote sources: with
 * tiles off the map is a plain canvas with a graticule; with a user-supplied
 * PMTiles file it gains a basemap read from this app's own server.
 */
import type { FeatureCollection, LineString } from 'geojson';
import type { LayerSpecification, StyleSpecification } from 'maplibre-gl';

export interface PmtilesInfo {
  tile_type: string;
  min_zoom: number;
  max_zoom: number;
  bounds: [number, number, number, number];
  vector_layers: string[];
  attribution?: string | null;
}

export interface Palette { background: string; graticule: string; land: string; water: string; road: string; boundary: string }

export const LIGHT: Palette = { background: '#eef1f5', graticule: '#c9d1dc', land: '#e3e7ec', water: '#bcd3ea', road: '#c7ccd4', boundary: '#9aa6b5' };
export const DARK: Palette = { background: '#141922', graticule: '#2a3342', land: '#1d2430', water: '#16273b', road: '#303a4a', boundary: '#4b576a' };

export function graticule(step = 15): FeatureCollection<LineString> {
  const features: FeatureCollection<LineString>['features'] = [];
  for (let lon = -180; lon <= 180; lon += step) {
    features.push({ type: 'Feature', properties: { kind: 'meridian' }, geometry: { type: 'LineString', coordinates: Array.from({ length: 33 }, (_, i) => [lon, -80 + i * 5]) } });
  }
  for (let lat = -75; lat <= 75; lat += step) {
    features.push({ type: 'Feature', properties: { kind: lat === 0 ? 'equator' : 'parallel' }, geometry: { type: 'LineString', coordinates: Array.from({ length: 73 }, (_, i) => [-180 + i * 5, lat]) } });
  }
  return { type: 'FeatureCollection', features };
}

function basemapLayers(info: PmtilesInfo, palette: Palette): LayerSpecification[] {
  if (info.tile_type !== 'mvt') {
    return [{ id: 'basemap-raster', type: 'raster', source: 'basemap', paint: { 'raster-opacity': 0.9 } }];
  }
  const layers: LayerSpecification[] = [];
  const colourFor = (name: string) => /water|ocean|lake|river/i.test(name) ? palette.water
    : /road|transport|street|highway/i.test(name) ? palette.road
    : /boundar|admin/i.test(name) ? palette.boundary : palette.land;
  for (const name of info.vector_layers) {
    const colour = colourFor(name);
    layers.push({ id: `bm-${name}-fill`, type: 'fill', source: 'basemap', 'source-layer': name,
      filter: ['==', ['geometry-type'], 'Polygon'], paint: { 'fill-color': colour, 'fill-opacity': 0.9 } });
    layers.push({ id: `bm-${name}-line`, type: 'line', source: 'basemap', 'source-layer': name,
      filter: ['==', ['geometry-type'], 'LineString'],
      paint: { 'line-color': colour, 'line-width': /road|transport/i.test(name) ? 1.2 : 0.8,
        ...(/boundar|admin/i.test(name) ? { 'line-dasharray': [2, 2] } : {}) } });
  }
  return layers;
}

export function buildStyle(options: { pmtilesUrl?: string | null; pmtiles?: PmtilesInfo | null; palette?: Palette } = {}): StyleSpecification {
  const palette = options.palette ?? LIGHT;
  const sources: StyleSpecification['sources'] = {
    graticule: { type: 'geojson', data: graticule() },
  };
  let base: LayerSpecification[] = [];
  if (options.pmtilesUrl && options.pmtiles) {
    const info = options.pmtiles;
    sources.basemap = info.tile_type === 'mvt'
      ? { type: 'vector', url: `pmtiles://${options.pmtilesUrl}`, attribution: info.attribution ?? undefined }
      : { type: 'raster', url: `pmtiles://${options.pmtilesUrl}`, tileSize: 256, attribution: info.attribution ?? undefined };
    base = basemapLayers(info, palette);
  }
  return {
    version: 8,
    sources,
    layers: [
      { id: 'background', type: 'background', paint: { 'background-color': palette.background } },
      ...base,
      { id: 'graticule', type: 'line', source: 'graticule',
        paint: { 'line-color': palette.graticule, 'line-width': ['match', ['get', 'kind'], 'equator', 1.2, 0.6] } },
    ],
  };
}

/** Every string in the style that could make the browser fetch something remote. */
export function remoteUrls(style: unknown, origin?: string): string[] {
  const found: string[] = [];
  const walk = (value: unknown) => {
    if (typeof value === 'string' && /^(https?:|pmtiles:)?\/\//i.test(value)) found.push(value);
    else if (Array.isArray(value)) value.forEach(walk);
    else if (value && typeof value === 'object') Object.values(value).forEach(walk);
  };
  walk(style);
  return found.filter((url) => !isLocalUrl(url, origin));
}

/** Same-origin, relative, data: and blob: URLs are local; everything else is blocked. */
export function isLocalUrl(url: string, origin = typeof location !== 'undefined' ? location.origin : 'http://localhost'): boolean {
  if (/^(data|blob):/i.test(url) || url.startsWith('/') && !url.startsWith('//')) return true;
  if (url.startsWith('pmtiles://')) return isLocalUrl(url.slice('pmtiles://'.length), origin);
  try {
    return new URL(url, origin).origin === origin;
  } catch {
    return false;
  }
}

/** Bounding box [minLon, minLat, maxLon, maxLat] of points. */
export function bounds(points: [number, number][]): [number, number, number, number] | null {
  if (!points.length) return null;
  let minLon = 180, minLat = 90, maxLon = -180, maxLat = -90;
  for (const [lon, lat] of points) {
    if (lon < minLon) minLon = lon;
    if (lon > maxLon) maxLon = lon;
    if (lat < minLat) minLat = lat;
    if (lat > maxLat) maxLat = lat;
  }
  return [minLon, minLat, maxLon, maxLat];
}
