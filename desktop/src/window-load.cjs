"use strict";

function isSameOriginNavigationAbort(win, requestedUrl, error) {
  const aborted =
    error?.code === "ERR_ABORTED" ||
    error?.errno === -3 ||
    /ERR_ABORTED\s*\(-3\)/i.test(String(error?.message || error || ""));
  if (!aborted) return false;

  let currentUrl = "";
  try {
    currentUrl = String(win?.webContents?.getURL?.() || "");
    if (!currentUrl) return false;
    return new URL(currentUrl).origin === new URL(requestedUrl).origin;
  } catch {
    return false;
  }
}

module.exports = {
  isSameOriginNavigationAbort,
};
