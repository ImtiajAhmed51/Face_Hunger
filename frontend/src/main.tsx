import { StrictMode } from 'react';
import { createRoot } from 'react-dom/client';
import { HashRouter } from 'react-router-dom';
import { App, ErrorBoundary } from './App';
import { AppProvider } from './context';
import { LockGate } from './components/AppLock';
import './styles/index.css';
import './appearance.css';

createRoot(document.getElementById('root')!).render(
  <StrictMode><ErrorBoundary><HashRouter><AppProvider><LockGate><App /></LockGate></AppProvider></HashRouter></ErrorBoundary></StrictMode>,
);
