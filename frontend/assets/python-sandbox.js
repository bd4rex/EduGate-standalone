/* Trusted controller; student code runs in a disposable opaque-origin worker. */
(() => {
  "use strict";
  let worker = null;
  let activeId = null;
  const base = new URL("/assets/vendor/pyodide/", location.href).href;
  addEventListener("pagehide", () => worker?.terminate());
  const send = (data) => parent.postMessage({source: "edugate-python-sandbox", id: activeId, ...data}, "*");
  addEventListener("message", (event) => {
    if (event.source !== parent || event.data?.source !== "edugate-python-host" || activeId) return;
    const {id, code} = event.data;
    if (typeof id !== "string" || typeof code !== "string") return;
    activeId = id;
    const script = `
      const send = globalThis.postMessage.bind(globalThis);
      const length = {stdout: 0, stderr: 0};
      const emit = (stream, text) => {
        const content = String(text).slice(0, Math.max(0, 12000 - length[stream]));
        length[stream] += content.length;
        if (content) send({type: stream, content});
      };
      try {
        const {loadPyodide} = await import(${JSON.stringify(base + "pyodide.mjs")});
        const python = await loadPyodide({indexURL: ${JSON.stringify(base)}});
        const decoders = {stdout: new TextDecoder(), stderr: new TextDecoder()};
        const writer = stream => ({write(buffer) {
          emit(stream, decoders[stream].decode(buffer, {stream: true}));
          return buffer.length;
        }});
        python.setStdout(writer("stdout"));
        python.setStderr(writer("stderr"));
        python.setStdin({stdin: () => undefined});
        python.runPython('import io, sys\\nsys.stdout = io.TextIOWrapper(io.FileIO(1, "w", closefd=False), encoding="utf-8", write_through=True)\\nsys.stderr = io.TextIOWrapper(io.FileIO(2, "w", closefd=False), encoding="utf-8", write_through=True)');
        onmessage = async ({data}) => {
          try {
            const value = await python.runPythonAsync(data.code);
            value?.destroy?.();
            for (const stream of ["stdout", "stderr"]) emit(stream, decoders[stream].decode());
            send({type: "done", exit_code: 0});
          } catch (error) {
            for (const stream of ["stdout", "stderr"]) emit(stream, decoders[stream].decode());
            emit("stderr", String(error) + "\\n");
            send({type: "done", exit_code: 1});
          }
        };
        send({type: "ready"});
      } catch (error) {
        send({type: "load-error", message: String(error)});
      }
    `;
    const url = "data:text/javascript;charset=utf-8," + encodeURIComponent(script);
    worker = new Worker(url, {type: "module", credentials: "omit"});
    worker.onmessage = ({data}) => {
      if (data?.type === "ready") {
        send({type: "ready"});
        worker.postMessage({code});
      } else if (data?.type === "stdout" || data?.type === "stderr") {
        send({type: data.type, content: String(data.content || "").slice(0, 12000)});
      } else if (data?.type === "done") {
        send({type: "done", exit_code: data.exit_code === 0 ? 0 : 1});
        worker.terminate();
      } else if (data?.type === "load-error") {
        send({type: "load-error", message: String(data.message || "Python 初始化失败")});
        worker.terminate();
      }
    };
    worker.onerror = (error) => send({type: "load-error", message: error.message || "浏览器 Python 运行环境加载失败，请检查资源是否完整。"});
  });
  parent.postMessage({source: "edugate-python-sandbox", type: "loaded"}, "*");
})();
