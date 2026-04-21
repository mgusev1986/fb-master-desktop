'use strict';

/**
 * Нотаризация Apple после codesign (только macOS).
 * Запускается electron-builder как afterSign; без APPLE_* переменных — просто выходит.
 */
exports.default = async function afterSign(context) {
  if (context.electronPlatformName !== 'darwin') {
    return;
  }
  if (process.env.SKIP_NOTARIZE === '1') {
    console.log('[afterSign] SKIP_NOTARIZE=1 — нотаризация отключена.');
    return;
  }
  const appleId = (process.env.APPLE_ID || '').trim();
  const appleIdPassword = (process.env.APPLE_APP_SPECIFIC_PASSWORD || '').trim();
  const teamId = (process.env.APPLE_TEAM_ID || '').trim();
  if (!appleId || !appleIdPassword || !teamId) {
    console.log(
      '[afterSign] Нотаризация пропущена: задайте APPLE_ID, APPLE_APP_SPECIFIC_PASSWORD, APPLE_TEAM_ID (см. SIGNING.md).'
    );
    return;
  }

  const { notarize } = require('@electron/notarize');
  const appName = context.packager.appInfo.productFilename;
  const appPath = `${context.appOutDir}/${appName}.app`;

  console.log('[afterSign] Нотаризация:', appPath);
  await notarize({
    appBundleId: context.packager.appInfo.appId,
    appPath,
    appleId,
    appleIdPassword,
    teamId,
  });
  console.log('[afterSign] Нотаризация завершена.');
};
