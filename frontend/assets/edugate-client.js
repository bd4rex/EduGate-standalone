(function (global) {
  "use strict";

  function normalizeBaseUrl(value) {
    return String(value || "").trim().replace(/\/+$/, "");
  }

  function randomId(prefix) {
    if (global.crypto && typeof global.crypto.randomUUID === "function") {
      return `${prefix}-${global.crypto.randomUUID()}`;
    }
    return `${prefix}-${Date.now()}-${Math.random().toString(36).slice(2)}`;
  }

  function safeStorage(kind) {
    try {
      const storage = global[kind];
      const key = "__edugate_storage_test__";
      storage.setItem(key, "1");
      storage.removeItem(key);
      return storage;
    } catch (_) {
      return null;
    }
  }

  async function responseError(response) {
    let detail = `HTTP ${response.status}`;
    try {
      const payload = await response.json();
      detail = payload.detail || payload.error?.message || detail;
    } catch (_) {
      const text = await response.text().catch(() => "");
      if (text) detail = text;
    }
    const error = new Error(String(detail));
    error.status = response.status;
    return error;
  }

  function parseEventBlock(block) {
    let event = "message";
    const data = [];
    for (const rawLine of block.split(/\r?\n/)) {
      const line = rawLine.trimEnd();
      if (!line || line.startsWith(":")) continue;
      if (line.startsWith("event:")) event = line.slice(6).trim();
      if (line.startsWith("data:")) data.push(line.slice(5).trimStart());
    }
    return { event, data: data.join("\n") };
  }

  class EduGateClient {
    constructor(options) {
      const config = options || {};
      this.baseUrl = normalizeBaseUrl(config.baseUrl || global.location?.origin);
      this.classToken = String(config.classToken || "").trim();
      this.scenarioId = String(config.scenarioId || "default").trim();
      this.computerName = String(config.computerName || "").trim();
      if (!this.baseUrl) throw new Error("EduGate baseUrl is required");
      if (!this.classToken) throw new Error("EduGate classToken is required");

      this.sessionStorage = safeStorage("sessionStorage");
      this.localStorage = safeStorage("localStorage");
      this.storageKey = `edugate:student:${this.baseUrl}:${this.classToken.slice(0, 12)}`;
      this.deviceKey = `edugate:device:${this.baseUrl}`;
      this.studentToken = config.studentToken || this.sessionStorage?.getItem(this.storageKey) || "";
      this.deviceId = config.deviceId || this.localStorage?.getItem(this.deviceKey) || randomId("browser");
      this.localStorage?.setItem(this.deviceKey, this.deviceId);
    }

    clearSession() {
      this.studentToken = "";
      this.sessionStorage?.removeItem(this.storageKey);
    }

    async join(options) {
      const config = options || {};
      const headers = {
        "Content-Type": "application/json",
        "X-Class-Token": this.classToken,
      };
      if (this.studentToken) headers["X-Student-Token"] = this.studentToken;
      const response = await fetch(`${this.baseUrl}/classroom/join`, {
        method: "POST",
        headers,
        signal: config.signal,
        body: JSON.stringify({
          device_id: this.deviceId,
          computer_name: config.computerName || this.computerName,
        }),
      });
      if (!response.ok) throw await responseError(response);
      const session = await response.json();
      this.studentToken = session.student_token;
      this.sessionStorage?.setItem(this.storageKey, this.studentToken);
      return session;
    }

    async ensureSession(options) {
      if (!this.studentToken) await this.join(options);
      return this.studentToken;
    }

    async runPython(code, options = {}) {
      await this.ensureSession(options);
      const getPolicy = () => fetch(`${this.baseUrl}/classroom/python-config`, {headers: {"X-Student-Token": this.studentToken}, signal: options.signal});
      let policyResponse = await getPolicy();
      if (policyResponse.status === 401) {
        this.clearSession();
        await this.join(options);
        policyResponse = await getPolicy();
      }
      const token = this.studentToken;
      const headers = {"X-Student-Token": token, "Content-Type": "application/json"};
      if (!policyResponse.ok) throw await responseError(policyResponse);
      const policy = await policyResponse.json();
      if (typeof code !== "string" || !code.trim() || code.length > policy.max_code_chars) throw new Error(`Python 代码需为 1–${policy.max_code_chars} 个字符。`);
      if (!global.EduGatePython) {
        if (!this.pythonLoader) this.pythonLoader = new Promise((resolve, reject) => {
          const script = document.createElement("script");
          script.src = `${this.baseUrl}/assets/edugate-python.js`;
          script.onload = resolve;
          script.onerror = () => { this.pythonLoader = null; reject(new Error("Python 客户端加载失败")); };
          document.head.append(script);
        });
        await this.pythonLoader;
      }
      const result = await global.EduGatePython.run(code, {...options, baseUrl: this.baseUrl, timeoutSeconds: policy.timeout_seconds});
      const saved = await fetch(`${this.baseUrl}/classroom/python-result`, {
        method: "POST", headers, signal: options.signal,
        body: JSON.stringify({...result, code, classroom_id: policy.classroom_id}),
      });
      if (!saved.ok) throw await responseError(saved);
      return result;
    }

    async *stream(messages, options) {
      const config = options || {};
      await this.ensureSession(config);
      let response = await this._streamRequest(messages, config);
      if (response.status === 401) {
        this.clearSession();
        await this.join(config);
        response = await this._streamRequest(messages, config);
      }
      if (!response.ok) throw await responseError(response);
      if (!response.body) throw new Error("This browser does not support streaming responses");

      const reader = response.body.getReader();
      const textDecoder = new TextDecoder();
      let buffer = "";
      try {
        while (true) {
          const { value, done } = await reader.read();
          buffer += textDecoder.decode(value || new Uint8Array(), { stream: !done });
          const blocks = buffer.split(/\r?\n\r?\n/);
          buffer = blocks.pop() || "";
          for (const block of blocks) {
            const parsed = parseEventBlock(block);
            if (!parsed.data) continue;
            if (parsed.data === "[DONE]") return;
            let payload;
            try {
              payload = JSON.parse(parsed.data);
            } catch (_) {
              continue;
            }
            if (parsed.event === "error") {
              const error = new Error(String(payload.detail || "EduGate stream failed"));
              error.status = payload.status_code || 502;
              throw error;
            }
            yield payload;
          }
          if (done) break;
        }
      } finally {
        reader.releaseLock();
      }
    }

    async *streamText(messages, options) {
      for await (const chunk of this.stream(messages, options)) {
        const choice = (chunk.choices || [])[0] || {};
        const content = choice.delta?.content ?? choice.message?.content ?? choice.text;
        if (typeof content === "string" && content) yield content;
      }
    }

    _streamRequest(messages, options) {
      return fetch(`${this.baseUrl}/chat/stream`, {
        method: "POST",
        headers: {
          "Content-Type": "application/json",
          Accept: "text/event-stream",
          "X-Student-Token": this.studentToken,
        },
        signal: options.signal,
        body: JSON.stringify({
          messages,
          scenario_id: options.scenarioId || this.scenarioId,
        }),
      });
    }
  }

  global.EduGateClient = EduGateClient;
})(globalThis);
