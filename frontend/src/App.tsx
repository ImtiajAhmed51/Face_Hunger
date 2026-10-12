import { Button } from "@/components/primitives/button";
import { Component, Suspense, lazy, useEffect, useRef, useState } from "react";
import type { ErrorInfo, ReactNode } from "react";
import {
  Link,
  NavLink,
  Route,
  Routes,
  useLocation,
  useNavigate,
} from "react-router-dom";
import { useResource } from "./hooks";
import type { Dashboard } from "./types";
import { Icon } from "./components/Icon";
import type { IconName } from "./components/Icon";
import { JobCard } from "./components/JobCard";
import { CommandPalette } from "./components/CommandPalette";
import { MediaCollection } from "./components/MediaGrid";
import { Dialog, Empty, PageHeader } from "./components/ui";
import { Cleanup } from "./pages/Cleanup";
import { Duplicates } from "./pages/Duplicates";
import { Home } from "./pages/Home";
import { Clusters } from "./pages/Clusters";
import { People, PersonProfile } from "./pages/People";
import { Review } from "./pages/Review";
import { Search } from "./pages/Search";
import { Settings } from "./pages/Settings";
// Phase 2 screens are code-split: each loads on first visit (MapLibre alone is ~290 KB gzip).
import { routeRendered } from "./diag";
import { Libraries, OfflineBanner } from "./components/Libraries";
import { useT } from "./i18n";
const MapPage = lazy(() => import("./pages/MapPage"));
const Health = lazy(() => import("./pages/Health").then((m) => ({ default: m.Health })));
const Diagnostics = lazy(() => import("./pages/Diagnostics").then((m) => ({ default: m.Diagnostics })));
const Plugins = lazy(() => import("./pages/Plugins").then((m) => ({ default: m.Plugins })));
const Storage = lazy(() => import("./pages/Storage").then((m) => ({ default: m.Storage })));
const Timeline = lazy(() => import("./pages/Timeline").then((m) => ({ default: m.Timeline })));
const Events = lazy(() => import("./pages/Events").then((m) => ({ default: m.Events })));
const EventDetail = lazy(() => import("./pages/Events").then((m) => ({ default: m.EventDetail })));
const Albums = lazy(() => import("./pages/Albums").then((m) => ({ default: m.Albums })));
const AlbumDetail = lazy(() => import("./pages/Albums").then((m) => ({ default: m.AlbumDetail })));
const CollectionDetail = lazy(() => import("./pages/Albums").then((m) => ({ default: m.CollectionDetail })));
const Favorites = lazy(() => import("./pages/Albums").then((m) => ({ default: m.Favorites })));

function ScreenLoading() {
  return <div className="gallery-skeleton" role="status" aria-label="Loading"><span className="sr-only">Loading</span>
    {Array.from({ length: 8 }, (_, i) => <div key={i} className="skeleton-tile" aria-hidden="true" />)}</div>;
}

const navigation: { to: string; label: string; icon: IconName; group?: boolean }[] = [
  { to: "/photos", label: "Photos", icon: "photo" },
  { to: "/search", label: "Search", icon: "search" },
  { to: "/people", label: "People", icon: "people" },
  { to: "/albums", label: "Albums", icon: "folder" },
  { to: "/favorites", label: "Favorites", icon: "heart" },
  { to: "/recently-added", label: "Recently added", icon: "calendar" },
  { to: "/videos", label: "Videos", icon: "video", group: true },
  { to: "/events", label: "Events", icon: "calendar" },
  { to: "/map", label: "Places", icon: "map" },
  { to: "/review", label: "Face review", icon: "review", group: true },
  { to: "/clusters", label: "Face clusters", icon: "layers" },
  { to: "/libraries", label: "Libraries", icon: "folder", group: true },
  { to: "/settings", label: "Settings", icon: "settings" },
  { to: "/overview", label: "Overview", icon: "home", group: true },
  { to: "/browse", label: "Media browser", icon: "photo" },
  { to: "/no-faces", label: "No faces", icon: "hidden" },
  { to: "/deleted", label: "Deleted", icon: "trash" },
  { to: "/duplicates", label: "Duplicates", icon: "merge" },
  { to: "/cleanup", label: "Cleanup", icon: "cleanup" },
  { to: "/storage", label: "Storage", icon: "drive" },
  { to: "/health", label: "Health", icon: "shield" },
  { to: "/plugins", label: "Plugins", icon: "plug" },
  { to: "/diagnostics", label: "Diagnostics", icon: "activity" },
];
const primaryNav = navigation.slice(0, 4);

function isPrimaryPath(pathname: string, to: string) {
  if (to === "/") return pathname === "/";
  return pathname === to || pathname.startsWith(`${to}/`);
}

function Brand() {
  return (
    <Link className="brand" to="/photos" aria-label="Face Hunger home">
      <span className="brand-mark" aria-hidden="true">
        <svg viewBox="0 0 32 32" width="28" height="28" fill="none">
          <path
            d="M11 5H8a3 3 0 0 0-3 3v3m16-6h3a3 3 0 0 1 3 3v3M5 21v3a3 3 0 0 0 3 3h3m16-6v3a3 3 0 0 1-3 3h-3"
            stroke="currentColor"
            strokeWidth="1.8"
            strokeLinecap="round"
          />
          <path
            d="M11 12v2m10-2v2m-5-2v6h-2m-3 3c2.8 2.6 7.2 2.6 10 0"
            stroke="currentColor"
            strokeWidth="1.8"
            strokeLinecap="round"
            strokeLinejoin="round"
          />
        </svg>
      </span>
      <span>
        Face Hunger<small>PRIVATE · LOCAL</small>
      </span>
    </Link>
  );
}
function Navigation({ onNavigate }: { onNavigate?: () => void }) {
  const location = useLocation();
  const [toolsOpen, setToolsOpen] = useState(false);
  const extraActive = navigation.slice(13).some(item => item.to === location.pathname);
  useEffect(() => { if (extraActive) setToolsOpen(true); }, [extraActive, location.pathname]);
  const { data } = useResource<Dashboard>("/dashboard");
  return (
    <nav className="navigation" aria-label="Main navigation">
      {navigation.slice(0, 13).map((item) => (
        <NavLink
          key={item.to}
          to={item.to}
          end={item.to === "/"}
          title={item.label}
          aria-label={item.label}
          className={({ isActive }) =>
            `nav-link ${isActive ? "active" : ""} ${item.group ? "nav-group" : ""}`
          }
          onClick={onNavigate}
        >
          <Icon name={item.icon} size={18} />
          <span>{item.label}</span>
          {item.to === "/review" && !!data?.review_count && (
            <span className="nav-count">
              {data.review_count > 999 ? "999+" : data.review_count}
            </span>
          )}
          {item.to === "/search" && <kbd>/</kbd>}
        </NavLink>
      ))}
      <details className="nav-tools" open={toolsOpen}>
        <summary onClick={event => { event.preventDefault(); setToolsOpen(value => !value); }} title="More tools">
          <Icon name="menu" size={18} /><span>More tools</span>
        </summary>
        <div className="nav-tools-links">{navigation.slice(13).map(item => <NavLink key={item.to} to={item.to} title={item.label}
          aria-label={item.label} className={({ isActive }) => `nav-link ${isActive ? "active" : ""}`} onClick={onNavigate}>
          <Icon name={item.icon} size={18} /><span>{item.label}</span>
        </NavLink>)}</div>
      </details>
    </nav>
  );
}

function BottomNav({ onMore, expanded }: { onMore: () => void; expanded: boolean }) {
  const { data } = useResource<Dashboard>("/dashboard");
  const location = useLocation();
  const extraActive = !primaryNav.some((item) =>
    isPrimaryPath(location.pathname, item.to),
  );
  return (
    <nav className="bottom-nav" aria-label="Primary">
      {primaryNav.map((item) => {
        const active = isPrimaryPath(location.pathname, item.to);
        return (
          <NavLink
            key={item.to}
            to={item.to}
            end={item.to === "/"}
            className={`bottom-nav-item${active ? " active" : ""}`}
          >
            <span className="bottom-nav-icon">
              <Icon name={item.icon} size={20} />
              {item.to === "/review" && !!data?.review_count && (
                <span className="bottom-nav-badge">
                  {data.review_count > 99 ? "99+" : data.review_count}
                </span>
              )}
            </span>
            <span>{item.label}</span>
          </NavLink>
        );
      })}
      <Button
        type="button"
        className={`bottom-nav-item${extraActive ? " active" : ""}`}
        aria-label="More pages"
        aria-expanded={expanded}
        aria-haspopup="dialog"
        onClick={onMore}
      >
        <span className="bottom-nav-icon">
          <Icon name="menu" size={20} />
        </span>
        <span>More</span>
      </Button>
    </nav>
  );
}
export class ErrorBoundary extends Component<
  { children: ReactNode },
  { error: string }
> {
  state = { error: "" };
  static getDerivedStateFromError(error: Error) {
    return { error: error.message };
  }
  componentDidCatch(error: Error, info: ErrorInfo) {
    console.error("Frontend rendering error", error, info.componentStack);
  }
  render() {
    return this.state.error ? (
      <main className="fatal-error">
        <h1>The collection couldn't open.</h1>
        <p>{this.state.error}</p>
        <p>Your files have not been changed. Reload to try again.</p>
        <Button
          className="button primary"
          onClick={() => window.location.reload()}
        >
          Reload application
        </Button>
      </main>
    ) : (
      this.props.children
    );
  }
}

function LibrariesPage() {
  const t = useT();
  return (
    <>
      <PageHeader eyebrow={t("lib.pageEyebrow")} title={t("lib.title")} description={t("lib.pageDescription")} />
      <Libraries />
    </>
  );
}

export function App() {
  const [sidebarCollapsed, setSidebarCollapsed] = useState(() => {
    try { return localStorage.getItem("lfs-sidebar-collapsed") === "true"; } catch { return false; }
  });
  const toggleSidebar = () => setSidebarCollapsed(value => {
    try { localStorage.setItem("lfs-sidebar-collapsed", String(!value)); } catch { /* optional */ }
    return !value;
  });
  useEffect(() => {
    document.documentElement.classList.toggle("sidebar-collapsed", sidebarCollapsed);
    return () => document.documentElement.classList.remove("sidebar-collapsed");
  }, [sidebarCollapsed]);
  const [mobileOpen, setMobileOpen] = useState(false);
  const [paletteOpen, setPaletteOpen] = useState(false);
  const location = useLocation();
  const navigate = useNavigate();
  const main = useRef<HTMLElement>(null);
  const pageContent = useRef<HTMLDivElement>(null);
  useEffect(() => routeRendered(location.pathname), [location.pathname]);
  // Keep the current destination visible when the sidebar is taller than the window.
  useEffect(() => { document.querySelector('.sidebar .nav-link.active')?.scrollIntoView?.({ block: 'nearest' }); }, [location.pathname]);
  useEffect(() => {
    if (window.matchMedia('(prefers-reduced-motion: reduce)').matches) return;
    const animation = pageContent.current?.animate(
      [{ opacity: 0.4, transform: 'translateY(5px)' }, { opacity: 1, transform: 'none' }],
      { duration: 220, easing: 'cubic-bezier(0.16, 1, 0.3, 1)' },
    );
    return () => animation?.cancel();
  }, [location.pathname]);
  useEffect(() => {
    setMobileOpen(false);
    main.current?.focus({ preventScroll: true });
    window.scrollTo({ top: 0, behavior: "instant" });
    const page =
      navigation.find((item) => item.to === location.pathname)?.label ??
      (location.pathname === "/" ? "Photos" : location.pathname.startsWith("/people/") ? "Person" : "Collection");
    document.title = `${page} - Face Hunger`;
  }, [location.pathname]);
  useEffect(() => {
    const onKey = (event: KeyboardEvent) => {
      if (event.key.toLowerCase() === "k" && (event.metaKey || event.ctrlKey) && !event.altKey) {
        event.preventDefault();
        setPaletteOpen((value) => !value);
        return;
      }
      if (
        event.key !== "/" ||
        event.ctrlKey ||
        event.metaKey ||
        event.altKey ||
        document.querySelector("dialog[open]")
      )
        return;
      if (
        event.target instanceof HTMLElement &&
        event.target.closest("input,textarea,select,[contenteditable=true]")
      )
        return;
      event.preventDefault();
      navigate("/search");
      setTimeout(() => document.getElementById("natural-query")?.focus(), 0);
    };
    document.addEventListener("keydown", onKey);
    return () => document.removeEventListener("keydown", onKey);
  }, [navigate]);
  return (
    <>
      <a
        className="skip-link"
        href="#main-content"
        onClick={(event) => {
          event.preventDefault();
          main.current?.focus();
        }}
      >
        Skip to content
      </a>
      <aside className="sidebar" id="desktop-navigation">
        <Brand />
        <Navigation />
        <div className="sidebar-bottom">
          <JobCard compact />
        </div>
      </aside>
      <header className="mobile-header">
        <Brand />
        <div className="mobile-header-actions">
          <Link className="icon-button" to="/search" aria-label="Search">
            <Icon name="search" size={20} />
          </Link>
          <Button
            className="icon-button"
            aria-label="Open navigation"
            aria-expanded={mobileOpen}
            onClick={() => setMobileOpen(true)}
          >
            <Icon name="menu" size={20} />
          </Button>
        </div>
      </header>
      <Dialog
        open={mobileOpen}
        onClose={() => setMobileOpen(false)}
        title="Your collection"
        className="mobile-navigation"
      >
        <Navigation onNavigate={() => setMobileOpen(false)} />
        <JobCard compact />
      </Dialog>
      <main id="main-content" className="main-content" tabIndex={-1} ref={main}>
        <OfflineBanner />
        <div className="topline">
          <Button className="icon-button sidebar-toggle" type="button" onClick={toggleSidebar}
            aria-label={sidebarCollapsed ? "Expand sidebar" : "Collapse sidebar"}
            aria-expanded={!sidebarCollapsed} aria-controls="desktop-navigation" title={sidebarCollapsed ? "Expand sidebar" : "Collapse sidebar"}>
            <Icon name="menu" />
          </Button>
          <form className="topbar-search" role="search" onSubmit={(event) => {
            event.preventDefault();
            const q = new FormData(event.currentTarget).get("q")?.toString().trim() ?? "";
            navigate(q ? `/search?q=${encodeURIComponent(q)}` : "/search");
          }}>
            <Icon name="search" size={20} />
            <input name="q" type="search" aria-label="Search your photos" placeholder="Search your photos" autoComplete="off" />
          </form>
          <span className="inline-actions">
            <Button type="button" className="button ghost small" onClick={() => setPaletteOpen(true)}
              aria-keyshortcuts="Control+K Meta+K">
              <Icon name="search" size={14} />
              Quick actions <kbd>⌘K</kbd>
            </Button>
            <Link to="/settings">
              <Icon name="shield" size={14} />
              On your device
            </Link>
          </span>
        </div>
        <div className="page-content" ref={pageContent}>
        <Suspense fallback={<ScreenLoading />}>
        <Routes>
          <Route path="/" element={<Timeline photos />} />
          <Route path="/overview" element={<Home />} />
          <Route path="/photos" element={<Timeline photos />} />
          <Route path="/recently-added" element={<>
            <PageHeader title="Recently added" description="Photos and videos ordered by when they were indexed." />
            <MediaCollection key="recent" initialSort="indexed" />
          </>} />
          <Route path="/people" element={<People />} />
          <Route path="/people/:id" element={<PersonProfile />} />
          <Route path="/clusters" element={<Clusters />} />
          <Route
            path="/browse"
            element={
              <>
                <PageHeader
                  eyebrow="YOUR LIBRARY"
                  title="Media browser"
                  description="Filter, select and organize your photos and videos."
                />
                <MediaCollection
                  key="photos"
                  emptyTitle="A collection waiting to happen"
                  emptyDescription="Connect a photo folder in Settings and start your first scan. Your originals stay right where they are."
                />
              </>
            }
          />
          <Route
            path="/videos"
            element={
              <>
                <PageHeader
                  eyebrow="MEMORIES IN MOTION"
                  title="Your videos"
                  description="Find familiar faces, right down to the moment they appear."
                />
                <MediaCollection
                  key="videos"
                  filters={{ kind: "video" }}
                  emptyTitle="Make room for the moving moments"
                  emptyDescription="Index a library containing videos to find people at exact sampled timestamps. Start with a folder in Settings."
                />
              </>
            }
          />
          <Route
            path="/no-faces"
            element={
              <>
                <PageHeader
                  eyebrow="NO ONE IN THE FRAME"
                  title="Media without faces"
                  description="Photos and videos where the engine found no faces. Browse them here separately from your people collection. Deleting moves items to the Deleted tab; originals stay on disk until you permanently remove them."
                />
                <MediaCollection
                  key="no-faces"
                  filters={{ no_faces: true }}
                  emptyTitle="Every file has a face"
                  emptyDescription="When a scan finds media with no detectable faces, those items will appear here."
                />
              </>
            }
          />
          <Route
            path="/deleted"
            element={
              <>
                <PageHeader
                  eyebrow="SOFT DELETED"
                  title="Deleted"
                  description="Media you soft-deleted. Restore them anytime, or permanently remove original files from disk. Empty Deleted clears everything listed here."
                />
                <MediaCollection
                  key="deleted"
                  deletedOnly
                  emptyTitle="Nothing in Deleted"
                  emptyDescription="Soft-deleted photos and videos will appear here. You can restore them or permanently delete the files from disk."
                />
              </>
            }
          />
          <Route path="/search" element={<Search />} />
          <Route path="/review" element={<Review />} />
          <Route path="/duplicates" element={<Duplicates />} />
          <Route path="/cleanup" element={<Cleanup />} />
          <Route path="/settings" element={<Settings />} />
          <Route path="/health" element={<Health />} />
          <Route path="/storage" element={<Storage />} />
          <Route path="/libraries" element={<LibrariesPage />} />
          <Route path="/plugins" element={<Plugins />} />
          <Route path="/diagnostics" element={<Diagnostics />} />
          <Route path="/timeline" element={<Timeline />} />
          <Route path="/events" element={<Events />} />
          <Route path="/albums" element={<Albums />} />
          <Route path="/albums/:id" element={<AlbumDetail />} />
          <Route path="/collections/:id" element={<CollectionDetail />} />
          <Route path="/favorites" element={<Favorites />} />
          <Route path="/events/:id" element={<EventDetail />} />
          <Route path="/map" element={<MapPage />} />
          <Route
            path="*"
            element={
              <Empty
                icon="search"
                title="This room hasn't been found"
                description="That page isn't part of this collection."
              >
                <Link className="button primary" to="/photos">
                  Back to photos
                </Link>
              </Empty>
            }
          />
        </Routes>
        </Suspense>
        </div>
        <footer className="page-footer">
          <span>
            Face Hunger <span aria-hidden="true">/</span> Your memories,
            locally.
          </span>
          <span>Your library. Your control.</span>
        </footer>
      </main>
      <BottomNav onMore={() => setMobileOpen(true)} expanded={mobileOpen} />
      <CommandPalette open={paletteOpen} onClose={() => setPaletteOpen(false)}
        pages={navigation.map((item) => ({ to: item.to, label: item.label }))} />
    </>
  );
}
