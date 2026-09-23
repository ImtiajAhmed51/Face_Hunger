import { StrictMode } from 'react';
import { createRoot } from 'react-dom/client';
import { HashRouter } from 'react-router-dom';
import { App, ErrorBoundary } from './App';
import { AppProvider } from './context';
import './styles.css';
import './appearance.css';

createRoot(document.getElementById('root')!).render(
  <StrictMode><ErrorBoundary><HashRouter><AppProvider><App /></AppProvider></HashRouter></ErrorBoundary></StrictMode>,
);
