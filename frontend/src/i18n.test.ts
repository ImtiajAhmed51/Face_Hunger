import { readFileSync } from 'node:fs';
import ts from 'typescript';
import { describe, expect, it } from 'vitest';
import { MESSAGES, type MessageKey } from './i18n';

// Screens added in Phase 2: every user-visible string must come from i18n.
const NEW_SCREENS = [
  'pages/Timeline.tsx', 'pages/MapPage.tsx', 'pages/Events.tsx', 'pages/Albums.tsx',
  'components/Memories.tsx', 'components/VideoInsights.tsx', 'components/VideoMoments.tsx',
  'components/QualityPanel.tsx', 'components/PhotoEdits.tsx', 'components/ShareDialog.tsx', 'components/Packages.tsx', 'pages/Storage.tsx', 'pages/Plugins.tsx', 'pages/Diagnostics.tsx', 'components/OrganizeActions.tsx', 'components/DuplicateResolver.tsx',
];

const KEY_NAMES = new Set(['Tab', 'Enter', 'Esc', 'Shift', 'Space']);
const CHECKED_ATTRS = new Set(['aria-label', 'title', 'placeholder', 'alt', 'label']);

/** JSX text nodes and string-literal user-facing attributes, found with the TypeScript parser. */
function literals(file: string, source: string): string[] {
  const found: string[] = [];
  const sf = ts.createSourceFile(file, source, ts.ScriptTarget.Latest, true, ts.ScriptKind.TSX);
  const visit = (node: ts.Node) => {
    if (ts.isJsxText(node)) {
      const text = node.getText(sf).trim();
      const parent = node.parent;
      const inKbd = ts.isJsxElement(parent) && parent.openingElement.tagName.getText(sf) === 'kbd';
      // Physical key names on <kbd> keycaps are not translated.
      if (/[A-Za-z]{2,}/.test(text) && !(inKbd && KEY_NAMES.has(text))) found.push(text);
    } else if (ts.isJsxAttribute(node) && node.initializer && ts.isStringLiteral(node.initializer)) {
      const name = node.name.getText(sf);
      if (CHECKED_ATTRS.has(name) && /[A-Za-z]{2,}/.test(node.initializer.text)) found.push(`${name}="${node.initializer.text}"`);
    }
    ts.forEachChild(node, visit);
  };
  visit(sf);
  return found;
}

describe('i18n coverage', () => {
  it.each(NEW_SCREENS)('%s has no hard-coded English', (file) => {
    const source = readFileSync(new URL(file, import.meta.url), 'utf8');
    expect(literals(file, source)).toEqual([]);
  });
  it('every key exists in Bangla and uses the same placeholders', () => {
    for (const key of Object.keys(MESSAGES.en) as MessageKey[]) {
      const bn = MESSAGES.bn[key];
      expect(bn, key).toBeTruthy();
      const vars = (s: string) => [...s.matchAll(/\{(\w+)\}/g)].map((m) => m[1]).sort();
      expect(vars(bn!), key).toEqual(vars(MESSAGES.en[key]!));
    }
  });
});
