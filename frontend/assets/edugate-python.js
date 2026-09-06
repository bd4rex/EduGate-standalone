(function (global) {
  "use strict";
  let running = false;
  global.EduGatePython = Object.freeze({
    run(code, {baseUrl = location.origin, timeoutSeconds = 3, signal, onOutput = () => {}} = {}) {
      if (running) return Promise.reject(new Error("此页面已有 Python 任务正在运行。"));
      if (signal?.aborted) return Promise.reject(new DOMException("已停止运行", "AbortError"));
      running = true;
      return new Promise((resolve, reject) => {
        const frame = document.createElement("iframe");
        frame.hidden = true;
        frame.sandbox = "allow-scripts";
        frame.referrerPolicy = "no-referrer";
        frame.src = `${baseUrl}/python-sandbox.html`;
        const id = `python-${Date.now()}-${Math.random().toString(36).slice(2)}`;
        const output = {stdout: "", stderr: ""};
        let started = 0;
        let finished = false;
        let timer = setTimeout(() => finish(null, new Error("Python 加载超时，请检查本地运行环境文件。")), 60000);
        function finish(exitCode, error, timedOut = false) {
          if (finished) return;
          finished = true;
          clearTimeout(timer);
          removeEventListener("message", receive);
          signal?.removeEventListener("abort", abort);
          frame.remove();
          running = false;
          if (error) reject(error);
          else resolve({...output, exit_code: exitCode, timed_out: timedOut, duration_ms: started ? Math.round(performance.now() - started) : 0});
        }
        const abort = () => finish(null, new DOMException("已停止运行", "AbortError"));
        function receive(event) {
          if (event.source !== frame.contentWindow || event.data?.source !== "edugate-python-sandbox") return;
          const data = event.data;
          if (data.type === "loaded") {
            frame.contentWindow.postMessage({source: "edugate-python-host", id, code}, "*");
            return;
          }
          if (data.id !== id) return;
          if (data.type === "ready" && !started) {
            started = performance.now();
            clearTimeout(timer);
            timer = setTimeout(() => {
              output.stderr = (output.stderr + "\n程序运行超时，已停止学生浏览器中的执行。\n").slice(0, 12000);
              finish(124, null, true);
            }, Math.max(0.1, Math.min(60, timeoutSeconds)) * 1000);
          } else if (data.type === "stdout" || data.type === "stderr") {
            const content = String(data.content || "").slice(0, Math.max(0, 12000 - output[data.type].length));
            output[data.type] += content;
            if (content) { try { onOutput(data.type, content); } catch (_) {} }
          } else if (data.type === "done") {
            finish(data.exit_code === 0 ? 0 : 1);
          } else if (data.type === "load-error") {
            finish(null, new Error(String(data.message || "Python 加载失败")));
          }
        }
        addEventListener("message", receive);
        signal?.addEventListener("abort", abort, {once: true});
        document.body.append(frame);
      });
    },
  });
})(globalThis);
