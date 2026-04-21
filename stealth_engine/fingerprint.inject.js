/* FB Master Stealth Shell — инъекция в каждую страницу (Playwright add_init_script).
 * Не заменяет патчи в бинарнике Chromium (TLS/JA3, нативный WebRTC): см. chromium_fork/HOWTO.txt
 */
(() => {
  const C = window.__FB_STEALTH_PARAMS__ || {};
  const seed = (typeof C.seed === "number" ? C.seed : 0x9e3779b9) >>> 0;
  function mulberry32(a) {
    return function () {
      let t = (a += 0x6d2b79f5);
      t = Math.imul(t ^ (t >>> 15), t | 1);
      t ^= t + Math.imul(t ^ (t >>> 7), t | 61);
      return ((t ^ (t >>> 14)) >>> 0) / 4294967296;
    };
  }
  const rnd = mulberry32(seed);

  try {
    Object.defineProperty(navigator, "webdriver", { get: () => undefined, configurable: true });
  } catch (e) {}
  try {
    window.chrome = window.chrome || { runtime: {} };
  } catch (e) {}

  const hw = typeof C.hwConcurrency === "number" ? C.hwConcurrency : 8;
  const mem = typeof C.deviceMemory === "number" ? C.deviceMemory : 8;
  const touch = typeof C.maxTouchPoints === "number" ? C.maxTouchPoints : 0;
  const plat = typeof C.platform === "string" ? C.platform : "Win32";

  try {
    Object.defineProperty(navigator, "hardwareConcurrency", { get: () => hw, configurable: true });
  } catch (e) {}
  try {
    if ("deviceMemory" in navigator) {
      Object.defineProperty(navigator, "deviceMemory", { get: () => mem, configurable: true });
    }
  } catch (e) {}
  try {
    Object.defineProperty(navigator, "platform", { get: () => plat, configurable: true });
  } catch (e) {}
  try {
    Object.defineProperty(navigator, "maxTouchPoints", { get: () => touch, configurable: true });
  } catch (e) {}

  const vendorGl = C.webglVendor || "Google Inc. (NVIDIA)";
  const rendererGl = C.webglRenderer || "ANGLE (NVIDIA, NVIDIA GeForce GTX 1660 Direct3D11 vs_5_0 ps_5_0, D3D11)";

  function hookWebGL(proto) {
    if (!proto || !proto.getParameter) return;
    const orig = proto.getParameter;
    proto.getParameter = function (p) {
      try {
        if (p === 37445) return vendorGl;
        if (p === 37446) return rendererGl;
      } catch (e) {}
      return orig.apply(this, arguments);
    };
  }
  try {
    hookWebGL(WebGLRenderingContext && WebGLRenderingContext.prototype);
    hookWebGL(WebGL2RenderingContext && WebGL2RenderingContext.prototype);
  } catch (e) {}

  function noiseCanvasData(imageData) {
    const d = imageData.data;
    const n = Math.max(1, Math.min(12, Math.floor(3 + rnd() * 6)));
    for (let k = 0; k < n; k++) {
      const i = (Math.floor(rnd() * (d.length / 4)) * 4) % d.length;
      d[i] ^= 1;
    }
  }

  try {
    const CP = CanvasRenderingContext2D && CanvasRenderingContext2D.prototype;
    if (CP && CP.getImageData) {
      const origGet = CP.getImageData;
      CP.getImageData = function () {
        const ret = origGet.apply(this, arguments);
        try {
          const canvas = this.canvas;
          if (canvas && canvas.width > 16 && canvas.height > 16 && ret && ret.data) {
            noiseCanvasData(ret);
          }
        } catch (e) {}
        return ret;
      };
    }
  } catch (e) {}

  try {
    const proto = HTMLCanvasElement && HTMLCanvasElement.prototype;
    if (proto && proto.toDataURL) {
      const orig = proto.toDataURL;
      proto.toDataURL = function () {
        const ctx = this.getContext && this.getContext("2d");
        try {
          if (ctx && this.width > 8 && this.height > 8) {
            const w = this.width;
            const h = this.height;
            const img = ctx.getImageData(w - 3, h - 3, 2, 2);
            noiseCanvasData(img);
            ctx.putImageData(img, w - 3, h - 3);
          }
        } catch (e) {}
        return orig.apply(this, arguments);
      };
    }
  } catch (e) {}

  try {
    if (window.RTCPeerConnection) {
      const Orig = window.RTCPeerConnection;
      window.RTCPeerConnection = function (cfg, cons) {
        const c = cfg && typeof cfg === "object" ? Object.assign({}, cfg) : {};
        if (!c.iceServers) c.iceServers = [];
        return new Orig(c, cons);
      };
      window.RTCPeerConnection.prototype = Orig.prototype;
    }
  } catch (e) {}

  try {
    const conn = {
      downlink: 10,
      effectiveType: "4g",
      rtt: 50,
      saveData: false,
      addEventListener: () => {},
      removeEventListener: () => {},
      dispatchEvent: () => false,
    };
    if (navigator.connection) {
      Object.defineProperty(navigator, "connection", { get: () => conn, configurable: true });
    }
  } catch (e) {}
})();
