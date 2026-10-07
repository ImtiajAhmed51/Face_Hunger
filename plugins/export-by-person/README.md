# Export to folders by person (reference plugin)

Shows the **export target** and **UI panel** extension points.

- `plan(items, options)` returns a relative path per file (`Person/Year/name.jpg`). The app checks
  every path (no `..`, nothing absolute, nothing outside the chosen folder) and copies the files
  itself. Existing files are never overwritten.
- It asks for one permission, **library.read**, to see people's names. If you do not grant it, the
  export still works and folders are called `person-12`.
- `panel/index.html` is shown in a sandboxed frame on the Plugins page and talks to the app only
  through `postMessage` (`host.info`, `library.summary`, `library.people`).
