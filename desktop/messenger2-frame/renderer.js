'use strict';

(function () {
  var wrap = document.getElementById('webview-wrap');
  var list = document.getElementById('accounts');
  var extBtn = document.getElementById('external-open');
  var currentWv = null;
  var currentChromeDir = null;

  function mountWebview(partition, src) {
    if (currentWv) {
      currentWv.remove();
      currentWv = null;
    }
    var wv = document.createElement('webview');
    wv.setAttribute('partition', partition);
    wv.setAttribute('src', src);
    wv.setAttribute('allowpopups', 'on');
    wv.style.display = 'flex';
    wv.style.width = '100%';
    wv.style.height = '100%';
    wrap.appendChild(wv);
    currentWv = wv;
  }

  function setExternalButton(dir) {
    currentChromeDir = dir || null;
    if (!extBtn) return;
    if (dir) {
      extBtn.style.display = 'inline-block';
      extBtn.onclick = function () {
        window.m2api.spawnChrome(dir).then(function (r) {
          if (!r.ok && r.error) {
            alert(r.error);
          }
        });
      };
    } else {
      extBtn.style.display = 'none';
      extBtn.onclick = null;
    }
  }

  window.m2api.getConfig().then(function (cfg) {
    var url = (cfg && cfg.messengerUrl) || 'https://www.facebook.com/messages/e2ee/t';
    var accounts = (cfg && cfg.accounts) || [];
    if (!accounts.length) {
      list.innerHTML = '<p style="padding:8px 12px;font-size:13px;color:#65676b">Добавьте аккаунты в accounts.json</p>';
      return;
    }

    accounts.forEach(function (acc, idx) {
      var btn = document.createElement('button');
      btn.type = 'button';
      btn.className = 'acc-item' + (idx === 0 ? ' active' : '');
      btn.textContent = acc.label || acc.id || ('Аккаунт ' + (idx + 1));
      btn.addEventListener('click', function () {
        var all = list.querySelectorAll('.acc-item');
        for (var i = 0; i < all.length; i++) all[i].classList.remove('active');
        btn.classList.add('active');
        var part = 'persist:m2-' + String(acc.id || idx);
        mountWebview(part, url);
        setExternalButton(acc.chromeUserDataDir || null);
      });
      list.appendChild(btn);
    });

    var a0 = accounts[0];
    mountWebview('persist:m2-' + String(a0.id || 0), url);
    setExternalButton(a0.chromeUserDataDir || null);
  });
})();
