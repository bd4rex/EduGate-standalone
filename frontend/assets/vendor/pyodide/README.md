# Offline browser Python runtime

Pinned Pyodide version: **314.0.6**. Its lockfile identifies CPython **3.14.2**, wasm32 / Emscripten 5.0.3. This is independent of EduGate's Python 3.10+ backend requirement.

The five runtime files are unmodified distributions from [Pyodide's versioned CDN](https://cdn.jsdelivr.net/pyodide/v314.0.6/full/). They are served locally during lessons; EduGate does not download packages from the Internet at runtime. `SHA256SUMS` records the expected bytes. Do not replace individual files with a different release or change line endings.

Source and notices:

- [Pyodide 314.0.6 source](https://github.com/pyodide/pyodide/tree/314.0.6), Mozilla Public License 2.0, reproduced in `LICENSE`.
- [CPython 3.14.2 source](https://github.com/python/cpython/tree/v3.14.2), license and incorporated notices reproduced in `LICENSE-CPYTHON`.
- [Emscripten 5.0.3 source](https://github.com/emscripten-core/emscripten/tree/5.0.3), license and included notices reproduced in `LICENSE-EMSCRIPTEN`.
- [Pyodide deployment documentation](https://pyodide.org/en/314.0.6/usage/downloading-and-deploying.html) and [stream handling](https://pyodide.org/en/314.0.6/usage/streams.html).

EduGate's own wrappers live outside this directory: `edugate-python.js` and `python-sandbox.js`. They create an opaque-origin iframe and a disposable Worker, stream UTF-8 output, and terminate it on timeout/cancel. Only these five runtime resources have the public cross-origin response needed by the opaque Worker; teacher APIs do not.

Only the runtime and standard library are bundled. The lockfile describes additional upstream packages, but those packages are not installed or downloaded by EduGate. Update the complete set, checksums, notices, and browser regression tests together.
