/**
 * Идентификатор для привязки ключа: стабильный отпечаток устройства (не случайный UUID на профиль).
 * В приложении FB Master сервер берёт отпечаток с железа из заголовка X-FB-Master-Device-Id.
 */
(function (global) {
  'use strict';

  var LEGACY_KEY = 'fbm_device_fingerprint_v1';
  var HW_KEY = 'fbm_device_fp_hw_v1';

  function bufToHex(buf) {
    return Array.from(new Uint8Array(buf))
      .map(function (b) {
        return b.toString(16).padStart(2, '0');
      })
      .join('');
  }

  async function computeHardwareLikeFingerprint() {
    var parts = [
      String(navigator.hardwareConcurrency || ''),
      String(navigator.deviceMemory || ''),
      String(navigator.maxTouchPoints != null ? navigator.maxTouchPoints : ''),
      String(screen.width),
      String(screen.height),
      String(screen.colorDepth),
      String(screen.availWidth),
      String(screen.availHeight),
      String(new Intl.DateTimeFormat().resolvedOptions().timeZone || ''),
      String((navigator.languages || []).join(',')),
      String(navigator.platform || ''),
    ];
    try {
      var canvas = document.createElement('canvas');
      var ctx = canvas.getContext('2d');
      if (ctx) {
        ctx.textBaseline = 'top';
        ctx.font = '14px system-ui,sans-serif,Arial';
        ctx.fillText('fbm|device|fp|v1', 2, 2);
        parts.push(canvas.toDataURL());
      }
    } catch (_e) {
      parts.push('');
    }
    try {
      var c2 = document.createElement('canvas');
      var gl = c2.getContext('webgl') || c2.getContext('experimental-webgl');
      if (gl) {
        var ext = gl.getExtension('WEBGL_debug_renderer_info');
        if (ext) {
          parts.push(String(gl.getParameter(ext.UNMASKED_VENDOR_WEBGL) || ''));
          parts.push(String(gl.getParameter(ext.UNMASKED_RENDERER_WEBGL) || ''));
        }
      }
    } catch (_e2) {
      parts.push('');
    }
    var s = parts.join('|');
    if (!global.crypto || !global.crypto.subtle) {
      return null;
    }
    var enc = new TextEncoder().encode(s);
    var digest = await global.crypto.subtle.digest('SHA-256', enc);
    return bufToHex(digest);
  }

  function randomFallback() {
    if (global.crypto && global.crypto.randomUUID) {
      return global.crypto.randomUUID();
    }
    return String(Date.now()) + Math.random().toString(36).slice(2);
  }

  global.fbmEnsureDeviceId = async function () {
    try {
      var legacy = global.localStorage.getItem(LEGACY_KEY);
      if (legacy && legacy.length >= 16) {
        return legacy;
      }
      var cached = global.localStorage.getItem(HW_KEY);
      if (cached && /^[a-f0-9]{64}$/i.test(cached)) {
        return cached;
      }
      var hw = await computeHardwareLikeFingerprint();
      if (!hw || hw.length < 32) {
        var fb = randomFallback();
        global.localStorage.setItem(LEGACY_KEY, fb);
        return fb;
      }
      global.localStorage.setItem(HW_KEY, hw);
      return hw;
    } catch (_e) {
      return randomFallback();
    }
  };
})(typeof window !== 'undefined' ? window : this);
