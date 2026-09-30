/** A bounded LIFO of undoable actions. UI-agnostic so it can be unit tested. */
export interface UndoEntry {
  id: number;
  label: string;
  undo: () => Promise<unknown> | unknown;
}

export class UndoStack {
  private entries: UndoEntry[] = [];
  private nextId = 1;
  private listeners = new Set<() => void>();
  constructor(private readonly limit = 50) {}

  push(label: string, undo: UndoEntry['undo']): UndoEntry {
    const entry = { id: this.nextId++, label, undo };
    this.entries = [...this.entries.slice(-(this.limit - 1)), entry];
    this.emit();
    return entry;
  }

  peek(): UndoEntry | undefined {
    return this.entries[this.entries.length - 1];
  }

  get size(): number {
    return this.entries.length;
  }

  /** Undo a specific entry (default: latest). Returns its label, or null if nothing to undo. */
  async undo(id?: number): Promise<string | null> {
    const index = id === undefined ? this.entries.length - 1 : this.entries.findIndex(e => e.id === id);
    if (index < 0) return null;
    const [entry] = this.entries.splice(index, 1);
    this.emit();
    try {
      await entry.undo();
    } catch (error) {
      // Put it back so the user can retry.
      this.entries.splice(index, 0, entry);
      this.emit();
      throw error;
    }
    return entry.label;
  }

  subscribe(listener: () => void): () => void {
    this.listeners.add(listener);
    return () => this.listeners.delete(listener);
  }

  private emit() {
    this.listeners.forEach(listener => listener());
  }
}
