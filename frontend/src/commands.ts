/** Command palette model: pure ranking so it can be unit tested. */
export interface Command {
  id: string;
  group: 'Go to' | 'People' | 'Search' | 'Saved searches' | 'Actions';
  label: string;
  hint?: string;
  keywords?: string;
  run: () => void;
}

/** Subsequence match score: contiguous and word-start hits rank higher; 0 = no match. */
export function score(text: string, query: string): number {
  const hay = text.toLowerCase();
  const needle = query.trim().toLowerCase();
  if (!needle) return 1;
  if (hay.startsWith(needle)) return 1000 - hay.length;
  const at = hay.indexOf(needle);
  if (at >= 0) return (hay[at - 1] === ' ' ? 800 : 600) - at;
  let pos = -1;
  let total = 0;
  for (const char of needle) {
    const next = hay.indexOf(char, pos + 1);
    if (next < 0) return 0;
    total += next === pos + 1 ? 3 : next === 0 || hay[next - 1] === ' ' ? 2 : 1;
    pos = next;
  }
  return total;
}

const GROUP_ORDER: Command['group'][] = ['Go to', 'People', 'Saved searches', 'Actions', 'Search'];

export function rankCommands(commands: Command[], query: string, limit = 40): Command[] {
  return commands
    .map((command, index) => ({ command, index, value: Math.max(score(command.label, query), score(command.keywords ?? '', query) * 0.8) }))
    .filter(entry => entry.value > 0 || entry.command.group === 'Search')
    .sort((a, b) => (query.trim() ? b.value - a.value : 0)
      || GROUP_ORDER.indexOf(a.command.group) - GROUP_ORDER.indexOf(b.command.group) || a.index - b.index)
    .slice(0, limit)
    .map(entry => entry.command);
}
