const test = require("node:test");
const assert = require("node:assert/strict");

const { isSameOriginNavigationAbort } = require("../src/window-load.cjs");

function windowAt(url) {
  return { webContents: { getURL: () => url } };
}

test("first-run agreement redirect is accepted as a same-origin navigation abort", () => {
  assert.equal(
    isSameOriginNavigationAbort(
      windowAt("http://127.0.0.1:3000/"),
      "http://127.0.0.1:3000",
      new Error("ERR_ABORTED (-3) loading 'http://127.0.0.1:3000/agreement?redirect=%2F'")
    ),
    true
  );
});

test("network failures and cross-origin aborts remain failures", () => {
  assert.equal(
    isSameOriginNavigationAbort(
      windowAt("http://127.0.0.1:3000/"),
      "http://127.0.0.1:3000",
      Object.assign(new Error("connection refused"), { code: "ERR_CONNECTION_REFUSED" })
    ),
    false
  );
  assert.equal(
    isSameOriginNavigationAbort(
      windowAt("https://example.invalid/agreement"),
      "http://127.0.0.1:3000",
      Object.assign(new Error("aborted"), { code: "ERR_ABORTED" })
    ),
    false
  );
});
