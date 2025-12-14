<p align="center">
  <b>xontrib-looseene</b><br>
  A lightning-fast, compressed, inverted-index history backend for <a href="https://xon.sh">xonsh shell</a>.
</p>

<p align="center">
<a href="https://github.com/Hammer2900/xontrib-looseene/blob/main/LICENSE"><img src="https://img.shields.io/github/license/Hammer2900/xontrib-looseene" alt="License"></a>
<a href="https://pypi.org/project/xontrib-looseene/"><img src="https://img.shields.io/pypi/v/xontrib-looseene" alt="PyPI"></a>
</p>

**Looseene** is a specialized history backend for xonsh that acts like a mini-search engine for your terminal. Unlike standard history (which scans text files), Looseene builds an inverted index using `mmap`, `zlib`, and `struct`, enabling instant search results even with massive history logs.

### 🚀 Features

*   **Full-Text Search:** Uses **BM25** ranking algorithm to find the most relevant commands, not just the most recent.
*   **Interactive UI:** Built-in TUI (Terminal User Interface) triggered by `Ctrl+R`.
*   **Smart Deduplication:** Automatically hashes commands (MD5) and ignores duplicates. Your history stays clean.
*   **Compression:** Data is stored in compressed binary segments (`zlib`), saving disk space.
*   **Safe Stemming:** Includes a lightweight English stemmer (e.g., searching for `commit` will also find `committed`).
*   **Pure Python:** No C-extensions or heavy dependencies (like ElasticSearch). It just works.

## 📦 Installation

### Method 1: Standard xpip (Recommended)
Open xonsh and run:
```xsh
xpip install xontrib-history-looseene
```

### Method 2: For Pipx / Unix-managed environments
If you installed xonsh via `pipx` or your system prevents direct pip usage, use this command to inject the plugin:

```bash
# If using pipx:
pipx inject xonsh xontrib-looseene

# Or using the raw subprocess method (if pipx is not available):
xonsh -c "import sys, subprocess; subprocess.check_call([sys.executable, '-m', 'pip', 'install', 'xontrib-looseene'])"
```

## ⚙️ Configuration

To activate the backend, add this line to your `.xonshrc` (usually located at `~/.xonshrc` or `~/.config/xonsh/rc.xsh`):

```python
xontrib load history_looseene
```

Restart your shell. You should see a message `Looseene: History backend loaded`.

## ⌨️ Usage

### Interactive Search
Press **`Ctrl+R`** to open the interactive search window.
*   **Type** to search.
*   **Up/Down** arrows to navigate results.
*   **Enter** to select the command and place it on your command line.

### CLI Search
You can also search from the command line without the UI:
```xsh
hsearch "docker run"
# or alias:
hs "git commit"
```

### Maintenance (Compaction)
Looseene stores history in small "segments" on disk to ensure fast writing. Over time, these files can accumulate. To optimize performance and merge segments into one file:

```xsh
history-compact
```
*Recommendation: Run this once a week or if you notice the history folder (`~/.xonsh/history_search_db`) getting crowded.*

## 🛠 Technical Details

*   **Storage:** `~/.local/share/xonsh/looseene_history`
*   **Index Structure:** Inverted index with delta-encoded postings lists.
*   **Backend:** Custom implementation inheriting from `xonsh.history.base.History`.

## 🤝 Contributing

Contributions are welcome!
1.  Fork the repo.
2.  Install in editable mode: `xpip install -e .`
3.  Submit a Pull Request.

## 📄 License

MIT License.
