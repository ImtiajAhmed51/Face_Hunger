import { Component, useEffect, useRef, useState } from "react";
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
import { MediaCollection } from "./components/MediaGrid";
import { Dialog, Empty, PageHeader } from "./components/ui";
import { Cleanup } from "./pages/Cleanup";
import { Home } from "./pages/Home";
import { Clusters } from "./pages/Clusters";
import { People, PersonProfile } from "./pages/People";
import { Review } from "./pages/Review";
import { Search } from "./pages/Search";
import { Settings } from "./pages/Settings";

const navigation: {
  to: string;
  label: string;
  icon: IconName;
  group?: boolean;
}[] = [
  { to: "/", label: "Overview", icon: "home" },
  { to: "/people", label: "People", icon: "people" },
  { to: "/clusters", label: "Clusters", icon: "spark" },
  { to: "/photos", label: "Photos", icon: "photo" },
  { to: "/videos", label: "Videos", icon: "video" },
  { to: "/no-faces", label: "No faces", icon: "hidden" },
  { to: "/search", label: "Search", icon: "search", group: true },
  { to: "/review", label: "Review", icon: "review" },
  { to: "/cleanup", label: "Cleanup", icon: "cleanup" },
  { to: "/settings", label: "Settings", icon: "settings", group: true },
];

function Brand() {
  return (
    <Link className="brand" to="/" aria-label="Face Hunger home">
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
        Face Hunger<small>LOCAL FACE SEARCH</small>
      </span>
    </Link>
  );
}
function Navigation({ onNavigate }: { onNavigate?: () => void }) {
  const { data } = useResource<Dashboard>("/dashboard");
  return (
    <nav className="navigation" aria-label="Main navigation">
      {navigation.map((item) => (
        <NavLink
          key={item.to}
          to={item.to}
          end={item.to === "/"}
          className={({ isActive }) =>
            `nav-link ${isActive ? "active" : ""} ${item.group ? "nav-group" : ""}`
          }
          onClick={onNavigate}
        >
          <Icon name={item.icon} size={20} />
          <span>{item.label}</span>
          {item.to === "/review" && !!data?.review_count && (
            <span className="nav-count">
              {data.review_count > 999 ? "999+" : data.review_count}
            </span>
          )}
          {item.to === "/search" && <kbd>/</kbd>}
        </NavLink>
      ))}
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
        <button
          className="button primary"
          onClick={() => window.location.reload()}
        >
          Reload application
        </button>
      </main>
    ) : (
      this.props.children
    );
  }
}

export function App() {
  const [mobileOpen, setMobileOpen] = useState(false);
  const location = useLocation();
  const navigate = useNavigate();
  const main = useRef<HTMLElement>(null);
  useEffect(() => {
    setMobileOpen(false);
    main.current?.focus({ preventScroll: true });
    window.scrollTo({ top: 0, behavior: "instant" });
    const page =
      navigation.find((item) => item.to === location.pathname)?.label ??
      (location.pathname.startsWith("/people/") ? "Person" : "Collection");
    document.title = `${page} - Face Hunger`;
  }, [location.pathname]);
  useEffect(() => {
    const onKey = (event: KeyboardEvent) => {
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
      <aside className="sidebar">
        <Brand />
        <Navigation />
        <div className="sidebar-bottom">
          <div className="sidebar-note">
            <span>Less searching.</span>
            <em>More remembering.</em>
          </div>
          <JobCard compact />
        </div>
      </aside>
      <header className="mobile-header">
        <Brand />
        <button
          className="icon-button"
          aria-label="Open navigation"
          aria-expanded={mobileOpen}
          onClick={() => setMobileOpen(true)}
        >
          <Icon name="menu" size={24} />
        </button>
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
        <div className="topline">
          <span>
            <span className="status-dot active" />
            Your private collection
          </span>
          <Link to="/settings">
            <Icon name="shield" size={14} />
            On your device
          </Link>
        </div>
        <Routes>
          <Route path="/" element={<Home />} />
          <Route path="/people" element={<People />} />
          <Route path="/people/:id" element={<PersonProfile />} />
          <Route path="/clusters" element={<Clusters />} />
          <Route
            path="/photos"
            element={
              <>
                <PageHeader
                  eyebrow="THE MOMENTS THAT STAY"
                  title="Your photographs"
                  description="Big days, small details, and all the faces in between."
                />
                <MediaCollection
                  key="photos"
                  filters={{ kind: "photo" }}
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
                  description="Photos and videos where the engine found no faces. Browse them here separately from your people collection. Deleting here permanently removes the original files from disk."
                />
                <MediaCollection
                  key="no-faces"
                  filters={{ no_faces: true }}
                  purgeOriginals
                  emptyTitle="Every file has a face"
                  emptyDescription="When a scan finds media with no detectable faces, those items will appear here."
                />
              </>
            }
          />
          <Route path="/search" element={<Search />} />
          <Route path="/review" element={<Review />} />
          <Route path="/cleanup" element={<Cleanup />} />
          <Route path="/settings" element={<Settings />} />
          <Route
            path="*"
            element={
              <Empty
                icon="search"
                title="This room hasn't been found"
                description="That page isn't part of this collection."
              >
                <Link className="button primary" to="/">
                  Back to overview
                </Link>
              </Empty>
            }
          />
        </Routes>
        <footer className="page-footer">
          <span>
            Face Hunger <span aria-hidden="true">/</span> Your memories,
            locally.
          </span>
          <span>Originals always untouched.</span>
        </footer>
      </main>
    </>
  );
}
